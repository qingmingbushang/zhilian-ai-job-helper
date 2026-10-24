#!/usr/bin/env python3
"""
模块 E（智联版）: 列表页内原地点击卡片读右侧 JD

对应 BOSS 版 jd_reader.py，流程一致:
  1. 按 jobId 在左侧列表定位卡片
  2. 滚到可视区域
  3. 原生点击（临时拦截 <a> 默认跳转，只让 SPA 切换右侧面板）
  4. 校验右侧面板确实换成了目标岗位
  5. 读 JD 全文

★ 智联特有的三件事:
  - 卡片本身常常就是 <a href=".../jobdetail/CCxxxJxxx.htm">，直接 click 会整页跳走。
    所以点前挂一个 capture 阶段的一次性 preventDefault 守卫，点后 500ms 自动摘除。
  - 列表和右侧面板同处一页，且卡片和面板的 class 极可能同名（title / salary / location）。
    ★ 所以读 JD 必须先解析出"面板根节点"，所有字段都在面板内取，
      绝不能在 document 上直接 firstText —— 否则读到的是左侧第一张卡片的数据。
  - 面板若缺少公司/地点等字段，用点击时抓到的卡片快照补齐。

返回结构与 BOSS 版一致:
    read_jd_from_panel() → {jd_text, job_title, company, salary, salary_decrypted,
                            city, experience, degree, boss_name, boss_title, full_text}
    read_jd_for_job(id)  → {ok, detail, job_id, jd}

独立运行:
    python zl_jd_reader.py CC123456789J40000000000
    python zl_jd_reader.py idx3 --output jd.json
    python zl_jd_reader.py --detail-url https://www.zhaopin.com/jobdetail/CC...htm
"""

import json
import os
import re
import sys
import time

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL_DIR)

from scripts.zhaopin_client import (
    SESSION, CARD_SELECTOR, JD_QUERY,
    evaluate, navigate, decrypt_salary, _build_js,
)

# ═══════════════════════════════════════════════════════════
# 面板根节点解析 —— 列表页/详情页共用
# ═══════════════════════════════════════════════════════════

_JS_PANEL_HELPERS = r"""
    var JD_KWS = ['职位描述', '岗位职责', '任职要求', '工作职责', '职位要求', '岗位要求'];

    function hasJdKw(t) {
        for (var k = 0; k < JD_KWS.length; k++) { if (t.indexOf(JD_KWS[k]) >= 0) return true; }
        return false;
    }

    // 面板根 = 右半屏、含 JD 关键词的最大容器（列表页能框住标题/薪资/公司；
    // 详情页则直接圈到详情主体）
    function resolvePanelRoot() {
        for (var i = 0; i < Q.panel.length; i++) {
            try {
                var el = document.querySelector(Q.panel[i]);
                if (el && (el.innerText || '').trim().length >= 80) return el;
            } catch (e) {}
        }
        var nodes = document.querySelectorAll('div, section, article, main');
        var best = null, bestLen = 0;
        for (var i = 0; i < nodes.length; i++) {
            var el = nodes[i];
            var r = el.getBoundingClientRect();
            if (r.width < 240 || r.left < window.innerWidth * 0.35) continue;
            var t = (el.innerText || '').trim();
            if (t.length < 80 || !hasJdKw(t)) continue;
            if (t.length > bestLen) { best = el; bestLen = t.length; }
        }
        return best;
    }

    // 面板内 JD 正文 = 含关键词的最内层容器
    function innermostJd(root) {
        var best = null, bestLen = 0;
        var nodes = root.querySelectorAll('div, section, article, p');
        for (var i = 0; i < nodes.length; i++) {
            var el = nodes[i];
            var t = (el.innerText || '').trim();
            if (t.length < 60 || !hasJdKw(t)) continue;
            var childHit = false;
            for (var c = 0; c < el.children.length; c++) {
                var ct = (el.children[c].innerText || '').trim();
                if (ct.length >= 60 && ct.length >= t.length * 0.6) { childHit = true; break; }
            }
            if (childHit) continue;
            if (t.length > bestLen) { best = el; bestLen = t.length; }
        }
        return best;
    }

    // 先在面板内找，找不到再往外一层，最后才退到 document
    function firstTextUp(root, sels) {
        if (!root) return '';
        var t = firstText(root, sels);
        if (t) return t;
        if (root && root.parentElement) {
            t = firstText(root.parentElement, sels);
            if (t) return t;
        }
        return '';
    }
"""

