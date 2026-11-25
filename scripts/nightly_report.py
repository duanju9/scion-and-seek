#!/usr/bin/env python3
"""汇总夜间巡检日志、SQLite 与趋势，生成巡检报告和优化方案。"""
import argparse
import collections
import datetime
import glob
import json
import os
import sqlite3


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def db_stats(path):
    if not os.path.isfile(path):
        return {}
    conn = sqlite3.connect(path)
    try:
        def one(sql, default=0):
            row = conn.execute(sql).fetchone()
            return row[0] if row and row[0] is not None else default
        return {
            "jobs": one("SELECT COUNT(*) FROM jobs"),
            "ai_jobs": one("SELECT COUNT(*) FROM jobs WHERE is_ai=1"),
            "jd_jobs": one("SELECT COUNT(*) FROM jobs WHERE has_jd=1"),
            "companies": one("SELECT COUNT(DISTINCT company) FROM jobs"),
            "snapshots": one("SELECT COUNT(*) FROM snapshots"),
            "latest_snapshot": one("SELECT MAX(ts) FROM snapshots", ""),
            "top_skills": conn.execute(
                "SELECT skills FROM jobs WHERE is_ai=1 AND skills IS NOT NULL AND skills!='' LIMIT 200"
            ).fetchall(),
        }
    finally:
        conn.close()


def issue_log(output_dir):
    """读取 output/issues.jsonl，按标题聚合出最新状态。"""
    path = os.path.join(output_dir, "issues.jsonl")
    items = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except Exception:
                    continue
    return items


ISSUE_TAG = {"created": "新建", "commented": "追加", "closed": "已关闭",
             "close-miss": "未命中", "dry-run": "演练"}


