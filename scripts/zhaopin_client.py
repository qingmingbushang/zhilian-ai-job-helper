#!/usr/bin/env python3
"""
智联招聘 WebBridge 客户端 — 共享模块（BOSS 版 webbridge_client.py 的智联对应实现）

★ 定位:
  BOSS 版靠 Vue 实例 `__vue__.$props.data` 拿结构化数据；智联列表页没有这层，
  只能依赖 DOM 选择器。本模块把所有选择器集中在 QUERY / JD_QUERY 里，
  上层脚本不出现任何硬编码选择器，改版时只改这里。

★ 与 BOSS 版的接口对齐（上层可直接套用同一套调用代码）:
    api / evaluate / navigate / click
    extract_jobs()            # → list[dict]，字段与 BOSS 版同构
    decrypt_salary()          # 智联无字体加密，保留同名接口返回原串
    parse_salary_max()        # 支持 万 / 千 / K / 元每月
    health_check / ensure_active_tab

★ 智联独有的东西:
    scroll_job_list()         # 列表可能在独立滚动容器里，不能只滚 window
    find_card_js / probe      # DOM 探测，改版后一条命令看清真实结构

仅读取，不涉及投递。
"""

import http.client
import json
import os
import random
import re
import sys
import time

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

from scripts.profile_loader import SESSION_NAME

# ═══════════════════════════════════════════════════════════
# 传输层常量
# ═══════════════════════════════════════════════════════════

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 10086
DEFAULT_TIMEOUT = 60
MAX_RETRIES = 3
RETRY_DELAY = 3

SESSION = SESSION_NAME

# ═══════════════════════════════════════════════════════════
# ★ 选择器配置 —— 智联改版时只需要改这一段
# ═══════════════════════════════════════════════════════════

# 岗位卡片根节点（用户给定；2026-10 新版分栏页实测 40 张/屏）
CARD_SELECTOR = ".job-card"

# 卡片内字段：数组 = 依次尝试，第一个非空的生效
# ★ 2026-10 实测（/jobs/?jl=854&kw=... 分栏页）:
#   .job-card__title-main / .job-card__salary / .job-card__company-name /
#   .job-card__location 均在；分类标签挂在 .job-card__skill-tag
QUERY = {
    "title":    [".job-card__title-main", ".job-card__title", "[class*='title'] h3", "h3", "h2"],
    "company":  [".job-card__company-name", "[class*='company-name']", "[class*='company']"],
    "location": [".job-card__location", "[class*='location']", "[class*='area']"],
    "salary":   [".job-card__salary", "[class*='salary']"],
    "industry": ["[class*='industry']", "[class*='company__tag']", "[class*='company-tag']"],
}

# 卡片标签（新版是技能标签；旧版可能是 ul>li），捞全集后在 Python 侧分类
CARD_TAG_SELECTOR = ".job-card__skill-tag, [class*='skill-tag'], [class*='tag'] li, [class*='label'] li"

# ═══════════════════════════════════════════════════════════
# JD 面板选择器
# ★ 2026-10 实测（新版分栏页右侧）:
#   .job-detail-panel
#     └ .job-detail-modules > .job-detail-modules__detail
#        ├ section.job-detail-summary
#        │   ├ h1.job-detail-summary__title
#        │   │   ├ span.job-detail-summary__title-text   ← 岗位标题
#        │   │   └ span.job-detail-summary__salary       ← 薪资
#        │   └ ul.job-detail-summary__tags > li.job-detail-summary__tag
#        │                                  ← 城市 / 经验 / 学历
#        ├ .job-detail-card__body
#        │   ├ .job-description__content                 ← ★ JD 正文
#        │   ├ .job-detail-publisher__info               ← 发布者块
#        │   └ .job-detail-address__bubble               ← 街道地址
#        └ a[href*="jobdetail/CC...J....htm"]            ← ★ 真实 jobId 来源
#   注意: 卡片本身没有 jobId（只有公司主页链接），所以 jobId 必须从面板链接里取。
# ═══════════════════════════════════════════════════════════

