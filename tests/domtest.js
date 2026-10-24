// 离线 DOM 冒烟测试：用 jsdom 造一个"左侧列表 + 右侧面板"的智联页面，
// 跑真实的提取 / 定位 / 点卡 / 读 JD / 校验 JS，断言数据确实来自正确的作用域。
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const dumpDir = path.join(__dirname, '_js_dump');
const JID2 = 'CC543940180J40698018604';

const html = `<!DOCTYPE html><html><body>
<div class="job-list">
  <div class="job-card">
    <a href="https://www.zhaopin.com/jobdetail/CC850829750J40620453813.htm">
      <span class="job-card__title-main">嵌入式软件工程师</span>
    </a>
    <span class="job-card__salary">12-18K·13薪</span>
    <span class="job-card__location">杭州·滨江区</span>
    <span class="job-card__company-name">某某科技有限公司</span>
    <ul class="job-card__tag-list"><li>3-5年</li><li>本科</li><li>五险一金</li></ul>
  </div>
  <div class="job-card">
    <a href="/jobdetail/${JID2}.htm">
      <span class="job-card__title-main">硬件工程师</span>
    </a>
    <span class="job-card__location">杭州·余杭区</span>
    <span class="job-card__company-name">睿冠信息科技有限公司</span>
    <ul class="job-card__tag-list"><li>1-3年</li><li>大专</li><li>双休</li></ul>
  </div>
  <div class="job-card">
    <a href="/jobdetail/CC111111111J11111111111.htm">
      <span class="job-card__title-main">测试工程师</span>
    </a>
    <div class="job-card__info">8千-1.2万 · 杭州</div>
    <span class="job-card__company-name">某某检测</span>
  </div>
  <div class="job-card">
    <span class="job-card__title-main">PCB 绘图员</span>
    <span class="job-card__company-name">某某电子</span>
  </div>
</div>
<div class="right-side">
  <div class="job-detail">
    <h1 class="job-detail__title">嵌入式软件工程师</h1>
    <span class="job-detail__salary">12-18K</span>
    <span class="job-detail__company">某某科技有限公司（总部）</span>
    <span class="job-detail__location">杭州·滨江区·长河</span>
    <div class="job-detail__content">
      职位描述
      岗位职责：负责 STM32 固件开发，MPU6050 驱动调试，PID 调速算法实现。
      任职要求：本科及以上，3 年以上嵌入式经验，熟悉 C 语言与 Keil 环境。
    </div>
    <div class="job-detail__recruiter">王女士</div>
    <a href="/jobdetail/CC850829750J40620453813.htm">查看详情</a>
  </div>
</div>
</body></html>`;

const dom = new JSDOM(html, {
  url: 'https://www.zhaopin.com/sou/jl530/p1',
  pretendToBeVisual: true,
  runScripts: 'outside-only',
});
const { window } = dom;
const { document } = window;

// jsdom 不实现 innerText，用 textContent 顶替
Object.defineProperty(window.HTMLElement.prototype, 'innerText', {
  get() { return this.textContent; },
  set(v) { this.textContent = v; },
  configurable: true,
});

// 给右侧面板伪造可见区域，让"右半屏"判据成立
Object.defineProperty(window.HTMLElement.prototype, 'getBoundingClientRect', {
  value: function () {
    const inRight = this.closest && !!this.closest('.right-side');
    return { left: inRight ? 600 : 20, top: 0, width: inRight ? 500 : 400, height: 300,
             right: 0, bottom: 0, x: 0, y: 0 };
  },
  configurable: true,
});

// jsdom 不实现 scrollIntoView / click 的滚动行为
window.Element.prototype.scrollIntoView = function () {};

const run = (file) => window.eval(fs.readFileSync(path.join(dumpDir, file), 'utf8'));

let fail = 0;
const check = (cond, msg, extra) => {
  if (!cond) { fail++; console.log(`  ✗ ${msg}`, extra === undefined ? '' : JSON.stringify(extra)); }
  else { console.log(`  ✓ ${msg}`); }
};

console.log('=== extract：列表卡片提取 ===');
const ext = JSON.parse(run('extract.js'));
ext.list.forEach((j, i) => console.log(`  [${i}] ${j.title} | ${j.salary || '-'} | ${j.company} | ${j.jobId} | ${j.location}`));
check(ext.list.length === 4, '卡片数 4', ext.list.length);
check(ext.list[0].jobId === 'CC850829750J40620453813', '绝对链接 jobId', ext.list[0].jobId);
check(ext.list[0].salary === '12-18K·13薪', '薪资选择器', ext.list[0].salary);
check(ext.list[1].jobId === JID2, '相对链接 jobId', ext.list[1].jobId);
check(ext.list[1].salary === '', '无薪资时为空', ext.list[1].salary);
check(ext.list[2].salary === '8千-1.2万 · 杭州', '薪资文本兜底扫描', ext.list[2].salary);
check(ext.list[3].jobId === 'idx3', '无链接卡片用 idxN 占位', ext.list[3].jobId);
check(ext.list[0].tags.join('|') === '3-5年|本科|五险一金', '标签收集', ext.list[0].tags);

console.log('\n=== find_card：按 jobId 定位 ===');
const found = JSON.parse(run('find_card.js'));
console.log(' ', JSON.stringify(found));
check(found.found === true, '定位成功', found);
check(found.title === '硬件工程师', '卡片标题', found.title);
check(found.company === '睿冠信息科技有限公司', '卡片公司', found.company);
check(found.href.endsWith(JID2 + '.htm'), '卡片链接', found.href);

console.log('\n=== click_card：原地点击（拦截 a 跳转）===');
let navigated = false;
window.addEventListener('beforeunload', () => { navigated = true; });
const clicked = JSON.parse(run('click_card.js'));
console.log(' ', JSON.stringify(clicked));
check(clicked.ok === true && clicked.clicked === 'job-card__title-main', '★ 点的是标题区而不是公司链接', clicked);
check(document.querySelectorAll('.job-card').length === 4, '点击后仍停留在列表页（未跳转）', navigated);

console.log('\n=== verify_panel：面板校验 ===');
const verified = JSON.parse(run('verify_panel.js'));
check(verified.len > 120, '面板文本长度足够', verified.len);

console.log('\n=== read_panel：JD 读取（作用域必须在右侧面板）===');
const panel = JSON.parse(run('read_panel.js'));
console.log(' ', JSON.stringify({ ...panel, full_text: panel.full_text.slice(0, 40) }, null, 1));
check(panel.job_title === '嵌入式软件工程师', '面板标题', panel.job_title);
check(panel.company === '某某科技有限公司（总部）', '★公司名取自面板而非卡片', panel.company);
check(panel.city === '杭州·滨江区·长河', '★地点取自面板而非卡片', panel.city);
check(panel.salary === '12-18K', '面板薪资', panel.salary);
check(!panel.tags.includes('双休'), '★标签未串入其他卡片', panel.tags);
check(panel.jd_text.includes('STM32'), 'JD 正文含关键词');
check(panel.recruiter === '王女士', '招聘者', panel.recruiter);
check(panel.job_id === 'CC850829750J40620453813', '★真实 jobId 从面板链接取', panel.job_id);

console.log(fail === 0 ? '\n✓ 全部断言通过' : `\n✗ ${fail} 项失败`);
process.exit(fail === 0 ? 0 : 1);
