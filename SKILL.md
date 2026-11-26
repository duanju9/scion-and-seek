---
name: boss-zhipin-job-research
description: 采集 BOSS 直聘岗位与公司信息，输出含职位描述（JD）全文的结构化主表与市场分析；读取用户个人简历（PDF/DOCX/TXT/MD）并按技能、经验、学历、薪资做岗位匹配，为匹配岗位生成定制招呼语，并支持单条与批量自动打招呼；提供本地联动工作台网页，把登录引导、对话记录、打招呼与岗位看板统一展示；支持历史快照、定时自动刷新、行业岗位趋势分析与持续自动投递（按简历匹配 Top 岗位、去重限速），数据持续更新。当用户要求抓取/搜索 BOSS 直聘岗位、公司、薪资、JD，做招聘市场分析、行业岗位趋势、竞品岗位调研、简历匹配/关键词对齐，上传简历，要求自动打招呼/投递开场白，或要一个可视化看板/工作台统一查看岗位与聊天记录时使用。基于 Chrome CDP 复用已登录会话，内置 JD 补抓、限速与覆盖率校验，避免"抓到岗位却没有 JD"的半成品交付。
agent_created: true
---

# BOSS 直聘岗位与公司信息采集

## 概览

把"登录 → 抓列表 → 补 JD → 合并主表 → 分析 → 快照 → 趋势 → 看板"编排成一条可复用流水线。核心采集器来自开源项目 `eatmoreduck/boss-zhipin-scraper`（MIT，见 `LICENSE-upstream`），本 skill 增加编排脚本修复其默认行为导致的 JD 缺失与 CSV 无 JD 问题，实现历史快照、行业岗位趋势与工作台自动刷新，并固化所有踩坑经验。

**重要：这是编排层 skill，不要重写采集器。** 上游已维护跨平台、风控码、城市码表、增量写入，重复造轮子只会落后。

## 前置条件

1. Python 3.10+ 与 `pip install requests websocket-client pypdf python-docx`（或 `uv`）。后两者仅在读取简历时需要，DOCX 可零依赖降级解析；PDF 为扫描件时需先 OCR。
2. Google Chrome 或 Edge 已安装。
3. 专用隔离浏览器 profile：`~/.boss-zhipin-scraper/chrome-profile`，**不复制主浏览器 Cookie**。
4. 登录必须由用户本人在专用浏览器中手动完成（扫码或密码）。禁止索取、存储或转交用户密码；禁止使用他人账号或 Cookie 池。

网络受限环境若 `git clone github.com` 失败（HTTP/2 framing / CONNECT 502），改用 `https://ghproxy.net/https://raw.githubusercontent.com/<repo>/<branch>/<path>` 逐文件下载。

## 工作流（严格按顺序）

### 前置步骤：读取用户简历（打招呼/匹配场景必须先做）

用户要求"按我的简历匹配岗位/生成招呼语"时，**先让用户上传简历文件**，不要凭空猜技能。支持 PDF / DOCX / TXT / MD：

```bash
python "$S/parse_resume.py" --input "<用户上传的简历路径>" --output "<结果目录>/resume.json"
```

输出包含：技能列表（与 BOSS 标签对齐）、工作年限（优先取简历明确写的"N年经验"）、学历、求职方向、联系方式、分段时间原文。纯本地解析，不上传。扫描件 PDF 需提示用户先 OCR。

匹配打分并生成招呼语：

```bash
python "$S/match_resume.py" --resume "$S/../data/resume.json" \
  --jobs "<交付目录>/岗位公司信息_含JD.json" --top 10 \
  --out-report "<交付目录>/简历匹配报告.md" \
  --out-targets "<结果目录>/targets.json"
```

打分：技能重合 60% + 薪资 15% + 经验 10% + 学历 10% + Boss 活跃 5%，报告逐项列出分项得分与重合技能，可解释。招呼语只用简历中真实存在的技能，不虚构经历。`targets.json` 可直接喂给下面的 `auto_greet.py --batch-file`。

### 第 1 步：环境预检与登录

```bash
S="<本 skill 的 scripts 目录绝对路径>"
python "$S/boss_cdp_raw.py" --check --cdp-port 9222
```