JD_QUERY = {
    "panel":    [".job-detail-panel", ".job-split-layout__right", ".job-detail-modules__detail",
                 "[class*='job-detail']", "[class*='detail-box']"],
    "jd_text":  [".job-description__content", ".job-description", ".job-detail__content",
                 "[class*='job-desc']", "[class*='describ']"],
    "title":    [".job-detail-summary__title-text", ".job-detail-summary__title",
                 ".job-detail__title", ".job-detail__name", "[class*='detail'] h1", "h1"],
    "salary":   [".job-detail-summary__salary", ".job-detail__salary", "[class*='salary']"],
    "company":  [".job-detail-company__name", "[class*='company-name']", "[class*='company__name']",
                 ".job-detail-publisher__company", "[class*='company']"],
    "location": [".job-detail-summary__tag", ".job-detail__location", "[class*='address']",
                 "[class*='location']"],
    "recruiter": [".job-detail-publisher__info", "[class*='publisher']",
                  "[class*='recruiter']", "[class*='hr-info']"],
    "recruiter_name": [".job-detail-publisher__name", "[class*='publisher__name']"],
    "tags": ".job-detail-summary__tag, .job-detail-summary__tags > li, [class*='tag'] li, [class*='require'] li",
}

# 页面到底文案
END_MARKERS = ["没有更多了", "已经到底", "没有更多", "到底了", "已加载全部", "暂无更多", "No more"]

# 智联 jobId 形态: CC<companyId>J<序号>
JOB_ID_RE = re.compile(r"CC[A-Z]?\d+J\d+")


# ═══════════════════════════════════════════════════════════
# 薪资：解密 + 解析
# ═══════════════════════════════════════════════════════════

def decrypt_salary(text):
    """智联目前无字体加密。保留同名接口，保证与 BOSS 版数据结构/调用一致。"""
    return text or ""


def parse_salary_max(s):
    """解析薪资字符串，返回上限（单位 K）。与 BOSS 版同语义，额外支持 万/千。

    例:
        "12-20K"      → 20
        "6-8千"       → 8
        "1.5-2.5万"   → 25
        "8千-1.2万"   → 12
        "2-3万·14薪"  → 30
        "4000-6000元/月" → 6
        "面议"        → 0
    """
    if not s:
        return 0
    s = str(s).replace("～", "-").replace("~", "-")
    s = re.sub(r"[·\s]*\d+薪", "", s)

    # 日薪/时薪无法折算成月薪，直接放弃（必须在"元"模式之前判断）
    if re.search(r"元\s*/\s*(天|时|小时|周|次)", s):
        return 0

    # 逐个取出 "数字+单位"，取换算后最大值
    tokens = re.findall(r"(\d+(?:\.\d+)?)\s*([万千kK])", s)
    if tokens:
        best = 0.0
        for num, unit in tokens:
            k = float(num) * (10.0 if unit == "万" else 1.0)
            best = max(best, k)
        return int(best) if float(best).is_integer() else round(best, 1)

    m = re.search(r"(\d+)\s*-\s*(\d+)\s*元/月", s)
    if m:
        return round(int(m.group(2)) / 1000.0, 1)

    # "9000-16000元"（智联面板常见，缺省 /月 后缀）
    m = re.search(r"(\d{3,6})\s*-\s*(\d{3,6})\s*元", s)
    if m:
        return round(int(m.group(2)) / 1000.0, 1)

    m = re.search(r"(\d{3,6})\s*元", s)
    if m:
        return round(int(m.group(1)) / 1000.0, 1)

    if "面议" in s:
        return 0
    return 0


# ═══════════════════════════════════════════════════════════
# 传输层（自带重试）
# ═══════════════════════════════════════════════════════════

