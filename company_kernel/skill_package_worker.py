from __future__ import annotations

import argparse
import json
import os
import shlex
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from company_kernel import companyctl


def root() -> Path:
    return Path(os.environ.get("OPENCLAW_COMPANY_KERNEL_ROOT", Path(__file__).resolve().parents[1])).resolve()


def db_path() -> Path:
    return root() / "company.sqlite"


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def connect() -> sqlite3.Connection:
    project_root = root()
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.executescript((project_root / "company_kernel" / "schema.sql").read_text(encoding="utf-8"))
    conn.commit()
    return conn


def run_companyctl(args: list[str]) -> tuple[int, dict, str]:
    project_root = root()
    cp = subprocess.run([str(project_root / "bin" / "companyctl"), *args], cwd=str(project_root), text=True, capture_output=True)
    try:
        payload = json.loads(cp.stdout or "{}")
    except json.JSONDecodeError:
        payload = {"ok": False, "raw": cp.stdout}
    return cp.returncode, payload, cp.stderr


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
            """
            SELECT * FROM tasks
            WHERE target_agent = ?
              AND (
                status = 'submitted'
                OR (status = 'claimed' AND claimed_by = ?)
              )
            ORDER BY updated_at DESC, created_at ASC
            LIMIT 1
            """,
            (agent, agent),
        ).fetchone()
    finally:
        conn.close()


