#!/usr/bin/env python3
"""从 APPROVE 100 分非外包池里预检，凑满 N 条干净候选。

v4（重排适配版）:
  ★ 实测: 列表刷新后智联会重排（922 张卡仅 4 位置与缓存对齐），idx 映射失效；
    且卡片不带任何 jobId（无 data-jobid / jobdetail href）。
  ★ 匹配方案: 快照全部卡片的（标题, 公司）双键 → 与判定结果逐一匹配 DOM 位置
    （重复键占用表防撞）→ 点开后用面板真实 jobId 终验，不一致即作废。
  ★ 页面守卫: URL 解码后必须含关键词，否则导回列表页。

干净标准（与已投 5 条一致）:
  APPROVE + score=100 + 公司不含外包/供应商模式词 + JD 无经验年限 + 按钮可投。

预检只读（点卡读 JD + 扫按钮），不点投递。结果落盘 zl_precheck_candidates.json。
"""
import json
import os
import random
import re
import sys
import time
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.zhaopin_client import evaluate, scroll_job_list, navigate, attach_active_tab
from scripts.zl_deliver import precheck, _load_log
from scripts.profile_loader import SEARCH_KEYWORD, build_url

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 关键词与列表页 URL 一律从 user_profile.json 读（不硬编码城市/关键词）
EXPECT_KW = SEARCH_KEYWORD
LIST_URL = build_url()
TARGET = int(sys.argv[1]) if len(sys.argv) > 1 else 10
MAX_TRY = int(sys.argv[2]) if len(sys.argv) > 2 else 80
MAX_SCROLL = int(sys.argv[3]) if len(sys.argv) > 3 else 60

JOB_ID_RE = re.compile(r"^CC[A-Z]?\d+J\d+$")

_SNAPSHOT_JS = r"""
(function(){
    var cards = document.querySelectorAll('.job-card');
    var out = [];
    for (var i = 0; i < cards.length; i++) {
        var c = cards[i];
        var tEl = c.querySelector('.job-card__title-main') || c.querySelector('.job-card__title');
        var cEl = c.querySelector('.job-card__company-name') || c.querySelector('[class*="company-name"]');
        out.push({title: tEl ? (tEl.innerText || '').replace(/\s+/g, ' ').trim() : '',
                  company: cEl ? (cEl.innerText || '').replace(/\s+/g, ' ').trim() : ''});
    }
    return JSON.stringify({count: cards.length, cards: out});
})()
"""


def _norm(t):
    return (t or "").replace(" ", "").replace("\u00a0", "")


def _card_count():
    try:
        return int(evaluate("document.querySelectorAll('.job-card').length") or 0)
    except (TypeError, ValueError):
        return 0


def _load_full_list():
    """滚动直到卡片数连续 3 轮不增长（或到达上限）。"""
    stable, last = 0, -1
    for i in range(MAX_SCROLL):
        n = _card_count()
        print(f"[加载] 第 {i+1} 轮滚动: {n} 张卡片")
        if n == last:
            stable += 1
            if stable >= 3:
                print(f"[加载] 列表加载完成: {n} 张")
                return n
        else:
            stable = 0
        last = n
        scroll_job_list()
        time.sleep(random.uniform(1.2, 2.0))
    return _card_count()


