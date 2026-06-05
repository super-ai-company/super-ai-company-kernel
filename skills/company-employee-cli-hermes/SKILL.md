---
name: company-employee-cli-hermes
description: Use when onboarding or operating Hermes as a Company Kernel supervisor/PM employee, including direct communication through OpenClaw/Hermes runtime, heartbeat checks, human reply routing, approval mediation, and closed-loop multi-round coordination.
---

# Company Employee: Hermes

Hermes is the supervisor/PM employee for Company Kernel installs. Hermes is not just another worker: it validates local employees, mediates approval-sensitive flows, coordinates human-facing updates, and closes the loop between the human, Company Kernel, Codex, Antigravity, and OpenClaw business agents.

## One-Sentence Onboarding

“Onboard Hermes as the default validation admin, verify real direct replies through the default Hermes/OpenClaw runtime session, lock its human reply target, and use it to run heartbeat plus multi-round employee handshakes before marking other employees active.”

## What Hermes Controls

Hermes controls the company through these surfaces:

1. **Direct runtime message**
   - Company Kernel command: `bin/companyctl message direct --from main --to hermes --body "只回复：HERMES_DIRECT_OK"`
   - Runtime route: `openclaw agent --agent default --session-key agent:default:<source> --message ... --json`
   - Why `default`: on a normal Hermes install, the live Hermes session is profile `default`, while the Company Kernel employee id is `hermes`.

2. **Task adapter**
   - Adapter: `bin/company-hermes-adapter --agent hermes`
   - Dry-run creates prompt/report only.
   - `--execute` runs `hermes -z <prompt>` inside the Hermes workspace.

3. **Heartbeat / attendance**
   - Local heartbeat record: `bin/companyctl heartbeat --agent hermes`
   - Real reply probe: `bin/companyctl attendance --agent hermes --reply-probe --timeout 60` when available, or direct smoke with an exact token.
   - Heartbeat alone is not control. It only says a record was written. Control requires an exact runtime reply or a structured blocked reply.

4. **Human reply routing**
   - Employee profile should store the current human channel when known:
     - `default_user_reply_channel`: `telegram`
     - `default_user_reply_account`: `default`
     - `default_user_reply_to`: actual owner chat id
     - `default_user_reply_deliver`: `true|false`
   - Do not invent these for customer installs. If unknown, keep `deliver=false` and use the current requesting agent as the reply surface.

## Required Checks

1. Verify commands:

```bash
command -v hermes
command -v openclaw
bin/companyctl runtime doctor --runtime hermes
```

2. Register/update Hermes:

```bash
bin/companyctl employee create --id hermes --name Hermes --role supervisor --runtime hermes --workspace "$HERMES_HOME"
```

If `HERMES_HOME` is unset, use the user’s Hermes home, commonly `~/.hermes`, but resolve to an absolute path before writing the profile.

3. Verify direct exact reply:

```bash
bin/companyctl message direct --from main --to hermes --body "只回复：HERMES_DIRECT_OK" --timeout 90
```

Pass criteria:

- `ok=true`.
- `runtime=hermes`.
- `agent_runtime_id=default` unless the customer configured a different Hermes profile.
- reply contains the exact token.
- a receipt message from `hermes` back to `main` is recorded.

4. Verify 3-round control before activation:

```bash
bin/companyctl employee verify-direct --id hermes --from main --rounds 3 --activate
```

## Multi-Round Control Loop

Use Hermes as the default installer/validation admin after install or repair:

1. `main -> hermes`: exact identity reply.
2. `main -> hermes`: ask for environment summary: Hermes home, profile, gateway availability, human reply route, known blockers.
3. `main -> hermes`: ask Hermes to validate one worker by direct smoke, normally Codex first.
4. Optional: ask Hermes to summarize next employee activation gates.

A successful loop must produce a receipt back to `main` for every round. If Hermes only writes inbox files or only updates heartbeat, the loop failed.

## Customer Install Contract

For a new customer machine:

1. Discover Hermes home/profile rather than assuming this Mac’s paths.
2. Register Hermes as `candidate` first.
3. Run exact direct smoke.
4. Lock the owner reply route only if provided by the current channel/config.
5. Run Hermes-led handshakes for reachable employees.
6. Promote employees to `active` only after real replies.
7. Produce a report listing:
   - Hermes home/profile
   - direct command used
   - direct reply result
   - heartbeat result
   - human reply route status
   - each worker’s active/candidate/blocked state

## Approval and Safety Mediation

Hermes is the right first hop for approval-sensitive checks. If Codex or Antigravity says a task is blocked by approval policy:

- Do not keep resending the same worker request.
- Ask Hermes for a read-only validation or for an approval task shape.
- Record the blocker with command, exit code, stderr/stdout, and next required action.
- Only after approval is explicit should an adapter execute write/deploy/restart actions.

## Hard Rules

- `message send` is record-only. It is not proof Hermes read anything.
- `heartbeat` is liveness metadata. It is not proof Hermes can be controlled.
- `message direct` or `POST /v1/messages/direct` is the control proof.
- Hermes employee id may be `hermes`, but live runtime id is usually `default`; do not create a fake `agent:hermes:*` runtime session when the system expects `agent:default:*`.
- Human-facing results must return to the requesting agent, then to the human. Do not leave the result only in Hermes internal outbox.
- Never store API keys, Telegram tokens, or other secrets in skills, reports, prompts, or employee profiles.

## Postmortem: 2026-06-04 Control Failure

Symptoms observed during early Company Kernel work:

- Messages were written to inbox files but Codex/Antigravity did not keep a sustained multi-round conversation.
- Heartbeat and app/CLI existence were mistaken for control.
- Hermes/current-user communication path was not modeled as a first-class employee route.
- Codex task execution hit approval/policy gates and missing progress-report helpers, so “task accepted” did not become “work progressing”.
- Antigravity/agy could return exact simple replies, but complex GUI/frontend execution lacked changed files plus browser evidence, so it could not be treated as autonomous implementation.

Root causes:

1. Record-only `message send` was confused with direct runtime invocation.
2. `active` status was too easy to infer from heartbeat/app presence.
3. Hermes runtime identity mapping was under-documented: Company employee `hermes` routes to runtime agent `default`.
4. No mandatory round-trip receipt rule existed for every internal request.
5. Progress evidence was not standardized across adapters.

Preventive rule:

A worker is not controllable until it completes 2-4 direct rounds with sender-visible receipts and, for execution tasks, writes repo-local progress evidence plus a final `done` or `blocked` receipt.

When Hermes supervises progress, use the normalized 5-layer protocol in heartbeat/progress artifacts:

- `received` -> `received|acknowledged|claimed`
- `working` -> `working|in_progress|actively_progressing`
- `waiting` -> `waiting|blocked_on_input_or_dependency`
- `blocked` -> `blocked|failed_to_progress`
- `done` -> `done|verified_complete|completed`
- 只要 layer 变化，Hermes/Kernel 应能读到对应 `progress.notification` 记录，并看到真实 `delivery_status`（`pending/sent/skipped/failed`）；只有 `sent` 才能宣称已通知到人。
