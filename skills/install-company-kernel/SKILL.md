---
name: install-company-kernel
description: Install Company Kernel from a fresh clone to a running company — prerequisites (Python 3.9+, zero third-party deps), the guided `companyctl init` flow, the manual path, starting the live console, self-check, going to production (auth token + backups + auto-start daemon), and cross-platform (Windows/Linux/macOS) notes + troubleshooting. Use when someone clones the repo, asks "how do I install/set up the kernel", "怎么安装内核", "从零跑起来", or is bringing the kernel up on a new machine for the first time.
---

# Install Company Kernel (从零装到跑起来 · 开源第一版)

Company Kernel turns any AI agent (Codex / Claude / Hermes / OpenClaw / Trae / Antigravity / your own) into a managed "employee" under one task protocol. This skill is the **canonical fresh-install path**: clone → working company. Everything below is verified against the current tree.

> **Convention**: run every command from the **kernel root** (the folder containing `bin/companyctl`). Throughout, `companyctl` means:
> - macOS / Linux: `bin/companyctl`
> - Windows: `python -m company_kernel.companyctl`

---

## 0. Prerequisites (极简)

| Need | Detail |
|---|---|
| **Python 3.9+** | The kernel runs on Python **3.9** and up. A guard test (`tests/test_python39_runtime_compat.py`) enforces that every module compiles under 3.9, and the daemon runs on system 3.9 in production. Check: `python3 --version`. |
| **Zero third-party deps** | The core is **pure Python standard library** — no `pip install` needed to run. Optional extras (e.g. local embedding recall) are listed in `requirements-optional.txt` and are not required for the kernel itself. |
| **git** | To clone. |
| **(per-agent) the agent's own CLI** | Each employee needs its runtime CLI installed (codex/claude/…). That's a separate step — see the `onboard-employee` skill. The kernel itself needs none of them to start. |

No database server, no message broker, no container. State lives in a local SQLite file the kernel creates on first run.

---

## 1. Fastest path — guided init

```bash
git clone <repo-url> company-kernel
cd company-kernel
export OPENCLAW_COMPANY_KERNEL_ROOT="$PWD"     # Windows (PowerShell): $env:OPENCLAW_COMPANY_KERNEL_ROOT=$PWD
bin/companyctl doctor --summary                 # self-check the bare checkout
bin/companyctl init                             # guided: detects installed agent CLIs, offers to register each as an employee, prints next steps
```

`companyctl init` (the `company-init` wizard) does the cold-start flow for you:
1. ensures the SQLite schema + state dirs exist,
2. seeds the owner principal,
3. **detects which agent CLIs are actually on this machine** (codex / claude / gemini / trae …),
4. offers to **register each detected runtime as an employee** (its `<runtime>-cli` worker twin),
5. prints the exact next commands (start the console, add the daemon).

That's it — you now have a company with employees.

---

## 2. Manual path (if you prefer explicit steps)

```bash
export OPENCLAW_COMPANY_KERNEL_ROOT="$PWD"
bin/companyctl doctor --summary                 # 1) self-check
bin/company-add-employee --id codex --name Codex --role developer \
  --runtime codex --workspace <your-repo> --enable-worker --execute   # 2) add one employee
bin/company-api-gateway --port 8765             # 3) start the live console + API
# open http://127.0.0.1:8765/
```

Add more employees with one `company-add-employee` per agent. Supported runtimes: `codex`, `claude`, `hermes`, `openclaw`, `trae`, `antigravity`, `local` (generic / any custom agent). Full per-runtime + cross-platform matrix: the **onboard-employee** skill and `docs/AGENT_ONBOARDING.md`.

---

## 3. Verify it's really up

```bash
bin/companyctl doctor                            # full health: daemon, schema, employees, heartbeats
curl -s http://127.0.0.1:8765/v1/health          # API health (ok=true, issues=[])
bin/companyctl employee list                     # your registered employees
```

A healthy kernel reports `ok: true` with an empty `issues` list. Task-level items (a failed run, a task needing review) show up under `attention`, not `issues` — the kernel itself is fine.

---

## 4. Make work actually run — the daemon

Employees only execute when the **daemon worker** is running (it claims queued tasks, runs adapters, applies watchdogs). Run it once to try, or install it to auto-start:

```bash
bin/company-daemon --once --summary              # run one tick by hand (try it)
bin/company-daemon-install-launchd               # macOS: auto-start every 30s via launchd
# Linux: systemd unit · Windows: Task Scheduler — see docs/AGENT_ONBOARDING.md
```

Cost note: an idle on-duty employee costs **nothing** — the daemon's checks are plain SQL (0 tokens). Tokens are spent only when an employee actually executes a task. (See `docs/ON_DUTY_COST_MODEL.md`.)

---

## 5. Going to production (do these before exposing it)

- **Auth** — `export COMPANY_KERNEL_API_TOKEN="<random>"`. Every API write/data endpoint then requires `Authorization: Bearer <token>`. The console picks the token up automatically.
- **Bind** — the gateway binds `127.0.0.1` by default. Expose with `--host 0.0.0.0` **only after** setting a token.
- **Backups** — on by default (daemon every 24h). Manual: `bin/company-backup snapshot | list | restore`.
- **Readiness** — walk `docs/GO_LIVE_READINESS.md` before real traffic.

---

## 6. Cross-platform one-liner reference

| OS | Run a command | Auto-start daemon |
|---|---|---|
| macOS | `bin/companyctl …` | `bin/company-daemon-install-launchd` |
| Linux | `bin/companyctl …` | systemd unit (see onboarding) |
| Windows | `python -m company_kernel.companyctl …` | Task Scheduler (see onboarding) |

---

## 7. Troubleshooting

- **`root provider not set` / paths wrong** → you didn't `export OPENCLAW_COMPANY_KERNEL_ROOT="$PWD"`, or you're not in the kernel root.
- **`doctor` shows daemon unhealthy** → the daemon isn't installed/running; do step 4.
- **API 401** → you set `COMPANY_KERNEL_API_TOKEN`; pass `Authorization: Bearer <token>` (the console does this for you once the token is set).
- **An employee won't pick up tasks** → it's not `active`, has no worker, or its runtime CLI isn't installed — re-run the `onboard-employee` flow and check `companyctl employee list`.
- **More pitfalls** → `docs/KERNEL_SETUP_LESSONS.md` (the setup gotchas list).

---

## Where to go next

| Want to… | Use |
|---|---|
| Add / configure an agent as an employee | **onboard-employee** skill · `docs/AGENT_ONBOARDING.md` |
| Wire an interactive app (Codex/Claude) via MCP | **company-kernel-mcp** skill |
| Understand the full command surface | `docs/USAGE_GUIDE.md` · `docs/COMPANY_KERNEL_USAGE.md` |
| Production readiness checklist | `docs/GO_LIVE_READINESS.md` |
