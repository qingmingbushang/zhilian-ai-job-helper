#!/usr/bin/env python3
"""
模块 E（智联版）: 投递辅助 —— 预检只读，投递点击必须显式确认

设计原则（与用户约定一致）:
  1. precheck()  只读: 点卡读 JD、扫描投递按钮状态、复检经验年限，不点任何投递/沟通按钮
  2. deliver()   会真的点击投递，必须传 confirm=True 且由用户在会话中逐条确认后才允许
  3. 每一步结果落盘 zl_deliver_log.json，可追溯
"""
import json
import os
import random
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.zhaopin_client import attach_active_tab, evaluate
from scripts.zl_jd_reader import read_jd_for_job
from scripts.profile_loader import USER_PROFILE

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(os.path.dirname(HERE), "zl_deliver_log.json")

# 经验年限复检: 命中即跳过（日历经验不在 profile，先硬编码常用档位，
# 后续可挪进 profile 变成 seniority_block_keywords）
SENIORITY_RE = re.compile(r"([3-9]|10)[-~～到]\d*年|([3-9]|10)年以上|五年以上|三年以上|资深|高级")

# 面板上的动作按钮按文案匹配（不走 CSS 选择器，避免改版常量漂移）
_BTN_JS = r"""
(function(){
    var out = [];
    var root = document.querySelector('.job-detail-panel') || document.body;
    root.querySelectorAll('button, a, div[class*="btn"], span[class*="btn"]').forEach(function(el){
        var t = (el.innerText || '').trim().replace(/\s+/g, '');
        if (!t || t.length > 12) return;
        if (/投递|立即沟通|已投递|继续沟通|微信沟通/.test(t)) {
            var r = el.getBoundingClientRect();
            out.push({text: t, tag: el.tagName.toLowerCase(),
                      visible: r.width > 0 && r.height > 0,
                      disabled: !!el.disabled});
        }
    });
    return JSON.stringify(out);
})()
"""


# 日投递上限（软保护）：当日成功投递条数达到上限后拒绝继续投。
# 统计口径 = zl_deliver_log.json 里今日 action=deliver 且 status=success 的条数。
DAILY_DELIVER_LIMIT = 20

# 验证码/风控弹窗探测：只查弹窗与验证类节点文本，
# 不扫 body 全文（JD 正文里出现"验证码"字样会误报）
CAPTCHA_JS = r"""
(function(){
    var nodes = document.querySelectorAll('[class*="dialog"], [class*="modal"], [class*="popup"], [class*="captcha"], [class*="verify"], [class*="Dialog"], [class*="toast"]');
    for (var i = 0; i < nodes.length; i++) {
        var r = nodes[i].getBoundingClientRect();
        if (r.width < 60 || r.height < 20) continue;
        var t = (nodes[i].innerText || '');
        if (/验证码|安全验证|滑动验证|拖动滑块|captcha/i.test(t)) {
            return JSON.stringify({captcha: true, text: t.replace(/\s+/g, ' ').trim().substring(0, 120)});
        }
    }
    return JSON.stringify({captcha: false});
})()
"""


def _delivered_today():
    today = time.strftime("%Y-%m-%d")
    return sum(1 for e in _load_log()
               if e.get("action") == "deliver" and e.get("status") == "success"
               and str(e.get("ts", "")).startswith(today))


def _captcha_present():
    try:
        return json.loads(evaluate(CAPTCHA_JS) or "{}")
    except (json.JSONDecodeError, TypeError):
        return {"captcha": False}


def _load_log():
    if os.path.exists(LOG_FILE):
        return json.load(open(LOG_FILE, encoding="utf-8"))
    return []


def _save_log(log):
    tmp = LOG_FILE + ".tmp"
    json.dump(log, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, LOG_FILE)


def precheck(idx_str):
    """只读预检: 读 JD + 按钮状态 + 经验年限复检。返回 dict，不点任何投递按钮。"""
    r = read_jd_for_job(idx_str)
    if not r.get("ok"):
        return {"idx": idx_str, "ok": False, "detail": r.get("detail")}

    jd = r["jd"]
    full = (jd.get("jd_text") or "") + "\n" + (jd.get("full_text") or "")
    seniority_hits = sorted(set(m.group(0) for m in SENIORITY_RE.finditer(full)))

    btns_raw = evaluate(_BTN_JS) or "[]"
    try:
        btns = json.loads(btns_raw)
    except json.JSONDecodeError:
        btns = []

    already = any(("已投递" in b["text"]) for b in btns)
    can_send = any(re.search(r"投递", b["text"]) and "已" not in b["text"] for b in btns)

    result = {
        "idx": idx_str, "ok": True,
        "jobId": r.get("job_id_real"),
        "title": jd.get("job_title", ""), "company": jd.get("company", ""),
        "salary": jd.get("salary", ""),
        "seniority_hits": seniority_hits,
        "seniority_block": bool(seniority_hits),
        "buttons": btns,
        "already_sent": already,
        "can_send": can_send and not already,
    }
    log = _load_log()
    log.append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "action": "precheck", **result})
    _save_log(log)
    return result


