from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import companyctl
from company_kernel.db_paths import ensure_db_parent, resolve_db_path
from company_kernel.policy_guard import require_approval


ROOT = Path(os.environ.get("OPENCLAW_COMPANY_KERNEL_ROOT", Path(__file__).resolve().parents[1])).resolve()
DB_PATH = resolve_db_path(ROOT)
OPENCLAW_ROOT = Path(os.environ.get("OPENCLAW_ROOT", str(Path.home() / "openclaw"))).expanduser().resolve()
OPENCLAW_BUS_AGENTS = {"main", "nestcar", "chindahotpot", "invest", "video-creator", "video-publisher", "video-ops", "krothong"}


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(ensure_db_parent(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript((ROOT / "company_kernel" / "schema.sql").read_text(encoding="utf-8"))
    conn.commit()
    return conn


def run_companyctl(args: list[str]) -> tuple[int, str, str]:
    env = {**os.environ, "OPENCLAW_COMPANY_KERNEL_ROOT": str(ROOT)}
    cp = subprocess.run([str(ROOT / "bin" / "companyctl"), *args], cwd=str(ROOT), text=True, capture_output=True, env=env)
    return cp.returncode, cp.stdout, cp.stderr


def run_companyctl_json(args: list[str]) -> tuple[int, dict, str]:
    code, out, err = run_companyctl(args)
    try:
        payload = json.loads(out or "{}")
    except json.JSONDecodeError:
        payload = {"ok": False, "raw": out}
    return code, payload, err


def employee(agent: str) -> sqlite3.Row | None:
    conn = connect()
    try:
        return conn.execute("SELECT * FROM employees WHERE id = ?", (agent,)).fetchone()
    finally:
        conn.close()


def next_task(agent: str) -> sqlite3.Row | None:
    conn = connect()
    try:
        claimed = conn.execute(
            "SELECT * FROM tasks WHERE target_agent = ? AND claimed_by = ? AND status = 'claimed' ORDER BY updated_at LIMIT 1",
            (agent, agent),
        ).fetchone()
        if claimed:
            return claimed
        return conn.execute(
            "SELECT * FROM tasks WHERE target_agent = ? AND status = 'submitted' ORDER BY created_at LIMIT 1",
            (agent,),
        ).fetchone()
    finally:
        conn.close()


def task_metadata(task_id: str) -> dict:
    conn = connect()
    try:
        row = conn.execute("SELECT metadata_json FROM task_metadata WHERE task_id = ?", (task_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return {}
    try:
        parsed = json.loads(row["metadata_json"] or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def paths(agent: str, task_id: str) -> dict[str, Path]:
    base = ROOT / "employees" / agent / "reports" / task_id
    base.mkdir(parents=True, exist_ok=True)
    return {
        "base": base,
        "payload": base / "openclaw-bus-payload.json",
        "report": base / "openclaw-adapter-report.md",
    }


def build_payload(task: sqlite3.Row) -> dict:
    return {
        "task_id": task["id"],
        "source_agent": task["source_agent"],
        "target_agent": task["target_agent"],
        "kind": "company_kernel_assignment",
        "human_origin": True,
        "reply_to_agent": task["source_agent"],
        "reply_surface": "company-kernel-message",
        "goal": task["title"],
        "description": task["description"],
        "non_goals": ["do not silently treat this as a human chat only", "do not complete without evidence"],
        "allowed_scope": ["target OpenClaw workspace only unless the task explicitly says otherwise"],
        "verification": ["return exit_code/stdout/stderr or a report path for the executed check"],
        "evidence_required": ["report_path", "exit_code", "stdout_stderr", "changed_files_or_none"],
        "blocker_format": "status/blocker/tried/evidence/next_action",
        "expected_receipts": ["claimed", "working or blocked", "done or blocked"],
        "expected_completion_evidence": "OpenClaw employee must return evidence path or blocker to Company Kernel.",
        "next_action": "openclaw_employee_claim_execute_or_block_and_report_to_source",
    }


def approval_reason(task: sqlite3.Row) -> str:
    title = (task["title"] or "").strip() or "(无标题)"
    desc = (task["description"] or "").strip().replace("\n", " ")
    if len(desc) > 140:
        desc = desc[:140] + "…"
    return (
        f"任务「{title}」(优先级 {task['priority']})。"
        f"由 {task['source_agent']} 发起,交给 {task['target_agent']} 执行(经 OpenClaw)。"
        + (f" 内容:{desc}" if desc else "")
    )


def write_report(path: Path, task: sqlite3.Row, *, status: str, detail: str, payload_path: Path, openclaw_file: str = "") -> None:
    path.write_text(
        "\n".join(
            [
                f"# OpenClaw Adapter Report: {task['id']}",
                "",
                f"- generated_at: `{now()}`",
                f"- status: `{status}`",
                f"- target_agent: `{task['target_agent']}`",
                f"- payload: `{payload_path}`",
                f"- openclaw_file: `{openclaw_file}`",
                "",
                "## Detail",
                "",
                detail,
                "",
            ]
        ),
        encoding="utf-8",
    )


def task_workspace_path(task_id: str) -> Path:
    conn = connect()
    try:
        workspace = conn.execute("SELECT path FROM task_workspaces WHERE task_id = ?", (task_id,)).fetchone()
        if workspace:
            return Path(workspace["path"])
        metadata = conn.execute("SELECT metadata_json FROM task_metadata WHERE task_id = ?", (task_id,)).fetchone()
        trace_id = ""
        if metadata:
            try:
                trace_id = str(json.loads(metadata["metadata_json"] or "{}").get("trace_id") or "")
            except json.JSONDecodeError:
                trace_id = ""
        created = companyctl.ensure_task_workspace(conn, task_id, trace_id)
        return Path(created["path"])
    finally:
        conn.close()


def copy_report_to_task_evidence(task_id: str, report: Path) -> Path:
    evidence_dir = task_workspace_path(task_id) / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    target = evidence_dir / f"openclaw-adapter-report-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
    target.write_bytes(report.read_bytes())
    return target


def submit_openclaw(source: str, target: str, priority: str, payload: dict) -> tuple[int, str, str]:
    oc_path = OPENCLAW_ROOT / "scripts" / "oc"
    if not oc_path.exists():
        return 127, "", f"OpenClaw executable not found at {oc_path}"
    cmd = [
        str(oc_path),
        "bus",
        "submit",
        "--source",
        source,
        "--target",
        target,
        "--type",
        "company_kernel_assignment",
        "--priority",
        priority,
        "--payload",
        json.dumps(payload, ensure_ascii=False),
        "--rollback",
        "Company Kernel bridge task; rollback by closing or failing the generated OpenClaw bus task.",
    ]
    env = {**os.environ, "OPENCLAW_COMPANY_KERNEL_ROOT": str(ROOT), "OPENCLAW_ROOT": str(OPENCLAW_ROOT)}
    try:
        cp = subprocess.run(cmd, cwd=str(OPENCLAW_ROOT), text=True, capture_output=True, env=env)
    except FileNotFoundError as exc:
        return 127, "", f"OpenClaw executable not found at {oc_path}: {exc}"
    except OSError as exc:
        return 1, "", f"OpenClaw bus submit failed before execution: {exc}"
    return cp.returncode, cp.stdout, cp.stderr


def process(args: argparse.Namespace) -> int:
    emp = employee(args.agent)
    if not emp:
        emit({"ok": False, "error": "unknown employee", "agent": args.agent})
        return 1
    if emp["runtime"] != "openclaw":
        emit({"ok": False, "error": "employee runtime is not openclaw", "agent": args.agent, "runtime": emp["runtime"]})
        return 1
    if args.agent not in OPENCLAW_BUS_AGENTS:
        emit({"ok": False, "error": "agent is not supported by OpenClaw legacy bus", "agent": args.agent})
        return 1
    task = next_task(args.agent)
    if not task:
        run_companyctl(["heartbeat", "--agent", args.agent])
        emit({"ok": True, "processed": 0, "agent": args.agent, "note": "no submitted OpenClaw task"})
        return 0
    artifact = paths(args.agent, task["id"])
    payload = build_payload(task)
    metadata = task_metadata(task["id"])
    task_type = str(metadata.get("task_type", "") or metadata.get("type", "") or "").strip()
    approval_metadata = {
        "adapter": "openclaw",
        "task_id": task["id"],
        "target_agent": args.agent,
        "priority": task["priority"],
    }
    if task_type:
        approval_metadata["task_type"] = task_type
    artifact["payload"].write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    claim_out = ""
    claim_err = ""
    if task["status"] != "claimed" or task["claimed_by"] != args.agent:
        claim_code, claim_out, claim_err = run_companyctl(["task", "claim", "--agent", args.agent, "--task-id", task["id"]])
        if claim_code != 0:
            emit({"ok": False, "error": "claim failed", "stdout": claim_out, "stderr": claim_err})
            return claim_code
    if not args.execute:
        detail = "OpenClaw adapter dry-run generated legacy bus payload. Use --execute to submit to OpenClaw ops/agent_bus."
        write_report(artifact["report"], task, status="completed", detail=detail, payload_path=artifact["payload"])
        done_code, done_out, done_err = run_companyctl(["task", "done", "--agent", args.agent, "--task-id", task["id"], "--summary", detail, "--evidence", str(artifact["report"])])
        run_companyctl(["heartbeat", "--agent", args.agent])
        emit({"ok": done_code == 0, "processed": 1, "executed": False, "task_id": task["id"], "payload": str(artifact["payload"]), "report": str(artifact["report"]), "companyctl_stdout": done_out, "companyctl_stderr": done_err})
        return done_code
    gate = require_approval(
        source=task["source_agent"],
        target=args.agent,
        action="external_send",
        reason=approval_reason(task),
        risk=task["priority"],
        evidence=str(artifact["payload"]),
        approval_id=args.approval_id,
        metadata=approval_metadata,
    )
    if not gate["allowed"]:
        detail = f"OpenClaw adapter execute blocked pending approval {gate['approval_request']['id']}."
        write_report(artifact["report"], task, status="blocked", detail=detail, payload_path=artifact["payload"])
        run_companyctl(["heartbeat", "--agent", args.agent])
        emit({"ok": False, "processed": 1, "executed": False, "blocked_by_approval": True, "task_id": task["id"], "approval": gate["approval_request"], "approval_file": gate["file"], "payload": str(artifact["payload"]), "report": str(artifact["report"])})
        return 2
    run_code, run_payload, run_err = run_companyctl_json(["task", "run", "--task-id", task["id"], "--agent", args.agent, "--by", args.agent, "--adapter-type", "openclaw", "--session-key", f"openclaw:{task['id']}"])
    if run_code != 0:
        emit({"ok": False, "error": "attempt start failed", "task_id": task["id"], "companyctl": run_payload, "stderr": run_err[-1000:]})
        return run_code
    attempt = run_payload["attempt"]
    attempt_id = attempt["attempt_id"]
    trace_id = str(attempt.get("trace_id", ""))
    session_id = f"openclaw-session-{args.agent}-{task['id']}"
    session_code, session_payload, session_err = run_companyctl_json(
        [
            "runtime",
            "session",
            "start",
            "--session-id",
            session_id,
            "--employee",
            args.agent,
            "--adapter-type",
            "openclaw",
            "--runtime-type",
            "legacy-bus",
            "--session-key",
            f"openclaw:{task['id']}",
            "--task-id",
            task["id"],
            "--attempt-id",
            attempt_id,
        ]
    )
    if session_code != 0:
        emit({"ok": False, "error": "runtime session start failed", "task_id": task["id"], "attempt": attempt, "companyctl": session_payload, "stderr": session_err[-1000:]})
        return session_code
    tool_call_id = f"openclaw-tool-{args.agent}-{task['id']}"
    run_companyctl_json(
        [
            "tool-call",
            "start",
            "--tool-call-id",
            tool_call_id,
            "--trace-id",
            trace_id,
            "--task-id",
            task["id"],
            "--attempt-id",
            attempt_id,
            "--employee",
            args.agent,
            "--session-id",
            session_id,
            "--tool-name",
            "openclaw.bus.submit",
            "--tool-type",
            "legacy-bus",
            "--input-summary",
            f"OpenClaw legacy bus submit target={args.agent} priority={task['priority']}",
            "--risk-level",
            "high" if task["priority"] == "P1" else "medium",
        ]
    )
    run_companyctl(["task", "progress", "--task-id", task["id"], "--agent", args.agent, "--attempt-id", attempt_id, "--state", "acknowledged", "--message", "OpenClaw adapter acknowledged managed legacy bus execution", "--progress", "5"])
    started_monotonic = time.monotonic()
    code, out, err = submit_openclaw("main", args.agent, task["priority"], payload)
    runtime_seconds = max(0, int(round(time.monotonic() - started_monotonic)))
    if code == 0:
        try:
            openclaw_file = json.loads(out).get("file", "")
        except Exception:
            openclaw_file = ""
        detail = "Submitted Company Kernel task to OpenClaw legacy bus."
        write_report(artifact["report"], task, status="completed", detail=detail, payload_path=artifact["payload"], openclaw_file=openclaw_file)
        evidence_report = copy_report_to_task_evidence(task["id"], artifact["report"])
        done_code, done_out, done_err = run_companyctl(["task", "done", "--agent", args.agent, "--task-id", task["id"], "--summary", detail, "--evidence", str(evidence_report)])
        tool_status = "success"
        attempt_status = "success"
        session_status = "stopped"
    else:
        blocker = err.strip() or out.strip() or f"OpenClaw bus submit failed exit_code={code}"
        detail = f"OpenClaw bus submit failed exit_code={code} blocker={blocker[:500]}"
        write_report(artifact["report"], task, status="blocked", detail=detail, payload_path=artifact["payload"])
        done_code, done_out, done_err = run_companyctl(["task", "block", "--agent", args.agent, "--task-id", task["id"], "--blocker", detail])
        tool_status = "failed"
        attempt_status = "failed"
        session_status = "failed"
    _, tool_payload, _ = run_companyctl_json(["tool-call", "finish", "--tool-call-id", tool_call_id, "--status", tool_status, "--output-summary", detail[:500], "--error", "" if code == 0 else detail[:500]])
    _, budget_payload, _ = run_companyctl_json(
        [
            "budget",
            "record",
            "--task-id",
            task["id"],
            "--attempt-id",
            attempt_id,
            "--employee",
            args.agent,
            "--cost-type",
            "openclaw_bridge_runtime",
            "--amount",
            "0",
            "--currency",
            "USD",
            "--model-name",
            "",
            "--provider",
            "openclaw",
            "--runtime-seconds",
            str(runtime_seconds),
            "--summary",
            f"openclaw bus submit exit_code={code}",
        ]
    )
    _, finish_payload, finish_err = run_companyctl_json(["task", "attempt", "finish", "--attempt-id", attempt_id, "--status", attempt_status, "--error", "" if code == 0 else detail[:500]])
    _, stopped_session, _ = run_companyctl_json(["runtime", "session", "stop", "--session-id", session_id, "--status", session_status, "--error", "" if code == 0 else detail[:500]])
    run_companyctl(["heartbeat", "--agent", args.agent])
    emit({"ok": code == 0 and done_code == 0, "processed": 1, "executed": True, "status": "completed" if code == 0 else "blocked", "task_id": task["id"], "blocker": "" if code == 0 else detail, "openclaw_exit_code": code, "openclaw_stdout": out, "openclaw_stderr": err, "attempt": finish_payload.get("attempt", attempt), "runtime_session": stopped_session.get("session", session_payload.get("session", {})), "tool_call": tool_payload.get("tool_call", {}), "budget_event": budget_payload.get("budget_event", {}), "payload": str(artifact["payload"]), "report": str(artifact["report"]), "companyctl_stdout": done_out, "companyctl_stderr": done_err, "companyctl_finish_stderr": finish_err[-1000:]})
    if done_code != 0:
        return done_code
    return 0 if code == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Company Kernel OpenClaw legacy bus adapter")
    parser.add_argument("--agent", required=True, help="OpenClaw target employee id, e.g. nestcar")
    parser.add_argument("--execute", action="store_true", help="actually submit to OpenClaw ops/agent_bus; without this only writes payload and report")
    parser.add_argument("--approval-id", default="", help="approved external_send approval id; if omitted the adapter searches matching approved approvals")
    return parser


def main(argv: list[str] | None = None) -> int:
    return process(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
