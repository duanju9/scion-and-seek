#!/usr/bin/env python3
"""自动投递工作台：按简历匹配结果，对目标岗位执行「沟通式投递」。

BOSS 直聘网页版本质流程：岗位页 -> 立即沟通 -> 校验收件人 -> 发送定制招呼语。
网页端没有可靠的「一键投递简历」按钮；若页面出现可用的「发送简历/投递简历」
控件且 --send-resume 打开，会额外点一次，否则记录 resume_card=ui-unavailable，
不虚构投递成功。

安全与准确性（个人自用也必须保留）：
- 发送前后校验聊天详情面板的收件人，避免误发到别的会话。
- 以 job_id / job_url 去重，结果实时落盘，中断后续跑不会重复投递。
- 随机间隔；遇到验证码、风控、操作频繁关键字立即停止。
- 默认 dry-run 只核对岗位页信息，不点「立即沟通」、不建会话；只有显式去掉 --dry-run 才会发送。

示例：
  python auto_apply.py --resume resume.json --jobs 岗位公司信息_含JD.json \
      --top 10 --limit 3 --state auto_apply_state.json --dry-run
  python auto_apply.py --targets targets.json --limit 3 --state auto_apply_state.json
"""
import argparse
import datetime
import importlib.util
import json
import os
import random
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


boss = _load("boss_cdp_raw", "boss_cdp_raw.py")
auto_greet = _load("auto_greet", "auto_greet.py")
match_resume = _load("match_resume", "match_resume.py")

DEFAULT_STATE = os.path.join(
    os.path.expanduser("~"), ".boss-zhipin-scraper", "history", "auto_apply_state.json"
)
RISK_WORDS = ("验证码", "安全验证", "操作频繁", "风控", "访问异常", "请稍后再试")


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    st.setdefault("applied", {})
    st.setdefault("results", [])
    return st


def save_state(path, st):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def build_targets(resume_path, jobs_path, top=10, state_path=None, min_score=0):
    """用 match_resume 的同一套打分逻辑生成投递目标，并过滤已投递岗位。"""
    with open(resume_path, encoding="utf-8") as f:
        resume = json.load(f)
    with open(jobs_path, encoding="utf-8") as f:
        data = json.load(f)
    jobs = data["jobs"] if isinstance(data, dict) and "jobs" in data else data
    applied = set(load_state(state_path).get("applied", {})) if state_path else set()

    scored = []
    for job in jobs:
        if not job.get("job_link") or not job.get("boss_name"):
            continue
        key = str(job.get("job_id") or job.get("job_link"))
        if key in applied:
            continue
        score, _detail = match_resume.score_job(job, resume)
        if score < min_score:
            continue
        scored.append((score, job))
    scored.sort(key=lambda x: -x[0])
    return [{
        "job_id": job.get("job_id"),
        "title": job.get("title"),
        "salary": job.get("salary"),
        "score": score,
        "job_url": job.get("job_link"),
        "recipient": job.get("boss_name"),
        "greeting": match_resume.build_greeting(job, resume),
    } for score, job in scored[:top]]


def try_send_resume_card(cdp):
    """若页面提供真实「发送简历」控件则点击；否则如实返回 ui-unavailable。"""
    clicked = cdp.eval_js(r"""(() => {
      const els=[...document.querySelectorAll('div,button,a,span')].filter(e=>{
        const t=(e.innerText||'').trim(); const r=e.getBoundingClientRect();
        const cls=String(e.className||'');
        return (t==='发送简历'||t==='投递简历'||t.includes('发送附件简历')) &&
               !cls.includes('unable') && !cls.includes('disabled') &&
               r.width>20 && r.height>10 && r.y>0 && r.y<window.innerHeight;
      });
      if(!els.length) return {ok:false, reason:'ui-unavailable'};
      els[0].click(); return {ok:true, label:(els[0].innerText||'').trim()};
    })()""")
    return clicked if isinstance(clicked, dict) else {"ok": False, "reason": "ui-unavailable"}


def verify_target(cdp, target):
    """预检只打开岗位页核对公司与职位，不点击「立即沟通」，避免产生真实会话。"""
    if not target.get("job_url") or not target.get("recipient"):
        return False, "缺少 job_url 或 recipient"
    targets = cdp.send("Target.getTargets")["result"]["targetInfos"]
    sid = None
    for t in targets:
        if t["type"] == "page" and "job_detail" in t.get("url", ""):
            sid = cdp.send("Target.attachToTarget",
                           {"targetId": t["targetId"], "flatten": True})["result"]["sessionId"]
            break
    if not sid:
        _tid, sid = boss.create_page_session(cdp, background=True)
    cdp.send("Page.navigate", {"url": target["job_url"]}, sid)
    time.sleep(5)
    text = cdp.eval_js("document.body.innerText||''", sid) or ""
    ok = target["recipient"] in text or str(target.get("title") or "") in text
    return bool(ok), ("DRY_RUN 页面校验通过（未沟通、未发送）" if ok else "页面信息与目标不匹配")