def api(action, args=None, session=SESSION, host=DEFAULT_HOST, port=DEFAULT_PORT,
        timeout=DEFAULT_TIMEOUT):
    if args is None:
        args = {}
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
            body = json.dumps({"action": action, "args": args, "session": session},
                              ensure_ascii=False).encode("utf-8")
            conn.request("POST", "/command", body=body,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            data = json.loads(resp.read().decode("utf-8"))
            conn.close()
            return data
        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
    raise last_error


def evaluate(code, session=SESSION):
    r = api("evaluate", {"code": code}, session=session)
    return r.get("data", {}).get("value", "")


def navigate(url, wait_range=(5, 7), session=SESSION):
    r = api("navigate", {"url": url}, session=session)
    time.sleep(random.uniform(*wait_range))
    return r


def click(selector, session=SESSION):
    return api("click", {"selector": selector}, session=session).get("data", {}).get("success", False)


def health_check(host=DEFAULT_HOST, port=DEFAULT_PORT):
    try:
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/")
        status = conn.getresponse().status
        conn.close()
        return status in (200, 404)
    except Exception:
        return False


def attach_active_tab(url_prefix="https://www.zhaopin.com", session=SESSION):
    """把用户当前正在看的智联标签页"借"进本 session（不新开、不刷新）

    WebBridge 的 find_tab 按 URL 前缀匹配前台标签页，
    成功后再 evaluate 才有页面可操作。返回 bool。
    """
    try:
        r = api("find_tab", {"url": url_prefix, "active": True}, session=session, timeout=12)
        if r.get("ok"):
            print(f"[标签页] 已附加: {r.get('data', {}).get('url', '')}")
            return True
        print(f"[标签页] 未找到匹配 `{url_prefix}` 的前台标签页: "
              f"{r.get('error', {}).get('message', '')}")
    except Exception as e:
        print(f"[标签页] find_tab 失败: {e}")
    return False


def ensure_active_tab(session=SESSION, timeout_wait=10):
    """确保 session 有可交互标签页；没有就导航到智联首页"""
    try:
        if not health_check():
            return False
        r = api("list_tabs", {}, session=session, timeout=5)
        if r.get("ok"):
            tabs_data = r.get("data", {})
            tabs = tabs_data.get("tabs", []) if isinstance(tabs_data, dict) else tabs_data
            if tabs and str(evaluate("1+1", session=session)) == "2":
                return True
        nav_r = navigate("https://www.zhaopin.com", wait_range=(timeout_wait, timeout_wait + 2),
                         session=session)
        if not nav_r.get("ok"):
            return False
        return str(evaluate("1+1", session=session)) == "2"
    except Exception:
        return False


def scroll_job_list(session=SESSION):
    """滚动列表加载下一屏。

    智联的卡片列表可能挂在内层 overflow 容器上（window.scrollTo 无效），
    所以先向上找卡片的可滚动祖先，没有才退回滚 window；
    最后再兜底把最后一张卡片 scrollIntoView，触发虚拟列表续渲染。

    返回:
        dict: {"mode": "container"|"window", "cards": int}
    """
    js = r"""
    (function(){
        var CARD = __CARD__;
        var cards = document.querySelectorAll(CARD);
        var before = cards.length;
        var best = null;
        if (cards.length) {
            var el = cards[cards.length - 1].parentElement;
            while (el && el !== document.body && el !== document.documentElement) {
                var st = window.getComputedStyle(el);
                if (/(auto|scroll)/.test(st.overflowY) && el.scrollHeight > el.clientHeight + 20) {
                    best = el; break;
                }
                el = el.parentElement;
            }
        }
        if (best) { best.scrollTop = best.scrollHeight; }
        else { window.scrollTo(0, document.body.scrollHeight); }
        if (cards.length) {
            try { cards[cards.length - 1].scrollIntoView({block: 'end'}); } catch(e) {}
        }
        return JSON.stringify({mode: best ? 'container' : 'window', cards: before});
    })()
    """
    raw = evaluate(js.replace("__CARD__", json.dumps(CARD_SELECTOR)), session=session) or "{}"
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"mode": "window", "cards": 0}


# ═══════════════════════════════════════════════════════════
# 卡片提取
# ═══════════════════════════════════════════════════════════

