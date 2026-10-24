#!/usr/bin/env python3
"""
模块 C（智联版）: 职位列表页导航 + 分批懒加载 + 增量提取

对应 BOSS 版 recommend_loader.py，保留同一套对外 API 与返回结构:

    navigate_to_recommend(city_code)   → bool
    scroll_next_batch(cache_file)      → dict{batch_num, new_jobs, new_count,
                                              total_extracted, total_cards,
                                              exhausted, exhaust_reason}
    is_exhausted() / get_state() / reset_state()

★ 为什么还要分批（BOSS 版踩过的坑，智联同样存在）:
  1. 一口气滚到底再提取 → 滚到后面时前面的卡片已被虚拟列表卸掉，提取不全
  2. 整页提取完才筛选 → 首批岗位迟迟进不了流程，白等十几分钟
  → 每批滚到 ~BATCH_SIZE 条就立刻提取落盘，上层筛选/读取可以马上开工

★ 跨进程状态:
  上层每批以独立进程调用，模块级 _state 会重置，所以进度必须落到
  zl_batch_state.json，否则第二批会从 page_order=0 重来。

★ 智联特有点:
  - 列表滚动容器可能是内层 div（见 zhaopin_client.scroll_job_list）
  - 虚拟列表会导致卡片数回落，用 peak_cards 记录历史峰值判断新增

独立运行:
    python zl_recommend_loader.py --step navigate --city 530
    python zl_recommend_loader.py --step next-batch
    python zl_recommend_loader.py --step probe          # 打印真实 DOM，改选择器用
    python zl_recommend_loader.py --step all            # 一次性跑完所有批次（测试用）
"""

import json
import os
import random
import sys
import time

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL_DIR)

from scripts.profile_loader import (
    WORK_DIR, CITY_CODE as _PROFILE_CITY, build_url,
)
from scripts.zhaopin_client import (
    SESSION, CARD_SELECTOR, END_MARKERS,
    navigate, evaluate, health_check, attach_active_tab,
    scroll_job_list, extract_jobs, probe_dom, parse_salary_max, decrypt_salary,
)
from scripts.surface_cache import KEEP_FIELDS, append_cache

# ═══════════════════════════════════════════════════════════
# 配置常量（与 BOSS 版一致，便于两边行为对照）
# ═══════════════════════════════════════════════════════════

BATCH_SIZE = 50
MAX_SCROLLS_PER_BATCH = 8
RENDER_WAIT = 3
MAX_TOTAL_SCROLLS = 80
STABLE_THRESHOLD = 4

DEFAULT_CACHE_FILE = "zl_surface_cache.json"
BATCH_STATE_FILE = os.path.join(WORK_DIR, "zl_batch_state.json")

# ═══════════════════════════════════════════════════════════
# 模块级状态
# ═══════════════════════════════════════════════════════════

_state = {
    "total_cards": 0,        # 页面卡片数（历史峰值，虚拟列表会回落）
    "total_extracted": 0,    # 已提取条数
    "total_scrolls": 0,      # 累计滚动次数
    "exhausted": False,
    "exhaust_reason": "",
    "batch_num": 0,
    "page_url": "",          # 本次抓取所在页面，换页/换城市时用于判断状态失效
}


def _load_state():
    global _state
    if os.path.exists(BATCH_STATE_FILE):
        try:
            with open(BATCH_STATE_FILE, "r", encoding="utf-8") as f:
                _state.update(json.load(f))
        except (json.JSONDecodeError, IOError) as e:
            print(f"[状态] 加载 {BATCH_STATE_FILE} 失败: {e}，使用默认状态")
    return _state


