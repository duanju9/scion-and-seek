#!/usr/bin/env python3
"""岗位查询的唯一口径（single source of truth）。

为什么要这一层
--------------
在此之前，同一份主表被三处各自解释：

  * 看板前端 `norm()/filtered()`（JS）
  * 导出 `export_jobs.match()/sort_val()`（Python）
  * `/api/jobs`（原样吐 JSON，不做任何处理）

三者对「城市叫什么」「q 搜哪些字段」「薪资按上限还是中位排」「面议排前还是排后」
各有一套答案。结果是页面上看到 42 条，点导出拿到 39 条，而且没人发现——
这比查不到更糟，因为它看起来是对的。

本模块把归一化、筛选、排序、统计收敛成一份实现。前端无法直接 import Python，
所以规则在这里定死后，`generate_dashboard.py` 里的 JS 按同一套规则重写，
并用 `/api/jobs?count=1` 做交叉校验：页面计数与服务端计数不一致时直接报警。

字段口径
--------
- 城市：`location` 取第一段；港澳台统一为中国香港 / 中国澳门 / 中国台湾。
- 经验/学历：从 `job_labels`（`1-3年 | 本科`）里识别，不是另存字段。
- 活跃：`boss_active_status`；列表页可能只有「在线」，详情页更细（「刚刚活跃」）。
- 薪资中位：`(低+高)/2`，只认 `N-MK`；日薪、面议记 None。
- AI 标记：优先取 DB 的 `is_ai`（ingest 时判定），DB 缺失时按标题+技能兜底。
"""
import json
import os
import re
import sqlite3

MASTER_NAME = "岗位公司信息_含JD.json"

CITY_ALIASES = {
    "香港": "中国香港",
    "澳门": "中国澳门",
    "台湾": "中国台湾",
    "中国台湾省": "中国台湾",
}

AI_HINTS = ("ai", "人工智能", "算法", "大模型", "机器学习", "深度学习",
            "llm", "agent", "多模态", "nlp", "cv", "推荐系统")

# q 参与匹配的字段。前端必须用同一组，否则同一个词在页面和导出里命中数不同。
SEARCH_FIELDS = ("title", "boss_name", "city", "area", "skills", "welfare",
                 "industry", "jd")

EXP_RE = re.compile(r"年|不限|应届|在校")
EDU_RE = re.compile(r"本科|大专|硕士|高中|学历不限|中专|初中|博士")
SALARY_RE = re.compile(r"(\d+)-(\d+)K")


def split_labels(v):
    return [x.strip() for x in str(v or "").split("|") if x.strip()]


def city_of(job):
    city = str(job.get("location") or "").split("·")[0].strip()
    return CITY_ALIASES.get(city, city)


def area_of(job):
    parts = str(job.get("location") or "").split("·")
    return "·".join(parts[1:]).strip() if len(parts) > 1 else ""


def exp_of(job):
    for x in split_labels(job.get("job_labels")):
        if EXP_RE.search(x):
            return x
    return ""


def edu_of(job):
    for x in split_labels(job.get("job_labels")):
        if EDU_RE.search(x):
            return x
    return ""


def salary_mid(s):
    m = SALARY_RE.search(str(s or ""))
    return round((int(m.group(1)) + int(m.group(2))) / 2, 1) if m else None


def salary_high(s):
    """页面排序曾经用薪资上限，导出用中位——两者顺序不同。保留此函数只为回归测试对比。"""
    m = SALARY_RE.search(str(s or ""))
    return int(m.group(2)) if m else None


def guess_ai(job):
    text = (str(job.get("title") or "") + " " + str(job.get("skills") or "")).lower()
    return 1 if any(h in text for h in AI_HINTS) else 0


