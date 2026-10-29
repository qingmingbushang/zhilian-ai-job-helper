#!/usr/bin/env python3
"""智联流水线投递：逐条 点开 → 读面板 → 判断 → 合规即投。

为什么不再预扫全表:
  1. 决策依据全部来自**当前面板实时内容**（标题/公司/JD/jobId 同源），
     不依赖任何缓存的 idx↔岗位映射，所以列表重排/刷新都不影响正确性；
  2. 只按 DOM 顺序逐张点开，读一张判一张，符合就当场投，省掉
     "滚 922 张 → 快照 → 双键匹配 → 再逐条预检 → 再投" 的三段开销；
  3. 面板数据直接传给 deliver(preloaded=...)，避免同一张卡读两遍。

安全边界（一条都不减）:
  - 附加标签页 + URL 关键词守卫（unquote 后比对）
  - 每条投前查验证码/风控弹窗，命中即整体中止，不停在原条重试
  - deliver() 内部仍有：按钮实时复检、点击后弹窗扫描、投后三重验证、日上限
  - 去重：以 jobId 为准（投过/unknown 的都不再投）
  - 断点：zl_stream_state.json 记录下一条待扫序号，--resume 接着扫

用法:
  python scripts/zl_stream_deliver.py --max-scan 60 --target 5 --dry-run   # 只看判断
  python scripts/zl_stream_deliver.py --max-scan 60 --target 5 --yes       # 真投
  python scripts/zl_stream_deliver.py --resume --yes                       # 接着上次
"""
import argparse
import json
import os
import random
import sys
import time
from urllib.parse import unquote

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from scripts.zhaopin_client import CARD_SELECTOR, attach_active_tab, api, evaluate, navigate
from scripts.profile_loader import SEARCH_KEYWORD, build_url
from scripts.zl_jd_reader import read_jd_for_job
from scripts.zl_judge import judge_jd
from scripts.zl_deliver import (
    DAILY_DELIVER_LIMIT, SENIORITY_RE, _captcha_present, _delivered_today, _load_log,
    deliver,
)

STATE_FILE = os.path.join(HERE, "zl_stream_state.json")

_COUNT_JS = r"""
(function(){
    return JSON.stringify({count: document.querySelectorAll(%s).length});
})()
""" % json.dumps(CARD_SELECTOR)


# ────────────────────────── 基础设施 ──────────────────────────
def _attach():
    """附加智联标签页：优先前台活动页，失败回退已绑定会话（不新开页面）。"""
    if attach_active_tab(url_prefix="https://www.zhaopin.com"):
        return True
    r = api("find_tab", {"url": "https://www.zhaopin.com", "active": False}, timeout=10)
    return bool(r.get("ok"))


def _guard():
    """页面守卫: URL 解码后必须含目标关键词，否则导回列表页。"""
    url = evaluate("window.location.href") or ""
    if SEARCH_KEYWORD in unquote(url):
        return True
    print(f"[守卫] 当前页不含 '{SEARCH_KEYWORD}'，导回列表页: {unquote(url)[:100]}")
    navigate(build_url(), wait_range=(6, 8))
    time.sleep(2)
    url = evaluate("window.location.href") or ""
    ok = SEARCH_KEYWORD in unquote(url)
    if not ok:
        print("[守卫] 导航后仍未回到目标关键词页，退出（不硬跑）")
    return ok


def _card_count():
    try:
        return int(json.loads(evaluate(_COUNT_JS) or "{}").get("count") or 0)
    except (json.JSONDecodeError, TypeError, ValueError):
        return 0


def _save_state(idx_next, scanned, delivered, stop_reason):
    data = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "keyword": SEARCH_KEYWORD, "next_idx": idx_next,
            "scanned": scanned, "delivered": delivered, "stop_reason": stop_reason}
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def _load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _done_job_ids():
    """已投过的 jobId（success / unknown 都算，避免重复轰炸）"""
    ids = set()
    for e in _load_log():
        if e.get("action") == "deliver" and e.get("status") in ("success", "unknown"):
            if e.get("jobId"):
                ids.add(e["jobId"])
        elif e.get("action") == "stream" and e.get("status") == "delivered":
            if e.get("jobId"):
                ids.add(e["jobId"])
    return ids


