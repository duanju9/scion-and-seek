#!/usr/bin/env python3
"""岗位快照与趋势分析：把每次采集固化为带时间戳的快照，并计算跨期趋势。

用法：
  # 从主表新增一份快照（同一分钟内重复调用会去重）
  python analyze_trends.py --history <目录> --add-from <主表.json>

  # 基于所有快照计算趋势
  python analyze_trends.py --history <目录> --out <trends.json> --keep 60

输出 trends.json 供看板内嵌 / 服务端 /api/trends 实时读取，包含：
  - 每期概览：岗位数、薪资中位数、活跃率、技能/行业/城市分布、新增与消失岗位数
  - 时间序列：岗位量、薪资中位数、新增岗位数、活跃率
  - 技能趋势 / 行业趋势：Top N 项在各期的计数变化
  - 最新一期的新增岗位明细与行业分布环比
"""
import argparse
import collections
import datetime
import glob
import json
import os
import re
import statistics

KEEP_FIELDS = (
    "job_id", "title", "salary", "location", "job_labels", "skills", "welfare",
    "company_scale", "company_stage", "company_industry", "boss_name",
    "boss_active_status", "job_link", "company_link", "jd", "has_jd",
)


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _jobs_of(data):
    return data["jobs"] if isinstance(data, dict) and "jobs" in data else data


def _salary_mid(s):
    m = re.search(r"(\d+)-(\d+)K", str(s or ""))
    return (int(m.group(1)) + int(m.group(2))) / 2 if m else None


def _split(v):
    return [x.strip() for x in str(v or "").split("|") if x.strip()]


