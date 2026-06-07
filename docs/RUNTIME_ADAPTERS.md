# Runtime Adapters

## Current Worker

当前最小 worker：

```bash
bin/company-adapter-worker --agent codex --dry-run
```

它会：

- 从 Company Kernel 领取该员工的一个 `submitted` 任务。
- 写入 `employees/<agent>/reports/<task-id>.adapter-report.md`。
- 用 `companyctl task done` 或 `companyctl task block` 回写状态。
- 写入 heartbeat。

`--dry-run` 不启动真实外部工具，适合安全验证。

## openclaw

入口：

```text
$OPENCLAW_ROOT/scripts/oc
$OPENCLAW_ROOT/ops/agent_bus
```

Company Kernel 不直接写真实业务 inbox。后续由 OpenClaw adapter 把内核任务转换成 OpenClaw 安全任务。

当前状态：

- `bin/company-openclaw-adapter --agent <openclaw-agent>` 已支持读取 Company Kernel 任务。
- 默认只生成 OpenClaw legacy bus payload 和 evidence，不写真实 `ops/agent_bus`。
- 加 `--execute` 后才调用 `$OPENCLAW_ROOT/scripts/oc bus submit`。
- `--execute` 受 `external_send` approval gate 保护；未批准只生成 pending approval 和 report，不写 legacy bus。
- 支持旧 bus agent：`main`、`nestcar`、`chindahotpot`、`invest`、`video-creator`、`video-publisher`、`video-ops`、`krothong`。

测试或 clone 到其他路径时可用 `OPENCLAW_COMPANY_KERNEL_ROOT` 指向 Company Kernel 根目录，用 `OPENCLAW_ROOT` 指向 OpenClaw 根目录。

边界：

- Company Kernel 是独立通信层，不替换 OpenClaw 原生 internal bus。
- OpenClaw 员工只能通过 `company-openclaw-adapter` 接收 Company Kernel 任务。
- `company-openclaw-adapter` 默认只写 evidence；只有 `--execute` 且 approval 通过后才提交到 OpenClaw。
- OpenClaw 自己的 session、bot、skill、hook、memory 仍由 OpenClaw 管，不由 Company Kernel 私自改。
- 如果 OpenClaw 原生 agent-to-agent 失败，先把失败写成 Company Kernel blocker，再由 main/owner 处理，不能假装已转交。

## hermes

入口：

```text
$OPENCLAW_HERMES_WORKSPACE
$HERMES_HOME
$HOME/.local/bin/hermes
$HERMES_TOOLS_HOME
```

Hermes 可以作为 supervisor 提交任务，但不能绕过审批和内核保护。

当前状态：

- `bin/company-hermes-adapter` 已支持从 Company Kernel 领取 Hermes 任务。
- 默认生成 `hermes -z` oneshot prompt 和 evidence，不启动外部 Hermes。
- 加 `--execute` 后才调用 `hermes -z <prompt>`。
- 遵守本机 Hermes 约束：不改远端 proxy、不重启容器、不切换 provider 配置。

## codex

入口：

```text
$OPENCLAW_CODEX_WORKSPACE
codex exec
codex review
```

Codex adapter 负责把任务转为 task card，收集输出、diff、测试结果，再回传 Company Kernel。

当前状态：

- `bin/company-codex-adapter` 已支持从 Company Kernel 领取 Codex 任务。
- 默认生成 task card 和 evidence，不启动外部 Codex。
- 加 `--execute` 后会运行 `codex exec`。
- 默认 sandbox 是 `read-only`，需要写代码时显式传 `--sandbox workspace-write`。
- `codex exec` 成功会完成 Company Kernel 任务并写 report；失败会把任务置为 blocked，并把 last message/events/report 路径保留下来。
- 测试或 clone 到其他路径时可用 `OPENCLAW_COMPANY_KERNEL_ROOT` 指向 Company Kernel 根目录，用 `OPENCLAW_CODEX_WORKSPACE` 指向 Codex 默认工作区。

Hermes PM 监督入口：

```bash
bin/company-codex-pm-supervisor --agent codex --stale-minutes 15
```

它只读取 Company Kernel 任务和 Codex workspace 的 `reports/progress_*.json`，不会修改 OpenClaw 内部通信能力。完成必须匹配当前 `task_id`；旧的 unrelated progress 文件不会被当成当前任务完成。

详细协议见：[CODEX_HERMES_PM_SUPERVISION.md](CODEX_HERMES_PM_SUPERVISION.md)。

## claude

入口待适配：Claude Code 或 Claude CLI。

当前状态：

- `bin/company-claude-adapter` 已支持从 Company Kernel 领取 Claude 任务。
- 默认生成 `claude -p` print prompt 和 evidence，不启动外部 Claude。
- 加 `--execute` 后才调用 `claude -p <prompt> --no-session-persistence --output-format text`。
- 默认 permission mode 是 `default`，需要其他权限必须显式传 `--permission-mode`。

## trae

入口：

```text
/usr/local/bin/trae
trae chat --mode ask|edit|agent <prompt>
```

当前状态：

- `bin/company-trae-adapter` 已支持从 Company Kernel 领取 Trae 任务。
- 默认生成 `trae chat` prompt 和 evidence，不启动外部 Trae。
- 加 `--execute` 后才调用 `trae chat --mode <mode> <prompt>`。
- 注意：Trae 是 IDE/GUI 型工具，真实执行可能打开或复用 Trae 窗口。

## antigravity

入口：

```text
/Applications/Antigravity.app
bundle id: com.google.antigravity
agy
```

当前状态：

- `bin/company-antigravity-adapter` 已支持从 Company Kernel 领取 Antigravity 任务。
- 默认生成 GUI task brief 和 evidence，不打开 App。
- 加 `--execute` 后只执行 `open -a Antigravity`。
- 若本机存在 `agy` CLI，direct message 会优先通过 `agy --print` 做真实回复和结构化证据校验。
- 简单 exact-token smoke 只能证明通信，不能证明 GUI/代码执行能力。
- GUI worker、`agy` 会话或人工脚本必须通过 `--complete --task-id <id> --summary ... --evidence ...` 或 `--block --task-id <id> --blocker ...` 把结果回传 Company Kernel。
- 复杂 GUI/frontend 任务必须返回 changed files、verification/browser evidence 和 blocker 字段；缺少这些时保持 `candidate` 或 `blocked`。
