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

from . import companyctl
from .adapter_result import compact_output, execution_detail
from .db_paths import ensure_db_parent, resolve_db_path
from .employee_comms import communication_protocol
from .sandboxing import wrap_command
from .verifiers import parse_verifier, verify_result


ROOT = Path(os.environ.get("OPENCLAW_COMPANY_KERNEL_ROOT", Path(__file__).resolve().parents[1])).resolve()
DB_PATH = resolve_db_path(ROOT)

# launchd agents get a minimal PATH; add common tool locations so the codex CLI resolves.
_PATH_EXTRAS = ["/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local/bin"), str(Path.home() / "bin"), str(Path.home() / ".npm-global/bin")]
os.environ["PATH"] = ":".join([p for p in os.environ.get("PATH", "").split(":") if p] + [p for p in _PATH_EXTRAS if p not in os.environ.get("PATH", "")])
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


def task_cost_so_far(task_id: str) -> float:
    """Sum of recorded budget cost for a task across all prior attempts."""
    conn = connect()
    try:
        row = conn.execute("SELECT COALESCE(SUM(amount), 0) AS c FROM budget_events WHERE task_id = ?", (task_id,)).fetchone()
        return float(row["c"] if row and row["c"] is not None else 0)
    finally:
        conn.close()


def task_tokens_so_far(task_id: str) -> int:
    """Total tokens (input+output) recorded for a task across all prior attempts."""
    conn = connect()
    try:
        row = conn.execute("SELECT COALESCE(SUM(token_input + token_output), 0) AS t FROM budget_events WHERE task_id = ?", (task_id,)).fetchone()
        return int(row["t"] if row and row["t"] is not None else 0)
    finally:
        conn.close()


def task_attempts_so_far(task_id: str) -> int:
    """Number of execution attempts already made for a task (retry counter)."""
    conn = connect()
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM execution_attempts WHERE task_id = ?", (task_id,)).fetchone()
        return int(row["n"] if row and row["n"] is not None else 0)
    finally:
        conn.close()


_TOKEN_KEY_PAIRS = (
    ("input_tokens", "output_tokens"),
    ("prompt_tokens", "completion_tokens"),
    ("token_input", "token_output"),
    ("inputTokens", "outputTokens"),
)


def _scan_token_dicts(node, found: list[tuple[int, int]]) -> None:
    """Recursively collect (input, output) token pairs from any nested dict."""
    if isinstance(node, dict):
        for in_key, out_key in _TOKEN_KEY_PAIRS:
            if in_key in node or out_key in node:
                try:
                    ti = int(node.get(in_key, 0) or 0)
                    to = int(node.get(out_key, 0) or 0)
                except (TypeError, ValueError):
                    ti, to = 0, 0
                if ti or to:
                    found.append((ti, to))
        for value in node.values():
            _scan_token_dicts(value, found)
    elif isinstance(node, list):
        for value in node:
            _scan_token_dicts(value, found)


def parse_token_usage(events: Path) -> tuple[int, int]:
    """Extract real token usage from a codex `exec --json` event stream.

    codex emits cumulative token-count events under varying shapes
    (`token_count`, nested `total_token_usage`, OpenAI-style `usage`), so we
    scan every JSON line for any recognized input/output token pair and keep
    the maximum seen — cumulative counts mean the largest is the run total.
    Returns (0, 0) when no usage is present (older codex, non-JSON output)."""
    if not events or not events.exists():
        return 0, 0
    max_in = max_out = 0
    try:
        text = events.read_text(encoding="utf-8")
    except OSError:
        return 0, 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] not in "{[":
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        found: list[tuple[int, int]] = []
        _scan_token_dicts(obj, found)
        for ti, to in found:
            max_in = max(max_in, ti)
            max_out = max(max_out, to)
    return max_in, max_out


