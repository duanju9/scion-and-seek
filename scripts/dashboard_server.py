#!/usr/bin/env python3
"""岗位工作台本地服务：把静态看板、登录引导、对话记录、打招呼联动到一个页面。

架构：
  浏览器 CDP（已登录的专用 Chrome） <--本服务--> 看板前端（fetch API）
  - GET  /api/status          登录与 CDP 状态
  - POST /api/login           打开/聚焦 BOSS 登录页并引导用户
  - GET  /api/conversations   会话列表
  - GET  /api/messages?who=   指定会话的消息记录
  - POST /api/greet           发送招呼语（含收件人校验）
  - GET  /api/jobs            最新岗位数据
  - GET  /api/trends          行业岗位趋势
  - GET  /api/refresh/status  刷新状态与下次刷新时间
  - POST /api/refresh         手动触发一次刷新（后台线程）
  - GET  /api/auto-apply/status  自动投递状态与去重记录
  - POST /api/auto-apply         手动触发一次匹配投递
  - POST /api/auto-apply/config  启用/关闭持续自动投递
  - GET  /api/resume/status      当前简历解析结果与 Top 匹配
  - POST /api/resume/upload      上传简历（base64）并本地解析/匹配
  - POST /api/resume/rematch     用当前岗位主表重新匹配
  - GET  /                    看板 HTML

启动：python dashboard_server.py --port 8765 --cdp-port 9222 --dashboard <html路径>
      [--interval-minutes 30] [--keyword "AI 工程师"] [--city 100010000] [--pages 8]
      [--resume <简历.json>] [--apply-limit 3] [--send-resume]
"""
import argparse
import base64
import datetime
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote, quote
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)  # 让 export_jobs / jobquery 这类同目录模块可被普通 import
import jobquery  # noqa: E402
spec = importlib.util.spec_from_file_location("boss_cdp_raw", os.path.join(HERE, "boss_cdp_raw.py"))
boss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boss)
spec2 = importlib.util.spec_from_file_location("refresh_data", os.path.join(HERE, "refresh_data.py"))
refresh_data = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(refresh_data)
spec3 = importlib.util.spec_from_file_location("auto_apply", os.path.join(HERE, "auto_apply.py"))
auto_apply = importlib.util.module_from_spec(spec3)
spec3.loader.exec_module(auto_apply)
spec4 = importlib.util.spec_from_file_location("export_jobs", os.path.join(HERE, "export_jobs.py"))
export_jobs = importlib.util.module_from_spec(spec4)
spec4.loader.exec_module(export_jobs)
spec5 = importlib.util.spec_from_file_location("account_probe", os.path.join(HERE, "account_probe.py"))
account_probe = importlib.util.module_from_spec(spec5)
spec5.loader.exec_module(account_probe)

# 所有 CDP 操作共用同一把锁：刷新、投递、手动打招呼互不抢页面
CDP_GATE = threading.Lock()

LOGIN_URL = "https://www.zhipin.com/web/geek/jobs?city=100010000"
JOBS_URL = "https://www.zhipin.com/web/geek/jobs?city=100010000"
GREET_JS = r"""(() => {
    const el=document.querySelector('.chat-input');
    if(!el) return {ok:false, reason:'no input'};
    el.focus(); el.textContent=%s;
    el.dispatchEvent(new InputEvent('input',{bubbles:true,data:%s,inputType:'insertText'}));
    return {ok:true};
})()"""
SEND_JS = r"""(() => {
    const b=document.querySelector('button.btn-send');
    if(!b || b.classList.contains('disabled')) return {ok:false, reason:'send disabled'};
    b.click(); return {ok:true};
})()"""


def cdp_connect(port):
    return boss.CDPSession(port)


def is_test_resume(path):
    """测试简历（张三/13800138000/test 样本）不允许用于真实自动投递。"""
    if not path or not os.path.isfile(path):
        return True
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return True
    text = " ".join([path, str(data.get("source_file", "")),
                     str(data.get("raw_text", ""))[:400]]).lower()
    return any(w in text for w in ("test", "sample", "测试", "示例", "张三", "13800138000"))


RESUME_EXTS = {".pdf", ".docx", ".txt", ".md"}
RESUME_MAX_BYTES = 15 * 1024 * 1024


def _safe_name(name):
    base = os.path.basename(name or "resume")
    base = re.sub(r"[^\w\u4e00-\u9fa5.\-]+", "_", base).strip("._") or "resume"
    return base[:80]


