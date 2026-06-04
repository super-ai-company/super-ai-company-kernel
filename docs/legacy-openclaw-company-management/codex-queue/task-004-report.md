【结论】done，远端 `openclaw status` 已可用，返回真实安装目录和容器状态。

【执行】改了远端：
- `/home/happy/bin/openclaw`：新增用户级 wrapper
- `/home/happy/.bashrc`、`/home/happy/.profile`：加入 `OPENCLAW_COMPOSE_DIR=/opt/1panel/apps/openclaw/li-test` 和 `~/bin` PATH
- 本地任务卡：[codex-queue/task-004.md](/Users/owner/openclaw/workspace-xmanx/projects/openclaw-company-management/codex-queue/task-004.md) 已改 `verdict: done` 并写入证据

【验证】远端复验成功：
`openclaw status` 返回 `install_dir=/opt/1panel/apps/openclaw/li-test`，容器 `1Panel-openclaw-lYOM` 状态为 `exited exit_code=137`，镜像 `1panel/openclaw:2026.3.24`。

【风险】CLI 环境已修好，但 OpenClaw 服务本体当前未运行；按任务要求未启动、未重启任何核心进程。

