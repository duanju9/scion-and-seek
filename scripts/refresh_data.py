#!/usr/bin/env python3
"""持续更新编排：列表采集 → 增量补 JD → 合并主表 → 快照 → 趋势 → 看板 → 分析。

既可独立运行，也可由 dashboard_server.py 在后台线程调用。
所有状态写入 state.json（原子替换），前端通过 /api/refresh/status 轮询。
"""
import argparse
import csv
import datetime
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


def _load(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


class _Logger(list):
    """追加即落盘，让前端能实时看到刷新进度。"""

    def __init__(self, state, path):
        super().__init__()
        self._state = state
        self._path = path
        state["steps"] = self

    def append(self, item):
        super().append(item)
        try:
            _write_state(self._path, self._state)
        except Exception:
            pass


def backfill_from_previous(master_path, previous_path, csv_path,
                           fields=("boss_active_status",)):
    """用上期同 job_id 的字段回填本期页面渲染缺失值，避免趋势出现假跳变。"""
    cur = _load(master_path)
    old = _load(previous_path)
    if not cur or not old:
        return 0
    old_jobs = old.get("jobs", []) if isinstance(old, dict) else old
    mapping = {}
    for j in old_jobs:
        jid = str(j.get("job_id") or "")
        if jid:
            mapping[jid] = {f: j[f] for f in fields if str(j.get(f) or "").strip()}
    jobs = cur.get("jobs", []) if isinstance(cur, dict) else cur
    filled = 0
    for j in jobs:
        jid = str(j.get("job_id") or "")
        for f in fields:
            if not str(j.get(f) or "").strip() and mapping.get(jid, {}).get(f):
                j[f] = mapping[jid][f]
                filled += 1
    with open(master_path, "w", encoding="utf-8") as fp:
        json.dump(cur, fp, ensure_ascii=False, indent=2)
    # CSV 同步，避免两份数据不一致
    try:
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as fp:
            writer = csv.DictWriter(fp, fieldnames=list(jobs[0].keys()) if jobs else [])
            writer.writeheader()
            writer.writerows(jobs)
    except Exception:
        pass
    return filled


def _run(cmd, timeout, log):
    """执行子脚本，返回 (ok, 输出摘要)。"""
    log.append("$ " + " ".join(cmd))
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        out = (p.stdout or "") + (p.stderr or "")
        log.append(out[-1200:].strip())
        return p.returncode == 0, out[-400:]
    except subprocess.TimeoutExpired:
        log.append(f"TIMEOUT after {timeout}s")
        return False, f"超时 {timeout}s"
    except Exception as e:
        log.append(f"ERROR {e}")
        return False, str(e)


def _logged_in(cdp_port):
    """用 CDP 快速判断专用浏览器是否处于登录态。"""
    try:
        spec = importlib.util.spec_from_file_location("boss_cdp_raw", os.path.join(HERE, "boss_cdp_raw.py"))
        boss = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(boss)
        cdp = boss.CDPSession(cdp_port)
        try:
            for t in cdp.send("Target.getTargets")["result"]["targetInfos"]:
                if t["type"] == "page" and "web/geek/chat" in t.get("url", ""):
                    sid = cdp.send("Target.attachToTarget", {"targetId": t["targetId"], "flatten": True})["result"]["sessionId"]
                    txt = cdp.eval_js("document.body.innerText", sid) or ""
                    return ("没有更多了" in txt or "与您进行过沟通" in txt or "全部" in txt and "未读" in txt), ""
            return False, "未打开聊天页"
        finally:
            cdp.close()
    except Exception as e:
        return False, f"CDP 连接失败：{e}"


def refresh(cfg, state_path, logged_check=None):
    """执行一次完整刷新，返回最终 state。

    logged_check 可选：由调用方（服务端）复用已有 CDP 会话判断登录态，
    避免在刷新线程里重复建立 websocket 连接造成阻塞。
    """
    state = _load(state_path, {}) or {}
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state.update({
        "status": "running", "started_at": now, "finished_at": "",
        "error": "", "steps": [], "ok_jobs": 0, "ok_jd": 0,
    })
    log = _Logger(state, state_path)
    _write_state(state_path, state)

    out_dir = cfg["output_dir"]
    hist_dir = cfg["history_dir"]
    tmp_dir = os.path.join(hist_dir, "_work")
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(hist_dir, exist_ok=True)
    os.makedirs(tmp_dir, exist_ok=True)

    master = os.path.join(out_dir, "岗位公司信息_含JD.json")
    csv_out = os.path.join(out_dir, "岗位公司信息_含JD.csv")
    trends = os.path.join(out_dir, "trends.json")
    dashboard = cfg["dashboard"]
    analysis = os.path.join(out_dir, "analysis_summary.txt")
    tmp_jobs = os.path.join(tmp_dir, "latest_jobs.json")
    seed_details = os.path.join(tmp_dir, "seed_details.json")
    merged_details = os.path.join(tmp_dir, "details_merged.json")

    try:
        # 0. 登录态
        if logged_check is not None:
            try:
                logged, why = bool(logged_check()), ""
            except Exception as e:
                logged, why = False, str(e)
        else:
            logged, why = _logged_in(cfg["cdp_port"])
        log.append(f"[0/7] 登录态检查：{'已登录' if logged else '未登录 - ' + why}")
        if not logged:
            raise RuntimeError("专用浏览器未登录，请先在工作台完成登录；不启动采集")

        # 1. 列表（不抓详情，快）
        log.append(f"[1/7] 采集列表：关键词={cfg['keyword']} 城市={cfg['city']} 页数={cfg['pages']}")
        ok, _ = _run([PY, os.path.join(HERE, "boss_cdp_raw.py"),
                      "--keyword", cfg["keyword"], "--city", cfg["city"],
                      "--pages", str(cfg["pages"]), "--no-detail",
                      "--output", tmp_jobs, "--cdp-port", str(cfg["cdp_port"])],
                     600, log)
        jobs_data = _load(tmp_jobs, {}) or {}
        jobs = jobs_data.get("jobs", []) if isinstance(jobs_data, dict) else jobs_data
        if not ok or not jobs:
            raise RuntimeError("列表采集失败或返回 0 条，已保留上次数据")
        log.append(f"列表 {len(jobs)} 条")

        # 2. 用当前主表的 JD 作为种子，避免重复抓；同时留一份供字段回填
        old_master = _load(master, {}) or {}
        old_jobs = old_master.get("jobs", []) if isinstance(old_master, dict) else old_master
        prev_master = os.path.join(tmp_dir, "previous_master.json")
        if os.path.isfile(master):
            shutil.copy2(master, prev_master)
        with open(seed_details, "w", encoding="utf-8") as f:
            json.dump([{"job_id": j.get("job_id", ""), "jd": j.get("jd", "")}
                       for j in old_jobs if j.get("jd")], f, ensure_ascii=False)
        log.append(f"[2/7] JD 种子 {sum(1 for j in old_jobs if j.get('jd'))} 条，只需补新增")

        # 3. 增量补 JD
        log.append(f"[3/7] 增量补抓 JD（间隔 {cfg['gap_min']}-{cfg['gap_max']}s）")
        ok, _ = _run([PY, os.path.join(HERE, "fetch_missing_details.py"),
                      "--jobs", tmp_jobs, "--details", seed_details,
                      "--output", merged_details, "--cdp-port", str(cfg["cdp_port"]),
                      "--gap-min", str(cfg["gap_min"]), "--gap-max", str(cfg["gap_max"])],
                     cfg.get("detail_timeout", 1200), log)
        if not ok or not os.path.isfile(merged_details):
            log.append("补抓异常，降级为仅用种子 JD 合并")
            if not os.path.isfile(merged_details):
                with open(merged_details, "w", encoding="utf-8") as f:
                    json.dump([], f)

        # 4. 合并主表
        log.append("[4/7] 合并主表")
        ok, _ = _run([PY, os.path.join(HERE, "merge_to_master.py"),
                      "--jobs", tmp_jobs, "--details", merged_details,
                      "--out-json", master, "--out-csv", csv_out], 120, log)
        if not ok:
            raise RuntimeError("合并主表失败")
        if os.path.isfile(prev_master):
            n = backfill_from_previous(master, prev_master, csv_out)
            if n:
                log.append(f"从上期回填 {n} 个缺失字段（主要是 Boss 活跃状态）")
        fresh = _load(master, {}) or {}
        fresh_jobs = fresh.get("jobs", []) if isinstance(fresh, dict) else fresh
        state["ok_jobs"] = len(fresh_jobs)
        state["ok_jd"] = sum(1 for j in fresh_jobs if j.get("has_jd") == "yes")

        # 5. 快照 + 趋势
        log.append("[5/7] 写入快照并计算行业岗位趋势")
        ok, _ = _run([PY, os.path.join(HERE, "analyze_trends.py"),
                      "--history", hist_dir, "--add-from", master,
                      "--out", trends, "--keep", str(cfg.get("keep", 60)),
                      "--top", "10"], 120, log)
        if not ok:
            raise RuntimeError("趋势计算失败")
        tr = _load(trends, {}) or {}
        state["snapshot_count"] = tr.get("snapshot_count", 0)

        # 6. 看板（含内嵌趋势）
        log.append("[6/7] 重新生成看板")
        cmd = [PY, os.path.join(HERE, "generate_dashboard.py"),
               "--input", master, "--output", dashboard]
        if os.path.isfile(trends):
            cmd += ["--trends", trends]
        ok, _ = _run(cmd, 120, log)
        if not ok:
            raise RuntimeError("看板生成失败")

        # 7. 分析文本
        log.append("[7/7] 更新岗位分析报告")
        try:
            with open(analysis, "w", encoding="utf-8") as f:
                subprocess.run([PY, os.path.join(HERE, "job_summary.py"),
                                "--input", master, "--details", merged_details, "--top", "20"],
                               stdout=f, stderr=subprocess.STDOUT, timeout=120)
        except Exception as e:
            log.append(f"分析文本生成失败（不影响主流程）：{e}")

        state.update({
            "status": "ok",
            "finished_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "last_ok_at": now,
            "data_version": str(int(time.time())),
            "master": master, "trends_path": trends,
        })
        log.append(f"完成：{state['ok_jobs']} 条岗位 / {state['ok_jd']} 条 JD")
    except Exception as e:
        state.update({
            "status": "error", "error": str(e),
            "finished_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        log.append("FAILED: " + str(e))
    _write_state(state_path, state)
    return state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keyword", default="AI 工程师")
    ap.add_argument("--city", default="100010000")
    ap.add_argument("--pages", type=int, default=8)
    ap.add_argument("--cdp-port", type=int, default=9222)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--history-dir", default="")
    ap.add_argument("--dashboard", required=True)
    ap.add_argument("--state", default="")
    ap.add_argument("--gap-min", type=float, default=2.5)
    ap.add_argument("--gap-max", type=float, default=5)
    ap.add_argument("--interval-minutes", type=int, default=0, help=">0 时循环定时刷新")
    args = ap.parse_args()

    hist = args.history_dir or os.path.join(os.path.expanduser("~"), ".boss-zhipin-scraper", "history")
    state_path = args.state or os.path.join(hist, "refresh_state.json")
    cfg = {
        "keyword": args.keyword, "city": args.city, "pages": args.pages,
        "cdp_port": args.cdp_port, "output_dir": args.output_dir,
        "history_dir": hist, "dashboard": args.dashboard,
        "gap_min": args.gap_min, "gap_max": args.gap_max,
    }
    while True:
        st = refresh(cfg, state_path)
        print(st["status"], st.get("error", ""), st.get("ok_jobs", 0))
        if args.interval_minutes <= 0:
            break
        time.sleep(args.interval_minutes * 60)


if __name__ == "__main__":
    main()
