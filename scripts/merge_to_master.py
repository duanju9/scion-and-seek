#!/usr/bin/env python3
"""把岗位列表与详情 JD 合并成一张完整主表（CSV + JSON）。

输出字段涵盖：岗位基本信息、薪资、地点、公司信息、技能福利、JD 全文。

多关键词轮换时，主表是「当轮 + 上轮余量」的并集：当轮抓到的岗位刷新 last_seen 并覆盖旧记录，
上轮有、本轮没有的岗位按 job_id 保留（last_seen 超过 --keep-days 才丢弃），
避免换关键词那一轮把别的新岗位整表冲掉。
"""
import argparse
import csv
import datetime
import json
import os

UNKNOWN_KEYWORD = "未标注"  # 历史行缺 source_keyword 时用，避免被当前词认领

FIELD_ORDER = ("job_id", "title", "salary", "salary_source", "location",
               "boss_name", "company_scale", "company_stage", "company_industry",
               "boss_title", "boss_active_status", "job_labels", "skills",
               "skill_tags", "welfare", "tags", "has_jd", "jd", "job_link",
               "company_link", "source_keyword", "last_keyword",
               "first_seen", "last_seen", "carried")


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _age_days(stamp):
    """距今天数。主表里时间戳有空格格式也有 ISO 'T'（带毫秒）两种写法，
    只认一种会让 keep_days 淘汰静默失效（#14）。"""
    s = str(stamp or "").strip()
    if not s:
        return None
    for cand in (s, s[:26], s[:19]):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
                    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                d = datetime.datetime.strptime(cand, fmt)
            except Exception:
                continue
            return (datetime.datetime.now() - d).total_seconds() / 86400.0
    return None


