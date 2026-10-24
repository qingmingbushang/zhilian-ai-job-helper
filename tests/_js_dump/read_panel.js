
(function(){

    var CARD = ".job-card";
    var Q = {"panel": [".job-detail-panel", ".job-split-layout__right", ".job-detail-modules__detail", "[class*='job-detail']", "[class*='detail-box']"], "jd_text": [".job-description__content", ".job-description", ".job-detail__content", "[class*='job-desc']", "[class*='describ']"], "title": [".job-detail-summary__title-text", ".job-detail-summary__title", ".job-detail__title", ".job-detail__name", "[class*='detail'] h1", "h1"], "salary": [".job-detail-summary__salary", ".job-detail__salary", "[class*='salary']"], "company": [".job-detail-company__name", "[class*='company-name']", "[class*='company__name']", ".job-detail-publisher__company", "[class*='company']"], "location": [".job-detail-summary__tag", ".job-detail__location", "[class*='address']", "[class*='location']"], "recruiter": [".job-detail-publisher__info", "[class*='publisher']", "[class*='recruiter']", "[class*='hr-info']"], "recruiter_name": [".job-detail-publisher__name", "[class*='publisher__name']"], "tags": ".job-detail-summary__tag, .job-detail-summary__tags > li, [class*='tag'] li, [class*='require'] li"};
    var TAG_SEL = ".job-detail-summary__tag, .job-detail-summary__tags > li, [class*='tag'] li, [class*='require'] li";

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
