#!/usr/bin/env python3
"""把岗位主表与趋势快照落入本地 SQLite，供巡检和报表查询。

用法：
  python ingest_db.py --input out/岗位公司信息_含JD.json \
      --db out/jobs.db --trends out/trends.json --label patrol

表：
  jobs       岗位主表（按 job_id upsert，保留 first_seen / last_seen）
  snapshots  每期汇总（岗位数、JD 数、AI 岗位数、薪资中位数、活跃率）
  runs       每次入库记录
"""
import argparse
import datetime
import json
import os
import re
import sqlite3

AI_RE = re.compile(
    r"(人工智能|算法|机器学习|深度学习|大模型|自然语言|计算机视觉|智能体|多模态|推荐系统|"
    r"(?<![A-Za-z])(AI|LLM|AIGC|NLP|Agent|RAG)(?![A-Za-z]))",
    re.IGNORECASE,
)


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def salary_mid(text):
    m = re.search(r"(\d+)\s*-\s*(\d+)\s*K", str(text or ""))
    return (int(m.group(1)) + int(m.group(2))) / 2 if m else None


def is_ai(job):
    text = " ".join(str(job.get(k) or "") for k in
                    ("title", "skills", "skill_tags", "jd", "company_industry"))
    return 1 if AI_RE.search(text) else 0


def ensure_account_column(conn):
    """snapshots.account：换号时段可分辨，趋势里「岗位数骤降」才能区分是换号还是行情。

    老库没有这列，用 ALTER 补；只加列不重建表，历史行 account 留空（未知就是未知）。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(snapshots)")}
    if "account" not in cols:
        conn.execute("ALTER TABLE snapshots ADD COLUMN account TEXT")


def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS jobs (
      job_id TEXT PRIMARY KEY,
      title TEXT,
      salary TEXT,
      salary_mid REAL,
      location TEXT,
      company TEXT,
      industry TEXT,
      scale TEXT,
      skills TEXT,
      jd TEXT,
      job_link TEXT,
      has_jd INTEGER,
      is_ai INTEGER,
      first_seen TEXT,
      last_seen TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_jobs_ai ON jobs(is_ai);
    CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);
    CREATE TABLE IF NOT EXISTS snapshots (
      ts TEXT PRIMARY KEY,
      job_count INTEGER,
      jd_count INTEGER,
      ai_count INTEGER,
      salary_mid REAL,
      active_rate REAL,
      source TEXT
    );    CREATE TABLE IF NOT EXISTS runs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      run_at TEXT,
      kind TEXT,
      total INTEGER,
      ai_count INTEGER,
      jd_count INTEGER,
      notes TEXT
    );
    """)
    ensure_account_column(conn)


def upsert_jobs(conn, jobs, stamp):
    rows = []
    for j in jobs:
        jid = str(j.get("job_id") or "")
        if not jid:
            continue
        jd = str(j.get("jd") or "")
        # 主表里的 first_seen 是岗位首次出现、last_seen 是最近一次真正命中；
        # 用落库时间顶替会让 DB 每轮看都像全表新岗（#16），只有缺字段才退回 stamp。
        first_seen = str(j.get("first_seen") or stamp)[:19].replace("T", " ")
        last_seen = str(j.get("last_seen") or stamp)[:19].replace("T", " ")
        rows.append((
            jid, j.get("title"), j.get("salary"), salary_mid(j.get("salary")),
            j.get("location"), j.get("boss_name"), j.get("company_industry"),
            j.get("company_scale"), j.get("skills") or j.get("skill_tags"),
            jd, j.get("job_link"), 1 if jd.strip() else 0, is_ai(j),
            first_seen, last_seen,
        ))
    conn.executemany("""
    INSERT INTO jobs (job_id,title,salary,salary_mid,location,company,industry,scale,
                      skills,jd,job_link,has_jd,is_ai,first_seen,last_seen)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(job_id) DO UPDATE SET
      title=excluded.title, salary=excluded.salary, salary_mid=excluded.salary_mid,
      location=excluded.location, company=excluded.company, industry=excluded.industry,
      scale=excluded.scale, skills=excluded.skills, jd=excluded.jd,
      job_link=excluded.job_link, has_jd=excluded.has_jd, is_ai=excluded.is_ai,
      first_seen=MIN(first_seen, excluded.first_seen),
      last_seen=excluded.last_seen
    """, rows)
    return len(rows)