# ═══════════════════════════════════════════════════════════
# 定位 / 点击
# ═══════════════════════════════════════════════════════════

_FIND_CARD_JS = r"""
(function(targetId){
__HELPERS__
    var cards = document.querySelectorAll(CARD);
    if (!cards.length) return JSON.stringify({found: false, detail: 'no_cards_on_page'});
    var card = findCardById(cards, targetId);
    if (!card) return JSON.stringify({found: false, detail: 'no_card_match', cards: cards.length});
    card.scrollIntoView({behavior: 'smooth', block: 'center'});
    return JSON.stringify({
        found: true,
        title: firstText(card, Q.title),
        company: firstText(card, Q.company),
        salary: findSalary(card),
        location: firstText(card, Q.location),
        href: pickLink(card)
    });
})(__TID__)
"""

_CLICK_CARD_JS = r"""
(function(targetId){
__HELPERS__
    var cards = document.querySelectorAll(CARD);
    var card = findCardById(cards, targetId);
    if (!card) return JSON.stringify({ok: false, detail: 'no_card_match'});

    // 一次性拦截 <a> 默认跳转，防止整页离开列表
    var guard = function(e){
        var t = e.target;
        var a = (t && t.closest) ? t.closest('a') : null;
        if (a) { e.preventDefault(); }
    };
    document.addEventListener('click', guard, true);

    // ★ 点哪里很重要：
    //   新版卡片里唯一的 <a> 是"公司主页"链接，点它不会切换右侧面板。
    //   实测点标题区 (.job-card__title-main) 才会切换，卡片变 .job-card--active。
    var target = card.querySelector('.job-card__title-main')
              || card.querySelector('.job-card__title')
              || card;
    var clicked = 'card';
    try { target.click(); clicked = (target.className || 'card').toString().split(' ')[0]; } catch (e) {}
    if (clicked === 'card') { try { card.click(); } catch (e) {} }

    setTimeout(function(){ document.removeEventListener('click', guard, true); }, 500);

    return JSON.stringify({ok: true, clicked: clicked, title: firstText(card, Q.title)});
})(__TID__)
"""


def click_card_inplace(job_id, verify=True, max_wait=5):
    """在列表页原地点击指定岗位卡片，切换右侧 JD 面板

    返回:
        dict: {"ok", "detail", "title", "href", "card"}
              card = 点击时抓到的卡片快照（title/company/salary/location），
              用于补齐右侧面板可能缺失的字段
    """
    tid = json.dumps(job_id)

    raw = evaluate(_build_js(_FIND_CARD_JS).replace("__TID__", tid)) or "{}"
    try:
        found = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"ok": False, "detail": f"定位脚本返回异常: {raw[:120]}",
                "title": "", "href": "", "card": {}}

    if not found.get("found"):
        return {"ok": False,
                "detail": f"未找到 jobId={job_id} 的卡片（{found.get('detail', '?')}）",
                "title": "", "href": "", "card": {}}

    card_info = {k: found.get(k, "") for k in ("title", "company", "salary", "location")}
    title = found.get("title", "")
    href = found.get("href", "")

    time.sleep(1.5)   # 等 scrollIntoView 完成

    click_raw = evaluate(_build_js(_CLICK_CARD_JS).replace("__TID__", tid)) or "{}"
    try:
        clicked = json.loads(click_raw)
    except (json.JSONDecodeError, TypeError):
        clicked = {"ok": False, "detail": click_raw[:120]}
    if not clicked.get("ok"):
        return {"ok": False, "detail": f"点击失败: {clicked.get('detail', '?')}",
                "title": title, "href": href, "card": card_info}

    time.sleep(2)     # 等右侧面板渲染

    if verify:
        m = re.match(r"^idx(\d+)$", str(job_id))
        expect_index = int(m.group(1)) if m else None
        v = _verify_panel(title, expect_index, max_wait)
        if not v["ok"]:
            return {"ok": False, "detail": f"右侧面板校验失败: {v['detail']}",
                    "title": title, "href": href, "card": card_info}

    return {"ok": True, "detail": "卡片已点击，右侧面板已切换",
            "title": title, "href": href, "card": card_info}


