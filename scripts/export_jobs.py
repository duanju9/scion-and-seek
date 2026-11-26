#!/usr/bin/env python3
"""岗位导出：按看板当前的筛选条件，把主表导成 Excel（中文表头）或 CSV。

设计要点
- 每次请求现读主表 JSON：定时刷新落库后，页面点「导出」拿到的就是最新一版，
  不需要重新生成模板、也不会导出陈旧缓存。
- 筛选语义与看板前端一致（关键词、城市、经验、学历、规模、活跃度、AI 标记、排序），
  导出的是「你眼前看到的这批」，不是整库。
- 表头固定中文；openpyxl 缺失时自动退回 UTF-8 BOM 的 CSV，Excel 打开不乱码。

可单独当脚本用：
    python export_jobs.py --output-dir <输出目录> [--q Python] [--city 深圳] --out 岗位表.xlsx
"""
import argparse
import csv
import datetime
import io
import json
import os
import re
import sqlite3


HEADERS = [
    ("序号", 6), ("岗位名称", 30), ("公司", 26), ("薪资", 15), ("月薪中位(K)", 11),
    ("城市", 9), ("区域", 16), ("行业", 18), ("公司规模", 11), ("融资阶段", 10),
    ("技能标签", 40), ("AI相关", 8), ("经验", 9), ("学历", 9), ("Boss活跃", 11),
    ("来源关键词", 13), ("首次发现", 16), ("最近更新", 16), ("岗位链接", 42),
    ("职位描述", 80),
]
FIELDS = [h[0] for h in HEADERS]
MASTER_NAME = "岗位公司信息_含JD.json"
AI_HINTS = ("AI", "人工智能", "算法", "大模型", "机器学习", "深度学习", "LLM", "Agent", "多模态")


def _split_labels(v):
    return [x.strip() for x in str(v or "").split("|") if x.strip()]


def _exp_of(job):
    for x in _split_labels(job.get("job_labels")):
        if re.search(r"年|不限|应届|在校", x):
            return x
    return ""


def _edu_of(job):
    for x in _split_labels(job.get("job_labels")):
        if re.search(r"本科|大专|硕士|高中|学历不限|中专|初中|博士", x):
            return x
    return ""


def _city_of(job):
    city = str(job.get("location") or "").split("·")[0].strip()
    return {"香港": "中国香港", "澳门": "中国澳门", "台湾": "中国台湾"}.get(city, city)


def _area_of(job):
    parts = str(job.get("location") or "").split("·")
    return "·".join(parts[1:]).strip() if len(parts) > 1 else ""


def _salary_mid(s):
    m = re.search(r"(\d+)-(\d+)K", str(s or ""))
    return round((int(m.group(1)) + int(m.group(2))) / 2, 1) if m else None


def load_master(output_dir):
    path = os.path.join(output_dir, MASTER_NAME)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data["jobs"] if isinstance(data, dict) else data


def load_meta(output_dir):
    """job_id -> (is_ai, salary_mid)，DB 不可用时返回空表（导出仍可用，标记留空）。"""
    path = os.path.join(output_dir, "jobs.db")
    meta = {}
    if not os.path.isfile(path):
        return meta
    try:
        conn = sqlite3.connect(path)
        try:
            for jid, is_ai, mid in conn.execute("SELECT job_id, is_ai, salary_mid FROM jobs"):
                meta[jid] = (int(is_ai or 0), mid)
        finally:
            conn.close()
    except Exception:
        return {}
    return meta


def parse_filters(qs):
    """把 query 参数（parse_qs 的结果或普通 dict）规整成筛选条件。"""
    def one(key, default=""):
        v = qs.get(key)
        if isinstance(v, (list, tuple)):
            v = v[0] if v else default
        return str(v or default).strip()

    return {
        "q": one("q").lower(),
        "city": one("city"),
        "exp": one("exp"),
        "edu": one("edu"),
        "scale": one("scale"),
        "active": one("active"),
        "keyword": one("keyword"),
        "ai": one("ai"),
        "sort": one("sort", "salary"),
        "dir": one("dir", "desc"),
    }


