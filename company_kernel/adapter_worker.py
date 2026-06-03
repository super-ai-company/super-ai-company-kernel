from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from company_kernel import companyctl


ROOT = Path(os.environ.get("OPENCLAW_COMPANY_KERNEL_ROOT", Path(__file__).resolve().parents[1])).resolve()
DB_PATH = ROOT / "company.sqlite"


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


def employee(agent: str) -> sqlite3.Row | None:
    conn = connect()
    return conn.execute("SELECT * FROM employees WHERE id = ?", (agent,)).fetchone()


def next_task(agent: str) -> sqlite3.Row | None:
    conn = connect()
    return conn.execute(
        "SELECT * FROM tasks WHERE target_agent = ? AND status = 'submitted' ORDER BY created_at LIMIT 1",
        (agent,),
    ).fetchone()


def run_companyctl(args: list[str]) -> tuple[int, str, str]:
    cp = subprocess.run([str(ROOT / "bin" / "companyctl"), *args], cwd=str(ROOT), text=True, capture_output=True)
    return cp.returncode, cp.stdout, cp.stderr


def report_path(agent: str, task_id: str) -> Path:
    p = ROOT / "employees" / agent / "reports" / f"{task_id}.adapter-report.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def write_report(agent: str, task: sqlite3.Row, emp: sqlite3.Row, *, dry_run: bool, status: str, detail: str) -> Path:
    p = report_path(agent, task["id"])
    p.write_text(
        "\n".join(
            [
                f"# Adapter Report: {task['id']}",
                "",
                f"- generated_at: `{now()}`",
                f"- agent: `{agent}`",
                f"- runtime: `{emp['runtime']}`",
                f"- dry_run: `{str(dry_run).lower()}`",
                f"- task_title: `{task['title']}`",
                f"- status: `{status}`",
                "",
                "## Detail",
                "",
                detail,
                "",
            ]
        ),
        encoding="utf-8",
    )
    return p


def runtime_detail(emp: sqlite3.Row, task: sqlite3.Row, *, dry_run: bool) -> tuple[str, str]:
    runtime = emp["runtime"]
    if dry_run:
        return "completed", f"Dry-run adapter acknowledged runtime `{runtime}`. No external tool was started."
    if runtime == "local":
        return "completed", "Local adapter completed a no-op task. Real command execution is not enabled yet."
    return "blocked", f"Runtime `{runtime}` adapter is registered but real execution is not enabled. Use --dry-run or implement a dedicated adapter."


def run_once(args: argparse.Namespace) -> int:
    emp = employee(args.agent)
    if not emp:
        emit({"ok": False, "error": "unknown employee", "agent": args.agent})
        return 1
    task = next_task(args.agent)
    if not task:
        run_companyctl(["heartbeat", "--agent", args.agent])
        emit({"ok": True, "agent": args.agent, "processed": 0, "note": "no submitted task"})
        return 0
    code, out, err = run_companyctl(["task", "claim", "--agent", args.agent, "--task-id", task["id"]])
    if code != 0:
        emit({"ok": False, "agent": args.agent, "task_id": task["id"], "error": "claim failed", "stdout": out, "stderr": err})
        return code
    status, detail = runtime_detail(emp, task, dry_run=args.dry_run)
    report = write_report(args.agent, task, emp, dry_run=args.dry_run, status=status, detail=detail)
    if status == "completed":
        code, out, err = run_companyctl(["task", "done", "--agent", args.agent, "--task-id", task["id"], "--summary", detail, "--evidence", str(report)])
    else:
        code, out, err = run_companyctl(["task", "block", "--agent", args.agent, "--task-id", task["id"], "--blocker", detail])
    run_companyctl(["heartbeat", "--agent", args.agent])
    emit(
        {
            "ok": code == 0,
            "agent": args.agent,
            "runtime": emp["runtime"],
            "processed": 1,
            "task_id": task["id"],
            "status": status,
            "report": str(report),
            "companyctl_stdout": out,
            "companyctl_stderr": err,
        }
    )
    return code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Company Kernel runtime adapter worker")
    parser.add_argument("--agent", required=True)
    parser.add_argument("--dry-run", action="store_true", help="do not start external runtime tools; write evidence and complete safely")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    return run_once(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
