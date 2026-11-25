# 同类项目对比与发布决策

## GitHub 同类项目（2026-11 调研）

| 项目 | 路线 | 规模 | 评价 |
|---|---|---|---|
| `eatmoreduck/boss-zhipin-scraper` | Chrome/Edge CDP 被动捕获 wapi 响应，明文薪资 | ~1.4k stars, MIT, v2.4 | **核心采集器选型**。跨平台、风控码识别、城市码表、增量写入、已支持 Agent Skill 分发 |
| `can4hou6joeng4/boss-agent-cli` | httpx 逆向 wapi + patchright，四级登录，错误码 | ~892 stars | 登录与错误码设计好，但偏"求职方自动打招呼"，功能过载 |
| `zhengziha/boss-zhipin` | Playwright + wapi + SQLite + LLM 方案文档 | 文档型 | 架构参考，非可运行采集器 |
| `wuhonglei/boss_analysis` | Playwright + Jinja2 报告 | ~3 stars | 分析输出形态参考 |
| `ufownl/auto-zhipin` / `wensia/boss-zhipin-automation` / `Snseam/boss-zhipin-mcp` | Playwright 沟通/招聘端/MCP | 小 | 不适配岗位+公司信息采集 |
| `MuYu10086/Boss-Crawler` / `SolidifyTime/BossZP-Spider` | Selenium 老方案 | 小 | 字体反爬、验证码问题多 |
| Apify actor | 第三方 | 已弃用 | 成功率存疑 |

## 本 skill 的增量

只有两点，不是新采集器：

1. `fetch_missing_details.py`：只补缺失 JD、降低间隔、放宽短 JD 阈值、增量写盘，解决"超时截断导致无 JD"。
2. `merge_to_master.py`：把 JD 按 `job_id` 写回主表，解决"CSV 无 JD 列"。

其余能力全部复用上游，避免重复维护。

## 是否有必要上传 GitHub（结论：不必要）

**不建议新建公开仓库**，理由：

- 上游已有 1.4k stars、MIT、持续维护、本身已支持 `npx skills add` 分发，重复发布是低价值分叉。
- 本 skill 的技术增量较小（两个约 5KB 脚本），不足以支撑一个独立项目。
- BOSS 直聘采集处于平台协议与《个人信息保护法》的灰色地带，公开发布爬虫仓库会增加法律与账号风险。
- 维护成本高：BOSS 前端与风控变化快，上游作者（风控架构师）在跟进，分叉只会落后。

**更合适的做法**（按优先级）：

1. 本地 skill 自用，把踩坑经验固化即可 —— 当前已完成。
2. 若想回馈社区，向上游提 PR / issue，把两个修复贡献回去（MIT 允许），而不是新开仓库。
3. 若确有分享需求，发布"使用说明/踩坑笔记"类文章，而非完整爬虫代码。

**何时才值得发布**：修复被上游采纳但长期未合入；或增量变成独立能力（如多平台统一采集、结构化分析产品化）；且完成合规评估与免责声明。