def _log(entry):
    log = _load_log()
    log.append(entry)
    tmp = os.path.join(HERE, "zl_deliver_log.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(HERE, "zl_deliver_log.json"))


def _seniority_hits(jd):
    full = (jd.get("jd_text") or "") + "\n" + (jd.get("full_text") or "")
    return sorted(set(m.group(0) for m in SENIORITY_RE.finditer(full)))


# ────────────────────────── 主流程 ──────────────────────────
def run(start=0, max_scan=60, target=5, min_score=95,
        dry_run=False, seniority_block=True):
    if not _attach():
        print("错误: 附加不到智联标签页。请先在浏览器打开智联列表页")
        return 2
    if not _guard():
        return 2

    done_today = _delivered_today()
    print(f"[启动] 今日已成功 {done_today}/{DAILY_DELIVER_LIMIT} 条 | "
          f"目标 {target} 条 | 最多扫 {max_scan} 张 | 最低分 {min_score} | "
          f"{'只读演练' if dry_run else '真投'}")

    count = _card_count()
    print(f"[页面] 当前 DOM 卡片 {count} 张，从 idx{start} 开始流水线扫描\n")

    done_ids = _done_job_ids()
    seen = set()
    scanned = delivered = 0
    stop_reason = "扫描完毕"
    t_run = time.time()

    for i in range(start, count):
        if scanned >= max_scan:
            stop_reason = f"达到扫描上限 {max_scan} 张"
            break
        if delivered >= target:
            stop_reason = f"达到投递目标 {target} 条"
            break

        idx = f"idx{i}"
        t0 = time.time()
        scanned += 1

        # 验证码前置检查（命中即整体中止，停下等用户）
        cap = _captcha_present()
        if cap.get("captcha"):
            stop_reason = "检测到验证码/风控弹窗，停下等处理"
            _log({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "action": "stream_stop",
                  "idx": idx, "reason": stop_reason, "captcha": cap})
            print(f"\n[中止] {stop_reason}")
            break

        # ── 单张卡片的处理，返回 (是否继续, 是否已投递) ──
        go_on, sent = _process_card(idx, done_ids, seen, min_score,
                                    seniority_block, dry_run)
        if sent:
            delivered += 1
        _save_state(i + 1, scanned, delivered, "进行中")

        if not go_on:
            stop_reason = "队列中止（验证码或日上限，详见日志）"
            break

        print(f"        ({time.time() - t0:.1f}s)")
        time.sleep(random.uniform(1.2, 2.2))   # 条间呼吸（投递后内部已另有等待）

    _finish(scanned, delivered, stop_reason, time.time() - t_run)
    return 0


def _process_card(idx, done_ids, seen, min_score, seniority_block, dry_run):
    """处理一张卡：读面板 → 去重 → 判断 → 合规投递。

    返回 (go_on, sent):
        go_on=False 表示必须中止整个队列（验证码/日上限），其余情况 True
        sent=True   表示本条已投出（success/unknown）
    """
    ts = lambda: time.strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731

    # 1) 点开 → 读面板（内部已校验"面板确实切到了这张卡"）
    try:
        r = read_jd_for_job(idx)
    except Exception as e:
        r = {"ok": False, "detail": f"读卡异常: {e}"}
    if not r.get("ok"):
        _log({"ts": ts(), "action": "stream", "idx": idx,
              "status": "skip_nocard", "detail": r.get("detail")})
        print(f"[{idx}] 跳过(读卡失败) {str(r.get('detail'))[:60]}")
        return True, False

    jd = r["jd"]
    job_id = r.get("job_id_real") or jd.get("jobId") or ""
    title = jd.get("job_title", "")
    company = jd.get("company", "")
    salary = jd.get("salary", "")
    head = f"{title[:26]} | {company[:14]}"

    # 2) 去重：以 jobId 为准（处理过的都不再处理）
    if job_id and (job_id in done_ids or job_id in seen):
        _log({"ts": ts(), "action": "stream", "idx": idx, "jobId": job_id,
              "title": title, "company": company, "status": "skip_dup"})
        print(f"[{idx}] 跳过(已处理过) {head}")
        if job_id:
            seen.add(job_id)
        return True, False
    if job_id:
        seen.add(job_id)

    # 3) 判断（本地规则，零额外网络开销）
    j = judge_jd(jd)
    verdict, score = j.get("verdict"), j.get("score", 0)
    sen = _seniority_hits(jd)
    base = {"ts": ts(), "action": "stream", "idx": idx, "jobId": job_id,
            "title": title, "company": company, "salary": salary,
            "verdict": verdict, "score": score, "seniority_hits": sen,
            "reasons": (j.get("reasons") or [])[:6], "hits": j.get("hits", {})}

    if verdict != "APPROVE" or score < min_score:
        base["status"] = "skip_judge"
        _log(base)
        print(f"[{idx}] 跳过 {verdict} {score} | {head}")
        return True, False

    if sen and seniority_block:
        base["status"] = "skip_seniority"
        _log(base)
        print(f"[{idx}] 跳过(经验 {sen}) {verdict} {score} | {head}")
        return True, False

    # 4) 合规 → 当场投（复用刚读到的面板，不再重复点卡读 JD）
    print(f"[{idx}] ★ 命中 {verdict} {score} | {head} | {salary}")
    if dry_run:
        base["status"] = "dry_run"
        _log(base)
        print("        (dry-run 不投)")
        return True, False

    d = deliver(idx, confirm=True, preloaded={"jd": jd, "jobId": job_id})
    status = d.get("status", "fail")
    base.update(status="delivered" if status in ("success", "unknown") else status,
                deliver_status=status, deliver_detail=d.get("detail", ""))
    _log(base)

    if status in ("success", "unknown"):
        done_ids.add(job_id)
        btns = "/".join(b["text"] for b in d.get("buttons_after", []))
        print(f"        → {status.upper()} | 按钮: {btns or '?'}")
        return True, True
    if status == "captcha":
        print(f"        → 验证码中止: {d.get('detail', '')}")
        return False, False
    if "上限" in str(d.get("detail", "")):
        print(f"        → {d.get('detail')}")
        return False, False
    print(f"        → 跳过: {d.get('detail', status)}")
    return True, False


def _finish(scanned, delivered, stop_reason, elapsed):
    url = ""
    try:
        url = (evaluate("window.location.href") or "")[:110]
    except Exception:
        pass
    print("\n" + "=" * 62)
    print(f"流水线结束: 扫描 {scanned} 张 | 投出 {delivered} 条 | 耗时 {elapsed / 60:.1f} 分钟 | "
          f"{elapsed / max(scanned, 1):.1f}s/张")
    print(f"停止原因: {stop_reason}")
    print(f"页面: {unquote(url)}")
    print(f"断点: {STATE_FILE}（--resume 可接着扫）")
    print("=" * 62)


def main():
    p = argparse.ArgumentParser(description="智联流水线投递（读一条 → 判断 → 合规即投）")
    p.add_argument("--start", type=int, default=0, help="起始卡片序号（默认 0）")
    p.add_argument("--resume", action="store_true", help="从 zl_stream_state.json 的断点继续")
    p.add_argument("--max-scan", type=int, default=60, help="本次最多扫多少张卡（默认 60）")
    p.add_argument("--target", type=int, default=5, help="本次最多投多少条（默认 5）")
    p.add_argument("--min-score", type=int, default=95, help="投递所需最低分（默认 95）")
    p.add_argument("--dry-run", action="store_true", help="只判断不投递")
    p.add_argument("--yes", action="store_true", help="确认执行投递（不加则强制 dry-run）")
    p.add_argument("--no-seniority-block", action="store_true", help="不因经验年限拦截")
    a = p.parse_args()

    start = a.start
    if a.resume:
        st = _load_state()
        if st.get("keyword") and st["keyword"] != SEARCH_KEYWORD:
            print(f"错误: 断点关键词 '{st['keyword']}' 与当前配置 '{SEARCH_KEYWORD}' 不一致，"
                  f"不能续跑（用 --start 指定）")
            return 1
        start = int(st.get("next_idx") or 0)
        print(f"[续跑] 从 idx{start} 继续（上次: {st.get('stop_reason')}，"
              f"扫 {st.get('scanned')} 投 {st.get('delivered')}）")

    dry = a.dry_run or not a.yes
    if not a.yes and not a.dry_run:
        print("[闸门] 未加 --yes，自动降级为只读演练（只判断不投）\n")

    return run(start=start, max_scan=a.max_scan, target=a.target,
               min_score=a.min_score, dry_run=dry,
               seniority_block=not a.no_seniority_block)


if __name__ == "__main__":
    sys.exit(main())
