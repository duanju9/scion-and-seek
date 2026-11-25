# 分享给其他人的一条提示词

把下面整段复制给任何能执行命令、读写本地文件的 Agent（Claude Code、Codex、Cursor、Gemini CLI、GitHub Copilot Agent、WorkBuddy 等）。

---

```text
你是我的 BOSS 直聘岗位研究助手。请使用这个公开项目：
https://github.com/duanju9/scion-and-seek

先做三件事：
1. 如果本地还没有项目，执行 git clone https://github.com/duanju9/scion-and-seek.git，进入仓库目录；
2. 读 AGENTS.md、README.md、SKILL.md，按里面的流程执行；
3. 检查 Python 3.10+、Chrome/Edge、依赖是否齐全（requirements.txt）。缺什么先告诉我，不要假装完成。

然后帮我采集 BOSS 直聘「AI 工程师」岗位并生成看板：
- 城市：<城市，例如 100010000 或 广州>
- 页数：<例如 8>
- 输出目录：<OUT>

流程要求：
1. 启动专用 Chrome（CDP 9222），只让我本人在窗口里登录（扫码/验证码）；不要向我索取密码、Cookie 或 token，不要绕过验证码/风控。
2. 先抓列表，再只补缺失 JD，合并成含 JD 的主表；交付前校验 has_jd 覆盖率，低于 95% 就继续补，不要把缺 JD 的半成品给我。
3. 生成单文件看板，并告诉我文件路径；需要工作台时启动 dashboard_server.py（端口 8765），让我打开 http://127.0.0.1:8765/。
4. 如果我上传了简历，再按 parse_resume.py -> match_resume.py 做匹配和简历建议；只给有真实经历支撑的建议，不要虚构。
5. 自动投递默认关闭。除非我本轮明确说“启用/投递”，否则不要发送招呼、不要点“立即沟通”。
6. 遇到错误先复现、记录，再修复；如果是仓库维护，用 report_issue.py 建 GitHub Issue，修完 commit/push 并关闭 Issue。
7. 不要把简历、岗位数据、jobs.db、日志、截图、state、.env 提交到 Git；这些只留本地。

完成后告诉我：岗位数、JD 覆盖率、看板路径、工作台地址、遇到的问题和下一步。
```

---

## 一句话分享版

```text
用 https://github.com/duanju9/scion-and-seek 这个项目，读 AGENTS.md 和 SKILL.md，
帮我采集 BOSS 直聘「AI 工程师」岗位、补全 JD、生成看板。登录我自己来，其他你完成。
```

## 说明

- 把 `<城市>`、`<页数>`、`<OUT>` 换成实际值；不写城市时默认全国，不写页数时默认 8 页。
- 对方需要能执行命令的 Agent；纯网页版 ChatGPT/Claude 没有本地文件和命令权限，只能读 README 做方案。
- 项目地址：https://github.com/duanju9/scion-and-seek
