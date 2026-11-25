# 夜间巡检 Runbook（2026-11-25 23:40 — 2026-11-26 07:00）

本文件是自动化任务每一轮的执行手册。工作区、输出目录、命令都使用绝对路径，不要依赖当前 shell 的 cwd。

## 固定路径

- 工作区：`C:/Users/LiYaobin/WorkBuddy/2026-11-25-20-21-34`
- Skill：`C:/Users/LiYaobin/.workbuddy/skills/boss-zhipin-job-research`
- 输出：`C:/Users/LiYaobin/WorkBuddy/2026-11-25-20-21-34/output`
- 工作台：`http://127.0.0.1:8765/`
- Chrome CDP：`9222`

## 每轮顺序

1. **健康检查**
   - `GET /api/status`、`/api/refresh/status`、`/api/resume/status`、`/api/auto-apply/status`。
   - 服务未启动：用后台任务启动 `dashboard_server.py --port 8765 --cdp-port 9222 --dashboard "<输出>/岗位数据看板.html" --output-dir "<输出>" --interval-minutes 30 --apply-limit 3 --no-browser`，确认 8765 可访问；已有服务不要重复启动。
   - CDP 不可用：记录 `cdp_down`，继续离线检查，不要尝试重启/登录 Chrome。

2. **数据刷新**
   - 已登录且刷新未运行：检查 `last_ok_at`，超过 30 分钟则 `POST /api/refresh`，轮询 `/api/refresh/status` 最多 15 分钟（换关键词的那轮要抓 120 条新岗的 JD，通常 10-13 分钟）。
   - 关键词按 `<输出>/keywords.json` 轮换，每轮换一个词；`refresh_state.json` 里的 `keyword_index` 记录下一轮用哪个。要改采集范围就改 `keywords.json`，不用改命令行。
   - 判断本轮是否真的扩量：看 `/api/refresh/status` 的 `new_jobs` 与 `pool_by_keyword`，`new_jobs=0` 连续两轮就说明该词已抓透，可以换词或加页。
   - 刷新正在运行：等待结束；不要同时触发第二次。
   - 未登录：记录 `login_required`，跳过在线采集，不扫码、不绕过验证码。

3. **单轮巡检**
   - `python "<Skill>/scripts/patrol_once.py" --output-dir "<输出>" --base http://127.0.0.1:8765 --start-server`
   - 它会：落库 `jobs.db`、编译检查、API 冒烟、看板 marker 检查、更新 `简历优化建议.md`、写 `patrol/patrol_*.json` 和 `巡检日志.md`。
   - 不发送招呼、不启用/执行自动投递、不点「立即沟通」。
4. **数据核查**
   - 用 sqlite3 查 `jobs.db`：岗位总数、`is_ai=1` 数、`has_jd=1` 数、最新 `snapshots.ts`。
   - 异常标准：岗位 0、JD 覆盖率 <90%、最新快照时间未递增。
   - JD 覆盖率跌破 90% 时，先给缺失岗位单独补抓（`fetch_missing_details.py --jobs <缺失子集> --details <已有JD种子>`），再回写主表并重建看板/趋势/落库；修不好要记录，不要静默。
   - 主表是并集（当轮 + 上轮余量），岗位总数只增不减属正常；「岗位数变少」才要查是不是 carry 失效。

5. **发现问题 -> GitHub Issue**
   - 先查是否已有同标题 open issue（脚本自动去重）。
   - 创建：
     `python "<Skill>/scripts/report_issue.py" --output-dir "<输出>" --title "巡检：<现象>" --labels patrol,bug --body "<现象/根因/影响/证据>"`
   - 修复并回归后关闭：
     `python "<Skill>/scripts/report_issue.py" --output-dir "<输出>" --title "巡检：<现象>" --close --fixed-by "<commit>"`
   - issue 里不要写简历、手机号、Cookie、token、完整 JD 里的个人信息。
   - 如果是改进项用 `--labels patrol,enhancement`；需要人工确认加 `needs-review`。

6. **修复与提交**
   - 修改代码/文档后：`python -m compileall -q "<Skill>/scripts"`，重跑相关接口/页面检查。
   - `git -C "<Skill>" add` 只加代码与文档；`git commit`、`git push origin main`。
   - 绝不提交 `output/`、`resume.json`、`targets.json`、`jobs.db`、`patrol/`、截图、state、`.env`。
   - 每个修复对应一个 issue，commit message 建议带 issue 编号，例如 `Fix login hang (#2)`。

7. **记录**
   - `patrol_once.py` 已写 JSON 日志；在 `output/巡检日志.md` 追加 2-3 行本轮结论（不要重复大段）。
   - 每轮运行 `python "<Skill>/scripts/nightly_report.py" --output-dir "<输出>"` 更新报告。
   - 当前时间 >= 2026-11-26 06:30 时，必须确保最终版：
     - `output/夜间巡检报告_20261126.md`
     - `output/优化方案_20261126.md`

## 硬性禁止

- 不发送任何招呼、不启用或执行自动投递、不点「立即沟通」。
- 不把简历、岗位数据、DB、state、截图上传 GitHub。
- 不绕过验证码/风控；不删除用户文件；不修改用户原始简历。
- 不创建新的自动化任务（避免递归）。
- 某轮失败要记录错误并在下一轮重试，不要静默跳过。

## 本轮完成标准

- 有一份 `patrol_*.json`，`status=ok` 或错误已记录。
- `jobs.db` 可查询，快照时间递增（或明确记录为何未递增）。
- 看板/接口/编译检查通过；发现的问题有 issue 链接与修复记录。
- `巡检日志.md` 与 `夜间巡检报告_20261126.md` 已更新。