def normalize(job, is_ai=None, mid=None):
    """主表一条 → 查询用的一行。所有派生字段只在这里算一次。"""
    return {
        "raw": job,
        "job_id": job.get("job_id") or "",
        "title": job.get("title") or "",
        "salary": job.get("salary") or "",
        "salary_mid": mid if mid is not None else salary_mid(job.get("salary")),
        "company": job.get("boss_name") or "",
        "city": city_of(job),
        "area": area_of(job),
        "industry": job.get("company_industry") or "",
        "scale": job.get("company_scale") or "",
        "stage": job.get("company_stage") or "",
        "exp": exp_of(job),
        "edu": edu_of(job),
        "active": job.get("boss_active_status") or "",
        "skills": job.get("skills") or "",
        "welfare": job.get("welfare") or "",
        "keyword": job.get("source_keyword") or "",
        "is_ai": int(is_ai if is_ai is not None else guess_ai(job)),
        "has_jd": bool(str(job.get("jd") or "").strip()),
        "jd": job.get("jd") or "",
        "first_seen": job.get("first_seen") or "",
        "last_seen": job.get("last_seen") or "",
        "job_link": job.get("job_link") or "",
        "company_link": job.get("company_link") or "",
    }


def parse_filters(qs):
    """query 参数（parse_qs 的 dict 或普通 dict）→ 规范化的筛选条件。"""
    def one(key, default=""):
        v = qs.get(key)
        if isinstance(v, (list, tuple)):
            v = v[0] if v else default
        return str(v or default).strip()

    ai = one("ai").lower()
    if ai in ("1", "true", "yes", "y"):
        ai = "1"
    elif ai in ("0", "false", "no", "n"):
        ai = "0"
    else:
        ai = ""

    return {
        "q": one("q").lower(),
        "city": CITY_ALIASES.get(one("city"), one("city")),  # 旧链接里的「香港」也能命中
        "area": one("area"),
        "exp": one("exp"),
        "edu": one("edu"),
        "scale": one("scale"),
        "active": one("active"),
        "industry": one("industry"),
        "keyword": one("keyword"),
        "ai": ai,
        "jded": one("jded").lower() in ("1", "true", "yes"),
        "sort": one("sort", "salary") or "salary",
        "dir": "asc" if one("dir").lower() == "asc" else "desc",
    }


def match(row, f):
    if f["city"] and row["city"] != f["city"]:
        return False
    if f["area"] and f["area"] not in row["area"]:
        return False
    if f["exp"] and row["exp"] != f["exp"]:
        return False
    if f["edu"] and row["edu"] != f["edu"]:
        return False
    if f["scale"] and row["scale"] != f["scale"]:
        return False
    if f["active"] and row["active"] != f["active"]:
        return False
    if f["industry"] and row["industry"] != f["industry"]:
        return False
    if f["keyword"] and row["keyword"] != f["keyword"]:
        return False
    if f["ai"] and bool(row["is_ai"]) != (f["ai"] == "1"):
        return False
    if f["jded"] and not row["has_jd"]:
        return False
    if f["q"]:
        hay = " ".join(str(row.get(k) or "") for k in SEARCH_FIELDS).lower()
        if f["q"] not in hay:
            return False
    return True


SORT_KEYS = {"salary": "salary_mid", "title": "title", "company": "company",
             "city": "city", "exp": "exp", "edu": "edu", "scale": "scale",
             "active": "active", "skills": "skills", "keyword": "keyword"}


def sort_rows(rows, key="salary", direction="desc"):
    """排序：薪资按中位，未知薪资无论升降序都排最后（否则一屏全是面议）。"""
    field = SORT_KEYS.get(key, "title")
    reverse = direction != "asc"

    if field == "salary_mid":
        known = [r for r in rows if r["salary_mid"] is not None]
        unknown = [r for r in rows if r["salary_mid"] is None]
        known.sort(key=lambda r: (r["salary_mid"], r["first_seen"]), reverse=reverse)
        return known + unknown

    # 非薪资字段：空值降序垫底、升序置顶都是表格惯例里的噪声，统一压到最后
    filled = [r for r in rows if str(r.get(field) or "").strip()]
    blank = [r for r in rows if not str(r.get(field) or "").strip()]
    filled.sort(key=lambda r: str(r[field]), reverse=reverse)
    return filled + blank