# ═══════════════════════════════════════════════════════════
# 投递点击 / 弹窗扫描 —— 全部按文案匹配，不做 CSS 深度依赖
# ═══════════════════════════════════════════════════════════

# 点击右侧面板里的"立即投递"按钮（先复核可见性，点不到就如实上报，绝不换目标硬点）
CLICK_DELIVER_JS = r"""
(function(){
    var root = document.querySelector('.job-detail-panel') || document.body;
    var els = root.querySelectorAll('button, a, div[class*="btn"], span[class*="btn"]');
    for (var i = 0; i < els.length; i++) {
        var t = (els[i].innerText || '').trim().replace(/\s+/g, '');
        if (/^(立即投递|投递简历|投递)$/.test(t)) {
            var r = els[i].getBoundingClientRect();
            if (r.width > 0 && r.height > 0 && !els[i].disabled) {
                els[i].click();
                return JSON.stringify({clicked: true, text: t, tag: els[i].tagName.toLowerCase()});
            }
            return JSON.stringify({clicked: false, reason: 'not_clickable', text: t});
        }
    }
    return JSON.stringify({clicked: false, reason: 'button_not_found'});
})()
"""

# 扫描弹窗/对话框：记录文本 + 内部按钮（不点击）
POPUP_SCAN_JS = r"""
(function(){
    var out = [];
    var nodes = document.querySelectorAll('[class*="dialog"], [class*="modal"], [class*="popup"], [class*="Dialog"], [class*="Modal"]');
    var seen = {};
    for (var i = 0; i < nodes.length; i++) {
        var el = nodes[i];
        var r = el.getBoundingClientRect();
        if (r.width < 100 || r.height < 60) continue;
        var t = (el.innerText || '').trim().replace(/\s+/g, ' ');
        if (!t) continue;
        var cls = (el.className || '').toString();
        if (seen[cls]) continue;
        seen[cls] = 1;
        var btns = [];
        el.querySelectorAll('button, a, [class*="btn"]').forEach(function(b){
            var bt = (b.innerText || '').trim().replace(/\s+/g, '');
            if (bt && bt.length <= 10) btns.push(bt);
        });
        out.push({cls: cls.substring(0, 100), text: t.substring(0, 200), buttons: btns});
    }
    // toast 类提示（投递成功 / 已投递 等）
    var toasts = document.querySelectorAll('[class*="toast"], [class*="message"], [class*="notice"]');
    for (var j = 0; j < toasts.length; j++) {
        var r2 = toasts[j].getBoundingClientRect();
        if (r2.width < 60 || r2.height < 20) continue;
        var t2 = (toasts[j].innerText || '').trim().replace(/\s+/g, ' ');
        if (t2 && t2.length <= 60) out.push({cls: 'toast', text: t2, buttons: []});
    }
    return JSON.stringify(out);
})()
"""

# 弹窗内确认：只点文案严格为 确认/确定/确认投递 的按钮，找不到就不动
POPUP_CONFIRM_JS = r"""
(function(){
    var nodes = document.querySelectorAll('[class*="dialog"], [class*="modal"], [class*="popup"], [class*="Dialog"], [class*="Modal"]');
    var clicked = false, target = '';
    for (var i = 0; i < nodes.length; i++) {
        var el = nodes[i];
        var r = el.getBoundingClientRect();
        if (r.width < 100 || r.height < 60) continue;
        var btns = el.querySelectorAll('button, a, [class*="btn"]');
        for (var k = 0; k < btns.length; k++) {
            var t = (btns[k].innerText || '').trim().replace(/\s+/g, '');
            if (/^(确认投递|确认|确定)$/.test(t)) {
                var br = btns[k].getBoundingClientRect();
                if (br.width > 0 && br.height > 0 && !btns[k].disabled) {
                    btns[k].click();
                    clicked = true; target = t;
                    break;
                }
            }
        }
        if (clicked) break;
    }
    return JSON.stringify({confirmed: clicked, text: target});
})()
"""

