#!/usr/bin/env python3
"""补抓缺失的岗位详情 JD（复用 boss_cdp_raw 的提取逻辑）。

- 读取列表 JSON 与已有详情 JSON，找出尚无 JD 的 job。
- 单会话、低频（默认 4-8 秒间隔），逐条增量写盘，异常退出不丢数据。
- 只补缺失，不重复抓已有 JD。
"""
import argparse
import glob
import importlib.util
import json
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "boss_cdp_raw.py")
spec = importlib.util.spec_from_file_location("boss_cdp_raw", RAW)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

RESULT_DIR = os.path.expanduser("~/.boss-zhipin-scraper/job-result")


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def atomic_write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def find_latest(pattern):
    files = sorted(glob.glob(os.path.join(RESULT_DIR, pattern)))
    return files[-1] if files else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", default=find_latest("boss_jobs_*.json"))
    ap.add_argument("--details", default=find_latest("boss_details_*.json"))
    ap.add_argument("--output", default=os.path.join(RESULT_DIR, "boss_details_merged.json"))
    ap.add_argument("--cdp-port", type=int, default=9222)
    ap.add_argument("--gap-min", type=float, default=4.0)
    ap.add_argument("--gap-max", type=float, default=8.0)
    ap.add_argument("--limit", type=int, default=0, help="最多补抓多少条，0=全部")
    args = ap.parse_args()

    if not args.jobs or not os.path.exists(args.jobs):
        print("❌ 找不到列表文件")
        return 1

    jobs_data = load_json(args.jobs)
    jobs = jobs_data.get("jobs", []) if isinstance(jobs_data, dict) else jobs_data
    details = load_json(args.details) if args.details and os.path.exists(args.details) else []
    if isinstance(details, dict):
        details = details.get("details") or details.get("jobs") or []

    have = {str(d.get("job_id") or "") for d in details if d.get("jd")}
    missing = [j for j in jobs if j.get("job_id") not in have and j.get("job_link")]
    if args.limit:
        missing = missing[: args.limit]

    print(f"列表 {len(jobs)} 条，已有 JD {len(have)} 条，待补 {len(missing)} 条")
    print(f"写入: {args.output}\n")

    results = list(details)
    ok = fail = 0
    for i, job in enumerate(missing, 1):
        title = job.get("title", "")
        company = job.get("boss_name", "")
        link = job.get("job_link", "")
        print(f"[{i}/{len(missing)}] {company} - {title}", flush=True)
        ws = None
        tid = None
        try:
            ws = m.CDPSession(args.cdp_port)
            tid, sid = m.create_page_session(ws)
            url = m.build_detail_url(job)
            ws.send("Page.navigate", {"url": url}, sid)
            time.sleep(random.uniform(3, 6))

            for _ in range(random.randint(2, 4)):
                ws.eval_js("window.scrollBy(0,%d)" % random.randint(200, 500), sid)
                time.sleep(random.uniform(0.6, 1.4))

            val = ws.eval_js(m.EXTRACT_DETAIL_JS, sid)
            try:
                d = json.loads(val) if isinstance(val, str) else {"jd": "", "tags": []}
            except (ValueError, TypeError):
                d = {"jd": "", "tags": []}

            fields = m.extract_detail_fields(d, min_length=40)
            d["jd"] = fields["jd"]
            d["boss_active_status"] = m.resolve_boss_active_status(
                list_status=job.get("boss_active_status", ""),
                detail_status=fields["boss_active_status"],
            )
            rec = m.build_detail_record(job, d)
            results.append(rec)
            ok += 1
            print(f"  ✅ JD {len(d['jd'])} 字", flush=True)
        except m.DetailLoginRequiredError:
            print("  ❌ 登录态失效，停止补抓（需重新登录）", flush=True)
            break
        except Exception as exc:
            fail += 1
            print(f"  ⚠️ 跳过: {type(exc).__name__}: {exc}", flush=True)
        finally:
            try:
                if tid and ws:
                    ws.send("Target.closeTarget", {"targetId": tid})
                if ws:
                    ws.close()
            except Exception:
                pass

        atomic_write(args.output, results)
        gap = random.uniform(args.gap_min, args.gap_max)
        print(f"  等待 {gap:.1f}s\n", flush=True)
        time.sleep(gap)

    atomic_write(args.output, results)
    print(f"\n完成：新增 {ok} 条，跳过 {fail} 条，详情总计 {len(results)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
