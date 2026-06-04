verdict: done
goal: 在远端 192.168.1.83 准确定位真实的 OpenClaw 运行目录，修复系统环境变量/别名，使 `openclaw status` 等核心命令可用。
repo/path: /Users/shift/openclaw/workspace-xmanx/projects/openclaw-company-management
branch: main
allowed_scope: 192.168.1.83 远程环境诊断与修复。
non_goals: 不要重装或重启远端正在运行的 OpenClaw 服务，仅修复 CLI 执行环境和 PATH。
verification: 远端直接执行 `openclaw status` 或 `openclaw --version` 能够成功返回真实状态，不再是 command not found 或占位包。

---
## 任务详情说明给 Codex:

你好 Codex。阶段 3 已经完美收官。现在进入阶段 4。

**背景与问题**：
在之前的摸底中，你发现 `192.168.1.83` 的远端环境里：
1. `openclaw` 命令不可用或指向的是 npm 的 `0.0.1` 占位包。
2. 远端存在多个疑似目录，比如 `/home/happy/openclaw`、`/opt/openclaw`、`/opt/1panel/apps/openclaw` 等。

**你的目标与计划要求**：
1. **顺藤摸瓜**：请通过 `ps`、`systemctl`、`docker ps` 等命令，查出远端 192.168.1.83 真正正在运行的 OpenClaw 后台进程到底隶属于哪个目录。
2. **修复环境**：找到真实的安装路径后，请修复远端的环境变量（例如修改 `~/.bashrc`, `~/.zshrc` 或建立 `/usr/local/bin/openclaw` 软链接）。
3. **安全第一**：只修路径，不要重启它正在运行的核心进程，以免中断线上业务。
4. **验证并完结**：确保在远端能够正常执行 `openclaw status`。将诊断过程、确认的真实路径以及修复方式写在下方，然后把 verdict 改为 done。

---

## Codex 执行结果

Verdict: done

真实路径确认：
- 远端主机：`happy@192.168.1.83`
- 真实 1Panel OpenClaw 安装目录：`/opt/1panel/apps/openclaw/li-test`
- compose 文件：`/opt/1panel/apps/openclaw/li-test/docker-compose.yml`
- 配置目录挂载：`/opt/1panel/apps/openclaw/li-test/data/conf -> /home/node/.openclaw`
- workspace 挂载：`/opt/1panel/apps/openclaw/li-test/data/workspace -> /home/node/.openclaw/workspace`
- 关联容器：`1Panel-openclaw-lYOM`
- 镜像：`1panel/openclaw:2026.3.24`

诊断证据：
- `ps` 未发现正在运行的 OpenClaw gateway 进程。
- `systemctl` 未发现 OpenClaw service unit。
- `docker ps` 未发现正在运行的 OpenClaw 容器。
- `docker ps -a` 确认容器 `1Panel-openclaw-lYOM` 存在但已退出：`Exited (137) 2 months ago`。
- `docker inspect 1Panel-openclaw-lYOM` 确认状态：`Status=exited ExitCode=137 StartedAt=2026-03-27T16:28:23.943383916Z FinishedAt=2026-03-27T16:29:09.508940922Z`。

修复方式：
- 新增用户级 CLI wrapper：`/home/happy/bin/openclaw`
- 写入环境变量：`OPENCLAW_COMPOSE_DIR=/opt/1panel/apps/openclaw/li-test`
- 更新 `~/.bashrc` 和 `~/.profile`，确保 `~/bin` 优先进入 PATH。
- 未修改 `/usr/local/bin`，因为当前用户无 sudo 免密权限。
- 未重启、未启动、未停止任何 OpenClaw 核心服务。

验证结果：
- `ssh happy@192.168.1.83 'bash -lc "command -v openclaw; openclaw status; openclaw --version; openclaw path"'` 成功。
- 返回：
  - `command -v openclaw` -> `/home/happy/bin/openclaw`
  - `openclaw status` -> `install_dir=/opt/1panel/apps/openclaw/li-test`
  - `openclaw status` -> `container=/1Panel-openclaw-lYOM status=exited exit_code=137 image=1panel/openclaw:2026.3.24`
  - `openclaw --version` -> `openclaw image=1panel/openclaw:2026.3.24 install_dir=/opt/1panel/apps/openclaw/li-test`

剩余风险：
- CLI 环境已修复，但远端 OpenClaw 服务本身当前不是 running；按本任务 non-goals，未执行启动或重启。