# 关闭投递成功弹窗: 只点"留在此页"（不跳转不离开当前页）
POPUP_STAY_JS = r"""
(function(){
    var els = document.querySelectorAll('button, a, [class*="btn"]');
    for (var i = 0; i < els.length; i++) {
        var t = (els[i].innerText || '').trim().replace(/\s+/g, '');
        if (t === '留在此页') {
            var r = els[i].getBoundingClientRect();
            if (r.width > 0 && r.height > 0) { els[i].click(); return JSON.stringify({dismissed: true}); }
        }
    }
    return JSON.stringify({dismissed: false});
})()
"""

# 扫描面板按钮状态（与 _BTN_JS 同源）
def _scan_buttons():
    try:
        raw = evaluate(_BTN_JS) or "[]"
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []


def _log(entry):
    log = _load_log()
    log.append(entry)
    _save_log(log)


def deliver(idx_str, confirm=False, preloaded=None):
    """真投递。confirm 必须显式为 True（会话中用户逐条确认后由调用方传入）。

    流程（闸门 2 = 点击前的实时状态复检）:
      1. read_jd_for_job 定位卡片并切面板（顺带拿到 jobId/标题等台账信息）
      2. 实时扫按钮: 已投递 → skip；无可点击投递按钮 → skip；异常 → skip
      3. 点击"立即投递" → 扫弹窗 → 弹窗内只点文案严格为 确认/确认投递 的按钮
      4. 复扫按钮: 出现"已投递" → success；否则 status=unknown（不重试不硬点）
      5. 全过程落盘 zl_deliver_log.json
    """
    if not confirm:
        return {"idx": idx_str, "ok": False,
                "detail": "confirm=False，拒绝投递。请先跑 precheck 并获得用户确认。"}

    entry = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "action": "deliver", "idx": idx_str}

    # 1) 定位卡片 + 切面板（失败/异常 → skip）
    #    preloaded: 调用方（流水线模式）刚读过同一张卡的面板，复用可省一次点击+渲染等待，
    #    数据来源仍是面板本身，不降低校验强度。
    if preloaded:
        r = {"ok": True, "jd": preloaded.get("jd") or {},
             "job_id_real": preloaded.get("jobId") or ""}
        entry["panel_reused"] = True
    else:
        try:
            r = read_jd_for_job(idx_str)
        except Exception as e:
            r = {"ok": False, "detail": f"读卡异常: {e}"}
    if not r.get("ok"):
        entry.update(ok=False, status="skip", detail=f"定位卡片失败: {r.get('detail')}")
        _log(entry)
        return entry

    jd = r["jd"]
    entry.update(jobId=r.get("job_id_real"), title=jd.get("job_title", ""),
                 company=jd.get("company", ""), salary=jd.get("salary", ""))

    # 日上限软保护
    done_today = _delivered_today()
    if done_today >= DAILY_DELIVER_LIMIT:
        entry.update(ok=True, status="skip",
                     detail=f"已达当日投递上限 {DAILY_DELIVER_LIMIT} 条（今日已成功 {done_today} 条）")
        _log(entry)
        return entry

    # 闸门 2 之一: 投递前先看有没有验证码/风控弹窗
    cap = _captcha_present()
    if cap.get("captcha"):
        entry.update(ok=True, status="captcha", detail="检测到验证码/风控弹窗，停下等用户处理",
                     captcha=cap)
        _log(entry)
        return entry

    # 闸门 2: 实时按钮状态
    btns_before = _scan_buttons()
    entry["buttons_before"] = btns_before
    if any("已投递" in b["text"] for b in btns_before):
        entry.update(ok=True, status="skip", detail="已是投递状态（已投递）")
        _log(entry)
        return entry
    clickable = [b for b in btns_before
                 if b.get("visible") and not b.get("disabled")
                 and re.search(r"投递", b["text"]) and "已" not in b["text"]]
    if not clickable:
        entry.update(ok=True, status="skip", detail="无可点击的投递按钮")
        _log(entry)
        return entry

    # 3) 点击 → 弹窗扫描 → 弹窗确认
    try:
        click_raw = evaluate(CLICK_DELIVER_JS) or "{}"
        entry["click"] = json.loads(click_raw)
    except (json.JSONDecodeError, TypeError) as e:
        entry["click"] = {"error": str(e)}
    if not entry["click"].get("clicked"):
        entry.update(ok=True, status="skip",
                     detail=f"点击未执行: {entry['click'].get('reason', 'unknown')}")
        _log(entry)
        return entry

    time.sleep(2.5)
    try:
        entry["popup"] = json.loads(evaluate(POPUP_SCAN_JS) or "[]")
    except (json.JSONDecodeError, TypeError):
        entry["popup"] = []

    # 点击后仍可能出现验证码: 停下不确认、不重试，交给用户
    cap2 = _captcha_present()
    if cap2.get("captcha"):
        entry["captcha"] = cap2
        entry.update(ok=True, status="captcha",
                     detail="点击后出现验证码/风控弹窗，停下等用户处理（本条不重试）")
        _log(entry)
        return entry

    try:
        entry["popup_confirm"] = json.loads(evaluate(POPUP_CONFIRM_JS) or "{}")
    except (json.JSONDecodeError, TypeError):
        entry["popup_confirm"] = {}

    # 4) 验证: 复扫按钮 + 当前 URL
    time.sleep(2.5)
    btns_after = _scan_buttons()
    entry["buttons_after"] = btns_after
    try:
        entry["url_after"] = (evaluate("window.location.href") or "")[:120]
    except Exception:
        entry["url_after"] = ""
    sent = any("已投递" in b["text"] for b in btns_after)
    # ★ 智联实测: 投递成功后按钮不是"已投递"而是"继续沟通"，
    #   且弹出 deliver-greeting-modal 写明"已向对方发送简历和打招呼语"。
    if not sent:
        sent = any("继续沟通" in b["text"] for b in btns_after)
    if not sent:
        sent = any("已向对方发送简历" in (p.get("text") or "") for p in entry["popup"])
    entry["status"] = "success" if sent else "unknown"
    if not sent:
        entry["detail"] = "点击已执行但未确认投递成功，标记 unknown 不重试（请人工核实）"
    entry["ok"] = True
    # 关掉投递成功弹窗（"留在此页"= 不跳转，安全）
    time.sleep(0.5)
    try:
        entry["popup_dismiss"] = json.loads(evaluate(POPUP_STAY_JS) or "{}")
    except (json.JSONDecodeError, TypeError):
        entry["popup_dismiss"] = {}
    _log(entry)
    return entry