def match(job, f, is_ai):
    if f["city"] and _city_of(job) != f["city"]:
        return False
    if f["exp"] and _exp_of(job) != f["exp"]:
        return False
    if f["edu"] and _edu_of(job) != f["edu"]:
        return False
    if f["scale"] and str(job.get("company_scale") or "") != f["scale"]:
        return False
    if f["active"] and str(job.get("boss_active_status") or "") != f["active"]:
        return False
    if f["keyword"] and str(job.get("source_keyword") or "") != f["keyword"]:
        return False
    if f["ai"] in ("1", "0") and bool(is_ai) != (f["ai"] == "1"):
        return False
    if f["q"]:
        hay = " ".join(str(job.get(k) or "") for k in
                       ("title", "boss_name", "skills", "jd", "location", "company_industry")).lower()
        if f["q"] not in hay:
            return False
    return True


def collect(output_dir, filters):
    jobs = load_master(output_dir)
    meta = load_meta(output_dir)
    out = []
    for j in jobs:
        known = meta.get(j.get("job_id"))
        if known:
            is_ai, mid = int(known[0] or 0), known[1]
        else:
            text = (str(j.get("title") or "") + " " + str(j.get("skills") or "")).lower()
            is_ai = 1 if any(h.lower() in text for h in AI_HINTS) else 0
            mid = None
        if mid is None:
            mid = _salary_mid(j.get("salary"))
        if not match(j, filters, is_ai):
            continue
        out.append((j, is_ai, mid))

    key = filters["sort"]
    reverse = filters["dir"] != "asc"

    def sort_val(item):
        j, is_ai, mid = item
        if key == "salary":
            return mid if mid is not None else -1
        if key == "company":
            return str(j.get("boss_name") or "")
        if key == "city":
            return _city_of(j)
        if key == "exp":
            return _exp_of(j)
        if key == "edu":
            return _edu_of(j)
        if key == "scale":
            return str(j.get("company_scale") or "")
        if key == "active":
            return str(j.get("boss_active_status") or "")
        if key == "skills":
            return str(j.get("skills") or "")
        return str(j.get("title") or "")

    out.sort(key=lambda x: sort_val(x), reverse=reverse)
    if key == "salary":
        # 面议/日薪没有中位数，无论升降序都压到最后，避免"未知"占满第一屏
        out = [x for x in out if x[2] is not None] + [x for x in out if x[2] is None]
    return out


def _row_values(idx, item):
    j, is_ai, mid = item
    return [
        idx, j.get("title") or "", j.get("boss_name") or "", j.get("salary") or "", mid,
        _city_of(j), _area_of(j), j.get("company_industry") or "",
        j.get("company_scale") or "", j.get("company_stage") or "",
        str(j.get("skills") or "").replace(" | ", "、"),
        "是" if is_ai else "", _exp_of(j), _edu_of(j), j.get("boss_active_status") or "",
        j.get("source_keyword") or "",
        str(j.get("first_seen") or "")[:16].replace("T", " "),
        str(j.get("last_seen") or "")[:16].replace("T", " "),
        j.get("job_link") or "", str(j.get("jd") or "").strip(),
    ]


