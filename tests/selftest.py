"""离线自检: 不连浏览器，检查选择器注入后的 JS 语法 + 纯函数行为，并导出 JS 供 jsdom 测试"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from scripts import zhaopin_client as zc
from scripts import zl_jd_reader as reader
from scripts.surface_cache import _dedupe_and_order, KEEP_FIELDS

OUT = os.path.join(HERE, "_js_dump")
os.makedirs(OUT, exist_ok=True)
JID = "CC543940180J40698018604"


def dump(name, code):
    with open(os.path.join(OUT, name + ".js"), "w", encoding="utf-8") as f:
        f.write(code)
    return name


def with_tid(tpl):
    return tpl.replace("__TID__", json.dumps(JID))


dumps = [
    dump("extract", zc._build_js(zc._EXTRACT_JS)),
    dump("find_card", zc._build_js(with_tid(reader._FIND_CARD_JS))),
    dump("click_card", zc._build_js(with_tid(reader._CLICK_CARD_JS))),
    dump("read_panel", zc._build_js(reader._READ_PANEL_JS, query=zc.JD_QUERY, tags=zc.JD_QUERY["tags"])
         .replace("__PANEL__", reader._JS_PANEL_HELPERS)),
    dump("verify_panel", zc._build_js(reader._VERIFY_JS, query=zc.JD_QUERY, tags=zc.JD_QUERY["tags"])
         .replace("__PANEL__", reader._JS_PANEL_HELPERS)),
]
print("dump:", dumps)

print("\n-- parse_salary_max --")
cases = {
    "12-20K": 20, "6-8千": 8, "1.5-2.5万": 25, "8千-1.2万": 12, "2-3万·14薪": 30,
    "4000-6000元/月": 6.0, "面议": 0, "": 0, "20-30k": 30, "1万-1.5万": 15, "150-200元/天": 0,
    "9000-16000元": 16.0, "12000元": 12.0,
}
bad = []
for s, expect in cases.items():
    got = zc.parse_salary_max(s)
    ok = got == expect
    if not ok:
        bad.append((s, expect, got))
    print(f"  [{'OK ' if ok else 'BAD'}] {s!r} -> {got} (expect {expect})")

print("\n-- 标签分类 / 城市提取 --")
print(" ", zc._split_tags(["3-5年", "本科", "五险一金", "双休"]))
print(" ", zc._split_tags(["本科", "C语言", "年节福利"]))
print(" ", zc.split_city("西安 雁塔 潼浒寨"), "|", zc.split_city("杭州·滨江区"))

print("\n-- 去重 / page_order / 字段顺序 --")
raw = [
    {"jobId": "CC1J1", "title": "嵌入式软件工程师", "salary": "12-18K", "company": "A",
     "industry": "", "location": "杭州·滨江区"},
    {"jobId": "CC1J1", "title": "嵌入式软件工程师", "salary": "12-18K", "company": "A",
     "industry": "", "location": "杭州·滨江区"},
    {"jobId": "CC2J2", "title": "硬件工程师", "salary": "8千-1.2万", "company": "B",
     "industry": "", "location": "杭州·余杭区"},
]
uniq, slim = _dedupe_and_order(raw)
assert list(slim[0].keys()) == KEEP_FIELDS, slim[0].keys()
print(f"  原始{len(raw)} → 去重{len(uniq)} → 精简{len(slim)}；字段 {list(slim[0].keys())}")
print(f"  salary_max_k: {[j['salary_max_k'] for j in slim]}")

print("\n-- 招聘者拆分 --")
print(" ", reader._split_recruiter("王女士\n招聘经理"))
print(" ", reader._split_publisher("江雪\n在线\n刚刚活跃\n总回复 30+\n人才经纪人-经营性招聘服务 · 科锐尔人力资源服务(苏州)有限公司", "江雪"))
print(" ", reader._split_publisher("杨玲玲 在线 刚刚活跃 总回复 30+ HR · 上海遨有信息技术有限公司", "杨玲玲"))
print(" ", reader._clean_name("江雪\n在线"))

print("\n-- jobId 正则 --")
print(" ", zc.JOB_ID_RE.findall("https://www.zhaopin.com/jobdetail/CC850829750J40620453813.htm"))

print("\n-- JD 语义判断 (zl_judge) --")
import scripts.zl_judge as zj
PROFILE = {
    "job_directions": ["嵌入式软件", "单片机开发", "驱动开发", "固件工程师"],
    "must_have_keywords": ["C语言", "C++", "单片机", "MCU", "STM32", "嵌入式", "驱动", "固件", "RTOS", "Keil", "ARM"],
    "reject_keywords": ["纯前端", "前端开发", "Java后端", "Java开发", "测试工程师", "软件测试", "销售", "客服", "教培", "课程顾问"],
    "reject_strict": False, "reject_keyword_weight": "heavy",
    "min_degree": "本科", "acceptable_degrees": ["本科", "学历不限"],
    "min_salary_k": 6, "downgrade_company_patterns": ["人力资源", "人才服务", "劳务", "外包"],
}
j1 = zj.judge_jd({"job_title": "嵌入式软件工程师", "jd_text": "负责STM32单片机驱动与固件开发，熟悉RTOS",
                  "company": "某智能硬件公司", "salary": "1.2-2万", "degree": "本科"}, profile=PROFILE)
print(f"  对口岗: {j1['verdict']} score={j1['score']}"); bad += [] if j1["verdict"] == "APPROVE" else [("j1", j1)]
j2 = zj.judge_jd({"job_title": "后/前端开发、测试【24/25/无经验也可投】接受java、c＋＋、js不限语言",
                  "jd_text": "掌握C++/Java/Go/Web(Vue、React、js前端开发)/python/H5，熟悉Linux/Android/iOS/Windows，用Idea/Maven/SVN",
                  "company": "外企德科数字技术有限公司", "salary": "1.5-3万·14薪", "degree": "本科"}, profile=PROFILE)
print(f"  华为代招泛岗: {j2['verdict']} score={j2['score']}"); bad += [] if j2["verdict"] == "REJECT" else [("j2", j2)]
j3 = zj.judge_jd({"job_title": "产品经理助理", "jd_text": "负责市场调研与需求文档撰写，无编程要求",
                  "company": "某公司", "salary": "8千-1.2万", "degree": "本科"}, profile=PROFILE)
print(f"  无必备词岗: {j3['verdict']} score={j3['score']}"); bad += [] if j3["verdict"] == "REJECT" else [("j3", j3)]
j5 = zj.judge_jd({"job_title": "嵌入式软件工程师", "jd_text": "STM32固件开发，配合销售部门做技术支持，参与软件测试验证",
                  "company": "某科技公司", "salary": "1.2-2万", "degree": "本科"}, profile=PROFILE)
print(f"  正文提及销售/测试: {j5['verdict']} score={j5['score']} (正文light只轻罚，应APPROVE或高DOWNGRADE)")
bad += [] if j5["verdict"] == "APPROVE" and j5["score"] >= 75 else [("j5", j5)]
j7 = zj.judge_jd({"job_title": "嵌入式开发", "jd_text": "STM32固件开发", "company": "某公司",
                  "salary": "300元/天", "degree": "本科"}, profile=PROFILE)
print(f"  日薪岗: {j7['verdict']} score={j7['score']} (daily_wage_policy=reject 应 REJECT)")
bad += [] if j7["verdict"] == "REJECT" and j7["score"] == 0 else [("j7", j7)]
j6 = zj.judge_jd({"job_title": "嵌入式开发工程师", "jd_text": "STM32固件开发，配合销售部门做技术支持",
                  "company": "科锐尔人力资源服务(苏州)有限公司", "salary": "1-1.5万", "degree": "本科"}, profile=PROFILE)
print(f"  标题干净+正文销售+外包: {j6['verdict']} score={j6['score']} (floor放宽后应DOWNGRADE)")
bad += [] if j6["verdict"] == "DOWNGRADE" else [("j6", j6)]
j4 = zj.judge_jd({"job_title": "嵌入式驱动工程师", "jd_text": "开发UART/SPI驱动", "company": "科锐尔人力资源服务(苏州)有限公司",
                  "salary": "1-1.5万", "degree": "本科"}, profile=PROFILE)
print(f"  外包对口岗: {j4['verdict']} score={j4['score']} (应为 DOWNGRADE: 对口但公司降权)"); bad += [] if j4["verdict"] == "DOWNGRADE" else [("j4", j4)]

print("\nBAD CASES:", bad if bad else "无")
sys.exit(1 if bad else 0)
