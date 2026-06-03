from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .sandboxing import wrap_command


ROOT = Path(os.environ.get("OPENCLAW_COMPANY_KERNEL_ROOT", Path(__file__).resolve().parents[1])).resolve()
DB_PATH = ROOT / "company.sqlite"
DEFAULT_WORKSPACE = Path(os.environ.get("OPENCLAW_HERMES_WORKSPACE", "/Users/owner/.hermes")).resolve()


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
        "prompt": base / "hermes-prompt.md",
        "output": base / "hermes-output.md",
        "report": base / "hermes-adapter-report.md",
    }


def build_prompt(task: sqlite3.Row) -> str:
    return "\n".join(
        [
            "# Hermes Company Kernel Task",
            "",
            "You are Hermes acting as a Super AI Company employee.",
            "Work under Company Kernel rules: do not modify Company Kernel internals, do not expose secrets, and do not perform external sends or destructive changes.",
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
            "Return a concise result with: status, summary, evidence produced, blockers, and next action.",
            "If safe execution is not possible, return a blocker instead of guessing.",
            "",
        ]
    )


def write_report(path: Path, task: sqlite3.Row, *, executed: bool, status: str, detail: str, prompt: Path, output: Path) -> None:
    path.write_text(
        "\n".join(
            [
                f"# Hermes Adapter Report: {task['id']}",
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


def build_hermes_command(prompt: Path, model: str, provider: str) -> list[str]:
    cmd = ["hermes", "-z", prompt.read_text(encoding="utf-8")]
    if model:
        cmd[1:1] = ["--model", model]
    if provider:
        cmd[1:1] = ["--provider", provider]
    return cmd


def run_hermes(prompt: Path, output: Path, workspace: Path, model: str, provider: str, isolation: str, sandbox_profile: str) -> tuple[int, str]:
    cmd = wrap_command(build_hermes_command(prompt, model, provider), runtime="hermes", workspace=workspace, isolation=isolation, profile_name=sandbox_profile)
    cp = subprocess.run(cmd, cwd=str(workspace), text=True, capture_output=True)
    output.write_text((cp.stdout or "") + ("\n\n## stderr\n\n" + cp.stderr if cp.stderr else ""), encoding="utf-8")
    return cp.returncode, " ".join(["hermes", "-z", "<prompt>"])


def process(args: argparse.Namespace) -> int:
    emp = employee(args.agent)
    if not emp:
        emit({"ok": False, "error": "unknown employee", "agent": args.agent})
        return 1
    if emp["runtime"] != "hermes":
        emit({"ok": False, "error": "employee runtime is not hermes", "agent": args.agent, "runtime": emp["runtime"]})
        return 1
    if args.execute and not shutil.which("hermes"):
        emit({"ok": False, "error": "hermes command not found"})
        return 1
    task = next_task(args.agent)
    if not task:
        run_companyctl(["heartbeat", "--agent", args.agent])
        emit({"ok": True, "processed": 0, "agent": args.agent, "note": "no submitted Hermes task"})
        return 0
    workspace = Path(args.workspace or emp["workspace"] or DEFAULT_WORKSPACE).expanduser()
    artifact = paths(args.agent, task["id"])
    artifact["prompt"].write_text(build_prompt(task), encoding="utf-8")
    claim_code, claim_out, claim_err = run_companyctl(["task", "claim", "--agent", args.agent, "--task-id", task["id"]])
    if claim_code != 0:
        emit({"ok": False, "error": "claim failed", "stdout": claim_out, "stderr": claim_err})
        return claim_code
    if not args.execute:
        detail = "Hermes adapter dry-run generated oneshot prompt. Use --execute to run hermes -z."
        write_report(artifact["report"], task, executed=False, status="completed", detail=detail, prompt=artifact["prompt"], output=artifact["output"])
        done_code, done_out, done_err = run_companyctl(["task", "done", "--agent", args.agent, "--task-id", task["id"], "--summary", detail, "--evidence", str(artifact["report"])])
        run_companyctl(["heartbeat", "--agent", args.agent])
        emit({"ok": done_code == 0, "processed": 1, "executed": False, "task_id": task["id"], "prompt": str(artifact["prompt"]), "report": str(artifact["report"]), "companyctl_stdout": done_out, "companyctl_stderr": done_err})
        return done_code
    code, cmd = run_hermes(artifact["prompt"], artifact["output"], workspace, args.model, args.provider, args.isolation, args.sandbox_profile)
    if code == 0:
        detail = f"hermes oneshot completed. command={cmd}"
        write_report(artifact["report"], task, executed=True, status="completed", detail=detail, prompt=artifact["prompt"], output=artifact["output"])
        done_code, done_out, done_err = run_companyctl(["task", "done", "--agent", args.agent, "--task-id", task["id"], "--summary", detail, "--evidence", str(artifact["report"])])
    else:
        detail = f"hermes oneshot failed exit_code={code}. command={cmd}"
        write_report(artifact["report"], task, executed=True, status="blocked", detail=detail, prompt=artifact["prompt"], output=artifact["output"])
        done_code, done_out, done_err = run_companyctl(["task", "block", "--agent", args.agent, "--task-id", task["id"], "--blocker", detail])
    run_companyctl(["heartbeat", "--agent", args.agent])
    emit({"ok": code == 0 and done_code == 0, "processed": 1, "executed": True, "task_id": task["id"], "hermes_exit_code": code, "prompt": str(artifact["prompt"]), "output": str(artifact["output"]), "report": str(artifact["report"]), "companyctl_stdout": done_out, "companyctl_stderr": done_err})
    return done_code if done_code != 0 else code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Company Kernel Hermes adapter")
    parser.add_argument("--agent", default="hermes")
    parser.add_argument("--workspace", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--provider", default="")
    parser.add_argument("--isolation", default="none", choices=["none", "docker", "firejail"], help="wrap hermes execution in a container/sandbox command")
    parser.add_argument("--sandbox-profile", default="default", help="sandbox profile name from config/sandbox_profiles.json")
    parser.add_argument("--execute", action="store_true", help="actually run hermes -z; without this only writes prompt and report")
    return parser


def main(argv: list[str] | None = None) -> int:
    return process(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
