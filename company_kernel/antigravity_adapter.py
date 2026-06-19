from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from .db_paths import ensure_db_parent, resolve_db_path as resolve_kernel_db_path
from .employee_comms import communication_protocol
from .proc_util import run_with_group_timeout


ROOT = Path(os.environ.get("OPENCLAW_COMPANY_KERNEL_ROOT", str(Path(__file__).resolve().parents[1]))).resolve()
APP_PATH = Path("/Applications/Antigravity.app")
AGY_COMMAND = "agy"


def resolve_db_path() -> Path:
    return resolve_kernel_db_path(ROOT)


DB_PATH = resolve_db_path()


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


def direct_paths(agent: str) -> dict[str, Path]:
    base = ROOT / "employees" / agent / "reports" / "direct"
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return {
        "base": base,
        "brief": base / f"antigravity-direct-brief-{stamp}.md",
        "report": base / f"antigravity-direct-report-{stamp}.json",
    }


def direct_reply_text(message: str) -> str:
    if "只回复" in message:
        marker = message.split("只回复", 1)[1].lstrip("：: ").strip().split()[0]
        if marker:
            return marker
    if "frontend" in message.lower() or "前端" in message or "页面" in message:
        return "ANTIGRAVITY_FRONTEND_BRIEF_READY"
    return "ANTIGRAVITY_DIRECT_ACK"


def expected_direct_token(message: str) -> str:
    for marker in ("只回复：", "只回复:", "只回复"):
        if marker in message:
            token = message.split(marker, 1)[1].lstrip("：: ").strip().split()[0]
            return token.strip()
    return ""


def is_lightweight_direct_message(message: str) -> bool:
    text = message.strip()
    if not text:
        return True
    if expected_direct_token(text):
        return True
    if len(text) <= 180 and any(marker in text for marker in ("DIRECT_OK", "VERIFY_ROUND", "CLI_OK", "ACK", "OK", "在岗")):
        return True
    execution_markers = ("执行", "修复", "实现", "测试", "验证", "git", "github", "repo", "项目", "文件", "代码", "开发", "push", "commit", "重构", "frontend", "dashboard")
    lower = text.lower()
    return not any(marker in lower or marker in text for marker in execution_markers)