def run_cost(token_input: int, token_output: int, runtime_seconds: int) -> float:
    """Cost of a single run: token-based when usage is captured, else runtime
    fallback. Shares companyctl.estimate_task_cost so cost-gate, economics, and
    ledger all agree on one formula."""
    rates = (companyctl.load_pricing_config().get("cost_rates") or {})
    ev = {"amount": 0, "token_input": token_input, "token_output": token_output, "runtime_seconds": runtime_seconds}
    return round(companyctl.estimate_task_cost(ev, rates), 6)


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


# Match a workspace/repo directive on any line: a workspace keyword, a colon, then an absolute
# path. Forgiving on purpose — dispatchers write it many ways (`工作区: /p`, `【工作区/仓库绝对路径】：/p`,
# `工作目录: /p`, `仓库路径：/p`, `workspace: /p`). A prose path without a workspace keyword+colon is
# NOT matched, and resolve_task_workspace still validates the dir exists, so a wrong path blocks
# with a clear message rather than silently running in /tmp.
WORKSPACE_DIRECTIVE = re.compile(
    r"^[^\n]*?(?:工作区|工作目录|仓库路径|仓库绝对路径|workspace|repo[ _]?path)[^\n]*?[:：]\s*[\[【(\"']?\s*([^\s\"'\]】)，,]+)",
    re.IGNORECASE | re.MULTILINE,
)
VERDICT_RE = re.compile(r"^\s*STATUS\s*[:：]\s*(completed|done|blocked)\b\s*[-—–:：]?\s*(.*)$", re.IGNORECASE | re.MULTILINE)


def resolve_task_workspace(task: sqlite3.Row, default: Path) -> tuple[Path, str]:
    """Honor a per-task `工作区: /abs/path` (or `workspace: /abs/path`) directive in the description.

    Returns (workspace, error). A non-empty error means the directive is invalid and the
    task must be blocked instead of silently running in the wrong directory.
    """
    text = str(task["description"] or "")
    match = WORKSPACE_DIRECTIVE.search(text)
    if not match:
        return default, ""
    raw = match.group(1)
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        return default, f"task workspace directive must be an absolute path, got: {raw}"
    candidate = candidate.resolve()
    if candidate == ROOT or ROOT in candidate.parents:
        return default, (
            "task workspace directive points inside Company Kernel; "
            "kernel self-modification must go through the RFC/approval flow, not a worker task"
        )
    if not candidate.is_dir():
        return default, f"task workspace directive does not exist or is not a directory: {candidate}"
    return candidate, ""


# Per-task timeout override: heavy tasks (big ETL, multi-step infra) need more than the default
# 30-min cap. Honor a `超时: <n>` / `timeout: <n>` line — `<n>min`/`<n>分钟` → minutes, else seconds.
# Capped so one task can't hang the synchronous daemon indefinitely.
TIMEOUT_DIRECTIVE = re.compile(
    r"^[^\n]*?(?:超时时间|超时|timeout)[^\n]*?[:：]\s*(\d+)\s*(min|mins|minute|minutes|分钟|分|m|s|sec|secs|second|seconds|秒)?",
    re.IGNORECASE | re.MULTILINE,
)
MAX_TASK_TIMEOUT_SECONDS = 3600  # 60 min hard cap


def resolve_task_timeout(task: sqlite3.Row, default_seconds: int) -> int:
    """Return the effective codex timeout for this task, honoring a `超时:`/`timeout:` directive
    (capped at MAX_TASK_TIMEOUT_SECONDS). Falls back to default_seconds when absent/unparseable."""
    match = TIMEOUT_DIRECTIVE.search(str(task["description"] or ""))
    if not match:
        return default_seconds
    try:
        value = int(match.group(1))
    except (TypeError, ValueError):
        return default_seconds
    unit = (match.group(2) or "").lower()
    minute_units = {"min", "mins", "minute", "minutes", "分钟", "分", "m"}
    seconds = value * 60 if unit in minute_units else value
    if seconds <= 0:
        return default_seconds
    return min(seconds, MAX_TASK_TIMEOUT_SECONDS)


