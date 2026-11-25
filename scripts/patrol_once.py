#!/usr/bin/env python3
"""夜间巡检单轮：健康检查 -> 触发/等待刷新 -> 落库 -> 测试 -> 简历建议 -> 写日志。

只做本地巡检与数据更新，**不发送招呼、不启用自动投递、不修改用户原始简历**。
服务未启动时会尝试以常驻后台方式拉起 dashboard_server.py。
"""
import argparse
import collections
import datetime
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from urllib.request import Request, urlopen


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def api(base, path, method="GET", timeout=20):
    req = Request(base.rstrip("/") + path, method=method)
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def parse_ts(s):
    try:
        return datetime.datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def prior_snapshot(patrol_dir):
    """取最近一份 patrol JSON 里的快照时间，用来判断快照是否递增。"""
    try:
        files = sorted(f for f in os.listdir(patrol_dir) if f.startswith("patrol_") and f.endswith(".json"))
    except Exception:
        return "", ""
    for fn in reversed(files):
        d = load_json(os.path.join(patrol_dir, fn), {}) or {}
        ts = str(((d.get("db") or {}).get("latest_snapshot")) or "")
        if ts:
            return ts, fn
    return "", ""


def health_errors(status):
    """runbook 的 cdp_down / login_required 落成可判定的错误。

    只把健康态写进 log["health"] 等于没有判定：Chrome 关掉或登录过期时本轮一条数据都采不到，
    数据全旧却仍然 status=ok，整夜静默停摆。
    """
    st = status or {}
    if not st.get("cdp"):
        return ["cdp_down：CDP 不可用，本轮无在线采集（未尝试重启 Chrome、未尝试登录）"]
    if not st.get("logged_in"):
        return ["login_required：Boss 未登录，已跳过在线采集（未扫码、未绕过验证码）"]
    return []


def data_checks(db, prev_snapshot, backlog=0):
    """把 runbook 的三条异常标准写成可执行检查：岗位 0 / JD 覆盖 <90% / 快照未递增。

    backlog>0 表示刷新开着单轮补抓上限（--detail-limit），覆盖率暂时偏低是设计内行为，
    只记 note 不算异常，否则轮换期间每轮都会被判 error。
    """
    out = {"ok": True, "jd_coverage": None, "latest_snapshot": "",
           "prev_snapshot": prev_snapshot, "notes": []}
    jobs = int(db.get("jobs") or 0)
    jd = int(db.get("jd_jobs") or 0)
    latest = str(db.get("latest_snapshot") or "")
    out["latest_snapshot"] = latest
    if jobs <= 0:
        out["ok"] = False
        out["notes"].append("岗位数为 0：列表采集失败或登录态失效")
    else:
        cov = jd / float(jobs)
        out["jd_coverage"] = round(cov, 4)
        if cov < 0.9:
            if backlog > 0:
                out["notes"].append(
                    f"JD 覆盖率 {cov:.1%}（{jd}/{jobs}）：单轮上限模式，积压 {backlog} 条待后续轮次消化")
            else:
                out["ok"] = False
                out["notes"].append(f"JD 覆盖率 {cov:.1%} < 90%（{jd}/{jobs}）")
    if prev_snapshot and latest and latest < prev_snapshot:
        out["ok"] = False
        out["notes"].append(f"最新快照 {latest} 早于上轮 {prev_snapshot}，快照未递增")
    return out


def wait_refresh(base, max_minutes=20):
    deadline = time.time() + max_minutes * 60
    last = {}
    while time.time() < deadline:
        try:
            last = api(base, "/api/refresh/status")
        except Exception as e:
            return {"status": "unreachable", "error": str(e)}
        if last.get("status") != "running":
            return last
        time.sleep(15)
    last["wait_timeout"] = True
    return last


def server_alive(base, timeout=5):
    """端口上已有活服务时返回 True，避免重复拉起第二个实例。"""
    try:
        api(base, "/api/status", timeout=timeout)
        return True
    except Exception:
        return False


