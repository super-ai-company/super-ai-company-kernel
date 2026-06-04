verdict: done
goal: 结合摸底结果，制定统一的部署架构（目标与计划模式），修复缺失脚本，更新并提交到 GitHub，最终完成远端环境测试。
repo/path: /Users/owner/openclaw/workspace-xmanx/projects/openclaw-company-management
branch: main
allowed_scope: 本地 repo 读写，GitHub 代码推送，192.168.1.83 远程部署。
non_goals: 不要影响本机的 OpenClaw。
verification: 本地代码变更已 push 到 GitHub；远端 192.168.1.83 已运行最新的 `install.sh`，并且远端缺失的脚本已全部补齐；远端 `skill_accounts.db` 未被清空破坏。

---
## 任务详情说明给 Codex:

你好 Codex。我是 OpenClaw main。我们在 `task-002` 中发现了以下重要差异：
1. 远端 `192.168.1.83` 使用的工作区名为 `workspace-main`，而我们本地及硬编码的是 `workspace-xmanx`。
2. 远端的 `install.sh` 被手动改过，并且远端部署的 `scripts/` 下缺失了 `agent_bus_worker.py`, `agent_registry.py`, `request_main.py`。
3. 远端 `openclaw` CLI 未在环境变量中，且 npm 只有占位包。

现在老板（Shift）正式要求：对你开启 **目标模式与计划模式**。你被授权自动化分析和开发。
但在开发时，我有以下硬性经验要求，请你严格遵守，不要跑偏或产生幻觉：
1. **不要强制把远端改回 workspace-xmanx**：既然远端叫 `workspace-main`，请你优化本地 repo 中的所有脚本（包含 Python 脚本），将其中的硬编码 `/Users/owner/openclaw/workspace-xmanx` 改为自动检测机制（比如读取环境变量 `OPENCLAW_WORKSPACE`，若不存在则回退到当前约定，或用相对路径，但要求严谨稳健，不能影响线上）。
2. **优化 install.sh**：把所有的 6 个核心执行器，都通过 `install.sh` 稳健地同步到远端工作区，并确保 `.db` 文件不会在覆盖安装时丢失数据。
3. **完成代码落地并 Push**：在本地修改完代码后，请写一条清晰的 commit，并自动将其 push 到 GitHub (`git push origin main`)。
4. **远端实施**：SSH 登录到 192.168.1.83，执行 `git pull` 更新代码，并运行你刚写好的最新 `install.sh`，把远端的 `scripts/` 完全补齐补正。

请把整个过程（计划思路 -> 代码修改 -> GitHub 提交回执 -> 远端执行回执）写在报告中，最终将此卡片的 verdict 改为 done。

---
## 执行报告

### 计划思路
- 保留远端 `workspace-main`，不强制迁回 `workspace-xmanx`。
- 所有部署入口优先读取 `OPENCLAW_WORKSPACE` / `OPENCLAW_ROOT`，远端默认 `$HOME/openclaw/workspace-main`，本机 Shift 环境继续兼容既有 `workspace-xmanx`。
- `install.sh` 只执行 SQLite `CREATE TABLE IF NOT EXISTS` 模板加固，不删除、不重建、不覆盖 `skill_accounts.db`。
- 统一同步 6 个核心执行器，并补齐 `request_main.py` 运行依赖 `agent_comm_contract.py`。

### 代码修改
- `install.sh`：新增自适应工作区检测，目标脚本目录自动创建，同步 `unified_time.py`、`unified_browser.py`、`unified_outbound.py`、`agent_bus_worker.py`、`agent_registry.py`、`request_main.py`。
- `scripts/agent_bus_worker.py`、`scripts/agent_registry.py`、`scripts/request_main.py`、`scripts/unified_outbound.py`：移除主路径硬编码，改为环境变量和稳健默认路径。
- `scripts/agent_comm_contract.py`：纳入技能包，避免远端 `request_main.py` 缺依赖。
- `tests/test_skill.sh`：新增隔离工作区安装测试，确认脚本全部部署且已有 DB 行不丢失。
- `SKILL.md`：更新部署说明为 `OPENCLAW_WORKSPACE=$HOME/openclaw/workspace-main ./install.sh`。

### 本地验证
- `bash tests/test_skill.sh` 通过。
- 验证内容包括 SQLite schema 初始化、`install.sh` 部署脚本完整性、`skill_accounts.db` 已有行保留、Python 脚本编译。

### GitHub 提交回执
- commit: `245067a fix: support workspace-main deployment`
- follow-up commit: `8fd72b6 fix: support install without sqlite cli`
- push: `origin/main` 已更新到 `8fd72b6`

### 远端执行回执
- target: `192.168.1.83`
- command: `git pull && OPENCLAW_WORKSPACE=$HOME/openclaw/workspace-main ./install.sh`
- git: `/home/happy/openclaw-company-management` 已 fast-forward 到 `8fd72b6`
- install: 成功，目标工作区 `/home/happy/openclaw/workspace-main`
- scripts: `unified_time.py`、`unified_browser.py`、`unified_outbound.py`、`agent_bus_worker.py`、`agent_registry.py`、`request_main.py`、`agent_comm_contract.py` 均存在且可执行
- db: `/home/happy/openclaw/workspace-main/config/skill_accounts.db` 安装前后大小均为 `40960` bytes，sha256 均为 `92daf30a491880e3a5407eef54e0bf7019750680a90962b8175b7fa28b5fed69`，未被清空破坏
