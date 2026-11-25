# AGENTS.md

项目地址：https://github.com/duanju9/scion-and-seek

本文件是本仓库的通用 Agent 操作规范。Claude Code、Codex、Cursor、Gemini CLI、GitHub Copilot、WorkBuddy 等 Agent 进入仓库后先读本文件，再按任务读取 `README.md`、`SKILL.md` 和 `references/`。

## 项目是什么

一个个人自用的 BOSS 直聘岗位研究工作台：

- 通过 Chrome DevTools Protocol（CDP）复用**用户本人已登录的专用 Chrome**；
- 采集岗位列表与 JD，生成主表、快照、趋势和单文件看板；
- 本地 HTTP 工作台提供登录引导、会话记录、简历上传、刷新状态和可选的沟通式投递；
- 支持夜间巡检、SQLite 落库、GitHub Issue 闭环。

不提供账号、不收集密码、不绕过验证码、不构造带 Cookie 的接口请求。

## 开始前必须读

1. `README.md`：安装、快速开始、能力边界。
2. `SKILL.md`：完整工作流与踩坑，尤其是 JD 补抓、会话选择器、登录卡住、趋势图。
3. `references/nightly-patrol.md`：巡检 Runbook（只在巡检/长期运行时读）。
4. `references/architecture.md`：架构与数据流。
5. `references/workflow.md`：参数速查与故障对照。

## 环境约定

- Python 3.10+；依赖见 `requirements.txt`。
- Chrome/Edge + 专用 profile：`~/.boss-zhipin-scraper/chrome-profile`。
- CDP 端口：`9222`；工作台端口：`8765`。
- 所有脚本都在 `scripts/`，命令中的 `$S` 指 `<repo>/scripts`。
- Windows 用 `python`，macOS/Linux 可用 `python3`；路径包含空格时要加引号。
- 数据默认写到用户指定的输出目录，不要写进仓库；仓库 `.gitignore` 已排除 `output/`、`data/`、`*.db`、`resume.json`、`targets.json`、`state`、截图。

## 标准任务

### 任务 A：首次采集与看板

```bash
python "$S/boss_cdp_raw.py" --setup-chrome --cdp-port 9222 --no-wait-login
# 让用户在打开的专用 Chrome 中手动登录，Agent 不要索取密码
python "$S/boss_cdp_raw.py" --check --cdp-port 9222

python "$S/boss_cdp_raw.py" --keyword "AI 工程师" --city 100010000 --pages 8 \
  --no-detail --output "<out>/boss_jobs.json" --cdp-port 9222
python "$S/fetch_missing_details.py" --jobs "<out>/boss_jobs.json" \
  --details "<out>/boss_details.json" --cdp-port 9222 --gap-min 2.5 --gap-max 5
python "$S/merge_to_master.py" --jobs "<out>/boss_jobs.json" --details "<out>/boss_details.json" \
  --out-json "<out>/岗位公司信息_含JD.json" --out-csv "<out>/岗位公司信息_含JD.csv"
```

交付前必须校验 `has_jd` 覆盖率；低于 95% 回到补抓步骤。详情补抓要后台运行，不要设 10 分钟以内超时。

### 任务 B：工作台

```bash
python "$S/generate_dashboard.py" --input "<out>/岗位公司信息_含JD.json" \
  --output "<out>/岗位数据看板.html" --trends "<out>/trends.json"
python "$S/dashboard_server.py" --port 8765 --cdp-port 9222 \
  --dashboard "<out>/岗位数据看板.html" --output-dir "<out>" \
  --interval-minutes 30 --apply-limit 3 --no-browser
```

工作台必须通过 `http://127.0.0.1:8765/` 打开；直接打开 HTML 是静态预览，不能上传简历、刷新或打招呼。

### 任务 C：简历匹配

先让用户上传简历（PDF/DOCX/TXT/MD），不要猜技能：

```bash
python "$S/parse_resume.py" --input "<简历路径>" --output "<out>/resume.json"
python "$S/match_resume.py" --resume "<out>/resume.json" --jobs "<out>/岗位公司信息_含JD.json" \
  --top 10 --out-report "<out>/简历匹配报告.md" --out-targets "<out>/targets.json"
```

只给有简历证据的建议，不虚构经历；不修改用户原始简历。

### 任务 D：夜间巡检

严格按 `references/nightly-patrol.md`。核心命令：

```bash
python "$S/patrol_once.py" --output-dir "<out>" --base http://127.0.0.1:8765 --start-server
python "$S/ingest_db.py" --input "<out>/岗位公司信息_含JD.json" --db "<out>/jobs.db" \
  --trends "<out>/trends.json" --label patrol
python "$S/nightly_report.py" --output-dir "<out>"
```

### 任务 E：发现问题 -> Issue -> 修复

先建 Issue，再修，修完关闭：

```bash
python "$S/report_issue.py" --output-dir "<out>" --title "巡检：<现象>" \
  --labels patrol,bug --body "<现象/根因/影响/证据>"
python "$S/report_issue.py" --output-dir "<out>" --title "巡检：<现象>" \
  --close --fixed-by "<commit>"
```

Issue 中不要写简历、手机号、Cookie、token、完整 JD 里的个人信息。

## 硬性边界

- 登录只由用户本人在专用 Chrome 完成；不索取、不存储、不转交密码。
- 不绕过验证码、风控码、登录墙；遇到 `code 31/37` 立即停止。
- 不发送任何招呼、不启用/执行自动投递，除非用户在本轮明确要求。
- 自动投递只允许在真实简历、明确启用、单会话低频、发送前后校验收件人的前提下运行。
- 不把 `output/`、`resume.json`、`targets.json`、`jobs.db`、`patrol` 日志、截图、state、`.env` 提交到 Git。
- 不删除用户文件；不覆盖用户原始简历；不做未经确认的破坏性系统操作。
- 不创建新的自动化任务，除非用户明确要求。

## 完成标准

- 命令实际跑通，接口/页面有验证结果，不是“应该可以”。
- 数据有 `has_jd` 覆盖率；趋势快照时间递增；看板/工作台可打开。
- 代码改动通过 `python -m compileall -q scripts`。
- 问题有 Issue 链接与修复 commit；报告写到输出目录。
- 最终回复说明：做了什么、改了哪些文件、验证结果、剩余风险/下一步。
