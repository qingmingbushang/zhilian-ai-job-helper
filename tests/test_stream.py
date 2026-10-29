"""流水线投递逻辑自测（离线，不连浏览器）

覆盖 _process_card 的分支路由：读卡失败 / 去重 / 判定不过 / 经验年限 /
dry-run / 投递成功 / 验证码中止，以及经验年限正则。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from scripts import zl_stream_deliver as S

ok_all = True


def check(name, cond, extra=""):
    global ok_all
    print(("[PASS] " if cond else "[FAIL] ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        ok_all = False


JD = {"job_title": "嵌入式软件工程师", "company": "西安某科技",
      "salary": "9000-15000元", "jd_text": "负责 STM32 固件开发，熟悉 C语言",
      "full_text": "负责 STM32 固件开发，熟悉 C语言", "tags": []}

# ── 打桩：替换掉所有会碰浏览器/文件的函数 ──
logged = []
S._log = lambda e: logged.append(e)
S._save_state = lambda *a, **k: None


def stub_read(payload):
    S.read_jd_for_job = lambda idx, **k: payload


def stub_judge(verdict="APPROVE", score=100):
    S.judge_jd = lambda jd, profile=None: {"verdict": verdict, "score": score,
                                           "reasons": [], "hits": {}}


def stub_deliver(status="success", detail=""):
    S.deliver = lambda idx, confirm=False, preloaded=None: {
        "idx": idx, "status": status, "detail": detail,
        "buttons_after": [{"text": "继续沟通", "visible": True}],
        "panel_reused": bool(preloaded)}


def run(idx="idx3", done=None, seen=None, min_score=95, block=True, dry=True):
    logged.clear()
    return S._process_card(idx, done if done is not None else set(),
                           seen if seen is not None else set(),
                           min_score, block, dry)


# 1) 读卡失败
stub_read({"ok": False, "detail": "未找到卡片"})
go, sent = run()
check("读卡失败 → (继续, 未投) 且记 skip_nocard",
      go is True and sent is False and logged[-1]["status"] == "skip_nocard")

# 2) 去重命中（已投过的 jobId）
stub_read({"ok": True, "jd": JD, "job_id_real": "CC1J1"})
stub_judge()
go, sent = run(done={"CC1J1"})
check("jobId 已投 → skip_dup，不判不投",
      go is True and sent is False and logged[-1]["status"] == "skip_dup")

# 3) 判定不过
stub_read({"ok": True, "jd": JD, "job_id_real": "CC1J2"})
stub_judge("DOWNGRADE", 60)
go, sent = run()
check("DOWNGRADE → skip_judge", go is True and sent is False
      and logged[-1]["status"] == "skip_judge")

# 4) 分数不够（APPROVE 但低于阈值）
stub_judge("APPROVE", 90)
go, sent = run(min_score=95)
check("APPROVE 90 < 95 → skip_judge", logged[-1]["status"] == "skip_judge")

# 5) 经验年限拦截
JD_SEN = dict(JD, jd_text="要求 3年以上 相关经验", full_text="要求 3年以上 相关经验")
stub_read({"ok": True, "jd": JD_SEN, "job_id_real": "CC1J3"})
stub_judge()
go, sent = run(block=True)
check("经验年限命中 → skip_seniority", logged[-1]["status"] == "skip_seniority")
go, sent = run(block=False, dry=True)
check("--no-seniority-block → 放行到 dry_run", logged[-1]["status"] == "dry_run")

# 6) dry-run 不投
stub_read({"ok": True, "jd": JD, "job_id_real": "CC1J4"})
stub_judge()
go, sent = run(dry=True)
check("dry-run → 不投递", go is True and sent is False and logged[-1]["status"] == "dry_run")

# 7) 真投成功
stub_deliver("success")
done = set()
go, sent = run(done=done, dry=False)
check("投递成功 → (继续, 已投) 且 jobId 入已投集合",
      go is True and sent is True and "CC1J4" in done and logged[-1]["status"] == "delivered")

# 8) 验证码 → 中止队列
stub_deliver("captcha", "检测到验证码")
go, sent = run(done=set(), dry=False)
check("验证码 → 中止队列 (go=False)", go is False and sent is False)

# 9) 日上限 → 中止队列
stub_deliver("skip", "已达当日投递上限 20 条")
go, sent = run(done=set(), dry=False)
check("日上限 → 中止队列 (go=False)", go is False)

# 10) 经验年限正则
hits = S._seniority_hits({"jd_text": "5年以上经验，资深工程师", "full_text": ""})
check("经验年限识别 5年以上/资深", "5年以上" in "".join(hits) and "资深" in "".join(hits),
      str(hits))
check("无经验要求 → 空", S._seniority_hits({"jd_text": "负责嵌入式开发", "full_text": ""}) == [])

print("\n" + ("全部通过 ✅" if ok_all else "存在失败 ❌"))
sys.exit(0 if ok_all else 1)