def parse_verdict(output: Path) -> tuple[str, str]:
    """Read codex's final message and extract its explicit self-verdict.

    Returns (verdict, reason) where verdict is one of: completed / blocked / missing.
    Exit code 0 only proves the codex PROCESS ended normally — the task outcome must
    come from an explicit `STATUS:` line, otherwise `done` would silently hide blockers.
    """
    try:
        text = output.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "missing", "no codex output captured"
    matches = list(VERDICT_RE.finditer(text))
    if not matches:
        return "missing", ""
    last = matches[-1]
    verdict = last.group(1).lower()
    if verdict == "done":
        verdict = "completed"
    return verdict, (last.group(2) or "").strip()


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
            communication_protocol(task["target_agent"], "codex"),
            "",
            "## Required final verdict (MANDATORY)",
            "",
            "The LAST line of your final message MUST be exactly one of:",
            "",
            "- `STATUS: completed` — only when every acceptance criterion actually passed",
            "- `STATUS: blocked - <one concrete reason>` — when anything failed, is missing, or could not be verified",
            "",
            "Without this line the kernel treats the task as NOT done and blocks it for human review.",
            "Never output `STATUS: completed` when verification did not actually pass.",
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
        shutil.which("codex") or "codex",  # absolute binary — immune to shell function/PATH wrappers
        "exec",
        "--ignore-rules",
        "--ephemeral",
        # workspace trust is decided by the kernel (resolve_task_workspace); codex's own
        # git-repo check would otherwise refuse legitimate non-git task workspaces.
        "--skip-git-repo-check",
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


TIMEOUT_EXIT_CODE = 124


def run_codex(task_card: Path, workspace: Path, output: Path, events: Path, sandbox: str, model: str, isolation: str, sandbox_profile: str, timeout_seconds: int = 1800) -> tuple[int, str]:
    cmd = wrap_command(build_codex_command(workspace, output, sandbox, model), runtime="codex", workspace=workspace, isolation=isolation, profile_name=sandbox_profile)
    try:
        with task_card.open("r", encoding="utf-8") as stdin, events.open("w", encoding="utf-8") as event_out:
            cp = subprocess.run(cmd, stdin=stdin, stdout=event_out, stderr=subprocess.STDOUT, text=True, timeout=timeout_seconds or None)
        return cp.returncode, " ".join(cmd)
    except subprocess.TimeoutExpired:
        note = f"codex exec killed after exceeding timeout of {timeout_seconds} seconds at {now()}"
        with events.open("a", encoding="utf-8") as event_out:
            event_out.write(json.dumps({"type": "adapter.timeout", "detail": note}, ensure_ascii=False) + "\n")
        if not output.exists() or not output.read_text(encoding="utf-8").strip():
            output.write_text(note + "\n", encoding="utf-8")
        return TIMEOUT_EXIT_CODE, " ".join(cmd)


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


def handle_converse(args: argparse.Namespace, emp) -> int:
    """Answer-only mode for meetings/discussions: run codex read-only to produce a reply to
    the prompt, with no task card, no progress reports, no execution loop. This is what lets
    codex genuinely participate in a conversation instead of running its execution flow."""
    run_companyctl(["heartbeat", "--agent", args.agent])
    if not shutil.which("codex"):
        emit({"ok": False, "error": "codex command not found", "agent": args.agent, "converse": True, "reply": ""})
        return 1
    workspace = Path(args.workspace or emp["workspace"] or DEFAULT_WORKSPACE).expanduser().resolve()
    art = direct_paths(args.agent)
    art["task_card"].write_text(args.converse_message, encoding="utf-8")
    timeout = args.timeout or 180
    run_code, _cmd = run_codex(
        art["task_card"], workspace, art["last_message"], art["events"],
        "read-only", args.model, args.isolation, args.sandbox_profile, timeout_seconds=timeout,
    )
    reply = (compact_output(art["last_message"], max_chars=1600) or "").strip()
    ok = run_code == 0 and bool(reply)
    emit({
        "ok": ok,
        "agent": args.agent,
        "converse": True,
        "source": args.direct_source,
        "session_key": args.direct_session_key,
        "reply": reply,
        "payloads": [{"text": reply}] if reply else [],
        "exit_code": run_code,
    })
    return 0 if ok else 1


