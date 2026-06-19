from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import contextlib
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from company_kernel import companyctl
from company_kernel import project_memory


def resolve_daemon_paths() -> dict[str, Path]:
    paths = companyctl.resolve_kernel_paths(Path(__file__).resolve().parents[1])
    root = Path(paths["root"])
    log_dir = Path(paths["log_dir"])
    return {
        "root": root,
        "config_path": root / "config" / "daemon.json",
        "state_dir": root / "state" / "daemon",
        "log_path": log_dir / "daemon.log",
    }


_DAEMON_PATHS = resolve_daemon_paths()
ROOT = _DAEMON_PATHS["root"]
CONFIG_PATH = _DAEMON_PATHS["config_path"]
STATE_DIR = _DAEMON_PATHS["state_dir"]
LOG_PATH = _DAEMON_PATHS["log_path"]


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def load_config(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"daemon config not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def augmented_path(current: str) -> str:
    """launchd agents get a minimal PATH; add common tool locations so codex/hermes/openclaw resolve."""
    home = str(Path.home())
    extras = ["/opt/homebrew/bin", "/usr/local/bin", f"{home}/.local/bin", f"{home}/bin", f"{home}/.npm-global/bin"]
    parts = [p for p in current.split(":") if p]
    for extra in extras:
        if extra not in parts:
            parts.append(extra)
    return ":".join(parts)


# Backstops so a single hung child can never freeze the whole synchronous daemon (a hung
# `claude -p` once blocked it for 90+ min → nothing got dispatched). Adapters get a generous
# ceiling (> codex's 60-min per-task cap); quick companyctl maintenance calls get a short one.
DEFAULT_ADAPTER_TIMEOUT_SECONDS = 4500   # 75 min
DEFAULT_COMPANYCTL_TIMEOUT_SECONDS = 600  # 10 min


def run_cmd(args: list[str], timeout: float | None = None) -> dict:
    env = {**os.environ, "OPENCLAW_COMPANY_KERNEL_ROOT": str(ROOT), "PATH": augmented_path(os.environ.get("PATH", ""))}
    # Own process group so on timeout we can kill orphaned grandchildren too (claude -p → node).
    proc = subprocess.Popen(args, cwd=str(ROOT), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, start_new_session=True)
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        returncode = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        try:
            stdout, stderr = proc.communicate(timeout=15)
        except Exception:
            stdout, stderr = "", ""
        returncode = 124
        stderr = (stderr or "") + f"\n[daemon] killed child after exceeding {timeout}s timeout (was hanging the daemon)"
    result = {
        "command": args,
        "returncode": returncode,
        "stdout": stdout or "",
        "stderr": stderr or "",
        "timed_out": timed_out,
    }
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": now(), **result}, ensure_ascii=False) + "\n")
    return result


def run_companyctl(*args: str) -> dict:
    return run_cmd([str(ROOT / "bin" / "companyctl"), *args], timeout=DEFAULT_COMPANYCTL_TIMEOUT_SECONDS)


def resolve_heartbeat_agents(config: dict) -> list[str]:
    agents = list(config.get("heartbeat_agents", []))
    runtimes = [runtime for runtime in config.get("heartbeat_runtimes", []) if runtime]
    if runtimes:
        wildcard = "*" in runtimes
        conn = companyctl.connect()
        try:
            if wildcard:
                rows = conn.execute("SELECT id FROM employees WHERE status = 'active' ORDER BY id").fetchall()
            else:
                rows = conn.execute(
                    f"""
                    SELECT id FROM employees
                    WHERE status = 'active'
                      AND runtime IN ({",".join("?" for _ in runtimes)})
                    ORDER BY id
                    """,
                    runtimes,
                ).fetchall()
        finally:
            conn.close()
        agents.extend(row["id"] for row in rows)
    seen = set()
    return [agent for agent in agents if agent and not (agent in seen or seen.add(agent))]


def run_adapter_once(worker: dict) -> dict:
    command = worker.get("command", "")
    if not command:
        return {"ok": False, "error": "missing adapter command", "worker": worker}
    executable = ROOT / "bin" / command
    args = [str(executable), "--agent", worker["agent"], *worker.get("args", [])]
    timeout = float(worker.get("daemon_timeout_seconds", DEFAULT_ADAPTER_TIMEOUT_SECONDS) or DEFAULT_ADAPTER_TIMEOUT_SECONDS)
    return run_cmd(args, timeout=timeout)


def parsed_stdout(result: dict) -> dict:
    raw = (result.get("stdout") or "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw[-1000:]}


def run_adapter(worker: dict) -> dict:
    max_tasks = int(worker.get("max_tasks_per_tick", worker.get("runs", 1)) or 1)
    runs = []
    processed_total = 0
    for index in range(max_tasks):
        result = run_adapter_once(worker)
        parsed = parsed_stdout(result)
        processed = int(parsed.get("processed", 0) or 0)
        processed_total += processed
        runs.append({"index": index + 1, "result": result, "parsed_stdout": parsed})
        if result.get("returncode", 1) != 0 or processed == 0:
            break
    state = {
        "ok": all(item["result"].get("returncode", 1) == 0 for item in runs),
        "agent": worker.get("agent", ""),
        "command": worker.get("command", ""),
        "processed": processed_total,
        "runs": runs,
        "retry_policy": worker.get("retry_policy", {}),
        "at": now(),
    }
    worker_state_path = STATE_DIR / "workers" / f"{worker.get('agent', 'unknown')}.json"
    worker_state_path.parent.mkdir(parents=True, exist_ok=True)
    worker_state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    state["state_file"] = str(worker_state_path)
    record_adapter_run(state)
    return state


def record_adapter_run(state: dict) -> None:
    task_id = adapter_state_task_id(state)
    # Don't pollute health/retry with non-actionable runs: a tick where the worker
    # claimed no task and produced no work (e.g. "no submitted task", or an environment
    # gap like "codex command not found" before any claim) is not a task failure.
    # Only record when a task was involved OR real work was processed.
    if not task_id and int(state.get("processed", 0) or 0) == 0:
        return
    conn = companyctl.connect()
    try:
        trace_id = companyctl.trace_id_for_task(conn, task_id, state.get("trace_id", ""))
        attempt = adapter_attempt_for_task(conn, task_id)
        next_retry_at = next_retry_at_for_state(state, attempt)
        conn.execute(
            """
            INSERT INTO adapter_runs(id, trace_id, agent_id, task_id, command, ok, processed, attempt, next_retry_at, result_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"adapter-run-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}",
                trace_id,
                state.get("agent", ""),
                task_id,
                state.get("command", ""),
                1 if state.get("ok") else 0,
                int(state.get("processed", 0) or 0),
                attempt,
                next_retry_at,
                json.dumps(state, ensure_ascii=False),
                state.get("at", now()),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def adapter_attempt_for_task(conn, task_id: str) -> int:
    if not task_id:
        return 1
    row = conn.execute("SELECT MAX(attempt) AS max_attempt FROM adapter_runs WHERE task_id = ?", (task_id,)).fetchone()
    return int((row["max_attempt"] if row and row["max_attempt"] is not None else 0) or 0) + 1


def next_retry_at_for_state(state: dict, attempt: int) -> str:
    if state.get("ok"):
        return ""
    policy = state.get("retry_policy") or {}
    max_attempts = int(policy.get("max_attempts", 0) or 0)
    if max_attempts <= 0 or attempt >= max_attempts:
        return ""
    base = int(policy.get("base_delay_seconds", 60) or 60)
    maximum = int(policy.get("max_delay_seconds", 3600) or 3600)
    delay = min(maximum, base * (2 ** max(attempt - 1, 0)))
    return (datetime.now(timezone.utc).astimezone() + timedelta(seconds=delay)).isoformat(timespec="seconds")


def adapter_state_task_id(state: dict) -> str:
    for run in state.get("runs", []):
        if isinstance(run, dict):
            parsed = run.get("parsed_stdout", {})
            if isinstance(parsed, dict) and parsed.get("task_id"):
                return str(parsed["task_id"])
    return ""


def retry_due_adapter_runs(config: dict) -> list[dict]:
    if not config.get("run_retries", True):
        return []
    conn = companyctl.connect()
    try:
        due = conn.execute(
            """
            SELECT * FROM adapter_runs
            WHERE ok = 0
              AND acknowledged_at = ''
              AND task_id != ''
              AND next_retry_at != ''
              AND next_retry_at <= ?
            ORDER BY next_retry_at ASC
            LIMIT ?
            """,
            (now(), int(config.get("max_retries_per_tick", 5) or 5)),
        ).fetchall()
    finally:
        conn.close()
    results = []
    for run in due:
        results.append(run_companyctl("runtime", "retry-adapter-run", "--run-id", run["id"], "--by", "openclaw-main", "--reason", "auto retry policy"))
    return results


WATCHDOG_STATE_PATH = STATE_DIR / "watchdog.json"


def maybe_backup(config: dict) -> list[dict]:
    """Take a DB snapshot when the newest backup is older than the configured interval."""
    backup_cfg = config.get("backup") or {}
    if not backup_cfg.get("enabled", False):
        return []
    from company_kernel import backup as backup_mod
    interval_hours = float(backup_cfg.get("interval_hours", 24) or 24)
    keep = int(backup_cfg.get("keep", 14) or 14)
    snaps = backup_mod.list_snapshots()
    if snaps:
        newest_age_h = (datetime.now().timestamp() - snaps[0].stat().st_mtime) / 3600.0
        if newest_age_h < interval_hours:
            return []
    result = backup_mod.snapshot(keep=keep, label="auto")
    return [{"step": "backup.snapshot", "result": {"returncode": 0 if result.get("ok") else 1,
            "stdout": json.dumps(result, ensure_ascii=False), "stderr": ""}}]


def check_unclaimed_tasks(config: dict) -> list[dict]:
    """Alert once per task when a submitted task stays unclaimed beyond the configured window."""
    watchdog = config.get("watchdog") or {}
    if not watchdog.get("enabled", False):
        return []
    minutes = int(watchdog.get("unclaimed_minutes", 10) or 10)
    notify = str(watchdog.get("notify", "") or "")
    sender = str(watchdog.get("from", "openclaw-main") or "openclaw-main")
    limit = int(watchdog.get("max_alerts_per_tick", 5) or 5)
    cutoff = (datetime.now(timezone.utc).astimezone() - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    # Interactive app employees (codex/claude/antigravity) don't auto-claim — an unclaimed app task is
    # not an outage, just a task waiting for the owner to open the app. Autonomous work is dispatched to
    # the CLI twins, which the daemon claims promptly. So never raise watchdog alerts for app targets.
    app_targets = tuple(project_memory.APP_CLI_PAIRS.keys())
    placeholders = ",".join("?" for _ in app_targets)
    conn = companyctl.connect()
    try:
        rows = conn.execute(
            "SELECT id, target_agent, title, created_at FROM tasks WHERE status = 'submitted' "
            f"AND created_at <= ? AND target_agent NOT IN ({placeholders}) ORDER BY created_at LIMIT ?",
            (cutoff, *app_targets, limit),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return []
    alerted: dict = {}
    if WATCHDOG_STATE_PATH.exists():
        try:
            alerted = json.loads(WATCHDOG_STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            alerted = {}
    results = []
    for row in rows:
        if alerted.get(row["id"]):
            continue
        body = (
            f"看门狗告警：任务 {row['id']}（目标 {row['target_agent']}，标题 {row['title']}）"
            f"已提交超过 {minutes} 分钟仍无人领取。请检查对应 adapter worker 是否启用、daemon 是否在跑。"
        )
        if notify:
            results.append(run_companyctl("message", "send", "--from", sender, "--to", notify, "--body", body))
        alerted[row["id"]] = now()
    WATCHDOG_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    WATCHDOG_STATE_PATH.write_text(json.dumps(alerted, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return results


def employee_exists(agent: str) -> bool:
    conn = companyctl.connect()
    try:
        return bool(conn.execute("SELECT 1 FROM employees WHERE id = ? AND status = 'active'", (agent,)).fetchone())
    finally:
        conn.close()


def employee_runtime(agent: str) -> str:
    conn = companyctl.connect()
    try:
        row = conn.execute("SELECT runtime FROM employees WHERE id = ? AND status = 'active'", (agent,)).fetchone()
        return str(row["runtime"]) if row else ""
    finally:
        conn.close()


def default_adapter_worker(agent: str) -> dict:
    runtime = employee_runtime(agent)
    command = companyctl.ADAPTER_COMMANDS.get(runtime, "company-adapter-worker")
    args = ["--dry-run"] if command == "company-adapter-worker" else []
    return {
        "agent": agent,
        "enabled": True,
        "command": command,
        "args": args,
        "max_tasks_per_tick": 1,
        "temporary": True,
        "runtime": runtime,
    }


def ensure_enabled_workers(config: dict, agents: list[str]) -> None:
    if not agents:
        return
    workers = config.setdefault("adapter_workers", [])
    by_agent = {str(worker.get("agent", "")): worker for worker in workers}
    for agent in agents:
        if agent in by_agent:
            by_agent[agent]["enabled"] = True
            continue
        if not employee_exists(agent):
            raise SystemExit(f"cannot enable unknown or inactive worker: {agent}")
        workers.append(default_adapter_worker(agent))


def write_state(state: dict) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / "last-run.json"
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


# Liveness: the daemon runs adapters synchronously, and one codex task can take up to its
# timeout (~30 min). Without a mid-cycle heartbeat, last-run.json's timestamp goes stale and
# the health check falsely reports "内核异常" while the daemon is simply busy. We beat at cycle
# start and before each adapter so the daemon's freshness reflects "loop alive", not "cycle done".
_LAST_OK = True


def heartbeat(phase: str) -> None:
    write_state({"ok": _LAST_OK, "at": now(), "phase": phase, "results": []})


def summarize_state(state: dict) -> dict:
    steps = state.get("results", [])
    failed_steps = []
    heartbeat_agents = []
    adapter_steps = []
    counts = {"steps": len(steps), "heartbeats": 0, "adapters": 0, "repair": 0, "scheduler": 0, "supervisor": 0, "watchdog": 0, "openclaw_sync": 0, "failed": 0}
    for item in steps:
        step = str(item.get("step", ""))
        result = item.get("result", {})
        returncode = result.get("returncode", 1) if isinstance(result, dict) else 1
        if returncode != 0:
            counts["failed"] += 1
            failed_steps.append(step)
        if step.startswith("heartbeat."):
            counts["heartbeats"] += 1
            heartbeat_agents.append(step.removeprefix("heartbeat."))
        elif step.startswith("adapter."):
            counts["adapters"] += 1
            adapter_steps.append(step.removeprefix("adapter."))
        elif step.startswith("repair."):
            counts["repair"] += 1
        elif step.startswith("scheduler."):
            counts["scheduler"] += 1
        elif step.startswith("supervisor."):
            counts["supervisor"] += 1
        elif step.startswith("watchdog."):
            counts["watchdog"] += 1
        elif step.startswith("openclaw-sync."):
            counts["openclaw_sync"] += 1
    return {
        "ok": state.get("ok", False),
        "at": state.get("at", ""),
        "counts": counts,
        "failed_steps": failed_steps,
        "heartbeat_agents": heartbeat_agents,
        "adapter_agents": adapter_steps,
        "state_file": state.get("state_file", ""),
    }


RECONCILE_STATE_PATH = STATE_DIR / "reconcile.json"
RECOVER_STATE_PATH = STATE_DIR / "recover.json"


def maybe_recover_employees(config: dict) -> list[dict]:
    """Auto-heal employees that were auto-downgraded (e.g. a runtime/proxy was down at boot) by
    re-verifying them and reactivating the ones that now respond. Heavy (invokes runtimes), so
    interval-gated. This is what makes a transient outage self-recover instead of staying offline."""
    cfg = config.get("auto_recover") or {}
    if not cfg.get("enabled", True):
        return []
    interval_minutes = float(cfg.get("interval_minutes", 10) or 10)
    if RECOVER_STATE_PATH.exists():
        try:
            last = json.loads(RECOVER_STATE_PATH.read_text(encoding="utf-8")).get("at", "")
            if last:
                age_m = (datetime.now(timezone.utc).astimezone() - datetime.fromisoformat(last)).total_seconds() / 60.0
                if age_m < interval_minutes:
                    return []
        except (json.JSONDecodeError, ValueError, OSError):
            pass
    result = run_companyctl("employee", "recover", "--max", str(int(cfg.get("max_per_run", 3))))
    RECOVER_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    RECOVER_STATE_PATH.write_text(json.dumps({"at": now()}, ensure_ascii=False) + "\n", encoding="utf-8")
    return [{"step": "presence.recover-unavailable", "result": result}]


def maybe_reconcile_status(config: dict) -> list[dict]:
    """Periodically re-probe every employee so the active roster reflects reality.
    Heavy (runs real CLIs), so gated by an interval like backups."""
    cfg = config.get("reconcile_status") or {}
    if not cfg.get("enabled", False):
        return []
    interval_hours = float(cfg.get("interval_hours", 6) or 6)
    if RECONCILE_STATE_PATH.exists():
        try:
            last = json.loads(RECONCILE_STATE_PATH.read_text(encoding="utf-8")).get("at", "")
            if last:
                age_h = (datetime.now(timezone.utc).astimezone() - datetime.fromisoformat(last)).total_seconds() / 3600.0
                if age_h < interval_hours:
                    return []
        except (json.JSONDecodeError, ValueError, OSError):
            pass
    result = run_cmd([sys.executable, "-m", "company_kernel.reconcile_status"])
    RECONCILE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    RECONCILE_STATE_PATH.write_text(json.dumps({"at": now()}, ensure_ascii=False) + "\n", encoding="utf-8")
    return [{"step": "reconcile.status", "result": {"returncode": result.get("returncode", 1), "stdout": "", "stderr": ""}}]


class HeartbeatKeeper:
    """Keeps worker heartbeats fresh DURING a long tick. The daemon runs adapters synchronously, so a
    single long run (e.g. a 30-min codex task) would otherwise leave every OTHER worker un-heartbeated
    for the whole tick — the console then shows them all 'off duty' even though they are fine, which
    directly undercuts the free-on-duty model. This background thread re-stamps the given agents'
    heartbeats every `interval` seconds (pure SQL via touch_heartbeat_internal — free, no LLM) so
    on-duty stays accurate no matter how long any adapter blocks. Used as a context manager around the
    adapter loop. The keepalive must never crash the tick, so all errors are swallowed."""

    def __init__(self, agents: list[str], interval_seconds: int = 240):
        self._agents = list(agents)
        self._interval = max(30, int(interval_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.consecutive_failures = 0  # observable: a persistently-failing keeper is NOT silent

    def _beat_once(self) -> None:
        """One keepalive round: refresh every tracked agent's heartbeat. Pure SQL, free."""
        conn = companyctl.connect()
        try:
            for agent in self._agents:
                companyctl.touch_heartbeat_internal(conn, agent)
            conn.commit()
        finally:
            conn.close()

    def _loop(self) -> None:
        # wait() returns True when stopped → exit promptly; else fires every interval
        while not self._stop.wait(self._interval):
            try:
                self._beat_once()
                self.consecutive_failures = 0
            except Exception as exc:  # noqa: BLE001 — a keepalive hiccup must never take down the tick
                # ...but it must NOT be swallowed silently either: a keeper whose heartbeat writes keep
                # failing would let every worker silently go 'off duty' with no trace. Count it and warn
                # on stderr (→ daemon log) so the failure is observable, then keep trying.
                self.consecutive_failures += 1
                print(f"⚠️ heartbeat-keeper write failed (x{self.consecutive_failures}): {exc}", file=sys.stderr)

    def __enter__(self) -> "HeartbeatKeeper":
        if self._agents:
            self._thread = threading.Thread(target=self._loop, name="heartbeat-keeper", daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)


def keeper_agents_for(config: dict, heartbeat_agents: list[str]) -> list[str]:
    """Who the keepalive thread must refresh during the adapter loop: the union of the configured
    heartbeat agents AND every ENABLED adapter worker. The workers at risk are exactly the enabled
    adapter_workers — each normally self-heartbeats by running its own tick, but won't run while
    another worker's long adapter holds the loop. Including them is essential: with the shipped
    `heartbeat_agents: []`, keying off heartbeat agents alone would make the keeper a no-op for the
    very agents that go stale during a long task."""
    enabled_worker_agents = [str(w.get("agent", "")) for w in config.get("adapter_workers", [])
                             if w.get("enabled", False) and w.get("agent")]
    return list(dict.fromkeys([*heartbeat_agents, *enabled_worker_agents]))


def tick(config: dict) -> dict:
    results = []
    heartbeat("cycle-start")  # mark the loop alive before any slow step runs
    if config.get("sync_openclaw_runtime", False):
        results.append({"step": "openclaw-sync.runtime", "result": run_companyctl("employee", "sync-openclaw-runtime")})
    if config.get("sync_openclaw_heartbeats", False):
        results.append({"step": "openclaw-sync.heartbeats", "result": run_companyctl("employee", "sync-openclaw-heartbeats")})
    if config.get("import_openclaw_native_results", False):
        results.append({"step": "openclaw-sync.import-results", "result": run_companyctl("openclaw", "import-results")})
    if config.get("run_repair", True):
        results.append({"step": "repair.reset-stale-claims", "result": run_companyctl("repair", "reset-stale-claims")})
    if config.get("run_watchdog_reaper", True):
        # Fault tolerance: force-fail any attempt running past its cap BEFORE the adapter loop, so a
        # hung run lands in the failure list (task blocked + dispatcher notified) instead of hanging
        # forever. Runs early so it fires even on a tick where an adapter later stalls.
        results.append({"step": "watchdog.reap-stuck", "result": run_companyctl("watchdog", "reap-stuck", "--notify")})
    if config.get("run_repair", True) and config.get("run_auto_triage", True):
        # auto-discard mis-dispatched tasks (e.g. codex with no 工作区:) + feed back to the dispatcher,
        # so a doomed order never sits queued/cycling
        results.append({"step": "repair.auto-triage", "result": run_companyctl("task", "auto-triage")})
    if config.get("run_scheduler", True):
        results.append({"step": "scheduler.run", "result": run_companyctl("scheduler", "run")})
    if config.get("run_approval_auto_sweep", True):
        # auto mode safety net: if the owner delegated full approval, clear+materialize any pending
        # route approval so nothing ever sits blocking. No-op in manual mode.
        results.append({"step": "approval.auto-sweep", "result": run_companyctl("approval", "auto-sweep")})
    if config.get("run_memory_curation", True):
        # project memory: the lead's auto-curation — dedup + rebuild digest for any project that got
        # new memory this cycle, so the shared project knowledge stays coherent and current.
        results.append({"step": "memory.curate", "result": run_companyctl("memory", "curate-all")})
    if config.get("run_supervisor_delivery_loop", config.get("run_scheduler", True)):
        results.append({"step": "supervisor.delivery-loop", "result": run_companyctl("supervisor", "delivery-loop")})
    if config.get("run_offline_reminder", True):
        # scheduled offline reminder; --dedup self-limits to once per change / hourly so it won't spam
        results.append({"step": "presence.offline-reminder", "result": run_companyctl("employee", "offline-report", "--notify", "--dedup")})
    for result in retry_due_adapter_runs(config):
        results.append({"step": "retry.adapter-run", "result": result})
    heartbeat_agents = resolve_heartbeat_agents(config)
    for agent in heartbeat_agents:
        results.append({"step": f"heartbeat.{agent}", "result": run_companyctl("heartbeat", "--agent", agent)})
    # Keep heartbeats fresh while the (possibly very long) adapter loop runs synchronously, so a single
    # long task doesn't make every OTHER worker look 'off duty' for the whole tick.
    keeper_agents = keeper_agents_for(config, heartbeat_agents)
    keeper = (HeartbeatKeeper(keeper_agents, int(config.get("heartbeat_keeper_interval_seconds", 240)))
              if config.get("run_heartbeat_keeper", True) else contextlib.nullcontext())
    with keeper:
        for worker in config.get("adapter_workers", []):
            if not worker.get("enabled", False):
                continue
            heartbeat(f"adapter:{worker.get('agent', '')}")  # a long codex run must not look like a dead daemon
            adapter_state = run_adapter(worker)
            results.append({"step": f"adapter.{worker.get('agent', '')}", "result": {"returncode": 0 if adapter_state["ok"] else 1, "stdout": json.dumps(adapter_state, ensure_ascii=False), "stderr": ""}})
    for result in check_unclaimed_tasks(config):
        results.append({"step": "watchdog.unclaimed-task", "result": result})
    results.extend(maybe_backup(config))
    results.extend(maybe_reconcile_status(config))
    results.extend(maybe_recover_employees(config))
    # 守护"整轮 ok"只反映循环基础设施是否正常,不被单个 adapter 任务的成败左右:
    # adapter 任务失败/受阻已由 failed_adapter_runs 单独跟踪(可确认),不应把整轮守护标失败、
    # 进而让"内核健康"徽章发红。因此计算整轮 ok 时排除 adapter.* 步骤。
    global _LAST_OK
    cycle_ok = all(
        item["result"].get("returncode", 1) == 0
        for item in results
        if not str(item.get("step", "")).startswith("adapter.")
    )
    _LAST_OK = cycle_ok  # remembered so mid-cycle liveness beats carry the right health
    state = {
        "ok": cycle_ok,
        "at": now(),
        "results": results,
    }
    path = write_state(state)
    state["state_file"] = str(path)
    return state


def run(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    ensure_enabled_workers(config, args.enable_worker)
    interval = args.interval or int(config.get("interval_seconds", 30))
    iterations = 1 if args.once else max(args.iterations, 0)
    count = 0
    last_state = {}
    while True:
        last_state = tick(config)
        count += 1
        if args.once or (iterations and count >= iterations):
            break
        time.sleep(interval)
    emit(summarize_state(last_state) if args.summary else last_state)
    return 0 if last_state.get("ok") else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Company Kernel daemon loop")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--iterations", type=int, default=0)
    parser.add_argument("--interval", type=int, default=0)
    parser.add_argument("--enable-worker", action="append", default=[], help="temporarily enable adapter worker for one agent without editing config")
    parser.add_argument("--summary", action="store_true", help="print compact run summary while preserving full state/log files")
    return parser


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
