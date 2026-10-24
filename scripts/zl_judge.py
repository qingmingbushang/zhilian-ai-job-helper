#!/usr/bin/env python3
"""
模块 D（智联版）: JD 语义判断 —— 只判断，不投递

消费 user_profile.json 的判断字段（全部经 USER_PROFILE.get() 读取，
用户改配置不改代码）:

    job_directions            方向短语，命中任一加分
    must_have_keywords        正向锚点，一个都没命中 → 硬 REJECT
    reject_keywords           反向词，命中后按下面两个字段处理
    reject_strict             true=命中直接 REJECT; false=只降权
    reject_keyword_weight     降权力度 light/medium/heavy
    min_salary_k              底薪下限（salary_max_k 有值且低于它 → REJECT）
    min_degree / acceptable_degrees   学历过滤
    downgrade_company_patterns        公司名命中则降权（外包/代招）

判定流程: 先硬规则，再打分
    硬规则:
      1. reject_strict 且命中 reject 词            → REJECT
      2. must_have 零命中                          → REJECT
      3. 学历标注不在 acceptable_degrees 白名单     → REJECT
      4. salary_max_k 有值且 < min_salary_k        → REJECT
    打分（score）:
      基础 60
      + 方向命中 25（job_directions 任一出现在标题/正文）
      + must_have 附加命中 每个 +10（封顶 +25）
      - reject 词命中 × 权重（light 25 / medium 40 / heavy 55）
      - 公司降权 20（downgrade_company_patterns 命中）
    verdict: score >= 75 → APPROVE; 55 ~ 74 → DOWNGRADE; < 55 → REJECT

独立运行:
    python zl_judge.py --card-idx idx0        # 点卡读面板并判断
    python zl_judge.py --jd-file jd.json      # 判断已落盘的 JD
"""

import json
import os
import re
import sys

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL_DIR)

from scripts.profile_loader import USER_PROFILE


def _get(key, default):
    """统一从 profile 取值：上层永远写 get(字段名, 默认值)，不硬编码"""
    return USER_PROFILE.get(key, default)


# 降权力度 → 扣分值。写进代码是因为它标定的是打分尺度，
# 用户通过 reject_keyword_weight 的三个合法值来选，不需要改这里。
REJECT_PENALTY = {"light": 25, "medium": 40, "heavy": 55}

COMPANY_PENALTY = 20
DIRECTION_BONUS = 25
MUST_HAVE_BONUS = 10
MUST_HAVE_BONUS_CAP = 25
BASE_SCORE = 60
SCORE_APPROVE = 75
SCORE_DOWNGRADE = 55