def issue_section(output_dir, limit=8):
    items = issue_log(output_dir)
    if not items:
        return ["- 无 issue 记录：本轮未建单。"], None
    latest = {}
    for it in items:
        latest[it.get("title", "")] = it
    closed = sum(1 for i in latest.values() if i.get("action") == "closed")
    opened = sum(1 for i in latest.values()
                 if i.get("action") in ("created", "commented", "close-miss"))
    failed = [i for i in items if str(i.get("action", "")).endswith("-failed")]
    lines = [f"- Issue 台账：open {opened} / closed {closed} / 建单或评论失败 {len(failed)} 次"]
    for title, it in list(latest.items())[-limit:]:
        tag = ISSUE_TAG.get(it.get("action"), it.get("action") or "-")
        url = str(it.get("url") or "")
        link = f" — {url}" if url.startswith("http") else ""
        lines.append(f"- [{tag}] {title}{link}")
    if failed:
        last = failed[-1]
        reason = str(last.get("output") or last.get("error") or "")[:120].replace("\n", " ")
        lines.append(f"- 最近一次失败：{last.get('at')} {reason}")
    return lines, (closed, opened, len(failed))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--db", default="")
    ap.add_argument("--day", default=datetime.date.today().isoformat())
    args = ap.parse_args()
    args.db = args.db or os.path.join(args.output_dir, "jobs.db")
    out = args.output_dir

    logs = []
    for fn in sorted(glob.glob(os.path.join(out, "patrol", "patrol_*.json"))):
        d = load_json(fn, {})
        if d:
            d["_file"] = os.path.basename(fn)
            logs.append(d)
    db = db_stats(args.db)
    trends = load_json(os.path.join(out, "trends.json"), {}) or {}
    resume = load_json(os.path.join(out, "resume.json"), {}) or {}
    targets = load_json(os.path.join(out, "targets.json"), []) or []
    advice_path = os.path.join(out, "简历优化建议.md")
    advice = open(advice_path, encoding="utf-8").read() if os.path.isfile(advice_path) else ""

    ok_runs = sum(1 for x in logs if x.get("status") == "ok")
    errors = collections.Counter()
    for x in logs:
        for e in x.get("errors", []):
            errors[str(e)[:120]] += 1
    first = logs[0]["run_at"] if logs else "-"
    last = logs[-1]["run_at"] if logs else "-"

    latest = trends.get("latest", {}) or {}
    delta = trends.get("delta", {}) or {}
    skill_counter = collections.Counter()
    for (raw,) in db.get("top_skills", []):
        for s in str(raw).split("|"):
            s = s.strip()
            if s:
                skill_counter[s] += 1
    top_skills = "、".join(f"{s}（{n}）" for s, n in skill_counter.most_common(10)) or "暂无"

    last_refresh = (logs[-1].get("refresh", {}) or {}) if logs else {}
    pool = last_refresh.get("pool_by_keyword") or {}
    pool_txt = " / ".join(f"{k} {v}" for k, v in
                          sorted(pool.items(), key=lambda x: -x[1])[:6]) or "-"
    lines = [
        f"# 夜间巡检报告（{args.day}）", "",
        f"> 统计范围：{first} — {last}；共 {len(logs)} 轮，成功 {ok_runs} 轮。", "",
        "## 一、执行概览", "",
        f"- 巡检轮次：{len(logs)}；成功：{ok_runs}；异常：{len(logs) - ok_runs}",
        f"- 最近一轮扩量：关键词「{last_refresh.get('keyword') or '-'}」"
        f"新增 {last_refresh.get('new_jobs', '-')} 条，存量带过 {last_refresh.get('carried', '-')} 条",
        f"- 岗位池按关键词：{pool_txt}",
        f"- 岗位总数（SQLite）：{db.get('jobs', 0)}；AI 相关：{db.get('ai_jobs', 0)}；含 JD：{db.get('jd_jobs', 0)}",
        f"- 覆盖公司：{db.get('companies', 0)}；快照期数：{db.get('snapshots', 0)}；最新快照：{db.get('latest_snapshot', '-')}",
        f"- 最新趋势：岗位 {latest.get('count', '-')} 条，薪资中位数 {latest.get('salary_mid', '-')}K，"
        f"Boss 活跃率 {latest.get('active_rate', '-')}%；环比岗位 {delta.get('job_count', '-')}、薪资 {delta.get('salary_mid', '-')}",
        "", "## 二、数据与落库", "",
        "| 指标 | 数值 |", "| --- | --- |",
        f"| 岗位主表 | {db.get('jobs', 0)} |",
        f"| AI 相关岗位 | {db.get('ai_jobs', 0)} |",
        f"| JD 覆盖 | {db.get('jd_jobs', 0)} |",
        f"| 公司数 | {db.get('companies', 0)} |",
        f"| 快照期数 | {db.get('snapshots', 0)} |",
        f"| AI 岗位高频技能 | {top_skills} |",
        "", "## 三、健康与测试", "",
    ]
    if logs:
        lines += ["| 时间 | 健康 | 刷新 | 岗位 | AI | JD | 错误 |", "| --- | --- | --- | --- | --- | --- | --- |"]
        for x in logs[-20:]:
            d = x.get("db", {})
            err = "；".join(x.get("errors", []))[:100].replace("|", "/") or "-"
            lines.append(f"| {x.get('run_at','-')} | {x.get('status','-')} | "
                         f"{x.get('refresh',{}).get('status','-')} | {d.get('jobs','-')} | "
                         f"{d.get('ai_jobs','-')} | {d.get('jd_jobs','-')} | {err} |")
    else:
        lines.append("暂无巡检日志。")
    lines += ["", "## 四、问题与修复", ""]
    if errors:
        for e, n in errors.most_common(12):
            lines.append(f"- {e}（{n} 次）")
    else:
        lines.append("- 本轮未发现阻塞性错误。")
    lines.append("")
    issue_lines, _issue_counts = issue_section(out)
    lines += issue_lines
    lines += [
        "- 保留护栏：自动投递默认关闭；测试简历拒绝启用；风控/验证码立即停止。",
        "", "## 五、简历与岗位匹配", "",
        f"- 简历：{resume.get('original_name') or resume.get('source_file') or '未上传'}",
        f"- 年限：{resume.get('years_experience') or '未识别'}；学历：{'/'.join(resume.get('education') or []) or '未识别'}",
        f"- 技能：{', '.join(resume.get('skills', [])[:16]) or '未识别'}",
        f"- 匹配目标：{len(targets)} 条",
    ]
    if targets:
        lines.append("- Top 5：")
        for i, t in enumerate(targets[:5], 1):
            lines.append(f"  {i}. {t.get('title')} — {t.get('recipient')}（{t.get('salary')}，{t.get('score')} 分）")
    if advice:
        body = advice.split("## 当前简历", 1)
        if len(body) > 1:
            section = "## 当前简历\n\n" + body[1].split("## 技能缺口")[0].strip()
        else:
            section = advice.split("## 技能缺口")[0].strip()
        lines += ["", "## 六、简历优化摘要", "", section]
        lines += ["", "完整建议见 `output/简历优化建议.md`。"]
    lines += ["", "## 七、结论", "",
              f"- 数据链路{'正常' if len(logs) == ok_runs else '存在异常，需要人工复核'}；"
              f"当前库内 AI 岗位 {db.get('ai_jobs', 0)} 条，JD 覆盖 {db.get('jd_jobs', 0)} 条。",
              "- 详细优化项见 `output/优化方案_20261126.md`。"]
    report = "\n".join(lines) + "\n"
    report_path = os.path.join(out, "夜间巡检报告_20261126.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    plan = f"""# 优化方案（{args.day}）

## P0 · 稳定性
1. 登录态巡检：每轮先读 `/api/status`，刷新中不 attach 页面；登录失效只记录、不绕过验证码。
2. 刷新互斥：`CDP_GATE` 串行化刷新/会话/招呼；刷新中禁止投递。
3. 数据完整性：交付前校验 `has_jd` 覆盖率，低于 95% 自动补抓。

## P1 · 数据与匹配
1. 继续扩充 AI 岗位样本，按城市/行业分层，避免单一关键词偏差。
2. 把岗位技能做归一化（大模型/LLM、RAG/检索增强），降低重复统计。
3. 简历匹配增加“岗位方向”约束：当前简历偏 B 端产品/解决方案，投纯算法岗匹配度天然偏低，应增加 AI 产品/解决方案/AI 交付岗的岗位池。
4. 每轮更新 `简历优化建议.md`，只给有证据的写法，不虚构经历。

## P2 · 体验与自动化
1. 工作台增加“最近巡检”卡片与失败重试按钮。
2. 夜间巡检输出异常摘要，早上直接看报告。
3. GitHub 只提交代码与文档，个人简历、岗位数据、数据库、快照全部留在本地。

## 验收标准
- 每 30 分钟有一轮巡检记录，连续 2 轮无阻塞错误。
- `jobs.db` 的 AI 岗位数、JD 覆盖数可查询，快照时间递增。
- 看板可打开、简历栏可上传、登录按钮不再卡住。
- 07:00 前产出 `夜间巡检报告_20261126.md` 与 `优化方案_20261126.md`。
"""
    plan_path = os.path.join(out, "优化方案_20261126.md")
    with open(plan_path, "w", encoding="utf-8") as f:
        f.write(plan)

    summary = {"ok": True, "runs": len(logs), "ok_runs": ok_runs,
               "jobs": db.get("jobs", 0), "ai_jobs": db.get("ai_jobs", 0),
               "jd_jobs": db.get("jd_jobs", 0), "report": report_path, "plan": plan_path}
    with open(os.path.join(out, "nightly_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
