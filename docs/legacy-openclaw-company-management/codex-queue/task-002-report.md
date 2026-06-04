【结论】已完成，只读摸底报告已写入并把任务状态改为 `done`。

- 改了什么：更新 [task-002.md](/Users/shift/openclaw/workspace-xmanx/projects/openclaw-company-management/codex-queue/task-002.md:1)，追加远端 OpenClaw 路径、运行状态、容器/端口摘要、同步差距。
- 远端结论：实际工作区是 `/home/happy/openclaw/workspace-main`；`openclaw status` 不能用，因为 CLI 不在 PATH，npm 包是占位 `openclaw@0.0.1`。
- 同步差距：远端技能仓库核心文件与本地一致，但实际工作区 `scripts/` 只部署了 `unified_time/browser/outbound`，缺 `agent_bus_worker.py`、`agent_registry.py`、`request_main.py`。
- 验证结果：SSH、远端路径、hash、Docker、端口、进程、systemd 都已只读确认；最后远端存在性检查返回 `VERIFY_REMOTE_OK`。
- 风险：远端没有发现 `workspace-xmanx/`，后续部署前要先统一使用 `workspace-main` 还是创建/迁移 `workspace-xmanx`。

