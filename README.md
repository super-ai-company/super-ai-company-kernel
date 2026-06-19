# Company Kernel · 超级 AI 公司内核

> **A governance kernel that turns any AI agent (Codex, Claude, Hermes, OpenClaw, Trae, Antigravity, or your own) into a managed "employee"** — with one shared task protocol for messaging, task routing, approvals, locks, audit, and recovery. Cross-platform (Windows / Linux / macOS). **v1.0**.
>
> **把任意 AI 智能体（Codex、Claude、Hermes、OpenClaw、Trae、Antigravity 或自建）统一为"公司员工"的治理内核** —— 用一套任务协议管理通信、派活、审批、锁、审计与恢复。跨平台（Windows / Linux / macOS）。**v1.0**。

English | [中文](#中文说明)

---

## English

### What it is

Modern AI tools are each powerful but isolated: each has its own rules, state, and task queue. Company Kernel pulls "how the company runs" out of any single tool. Tools only **execute tasks** — they cannot change company rules, the communication protocol, approval policy, or the underlying state. Any tool can crash, go offline, or upgrade without destroying the company itself.

### Key features

- **Unified employee model** — register any runtime as an employee with one command; adding an agent is a command, not a code change.
- **Task protocol** — submit / claim / done / block with mandatory evidence; event-driven hooks.
- **Meetings** — beyond one-off tasks, employees hold real multi-round meetings. `companyctl conversation run` spawns an autonomous discussion (each employee speaks, a chair writes the minutes/decision); a memory-tied meeting reads the project digest and writes its conclusion back. An employee stuck on a hard call can convene its own meeting (`companyctl meeting request`, also an MCP tool) instead of guessing — then poll `meeting result` for the verdict. Start one from the console's **meeting room** (🚀 发起会议) or the CLI/MCP.
- **Verdict gate** — a worker's exit code 0 is not "done"; the agent must emit `STATUS: completed` / `STATUS: blocked - <reason>`, otherwise the task blocks for human review (no fake completions).
- **Per-task workspace** — a task can target a specific repo via `工作区: /abs/path`; the kernel validates it and refuses to let workers modify the kernel itself.
- **Governance** — high-risk actions (external send, deploy, kernel change) require owner approval; rule changes go through RFCs.
- **Reliability** — execution timeout, retry policy, stale-task watchdog, honest heartbeats.
- **Security & ops** — opt-in Bearer-token gateway auth, automatic SQLite online backup + guarded restore.
- **Live console** — employees on-duty, task kanban, messaging, the meeting room, approvals, and a unified **company activity feed** (📜 who dispatched / ran / met / said what, in one stream); served by the API gateway.
- **Interfaces** — `companyctl` CLI, REST / JSON-RPC / gRPC gateways, launchd/systemd/Task-Scheduler daemon.

### 30-second quick start

```bash
git clone https://github.com/super-ai-company/super-ai-company-kernel.git
cd super-ai-company-kernel
export OPENCLAW_COMPANY_KERNEL_ROOT="$PWD"      # Windows: see onboarding guide
bin/companyctl doctor --summary                  # self-check (Win: python -m company_kernel.companyctl ...)
bin/company-add-employee --id codex --name Codex --role developer \
  --runtime codex --workspace <your-repo> --enable-worker --execute
bin/company-api-gateway --port 8765              # open http://127.0.0.1:8765/
```

### Onboard any agent

One command per employee. Supported runtimes: `codex`, `claude`, `hermes`, `openclaw`, `trae`, `antigravity`, `local` (generic / any custom agent). Full Windows/Linux/macOS matrix and per-runtime notes:
**[docs/AGENT_ONBOARDING.md](docs/AGENT_ONBOARDING.md)**.

### Going to production

- Enable auth: `export COMPANY_KERNEL_API_TOKEN="<random>"` — all API write/data endpoints then require `Authorization: Bearer`.
- Backups: on by default (daemon every 24h); manual via `bin/company-backup snapshot|list|restore`.
- The gateway binds `127.0.0.1` by default; expose with `--host 0.0.0.0` **only** after setting a token.
- Readiness assessment: [docs/GO_LIVE_READINESS.md](docs/GO_LIVE_READINESS.md).

### Tests

```bash
python3 -B -m unittest discover -s tests      # baseline: 539 passing
```

### Documentation index

| Doc | What |
|---|---|
| [docs/AGENT_ONBOARDING.md](docs/AGENT_ONBOARDING.md) | Cross-platform onboarding for every agent |
| [docs/GO_LIVE_READINESS.md](docs/GO_LIVE_READINESS.md) | Production readiness + upgrade backlog |
| [docs/COMPLETION_REPORT.md](docs/COMPLETION_REPORT.md) | Delivered capabilities |
| [docs/SUPER_AI_COMPANY_GOAL.md](docs/SUPER_AI_COMPANY_GOAL.md) | Project vision |
| [docs/RUNTIME_ADAPTERS.md](docs/RUNTIME_ADAPTERS.md) | Adapter design |
| [DELIVERY.md](DELIVERY.md) | Change log / delivery notes |

---

## 中文说明

### 这是什么

现在的 AI 工具各自都很强，但彼此孤立：每个都有自己的规则、状态和任务队列。Company Kernel 把"公司怎么运转"从任何单个工具里抽离出来——工具只负责**执行任务**，不能修改公司制度、通信协议、审批规则或底层状态。任何工具崩溃、离线或升级，都不会摧毁公司本身。

### 核心能力

- **统一员工模型** —— 一条命令把任意运行时注册为员工；新增智能体是命令，不是改代码。
- **任务协议** —— 提交/领取/完成/阻塞，强制证据；事件驱动钩子接力。
- **开会** —— 不止派单：员工能开真正的多轮会议。`companyctl conversation run` 起一场自主讨论（每个员工发言、主持人收口出纪要/决策）；绑定项目的会议会读取项目记忆摘要、并把结论写回。员工卡在难决策上时，可自己召集会议（`companyctl meeting request`，也是 MCP 工具）而不是瞎猜，再用 `meeting result` 轮询结论。控制台**会议室**（🚀 发起会议）或 CLI/MCP 都能发起。
- **裁决门** —— worker 退出码 0 不等于"完成"；智能体必须输出 `STATUS: completed` 或 `STATUS: blocked - 原因`，否则任务进入受阻待人工复核（杜绝假完成）。
- **任务级工作区** —— 任务可用 `工作区: /绝对路径` 指定目标仓库；内核校验路径并禁止 worker 改动内核自身。
- **治理** —— 高风险动作（外发、部署、改内核）需 owner 审批；规则变更走 RFC。
- **可靠性** —— 执行超时、重试策略、断链看门狗、诚实心跳。
- **安全与运维** —— 可选 Bearer-token 网关鉴权、SQLite 自动在线备份 + 守护式恢复。
- **实时控制台** —— 员工在岗、任务看板、消息、**会议室**、审批，以及统一的**全公司动态总流**（📜 谁派活 / 谁执行 / 谁开会 / 谁说了啥，汇成一条流），由 API 网关直接提供。
- **接口** —— `companyctl` 命令行、REST / JSON-RPC / gRPC 网关、launchd/systemd/任务计划程序常驻。

### 30 秒上手

```bash
git clone https://github.com/super-ai-company/super-ai-company-kernel.git
cd super-ai-company-kernel
export OPENCLAW_COMPANY_KERNEL_ROOT="$PWD"      # Windows 见接入指南
bin/companyctl doctor --summary                  # 自检（Windows: python -m company_kernel.companyctl ...）
bin/company-add-employee --id codex --name Codex --role developer \
  --runtime codex --workspace <你的代码仓库> --enable-worker --execute
bin/company-api-gateway --port 8765              # 浏览器开 http://127.0.0.1:8765/
```

### 接入任意智能体

每个员工一条命令。支持的运行时：`codex`、`claude`、`hermes`、`openclaw`、`trae`、`antigravity`、`local`（通用 / 任意自建智能体）。完整的 Windows/Linux/macOS 矩阵与各运行时要点见：
**[docs/AGENT_ONBOARDING.md](docs/AGENT_ONBOARDING.md)**。

### 上线生产

- 开启鉴权：`export COMPANY_KERNEL_API_TOKEN="<强随机串>"` —— 之后所有 API 数据/写操作都需 `Authorization: Bearer`。
- 备份：默认开启（daemon 每 24h）；手动 `bin/company-backup snapshot|list|restore`。
- 网关默认只绑 `127.0.0.1`；要对外暴露请**先设 token** 再 `--host 0.0.0.0`。
- 上线就绪评估见 [docs/GO_LIVE_READINESS.md](docs/GO_LIVE_READINESS.md)。

### 测试

```bash
python3 -B -m unittest discover -s tests      # 基线：539 通过
```

### 文档索引

| 文档 | 内容 |
|---|---|
| [docs/AGENT_ONBOARDING.md](docs/AGENT_ONBOARDING.md) | 跨平台全智能体接入指南 |
| [docs/GO_LIVE_READINESS.md](docs/GO_LIVE_READINESS.md) | 上线就绪评估 + 升级清单 |
| [docs/COMPLETION_REPORT.md](docs/COMPLETION_REPORT.md) | 已交付能力 |
| [docs/SUPER_AI_COMPANY_GOAL.md](docs/SUPER_AI_COMPANY_GOAL.md) | 项目愿景 |
| [docs/RUNTIME_ADAPTERS.md](docs/RUNTIME_ADAPTERS.md) | 适配器设计 |
| [DELIVERY.md](DELIVERY.md) | 交付与变更记录 |

---

## License / 许可

See repository owner. 详见仓库所有者说明。
