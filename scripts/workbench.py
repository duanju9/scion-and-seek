#!/usr/bin/env python3
"""工作台一键启动 / 看护（BOSS 直聘求职工作台）。

为什么要有这个文件
------------------
之前启动一次要记四条命令：先开专用 Chrome（带 --remote-debugging-port），再起
dashboard_server，再确认端口，再手动打开页面。中间任何一步漏了，页面就显示
「CDP 未启动」，而刷新是静默不工作的——最糟的失败方式是「看起来在跑」。

现在收敛成一个入口：

    python scripts/workbench.py start      # 自检 → 按需拉起 Chrome / 服务 → 开页面
    python scripts/workbench.py status     # 一眼看清 CDP / 服务 / 刷新 / 账号
    python scripts/workbench.py stop       # 只停服务与看护，不动浏览器
    python scripts/workbench.py watchdog   # 常驻看护：服务挂了重拉、CDP 掉了重开
    python scripts/workbench.py autostart  # 开机自启（可选，写入 Startup）

硬性边界
--------
- 不发送招呼、不启用自动投递、不点「立即沟通」；
- 不绕过验证码/风控：登录失效只记录并提示扫码，不模拟登录；
- 不删除数据、不改原始简历；
- Chrome 只在「CDP 端口不通」时拉起，绝不杀掉正在采集的实例。
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
LOCAL_PORT = 8765
CDP_PORT = 9222
HTTP_TIMEOUT = 4

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
PROFILE_DIR = os.path.join(os.path.expanduser("~"), ".boss-zhipin-scraper", "chrome-profile")


# ---------------------------------------------------------------- 基础工具

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def http_json(url, timeout=HTTP_TIMEOUT, method="GET", body=None):
    req = urllib.request.Request(url, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(body).encode("utf-8")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def port_alive(port, path="/"):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2):
            return True
    except Exception:
        return False


def state_path(out):
    return os.path.join(out, ".workbench_state.json")


def load_state(out):
    try:
        with open(state_path(out), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(out, **kw):
    st = load_state(out)
    st.update(kw)
    tmp = state_path(out) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, state_path(out))
    return st


def log_dir(out):
    d = os.path.join(out, "logs")
    os.makedirs(d, exist_ok=True)
    return d


def log(out, msg, tag="workbench"):
    """写一条日志到控制台 + logs/<tag>-YYYYMMDD.log（超过 5MB 轮转）。"""
    line = f"[{now()}] {msg}"
    print(line, flush=True)
    try:
        path = os.path.join(log_dir(out), f"{tag}-{datetime.now():%Y%m%d}.log")
        if os.path.exists(path) and os.path.getsize(path) > 5 * 1024 * 1024:
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def pid_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True, timeout=8).stdout
            return str(pid) in out
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def kill_pid(pid):
    if not pid:
        return False
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=10)
        else:
            os.kill(int(pid), 15)
        return True
    except Exception:
        return False


def detached_flags():
    if os.name == "nt":
        # DETACHED_PROCESS：父进程退出后子进程继续跑，关掉黑窗口也不影响
        return subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    return 0


# ---------------------------------------------------------------- 各部件

def find_chrome():
    for p in CHROME_CANDIDATES:
        if p and os.path.isfile(p):
            return p
    return ""


def cdp_up():
    return port_alive(CDP_PORT, "/json/version")


def ensure_chrome(out, wait=45):
    """CDP 不通才拉起（用专用 profile，不动用户主 Chrome）。"""
    if cdp_up():
        return True, "CDP 已在运行"
    if os.environ.get("BOSS_SKIP_CHROME") == "1":
        return False, "按 BOSS_SKIP_CHROME=1 跳过浏览器启动（仅用于自检）"
    exe = find_chrome()
    if not exe:
        return False, "没找到 Chrome/Edge，请手动打开并带 --remote-debugging-port=9222"
    args = [
        exe,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-popup-blocking",
        "https://www.zhipin.com/web/geek/jobs?city=100010000",
    ]
    log(out, f"CDP 未启动，拉起浏览器：{os.path.basename(exe)} · profile={PROFILE_DIR}")
    try:
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=detached_flags(), close_fds=True)
    except Exception as e:
        return False, f"拉起浏览器失败：{e}"

    deadline = time.time() + wait
    while time.time() < deadline:
        if cdp_up():
            log(out, "CDP 已就绪")
            return True, "浏览器已拉起"
        time.sleep(1.5)
    return False, f"浏览器已启动但 {wait}s 内 CDP 未就绪（可能被安全软件拦截）"


def server_cmd(out, dashboard, interval):
    return [
        sys.executable, os.path.join(HERE, "dashboard_server.py"),
        "--port", str(LOCAL_PORT),
        "--cdp-port", str(CDP_PORT),
        "--dashboard", dashboard,
        "--output-dir", out,
        "--interval-minutes", str(interval),
        "--no-browser",
    ]


def server_up():
    try:
        http_json(f"http://127.0.0.1:{LOCAL_PORT}/api/status", timeout=8)
        return True
    except Exception:
        return False


def ensure_server(out, dashboard, interval=30, wait=25):
    if server_up():
        return True, "服务已在运行"
    if not os.path.isfile(dashboard):
        return False, f"看板文件不存在：{dashboard}（先跑一次 generate_dashboard.py）"
    cmd = server_cmd(out, dashboard, interval)
    logf = os.path.join(log_dir(out), f"server-{datetime.now():%Y%m%d}.log")
    log(out, "服务未运行，启动：" + " ".join(cmd[1:3]))
    fh = open(logf, "a", encoding="utf-8")
    try:
        p = subprocess.Popen(cmd, cwd=SKILL_ROOT, stdout=fh, stderr=subprocess.STDOUT,
                             creationflags=detached_flags(), close_fds=True)
    except Exception as e:
        fh.close()
        return False, f"启动服务失败：{e}"
    save_state(out, server_pid=p.pid, dashboard=dashboard, started_at=now())
    deadline = time.time() + wait
    while time.time() < deadline:
        if server_up():
            log(out, f"服务就绪（pid={p.pid}）")
            return True, "服务已启动"
        if p.poll() is not None:
            fh.close()
            return False, f"服务启动后立即退出，见 {logf}"
        time.sleep(1.0)
    return False, f"服务 {wait}s 内未就绪，见 {logf}"


def refresh_state():
    try:
        return http_json(f"http://127.0.0.1:{LOCAL_PORT}/api/refresh/status", timeout=8)
    except Exception:
        return {}


def trigger_refresh(out):
    try:
        r = http_json(f"http://127.0.0.1:{LOCAL_PORT}/api/refresh", timeout=10,
                      method="POST", body={})
        log(out, f"手动触发刷新：{r.get('msg') or r}")
        return True
    except Exception as e:
        log(out, f"触发刷新失败：{e}")
        return False


def default_dashboard(out):
    return os.path.join(out, "岗位数据看板.html")


# ---------------------------------------------------------------- 命令

def cmd_start(args):
    out = os.path.abspath(args.output_dir)
    os.makedirs(out, exist_ok=True)
    dash = os.path.abspath(args.dashboard or default_dashboard(out))

    log(out, "=" * 58)
    log(out, f"工作台启动 · 数据目录 {out}")

    ok, msg = ensure_chrome(out)
    log(out, f"浏览器：{msg}")

    ok2, msg2 = ensure_server(out, dash, args.interval)
    log(out, f"服务：{msg2}")

    if ok2:
        try:
            st = http_json(f"http://127.0.0.1:{LOCAL_PORT}/api/status", timeout=8)
            log(out, f"健康：cdp={st.get('cdp')} login={st.get('logged_in')}")
        except Exception:
            pass

    if args.watchdog:
        started = start_watchdog(out, dash, args.interval)
        log(out, f"看护进程：{'已启动' if started else '已在运行'}")

    url = f"http://127.0.0.1:{LOCAL_PORT}/"
    if ok2 and not args.no_open:
        webbrowser.open(url)
        log(out, f"已打开 {url}")
    elif not ok2:
        log(out, "服务未就绪，暂不打开页面；请查看上面的错误")

    log(out, "说明：刷新由服务按间隔自动执行；看护进程负责服务/浏览器掉线后的恢复")
    log(out, "边界：不发招呼、不自动投递、不绕过验证码" + ("；登录失效请手动扫码" if not ok else ""))
    return 0 if ok2 else 1


def cmd_status(args):
    out = os.path.abspath(args.output_dir)
    print(f"数据目录  {out}")
    print(f"服务      {'在线' if server_up() else '离线'}  http://127.0.0.1:{LOCAL_PORT}/")
    print(f"浏览器    {'CDP 就绪' if cdp_up() else 'CDP 未启动'}  :{CDP_PORT}")
    st = {}
    try:
        st = http_json(f"http://127.0.0.1:{LOCAL_PORT}/api/status", timeout=8)
        print(f"登录      {'已登录' if st.get('logged_in') else '未登录'}"
              f"{'（探测超时，状态未知）' if st.get('cdp') is None else ''}")
    except Exception:
        pass
    r = refresh_state()
    if r:
        last = r.get("finished_at") or r.get("started_at") or "-"
        flag = "（已到期，等登录/CDP 恢复）" if r.get("overdue") else ""
        print(f"刷新      {r.get('status', '-')}  上次 {last}  下次 {r.get('next_at', '-')}{flag}")
        if r.get("error"):
            print(f"          原因 {str(r['error'])[:100]}")
        if r.get("ok_jobs"):
            print(f"岗位      {r.get('ok_jobs')}  条 · JD {r.get('ok_jd')} · "
                  f"覆盖 {r.get('jd_coverage')}")
    wd = load_state(out).get("watchdog_pid")
    print(f"看护      {'运行中 pid=' + str(wd) if pid_alive(wd) else '未运行'}")
    return 0


def cmd_stop(args):
    out = os.path.abspath(args.output_dir)
    st = load_state(out)
    for key in ("watchdog_pid", "server_pid"):
        pid = st.get(key)
        if pid_alive(pid):
            kill_pid(pid)
            log(out, f"已停止 {key}={pid}")
    save_state(out, server_pid=None, watchdog_pid=None)
    if args.with_chrome and cdp_up():
        # 只有显式要求才动浏览器；专用 profile 与用户主 Chrome 不是同一个实例
        log(out, "按参数要求关闭专用浏览器实例")
        subprocess.run(["taskkill", "/IM", "chrome.exe", "/F"], capture_output=True)
    return 0


def start_watchdog(out, dash, interval):
    st = load_state(out)
    if pid_alive(st.get("watchdog_pid")):
        return False
    logf = os.path.join(log_dir(out), f"watchdog-{datetime.now():%Y%m%d}.log")
    fh = open(logf, "a", encoding="utf-8")
    p = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "watchdog",
         "--output-dir", out, "--dashboard", dash, "--interval", str(interval)],
        cwd=SKILL_ROOT, stdout=fh, stderr=subprocess.STDOUT,
        creationflags=detached_flags(), close_fds=True)
    save_state(out, watchdog_pid=p.pid)
    return True


def cmd_watchdog(args):
    out = os.path.abspath(args.output_dir)
    os.makedirs(out, exist_ok=True)
    dash = os.path.abspath(args.dashboard or default_dashboard(out))
    log(out, f"看护启动 pid={os.getpid()} 间隔={args.every}s", tag="watchdog")

    chrome_attempts = []
    last_refresh_nudge = 0.0
    while True:
        try:
            # 1) 服务
            if not server_up():
                log(out, "看护：服务无响应，重新拉起", tag="watchdog")
                ok, msg = ensure_server(out, dash, args.interval)
                log(out, f"看护：{msg}", tag="watchdog")

            # 2) 浏览器（只拉起，不杀；刷新进行中更不去碰）
            if not cdp_up():
                cutoff = time.time() - 3600
                chrome_attempts = [t for t in chrome_attempts if t > cutoff]
                if len(chrome_attempts) < 3:
                    chrome_attempts.append(time.time())
                    ok, msg = ensure_chrome(out, wait=40)
                    log(out, f"看护：{msg}", tag="watchdog")
                else:
                    log(out, "看护：一小时内已尝试 3 次，暂停拉起浏览器", tag="watchdog")

            # 3) 刷新兜底：服务在跑但过了两个周期还没刷过，催一次
            r = refresh_state()
            if (r and r.get("status") not in ("running",) and args.interval > 0
                    and cdp_up()):
                last = r.get("finished_at") or r.get("started_at")
                if last:
                    try:
                        age = (datetime.now()
                               - datetime.strptime(last, "%Y-%m-%d %H:%M:%S")).total_seconds()
                        if age > args.interval * 60 * 2 and time.time() - last_refresh_nudge > 900:
                            log(out, f"看护：上次刷新已 {int(age/60)} 分钟，催一次", tag="watchdog")
                            trigger_refresh(out)
                            last_refresh_nudge = time.time()
                    except Exception:
                        pass

            save_state(out, watchdog_beat=now())
            if args.once:
                return 0
            time.sleep(max(30, args.every))
        except KeyboardInterrupt:
            log(out, "看护退出（Ctrl+C）", tag="watchdog")
            return 0
        except Exception as e:  # 看护自己不能死
            log(out, f"看护异常：{e}", tag="watchdog")
            time.sleep(30)


AUTOSTART_NAME = "BOSS工作台看护.vbs"


def _startup_dir():
    return os.path.join(os.environ.get("APPDATA", ""),
                        "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def cmd_autostart(args):
    if os.name != "nt":
        print("仅 Windows 支持开机自启")
        return 1
    out = os.path.abspath(args.output_dir)
    dash = os.path.abspath(args.dashboard or default_dashboard(out))
    startup = _startup_dir()
    os.makedirs(startup, exist_ok=True)
    target = os.path.join(startup, AUTOSTART_NAME)
    if args.off:
        if os.path.exists(target):
            os.remove(target)
            print(f"已移除开机自启：{target}")
        else:
            print("开机自启未配置")
        return 0
    cmd = (f'"{sys.executable}" "{os.path.join(HERE, "workbench.py")}" start '
           f'--output-dir "{out}" --dashboard "{dash}" --interval {args.interval} --watchdog --no-open')
    vbs = ('Set s = CreateObject("WScript.Shell")\r\n'
           f's.Run "{cmd.replace(chr(34), chr(34) * 3)}", 0, False\r\n')
    with open(target, "w", encoding="utf-8") as f:
        f.write(vbs)
    print(f"已配置开机自启（静默启动看护）：{target}")
    print("取消：python scripts/workbench.py autostart --off")
    return 0


def main():
    ap = argparse.ArgumentParser(description="BOSS 工作台一键启动/看护")
    ap.add_argument("command", choices=["start", "stop", "status", "watchdog", "autostart"])
    ap.add_argument("--output-dir", default=os.environ.get("BOSS_OUTPUT_DIR", "output"))
    ap.add_argument("--dashboard", default="")
    ap.add_argument("--interval", type=int, default=30, help="自动刷新间隔（分钟）")
    ap.add_argument("--watchdog", action="store_true", help="start 时同时启动看护")
    ap.add_argument("--no-open", action="store_true", help="不自动打开页面")
    ap.add_argument("--with-chrome", action="store_true", help="stop 时一并关闭专用浏览器")
    ap.add_argument("--every", type=int, default=60, help="看护轮询间隔（秒）")
    ap.add_argument("--once", action="store_true", help="看护只跑一轮（测试用）")
    ap.add_argument("--off", action="store_true", help="autostart --off 取消自启")
    args = ap.parse_args()
    args.output_dir = os.path.abspath(args.output_dir)
    return {
        "start": cmd_start, "stop": cmd_stop, "status": cmd_status,
        "watchdog": cmd_watchdog, "autostart": cmd_autostart,
    }[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