_VERIFY_JS = r"""
(function(){
__HELPERS__
__PANEL__
    var root = resolvePanelRoot();
    if (!root) return JSON.stringify({len: 0, text: '', active: -1});
    var t = (root.innerText || '').trim();

    // 顺带报告"哪张卡片被选中"（新版会给选中的卡片加 job-card--active）
    var active = -1;
    var cards = document.querySelectorAll(CARD);
    for (var i = 0; i < cards.length; i++) {
        if ((cards[i].className || '').toString().indexOf('job-card--active') >= 0) { active = i; break; }
    }
    return JSON.stringify({len: t.length, text: t.substring(0, 4000), active: active});
})()
"""


def _verify_panel(expect_title="", expect_index=None, max_wait=5):
    """校验右侧面板确实切到了目标岗位

    两个判据满足其一即通过:
      1. 面板文本里出现目标卡片标题
      2. 选中卡片的索引等于目标索引（新版卡片带 .job-card--active）
    """
    js = _build_js(_VERIFY_JS, query=JD_QUERY,
                   tags=JD_QUERY.get("tags")).replace("__PANEL__", _JS_PANEL_HELPERS)
    deadline = time.time() + max_wait
    last_len, last_active = 0, -1
    while time.time() < deadline:
        raw = evaluate(js) or "{}"
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            data = {}
        text = data.get("text") or ""
        last_len = len(text)
        last_active = data.get("active", -1)
        if last_len > 120:
            if expect_title and (expect_title in text or text[:80] in expect_title):
                return {"ok": True, "detail": f"面板已切换 (标题匹配, len={last_len})"}
            if expect_index is not None and last_active == expect_index:
                return {"ok": True,
                        "detail": f"面板已切换 (选中卡片索引={last_active}, len={last_len})"}
        time.sleep(1)
    return {"ok": False,
            "detail": f"等待{max_wait}s未校验到目标岗位 (面板长度={last_len}, 选中索引={last_active})"}


# ═══════════════════════════════════════════════════════════
# JD 读取
# ═══════════════════════════════════════════════════════════

_READ_PANEL_JS = r"""
(function(){
__HELPERS__
__PANEL__
    var root = resolvePanelRoot();
    var scope = root || document.body;

    // ★ 所有字段都在面板内取，避免读到左侧列表卡片
    var jdText = firstText(scope, Q.jd_text);
    if (!jdText || jdText.length < 60) {
        var inner = innermostJd(scope);
        if (inner) jdText = (inner.innerText || '').trim();
    }
    if (!jdText) jdText = (scope.innerText || '').trim();

    var title = firstTextUp(root, Q.title);
    var salary = firstTextUp(root, Q.salary);
    var company = firstTextUp(root, Q.company);
    var location = firstTextUp(root, Q.location);

    var experience = '', degree = '', tags = [];
    var tEls = scope.querySelectorAll(TAG_SEL);
    for (var i = 0; i < tEls.length; i++) {
        var t = txt(tEls[i]);
        if (!t || t.length > 20) continue;
        tags.push(t);
        if (!experience && (t.indexOf('年') >= 0 || t.indexOf('经验') >= 0)) experience = t;
        else if (!degree && /本科|大专|硕士|博士|中专|高中|学历|不限/.test(t)) degree = t;
    }

    var recruiter = firstText(scope, Q.recruiter) || firstTextUp(root, Q.recruiter);
    var recruiterName = firstText(scope, Q.recruiter_name) || firstTextUp(root, Q.recruiter_name);

    // ★ 卡片本身没有 jobId，真实 jobId 只能从面板里的"查看详情"链接拿到
    var jobId = '';
    var jlink = scope.querySelector('a[href*="jobdetail"]')
             || document.querySelector('a[href*="jobdetail"]');
    if (jlink) {
        var jm = (jlink.getAttribute('href') || '').match(/CC[A-Z]?\d+J\d+/);
        if (jm) jobId = jm[0];
    }

    return JSON.stringify({
        jd_text: jdText,
        job_title: title,
        salary: salary,
        company: company,
        city: location,
        experience: experience,
        degree: degree,
        recruiter: recruiter,
        recruiter_name: recruiterName,
        job_id: jobId,
        tags: tags.slice(0, 15),
        full_text: (scope.innerText || '').trim()
    });
})()
"""

