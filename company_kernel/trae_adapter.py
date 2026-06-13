from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .adapter_result import execution_detail
from .db_paths import ensure_db_parent, resolve_db_path


ROOT = Path(os.environ.get("OPENCLAW_COMPANY_KERNEL_ROOT", Path(__file__).resolve().parents[1])).resolve()
DB_PATH = resolve_db_path(ROOT)
DEFAULT_WORKSPACE = Path(os.environ.get("COMPANY_TRAE_WORKSPACE", str(Path.home()))).expanduser().resolve()


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
    cp = subprocess.run([str(ROOT / "bin" / "companyctl"), *args], cwd=str(ROOT), text=True, capture_output=True)
    return cp.returncode, cp.stdout, cp.stderr


def employee(agent: str) -> sqlite3.Row | None:
    conn = connect()
    return conn.execute("SELECT * FROM employees WHERE id = ?", (agent,)).fetchone()


def next_task(agent: str) -> sqlite3.Row | None:
    conn = connect()
    return conn.execute(
        "SELECT * FROM tasks WHERE target_agent = ? AND status = 'submitted' ORDER BY created_at LIMIT 1",
        (agent,),
    ).fetchone()


def paths(agent: str, task_id: str) -> dict[str, Path]:
    base = ROOT / "employees" / agent / "reports" / task_id
    base.mkdir(parents=True, exist_ok=True)
    return {
        "base": base,
        "prompt": base / "trae-prompt.md",
        "output": base / "trae-output.md",
        "report": base / "trae-adapter-report.md",
    }


def build_prompt(task: sqlite3.Row) -> str:
    return "\n".join(
        [
            "# Trae Company Kernel Task",
            "",
            "You are Trae acting as a Super AI Company IDE employee.",
            "Follow Company Kernel rules: no secrets, no destructive operations, no external sends, and always provide evidence or blocker.",
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
            "## Required output",
            "",
            "Return status, summary, evidence, blockers, and next action.",
            "",
        ]
    )


def write_report(path: Path, task: sqlite3.Row, *, executed: bool, status: str, detail: str, prompt: Path, output: Path) -> None:
    path.write_text(
        "\n".join(
            [
                f"# Trae Adapter Report: {task['id']}",
                "",
                f"- generated_at: `{now()}`",
                f"- executed: `{str(executed).lower()}`",
                f"- status: `{status}`",
                f"- prompt: `{prompt}`",
                f"- output: `{output}`",
                "",
                "## Detail",
                "",
                detail,
                "",
            ]
        ),
        encoding="utf-8",
    )


def run_trae(prompt: Path, output: Path, workspace: Path, mode: str, new_window: bool) -> tuple[int, str]:
    cmd = ["trae", "chat", "--mode", mode, prompt.read_text(encoding="utf-8")]
    if new_window:
        cmd.append("--new-window")
    else:
        cmd.append("--reuse-window")
    cp = subprocess.run(cmd, cwd=str(workspace), text=True, capture_output=True)
    output.write_text((cp.stdout or "") + ("\n\n## stderr\n\n" + cp.stderr if cp.stderr else ""), encoding="utf-8")
    return cp.returncode, " ".join(["trae", "chat", "--mode", mode, "<prompt>"])


def process(args: argparse.Namespace) -> int:
    emp = employee(args.agent)
    if not emp:
        emit({"ok": False, "error": "unknown employee", "agent": args.agent})
        return 1
    if emp["runtime"] != "trae":
        emit({"ok": False, "error": "employee runtime is not trae", "agent": args.agent, "runtime": emp["runtime"]})
        return 1
    if args.execute and not shutil.which("trae"):
        emit({"ok": False, "error": "trae command not found"})
        return 1
    task = next_task(args.agent)
    if not task:
        run_companyctl(["heartbeat", "--agent", args.agent])
        emit({"ok": True, "processed": 0, "agent": args.agent, "note": "no submitted Trae task"})
        return 0
    workspace = Path(args.workspace or emp["workspace"] or DEFAULT_WORKSPACE).expanduser()
    artifact = paths(args.agent, task["id"])
    artifact["prompt"].write_text(build_prompt(task), encoding="utf-8")
    claim_code, claim_out, claim_err = run_companyctl(["task", "claim", "--agent", args.agent, "--task-id", task["id"]])
    if claim_code != 0:
        emit({"ok": False, "error": "claim failed", "stdout": claim_out, "stderr": claim_err})
        return claim_code
    if not args.execute:
        detail = "Trae adapter dry-run generated chat prompt. Use --execute to run trae chat."
        write_report(artifact["report"], task, executed=False, status="completed", detail=detail, prompt=artifact["prompt"], output=artifact["output"])
        done_code, done_out, done_err = run_companyctl(["task", "done", "--agent", args.agent, "--task-id", task["id"], "--summary", detail, "--evidence", str(artifact["report"])])
        run_companyctl(["heartbeat", "--agent", args.agent])
        emit({"ok": done_code == 0, "processed": 1, "executed": False, "task_id": task["id"], "prompt": str(artifact["prompt"]), "report": str(artifact["report"]), "companyctl_stdout": done_out, "companyctl_stderr": done_err})
        return done_code
    code, cmd = run_trae(artifact["prompt"], artifact["output"], workspace, args.mode, args.new_window)
    if code == 0:
        detail = execution_detail(cmd, artifact["output"], success=True)
        write_report(artifact["report"], task, executed=True, status="completed", detail=detail, prompt=artifact["prompt"], output=artifact["output"])
        done_code, done_out, done_err = run_companyctl(["task", "done", "--agent", args.agent, "--task-id", task["id"], "--summary", detail, "--evidence", str(artifact["report"])])
    else:
        detail = execution_detail(cmd, artifact["output"], exit_code=code, success=False)
        write_report(artifact["report"], task, executed=True, status="blocked", detail=detail, prompt=artifact["prompt"], output=artifact["output"])
        done_code, done_out, done_err = run_companyctl(["task", "block", "--agent", args.agent, "--task-id", task["id"], "--blocker", detail])
    run_companyctl(["heartbeat", "--agent", args.agent])
    emit({"ok": code == 0 and done_code == 0, "processed": 1, "executed": True, "task_id": task["id"], "trae_exit_code": code, "prompt": str(artifact["prompt"]), "output": str(artifact["output"]), "report": str(artifact["report"]), "companyctl_stdout": done_out, "companyctl_stderr": done_err})
    return done_code if done_code != 0 else code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Company Kernel Trae adapter")
    parser.add_argument("--agent", default="trae")
    parser.add_argument("--workspace", default="")
    parser.add_argument("--mode", default="ask", choices=["ask", "edit", "agent"])
    parser.add_argument("--new-window", action="store_true")
    parser.add_argument("--execute", action="store_true", help="actually run trae chat; without this only writes prompt and report")
    return parser


def main(argv: list[str] | None = None) -> int:
    return process(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