若 CDP 不通，启动专用浏览器并**常驻**该后台任务（命令退出会回收浏览器进程）：

```bash
python "$S/boss_cdp_raw.py" --setup-chrome --cdp-port 9222 --no-wait-login
curl -sS -X PUT "http://127.0.0.1:9222/json/new?https%3A%2F%2Fwww.zhipin.com%2Fweb%2Fgeek%2Fjobs%3Fcity%3D100010000"
```

然后**只请求用户做登录这一件事**，等用户确认后再继续。用 `--check` 验证登录状态，不要相信脚本自带的 `wait_for_login`——它在空搜索响应时会误判失败。

若沙箱回收浏览器进程，用一个常驻任务（如 `tail -f /dev/null`）保持会话存活。

### 第 2 步：抓列表（不带详情）

列表抓取快，详情慢，必须分两步。默认 8 页约 120 条；页数上限 20。

```bash
python "$S/boss_cdp_raw.py" --keyword "AI 工程师" --city 100010000 --pages 8 \
  --no-detail --output "<结果目录>/boss_jobs.json" --cdp-port 9222
```

`--city` 支持中文或 9 位城市码，码表见 `assets/city_codes.json`；可用 `--list-cities 江` 查询。

### 第 3 步：补抓 JD（后台运行，切勿设短超时）

**关键**：上游详情抓取每条强制 10–25s 间隔 + 5–10s 加载 + 滚动，45 条需 20 分钟以上；任何 10 分钟以内的前台命令都会把它截断，产生"有岗位没 JD"的半成品。必须后台运行补抓脚本：

```bash
python "$S/fetch_missing_details.py" --jobs "<结果目录>/boss_jobs.json" \
  --details "<已有详情文件或留空>" --output "<结果目录>/boss_details.json" \
  --cdp-port 9222 --gap-min 2.5 --gap-max 5
```

该脚本只补缺失、按 `job_id` 增量写盘、阈值放宽到 40 字（上游 120 字会丢弃真实短 JD），单会话低频。**遇到 `DetailLoginRequiredError` 立即停止，请求用户重新登录。**

### 第 4 步：合并主表（交付核心）

上游列表 CSV 不含 JD 列，必须合并：

```bash
python "$S/merge_to_master.py" --jobs "<结果目录>/boss_jobs.json" \
  --details "<结果目录>/boss_details.json" \
  --out-csv "<交付目录>/岗位公司信息_含JD.csv" \
  --out-json "<交付目录>/岗位公司信息_含JD.json"
```

输出 20 字段：岗位、薪资、地点、公司规模/阶段/行业、技能福利、JD 全文、链接等，UTF-8 BOM 编码可直接用 Excel 打开。

### 第 5 步：覆盖率校验与分析

交付前必须校验 `has_jd` 字段：

```bash
python -c "import csv,sys; r=list(csv.DictReader(open(sys.argv[1],encoding='utf-8-sig'))); print(f'{len(r)} 条，含 JD {sum(1 for x in r if x[\"jd\"].strip())} 条')" "<交付目录>/岗位公司信息_含JD.csv"
```

覆盖率低于预期就回第 3 步补抓。**不要把缺 JD 的表当成品交付。**

生成分析报告：

```bash
python "$S/job_summary.py" --input "<结果目录>/boss_jobs.json" \
  --details "<结果目录>/boss_details.json" --top 20
```

### 第 6 步：可视化看板（可选）

用合并后的主表生成**单文件、离线可用**的 HTML 看板，含概览指标、9 张分布图、技能标签、可搜索/筛选/排序的岗位表与 JD 展开：

```bash
python "$S/generate_dashboard.py" \
  --input "<交付目录>/岗位公司信息_含JD.csv 对应的 .json" \
  --output "<交付目录>/岗位数据看板.html"
```

生成后用浏览器打开验证：卡片数、图表数、表格行数、搜索过滤与 JD 展开是否正常。

### 第 7 步：联动工作台（推荐，统一展示入口）

静态看板无法直接操作浏览器。用本地服务把**登录引导 + 对话记录 + 打招呼 + 岗位浏览**统一到一个页面。

