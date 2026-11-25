# 工作流细节与踩坑记录

架构图、刷新流程图与自动投递时序图见 `references/architecture.md`。

## 已知问题与处置

| 现象 | 根因 | 处置 |
|---|---|---|
| 详情只抓到前 13 条，后续无 JD | 上游每条详情强制 10–25s 间隔 + 5–10s 加载 + 滚动，45 条需 20 分钟；前台命令 10 分钟超时被杀 | 列表与详情分两步；补抓脚本后台运行，不设短超时 |
| 短 JD 被丢弃（`detail extractor returned... too short`） | 上游 `MIN_DETAIL_TEXT_LENGTH=120`，真实短描述被判无效 | 补抓脚本调用 `extract_detail_fields(d, min_length=40)` |
| CSV 无职位描述列 | 上游列表 CSV 与详情 CSV 是两个文件 | `merge_to_master.py` 按 `job_id` 合并，输出 20 字段主表 |
| 只剩 3 页 45 条 | 默认 `--pages 3` | 用 `--pages 8` 或更高（上限 20） |
| `git clone github.com` 失败 | HTTP/2 framing error / CONNECT 502（代理） | `git -c http.version=HTTP/1.1` 仍失败时用 `https://ghproxy.net/https://raw.githubusercontent.com/...` 逐文件下载 |
| 登录探测误判（`wait_for_login` 提前退出） | 空/非 JSON 的 joblist 响应被计为连续异常 | 跳过自动等待，用户手动确认后 `--check` 验证 |
| 浏览器启动后立刻消失 | 命令退出时沙箱回收子进程 | 常驻后台任务持有 CDP 会话，或用 `--setup-chrome` 的分离进程模式 |
| 直调 wapi 返回 `code 37` 环境异常 | 无浏览器指纹与登录态 | 必须走真实 Chrome CDP，被动监听页面请求，不要自己发 XHR |
| Windows 控制台中文乱码 | GBK 编码 | 上游已修复；读写文件统一 `encoding='utf-8'` / CSV 用 `utf-8-sig` |
| 招呼语误发到无关会话 | "立即沟通"打开新标签且停在旧会话；JS `.click()` 不切换面板 | 原生 `Input.dispatchMouseEvent` 点会话 + 发送前后校验收件人姓名区 |
| 简历年限算成读书年限 | 日期区间推断把教育经历也算进去 | 优先取明确的"N年经验"；回退时截断到教育背景之前 |
| 招呼语出现 `python` 小写 | 匹配时用归一化技能名直接展示 | 归一化仅用于打分，展示取岗位/简历原始写法 |
| 工作台 CDP 连不上（`'webSocketDebuggerUrl'`） | HTTP 端口被误当 CDP 端口 | `--port 8765 --cdp-port 9222` 分开指定 |
| 页面提示「未连接到本地服务」 | 打开的是 `present_files` 的静态预览（9083/file://），不是工作台服务 | 打开 `http://127.0.0.1:8765/`；静态预览下简历上传/刷新/打招呼均不可用 |
| 登录按钮一直「正在打开…」，浏览器像卡住 | `/api/login` 先 `find_page` attach 了正在被刷新使用的页面；聊天标签卡死时 `/api/status` 的 `Runtime.evaluate` 默认 30s 超时，前端 fetch 也没有超时 | 登录只 `activateTarget`/`/json/new` 不 attach；状态在刷新中走缓存，平时 eval 限 4s；前端统一 `AbortController`（普通 15s、上传 180s）并在 finally 恢复按钮 |
| 自动刷新抢占浏览器导致登录页打不开 | 服务启动即刷新，没检查登录态 | `_can_refresh()` 先确认已登录；未登录只等用户登录，不启动采集 |
| 看板页面 JS 全不执行（收到带引号的 HTML） | 首页字符串被当 JSON 序列化 | `_send` 前先 `.encode('utf-8')`，按 bytes 发送 |
| `Cannot access 'greeted' before initialization` | 全局在首次 `render()` 之后声明 | 声明提到脚本顶部（暂时性死区） |
| 前端点会话后消息为空 | 姓名已在详情区但未真正切换会话；消息未渲染完 | 强制点击目标会话 + 校验 `.friend-content.selected` |
| 点每个会话，右侧消息都一模一样 | BOSS 详情头部先变、消息列表后变（可差 5 秒）；旧消息还在 DOM 里时 `.text-content` 已非空，轮询立即误判完成 | 用左侧 `.last-msg-text` 与详情最后一条消息做一致性校验，连续两次一致才返回；附件卡片消息回退 `li.innerText` |
| 原生鼠标点会话不切换 | 列表项内部元素吞掉 click | 点后检查 selected，未切换则 DOM `.click()` 兜底 |
| 自动刷新报"列表采集失败" | 8 页需 4–5 分钟，180s 子进程超时误判 | 列表子进程超时设 600s；失败时保留上次数据 |
| 刷新进度一直空白 | 日志只存在内存，结束才写状态文件 | 用追加即落盘的 `_Logger`，前端实时显示步骤 |
| 刷新线程卡在登录检查 | 线程内重复 `importlib` + 新建 CDP websocket 阻塞 | 复用服务端 `api_status()` 回调做登录判断 |
| Boss 活跃率假跌（80.8%→19.2%） | 新列表 99/125 条活跃字段渲染为空，种子 JD 只含 jd | 从上期同 `job_id` 回填 `boss_active_status`，JSON/CSV 同步 |
| 刷新被强杀后不再自动更新 | 状态残留 `status=running`，`_due()` 直接返回 False | running 超 15 分钟视为卡死并强制重试 |
| 采集越来越慢 | 反复打开岗位页/看板页，标签堆积到 13 个 | 刷新前关掉多余 `job_detail`/看板/新标签页 |
| 趋势首期"新增 120 条"尖峰 | 首期没有对比基准 | `new_count`/`gone_count` 首期记 0 |
| 趋势只有两条线且一条平、一条斜穿全图 | 四条序列各自归一到 0–1 后画在同一图：120→125 与 0→11 完全重合，常量落在中线 | 改为四个分面小图，独立纵轴 + 真实数值标签 |
| 自动投递启用了测试简历 | `test_resume.json` 是张三/13800138000 假数据 | `is_test_resume()` 拦截 test/sample/测试/示例/张三/13800138000，必须换真实简历 |

