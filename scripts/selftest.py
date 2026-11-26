#!/usr/bin/env python3
"""工作台端到端自检（可反复跑，作为每轮优化的回归闸门）。

覆盖的产品维度
--------------
- 正常库：facets 计数、筛选计数、导出条数三者一致；城市别名；排序空值；
- 异常输入：不存在的城市、乱传 sort、缺参数；
- 空库：0 条时页面/导出/接口都不能崩，不能出现 NaN；
- 缺主表：接口返回结构化错误而不是挂掉；
- 并发：重建看板的同时持续读取，读到的 HTML 必须完整（原子写）。

跑法：
    python scripts/selftest.py            # 全绿退出码 0，有失败退出码 1
    python scripts/selftest.py -v         # 额外打印每项耗时
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import jobquery  # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    flag = "PASS" if cond else "FAIL"
    print(f"[{flag}] {name}" + (f" · {detail}" if detail else ""), flush=True)
    return bool(cond)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def make_jobs(n=6):
    """构造一批边界样本：港澳台、面议、无 JD、缺行业、重复公司。"""
    base = dict(salary="20-30K", company_scale="100-499人", company_industry="互联网",
                boss_active_status="刚刚活跃", job_labels="3-5年 | 本科",
                skills="大模型 | 数据分析", welfare="双休 | 五险一金",
                source_keyword="AI 产品经理", jd="负责大模型产品落地",
                first_seen="2026-11-26 06:00:00", last_seen="2026-11-26 12:00:00",
                boss_name="某某科技", company_link="https://example.com/c",
                job_link="https://example.com/j")
    rows = []
    for i in range(n):
        r = dict(base)
        r["job_id"] = f"job{i}"
        r["title"] = f"AI 产品经理 {i}"
        r["location"] = "深圳·南山区"
        rows.append(r)
    rows[0].update(job_id="hk", location="香港·中西区", salary="40-60K")
    if len(rows) > 1:
        rows[1].update(job_id="nojd", jd="", location="北京·朝阳区", salary="面议")
    if len(rows) > 2:
        rows[2].update(job_id="nodata", title="售前工程师", company_industry="",
                       welfare="", skills="客户沟通", jd="", salary="300-500元/天")
    return rows


def write_master(d, jobs):
    with open(os.path.join(d, jobquery.MASTER_NAME), "w", encoding="utf-8") as f:
        json.dump({"scraped_at": "2026-11-26 12:00:00", "jobs": jobs}, f, ensure_ascii=False)


def get(url, timeout=20):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 0, str(e).encode()


def get_json(url):
    code, body = get(url)
    try:
        return code, json.loads(body.decode("utf-8"))
    except Exception:
        return code, None


class Server:
    def __init__(self, out, dash, port):
        self.out, self.dash, self.port = out, dash, port
        self.p = None

    def start(self):
        self.p = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "dashboard_server.py"),
             "--port", str(self.port), "--cdp-port", "19222",
             "--dashboard", self.dash, "--output-dir", self.out,
             "--interval-minutes", "0", "--no-browser"],
            cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        deadline = time.time() + 30
        while time.time() < deadline:
            code, d = get_json(f"http://127.0.0.1:{self.port}/api/status")
            if code == 200 and isinstance(d, dict):
                return True
            if self.p.poll() is not None:
                print((self.p.stdout.read() or b"").decode("utf-8", "replace")[:800])
                return False
            time.sleep(0.4)
        return False

    def stop(self):
        if self.p and self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait(timeout=5)

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"


def build_dashboard(out):
    dash = os.path.join(out, "岗位数据看板.html")
    rc = subprocess.run([sys.executable, os.path.join(HERE, "generate_dashboard.py"),
                         "--input", os.path.join(out, jobquery.MASTER_NAME),
                         "--output", dash],
                        capture_output=True, text=True)
    if rc.returncode != 0:
        print(rc.stdout[-600:], rc.stderr[-600:])
    return dash if rc.returncode == 0 else ""


def export_rows(srv, query):
    code, body = get(srv.url("/api/export.xlsx?" + query), timeout=60)
    if code != 200 or len(body) < 1000:
        return None
    tmp = os.path.join(tempfile.mkdtemp(), "e.xlsx")
    with open(tmp, "wb") as f:
        f.write(body)
    try:
        import openpyxl
        wb = openpyxl.load_workbook(tmp)
        return wb["岗位明细"].max_row - 1
    except Exception:
        return None


def phase_normal(verbose):
    print("\n== 正常库 ==")
    d = tempfile.mkdtemp(prefix="wbtest_")
    jobs = make_jobs(6)
    write_master(d, jobs)
    dash = build_dashboard(d)
    if not check("看板生成", bool(dash)):
        return
    srv = Server(d, dash, free_port())
    if not check("服务启动", srv.start()):
        return
    try:
        code, d1 = get_json(srv.url("/api/facets"))
        f = (d1 or {}).get("facets") or {}
        check("facets 计数一致", code == 200 and d1.get("count") == len(jobs),
              f"{d1.get('count') if d1 else '-'} vs {len(jobs)}")
        check("facets 港澳台归一", (f.get("city") or {}).get("中国香港") == 1,
              str(list((f.get("city") or {}).items())[:3]))
        check("facets 带福利项", "双休" in (f.get("welfare") or {}), "")

        # 每个筛选条件：接口计数 == 导出条数 == 期望值
        cases = [
            ("", 6),
            ("?q=%E5%8F%8C%E4%BC%91", 5),
            ("?q=%E6%B7%B1%E5%9C%B3", 4),
            ("?city=%E9%A6%99%E6%B8%AF", 1),
            ("?city=%E4%B8%AD%E5%9B%BD%E9%A6%99%E6%B8%AF", 1),
            ("?city=%E7%81%AB%E6%98%9F", 0),
            ("?ai=1", 5),
            ("?sort=salary&dir=asc", 6),
            ("?sort=bogus", 6),
        ]
        for q, expect in cases:
            url = "/api/jobs?" + (q[1:] + "&" if q else "") + "count=1"
            code, d2 = get_json(srv.url(url))
            got = (d2 or {}).get("count")
            rows = export_rows(srv, q[1:] if q else "")
            ok = code == 200 and got == expect and rows == expect
            check(f"筛选 {q or '(全部)'}", ok, f"api={got} export={rows} 期望={expect}")

        # 排序：无论升降序，没有中位数的薪资（面议/日薪）必须排在所有已知薪资之后
        for direction in ("asc", "desc"):
            code, body = get(srv.url(f"/api/jobs?sort=salary&dir={direction}"))
            lst = (json.loads(body.decode("utf-8")) or {}).get("jobs") or []
            unknown_at = [i for i, j in enumerate(lst)
                          if jobquery.salary_mid(j.get("salary")) is None]
            check(f"{direction} 序未知薪资垫底",
                  bool(unknown_at) and min(unknown_at) >= len(lst) - len(unknown_at),
                  f"空值位置={unknown_at} 共{len(lst)}条")

        # 原始接口形状
        code, body = get(srv.url("/api/jobs"))
        raw = json.loads(body.decode("utf-8"))
        check("/api/jobs 原始形状", isinstance(raw, dict) and len(raw.get("jobs") or []) == 6)

        # 并发读看板：重建 3 次，读 60 次，不能读到半截 HTML
        bad = []

        def reader():
            for _ in range(60):
                try:
                    with open(dash, encoding="utf-8") as f:
                        h = f.read()
                    if not h.rstrip().endswith("</html>"):
                        bad.append(len(h))
                except Exception as e:
                    bad.append(str(e)[:40])
                time.sleep(0.01)

        t = threading.Thread(target=reader)
        t.start()
        for _ in range(3):
            build_dashboard(d)
        t.join()
        check("并发重建不出现半截 HTML", not bad, f"异常 {len(bad)} 次")

        # 页面关键能力
        with open(dash, encoding="utf-8") as f:
            h = f.read()
        for marker in ("checkParity", "api/facets", "CITY_ALIAS", "导出 Excel"):
            check(f"页面含 {marker}", marker in h)
        check("空数据不产生 NaN", "NaN%" not in h and ">NaN" not in h, "")
    finally:
        srv.stop()
        shutil.rmtree(d, ignore_errors=True)


def phase_empty(verbose):
    print("\n== 空库（0 条） ==")
    d = tempfile.mkdtemp(prefix="wbtest_empty_")
    write_master(d, [])
    dash = build_dashboard(d)
    if not check("空库看板生成", bool(dash)):
        return
    with open(dash, encoding="utf-8") as f:
        _h = f.read()
        check("空库页面无 NaN", "NaN%" not in _h and ">NaN" not in _h)
    srv = Server(d, dash, free_port())
    if not check("空库服务启动", srv.start()):
        return
    try:
        code, d1 = get_json(srv.url("/api/facets"))
        check("空库 facets=0", code == 200 and (d1 or {}).get("count") == 0)
        code, d2 = get_json(srv.url("/api/jobs?count=1"))
        check("空库计数=0", code == 200 and (d2 or {}).get("count") == 0)
        check("空库导出 0 行", export_rows(srv, "") == 0)
    finally:
        srv.stop()
        shutil.rmtree(d, ignore_errors=True)


def phase_missing(verbose):
    print("\n== 缺主表 ==")
    d = tempfile.mkdtemp(prefix="wbtest_missing_")
    dash = build_dashboard(d)  # 没有主表，生成应失败
    check("缺主表时生成失败但不崩溃", dash == "")
    if not dash:
        # 用一个占位 HTML 起服务，验证接口降级
        dash = os.path.join(d, "岗位数据看板.html")
        with open(dash, "w", encoding="utf-8") as f:
            f.write("<!DOCTYPE html><html><body>placeholder</body></html>")
    srv = Server(d, dash, free_port())
    if not check("缺主表服务仍能启动", srv.start()):
        return
    try:
        code, body = get(srv.url("/api/jobs?count=1"))
        d3 = json.loads(body.decode("utf-8")) if code in (200, 500) else None
        check("缺主表计数返回结构化错误", code in (200, 500) and isinstance(d3, dict)
              and d3.get("ok") is not True, f"http={code}")
        code, body = get(srv.url("/api/facets"))
        d4 = json.loads(body.decode("utf-8")) if code in (200, 500) else None
        check("缺主表 facets 降级", isinstance(d4, dict) and d4.get("ok") is not True,
              f"http={code}")
        code, body = get(srv.url("/"))
        check("缺主表页面仍可访问", code == 200 and len(body) > 10)
    finally:
        srv.stop()
        shutil.rmtree(d, ignore_errors=True)


def phase_hostile():
    """脏数据与恶意输入：超长 JD、公式、伪协议、重复 id、坏请求。"""
    print("\n== 脏数据 / 恶意输入 ==")
    d = tempfile.mkdtemp(prefix="wbtest_hostile_")
    long_jd = "岗位职责：" + ("负责大模型产品设计与落地。" * 3000)   # 约 4 万字
    jobs = [
        dict(job_id="long", title="=SUM(A1:A2)", salary="20-30K", location="深圳·南山区",
             boss_name="某某科技", company_scale="100-499人", company_industry="互联网",
             boss_active_status="刚刚活跃", job_labels="3-5年 | 本科", skills="大模型",
             welfare="双休", source_keyword="AI 产品经理", jd=long_jd,
             first_seen="2026-11-26 06:00:00", last_seen="2026-11-26 06:00:00",
             job_link="javascript:alert(1)", company_link="javascript:alert(2)"),
        dict(job_id="dup", title="重复岗 A", salary="10-15K", location="北京·朝阳区",
             boss_name="甲公司", company_industry="互联网", boss_active_status="在线",
             job_labels="1-3年 | 本科", skills="SQL", welfare="双休",
             source_keyword="B端产品经理", jd="短 JD",
             first_seen="2026-11-26 06:00:00", last_seen="2026-11-26 06:00:00"),
        dict(job_id="dup", title="重复岗 B", salary="10-15K", location="北京·朝阳区",
             boss_name="甲公司", company_industry="互联网", boss_active_status="在线",
             job_labels="1-3年 | 本科", skills="SQL", welfare="双休",
             source_keyword="B端产品经理", jd="更完整的 JD" * 10,
             first_seen="2026-11-26 06:00:00", last_seen="2026-11-26 07:00:00"),
    ]
    write_master(d, jobs)
    dash = build_dashboard(d)
    if not check("脏数据看板生成", bool(dash)):
        return
    with open(dash, encoding="utf-8") as f:
        h = f.read()
    # 数据 JSON 里当然还有原始 job_link，只检查真正进 href 的那一处
    check("伪协议链接被拦", 'href="javascript:' not in h and "safeUrl" in h, "")

    srv = Server(d, dash, free_port())
    if not check("脏数据服务启动", srv.start()):
        return
    try:
        code, d1 = get_json(srv.url("/api/facets"))
        check("重复 job_id 去重", (d1 or {}).get("count") == 2, f"count={(d1 or {}).get('count')}")

        code, body = get(srv.url("/api/export.xlsx"))
        tmp = os.path.join(d, "e.xlsx")
        with open(tmp, "wb") as f:
            f.write(body)
        import openpyxl
        ws = openpyxl.load_workbook(tmp)["岗位明细"]
        check("导出不因超长 JD 失败", ws.max_row - 1 == 2, f"rows={ws.max_row - 1}")
        jd_cell = ws.cell(row=2, column=20).value or ""
        check("超长 JD 主动截断并标注", len(jd_cell) <= 32767 and "已截断" in jd_cell,
              f"len={len(jd_cell)}")
        tcell = ws.cell(row=2, column=2)
        check("公式型标题按文本写", tcell.data_type == "s" and tcell.value == "=SUM(A1:A2)",
              f"type={tcell.data_type}")

        code, body = get(srv.url("/api/nope"))
        check("未知路由返回 404", code == 404)

        # 坏 JSON 的 POST 不能把服务打挂
        req = urllib.request.Request(srv.url("/api/refresh"), data=b"{not json",
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception:
            code = 0
        code2, _ = get(srv.url("/api/status"))
        check("坏 JSON 请求不致命", code in (200, 400) and code2 == 200, f"post={code}")

        # 并发计数：10 个请求结果必须一致
        outs = []
        def hit():
            c, dd = get_json(srv.url("/api/jobs?count=1"))
            outs.append((c, (dd or {}).get("count")))
        ts = [threading.Thread(target=hit) for _ in range(10)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        check("并发计数一致", len(set(outs)) == 1 and outs[0][1] == 2, str(set(outs)))
    finally:
        srv.stop()
        shutil.rmtree(d, ignore_errors=True)


def phase_workbench(verbose):
    print("\n== 启动器静态检查 ==")
    import workbench
    check("port_pid 可调用", callable(workbench.port_pid))
    # 坏 state 文件不能让启动器崩
    d = tempfile.mkdtemp(prefix="wbtest_state_")
    with open(os.path.join(d, ".workbench_state.json"), "w", encoding="utf-8") as f:
        f.write("{ broken")
    try:
        st = workbench.load_state(d)
        check("坏 state 文件降级为空", st == {}, str(st)[:40])
    except Exception as e:
        check("坏 state 文件降级为空", False, str(e)[:60])
    shutil.rmtree(d, ignore_errors=True)
    # 端口探测能认出真实监听者（用当前 8765 若有）
    if workbench.server_up():
        pid = workbench.port_pid(workbench.LOCAL_PORT)
        check("port_pid 找到监听进程", bool(pid), f"pid={pid}")
    else:
        check("port_pid 找到监听进程", True, "8765 未运行，跳过")
    p = subprocess.run([sys.executable, os.path.join(HERE, "workbench.py"), "--help"],
                       capture_output=True, text=True)
    check("workbench --help", p.returncode == 0 and "watchdog" in p.stdout)


def phase_js():
    print("\n== 页面 JS 语法 ==")
    node = shutil.which("node")
    if not node:
        check("node 可用（跳过 JS 校验）", True, "未安装 node")
        return
    d = tempfile.mkdtemp(prefix="wbtest_js_")
    write_master(d, make_jobs(2))
    dash = build_dashboard(d)
    if not dash:
        check("JS 校验前置（看板生成）", False)
        return
    with open(dash, encoding="utf-8") as f:
        h = f.read()
    import re
    scripts = re.findall(r"<script>(.*?)</script>", h, re.S)
    js = os.path.join(d, "bundle.js")
    with open(js, "w", encoding="utf-8") as f:
        f.write("\n".join(scripts))
    r = subprocess.run([node, "--check", js], capture_output=True, text=True)
    check("JS 语法", r.returncode == 0, (r.stderr or "")[:160])
    shutil.rmtree(d, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    t0 = time.time()
    phase_normal(args.verbose)
    phase_empty(args.verbose)
    phase_missing(args.verbose)
    phase_hostile()
    phase_workbench(args.verbose)
    phase_js()

    ok = sum(1 for _n, c, _d in RESULTS if c)
    bad = [n for n, c, _d in RESULTS if not c]
    print(f"\n合计 {len(RESULTS)} 项：通过 {ok}，失败 {len(bad)}，耗时 {time.time() - t0:.1f}s")
    if bad:
        print("失败项：" + "；".join(bad))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