**一键启动（推荐）**：不用记上面那串参数，双击交付目录里的 `启动工作台.bat` 即可。它会依次做四件事：

1. 检查 9222，没起就用**专用 profile** 拉起 Chrome（不动用户主 Chrome，登录态沿用 profile）；
2. 检查 8765，没起就后台启动 `dashboard_server.py`（自动刷新 + 定时落库）；
3. 轮询到 `/api/status` 返回后再打开页面，避免「打开就是未连接」；
4. 启动看护进程：服务挂了重拉、CDP 掉了重开、超过两个周期没刷新就催一次。

```bash
python "$S/workbench.py" start    --output-dir "<交付目录>" --watchdog
python "$S/workbench.py" status   --output-dir "<交付目录>"
python "$S/workbench.py" stop     --output-dir "<交付目录>"
python "$S/workbench.py" autostart --output-dir "<交付目录>"   # 开机静默自启（--off 取消）
```

看护只做三件事：拉起、重试、催刷新。**不发招呼、不开自动投递、不绕过验证码**；登录失效只记录并提示扫码。

```bash
python "$S/dashboard_server.py" --port 8765 --cdp-port 9222 \
  --dashboard "<交付目录>/岗位数据看板.html" \
  --output-dir "<交付目录>" --interval-minutes 30 \
  --resume "<结果目录>/resume.json" --apply-limit 3 --no-browser
# 打开 http://127.0.0.1:8765/ ；去掉 --no-browser 会自动打开
# --interval-minutes 0 可关闭自动刷新
# --resume 只配置简历；是否启用持续投递由页面上的开关决定
```

服务以**常驻后台任务**运行（命令退出会回收）。功能：
- 顶部登录状态条：绿点=已登录；未登录时点「打开登录页」引导用户在专用 Chrome 完成扫码/验证码。
- 简历栏：直接上传 PDF / DOCX / TXT / MD，本地解析技能/年限/学历并自动生成 Top 匹配；解析结果显示在栏内，`resume.json`、`简历匹配报告.md`、`targets.json` 落在输出目录，Agent 可直接读取分析。
- 对话面板：左侧会话列表、右侧消息气泡（区分我/对方/系统），点会话即时加载。
- 岗位表每行「打招呼」按钮：弹窗预填基于该岗位技能的招呼语，确认后发送并回写「已招呼」，自动刷新对话列表。
- 趋势区：岗位量/薪资/新增/活跃率**四个独立小图**（各用自身纵轴与真实数值标签，避免不同量纲归一后线条重叠误读）、技能与行业环比、本期新增岗位。
- 刷新栏：最后更新时间、下次自动刷新时间、即时进度；数据版本变更时前端自动重载。
- 自动投递栏：启用/关闭、立即投递、累计去重条数与进度；测试简历会被拒绝启用。
- 看板 HTML 每次请求重读，重新生成看板无需重启服务。

API：`/api/status`、`/api/login`、`/api/conversations`、`/api/messages?who=`、`/api/greet`、`/api/jobs`、`/api/trends`、`/api/refresh/status`、`/api/refresh`、`/api/auto-apply/status`、`/api/auto-apply`、`/api/auto-apply/config`、`/api/resume/status`、`/api/resume/upload`、`/api/resume/rematch`、`/api/facets`、`/api/export.xlsx`。浏览器类操作经 CDP；简历上传只落本地磁盘，不联网。

**查询与导出同一口径**：`jobquery.py` 是唯一实现，页面 JS、`/api/export.xlsx`、`/api/jobs?…` 都按它算。页面每次筛选后会拿同一组参数请求 `/api/jobs?count=1`，计数不一致就在「显示 N 条」处标红——两套实现一旦分叉会立刻暴露，而不是等导出后发现少了几条。`/api/facets` 给下拉选项（带条数，城市已按港澳台归一）。

**静态预览 vs 工作台**：`present_files` 打开的是静态 HTML 预览（端口 9083 或 file://），没有本地服务，所有 fetch 都会失败，页面因此提示「未连接到本地服务」。必须打开 `http://127.0.0.1:8765/` 才是可上传简历、可刷新、可打招呼的工作台。