def judge_jd(jd, profile=None):
    """对单条 JD 做语义判断

    参数:
        jd: zl_jd_reader 产出的 JD dict（至少含 job_title/jd_text/company/
            salary；含 full_text/tags/salary_max_k/degree 时判断更准）
        profile: 覆盖全局 USER_PROFILE（测试用），None 则用真实配置

    返回:
        {"verdict": APPROVE|DOWNGRADE|REJECT,
         "score": int, "reasons": [str],
         "hits": {must_have/reject/direction/company: [...]}}
    """
    p = profile if profile is not None else USER_PROFILE
    title = str(jd.get("job_title") or "")
    body = " ".join(filter(None, [
        title,
        str(jd.get("jd_text") or ""),
        str(jd.get("full_text") or ""),
        " ".join(jd.get("tags") or []),
    ]))
    company = str(jd.get("company") or "")
    reasons, hits = [], {"must_have": [], "reject": [], "direction": [], "company": []}

    # ---- 词命中（大小写不敏感，"C++"这类符号词原样匹配）----
    tl, bl, cl = title.lower(), body.lower(), company.lower()
    for kw in p.get("must_have_keywords", []):
        if kw.lower() in bl:
            hits["must_have"].append(kw)
    for kw in p.get("reject_keywords", []):
        # reject 词在标题里才致命；正文顺带提到只算弱信号 → 标题命中记双份
        if kw.lower() in tl:
            hits["reject"].append(kw + "(标题)")
        elif kw.lower() in bl:
            hits["reject"].append(kw + "(正文)")
    for kw in p.get("job_directions", []):
        if kw.lower() in bl:
            hits["direction"].append(kw)
    for pat in p.get("downgrade_company_patterns", []):
        if pat.lower() in cl:
            hits["company"].append(pat)

    # ---- 硬规则 ----
    reject_strict = p.get("reject_strict", False)
    if reject_strict and hits["reject"]:
        reasons.append(f"reject_strict=true 且命中排除词: {hits['reject']}")
        return {"verdict": "REJECT", "score": 0, "reasons": reasons, "hits": hits}

    if not hits["must_have"]:
        reasons.append(f"必备词零命中（{p.get('must_have_keywords', [])} 里无一出现），与求职方向不符")
        return {"verdict": "REJECT", "score": 0, "reasons": reasons, "hits": hits}

    degree = str(jd.get("degree") or "")
    acceptable = [str(d) for d in p.get("acceptable_degrees", [])]
    if degree and acceptable and degree not in acceptable:
        reasons.append(f"学历要求 {degree} 不在可接受范围 {acceptable}")
        return {"verdict": "REJECT", "score": 0, "reasons": reasons, "hits": hits}

    from scripts.zhaopin_client import parse_salary_max
    salary = str(jd.get("salary") or "")
    # 日薪/时薪/周薪无法与月薪底薪比较（parse_salary_max 对其返回 0 会跳过检查），
    # 按 daily_wage_policy 决定：reject=视为不达标；skip=沿用旧行为
    if str(p.get("daily_wage_policy", "reject")).lower() == "reject" and \
            re.search(r"元\s*/\s*(天|日|时|小时|周|次)", salary):
        reasons.append(f"薪资为日薪/时薪口径（{salary}），按 daily_wage_policy=reject 视为不达标")
        return {"verdict": "REJECT", "score": 0, "reasons": reasons, "hits": hits}
    salary_max_k = jd.get("salary_max_k")
    if salary_max_k is None:
        salary_max_k = parse_salary_max(salary)
    min_salary_k = p.get("min_salary_k", 0)
    if salary_max_k and salary_max_k < min_salary_k:
        reasons.append(f"薪资上限 {salary_max_k}K 低于底薪要求 {min_salary_k}K")
        return {"verdict": "REJECT", "score": 0, "reasons": reasons, "hits": hits}

    # ---- 打分 ----
    score = BASE_SCORE
    if hits["direction"]:
        score += DIRECTION_BONUS
        reasons.append(f"方向命中: {hits['direction']} (+{DIRECTION_BONUS})")
    else:
        reasons.append("方向词未命中（job_directions 无一出现）")
    extra = min(MUST_HAVE_BONUS_CAP, MUST_HAVE_BONUS * (len(hits["must_have"]) - 1))
    if extra:
        score += extra
    reasons.append(f"必备词命中: {hits['must_have']} (+10 起, 附加封顶 +{MUST_HAVE_BONUS_CAP})")

    weight_t = str(p.get("reject_title_weight",
                         p.get("reject_keyword_weight", "medium"))).lower()
    weight_b = str(p.get("reject_body_weight", "light")).lower()
    if hits["reject"]:
        title_hits = [h for h in hits["reject"] if h.endswith("(标题)")]
        body_hits = [h for h in hits["reject"] if h.endswith("(正文)")]
        if title_hits:
            penalty = REJECT_PENALTY.get(weight_t, REJECT_PENALTY["medium"])
            score -= penalty
            reasons.append(f"标题命中排除词 {title_hits}，按 {weight_t} 档降权 (-{penalty})")
        if body_hits:
            penalty = REJECT_PENALTY.get(weight_b, REJECT_PENALTY["light"])
            score -= penalty
            reasons.append(f"正文命中排除词 {body_hits}，按 {weight_b} 档降权 (-{penalty})")

    if hits["company"]:
        score -= COMPANY_PENALTY
        reasons.append(f"公司名命中降权模式 {hits['company']}（外包/代招，降序不排除） (-{COMPANY_PENALTY})")

    score = max(0, min(100, score))
    verdict = "APPROVE" if score >= SCORE_APPROVE else (
        "DOWNGRADE" if score >= SCORE_DOWNGRADE else "REJECT")

    # 降权不排除：标题未命中排除词的岗位，软性扣分（正文提及/公司降权/方向未命中）
    # 最多降到 DOWNGRADE，不允许把"对口但正文顺带提过排除词"的岗打成 REJECT。
    # 真正的 REJECT 只能来自: 硬规则 / reject_strict / 标题排除词把分拉穿。
    title_rejects = [h for h in hits["reject"] if h.endswith("(标题)")]
    if verdict == "REJECT" and not title_rejects:
        verdict = "DOWNGRADE"
        reasons.append("标题无排除词命中，按「降权不排除」策略保底为 DOWNGRADE（正文提及不阻断保护）")
    reasons.append(f"总分 {score} → {verdict}（≥{SCORE_APPROVE} 过 / ≥{SCORE_DOWNGRADE} 降权 / 以下拒）")
    return {"verdict": verdict, "score": score, "reasons": reasons, "hits": hits}


