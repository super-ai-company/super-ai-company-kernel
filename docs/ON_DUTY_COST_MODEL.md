# Employee on-duty cost model · 员工在岗成本模型

**An employee can stay on duty for a week (or forever) at ZERO token cost.** This is a core design
property — detection and presence are free; tokens are spent only when an employee does real work.

## The rule

| Activity | How it works | Token cost |
|---|---|---|
| **Presence / 在岗** | the daemon writes a SQL heartbeat each tick (`INSERT INTO heartbeats`) | **0** |
| **Task detection / 检测任务** | daemon polls `SELECT … FROM tasks WHERE status='submitted'` per tick | **0** |
| **Message / handoff delivery** | written to the recipient's inbox + `company_events` (SQL/file) | **0** |
| **Executing a real task** | the adapter invokes the runtime's LLM (`codex exec`, `claude -p`, …) | tokens (only when work exists) |
| **Speaking in a meeting** | each turn spawns the runtime | tokens (only during the meeting) |

### Why a week of idle = 0 tokens

A 30-second daemon tick → ~20,160 ticks/week. Each idle tick is **one SQL query + one SQL heartbeat
write, no LLM call**. The runtime's model is invoked *only* when `next_task()` returns an actual task.
No task → the adapter returns `"no submitted task"` immediately, having spent nothing.

## Do this / don't do this

- ✅ **Run employees as daemon workers** (`company-add-employee … --enable-worker`, or `companyctl init`).
  The daemon keeps them on duty and detects work for free. This is the token-free "always checking" you want.
- ✅ Internal coordination — who has which task, status, handoffs, delivery notices — flows for free
  (SQL + events). Only an actual LLM *response* costs tokens.
- ❌ **Do NOT make an interactive app re-run `list_my_tasks` every conversation.** That burns tokens on
  every chat even when there's no work — the one pattern to avoid. The daemon worker twin (`<rt>-cli`)
  already does the same detection for free; the interactive check-in is a redundant, expensive copy.
- The `install-integration` block is **capability-awareness only** (the agent knows it *can* use the
  kernel) — deliberately not a forced per-turn check-in, to keep interactive use cheap.

## TL;DR

On duty + internal coordination = **free** (SQL). Producing intelligence (executing/responding) = tokens,
and only then. Keep employees as daemon workers; never wire a per-conversation check-in into an
interactive app.
