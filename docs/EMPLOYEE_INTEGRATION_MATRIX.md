# Employee Integration Matrix

This matrix defines how each runtime becomes a Company Kernel employee without replacing its native communication system.

## Activation States

Every employee starts as `candidate`.

Promotion to `active` requires:

- runtime discovered;
- workspace resolved without hard-coded local paths;
- direct communication smoke passes for 2-4 rounds;
- at least one sender-visible receipt exists;
- execution tasks have durable evidence, not only chat text.

Repo-local runtime files under `employees/<agent>/` are host state, not portable release truth. If SQLite state and profile JSON disagree, the installer or operator must reconcile the employee before activation instead of treating either side as automatically authoritative.

## Runtime Matrix

| Employee | Runtime | Default role | Direct smoke | Execution evidence | PM supervision |
|---|---|---|---|---|---|
| `hermes` | `hermes` | supervisor / project manager | `bin/companyctl message direct --from main --to hermes --body "只回复：hermes_DIRECT_OK"` | `employees/hermes/reports/<task>/hermes-adapter-report.md` | owns Codex PM supervisor |
| `codex` | `codex` | developer / verifier | `bin/companyctl message direct --from main --to codex --body "只回复：codex_DIRECT_OK"` | `<codex-workspace>/reports/progress_*.json` and adapter report | `bin/company-codex-pm-supervisor` |
| `antigravity` / `agy` | `antigravity` | GUI / browser / product QA worker | `agy --print` direct smoke when available | structured direct report, changed files/browser evidence, final `--complete` or `--block` evidence | add only after durable status API or progress file exists |
| `openclaw-main` | `openclaw` | business control plane | `message direct` through OpenClaw adapter | OpenClaw adapter evidence or approved bus payload | Company Kernel bridge only; OpenClaw native supervision remains separate |
| business agents | `openclaw` | business employee | direct smoke by agent id | business workspace evidence | do not bypass business rules |
| `claude` | `claude` | coding/review worker | Claude adapter direct smoke | print prompt output / adapter report | add after direct receipt is stable |
| `trae` | `trae` | IDE worker | Trae adapter smoke | IDE prompt/report or explicit completion | add after GUI/CLI evidence is durable |
| local model agent | `local` | local worker | script smoke | script output/report | add custom supervisor only if needed |

## Communication Rules

Use these rules for all employees:

- `message send` is record-only unless the runtime adapter explicitly processes it.
- `message direct` is the control path for smoke and immediate replies.
- `task submit` is the durable work assignment path.
- `task done` requires evidence.
- `task block` requires one concrete blocker and next action.
- `heartbeat` is liveness only, not task completion.
- Human chat receives short event summaries, not raw counters or debug tables.

## OpenClaw Boundary

Do not modify OpenClaw's existing internal agent bus or channel behavior from Company Kernel.

Allowed bridge:

```text
Company Kernel task -> company-openclaw-adapter -> documented OpenClaw adapter execution path
```

Blocked by default:

- direct writes into OpenClaw private runtime state;
- changing OpenClaw bot sessions from Company Kernel;
- using OpenClaw business agent memory as Company Kernel source of truth;
- treating OpenClaw inbox/state files as Company Kernel completion evidence without adapter verification.

Detailed OpenClaw bridge contract: [OPENCLAW_COMPANY_BRIDGE.md](OPENCLAW_COMPANY_BRIDGE.md).

## Codex + Hermes Project Flow

Recommended project-manager flow:

```text
human/main creates project
-> hermes creates/splits tasks
-> codex claims bounded implementation task
-> codex writes progress JSON
-> hermes PM supervisor polls
-> hermes accepts completed/blocked/stalled
-> main/human receives one-line event
```

Acceptance for Codex work:

- changed files are identified;
- verification command or browser evidence exists;
- final progress file has matching `task_id`;
- Hermes PM supervisor report exists;
- no unrelated old progress file was used.

## Extending To New Employees

To add a new runtime employee:

1. Create employee profile/capabilities/permissions.
2. Implement or configure a runtime adapter.
3. Add direct smoke instructions to the employee skill.
4. Add progress evidence contract.
5. Add tests for:
   - direct smoke;
   - task evidence;
   - stale/no-response handling;
   - old evidence not being reused as current completion.
6. Promote from `candidate` to `active` only after tests pass.
