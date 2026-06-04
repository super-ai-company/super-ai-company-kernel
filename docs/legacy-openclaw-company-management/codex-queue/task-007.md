verdict: done
goal: 第三轮最终优化与收尾 (Goal Mode Final Wrap-up)
repo/path: /Users/shift/openclaw/workspace-xmanx/projects/openclaw-company-management
branch: main
allowed_scope: 整个本地代码库 (README, install.sh, scripts/*, PROJECT_STATE.md)
non_goals: 不要改变核心业务逻辑，只做工程化收尾与文档优化。

## 目标说明 (Goal Mode)

项目已经跑通了三条核心闭环：
1. 基础架构与环境：远端 OpenClaw CLI 环境修复与统一执行器自适应。
2. 业务管控：单源 `skill_accounts.db` 路由机制、03:00 零垃圾态定时清理。
3. 通知闭环 (Codex -> Main)：强制 4 态汇报脚本 `progress_report.py`。

本任务是第三轮（最终轮）升级收尾。请执行以下动作：

1. 立即上报：先执行 `python3 scripts/progress_report.py --state in_progress --project openclaw-company-management --action "第三轮最终代码清理与README更新" --checking "审阅全局代码与文档" --apply`。
2. 文档对齐：彻底更新 `README.md`，把上述 3 条核心闭环架构写清楚，作为本项目的最终交付白皮书。
3. 状态更新：更新 `PROJECT_STATE.md` 为 100% 收尾完成。
4. 代码优化：快速 Review `scripts/` 下的所有脚本和 `install.sh`，确保头部引用一致，清理掉不需要的空白或冗余注释。
5. 闭环汇报：全部修改完成后，执行 `python3 scripts/progress_report.py --state completed --project openclaw-company-management --action "完成了第三轮工程化收尾与白皮书更新" --checking "代码 Review 通过，项目正式结项" --apply`。
6. 修改本任务卡上方 `verdict: done`。