def structured_status(reply: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in reply.splitlines():
        match = re.match(r"^\s*(status|current_action|changed_files|verification_run|browser_check|blocker|eta)\s*[:：]\s*(.*)\s*$", line, re.I)
        if match:
            result[match.group(1).lower()] = match.group(2).strip()
    return result


def command_output(args: list[str]) -> tuple[int, str, str]:
    cp = subprocess.run(args, cwd=str(ROOT), text=True, capture_output=True)
    return cp.returncode, cp.stdout.strip(), cp.stderr.strip()


def git_changed_files() -> list[str]:
    code, out, _ = command_output(["git", "status", "--porcelain", "--untracked-files=no"])
    if code != 0:
        return []
    files: list[str] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip()
        if path:
            files.append(path)
    return files


def build_guarded_task_prompt(message: str) -> str:
    return "\n".join(
        [
            "You are the Antigravity Company Kernel frontend employee.",
            "Use only the current repository and current user request. Ignore stale Hermes/Codex/permission tasks from previous context.",
            "If the request is not a frontend/dashboard task, report blocked_context_mismatch.",
            "",
            communication_protocol("antigravity", "antigravity"),
            "",
            "User request:",
            message.strip(),
            "",
            "Required final output. Do not echo this template. Fill concrete values:",
            "status: working|done|blocked",
            "current_action: <what you actually did>",
            "changed_files: <comma-separated files actually changed, or ->",
            "verification_run: <commands actually run and result, or ->",
            "browser_check: <browser checks actually run, or ->",
            "blocker: <specific blocker, or ->",
            "eta: <remaining time, or ->",
        ]
    )


def build_managed_task_prompt(task: sqlite3.Row) -> str:
    base = build_guarded_task_prompt(
        "\n".join(
            [
                f"task_id: {task['id']}",
                f"title: {task['title']}",
                "description:",
                task["description"] or "",
            ]
        )
    )
    return "\n".join(
        [
            base,
            "",
            "Managed attempt evidence contract:",
            "- verification_run must be a concrete command you actually ran in THIS repo plus its result; never '-' for status done.",
            "- For a read-only review, verification_run is the inspection you actually ran — e.g. grep/search for the symbols, or viewing the exact files and line ranges named in the task. A lint/build command is fine ONLY if it already exists in this repo.",
            "- NEVER create, modify, or compile files just to satisfy verification. This is review-only: changed_files must be '-'. If you cannot verify without writing files, return status: blocked.",
            "- Do not claim done unless current_action and verification_run are both concrete.",
        ]
    )


def validate_agy_reply(*, message: str, reply: str, before_files: list[str], after_files: list[str]) -> dict:
    token = expected_direct_token(message)
    if is_lightweight_direct_message(message):
        if token:
            ok = reply.strip() == token
            return {
                "ok": ok,
                "mode": "lightweight",
                "status": "working" if ok else "blocked",
                "activation_eligible": False,
                "blocker": "" if ok else f"expected exact token {token!r}, got {reply.strip()!r}",
                "fields": {},
                "changed_files": [],
            }
        return {
            "ok": bool(reply.strip()),
            "mode": "lightweight",
            "status": "working" if reply.strip() else "blocked",
            "activation_eligible": False,
            "blocker": "" if reply.strip() else "empty Antigravity reply",
            "fields": {},
            "changed_files": [],
        }
    fields = structured_status(reply)
    changed_files = sorted(set(after_files) - set(before_files))
    stale_markers = ("HERMES_LOCAL_VERIFY_OK", "approval-route-task-hermes", "dangerously-skip-permissions", "protected path", "permission grants")
    stale_hit = next((marker for marker in stale_markers if marker.lower() in reply.lower()), "")
    planning_only = bool(
        re.search(r"(?im)^\s*I will\b", reply)
        or re.search(r"(?im)^\s*我将|^\s*我会", reply)
        or "timed out waiting for response" in reply.lower()
    )
    required = ("status", "current_action", "changed_files", "verification_run", "blocker")
    missing = [field for field in required if not fields.get(field)]
    status = fields.get("status", "").lower()
    placeholder_changed = fields.get("changed_files", "").strip() in {"", "-", "n/a", "none"}
    placeholder_verification = fields.get("verification_run", "").strip() in {"", "-", "n/a", "none"}
    done_without_evidence = status == "done" and placeholder_verification
    has_execution_evidence = not placeholder_verification and fields.get("current_action", "").strip() not in {"", "-"}
    ok = not stale_hit and not missing and status in {"working", "done", "blocked"} and not done_without_evidence and has_execution_evidence
    blocker = ""
    if stale_hit:
        blocker = f"blocked_context_mismatch: reply contains stale marker {stale_hit}"
    elif planning_only:
        blocker = "planning_only_or_timeout: Antigravity returned plan text or timed out before structured evidence"
    elif missing:
        blocker = "missing structured fields: " + ", ".join(missing)
    elif status not in {"working", "done", "blocked"}:
        blocker = f"invalid status: {fields.get('status', '')}"
    elif done_without_evidence:
        blocker = "status done requires concrete verification_run"
    elif not has_execution_evidence:
        blocker = "execution validation requires concrete current_action and verification_run"
    elif status == "blocked":
        blocker = fields.get("blocker", "blocked")
    return {
        "ok": ok and status != "blocked",
        "mode": "execution",
        "status": status if status in {"working", "done", "blocked"} else "blocked",
        "activation_eligible": bool(ok and status != "blocked" and has_execution_evidence),
        "blocker": blocker,
        "fields": fields,
        "changed_files": changed_files,
    }


# Per-task timeout for managed reviews: honor a `超时:`/`timeout:` directive, with a generous floor
# so multi-screen reviews don't get cut off (the old 120s default only suited the attendance probe).
TIMEOUT_DIRECTIVE = re.compile(
    r"(?:超时|超時|timeout)\s*[:：]\s*(\d+)\s*(min|mins|minute|minutes|分钟|分|m|s|sec|secs|second|seconds|秒)?",
    re.IGNORECASE,
)
MANAGED_ATTEMPT_MIN_TIMEOUT_SECONDS = 1800   # 30 min floor for real reviews
MAX_TASK_TIMEOUT_SECONDS = 3600              # 1 h cap


def resolve_managed_timeout(task: sqlite3.Row, base_default: int) -> int:
    """Effective `agy --print` timeout for a managed review: at least the floor, overridable by a
    `超时:`/`timeout:` directive in the task description (capped). Larger than the CLI default so
    multi-screen reviews finish instead of timing out at 120s."""
    default = max(int(base_default or 0), MANAGED_ATTEMPT_MIN_TIMEOUT_SECONDS)
    match = TIMEOUT_DIRECTIVE.search(str(task["description"] or ""))
    if not match:
        return default
    try:
        value = int(match.group(1))
    except (TypeError, ValueError):
        return default
    unit = (match.group(2) or "").lower()
    minute_units = {"min", "mins", "minute", "minutes", "分钟", "分", "m"}
    seconds = value * 60 if unit in minute_units else value
    if seconds <= 0:
        return default
    return min(seconds, MAX_TASK_TIMEOUT_SECONDS)


WORKSPACE_DIRECTIVE = re.compile(
    r"^[^\n]*?(?:工作区|工作目录|仓库路径|仓库绝对路径|workspace|repo[ _]?path)[^\n]*?[:：]\s*[\[【(\"']?\s*([^\s\"'\]】)，,]+)",
    re.IGNORECASE | re.MULTILINE,
)


def resolve_managed_workspace(task: sqlite3.Row, emp: sqlite3.Row) -> Path:
    """Where agy should run a review. A per-task `工作区:` directive wins, else the employee's
    configured workspace, else the kernel root. Must be an existing ABSOLUTE dir — the system uses
    it automatically so the dispatcher never has to paste absolute paths (the recurring path bug)."""
    candidates = []
    m = WORKSPACE_DIRECTIVE.search(str(task["description"] or ""))
    if m:
        candidates.append(m.group(1).strip())
    if emp is not None:
        try:
            candidates.append(str(emp["workspace"] or "").strip())
        except (KeyError, IndexError, TypeError):
            pass
    for raw in candidates:
        if not raw:
            continue
        p = Path(raw).expanduser()
        if p.is_absolute() and p.is_dir():
            return p.resolve()
    return ROOT


# ---------------------------------------------------------------- agy persistent memory (opt-in)
# agy persists each conversation as a <UUID>.db file and `--print` can resume one with
# `--conversation <UUID>`. Like codex, agy can't name a conversation on creation, so memory mode
# snapshots the conversations dir around the run and captures the one new UUID, then resumes it next
# time. Ambiguous diff (0 or >1 new) → stay stateless (never resume the wrong conversation). No
# memory_key → unchanged behavior.
def _agy_conversation_dirs() -> list[Path]:
    home = Path(os.environ.get("ANTIGRAVITY_HOME", str(Path.home() / ".gemini")))
    return [home / "antigravity-cli" / "conversations", home / "antigravity" / "conversations"]


def _agy_snapshot() -> set[str]:
    out: set[str] = set()
    for d in _agy_conversation_dirs():
        if d.exists():
            out |= {str(p) for p in d.glob("*.db")}
    return out


def _agy_conversation_id(path: str) -> str:
    name = Path(path).stem  # the .db filename IS the conversation UUID
    return name if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", name) else ""


def _agy_memory_marker(agent: str, memory_key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", memory_key)[:120]
    return ROOT / "employees" / agent / "agy-sessions" / f"{safe}.json"


def agy_memory_session(agent: str, memory_key: str) -> str:
    if not memory_key:
        return ""
    try:
        return str(json.loads(_agy_memory_marker(agent, memory_key).read_text(encoding="utf-8")).get("conversation_id", "") or "")
    except (OSError, json.JSONDecodeError):
        return ""


def store_agy_memory_session(agent: str, memory_key: str, conversation_id: str) -> None:
    p = _agy_memory_marker(agent, memory_key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"conversation_id": conversation_id, "memory_key": memory_key, "created_at": now()}, ensure_ascii=False), encoding="utf-8")


