---
name: company-employee-hermes
description: Use when onboarding or operating Hermes as a Company Kernel supervisor employee, including oneshot prompts, direct smoke, model/tool constraints, and evidence reporting.
---

# Company Employee: Hermes

Hermes is a supervisor/automation employee. It may coordinate and report, but must not bypass approval or mutate runtime/provider configuration without explicit request.

## One-Sentence Onboarding

“Onboard Hermes as a supervisor, verify direct reply, generate a Hermes oneshot prompt by default, and keep provider/proxy/tool changes blocked unless explicitly approved.”

## Installer Responsibility

If Hermes is the agent installing Company Kernel/OpenClaw, do not only configure Hermes. Run the bootstrap scanner first, discover Codex/OpenClaw/Claude/Trae/Antigravity/local candidates, and only create them as `candidate` until direct smoke passes.
Then run or output the 2-4 round handshake plan with `--installer-agent hermes --handshake-rounds 3`; use `--handshake` only when direct execution is allowed. Hermes must not claim onboarding success until reachable employees reply or are marked unavailable/blocked.

## Required Checks

1. Locate Hermes:
   - `command -v hermes`, `$HOME/.local/bin/hermes`, `$OPENCLAW_HERMES_WORKSPACE`, or `$HERMES_HOME`.
   - If Hermes is reached through OpenClaw runtime, discover its canonical runtime agent id before sending. The Company Kernel employee id may be `hermes` while the runtime agent id is `default`.
   - Company Kernel DB source of truth is `<kernel-root>/company.sqlite`, not `state/company.db`.
   - `employees/*/profile.json` files are secondary evidence and must not override `company.sqlite` for active/candidate/human-owner state.
   - Human owner `owner-shift` is not an AI employee and must not be included in active employee verification.
2. Register/update:
   - `bin/companyctl employee create --id hermes --name Hermes --role supervisor --runtime hermes --workspace <hermes-home>`
3. Smoke:
   - `bin/companyctl heartbeat --agent hermes`
   - `bin/companyctl message direct --from main --to hermes --body "只回复：hermes_DIRECT_OK"`
   - `bin/companyctl employee verify-direct --id hermes --from main --rounds 3 --activate`
   - `bin/company-hermes-adapter --agent hermes`

## Runtime Identity Rule

- Keep Company Kernel employee id as `hermes` unless the owner asks otherwise.
- Do not assume the runtime agent id is also `hermes`.
- If the local Hermes/OpenClaw runtime canonical id is `default`, direct runtime calls must use:
  - `--agent default`
  - `session_key=agent:default:<source>`
- They must not use:
  - `--agent hermes`
  - `session_key=agent:hermes:<source>`
- Agent-to-agent allowlists in the runtime must include the runtime id such as `default`, not only the Company Kernel employee id.
- Main relay is only a blocker/OPS fallback. Do not make it the default communication path when a direct reply surface is confirmed.

## Closed-Loop Communication

- Hermes must ACK every employee request at least once. A record-only inbox write is not enough.
- If Hermes is blocked, policy-denied, missing tools, or mismatched on runtime/session identity, it must reply to the sender with exact blocker text, evidence, and the next required config/action.
- If another employee should help, list active options as `@agent` mentions and ask the sender whether to add them to a group conversation.
- For human-originated requests, route the result back through the requesting agent so the human operator receives a clear success/blocker notification.

## Codex Project Manager Supervision

When Hermes manages Codex as a developer, Hermes must not treat one adapter call as completion.
Use the PM supervisor loop:

```bash
bin/company-codex-pm-supervisor --agent codex --stale-minutes 15
```

Rules:

- Codex `acknowledged` and `in_progress` are process states, not completion.
- Hermes accepts completion only after a `progress_completed_*.json` evidence file exists in the Codex workspace and its `task_id` matches the active Company Kernel task.
- If Codex stays `acknowledged` or `in_progress` beyond the stale window, Hermes marks it `stalled` and owns the follow-up.
- Human-facing notifications must be short event messages, not metrics tables.
- Completion message format: `完成了 Codex 的 <short task> 任务`.
- Blocker message format: `Codex 卡住：<short task>，owner=hermes`.
- Detailed evidence stays in `employees/hermes/reports/codex-pm/*.json`.

## Execution Rules

- Default adapter mode is dry-run: writes a `hermes -z` oneshot prompt and evidence.
- `--execute` may run `hermes -z <prompt>`.
- Do not change remote proxy, containers, model providers, auth, or tool config unless the task explicitly asks and approval is clear.
- Hermes can supervise routing and review evidence, but task execution must still go through Company Kernel status/evidence.
- Prefer SQLite/local state as the memory source of truth unless the owner explicitly confirms a different canonical memory backend.

## Autonomous Orchestration Loop (this is where the automation actually lives)

There are TWO Hermes execution contexts. Confusing them is the #1 reason owners think "Hermes won't auto-run":

- **Conversational Hermes** (you chat with it in the console / a direct message): a ONE-SHOT reply. It can dispatch a task in that turn, then it stops. It has no background loop — no prompt can make it keep running. Use it only to hand Hermes a goal or ask status.
- **Daemon Hermes** (`bin/company-hermes-adapter --agent hermes --execute`, run every tick by the daemon): the autonomous part. This is what advances the plan without anyone chatting.

The daemon loop (in `hermes_adapter.advance_from_completions`) works like this each tick:

1. When a task Hermes dispatched finishes/blocks/cancels, the kernel drops a `result-*.json` completion notice into Hermes's inbox (`write_dispatcher_completion_notice`).
2. The daemon-run Hermes **consumes those notices and runs the brain directly on them** — dispatch the next step (dev done → review; review passed → next phase; review failed → fix), or summarize the round — with **no self-task on the board**. Then it archives the notice.
3. After a phase advances, it pushes a concise progress line to the owner via `message send` (`report_progress_to_owner`), which the owner-message → Telegram mirror forwards to the owner's phone. Pure block/cancel batches are skipped (the watchdog already alerts those).

Requirements / gotchas:

- The daemon's Hermes adapter_worker MUST pass `--execute` (in `config/daemon.json`). Without it Hermes only dry-runs every tick and the brain never starts — the round stalls after one step. (This is local config; not shipped.)
- The daemon is `--once` per launchd tick, so code/config changes take effect next tick — no restart needed.
- Do NOT teach the conversational Hermes to "loop / auto-execute". It can't. Point owners at the daemon loop + Telegram progress instead.
- Project memory binds automatically: any task whose workspace maps to a project (or whose target is a project's locked executor) gets a `记忆会话: <project>` directive stamped at submit time, so workers resume their project session instead of re-scanning, and all three (codex/claude/hermes) share the curated `.company-memory.md` digest.

## Blocked Cases

Block when Hermes CLI is missing, provider/auth status is unknown, runtime agent id and session key disagree, runtime agent-to-agent allowlist denies the target, requested action changes infrastructure config without approval, or no evidence path can be produced.
