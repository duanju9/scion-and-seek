#!/usr/bin/env python3
"""汇总夜间巡检日志、SQLite 与趋势，生成巡检报告和优化方案。"""
import argparse
import collections
import datetime
import glob
import json
import os
import re
import sqlite3
import subprocess


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


def norm_title(s):
    """标题规范化：全角冒号、冒号两侧空白、连续空白、大小写统一后再比对（#18）。"""
    t = str(s or "").replace("：", ":").replace("\u3000", " ")
    t = re.sub(r"\s*:\s*", ":", t)
    return " ".join(t.split()).lower()


def issue_states(items):
    """按标题聚合出最终状态。

    closed 是终态：同一标题先关成功、之后又出现一条 close-miss（标题写法不同没找到），
    不能把已关闭的工单翻回「未命中」。只有 close-miss、从来没有建单记录的标题属噪声，直接丢（#18）。
    """
    agg = {}
    for it in items:
        key = norm_title(it.get("title"))
        if not key:
            continue
        ent = agg.setdefault(key, {"title": "", "url": "", "at": "",
                                   "created": False, "closed": False,
                                   "miss": False, "failed": False})
        act = str(it.get("action") or "")
        if it.get("title"):
            ent["title"] = it["title"]
        if str(it.get("url") or "").startswith("http") and (act == "closed" or not ent["url"]):
            ent["url"] = it["url"]
        if act in ("created", "commented"):
            ent["created"] = True
            ent["at"] = it.get("at") or ent["at"]
        elif act == "closed":
            ent["closed"] = True
            ent["at"] = it.get("at") or ent["at"]
        elif act == "close-miss":
            # --close 只找「同标题的 open issue」；没找到说明这个标题下已经没有 open 的了
            ent["miss"] = True
        elif act.endswith("-failed"):
            ent["failed"] = True
    out = {}
    for key, ent in agg.items():
        if ent["closed"]:
            ent["state"] = "closed"
        elif ent["created"] and ent["miss"]:
            ent["state"] = "closed-miss"
        elif ent["created"]:
            ent["state"] = "open"
        elif ent["failed"]:
            ent["state"] = "failed"
        else:
            continue  # 只有 close-miss / 演练，没有建单记录
        out[key] = ent
    return out