**实现踩坑（务必保留）**：
1. HTTP 端口与 CDP 端口必须分开传（曾把 8765 误当 CDP 端口导致连不上 9222）。
2. 首页 HTML 必须按 bytes 返回；若把字符串传给 JSON 序列化函数，浏览器收到的是带引号的 JSON 文本，JS 不执行。
3. 前端 `render()` 用到的全局（如 `greeted`）必须在首次 `render()` 调用**之前**声明，否则触发暂时性死区。
4. 读会话消息要**强制点击**目标会话，并等待「详情头部 + 消息内容」都切换完成。BOSS 是头部先变、消息列表后变（实测可差 5 秒）；只等 `.text-content` 出现会把上一个会话的消息当成本会话，表现为「点每个会话内容都一样」。
5. 会话切换的可靠判据：消息列表最后一条要与左侧该会话的 `.last-msg-text` 一致，且连续两次轮询一致；附件/卡片消息没有 `.text-content`，要回退到 `li.innerText` 提取，否则永远匹配不上。
6. 列表项的原生鼠标点击有时被内部元素吞掉，点完必须检查 `.friend-content.selected` 是否包含目标名；未切换时用 DOM `.click()` 兜底。
7. 收件人校验不可省：发送前后各校验一次详情面板姓名区，防止误发。
8. 登录/状态接口不要 attach 页面：`/api/status` 在刷新中直接返回缓存登录态；`/api/login` 只 `Target.activateTarget` 或用 `/json/new` 新开标签（刷新中优先新开，避免抢采集器页面），并 `Browser.setWindowBounds(normal)` 把窗口置前，否则聊天标签卡死时按钮会永远停在「正在打开…」。前端所有 API 必须带 `AbortController` 超时（普通 15s、上传 180s），并在 `finally` 恢复按钮文字；登录成功文案保持 8 秒，不被轮询覆盖。
9. 自动刷新启动前先检查登录态；未登录不启动采集，避免采集器占着浏览器让用户打不开登录页。
10. 筛选/排序只允许在 `jobquery.py` 里实现。页面、导出、API 三处各写一套时，「城市叫香港还是中国香港」「q 搜不搜行业」「薪资按上限还是中位排序」必然分叉——页面 42 条、导出 39 条，看起来却都对。
11. 主表 2MB+，必须 `os.replace` 原子写；`json.dump` 直写期间读方会拿到半截 JSON（`JSONDecodeError`），表现为「偶尔打不开看板/导出失败」。
12. 调度器的 `next_at` 不能只按 `last_ok_at` 算，否则一轮失败后页面会显示一个过去的「下次时间」；失败后也不要每 30 秒重试，按 5 分钟退避。

### 第 8 步：持续更新与行业岗位趋势（工作台默认开启）

工作台启动后按 `--interval-minutes` 定时执行完整刷新；数据过期则启动即刷新一次。每次刷新把主表固化为时间戳快照，再计算跨期趋势。也可独立运行：

```bash
python "$S/refresh_data.py" --output-dir "<交付目录>" \
  --dashboard "<交付目录>/岗位数据看板.html" --cdp-port 9222 \
  --keywords "AI 工程师|AI 产品经理|解决方案工程师|售前工程师|B端产品经理" \
  --city 100010000 --pages 8 --interval-minutes 30
```

刷新链路（7 步，每步实时写状态）：登录态检查 → 抓列表（不带详情）→ 用上期 JD 作种子 → 只补新增 JD → 合并主表并回填缺失字段 → 写快照+算趋势 → 重生成看板+分析报告。

多关键词轮换（扩量的关键）：

- 关键词列表默认从 `<交付目录>/keywords.json` 读，`--keywords` 优先；`refresh_state.json` 的 `keyword_index` 记录轮换位置，每轮成功后才前移，失败不跳词。
- 主表是**并集**：当轮列表 + 上轮余量，按 `job_id` 去重；存量岗超过 `--keep-days`（默认 7 天）没再出现才丢弃。否则换关键词那一轮会把别的词抓到的岗整表冲掉。
- 每行带 `source_keyword`（哪个词抓到的）和 `carried`（是否本轮没出现、从存量带过来），看板标题会自动从单关键词变成「AI 岗位池 · 关键词 A 120 / B 40」。
- 状态新增 `new_jobs`（本轮净新增）、`carried`（存量条数）、`pool_by_keyword`（按词分布）、`next_keyword`；`new_jobs` 连续两轮为 0 说明该词已抓透。