def _save_state():
    try:
        with open(BATCH_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(_state, f, ensure_ascii=False, indent=2)
    except IOError as e:
        print(f"[状态] 保存 {BATCH_STATE_FILE} 失败: {e}")


# ═══════════════════════════════════════════════════════════
# 导航
# ═══════════════════════════════════════════════════════════

def _count_cards():
    raw = evaluate(f"String(document.querySelectorAll({json.dumps(CARD_SELECTOR)}).length)") or "0"
    try:
        return int(str(raw).strip())
    except (ValueError, TypeError):
        return 0


def navigate_to_recommend(city_code=None, url=None):
    """打开智联职位列表页

    参数:
        city_code: 智联 jl 编码（如 530=杭州），套进 URL 模板
        url: 直接指定完整地址，优先级高于 city_code

    返回:
        bool: 页面上是否已出现岗位卡片
    """
    target = url or build_url(city_code or _PROFILE_CITY)
    print(f"[导航] {target}")
    navigate(target, wait_range=(6, 9))

    current_url = str(evaluate("window.location.href") or "")
    if "zhaopin.com" not in current_url:
        print(f"[警告] 当前页面不在智联域名下: {current_url}")
        return False
    if "login" in current_url or "passport" in current_url:
        print(f"[警告] 疑似被跳到登录页，请先在浏览器里登录智联: {current_url}")
        return False

    time.sleep(2)
    card_count = _count_cards()
    if card_count == 0:
        print("[提示] 首屏无卡片，等待渲染后重试一次...")
        time.sleep(4)
        card_count = _count_cards()
    if card_count == 0:
        print(f"[警告] 仍未找到 `{CARD_SELECTOR}` 卡片。请用 --step probe 查看真实 DOM，"
              f"或用 --url 指定你浏览器里实际打开的列表页地址")

    print(f"[导航] 列表页已加载，当前卡片数: {card_count}")
    _state["total_cards"] = card_count
    _state["page_url"] = current_url
    _save_state()
    return card_count > 0


def _check_end_marker():
    """页面上是否已出现"没有更多了"之类的到底文案"""
    js = r"""
    (function(){
        var t = document.body.innerText || '';
        var ms = __MARKERS__;
        for (var i = 0; i < ms.length; i++) { if (t.indexOf(ms[i]) >= 0) return 'true'; }
        return 'false';
    })()
    """.replace("__MARKERS__", json.dumps(END_MARKERS, ensure_ascii=False))
    return (evaluate(js) or "false") == "true"


# ═══════════════════════════════════════════════════════════
# 增量提取
# ═══════════════════════════════════════════════════════════

def _cached_ids(cache_file):
    """已落盘的 jobId 集合，用于跨批去重（虚拟列表重复渲染时防重）"""
    if not os.path.exists(cache_file):
        return set()
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            return {j.get("jobId", "") for j in json.load(f)}
    except (json.JSONDecodeError, IOError):
        return set()


def _extract_new_jobs(from_index, cache_file=DEFAULT_CACHE_FILE):
    """从 from_index（已提取条数）往后取新岗位，并精简成 KEEP_FIELDS

    与 BOSS 版的差别: 额外按 jobId 过滤掉已落盘的条目（智联虚拟列表
    在滚动回弹时会重复渲染同一批卡片）。
    """
    jobs = extract_jobs()

    seen = set()
    unique = []
    for job in jobs:
        jid = job.get("jobId", "")
        if jid and jid not in seen:
            seen.add(jid)
            unique.append(job)

    # 用峰值更新卡片总数，避免虚拟列表回落造成"没有新增"的误判
    _state["total_cards"] = max(_state["total_cards"], len(unique))

    if len(unique) <= from_index:
        return []

    already = _cached_ids(cache_file)
    slim_jobs = []
    next_order = from_index
    for job in unique[from_index:]:
        if job.get("jobId") in already:
            continue
        job["salary"] = decrypt_salary(job.get("salary", ""))
        job["salary_max_k"] = parse_salary_max(job.get("salary", ""))
        job["page_order"] = next_order
        next_order += 1
        slim_jobs.append({k: job.get(k, "") for k in KEEP_FIELDS})
    return slim_jobs


# ═══════════════════════════════════════════════════════════
# 分批滚动
# ═══════════════════════════════════════════════════════════

def scroll_next_batch(cache_file=DEFAULT_CACHE_FILE):
    """滚动加载下一批并增量落盘

    返回 dict:
        {batch_num, new_jobs, new_count, total_extracted,
         total_cards, exhausted, exhaust_reason}
    """
    _load_state()

    if _state["exhausted"]:
        print(f"[分批] 已穷尽({_state['exhaust_reason']})，不再滚动；"
              f"累计 {_state['total_extracted']} 条")
        return {
            "batch_num": _state["batch_num"],
            "new_jobs": [],
            "new_count": 0,
            "total_extracted": _state["total_extracted"],
            "total_cards": _state["total_cards"],
            "exhausted": True,
            "exhaust_reason": _state["exhaust_reason"],
        }

    _state["batch_num"] += 1
    batch_num = _state["batch_num"]
    from_index = _state["total_extracted"]

    print(f"\n[分批] === 第{batch_num}批 === 从 page_order={from_index} 开始")
    print(f"[分批] 目标: 新增 {BATCH_SIZE} 条 (最多滚动 {MAX_SCROLLS_PER_BATCH} 次)")

    stable_count = 0
    batch_scrolls = 0

    while batch_scrolls < MAX_SCROLLS_PER_BATCH:
        if _state["total_scrolls"] >= MAX_TOTAL_SCROLLS:
            _state["exhausted"] = True
            _state["exhaust_reason"] = f"达到全局滚动上限{MAX_TOTAL_SCROLLS}次"
            print(f"[分批] {_state['exhaust_reason']}")
            _save_state()
            break

        if _check_end_marker():
            _state["exhausted"] = True
            _state["exhaust_reason"] = "检测到底部标记"
            print(f"[分批] {_state['exhaust_reason']}")
            _save_state()
            break

        info = scroll_job_list()
        _state["total_scrolls"] += 1
        batch_scrolls += 1
        time.sleep(random.uniform(0.8, 1.5))

        if batch_scrolls % 2 == 0:      # 每滚 2 次检查一次
            time.sleep(RENDER_WAIT)
            current = _count_cards()
            peak = max(_state["total_cards"], current)
            added = peak - _state["total_cards"]
            _state["total_cards"] = peak

            if added > 0:
                stable_count = 0
                print(f"[分批] 滚动{_state['total_scrolls']}: 卡片{current} (新增{added})")
            else:
                stable_count += 1
                print(f"[分批] 滚动{_state['total_scrolls']}: 卡片{current} "
                      f"(无新增, 稳定{stable_count}/{STABLE_THRESHOLD}, 容器={info.get('mode')})")

            if stable_count >= STABLE_THRESHOLD:
                _state["exhausted"] = True
                _state["exhaust_reason"] = f"连续{STABLE_THRESHOLD}次无新增"
                print(f"[分批] {_state['exhaust_reason']}")
                _save_state()
                break

            if peak - from_index >= BATCH_SIZE:
                print(f"[分批] 本批新增{peak - from_index}条，达到目标{BATCH_SIZE}，停止滚动")
                break

    new_jobs = _extract_new_jobs(from_index, cache_file)

    if new_jobs:
        append_cache(new_jobs, cache_file)
        _state["total_extracted"] += len(new_jobs)
        print(f"[分批] 第{batch_num}批提取 {len(new_jobs)} 条，"
              f"累计 {_state['total_extracted']} 条")
        for j in new_jobs[:5]:
            print(f"  [{j.get('page_order')}] {(j.get('title') or '')[:24]} | "
                  f"{j.get('salary') or '-'} | {(j.get('company') or '')[:14]} | "
                  f"{j.get('location') or ''}")
        if len(new_jobs) > 5:
            print(f"  ... 共{len(new_jobs)}条")
    else:
        print(f"[分批] 第{batch_num}批无新岗位")
        if not _state["exhausted"]:
            _state["exhausted"] = True
            _state["exhaust_reason"] = "本批无新岗位"

    _save_state()

    if not _state["exhausted"]:
        print(f"\n[分批] ★ 本批处理完后继续 --step next-batch 加载第{batch_num + 1}批")
    else:
        print(f"\n[分批] ★ 已穷尽: {_state['exhaust_reason']}；"
              f"累计 {_state['total_extracted']} 条")

    return {
        "batch_num": batch_num,
        "new_jobs": new_jobs,
        "new_count": len(new_jobs),
        "total_extracted": _state["total_extracted"],
        "total_cards": _state["total_cards"],
        "exhausted": _state["exhausted"],
        "exhaust_reason": _state["exhaust_reason"],
    }


def is_exhausted():
    _load_state()
    return _state["exhausted"]


def get_state():
    _load_state()
    return dict(_state)


def reset_state():
    _state.update({
        "total_cards": 0, "total_extracted": 0, "total_scrolls": 0,
        "exhausted": False, "exhaust_reason": "", "batch_num": 0, "page_url": "",
    })
    _save_state()
    print("[状态] 已重置分批进度")


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

def main():
    global BATCH_SIZE
    import argparse
    parser = argparse.ArgumentParser(description="智联: 列表页导航 + 分批懒加载")
    parser.add_argument("--step", default="next-batch",
                        choices=["navigate", "batch", "next-batch", "probe", "all"],
                        help="navigate=只导航; next-batch=滚一批; probe=打印DOM; all=跑完")
    parser.add_argument("--city", default=_PROFILE_CITY, help="智联 jl 城市编码")
    parser.add_argument("--url", default=None, help="直接指定列表页 URL（覆盖 --city）")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--cache", default=DEFAULT_CACHE_FILE)
    parser.add_argument("--reset", action="store_true", help="重置分批状态")
    args = parser.parse_args()

    BATCH_SIZE = args.batch_size

    if args.reset:
        # 只重置，不往下走：重置后必须停下来，由人决定下一步
        # （踩过坑：reset 曾落入默认 next-batch 分支直接开滚）
        reset_state()
        print("[提示] 分批状态已清零，缓存文件保留。确认页面后请手动执行 "
              "--step next-batch 或 --step navigate")
        sys.exit(0)

    if not health_check():
        print("WebBridge 不可达，请先确认浏览器扩展/daemon 已启动")
        sys.exit(1)

    # 只"借用"用户已经打开的智联标签页；附加失败一律报错退出，
    # 绝不自动新开页面（自动新开过一次杭州页，污染了缓存）。
    if not attach_active_tab(url_prefix=args.url or "https://www.zhaopin.com"):
        print("错误: 附加不到已打开的智联标签页。请先在浏览器里打开/切到智联列表页，"
              "或用 --url 指定地址前缀。本脚本不会自动新开页面。")
        sys.exit(1)

    if args.step == "navigate":
        ok = navigate_to_recommend(args.city, args.url)
        sys.exit(0 if ok else 1)

    if args.step == "probe":
        probe = probe_dom()
        print(f"URL: {probe.get('url')}")
        print(f"卡片数: {probe.get('card_count')} (选择器 `{CARD_SELECTOR}`)")

        print("\n--- 配置里的选择器命中情况 ---")
        for key, label in (("panel_hit", "JD 面板根"), ("jd_hit", "JD 正文"),
                           ("title_hit", "岗位标题"), ("salary_hit", "薪资")):
            hit = probe.get(key)
            if hit:
                print(f"  {label}: {hit['sel']}  (class={hit['cls']}, len={hit['len']})")
            else:
                print(f"  {label}: MISS —— 需要补选择器")
        print(f"  jobId 链接: {probe.get('jobid_hit') or 'MISS (卡片上无 jobId，需从面板取)'}")

        print("\n--- 前 3 张卡片解析结果 ---")
        for j in extract_jobs()[:3]:
            print(f"  jobId={j['jobId']} | {j['title'][:26]} | {j['salary']} | "
                  f"{j['company'][:18]} | {j['location']} | 经验={j['experience'] or '-'} "
                  f"学历={j['degree'] or '-'}")

        print("\n--- 第一张卡片 outerHTML (前 1500 字) ---")
        print((probe.get("first_card_html") or "(空)")[:1500])

        print("\n--- 右侧 JD 候选容器 (按文本量升序) ---")
        for c in probe.get("panel_candidates", []):
            print(f"  <{c['tag']} class=\"{c['cls']}\"> len={c['textLen']} left={c['left']}")
        sys.exit(0)

    if args.step == "all":
        if not navigate_to_recommend(args.city, args.url):
            sys.exit(1)
        while not is_exhausted():
            r = scroll_next_batch(args.cache)
            print(f"\n[批次{r['batch_num']}] 新增{r['new_count']} / "
                  f"累计{r['total_extracted']} / 穷尽={r['exhausted']}")
        st = get_state()
        print("\n" + "=" * 56)
        print(f"完成: 共{st['batch_num']}批, {st['total_extracted']}条, "
              f"滚动{st['total_scrolls']}次, 穷尽原因={st['exhaust_reason'] or '无'}")
        sys.exit(0)

    # 默认 next-batch
    if not _state["page_url"] and _count_cards() == 0:
        print("[提示] 当前页无卡片，先导航")
        if not navigate_to_recommend(args.city, args.url):
            sys.exit(1)
    r = scroll_next_batch(args.cache)
    print(f"\n[批次{r['batch_num']}] 新增{r['new_count']} / "
          f"累计{r['total_extracted']} / 穷尽={r['exhausted']}")
    sys.exit(0)


if __name__ == "__main__":
    main()