def open_issue_count():
    """GitHub 上真实 open 的 issue 数。台账 issues.jsonl 会被 close-miss 污染，报告里以仓库为准。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        p = subprocess.run(["gh", "issue", "list", "--state", "open", "--limit", "100",
                            "--json", "number"], cwd=root, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
        if p.returncode == 0:
            return len(json.loads(p.stdout or "[]"))
    except Exception:
        pass
    return None


def issue_section(output_dir, limit=8):
    items = issue_log(output_dir)
    if not items:
        return ["- 无 issue 记录：本轮未建单。"], None
    states = issue_states(items)
    closed = sum(1 for e in states.values() if e["state"].startswith("closed"))
    opened = sum(1 for e in states.values() if e["state"] == "open")
    failed = [i for i in items if str(i.get("action", "")).endswith("-failed")]
    lines = [f"- Issue 台账（本地记录，close 落空会留痕）：建单未关 {opened} / 已关 {closed} / "
             f"建单或评论失败 {len(failed)} 次；GitHub 实际 open 见下一条"]
    for e in sorted(states.values(), key=lambda x: x.get("at") or "")[-limit:]:
        tag = {"closed": "已关闭", "closed-miss": "已关闭（close 未命中）",
               "open": "开放", "failed": "失败"}.get(e["state"], e["state"])
        url = str(e.get("url") or "")
        link = f" — {url}" if url.startswith("http") else ""
        lines.append(f"- [{tag}] {e['title']}{link}")
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
    open_issues = open_issue_count()
    if open_issues is not None:
        issue_lines.append(f"- GitHub 实际 open：{open_issues} 条")
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
    # 结论看「最近 3 轮」和「当前 open 工单」，而不是整夜全局：02:43 那种重启后自愈的瞬时错误
    # 留在健康表里当历史即可，不该一直把结论按在“需要人工复核”上（#18）
    tail = logs[-3:]
    tail_ok = bool(tail) and all(x.get("status") == "ok" for x in tail)
    open_now = open_issue_count()
    if open_now is None:
        open_now = (_issue_counts or (0, 0, 0))[1] if _issue_counts else 0
    if tail_ok and not open_now:
        verdict = f"数据链路正常：最近 {len(tail)} 轮全部 ok，GitHub 无 open 工单"
    elif tail_ok:
        verdict = f"数据链路正常：最近 {len(tail)} 轮全部 ok，但仍有 {open_now} 个 open 工单待推进"
    else:
        bad = [x.get("run_at", "-") for x in tail if x.get("status") != "ok"]
        verdict = (f"数据链路需要复核：最近 3 轮中 {len(bad)} 轮异常"
                   f"（{('、'.join(bad))[:60]}），open 工单 {open_now} 个")
    lines += ["", "## 七、结论", "",
              f"- {verdict}；整夜 {len(logs)} 轮成功 {ok_runs} 轮，"
              f"当前库内 AI 岗位 {db.get('ai_jobs', 0)} 条，JD 覆盖 {db.get('jd_jobs', 0)} 条。",
              "- 历史异常（健康表里的 error 行）已记录，若下一轮仍复现会自动建单。",
              "- 详细优化项见 `output/优化方案_20261126.md`。"]
    report = "\n".join(lines) + "\n"
    report_path = os.path.join(out, "夜间巡检报告_20261126.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    # ---- 优化方案：按当晚实测数据生成，不用写死模板 ----
    jobs_n = db.get("jobs", 0) or 0
    jd_n = db.get("jd_jobs", 0) or 0
    cov = (jd_n / jobs_n) if jobs_n else 0.0
    backlog = max(0, jobs_n - jd_n)
    rounds_needed = (backlog + 59) // 60 if backlog else 0
    added = 0
    for x in logs:
        try:
            added += int((x.get("refresh") or {}).get("new_jobs") or 0)
        except (TypeError, ValueError):
            pass
    thin = sorted([f"{k}（{v}）" for k, v in pool.items() if v and v < 15])
    top_pool = sorted(pool.items(), key=lambda kv: -kv[1])[:3]
    failed_rounds = [x for x in logs if x.get("status") != "ok"]

    gaps = []
    if advice:
        seg = advice.split("## 技能缺口", 1)
        if len(seg) > 1:
            for line in seg[1].splitlines():
                t = line.strip()
                if t.startswith(("-", "*")):
                    gaps.append(t.lstrip("-* ").split("（")[0].strip())
                if len(gaps) >= 6:
                    break

    p0 = []
    if open_issues:
        p0.append(f"关掉仍挂着的 {open_issues} 个 open issue（本轮已建单未闭环的项，逐条复现→修→回归→关闭）")
    if cov < 0.95:
        p0.append(f"JD 覆盖率 {cov:.1%}（{jd_n}/{jobs_n}），还差 {backlog} 条；"
                  f"单轮上限 60 条，按当前节奏约 {rounds_needed} 轮补齐，补齐前不要报「数据完整」")
    if failed_rounds:
        p0.append(f"复盘 {len(failed_rounds)} 个失败轮次（{failed_rounds[0].get('run_at', '-')} 起），"
                  f"把「等待超时仍落旧主表」这类判 ok 的路径改成显式 error")
    p0.append("keep_days 淘汰与 carry 口径做成可观测指标（carried / expired 进快照表），"
              "否则岗位池「只增不减」时看不出是真实扩量还是僵尸堆积")

    p1 = []
    if added <= 60:
        p1.append(f"当晚累计新增仅 {added} 条，关键词轮换已进饱和区：同一批 8 页 × 5 词反复命中同一批岗位，"
                  f"需换维度（新增关键词 / 提页数 / 分城市）而不是单纯加轮次")
    if thin:
        p1.append(f"下列关键词池子过薄，下一圈优先补：{'、'.join(thin)}")
    if top_pool:
        p1.append("池子结构：" + "、".join(f"{k} {v}" for k, v in top_pool) +
                  "；投递建议应按方向分组（解决方案 / B 端产品 / AI 应用），不要混在一张榜里排序")
    if gaps:
        p1.append("简历缺口按岗位高频要求排序补证据（只写做过的）：" + "、".join(gaps))
    p1.append("技能词归一化（大模型/LLM、RAG/检索增强、Agent/智能体），避免同一能力被拆成多条统计")

    p2 = []
    p2.append("工作台加「最近巡检」卡片：本轮关键词、新增、carried、JD 覆盖、失败原因，一眼可查")
    p2.append("看板增加「本轮新增 / 存量」两列，区分今晚真的进了新岗还是只是把旧岗又数了一遍")
    p2.append("报告与方案每次由脚本按数据生成，禁止写死段落（本轮已把优化方案改成数据驱动）")

    def _block(title, items, fallback):
        return [f"## {title}", ""] + ([f"{i}. {t}" for i, t in enumerate(items, 1)] if items else [fallback]) + [""]

    plan_lines = [f"# 优化方案（{args.day}）", "",
                  "## 现状（截至最近一轮巡检）", "",
                  f"- 岗位池 {jobs_n} 条 / AI 相关 {db.get('ai_jobs', 0)} 条 / 带 JD {jd_n} 条（覆盖 {cov:.1%}）",
                  f"- 当晚 {len(logs)} 轮巡检，成功 {ok_runs} 轮；累计新增 {added} 条；最新快照 {db.get('latest_snapshot', '-')}",
                  f"- 池子构成：" + ("、".join(f"{k} {v}" for k, v in sorted(pool.items(), key=lambda kv: -kv[1])) or "-"),
                  ""]
    plan_lines += _block("P0 · 正确性与完整性", p0, "无阻塞项：保持每轮 compile / API / 看板 marker 三查。")
    plan_lines += _block("P1 · 扩量与匹配", p1, "样本已够，转入匹配质量优化。")
    plan_lines += _block("P2 · 体验与自动化", p2, "无。")
    plan_lines += ["## 验收标准", "",
                   f"1. 连续 2 轮巡检 status=ok，且 checks 无 warning；当前 JD 覆盖 {cov:.1%}，"
                   f"要求提升到 {(0.95 if cov < 0.95 else round(cov, 2)):.0%} 以上"
                   f"（即至少 {int(jobs_n * (0.95 if cov < 0.95 else cov))} 条带 JD）。",
                   "2. 每轮刷新 carried 与 new_jobs 均为非负整数且 carried>0（并集生效），"
                   "快照时间严格递增。",
                   "3. 本轮发现的 issue 全部 closed，且关闭动作由 report_issue.py --close 完成（不再手工 gh）。",
                   "4. 看板可打开、简历栏可上传、自动投递保持 disabled；未发送任何招呼。",
                   "5. 06:30 后产出定稿的 `夜间巡检报告_20261126.md` 与 `优化方案_20261126.md`。"]
    plan = "\n".join(plan_lines) + "\n"
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