- 快照目录：`~/.boss-zhipin-scraper/history/snapshot_YYYYMMDD_HHMMSS.json`，默认保留最近 60 期。
- 趋势文件：`<交付目录>/trends.json`，同时内嵌进看板，离线也能看。
- 趋势内容：岗位量/薪资中位数/新增岗位/活跃率时间序列，技能与行业 Top N 变化，行业环比增减，本期新增岗位明细。
- 状态文件：`<快照目录>/refresh_state.json`，前端每 8 秒轮询；刷新期间拒绝打招呼，避免共用 CDP 页面冲突。
- 单独算趋势：`python "$S/analyze_trends.py" --history <目录> --out <trends.json> [--add-from <主表.json>]`。

**实现踩坑（务必保留）**：
1. 8 页列表采集要 4–5 分钟，子进程超时必须给 600s；180s 会误判失败（旧数据仍会保留，但更新中断）。
2. 刷新线程内**不要另建 CDP websocket** 做登录检查，会阻塞；复用服务端已有的状态检查回调。
3. 进度日志要**追加即落盘**（`_Logger`），否则前端只能看到空白直到结束。
4. 新列表常有大批 `boss_active_status` 为空（页面渲染时序），复制上期同 `job_id` 的值回填，否则活跃率趋势出现假跳变（曾从 80.8% 假跌到 19.2%）。
5. 浏览器标签会随反复操作堆积拖慢采集；刷新前可关闭多余的 `job_detail`/看板/新标签页，只留 chat 与 jobs。
6. 服务被强杀会留下 `status=running` 残状态；超过 15 分钟视为卡死并允许重试。
7. 快照首期的新增数记 0，避免趋势图出现虚假尖峰。
8. 趋势折线不要多序列归一叠在一张图：不同量纲（条/K/%）会出现完全重合的斜线，误以为只有两条线。改成四个独立小图，各自纵轴 + 真实数值标签。
9. 自动刷新成功后若页面开关为「已启用」，会再触发一轮自动投递；两者共用 `CDP_GATE`，不会同时抢浏览器标签。

架构图、刷新流程图与自动投递时序图见 `references/architecture.md`。

### 可选：夜间巡检与数据落库

长时间无人值守时，用巡检脚本代替人工盯盘：

```bash
python "$S/patrol_once.py" --output-dir "<交付目录>" --base http://127.0.0.1:8765 \
  --start-server --max-wait-minutes 9
python "$S/ingest_db.py" --input "<交付目录>/岗位公司信息_含JD.json" \
  --db "<交付目录>/jobs.db" --trends "<交付目录>/trends.json" --label patrol
python "$S/nightly_report.py" --output-dir "<交付目录>"
```

`patrol_once.py` 只做健康检查、等待/触发刷新、落库、编译与看板静态检查、更新 `简历优化建议.md`，**不发送招呼、不启用自动投递、不修改原始简历**。服务未启动时可加 `--start-server` 尝试拉起；登录失效只记录，不绕过验证码。`nightly_report.py` 汇总 `patrol/patrol_*.json`、`jobs.db` 与趋势，输出 `夜间巡检报告_*.md` 和 `优化方案_*.md`。

发现问题先记 GitHub Issue，修复后关闭：

```bash
python "$S/report_issue.py" --title "巡检：<现象>" --labels patrol,bug --body "<根因/影响/证据>"
python "$S/report_issue.py" --title "巡检：<现象>" --close --fixed-by "<commit>"
```

同标题 open issue 只追加评论；GraphQL 失败自动走 REST；无 `gh` 时写 `output/issues.jsonl`。不要把简历、手机号、Cookie、token 写进 issue。

### 可选：自动打招呼

按用户要求向目标岗位发送招呼语，支持单条与批量（个人自用）。

