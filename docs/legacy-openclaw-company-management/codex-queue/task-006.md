verdict: done
goal: 落地远端分公司的业务管理规划（Agent入职、路由表测试、零垃圾态纪律），并执行端到端测试。
repo/path: /Users/owner/openclaw/workspace-xmanx/projects/openclaw-company-management
branch: main
allowed_scope: 192.168.1.83 远程环境，操作 workspace-main。
non_goals: 不要破坏 192.168.1.83 上正在运行的业务逻辑。
verification: 远端 Agent Registry 就绪；远端统一执行器测试通过；远端清理任务配置完毕。

---
## 任务详情说明给 Codex:

Codex，项目总管 (main) 收到了最高指示：不再等待人工批复，直接推进并完成测试！

请立即在 192.168.1.83 的 OpenClaw 容器环境（或绑定的宿主机工作区 `/home/happy/openclaw/workspace-main`）中执行以下 3 项部署和测试：

1. **远端 Agent 入职 (Registry)**
   - 检查远端 `config/agent_registry.json`，如果没有内容，请通过执行你之前写的 `python3 scripts/agent_registry.py --discover` 或者主动拉取同步来完成初始化。

2. **零垃圾态纪律落地 (Zero-Trash Cron)**
   - 在远端宿主机配置一个 cron job，或者写一个专用的 cleanup 脚本 `scripts/cleanup_trash.sh` 并赋予执行权限。
   - 要求：定期（例如每天凌晨 3 点，或用 find 限制 mtime）清理 `/home/happy/openclaw/workspace-main/tmp` 和 `logs` 目录下的调试残留（`.bak`, `.patch`, 失败残余）。

3. **端到端业务执行器测试 (E2E Test)**
   - 在远端执行 `OPENCLAW_WORKSPACE=/home/happy/openclaw/workspace-main python3 scripts/unified_time.py --target current`，验证代码确实能跑。
   - 在远端验证数据库连通性：`OPENCLAW_WORKSPACE=/home/happy/openclaw/workspace-main python3 scripts/skill_accounts_db.py get --business nestcar --platform line --action send` （允许返回 NotFound 或空，只要不报语法/路径错误即可）。

执行完毕后，将结果、测试数据记录在下方的报告区，然后将 verdict 改为 done。

---
## 报告区

【结论】done，远端 `192.168.1.83:/home/happy/openclaw/workspace-main` 的 Agent Registry、统一执行器测试、零垃圾态 cron 均已落地并通过验证。

【执行】
- 新增并部署：`scripts/skill_accounts_db.py`、`scripts/cleanup_trash.sh`。
- 更新并部署：`install.sh` 分发 `skill_accounts_db.py` 和 `cleanup_trash.sh` 到远端工作区。
- 远端 Registry：`/home/happy/openclaw/workspace-main/config/agent_registry.json` 已初始化，包含 `main` 控制塔档案。
- 远端 cron：保留原 `@reboot actions-runner`，新增 `0 3 * * * ... cleanup_trash.sh`。

【验证】
- 本地包装测试：`bash tests/test_skill.sh` 通过。
- 远端 Registry：`agent_registry.py main` 返回 `agent_id=main`、`workspace=/home/happy/openclaw/workspace-main`、`role=control_tower`。
- 远端清理脚本：手动执行成功，并确认 `tmp/`、`logs/` 目录存在。
- 远端 cron：`crontab -l | grep cleanup_trash.sh` 返回每天 03:00 清理任务。
- 远端统一时间：`OPENCLAW_WORKSPACE=/home/happy/openclaw/workspace-main python3 scripts/unified_time.py --target current` 返回 `2026-06-01`。
- 远端数据库连通：`OPENCLAW_WORKSPACE=/home/happy/openclaw/workspace-main python3 scripts/skill_accounts_db.py get --business nestcar --platform line --action send` 返回 `not_found`，无语法/路径错误，数据库路径为 `/home/happy/openclaw/workspace-main/config/skill_accounts.db`。
- 远端服务状态：`openclaw status` 显示容器 `1Panel-openclaw-lYOM status=running exit_code=0`。

【风险】
- 远端 `openclaw` wrapper 当前只支持 `status/version/path`，不支持 `openclaw agents list --json`；本次 Registry 采用实际远端工作区最小初始化。
