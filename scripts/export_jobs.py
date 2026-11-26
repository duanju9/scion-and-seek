#!/usr/bin/env python3
"""岗位导出：按看板当前的筛选条件，把主表导成 Excel（中文表头）或 CSV。

口径
----
筛选、排序、派生字段全部交给 `jobquery`，本文件只负责「排版 + 输出」。
之前这里自带一套 match/sort_val，和前端各说各话，导致页面上 42 条、导出 39 条，
所以现在不再允许任何筛选逻辑出现在导出层。

可单独当脚本用：
    python export_jobs.py --output-dir <输出目录> [--q Python] [--city 深圳] --out 岗位表.xlsx
"""
import argparse
import csv
import datetime
import io
import json
import os
import sys

# 以脚本方式运行时 sys.path[0] 是 scripts/，但被 importlib 从别处加载时不是
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jobquery  # noqa: E402  （依赖上面的 sys.path 修正）


HEADERS = [
    ("序号", 6), ("岗位名称", 30), ("公司", 26), ("薪资", 15), ("月薪中位(K)", 11),
    ("城市", 9), ("区域", 16), ("行业", 18), ("公司规模", 11), ("融资阶段", 10),
    ("技能标签", 40), ("AI相关", 8), ("经验", 9), ("学历", 9), ("Boss活跃", 11),
    ("来源关键词", 13), ("首次发现", 16), ("最近更新", 16), ("岗位链接", 42),
    ("职位描述", 80),
]
FIELDS = [h[0] for h in HEADERS]


def collect(output_dir, filters):
    """返回排序后的归一化行（jobquery 的 row dict）。"""
    jobs = jobquery.load_master(output_dir)
    rows = jobquery.build_rows(jobs, jobquery.load_ai_map(output_dir))
    hit = [r for r in rows if jobquery.match(r, filters)]
    return jobquery.sort_rows(hit, filters["sort"], filters["dir"])


def _dt(v):
    return str(v or "")[:16].replace("T", " ")


# Excel 单元格上限 32767 字符，超了会被静默截断（读回来少一截，没人知道为什么）。
# 我们主动截并在文末标注，读者至少知道该点链接看原文。
MAX_CELL = 32000
CLIP_NOTE = "…（内容超长已截断，完整内容见岗位链接）"


def _clip(s):
    s = "" if s is None else str(s)
    if len(s) <= MAX_CELL:
        return s
    cut = s[:MAX_CELL]
    for sep in ("\n", "。", "；", "，"):
        i = cut.rfind(sep)
        if i > MAX_CELL * 0.8:
            cut = cut[:i + 1]
            break
    return cut + CLIP_NOTE


def _row_values(idx, r):
    return [
        idx, r["title"], r["company"], r["salary"], r["salary_mid"],
        r["city"], r["area"], r["industry"], r["scale"], r["stage"],
        r["skills"].replace(" | ", "、"),
        "是" if r["is_ai"] else "", r["exp"], r["edu"], r["active"],
        r["keyword"], _dt(r["first_seen"]), _dt(r["last_seen"]),
        r["job_link"], _clip(r["jd"].strip()),
    ]


def build_xlsx(items, filters=None):
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

    for idx, r in enumerate(items, 1):
        ws.append(_row_values(idx, r))
        row = ws.max_row
        for col in range(1, len(FIELDS) + 1):
            cell = ws.cell(row=row, column=col)
            # openpyxl 把以 = 开头的字符串当公式写，JD 里出现 "=..." 时会变成公式；
            # 强制回文本，值不变、不会再被 Excel 执行。
            if isinstance(cell.value, str) and cell.value.startswith("="):
                cell.data_type = "s"
            cell.font, cell.border = body_font, border
            cell.alignment = Alignment(vertical="top", wrap_text=col in (11, 20))
        ws.cell(row=row, column=5).number_format = "0.0"
        link = ws.cell(row=row, column=19)
        if str(link.value or "").startswith("http"):
            link.hyperlink = link.value
            link.font = Font(name="微软雅黑", size=9, color="0563C1", underline="single")
        if r["is_ai"]:
            ws.cell(row=row, column=12).font = Font(name="微软雅黑", size=10, bold=True, color="C00000")
        jd_len = len(r["jd"])
        lines = max(1, min(8, (jd_len + 54) // 55))
        ws.row_dimensions[row].height = min(110, 14 * lines)

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
        ("排序", "默认按月薪中位降序；面议/日薪无论升降序都在最后"),
        ("筛选", "与页面当前筛选同一套口径（jobquery），不是整库"),
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


def _csv_safe(v):
    """CSV 里以 = + - @ 开头的值会被 Excel 当公式执行，前面补一个单引号。"""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def build_csv(items):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(FIELDS)
    for idx, r in enumerate(items, 1):
        w.writerow([_csv_safe(v) for v in _row_values(idx, r)])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def count(qs, output_dir):
    """给页面做口径校验：同一组参数，服务端算出多少条。"""
    filters = jobquery.parse_filters(qs)
    return len(collect(output_dir, filters))


def export(qs, output_dir):
    """返回 (bytes, 建议文件名, content-type)。"""
    filters = jobquery.parse_filters(qs)
    items = collect(output_dir, filters)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    tag = "筛选" if any(filters[k] for k in
                       ("q", "city", "area", "exp", "edu", "scale", "active",
                        "keyword", "ai", "industry", "jded")) else "全部"
    base = f"岗位表_{stamp}_{tag}{len(items)}条"
    try:
        return build_xlsx(items, filters), base + ".xlsx", \
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    except ImportError:
        return build_csv(items), base + ".csv", "text/csv; charset=utf-8"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--out", default="")
    for k in ("q", "city", "area", "exp", "edu", "scale", "active",
              "keyword", "ai", "industry"):
        ap.add_argument(f"--{k}", default="")
    ap.add_argument("--jded", action="store_true")
    ap.add_argument("--sort", default="salary")
    ap.add_argument("--dir", default="desc")
    args = ap.parse_args()
    qs = {k: getattr(args, k) for k in
          ("q", "city", "area", "exp", "edu", "scale", "active",
           "keyword", "ai", "industry", "sort", "dir")}
    if args.jded:
        qs["jded"] = "1"
    body, name, _ctype = export(qs, args.output_dir)
    out = args.out or os.path.join(args.output_dir, name)
    with open(out, "wb") as f:
        f.write(body)
    print(json.dumps({"out": out, "bytes": len(body),
                      "items": count(qs, args.output_dir)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