def active_attempt(task_id: str, agent: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            """
            SELECT * FROM execution_attempts
            WHERE task_id = ?
              AND employee_id = ?
              AND status IN ('starting', 'running', 'correcting', 'cancelling')
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (task_id, agent),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def load_manifest(path: Path) -> dict:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    required = ["id", "name", "version", "input_schema", "output_schema", "runtime", "permissions", "pricing", "acceptance"]
    missing = [key for key in required if key not in manifest]
    if missing:
        raise ValueError("missing skill manifest fields: " + ", ".join(missing))
    runtime = manifest.get("runtime") if isinstance(manifest.get("runtime"), dict) else {}
    if runtime.get("type") != "local-script":
        raise ValueError("only local-script skill runtime is supported in phase 1")
    if not runtime.get("command"):
        raise ValueError("skill runtime.command is required")
    acceptance = manifest.get("acceptance") if isinstance(manifest.get("acceptance"), dict) else {}
    if not acceptance.get("final_artifact"):
        raise ValueError("skill acceptance.final_artifact is required")
    return manifest


def task_workspace(task_id: str) -> dict:
    code, payload, err = run_companyctl(["task", "workspace", "--task-id", task_id])
    if code != 0:
        raise RuntimeError(err or json.dumps(payload, ensure_ascii=False))
    return payload["workspace"]


def run_skill_command(command: str, workspace: Path, package_dir: Path, *, task_id: str, agent: str, manifest: dict, timeout: int) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "OPENCLAW_COMPANY_KERNEL_ROOT": str(root()),
        "TASK_ID": task_id,
        "EMPLOYEE_ID": agent,
        "SKILL_ID": str(manifest["id"]),
        "TASK_WORKSPACE": str(workspace),
        "PACKAGE_DIR": str(package_dir),
    }
    return subprocess.run(command, cwd=str(package_dir), env=env, shell=True, text=True, capture_output=True, timeout=timeout)


def process(args: argparse.Namespace) -> int:
    emp = employee(args.agent)
    if not emp:
        emit({"ok": False, "error": "unknown employee", "agent": args.agent})
        return 1
    if emp["runtime"] != "skill":
        emit({"ok": False, "error": "employee runtime is not skill", "agent": args.agent, "runtime": emp["runtime"]})
        return 2
    try:
        package_path = Path(args.package).expanduser().resolve()
        manifest = load_manifest(package_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        emit({"ok": False, "error": str(exc), "package": args.package})
        return 2
    task = next_task(args.agent)
    if not task:
        run_companyctl(["heartbeat", "--agent", args.agent])
        emit({"ok": True, "processed": 0, "agent": args.agent, "note": "no submitted task"})
        return 0
    code, claimed, err = run_companyctl(["task", "claim", "--agent", args.agent, "--task-id", task["id"]])
    if code != 0:
        emit({"ok": False, "processed": 0, "agent": args.agent, "task_id": task["id"], "error": "claim failed", "companyctl": claimed, "stderr": err[-1000:]})
        return code
    workspace = Path(claimed["task"]["workspace"]["path"])
    existing_attempt = active_attempt(task["id"], args.agent)
    if existing_attempt:
        run_payload = {"attempt": existing_attempt, "reused": True}
    else:
        run_code, run_payload, run_err = run_companyctl(["task", "run", "--task-id", task["id"], "--agent", args.agent, "--by", args.by, "--adapter-type", "skill", "--session-key", f"skill:{manifest['id']}"])
        if run_code != 0:
            emit({"ok": False, "processed": 0, "agent": args.agent, "task_id": task["id"], "error": "attempt start failed", "companyctl": run_payload, "stderr": run_err[-1000:]})
            return run_code
    attempt_id = run_payload["attempt"]["attempt_id"]
    run_companyctl(["task", "progress", "--task-id", task["id"], "--agent", args.agent, "--attempt-id", attempt_id, "--state", "acknowledged", "--message", f"Skill package {manifest['id']} acknowledged", "--progress", "5"])
    try:
        cp = run_skill_command(manifest["runtime"]["command"], workspace, package_path.parent, task_id=task["id"], agent=args.agent, manifest=manifest, timeout=args.timeout)
    except subprocess.TimeoutExpired as exc:
        blocker = f"skill command timeout after {args.timeout}s"
        run_companyctl(["task", "block", "--agent", args.agent, "--task-id", task["id"], "--blocker", blocker])
        run_companyctl(["task", "attempt", "finish", "--attempt-id", attempt_id, "--status", "failed", "--error", blocker])
        emit({"ok": False, "processed": 1, "status": "blocked", "task_id": task["id"], "blocker": blocker, "stdout": exc.stdout or "", "stderr": exc.stderr or ""})
        return 1
    final_rel = manifest["acceptance"]["final_artifact"]
    final_path = (workspace / final_rel).resolve()
    if cp.returncode != 0 or not final_path.exists():
        blocker = f"skill command failed or missing final artifact: {final_rel}"
        run_companyctl(["task", "progress", "--task-id", task["id"], "--agent", args.agent, "--attempt-id", attempt_id, "--state", "blocked_on_input_or_dependency", "--message", blocker, "--progress", "50"])
        run_companyctl(["task", "block", "--agent", args.agent, "--task-id", task["id"], "--blocker", blocker])
        run_companyctl(["task", "attempt", "finish", "--attempt-id", attempt_id, "--status", "failed", "--error", blocker])
        emit({"ok": False, "processed": 1, "status": "blocked", "task_id": task["id"], "blocker": blocker, "exit_code": cp.returncode, "stdout": cp.stdout[-2000:], "stderr": cp.stderr[-2000:]})
        return 1
    summary = f"Skill package {manifest['id']} produced {final_rel}"
    code, artifact_payload, err = run_companyctl(["task", "artifact", "register", "--task-id", task["id"], "--employee", args.agent, "--path", str(final_path), "--type", final_path.suffix.lstrip(".") or "file", "--stage", "final", "--final", "--summary", summary, "--metadata", json.dumps({"skill_package": manifest["id"], "version": manifest["version"]}, ensure_ascii=False)])
    if code != 0:
        emit({"ok": False, "processed": 1, "status": "blocked", "task_id": task["id"], "error": "artifact register failed", "companyctl": artifact_payload, "stderr": err[-1000:]})
        return code
    artifact = artifact_payload["artifact"]
    code, approved, err = run_companyctl(["task", "artifact", "approve", "--artifact-id", artifact["artifact_id"], "--by", args.agent, "--reason", "skill package final artifact accepted"])
    if code != 0:
        emit({"ok": False, "processed": 1, "status": "blocked", "task_id": task["id"], "error": "artifact approve failed", "companyctl": approved, "stderr": err[-1000:]})
        return code
    code, evidence_payload, err = run_companyctl(["task", "evidence", "promote", "--artifact-id", artifact["artifact_id"], "--by", args.agent, "--summary", summary, "--type", artifact["artifact_type"]])
    if code != 0:
        emit({"ok": False, "processed": 1, "status": "blocked", "task_id": task["id"], "error": "evidence promote failed", "companyctl": evidence_payload, "stderr": err[-1000:]})
        return code
    evidence = evidence_payload["evidence"]
    run_companyctl(["task", "progress", "--task-id", task["id"], "--agent", args.agent, "--attempt-id", attempt_id, "--state", "in_progress", "--message", summary, "--progress", "80"])
    code, done, err = run_companyctl(["task", "done", "--agent", args.agent, "--task-id", task["id"], "--summary", summary, "--evidence", evidence["path_or_url"]])
    finish_code, finish, finish_err = run_companyctl(["task", "attempt", "finish", "--attempt-id", attempt_id, "--status", "success" if code == 0 else "failed", "--error", "" if code == 0 else (err or done.get("error", ""))])
    run_companyctl(["heartbeat", "--agent", args.agent])
    emit(
        {
            "ok": code == 0 and finish_code == 0,
            "processed": 1,
            "status": "completed" if code == 0 else "blocked",
            "agent": args.agent,
            "task_id": task["id"],
            "attempt": finish.get("attempt", run_payload["attempt"]),
            "artifact": artifact,
            "evidence": evidence,
            "stdout": cp.stdout[-2000:],
            "stderr": cp.stderr[-2000:],
            "companyctl_done": done,
            "companyctl_done_stderr": err[-1000:],
            "companyctl_finish_stderr": finish_err[-1000:],
        }
    )
    return 0 if code == 0 and finish_code == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Company Kernel Skill Package worker")
    parser.add_argument("--agent", required=True)
    parser.add_argument("--package", required=True, help="path to skill.json")
    parser.add_argument("--by", default="hermes", help="supervisor employee")
    parser.add_argument("--timeout", type=int, default=300, help="single command wait timeout; not task lifetime")
    return parser


def main(argv: list[str] | None = None) -> int:
    return process(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
