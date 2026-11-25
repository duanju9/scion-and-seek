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
import tempfile
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


def jd_coverage(jobs):
    """JD 覆盖率；没有岗位时按 1.0 处理，避免除零。"""
    if not jobs:
        return 1.0
    return sum(1 for j in jobs if j.get("has_jd") == "yes") / float(len(jobs))


def backfill_missing_jd(master_path, csv_path, run_keyword, cfg, tmp_dir, log,
                        min_coverage=0.9, round_timeout=600):
    """JD 覆盖率不足时补一轮，只抓缺 JD 的岗位，抓完重新合并主表。

    第 3 步补抓有总超时，新增岗位一多就会被截断，缺口会被固化进主表和快照。
    这里在合并之后做一次有上限的兜底：单轮只补缺失部分，仍补不齐就把覆盖率
    写回 state 交给巡检读取，不再静默留缺口。

    返回 (jobs, 本轮尝试补抓条数, 最终覆盖率)。
    """
    data = _load(master_path, {}) or {}
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    if not jobs:
        return jobs, 0, 1.0
    cov = jd_coverage(jobs)
    missing = [j for j in jobs if j.get("has_jd") != "yes"]
    if not missing or cov >= min_coverage:
        return jobs, 0, cov

    log.append(f"[4.5/7] JD 覆盖率 {cov:.1%} < {min_coverage:.0%}，"
               f"对 {len(missing)} 条缺失岗位补抓一轮")
    jobs_dump = os.path.join(tmp_dir, "backfill_jobs.json")
    seeds = os.path.join(tmp_dir, "backfill_seeds.json")
    merged = os.path.join(tmp_dir, "backfill_details.json")
    stamp = data.get("scraped_at", "") if isinstance(data, dict) else ""
    with open(jobs_dump, "w", encoding="utf-8") as f:
        json.dump({"scraped_at": stamp, "keyword": run_keyword, "jobs": jobs},
                  f, ensure_ascii=False)
    with open(seeds, "w", encoding="utf-8") as f:
        json.dump([{"job_id": j.get("job_id", ""), "jd": j.get("jd", "")}
                   for j in jobs if j.get("jd")], f, ensure_ascii=False)

    ok, _ = _run([PY, os.path.join(HERE, "fetch_missing_details.py"),
                  "--jobs", jobs_dump, "--details", seeds, "--output", merged,
                  "--cdp-port", str(cfg["cdp_port"]),
                  "--gap-min", str(cfg.get("gap_min", 2.5)),
                  "--gap-max", str(cfg.get("gap_max", 5)),
                  "--limit", str(len(missing))], round_timeout, log)
    if not ok or not os.path.isfile(merged):
        log.append("JD 兜底补抓未成功，缺口保留并登记到 state")
        return jobs, 0, cov

    ok, _ = _run([PY, os.path.join(HERE, "merge_to_master.py"),
                  "--jobs", jobs_dump, "--details", merged,
                  "--out-json", master_path, "--out-csv", csv_path,
                  "--source-keyword", run_keyword], 180, log)
    if not ok:
        log.append("JD 兜底合并失败，缺口保留并登记到 state")
        return jobs, 0, cov

    fresh = _load(master_path, {}) or {}
    jobs = fresh.get("jobs", []) if isinstance(fresh, dict) else fresh
    cov2 = jd_coverage(jobs)
    left = sum(1 for j in jobs if j.get("has_jd") != "yes")
    log.append(f"兜底补抓后 JD 覆盖率 {cov2:.1%}（仍缺 {left} 条）")
    return jobs, len(missing), cov2


def _pid_alive(pid):
    """判断进程是否还活着（跨平台，不依赖 psutil）。"""
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        # Windows 上 os.kill(pid, 0) 会直接终止进程，必须走 OpenProcess
        import ctypes
        SYNCHRONIZE = 0x00100000
        h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not h:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(h, 0) == 0x00000102  # WAIT_TIMEOUT=活着
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        return False


