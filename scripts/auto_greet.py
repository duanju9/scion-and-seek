#!/usr/bin/env python3
"""在已登录的专用 Chrome 中，对 BOSS 岗位发送打招呼语（个人自用，支持单条与批量）。

技术约束（保护账号与准确性，非功能限制）：
- 发送前必须校验聊天详情面板的收件人，防止误发到其他会话。
- 点击会话必须用原生鼠标事件（Input.dispatchMouseEvent），JS .click() 不切换面板。
- 单会话、低频；遇到风控码/验证码立即停止。

单条：
  python auto_greet.py --job-url "<URL>" --recipient "<公司关键字>" --greeting "<文案>"
批量（--batch-file 为 JSON 数组，每项 {"job_url","recipient","greeting"}）：
  python auto_greet.py --batch-file targets.json --gap-min 5 --gap-max 10
两种模式都支持 --dry-run 预检。
"""
import argparse
import importlib.util
import json
import os
import random
import time

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("boss_cdp_raw", os.path.join(HERE, "boss_cdp_raw.py"))
boss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boss)

DEFAULT_JOB_URL = "https://www.zhipin.com/job_detail/77592c4e6a7b1c460n162dS1GFVX.html"
DEFAULT_GREETING = (
    "您好，看到三一重机后市场配件AI工程师这个岗位很感兴趣。"
    "我主要做 Python 算法开发，熟悉大模型应用与算法工程化落地，"
    "对设备配件场景的智能化很感兴趣。方便的话想了解下团队目前在做的方向和重点？"
)


def attach_chat(cdp):
    for _ in range(8):
        for t in cdp.send("Target.getTargets")["result"]["targetInfos"]:
            if t["type"] == "page" and "web/geek/chat" in t.get("url", ""):
                sid = cdp.send(
                    "Target.attachToTarget", {"targetId": t["targetId"], "flatten": True}
                )["result"]["sessionId"]
                return sid
        time.sleep(2)
    return None


def detail_head(cdp, sid):
    """详情面板姓名区（'没有更多了' 之后），用于识别当前收件人。"""
    return cdp.eval_js(
        "(() => {const t=document.body.innerText;const i=t.indexOf('没有更多了');"
        "return i<0 ? '' : t.slice(i+6, i+200);})()", sid
    ) or ""


def click_job_button(cdp, sid, job_url):
    """打开岗位页并点击视口内可见的「立即沟通」。"""
    cdp.send("Page.navigate", {"url": job_url}, sid)
    time.sleep(6)
    clicked = cdp.eval_js(r"""(() => {
        const els=[...document.querySelectorAll('.btn-startchat')].filter(e => {
            const r=e.getBoundingClientRect();
            return r.width>50 && r.y>0 && r.y<window.innerHeight;
        });
        if(!els.length) return {ok:false, reason:'无可见沟通按钮'};
        els[0].click(); return {ok:true};
    })()""", sid)
    return clicked


def click_session(cdp, sid, keyword):
    """原生鼠标事件点击左侧含 keyword 的会话项。"""
    pos = cdp.eval_js(
        r"""(() => {
          const e=[...document.querySelectorAll('.friend-content-warp')]
            .find(x => (x.innerText||'').includes(%s));
          if(!e) return null;
          const r=e.getBoundingClientRect();
          return {x:r.x+r.width/2, y:r.y+r.height/2};
        })()""" % json.dumps(keyword, ensure_ascii=False), sid
    )
    if not pos:
        return False
    for typ in ("mousePressed", "mouseReleased"):
        cdp.send("Input.dispatchMouseEvent", {
            "type": typ, "x": pos["x"], "y": pos["y"],
            "button": "left", "clickCount": 1,
        }, sid)
    time.sleep(2)
    return True


