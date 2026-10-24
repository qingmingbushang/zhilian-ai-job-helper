#!/usr/bin/env python3
"""批量投递 zl_precheck_candidates.json 里的候选（用户已在会话中确认）。

v1（与 v4 预检同源的安全垫）:
  1. 附加标签页 + URL 关键词守卫（unquote 比对）
  2. 滚完整表 → 快照（标题,公司）双键匹配 DOM 位置
  3. 每条投递前先 precheck（只读）拿到面板真实 jobId 终验，不一致绝不投
  4. deliver(confirm=True) —— 内含闸门2实时复检、验证码双检测（命中即中止）
  5. 结果落盘 zl_deliver_log.json + 汇总打印
"""
import json
import os
import random
import sys
import time
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.zhaopin_client import evaluate, scroll_job_list, navigate, attach_active_tab
from scripts.zl_deliver import precheck, deliver
from scripts.zl_precheck_batch import _norm, _load_full_list, _SNAPSHOT_JS, EXPECT_KW, LIST_URL

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAND_FILE = os.path.join(HERE, "zl_precheck_candidates.json")


def main():
    cands = json.load(open(CAND_FILE, encoding="utf-8"))
    print(f"待投候选: {len(cands)} 条（用户已确认）")

    # 附加失败时回退已绑定会话（绝不新开页面）
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
        print(f"[守卫] 当前页面不含关键词，导回列表页")
        navigate(LIST_URL, wait_range=(6, 8))
        time.sleep(3)
        url = evaluate("window.location.href") or ""
        if EXPECT_KW not in unquote(url):
            print("错误: 未回到目标关键词页，退出")
            sys.exit(1)
    print(f"[守卫] 页面关键词校验通过")

    _load_full_list()
    raw = evaluate(_SNAPSHOT_JS) or "{}"
    try:
        snap = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        snap = {}
    dom_keys = [(_norm(c.get("title")), _norm(c.get("company"))) for c in (snap.get("cards") or [])]
    print(f"[快照] DOM 卡片 {snap.get('count', '?')} 张")

    used = set()
    def _find_pos(title, company):
        key = (_norm(title), _norm(company))
        for i, k in enumerate(dom_keys):
            if i not in used and k == key:
                return i
        return -1

    results = []
    for cand in cands:
        tag = f"缓存{cand.get('cache_idx', '?')} {cand.get('expect_title', cand.get('title', ''))[:22]}"
        pos = _find_pos(cand.get("expect_title") or cand.get("title", ""),
                        cand.get("expect_company") or cand.get("company", ""))
        if pos < 0:
            print(f"[{tag}] 未在当前列表匹配到位置 → skip")
            results.append({"tag": tag, "status": "skip",
                            "detail": "列表中未匹配到（标题+公司双键）"})
            continue
        used.add(pos)
        idx = "idx%d" % pos

        # 投前终验: 只读 precheck 拿面板真实 jobId
        try:
            p = precheck(idx)
        except Exception as ex:
            print(f"[{tag}] 投前终验异常: {ex} → skip")
            results.append({"tag": tag, "status": "skip", "detail": f"终验异常: {ex}"})
            continue
        if not p.get("ok"):
            print(f"[{tag}] 投前终验失败: {p.get('detail')} → skip")
            results.append({"tag": tag, "status": "skip", "detail": f"终验失败: {p.get('detail')}"})
            continue
        if p.get("jobId") != cand.get("jobId"):
            print(f"[{tag}] jobId 终验失配! 期望={cand.get('jobId')} 面板={p.get('jobId')} → 作废不投")
            results.append({"tag": tag, "status": "skip",
                            "detail": f"jobId 失配（期望 {cand.get('jobId')} / 面板 {p.get('jobId')}）"})
            continue

        d = deliver(idx, confirm=True)
        d["tag"] = tag
        results.append(d)
        print(f"[{tag}] {d.get('status', 'fail')} | {d.get('detail') or ''}")
        if d.get("status") == "captcha":
            print("检测到验证码/风控弹窗，中止剩余投递队列，请人工处理后重跑。")
            break
        time.sleep(random.uniform(3, 5))

    print("\n=== 投递汇总 ===")
    for r in results:
        print(f"  {r.get('status', 'fail'):8s} | {r.get('tag', '')} | {r.get('detail', '')[:40]}")


if __name__ == "__main__":
    main()
