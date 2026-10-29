#!/usr/bin/env python3
"""智联列表全量采集（后台标签页可用），直接输出下游判定所需的卡片字段。

已踩的坑与对策:
  1. visibilityState=hidden ⇒ 需 scrollTo 到底 + 长等待（8~10s/屏）才出下一屏。
  2. 屏幕外卡片标题被 vue-clamp 回收 ⇒ 每屏抽一次，靠公司名+薪资锚定去重。
  3. 标题空时改用 span.job-card__title-clamp 的 aria-label；公司名取 a[title] 兜底。
  4. 卡片无 jobId ⇒ jobId 先填 idxN 占位，真实 jobId 由点开面板时回填。

用法:
  python scripts/zl_collect_all.py --kw 嵌入式软件 --out zl_collect_kw1.json
"""
import argparse
import json
import os
import random
import re
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.chdir(HERE)

from scripts.zhaopin_client import (  # noqa: E402
    attach_active_tab, decrypt_salary, evaluate, navigate, parse_salary_max,
)
from scripts.profile_loader import build_url  # noqa: E402

COUNT_JS = "document.querySelectorAll('.job-card').length"
HEIGHT_JS = "document.documentElement.scrollHeight"

# 单屏抽取：标题走 innerText → aria-label → clamp title；公司名走 innerText → a[title]
HARVEST_JS = r"""(function(){
    function clean(t) { return (t || '').replace(/\s+/g, ' ').trim(); }
    var cards = document.querySelectorAll('.job-card');
    var out = [];
    for (var i = 0; i < cards.length; i++) {
        var c = cards[i];
        var tEl = c.querySelector('.job-card__title-main');
        var t = clean(tEl ? tEl.innerText : '');
        if (!t && tEl) {
            var cl = tEl.querySelector('[aria-label]');
            if (cl) t = clean(cl.getAttribute('aria-label'));
        }
        if (!t) {
            var any = c.querySelector('[aria-label]');
            if (any) t = clean(any.getAttribute('aria-label'));
        }
        var cEl = c.querySelector('.job-card__company-name') || c.querySelector('.job-card__company');
        var comp = clean(cEl ? cEl.innerText : '');
        if (!comp) {
            var a = c.querySelector('.job-card__company a[title]');
            if (a) comp = clean(a.getAttribute('title'));
        }
        var sEl = c.querySelector('.job-card__salary');
        var sal = clean(sEl ? sEl.innerText : '');
        var lEl = c.querySelector('.job-card__location') || c.querySelector('[class*="location"]');
        var loc = clean(lEl ? lEl.innerText : '');
        var tags = [];
        var tagEls = c.querySelectorAll('.job-card__skill-tag');
        for (var k = 0; k < tagEls.length; k++) { var tt = clean(tagEls[k].innerText); if (tt) tags.push(tt); }
        out.push({i: i, title: t, company: comp, salary: sal, location: loc, tags: tags});
    }
    return JSON.stringify(out);
})()"""

SAL_RE = re.compile(r"\d")


def _num(js):
    try:
        return int(float(evaluate(js) or 0))
    except (TypeError, ValueError):
        return 0


def _to_bottom():
    evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")


def _nudge():
    evaluate("window.scrollBy(0, -800)")
    time.sleep(random.uniform(0.6, 1.0))
    _to_bottom()


def _harvest(store):
    try:
        rows = json.loads(evaluate(HARVEST_JS) or "[]")
    except (json.JSONDecodeError, TypeError):
        return 0
    got = 0
    for r in rows:
        title = (r.get("title") or "").strip()
        comp = (r.get("company") or "").strip()
        sal = (r.get("salary") or "").strip()
        if not title or not SAL_RE.search(sal):
            continue
        key = (title.replace(" ", ""), comp.replace(" ", ""), sal.replace(" ", ""))
        if key in store:
            continue
        tags = r.get("tags") or []
        store[key] = {
            "title": title,
            "company": comp,
            "salary": sal,
            "location": r.get("location", ""),
            "city": (r.get("location", "").split(" ")[0] if r.get("location") else ""),
            "industry": "",
            "welfare": "|".join(tags),
            "degree": next((t for t in tags if t in ("大专", "本科", "硕士", "博士", "学历不限")), ""),
            "experience": next((t for t in tags if "年" in t or t in ("应届", "在校/应届")), ""),
            "jobId": "",
            "href": "",
            "_rw": r["i"],
        }
        got += 1
    return got


def run(tag, max_rounds=140, patience=9):
    store = {}
    _harvest(store)
    print(f"  [{tag}] 首屏: {len(store)} 条", flush=True)
    prev_cards, prev_h = _num(COUNT_JS), _num(HEIGHT_JS)
    stable = 0
    for r in range(max_rounds):
        _to_bottom()
        time.sleep(random.uniform(7.0, 9.0))
        got = _harvest(store)
        cards, h = _num(COUNT_JS), _num(HEIGHT_JS)
        print(f"  [{tag}] r{r}: cards={cards} h={h} 新增{got} 累计={len(store)}", flush=True)
        if cards <= prev_cards and h <= prev_h:
            _nudge()
            time.sleep(random.uniform(6.0, 8.0))
            got2 = _harvest(store)
            cards2, h2 = _num(COUNT_JS), _num(HEIGHT_JS)
            if cards2 <= prev_cards and h2 <= prev_h:
                stable += 1
                if stable >= patience:
                    print(f"  [{tag}] 判定到底 cards={cards2} h={h2}", flush=True)
                    break
            else:
                stable = 0
                cards, h = cards2, h2
        else:
            stable = 0
        prev_cards, prev_h = max(prev_cards, cards), max(prev_h, h)
    return store


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kw", default=None)
    ap.add_argument("--url", default=None)
    ap.add_argument("--out", default="zl_collect_raw.json")
    ap.add_argument("--keep-page", action="store_true")
    ap.add_argument("--max-rounds", type=int, default=140)
    args = ap.parse_args()

    attach_active_tab(url_prefix="https://www.zhaopin.com")
    if not args.keep_page:
        url = args.url or (build_url(kw=args.kw) if args.kw else build_url())
        print(f"[导航] {url}")
        navigate(url, wait_range=(6, 9))
        time.sleep(4.5)

    print(f"[当前页] {evaluate('window.location.href')}")
    evaluate("window.scrollTo(0,0)")
    time.sleep(1.5)

    tag = args.kw or "page"
    store = run(tag, max_rounds=args.max_rounds)

    evaluate("window.scrollTo(0,0)")
    time.sleep(1.5)
    _harvest(store)

    out = []
    for i, rec in enumerate(store.values()):
        rec["jobId"] = "idx%d" % i
        rec["page_order"] = i
        rec["salary_max_k"] = parse_salary_max(decrypt_salary(rec["salary"]))
        rec["_kw"] = tag
        out.append(rec)
    json.dump(out, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[落盘] {args.out}: {len(out)} 条")
    tk = [o["title"] for o in out]
    print(f"[抽查] 前3: {tk[:3]} | 后3: {tk[-3:]}")


if __name__ == "__main__":
    main()
