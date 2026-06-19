# Agent Onboarding Guide (Cross-platform · All agents)

Bring any AI agent (codex / claude / hermes / openclaw / trae / antigravity, or a custom one)
into the Company Kernel employee system on **Windows / Linux / macOS**. No machine-specific
paths — everything is expressed via environment variables.

> 中文版见 [AGENT_ONBOARDING.md](AGENT_ONBOARDING.md)。

---

## 0. Prerequisites (any OS)

```bash
git clone https://github.com/super-ai-company/super-ai-company-kernel.git
cd super-ai-company-kernel
python3 --version   # needs Python 3.10+
```

Set the kernel root (all commands read this; defaults to the repo dir):

| OS | How |
|---|---|
| macOS / Linux (bash/zsh) | `export OPENCLAW_COMPANY_KERNEL_ROOT="$PWD"` |
| Windows (PowerShell) | `$env:OPENCLAW_COMPANY_KERNEL_ROOT = (Get-Location).Path` |
| Windows (cmd) | `set OPENCLAW_COMPANY_KERNEL_ROOT=%CD%` |

> On Windows replace `bin/companyctl` with `python -m company_kernel.companyctl` and
> `bin/company-daemon` with `python -m company_kernel.company_daemon`; arguments are identical.

Self-check: `bin/companyctl doctor --summary`.

---

## 1. One command to add any employee

```bash
bin/company-add-employee \
  --id <id> --name "<display name>" --role <role> \
  --runtime <runtime> --workspace <absolute path to that employee's project> \
  [--skills "skill1,skill2"] [--enable-worker] [--execute]
```

- without `--enable-worker`: register only (safe, no auto-execution)
- with `--enable-worker`: also enable the daemon adapter worker
- with `--execute`: the worker really invokes the runtime (otherwise dry-run task cards)

| runtime | adapter | real-exec command | role |
|---|---|---|---|
| `codex` | company-codex-adapter | `codex exec` | engineering, supports `--model` |
| `claude` | company-claude-adapter | `claude -p` | analysis / docs / review |
| `hermes` | company-hermes-adapter | `hermes -z` | local tooling / automation lead |
| `openclaw` | company-openclaw-adapter | writes OpenClaw `ops/agent_bus` | business ops |
| `trae` | company-trae-adapter | `trae chat` | IDE-style dev |
| `antigravity` | company-antigravity-adapter | opens app + GUI worker returns evidence | multi-agent / browser |
| `local` | company-adapter-worker | none (dry-run / human executor) | generic placeholder |

---

## 2. Per-runtime notes

- **codex** — install codex CLI; add employee with `--runtime codex`; optional `--model` in the daemon worker args. Final output must end with `STATUS: completed` / `STATUS: blocked - <reason>`.
- **claude** — install Claude CLI; first `companyctl runtime verify-adapters --agents claude --allow-candidate`, then onboard + flip to active.
- **hermes** — confirm `hermes --version`; onboard dry-run first, add `--execute` once verified.
- **openclaw** — bridges tasks to OpenClaw legacy `ops/agent_bus`; dry-run by default; `--execute` writes the real bus and is approval-gated.
- **trae** — confirm `trae` CLI; real exec calls `trae chat`.
- **antigravity** — dry-run generates a GUI brief; `--execute` opens the app and a GUI worker returns evidence via `companyctl task done/block`.
- **any custom agent** — `companyctl runtime register --runtime <name> --command "<cmd>"`, then onboard with that runtime or `local`.

---

## 3. Run the background loop

- **macOS**: `bash bin/company-daemon-install-launchd`
- **Linux**: put `bin/company-daemon --once --summary` in a systemd timer or crontab (every 1–5 min)
- **Windows**: Task Scheduler running `python -m company_kernel.company_daemon --once --summary`

Console: `bin/company-api-gateway --port 8765` → open `http://127.0.0.1:8765/`.

---

## 4. Production safety

```bash
export COMPANY_KERNEL_API_TOKEN="<strong random>"   # macOS/Linux
$env:COMPANY_KERNEL_API_TOKEN = "<token>"            # Windows PowerShell
```

All `/v1` data & write endpoints then require `Authorization: Bearer`. The console prompts for the
token on first visit. DB auto-backup is on by default (`bin/company-backup` for manual snapshot/restore).
The gateway binds `127.0.0.1` unless you pass `--host 0.0.0.0` — only do that with a token set.

---

## 5. Verify

```bash
bin/companyctl employee list
bin/companyctl task submit --from owner --to <id> --title "smoke test"
bin/company-daemon --once --summary
bin/companyctl task list --status completed
bin/companyctl conversation probe --participants active            # which employees can actually join a meeting
bin/companyctl meeting request --from owner --topic "choice" \
  --participants <empA>,<empB> --question "A or B?"               # an employee convenes its own meeting (async)
bin/companyctl meeting result --conversation-id <cid from above>    # poll for the conclusion/minutes
```

Full regression: `python3 -B -m unittest discover -s tests` (baseline 539 passing).
