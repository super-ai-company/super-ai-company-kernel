# OpenClaw Company Management

> Unified local company-management execution layer for OpenClaw agents.
> OpenClaw 本机公司管理执行层：统一账号路由、跨业务执行入口、Agent 汇报与零垃圾维护。

[English](#english) | [中文](#中文)

---

## English

### Project Introduction

`openclaw-company-management` is the company-management skill package for an OpenClaw local workspace. It turns repeated multi-business, multi-account, multi-agent operations into a small, installable, auditable execution layer.

The project focuses on three production requirements:

- every business action must resolve through a shared routing source;
- every agent handoff must carry structured execution status;
- every local workspace should stay clean enough to operate continuously.

It is designed for local-first OpenClaw deployments, but the install path can be overridden through environment variables so the same package can be reused on other machines.

### Core Features

- **Unified Execution**
  Provides shared command entry points for time lookup, browser execution, outbound platform actions, Main-agent requests, and agent-bus work consumption.

- **Single Source of Truth**
  Uses `skill_accounts.db` and `templates/skill_accounts.sql` to centralize business account routing, platform routing, business configuration, and authorization records.

- **Account and Route Constraints**
  Enforces SQLite uniqueness constraints so the same business/platform/action cannot silently fork into duplicate routes.

- **Agent Communication Contract**
  Standardizes Codex/Main communication around explicit states such as `acknowledged`, `in_progress`, `blocked`, and `completed`.

- **Company Kernel Bridge**
  Exposes a lightweight OpenClaw-side health bridge for Company Kernel `doctor --summary` and the runtime heartbeat alert, so OpenClaw can consume the evolved heartbeat mechanism without duplicating kernel logic.

- **Approval-to-Codex Queue Bridge**
  Syncs approved OpenClaw OPS approval files into `codex-queue` and writes a Codex-side receipt without starting an independent Telegram Bot API polling watcher.

- **Zero-Trash Cron Ready**
  Includes `scripts/cleanup_trash.sh` for scheduled cleanup of temporary debug, failure, patch, and log residues.

- **Portable Workspace Defaults**
  Defaults to `/Users/owner/openclaw/workspace-xmanx` on the primary local machine and `${HOME}/openclaw/workspace-main` elsewhere, with explicit environment-variable overrides.

- **Installable Skill Package**
  `install.sh` initializes the database schema and distributes executable scripts into the target OpenClaw workspace.

### Directory Structure

```text
openclaw-company-management/
├── README.md                         # Bilingual release documentation
├── SKILL.md                          # OpenClaw skill instructions
├── install.sh                        # Installer for database schema and scripts
├── PROJECT_STATE.md                  # Project status notes
├── codex-queue/                      # Codex task cards and reports
├── scripts/
│   ├── agent_bus_worker.py           # Agent-bus inbox worker
│   ├── agent_comm_contract.py        # Agent communication validation
│   ├── approval_to_codex_queue.py    # OPS approval -> Codex queue bridge
│   ├── company_kernel_bridge.py      # Company Kernel health and heartbeat bridge
│   ├── agent_registry.py             # Agent registry helpers
│   ├── cleanup_trash.sh              # Zero-trash cleanup script
│   ├── progress_report.py            # Structured progress reporter
│   ├── request_main.py               # Request helper for Main agent
│   ├── skill_accounts_db.py          # SQLite account/route lookup
│   ├── unified_browser.py            # Unified browser action entry point
│   ├── unified_outbound.py           # Unified outbound action entry point
│   └── unified_time.py               # Unified time lookup helper
├── templates/
│   └── skill_accounts.sql            # SQLite schema and constraints
└── tests/
    └── test_skill.sh                 # Basic skill validation script
```

### Installation

Run the installer from the project root:

```bash
./install.sh
```

Default target workspace:

- primary local machine: `/Users/owner/openclaw/workspace-xmanx`
- other machines: `${HOME}/openclaw/workspace-main`

Override the target paths when needed:

```bash
OPENCLAW_ROOT=/path/to/openclaw \
OPENCLAW_WORKSPACE=/path/to/workspace \
./install.sh
```

The installer creates or updates:

- `${OPENCLAW_WORKSPACE}/config/skill_accounts.db`
- `${OPENCLAW_WORKSPACE}/scripts/*.py`
- `${OPENCLAW_WORKSPACE}/scripts/cleanup_trash.sh`

### Usage

Query an account route:

```bash
python3 scripts/skill_accounts_db.py get \
  --business nestcar \
  --platform line \
  --action send
```

Run a unified time lookup:

```bash
python3 scripts/unified_time.py --target current
```

Report agent progress:

```bash
python3 scripts/progress_report.py \
  --state completed \
  --project openclaw-company-management \
  --action "release documentation updated" \
  --checking "GitHub README is ready"
```

Clean temporary workspace residue:

```bash
bash scripts/cleanup_trash.sh
```

### Verification

Minimal local checks:

```bash
python3 -m py_compile scripts/*.py
bash -n install.sh scripts/cleanup_trash.sh
python3 scripts/unified_time.py --target current
python3 scripts/progress_report.py \
  --state completed \
  --project openclaw-company-management \
  --action "dry run" \
  --checking "dry run"
```

Submit a structured request to Main from a Company Kernel employee:

```bash
python3 scripts/request_main.py \
  --agent codex \
  --request-type ops_request \
  --objective "route future approvals to Telegram" \
  --requested-action "notify the owner through Telegram when approval is required" \
  --apply
```

`request_main.py` resolves OpenClaw business agents from `agent_registry.json` and also accepts Company Kernel employees such as `codex`, `hermes`, `claude`, `trae`, `antigravity`, and `openclaw-main` even when they are not listed in the legacy OpenClaw registry.

Check the evolved Company Kernel heartbeat status from OpenClaw:

```bash
python3 scripts/company_kernel_bridge.py health
python3 scripts/company_kernel_bridge.py heartbeat-alert
```

`progress_report.py` runs as a dry run by default. Add `--apply` only when the report should be written into the configured OpenClaw agent bus.

Run an employee attendance sweep without trusting directory status:

```bash
python3 scripts/attendance_sweep.py sweep
```

The sweep classifies each employee as `online`, `session_missing`, `worker_stalled`, `heartbeat_disabled`, or `no_reply`. `online` requires a non-empty OpenClaw session store and a clear Telegram ingress spool; if an employee has pending or processing ingress files, it is reported as `worker_stalled`.

Run a local communication smoke without restarting existing services:

```bash
python3 scripts/agent_comm_smoke.py --agents main,nestcar --line-account nestcar
```

This verifies Gateway reachability, the existing LINE webhook endpoint, attendance state, and real OpenClaw agent replies in one JSON evidence report.

Sync an approved Telegram OPS approval into the Codex queue without touching Telegram polling:

```bash
python3 scripts/approval_to_codex_queue.py \
  --task-id company-kernel-telegram-real-button-click-smoke \
  --json
```

This proves the approved-file to Codex queue path. It does not claim Codex has sent a final Telegram reply; that requires a separate completion receipt from Codex or an OpenClaw-native outbound message path.

### Configuration

Common environment variables:

| Variable | Purpose |
| --- | --- |
| `OPENCLAW_ROOT` | OpenClaw root directory |
| `OPENCLAW_WORKSPACE` | Target workspace for installed scripts and config |
| `OPENCLAW_AGENT_BUS` | Agent-bus root for structured progress and requests |
| `OPENCLAW_SKILL_ACCOUNTS_DB` | Explicit SQLite database path |

Secrets, tokens, and passwords should stay outside the repository and be passed through environment variables or existing local credential storage.

---

## 中文

### 项目介绍

`openclaw-company-management` 是 OpenClaw 本机公司管理技能包，用于把多业务、多账号、多 Agent 的重复执行动作收敛成可安装、可审计、可长期维护的统一执行层。

本项目重点解决三件事：

- 所有业务动作先查统一账号与路由来源；
- 所有 Agent 交接必须带结构化状态；
- 本机工作区持续保持可运行、可清理、可验证。

它默认服务本机 OpenClaw 工作区，也支持通过环境变量部署到其他机器或其他 workspace。

### 核心特性

- **统一执行入口**
  提供统一时间查询、浏览器执行、外发动作、Main 请求、agent-bus 消费等脚本入口。

- **单一事实源**
  通过 `skill_accounts.db` 与 `templates/skill_accounts.sql` 管理业务账号、平台路由、业务配置与授权关系。

- **账号与路由硬约束**
  使用 SQLite 唯一约束，避免同一业务、平台、动作被重复注册成多条不确定路径。

- **Agent 通信契约**
  使用 `acknowledged`、`in_progress`、`blocked`、`completed` 等明确状态，减少无证据交接。

- **Company Kernel 桥接**
  提供 OpenClaw 侧轻量健康桥，统一读取 Company Kernel `doctor --summary` 与运行时心跳告警，让新版心跳机制能被 OpenClaw 继续监控，避免重复实现内核逻辑。

- **审批到 Codex 队列桥接**
  将 OpenClaw OPS 已批准审批文件同步到 `codex-queue`，并写入 Codex 侧回执；不启动独立 Telegram Bot API polling watcher，避免抢占 OpenClaw 原生 Telegram 消息流。

- **零垃圾定时清理**
  `scripts/cleanup_trash.sh` 可用于定时清理 `tmp`、`logs` 中过期调试、失败、补丁残留文件。

- **可迁移工作区默认值**
  在主力本机默认指向 `/Users/owner/openclaw/workspace-xmanx`，其他环境默认使用 `${HOME}/openclaw/workspace-main`，并支持环境变量覆盖。

- **可安装技能包**
  `install.sh` 会初始化数据库结构，并把统一执行器同步到目标 OpenClaw 工作区。

### 目录结构

```text
openclaw-company-management/
├── README.md                         # 中英文上架说明
├── SKILL.md                          # OpenClaw 技能说明
├── install.sh                        # 数据库与脚本安装器
├── PROJECT_STATE.md                  # 项目状态记录
├── codex-queue/                      # Codex 任务卡与报告
├── scripts/
│   ├── agent_bus_worker.py           # agent-bus inbox worker
│   ├── agent_comm_contract.py        # Agent 通信字段校验
│   ├── approval_to_codex_queue.py    # OPS 审批 -> Codex 队列桥接
│   ├── agent_registry.py             # Agent 注册表辅助脚本
│   ├── cleanup_trash.sh              # 零垃圾清理脚本
│   ├── progress_report.py            # 结构化进度上报
│   ├── request_main.py               # 向 Main 提交请求
│   ├── skill_accounts_db.py          # SQLite 账号与路由查询
│   ├── unified_browser.py            # 统一浏览器动作入口
│   ├── unified_outbound.py           # 统一外发动作入口
│   └── unified_time.py               # 统一时间查询
├── templates/
│   └── skill_accounts.sql            # SQLite 表结构与约束
└── tests/
    └── test_skill.sh                 # 基础验证脚本
```

### 部署

在项目根目录执行：

```bash
./install.sh
```

默认部署目标：

- 主力本机：`/Users/owner/openclaw/workspace-xmanx`
- 其他环境：`${HOME}/openclaw/workspace-main`

如需指定路径：

```bash
OPENCLAW_ROOT=/path/to/openclaw \
OPENCLAW_WORKSPACE=/path/to/workspace \
./install.sh
```

安装器会创建或更新：

- `${OPENCLAW_WORKSPACE}/config/skill_accounts.db`
- `${OPENCLAW_WORKSPACE}/scripts/*.py`
- `${OPENCLAW_WORKSPACE}/scripts/cleanup_trash.sh`

### 使用方法

查询业务账号路由：

```bash
python3 scripts/skill_accounts_db.py get \
  --business nestcar \
  --platform line \
  --action send
```

查询当前时间：

```bash
python3 scripts/unified_time.py --target current
```

上报 Agent 进度：

```bash
python3 scripts/progress_report.py \
  --state completed \
  --project openclaw-company-management \
  --action "release documentation updated" \
  --checking "GitHub README is ready"
```

清理临时残留：

```bash
bash scripts/cleanup_trash.sh
```

### 验证

最小验证命令：

```bash
python3 -m py_compile scripts/*.py
bash -n install.sh scripts/cleanup_trash.sh
python3 scripts/unified_time.py --target current
python3 scripts/progress_report.py \
  --state completed \
  --project openclaw-company-management \
  --action "dry run" \
  --checking "dry run"
```

`progress_report.py` 默认是 dry run；只有加 `--apply` 才会写入配置的 OpenClaw agent bus。

把已通过的 Telegram OPS 审批同步到 Codex 队列，且不接管 Telegram polling：

```bash
python3 scripts/approval_to_codex_queue.py \
  --task-id company-kernel-telegram-real-button-click-smoke \
  --json
```

这只能证明“审批文件已进入 Codex 侧队列”。它不等于“Codex 已自动发 Telegram final reply”；后者需要 Codex 完成回执或接入 OpenClaw 原生外发通道。

### 配置

常用环境变量：

| 变量 | 用途 |
| --- | --- |
| `OPENCLAW_ROOT` | OpenClaw 根目录 |
| `OPENCLAW_WORKSPACE` | 脚本与配置安装目标 workspace |
| `OPENCLAW_AGENT_BUS` | 结构化进度与请求写入的 agent-bus 根目录 |
| `OPENCLAW_SKILL_ACCOUNTS_DB` | 显式指定 SQLite 数据库路径 |

密钥、token、密码不应写入仓库，应通过环境变量或本机既有凭证存储传入。

### Release Status

This repository is ready for official GitHub listing as a local OpenClaw company-management skill package. The current release focuses on documentation, installer behavior, database constraints, and operational scripts. Business-specific secrets and account data are intentionally kept outside the repository.