def _ts_now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def add_snapshot(history_dir, master_path):
    """从主表新增一份精简快照；同分钟内同关键词不重复写入。"""
    data = _load(master_path)
    jobs = _jobs_of(data)
    ts = data.get("scraped_at") or _ts_now()
    keyword = data.get("keyword", "") if isinstance(data, dict) else ""
    city = data.get("city", "") if isinstance(data, dict) else ""
    os.makedirs(history_dir, exist_ok=True)

    # 去重：同一天内同一关键词 + 相同岗位数 + 10 分钟内，视为同一批
    cutoff = datetime.datetime.now() - datetime.timedelta(minutes=10)
    for p in glob.glob(os.path.join(history_dir, "snapshot_*.json")):
        try:
            old = _load(p)
        except Exception:
            continue
        if old.get("keyword") == keyword and old.get("count") == len(jobs):
            try:
                t = datetime.datetime.strptime(old["ts"][:19], "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            if t >= cutoff:
                return {"ok": True, "skipped": True, "path": p, "count": len(jobs)}

    slim = [{k: j.get(k, "") for k in KEEP_FIELDS} for j in jobs]
    fname = "snapshot_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".json"
    path = os.path.join(history_dir, fname)
    snap = {
        "ts": ts,
        "file_created_at": _ts_now(),
        "keyword": keyword,
        "city": city,
        "count": len(jobs),
        "jobs": slim,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False)
    return {"ok": True, "skipped": False, "path": path, "count": len(jobs)}


def summarize(snap):
    jobs = snap.get("jobs", [])
    mids = [m for m in (_salary_mid(j.get("salary")) for j in jobs) if m is not None]
    skills = collections.Counter()
    industries = collections.Counter()
    cities = collections.Counter()
    exps = collections.Counter()
    edus = collections.Counter()
    for j in jobs:
        for s in _split(j.get("skills")):
            skills[s] += 1
        ind = str(j.get("company_industry") or "未知").strip()
        industries[ind] += 1
        cities[str(j.get("location") or "未知").split("·")[0].strip()] += 1
        labels = _split(j.get("job_labels"))
        exp = next((x for x in labels if re.search(r"年|不限|应届|在校", x)), "")
        edu = next((x for x in labels if re.search(r"本科|大专|硕士|高中|中专|初中|学历不限", x)), "")
        if exp:
            exps[exp] += 1
        if edu:
            edus[edu] += 1
    active = sum(1 for j in jobs if re.search(r"刚刚活跃|在线|今日活跃", str(j.get("boss_active_status") or "")))
    return {
        "ts": snap.get("ts", ""),
        "count": len(jobs),
        "salary_mid": round(statistics.median(mids), 1) if mids else 0,
        "salary_min_avg": round(statistics.mean(m for m in (_salary_mid(j.get("salary")) for j in jobs) if m is not None), 1) if mids else 0,
        "active_rate": round(active / len(jobs) * 100, 1) if jobs else 0,
        "companies": len({j.get("boss_name") for j in jobs if j.get("boss_name")}),
        "jd_coverage": round(sum(1 for j in jobs if j.get("has_jd") == "yes") / len(jobs) * 100, 1) if jobs else 0,
        "skills": dict(skills.most_common()),
        "industries": dict(industries.most_common()),
        "cities": dict(cities.most_common()),
        "experiences": dict(exps.most_common()),
        "education": dict(edus.most_common()),
        "ids": {str(j.get("job_id")) for j in jobs if j.get("job_id")},
        "jobs": jobs,
    }


def _series(summaries, key):
    return [{"ts": s["ts"], "v": s[key]} for s in summaries]


def _trend_map(summaries, field, top=8):
    """以最后一期 Top N 为基准，输出每项的时间序列。"""
    if not summaries:
        return {}
    names = list(summaries[-1][field].keys())[:top]
    return {
        name: [{"ts": s["ts"], "v": s[field].get(name, 0)} for s in summaries]
        for name in names
    }


def compute(history_dir, top=8, keep=60):
    files = sorted(glob.glob(os.path.join(history_dir, "snapshot_*.json")))
    snaps = []
    for p in files:
        try:
            snaps.append(_load(p))
        except Exception:
            continue
    summaries = [summarize(s) for s in snaps]

    # 新增 / 消失岗位（对比上一期）
    for i, s in enumerate(summaries):
        prev = summaries[i - 1]["ids"] if i else set()
        cur = s["ids"]
        # 首期没有对比基准，新增/消失记 0，避免趋势图出现虚假尖峰
        s["new_count"] = len(cur - prev) if i else 0
        s["gone_count"] = len(prev - cur) if i else 0
        s["new_ids"] = sorted(cur - prev) if i else []

    latest = summaries[-1] if summaries else None
    prev = summaries[-2] if len(summaries) >= 2 else None

    delta = {}
    industry_delta = []
    if latest and prev:
        delta = {
            "job_count": latest["count"] - prev["count"],
            "salary_mid": round(latest["salary_mid"] - prev["salary_mid"], 1),
            "active_rate": round(latest["active_rate"] - prev["active_rate"], 1),
            "companies": latest["companies"] - prev["companies"],
        }
        names = set(latest["industries"]) | set(prev["industries"])
        industry_delta = sorted(
            ({"name": n, "now": latest["industries"].get(n, 0),
              "prev": prev["industries"].get(n, 0),
              "delta": latest["industries"].get(n, 0) - prev["industries"].get(n, 0)}
             for n in names),
            key=lambda x: (-abs(x["delta"]), -x["now"]),
        )[:12]

    new_jobs = []
    if latest:
        idset = set(latest.get("new_ids", []))
        new_jobs = [
            {k: j.get(k, "") for k in ("job_id", "title", "salary", "location",
                                        "boss_name", "skills", "job_link", "has_jd")}
            for j in latest["jobs"] if str(j.get("job_id")) in idset
        ][:20]

    public_summaries = []
    for s in summaries:
        public_summaries.append({k: v for k, v in s.items() if k not in ("ids", "jobs", "new_ids")})

    result = {
        "generated_at": _ts_now(),
        "snapshot_count": len(snaps),
        "history_files": [os.path.basename(p) for p in files],
        "summaries": public_summaries,
        "series": {
            "job_count": _series(summaries, "count"),
            "salary_mid": _series(summaries, "salary_mid"),
            "new_jobs": _series(summaries, "new_count"),
            "active_rate": _series(summaries, "active_rate"),
        },
        "skill_trends": _trend_map(summaries, "skills", top),
        "industry_trends": _trend_map(summaries, "industries", top),
        "industry_delta": industry_delta,
        "delta": delta,
        "new_jobs": new_jobs,
        "latest": {k: v for k, v in latest.items() if k not in ("ids", "jobs", "new_ids")} if latest else None,
    }
    return result


def prune(history_dir, keep):
    files = sorted(glob.glob(os.path.join(history_dir, "snapshot_*.json")))
    removed = []
    for p in files[:-keep] if keep > 0 else []:
        try:
            os.remove(p)
            removed.append(os.path.basename(p))
        except Exception:
            pass
    return removed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", required=True, help="快照目录")
    ap.add_argument("--add-from", help="从该主表 JSON 新增一份快照")
    ap.add_argument("--out", help="输出 trends.json")
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--keep", type=int, default=60, help="保留最近 N 份快照")
    args = ap.parse_args()

    os.makedirs(args.history, exist_ok=True)
    if args.add_from:
        r = add_snapshot(args.history, args.add_from)
        print("SNAPSHOT", json.dumps(r, ensure_ascii=False))
    removed = prune(args.history, args.keep)
    if removed:
        print("PRUNED", len(removed))
    if args.out:
        result = compute(args.history, top=args.top, keep=args.keep)
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)
        print("TRENDS", args.out, "snapshots", result["snapshot_count"],
              "jobs", (result["latest"] or {}).get("count", 0))


if __name__ == "__main__":
    main()