# 共用 JS 工具（每段脚本内部 IIFE 前注入）
_JS_HELPERS = r"""
    var CARD = __CARD__;
    var Q = __QUERY__;
    var TAG_SEL = __TAGS__;

    function txt(el) {
        return el ? (el.textContent || '').replace(/\s+/g, ' ').trim() : '';
    }
    function firstText(root, sels) {
        if (!root || !root.querySelector) return '';
        for (var i = 0; i < sels.length; i++) {
            try {
                var el = root.querySelector(sels[i]);
                if (el) { var t = txt(el); if (t) return t; }
            } catch (e) {}
        }
        return '';
    }
    function pickJobId(card) {
        var attrs = ['data-jobid', 'data-job-id', 'data-id', 'data-number', 'data-itemid'];
        for (var i = 0; i < attrs.length; i++) {
            var v = card.getAttribute(attrs[i]);
            if (v && /CC[A-Z]?\d+J\d+/.test(v)) return v;
        }
        var links = card.querySelectorAll('a[href]');
        for (var i = 0; i < links.length; i++) {
            var href = links[i].getAttribute('href') || '';
            var m = href.match(/CC[A-Z]?\d+J\d+/);
            if (m) return m[0];
        }
        for (var i = 0; i < attrs.length; i++) {
            var v = card.getAttribute(attrs[i]);
            if (v) return v;
        }
        return '';
    }
    function pickLink(card) {
        var links = card.querySelectorAll('a[href]');
        for (var i = 0; i < links.length; i++) {
            var href = links[i].getAttribute('href') || '';
            // ★ 只认岗位详情链接。新版卡片里唯一的链接是公司主页，
            //   绝不能拿它当 href，否则下游会去点公司页。
            if (/jobdetail|jobs\.zhaopin\.com|CC\d+J\d+/.test(href)) return links[i].href;
        }
        return '';
    }
    function findSalary(card) {
        var t = firstText(card, Q.salary);
        if (t) return t;
        var text = card.innerText || '';
        var lines = text.split('\n');
        var re = /\d+(?:\.\d+)?\s*(?:[万千Kk]\s*)?[-～~]\s*\d+(?:\.\d+)?\s*(?:[万千Kk]|元)/;
        for (var i = 0; i < lines.length; i++) {
            var l = lines[i].trim();
            if (l && l.length <= 20 && re.test(l)) return l;
        }
        // 兜底：整块文本里直接捞第一处薪资样式（innerText 换行行为在部分 DOM 下不可靠）
        var m = text.match(re);
        if (m) return m[0].replace(/\s+/g, '');
        if (text.indexOf('面议') >= 0) return '面议';
        return '';
    }
    function collectTags(card) {
        var out = [], seen = {};
        var els = card.querySelectorAll(TAG_SEL);
        for (var i = 0; i < els.length; i++) {
            var t = txt(els[i]);
            if (t && t.length <= 20 && !seen[t]) { seen[t] = 1; out.push(t); }
        }
        return out;
    }
    // 按 jobId 找卡片；兼容提取阶段的 "idx3" 占位 id（无 jobId 的卡片）
    function findCardById(cards, targetId) {
        if (/^idx\d+$/.test(targetId)) return cards[parseInt(targetId.slice(3), 10)] || null;
        for (var i = 0; i < cards.length; i++) {
            if (pickJobId(cards[i]) === targetId) return cards[i];
        }
        for (var i = 0; i < cards.length; i++) {
            var links = cards[i].querySelectorAll('a[href]');
            for (var k = 0; k < links.length; k++) {
                if ((links[k].getAttribute('href') || '').indexOf(targetId) >= 0) return cards[i];
            }
        }
        return null;
    }
"""

_EXTRACT_JS = r"""
(function(){
__HELPERS__
    var cards = document.querySelectorAll(CARD);
    var jobs = [], seen = {};
    for (var i = 0; i < cards.length; i++) {
        var card = cards[i];
        try {
            var jid = pickJobId(card);
            var title = firstText(card, Q.title);
            if (!jid) jid = 'idx' + i;          // 无 jobId 时用占位，Python 侧仍可去重
            if (!title) continue;
            if (seen[jid]) continue;
            seen[jid] = 1;
            jobs.push({
                title: title,
                salary: findSalary(card),
                location: firstText(card, Q.location),
                company: firstText(card, Q.company),
                industry: firstText(card, Q.industry),
                tags: collectTags(card),
                jobId: jid,
                href: pickLink(card),
                domIndex: i
            });
        } catch (e) {}
    }
    return JSON.stringify({total: jobs.length, list: jobs});
})()
"""


def _build_js(template, query=None, tags=None):
    """把公共 JS 工具与选择器注入脚本模板

    query: 选择器字典，默认用列表卡片 QUERY；读 JD 时传 JD_QUERY
    tags:  标签选择器，默认 TAG_SELECTOR
    """
    return (template
            .replace("__HELPERS__", _JS_HELPERS)
            .replace("__CARD__", json.dumps(CARD_SELECTOR))
            .replace("__QUERY__", json.dumps(query or QUERY, ensure_ascii=False))
            .replace("__TAGS__", json.dumps(tags or CARD_TAG_SELECTOR)))


_EXP_RE = re.compile(r"^(\d+\s*(-\s*\d+)?\s*年|经验不限|应届|在校|\d+\s*年以上|1年以内|\d+\s*个月)")


def _split_tags(tags):
    """把卡片标签分类成 经验 / 学历 / 福利，与 BOSS 版字段语义对齐

    ★ 经验必须严格匹配（3-5年 / 经验不限 / 应届 ...）。
      早期版本用 "年" in t，会把"年节福利"当成经验年限。
    """
    experience, degree, welfare = "", "", []
    degree_kw = ("本科", "大专", "硕士", "博士", "中专", "高中", "学历")
    for t in tags or []:
        if not experience and _EXP_RE.match(t):
            experience = t
        elif not degree and (any(k in t for k in degree_kw) or t == "不限"):
            degree = t
        else:
            welfare.append(t)
    return experience, degree, "|".join(welfare)


def split_city(location):
    """'西安 雁塔 潼浒寨' / '西安·雁塔区' → '西安'"""
    if not location:
        return ""
    return re.split(r"[·\s]+", str(location).strip())[0]