def run_agy_print(message: str, timeout: int, workspace: Path | None = None, memory_key: str = "", agent: str = "antigravity") -> tuple[int, str, str]:
    command = shutil.which(AGY_COMMAND)
    if not command:
        return 127, "", "agy command not found"
    cwd = (workspace or ROOT)
    resume_id = agy_memory_session(agent, memory_key) if memory_key else ""
    capture = bool(memory_key) and not resume_id
    before = _agy_snapshot() if capture else set()
    cmd = [command]
    if Path(cwd).resolve() != ROOT:
        cmd += ["--add-dir", str(cwd)]   # ensure the review repo is in agy's workspace
    cmd += ["--print", message, "--print-timeout", f"{timeout}s"]
    if resume_id:
        cmd += ["--conversation", resume_id]  # resume this memory-key's conversation
    # run_with_group_timeout kills the WHOLE `agy --print` tree on timeout (a plain timeout would
    # orphan its child processes and block the daemon tick — freezing every employee's heartbeat).
    cp = run_with_group_timeout(cmd, timeout=timeout + 10, cwd=str(cwd), text=True, capture_output=True)
    if capture and cp.returncode == 0:
        new = [_agy_conversation_id(p) for p in (_agy_snapshot() - before)]
        new = [u for u in new if u]
        if len(new) == 1:  # exactly one new conversation → ours; ambiguous → stay stateless
            store_agy_memory_session(agent, memory_key, new[0])
    return cp.returncode, cp.stdout.strip(), cp.stderr.strip()


