---
name: onboard-employee
description: Complete guide for a customer to add and configure an AI employee in Company Kernel — pick a runtime, install its CLI (Windows / Linux / macOS), create the employee, verify and activate it, wire the daemon worker, and set up auto-start. Use whenever someone wants to add a new employee/agent, says "add an employee", "onboard an agent", "配置员工", "加员工", or is setting up the kernel on a new machine.
---

# Onboard an Employee (客户加员工完整引导)

An **employee** in Company Kernel is an AI agent bound to a **runtime** (the actual CLI/engine that does the work). The kernel dispatches tasks, runs meetings, tracks evidence, and reports results; the runtime is what executes. Adding an employee = (1) install its runtime, (2) register the employee, (3) verify it actually responds, (4) activate it, (5) wire the daemon worker, (6) ensure auto-start.

> Do every command from the kernel root (the folder containing `bin/companyctl`). Below, `companyctl` means `bin/companyctl` on macOS/Linux or `python -m company_kernel.companyctl` on Windows.

---

## 0. Supported runtimes

`companyctl runtime list` shows what's registered. Built-in (`KNOWN_RUNTIMES`):

| runtime | what it is | needs |
|---|---|---|
| `codex` | Codex CLI (gpt-5.x) — coding/backend | `codex` CLI + a real repo path per task (`工作区:`) |
| `claude` | Claude Code / Claude CLI | `claude` CLI logged in |
| `gemini` | Gemini via an Anthropic-compatible proxy | the proxy reachable (default `http://localhost:8080`) |
| `openclaw` | OpenClaw business agent (LINE/Telegram relay) | OpenClaw runtime + agent registered |
| `hermes` | Local supervisor/PM runtime | OpenClaw/hermes runtime |
| `antigravity` | Google Antigravity (`agy`) CLI | `agy` CLI |
| `trae` | Trae IDE/Agent | `trae` adapter |
| `local` | local script / manual | a script |

**If you add a NEW runtime type** (not in the list above), register it first or the kernel marks the employee "未知运行时" and auto-pauses it:
```
companyctl runtime register --runtime <name> --command <adapter-bin>
```
(For a Claude-compatible proxy runtime, it can ride the claude adapter — see the gemini appendix.)

---

## 1. Prerequisites (per OS)

The kernel itself is Python and cross-platform. Install these once:

### macOS
```bash
brew install python@3.13 git node            # node for codex/claude CLIs
python3 -m pip install -r requirements-optional.txt   # optional extras
```

### Linux (Debian/Ubuntu)
```bash
sudo apt update && sudo apt install -y python3 python3-pip git nodejs npm
python3 -m pip install -r requirements-optional.txt
```

### Windows (PowerShell)
```powershell
winget install Python.Python.3.13 Git.Git OpenJS.NodeJS
python -m pip install -r requirements-optional.txt
```
> Windows note: the daemon's process-group kill (POSIX) and the launchd installer are not Windows-native — see §6 for the Task Scheduler path. Run the kernel under WSL2 for the smoothest experience.

Then install the **runtime CLI** for the employee you're adding (see the per-runtime appendix in §7). Verify on PATH, e.g. `codex --version`, `claude --version`, `agy --version`.

---

## 2. Create the employee

Use `onboard` (richer than `create` — sets skills/tools/comms in one shot):

```
companyctl employee onboard \
  --id codex --name "Codex" --role developer \
  --runtime codex \
  --workspace /ABSOLUTE/path/to/the/repo \
  --skills "code,review,test" --tools "git,shell" \
  --task-types "backend,bugfix" \
  --can-talk-to "hermes,owner"
```

- `--id` lowercase-kebab, unique. `--workspace` MUST be an absolute path that exists.
- New employees start as **`candidate`** (not yet trusted to receive work) — that's intentional. §3 promotes them.
- Paths are OS-native: `/Users/you/...` (mac), `/home/you/...` (linux), `C:\Users\you\...` (windows).

---

## 3. Verify it actually responds, then activate

A candidate must prove its runtime works before it's promoted — the kernel won't activate on a password echo, only on real runtime evidence:

```
companyctl employee verify-runtime --agent codex --activate
```

- Pass → status becomes `active`, the employee can receive tasks.
- Fail → stays `candidate` with an `unavailable_reason`. Fix the runtime (CLI on PATH? logged in? proxy up?) and re-run.
- Check anytime: `companyctl employee show --id codex`.

If an employee was paused (manually or auto), resume comms:
```
companyctl communication resume --agent codex
```

---

## 4. Wire the daemon worker (so it auto-executes tasks)

Add a worker block to `config/daemon.json` → `adapter_workers` so the daemon dispatches tasks to it each tick:

```json
{ "agent": "codex", "enabled": true, "command": "company-codex-adapter",
  "args": ["--execute", "--sandbox", "danger-full-access", "--model", "gpt-5.5", "--timeout-seconds", "1800"],
  "max_tasks_per_tick": 1,
  "retry_policy": {"max_attempts": 3, "base_delay_seconds": 60, "max_delay_seconds": 900} }
```

The daemon reads this **only at start** → restart the daemon after editing (see §6). Per-runtime worker commands are in §7.