def apply_one(cdp, target, dry_run=False, send_resume=False):
    if dry_run:
        ok, msg = verify_target(cdp, target)
    else:
        ok, msg = auto_greet.send_one(
            cdp, target.get("job_url"), target.get("recipient"),
            target.get("greeting") or "", False,
        )
    result = {
        "job_id": target.get("job_id"),
        "title": target.get("title"),
        "recipient": target.get("recipient"),
        "score": target.get("score"),
        "ok": bool(ok), "msg": str(msg), "at": now(),
        "mode": "dry-run" if dry_run else "sent",
        "resume_card": "skipped",
    }
    if ok and not dry_run and send_resume:
        card = try_send_resume_card(cdp)
        result["resume_card"] = card.get("label") if card.get("ok") else card.get("reason")
    return result


def run_batch(targets, cdp_port=9222, state_path=DEFAULT_STATE, limit=3,
              gap_min=8, gap_max=15, dry_run=True, send_resume=False):
    st = load_state(state_path)
    st.update({
        "status": "running", "started_at": now(), "finished_at": "", "error": "",
        "mode": "dry-run" if dry_run else "sent", "progress": 0,
        "total": min(int(limit), len(targets)), "results": [],
    })
    save_state(state_path, st)
    cdp = boss.CDPSession(cdp_port)
    try:
        for i, target in enumerate(targets[:int(limit)], 1):
            key = str(target.get("job_id") or target.get("job_url") or "")
            if key in st["applied"]:
                continue
            try:
                res = apply_one(cdp, target, dry_run=dry_run, send_resume=send_resume)
            except Exception as exc:
                res = {"job_id": target.get("job_id"), "title": target.get("title"),
                       "recipient": target.get("recipient"), "ok": False,
                       "msg": f"异常：{exc}", "at": now(), "mode": st["mode"],
                       "resume_card": "skipped"}
            res["job_key"] = key
            st["results"].append(res)
            st["progress"] = i
            if res["ok"] and not dry_run:
                st["applied"][key] = {"title": res.get("title"),
                                      "recipient": res.get("recipient"), "at": res["at"]}
            if any(w in res.get("msg", "") for w in RISK_WORDS):
                st.update({"status": "blocked", "error": "检测到风控/验证码，已停止"})
                save_state(state_path, st)
                break
            save_state(state_path, st)
            if i < st["total"]:
                gap = random.uniform(gap_min, gap_max)
                time.sleep(gap)
        if st["status"] == "running":
            st["status"] = "ok"
    except Exception as exc:
        st.update({"status": "error", "error": str(exc)})
    finally:
        cdp.close()
        st["finished_at"] = now()
        save_state(state_path, st)
    return st


def main():
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--targets", help="JSON 数组，含 job_url/recipient/greeting")
    src.add_argument("--resume", help="parse_resume.py 产出的简历 JSON")
    ap.add_argument("--jobs", help="含 JD 的岗位主表 JSON（配合 --resume）")
    ap.add_argument("--top", type=int, default=10, help="打分后取前 N 个候选")
    ap.add_argument("--min-score", type=float, default=0)
    ap.add_argument("--cdp-port", type=int, default=9222)
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--limit", type=int, default=3, help="本次最多投递条数")
    ap.add_argument("--gap-min", type=float, default=8)
    ap.add_argument("--gap-max", type=float, default=15)
    ap.add_argument("--send-resume", action="store_true", help="页面存在控件时附带投简历")
    ap.add_argument("--dry-run", action="store_true", help="只预检不发送（默认请先用一次）")
    args = ap.parse_args()

    if args.targets:
        with open(args.targets, encoding="utf-8") as f:
            targets = json.load(f)
    else:
        if not args.jobs:
            ap.error("--resume 需要同时提供 --jobs")
        targets = build_targets(args.resume, args.jobs, args.top,
                                args.state, args.min_score)

    st = run_batch(targets, args.cdp_port, args.state, args.limit,
                   args.gap_min, args.gap_max, args.dry_run, args.send_resume)
    print(json.dumps({
        "status": st["status"], "mode": st["mode"], "progress": st["progress"],
        "total": st["total"], "error": st.get("error", ""),
        "results": st["results"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