def process(args: argparse.Namespace) -> int:
    emp = employee(args.agent)
    if not emp:
        emit({"ok": False, "error": "unknown employee", "agent": args.agent})
        return 1
    if emp["runtime"] != "codex":
        emit({"ok": False, "error": "employee runtime is not codex", "agent": args.agent, "runtime": emp["runtime"]})
        return 1
    if args.converse_message:
        return handle_converse(args, emp)
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
        run_code, cmd = run_codex(artifact["task_card"], workspace, artifact["last_message"], artifact["events"], "workspace-write", args.model, args.isolation, args.sandbox_profile, timeout_seconds=args.timeout_seconds)
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
    default_workspace = Path(args.workspace or emp["workspace"] or DEFAULT_WORKSPACE).expanduser()
    workspace, workspace_error = resolve_task_workspace(task, default_workspace)
    task_timeout = resolve_task_timeout(task, args.timeout_seconds)
    artifact = paths(args.agent, task["id"])
    artifact["task_card"].write_text(build_task_card(task, workspace, args.sandbox), encoding="utf-8")
    claim_code, claim_out, claim_err = run_companyctl(["task", "claim", "--agent", args.agent, "--task-id", task["id"]])
    if claim_code != 0:
        emit({"ok": False, "error": "claim failed", "stdout": claim_out, "stderr": claim_err})
        return claim_code
    if workspace_error:
        blocker = f"invalid task workspace directive: {workspace_error}"
        write_report(artifact["report"], task, executed=False, status="blocked", detail=blocker, task_card=artifact["task_card"], output=artifact["last_message"])
        done_code, done_out, done_err = run_companyctl(["task", "block", "--agent", args.agent, "--task-id", task["id"], "--blocker", blocker])
        run_companyctl(["heartbeat", "--agent", args.agent])
        emit({"ok": done_code == 0, "processed": 1, "executed": False, "verdict": "workspace_invalid", "task_id": task["id"], "blocker": blocker, "report": str(artifact["report"]), "companyctl_stdout": done_out, "companyctl_stderr": done_err})
        return done_code
    if not args.execute:
        detail = "Codex adapter dry-run generated task card. Use --execute to run codex exec."
        write_report(artifact["report"], task, executed=False, status="completed", detail=detail, task_card=artifact["task_card"], output=artifact["last_message"])
        done_code, done_out, done_err = run_companyctl(["task", "done", "--agent", args.agent, "--task-id", task["id"], "--summary", detail, "--evidence", str(artifact["report"])])
        run_companyctl(["heartbeat", "--agent", args.agent])
        emit({"ok": done_code == 0, "processed": 1, "executed": False, "task_id": task["id"], "task_card": str(artifact["task_card"]), "report": str(artifact["report"]), "companyctl_stdout": done_out, "companyctl_stderr": done_err})
        return done_code
    # Resource gate: if this task already burned past any per-task cap (cumulative cost,
    # tokens, or retry count), stop and route to a human quote instead of pouring more
    # tokens into the same wall. This is what keeps outcome-based pricing from going
    # loss-making on hard tasks and caps token variance.
    gate_reason = ""
    gate_fields: dict = {}
    if args.max_cost and args.max_cost > 0:
        spent = task_cost_so_far(task["id"])
        if spent >= args.max_cost:
            gate_reason = f"成本上限到达：已花费 ${spent:.4f} ≥ 上限 ${args.max_cost:.2f}"
            gate_fields = {"verdict": "cost_capped", "spent": spent, "max_cost": args.max_cost}
    if not gate_reason and args.max_tokens and args.max_tokens > 0:
        used = task_tokens_so_far(task["id"])
        if used >= args.max_tokens:
            gate_reason = f"Token 上限到达：已用 {used} ≥ 上限 {args.max_tokens} tokens"
            gate_fields = {"verdict": "token_capped", "tokens_used": used, "max_tokens": args.max_tokens}
    if not gate_reason and args.max_retries and args.max_retries > 0:
        attempts = task_attempts_so_far(task["id"])
        if attempts >= args.max_retries:
            gate_reason = f"重试上限到达：已尝试 {attempts} 次 ≥ 上限 {args.max_retries}"
            gate_fields = {"verdict": "retry_capped", "attempts": attempts, "max_retries": args.max_retries}
    if gate_reason:
        blocker = gate_reason + "，转人工报价（needs quote），不再自动执行以免亏损。"
        write_report(artifact["report"], task, executed=False, status="blocked", detail=blocker, task_card=artifact["task_card"], output=artifact["last_message"])
        done_code, done_out, done_err = run_companyctl(["task", "block", "--agent", args.agent, "--task-id", task["id"], "--blocker", blocker])
        run_companyctl(["heartbeat", "--agent", args.agent])
        emit({"ok": done_code == 0, "processed": 1, "executed": False, "needs_quote": True, "task_id": task["id"], "blocker": blocker, "report": str(artifact["report"]), **gate_fields})
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
    code, cmd = run_codex(artifact["task_card"], workspace, artifact["last_message"], artifact["events"], args.sandbox, args.model, args.isolation, args.sandbox_profile, timeout_seconds=task_timeout)
    runtime_seconds = max(0, int(round(time.monotonic() - started_monotonic)))
    if code == 0:
        verdict, verdict_reason = parse_verdict(artifact["last_message"])
    else:
        verdict, verdict_reason = "crashed", f"codex exec exit_code={code}"
    # Pluggable verifier: when the agent claims completed, an external verifier (declared in
    # the task card) has the final say. Agent self-report alone never marks a task done.
    verifier_kind = verifier_arg = verifier_result = verifier_detail = ""
    if verdict == "completed":
        vkind, varg = parse_verifier(task["description"] or "")
        verifier_kind, verifier_arg = vkind, varg
        if vkind != "status":
            output_text = artifact["last_message"].read_text(encoding="utf-8", errors="replace") if artifact["last_message"].exists() else ""
            vresult, vdetail = verify_result(vkind, varg, workspace=workspace, output_text=output_text, agent_verdict=verdict)
            verifier_result, verifier_detail = vresult, vdetail
            if vresult == "pass":
                verdict_reason = f"verifier[{vkind}] pass: {vdetail}"
            elif vresult == "needs_human":
                verdict, verdict_reason = "needs_human", f"verifier[{vkind}]: {vdetail}"
            else:  # fail / error
                verdict, verdict_reason = "verifier_failed", f"verifier[{vkind}] {vresult}: {vdetail}"
        else:
            verifier_result, verifier_detail = "pass", "agent STATUS: completed (no external verifier declared)"
    if verdict == "completed":
        detail = execution_detail(cmd, artifact["last_message"], success=True)
        write_report(artifact["report"], task, executed=True, status="completed", detail=detail, task_card=artifact["task_card"], output=artifact["last_message"])
        evidence_report = copy_report_to_task_evidence(task["id"], artifact["report"])
        done_code, done_out, done_err = run_companyctl(["task", "done", "--agent", args.agent, "--task-id", task["id"], "--summary", detail, "--evidence", str(evidence_report)])
        tool_status = "success"
        attempt_status = "success"
        session_status = "stopped"
    else:
        if verdict == "blocked":
            header = f"codex verdict: blocked — {verdict_reason or 'no reason given'}"
        elif verdict == "missing":
            header = "codex output has no `STATUS:` verdict line; exit code 0 alone does not prove completion — blocked for human review"
        elif verdict == "verifier_failed":
            header = f"agent claimed completed but the result verifier rejected it — {verdict_reason}"
        elif verdict == "needs_human":
            header = f"result requires human verification — {verdict_reason}"
        else:
            header = f"codex execution failed: {verdict_reason}"
        detail = header + "\n\n" + execution_detail(cmd, artifact["last_message"], exit_code=code, success=False)
        write_report(artifact["report"], task, executed=True, status="blocked", detail=detail, task_card=artifact["task_card"], output=artifact["last_message"])
        done_code, done_out, done_err = run_companyctl(["task", "block", "--agent", args.agent, "--task-id", task["id"], "--blocker", detail])
        tool_status = "failed"
        attempt_status = "failed"
        session_status = "failed"
    _, tool_payload, _ = run_companyctl_json(["tool-call", "finish", "--tool-call-id", tool_call_id, "--status", tool_status, "--output-summary", detail[:500], "--error", "" if code == 0 else detail[:500]])
    token_input, token_output = parse_token_usage(artifact["events"])
    amount = run_cost(token_input, token_output, runtime_seconds)
    cost_basis = "tokens" if (token_input or token_output) else "runtime"
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
            str(amount),
            "--currency",
            "USD",
            "--token-input",
            str(token_input),
            "--token-output",
            str(token_output),
            "--model-name",
            args.model or "",
            "--provider",
            "openai" if args.model else "",
            "--runtime-seconds",
            str(runtime_seconds),
            "--summary",
            f"codex exec exit_code={code} cost_basis={cost_basis} in={token_input} out={token_output}",
        ]
    )
    if verifier_kind:
        run_companyctl([
            "verifier", "record",
            "--task-id", task["id"],
            "--attempt-id", attempt_id,
            "--employee", args.agent,
            "--kind", verifier_kind,
            "--arg", verifier_arg or "",
            "--result", verifier_result or "",
            "--agent-verdict", "completed",
            "--detail", (verifier_detail or "")[:500],
        ])
    _, finish_payload, finish_err = run_companyctl_json(["task", "attempt", "finish", "--attempt-id", attempt_id, "--status", attempt_status, "--error", "" if code == 0 else detail[:500]])
    _, stopped_session, _ = run_companyctl_json(["runtime", "session", "stop", "--session-id", session_id, "--status", session_status, "--error", "" if code == 0 else detail[:500]])
    run_companyctl(["heartbeat", "--agent", args.agent])
    # ok=False only for infrastructure failures (crash/timeout) so the daemon retry policy
    # re-runs those; deterministic verdict blocks must NOT auto-retry into the same wall.
    infra_failure = code != 0
    emit({"ok": done_code == 0 and not infra_failure, "processed": 1, "executed": True, "verdict": verdict, "verdict_reason": verdict_reason, "task_id": task["id"], "codex_exit_code": code, "attempt": finish_payload.get("attempt", attempt), "runtime_session": stopped_session.get("session", session_payload.get("session", {})), "tool_call": tool_payload.get("tool_call", {}), "budget_event": budget_payload.get("budget_event", {}), "task_card": str(artifact["task_card"]), "last_message": str(artifact["last_message"]), "events": str(artifact["events"]), "report": str(artifact["report"]), "companyctl_stdout": done_out, "companyctl_stderr": done_err, "companyctl_finish_stderr": finish_err[-1000:]})
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
    parser.add_argument("--timeout-seconds", type=int, default=1800, help="kill codex exec after this many seconds and block the task with evidence (0 disables)")
    parser.add_argument("--max-cost", type=float, default=0.0, help="per-task cumulative cost cap (USD); when reached, block and route to human quote instead of running again (0 disables)")
    parser.add_argument("--max-tokens", type=int, default=0, help="per-task cumulative token cap (input+output); when reached, block and route to human quote (0 disables)")
    parser.add_argument("--max-retries", type=int, default=0, help="per-task attempt cap; when prior attempts reach this, block and route to human quote (0 disables)")
    parser.add_argument("--attendance-probe", action="store_true", help="reply to attendance without claiming or processing tasks")
    parser.add_argument("--direct-message", default="", help="reply to a direct reachability message without claiming tasks")
    parser.add_argument("--direct-source", default="", help="source employee for direct reachability messages")
    parser.add_argument("--direct-session-key", default="", help="session key used by the company direct message resolver")
    parser.add_argument("--converse-message", default="", help="answer-only mode: run codex read-only to produce a discussion/meeting reply, no task execution")
    parser.add_argument("--timeout", type=int, default=120, help="timeout seconds for direct replies")
    return parser


def main(argv: list[str] | None = None) -> int:
    return process(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
