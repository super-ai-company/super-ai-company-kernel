【结论】done，远端 `192.168.1.83` 的 `workspace-main` 已完成 Registry、Zero-Trash cron、E2E 测试。

【执行】
- 本地新增：[scripts/skill_accounts_db.py](/Users/shift/openclaw/workspace-xmanx/projects/openclaw-company-management/scripts/skill_accounts_db.py)、[scripts/cleanup_trash.sh](/Users/shift/openclaw/workspace-xmanx/projects/openclaw-company-management/scripts/cleanup_trash.sh)
- 本地更新：[install.sh](/Users/shift/openclaw/workspace-xmanx/projects/openclaw-company-management/install.sh) 分发新增脚本
- 远端已部署到：`/home/happy/openclaw/workspace-main/scripts/`
- 远端 Registry：`/home/happy/openclaw/workspace-main/config/agent_registry.json`
- 远端 cron：每天 `03:00` 执行 `cleanup_trash.sh`
- 报告已写入：[codex-queue/task-006.md](/Users/shift/openclaw/workspace-xmanx/projects/openclaw-company-management/codex-queue/task-006.md)，`verdict: done`

【验证】
- 本地：`bash tests/test_skill.sh` 通过
- 远端 `agent_registry.py main` 返回 `main/control_tower`
- 远端 `unified_time.py --target current` 返回 `2026-06-01`
- 远端 `skill_accounts_db.py get ...` 返回 `not_found`，无语法/路径错误，符合允许条件
- 远端 `openclaw status`：`1Panel-openclaw-lYOM status=running exit_code=0`

【风险】
- 远端 `openclaw` wrapper 不支持 `agents list --json`，所以 Registry 用实际 `workspace-main` 做最小初始化。

