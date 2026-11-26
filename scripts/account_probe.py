#!/usr/bin/env python3
"""账号指纹与换号审计。

为什么需要：定时任务固定用一个专用 Chrome 跑。一旦换号（换登录手机号、换人、换微信绑定），
cookie 全变，但 jobs.db 和主表里没有任何字段能说清「这批岗位是哪个号看到的」。
等发现数据不对再去补，就只能靠猜——这就是换号最大的隐形成本。

这里的做法刻意做到「可解释、可回滚、不猜」：
- 指纹：只对登录相关 cookie 的 name=value 做 SHA-256，取前 10 位；**原文不落盘**。
- 状态：output/account_state.json（key / label / since / labels / seen / switches）。
- 换号：指纹变了就算换号，自动分配下一个标签（账号A/账号B/…），追加一条审计事件；
  切回老号能按 labels 映射找回原标签，不会变成「账号C」。
- 退化：CDP 没开、取不到 cookie 时返回 known=False，**不写状态、不误判换号**——
  宁可这一轮不知道，也不要制造一条假的换号记录。

单独跑：
    python account_probe.py --output-dir <输出目录> --cdp-port 9222
"""
import argparse
import datetime
import hashlib
import json
import os
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_NAME = "account_state.json"
# 只挑能代表「谁登录着」的 cookie；站点改字段时改这里即可
COOKIE_IDS = ("bst", "wt2", "zp_at", "__zp_stoken__", "geek_zp_token", "lastCity")
MAX_SWITCHES = 50


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _load_boss():
    import importlib.util
    spec = importlib.util.spec_from_file_location("boss_cdp_raw", os.path.join(HERE, "boss_cdp_raw.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read_cookies(port):
    """从任意 zhipin 页面会话读 cookie，返回 {name: value}。"""
    boss = _load_boss()
    cdp = boss.CDPSession(port)
    try:
        targets = cdp.send("Target.getTargets", timeout=8)["result"]["targetInfos"]
        page = next((t for t in targets
                     if t.get("type") == "page" and "zhipin.com" in t.get("url", "")), None)
        sid = None
        if page:
            sid = cdp.send("Target.attachToTarget",
                           {"targetId": page["targetId"], "flatten": True},
                           timeout=8)["result"]["sessionId"]
        resp = cdp.send("Network.getCookies", {"urls": ["https://www.zhipin.com/"]}, sid, timeout=8)
        cookies = (resp.get("result") or {}).get("cookies") or []
        return {c["name"]: c.get("value", "") for c in cookies if c.get("name")}
    finally:
        try:
            cdp.ws.close()
        except Exception:
            pass


def fingerprint(cookies):
    """登录 cookie 的哈希指纹；一个都没有时返回 None（表示不知道，不是换号）。"""
    picked = [f"{k}={cookies[k]}" for k in COOKIE_IDS if cookies.get(k)]
    if not picked:
        return None
    return hashlib.sha256("|".join(sorted(picked)).encode("utf-8")).hexdigest()[:10]


def state_path(output_dir):
    return os.path.join(output_dir, STATE_NAME)


def load_state(output_dir):
    try:
        with open(state_path(output_dir), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_state(output_dir, state):
    tmp = state_path(output_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, state_path(output_dir))


def _next_label(state):
    used = set((state.get("labels") or {}).values())
    for i in range(26 * 2):
        label = "账号" + (chr(65 + i) if i < 26 else chr(65 + i - 26) * 2)
        if label not in used:
            return label
    return "账号" + str(len(used) + 1)


def _read_with_timeout(reader, port, timeout=10):
    """CDP 卡住时 websocket 建连能等 60s，巡检会被一起拖住；这里给探测定个上限。"""
    box = {}

    def work():
        try:
            box["value"] = reader(port)
        except Exception as e:  # noqa: BLE001 - 交给调用方降级
            box["error"] = e

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"读取 cookie 超时（{timeout}s，CDP 无响应）")
    if "error" in box:
        raise box["error"]
    return box.get("value") or {}


def observe(output_dir, port, cookie_reader=None):
    """探测当前账号；指纹变化时记一条换号事件。任何异常都降级成 known=False。"""
    os.makedirs(output_dir, exist_ok=True)
    state = load_state(output_dir) or {}
    reader = cookie_reader or read_cookies
    try:
        cookies = _read_with_timeout(reader, port) if cookie_reader is None else reader(port)
    except Exception as e:
        return dict(state, known=False, switched=False,
                    probe_error=f"CDP 不可用：{e}"[:160])

    key = fingerprint(cookies)
    if not key:
        return dict(state, known=False, switched=False,
                    probe_error="未取到登录 cookie（可能未登录或页面未打开）")

    stamp = now()
    switched = False
    event = None
    if not state.get("key"):
        state = {"key": key, "label": "账号A", "since": stamp,
                 "labels": {key: "账号A"}, "seen": [key], "switches": []}
    elif state["key"] != key:
        label = (state.get("labels") or {}).get(key) or _next_label(state)
        event = {"at": stamp, "from_label": state.get("label"), "to_label": label,
                 "from_key": str(state.get("key"))[:6], "to_key": key[:6]}
        state.setdefault("switches", []).append(event)
        state["switches"] = state["switches"][-MAX_SWITCHES:]
        state["labels"] = {**(state.get("labels") or {}), key: label}
        state["seen"] = list(dict.fromkeys((state.get("seen") or []) + [key]))
        state["key"], state["label"], state["since"] = key, label, stamp
        switched = True

    state["last_seen"] = stamp
    state["cookie_names"] = sorted(k for k in cookies if k in COOKIE_IDS)
    save_state(output_dir, state)
    return dict(state, known=True, switched=switched, switch=event)


def summary(state):
    if not state or not state.get("known"):
        return "账号未知"
    return f"{state.get('label')}（指纹 {str(state.get('key'))[:6]}，自 {state.get('since')}）"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--cdp-port", type=int, default=9222)
    args = ap.parse_args()
    print(json.dumps(observe(args.output_dir, args.cdp_port), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
