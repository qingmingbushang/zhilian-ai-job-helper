#!/usr/bin/env python3
"""
模块 D（智联版）: 批量读取岗位卡片表面信息 → 缓存文件

对应 BOSS 版 surface_cache.py，字段白名单保持一致，便于下游筛选逻辑复用:
    page_order, jobId, title, salary, salary_max_k, company, industry
额外多带一个 location（智联卡片自带"城市·区域"，BOSS 版因推荐页已限定城市而丢弃）。
下游只读前 7 个字段也能正常工作。

★ 顺序约束:
  page_order 从 0 开始（0 = 页面最顶部），筛选阶段生成 jd_to_read.json
  时必须保持此顺序，禁止重排。
"""

import json
import os
import sys

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL_DIR)

from scripts.zhaopin_client import extract_jobs, parse_salary_max

# 与 BOSS 版对齐的 7 个核心字段 + 智联扩展字段
BOSS_FIELDS = ['page_order', 'jobId', 'title', 'salary', 'salary_max_k', 'company', 'industry']
KEEP_FIELDS = BOSS_FIELDS + ['location']


def _dedupe_and_order(jobs):
    """按 jobId 去重（保序），再补 salary_max_k / page_order 并精简字段"""
    seen = set()
    unique = []
    for job in jobs:
        jid = job.get('jobId', '')
        if jid and jid not in seen:
            seen.add(jid)
            unique.append(job)

    for i, job in enumerate(unique):
        job['page_order'] = i
        job['salary_max_k'] = parse_salary_max(job.get('salary', ''))

    slim = []
    for job in unique:
        slim.append({k: job.get(k, '') for k in KEEP_FIELDS})
    return unique, slim


def extract_all_surface():
    """一次性提取当前页全部岗位表面信息（精简字段）"""
    print("[缓存] 开始批量提取岗位表面信息...")
    jobs = extract_jobs()
    unique, slim = _dedupe_and_order(jobs)
    print(f"[缓存] 提取完成: {len(jobs)} 条原始, {len(slim)} 条去重后")
    print(f"[缓存] 字段: {', '.join(KEEP_FIELDS)}")
    return slim


def extract_surface_since(last_count=0):
    """增量提取：只返回 page_order >= last_count 的新岗位（分批模式用）"""
    print(f"[缓存] 增量提取 (page_order >= {last_count})...")
    jobs = extract_jobs()
    unique, slim = _dedupe_and_order(jobs)

    if len(slim) <= last_count:
        print(f"[缓存] 无新增岗位 (当前总数{len(slim)}, 已提取{last_count})")
        return []

    new_jobs = slim[last_count:]
    for i, job in enumerate(new_jobs):
        job['page_order'] = last_count + i
    print(f"[缓存] 新增 {len(new_jobs)} 条 (page_order {last_count}~"
          f"{last_count + len(new_jobs) - 1})")
    return new_jobs


def save_cache(jobs, filepath="zl_surface_cache.json"):
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(jobs, f, ensure_ascii=False, indent=2)
    print(f"[缓存] 已保存 {len(jobs)} 条至 {filepath}")
    return filepath


def append_cache(new_jobs, filepath="zl_surface_cache.json"):
    existing = []
    if os.path.exists(filepath):
        with open(filepath, 'r', encoding='utf-8') as f:
            existing = json.load(f)
    existing.extend(new_jobs)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    print(f"[缓存] 追加 {len(new_jobs)} 条，总计 {len(existing)} 条至 {filepath}")
    return len(existing)


def load_cache(filepath="zl_surface_cache.json"):
    if not os.path.exists(filepath):
        print(f"[缓存] 文件不存在: {filepath}")
        return []
    with open(filepath, 'r', encoding='utf-8') as f:
        jobs = json.load(f)
    print(f"[缓存] 已加载 {len(jobs)} 条 from {filepath}")
    return jobs


def print_summary(jobs):
    if not jobs:
        print("缓存为空")
        return
    print("\n" + "=" * 56)
    print(f"岗位缓存摘要: 共 {len(jobs)} 条")
    print("=" * 56)

    buckets = {"未知": 0, "<5K": 0, "5-10K": 0, "10-15K": 0, "15-20K": 0, "20K+": 0}
    for j in jobs:
        sal = j.get('salary_max_k', 0) or 0
        if not sal:
            buckets["未知"] += 1
        elif sal < 5:
            buckets["<5K"] += 1
        elif sal < 10:
            buckets["5-10K"] += 1
        elif sal < 15:
            buckets["10-15K"] += 1
        elif sal < 20:
            buckets["15-20K"] += 1
        else:
            buckets["20K+"] += 1
    print("\n薪资分布 (按上限):")
    for k, v in buckets.items():
        if v:
            print(f"  {k}: {v} 条")

    print("\n前 10 条预览:")
    print(f"  {'#':>3}  {'order':>5}  {'标题':<22}  {'薪资':<12}  {'公司':<16}  {'地点'}")
    for i, j in enumerate(jobs[:10], 1):
        print(f"  {i:>3}  {j.get('page_order', i - 1):>5}  "
              f"{(j.get('title') or '')[:22]:<22}  {(j.get('salary') or '')[:12]:<12}  "
              f"{(j.get('company') or '')[:16]:<16}  {j.get('location') or ''}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="智联: 批量读取岗位卡片 → 缓存文件")
    parser.add_argument("--output", default="zl_surface_cache.json")
    args = parser.parse_args()

    jobs = extract_all_surface()
    if not jobs:
        print("未提取到任何岗位，请先打开智联职位列表页（或用 --step probe 查看 DOM）")
        sys.exit(1)
    save_cache(jobs, args.output)
    print_summary(jobs)


if __name__ == "__main__":
    main()