def resume_status(output_dir):
    """读取工作台当前简历解析结果与 Top 匹配，供前端/Agent 使用。"""
    path = os.path.join(output_dir, "resume.json")
    if not os.path.isfile(path):
        return {"has_resume": False, "msg": "尚未上传简历"}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return {"has_resume": False, "msg": f"简历 JSON 读取失败：{e}"}
    targets = []
    tpath = os.path.join(output_dir, "targets.json")
    if os.path.isfile(tpath):
        try:
            with open(tpath, encoding="utf-8") as f:
                targets = json.load(f)
        except Exception:
            targets = []
    return {
        "has_resume": True,
        "name": data.get("original_name") or os.path.basename(str(data.get("source_file") or path)),
        "path": path,
        "parsed_at": datetime.datetime.fromtimestamp(
            os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M:%S"),
        "skills": (data.get("skills") or [])[:14],
        "years_experience": data.get("years_experience"),
        "education": data.get("education") or [],
        "target_roles": data.get("target_roles") or [],
        "is_test": is_test_resume(path),
        "targets_count": len(targets),
        "top_matches": [{
            "title": t.get("title"), "company": t.get("recipient"),
            "salary": t.get("salary"), "score": t.get("score"),
        } for t in targets[:5]],
        "report": os.path.join(output_dir, "简历匹配报告.md"),
    }


def _run_script(script, args, timeout=180):
    cmd = [sys.executable, os.path.join(HERE, script)] + args
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def api_resume_upload(data, output_dir, manager=None):
    """接收前端 base64 文件，本地解析简历并重跑匹配；不上传任何云端。"""
    name = _safe_name(data.get("filename"))
    ext = os.path.splitext(name)[1].lower()
    if ext not in RESUME_EXTS:
        return {"ok": False, "msg": "仅支持 PDF / DOCX / TXT / MD"}
    try:
        raw = base64.b64decode(data.get("content_b64") or "", validate=True)
    except Exception:
        return {"ok": False, "msg": "文件内容解码失败"}
    if not raw:
        return {"ok": False, "msg": "文件为空"}
    if len(raw) > RESUME_MAX_BYTES:
        return {"ok": False, "msg": "文件超过 15MB，请压缩后再上传"}
    if not output_dir:
        return {"ok": False, "msg": "未配置输出目录"}

    raw_dir = os.path.join(os.path.expanduser("~"), ".boss-zhipin-scraper", "resume")
    os.makedirs(raw_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    raw_path = os.path.join(raw_dir, f"{stamp}_{name}")
    with open(raw_path, "wb") as f:
        f.write(raw)

    resume_json = os.path.join(output_dir, "resume.json")
    try:
        parsed = _run_script("parse_resume.py", ["--input", raw_path, "--output", resume_json])
    except subprocess.TimeoutExpired:
        return {"ok": False, "msg": "简历解析超时，请换一份文件或先 OCR 扫描件"}
    if parsed.returncode != 0 or not os.path.isfile(resume_json):
        detail = (parsed.stderr or parsed.stdout or "").strip().splitlines()[-1:] or ["解析失败"]
        return {"ok": False, "msg": f"简历解析失败：{detail[0]}"}

    # 保留用户原始文件名，避免界面显示带时间戳的内部文件名
    try:
        with open(resume_json, encoding="utf-8") as f:
            parsed_data = json.load(f)
        parsed_data["original_name"] = name
        parsed_data["uploaded_at"] = stamp
        with open(resume_json, "w", encoding="utf-8") as f:
            json.dump(parsed_data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    master = os.path.join(output_dir, "岗位公司信息_含JD.json")
    matched = None
    warning = ""
    if os.path.isfile(master):
        try:
            matched = _run_script("match_resume.py", [
                "--resume", resume_json, "--jobs", master, "--top", "10",
                "--out-report", os.path.join(output_dir, "简历匹配报告.md"),
                "--out-targets", os.path.join(output_dir, "targets.json"),
            ])
        except subprocess.TimeoutExpired:
            warning = "匹配打分超时，简历已解析"
        if matched is not None and matched.returncode != 0:
            warning = "简历已解析，但匹配打分失败"
    else:
        warning = "简历已解析；岗位主表不存在，暂未匹配"

    if manager is not None:
        manager.set_resume(resume_json)
    st = resume_status(output_dir)
    st.update({"ok": True, "msg": warning or "简历已解析并完成匹配", "raw_path": raw_path})
    return st


def api_resume_rematch(output_dir):
    resume_json = os.path.join(output_dir, "resume.json")
    master = os.path.join(output_dir, "岗位公司信息_含JD.json")
    if not os.path.isfile(resume_json):
        return {"ok": False, "msg": "请先上传简历"}
    if not os.path.isfile(master):
        return {"ok": False, "msg": "岗位主表不存在，请先刷新数据"}
    try:
        r = _run_script("match_resume.py", [
            "--resume", resume_json, "--jobs", master, "--top", "10",
            "--out-report", os.path.join(output_dir, "简历匹配报告.md"),
            "--out-targets", os.path.join(output_dir, "targets.json"),
        ])
    except subprocess.TimeoutExpired:
        return {"ok": False, "msg": "匹配打分超时"}
    if r.returncode != 0:
        return {"ok": False, "msg": (r.stderr or r.stdout or "匹配失败").strip().splitlines()[-1]}
    st = resume_status(output_dir)
    st.update({"ok": True, "msg": "已按当前岗位重新匹配"})
    return st


def find_page(cdp, url_part, timeout=5):
    for t in cdp.send("Target.getTargets", timeout=timeout)["result"]["targetInfos"]:
        if t["type"] == "page" and url_part in t.get("url", ""):
            return t["targetId"], cdp.send(
                "Target.attachToTarget", {"targetId": t["targetId"], "flatten": True},
                timeout=timeout,
            )["result"]["sessionId"]
    return None, None


def detach(cdp, *sids):
    """显式 detach 页面会话。

    attachToTarget 出来的会话不会因为 websocket 关闭就消失，会一直挂在 Chrome 上；
    看板每分钟轮询状态、巡检每轮再探一次，会话只增不减，几小时后浏览器就被拖到不响应
    （表现就是 Runtime.evaluate 一直不返回、/api/status 超时）。
    """
    for s in sids:
        if not s:
            continue
        try:
            cdp.send("Target.detachFromTarget", {"sessionId": s}, timeout=2)
        except Exception:
            pass


def eval_fast(cdp, js, sid, timeout=4):
    r = cdp.send("Runtime.evaluate", {"expression": js, "returnByValue": True},
                 sid, timeout=timeout)
    return r.get("result", {}).get("result", {}).get("value", None)


def detail_name(cdp, sid):
    return cdp.eval_js(
        "(() => {const t=document.body.innerText;const i=t.indexOf('没有更多了');"
        "return i<0?'':t.slice(i+6,i+120);})()", sid
    ) or ""


def selected_text(cdp, sid):
    return cdp.eval_js(
        "(() => {const e=document.querySelector('.friend-content.selected');"
        "return e?(e.innerText||''):'';})()", sid
    ) or ""


def click_by_text(cdp, sid, keyword):
    """点击左侧会话：先原生鼠标，未切换再用 DOM click 兜底，最后用 selected 校验。"""
    pos = cdp.eval_js(
        r"""(() => {const w=[...document.querySelectorAll('.friend-content-warp')]
          .find(x=>(x.innerText||'').includes(%s));
          if(!w) return null; const e=w.querySelector('.friend-content')||w;
          const r=e.getBoundingClientRect();
          return {x:r.x+r.width/2,y:r.y+r.height/2};})()""" % json.dumps(keyword, ensure_ascii=False),
        sid,
    )
    if not pos:
        return False
    for typ in ("mousePressed", "mouseReleased"):
        cdp.send("Input.dispatchMouseEvent", {
            "type": typ, "x": pos["x"], "y": pos["y"],
            "button": "left", "clickCount": 1,
        }, sid)
    time.sleep(0.6)
    if keyword in selected_text(cdp, sid):
        return True
    # 某些列表项的原生点击会被内部元素吞掉，用 DOM click 兜底
    cdp.eval_js(
        r"""(() => {const w=[...document.querySelectorAll('.friend-content-warp')]
          .find(x=>(x.innerText||'').includes(%s));
          if(!w) return false; (w.querySelector('.friend-content')||w).click(); return true;})()"""
        % json.dumps(keyword, ensure_ascii=False), sid)
    time.sleep(0.6)
    return keyword in selected_text(cdp, sid)


_STATUS_PROBE_LOCK = threading.Lock()
_STATUS_CACHE = {"at": 0.0, "value": None}


def api_status_guarded(port, manager=None, timeout=8):
    """给 /api/status 加硬超时。

    CDP 的 Runtime.evaluate 在页面被节流/挂住时不返回，find_page + eval 能一路挂到几十秒，
    HTTP 请求与巡检的健康检查就一起超时，最后被记成「server unreachable / cdp_down」——
    明明是「探测卡住」，却报成「浏览器没开」。这里超时就返回明确的三态：cdp=None。

    另外用一把非阻塞锁保证同一时刻只有一个探测线程：否则页面一直卡着时，
    每分钟一次的轮询会不断堆积僵尸线程。
    """
    if not _STATUS_PROBE_LOCK.acquire(blocking=False):
        cached = _STATUS_CACHE.get("value") or {}
        return dict(cached, stale=True,
                    message="上一次状态探测仍未返回（CDP 页面卡住），先沿用缓存；"
                            "可在 Chrome 里刷新/重开标签页后恢复")

    box = {}

    def work():
        try:
            box["value"] = api_status(port, manager)
        except Exception as e:  # noqa: BLE001 - 探测失败要降级而不是抛给调用方
            box["error"] = e
        finally:
            try:
                _STATUS_PROBE_LOCK.release()
            except Exception:
                pass

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return {"cdp": None, "logged_in": None, "has_chat": None, "timeout": True,
                "message": f"状态探测 {timeout}s 未返回（CDP 页面无响应），本次不判定在线/离线"}
    if "error" in box:
        return {"cdp": None, "logged_in": None, "has_chat": None, "timeout": False,
                "message": f"状态探测异常：{box['error']}"}
    value = box.get("value") or {}
    _STATUS_CACHE.update({"at": time.time(), "value": value})
    return value


def api_status(port, manager=None):
    result = {"cdp": False, "logged_in": False, "has_chat": False,
              "message": "", "profile": "~/.boss-zhipin-scraper/chrome-profile"}
    # 刷新期间浏览器正被采集器占用，不要再 attach 页面（会卡住 5 秒以上甚至超时）
    if manager is not None and manager.is_running():
        last = manager.status().get("last_ok_at", "")
        return {"cdp": True, "logged_in": True, "has_chat": True,
                "refreshing": True, "last_ok_at": last,
                "message": "数据刷新中，登录态沿用当前专用 Chrome；完成后自动更新",
                "profile": result["profile"]}
    try:
        cdp = cdp_connect(port)
    except Exception as e:
        result["message"] = f"CDP 未启动：{e}"
        return result
    chat_sid = None
    jobs_sid = None
    try:
        result["cdp"] = True
        tid, sid = find_page(cdp, "web/geek/chat", timeout=5)
        if not tid:
            # 没有聊天页时用职位页判断登录态，避免自动刷新永远不启动
            jtid, jsid = find_page(cdp, "web/geek/jobs", timeout=5)
            jobs_sid = jsid
            if jtid:
                txt = eval_fast(cdp, "document.body.innerText", jsid, timeout=4) or ""
                result["logged_in"] = ("登录/注册" not in txt and
                                       any(w in txt for w in ("消息", "简历", "钥匙")))
                result["message"] = ("已登录" if result["logged_in"]
                                     else "未登录，请点击「打开登录页」完成扫码/验证码")
            else:
                result["message"] = "浏览器已连接，但未打开职位页；点击「打开登录页」引导"
            return result
        result["has_chat"] = True
        chat_sid = sid
        txt = eval_fast(cdp, "document.body.innerText", sid, timeout=4) or ""
        result["logged_in"] = ("没有更多了" in txt or "与您进行过沟通" in txt or
                               bool("在线简历" in txt) or "全部" in txt and "未读" in txt)
        result["message"] = "已登录" if result["logged_in"] else "疑似未登录，请点击「打开登录页」完成登录"
    except TimeoutError:
        # 聊天标签被前端/浏览器卡死时，reload 一次，别让状态检查无限等待
        if chat_sid:
            try:
                cdp.send("Page.reload", {}, chat_sid, timeout=3)
            except Exception:
                pass
        result["message"] = "聊天页无响应，已尝试重载；请点「打开登录页」或手动刷新 Chrome 标签"
    except Exception as e:
        result["message"] = f"状态检查失败：{e}"
    finally:
        detach(cdp, chat_sid, jobs_sid)
        cdp.close()
    return result


def _raise_window(cdp, target_id):
    try:
        win = cdp.send("Browser.getWindowForTarget", {"targetId": target_id},
                       timeout=3)["result"]["windowId"]
        cdp.send("Browser.setWindowBounds",
                 {"windowId": win, "bounds": {"windowState": "normal"}}, timeout=3)
    except Exception:
        pass


def api_login(port, manager=None):
    """打开/激活登录页：只做 activate 或 HTTP 新建标签，不 attach 页面，避免刷新时卡住。"""
    try:
        cdp = cdp_connect(port)
    except Exception as e:
        return {"ok": False, "msg": f"CDP 未启动：{e}"}
    try:
        # 刷新中不要抢采集器正在用的职位页，改为新开一个登录标签
        if not (manager is not None and manager.is_running()):
            try:
                targets = cdp.send("Target.getTargets", timeout=5)["result"]["targetInfos"]
                jobs = [t for t in targets if t.get("type") == "page" and "web/geek/jobs" in t.get("url", "")]
                if jobs:
                    cdp.send("Target.activateTarget", {"targetId": jobs[0]["targetId"]}, timeout=3)
                    _raise_window(cdp, jobs[0]["targetId"])
                    return {"ok": True,
                            "msg": "已切到 BOSS 职位页；如果页面显示未登录，请在专用 Chrome 窗口扫码/验证码",
                            "url": jobs[0].get("url", LOGIN_URL)}
            except Exception:
                pass   # 目标正在被刷新关闭时忽略，改走 HTTP 新建标签
    finally:
        cdp.close()

    # 用 DevTools HTTP 接口新开前台标签，不 attach 页面，返回更快
    try:
        req = Request(f"http://127.0.0.1:{port}/json/new?{quote(LOGIN_URL, safe='')}", method="PUT")
        raw = urlopen(req, timeout=6).read()
        info = json.loads(raw.decode("utf-8") or "{}")
        tid = info.get("id")
        if tid:
            try:
                cdp2 = cdp_connect(port)
                _raise_window(cdp2, tid)
                cdp2.close()
            except Exception:
                pass
        return {"ok": True, "msg": "已打开 BOSS 职位页；请在专用 Chrome 窗口完成登录（扫码或验证码）",
                "url": LOGIN_URL}
    except Exception as e:
        return {"ok": False, "msg": f"打开登录页失败：{e}，请确认专用 Chrome 仍在运行"}


def api_conversations(port):
    try:
        cdp = cdp_connect(port)
    except Exception as e:
        return {"ok": False, "msg": str(e), "items": []}
    sid = None
    try:
        tid, sid = find_page(cdp, "web/geek/chat")
        if not tid:
            return {"ok": False, "msg": "未打开聊天页", "items": []}
        items = cdp.eval_js(r"""(() => {
          return [...document.querySelectorAll('.friend-content-warp')].map((w,i)=>{
            const name=w.querySelector('.name-text')?.innerText.trim()||'';
            const title=w.querySelector('.title-box')?.innerText.trim().replace(/\n/g,' ')||'';
            const time=w.querySelector('.time')?.innerText.trim()||'';
            const last=w.querySelector('.last-msg-text')?.innerText.trim()||'';
            const selected=!!w.querySelector('.friend-content.selected');
            return {i,name,title,time,last,selected};
          });
        })()""", sid) or []
        return {"ok": True, "items": items, "active": detail_name(cdp, sid).split("\n")[0]}
    except Exception as e:
        return {"ok": False, "msg": f"聊天页无响应：{e}", "items": []}
    finally:
        detach(cdp, sid)
        cdp.close()


MSG_SIG_JS = r"""(() => [...document.querySelectorAll('ul.im-list li.message-item')].map(li=>{
  const tc=li.querySelector('.text-content');
  if(tc && (tc.innerText||'').trim()) return tc.innerText.trim();
  const c=li.cloneNode(true);
  c.querySelectorAll('.item-time,.message-status,.time').forEach(e=>e.remove());
  return (c.innerText||'').trim();
}).filter(Boolean).join('\u0001'))()"""

CONV_LAST_JS = r"""(() => {
  const w=[...document.querySelectorAll('.friend-content-warp')]
    .find(x=>(x.innerText||'').includes(%s));
  return w?(w.querySelector('.last-msg-text')?.innerText.trim()||''):'';
})()"""

MSG_READ_JS = r"""(() => {
  return [...document.querySelectorAll('ul.im-list li.message-item')].map(li=>{
    const cls=li.className||'';
    const role=cls.includes('item-myself')?'me':cls.includes('item-friend')?'them':'system';
    const time=(li.querySelector('.message-time')||li.querySelector('.item-time .time'))?.innerText.trim()||'';
    let text=li.querySelector('.text-content')?.innerText.trim()||'';
    if(!text){
      const c=li.cloneNode(true);
      c.querySelectorAll('.item-time,.message-status,.time').forEach(e=>e.remove());
      text=(c.innerText||'').trim();
    }
    return {role,time,text};
  }).filter(m=>m.text);
})()"""


def api_messages(port, who):
    try:
        cdp = cdp_connect(port)
    except Exception as e:
        return {"ok": False, "msg": str(e), "messages": []}
    sid = None
    try:
        tid, sid = find_page(cdp, "web/geek/chat")
        if not tid:
            return {"ok": False, "msg": "未打开聊天页", "messages": []}
        if not who:
            return {"ok": True, "who": detail_name(cdp, sid).split("\n")[0],
                    "messages": cdp.eval_js(MSG_READ_JS, sid) or []}

        def norm(s):
            return "".join(str(s or "").split())

        expected = cdp.eval_js(CONV_LAST_JS % json.dumps(who, ensure_ascii=False), sid) or ""
        exp_key = norm(expected)[:16]
        before = cdp.eval_js(MSG_SIG_JS, sid) or ""
        head_before = detail_name(cdp, sid)
        already = who in head_before and who in selected_text(cdp, sid)

        if not already and not click_by_text(cdp, sid, who):
            return {"ok": False, "msg": f"会话列表中未能选中「{who}」", "messages": []}

        # 切换会话时详情头部先变、消息列表后变（实测可差 5 秒）；同会话也可能残留旧消息。
        # 判定标准：头部匹配 + 最后一条消息与该会话列表里的 last 文本一致，且连续两次一致。
        head, msgs, stable, last_sig = "", [], 0, None
        for i in range(24):
            time.sleep(0.5)
            head = detail_name(cdp, sid)
            msgs = cdp.eval_js(MSG_READ_JS, sid) or []
            sig = "\u0001".join(norm(m["text"]) for m in msgs)
            head_ok = who in head
            if exp_key:
                body = norm(" ".join(m["text"] for m in msgs))
                content_ok = exp_key in body
            else:
                content_ok = bool(msgs) and sig != before
            if head_ok and content_ok:
                stable = stable + 1 if sig == last_sig else 0
                last_sig = sig
                if stable >= 1:
                    break
            elif not exp_key and head_ok and not msgs and already and i >= 4:
                break   # 本来就是空会话，最多等 2 秒
            if i == 23:
                break

        if who not in head:
            return {"ok": False, "msg": f"会话切换超时（当前：{head.split(chr(10))[0] or '未知'}）", "messages": []}
        if exp_key:
            body = norm(" ".join(m["text"] for m in msgs))
            if exp_key not in body:
                return {"ok": False, "msg": "消息列表仍是旧内容，请重试", "messages": []}
        elif before and not msgs:
            return {"ok": False, "msg": "消息列表未加载完成，请重试", "messages": []}

        return {"ok": True, "who": who, "messages": msgs}
    except Exception as e:
        return {"ok": False, "msg": f"聊天页无响应：{e}", "messages": []}
    finally:
        detach(cdp, sid)
        cdp.close()


def api_greet(port, job_url, recipient, greeting):
    """发送招呼语：打开岗位页 → 立即沟通 → 校验收件人 → 发送 → 复核。"""
    if not all([job_url, recipient, greeting]):
        return {"ok": False, "msg": "缺少 job_url/recipient/greeting"}
    try:
        cdp = cdp_connect(port)
    except Exception as e:
        return {"ok": False, "msg": f"CDP 未启动：{e}"}
    try:
        # 打开岗位页并点「立即沟通」
        tid, sid = find_page(cdp, "job_detail")
        if not tid:
            _tid, sid = boss.create_page_session(cdp, background=False)
        cdp.send("Page.navigate", {"url": job_url}, sid)
        time.sleep(6)
        clicked = cdp.eval_js(r"""(() => {
          const els=[...document.querySelectorAll('.btn-startchat')].filter(e=>{
            const r=e.getBoundingClientRect();
            return r.width>50 && r.y>0 && r.y<window.innerHeight;
          });
          if(!els.length) return {ok:false};
          els[0].click(); return {ok:true};
        })()""", sid)
        if not clicked.get("ok"):
            return {"ok": False, "msg": "未找到可见的「立即沟通」按钮"}

        # 等待聊天页
        ctid, csid = None, None
        for _ in range(8):
            ctid, csid = find_page(cdp, "web/geek/chat")
            if ctid:
                break
            time.sleep(2)
        if not ctid:
            return {"ok": False, "msg": "聊天页未打开"}
        cdp.send("Page.reload", {}, csid)
        time.sleep(5)

        if not click_by_text(cdp, csid, recipient):
            return {"ok": False, "msg": f"会话列表中未找到「{recipient}」"}
        head = detail_name(cdp, csid)
        if recipient not in head:
            return {"ok": False, "msg": f"收件人不匹配：{head[:60]}，拒绝发送"}
        msg_js = GREET_JS % (json.dumps(greeting, ensure_ascii=False),
                             json.dumps(greeting, ensure_ascii=False))
        typed = cdp.eval_js(msg_js, csid)
        time.sleep(1)
        sent = cdp.eval_js(SEND_JS, csid)
        time.sleep(2)
        after = detail_name(cdp, csid)
        ok = bool(typed.get("ok") and sent.get("ok") and recipient in after)
        return {"ok": ok,
                "msg": "招呼语已送达" if ok else f"发送异常 typed={typed} sent={sent}",
                "recipient": recipient, "head": head.split("\n")[0]}
    finally:
        cdp.close()


class RefreshManager:
    """后台定时刷新：状态落盘，前端轮询；刷新与打招呼互斥，避免共用 CDP 页面冲突。"""

    def __init__(self, cfg, state_path, interval_minutes, apply_manager=None):
        self.cfg = cfg
        self.state_path = state_path
        self.interval = interval_minutes
        self.apply_manager = apply_manager
        self._lock = threading.Lock()

    def _read(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def is_running(self):
        return self._read().get("status") == "running"

    def _parse(self, s):
        try:
            return datetime.datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None

    def _next_at(self, st):
        """下次时间 = 最近一次「尝试」+ 间隔。

        以前只认 last_ok_at：一轮失败后 next_at 会退回到上一个成功点之前，
        页面上出现「下次 06:31」而当时已经 12:59——不是小瑕疵，是让人以为调度坏了。
        """
        if self.interval <= 0:
            return ""
        base = None
        for key in ("last_ok_at", "finished_at", "started_at"):
            d = self._parse(st.get(key))
            if d and (base is None or d > base):
                base = d
        base = base or datetime.datetime.now()
        return (base + datetime.timedelta(minutes=self.interval)).strftime("%Y-%m-%d %H:%M:%S")

    def status(self):
        st = self._read()
        st["interval_minutes"] = self.interval
        st["next_at"] = self._next_at(st)
        st["last_attempt_at"] = st.get("finished_at") or st.get("started_at") or ""
        nxt = self._parse(st["next_at"])
        st["overdue"] = bool(nxt and nxt < datetime.datetime.now()
                             and st.get("status") != "running")
        st["msg"] = st.get("error", "") if st.get("status") == "error" else ""
        return st

    def request(self, force=False):
        if not force and self.is_running():
            return {"ok": False, "msg": "正在刷新中，请稍候"}
        threading.Thread(target=self._worker, daemon=True).start()
        return {"ok": True, "msg": "刷新任务已启动，通常 1-5 分钟"}

    def _worker(self):
        with self._lock, CDP_GATE:
            try:
                refresh_data.refresh(
                    self.cfg, self.state_path,
                    # 启动前已做过登录检查；刷新中不再 attach 页面，避免登录检查把线程卡死
                    logged_check=lambda: api_status(self.cfg["cdp_port"], self).get("logged_in", False),
                )
                # 刷新成功且用户已显式启用时，对新数据触发一轮自动投递
                if self.apply_manager and self.apply_manager.status().get("enabled"):
                    self.apply_manager.request(reason="refresh")
            except Exception as e:
                st = self._read()
                st.update({"status": "error", "error": str(e),
                           "finished_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
                refresh_data._write_state(self.state_path, st)

    def _due(self):
        st = self.status()
        now = datetime.datetime.now()
        if st.get("status") == "running":
            # 进程被强杀会留下 running 残状态：超过 15 分钟视为卡死，允许重试
            start = self._parse(st.get("started_at"))
            return start is not None and (now - start).total_seconds() >= 15 * 60
        last_ok = self._parse(st.get("last_ok_at"))
        if st.get("status") == "error":
            # 失败后 5 分钟再试（CDP 掉线时立刻重试只会连刷失败记录）
            last_try = self._parse(st.get("finished_at")) or last_ok
            return last_try is None or (now - last_try).total_seconds() >= 5 * 60
        return last_ok is None or (now - last_ok).total_seconds() >= self.interval * 60

    def _can_refresh(self):
        """未登录时不启动自动刷新，避免采集器占着浏览器让登录页打不开。"""
        if self.is_running():
            return False
        try:
            return bool(api_status(self.cfg["cdp_port"], self).get("logged_in"))
        except Exception:
            return False

    def start(self):
        if self.interval <= 0:
            return
        def loop():
            time.sleep(5)   # 先把 HTTP 服务让出来，再在后台做登录检查
            while True:
                try:
                    # 先回收上一轮遗留的 running 残影，否则 is_running() 永远为真、调度永不触发（#20）
                    if refresh_data.recover_stale_state(self.state_path):
                        print("检测到上一轮刷新遗留的 running 状态（线程已消失），已回收为 error 并重试")
                    if self._due() and self._can_refresh():
                        self.request(force=True)
                except Exception:
                    pass
                time.sleep(30)
        threading.Thread(target=loop, daemon=True).start()


class ApplyManager:
    """持续自动投递：每次刷新后用当前简历重新打分，只投递未投递过的 Top 岗位。

    默认关闭。启用时必须提供真实简历（测试简历会被拒绝）。
    状态独立于刷新状态，去重记录写在 auto_apply_state.json。
    """

    def __init__(self, cfg, state_path, resume_path, limit, gap_min, gap_max, send_resume=False):
        self.cfg = cfg
        self.state_path = state_path
        self.resume_path = resume_path
        self.master_path = os.path.join(cfg["output_dir"], "岗位公司信息_含JD.json")
        self.limit = limit
        self.gap_min = gap_min
        self.gap_max = gap_max
        self.send_resume = send_resume
        self._lock = threading.Lock()

    def set_resume(self, path):
        self.resume_path = path

    def status(self):
        st = auto_apply.load_state(self.state_path)
        return {
            "enabled": bool(st.get("enabled")),
            "status": st.get("status", "idle"),
            "progress": st.get("progress", 0),
            "total": st.get("total", 0),
            "applied_count": len(st.get("applied", {})),
            "last_at": st.get("finished_at", ""),
            "error": st.get("error", ""),
            "msg": st.get("msg", ""),
            "resume": os.path.basename(self.resume_path or ""),
            "limit": self.limit,
        }

    def is_running(self):
        return self.status().get("status") == "running"

    def enable(self, enabled, dry_run=False):
        if enabled:
            if not self.resume_path or is_test_resume(self.resume_path):
                msg = "拒绝启用：请先上传真实简历（当前为测试样本或文件不存在）"
                st = auto_apply.load_state(self.state_path)
                st.update({"enabled": False, "msg": msg})
                auto_apply.save_state(self.state_path, st)
                return {"ok": False, "msg": msg}
            if not os.path.isfile(self.master_path):
                return {"ok": False, "msg": "拒绝启用：岗位主表不存在，请先刷新数据"}
        st = auto_apply.load_state(self.state_path)
        st.update({
            "enabled": bool(enabled), "dry_run": bool(dry_run),
            "enabled_at": auto_apply.now(),
            "msg": "已启用：每次刷新后自动投递 Top 匹配岗位" if enabled else "已关闭自动投递",
        })
        auto_apply.save_state(self.state_path, st)
        return {"ok": True, "msg": st["msg"]}

    def request(self, reason="manual"):
        st = self.status()
        if not st["enabled"]:
            return {"ok": False, "msg": "自动投递未启用"}
        if st["status"] == "running":
            return {"ok": False, "msg": "正在投递中，请稍候"}
        threading.Thread(target=self._worker, args=(reason,), daemon=True).start()
        return {"ok": True, "msg": "投递任务已启动"}

    def _worker(self, reason):
        with self._lock, CDP_GATE:
            st = auto_apply.load_state(self.state_path)
            st.update({"status": "running", "started_at": auto_apply.now(),
                       "triggered_by": reason, "error": ""})
            auto_apply.save_state(self.state_path, st)
            try:
                targets = auto_apply.build_targets(
                    self.resume_path, self.master_path,
                    top=max(self.limit * 3, self.limit), state_path=self.state_path,
                )
                final = auto_apply.run_batch(
                    targets, self.cfg["cdp_port"], self.state_path, self.limit,
                    self.gap_min, self.gap_max,
                    dry_run=bool(st.get("dry_run")), send_resume=self.send_resume,
                )
                st = auto_apply.load_state(self.state_path)
                st["last_at"] = final.get("finished_at")
                auto_apply.save_state(self.state_path, st)
            except Exception as exc:
                st = auto_apply.load_state(self.state_path)
                st.update({"status": "error", "error": str(exc),
                           "finished_at": auto_apply.now()})
                auto_apply.save_state(self.state_path, st)


def make_handler(port, html_path, manager=None, output_dir=None, apply_manager=None):
    def load_html():
        # 每次请求都重读，支持重新生成看板后免重启热加载
        if html_path and os.path.isfile(html_path):
            with open(html_path, encoding="utf-8") as f:
                return f.read()
        return b""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # 静默，避免刷屏

        def _send(self, obj, status=200, ctype="application/json; charset=utf-8"):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8") if not isinstance(obj, bytes) else obj
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/":
                page = load_html()
                if isinstance(page, str):
                    page = page.encode("utf-8")
                self._send(page, ctype="text/html; charset=utf-8")
            elif u.path == "/api/status":
                self._send(api_status_guarded(port, manager))
            elif u.path == "/api/conversations":
                if manager and manager.is_running():
                    self._send({"ok": False, "msg": "数据刷新中，会话列表稍后自动恢复", "items": []})
                else:
                    self._send(api_conversations(port))
            elif u.path == "/api/messages":
                if manager and manager.is_running():
                    self._send({"ok": False, "msg": "数据刷新中，消息记录稍后自动恢复", "messages": []})
                else:
                    q = parse_qs(u.query)
                    self._send(api_messages(port, q.get("who", [""])[0]))
            elif u.path == "/api/refresh/status":
                self._send(manager.status() if manager else
                           {"status": "offline", "msg": "未启用自动刷新"})
            elif u.path == "/api/auto-apply/status":
                self._send(apply_manager.status() if apply_manager else
                           {"enabled": False, "status": "offline", "msg": "未启用自动投递"})
            elif u.path == "/api/resume/status":
                self._send(resume_status(output_dir))
            elif u.path in ("/api/export.xlsx", "/api/export.csv"):
                # 现读主表再导出：定时刷新落库后点一下就拿到最新一版，不用重建模板
                q = parse_qs(u.query)
                try:
                    body, fname, ctype = export_jobs.export(
                        q, output_dir or os.path.dirname(html_path or ""))
                except Exception as e:
                    self._send({"ok": False, "msg": f"导出失败：{e}"}, status=500)
                    return
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Disposition",
                                 'attachment; filename="jobs.xlsx"; filename*=UTF-8\'\''
                                 + quote(fname))
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/account":
                # 刷新期间浏览器被采集器占着，不去抢 CDP，直接回上次探测结果
                out = output_dir or os.path.dirname(html_path or "")
                if manager and manager.is_running():
                    state = account_probe.load_state(out)
                    self._send(dict(state or {"known": False}, stale=True,
                                    msg="刷新中，账号信息沿用上次探测"))
                else:
                    try:
                        self._send(account_probe.observe(out, port))
                    except Exception as e:
                        self._send(dict(account_probe.load_state(out) or {"known": False},
                                        error=str(e)))
            elif u.path in ("/api/trends", "/api/jobs"):
                out = output_dir or os.path.dirname(html_path or "")
                if u.path == "/api/jobs" and u.query:
                    # 带参数时走统一查询层，页面用它校验「我数出来的条数」和
                    # 「导出会拿到的条数」是否一致；不一致就是口径又分叉了。
                    qs = parse_qs(u.query)
                    try:
                        if str((qs.get("count") or [""])[0]).lower() in ("1", "true", "yes"):
                            self._send({"ok": True, "count": export_jobs.count(qs, out)})
                        else:
                            rows, _f, st = jobquery.query(out, params=qs)
                            self._send({"ok": True, "count": len(rows), "stats": st,
                                        "jobs": [r["raw"] for r in rows]})
                    except Exception as e:
                        self._send({"ok": False, "msg": f"查询失败：{e}"}, status=500)
                    return
                name = "trends.json" if u.path == "/api/trends" else "岗位公司信息_含JD.json"
                path = os.path.join(out, name)
                data = None
                try:
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    data = None
                self._send(data if data is not None else
                           {"ok": False, "msg": f"尚未生成 {name}"})
            elif u.path == "/api/facets":
                # 下拉选项与条数：城市已按港澳台口径归一，不会再出现「选了香港 0 条」
                out = output_dir or os.path.dirname(html_path or "")
                try:
                    rows = jobquery.build_rows(jobquery.load_master(out),
                                               jobquery.load_ai_map(out))
                    self._send({"ok": True, "count": len(rows),
                                "facets": jobquery.facets(rows)})
                except Exception as e:
                    self._send({"ok": False, "msg": f"读取失败：{e}"}, status=500)
            else:
                self._send({"error": "not found"}, status=404)

        def do_POST(self):
            u = urlparse(self.path)
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                data = {}
            if u.path == "/api/login":
                self._send(api_login(port, manager))
            elif u.path == "/api/refresh":
                self._send(manager.request() if manager else
                           {"ok": False, "msg": "未启用自动刷新"})
            elif u.path == "/api/auto-apply":
                self._send(apply_manager.request() if apply_manager else
                           {"ok": False, "msg": "未启用自动投递"})
            elif u.path == "/api/auto-apply/config":
                self._send(apply_manager.enable(bool(data.get("enabled")),
                                                bool(data.get("dry_run")))
                           if apply_manager else {"ok": False, "msg": "未启用自动投递"})
            elif u.path == "/api/resume/upload":
                self._send(api_resume_upload(data, output_dir or
                                             os.path.dirname(html_path or ""), apply_manager))
            elif u.path == "/api/resume/rematch":
                self._send(api_resume_rematch(output_dir or
                                              os.path.dirname(html_path or "")))
            elif u.path == "/api/greet":
                if (manager and manager.is_running()) or (apply_manager and apply_manager.is_running()):
                    self._send({"ok": False, "msg": "数据刷新或自动投递中，请稍后再打招呼"})
                elif not CDP_GATE.acquire(timeout=1):
                    self._send({"ok": False, "msg": "浏览器正忙，请稍后再试"})
                else:
                    try:
                        self._send(api_greet(port, data.get("job_url"), data.get("recipient"),
                                             data.get("greeting")))
                    finally:
                        CDP_GATE.release()
            else:
                self._send({"error": "not found"}, status=404)

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765, help="HTTP 服务端口")
    ap.add_argument("--cdp-port", type=int, default=9222, help="Chrome CDP 端口")
    ap.add_argument("--dashboard", required=True, help="看板 HTML 路径")
    ap.add_argument("--output-dir", default="", help="数据输出目录（默认看板所在目录）")
    ap.add_argument("--history-dir", default="", help="快照目录")
    ap.add_argument("--interval-minutes", type=int, default=30, help="自动刷新间隔分钟，0=关闭")
    ap.add_argument("--keyword", default="AI 工程师")
    ap.add_argument("--keywords", default="",
                    help="多关键词按轮次轮换，用 | 或 , 分隔；给了就覆盖 --keyword")
    ap.add_argument("--city", default="100010000")
    ap.add_argument("--pages", type=int, default=8)
    ap.add_argument("--detail-limit", type=int, default=80,
                    help="单轮最多补抓多少条 JD，0=不限；宽关键词靠它把单轮控制在刷新间隔内")
    ap.add_argument("--gap-min", type=float, default=2.5)
    ap.add_argument("--gap-max", type=float, default=5)
    ap.add_argument("--resume", default="", help="parse_resume.py 产出的简历 JSON（启用自动投递必需）")
    ap.add_argument("--apply-limit", type=int, default=3, help="每次刷新后最多投递条数")
    ap.add_argument("--apply-gap-min", type=float, default=8)
    ap.add_argument("--apply-gap-max", type=float, default=15)
    ap.add_argument("--send-resume", action="store_true", help="页面有控件时附带投简历")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    out_dir = args.output_dir or os.path.dirname(os.path.abspath(args.dashboard))
    hist = args.history_dir or os.path.join(os.path.expanduser("~"), ".boss-zhipin-scraper", "history")
    state_path = os.path.join(hist, "refresh_state.json")
    kw_list = [k.strip() for k in (args.keywords or "").replace("|", ",").split(",") if k.strip()]
    cfg = {
        # keywords 留空时由 refresh_data.load_keywords 去读 output/keywords.json，
        # 这样即使 patrol_once 不带参数重启服务，多关键词轮换也不会丢
        "keyword": args.keyword, "keywords": kw_list,
        "city": args.city, "pages": args.pages,
        "cdp_port": args.cdp_port, "output_dir": out_dir,
        "history_dir": hist, "dashboard": args.dashboard,
        "gap_min": args.gap_min, "gap_max": args.gap_max,
        "detail_limit": args.detail_limit,
    }
    apply_state = os.path.join(hist, "auto_apply_state.json")
    uploaded_resume = os.path.join(out_dir, "resume.json")
    resume_path = uploaded_resume if os.path.isfile(uploaded_resume) else args.resume
    apply_manager = ApplyManager(cfg, apply_state, resume_path, args.apply_limit,
                                 args.apply_gap_min, args.apply_gap_max, args.send_resume)
    manager = RefreshManager(cfg, state_path, args.interval_minutes, apply_manager)
    # 启动即回收僵尸 running 状态，避免重启后立刻继承死线程的残影（#20）
    try:
        if refresh_data.recover_stale_state(state_path):
            print("上一轮刷新遗留 running 状态已回收（线程已消失），本轮会重新刷新")
    except Exception as e:
        print(f"僵尸刷新状态回收失败：{e}")
    handler = make_handler(args.cdp_port, args.dashboard, manager, out_dir, apply_manager)
    # Windows 上 allow_reuse_address=True 等于 SO_REUSEADDR，重复启动会静默绑定成功，
    # 出现两个进程抢同一份 state/output；这里关掉复用，让第二个实例直接失败。
    ThreadingHTTPServer.allow_reuse_address = False
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    except OSError as e:
        print(f"端口 {args.port} 已被占用（{e}），已有工作台在运行，本进程退出")
        return 1
    url = f"http://127.0.0.1:{args.port}/"
    print(f"工作台已启动：{url}")
    print("API：/api/status /api/login /api/conversations /api/messages /api/greet "
          "/api/jobs /api/trends /api/refresh /api/refresh/status "
          "/api/auto-apply /api/auto-apply/status /api/auto-apply/config "
          "/api/resume/status /api/resume/upload /api/resume/rematch")
    manager.start()
    print(f"自动刷新：{'每 ' + str(args.interval_minutes) + ' 分钟' if args.interval_minutes else '已关闭'}")
    print(f"自动投递：{'已配置简历 ' + os.path.basename(resume_path) + '，待在页面启用' if resume_path else '未配置简历，可在页面直接上传'}")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        server.shutdown()


if __name__ == "__main__":
    main()
