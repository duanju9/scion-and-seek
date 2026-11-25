#!/usr/bin/env python3
"""把岗位列表与详情 JD 合并成一张完整主表（CSV + JSON）。

输出字段涵盖：岗位基本信息、薪资、地点、公司信息、技能福利、JD 全文。
"""
import argparse
import csv
import json
import os


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", required=True)
    ap.add_argument("--details", required=True)
    ap.add_argument("--out-csv", required=True)
    ap.add_argument("--out-json", required=True)
    args = ap.parse_args()

    jobs_data = load_json(args.jobs)
    jobs = jobs_data.get("jobs", []) if isinstance(jobs_data, dict) else jobs_data
    details = load_json(args.details)
    if isinstance(details, dict):
        details = details.get("details") or details.get("jobs") or []

    by_id = {}
    for d in details:
        jid = str(d.get("job_id") or "")
        if jid and d.get("jd"):
            by_id[jid] = d

    rows = []
    for j in jobs:
        jid = str(j.get("job_id") or "")
        d = by_id.get(jid, {})
        row = dict(j)
        row["jd"] = d.get("jd", "")
        row["skill_tags"] = " | ".join(d.get("skill_tags") or [])
        row["boss_active_status_detail"] = d.get("boss_active_status", "")
        row["has_jd"] = "yes" if d.get("jd") else "no"
        # 统一字段顺序
        ordered = {
            "job_id": row.get("job_id", ""),
            "title": row.get("title", ""),
            "salary": row.get("salary", ""),
            "salary_source": row.get("salary_source", ""),
            "location": row.get("location", ""),
            "boss_name": row.get("boss_name", ""),
            "company_scale": row.get("company_scale", ""),
            "company_stage": row.get("company_stage", ""),
            "company_industry": row.get("company_industry", ""),
            "boss_title": row.get("boss_title", ""),
            "boss_active_status": row.get("boss_active_status_detail") or row.get("boss_active_status", ""),
            "job_labels": " | ".join(row.get("job_labels") or []) if isinstance(row.get("job_labels"), list) else row.get("job_labels", ""),
            "skills": row.get("skills", ""),
            "skill_tags": row.get("skill_tags", ""),
            "welfare": " | ".join(row.get("welfare") or []) if isinstance(row.get("welfare"), list) else row.get("welfare", ""),
            "tags": row.get("tags", ""),
            "has_jd": row.get("has_jd", "no"),
            "jd": row.get("jd", ""),
            "job_link": row.get("job_link", ""),
            "company_link": row.get("company_link", ""),
        }
        rows.append(ordered)

    with open(args.out_json, "w", encoding="utf-8") as f:
        json.dump({"keyword": jobs_data.get("keyword", ""), "city": jobs_data.get("city", ""),
                   "scraped_at": jobs_data.get("scraped_at", ""), "total": len(rows), "jobs": rows},
                  f, ensure_ascii=False, indent=2)

    with open(args.out_csv, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
        writer.writeheader()
        writer.writerows(rows)

    with_jd = sum(1 for r in rows if r["has_jd"] == "yes")
    print(f"合并完成：{len(rows)} 条岗位，其中 {with_jd} 条带 JD，{len(rows)-with_jd} 条无 JD")
    print(f"CSV: {args.out_csv}")
    print(f"JSON: {args.out_json}")


if __name__ == "__main__":
    main()
