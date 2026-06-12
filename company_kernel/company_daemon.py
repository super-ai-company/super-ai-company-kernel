from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from company_kernel import companyctl


ROOT = Path(os.environ.get("OPENCLAW_COMPANY_KERNEL_ROOT", Path(__file__).resolve().parents[1])).resolve()
CONFIG_PATH = ROOT / "config" / "daemon.json"
STATE_DIR = ROOT / "state" / "daemon"
LOG_PATH = ROOT / "logs" / "daemon.log"


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def load_config(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"daemon config not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def run_cmd(args: list[str]) -> dict:
    env = {**os.environ, "OPENCLAW_COMPANY_KERNEL_ROOT": str(ROOT)}
    cp = subprocess.run(args, cwd=str(ROOT), text=True, capture_output=True, env=env)
    result = {
        "command": args,
        "returncode": cp.returncode,
        "stdout": cp.stdout,
        "stderr": cp.stderr,
    }
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": now(), **result}, ensure_ascii=False) + "\n")
    return result


def run_companyctl(*args: str) -> dict:
    return run_cmd([str(ROOT / "bin" / "companyctl"), *args])


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
    return run_cmd(args)


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
    conn = companyctl.connect()
    try:
        rows = conn.execute(
            "SELECT id, target_agent, title, created_at FROM tasks WHERE status = 'submitted' AND created_at <= ? ORDER BY created_at LIMIT ?",
            (cutoff, limit),
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


def summarize_state(state: dict) -> dict:
    steps = state.get("results", [])
    failed_steps = []
    heartbeat_agents = []
    adapter_steps = []
    counts = {"steps": len(steps), "heartbeats": 0, "adapters": 0, "repair": 0, "scheduler": 0, "supervisor": 0, "watchdog": 0, "failed": 0}
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
    return {
        "ok": state.get("ok", False),
        "at": state.get("at", ""),
        "counts": counts,
        "failed_steps": failed_steps,
        "heartbeat_agents": heartbeat_agents,
        "adapter_agents": adapter_steps,
        "state_file": state.get("state_file", ""),
    }


def tick(config: dict) -> dict:
    results = []
    if config.get("run_repair", True):
        results.append({"step": "repair.reset-stale-claims", "result": run_companyctl("repair", "reset-stale-claims")})
    if config.get("run_scheduler", True):
        results.append({"step": "scheduler.run", "result": run_companyctl("scheduler", "run")})
    if config.get("run_supervisor_delivery_loop", config.get("run_scheduler", True)):
        results.append({"step": "supervisor.delivery-loop", "result": run_companyctl("supervisor", "delivery-loop")})
    for result in retry_due_adapter_runs(config):
        results.append({"step": "retry.adapter-run", "result": result})
    for agent in resolve_heartbeat_agents(config):
        results.append({"step": f"heartbeat.{agent}", "result": run_companyctl("heartbeat", "--agent", agent)})
    for worker in config.get("adapter_workers", []):
        if not worker.get("enabled", False):
            continue
        adapter_state = run_adapter(worker)
        results.append({"step": f"adapter.{worker.get('agent', '')}", "result": {"returncode": 0 if adapter_state["ok"] else 1, "stdout": json.dumps(adapter_state, ensure_ascii=False), "stderr": ""}})
    for result in check_unclaimed_tasks(config):
        results.append({"step": "watchdog.unclaimed-task", "result": result})
    state = {"ok": all(item["result"].get("returncode", 1) == 0 for item in results), "at": now(), "results": results}
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