---

## 5. Confirm it works end-to-end

```
companyctl employee list                 # employee shows active, no backlog
companyctl conversation probe --participants <id>   # can it join meetings?
# dispatch a tiny real task (codex example — note the 工作区 directive!):
companyctl task submit --from owner --to codex --title "smoke" \
  --description "工作区: /ABSOLUTE/repo
列出仓库根目录文件并回报。"
```
Watch the console (`http://127.0.0.1:8765/`) → the task should claim, run, and show in **完成回报**.

---

## 6. Auto-start on boot (per OS)

The kernel runs three services: **daemon** (dispatches work), **api** (console + REST on :8765), and optionally task-intake. They must auto-start so work resumes after reboot.

### macOS — launchd (shipped)
```bash
bash bin/company-services-install-launchd     # installs daemon + api (KeepAlive + RunAtLoad)
launchctl kickstart -k gui/$(id -u)/ai.openclaw.company-kernel.daemon   # restart daemon after config edits
```
Uninstall: `bash bin/company-services-uninstall-launchd`.

### Linux — systemd (template; not shipped, paste these)
`/etc/systemd/system/company-kernel-api.service`:
```ini
[Unit]
Description=Company Kernel API
After=network.target
[Service]
WorkingDirectory=/opt/company-kernel
ExecStart=/opt/company-kernel/bin/company-api-gateway 127.0.0.1 8765 --quiet /opt/company-kernel
Restart=always
[Install]
WantedBy=multi-user.target
```
`/etc/systemd/system/company-kernel-daemon.service` + a `.timer` that runs `bin/company-daemon --once` every 30s (or set the service to loop). Then:
```bash
sudo systemctl daemon-reload && sudo systemctl enable --now company-kernel-api company-kernel-daemon.timer
```

### Windows — Task Scheduler / NSSM (not shipped)
- API: a Task Scheduler task "At log on", action `python -m company_kernel.api_gateway --host 127.0.0.1 --port 8765`, "restart on failure".
- Daemon: a task triggered every 1 minute running `python -m company_kernel.company_daemon --once`.
- Or wrap both as Windows services with **NSSM** (`nssm install CompanyKernelApi ...`).
- Recommended: run under **WSL2** and use the Linux/systemd path instead (the daemon's timeout/kill code assumes POSIX).

---

## 7. Per-runtime appendix

**codex** — install: `npm i -g @openai/codex`; log in. Worker: `company-codex-adapter --execute --sandbox danger-full-access --model gpt-5.5 --timeout-seconds 1800`. ⚠️ Every codex task description MUST carry `工作区: /abs/repo` (and `超时: 3600` for heavy ETL) or it lands in /tmp and blocks — the submit guard now rejects pathless codex tasks. See [dispatch-task-to-codex](../dispatch-task-to-codex/SKILL.md).

**claude** — install: `npm i -g @anthropic-ai/claude-code`; `claude` login. Worker: `company-claude-adapter --execute --permission-mode bypassPermissions`. claude is managed manually as an analyst; set profile `auto_recover: false` if you don't want auto-reactivation.

**gemini** — a Claude-compatible proxy runtime. Set the employee profile's `proxy_base_url` (default `http://localhost:8080`), `proxy_token`, `proxy_model`. It runs on the claude adapter (`company-claude-adapter --agent gemini ...`). Make sure the proxy is up before activating, or it auto-pauses; the daemon's `presence.recover-unavailable` step re-activates it once the proxy returns.

**openclaw / hermes** — register the OpenClaw agent, then `companyctl employee import-openclaw` / `sync-openclaw-runtime`. Worker: `company-openclaw-adapter --execute`. These relay to LINE/Telegram; see [company-employee-openclaw](../company-employee-openclaw/SKILL.md).

**antigravity (agy)** — headless path is `company-antigravity-adapter --managed-attempt --by hermes` (NOT `--execute`, which opens the GUI). Install the `agy` CLI first.

**local** — a script runtime; point `--workspace` at the script dir and provide a `local` adapter command.

---

## 8. Common pitfalls (learned the hard way)

- **New runtime not in `KNOWN_RUNTIMES`** → employee gets "未知运行时" + auto-paused every boot. Register it, or add it to the built-in set if it's a first-class runtime.
- **codex task with no `工作区:`** → runs in /tmp, blocks. Submit guard now rejects these; always include the repo path.
- **Duplicate / just-discarded task** → submit guard rejects (60-min cooldown). Use `--force` to override.
- **An employee dispatching garbage** → `companyctl communication pause --agent <id>` (止血), fix it, then `resume`.
- **Config edits don't take effect** → the daemon reads config only at start; restart it (§6).
- **Reboot lost a manual pause** → startup sync regenerates `company_communications.json`; manual pauses don't persist across reboot by design.
- **A long task froze the daemon** → fixed: adapters now have a timeout backstop; a hung `claude -p`/`codex exec` is killed, not left to freeze dispatching.

## 9. Remove an employee

```
companyctl employee offboard --id <id>                # soft: archive + cancel its tasks
companyctl employee offboard --id <id> --hard-delete  # also delete kernel-managed files (cancels tasks to avoid orphan evidence)
```
