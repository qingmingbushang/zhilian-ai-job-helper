
(function(){

    var CARD = ".job-card";
    var Q = {"title": [".job-card__title-main", ".job-card__title", "[class*='title'] h3", "h3", "h2"], "company": [".job-card__company-name", "[class*='company-name']", "[class*='company']"], "location": [".job-card__location", "[class*='location']", "[class*='area']"], "salary": [".job-card__salary", "[class*='salary']"], "industry": ["[class*='industry']", "[class*='company__tag']", "[class*='company-tag']"]};
    var TAG_SEL = ".job-card__skill-tag, [class*='skill-tag'], [class*='tag'] li, [class*='label'] li";

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