def send_one(cdp, job_url, recipient, greeting, dry_run=False):
    """给单个岗位发招呼语，返回 (是否成功, 说明)。"""
    # 打开岗位页：复用已有标签，避免无限开新页
    targets = cdp.send("Target.getTargets")["result"]["targetInfos"]
    job_sid = None
    for t in targets:
        if t["type"] == "page" and "job_detail" in t.get("url", ""):
            job_sid = cdp.send(
                "Target.attachToTarget", {"targetId": t["targetId"], "flatten": True}
            )["result"]["sessionId"]
            break
    if not job_sid:
        _tid, job_sid = boss.create_page_session(cdp, background=False)

    clicked = click_job_button(cdp, job_sid, job_url)
    print("  CLICK:", json.dumps(clicked, ensure_ascii=False))
    if not clicked.get("ok"):
        return False, "无可见沟通按钮"

    chat_sid = attach_chat(cdp)
    if not chat_sid:
        return False, "未找到聊天页"
    cdp.send("Page.reload", {}, chat_sid)
    time.sleep(5)

    if not click_session(cdp, chat_sid, recipient):
        return False, "会话列表中未找到收件人"
    head = detail_head(cdp, chat_sid)
    if recipient not in head:
        return False, f"收件人不匹配：{head[:60]}"
    print("  RECIPIENT_OK:", head.split("\n")[0])

    if dry_run:
        return True, "DRY_RUN 未发送"

    typed = cdp.eval_js(r"""(() => {
        const el=document.querySelector('.chat-input');
        if(!el) return {ok:false};
        el.focus(); el.textContent=%s;
        el.dispatchEvent(new InputEvent('input',{bubbles:true,data:%s,inputType:'insertText'}));
        return {ok:true};
    })()""" % (json.dumps(greeting, ensure_ascii=False),
               json.dumps(greeting, ensure_ascii=False)), chat_sid)
    time.sleep(1)
    sent = cdp.eval_js(r"""(() => {
        const b=document.querySelector('button.btn-send');
        if(!b || b.classList.contains('disabled')) return {ok:false};
        b.click(); return {ok:true};
    })()""", chat_sid)
    time.sleep(2)
    after = detail_head(cdp, chat_sid)
    ok = bool(typed.get("ok") and sent.get("ok") and recipient in after)
    return ok, f"typed={typed.get('ok')} sent={sent.get('ok')} after_ok={recipient in after}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp-port", type=int, default=9222)
    ap.add_argument("--job-url", default=DEFAULT_JOB_URL)
    ap.add_argument("--recipient", default="三一重机", help="收件人关键字，必须与会话列表匹配")
    ap.add_argument("--greeting", default=DEFAULT_GREETING)
    ap.add_argument("--batch-file", help="JSON 数组文件，每项含 job_url/recipient/greeting")
    ap.add_argument("--gap-min", type=float, default=5, help="批量时两条之间的最小间隔秒")
    ap.add_argument("--gap-max", type=float, default=10, help="批量时两条之间的最大间隔秒")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.batch_file:
        with open(args.batch_file, encoding="utf-8") as f:
            targets = json.load(f)
    else:
        targets = [{"job_url": args.job_url, "recipient": args.recipient,
                    "greeting": args.greeting}]

    cdp = boss.CDPSession(args.cdp_port)
    try:
        results = []
        for i, t in enumerate(targets, 1):
            print(f"\n[{i}/{len(targets)}] {t.get('recipient')} | {t.get('job_url')}")
            try:
                ok, msg = send_one(cdp, t["job_url"], t["recipient"],
                                   t.get("greeting", args.greeting), args.dry_run)
            except Exception as e:
                ok, msg = False, f"异常：{e}"
            print("  RESULT:", "OK" if ok else "FAIL", msg)
            results.append({"recipient": t.get("recipient"), "ok": ok, "msg": msg})
            if i < len(targets):
                gap = random.uniform(args.gap_min, args.gap_max)
                print(f"  WAIT {gap:.1f}s")
                time.sleep(gap)
        print("\n=== SUMMARY ===")
        print(json.dumps(results, ensure_ascii=False, indent=2))
    finally:
        cdp.close()


if __name__ == "__main__":
    main()