_EMPTY_JD = {
    "jd_text": "", "job_title": "", "company": "", "salary": "", "salary_decrypted": "",
    "city": "", "experience": "", "degree": "", "boss_name": "", "boss_title": "",
    "full_text": "", "recruiter": "", "tags": [], "jobId": "",
}

# 面板标签列表里出现这些值时，说明它不是城市
_NOT_CITY = re.compile(
    r"^(\d+\s*(-\s*\d+)?\s*年|经验不限|应届|在校|\d+\s*年以上|\d+\s*个月|"
    r"本科|大专|硕士|博士|中专|高中|学历|不限)$")


def _split_recruiter(text):
    """把招聘者信息拆成 姓名 / 职位（对应 BOSS 版的 boss_name / boss_title）"""
    if not text:
        return "", ""
    parts = [p.strip() for p in str(text).replace("·", "\n").split("\n") if p.strip()]
    if not parts:
        return "", ""
    return parts[0], (parts[1] if len(parts) > 1 else "")


def _split_publisher(text, name=""):
    """智联发布者块形如:
        江雪 / 在线 / 刚刚活跃 / 总回复 30+ / 人才经纪人-经营性招聘服务 · 科锐尔人力资源服务(苏州)有限公司
        杨玲玲 在线 刚刚活跃 总回复 30+ HR · 上海遨有信息技术有限公司      ← 单行紧凑形态
    → ("人才经纪人-经营性招聘服务" / "HR", "科锐尔...公司" / "上海遨有...公司")

    先把"在线/刚刚活跃/总回复 30+/姓名"这些噪声剔掉，只留职位；
    公司取 " · " 右侧那一段（才是列表上显示的招聘主体）。
    """
    if not text:
        return "", ""
    for line in str(text).split("\n"):
        left, sep, right = line.partition(" · ")
        left = re.sub(r"(总回复|回复)\s*\d+\+?", " ", left)
        left = re.sub(r"(在线|刚刚活跃|今日活跃|本月活跃|离线)", " ", left)
        tokens = [t for t in left.split() if t]
        if name and tokens and tokens[0] == name:
            tokens = tokens[1:]
        title = " ".join(tokens).strip()
        company = right.strip() if sep else ""
        if title or company:
            return title, company
    return "", ""


def _clean_name(text):
    """发布者姓名：'.job-detail-publisher__name' 通常只含姓名，兜底时取首行首个 token"""
    if not text:
        return ""
    first = str(text).split("\n")[0].strip()
    return first.split(" ")[0] if first else ""


def read_jd_from_panel(card_info=None):
    """读右侧面板 JD，返回结构与 BOSS 版一致（额外带 recruiter / tags / jobId）

    参数:
        card_info: 点击卡片时抓到的快照，面板缺字段时用它补齐
    """
    js = _build_js(_READ_PANEL_JS, query=JD_QUERY,
                   tags=JD_QUERY.get("tags")).replace("__PANEL__", _JS_PANEL_HELPERS)
    raw = evaluate(js) or "{}"
    try:
        result = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        result = {}

    card_info = card_info or {}
    title = result.get("job_title") or card_info.get("title", "")
    # ★ 公司名优先取卡片上的：代招岗位的面板会显示"客户公司：xxx"，
    #   而列表卡片显示的才是招聘主体（如外包/人力公司）。
    company = card_info.get("company") or result.get("company", "")
    salary = result.get("salary") or card_info.get("salary", "")
    city = result.get("city") or ""
    if _NOT_CITY.match(city or ""):      # 标签列表里第一个不一定是城市
        city = ""
    city = city or card_info.get("location", "")

    recruiter = result.get("recruiter", "")
    name = _clean_name(result.get("recruiter_name")) or _clean_name(recruiter)
    pub_title, pub_company = _split_publisher(recruiter, name)
    if not company:
        company = pub_company

    return {
        "jd_text": result.get("jd_text", ""),
        "job_title": title,
        "company": company,
        "salary": salary,
        "salary_decrypted": decrypt_salary(salary),
        "city": city,
        "experience": result.get("experience", ""),
        "degree": result.get("degree", ""),
        "boss_name": name,
        "boss_title": pub_title,
        "full_text": result.get("full_text", ""),
        "recruiter": recruiter,
        "tags": result.get("tags", []),
        "jobId": result.get("job_id", ""),
    }


