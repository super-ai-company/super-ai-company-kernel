from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "company.sqlite"
DEFAULT_WORKSPACE = Path("/Users/shift/openclaw/workspace-xmanx/projects/openclaw-codex-controller")


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
    return conn.execute("SELECT * FROM employees WHERE id = ?", (agent,)).fetchone()


def next_codex_task(agent: str) -> sqlite3.Row | None:
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
        "task_card": base / "codex-task-card.md",
        "last_message": base / "codex-last-message.md",
        "events": base / "codex-events.jsonl",
        "report": base / "codex-adapter-report.md",
    }


def build_task_card(task: sqlite3.Row, workspace: Path, sandbox: str) -> str:
    return "\n".join(
        [
            "# Codex Task Card",
            "",
            "## Goal",
            "",
            task["title"],
            "",
            "## Description",
            "",
            task["description"] or "No extra description provided.",
            "",
            "## Canonical repo / workspace",
            "",
            f"- Path: `{workspace}`",
            "- Active owner: Codex implements or reports blocker; Company Kernel verifies evidence.",
            "",
            "## Allowed scope",
            "",
            "- Stay inside the canonical workspace unless the task explicitly says otherwise.",
            "- Do not modify Company Kernel internals unless the task explicitly targets this project.",
            "",
            "## Forbidden actions",
            "",
            "- Do not expose secrets or credentials.",
            "- Do not perform external sends, payment, destructive DB writes, or production deploys.",
            "- Do not edit OpenClaw/Hermes/Codex runtime configuration unless explicitly requested.",
            "",
            "## Reporting",
            "",
            "- Return changed files, verification commands and results, blocker/risk, and next action.",
            "- If no code change is safe, explain why and return a blocker.",
            "",
            "## Company Kernel Metadata",
            "",
            f"- task_id: `{task['id']}`",
            f"- source_agent: `{task['source_agent']}`",
            f"- target_agent: `{task['target_agent']}`",
            f"- priority: `{task['priority']}`",
            f"- sandbox: `{sandbox}`",
            "",
        ]
    )


def write_report(p: Path, task: sqlite3.Row, *, executed: bool, status: str, detail: str, task_card: Path, output: Path) -> None:
    p.write_text(
        "\n".join(
            [
                f"# Codex Adapter Report: {task['id']}",
                "",
                f"- generated_at: `{now()}`",
                f"- executed: `{str(executed).lower()}`",
                f"- status: `{status}`",
                f"- task_card: `{task_card}`",
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


def run_codex(task_card: Path, workspace: Path, output: Path, events: Path, sandbox: str, model: str) -> tuple[int, str]:
    cmd = [
        "codex",
        "exec",
        "--ignore-rules",
        "--ephemeral",
        "-C",
        str(workspace),
        "-s",
        sandbox,
        "-o",
        str(output),
        "-",
    ]
    if model:
        cmd[2:2] = ["--model", model]
    with task_card.open("r", encoding="utf-8") as stdin, events.open("w", encoding="utf-8") as event_out:
        cp = subprocess.run(cmd, stdin=stdin, stdout=event_out, stderr=subprocess.STDOUT, text=True)
    return cp.returncode, " ".join(cmd)


def process(args: argparse.Namespace) -> int:
    emp = employee(args.agent)
    if not emp:
        emit({"ok": False, "error": "unknown employee", "agent": args.agent})
        return 1
    if emp["runtime"] != "codex":
        emit({"ok": False, "error": "employee runtime is not codex", "agent": args.agent, "runtime": emp["runtime"]})
        return 1
    if args.execute and not shutil.which("codex"):
        emit({"ok": False, "error": "codex command not found"})
        return 1
    task = next_codex_task(args.agent)
    if not task:
        run_companyctl(["heartbeat", "--agent", args.agent])
        emit({"ok": True, "processed": 0, "agent": args.agent, "note": "no submitted Codex task"})
        return 0
    workspace = Path(args.workspace or emp["workspace"] or DEFAULT_WORKSPACE).expanduser()
    artifact = paths(args.agent, task["id"])
    artifact["task_card"].write_text(build_task_card(task, workspace, args.sandbox), encoding="utf-8")
    claim_code, claim_out, claim_err = run_companyctl(["task", "claim", "--agent", args.agent, "--task-id", task["id"]])
    if claim_code != 0:
        emit({"ok": False, "error": "claim failed", "stdout": claim_out, "stderr": claim_err})
        return claim_code
    if not args.execute:
        detail = "Codex adapter dry-run generated task card. Use --execute to run codex exec."
        write_report(artifact["report"], task, executed=False, status="completed", detail=detail, task_card=artifact["task_card"], output=artifact["last_message"])
        done_code, done_out, done_err = run_companyctl(["task", "done", "--agent", args.agent, "--task-id", task["id"], "--summary", detail, "--evidence", str(artifact["report"])])
        run_companyctl(["heartbeat", "--agent", args.agent])
        emit({"ok": done_code == 0, "processed": 1, "executed": False, "task_id": task["id"], "task_card": str(artifact["task_card"]), "report": str(artifact["report"]), "companyctl_stdout": done_out, "companyctl_stderr": done_err})
        return done_code
    code, cmd = run_codex(artifact["task_card"], workspace, artifact["last_message"], artifact["events"], args.sandbox, args.model)
    if code == 0:
        detail = f"codex exec completed. command={cmd}"
        write_report(artifact["report"], task, executed=True, status="completed", detail=detail, task_card=artifact["task_card"], output=artifact["last_message"])
        done_code, done_out, done_err = run_companyctl(["task", "done", "--agent", args.agent, "--task-id", task["id"], "--summary", detail, "--evidence", str(artifact["report"])])
    else:
        detail = f"codex exec failed exit_code={code}. command={cmd}"
        write_report(artifact["report"], task, executed=True, status="blocked", detail=detail, task_card=artifact["task_card"], output=artifact["last_message"])
        done_code, done_out, done_err = run_companyctl(["task", "block", "--agent", args.agent, "--task-id", task["id"], "--blocker", detail])
    run_companyctl(["heartbeat", "--agent", args.agent])
    emit({"ok": done_code == 0 and code == 0, "processed": 1, "executed": True, "task_id": task["id"], "codex_exit_code": code, "task_card": str(artifact["task_card"]), "last_message": str(artifact["last_message"]), "events": str(artifact["events"]), "report": str(artifact["report"]), "companyctl_stdout": done_out, "companyctl_stderr": done_err})
    return done_code if done_code != 0 else code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Company Kernel Codex adapter")
    parser.add_argument("--agent", default="codex")
    parser.add_argument("--workspace", default="")
    parser.add_argument("--sandbox", default="read-only", choices=["read-only", "workspace-write", "danger-full-access"])
    parser.add_argument("--model", default="")
    parser.add_argument("--execute", action="store_true", help="actually run codex exec; without this only writes task card and report")
    return parser


def main(argv: list[str] | None = None) -> int:
    return process(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
