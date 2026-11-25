# GitHub Copilot Agent Instructions

Read `AGENTS.md` first, then `README.md` and `SKILL.md`. For patrol work, read `references/nightly-patrol.md`.

Ready-to-copy prompts: `prompts/universal-agent-prompt.md`.

Hard rules:
- Only the user logs in through the dedicated Chrome window. Never ask for passwords, cookies, or tokens.
- Never bypass CAPTCHAs, risk-control codes, or login walls.
- Auto-apply is off by default. Do not send greetings or enable it unless explicitly requested.
- Never commit `output/`, `resume.json`, `targets.json`, `jobs.db`, patrol logs, screenshots, state, or `.env`.
- Record reproducible problems as GitHub issues with `scripts/report_issue.py`, fix, test, commit, and close the issue.
