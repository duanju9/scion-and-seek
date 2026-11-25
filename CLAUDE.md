# Claude Code

本仓库的完整 Agent 规范在 `AGENTS.md`。开始任何任务前先读它，再读 `README.md` 和 `SKILL.md`；巡检任务再读 `references/nightly-patrol.md`。

可直接复制的提示词见 `prompts/universal-agent-prompt.md`。

核心边界：只让用户本人在专用 Chrome 登录；不绕过验证码/风控；自动投递默认关闭；不把简历、岗位数据、DB、state、截图提交到 Git。