def _kill_tree(p):
    """结束子进程及其子进程；失败也不抛出，避免卡在清理上。"""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                           capture_output=True, timeout=20)
        else:
            os.killpg(os.getpgid(p.pid), 9)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def _run(cmd, timeout, log):
    """执行子脚本，返回 (ok, 输出摘要)。

    输出重定向到临时文件而不是管道：管道模式下只要孙进程继承了写端句柄，
    子进程退出后父进程仍等不到 EOF，communicate() 会无限阻塞（timeout 也进不去），
    整条刷新线程就此挂死（#20）。文件句柄不会出现这种继承死等。
    """
    log.append("$ " + " ".join(cmd))
    tmp = os.path.join(tempfile.gettempdir(),
                       "patrol_run_%d_%d.log" % (os.getpid(), int(time.time() * 1000) % 1000000))
    try:
        with open(tmp, "wb") as fh:
            p = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL,
                                 start_new_session=(os.name != "nt"))
            try:
                rc = p.wait(timeout=timeout if timeout else None)
            except subprocess.TimeoutExpired:
                _kill_tree(p)
                try:
                    p.wait(timeout=10)
                except Exception:
                    pass
                log.append(f"TIMEOUT after {timeout}s")
                return False, f"超时 {timeout}s"
        try:
            with open(tmp, "r", encoding="utf-8", errors="replace") as fh:
                out = fh.read()
        except Exception:
            out = ""
        log.append(out[-1200:].strip())
        return rc == 0, out[-400:]
    except Exception as e:
        log.append(f"ERROR {e}")
        return False, str(e)
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


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