## 错误处理对照

- 跳转登录页 / `code 37` / 登录墙截断 → 停止，请用户重新登录，不要自动重试。
- `code 31` 或 message 含"环境存在异常/访问频繁/安全校验" → 立即停止当日任务。
- `DetailExtractionError` → 记录并跳过该岗位，继续其余（补抓脚本已实现）。
- 覆盖率不足 → 重跑补抓脚本，它只处理没有 JD 的岗位。

## 参数速查

```
--keyword         搜索关键词
--city            中文城市名或 9 位城市码；--list-cities [关键词] 查询
--pages           页数，上限 20（每页约 15 条）
--no-detail       跳过详情（第 2 步必用）
--company         按公司名本地子串过滤，零额外请求
--scale/--salary/--experience/--degree   筛选条件
--check           环境 + CDP + 登录态检查
--smoke-test      真实浏览器跑一次搜索，不写结果
--stop-chrome     关闭专用浏览器（只匹配隔离 profile，不碰主浏览器）
```

## 联动工作台参数

```
generate_dashboard.py --input 主表.json --output 看板.html [--trends trends.json]
                      生成单文件看板（登录条/对话面板/招呼按钮/趋势区）
dashboard_server.py   --port 8765 --cdp-port 9222 --dashboard 看板.html
                      --output-dir 交付目录 --interval-minutes 30 [--no-browser]
                      [--resume resume.json] [--apply-limit 3]
                      [--apply-gap-min 8 --apply-gap-max 15] [--send-resume]
                      启动本地服务，浏览器打开 http://127.0.0.1:8765/
                      API: status / login / conversations / messages / greet
                           jobs / trends / refresh / refresh/status
                           auto-apply / auto-apply/status / auto-apply/config
                           resume/status / resume/upload / resume/rematch
```

