# 在不同 Agent 里使用

项目地址：https://github.com/duanju9/scion-and-seek

本目录提供可直接复制的提示词。所有 Agent 的规范源头是仓库根目录的 `AGENTS.md`，各平台入口只是薄封装，避免多份规则漂移。

| Agent | 自动读取的文件 | 使用方式 |
| --- | --- | --- |
| Claude Code | `CLAUDE.md` | 在仓库根目录启动 `claude`，然后粘贴 `prompts/universal-agent-prompt.md` 的中文版 |
| OpenAI Codex | `AGENTS.md` | 在仓库根目录启动 Codex，直接描述任务，或粘贴通用提示词 |
| Cursor | `.cursor/rules/boss-jobs.mdc` | 打开仓库，Composer/Agent 模式直接说“按 AGENTS.md 采集 AI 岗位并出看板” |
| Gemini CLI | `GEMINI.md` | 在仓库根目录启动 `gemini`，粘贴通用提示词 |
| GitHub Copilot Agent | `.github/copilot-instructions.md` | 在 GitHub 仓库里让 Copilot Agent 按提示词执行 |
| WorkBuddy | `SKILL.md` | 把仓库放进 skills 目录，或直接粘贴通用提示词 |
| ChatGPT / Claude 网页版 | 无自动读取 | 上传仓库压缩包或粘贴 `AGENTS.md` + 通用提示词；没有本地文件权限时只能做方案，不要假装执行了命令 |

## 直接分享给别人的提示词

`prompts/shareable-prompt.md`：自带仓库地址、克隆命令、采集/补 JD/看板/简历/边界说明，复制即可发给其他人。

## 三种直接可用的提示词

1. **分享版**：`prompts/shareable-prompt.md`（推荐直接转发）
2. **通用版**：`prompts/universal-agent-prompt.md`（中英双语，适合任何 Agent）
3. **首次采集 / 巡检**：见下方“最小可用”和“巡检”

### 最小可用

```text
读 AGENTS.md 和 SKILL.md，按里面的流程帮我采集 BOSS 直聘「AI 工程师」岗位并生成看板。
输出到 <OUT>。登录我自己来，其他你完成。缺 JD 要补全再交付。
```

### 巡检

```text
读 AGENTS.md 和 references/nightly-patrol.md，执行一轮巡检：
检查服务与登录态 -> 必要时刷新 -> patrol_once.py 落库/测试 -> 检查 jobs.db 与看板 -> 更新简历建议和报告。
发现问题先用 report_issue.py 建 GitHub Issue，修复后 commit/push 并关闭 Issue。
不要发送招呼、不要启用自动投递、不要上传个人数据。
```

## 使用前准备

- Python 3.10+，安装 `requirements.txt`。
- 专用 Chrome profile：`~/.boss-zhipin-scraper/chrome-profile`，由你本人手动登录。
- Agent 需要“读写本地文件 + 执行命令”的权限；只给聊天权限的 Agent 无法真正采集。
- 把 `<OUT>` 换成你的输出目录，例如 `./out` 或绝对路径。

## 安全边界

- 不把密码、Cookie、token 写进提示词或仓库。
- 不把 `output/`、`resume.json`、`targets.json`、`jobs.db`、巡检日志、截图提交到 Git。
- 自动投递默认关闭；只有你明确要求时才允许发送招呼。
- 遇到验证码/风控/登录失效，Agent 应停止并让你本人处理，不得绕过。
