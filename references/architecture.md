# 架构、流程与持续更新机制

本文件是 skill 的内部设计说明。所有数据采集与投递动作都发生在**用户本人已登录的专用 Chrome**中，通过 CDP 驱动真实页面；不构造带 Cookie 的接口请求，不绕过验证码/风控。

## 1. 总体架构

```mermaid
flowchart LR
  A["专用 Chrome<br/>profile ~/.boss-zhipin-scraper<br/>CDP 9222"] -->|"页面操作 / DOM 读取"| B["CDP 编排层"]
  B --> C["采集器 boss_cdp_raw<br/>列表 + 详情"]
  B --> D["工作台服务 dashboard_server<br/>HTTP 8765"]
  D -->|"GET / 与 API"| E["单文件看板 HTML<br/>岗位表 + 趋势 + 聊天 + 开关"]
  E -->|"fetch 操作"| D

  C --> F["主表<br/>岗位公司信息_含JD.json/.csv"]
  F --> G["快照 history/snapshot_*.json"]
  G --> H["趋势 trends.json"]
  H --> E
  F --> E

  D --> I["简历 resume.json"]
  I --> J["匹配打分 match_resume"]
  F --> J
  J --> K["投递队列 auto_apply_state.json"]
  K --> D
  D -->|"立即沟通 + 定制招呼语"| A

  subgraph 锁与状态
    L["CDP_GATE 全局互斥"]
    M["refresh_state.json"]
    N["auto_apply_state.json"]
  end
  D --- L
  C --- L
  K --- N
  G --- M
```

要点：

- **采集层**不重写上游 `boss_cdp_raw.py`，只编排：先列表后详情、只补缺失 JD、阈值放宽到 40 字。
- **数据层**以主表为唯一事实源，快照只追加不覆盖，趋势由快照序列计算。
- **服务层**把静态看板变成可操作工作台，所有浏览器动作经 `CDP_GATE` 串行化，避免刷新、手动招呼、自动投递互相抢标签。
- **交互层**单文件 HTML 内嵌数据，离线可看；连上本地服务后获得登录引导、会话记录、刷新与投递能力。

## 2. 一次刷新（持续更新）流程

```mermaid
flowchart TD
  S["定时器到点 / 手动点击刷新"] --> L{"已登录专用 Chrome?"}
  L -->|否| Q["打开登录页，等待用户扫码/验证码"]
  Q --> L
  L -->|是| C1["1. 抓列表 8 页（不带详情）"]
  C1 --> C2["2. 读上期快照 JD 作种子"]
  C2 --> C3["3. 只补新增岗位 JD（2.5–5s 间隔）"]
  C3 --> C4["4. 按 job_id 合并主表 + 回填活跃度"]
  C4 --> C5["5. 写本期快照，保留最近 60 期"]
  C5 --> C6["6. 重算 trends.json（含行业/技能环比）"]
  C6 --> C7["7. 重生成看板 + 分析报告"]
  C7 --> V{"已启用自动投递?"}
  V -->|否| Z["结束，前端检测 data_version 自动重载"]
  V -->|是| A1["按简历重新打分 Top 岗位"]
  A1 --> A2["查 applied 去重表"]
  A2 --> A3["逐条沟通 + 校验收件人 + 发招呼语"]
  A3 --> Z
```

状态与文件：

| 文件 | 作用 |
| --- | --- |
| `~/.boss-zhipin-scraper/history/snapshot_YYYYMMDD_HHMMSS.json` | 每期主表快照，趋势的数据源 |
| `.../refresh_state.json` | 刷新状态、步骤日志、上次成功时间、下次刷新时间 |
| `.../auto_apply_state.json` | 自动投递开关、去重表、逐条结果 |
| `<输出目录>/岗位公司信息_含JD.json/.csv` | 当前主表，前端与匹配共用 |
| `<输出目录>/trends.json` | 时间序列、环比、技能/行业变化、本期新增 |

刷新异常时的行为：子进程超时 600 秒；`running` 状态超过 15 分钟视为卡死并允许重试；登录失效立即停止，旧数据保留不被清空。

## 3. 持续自动投递时序

```mermaid
sequenceDiagram
  participant U as 用户
  participant W as 工作台页面
  participant S as dashboard_server
  participant R as RefreshManager
  participant A as ApplyManager
  participant C as 专用 Chrome (CDP)
  participant B as BOSS 页面

  U->>W: 上传简历 -> parse_resume.py 生成 resume.json
  U->>W: 点「启用自动投递」
  W->>S: POST /api/auto-apply/config {enabled:true}
  S->>S: 校验非测试简历，写 auto_apply_state.json
  R->>C: 定时刷新（抓列表/补 JD/出快照）
  R->>S: 刷新成功，且开关已启用
  S->>A: request(reason=refresh)
  A->>A: 读主表 + 简历，打分取 Top
  A->>A: 过滤 applied 去重表
  A->>C: 获取 CDP_GATE
  loop 每条目标（默认最多 3 条，间隔 8–15s）
    A->>B: 打开岗位页，点「立即沟通」
    B-->>C: 打开新 chat 标签
    A->>B: 原生鼠标事件点目标会话
    A->>B: 校验收件人（发送前）
    A->>B: 输入定制招呼语并发送
    A->>B: 校验收件人（发送后）
    A->>A: 写 applied + results（逐条落盘）
  end
  A-->>W: GET /api/auto-apply/status 展示进度
```

平台事实：网页岗位页只有「立即沟通」，没有稳定的一键投递按钮；聊天里的「发简历」是向 Boss 索要简历。因此这里的「投递」是**沟通式投递**：立即沟通 + 简历随个人资料展示 + 定制招呼语。`--send-resume` 仅在页面真出现可用投递控件时附带点击，否则记录 `ui-unavailable`。

## 4. 为什么不把趋势线叠在一张图

旧实现把四条序列各自归一到 0–1 后画在同一坐标系：岗位量 120→125 与新增岗位 0→11 都变成 0→1，两条线完全重合；薪资与活跃率恒定又都落在中线，看起来「只有两条线且其中一条是平的」。现在改为四个分面小图，每个指标独立纵轴并标注真实数值，直接可读；不同面板的高度不代表可横向比较的幅度。
