from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import shutil
import sqlite3
import subprocess
import uuid
from datetime import timedelta
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import codex_pm_supervisor
from . import company_daemon
from . import companyctl
from .db_paths import ensure_db_parent, resolve_db_path


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = resolve_db_path(ROOT)
SCHEMA = ROOT / "company_kernel" / "schema.sql"
DEFAULT_OUTPUT_DIR = ROOT / "reports" / "communication-acceptance"
DEFAULT_OPENCLAW_ROOT = Path("/Users/shift/openclaw")
DEFAULT_OPENCLAW_LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / "com.shift.ops-bus-worker.plist"

MAIN_AGENT = "main"
HERMES_AGENT = "hermes"
CODEX_AGENT = "codex"
ANTIGRAVITY_AGENT = "antigravity"
BUS_COUNT_KEYS = ("inbox", "running", "done", "failed")


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def emit(obj: dict[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def connect(db_path: Path | None = None, schema_path: Path | None = None) -> sqlite3.Connection:
    db_path = (db_path or DB_PATH).expanduser().resolve()
    schema_path = (schema_path or SCHEMA).expanduser().resolve()
    conn = sqlite3.connect(ensure_db_parent(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(schema_path.read_text(encoding="utf-8"))
    conn.commit()
    return conn


def rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def parse_json_output(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {"raw": raw}
    except json.JSONDecodeError:
        return {"raw": raw}


def run_companyctl_json(args: list[str], *, timeout: int = 180) -> tuple[int, dict[str, Any], str]:
    cp = subprocess.run([str(ROOT / "bin" / "companyctl"), *args], cwd=str(ROOT), text=True, capture_output=True, timeout=timeout)
    return cp.returncode, parse_json_output(cp.stdout), cp.stderr


def direct_token(source: str, target: str, round_index: int, run_id: str) -> str:
    return f"{source}_{target}_ROUND_{round_index}_{run_id}_OK"


def direct_body(token: str) -> str:
    return f"本机员工协作通信验收：请只回复 {token}"


def simulated_direct_payload(source: str, target: str, token: str, message_id: str) -> dict[str, Any]:
    return {
        "ok": True,
        "source": source,
        "target": target,
        "runtime": "simulated",
        "reply": token,
        "message": {"id": message_id, "source_agent": source, "target_agent": target, "body": direct_body(token)},
        "receipt": {"id": f"{message_id}-receipt", "source_agent": target, "target_agent": source, "body": token},
        "receipt_file": str((ROOT / "employees" / source / "inbox" / f"{message_id}-receipt.message.json").resolve()),
        "exit_code": 0,
    }


def evaluate_direct_payload(payload: dict[str, Any], *, expected_token: str) -> dict[str, Any]:
    if payload.get("ok") is not True:
        return {"passed": False, "reason": "direct_command_failed"}
    if expected_token not in str(payload.get("reply") or ""):
        return {"passed": False, "reason": "expected_reply_missing"}
    if not payload.get("receipt"):
        return {"passed": False, "reason": "missing_sender_visible_receipt"}
    return {"passed": True, "reason": "ok"}


def run_direct_probe(
    *,
    source: str,
    target: str,
    round_index: int,
    run_id: str,
    simulate: bool,
    timeout: int,
) -> dict[str, Any]:
    token = direct_token(source, target, round_index, run_id)
    message_id = f"accept-{run_id}-{source}-{target}-r{round_index}"
    if simulate:
        code = 0
        payload = simulated_direct_payload(source, target, token, message_id)
        stderr = ""
    else:
        code, payload, stderr = run_companyctl_json(
            [
                "message",
                "direct",
                "--from",
                source,
                "--to",
                target,
                "--body",
                direct_body(token),
                "--message-id",
                message_id,
                "--timeout",
                str(timeout),
            ],
            timeout=timeout + 20,
        )
    verdict = evaluate_direct_payload(payload, expected_token=token)
    return {
        "source": source,
        "target": target,
        "round": round_index,
        "message_id": message_id,
        "expected_token": token,
        "passed": bool(code == 0 and verdict["passed"]),
        "reason": verdict["reason"] if code == 0 else "direct_command_exit_nonzero",
        "payload": payload,
        "stderr": stderr[-2000:],
    }


def run_direct_matrix(*, direct_rounds: int, simulate: bool, timeout: int, run_id: str) -> dict[str, Any]:
    pairs = [
        (MAIN_AGENT, HERMES_AGENT),
        (MAIN_AGENT, CODEX_AGENT),
        (HERMES_AGENT, CODEX_AGENT),
        (CODEX_AGENT, HERMES_AGENT),
    ]
    probes = []
    for source, target in pairs:
        for index in range(1, direct_rounds + 1):
            probes.append(run_direct_probe(source=source, target=target, round_index=index, run_id=run_id, simulate=simulate, timeout=timeout))
    passed = sum(1 for item in probes if item["passed"])
    return {
        "ok": passed == len(probes),
        "pairs": [f"{source}->{target}" for source, target in pairs],
        "rounds": direct_rounds,
        "passed": passed,
        "total": len(probes),
        "success_rate": passed / len(probes) if probes else 0,
        "probes": probes,
    }


def write_progress(workspace: Path, *, task_id: str, state: str, action: str, created_at: str) -> Path:
    reports = workspace / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = reports / f"progress_{state}_{task_id}_{stamp}.json"
    path.write_text(
        json.dumps(
            {
                "ok": True,
                "task_id": task_id,
                "report": {
                    "state": state,
                    "project": "communication-acceptance",
                    "action": action,
                    "checking": f"acceptance state={state}",
                    "created_at": created_at,
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def ensure_acceptance_task(conn: sqlite3.Connection, *, task_id: str, status: str, title: str, created_at: str) -> None:
    conn.execute(
        """
        UPDATE tasks
        SET status = 'blocked',
            blocker = 'superseded by a newer communication acceptance scenario',
            updated_at = ?
        WHERE id LIKE 'acceptance-%'
          AND id != ?
          AND target_agent = ?
          AND status IN ('submitted', 'claimed')
        """,
        (created_at, task_id, CODEX_AGENT),
    )
    row = conn.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row:
        conn.execute(
            "UPDATE tasks SET source_agent = ?, target_agent = ?, title = ?, status = ?, claimed_by = ?, updated_at = ? WHERE id = ?",
            (HERMES_AGENT, CODEX_AGENT, title, status, CODEX_AGENT if status == "claimed" else "", created_at, task_id),
        )
    else:
        # INTENTIONALLY EXEMPT from submit normalization (no app→cli reroute / executor lock / 记忆会话
        # stamp): this `acceptance-*` task is local communication-acceptance scaffolding that must target
        # the CODEX_AGENT runtime exactly to verify that specific channel — rerouting it to a cli twin
        # would defeat the test. Not a real work dispatch.
        conn.execute(
            """
            INSERT INTO tasks(id, source_agent, target_agent, title, description, priority, status, claimed_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'P3', ?, ?, ?, ?)
            """,
            (
                task_id,
                HERMES_AGENT,
                CODEX_AGENT,
                title,
                "本机员工协作通信机制验收任务，不代表真实业务交付。",
                status,
                CODEX_AGENT if status == "claimed" else "",
                created_at,
                created_at,
            ),
        )
    conn.execute(
        "INSERT OR REPLACE INTO task_metadata(task_id, metadata_json, updated_at) VALUES (?, ?, ?)",
        (task_id, json.dumps({"acceptance_run": True, "trace_id": f"trace-{task_id}"}, ensure_ascii=False), created_at),
    )
    conn.commit()


def codex_workspace(conn: sqlite3.Connection) -> Path:
    row = conn.execute("SELECT workspace FROM employees WHERE id = ?", (CODEX_AGENT,)).fetchone()
    if row and row["workspace"]:
        return Path(str(row["workspace"])).expanduser().resolve()
    return (ROOT / "employees" / CODEX_AGENT).resolve()


def acceptance_workspace(run_id: str) -> Path:
    return (ROOT / "state" / "communication-acceptance" / "workspaces" / run_id / CODEX_AGENT).resolve()


def acceptance_db_path(run_id: str) -> Path:
    return (ROOT / "state" / "communication-acceptance" / "db" / f"{run_id}.sqlite").resolve()


def prepare_acceptance_db(run_id: str) -> Path:
    target = acceptance_db_path(run_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        shutil.copy2(DB_PATH, target)
    else:
        conn = sqlite3.connect(target)
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        conn.commit()
        conn.close()
    return target


def run_pm_completed_scenario(conn: sqlite3.Connection, *, run_id: str, timestamp: str, report_root: Path, workspace: Path | None = None, db_path: Path | None = None) -> dict[str, Any]:
    task_id = f"acceptance-{run_id}-codex-complete"
    ensure_acceptance_task(conn, task_id=task_id, status="claimed", title="读取 README 并总结 3 条当前能力", created_at=timestamp)
    workspace = (workspace or codex_workspace(conn)).expanduser().resolve()
    write_progress(workspace, task_id=task_id, state="acknowledged", action="Codex 已接收 Hermes 任务", created_at=timestamp)
    write_progress(workspace, task_id=task_id, state="in_progress", action="Codex 正在读取 README", created_at=timestamp)
    completed = write_progress(workspace, task_id=task_id, state="completed", action="Codex 完成 README 能力总结", created_at=timestamp)
    result = codex_pm_supervisor.supervise_once(
        agent=CODEX_AGENT,
        now_ts=timestamp,
        stale_minutes=15,
        close_completed=True,
        db_path=db_path or DB_PATH,
        schema_path=SCHEMA,
        workspace=workspace,
        report_root=report_root,
        include_fixtures=True,  # self-test deliberately supervises its acceptance-* fixture
    )
    return {"ok": result.get("status") == "completed" and result.get("evidence_path") == str(completed.resolve()), "task_id": task_id, "supervisor": result}


def run_mismatch_stale_scenario(conn: sqlite3.Connection, *, run_id: str, timestamp: str, report_root: Path, workspace: Path | None = None, db_path: Path | None = None) -> dict[str, Any]:
    task_id = f"acceptance-{run_id}-codex-mismatch"
    # This scenario asserts a *stale* task (claimed long ago, only a wrong-task progress file) gets
    # escalated. Age it past the supervisor's fresh-task grace window so the grace path doesn't
    # (correctly) treat it as still-working.
    aged = (codex_pm_supervisor.parse_time(timestamp) - timedelta(minutes=30)).isoformat()
    ensure_acceptance_task(conn, task_id=task_id, status="claimed", title="故意验证 task_id mismatch 不得完成", created_at=aged)
    workspace = (workspace or codex_workspace(conn)).expanduser().resolve()
    write_progress(workspace, task_id=f"{task_id}-wrong", state="completed", action="错误 task_id 的旧完成报告", created_at=timestamp)
    result = codex_pm_supervisor.supervise_once(
        agent=CODEX_AGENT,
        now_ts=timestamp,
        stale_minutes=1,
        close_completed=False,
        db_path=db_path or DB_PATH,
        schema_path=SCHEMA,
        workspace=workspace,
        report_root=report_root,
        include_fixtures=True,  # self-test deliberately supervises its acceptance-* fixture
    )
    return {"ok": result.get("status") in {"stalled", "blocked"}, "task_id": task_id, "mismatch_supervisor": result}


def run_continuity(conn: sqlite3.Connection, *, continuity_runs: int, run_id: str, timestamp: str, report_root: Path, workspace: Path | None = None, db_path: Path | None = None) -> dict[str, Any]:
    results = []
    for index in range(1, continuity_runs + 1):
        result = run_pm_completed_scenario(conn, run_id=f"{run_id}-loop-{index}", timestamp=timestamp, report_root=report_root, workspace=workspace, db_path=db_path)
        results.append({"run": index, "passed": bool(result["ok"]), "task_id": result["task_id"], "status": result["supervisor"].get("status"), "evidence_path": result["supervisor"].get("evidence_path", "")})
    passed = sum(1 for item in results if item["passed"])
    return {"ok": passed == continuity_runs, "passed": passed, "total": continuity_runs, "runs": results}


def baseline_snapshot(*, simulate: bool) -> dict[str, Any]:
    git = {}
    try:
        cp = subprocess.run(["git", "status", "--short", "--branch"], cwd=str(ROOT), text=True, capture_output=True, timeout=30)
        git = {"exit_code": cp.returncode, "status_short_branch": cp.stdout.strip().splitlines()}
    except Exception as exc:
        git = {"exit_code": 1, "error": str(exc)}
    if simulate:
        conn = connect()
        try:
            employees = {"ok": True, "employees": rows(conn, "SELECT * FROM employees ORDER BY id")}
            counts = {
                "employees": conn.execute("SELECT COUNT(*) FROM employees").fetchone()[0],
                "active_projects": conn.execute("SELECT COUNT(*) FROM projects WHERE status = 'active'").fetchone()[0],
                "claimed_tasks": conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'claimed'").fetchone()[0],
                "pending_events": conn.execute("SELECT COUNT(*) FROM company_events WHERE processed_at = ''").fetchone()[0],
                "pending_approvals": conn.execute("SELECT COUNT(*) FROM approvals WHERE status = 'pending'").fetchone()[0],
                "pending_rfcs": conn.execute("SELECT COUNT(*) FROM rfcs WHERE status = 'pending'").fetchone()[0],
                "heartbeats": conn.execute("SELECT COUNT(*) FROM heartbeats").fetchone()[0],
                "adapter_runs": conn.execute("SELECT COUNT(*) FROM adapter_runs").fetchone()[0],
                "failed_adapter_runs": conn.execute("SELECT COUNT(*) FROM adapter_runs WHERE ok = 0 AND acknowledged_at = ''").fetchone()[0],
                "capability_issues": 0,
                "task_evidence_issues": 0,
            }
            doctor = {
                "ok": True,
                "issues": [],
                "counts": counts,
                "heartbeat": {"stale_minutes": 15, "missing": 0, "stale": 0, "missing_agents": [], "stale_agents": []},
            }
        finally:
            conn.close()
        employee_code, employee_stderr = 0, ""
        doctor_code, doctor_stderr = 0, ""
    else:
        employee_code, employees, employee_stderr = run_companyctl_json(["employee", "list"])
        doctor_code, doctor, doctor_stderr = run_companyctl_json(["doctor", "--summary"])
    return {
        "git": git,
        "employee_list": {"exit_code": employee_code, "payload": employees, "stderr": employee_stderr[-2000:]},
        "doctor_summary": {"exit_code": doctor_code, "payload": doctor, "stderr": doctor_stderr[-2000:]},
    }


def restore_runtime_state(*, write_heartbeats: bool, run_daemon_once: bool, simulate: bool) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    if write_heartbeats:
        for agent in (MAIN_AGENT, HERMES_AGENT, CODEX_AGENT):
            if simulate:
                conn = connect()
                try:
                    row = conn.execute("SELECT runtime, workspace FROM employees WHERE id = ?", (agent,)).fetchone()
                    ts = now()
                    conn.execute(
                        """
                        INSERT INTO heartbeats(agent_id, runtime, workspace, status, last_seen_at, metadata_json)
                        VALUES (?, ?, ?, 'alive', ?, ?)
                        ON CONFLICT(agent_id) DO UPDATE SET runtime=excluded.runtime, workspace=excluded.workspace,
                          status='alive', last_seen_at=excluded.last_seen_at, metadata_json=excluded.metadata_json
                        """,
                        (agent, row["runtime"] if row else "", row["workspace"] if row else "", ts, json.dumps({"source": "communication_acceptance_simulated"}, ensure_ascii=False)),
                    )
                    conn.commit()
                finally:
                    conn.close()
                code, payload, stderr = 0, {"ok": True, "heartbeat": {"agent_id": agent}}, ""
            else:
                code, payload, stderr = run_companyctl_json(["heartbeat", "--agent", agent])
            actions.append({"type": "heartbeat", "agent": agent, "exit_code": code, "ok": payload.get("ok") is True, "payload": payload, "stderr": stderr[-1000:]})
    if run_daemon_once:
        if simulate:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                try:
                    code = company_daemon.main(["--once", "--summary"])
                except SystemExit as exc:
                    code = int(exc.code or 0)
            payload = parse_json_output(output.getvalue())
            stderr = ""
        else:
            cp = subprocess.run([str(ROOT / "bin" / "company-daemon"), "--once", "--summary"], cwd=str(ROOT), text=True, capture_output=True, timeout=120)
            code = cp.returncode
            payload = parse_json_output(cp.stdout)
            stderr = cp.stderr
        actions.append({"type": "daemon_once", "exit_code": code, "ok": bool(payload.get("ok")), "payload": payload, "stderr": stderr[-1000:]})
    return {"actions": actions, "ok": all(item["ok"] for item in actions) if actions else True}


def antigravity_runtime_scope(conn: sqlite3.Connection, *, simulate: bool, timeout: int, run_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT status FROM employees WHERE id = ?", (ANTIGRAVITY_AGENT,)).fetchone()
    status = str(row["status"]) if row else "missing"
    probe = run_direct_probe(source=MAIN_AGENT, target=ANTIGRAVITY_AGENT, round_index=1, run_id=run_id, simulate=simulate, timeout=timeout) if row else {}
    if status == "active":
        return {
            "ok": bool(probe.get("passed")),
            "scope": "active_runtime_smoke",
            "status": status,
            "probe": probe,
            "rule": "Active Antigravity must return sender-visible direct receipt; structured runtime evidence is verified by employee verify-runtime.",
        }
    return {
        "ok": status == "candidate",
        "scope": "candidate_only",
        "status": status,
        "probe": probe,
        "rule": "Antigravity remains candidate until it returns durable structured evidence.",
    }


def read_json_file(path: Path) -> tuple[dict[str, Any] | None, str]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, dict) else {"value": parsed}, ""
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def first_existing(paths: list[Path]) -> Path | None:
    for path in paths:
        if path.exists():
            return path
    return None


def bus_totals(state: dict[str, Any] | None, key: str) -> dict[str, int]:
    totals = {name: 0 for name in BUS_COUNT_KEYS}
    section = state.get(key) if isinstance(state, dict) else None
    if not isinstance(section, dict):
        return totals
    for item in section.values():
        if not isinstance(item, dict):
            continue
        for name in BUS_COUNT_KEYS:
            totals[name] += int(item.get(name, 0) or 0)
    return totals


def collect_message_ids(value: Any) -> list[str]:
    found: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key, val in item.items():
                if key in {"messageId", "message_id"} and val not in (None, ""):
                    found.append(str(val))
                visit(val)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return sorted(set(found))


def openclaw_candidate_paths(openclaw_root: Path) -> dict[str, list[Path]]:
    return {
        "supervisor_script": [openclaw_root / "scripts" / "openclaw_agent_supervisor.py"],
        "delivery_loop": [
            openclaw_root / "workspace-xmanx" / "scripts" / "supervisor_autonomous_delivery_loop.py",
            openclaw_root / "scripts" / "supervisor_autonomous_delivery_loop.py",
        ],
        "supervisor_state": [
            openclaw_root / "reports" / "openclaw-agent-supervisor-state.json",
            openclaw_root / "state" / "openclaw-agent-supervisor-state.json",
        ],
        "delivery_state": [
            openclaw_root / "reports" / "openclaw-agent-supervisor-delivery-state.json",
            openclaw_root / "state" / "openclaw-agent-supervisor-delivery-state.json",
        ],
    }


def read_openclaw_autonomous_evidence(
    *,
    openclaw_root: Path | str = DEFAULT_OPENCLAW_ROOT,
    launch_agent_path: Path | str = DEFAULT_OPENCLAW_LAUNCH_AGENT,
    expected_message_ids: list[str] | None = None,
) -> dict[str, Any]:
    openclaw_root = Path(openclaw_root).expanduser().resolve()
    launch_agent_path = Path(launch_agent_path).expanduser().resolve()
    candidates = openclaw_candidate_paths(openclaw_root)
    resolved = {key: first_existing(paths) for key, paths in candidates.items()}
    missing = [key for key, path in resolved.items() if path is None]
    if not launch_agent_path.exists():
        missing.append("launch_agent")

    state: dict[str, Any] | None = None
    delivery: dict[str, Any] | None = None
    errors: dict[str, str] = {}
    if resolved["supervisor_state"]:
        state, error = read_json_file(resolved["supervisor_state"])
        if error:
            errors["supervisor_state"] = error
    if resolved["delivery_state"]:
        delivery, error = read_json_file(resolved["delivery_state"])
        if error:
            errors["delivery_state"] = error

    launch_text = ""
    if launch_agent_path.exists():
        try:
            launch_text = launch_agent_path.read_text(encoding="utf-8")
        except Exception as exc:
            errors["launch_agent"] = f"{type(exc).__name__}: {exc}"

    supervisor_path = resolved["supervisor_script"]
    references_supervisor = bool(supervisor_path and (str(supervisor_path) in launch_text or supervisor_path.name in launch_text))
    start_interval_match = re.search(r"<key>StartInterval</key>\s*<integer>(\d+)</integer>", launch_text)
    after_totals = bus_totals(state, "after")
    before_totals = bus_totals(state, "before")
    message_ids = collect_message_ids(delivery)
    expected = expected_message_ids or []
    expected_found = [message_id for message_id in expected if message_id in message_ids]
    expected_missing = [message_id for message_id in expected if message_id not in message_ids]
    state_ok = state.get("ok") is True if isinstance(state, dict) else False
    delivery_ok = delivery.get("ok") is True if isinstance(delivery, dict) else False
    delivery_suppressed = delivery.get("suppressed") is True if isinstance(delivery, dict) else False
    all_paths_readable = not missing and not errors
    green = bool(all_paths_readable and state_ok and delivery_ok and not delivery_suppressed and after_totals["inbox"] == 0 and after_totals["running"] == 0)

    return {
        "ok": bool(all_paths_readable and state is not None and delivery is not None),
        "green": green,
        "read_only": True,
        "openclaw_root": str(openclaw_root),
        "paths": {key: str(path) if path else "" for key, path in resolved.items()},
        "missing": missing,
        "errors": errors,
        "launch_agent": {
            "path": str(launch_agent_path),
            "exists": launch_agent_path.exists(),
            "references_supervisor": references_supervisor,
            "start_interval_seconds": int(start_interval_match.group(1)) if start_interval_match else None,
        },
        "state": {
            "ok": state_ok,
            "started_at": state.get("started_at", "") if isinstance(state, dict) else "",
            "finished_at": state.get("finished_at", "") if isinstance(state, dict) else "",
            "failed_steps": [
                {"name": step.get("name", ""), "returncode": step.get("returncode", "")}
                for step in (state.get("steps", []) if isinstance(state, dict) else [])
                if isinstance(step, dict) and step.get("ok") is not True
            ],
        },
        "delivery": {
            "ok": delivery_ok,
            "suppressed": delivery_suppressed,
            "dry_run": delivery.get("dry_run") if isinstance(delivery, dict) else None,
            "notify_reason": delivery.get("notify_reason", "") if isinstance(delivery, dict) else "",
            "timestamp": delivery.get("timestamp", "") if isinstance(delivery, dict) else "",
        },
        "bus_totals": {"before": before_totals, "after": after_totals},
        "telegram_message_ids": message_ids,
        "expected_message_ids": {"expected": expected, "found": expected_found, "missing": expected_missing},
        "note": "Read-only OpenClaw evidence collection. It does not mutate LaunchAgent, bus, bots, or OpenClaw state.",
    }


def write_reports(result: dict[str, Any], output_dir: Path, run_id: str) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{run_id}.json"
    md_path = output_dir / f"{run_id}.md"
    result["reports"] = {"json": str(json_path), "markdown": str(md_path)}
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    latest_json = output_dir / "latest.json"
    latest_md = output_dir / "latest.md"
    latest_json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    latest_md.write_text(render_markdown(result), encoding="utf-8")
    result["reports"]["latest_json"] = str(latest_json)
    result["reports"]["latest_markdown"] = str(latest_md)
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result["reports"]


def render_markdown(result: dict[str, Any]) -> str:
    metrics = result.get("metrics", {})
    lines = [
        "# 本机 AI 员工协作通信机制验收报告",
        "",
        f"- run_id: `{result.get('run_id', '')}`",
        f"- mode: `{result.get('mode', '')}`",
        f"- mechanism_ok: `{result.get('mechanism_ok')}`",
        f"- real_execution: `{result.get('real_execution')}`",
        f"- direct: {metrics.get('direct_passed', 0)}/{metrics.get('direct_total', 0)}",
        f"- continuity: {metrics.get('continuity_passed', 0)}/{metrics.get('continuity_total', 0)}",
        "",
        "## 人类可读结果",
        "",
        result.get("human_summary", ""),
        "",
        "## 线路结果",
        "",
    ]
    for key, route in result.get("routes", {}).items():
        lines.append(f"- `{key}`: ok={route.get('ok')} status={route.get('status', route.get('scope', ''))}")
    lines.extend(
        [
            "",
            "## 风险",
            "",
        ]
    )
    risks = result.get("risks", [])
    if risks:
        lines.extend(f"- {risk}" for risk in risks)
    else:
        lines.append("- 未发现报告级风险。")
    openclaw = result.get("routes", {}).get("G_openclaw_autonomous_delivery_readonly") if isinstance(result.get("routes"), dict) else None
    if isinstance(openclaw, dict):
        lines.extend(
            [
                "",
                "## OpenClaw 主链路只读证据",
                "",
                f"- evidence_ok: `{openclaw.get('ok')}`",
                f"- green: `{openclaw.get('green')}`",
                f"- state_ok: `{openclaw.get('state', {}).get('ok') if isinstance(openclaw.get('state'), dict) else ''}`",
                f"- delivery_ok: `{openclaw.get('delivery', {}).get('ok') if isinstance(openclaw.get('delivery'), dict) else ''}`",
                f"- delivery_suppressed: `{openclaw.get('delivery', {}).get('suppressed') if isinstance(openclaw.get('delivery'), dict) else ''}`",
                f"- bus_after: `{openclaw.get('bus_totals', {}).get('after') if isinstance(openclaw.get('bus_totals'), dict) else {}}`",
                f"- expected_message_ids: `{openclaw.get('expected_message_ids', {})}`",
                f"- state_path: `{openclaw.get('paths', {}).get('supervisor_state', '') if isinstance(openclaw.get('paths'), dict) else ''}`",
                f"- delivery_path: `{openclaw.get('paths', {}).get('delivery_state', '') if isinstance(openclaw.get('paths'), dict) else ''}`",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def compact_result(result: dict[str, Any]) -> dict[str, Any]:
    compact = {
        "ok": result.get("ok"),
        "mechanism_ok": result.get("mechanism_ok"),
        "real_execution": result.get("real_execution"),
        "mode": result.get("mode"),
        "run_id": result.get("run_id", ""),
        "metrics": result.get("metrics", {}),
        "human_summary": result.get("human_summary", ""),
        "risks": result.get("risks", []),
        "reports": result.get("reports", {}),
        "acceptance_db": result.get("acceptance_db", ""),
    }
    openclaw_route = result.get("routes", {}).get("G_openclaw_autonomous_delivery_readonly") if isinstance(result.get("routes"), dict) else None
    if isinstance(openclaw_route, dict):
        compact["openclaw_evidence"] = {
            "ok": openclaw_route.get("ok"),
            "green": openclaw_route.get("green"),
            "state_ok": openclaw_route.get("state", {}).get("ok") if isinstance(openclaw_route.get("state"), dict) else None,
            "delivery_ok": openclaw_route.get("delivery", {}).get("ok") if isinstance(openclaw_route.get("delivery"), dict) else None,
            "delivery_suppressed": openclaw_route.get("delivery", {}).get("suppressed") if isinstance(openclaw_route.get("delivery"), dict) else None,
            "bus_after": openclaw_route.get("bus_totals", {}).get("after") if isinstance(openclaw_route.get("bus_totals"), dict) else None,
            "expected_message_ids": openclaw_route.get("expected_message_ids", {}),
        }
    return compact


def classify_pending(doctor_payload: dict[str, Any]) -> dict[str, Any]:
    counts = doctor_payload.get("counts") if isinstance(doctor_payload.get("counts"), dict) else {}
    heartbeat = doctor_payload.get("heartbeat") if isinstance(doctor_payload.get("heartbeat"), dict) else {}
    return {
        "pending_events": int(counts.get("pending_events", 0) or 0),
        "pending_approvals": int(counts.get("pending_approvals", 0) or 0),
        "pending_rfcs": int(counts.get("pending_rfcs", 0) or 0),
        "missing_heartbeat_agents": heartbeat.get("missing_agents", []),
        "stale_heartbeat_agents": heartbeat.get("stale_agents", []),
    }


def run_acceptance(
    *,
    simulate: bool = False,
    direct_rounds: int = 3,
    continuity_runs: int = 10,
    output_dir: Path | None = None,
    timeout: int = 120,
    now_ts: str | None = None,
    write_heartbeats: bool = False,
    run_daemon_once: bool = False,
    include_openclaw_evidence: bool = False,
    strict_openclaw_evidence: bool = False,
    openclaw_root: Path | str = DEFAULT_OPENCLAW_ROOT,
    openclaw_launch_agent_path: Path | str = DEFAULT_OPENCLAW_LAUNCH_AGENT,
    expected_openclaw_message_ids: list[str] | None = None,
) -> dict[str, Any]:
    if direct_rounds < 1:
        raise ValueError("direct_rounds must be >= 1")
    if continuity_runs < 1:
        raise ValueError("continuity_runs must be >= 1")
    timestamp = now_ts or now()
    run_id = f"comm-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    output_dir = (output_dir or DEFAULT_OUTPUT_DIR).expanduser().resolve()
    baseline_before = baseline_snapshot(simulate=simulate)
    runtime_restore = restore_runtime_state(write_heartbeats=write_heartbeats, run_daemon_once=run_daemon_once, simulate=simulate)
    acceptance_db = prepare_acceptance_db(run_id)
    conn = connect(db_path=acceptance_db)
    try:
        direct = run_direct_matrix(direct_rounds=direct_rounds, simulate=simulate, timeout=timeout, run_id=run_id)
        isolated_workspace = acceptance_workspace(run_id)
        pm = run_pm_completed_scenario(conn, run_id=run_id, timestamp=timestamp, report_root=ROOT, workspace=isolated_workspace, db_path=acceptance_db)
        states_seen = [item.get("state", "") for item in pm["supervisor"].get("progress_history", [])]
        mismatch = run_mismatch_stale_scenario(conn, run_id=run_id, timestamp=timestamp, report_root=ROOT, workspace=isolated_workspace, db_path=acceptance_db)
        continuity = run_continuity(conn, continuity_runs=continuity_runs, run_id=run_id, timestamp=timestamp, report_root=ROOT, workspace=isolated_workspace, db_path=acceptance_db)
        antigravity = antigravity_runtime_scope(conn, simulate=simulate, timeout=timeout, run_id=run_id)
    finally:
        conn.close()
    baseline_after = baseline_snapshot(simulate=simulate)
    direct_total = int(direct["total"])
    direct_passed = int(direct["passed"])
    continuity_total = int(continuity["total"])
    continuity_passed = int(continuity["passed"])
    routes = {
        "A_direct_matrix": direct,
        "B_hermes_codex_pm": pm,
        "C_task_state_closure": {"ok": pm["supervisor"].get("status") == "completed", "status": pm["supervisor"].get("status"), "task_id": pm["task_id"], "evidence_path": pm["supervisor"].get("evidence_path", "")},
        "D_progress_visibility": {"ok": states_seen == ["acknowledged", "in_progress", "completed"], "states_seen": states_seen},
        "E_stale_blocked": mismatch,
        "F_antigravity_runtime": antigravity,
    }
    openclaw_evidence: dict[str, Any] | None = None
    if include_openclaw_evidence:
        openclaw_evidence = read_openclaw_autonomous_evidence(
            openclaw_root=openclaw_root,
            launch_agent_path=openclaw_launch_agent_path,
            expected_message_ids=expected_openclaw_message_ids,
        )
        routes["G_openclaw_autonomous_delivery_readonly"] = openclaw_evidence
    mechanism_routes = {key: route for key, route in routes.items() if key != "G_openclaw_autonomous_delivery_readonly"}
    mechanism_ok = all(bool(route.get("ok")) for route in mechanism_routes.values())
    if strict_openclaw_evidence and openclaw_evidence is not None:
        mechanism_ok = mechanism_ok and bool(openclaw_evidence.get("green"))
    doctor_after = baseline_after.get("doctor_summary", {}).get("payload", {})
    pending = classify_pending(doctor_after)
    risks = []
    if pending["pending_events"]:
        risks.append(f"pending_events={pending['pending_events']} needs classification; acceptance does not clear historical events.")
    if pending["pending_approvals"]:
        risks.append(f"pending_approvals={pending['pending_approvals']} needs owner decision; acceptance does not auto-approve.")
    if pending["missing_heartbeat_agents"] or pending["stale_heartbeat_agents"]:
        risks.append(f"heartbeat issues remain: missing={pending['missing_heartbeat_agents']} stale={pending['stale_heartbeat_agents']}")
    if openclaw_evidence is not None and not openclaw_evidence.get("green"):
        risks.append("OpenClaw autonomous delivery evidence is readable but not green; do not claim full production health.")
    if openclaw_evidence is not None and openclaw_evidence.get("expected_message_ids", {}).get("missing"):
        risks.append(f"OpenClaw expected Telegram messageIds missing from latest delivery evidence: {openclaw_evidence['expected_message_ids']['missing']}")
    result: dict[str, Any] = {
        "ok": mechanism_ok,
        "mechanism_ok": mechanism_ok,
        "real_execution": not simulate,
        "mode": "simulated" if simulate else "live",
        "run_id": run_id,
        "generated_at": timestamp,
        "baseline_before": baseline_before,
        "runtime_restore": runtime_restore,
        "baseline_after": baseline_after,
        "acceptance_db": str(acceptance_db),
        "routes": routes,
        "metrics": {
            "direct_total": direct_total,
            "direct_passed": direct_passed,
            "direct_success_rate": direct_passed / direct_total if direct_total else 0,
            "continuity_total": continuity_total,
            "continuity_passed": continuity_passed,
            "continuity_success_rate": continuity_passed / continuity_total if continuity_total else 0,
            "progress_states_seen": states_seen,
            "stale_detection_status": mismatch["mismatch_supervisor"].get("status", ""),
        },
        "human_summary": (
            f"本机员工协作通信机制{'通过' if mechanism_ok else '未通过'}：direct {direct_passed}/{direct_total}，"
            f"连续闭环 {continuity_passed}/{continuity_total}，"
            f"stale/mismatch 判定为 {mismatch['mismatch_supervisor'].get('status', '')}。"
        ),
        "risks": risks,
        "rules": [
            "ACK/stdout/inbox/heartbeat alone is not completion.",
            "Completion requires sender-visible receipt, task-scoped progress, supervisor decision, and evidence/blocker.",
            "Antigravity remains candidate unless durable structured evidence exists; active Antigravity requires sender-visible runtime smoke.",
        ],
    }
    reports = write_reports(result, output_dir, run_id)
    result["reports"] = reports
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run local AI employee communication acceptance checks.")
    parser.add_argument("--simulate", action="store_true", help="use deterministic simulated direct replies for offline verification")
    parser.add_argument("--direct-rounds", type=int, default=3)
    parser.add_argument("--continuity-runs", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--write-heartbeats", action="store_true", help="write heartbeat for main/hermes/codex before checks")
    parser.add_argument("--run-daemon-once", action="store_true", help="run company-daemon --once --summary before checks")
    parser.add_argument("--openclaw-evidence", action="store_true", help="include read-only OpenClaw autonomous delivery evidence")
    parser.add_argument("--strict-openclaw-evidence", action="store_true", help="fail acceptance unless OpenClaw read-only evidence is green")
    parser.add_argument("--openclaw-root", default=str(DEFAULT_OPENCLAW_ROOT), help="OpenClaw root for read-only evidence collection")
    parser.add_argument("--openclaw-launch-agent", default=str(DEFAULT_OPENCLAW_LAUNCH_AGENT), help="LaunchAgent plist path for read-only evidence collection")
    parser.add_argument("--expected-openclaw-message-id", action="append", default=[], help="Telegram messageId expected in latest OpenClaw delivery evidence; repeatable")
    parser.add_argument("--summary", action="store_true", help="print compact summary while full JSON/Markdown reports are still written")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_acceptance(
        simulate=args.simulate,
        direct_rounds=args.direct_rounds,
        continuity_runs=args.continuity_runs,
        output_dir=Path(args.output_dir),
        timeout=args.timeout,
        write_heartbeats=args.write_heartbeats,
        run_daemon_once=args.run_daemon_once,
        include_openclaw_evidence=args.openclaw_evidence,
        strict_openclaw_evidence=args.strict_openclaw_evidence,
        openclaw_root=Path(args.openclaw_root),
        openclaw_launch_agent_path=Path(args.openclaw_launch_agent),
        expected_openclaw_message_ids=args.expected_openclaw_message_id,
    )
    emit(compact_result(result) if args.summary else result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
