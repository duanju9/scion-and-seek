# Scion & Seek

> 一个嫁接在 BOSS 直聘上的岗位研究工作台。名字是 **scion（接穗）+ hide-and-seek（捉迷藏）** 的谐音：把一段简历接到岗位上，再去找它。和招聘平台本身没有关系。

项目地址：<https://github.com/duanju9/scion-and-seek>

通过 Chrome DevTools Protocol 复用**你自己已登录的专用 Chrome**，完成岗位列表与 JD 采集、历史快照、行业趋势、简历匹配、可视化看板、本地会话查看，以及可选的沟通式投递。

> 本项目不提供账号、不绕过验证码、不构造带 Cookie 的接口请求。所有浏览器动作都发生在你本人登录的专用 Chrome 中。请遵守目标网站的服务条款，仅用于个人求职研究。仓库公开的是代码与提示词，个人简历、岗位数据、数据库和日志不会提交。

## 功能

- **岗位采集**：关键词 + 城市 + 多页列表；只补缺失 JD，40 字短 JD 不丢弃。
- **主表交付**：20 字段 JSON/CSV，含 JD 全文、公司规模/行业/活跃度、岗位链接。
- **可视化看板**：单文件 HTML，离线可看；岗位搜索/筛选/排序/JD 展开；趋势、技能、行业分布。
- **持续更新**：默认 30 分钟刷新一次；时间戳快照保留 60 期；趋势与行业环比自动重算。
- **本地工作台**：HTTP 8765 提供登录引导、对话记录、岗位浏览、刷新状态、简历上传、自动投递开关。
- **简历匹配**：PDF/DOCX/TXT/MD 本地解析；技能 60% + 薪资 15% + 经验 10% + 学历 10% + 活跃 5%；输出匹配报告与定制招呼语。
- **自动投递（可选）**：按 `job_id` 去重、随机间隔、发送前后校验收件人；测试简历拒绝启用；遇风控立即停止。
- **趋势修正**：四个指标各自独立纵轴的分面小图，避免不同量纲归一后线条重合误读。

## 用 Agent 直接启动

仓库已经为不同 Agent 准备好入口，复制提示词即可使用：

| Agent | 入口 |
| --- | --- |
| Claude Code | `CLAUDE.md` |
| OpenAI Codex | `AGENTS.md` |
| Cursor | `.cursor/rules/boss-jobs.mdc` |
| Gemini CLI | `GEMINI.md` |
| GitHub Copilot Agent | `.github/copilot-instructions.md` |
| WorkBuddy / 其他 | `prompts/universal-agent-prompt.md` |

最小提示词：

```text
读 AGENTS.md 和 SKILL.md，按里面的流程帮我采集 BOSS 直聘「AI 工程师」岗位并生成看板。
输出到 <OUT>。登录我自己来，其他你完成。缺 JD 要补全再交付。
```

要直接分享给别人，用 `prompts/shareable-prompt.md`：里面自带仓库地址、克隆命令和完整边界说明。更多提示词见 `prompts/README.md`。

## 快速开始

### 1. 安装依赖

```bash
python -m pip install requests websocket-client pypdf python-docx
```

### 2. 启动专用 Chrome

```bash
python scripts/boss_cdp_raw.py --setup-chrome --cdp-port 9222 --no-wait-login
```

在打开的专用 Chrome 中手动完成 BOSS 直聘登录（扫码或验证码）。登录态保存在 `~/.boss-zhipin-scraper/chrome-profile`，不会读取你主浏览器的 Cookie。

### 3. 采集与补 JD

```bash
python scripts/boss_cdp_raw.py --keyword "AI 工程师" --city 100010000 --pages 8 \
  --no-detail --output out/boss_jobs.json --cdp-port 9222

python scripts/fetch_missing_details.py --jobs out/boss_jobs.json \
  --output out/boss_details.json --cdp-port 9222 --gap-min 2.5 --gap-max 5

python scripts/merge_to_master.py --jobs out/boss_jobs.json --details out/boss_details.json \
  --out-json out/岗位公司信息_含JD.json --out-csv out/岗位公司信息_含JD.csv
```

### 4. 多关键词轮换与 JD 兜底

单关键词只能看到一类岗位，`refresh_data.py` 支持按轮次轮换关键词。把轮换顺序写进 `out/keywords.json`，服务重启后也会继续沿用：

```json
{ "keywords": ["AI 工程师", "AI 产品经理", "解决方案工程师", "售前工程师", "B端产品经理"] }
```

