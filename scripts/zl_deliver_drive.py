#!/usr/bin/env python3
"""从预筛池直接投递（跳过全量 JD 判定的等待）。

定位方案（沿用 skill 规范，只是把「滚完整表」换成「按公司名搜索定位」）：
  1. 用公司名在列表页搜索，让目标进入首屏，避免滚 1000 张卡；
  2. 快照首屏 20 张卡，按 (标题, 公司) 双键定位 DOM 位置；
  3. precheck（只读）读右侧面板，用面板的 标题/公司 做终验，不一致绝不投；
  4. 面板按钮可投 + 无经验硬门槛 → deliver(confirm=True)；
  5. 命中验证码/风控 → 立即中止队列；命中日上限 → 自然结束。

用法: python scripts/zl_deliver_drive.py [--limit 20]
"""
import argparse
import json
import os
import random
import re
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(HERE)
sys.path.insert(0, HERE)

from scripts.zhaopin_client import attach_active_tab, evaluate, navigate  # noqa: E402
from scripts.zl_deliver import _load_log, deliver, precheck  # noqa: E402
from scripts.profile_loader import build_url  # noqa: E402

POOL = "zl_prescreen_pass.json"
SEARCH_URL = ("https://www.zhaopin.com/jobs/?jl=489&pageMode=search"
              "&kw={kw}&sl=6001%2C8000")

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
        out.push({i:i,t:t,comp:comp});
    }
    return JSON.stringify(out);
})()"""


def norm(s):
    return (s or "").replace(" ", "").replace("\u00a0", "").strip()


def same(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def snapshot():
    try:
        return json.loads(evaluate(SNAP_JS) or "[]")
    except (json.JSONDecodeError, TypeError):
        return []


def locate(title, company, kw):
    """按公司名搜索定位；失败再用标题主干词搜一次。返回 DOM 序号或 -1"""
    stem = re.split(r"[（(【/_—-]", title)[0].strip()
    for keyword in [company[:12], stem]:
        if not keyword or len(keyword) < 2:
            continue
        navigate(SEARCH_URL.format(kw=keyword), wait_range=(6, 9))
        time.sleep(3.2)
        for c in snapshot():
            if same(c["t"], title) and same(c["comp"], company):
                return c["i"]
    return -1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--pool", default=POOL)
    args = ap.parse_args()

    pool = json.load(open(args.pool, encoding="utf-8"))
    log = _load_log()
    sent_pairs = {(norm(e.get("title")), norm(e.get("company")))
                  for e in log if e.get("action") == "deliver" and e.get("status") == "success"}
    print(f"[投递] 候选池 {len(pool)} 条 | 历史已投 {len(sent_pairs)} 组")

    attach_active_tab(url_prefix="https://www.zhaopin.com")

    ok = fail = skip = 0
    for n, cand in enumerate(pool, 1):
        if args.limit is not None and ok >= args.limit:
            print(f"[投递] 达到本次上限 {args.limit}，停止")
            break
        title, company = cand["title"], cand["company"]
        if (norm(title), norm(company)) in sent_pairs:
            continue

        pos = locate(title, company, cand.get("_kw", ""))
        if pos < 0:
            skip += 1
            print(f"[{n}] 定位失败 → skip | {title[:24]} | {company[:14]}", flush=True)
            continue

        idx = "idx%d" % pos
        try:
            p = precheck(idx)
        except Exception as ex:
            skip += 1
            print(f"[{n}] 预检异常 {ex} → skip", flush=True)
            continue
        if not p.get("ok"):
            skip += 1
            print(f"[{n}] 预检失败 {p.get('detail')} → skip", flush=True)
            continue
        # 终验：面板标题与公司都必须对上候选，否则绝不投
        if not (same(p.get("title"), title) and same(p.get("company"), company)):
            skip += 1
            print(f"[{n}] 终验失配 → 作废 | 期望={title[:18]}/{company[:12]} "
                  f"面板={str(p.get('title'))[:18]}/{str(p.get('company'))[:12]}", flush=True)
            continue
        if p.get("seniority_block"):
            skip += 1
            print(f"[{n}] 经验门槛 {p.get('seniority_hits')} → skip | {title[:22]}", flush=True)
            continue
        if not p.get("can_send"):
            skip += 1
            print(f"[{n}] 按钮不可投 {p.get('buttons')} → skip | {title[:22]}", flush=True)
            continue

        d = deliver(idx, confirm=True)
        st = d.get("status")
        if st == "success":
            ok += 1
            sent_pairs.add((norm(title), norm(company)))
            print(f"[{n}] ✓ 投递成功 | {p.get('title','')[:24]} | {p.get('company','')[:16]} | "
                  f"{p.get('salary','')} | jobId={p.get('jobId')}", flush=True)
        elif st == "captcha":
            print(f"[{n}] ⚠ 命中验证码/风控，中止队列等人工处理", flush=True)
            break
        elif st == "skip" and "上限" in str(d.get("detail", "")):
            print(f"[{n}] 已达当日投递上限，停止", flush=True)
            break
        else:
            fail += 1
            print(f"[{n}] ✗ {st} | {d.get('detail','')[:50]} | {title[:22]}", flush=True)
        time.sleep(random.uniform(3, 5))

    print(f"\n[投递] 成功 {ok} / 跳过 {skip} / 失败 {fail}")


if __name__ == "__main__":
    main()