def recover_stale_state(state_path, max_minutes=45):
    """把「持有者已消失」的 running 状态回收成 error。

    刷新线程随服务进程一起死掉时，state 会永久停在 running，
    而调度只认 status 字段，于是整条刷新链路被一个死线程的残影锁死（#20）。
    这里按 pid 存活 + 起始时间双重校验，只要有一项证明它不可能还在跑就回收。
    返回 True 表示做过回收。
    """
    state = _load(state_path, {}) or {}
    if state.get("status") != "running":
        return False
    started = str(state.get("started_at") or "")
    too_old = False
    try:
        t0 = datetime.datetime.strptime(started[:19], "%Y-%m-%d %H:%M:%S")
        too_old = (datetime.datetime.now() - t0).total_seconds() > max_minutes * 60
    except Exception:
        too_old = True
    pid = state.get("pid")
    pid_dead = (pid is not None) and (not _pid_alive(pid))
    # 旧版本 state 没有 pid 字段，用刷新锁的持有者兜底判断：锁在但进程没了，说明写它的人已消失
    lock_pid = None
    try:
        with open(os.path.join(os.path.dirname(state_path), "refresh.lock"), encoding="utf-8") as f:
            lock_pid = json.load(f).get("pid")
    except Exception:
        lock_pid = None
    lock_dead = (lock_pid is not None) and (not _pid_alive(lock_pid))
    if not (too_old or pid_dead or lock_dead):
        return False
    why = "进程已退出" if (pid_dead or lock_dead) else f"运行超过 {max_minutes} 分钟"
    state.update({
        "status": "error",
        "error": f"上一轮刷新异常中断（{why}），已回收，下一轮重试",
        "finished_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    steps = state.get("steps") or []
    steps.append(f"[recover] 上一轮刷新遗留 running 状态（{why}），已回收为 error 以便重试")
    state["steps"] = steps
    _write_state(state_path, state)
    return True


def _lock_age(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        started = datetime.datetime.strptime(str(data.get("at", ""))[:19], "%Y-%m-%d %H:%M:%S")
        return (datetime.datetime.now() - started).total_seconds()
    except Exception:
        try:
            return time.time() - os.path.getmtime(path)
        except Exception:
            return 0


def acquire_lock(hist_dir, stale_seconds=45 * 60):
    """跨进程刷新锁。

    多个工作台/巡检进程共用一个 Chrome CDP 时，会同时抓列表、互相串页、
    把对方的标签页挤掉（实测出现 13 个残留页）。这里用文件锁把刷新串行化，
    持锁进程的 pid 写进锁文件，超过 stale_seconds 视为死锁可抢占。
    """
    os.makedirs(hist_dir, exist_ok=True)
    path = os.path.join(hist_dir, "refresh.lock")
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(),
                           "at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                          f, ensure_ascii=False)
            return path
        except FileExistsError:
            age = _lock_age(path)
            holder = None
            try:
                with open(path, encoding="utf-8") as f:
                    holder = json.load(f).get("pid")
            except Exception:
                holder = None
            # 持锁进程已死就立刻可抢占：等满 45 分钟会让死锁把整夜巡检吞掉（#20）
            if holder is not None and not _pid_alive(holder):
                try:
                    os.replace(path, path + ".stale")
                except Exception:
                    pass
                continue
            if age > stale_seconds:
                try:
                    os.replace(path, path + ".stale")
                except Exception:
                    pass
                continue
            return None
    return None


def release_lock(path):
    if not path:
        return
    try:
        os.remove(path)
    except Exception:
        pass


def load_keywords(cfg):
    """关键词来源优先级：命令行 --keywords > output/keywords.json > --keyword。

    keywords.json 让服务被 patrol_once 重启后仍保持多关键词轮换，
    不用每次手工补参数。
    """
    kws = [k.strip() for k in (cfg.get("keywords") or []) if str(k).strip()]
    if kws:
        return kws
    path = os.path.join(cfg.get("output_dir") or "", "keywords.json")
    if cfg.get("output_dir") and os.path.isfile(path):
        data = _load(path, None)
        if isinstance(data, dict):
            data = data.get("keywords")
        if isinstance(data, list):
            kws = [str(k).strip() for k in data if str(k).strip()]
            if kws:
                return kws
    return [cfg.get("keyword") or "AI 工程师"]


def keyword_plan(cfg, state):
    """多关键词轮换：返回 (本轮关键词, 全部关键词, 本轮序号)。"""
    kws = load_keywords(cfg)
    try:
        idx = int(state.get("keyword_index") or 0) % len(kws)
    except Exception:
        idx = 0
    return kws[idx], kws, idx


def refresh(cfg, state_path, logged_check=None):
    """执行一次完整刷新，返回最终 state。

    logged_check 可选：由调用方（服务端）复用已有 CDP 会话判断登录态，
    避免在刷新线程里重复建立 websocket 连接造成阻塞。
    """
    state = _load(state_path, {}) or {}
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    run_keyword, all_keywords, kw_idx = keyword_plan(cfg, state)
    state.update({
        "status": "running", "started_at": now, "finished_at": "",
        "error": "", "steps": [], "ok_jobs": 0, "ok_jd": 0,
        "keyword": run_keyword, "keywords": all_keywords,
        "keyword_index": kw_idx, "new_jobs": 0,
        "pid": os.getpid(),
    })
    log = _Logger(state, state_path)
    _write_state(state_path, state)

    out_dir = cfg["output_dir"]
    hist_dir = cfg["history_dir"]
    lock_path = acquire_lock(hist_dir)
    if not lock_path:
        state.update({"status": "skipped", "ok_jobs": state.get("ok_jobs", 0),
                      "finished_at": now,
                      "msg": "另一进程正在刷新（refresh.lock 被占用），本轮跳过"})
        log.append("已有刷新在跑（refresh.lock），本轮跳过，不重复占用浏览器")
        _write_state(state_path, state)
        return state
    try:
        return _refresh_locked(cfg, state_path, state, log, now, run_keyword,
                               all_keywords, kw_idx, logged_check)
    finally:
        release_lock(lock_path)


def _refresh_locked(cfg, state_path, state, log, now, run_keyword, all_keywords, kw_idx,
                    logged_check=None):
    """持锁执行真正的刷新流程。"""
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
        # boss_cdp_raw 的 flush_jobs 是追加写，跨轮复用同一个文件会把前几轮的存量也带进
        # 「当轮列表」：merge 见到所有 job_id 都命中本轮分支，last_seen 被整表刷成 now、
        # carried 恒为 0、keep_days 过期永不触发（#14）。每轮开跑前清掉，只留本轮结果。
        try:
            if os.path.isfile(tmp_jobs):
                os.remove(tmp_jobs)
        except OSError as e:
            log.append(f"清理上轮列表失败（忽略）：{e}")
        log.append(f"[1/7] 采集列表：关键词={run_keyword}"
                   f"（第 {kw_idx + 1}/{len(all_keywords)} 个）城市={cfg['city']} 页数={cfg['pages']}")
        ok, _ = _run([PY, os.path.join(HERE, "boss_cdp_raw.py"),
                      "--keyword", run_keyword, "--city", cfg["city"],
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

        # 2.5 补抓清单 = 本轮列表 + 主表里仍缺 JD 的存量。
        # 列表文件改成每轮干净后，存量岗位不会再混进「当轮列表」，积压 JD 要单独拼进来才不会饿死（#14）。
        detail_jobs = tmp_jobs
        round_ids = {str(j.get("job_id") or "") for j in jobs}
        backlog = [j for j in old_jobs
                   if str(j.get("job_id") or "") not in round_ids
                   and j.get("has_jd") != "yes" and j.get("job_link")]
        if backlog:
            # 积压与本轮交替排：只把本轮排前面，上限模式下存量 JD 会被永远挤在队列尾部饿死
            mixed, b, f = [], list(backlog), list(jobs)
            while b or f:
                if b:
                    mixed.append(b.pop(0))
                if f:
                    mixed.append(f.pop(0))
            backlog_jobs = os.path.join(tmp_dir, "backlog_jobs.json")
            with open(backlog_jobs, "w", encoding="utf-8") as fh:
                json.dump({"jobs": mixed}, fh, ensure_ascii=False, indent=2)
            detail_jobs = backlog_jobs
            log.append(f"[2.5/7] 补抓清单：本轮 {len(jobs)} 条 + 存量缺 JD {len(backlog)} 条（交替排序）")

        # 3. 增量补 JD（单轮限量：宽关键词一次能冒出 100+ 新岗，不封顶会把 30 分钟窗口吃满）
        detail_cap = int(cfg.get("detail_limit", 80))
        log.append(f"[3/7] 增量补抓 JD（间隔 {cfg['gap_min']}-{cfg['gap_max']}s，"
                   f"本轮上限 {detail_cap or '不限'} 条）")
        cmd_detail = [PY, os.path.join(HERE, "fetch_missing_details.py"),
                      "--jobs", detail_jobs, "--details", seed_details,
                      "--output", merged_details, "--cdp-port", str(cfg["cdp_port"]),
                      "--gap-min", str(cfg["gap_min"]), "--gap-max", str(cfg["gap_max"])]
        if detail_cap > 0:
            cmd_detail += ["--limit", str(detail_cap)]
        ok, _ = _run(cmd_detail, cfg.get("detail_timeout", 1200), log)
        if not ok or not os.path.isfile(merged_details):
            log.append("补抓异常，降级为仅用种子 JD 合并")
            if not os.path.isfile(merged_details):
                with open(merged_details, "w", encoding="utf-8") as f:
                    json.dump([], f)

        # 4. 合并主表（当轮 + 上轮余量，按 job_id 去重）
        log.append("[4/7] 合并主表（并集，保留其它关键词的存量岗位）")
        ok, _ = _run([PY, os.path.join(HERE, "merge_to_master.py"),
                      "--jobs", tmp_jobs, "--details", merged_details,
                      "--out-json", master, "--out-csv", csv_out,
                      "--source-keyword", run_keyword,
                      "--carry-from", prev_master,
                      "--keep-days", str(cfg.get("keep_days", 7))], 120, log)
        if not ok:
            raise RuntimeError("合并主表失败")
        if os.path.isfile(prev_master):
            n = backfill_from_previous(master, prev_master, csv_out)
            if n:
                log.append(f"从上期回填 {n} 个缺失字段（主要是 Boss 活跃状态）")
        fresh = _load(master, {}) or {}
        fresh_jobs = fresh.get("jobs", []) if isinstance(fresh, dict) else fresh
        if detail_cap > 0:
            # 上限模式下覆盖率必然暂时低于阈值，兜底再跑一轮会把单轮时间又拉长到窗口之外；
            # 积压交给后续轮次消化，这里只记录。
            backfilled, jd_cov = 0, jd_coverage(fresh_jobs)
            backlog = sum(1 for j in fresh_jobs if j.get("has_jd") != "yes")
            if backlog:
                log.append(f"[4.5/7] 单轮上限模式：跳过兜底补抓，"
                           f"{backlog} 条 JD 积压留待后续轮次")
        else:
            fresh_jobs, backfilled, jd_cov = backfill_missing_jd(
                master, csv_out, run_keyword, cfg, tmp_dir, log,
                min_coverage=cfg.get("min_jd_coverage", 0.9),
                round_timeout=cfg.get("backfill_timeout", 600))
        state["backfilled"] = backfilled
        state["jd_coverage"] = round(jd_cov, 4)
        state["jd_missing"] = sum(1 for j in fresh_jobs if j.get("has_jd") != "yes")
        state["ok_jobs"] = len(fresh_jobs)
        state["ok_jd"] = sum(1 for j in fresh_jobs if j.get("has_jd") == "yes")
        prev_ids = {str(j.get("job_id")) for j in old_jobs if j.get("job_id")}
        fresh_ids = {str(j.get("job_id")) for j in fresh_jobs if j.get("job_id")}
        state["new_jobs"] = len(fresh_ids - prev_ids)
        state["carried"] = sum(1 for j in fresh_jobs if j.get("carried") == "yes")
        pool = {}
        for j in fresh_jobs:
            k = str(j.get("source_keyword") or "未标注")
            pool[k] = pool.get(k, 0) + 1
        state["pool_by_keyword"] = pool
        log.append(f"并集 {state['ok_jobs']} 条（本轮新增 {state['new_jobs']}，"
                   f"存量 {state['carried']}，兜底补抓 {state['backfilled']}）；"
                   f"JD {state['ok_jd']} 条 / 覆盖 {state['jd_coverage']:.1%}"
                   f"（积压 {state['jd_missing']} 条留待后续轮次）；关键词 {run_keyword}")
        _write_state(state_path, state)

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
            "keyword_index": (kw_idx + 1) % len(all_keywords),
            "next_keyword": all_keywords[(kw_idx + 1) % len(all_keywords)],
        })
        log.append(f"完成：{state['ok_jobs']} 条岗位 / {state['ok_jd']} 条 JD"
                   f"（本轮新增 {state['new_jobs']}）；"
                   f"下一轮关键词 {state['next_keyword']}")
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
    ap.add_argument("--keywords", default="",
                    help="多关键词按轮次轮换，用 | 或 , 分隔；给了就覆盖 --keyword")
    ap.add_argument("--city", default="100010000")
    ap.add_argument("--pages", type=int, default=8)
    ap.add_argument("--detail-limit", type=int, default=80,
                    help="单轮最多补抓多少条 JD，0=不限；宽关键词靠它把单轮控制在刷新间隔内")
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
    kw_list = [k.strip() for k in (args.keywords or "").replace("|", ",").split(",") if k.strip()]
    cfg = {
        "keyword": args.keyword, "keywords": kw_list,
        "city": args.city, "pages": args.pages,
        "cdp_port": args.cdp_port, "output_dir": args.output_dir,
        "history_dir": hist, "dashboard": args.dashboard,
        "gap_min": args.gap_min, "gap_max": args.gap_max,
        "detail_limit": args.detail_limit,
    }
    while True:
        st = refresh(cfg, state_path)
        print(st["status"], st.get("error", ""), st.get("ok_jobs", 0))
        if args.interval_minutes <= 0:
            break
        time.sleep(args.interval_minutes * 60)


if __name__ == "__main__":
    main()