def extract_jobs(session=SESSION):
    """提取当前页所有岗位卡片，字段与 BOSS 版 extract_jobs 完全同构。

    返回 list[dict]，每条包含:
        title, salary, city, company, industry, scale, stage, online,
        welfare, degree, experience, jobId, location, href, domIndex

    其中 scale / stage / online 智联列表页没有对应信息，统一给空值/False，
    保证下游（筛选、报表）不用做平台分支。
    """
    raw = evaluate(_build_js(_EXTRACT_JS), session=session)
    try:
        data = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        return []

    jobs = []
    for j in data.get("list", []):
        location = j.get("location", "")
        city = split_city(location)
        experience, degree, welfare = _split_tags(j.get("tags"))
        salary = decrypt_salary(j.get("salary", ""))
        jobs.append({
            "title": j.get("title", ""),
            "salary": salary,
            "city": city,
            "company": j.get("company", ""),
            "industry": j.get("industry", ""),
            "scale": "",
            "stage": "",
            "online": False,
            "welfare": welfare,
            "degree": degree,
            "experience": experience,
            "jobId": j.get("jobId", ""),
            # —— 智联额外字段（不影响上面 12 个 BOSS 同构字段）——
            "location": location,
            "href": j.get("href", ""),
            "domIndex": j.get("domIndex", 0),
        })
    return jobs


# ═══════════════════════════════════════════════════════════
# DOM 探测 —— 智联改版后的第一现场
# ═══════════════════════════════════════════════════════════

def probe_dom(session=SESSION):
    """打印当前页的真实结构，用来校正 QUERY / JD_QUERY 选择器。

    返回:
        dict: {"url", "card_count", "first_card_html", "panel_candidates",
               "panel_hit", "jd_hit", "jobid_hit"}
    """
    js = r"""
    (function(){
        var CARD = __CARD__;
        var JDQ = __JDQ__;
        var cards = document.querySelectorAll(CARD);
        var first = cards.length ? cards[0].outerHTML : '';
        if (first.length > 4000) first = first.substring(0, 4000);

        // 右侧候选面板：右半屏、文字够多、含 JD 关键词
        var kws = ['职位描述', '岗位职责', '任职要求', '工作职责', '职位要求'];
        var cands = [];
        var nodes = document.querySelectorAll('div, section, article, main');
        for (var i = 0; i < nodes.length; i++) {
            var el = nodes[i];
            var r = el.getBoundingClientRect();
            if (r.width < 240 || r.left < window.innerWidth * 0.35) continue;
            var t = (el.innerText || '').trim();
            if (t.length < 150) continue;
            var hit = false;
            for (var k = 0; k < kws.length; k++) { if (t.indexOf(kws[k]) >= 0) { hit = true; break; } }
            if (!hit) continue;
            cands.push({
                tag: el.tagName.toLowerCase(),
                cls: (el.className || '').toString().substring(0, 120),
                textLen: t.length,
                left: Math.round(r.left)
            });
        }
        cands.sort(function(a, b){ return a.textLen - b.textLen; });

        // 配置里的选择器逐个试，报告第一个命中的
        function firstHit(list) {
            for (var i = 0; i < list.length; i++) {
                try {
                    var el = document.querySelector(list[i]);
                    if (el) {
                        return {sel: list[i], cls: (el.className || '').toString().substring(0, 80),
                                len: (el.innerText || '').trim().length};
                    }
                } catch (e) {}
            }
            return null;
        }

        // jobId 是否可从页面链接拿到
        var jid = '';
        var a = document.querySelector('a[href*="jobdetail"]');
        if (a) { var m = (a.getAttribute('href') || '').match(/CC[A-Z]?\d+J\d+/); if (m) jid = m[0]; }

        return JSON.stringify({
            url: location.href,
            card_count: cards.length,
            first_card_html: first,
            panel_candidates: cands.slice(0, 8),
            panel_hit: firstHit(JDQ.panel),
            jd_hit: firstHit(JDQ.jd_text),
            title_hit: firstHit(JDQ.title),
            salary_hit: firstHit(JDQ.salary),
            jobid_hit: jid
        });
    })()
    """
    js = js.replace("__CARD__", json.dumps(CARD_SELECTOR)).replace(
        "__JDQ__", json.dumps(JD_QUERY, ensure_ascii=False))
    raw = evaluate(js, session=session) or "{}"
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"url": "", "card_count": 0, "first_card_html": "",
                "panel_candidates": [], "panel_hit": None, "jd_hit": None,
                "title_hit": None, "salary_hit": None, "jobid_hit": ""}


__version__ = "0.1.0"
__updated__ = "2026-10-23"