# ═══════════════════════════════════════════════════════════
# 批量判断（断点续跑）
# ═══════════════════════════════════════════════════════════

def _load_results(out_file):
    """读已有结果文件；断点 = 里面已判过的 idx 集合"""
    if not os.path.exists(out_file):
        return []
    try:
        with open(out_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"[批量] 结果文件损坏({e})，请手动处理 {out_file} 后重试")
        sys.exit(1)


def _save_results(out_file, results):
    tmp = out_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    os.replace(tmp, out_file)     # 原子替换，断电也不损文件


def _live_card_title(idx):
    """取页面上第 idx 张卡片的标题，用于校验页面状态没漂移"""
    from scripts.zhaopin_client import evaluate
    js = """
    (function(){
        var c = document.querySelectorAll('.job-card')[__I__];
        if (!c) return '';
        var t = c.querySelector('.job-card__title-main');
        return t ? (t.innerText || '').trim() : '';
    })()
    """.replace("__I__", str(idx))
    return (evaluate(js) or "").strip()


def run_batch(cache_file="zl_surface_cache.json", out_file="zl_judge_full_results.json",
              limit=None, fresh=False):
    """全量判断缓存里的岗位，断点续跑

    断点机制: out_file 里已有 idx 直接跳过；每判完一条立即原子落盘，
    中断(Ctrl+C/任务停止/浏览器断开)后重跑同一命令即从断点继续。

    页面一致性: 判断前核对第 idx 张卡片的标题与缓存是否一致，
    不一致说明页面被刷新/换词/重排，立即中止（防止张冠李戴）。

    参数:
        limit: 最多新判多少条（None=判到完）
        fresh: True 则忽略已有结果重新开始
    返回: {"done": 本次新判条数, "skipped": 跳过条数, "total": 结果总条数}
    """
    from scripts.zl_jd_reader import read_jd_for_job

    if not os.path.exists(cache_file):
        print(f"[批量] 缓存不存在: {cache_file}")
        sys.exit(1)
    with open(cache_file, "r", encoding="utf-8") as f:
        cache = json.load(f)

    results = [] if fresh else _load_results(out_file)
    done = {r["idx"] for r in results}
    print(f"[批量] 缓存 {len(cache)} 条 | 已判 {len(done)} 条 | "
          f"{'全新开始' if fresh else '断点续跑'}")

    new_count = skipped = 0
    try:
        for job in cache:
            idx = job.get("page_order")
            if idx in done:
                skipped += 1
                continue
            if limit is not None and new_count >= limit:
                print(f"[批量] 达到本次上限 {limit} 条，停止")
                break

            # 页面一致性校验：卡片标题必须和缓存对得上
            live = _live_card_title(idx)
            cached_title = str(job.get("title") or "").strip()
            if not live or (live not in cached_title and cached_title[:10] not in live):
                print(f"[批量] 页面状态校验失败 @ idx{idx}: "
                      f"缓存='{cached_title[:20]}' vs 页面='{live[:20]}'。"
                      f"页面可能被刷新/换词，请重抓列表后再跑")
                break

            r = read_jd_for_job(f"idx{idx}")
            if not r.get("ok"):
                rec = {"idx": idx, "jobId": "", "title": cached_title,
                       "company": job.get("company", ""), "salary": job.get("salary", ""),
                       "verdict": "READ_FAIL", "score": -1,
                       "reasons": [str(r.get("detail"))], "hits": {}}
            else:
                jd = r["jd"]
                v = judge_jd(jd)
                rec = {"idx": idx, "jobId": r.get("job_id_real", ""),
                       "title": (jd.get("job_title") or cached_title)[:60],
                       "company": jd.get("company", ""), "salary": jd.get("salary", ""),
                       "verdict": v["verdict"], "score": v["score"],
                       "reasons": v["reasons"], "hits": v["hits"]}

            results.append(rec)
            _save_results(out_file, results)
            new_count += 1
            print(f"[{new_count:3d}/idx{idx:<4d}] {rec['verdict']:9s} "
                  f"{rec['score']:3d} | {rec['title'][:26]}")
    except KeyboardInterrupt:
        print("\n[批量] 手动中断，进度已落盘，重跑同一命令即续")

    print(f"[批量] 本次新判 {new_count} / 跳过 {skipped}，"
          f"结果文件共 {len(results)} 条 → {out_file}")
    return {"done": new_count, "skipped": skipped, "total": len(results)}


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="智联: JD 语义判断（只判断不投递）")
    parser.add_argument("--card-idx", help="列表卡片序号 idxN，点开读面板后判断")
    parser.add_argument("--jd-file", help="已落盘的 JD JSON 文件路径")
    parser.add_argument("--all", action="store_true", help="批量判断整个缓存（断点续跑）")
    parser.add_argument("--cache", default="zl_surface_cache.json")
    parser.add_argument("--out", default="zl_judge_full_results.json")
    parser.add_argument("--limit", type=int, default=None, help="本次最多新判多少条")
    parser.add_argument("--fresh", action="store_true", help="忽略已有结果，全新开始")
    args = parser.parse_args()

    if args.all:
        run_batch(args.cache, args.out, limit=args.limit, fresh=args.fresh)
        sys.exit(0)

    if args.jd_file:
        with open(args.jd_file, "r", encoding="utf-8") as f:
            jd = json.load(f)
    elif args.card_idx:
        from scripts.zl_jd_reader import read_jd_for_job
        r = read_jd_for_job(args.card_idx)
        if not r.get("ok"):
            print(f"读取失败: {r.get('detail')}")
            sys.exit(1)
        jd = r["jd"]
        print(f"已读取卡片 {args.card_idx} → 真实 jobId: {r.get('job_id_real')}")
    else:
        parser.print_help()
        sys.exit(1)

    result = judge_jd(jd)
    print(f"\n岗位: {jd.get('job_title', '')[:40]}")
    print(f"公司: {jd.get('company', '')} | 薪资: {jd.get('salary', '')}")
    print(f"\n判定: {result['verdict']} (score={result['score']})")
    for r in result["reasons"]:
        print(f"  - {r}")
    sys.exit(0 if result["verdict"] == "APPROVE" else 2)


if __name__ == "__main__":
    main()