def attendance_probe(args: argparse.Namespace) -> int:
    emp = employee(args.agent)
    if not emp:
        emit({"ok": False, "error": "unknown employee", "agent": args.agent})
        return 1
    if emp["runtime"] != "antigravity":
        emit({"ok": False, "error": "employee runtime is not antigravity", "agent": args.agent, "runtime": emp["runtime"]})
        return 1
    expected = f"{args.agent} 在岗"
    run_companyctl(["heartbeat", "--agent", args.agent])
    code, reply, err = run_agy_print(f"只回复 {expected}", args.timeout)
    ok = code == 0 and reply == expected
    emit(
        {
            "ok": ok,
            "processed": 0,
            "agent": args.agent,
            "attendance_probe": True,
            "reply": reply,
            "expected": expected,
            "agy_exit_code": code,
            "agy_stderr": err[-1000:],
            "blocker": "" if ok else (err or f"expected exact reply {expected!r}, got {reply!r}"),
        }
    )
    return 0 if ok else 1


def write_direct_brief(agent: str, source: str, session_key: str, message: str, reply: str, validation: dict | None = None) -> dict[str, Path]:
    validation = validation or {}
    artifact = direct_paths(agent)
    artifact["brief"].write_text(
        "\n".join(
            [
                "# Antigravity Direct GUI Brief",
                "",
                "Antigravity is a GUI/IDE employee. This direct request has been recorded and acknowledged, but implementation is not complete until Antigravity or a human GUI worker returns evidence.",
                "",
                "## Request",
                "",
                f"- source_agent: `{source}`",
                f"- target_agent: `{agent}`",
                f"- session_key: `{session_key}`",
                f"- created_at: `{now()}`",
                "",
                "## Message",
                "",
                message,
                "",
                "## Required return",
                "",
                "- Review every dashboard page, not only docs.",
                "- Return implemented changes on a branch or a blocker with missing GUI/runtime capability.",
                "- Reply at least once to the sender; record-only inbox files are not enough.",
            ]
        ),
        encoding="utf-8",
    )
    artifact["report"].write_text(
        json.dumps(
            {
                "ok": True,
                "state": "brief_ready",
                "agent": agent,
                "source": source,
                "session_key": session_key,
                "reply": reply,
                "activation_eligible": bool(validation.get("activation_eligible")),
                "validation": validation,
                "brief": str(artifact["brief"]),
                "created_at": now(),
                "next_action": "Antigravity direct request accepted only when validation.ok=true; otherwise sender must receive blocked status.",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return artifact


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


def send_source_status(agent: str, source: str, body: str) -> dict:
    if not source:
        return {"ok": False, "skipped": True, "reason": "missing direct source"}
    message_id = f"msg-{agent}-status-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    code, out, err = run_companyctl(["message", "send", "--from", agent, "--to", source, "--body", body, "--message-id", message_id])
    try:
        payload = json.loads(out or "{}")
    except json.JSONDecodeError:
        payload = {}
    event_id = str(payload.get("event_id") or "")
    if event_id:
        run_companyctl(["scheduler", "skip-event", "--event-id", event_id, "--by", agent, "--reason", "direct status notification handled by adapter"])
    return {"ok": code == 0, "exit_code": code, "message_id": message_id, "payload": payload, "stderr": err[-1000:]}


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


def managed_attempt_report_path(agent: str, task_id: str) -> Path:
    artifact = paths(agent, task_id)
    return artifact["base"] / f"antigravity-managed-attempt-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"


def promote_managed_report_to_task_evidence(agent: str, task_id: str, report_path: Path, summary: str) -> tuple[int, dict, str]:
    context_code, context_payload, context_err = run_companyctl_json(["task", "context", "--task-id", task_id, "--employee", agent])
    if context_code != 0:
        return context_code, context_payload, context_err
    workspace_path = Path(context_payload["context"]["workspace"]["path"]).resolve()
    final_dir = workspace_path / "final"
    final_dir.mkdir(parents=True, exist_ok=True)
    final_path = final_dir / report_path.name
    if report_path.resolve() != final_path.resolve():
        shutil.copy2(report_path, final_path)
    register_code, register_payload, register_err = run_companyctl_json(
        [
            "task",
            "artifact",
            "register",
            "--task-id",
            task_id,
            "--employee",
            agent,
            "--path",
            str(final_path),
            "--type",
            "json",
            "--name",
            final_path.name,
            "--stage",
            "final",
            "--summary",
            summary or "Antigravity managed attempt evidence",
            "--final",
        ]
    )
    if register_code != 0:
        return register_code, register_payload, register_err
    artifact_id = register_payload["artifact"]["artifact_id"]
    promote_code, promote_payload, promote_err = run_companyctl_json(
        [
            "task",
            "evidence",
            "promote",
            "--artifact-id",
            artifact_id,
            "--employee",
            agent,
            "--summary",
            summary or "Antigravity managed attempt evidence",
        ]
    )
    if promote_code != 0:
        return promote_code, promote_payload, promote_err
    return 0, {**promote_payload, "final_path": str(final_path), "artifact": register_payload.get("artifact")}, ""


def process_managed_attempt(args: argparse.Namespace, emp: sqlite3.Row) -> int:
    task = task_by_id(args.task_id) if args.task_id else next_task(args.agent)
    if not task:
        run_companyctl(["heartbeat", "--agent", args.agent])
        emit({"ok": True, "processed": 0, "agent": args.agent, "managed_attempt": True, "note": "no submitted Antigravity task"})
        return 0
    if task["target_agent"] != args.agent and task["claimed_by"] != args.agent:
        emit({"ok": False, "error": "task not assigned to agent", "task_id": task["id"], "agent": args.agent})
        return 1
    artifact = paths(args.agent, task["id"])
    prompt = build_managed_task_prompt(task)
    eff_timeout = resolve_managed_timeout(task, args.timeout)
    eff_workspace = resolve_managed_workspace(task, emp)
    artifact["brief"].write_text(build_brief(task) + "\n## Managed Prompt\n\n" + prompt + "\n", encoding="utf-8")
    run_code, run_payload, run_err = run_companyctl_json(
        [
            "task",
            "run",
            "--task-id",
            task["id"],
            "--agent",
            args.agent,
            "--by",
            args.by,
            "--adapter-type",
            "antigravity",
            "--session-key",
            f"agent:{args.agent}:{args.by}",
            "--max-runtime-seconds",
            str(args.max_runtime_seconds),
            "--heartbeat-interval-seconds",
            str(args.heartbeat_interval_seconds),
            "--progress-interval-seconds",
            str(args.progress_interval_seconds),
            "--stale-after-seconds",
            str(args.stale_after_seconds),
            # pid stamp → watchdog can fast-reap an orphaned attempt if this adapter process dies
            "--pid",
            str(os.getpid()),
        ]
    )
    if run_code != 0:
        emit({"ok": False, "processed": 0, "agent": args.agent, "managed_attempt": True, "task_id": task["id"], "error": "task run failed", "companyctl": run_payload, "stderr": run_err[-1000:]})
        return run_code
    attempt = run_payload["attempt"]
    attempt_id = attempt["attempt_id"]
    trace_id = str(attempt.get("trace_id", ""))
    session_id = f"antigravity-session-{args.agent}-{task['id']}"
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
            "antigravity",
            "--runtime-type",
            "cli",
            "--session-key",
            f"agent:{args.agent}:{args.by}",
            "--task-id",
            task["id"],
            "--attempt-id",
            attempt_id,
        ]
    )
    if session_code != 0:
        emit({"ok": False, "processed": 0, "agent": args.agent, "managed_attempt": True, "task_id": task["id"], "error": "runtime session start failed", "attempt": attempt, "companyctl": session_payload, "stderr": session_err[-1000:]})
        return session_code
    tool_call_id = f"antigravity-tool-{args.agent}-{task['id']}"
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
            "antigravity.print",
            "--tool-type",
            "cli",
            "--input-summary",
            f"agy --print timeout={eff_timeout}s cwd={eff_workspace}",
            "--risk-level",
            "medium",
        ]
    )
    run_companyctl_json(["task", "progress", "--task-id", task["id"], "--agent", args.agent, "--attempt-id", attempt_id, "--state", "acknowledged", "--message", "Antigravity managed attempt acknowledged", "--progress", "5"])
    before_files = git_changed_files()
    started_monotonic = time.monotonic()
    agy_code, agy_reply, agy_err = run_agy_print(prompt, eff_timeout, eff_workspace)
    runtime_seconds = max(0, int(round(time.monotonic() - started_monotonic)))
    after_files = git_changed_files()
    validation = validate_agy_reply(message=prompt, reply=agy_reply, before_files=before_files, after_files=after_files)
    report_path = managed_attempt_report_path(args.agent, task["id"])
    report = {
        "ok": agy_code == 0 and bool(validation["ok"]),
        "managed_attempt": True,
        "agent": args.agent,
        "task_id": task["id"],
        "attempt_id": attempt_id,
        "reply": agy_reply,
        "validation": validation,
        "agy_exit_code": agy_code,
        "agy_stderr": agy_err[-1000:],
        "created_at": now(),
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if report["ok"]:
        run_companyctl_json(["task", "progress", "--task-id", task["id"], "--agent", args.agent, "--attempt-id", attempt_id, "--state", "in_progress", "--message", validation["fields"].get("current_action", "Antigravity structured execution in progress"), "--progress", "80", "--payload", json.dumps({"validation": validation, "report": str(report_path)}, ensure_ascii=False)])
        summary = validation["fields"].get("current_action") or "Antigravity managed attempt completed"
        evidence_code, evidence_payload, evidence_err = promote_managed_report_to_task_evidence(args.agent, task["id"], report_path, summary)
        evidence_path = str(evidence_payload.get("evidence", {}).get("path_or_url") or evidence_payload.get("final_path") or report_path)
        done_code, done_payload, done_err = run_companyctl_json(["task", "done", "--agent", args.agent, "--task-id", task["id"], "--summary", summary, "--evidence", evidence_path])
        _, tool_payload, _ = run_companyctl_json(["tool-call", "finish", "--tool-call-id", tool_call_id, "--status", "success", "--output-summary", summary[:500], "--error", ""])
        _, budget_payload, _ = run_companyctl_json(["budget", "record", "--task-id", task["id"], "--attempt-id", attempt_id, "--employee", args.agent, "--cost-type", "antigravity_runtime", "--amount", "0", "--currency", "USD", "--model-name", "", "--provider", "antigravity", "--runtime-seconds", str(runtime_seconds), "--summary", f"agy --print exit_code={agy_code}"])
        finish_code, finish_payload, finish_err = run_companyctl_json(["task", "attempt", "finish", "--attempt-id", attempt_id, "--status", "success"])
        _, stopped_session, _ = run_companyctl_json(["runtime", "session", "stop", "--session-id", session_id, "--status", "stopped", "--error", ""])
        run_companyctl(["heartbeat", "--agent", args.agent])
        shown_code, shown_payload, _shown_err = run_companyctl_json(["task", "show", "--task-id", task["id"]])
        ok = evidence_code == 0 and done_code == 0 and finish_code == 0
        emit({"ok": ok, "processed": 1, "managed_attempt": True, "task_id": task["id"], "agent": emp["id"], "attempt": finish_payload.get("attempt", attempt), "runtime_session": stopped_session.get("session", session_payload.get("session", {})), "tool_call": tool_payload.get("tool_call", {}), "budget_event": budget_payload.get("budget_event", {}), "task": shown_payload.get("task", {}) if shown_code == 0 else {}, "evidence": evidence_path, "report": str(report_path), "validation": validation, "companyctl_evidence": evidence_payload, "companyctl_evidence_stderr": evidence_err[-1000:], "companyctl_done": done_payload, "companyctl_done_stderr": done_err[-1000:], "companyctl_finish_stderr": finish_err[-1000:]})
        return 0 if ok else 1
    blocker = validation["blocker"] or agy_err or "Antigravity managed attempt failed validation"
    run_companyctl_json(["task", "progress", "--task-id", task["id"], "--agent", args.agent, "--attempt-id", attempt_id, "--state", "blocked_on_input_or_dependency", "--message", blocker, "--progress", "50", "--payload", json.dumps({"validation": validation, "report": str(report_path)}, ensure_ascii=False)])
    block_code, block_payload, block_err = run_companyctl_json(["task", "block", "--agent", args.agent, "--task-id", task["id"], "--blocker", blocker])
    _, tool_payload, _ = run_companyctl_json(["tool-call", "finish", "--tool-call-id", tool_call_id, "--status", "failed", "--output-summary", blocker[:500], "--error", blocker[:500]])
    _, budget_payload, _ = run_companyctl_json(["budget", "record", "--task-id", task["id"], "--attempt-id", attempt_id, "--employee", args.agent, "--cost-type", "antigravity_runtime", "--amount", "0", "--currency", "USD", "--model-name", "", "--provider", "antigravity", "--runtime-seconds", str(runtime_seconds), "--summary", f"agy --print exit_code={agy_code}"])
    finish_code, finish_payload, finish_err = run_companyctl_json(["task", "attempt", "finish", "--attempt-id", attempt_id, "--status", "failed", "--error", blocker])
    _, stopped_session, _ = run_companyctl_json(["runtime", "session", "stop", "--session-id", session_id, "--status", "failed", "--error", blocker[:500]])
    run_companyctl(["heartbeat", "--agent", args.agent])
    emit({"ok": False, "processed": 1 if block_code == 0 else 0, "managed_attempt": True, "task_id": task["id"], "agent": emp["id"], "attempt": finish_payload.get("attempt", attempt), "runtime_session": stopped_session.get("session", session_payload.get("session", {})), "tool_call": tool_payload.get("tool_call", {}), "budget_event": budget_payload.get("budget_event", {}), "status": "blocked", "blocker": blocker, "report": str(report_path), "validation": validation, "companyctl_block": block_payload, "companyctl_block_stderr": block_err[-1000:], "companyctl_finish_stderr": finish_err[-1000:]})
    return 1 if finish_code == 0 else finish_code


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
    if args.attendance_probe:
        return attendance_probe(args)
    emp = employee(args.agent)
    if not emp:
        emit({"ok": False, "error": "unknown employee", "agent": args.agent})
        return 1
    if emp["runtime"] != "antigravity":
        emit({"ok": False, "error": "employee runtime is not antigravity", "agent": args.agent, "runtime": emp["runtime"]})
        return 1
    if args.managed_attempt:
        return process_managed_attempt(args, emp)
    if args.direct_message:
        run_companyctl(["heartbeat", "--agent", args.agent])
        before_files = git_changed_files()
        agy_prompt = args.direct_message if is_lightweight_direct_message(args.direct_message) else build_guarded_task_prompt(args.direct_message)
        agy_code, agy_reply, agy_err = run_agy_print(agy_prompt, args.timeout, memory_key=getattr(args, "memory_session", "") or "", agent=args.agent)
        after_files = git_changed_files()
        reply = agy_reply or direct_reply_text(args.direct_message)
        validation = validate_agy_reply(message=args.direct_message, reply=reply, before_files=before_files, after_files=after_files)
        artifact = write_direct_brief(args.agent, args.direct_source, args.direct_session_key, args.direct_message, reply, validation)
        agy_ok = agy_code == 0 and bool(agy_reply) and bool(validation["ok"])
        status_body = status_reply_text(
            status=validation["status"] if agy_code == 0 and agy_reply else "blocked",
            current_action=validation["fields"].get("current_action", "Antigravity direct reply validated" if agy_ok else "Antigravity direct reply rejected by execution guard"),
            changed_files=validation["fields"].get("changed_files") or (", ".join(validation["changed_files"]) if validation["changed_files"] else str(artifact["brief"])),
            verification_run=validation["fields"].get("verification_run") or str(artifact["report"]),
            blocker="-" if agy_ok else (validation["blocker"] or agy_err or "Antigravity reply failed validation"),
            eta="-",
        )
        status_delivery = send_source_status(args.agent, args.direct_source, status_body)
        emit(
            {
                "ok": agy_ok,
                "processed": 0,
                "agent": args.agent,
                "direct_message": True,
                "source": args.direct_source,
                "session_key": args.direct_session_key,
                "reply": reply,
                "activation_eligible": bool(validation["activation_eligible"]) and agy_ok,
                "brief": str(artifact["brief"]),
                "report": str(artifact["report"]),
                "status_delivery": status_delivery,
                "agy_exit_code": agy_code,
                "agy_stderr": agy_err[-1000:],
                "validation": validation,
                "blocked_execution": not agy_ok,
                "blocker": "" if agy_ok else (validation["blocker"] or "Antigravity direct reply did not meet execution evidence contract."),
            }
        )
        return 0 if agy_ok else 1
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
    parser.add_argument("--direct-message", default="", help="record and acknowledge a direct GUI request")
    parser.add_argument("--direct-source", default="", help="source employee for direct GUI request")
    parser.add_argument("--direct-session-key", default="", help="session key for direct GUI request")
    parser.add_argument("--timeout", type=int, default=120, help="timeout seconds for direct CLI replies")
    parser.add_argument("--memory-session", default="", help="stable memory key: resume ONE agy conversation across turns so it remembers (used by conversations)")
    parser.add_argument("--attendance-probe", action="store_true", help="send exact Antigravity CLI attendance probe")
    parser.add_argument("--managed-attempt", action="store_true", help="run a submitted task through Kernel-managed attempt/progress/evidence")
    parser.add_argument("--by", default="hermes", help="supervisor employee for --managed-attempt")
    parser.add_argument("--max-runtime-seconds", type=int, default=36000)
    parser.add_argument("--heartbeat-interval-seconds", type=int, default=60)
    parser.add_argument("--progress-interval-seconds", type=int, default=300)
    parser.add_argument("--stale-after-seconds", type=int, default=900)
    result = parser.add_mutually_exclusive_group()
    result.add_argument("--complete", action="store_true", help="return completed GUI result to Company Kernel")
    result.add_argument("--block", action="store_true", help="return blocked GUI result to Company Kernel")
    parser.add_argument("--execute", action="store_true", help="open Antigravity.app; without this only writes brief and report")
    return parser


def main(argv: list[str] | None = None) -> int:
    return process(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
