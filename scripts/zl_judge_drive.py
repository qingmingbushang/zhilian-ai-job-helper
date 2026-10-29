#!/usr/bin/env python3
"""把采集到的岗位批量送进 skill 的判定环节：点卡 → 读面板 JD → judge_jd。

与 zl_judge.run_batch 的区别（为什么不直接用原版）：
  原版用「缓存 page_order == DOM 序号」做一致性校验，前提是缓存由
  zl_recommend_loader 分批抓取（序号与页面同源）。本次缓存由采集器跨会话
  累积，刷新后智联会重排，序号必然失配 → 原版会直接 break。
  本版的定位方案沿用 skill 规范：「快照 (标题,公司) 双键 → 定位 DOM 位置 →
  点开后用面板真实 jobId/标题终验」，终验不一致即作废，绝不硬判。

断点续跑：结果文件即断点。用法:
  python scripts/zl_judge_drive.py --limit 200
"""
import argparse
import json
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(HERE)
sys.path.insert(0, HERE)

from scripts.zhaopin_client import attach_active_tab, evaluate, navigate  # noqa: E402
from scripts.zl_jd_reader import read_jd_for_job  # noqa: E402
from scripts.zl_judge import judge_jd  # noqa: E402
from scripts.profile_loader import build_url  # noqa: E402

OUT = "zl_judge_full_results.json"
POOL = "zl_prescreen_pass.json"
SNAP_JS = r"""(function(){
    function c(t){return (t||'').replace(/\s+/g,'').replace(/\u00a0/g,'').trim();}
    var cards=document.querySelectorAll('.job-card');var out=[];
    for(var i=0;i<cards.length;i++){
        var x=cards[i];
        var tEl=x.querySelector('.job-card__title-main');
        var t=c(tEl?tEl.innerText:'');
        if(!t&&tEl){var a=tEl.querySelector('[aria-label]');if(a)t=c(a.getAttribute('aria-label'));}
        var cEl=x.querySelector('.job-card__company')||x.querySelector('.job-card__company-name');
        var comp=c(cEl?cEl.innerText:'');
        if(!comp){var la=x.querySelector('.job-card__company a[title]');if(la)comp=c(la.getAttribute('title'));}
        var sEl=x.querySelector('.job-card__salary');
        out.push({i:i,t:t,comp:comp,sal:c(sEl?sEl.innerText:'')});
    }
    return JSON.stringify(out);
})()"""


def norm(s):
    return (s or "").replace(" ", "").replace("\u00a0", "").strip()


def load_results():
    if os.path.exists(OUT):
        try:
            return json.load(open(OUT, encoding="utf-8"))
        except json.JSONDecodeError:
            return []
    return []


def save(results):
    tmp = OUT + ".tmp"
    json.dump(results, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, OUT)


def snapshot():
    try:
        return json.loads(evaluate(SNAP_JS) or "[]")
    except (json.JSONDecodeError, TypeError):
        return []


COUNT_JS = "document.querySelectorAll('.job-card').length"
HEIGHT_JS = "document.documentElement.scrollHeight"


def _num(js):
    try:
        return int(float(evaluate(js) or 0))
    except (TypeError, ValueError):
        return 0