def build_xlsx(items):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    head_fill = PatternFill("solid", fgColor="1F4E79")
    head_font = Font(name="微软雅黑", size=10, bold=True, color="FFFFFF")
    body_font = Font(name="微软雅黑", size=10)
    thin = Side(style="thin", color="D9D9D9")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    wb = Workbook()
    ws = wb.active
    ws.title = "岗位明细"
    ws.append(FIELDS)
    for i, (_name, width) in enumerate(HEADERS, 1):
        ws.column_dimensions[get_column_letter(i)].width = width
        c = ws.cell(row=1, column=i)
        c.fill, c.font, c.border = head_fill, head_font, border
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[1].height = 28

    for idx, item in enumerate(items, 1):
        ws.append(_row_values(idx, item))
        r = ws.max_row
        for col in range(1, len(FIELDS) + 1):
            cell = ws.cell(row=r, column=col)
            cell.font, cell.border = body_font, border
            cell.alignment = Alignment(vertical="top", wrap_text=col in (11, 20))
        ws.cell(row=r, column=5).number_format = "0.0"
        link = ws.cell(row=r, column=19)
        if str(link.value or "").startswith("http"):
            link.hyperlink = link.value
            link.font = Font(name="微软雅黑", size=9, color="0563C1", underline="single")
        if item[1]:
            ws.cell(row=r, column=12).font = Font(name="微软雅黑", size=10, bold=True, color="C00000")
        jd_len = len(str(item[0].get("jd") or ""))
        lines = max(1, min(8, (jd_len + 54) // 55))
        ws.row_dimensions[r].height = min(110, 14 * lines)

    ws.freeze_panes = "C2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(FIELDS))}{ws.max_row}"

    # 第二页放口径说明，避免拿到表的人误读「AI相关」「月薪中位」
    info = wb.create_sheet("说明")
    info.column_dimensions["A"].width = 20
    info.column_dimensions["B"].width = 78
    rows = [
        ("导出时间", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("岗位数", len(items)),
        ("数据来源", "BOSS 直聘公开搜索页 · 本地采集并集主表（保留 7 天）"),
        ("月薪中位(K)", "薪资区间中位数，按 12 个月折算；日薪/面议留空"),
        ("AI相关", "落库判定：标题或技能命中 AI / 算法 / 大模型等词"),
        ("排序", "默认按月薪中位降序，可在页面点表头改成其它列"),
        ("筛选", "导出内容与页面当前筛选一致，不是整库"),
        ("合规", "仅本地自查使用；未发送招呼、未启用自动投递"),
    ]
    for i, (k, v) in enumerate(rows, 1):
        a = info.cell(row=i, column=1, value=k)
        a.font = head_font if i == 1 else Font(name="微软雅黑", size=10, bold=True)
        a.fill = head_fill if i == 1 else PatternFill(fill_type=None)
        b = info.cell(row=i, column=2, value=v)
        b.font = Font(name="微软雅黑", size=10)
        b.alignment = Alignment(wrap_text=True, vertical="top")

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_csv(items):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(FIELDS)
    for idx, item in enumerate(items, 1):
        w.writerow(_row_values(idx, item))
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def export(qs, output_dir):
    """返回 (bytes, 建议文件名, content-type)。"""
    filters = parse_filters(qs)
    items = collect(output_dir, filters)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    tag = "筛选" if any(filters[k] for k in ("q", "city", "exp", "edu", "scale", "active", "keyword", "ai")) else "全部"
    base = f"岗位表_{stamp}_{tag}{len(items)}条"
    try:
        return build_xlsx(items), base + ".xlsx", \
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    except ImportError:
        return build_csv(items), base + ".csv", "text/csv; charset=utf-8"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--q", default="")
    ap.add_argument("--city", default="")
    ap.add_argument("--exp", default="")
    ap.add_argument("--edu", default="")
    ap.add_argument("--scale", default="")
    ap.add_argument("--active", default="")
    ap.add_argument("--keyword", default="")
    ap.add_argument("--ai", default="")
    ap.add_argument("--sort", default="salary")
    ap.add_argument("--dir", default="desc")
    args = ap.parse_args()
    qs = {k: getattr(args, k) for k in
          ("q", "city", "exp", "edu", "scale", "active", "keyword", "ai", "sort", "dir")}
    body, name, _ctype = export(qs, args.output_dir)
    out = args.out or os.path.join(args.output_dir, name)
    with open(out, "wb") as f:
        f.write(body)
    print(json.dumps({"out": out, "bytes": len(body),
                      "items": len(collect(args.output_dir, parse_filters(qs)))},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
