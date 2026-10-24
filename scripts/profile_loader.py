#!/usr/bin/env python3
"""
智联版中央配置加载器 — 对应 BOSS 版 profile_loader.py

与 BOSS 版的差异:
  1. 配置文件缺失时不抛异常，全部降级为默认值（读取模块先跑通比强校验重要）
  2. 城市用智联的 jl 编码（如 530 = 杭州），而非 BOSS 的 101210100
  3. 列表页 URL 提供模板，可用 user_profile.json 的 recommend_url 覆盖

用法:
    from scripts.profile_loader import (
        SESSION_NAME, CITY_CODE, TARGET_CITY, WORK_DIR, SKILL_DIR,
        RECOMMEND_URL_TEMPLATE, build_url, USER_PROFILE,
    )
"""

import json
import os

# ═══════════════════════════════════════════════════════════
# 路径解析
# ═══════════════════════════════════════════════════════════

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USER_PROFILE_FILE = os.path.join(SKILL_DIR, "user_profile.json")

# 工作目录: 优先环境变量，否则当前目录
WORK_DIR = os.environ.get("ZL_WORK_DIR", os.getcwd())

# ═══════════════════════════════════════════════════════════
# 加载用户画像（缺失则用默认值）
# ═══════════════════════════════════════════════════════════


def _load_profile():
    if not os.path.exists(USER_PROFILE_FILE):
        print(f"[配置] 未找到 {USER_PROFILE_FILE}，使用默认配置")
        return {}
    try:
        with open(USER_PROFILE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"[配置] 读取 user_profile.json 失败: {e}，使用默认配置")
        return {}


USER_PROFILE = _load_profile()

# --- WebBridge 会话 ---
SESSION_NAME = USER_PROFILE.get("session_name", "zl-main")

# --- 目标城市 ---
TARGET_CITY = USER_PROFILE.get("target_city", "杭州")
CITY_CODE = USER_PROFILE.get("city_code", "530")   # 智联 jl 编码

# --- 投递参数（读取阶段仅用于上层编排，本模块不投递）---
DAILY_TARGET = USER_PROFILE.get("daily_target", 50)
MIN_SALARY_K = USER_PROFILE.get("min_salary_k", 0)

# --- 列表页 URL ---
#   ★ 2026-10 起智联改版为"左侧列表 + 右侧详情"分栏页:
#     https://www.zhaopin.com/jobs/?jl=<城市编码>&kw=<关键词>
#   模板里的 {city} / {kw} 会被替换；两者都没有时原样返回。
#   若你在浏览器里打开的是别的地址（推荐页 / 收藏夹 / 搜索结果旧版 /sou/jl..），
#   直接把完整 URL 写进 user_profile.json 的 recommend_url 即可。
RECOMMEND_URL_TEMPLATE = USER_PROFILE.get(
    "recommend_url",
    "https://www.zhaopin.com/jobs/?jl={city}&kw={kw}",
)

# 搜索关键词（模板里带 {kw} 时使用）
SEARCH_KEYWORD = USER_PROFILE.get("search_keyword", "嵌入式软件工程师")


def build_url(city_code=None, kw=None):
    """把城市编码/关键词套进 URL 模板"""
    tpl = RECOMMEND_URL_TEMPLATE
    city = str(city_code if city_code is not None else CITY_CODE)
    keyword = SEARCH_KEYWORD if kw is None else kw
    out = tpl
    if "{city}" in out:
        out = out.replace("{city}", city)
    if "{kw}" in out:
        from urllib.parse import quote
        out = out.replace("{kw}", quote(keyword, safe=""))
    return out


def get_work_dir():
    return os.environ.get("ZL_WORK_DIR", os.getcwd())


def get_profile():
    return USER_PROFILE.copy()


__version__ = "0.1.0"
__updated__ = "2026-10-23"
