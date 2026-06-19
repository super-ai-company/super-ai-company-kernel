# Company Kernel — Quickstart (Windows / Linux / macOS)

Get the kernel + web console running, then add your first employee. Two paths: **Docker** (one command, any OS) or **native**.

---

## Path A — Docker (recommended, cross-OS)

Works identically on Windows (Docker Desktop / WSL2), Linux, and macOS. Runs the **daemon** (dispatches work) and **API + console** (`:8765`) together, auto-restarts on crash/reboot.

```bash
docker compose up -d
# open the console:
open http://localhost:8765        # macOS;  Linux: xdg-open … ;  Windows: start http://localhost:8765
```

- Data persists in the `company-kernel-data` volume (DB at `/data/company.sqlite`).
- License is **default-allow** for self-host (no key needed). Only `COMPANY_KERNEL_LICENSE_ENFORCE=1` turns enforcement on.
- Exposing beyond localhost? Set `COMPANY_KERNEL_API_TOKEN` in `docker-compose.yml` first.

> **Runtime CLIs:** the base image runs the kernel and `openclaw`/`hermes` agents. To run **codex / claude / gemini** employees, those CLIs must be reachable (install them in a derived image with their login, or run those runtimes on the host). See the runtime appendix in `skills/onboard-employee/SKILL.md`.

Stop / logs:
```bash
docker compose logs -f        # watch
docker compose down           # stop (data kept)
```

---

## Path B — Native

Best when your runtime CLIs (codex/claude/agy) already live on the host with their logins.

```bash
# 1. prerequisites (see skills/onboard-employee §1 for per-OS install)
python3 -m pip install -r requirements-optional.txt   # optional extras; core is stdlib-only

# 2. start the API + console
bin/company-api-gateway 127.0.0.1 8765 &      # Windows: python -m company_kernel.api_gateway --host 127.0.0.1 --port 8765

# 3. start the daemon (dispatch loop)
bin/company-daemon &                          # Windows: python -m company_kernel.company_daemon

# 4. (macOS) install as auto-starting services instead of the above:
bash bin/company-services-install-launchd
```
Linux systemd / Windows Task Scheduler templates: see `skills/onboard-employee/SKILL.md` §6.

---

## Add your first employee (web, no commands)

1. Open the console → **员工 (Employees)** tab → **＋ 新增员工**.
2. Fill: ID, name, role, **runtime** (codex/claude/gemini/openclaw/…), **workspace** (absolute repo path).
3. **注册员工** → it's created as a `candidate`.
4. On the new card click **🔌 验证并激活** → the kernel verifies the runtime really responds, then activates it. (Fails? check `logs/verify-runtime/<id>.log` — usually the CLI isn't installed/logged-in, or a proxy is down.)
5. To auto-execute tasks, add a worker block in `config/daemon.json` and restart the daemon — see `skills/onboard-employee/SKILL.md` §4.

CLI equivalent:
```bash
bin/companyctl employee onboard --id codex --name Codex --role developer \
  --runtime codex --workspace /ABS/repo
bin/companyctl employee verify-runtime --agent codex --activate
```

---

## Verify it works

- Console **总览** → 内核 should read **正常**.
- Dispatch a smoke task (codex needs the `工作区:` line, or the submit guard rejects it):
  ```bash
  bin/companyctl task submit --from owner --to codex --title smoke \
    --description "工作区: /ABS/repo
  列出仓库根目录并回报。"
  ```
- Watch it land in the **完成回报 (Results)** tab.

Full onboarding details, per-runtime setup, OS service auto-start, and pitfalls: **`skills/onboard-employee/SKILL.md`**.