def ordered_row(row, source_keyword, carried):
    out = {k: row.get(k, "") for k in FIELD_ORDER}
    # source_keyword 记「首次抓到这个岗的词」，last_keyword 记「最近一次命中的词」，
    # 这样换词轮换不会把池子构成全部改写成最后一个词
    out["source_keyword"] = row.get("source_keyword") or source_keyword
    out["last_keyword"] = row.get("last_keyword") or source_keyword
    out["carried"] = carried
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", required=True)
    ap.add_argument("--details", required=True)
    ap.add_argument("--out-csv", required=True)
    ap.add_argument("--out-json", required=True)
    ap.add_argument("--source-keyword", default="",
                    help="本轮采集用的关键词（写入每行 source_keyword）")
    ap.add_argument("--carry-from", default="",
                    help="上一轮主表 JSON；用于保留本轮列表之外的存量岗位")
    ap.add_argument("--keep-days", type=float, default=7.0,
                    help="存量岗位 last_seen 超过这么多天就丢弃")
    args = ap.parse_args()

    jobs_data = load_json(args.jobs)
    jobs = jobs_data.get("jobs", []) if isinstance(jobs_data, dict) else jobs_data
    details = load_json(args.details)
    if isinstance(details, dict):
        details = details.get("details") or details.get("jobs") or []

    keyword = args.source_keyword or (
        jobs_data.get("keyword", "") if isinstance(jobs_data, dict) else "")
    stamp = (jobs_data.get("scraped_at", "") if isinstance(jobs_data, dict) else "") \
        or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    prev_jobs, prev_kw, prev_stamp = [], "", ""
    if args.carry_from and os.path.isfile(args.carry_from):
        prev = load_json(args.carry_from)
        prev_jobs = prev.get("jobs", []) if isinstance(prev, dict) else prev
        prev_kw = prev.get("keyword", "") if isinstance(prev, dict) else ""
        prev_stamp = prev.get("scraped_at", "") if isinstance(prev, dict) else ""
    prev_index = {str(p.get("job_id")): p for p in prev_jobs if p.get("job_id")}

    by_id = {}
    for d in details:
        jid = str(d.get("job_id") or "")
        if jid and d.get("jd"):
            by_id[jid] = d

    rows, seen = [], set()
    for j in jobs:
        jid = str(j.get("job_id") or "")
        if jid and jid in seen:
            continue
        d = by_id.get(jid, {})
        old = prev_index.get(jid, {})
        row = dict(j)
        row["jd"] = d.get("jd", "")
        row["skill_tags"] = " | ".join(d.get("skill_tags") or [])
        row["boss_active_status"] = d.get("boss_active_status") or row.get("boss_active_status", "")
        row["job_labels"] = " | ".join(row.get("job_labels") or []) if isinstance(row.get("job_labels"), list) else row.get("job_labels", "")
        row["welfare"] = " | ".join(row.get("welfare") or []) if isinstance(row.get("welfare"), list) else row.get("welfare", "")
        row["has_jd"] = "yes" if d.get("jd") else "no"
        row["first_seen"] = old.get("first_seen") or stamp
        row["last_seen"] = stamp
        # 首次命中的岗才归到本轮词；已在主表里的行保留首次归属，
        # 历史缺字段的行记「未标注」，不要顺手认领成本轮关键词
        if jid and jid in prev_index:
            row["source_keyword"] = old.get("source_keyword") or UNKNOWN_KEYWORD
        else:
            row["source_keyword"] = old.get("source_keyword") or keyword
        row["last_keyword"] = keyword
        rows.append(ordered_row(row, keyword, "no"))
        if jid:
            seen.add(jid)

    carried, expired = 0, 0
    for pj in prev_jobs:
        jid = str(pj.get("job_id") or "")
        if not jid or jid in seen:
            continue
        # 老版本主表没有 last_seen，用上轮采集时间兜底，避免存量永不过期
        seen_at = pj.get("last_seen") or pj.get("first_seen") or prev_stamp
        age = _age_days(seen_at)
        if age is not None and args.keep_days > 0 and age > args.keep_days:
            expired += 1
            continue
        row = dict(pj)
        row["last_seen"] = seen_at
        # 存量岗位的 JD 可能在这一轮被积压补抓补上，合并时不能丢（#14）
        d = by_id.get(jid, {})
        if d:
            if d.get("jd") and not row.get("jd"):
                row["jd"] = d["jd"]
                row["has_jd"] = "yes"
            if d.get("skill_tags") and not row.get("skill_tags"):
                tags = d.get("skill_tags") or []
                row["skill_tags"] = " | ".join(tags) if isinstance(tags, list) else tags
            if d.get("boss_active_status") and not row.get("boss_active_status"):
                row["boss_active_status"] = d["boss_active_status"]
        if row.get("jd"):
            row["has_jd"] = "yes"
        rows.append(ordered_row(row, row.get("source_keyword") or UNKNOWN_KEYWORD, "yes"))
        seen.add(jid)
        carried += 1

    meta = {
        "keyword": keyword,
        "city": jobs_data.get("city", "") if isinstance(jobs_data, dict) else "",
        "scraped_at": stamp,
        "total": len(rows),
        "this_round": len(rows) - carried,
        "carried": carried,
        "expired": expired,
    }
    with open(args.out_json, "w", encoding="utf-8") as f:
        json.dump(dict(meta, jobs=rows), f, ensure_ascii=False, indent=2)

    with open(args.out_csv, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(FIELD_ORDER))
        writer.writeheader()
        writer.writerows(rows)

    with_jd = sum(1 for r in rows if r["has_jd"] == "yes")
    print(f"合并完成：{len(rows)} 条岗位（本轮 {len(rows) - carried} + 存量 {carried}），"
          f"其中 {with_jd} 条带 JD")
    if expired:
        print(f"另有 {expired} 条存量岗位超过 {args.keep_days:g} 天未再出现，已丢弃")
    print(f"CSV: {args.out_csv}")
    print(f"JSON: {args.out_json}")


if __name__ == "__main__":
    main()