def insert_history(conn, trends):
    """把趋势里的历史 summaries 也写入 snapshots，保持与 trends.snapshot_count 一致。"""
    for s in (trends or {}).get("summaries", []) or []:
        ts = str(s.get("ts") or "")[:19].replace("T", " ")
        if not ts:
            continue
        conn.execute("""
        INSERT INTO snapshots (ts,job_count,jd_count,ai_count,salary_mid,active_rate,source)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(ts) DO UPDATE SET
          job_count=excluded.job_count, salary_mid=excluded.salary_mid,
          active_rate=excluded.active_rate, source=excluded.source
        """, (ts, s.get("count"), None, s.get("ai_count"),
              s.get("salary_mid"), s.get("active_rate"), "trends"))


def insert_snapshot(conn, data, jobs, trends, label, account=""):
    latest = (trends or {}).get("latest") or {}
    jd_count = sum(1 for j in jobs if str(j.get("jd") or "").strip())
    ai_count = sum(is_ai(j) for j in jobs)
    ts = (data.get("scraped_at") if isinstance(data, dict) else "") or now()
    ts = str(ts)[:19].replace("T", " ")
    conn.execute("""
    INSERT INTO snapshots (ts,job_count,jd_count,ai_count,salary_mid,active_rate,source,account)
    VALUES (?,?,?,?,?,?,?,?)
    ON CONFLICT(ts) DO UPDATE SET
      job_count=excluded.job_count, jd_count=excluded.jd_count,
      ai_count=excluded.ai_count, salary_mid=excluded.salary_mid,
      active_rate=excluded.active_rate, source=excluded.source,
      -- 这一轮没探到账号时保留旧值，别把已知账号刷成空
      account=COALESCE(excluded.account, account)
    """, (ts, len(jobs), jd_count, ai_count,
          latest.get("salary_mid") or sum(salary_mid(j.get("salary")) or 0 for j in jobs) /
          max(1, sum(1 for j in jobs if salary_mid(j.get("salary")))),
          latest.get("active_rate"), label, account or None))
    return ts


def read_account_label(db_path):
    """账号标签由 account_probe.py 写在同一目录的 account_state.json，读不到就留空。"""
    path = os.path.join(os.path.dirname(os.path.abspath(db_path)), "account_state.json")
    try:
        with open(path, encoding="utf-8") as f:
            return str((json.load(f) or {}).get("label") or "")
    except Exception:
        return ""


def summary(conn):
    def one(sql):
        row = conn.execute(sql).fetchone()
        return row[0] if row and row[0] is not None else 0
    return {
        "jobs": one("SELECT COUNT(*) FROM jobs"),
        "ai_jobs": one("SELECT COUNT(*) FROM jobs WHERE is_ai=1"),
        "jd_jobs": one("SELECT COUNT(*) FROM jobs WHERE has_jd=1"),
        "companies": one("SELECT COUNT(DISTINCT company) FROM jobs"),
        "snapshots": one("SELECT COUNT(*) FROM snapshots"),
        "latest_snapshot": one("SELECT MAX(ts) FROM snapshots"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="含 JD 的岗位主表 JSON")
    ap.add_argument("--db", required=True, help="SQLite 输出路径")
    ap.add_argument("--trends", default="", help="趋势 JSON（可选）")
    ap.add_argument("--label", default="manual")
    args = ap.parse_args()

    data = load_json(args.input, {})
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    trends = load_json(args.trends, {}) if args.trends else {}
    os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)
    conn = sqlite3.connect(args.db)
    try:
        init_db(conn)
        stamp = now()
        n = upsert_jobs(conn, jobs, stamp)
        insert_history(conn, trends)
        account = read_account_label(args.db)
        ts = insert_snapshot(conn, data, jobs, trends, args.label, account)
        s = summary(conn)
        conn.execute("INSERT INTO runs (run_at,kind,total,ai_count,jd_count,notes) VALUES (?,?,?,?,?,?)",
                     (stamp, args.label, n, s["ai_jobs"], s["jd_jobs"], ts))
        conn.commit()
        s.update({"ok": True, "upserted": n, "db": args.db,
                  "snapshot_ts": ts, "run_at": stamp, "account": account})
    finally:
        conn.close()
    print(json.dumps(s, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
