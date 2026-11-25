#!/usr/bin/env python3
"""把巡检发现的问题记录到 GitHub Issues，并在修复后关闭。

幂等：同一标题已有 open issue 时只追加评论，不重复创建。
无 gh / 未登录时退回本地 output/issues.jsonl，不阻塞巡检。

用法：
  python report_issue.py --title "登录按钮卡住" --body "..." --labels patrol,bug
  python report_issue.py --title "登录按钮卡住" --close --fixed-by "abc1234"
"""
import argparse
import datetime
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LABELS = {
    "patrol": ("5319e7", "夜间巡检自动记录"),
    "bug": ("d73a4a", "可复现的功能错误"),
    "enhancement": ("a2eeef", "改进项"),
    "auto-fix": ("0e8a16", "巡检代理已修复"),
    "needs-review": ("fbca04", "需要人工复核"),
}


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def gh(args, timeout=60):
    return subprocess.run(["gh"] + args, cwd=ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)


def ensure_labels():
    for name, (color, desc) in LABELS.items():
        p = gh(["label", "create", name, "--color", color, "--description", desc])
        # 已存在会返回非 0，忽略即可


def list_open():
    p = gh(["issue", "list", "--state", "open", "--limit", "100",
            "--json", "number,title,url"])
    if p.returncode != 0:
        return []
    try:
        return json.loads(p.stdout or "[]")
    except Exception:
        return []


def repo_slug():
    p = gh(["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"])
    return (p.stdout or "").strip() if p.returncode == 0 else ""


def rest_issue(action, number=None, title="", body="", labels=None):
    """gh issue 的 GraphQL 偶发失败时，退回 REST API。"""
    slug = repo_slug()
    if not slug:
        return None
    path = f"repos/{slug}/issues" + (f"/{number}" if number else "")
    if action == "create":
        cmd = ["api", "-X", "POST", path, "-f", f"title={title}", "-f", f"body={body}"]
        for lab in labels or []:
            cmd += ["-f", f"labels[]={lab}"]
    elif action == "comment":
        cmd = ["api", "-X", "POST", path + "/comments", "-f", f"body={body}"]
    elif action == "close":
        cmd = ["api", "-X", "PATCH", path, "-f", "state=closed"]
    else:
        return None
    return gh(cmd)


def record(output_dir, payload):
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "issues.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", required=True)
    ap.add_argument("--body", default="")
    ap.add_argument("--labels", default="patrol,bug")
    ap.add_argument("--output-dir", default=os.path.join(ROOT, "output"))
    ap.add_argument("--close", action="store_true", help="关闭同标题的 open issue")
    ap.add_argument("--fixed-by", default="", help="修复 commit / PR")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    ensure_labels()
    issues = list_open()
    hit = next((x for x in issues if x.get("title", "").strip() == args.title.strip()), None)
    payload = {"at": now(), "title": args.title, "labels": args.labels}
    if args.dry_run:
        payload.update({"action": "dry-run", "existing": hit.get("url") if hit else ""})
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.close:
        if not hit:
            payload.update({"action": "close-miss", "msg": "没有同标题 open issue"})
        else:
            body = f"巡检回归通过，修复已合入。\n\n修复记录：{args.fixed_by or '见最近提交'}\n时间：{now()}"
            p = gh(["issue", "close", str(hit["number"]), "--comment", body])
            if p.returncode != 0:
                rest_issue("comment", hit["number"], body=body)
                p = rest_issue("close", hit["number"])
            payload.update({"action": "closed" if p is not None and p.returncode == 0 else "close-failed",
                            "url": hit.get("url"), "error": ((p.stderr if p else "") or "")[-300:]})
        record(args.output_dir, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    body = args.body.strip() or "巡检发现异常，待补充复现信息。"
    body += f"\n\n---\n- 来源：夜间巡检\n- 时间：{now()}\n- 环境：Windows / Chrome CDP 9222 / 工作台 8765\n"
    labels = [x.strip() for x in args.labels.split(",") if x.strip()]
    if hit:
        p = gh(["issue", "comment", str(hit["number"]),
                "--body", f"巡检再次发现同类问题（{now()}）：\n\n{body}"])
        if p.returncode != 0:
            p = rest_issue("comment", hit["number"],
                           body=f"巡检再次发现同类问题（{now()}）：\n\n{body}")
        payload.update({"action": "commented" if p is not None and p.returncode == 0 else "comment-failed",
                        "url": hit.get("url"), "number": hit["number"],
                        "error": ((p.stderr if p else "") or "")[-300:]})
    else:
        cmd = ["issue", "create", "--title", args.title, "--body", body]
        for lab in labels:
            cmd += ["--label", lab]
        p = gh(cmd)
        if p.returncode != 0:
            p = rest_issue("create", title=args.title, body=body, labels=labels)
        out = (p.stdout if p else "") or (p.stderr if p else "") or ""
        try:
            created = json.loads(out)
            url = created.get("html_url") or created.get("url") or out.strip()
        except Exception:
            url = out.strip()
        payload.update({"action": "created" if p is not None and p.returncode == 0 else "create-failed",
                        "url": url, "output": out.strip()[-500:]})
    record(args.output_dir, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