def _attach_or_reuse():
    """优先附加前台智联标签页；失败时沿用已绑定会话（绝不新开页面）。"""
    if attach_active_tab(url_prefix="https://www.zhaopin.com"):
        return True
    url = evaluate("window.location.href") or ""
    if "zhaopin.com" in url:
        print(f"[标签页] 前台无智联页，沿用已绑定会话: {url[:60]}")
        return True
    return False


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="智联: 投递预检（只读）/ 投递（需 --yes）")
    parser.add_argument("--precheck", nargs="+", help="卡片序号列表，如 idx91 idx13")
    parser.add_argument("--deliver", nargs="+", metavar="IDX",
                        help="按顺序逐条投递（每条内部含闸门2实时复检，异常自动跳过）")
    parser.add_argument("--yes", action="store_true",
                        help="投递确认开关；不带则拒绝执行")
    args = parser.parse_args()

    if not args.deliver and not args.precheck:
        parser.print_help()
        sys.exit(1)
    if args.deliver and not args.yes:
        print("错误: --deliver 必须搭配 --yes 才会执行投递")
        sys.exit(1)
    if not _attach_or_reuse():
        print("错误: 附加不到智联标签页且已绑定会话无效。请先在浏览器打开智联列表页")
        sys.exit(1)

    if args.precheck:
        for idx in args.precheck:
            p = precheck(idx)
            if not p.get("ok"):
                print(f"[{idx}] 预检失败: {p.get('detail')}")
                continue
            print(f"[{idx}] {p['title'][:24]} | {p['company'][:14]} | {p['salary']} | jobId={p['jobId']}")
            print(f"      经验复检: {'命中 ' + str(p['seniority_hits']) + ' → 建议跳过' if p['seniority_block'] else '未命中经验年限'}")
            print(f"      按钮: {[(b['text'], b['visible']) for b in p['buttons']]}")
            print(f"      状态: can_send={p['can_send']} already_sent={p['already_sent']}")

    if args.deliver:
        summary = []
        for idx in args.deliver:
            d = deliver(idx, confirm=True)
            summary.append(d)
            print(f"[{idx}] {d.get('status', 'fail')} | {d.get('title', '')[:24]} | "
                  f"{d.get('detail') or d.get('click', {}).get('reason', '')}")
            if d.get("status") == "captcha":
                print("检测到验证码/风控弹窗，中止剩余投递队列，请人工处理后重跑。")
                break
            time.sleep(random.uniform(3, 5))  # 条与条之间留呼吸间隔
        print("\n=== 投递汇总 ===")
        for d in summary:
            print(f"  {d['idx']}: {d.get('status', 'fail')} - {d.get('title', '')[:28]}")
