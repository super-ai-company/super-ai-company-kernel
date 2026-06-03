from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from company_kernel.policy_guard import require_approval


ROOT = Path(os.environ.get("OPENCLAW_COMPANY_KERNEL_ROOT", Path(__file__).resolve().parents[1])).resolve()
DB_PATH = ROOT / "company.sqlite"
OPENCLAW_ROOT = Path(os.environ.get("OPENCLAW_ROOT", "/Users/owner/openclaw")).resolve()
OPENCLAW_BUS_AGENTS = {"main", "nestcar", "chindahotpot", "invest", "video-creator", "video-publisher", "video-ops", "krothong"}


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript((ROOT / "company_kernel" / "schema.sql").read_text(encoding="utf-8"))
    conn.commit()
    return conn


def run_companyctl(args: list[str]) -> tuple[int, str, str]:
    env = {**os.environ, "OPENCLAW_COMPANY_KERNEL_ROOT": str(ROOT)}
    cp = subprocess.run([str(ROOT / "bin" / "companyctl"), *args], cwd=str(ROOT), text=True, capture_output=True, env=env)
    return cp.returncode, cp.stdout, cp.stderr


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
        "summary": task["title"],
        "description": task["description"],
        "company_kernel_task_id": task["id"],
        "company_kernel_source_agent": task["source_agent"],
        "expected_completion_evidence": "OpenClaw employee must return evidence path or blocker to Company Kernel.",
        "next_action": "openclaw_employee_process_and_report",
    }


def approval_reason(task: sqlite3.Row) -> str:
    return (
        f"OpenClaw adapter is about to submit Company Kernel task {task['id']} "
        f"to OpenClaw legacy bus target {task['target_agent']}. "
        "This can wake an external business/runtime agent and must be approved."
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


def submit_openclaw(source: str, target: str, priority: str, payload: dict) -> tuple[int, str, str]:
    cmd = [
        str(OPENCLAW_ROOT / "scripts" / "oc"),
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
    cp = subprocess.run(cmd, cwd=str(OPENCLAW_ROOT), text=True, capture_output=True, env=env)
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
        risk="P1",
        evidence=str(artifact["payload"]),
        approval_id=args.approval_id,
        metadata={"adapter": "openclaw", "task_id": task["id"], "target_agent": args.agent},
    )
    if not gate["allowed"]:
        detail = f"OpenClaw adapter execute blocked pending approval {gate['approval_request']['id']}."
        write_report(artifact["report"], task, status="blocked", detail=detail, payload_path=artifact["payload"])
        run_companyctl(["heartbeat", "--agent", args.agent])
        emit({"ok": False, "processed": 1, "executed": False, "blocked_by_approval": True, "task_id": task["id"], "approval": gate["approval_request"], "approval_file": gate["file"], "payload": str(artifact["payload"]), "report": str(artifact["report"])})
        return 2
    code, out, err = submit_openclaw("main", args.agent, task["priority"], payload)
    if code == 0:
        try:
            openclaw_file = json.loads(out).get("file", "")
        except Exception:
            openclaw_file = ""
        detail = "Submitted Company Kernel task to OpenClaw legacy bus."
        write_report(artifact["report"], task, status="completed", detail=detail, payload_path=artifact["payload"], openclaw_file=openclaw_file)
        done_code, done_out, done_err = run_companyctl(["task", "done", "--agent", args.agent, "--task-id", task["id"], "--summary", detail, "--evidence", str(artifact["report"])])
    else:
        detail = f"OpenClaw bus submit failed exit_code={code} stderr={err[:500]}"
        write_report(artifact["report"], task, status="blocked", detail=detail, payload_path=artifact["payload"])
        done_code, done_out, done_err = run_companyctl(["task", "block", "--agent", args.agent, "--task-id", task["id"], "--blocker", detail])
    run_companyctl(["heartbeat", "--agent", args.agent])
    emit({"ok": code == 0 and done_code == 0, "processed": 1, "executed": True, "task_id": task["id"], "openclaw_exit_code": code, "openclaw_stdout": out, "openclaw_stderr": err, "payload": str(artifact["payload"]), "report": str(artifact["report"]), "companyctl_stdout": done_out, "companyctl_stderr": done_err})
    return done_code if done_code != 0 else code


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
