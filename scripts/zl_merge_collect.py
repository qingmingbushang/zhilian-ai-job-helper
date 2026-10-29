#!/usr/bin/env python3
"""把多个关键词的采集结果合并成 zl_surface_cache.json（skill 判定脚本的标准输入）。

去重策略：同一 (城市, 公司, 薪资, 标题) 视为同一岗位（智联卡片无 jobId，
真实 jobId 由 zl_judge 点开面板时回填）。

用法: python scripts/zl_merge_collect.py out1.json out2.json ...
"""
import collections
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(HERE)

KEEP = ["page_order", "jobId", "title", "salary", "salary_max_k",
        "company", "industry", "location"]


def main(files):
    merged = {}
    order = []
    for f in files:
        if not os.path.exists(f):
            print(f"[跳过] 不存在: {f}")
            continue
        data = json.load(open(f, encoding="utf-8"))
        print(f"[读取] {f}: {len(data)} 条")
        for rec in data:
            key = (rec.get("city", ""), rec.get("company", ""),
                   rec.get("salary", ""), rec.get("title", ""))
            if key in merged:
                # 合并 welfare（技能标签），信息越全越好
                old = merged[key]
                if len(rec.get("welfare", "")) > len(old.get("welfare", "")):
                    old["welfare"] = rec["welfare"]
                continue
            merged[key] = {
                "title": rec.get("title", ""),
                "salary": rec.get("salary", ""),
                "salary_max_k": rec.get("salary_max_k", 0),
                "company": rec.get("company", ""),
                "industry": rec.get("industry", ""),
                "location": rec.get("location", ""),
                "welfare": rec.get("welfare", ""),
                "degree": rec.get("degree", ""),
                "experience": rec.get("experience", ""),
            }
            order.append(key)

    out = []
    for i, key in enumerate(order):
        rec = merged[key]
        rec["page_order"] = i
        rec["jobId"] = "idx%d" % i
        out.append({k: rec.get(k, "") for k in KEEP})
        out[-1]["_welfare"] = rec.get("welfare", "")
        out[-1]["_degree"] = rec.get("degree", "")
        out[-1]["_experience"] = rec.get("experience", "")

    json.dump(out, open("zl_surface_cache.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump(out, open("zl_cache_merged.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"[合并] 去重后 {len(out)} 条 → zl_surface_cache.json / zl_cache_merged.json")
    print(f"[地区] {collections.Counter(r['location'].split(' ')[0] for r in out).most_common(8)}")


if __name__ == "__main__":
    main(sys.argv[1:] or ["zl_collect_kw1.json"])