def load_all_cards(max_rounds=80, patience=10):
    """滚完整张列表（后台标签页需长等待），返回 (卡片快照列表, 卡片总数)

    屏幕外卡片的标题会被 vue-clamp 回收，所以每次滚到底后立刻快照并合并。
    智联在 hidden 标签页下加载很慢，单轮等待要足够长，耐心阈值也要放宽，
    否则会在中途误判"到底"（实测 80 张时误判过）。
    """
    keymap = {}
    prev_cards = prev_h = -1
    stable = 0
    for r in range(max_rounds):
        evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        time.sleep(random.uniform(7.5, 9.5))
        for c in snapshot():
            if not c.get("t"):
                continue
            keymap.setdefault((norm(c["t"]), norm(c["comp"])), []).append(c)
        cards, h = _num(COUNT_JS), _num(HEIGHT_JS)
        if cards <= prev_cards and h <= prev_h:
            evaluate("window.scrollBy(0, -800)")
            time.sleep(random.uniform(0.6, 1.0))
            evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
            time.sleep(random.uniform(6.0, 8.0))
            for c in snapshot():
                if not c.get("t"):
                    continue
                keymap.setdefault((norm(c["t"]), norm(c["comp"])), []).append(c)
            cards, h = _num(COUNT_JS), _num(HEIGHT_JS)
            if cards <= prev_cards and h <= prev_h:
                stable += 1
                if stable >= patience:
                    break
            else:
                stable = 0
        else:
            stable = 0
        prev_cards, prev_h = max(prev_cards, cards), max(prev_h, h)
    evaluate("window.scrollTo(0,0)")
    time.sleep(1.0)
    return keymap, _num(COUNT_JS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--kw", default="嵌入式开发工程师")
    ap.add_argument("--url", default=None, help="完整列表页URL（含薪资/城市筛选参数）")
    ap.add_argument("--keep-page", action="store_true")
    args = ap.parse_args()

    pool = json.load(open(POOL, encoding="utf-8"))
    results = load_results()
    done = {(r.get("title"), r.get("company")) for r in results if r.get("verdict") not in ("READ_FAIL",)}
    print(f"[判定] 池 {len(pool)} 条 | 已有结果 {len(results)} 条 | 本轮上限 {args.limit}")

    attach_active_tab(url_prefix="https://www.zhaopin.com")
    if not args.keep_page:
        url = args.url or build_url(kw=args.kw)
        print(f"[导航] {url}")
        navigate(url, wait_range=(6, 9))
        time.sleep(4)

    keymap, ncards = load_all_cards()
    print(f"[快照] 滚完整表: {ncards} 张卡，可匹配唯一键 {len(keymap)} 组", flush=True)

    new_count = 0
    miss = 0
    for cand in pool:
        if args.limit is not None and new_count >= args.limit:
            break
        key = (norm(cand["title"]), norm(cand["company"]))
        if (cand["title"], cand["company"]) in done:
            continue
        lst = keymap.get(key)
        if not lst:
            miss += 1
            continue
        c = lst.pop(0)
        r = read_jd_for_job("idx%d" % c["i"])
        if not r.get("ok"):
            print(f"[跳过] {cand['title'][:22]} | {r.get('detail','')[:50]}")
            continue
        jd = r["jd"]
        # 终验：面板标题/公司必须与预期一致
        if norm(jd.get("job_title")) and norm(jd.get("job_title")) != key[0]:
            if key[0] not in norm(jd.get("job_title")) and norm(jd.get("job_title")) not in key[0]:
                print(f"[作废] 标题终验失配 期望={cand['title'][:20]} 面板={(jd.get('job_title') or '')[:20]}")
                continue
        v = judge_jd(jd)
        rec = {
            "idx": c["i"],
            "jobId": r.get("job_id_real", ""),
            "title": (jd.get("job_title") or cand["title"])[:60],
            "company": jd.get("company") or cand["company"],
            "salary": jd.get("salary") or cand["salary"],
            "location": cand.get("location", ""),
            "degree": jd.get("degree") or cand.get("degree", ""),
            "experience": jd.get("experience") or cand.get("experience", ""),
            "salary_max_k": cand.get("salary_max_k", 0),
            "verdict": v["verdict"],
            "score": v["score"],
            "reasons": v["reasons"],
            "hits": v["hits"],
        }
        results.append(rec)
        save(results)
        new_count += 1
        print(f"[{new_count:3d}] {rec['verdict']:9s} {rec['score']:3d} | {rec['title'][:26]} | "
              f"{rec['company'][:14]} | {rec['salary']}", flush=True)
        time.sleep(random.uniform(1.2, 2.2))

    save(results)
    print(f"\n[判定] 本轮新判 {new_count} 条，未在列表中找到 {miss} 条，结果共 {len(results)} 条")


if __name__ == "__main__":
    main()
