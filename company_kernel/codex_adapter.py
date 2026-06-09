from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import companyctl
from .adapter_result import compact_output, execution_detail
from .db_paths import ensure_db_parent, resolve_db_path
from .sandboxing import wrap_command


ROOT = Path(os.environ.get("OPENCLAW_COMPANY_KERNEL_ROOT", Path(__file__).resolve().parents[1])).resolve()
DB_PATH = resolve_db_path(ROOT)
DEFAULT_WORKSPACE = Path(
    os.environ.get("OPENCLAW_CODEX_WORKSPACE", str(Path.home() / ".codex"))
).expanduser().resolve()


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


def next_codex_task(agent: str) -> sqlite3.Row | None:
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
        "task_card": base / "codex-task-card.md",
        "last_message": base / "codex-last-message.md",
        "events": base / "codex-events.jsonl",
        "report": base / "codex-adapter-report.md",
    }


def direct_report_path(agent: str) -> Path:
    base = ROOT / "employees" / agent / "reports" / "direct"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"progress_acknowledged_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"


def direct_paths(agent: str) -> dict[str, Path]:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = ROOT / "employees" / agent / "reports" / "direct" / stamp
    base.mkdir(parents=True, exist_ok=True)
    return {
        "base": base,
        "task_card": base / "codex-direct-task-card.md",
        "last_message": base / "codex-direct-last-message.md",
        "events": base / "codex-direct-events.jsonl",
        "report": base / "codex-direct-adapter-report.md",
    }