- 每轮只抓其中一个词，`merge_to_master.py` 按 `job_id` 与上期主表取并集，`last_seen` 超过 7 天（`--keep-days`）才淘汰，换词那一轮不会把别的岗位整表冲掉。
- 每行带 `source_keyword`（该岗位是被哪个词搜到的），看板头部会显示关键词分布。
- 第 3 步补抓有总超时，新增岗位一多会截断。合并后会按 `min_jd_coverage`（默认 0.9）检查 JD 覆盖率，不达标就只对缺失岗位再补一轮，补完重新合并；仍不齐时把 `jd_coverage` / `jd_missing` 写进 `refresh_state.json`，不再静默留缺口。

### 5. 生成看板

```bash
python scripts/generate_dashboard.py --input out/岗位公司信息_含JD.json \
  --output out/岗位数据看板.html --trends out/trends.json
```

### 6. 启动工作台

```bash
python scripts/dashboard_server.py --port 8765 --cdp-port 9222 \
  --dashboard out/岗位数据看板.html --output-dir out \
  --interval-minutes 30 --apply-limit 3 --no-browser
```

打开 <http://127.0.0.1:8765/>。

> 直接打开 HTML 文件只能看静态数据；简历上传、刷新、会话、投递都需要本地工作台服务。

## 自动投递的边界

BOSS 直聘网页端岗位页只有「立即沟通」，没有稳定的一键投递按钮；聊天工具栏的「发简历」是向 Boss 索要简历，不是求职者投递。因此本项目把「投递」定义为：

**立即沟通 + 校验收件人 + 发送基于简历真实技能的定制招呼语**（平台会同时展示你的在线简历）。

`--send-resume` 仅在页面确实出现可用的投递控件时才附带点击，否则如实记录 `ui-unavailable`，不谎报成功。自动投递默认关闭，需要上传真实简历后在页面显式启用；测试简历、验证码、操作频繁都会阻止或停止发送。

## 目录

```
scripts/
  boss_cdp_raw.py            上游采集器（MIT，见 LICENSE-upstream）
  fetch_missing_details.py   只补缺失 JD
  merge_to_master.py         合并主表
  parse_resume.py            简历解析
  match_resume.py            简历匹配与招呼语
  generate_dashboard.py      单文件看板
  dashboard_server.py        本地工作台服务
  refresh_data.py            持续刷新编排
  analyze_trends.py          快照与趋势
  auto_greet.py              单条/批量打招呼
  auto_apply.py              自动投递队列
  ingest_db.py               岗位主表/快照落 SQLite
  patrol_once.py             夜间巡检单轮（健康/刷新/落库/测试/简历建议）
  nightly_report.py          巡检报告与优化方案
  report_issue.py            巡检问题写入 GitHub Issue（幂等 + 修复后关闭）
references/
  architecture.md            架构图、刷新流程图、投递时序图
  workflow.md                参数速查与踩坑
  project-comparison.md      方案对比
```

## 巡检问题 -> GitHub Issue

巡检发现可复现问题时，先建 issue 再修，修复后关闭，形成可追溯记录：

```bash
python scripts/report_issue.py --title "巡检：登录按钮卡住" \
  --labels patrol,bug --body "现象、根因、影响范围"

# 修复并回归后
python scripts/report_issue.py --title "巡检：登录按钮卡住" \
  --close --fixed-by "<commit>"
```

同一标题已有 open issue 时只追加评论，不重复创建；GraphQL 失败会自动退回 REST API。没有 `gh` 或未登录时写入 `output/issues.jsonl`，不阻塞巡检。

## 已知限制

- 仅支持 Windows/macOS 上已安装 Chrome/Edge 的桌面环境；需要 Python 3.10+。
- 扫描件 PDF 需要先 OCR，否则无法解析文本。
- BOSS 页面结构可能变化；采集/会话选择器需要随页面调整。
- 高频采集或批量投递可能触发平台风控；本项目保留单会话、随机间隔、风控即停等技术约束。
- 自动刷新期间浏览器被采集器占用，工作台会暂缓会话读取；刷新完成后恢复。

## 许可

采集器来自 [`eatmoreduck/boss-zhipin-scraper`](https://github.com/eatmoreduck/boss-zhipin-scraper)，MIT 许可，见 `LICENSE-upstream`。本仓库其余编排脚本为个人自用工具，使用前请自行评估平台条款与账号风险。
