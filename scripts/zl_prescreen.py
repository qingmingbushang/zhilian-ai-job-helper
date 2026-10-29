#!/usr/bin/env python3
"""卡片级快速预筛：用卡片自带的技能标签/标题/薪资/学历先砍掉明显不对口的，
把点击面板读 JD 的预算留给真正可能的岗位。

筛选规则全部读 user_profile.json，不在代码里硬编码词表：
  1. 学历：卡片刻度学历必须在 acceptable_degrees 内（缺省视为不限）
  2. 薪资：日薪/时薪岗按 daily_wage_policy 处理；salary_max_k < min_salary_k 剔除
  3. 必备词：must_have_keywords 在 (标题+标签) 中至少命中 1 个
  4. 排除词：reject_keywords 命中标题 → 直接剔除（标题命中即主体不对口）
  5. 外包公司：downgrade_company_patterns / supplier_company_patterns 单独统计

输出: zl_prescreen_pass.json（按优先级排序的待判定清单）
用法: python scripts/zl_prescreen.py
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(HERE)
sys.path.insert(0, HERE)

from scripts.profile_loader import USER_PROFILE  # noqa: E402

CACHE = "zl_surface_cache.json"
OUT = "zl_prescreen_pass.json"

DEGREE_ORDER = ["学历不限", "大专", "本科", "硕士", "博士"]
DAY_RE = re.compile(r"元\s*/\s*(天|时|小时|周|次)")


def salary_min_k(s):
    """从薪资串里取起薪（单位 K），用于判断是否超出目标区间。

    例: "8000-16000元" → 8.0；"1-2万" → 10.0；"6000-10000元·13薪" → 6.0
    ★ "1-2万" 的单位只挂在第二个数字上，必须把同一单位套到左值，
      否则会把起薪算成 20K（曾踩过）。
    """
    if not s:
        return 0.0
    s = str(s).replace("～", "-").replace("~", "-")
    s = re.sub(r"[·\s]*\d+薪", "", s)
    m = re.search(r"(\d+(?:\.\d+)?)\s*([万千kK])?\s*-\s*(\d+(?:\.\d+)?)\s*([万千kK])?", s)
    if m:
        lo, lo_unit, _hi, hi_unit = m.group(1), m.group(2), m.group(3), m.group(4)
        unit = lo_unit or hi_unit
        v = float(lo)
        return round(v * (10.0 if unit == "万" else 1.0), 1) if unit else round(v / 1000.0, 1)
    m = re.search(r"(\d+(?:\.\d+)?)\s*([万千kK])", s)
    if m:
        v, unit = float(m.group(1)), m.group(2)
        return round(v * (10.0 if unit == "万" else 1.0), 1)
    m = re.search(r"(\d{3,6})", s)
    if m:
        return round(int(m.group(1)) / 1000.0, 1)
    return 0.0


def main():
    p = USER_PROFILE
    must = [w.lower() for w in p.get("must_have_keywords", [])]
    reject = [w.lower() for w in p.get("reject_keywords", [])]
    accept_deg = p.get("acceptable_degrees", ["本科", "学历不限"])
    min_k = float(p.get("min_salary_k", 0) or 0)
    band_lo = float(p.get("salary_band_min_k", 0) or 0)
    band_hi = float(p.get("salary_band_max_k", 0) or 0)
    out_pats = (p.get("downgrade_company_patterns", []) +
                p.get("supplier_company_patterns", []))

    cache = json.load(open(CACHE, encoding="utf-8"))
    print(f"[预筛] 输入 {len(cache)} 条；必备词={must}")
    print(f"[预筛] 排除词={reject}；可接受学历={accept_deg}；底薪下限={min_k}K；"
          f"目标薪资区间={band_lo}-{band_hi}K")

    passed, stats = [], {"degree": 0, "salary": 0, "day": 0, "must": 0, "reject": 0, "band": 0}
    for job in cache:
        title = job.get("title", "")
        tags = job.get("_welfare", "")
        blob = (title + "|" + tags).lower()
        deg = job.get("_degree", "") or ""
        sal = job.get("salary", "") or ""
        smax = float(job.get("salary_max_k", 0) or 0)
        smin = salary_min_k(sal)

        if reject and any(w in title.lower() for w in reject):
            stats["reject"] += 1
            continue
        if deg and deg not in accept_deg:
            stats["degree"] += 1
            continue
        if DAY_RE.search(sal):
            stats["day"] += 1
            continue
        if smax and min_k and smax < min_k:
            stats["salary"] += 1
            continue
        # 目标薪资区间过滤：区间 [smin, smax] 必须与 [band_lo, band_hi] 有交集，
        # 即起薪不能超过 band_hi（够不着），上限不能低于 band_lo（给不到）。
        if band_lo and band_hi and smin and smax:
            if smin > band_hi or smax < band_lo:
                stats["band"] += 1
                continue
        if must and not any(w in blob for w in must):
            stats["must"] += 1
            continue

        hits = [w for w in must if w in blob]
        direction_hits = [w for w in p.get("job_directions", []) if w in blob]
        score = len(hits) * 3 + len(direction_hits) * 2
        if title.count("嵌入式") or title.count("单片机") or title.count("驱动"):
            score += 4
        passed.append({
            "idx": job.get("jobId"),
            "page_order": job.get("page_order"),
            "title": title,
            "company": job.get("company", ""),
            "salary": sal,
            "salary_max_k": smax,
            "location": job.get("location", ""),
            "degree": deg,
            "experience": job.get("_experience", ""),
            "tags": tags,
            "hits": hits,
            "direction_hits": direction_hits,
            "is_supplier": any(x in job.get("company", "") for x in out_pats),
            "_rank": score,
        })

    # 非外包优先、分数降序
    passed.sort(key=lambda x: (x["is_supplier"], -x["_rank"]))
    for i, r in enumerate(passed):
        r["page_order"] = i
        r["idx"] = "idx%d" % i

    json.dump(passed, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[预筛] 剔除: 排除词{stats['reject']} / 学历{stats['degree']} / "
          f"日薪{stats['day']} / 薪资{stats['salary']} / 薪资区间{stats['band']} / "
          f"无必备词{stats['must']}")
    print(f"[预筛] 通过 {len(passed)} 条 "
          f"(其中供应商/外包 {sum(1 for x in passed if x['is_supplier'])}) → {OUT}")


if __name__ == "__main__":
    main()