def stats(rows):
    cities = {}
    for r in rows:
        cities[r["city"]] = cities.get(r["city"], 0) + 1
    mids = [r["salary_mid"] for r in rows if r["salary_mid"] is not None]
    mids.sort()
    return {
        "total": len(rows),
        "ai": sum(1 for r in rows if r["is_ai"]),
        "with_jd": sum(1 for r in rows if r["has_jd"]),
        "companies": len({r["company"] for r in rows if r["company"]}),
        "salary_mid": round(mids[len(mids) // 2], 1) if mids else None,
        "cities": dict(sorted(cities.items(), key=lambda kv: -kv[1])[:12]),
    }


def facets(rows):
    """下拉选项（带条数）。选项与数据同源，避免出现「选了香港一条都没有」。"""
    def cnt(field):
        c = {}
        for r in rows:
            v = str(r.get(field) or "").strip()
            if v:
                c[v] = c.get(v, 0) + 1
        return dict(sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))

    return {k: cnt(k) for k in
            ("city", "area", "exp", "edu", "scale", "active", "industry", "keyword")}


def load_master(output_dir, retries=3):
    """读主表。刷新正在写盘时可能读到半截 JSON，重试几次再放弃。"""
    path = os.path.join(output_dir, MASTER_NAME)
    last = None
    for _ in range(max(1, retries)):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data["jobs"] if isinstance(data, dict) else data
        except (json.JSONDecodeError, OSError) as e:  # 写入中/被占用
            last = e
    raise RuntimeError(f"主表读取失败（{path}）：{last}")


def load_ai_map(output_dir, jobs=None):
    """job_id -> (is_ai, salary_mid)。DB 不可用时返回空表，调用方退回启发式。"""
    path = os.path.join(output_dir, "jobs.db")
    if not os.path.isfile(path):
        return {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
        try:
            return {jid: (int(ai or 0), mid)
                    for jid, ai, mid in conn.execute(
                        "SELECT job_id, is_ai, salary_mid FROM jobs")}
        finally:
            conn.close()
    except Exception:
        pass
    # 中文/空格路径在 URI 模式下可能被拒，退回普通连接（只读由调用方保证）
    try:
        conn = sqlite3.connect(path, timeout=3)
        try:
            return {jid: (int(ai or 0), mid)
                    for jid, ai, mid in conn.execute(
                        "SELECT job_id, is_ai, salary_mid FROM jobs")}
        finally:
            conn.close()
    except Exception:
        return {}


def build_rows(jobs, ai_map=None):
    ai_map = ai_map or {}
    rows = []
    for j in jobs:
        known = ai_map.get(j.get("job_id"))
        if known:
            rows.append(normalize(j, is_ai=int(known[0] or 0), mid=known[1]))
        else:
            rows.append(normalize(j))
    return rows


def query(output_dir=None, jobs=None, params=None, **kw):
    """一次性完成 读表 → 归一 → 筛选 → 排序 → 统计。"""
    f = parse_filters(params or {})
    if jobs is None:
        jobs = load_master(output_dir)
    rows = build_rows(jobs, load_ai_map(output_dir) if output_dir else None)
    hit = [r for r in rows if match(r, f)]
    hit = sort_rows(hit, f["sort"], f["dir"])
    return hit, f, stats(hit)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="按统一口径查询岗位（调试用）")
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--city")
    ap.add_argument("--q")
    ap.add_argument("--ai", choices=["1", "0"])
    ap.add_argument("--limit", type=int, default=5)
    a = ap.parse_args()
    rows, flt, st = query(a.output_dir, params={k: v for k, v in vars(a).items()
                                                if k in ("city", "q", "ai") and v})
    print(json.dumps(st, ensure_ascii=False))
    for r in rows[:a.limit]:
        print(f"  {r['salary_mid']}\t{r['city']}\t{r['title']}\t{r['company']}")