def read_jd_for_job(job_id, verify=True):
    """完整流程: 点击卡片 → 读 JD，返回结构与 BOSS 版一致

    额外返回 job_id_real: 从面板链接里取到的真实 jobId（卡片上没有），
    上层可用它回填 surface_cache 里的 idxN 占位 id。
    """
    click_result = click_card_inplace(job_id, verify=verify)
    if not click_result["ok"]:
        return {"ok": False, "detail": click_result["detail"], "job_id": job_id,
                "job_id_real": "", "jd": {}}

    jd = read_jd_from_panel(click_result.get("card"))
    if not jd.get("jd_text") and not jd.get("full_text"):
        return {"ok": False,
                "detail": "右侧面板无内容（可能是纯跳转型列表，试试 --detail-url 方式）",
                "job_id": job_id, "job_id_real": jd.get("jobId", ""), "jd": jd}

    return {"ok": True, "detail": "JD 读取成功", "job_id": job_id,
            "job_id_real": jd.get("jobId", ""), "jd": jd}


def read_jd_via_detail(url, go_back=True):
    """兜底方案: 直接打开详情页读 JD，读完回列表

    适用于列表页没有右侧面板、点击即整页跳转的情况。
    history.back() 后列表页是 SPA 会保持状态，卡片顺序不变。
    """
    print(f"[JD] 打开详情页: {url}")
    navigate(url, wait_range=(5, 8))
    jd = read_jd_from_panel()
    if go_back:
        evaluate("history.back()")
        time.sleep(2.5)
    ok = bool(jd.get("jd_text") or jd.get("full_text"))
    return {"ok": ok,
            "detail": "详情页 JD 读取成功" if ok else "详情页无 JD 内容",
            "job_id": "", "jd": jd}


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="智联: 列表页内原地点击卡片读 JD")
    parser.add_argument("jobId", nargs="?", help="岗位 ID（CCxxxJxxx）或提取阶段的 idxN")
    parser.add_argument("--detail-url", help="改为直接打开详情页读 JD（兜底模式）")
    parser.add_argument("--output", help="输出 JSON 路径")
    parser.add_argument("--no-verify", action="store_true", help="跳过面板校验")
    args = parser.parse_args()

    if args.detail_url:
        result = read_jd_via_detail(args.detail_url)
    elif args.jobId:
        print(f"[JD] jobId={args.jobId}")
        result = read_jd_for_job(args.jobId, verify=not args.no_verify)
    else:
        parser.error("需要 jobId 或 --detail-url")

    if not result["ok"]:
        print(f"失败: {result['detail']}")
        sys.exit(1)

    jd = result["jd"]
    print(f"岗位: {jd.get('job_title') or '?'}")
    print(f"公司: {jd.get('company') or '?'}")
    print(f"薪资: {jd.get('salary') or '?'}")
    print(f"地点: {jd.get('city') or '?'}")
    print(f"经验: {jd.get('experience') or '?'}  学历: {jd.get('degree') or '?'}")
    print(f"招聘者: {jd.get('boss_name') or '?'} {jd.get('boss_title') or ''}")
    print(f"\nJD 正文 ({len(jd.get('jd_text', ''))} 字):")
    print((jd.get("jd_text") or "(空)")[:500])

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n已保存至 {args.output}")


if __name__ == "__main__":
    main()