def main():
    profile = json.load(open(os.path.join(HERE, "user_profile.json"), encoding="utf-8"))
    exclude_pats = (profile.get("downgrade_company_patterns", [])
                    + profile.get("supplier_company_patterns", []))

    results = json.load(open(os.path.join(HERE, "zl_judge_full_results.json"), encoding="utf-8"))
    log = _load_log()
    already_touched = {e.get("idx") for e in log
                       if e.get("action") in ("precheck", "deliver") and e.get("ok", True)}
    already_jids = {e.get("jobId") for e in log if e.get("jobId")}

    pool = []
    for e in results:
        if e.get("verdict") != "APPROVE" or e.get("score") != 100:
            continue
        jid = e.get("jobId", "")
        idx = "idx%d" % e["idx"]
        if not JOB_ID_RE.match(jid) or jid in already_jids or idx in already_touched:
            continue
        comp = e.get("company", "")
        if any(p in comp for p in exclude_pats):
            continue
        pool.append({"order": e["idx"], "idx": idx, "jobId": jid,
                     "title": e.get("title", ""), "company": comp, "salary": e.get("salary", "")})
    pool.sort(key=lambda x: x["order"])
    print(f"候选池: {len(pool)} 条（APPROVE+100分+非外包+真实jobId+未碰过），"
          f"目标 {TARGET} 条，最多试 {MAX_TRY} 次")

    # ── 页面守卫 ──
    # 附加失败时回退已绑定会话（绝不新开页面，与 zl_deliver._attach_or_reuse 一致）
    attached = attach_active_tab(url_prefix="https://www.zhaopin.com")
    if not attached:
        url_now = evaluate("window.location.href") or ""
        if "zhaopin.com" in url_now:
            print(f"[标签页] 前台无智联页，回退已绑定会话: {url_now[:60]}")
            attached = True
    if not attached:
        print("错误: 附加不到智联标签页且已绑定会话无效")
        sys.exit(1)
    url = evaluate("window.location.href") or ""
    if EXPECT_KW not in unquote(url):
        print(f"[守卫] 当前页面不含关键词 '{EXPECT_KW}': {url[:80]}，导回列表页")
        navigate(LIST_URL, wait_range=(6, 8))
        time.sleep(3)
        url = evaluate("window.location.href") or ""
        if EXPECT_KW not in unquote(url):
            print("错误: 导航后仍未回到目标关键词页，退出")
            sys.exit(1)

    # ── 滚完整表 + (标题, 公司) 快照 ──
    total = _load_full_list()
    raw = evaluate(_SNAPSHOT_JS) or "{}"
    try:
        snap = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        snap = {}
    dom_cards = snap.get("cards") or []
    dom_keys = [(_norm(c.get("title")), _norm(c.get("company"))) for c in dom_cards]
    print(f"[快照] DOM 卡片 {snap.get('count', '?')} 张")

    # 位置占用表: 同 (标题,公司) 的重复岗位不重复匹配
    used = set()
    def _find_pos(title, company):
        key = (_norm(title), _norm(company))
        for i, k in enumerate(dom_keys):
            if i in used:
                continue
            if k == key:
                return i
        return -1

    passed, tried, mismatch, not_in_list = [], 0, 0, 0
    for cand in pool:
        if len(passed) >= TARGET or tried >= MAX_TRY:
            break
        pos = _find_pos(cand["title"], cand["company"])
        if pos < 0:
            not_in_list += 1
            print(f"[缓存{cand['order']}] {cand['title'][:24]} | {cand['company'][:14]} "
                  f"未在当前列表中找到（可能已下线），跳过")
            continue
        used.add(pos)
        tried += 1
        try:
            p = precheck("idx%d" % pos)
        except Exception as ex:
            print(f"[缓存{cand['order']}] 预检异常: {ex}")
            continue
        if not p.get("ok"):
            print(f"[缓存{cand['order']}] 预检失败: {p.get('detail')}")
            continue
        # ★ 终验: 面板读出的真实 jobId 必须与判定结果一致
        if p.get("jobId") != cand["jobId"]:
            mismatch += 1
            print(f"[缓存{cand['order']}] jobId 终验失配! 期望={cand['jobId']} "
                  f"面板={p.get('jobId')} → 作废跳过")
            continue
        p["cache_idx"] = cand["order"]        # 保留缓存序号便于对照
        p["expect_title"] = cand["title"]
        p["expect_company"] = cand["company"]
        blocked = p.get("seniority_block") or not p.get("can_send")
        why = ("经验命中" + str(p["seniority_hits"])) if p.get("seniority_block") else \
              ("按钮不可投 " + str(p.get("buttons"))) if not p.get("can_send") else "干净"
        print(f"[缓存{cand['order']}→DOM{pos}] {'BLOCK' if blocked else 'PASS '} | "
              f"{p['title'][:26]} | {p['company'][:16]} | {p['salary']} | {why}")
        if not blocked:
            passed.append(p)
        time.sleep(random.uniform(1.0, 2.0))

    out = os.path.join(HERE, "zl_precheck_candidates.json")
    json.dump(passed, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n=== 预检完成: 实际预检 {tried} 条, 通过 {len(passed)} 条 "
          f"(标题失配 {mismatch} / 不在列表 {not_in_list}) ===")
    for p in passed:
        print(f"  缓存{p.get('cache_idx')} DOM{p['idx']} | {p['title'][:28]} | "
              f"{p['company'][:18]} | {p['salary']} | jobId={p['jobId']}")


if __name__ == "__main__":
    main()
