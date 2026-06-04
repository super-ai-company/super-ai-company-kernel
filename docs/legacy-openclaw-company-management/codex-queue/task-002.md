verdict: done
goal: 连接并摸底 192.168.1.83 上的 OpenClaw 环境现状，为后续部署和维护制定计划。
repo/path: /Users/owner/openclaw/workspace-xmanx/projects/openclaw-company-management
branch: main
allowed_scope: 仅限连接到 192.168.1.83 并做只读环境检查
non_goals: 本地仓库不要修改代码；暂不在远端进行破坏性写入或重启，纯搜集信息。
verification: 输出远端环境的 OpenClaw 安装路径、版本、运行状态，并列出与本地 `company-management` 技能的同步差距。

---
## 任务详情说明给 Codex:
你好 Codex。我是 OpenClaw 的 main 负责人，现在我们正式启用了基于 `codex-queue` 的协作与沟通流。
192.168.1.83 这个靶机的 SSH 免密凭据（happy）我已经为你配置好。请直接执行 `ssh happy@192.168.1.83 "..."` 的相关命令，或者进去做交互式检查。

请探明：
1. 远端 192.168.1.83 服务器上的 OpenClaw 装在哪里？处于什么运行状态（`openclaw status`）？
2. 远端的 `workspace-xmanx/config/` 和 `workspace-xmanx/scripts/` 中是否已有咱们最新的 `company-management` 统一代码？
3. 将你的摸底报告详细附在本次输出的末尾，并将文件状态改为 done。

---

## Verdict: done

【结论】已完成只读摸底：SSH 可用；远端 OpenClaw 主工作区实际在 `/home/happy/openclaw/workspace-main`，但 `openclaw` CLI 不在 PATH，`openclaw status` 当前不可执行；`workspace-xmanx/config/` 和 `workspace-xmanx/scripts/` 远端未发现。

【执行】
- SSH 目标：`happy@192.168.1.83`
- 远端主机：`ubuntuxuniji`
- 远端用户：`happy`
- 远端默认 PATH：`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/games:/usr/local/games:/snap/bin`

OpenClaw 路径摸底：
- `/home/happy/openclaw`：存在，只包含 `workspace-main/`
- `/home/happy/openclaw/workspace-main`：存在，当前实际工作区
- `/opt/openclaw`：存在，仅见 `bin/`
- `/opt/1panel/apps/openclaw`：存在，含 `li-test/`
- `/home/happy/.npm-global/lib/node_modules/openclaw`：存在，但 `package.json` 显示占位包 `openclaw@0.0.1`
- `/home/happy/openclaw-company-management`：存在，是远端 company-management 技能仓库副本

运行状态：
- `command -v openclaw`：无输出
- `openclaw status`：`bash: line 1: openclaw: command not found`
- `/home/happy/.npm-global/bin`：未发现可用 openclaw bin
- systemd/user systemd：未发现 openclaw/codex 相关 service
- 进程中未发现明确的 `openclaw` CLI/service 进程；远端有大量 Docker/Node/Python 服务运行

远端容器/端口现状摘要：
- Docker daemon 正在运行
- `qdrant`：Up 4 days healthy，监听 `127.0.0.1:6333-6334`
- `cli-proxy-api-plus`：Up 2 weeks，监听 `0.0.0.0:8317`
- `1Panel-postgresql-FGcQ`：Up 4 days healthy，监听 `0.0.0.0:54321`
- `1Panel-redis-6TjB`：Up 2 weeks，监听 `127.0.0.1:6379`
- 另有 chinda、aiclient2api、sub2api、pixelle、exchange 等服务，未做写入或重启

company-management 同步差距：
- 远端 `/home/happy/openclaw-company-management` 与本地核心文件 hash 一致：
  - `SKILL.md`
  - `scripts/agent_bus_worker.py`
  - `scripts/agent_registry.py`
  - `scripts/request_main.py`
  - `scripts/unified_browser.py`
  - `scripts/unified_outbound.py`
  - `scripts/unified_time.py`
  - `templates/skill_accounts.sql`
  - `tests/test_skill.sh`
- 远端 `/home/happy/openclaw-company-management/install.sh` 与本地不一致；远端有未提交修改，主要把默认工作区从本机 `/Users/owner/openclaw/workspace-xmanx` 改成 `$HOME/openclaw/workspace-main`，并用 Python sqlite3 初始化 schema。
- 远端 `/home/happy/openclaw/workspace-main/scripts/` 只有 3 个统一脚本：
  - `unified_browser.py`：hash 与本地一致
  - `unified_outbound.py`：hash 与本地一致
  - `unified_time.py`：hash 与本地一致
- 远端 `/home/happy/openclaw/workspace-main/scripts/` 缺少本地已有的 3 个脚本：
  - `agent_bus_worker.py`
  - `agent_registry.py`
  - `request_main.py`
- 远端 `/home/happy/openclaw/workspace-main/config/skill_accounts.db` 存在，大小 `40960` bytes，hash `92daf30a491880e3a5407eef54e0bf7019750680a90962b8175b7fa28b5fed69`
- 远端未发现 `/home/happy/openclaw/workspace-xmanx/config/` 或 `/home/happy/openclaw/workspace-xmanx/scripts/`
- 在 `/home/happy`、`/opt/openclaw`、`/opt/1panel/apps/openclaw` 的搜索范围内未发现名为 `workspace-xmanx` 的目录

【验证】
- 已用 `ssh -o BatchMode=yes -o ConnectTimeout=8 happy@192.168.1.83 ...` 验证 SSH 可连接。
- 已运行远端 `find`、`ls`、`git status`、`sha256sum`、`docker ps`、`ss -ltnp`、`ps`、`systemctl list-units` 做只读核验。
- 本地与远端 company-management 核心文件 hash 已比对；除远端 `install.sh` 外，技能仓库核心文件一致。

【风险】
- `openclaw status` 无法执行，因为远端 CLI 未进入 PATH，且 npm 安装位置里是占位包 `openclaw@0.0.1`。
- 远端实际工作区名是 `workspace-main`，不是任务要求里的 `workspace-xmanx`；后续部署计划需要先统一工作区路径约定。
- 当前只读摸底未检查数据库内容表结构细节，未执行 install，未同步缺失脚本，未重启任何服务。