def write_direct_report(agent: str, source: str, session_key: str, message: str, reply: str, *, state: str = "acknowledged", workspace_report: Path | None = None) -> Path:
    report = direct_report_path(agent)
    report.write_text(
        json.dumps(
            {
                "ok": True,
                "state": state,
                "agent": agent,
                "source": source,
                "session_key": session_key,
                "message": message,
                "reply": reply,
                "created_at": now(),
                "workspace_report": str(workspace_report or ""),
                "next_action": "reply delivered to Company Kernel sender inbox",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return report


def workspace_progress_dir(workspace: Path) -> Path:
    path = workspace / "reports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_workspace_progress(workspace: Path, *, state: str, project: str, action: str, checking: str = "", risks: str = "", blocked_on: str = "", tried: str = "", needs_action_from: str = "", task_id: str = "") -> Path:
    out_dir = workspace_progress_dir(workspace)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    resolved_task_id = task_id.strip() or f"direct-{stamp}"
    safe_task_id = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in resolved_task_id).strip("_") or "direct"
    path = out_dir / f"progress_{state}_{safe_task_id}_{stamp}.json"
    payload = {
        "ok": True,
        "task_id": resolved_task_id,
        "report": {
            "state": state,
            "project": project,
            "targets": str(workspace),
            "action": action,
            "checking": checking,
            "risks": risks,
            "blocked_on": blocked_on,
            "tried": tried,
            "needs_action_from": needs_action_from,
            "created_at": now(),
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def status_reply_text(*, status: str, current_action: str, changed_files: str = "-", verification_run: str = "-", blocker: str = "-", eta: str = "-") -> str:
    return "\n".join(
        [
            f"status: {status}",
            f"current_action: {current_action}",
            f"changed_files: {changed_files}",
            f"verification_run: {verification_run}",
            f"blocker: {blocker}",
            f"eta: {eta}",
        ]
    )


def send_source_progress(agent: str, source: str, body: str) -> dict:
    if not source:
        return {"ok": False, "skipped": True, "reason": "missing direct source"}
    message_id = f"msg-{agent}-progress-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    code, out, err = run_companyctl(["message", "send", "--from", agent, "--to", source, "--body", body, "--message-id", message_id])
    try:
        payload = json.loads(out or "{}")
    except json.JSONDecodeError:
        payload = {}
    event_id = str(payload.get("event_id") or "")
    if event_id:
        run_companyctl(["scheduler", "skip-event", "--event-id", event_id, "--by", agent, "--reason", "direct progress notification handled by adapter"])
    return {"ok": code == 0, "exit_code": code, "message_id": message_id, "payload": payload, "stderr": err[-1000:]}


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
    target = evidence_dir / f"codex-adapter-report-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
    target.write_bytes(report.read_bytes())
    return target


def build_codex_command(workspace: Path, output: Path, sandbox: str, model: str) -> list[str]:
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
    return cmd


def run_codex(task_card: Path, workspace: Path, output: Path, events: Path, sandbox: str, model: str, isolation: str, sandbox_profile: str) -> tuple[int, str]:
    cmd = wrap_command(build_codex_command(workspace, output, sandbox, model), runtime="codex", workspace=workspace, isolation=isolation, profile_name=sandbox_profile)
    with task_card.open("r", encoding="utf-8") as stdin, events.open("w", encoding="utf-8") as event_out:
        cp = subprocess.run(cmd, stdin=stdin, stdout=event_out, stderr=subprocess.STDOUT, text=True)
    return cp.returncode, " ".join(cmd)


def direct_reply_text(agent: str, message: str) -> str:
    text = message.strip()
    for marker in ("只回复：", "只回复:", "只回复"):
        if marker in text:
            reply = text.split(marker, 1)[1].strip()
            return reply or f"{agent} 收到"
    return f"{agent} 收到：{text}" if text else f"{agent} 收到"


def is_lightweight_direct_message(message: str) -> bool:
    text = message.strip()
    if not text:
        return True
    if len(text) <= 160 and any(marker in text for marker in ("只回复", "在岗", "DIRECT_OK", "PROGRESS_OK", "ACK", "OK")):
        return True
    lower = text.lower()
    execution_markers = ("执行", "修复", "实现", "测试", "验证", "git", "github", "repo", "项目", "文件", "代码", "开发", "push", "commit")
    return not any(marker in lower or marker in text for marker in execution_markers)


def build_direct_task_card(agent: str, source: str, session_key: str, message: str, workspace: Path) -> str:
    project = workspace.name
    return "\n".join(
        [
            "# Codex Direct Execution Task",
            "",
            "## Objective",
            "",
            message.strip() or "Handle the direct request and report status.",
            "",
            "## Canonical repo / workspace",
            "",
            f"- Path: `{workspace}`",
            f"- Project: `{project}`",
            f"- Source: `{source}`",
            f"- Target: `{agent}`",
            f"- Session key: `{session_key}`",
            "",
            "## Mandatory communication loop",
            "",
            "This is not a receipt-only direct message. You must do real work or report a real blocker.",
            "Before completion, write structured progress inside the repo-local `reports/` directory.",
            "",
            "Required commands:",
            "",
            "```bash",
            f"python3 scripts/progress_report.py --state acknowledged --project {project} --action \"received direct task from {source}\" --checking \"planning\" --out-dir reports/",
            f"python3 scripts/progress_report.py --state in_progress --project {project} --action \"working on direct task\" --checking \"implementation and verification\" --out-dir reports/",
            "```",
            "",
            "If blocked:",
            "",
            "```bash",
            f"python3 scripts/progress_report.py --state blocked --project {project} --action \"blocked direct task\" --checking \"last attempted check\" --blocked_on \"<specific blocker>\" --out-dir reports/",
            "```",
            "",
            "If completed:",
            "",
            "```bash",
            f"python3 scripts/progress_report.py --state completed --project {project} --action \"completed direct task\" --checking \"<verification commands and results>\" --out-dir reports/",
            "```",
            "",
            "## Required final output",
            "",
            "Return exactly these fields with concrete values, not placeholders:",
            "",
            "- status: working / blocked / done",
            "- current_action",
            "- changed_files",
            "- verification_run",
            "- blocker",
            "- eta",
            "",
            "Do not echo this template. If you cannot perform the task, set status=blocked and explain the blocker.",
            "",
        ]
    )


def direct_execution_reply(state: str, output: Path, *, workspace_report: Path, exit_code: int = 0) -> str:
    final = compact_output(output, max_chars=900)
    status = "done" if state == "completed" else "blocked"
    if not final:
        final = "No final Codex output captured."
    return "\n".join(
        [
            f"status: {status}",
            f"current_action: Codex direct execution {'completed' if state == 'completed' else 'blocked'}",
            f"changed_files: see workspace git diff / Codex final output",
            f"verification_run: see Codex final output and progress report {workspace_report}",
            f"blocker: {'-' if state == 'completed' else f'codex exit_code={exit_code}'}",
            "eta: -",
            "",
            final,
        ]
    )


def process(args: argparse.Namespace) -> int:
    emp = employee(args.agent)
    if not emp:
        emit({"ok": False, "error": "unknown employee", "agent": args.agent})
        return 1
    if emp["runtime"] != "codex":
        emit({"ok": False, "error": "employee runtime is not codex", "agent": args.agent, "runtime": emp["runtime"]})
        return 1
    if args.direct_message:
        code, hb_out, hb_err = run_companyctl(["heartbeat", "--agent", args.agent])
        workspace = Path(args.workspace or emp["workspace"] or DEFAULT_WORKSPACE).expanduser().resolve()
        if is_lightweight_direct_message(args.direct_message):
            reply = direct_reply_text(args.agent, args.direct_message)
            report = write_direct_report(args.agent, args.direct_source, args.direct_session_key, args.direct_message, reply)
            emit({
                "ok": code == 0,
                "processed": 0,
                "agent": args.agent,
                "direct_message": True,
                "source": args.direct_source,
                "session_key": args.direct_session_key,
                "reply": reply,
                "progress_report": str(report),
                "direct_mode": "receipt",
                "companyctl_stdout": hb_out,
                "companyctl_stderr": hb_err,
            })
            return code
        if not shutil.which("codex"):
            workspace_report = write_workspace_progress(
                workspace,
                state="blocked",
                project=workspace.name,
                action="blocked direct task before execution",
                checking="codex command availability",
                blocked_on="codex command not found",
                needs_action_from="operator",
            )
            reply = direct_execution_reply("blocked", workspace_report, workspace_report=workspace_report, exit_code=127)
            report = write_direct_report(args.agent, args.direct_source, args.direct_session_key, args.direct_message, reply, state="blocked", workspace_report=workspace_report)
            emit({
                "ok": False,
                "processed": 0,
                "agent": args.agent,
                "direct_message": True,
                "source": args.direct_source,
                "session_key": args.direct_session_key,
                "reply": reply,
                "progress_report": str(report),
                "workspace_progress_report": str(workspace_report),
                "direct_mode": "execution",
                "error": "codex command not found",
                "companyctl_stdout": hb_out,
                "companyctl_stderr": hb_err,
            })
            return 1
        artifact = direct_paths(args.agent)
        artifact["task_card"].write_text(build_direct_task_card(args.agent, args.direct_source, args.direct_session_key, args.direct_message, workspace), encoding="utf-8")
        acknowledged = write_workspace_progress(
            workspace,
            state="acknowledged",
            project=workspace.name,
            action=f"received direct task from {args.direct_source or 'unknown'}",
            checking=f"task card {artifact['task_card']}",
            task_id=args.direct_session_key or "",
        )
        in_progress = write_workspace_progress(
            workspace,
            state="in_progress",
            project=workspace.name,
            action="started direct Codex execution",
            checking=f"running codex exec for {args.direct_source or 'unknown'}",
            task_id=args.direct_session_key or "",
        )
        working_reply = status_reply_text(
            status="working",
            current_action="Codex adapter started direct execution",
            changed_files="-",
            verification_run=f"in progress report {in_progress}",
            blocker="-",
            eta="running",
        )
        working_delivery = send_source_progress(args.agent, args.direct_source, working_reply)
        run_code, cmd = run_codex(artifact["task_card"], workspace, artifact["last_message"], artifact["events"], "workspace-write", args.model, args.isolation, args.sandbox_profile)
        if run_code == 0:
            workspace_report = write_workspace_progress(
                workspace,
                state="completed",
                project=workspace.name,
                action="completed direct Codex execution",
                checking=compact_output(artifact["last_message"], max_chars=600) or "codex exec exit_code=0",
                task_id=args.direct_session_key or "",
            )
            state = "completed"
        else:
            workspace_report = write_workspace_progress(
                workspace,
                state="blocked",
                project=workspace.name,
                action="blocked direct Codex execution",
                checking=compact_output(artifact["last_message"], max_chars=600),
                blocked_on=f"codex exec exit_code={run_code}",
                tried=cmd,
                needs_action_from="operator",
                task_id=args.direct_session_key or "",
            )
            state = "blocked"
        detail = execution_detail(cmd, artifact["last_message"], exit_code=run_code, success=run_code == 0)
        write_report(artifact["report"], {"id": f"direct-{datetime.now().strftime('%Y%m%d-%H%M%S')}"}, executed=True, status=state, detail=detail, task_card=artifact["task_card"], output=artifact["last_message"])
        reply = direct_execution_reply(state, artifact["last_message"], workspace_report=workspace_report, exit_code=run_code)
        report = write_direct_report(args.agent, args.direct_source, args.direct_session_key, args.direct_message, reply, state=state, workspace_report=workspace_report)
        emit({
            "ok": code == 0 and run_code == 0,
            "processed": 1,
            "agent": args.agent,
            "direct_message": True,
            "source": args.direct_source,
            "session_key": args.direct_session_key,
            "reply": reply,
            "direct_mode": "execution",
            "codex_exit_code": run_code,
            "task_card": str(artifact["task_card"]),
            "last_message": str(artifact["last_message"]),
            "events": str(artifact["events"]),
            "adapter_report": str(artifact["report"]),
            "progress_report": str(report),
            "workspace_acknowledged_report": str(acknowledged),
            "workspace_in_progress_report": str(in_progress),
            "workspace_progress_report": str(workspace_report),
            "working_delivery": working_delivery,
            "companyctl_stdout": hb_out,
            "companyctl_stderr": hb_err,
        })
        return code if code != 0 else run_code
    if args.attendance_probe:
        code, hb_out, hb_err = run_companyctl(["heartbeat", "--agent", args.agent])
        emit({"ok": code == 0, "processed": 0, "agent": args.agent, "attendance_probe": True, "reply": f"{args.agent} 在岗", "companyctl_stdout": hb_out, "companyctl_stderr": hb_err})
        return code
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
    run_code, run_payload, run_err = run_companyctl_json(["task", "run", "--task-id", task["id"], "--agent", args.agent, "--by", args.agent, "--adapter-type", "codex", "--session-key", f"codex:{task['id']}"])
    if run_code != 0:
        emit({"ok": False, "error": "attempt start failed", "task_id": task["id"], "companyctl": run_payload, "stderr": run_err[-1000:]})
        return run_code
    attempt = run_payload["attempt"]
    attempt_id = attempt["attempt_id"]
    trace_id = str(attempt.get("trace_id", ""))
    session_id = f"codex-session-{args.agent}-{task['id']}"
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
            "codex",
            "--runtime-type",
            "cli",
            "--session-key",
            f"codex:{task['id']}",
            "--task-id",
            task["id"],
            "--attempt-id",
            attempt_id,
        ]
    )
    if session_code != 0:
        emit({"ok": False, "error": "runtime session start failed", "task_id": task["id"], "attempt": attempt, "companyctl": session_payload, "stderr": session_err[-1000:]})
        return session_code
    tool_call_id = f"codex-tool-{args.agent}-{task['id']}"
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
            "codex.exec",
            "--tool-type",
            "cli",
            "--input-summary",
            f"codex exec model={args.model or '-'} sandbox={args.sandbox}",
            "--risk-level",
            "medium" if args.sandbox != "read-only" else "low",
        ]
    )
    run_companyctl(["task", "progress", "--task-id", task["id"], "--agent", args.agent, "--attempt-id", attempt_id, "--state", "acknowledged", "--message", "Codex adapter acknowledged managed execution", "--progress", "5"])
    started_monotonic = time.monotonic()
    code, cmd = run_codex(artifact["task_card"], workspace, artifact["last_message"], artifact["events"], args.sandbox, args.model, args.isolation, args.sandbox_profile)
    runtime_seconds = max(0, int(round(time.monotonic() - started_monotonic)))
    if code == 0:
        detail = execution_detail(cmd, artifact["last_message"], success=True)
        write_report(artifact["report"], task, executed=True, status="completed", detail=detail, task_card=artifact["task_card"], output=artifact["last_message"])
        evidence_report = copy_report_to_task_evidence(task["id"], artifact["report"])
        done_code, done_out, done_err = run_companyctl(["task", "done", "--agent", args.agent, "--task-id", task["id"], "--summary", detail, "--evidence", str(evidence_report)])
        tool_status = "success"
        attempt_status = "success"
        session_status = "stopped"
    else:
        detail = execution_detail(cmd, artifact["last_message"], exit_code=code, success=False)
        write_report(artifact["report"], task, executed=True, status="blocked", detail=detail, task_card=artifact["task_card"], output=artifact["last_message"])
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
            "codex_runtime",
            "--amount",
            "0",
            "--currency",
            "USD",
            "--model-name",
            args.model or "",
            "--provider",
            "openai" if args.model else "",
            "--runtime-seconds",
            str(runtime_seconds),
            "--summary",
            f"codex exec exit_code={code}",
        ]
    )
    _, finish_payload, finish_err = run_companyctl_json(["task", "attempt", "finish", "--attempt-id", attempt_id, "--status", attempt_status, "--error", "" if code == 0 else detail[:500]])
    _, stopped_session, _ = run_companyctl_json(["runtime", "session", "stop", "--session-id", session_id, "--status", session_status, "--error", "" if code == 0 else detail[:500]])
    run_companyctl(["heartbeat", "--agent", args.agent])
    emit({"ok": done_code == 0 and code == 0, "processed": 1, "executed": True, "task_id": task["id"], "codex_exit_code": code, "attempt": finish_payload.get("attempt", attempt), "runtime_session": stopped_session.get("session", session_payload.get("session", {})), "tool_call": tool_payload.get("tool_call", {}), "budget_event": budget_payload.get("budget_event", {}), "task_card": str(artifact["task_card"]), "last_message": str(artifact["last_message"]), "events": str(artifact["events"]), "report": str(artifact["report"]), "companyctl_stdout": done_out, "companyctl_stderr": done_err, "companyctl_finish_stderr": finish_err[-1000:]})
    return done_code if done_code != 0 else code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Company Kernel Codex adapter")
    parser.add_argument("--agent", default="codex")
    parser.add_argument("--workspace", default="")
    parser.add_argument("--sandbox", default="read-only", choices=["read-only", "workspace-write", "danger-full-access"])
    parser.add_argument("--model", default="")
    parser.add_argument("--isolation", default="none", choices=["none", "docker", "firejail"], help="wrap codex exec in a container/sandbox command")
    parser.add_argument("--sandbox-profile", default="default", help="sandbox profile name from config/sandbox_profiles.json")
    parser.add_argument("--execute", action="store_true", help="actually run codex exec; without this only writes task card and report")
    parser.add_argument("--attendance-probe", action="store_true", help="reply to attendance without claiming or processing tasks")
    parser.add_argument("--direct-message", default="", help="reply to a direct reachability message without claiming tasks")
    parser.add_argument("--direct-source", default="", help="source employee for direct reachability messages")
    parser.add_argument("--direct-session-key", default="", help="session key used by the company direct message resolver")
    parser.add_argument("--timeout", type=int, default=120, help="timeout seconds for direct replies")
    return parser


def main(argv: list[str] | None = None) -> int:
    return process(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
