---
name: employee-communication
description: How every AI employee in Company Kernel communicates — employee↔employee, employee↔owner, and employee↔customer — across the four channels (messages, meetings/conversations, external delivery, owner notifications). Covers the open/strict policy, which medium each runtime actually uses, and how to restrict comms. Use when asking "can X talk to Y", "how do employees communicate", "派/发消息", setting up notifications, or restricting who can talk to whom.
---

# How employees communicate

Communication is **open by default**: `config/company_communications.json` → `policy.mode: open`
means **any registered employee can message or assign to any other (and the owner) unless a
`blocked_*` list denies it.** Nothing is blocked today — every employee can already reach every
other. `can_talk_to` only matters in `strict` mode (see *Restricting* below).

But "open" only removes the *gate* — the **medium** differs by runtime. Use the right one.

## The four channels

| Channel | Command / surface | What it does | Who it's for |
|---|---|---|---|
| **Message (record)** | `companyctl message send --from A --to B --body …` | logs a message A→B in the ledger | any → any (audit trail, hand-offs) |
| **Message (direct)** | `companyctl message direct …` | delivers so the recipient *receives and replies* | **only messageable runtimes** = openclaw agents |
| **Meeting / conversation** | console 会议室, or `companyctl conversation run` | structured multi-employee dialogue → minutes | **compute employees** (codex/claude/agy/gemini/hermes) align here |
| **External delivery** | `companyctl message channel-send` / task `--deliver-to <group>` | push to a LINE/Telegram customer group | **openclaw agents** (nestcar, chindahotpot, …) |
| **Owner notification** | `notification.routes` (Telegram) | pushes approvals/errors to the owner | everyone → owner |

## Who uses what (by runtime)

- **owner (you)** ↔ everyone: the **console** (@mention to dispatch, message center, 会议室),
  plus **Telegram** for approvals/errors coming back. You can reach anyone.
- **Compute employees** — codex, claude, antigravity (agy), gemini, hermes: they **act on Tasks and
  Meetings**, not live chat. `message send` to them is logged but they won't "reply" like a chat —
  to make them *do* something, dispatch a **task** (see [dispatch-to-employees](../dispatch-to-employees/SKILL.md))
  or pull them into a **meeting**. agy is review-only; hermes chairs/synthesizes.
- **openclaw agents** — nestcar, chindahotpot, krothong, invest, main, default, video-*: they have a
  **real chat channel** and relay to **LINE/Telegram**. Use `message direct` (they reply) or
  `channel-send` / `--deliver-to <group>` to reach customers.

## Interactive apps talk natively via MCP

The three apps the owner types in — **Codex / Claude / Antigravity** — are employees (`codex` /
`claude` / `antigravity`) that reach the kernel through the **`company-kernel` MCP server**:
`list_my_tasks` / `claim_task` / `report_done` / `dispatch_task` / `check_completions`. Each tool
call renders in the conversation, so the owner sees receive → execute → feedback in the chat record.
This is how those apps check in, pick up tasks, and report — see
[company-kernel-mcp](../company-kernel-mcp/SKILL.md). The headless runtimes still go through the
daemon + adapters.

## Reach the owner (notifications)

`notification.routes` currently routes **approval** and **error** events to the owner over Telegram
(per-employee bots; owner chat id in `config/secrets.env`). Add routes for other event types there if
you want more pushed to your phone. Approvals also surface in the console 审批 tab.

## Channels (group threads)

Named channels in the registry: **video-production**, **all-hands**. `message channel-send` posts to
a channel/group; meetings can target a set of participants for a one-off thread.

## Restricting comms (when you DON'T want everyone talking)

Open is the default. To lock it down:
1. Set `policy.mode: strict` (or `allowlist`) in `config/company_communications.json`.
2. Give each employee a `can_talk_to` / `can_assign_to` allowlist (or `blocked_talk_to` /
   `blocked_assign_to` to deny specific targets even in open mode).
3. In strict mode, a non-empty allowlist is enforced; an empty one still allows all (so set it
   explicitly to restrict).
`config/company_communications.json` is local/gitignored — never commit it.

## Quick verification

```
companyctl message send --from codex --to claude --body "ping"     # any→any, should be ok:true
companyctl conversation probe --participants active                # who can actually meet
companyctl employee show --id <id>                                 # see status + comms posture
```

## Known nuances (not blockers, but know them)

- **Result feedback varies by runtime** — some employees report completion cleanly, others less so;
  the console **完成回报** tab + evidence files are the source of truth, not a chat reply.
- Compute employees **don't chat back** — don't wait for a "reply" to `message send`; watch the task
  status / 完成回报 instead.
