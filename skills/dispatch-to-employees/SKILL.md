---
name: dispatch-to-employees
description: How to dispatch work to each AI employee/runtime in Company Kernel — which employee fits, the exact submit command, required directives (codex 工作区:, 超时:), per-runtime gotchas, and how external apps (Codex/Antigravity apps) drive them. Use when assigning a task, "派任务", choosing who should do something, or when a dispatch failed (timed out, ran in /tmp, "not executed", agy didn't change code).
---

# Dispatching work to AI employees

An employee executes work through its **runtime**. Dispatch the same way for everyone; the
*content* (directives, scope) is what differs. Get the directives wrong and the task times out,
runs in the wrong place, or silently does nothing.

## Three ways to dispatch

1. **Native MCP tool** (the interactive apps — Codex / Claude / Antigravity): they have the
   `company-kernel` MCP server registered, so they dispatch with the `dispatch_task` tool
   (`from_agent`, `to_agent`, `title`, `description`) — and track results with `check_completions`.
   The tool call renders in the conversation, so receive→execute→feedback stays visible. **Preferred
   for the apps.** See [company-kernel-mcp](../company-kernel-mcp/SKILL.md).
2. **CLI / API** (you, or an orchestrating employee like codex):
   ```
   companyctl task submit --from <who> --to <employee> --title "…" --description "…"
   ```
3. **File-drop bridge** (CI / other external apps): drop JSON into
   `state/task-intake/incoming/` → auto-submitted. See [onboard-employee](../onboard-employee/SKILL.md) §11.

The daemon then dispatches submitted tasks to each employee's worker automatically (no manual run).

## High-risk dispatch → owner approval → auto-execute

If the title/description hits a sensitive keyword (支付 / 外发 / 部署 / 密钥 / 赔偿 … see
`config/policy.json` `route_approval.actions`), the submit is **gated**: no task is created yet —
only an **approval request** (returned `rc=2`, surfaced in the console 审批 tab + Telegram). This is
the safety闸, not a failure.

**The owner approving the request now materializes the task automatically** — it becomes a real
`submitted` task for the target and the daemon/app executes it. The approval semantic is "hold, then
**must run once approved**" — approving never deletes the work. You do **not** re-submit after
approval (that old two-step flow is gone); re-approving is idempotent — no duplicate task.

## Pick the right employee

| Employee / runtime | Best for | Returns | Edits code? |
|---|---|---|---|
| **codex** | backend / coding / bugfix | code changes on a branch + self-verdict | ✅ yes |
| **claude** | analysis, code review, writing, planning | report + evidence | ✅ yes (with execute) |
| **gemini** | PM / UX (via a Claude-compatible proxy) | report | ✅ yes |
| **antigravity (agy)** | **frontend / UI review & suggestions** (headless `agy --print`) | concrete suggestions w/ file:line | ❌ **review only — does NOT change code** |
| **openclaw agents** (nestcar, chindahotpot, krothong, …) | customer comms relay (LINE / Telegram) | message delivery / replies | ❌ relay only |
| **hermes** | supervision, PM, meeting chair / minutes | summaries, minutes | ❌ orchestrates |

## Per-employee dispatch + gotchas

### codex — must carry a workspace
- `工作区: /abs/repo` is **mandatory** in the description, or codex runs in `/tmp` and the task is
  blocked by the submit guard. Optional `超时: 3600` for heavy ETL/builds (cap 1 h).
- See [dispatch-task-to-codex](../dispatch-task-to-codex/SKILL.md).
```
companyctl task submit --from owner-shift --to codex --title "fix login" \
  --description "工作区: /Users/you/app
超时: 1800
修复登录页 race condition 并跑测试。"
```

### antigravity (agy) — review only, headless, give it time
- Runs headless via `agy --print` (managed-attempt). It **gives suggestions; it does NOT edit code** —
  you (or codex) implement the worthwhile ones. Don't expect a branch back.
- **Big reviews need a long timeout.** Managed reviews floor at **30 min** and honor a `超时:`
  directive up to **1 h**. The old 120 s default is what timed out multi-screen reviews
  (e.g. "审核 S01-S15"). For wide scopes, add `超时: 3600`.
- **Reviews run in agy's configured workspace automatically** — set it once
  (`companyctl employee update --id antigravity --workspace /abs/frontend-repo`) and you never paste
  absolute paths per task. A per-task `工作区: /abs/repo` directive overrides it for a one-off review.
```
companyctl task submit --from codex --to antigravity --title "前端审核 主收银端 S01-S15" \
  --description "超时: 3600
审查 android-pos 的 S01-S15 各屏(agy 在其 workspace 里跑,无需贴绝对路径)。
从①视觉层级②可访问性(对比度/键盘/焦点)③商用专业度给每屏 3-5 条带 file:line 的可执行建议。只审,不改码。"
```

### claude — analyst; absolute repo path
- Managed as `--execute --permission-mode bypassPermissions`. Workspace tasks need an absolute repo
  path (same /tmp pitfall as codex). Activation is manual (runtime-evidence, not password echo) —
  profile may set `auto_recover:false`.

### gemini — proxy must be up
- A Claude-compatible proxy runtime (default `http://localhost:8080`). If the proxy is down it
  auto-pauses; the daemon's `presence.recover-unavailable` re-activates it once the proxy returns.

### openclaw agents — relay, not compute
- nestcar / chindahotpot / krothong / invest / video-* relay to LINE/Telegram. Dispatch a message
  task; route to a group with `--deliver-to`. They don't run code.

### hermes — supervisor / chair
- Use as a meeting chair (writes minutes) or PM. Drives others; not a code executor.

## When a dispatch fails — fix table

| Symptom | Cause | Fix |
|---|---|---|
| agy review **timed out** | scope too big for default timeout | add `超时: 3600`; managed reviews already floor at 30 min |
| task **"not executed" yet** | daemon runs 1 managed task/employee/tick | it runs on the next tick; or run the adapter once manually |
| codex **blocked**, ran in /tmp | missing `工作区:` | add `工作区: /abs/repo` |
| **duplicate / just-discarded** rejected | submit guard 60-min cooldown | `--force` to override |
| employee **未知运行时** + auto-paused | runtime not in `KNOWN_RUNTIMES` | register the runtime (onboard §0) |
| **approved an approval but no task appeared** | pre-fix kernel only flipped the approval status, never created the held task | fixed: approving now auto-materializes & runs the task. On an old build, upgrade — do **not** re-submit (that re-creates duplicates) |
| config edit **no effect** | daemon reads config at start | restart daemon (onboard §6) |

## Boundaries
- agy gives frontend suggestions only — **don't have it touch backend/admin code**; pick the
  Android/phone-side suggestions to implement and leave the rest.
- Match the employee to the work: code → codex/claude; UI review → agy; customer comms → openclaw;
  coordination/minutes → hermes.
