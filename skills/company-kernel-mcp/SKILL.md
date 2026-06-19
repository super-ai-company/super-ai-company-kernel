---
name: company-kernel-mcp
description: How the interactive apps (Codex / Claude / Antigravity) use Company Kernel natively through its MCP server — the seven tools, per-app registration, the visible per-step check-in loop, and how high-risk dispatch flows through owner approval into auto-execution. Use when wiring an app to the kernel, asking "how does Codex/Claude/Antigravity pick up tasks", making the interaction visible in chat, or when an app isn't seeing its tasks.
---

# Company Kernel via MCP (the apps' native interface)

The daemon drives the *headless* runtimes. The three **interactive apps** the owner actually types in
— **Codex app, Claude app, Antigravity app** — are kernel employees too (`codex` / `claude` /
`antigravity`), but they're conversational: they don't poll. They reach the kernel through the
**`company-kernel` MCP server** (`company_kernel/mcp_server.py`, wrapper `bin/company-kernel-mcp`),
a dependency-free stdio JSON-RPC server that shells out to the absolute `companyctl`.

Why MCP and not pasted prompts: the tool calls **render inline in the conversation**, so the owner
sees, in the chat record, exactly what each app received, did, and reported back — receive →
execute → feedback, all visible. That visibility is the point.

## The seven tools

`agent` is always the app's own id (`codex` / `claude` / `antigravity`).

| Tool | What it does |
|---|---|
| `list_my_tasks(agent)` | tasks assigned to you still needing work (submitted/claimed) |
| `show_task(task_id)` | full detail — description, workspace, 超时, acceptance |
| `claim_task(agent, task_id)` | claim before working (takes a lock; no double-processing) |
| `report_done(agent, task_id, summary, evidence)` | mark done — `evidence` is an absolute file path |
| `report_blocked(agent, task_id, blocker)` | mark blocked with a concrete reason — never fake done |
| `dispatch_task(from_agent, to_agent, title, description)` | hand work to a colleague (codex backend / claude analysis / antigravity(agy) frontend review / hermes coordination). codex needs `工作区: /abs/repo`; big agy review needs `超时: 3600` |
| `check_completions(agent)` | results of tasks YOU dispatched — the kernel pushes a `result-*.json` to your inbox the instant they finish (no polling) |
| `start_meeting(from_agent, topic, participants, question, project?)` | stuck on a hard decision you can't make alone (e.g. a design fork)? convene a quick meeting with colleagues instead of guessing. Runs async in the background; reserve it for the few genuinely hard calls. Optional `project` ties the meeting to a project memory bank (reads its digest, stores the conclusion). |
| `meeting_result(conversation_id)` | poll a meeting you started for its conclusion (the chair's 方案/决策/纪要). Returns `done=false` while colleagues are still talking |

Note the apps **self-report** (`report_done`/`report_blocked`) because they execute interactively.
This differs from the headless adapter path, where the kernel reports for the runtime — don't carry
the "don't self-report" adapter rule into the apps.

## Registration — automated (recommended)

`companyctl employee install-integration --runtime <codex|claude|gemini> [--agent-id <id>]` writes BOTH
the MCP server entry **and** the "you are an employee" instruction block into that agent's own config
(idempotent, backs up first). `companyctl init` offers to do this per detected runtime. After it runs,
restart the app. This is what makes an agent *truly on-duty* — chatting with it, it knows it can use the
kernel — instead of only being listed in the kernel DB.

## Registration — manual (what the command writes, for reference)

(local, per app — restart the app to load)

| App | File | Key |
|---|---|---|
| Codex | `~/.codex/config.toml` | `[mcp_servers.company_kernel]` → command = `bin/company-kernel-mcp` |
| Claude | `~/.claude.json` | `mcpServers.company-kernel` |
| Antigravity | `~/.gemini/config/mcp_config.json` | `mcpServers.company-kernel` |

Each app also reads an instruction file teaching the loop below: Codex `~/.codex/AGENTS.md`,
Claude `~/.claude/CLAUDE.md`, Antigravity `~/.gemini/GEMINI.md` (if the IDE doesn't auto-read it,
paste the loop into its Rules panel). These are **local, never committed.**

## The visible check-in loop (what each app does every conversation)

Narrate every step in one short line so the flow shows up in chat:

1. `list_my_tasks` → 「📥 有 N 个待办」or「无待办」.
2. Per task: `claim_task` → (`show_task`) → do it in the workspace → `report_done`/`report_blocked`,
   narrating「✅ 已认领 #id / 🔧 执行中… / ✅ 完成 #id:摘要 / ⛔ 受阻 #id:原因」.
3. `check_completions` → 「📨 你派的 #id 回来了:<status> <summary/blocker>」.
4. Nothing pending → 「无待办、无新完成」, then continue normally.

## High-risk dispatch → approval → auto-execute

A `dispatch_task` whose text hits a sensitive keyword (支付 / 外发 / 部署 / 密钥 …) is **gated**: the
kernel creates an **approval request** instead of a task, and the owner sees it (console 审批 tab /
Telegram). When the owner approves, the kernel **auto-materializes the held task** — it becomes a
real `submitted` task and runs. Approving never deletes the work, and no re-submit is needed. See
[dispatch-to-employees](../dispatch-to-employees/SKILL.md).

## Fallback (MCP not loaded) + pitfalls

If the MCP server isn't available, the same loop works via the absolute CLI
`/Users/<user>/openclaw/company-kernel/bin/companyctl task list/claim/show/done/block/submit …`.
When calling runtime CLIs (`claude`/`codex`/`agy`) directly, use the **absolute binary** or
`command <cmd>` — a bare name can hit a shell wrapper like `claude --bare` that ignores OAuth login
and falsely reports "not logged in". `companyctl` is already absolute and immune.

## If an app isn't seeing its tasks

- Restart the app after editing its MCP config (config is read at startup).
- Confirm the server runs: pipe `{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}` into
  `bin/company-kernel-mcp` — it should return `serverInfo.name = "company-kernel"`.
- Check the app is using the right `agent` id (codex/claude/antigravity), not a guessed one.