def maybe_start_server(args):
    if server_alive(args.base):
        return True
    script = os.path.join(args.skill_dir, "scripts", "dashboard_server.py")
    cmd = [sys.executable, script, "--port", str(args.port), "--cdp-port", str(args.cdp_port),
           "--dashboard", args.dashboard, "--output-dir", args.output_dir,
           "--interval-minutes", "30", "--no-browser"]
    kwargs = {"cwd": args.skill_dir, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(cmd, **kwargs)
        # 起来后二次确认，起不来就别当成已启动
        for _ in range(6):
            time.sleep(2)
            if server_alive(args.base):
                return True
        return False
    except Exception:
        return False


def resume_advice(output_dir, master_jobs):
    resume = load_json(os.path.join(output_dir, "resume.json"), {})
    targets = load_json(os.path.join(output_dir, "targets.json"), [])
    path = os.path.join(output_dir, "简历优化建议.md")
    if not resume:
        text = "# 简历优化建议\n\n尚未上传简历。请在工作台 http://127.0.0.1:8765/ 上传 PDF/DOCX/TXT/MD。\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return {"has_resume": False, "path": path}

    def norm(s):
        return re.sub(r"[\s/()（）\-]+", "", str(s or "").lower())

    resume_skills = {norm(s) for s in resume.get("skills", [])}
    freq = collections.Counter()
    for job in master_jobs:
        raw = str(job.get("skills") or "") + "|" + str(job.get("skill_tags") or "")
        for s in [x.strip() for x in raw.split("|") if x.strip()]:
            freq[s] += 1
    missing = [(s, n) for s, n in freq.most_common(40) if norm(s) not in resume_skills][:12]

    lines = [
        "# 简历优化建议（自动生成）", "",
        f"> 生成时间：{now()}；基于 {len(master_jobs)} 个岗位、{len(targets)} 个匹配目标。",
        "> 只给表述建议，不会修改或上传你的原始简历。", "",
        "## 当前简历", "",
        f"- 文件：{resume.get('original_name') or os.path.basename(str(resume.get('source_file') or ''))}",
        f"- 年限：{resume.get('years_experience') or '未识别'}",
        f"- 学历：{'/'.join(resume.get('education') or []) or '未识别'}",
        f"- 技能：{', '.join(resume.get('skills', [])[:16]) or '未识别'}",
        "", "## 技能缺口（岗位高频，简历未出现）", "",
        "| 技能 | 岗位出现次数 | 建议 |", "| --- | --- | --- |",
    ]
    for s, n in missing:
        lines.append(f"| {s} | {n} | 如果确有实践，补进技能栏并写一条项目证据；没有就不要虚构 |")
    lines += ["", "## 简历表述建议", ""]
    if missing:
        lines.append(f"1. 技能栏优先补齐：{', '.join(s for s, _ in missing[:6])}；每个技能最好对应一条项目 bullet。")
    lines.append("2. 项目 bullet 用「场景 + 动作 + 技术 + 结果」四段式，结果尽量量化（如准确率、耗时、成本）。")
    lines.append("3. 把 JD 里的原词（如 RAG、Agent、模型微调、向量检索）自然嵌入项目描述，避免只写“熟悉 AI”。")
    lines.append("4. 工作经历按「业务问题 → 你的方案 → 技术栈 → 指标」重写，删掉无证据的形容词。")
    lines.append("5. 针对不同岗位保留一版主简历 + 一版关键词微调版，不要一份简历投所有方向。")

    lines += ["", "## 匹配 Top 岗位", ""]
    if targets:
        for i, t in enumerate(targets[:5], 1):
            lines.append(f"{i}. {t.get('title')} — {t.get('recipient')}（{t.get('salary')}，匹配 {t.get('score')} 分）")
    else:
        lines.append("暂无 targets.json；刷新岗位后在“简历”栏点“重新匹配”。")
    lines += ["", "## 关键词对齐", "",
              "把下列词与你的真实经历对照，能在项目里找到证据的写进简历，找不到的不要硬塞：", "",
              "、".join(s for s, _ in missing[:10]) or "暂无"]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return {"has_resume": True, "path": path, "missing": [s for s, _ in missing[:10]],
            "targets": len(targets)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--cdp-port", type=int, default=9222)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--skill-dir", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--dashboard", default="")
    ap.add_argument("--db", default="")
    ap.add_argument("--no-refresh", action="store_true")
    ap.add_argument("--start-server", action="store_true")
    ap.add_argument("--max-wait-minutes", type=int, default=20,
                    help="多关键词轮换后单轮实测 13-17 分钟（列表 4 分钟 + 明细 60 条约 13 分钟），"
                         "等待窗口给到 20 分钟，避免把还在跑的刷新误判成结束")
    args = ap.parse_args()
    args.dashboard = args.dashboard or os.path.join(args.output_dir, "岗位数据看板.html")
    args.db = args.db or os.path.join(args.output_dir, "jobs.db")

    stamp = now()
    log = {"run_at": stamp, "health": {}, "refresh": {}, "db": {},
           "api": {}, "tests": {}, "advice": {}, "errors": []}

    try:
        status = api(args.base, "/api/status", timeout=15)
    except Exception as e:
        log["errors"].append(f"server unreachable: {e}")
        if args.start_server and maybe_start_server(args):
            try:
                status = api(args.base, "/api/status", timeout=15)
            except Exception as e2:
                status = {"cdp": False, "logged_in": False, "message": str(e2)}
        else:
            status = {"cdp": False, "logged_in": False, "message": "server unreachable"}
    log["health"]["status"] = status
    log["errors"].extend(health_errors(status))

    refresh_state = {}
    if not args.no_refresh:
        try:
            refresh_state = api(args.base, "/api/refresh/status", timeout=15)
            if refresh_state.get("status") == "running":
                refresh_state = wait_refresh(args.base, args.max_wait_minutes)
            elif status.get("logged_in"):
                last = parse_ts(refresh_state.get("last_ok_at"))
                due = last is None or (datetime.datetime.now() - last).total_seconds() >= 30 * 60
                if due:
                    api(args.base, "/api/refresh", method="POST", timeout=20)
                    time.sleep(1)
                    refresh_state = wait_refresh(args.base, args.max_wait_minutes)
        except Exception as e:
            log["errors"].append(f"refresh failed: {e}")
            refresh_state = {"status": "error", "error": str(e)}
    log["refresh"] = refresh_state
    # 超时还对着旧主表落库，等于把上一轮的数据当成这一轮的结论，必须显式记错（runbook 的异常要留痕）
    if refresh_state.get("wait_timeout"):
        log["errors"].append(
            f"refresh 等待超时（>{args.max_wait_minutes} 分钟仍在 running，"
            f"started_at={refresh_state.get('started_at', '')}），本轮落库可能仍是旧主表")

    # 落库
    master_path = os.path.join(args.output_dir, "岗位公司信息_含JD.json")
    trends_path = os.path.join(args.output_dir, "trends.json")
    try:
        script = os.path.join(args.skill_dir, "scripts", "ingest_db.py")
        p = subprocess.run([sys.executable, script, "--input", master_path, "--db", args.db,
                            "--trends", trends_path, "--label", "patrol"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=120)
        if p.returncode == 0:
            log["db"] = json.loads(p.stdout)
        else:
            log["errors"].append(f"ingest_db: {(p.stderr or p.stdout)[-300:]}")
    except Exception as e:
        log["errors"].append(f"ingest_db exception: {e}")

    # 数据异常判定（runbook：岗位 0 / JD 覆盖 <90% / 快照未递增）
    patrol_dir = os.path.join(args.output_dir, "patrol")
    prev_snap, prev_file = prior_snapshot(patrol_dir) if os.path.isdir(patrol_dir) else ("", "")
    log["checks"] = data_checks(log.get("db") or {}, prev_snap,
                                backlog=int(log["refresh"].get("jd_missing") or 0))
    log["checks"]["prev_patrol"] = prev_file
    if log["checks"].get("ok"):
        # 上限模式的积压说明只是提示，不该把整轮判成失败
        log["warnings"] = ["data check: " + n for n in log["checks"]["notes"]]
    else:
        for note in log["checks"]["notes"]:
            log["errors"].append("data check: " + note)

    # API 冒烟
    for path in ("/api/jobs", "/api/trends", "/api/resume/status", "/api/auto-apply/status"):
        try:
            data = api(args.base, path, timeout=20)
            log["api"][path] = "ok" if isinstance(data, (dict, list)) else "empty"
        except Exception as e:
            log["api"][path] = f"error: {e}"
            log["errors"].append(f"api {path}: {e}")

    # 编译 + 看板静态检查
    try:
        p = subprocess.run([sys.executable, "-m", "compileall", "-q",
                            os.path.join(args.skill_dir, "scripts")],
                           capture_output=True, text=True, timeout=120)
        log["tests"]["compile"] = "ok" if p.returncode == 0 else (p.stderr or p.stdout)[-300:]
        if p.returncode != 0:
            log["errors"].append("compile failed")
    except Exception as e:
        log["tests"]["compile"] = f"error: {e}"
    try:
        html = open(args.dashboard, encoding="utf-8").read()
        markers = ["trendgrid", "btnResumeUpload", "applybar", "AbortController"]
        log["tests"]["dashboard"] = {
            "size": len(html), "markers": {m: m in html for m in markers}}
        if not all(m in html for m in markers):
            log["errors"].append("dashboard markers missing")
    except Exception as e:
        log["tests"]["dashboard"] = f"error: {e}"
        log["errors"].append(f"dashboard: {e}")

    master = load_json(master_path, {})
    jobs = master.get("jobs", []) if isinstance(master, dict) else master
    # 主表与 SQLite 条数对不上说明 ingest 漏写/主表被并发改写，落库结果不能当结论
    master_n = len(jobs or [])
    db_n = int((log.get("db") or {}).get("jobs") or 0)
    log["checks"]["master_jobs"] = master_n
    log["checks"]["db_master_delta"] = db_n - master_n if master_n else None
    if master_n and db_n != master_n:
        log["errors"].append(
            f"落库 {db_n} 条与主表 {master_n} 条不一致（差值 {db_n - master_n}）：ingest 可能漏写")
    log["advice"] = resume_advice(args.output_dir, jobs)

    log["status"] = "error" if log["errors"] else "ok"
    patrol_dir = os.path.join(args.output_dir, "patrol")
    os.makedirs(patrol_dir, exist_ok=True)
    fn = os.path.join(patrol_dir, "patrol_" + stamp.replace(":", "").replace(" ", "_") + ".json")
    with open(fn, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)

    journal = os.path.join(args.output_dir, "巡检日志.md")
    first = not os.path.exists(journal)
    with open(journal, "a", encoding="utf-8") as f:
        if first:
            f.write("# 夜间巡检日志\n\n")
            f.write("| 时间 | 健康 | 刷新 | 岗位 | AI 岗位 | JD | 错误 |\n")
            f.write("| --- | --- | --- | --- | --- | --- | --- |\n")
        db = log.get("db", {})
        err = "；".join(log["errors"])[:160].replace("|", "/") or "-"
        f.write(f"| {stamp} | {log['status']} | {log['refresh'].get('status','-')} | "
                f"{db.get('jobs','-')} | {db.get('ai_jobs','-')} | {db.get('jd_jobs','-')} | {err} |\n")
    log["log_path"] = fn
    print(json.dumps(log, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
