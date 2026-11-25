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
import re
import subprocess
import sys
import time

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


TRANSIENT = ("Something went wrong", "Server Error", "Internal Server Error",
             "502", "503", "504", "timeout", "timed out", "Connection reset",
             "connection reset", "EOF", "TLS handshake", "temporarily unavailable")
BACKOFF = (3, 8, 15)


def gh(args, timeout=60, stdin=None):
    return subprocess.run(["gh"] + args, cwd=ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, input=stdin)


def is_transient(p):
    if p is None or p.returncode == 0:
        return False
    text = ((p.stderr or "") + (p.stdout or ""))
    return any(k in text for k in TRANSIENT)


def gh_retry(args, tries=3, timeout=60, stdin=None, sleep=time.sleep):
    """临时性 5xx / 超时按退避重试；标签、权限类错误立即返回，不浪费重试。"""
    p = None
    for i in range(max(1, tries)):
        p = gh(args, timeout=timeout, stdin=stdin)
        if p.returncode == 0 or not is_transient(p) or i == max(1, tries) - 1:
            return p
        sleep(BACKOFF[min(i, len(BACKOFF) - 1)])
    return p


def ensure_labels():
    for name, (color, desc) in LABELS.items():
        p = gh(["label", "create", name, "--color", color, "--description", desc])
        # 已存在会返回非 0，忽略即可


def list_open():
    """列出 open issue。调用失败与「确实没有」必须分开，否则 --close 会静默落空（#15）。

    返回 (issues, error)：error 为空串表示调用成功。
    """
    p = gh(["issue", "list", "--state", "open", "--limit", "100",
            "--json", "number,title,url"])
    if p.returncode == 0:
        try:
            return json.loads(p.stdout or "[]"), ""
        except Exception as e:
            return [], f"解析 gh issue list 输出失败：{e}"
    # gh CLI 失败（非仓库目录、GraphQL 抖动、限流）时退回 REST 再找一次
    slug = repo_slug()
    if slug:
        q = gh(["api", f"repos/{slug}/issues?state=open&per_page=100"])
        if q.returncode == 0:
            try:
                data = json.loads(q.stdout or "[]")
                items = [{"number": x.get("number"), "title": x.get("title", ""),
                          "url": x.get("html_url", "")}
                         for x in data if isinstance(x, dict) and "pull_request" not in x]
                return items, ""
            except Exception as e:
                return [], f"解析 REST issues 失败：{e}"
        return [], ((q.stderr or "") or (p.stderr or "") or "gh issue list 失败").strip()[-300:]
    return [], ((p.stderr or "") or "gh issue list 失败").strip()[-300:]


def norm_title(s):
    """标题比对前规范化：全角冒号转半角、冒号两侧空白统一、连续空白折叠、忽略大小写。
    中文标题里「巡检：xxx」与「巡检: xxx」写法都出现过，不能靠 strip 精确比对（#15）。"""
    t = str(s or "").replace("：", ":").replace("\u3000", " ")
    t = re.sub(r"\s*:\s*", ":", t)
    return " ".join(t.split()).lower()


def find_open(issues, title):
    want = norm_title(title)
    for x in issues:
        if norm_title(x.get("title", "")) == want:
            return x
    return None


def repo_slug():
    p = gh(["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"])
    return (p.stdout or "").strip() if p.returncode == 0 else ""


def rest_issue(action, number=None, title="", body="", labels=None, tries=3):
    """gh issue 的 GraphQL 偶发失败时，退回 REST API。

    用 JSON 载荷（--input -）而不是 -f 键值，避免 labels 数组被当成字符串传坏。
    """
    slug = repo_slug()
    if not slug:
        return None
    path = f"repos/{slug}/issues" + (f"/{number}" if number else "")
    if action == "create":
        payload = {"title": title, "body": body, "labels": labels or []}
        cmd = ["api", "-X", "POST", path, "--input", "-"]
    elif action == "comment":
        payload = {"body": body}
        cmd = ["api", "-X", "POST", path + "/comments", "--input", "-"]
    elif action == "close":
        payload = {"state": "closed"}
        cmd = ["api", "-X", "PATCH", path, "--input", "-"]
    else:
        return None
    return gh_retry(cmd, tries=tries,
                    stdin=json.dumps(payload, ensure_ascii=False))


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
    ap.add_argument("--retries", type=int, default=3,
                    help="临时性 5xx/超时的重试次数（退避 3/8/15 秒）")
    args = ap.parse_args()

    ensure_labels()
    issues, list_err = list_open()
    hit = find_open(issues, args.title)
    payload = {"at": now(), "title": args.title, "labels": args.labels}
    if list_err:
        payload["list_error"] = list_err
    if args.dry_run:
        payload.update({"action": "dry-run", "existing": hit.get("url") if hit else "",
                        "open_count": len(issues)})
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.close:
        if not hit:
            if list_err:
                payload.update({"action": "close-miss",
                                "msg": f"无法确认 open issue 列表：{list_err}",
                                "open_count": len(issues)})
            else:
                titles = [x.get("title", "")[:40] for x in issues[:5]]
                payload.update({"action": "close-miss",
                                "msg": f"没有同标题 open issue（当前 open {len(issues)} 条）",
                                "open_titles": titles})
        else:
            body = f"巡检回归通过，修复已合入。\n\n修复记录：{args.fixed_by or '见最近提交'}\n时间：{now()}"
            p = gh_retry(["issue", "close", str(hit["number"]), "--comment", body],
                         tries=args.retries)
            if p.returncode != 0:
                rest_issue("comment", hit["number"], body=body, tries=args.retries)
                p = rest_issue("close", hit["number"], tries=args.retries)
            payload.update({"action": "closed" if p is not None and p.returncode == 0 else "close-failed",
                            "url": hit.get("url"), "error": ((p.stderr if p else "") or "")[-300:]})
        record(args.output_dir, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    body = args.body.strip() or "巡检发现异常，待补充复现信息。"
    body += f"\n\n---\n- 来源：夜间巡检\n- 时间：{now()}\n- 环境：Windows / Chrome CDP 9222 / 工作台 8765\n"
    labels = [x.strip() for x in args.labels.split(",") if x.strip()]
    if hit:
        p = gh_retry(["issue", "comment", str(hit["number"]),
                      "--body", f"巡检再次发现同类问题（{now()}）：\n\n{body}"],
                     tries=args.retries)
        if p.returncode != 0:
            p = rest_issue("comment", hit["number"],
                           body=f"巡检再次发现同类问题（{now()}）：\n\n{body}",
                           tries=args.retries)
        payload.update({"action": "commented" if p is not None and p.returncode == 0 else "comment-failed",
                        "url": hit.get("url"), "number": hit["number"],
                        "error": ((p.stderr if p else "") or "")[-300:]})
    else:
        cmd = ["issue", "create", "--title", args.title, "--body", body]
        for lab in labels:
            cmd += ["--label", lab]
        p = gh_retry(cmd, tries=args.retries)
        if p.returncode != 0:
            p = rest_issue("create", title=args.title, body=body, labels=labels,
                           tries=args.retries)
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
