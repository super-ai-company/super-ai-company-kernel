from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "company.sqlite"
APP_PATH = Path("/Applications/Antigravity.app")


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
    cp = subprocess.run([str(ROOT / "bin" / "companyctl"), *args], cwd=str(ROOT), text=True, capture_output=True)
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
        "brief": base / "antigravity-brief.md",
        "report": base / "antigravity-adapter-report.md",
    }


def build_brief(task: sqlite3.Row) -> str:
    return "\n".join(
        [
            "# Antigravity Company Kernel Task",
            "",
            "Antigravity is currently connected as a GUI/IDE employee.",
            "This adapter can prepare a task brief and optionally open the Antigravity app, but it cannot yet drive the GUI or claim completion automatically.",
            "",
            "## Task",
            "",
            f"- task_id: `{task['id']}`",
            f"- source_agent: `{task['source_agent']}`",
            f"- target_agent: `{task['target_agent']}`",
            f"- priority: `{task['priority']}`",
            f"- title: `{task['title']}`",
            "",
            "## Description",
            "",
            task["description"] or "No extra description provided.",
            "",
            "## Required evidence",
            "",
            "A future Antigravity GUI worker must return evidence_path or blocker through companyctl.",
            "",
        ]
    )


def task_by_id(task_id: str) -> sqlite3.Row | None:
    conn = connect()
    try:
        return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    finally:
        conn.close()


def write_report(path: Path, task: sqlite3.Row, *, executed: bool, status: str, detail: str, brief: Path) -> None:
    path.write_text(
        "\n".join(
            [
                f"# Antigravity Adapter Report: {task['id']}",
                "",
                f"- generated_at: `{now()}`",
                f"- executed: `{str(executed).lower()}`",
                f"- status: `{status}`",
                f"- app_path: `{APP_PATH}`",
                f"- brief: `{brief}`",
                "",
                "## Detail",
                "",
                detail,
                "",
            ]
        ),
        encoding="utf-8",
    )


def return_result(args: argparse.Namespace, emp: sqlite3.Row) -> int:
    if not args.task_id:
        emit({"ok": False, "error": "task id is required for result return"})
        return 2
    task = task_by_id(args.task_id)
    if not task:
        emit({"ok": False, "error": "task not found", "task_id": args.task_id})
        return 1
    if task["target_agent"] != args.agent and task["claimed_by"] != args.agent:
        emit({"ok": False, "error": "task not owned by agent", "task_id": args.task_id, "agent": args.agent})
        return 1
    artifact = paths(args.agent, task["id"])
    if not artifact["brief"].exists():
        artifact["brief"].write_text(build_brief(task), encoding="utf-8")
    status = "completed" if args.complete else "blocked"
    if args.complete and not args.evidence:
        emit({"ok": False, "error": "evidence is required for completion", "task_id": args.task_id})
        return 2
    detail = args.summary if args.complete else args.blocker
    if not detail:
        detail = "Antigravity GUI result returned through Company Kernel adapter."
    write_report(artifact["report"], task, executed=True, status=status, detail=detail, brief=artifact["brief"])
    if args.complete:
        code, out, err = run_companyctl(["task", "done", "--agent", args.agent, "--task-id", task["id"], "--summary", detail, "--evidence", args.evidence])
    else:
        code, out, err = run_companyctl(["task", "block", "--agent", args.agent, "--task-id", task["id"], "--blocker", detail])
    run_companyctl(["heartbeat", "--agent", args.agent])
    emit({"ok": code == 0, "processed": 1 if code == 0 else 0, "returned": True, "status": status, "task_id": task["id"], "agent": emp["id"], "brief": str(artifact["brief"]), "report": str(artifact["report"]), "evidence": args.evidence, "companyctl_stdout": out, "companyctl_stderr": err})
    return code


def process(args: argparse.Namespace) -> int:
    emp = employee(args.agent)
    if not emp:
        emit({"ok": False, "error": "unknown employee", "agent": args.agent})
        return 1
    if emp["runtime"] != "antigravity":
        emit({"ok": False, "error": "employee runtime is not antigravity", "agent": args.agent, "runtime": emp["runtime"]})
        return 1
    if args.complete or args.block:
        return return_result(args, emp)
    if not APP_PATH.exists():
        emit({"ok": False, "error": "Antigravity.app not found", "path": str(APP_PATH)})
        return 1
    task = next_task(args.agent)
    if not task:
        run_companyctl(["heartbeat", "--agent", args.agent])
        emit({"ok": True, "processed": 0, "agent": args.agent, "note": "no submitted Antigravity task"})
        return 0
    artifact = paths(args.agent, task["id"])
    artifact["brief"].write_text(build_brief(task), encoding="utf-8")
    claim_code, claim_out, claim_err = run_companyctl(["task", "claim", "--agent", args.agent, "--task-id", task["id"]])
    if claim_code != 0:
        emit({"ok": False, "error": "claim failed", "stdout": claim_out, "stderr": claim_err})
        return claim_code
    if not args.execute:
        detail = "Antigravity adapter dry-run generated GUI task brief. Use --execute to open Antigravity app; completion still requires future GUI worker evidence."
        write_report(artifact["report"], task, executed=False, status="completed", detail=detail, brief=artifact["brief"])
        done_code, done_out, done_err = run_companyctl(["task", "done", "--agent", args.agent, "--task-id", task["id"], "--summary", detail, "--evidence", str(artifact["report"])])
        run_companyctl(["heartbeat", "--agent", args.agent])
        emit({"ok": done_code == 0, "processed": 1, "executed": False, "task_id": task["id"], "brief": str(artifact["brief"]), "report": str(artifact["report"]), "companyctl_stdout": done_out, "companyctl_stderr": done_err})
        return done_code
    cp = subprocess.run(["open", "-a", "Antigravity"], text=True, capture_output=True)
    detail = "Antigravity app opened. Task is blocked until a GUI worker or human returns evidence through Company Kernel."
    write_report(artifact["report"], task, executed=True, status="blocked", detail=detail, brief=artifact["brief"])
    done_code, done_out, done_err = run_companyctl(["task", "block", "--agent", args.agent, "--task-id", task["id"], "--blocker", detail])
    run_companyctl(["heartbeat", "--agent", args.agent])
    emit({"ok": cp.returncode == 0 and done_code == 0, "processed": 1, "executed": True, "task_id": task["id"], "open_exit_code": cp.returncode, "open_stdout": cp.stdout, "open_stderr": cp.stderr, "brief": str(artifact["brief"]), "report": str(artifact["report"]), "companyctl_stdout": done_out, "companyctl_stderr": done_err})
    return done_code if done_code != 0 else cp.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Company Kernel Antigravity GUI adapter")
    parser.add_argument("--agent", default="antigravity")
    parser.add_argument("--task-id", default="", help="task id for GUI result return")
    parser.add_argument("--summary", default="", help="completion summary for --complete")
    parser.add_argument("--evidence", default="", help="evidence path for --complete")
    parser.add_argument("--blocker", default="", help="blocker text for --block")
    result = parser.add_mutually_exclusive_group()
    result.add_argument("--complete", action="store_true", help="return completed GUI result to Company Kernel")
    result.add_argument("--block", action="store_true", help="return blocked GUI result to Company Kernel")
    parser.add_argument("--execute", action="store_true", help="open Antigravity.app; without this only writes brief and report")
    return parser


def main(argv: list[str] | None = None) -> int:
    return process(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