工作台首次打开若显示"正在检测"，等 1-2 秒异步状态刷新完成。前端每 60 秒自动刷新登录状态、每 8 秒轮询刷新进度；数据版本变化自动重载页面。重新生成看板后无需重启服务（首页热加载）。服务必须常驻运行，命令退出会被回收。

## 持续更新与趋势参数

```
refresh_data.py       --output-dir 交付目录 --dashboard 看板.html [--cdp-port 9222]
                      [--keyword "AI 工程师"] [--city 100010000] [--pages 8]
                      [--interval-minutes 30] [--gap-min 2.5 --gap-max 5]
                      完整刷新链路；interval>0 时循环定时运行
analyze_trends.py     --history 快照目录 [--add-from 主表.json] [--out trends.json]
                      [--top 10] [--keep 60]
                      新增快照 / 计算趋势 / 清理过期快照
```

快照字段：从主表抽取 16 个关键字段（含 `job_id`、`salary`、`skills`、`company_industry`、`boss_active_status`、`jd`），默认保留 60 期。趋势输出 `summaries`（每期概览）、`series`（4 条时间序列）、`skill_trends`、`industry_trends`、`industry_delta`、`delta`（环比）、`new_jobs`（本期新增明细）。看板内嵌同一份趋势数据，离线可看。

## 简历解析与匹配参数

```
parse_resume.py   --input <PDF/DOCX/TXT/MD> [--output resume.json]
                  支持扫描件外的大多数简历；DOCX 无 python-docx 时 XML 降级解析
match_resume.py   --resume resume.json --jobs 主表.json --top 10
                  [--out-report 报告.md] [--out-targets targets.json]
                  打分：技能60% + 薪资15% + 经验10% + 学历10% + 活跃5%
auto_greet.py     --batch-file targets.json [--gap-min 5 --gap-max 10] [--dry-run]
                  targets.json 每项含 job_url / recipient / greeting
auto_apply.py     (--targets targets.json | --resume resume.json --jobs 主表.json)
                  --state auto_apply_state.json [--top 10 --limit 3]
                  [--gap-min 8 --gap-max 15] [--send-resume] [--dry-run]
                  dry-run 仅核对岗位页，不点沟通/不发消息；去重表按 job_id 落盘
ingest_db.py      --input 主表.json --db jobs.db [--trends trends.json] [--label patrol]
                  按 job_id upsert jobs，写 snapshots/runs，输出总数/AI 岗位/JD 覆盖
patrol_once.py    --output-dir 交付目录 [--base http://127.0.0.1:8765]
                  [--start-server] [--no-refresh] [--max-wait-minutes 9]
                  单轮巡检；不发送招呼/不投递/不修改原始简历
nightly_report.py --output-dir 交付目录 [--db jobs.db]
                  汇总巡检日志/DB/趋势，生成报告与优化方案
report_issue.py   --title "巡检：<现象>" [--body ...] [--labels patrol,bug]
                  [--close --fixed-by <commit>] [--dry-run]
                  同标题 open issue 幂等；GraphQL 失败走 REST；无 gh 写 output/issues.jsonl
```

简历 JSON 字段：`skills`、`years_experience`、`education`、`target_roles`、`contact`、`sections`、`raw_text`。扫描件 PDF 无文本层，需先 OCR 再解析。

也可以直接在工作台 `http://127.0.0.1:8765/` 的「个人简历」栏上传：服务端接收 base64 文件，写入 `~/.boss-zhipin-scraper/resume/`，调用 `parse_resume.py` 生成 `output/resume.json`，再调用 `match_resume.py` 生成 `简历匹配报告.md` 与 `targets.json`；前端展示技能、年限、学历和 Top 5 匹配。原始文件名保存在 `original_name` 字段，避免界面显示时间戳前缀。

## 数据字段说明

主表字段：`job_id`、`title`、`salary`、`salary_source`、`location`、`boss_name`（公司名）、`company_scale`、`company_stage`、`company_industry`、`boss_title`、`boss_active_status`、`job_labels`、`skills`、`skill_tags`、`welfare`、`tags`、`has_jd`、`jd`、`job_link`、`company_link`。

薪资原文如 `15-25K·13薪`、`100-200元/天`，分析前需解析为数值区间。