单条：

```bash
python "$S/auto_greet.py" --cdp-port 9222 \
  --job-url "<岗位详情页 URL>" --recipient "<公司或 Boss 名关键字>" \
  --greeting "<招呼语>" --dry-run   # 先预检，确认收件人匹配
python "$S/auto_greet.py" ...        # 去掉 --dry-run 正式发送
```

批量：准备 `targets.json`（数组，每项含 `job_url`/`recipient`/`greeting`），脚本逐条处理并按 `--gap-min/--gap-max` 间隔，逐条校验收件人、输出汇总：

```bash
python "$S/auto_greet.py" --cdp-port 9222 --batch-file targets.json \
  --gap-min 5 --gap-max 10 --dry-run
python "$S/auto_greet.py" --cdp-port 9222 --batch-file targets.json --gap-min 5 --gap-max 10
```

**三个必须遵守的坑**（曾导致招呼语误发到无关会话）：
1. 平台自带的"立即沟通"会打开**新标签页** `web/geek/chat`，且默认停在**上一个会话**；不能假设当前面板就是目标 Boss。
2. JS `.click()` 不切换会话面板；必须用原生 `Input.dispatchMouseEvent` 点击左侧会话项。
3. 发送前必须读取详情面板姓名区（"没有更多了"之后）并校验包含 `--recipient`，不匹配就停止。发送后再复核一次。
4. 点击"立即沟通"会先自动发一条平台默认招呼语（如"想知道贵公司…有什么其他要求"）；我们的自定义招呼语是**补充的第二条**，不要误以为没发出去而重复点击。

### 可选：持续自动投递（刷新后自动执行）

`auto_apply.py` 复用同一套简历打分，把匹配 Top 岗位变成可去重、可限速的投递队列。**默认关闭**，且测试简历（`test_resume.json`/张三/13800138000）一律拒绝启用。

先 dry-run 预检（只打开岗位页核对公司与职位，不点「立即沟通」、不产生会话）：

```bash
python "$S/auto_apply.py" --resume "<结果目录>/resume.json" \
  --jobs "<交付目录>/岗位公司信息_含JD.json" --top 10 --limit 3 \
  --state ~/.boss-zhipin-scraper/history/auto_apply_state.json --dry-run
```

去掉 `--dry-run` 才真正发送。也可在工作台页面点「启用自动投递」：每次数据刷新后自动跑一轮，默认每轮最多 3 条、间隔 8–15 秒，结果写入 `auto_apply_state.json`（`applied` 去重表 + `results` 明细）。

需要说明的平台事实：BOSS 直聘网页端岗位页只有「立即沟通」，没有稳定的「一键投递简历」按钮；聊天工具栏「发简历」是**向 Boss 索要简历**，不是我们投递。因此本 skill 的「投递」= 立即沟通 + 校验收件人 + 发送基于简历的定制招呼语（平台会同时展示你的在线简历）。`--send-resume` 仅在页面确实出现可用的「发送简历/投递简历」控件时附带点击，否则如实记录 `ui-unavailable`，不谎报投递成功。

安全约束：单会话、随机间隔、风控/验证码关键字立即停止；发送前后各校验一次收件人；自动投递与手动招呼、数据刷新互斥。

## 使用约束（个人自用）

本 skill 为用户个人自用的求职调研工具，不附加批量打招呼/投递/分发的功能性限制；是否批量、批量多少由使用者自行决定。保留以下技术性约束，目的是保护账号与数据准确性，而非功能限制：

- 单会话、不加并发、不用多账号池；避免触发平台风控。
- JD 中可能含招聘者个人信息，仅在个人范围内使用，不公开传播。
- 遇到验证码或 `code 31/37` 风控码立即停止，不要试图绕过。
- 自动打招呼必须做收件人校验（见上节四个坑），防止误发到无关会话。

## 参考

- `references/workflow.md`：踩坑记录、错误处理、参数速查。
- `references/project-comparison.md`：为何选用上游方案、同类项目对比、是否值得自行发布。
- `LICENSE-upstream`：上游 MIT 许可，修改分发须保留版权声明。
