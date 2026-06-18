from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import companyctl
from . import project_memory
from .adapter_result import execution_detail
from .db_paths import ensure_db_parent, resolve_db_path
from .employee_comms import communication_protocol


ROOT = Path(os.environ.get("OPENCLAW_COMPANY_KERNEL_ROOT", Path(__file__).resolve().parents[1])).resolve()
DB_PATH = resolve_db_path(ROOT)
DEFAULT_WORKSPACE = Path(os.environ.get("COMPANY_CLAUDE_WORKSPACE", str(Path.home()))).expanduser().resolve()


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
    """v3 file-flow 任务要求 task done 携带「可提升的最终证据」:证据须落在任务工作区
    的 evidence/ 目录下,task done 才会自动 promote 为 final。把员工 reports 目录下的
    报告复制进任务工作区 evidence/(与 codex 适配器一致),让 claude 能真正完成任务。"""
    evidence_dir = task_workspace_path(task_id) / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    target = evidence_dir / f"claude-adapter-report-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
    target.write_bytes(report.read_bytes())
    return target


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
        "prompt": base / "claude-prompt.md",
        "output": base / "claude-output.md",
        "report": base / "claude-adapter-report.md",
    }


def _employee_persona(agent: str) -> str:
    """PER-EMPLOYEE persona: if the target employee's profile.json carries a `persona`
    string, inject it so every task to that employee carries its role lens
    (e.g. gemini = 产品经理+UX 评审)。未设 persona 的员工保持默认。"""
    try:
        prof = json.loads((ROOT / "employees" / agent / "profile.json").read_text(encoding="utf-8"))
        return str(prof.get("persona") or "").strip()
    except Exception:
        return ""


def _project_memory_block(task: sqlite3.Row) -> str:
    """Shared project memory for this task's project, injected so the employee reads it first.
    Never let a memory hiccup break prompt building."""
    try:
        conn = companyctl.connect()
        try:
            return project_memory.digest_block_for_task(conn, dict(task))
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return ""


RUNTIME_TIMEOUT_RC = 124


def output_is_substantive(output: Path) -> bool:
    """Did the runtime actually produce a real result before it got killed? Used to auto-judge a
    timeout: a full answer present = it executed (and only the wrap-down hung); empty/tiny = a real
    hang. We drop the stderr tail we append and the timeout note before measuring."""
    try:
        txt = output.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    body = txt.split("## stderr", 1)[0]
    body = re.sub(r"claude -p killed after exceeding.*", "", body).strip()
    return len(body) >= 200  # a genuine result, not just a one-line error/timeout note


def build_prompt(task: sqlite3.Row) -> str:
    persona = _employee_persona(task["target_agent"])
    persona_block = ["## Your role (persona)", "", persona, ""] if persona else []
    return "\n".join(
        [
            "# Claude Company Kernel Task",
            "",
            "You are Claude acting as a Super AI Company employee.",
            "Follow Company Kernel rules: no secrets, no destructive operations, no external sends, and always provide evidence or blocker.",
            "",
            *persona_block,
            communication_protocol(task["target_agent"], "gemini" if task["target_agent"] == "gemini" else "claude"),
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
            _project_memory_block(task),
            "",
            "## Required output",
            "",
            "Return concise status, summary, evidence, blockers, and next action.",
            "",
        ]
    )


def write_report(path: Path, task: sqlite3.Row, *, executed: bool, status: str, detail: str, prompt: Path, output: Path) -> None:
    path.write_text(
        "\n".join(
            [
                f"# Claude Adapter Report: {task['id']}",
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


def resolve_claude_proxy(agent: str, model: str) -> tuple[dict, str, str]:
    """PER-EMPLOYEE proxy routing: only an employee whose profile.json carries `proxy_base_url` routes
    its `claude -p` through that proxy (e.g. the `gemini` employee → antigravity-claude-proxy → 7 Google
    accounts). The native `claude` employee has no proxy field, so it always uses the local paid Claude.
    Probing first means a down proxy falls back to direct instead of failing every task."""
    try:
        profile = json.loads((ROOT / "employees" / agent / "profile.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        profile = {}
    base = str(profile.get("proxy_base_url") or "").strip()
    if not base:
        return {}, model, "direct"
    token = str(profile.get("proxy_token") or "test")
    try:
        req = urllib.request.Request(base.rstrip("/") + "/v1/models", headers={"x-api-key": token})
        urllib.request.urlopen(req, timeout=4).close()
    except (urllib.error.URLError, OSError, ValueError):
        return {}, model, "direct (proxy unreachable)"
    env = {"ANTHROPIC_BASE_URL": base, "ANTHROPIC_AUTH_TOKEN": token}
    return env, (str(profile.get("proxy_model") or "").strip() or model), f"proxy {base}"


CLAUDE_RUN_TIMEOUT_SECONDS = int(os.environ.get("COMPANY_CLAUDE_TIMEOUT_SECONDS", "1800"))  # 30 min; a hung claude -p must not run forever
CLAUDE_TIMEOUT_CAP = int(os.environ.get("COMPANY_CLAUDE_TIMEOUT_CAP", "3600"))  # 1h ceiling for big tasks
_CLAUDE_TIMEOUT_DIRECTIVE = re.compile(r"(?:超时|超時|timeout)\s*[:：]\s*(\d+)", re.IGNORECASE)
_BIG_TASK_MARKERS = ("全流程", "完整", "重测", "e2e", "端到端", "全端", "巡检", "全系统", "全部页面", "整改", "审核 s", "s01", "s15")


def resolve_claude_timeout(task: "sqlite3.Row") -> int:
    """Big tasks shouldn't die at the 30-min default — that's how done work gets killed at wrap-down.
    Honor an explicit `超时: <秒>` directive (capped at 1h); else auto-bump tasks that look large
    (long description or big-task markers) toward the cap; small tasks keep the short default."""
    desc = str(task["description"] or "")
    m = _CLAUDE_TIMEOUT_DIRECTIVE.search(desc)
    if m:
        return max(120, min(int(m.group(1)), CLAUDE_TIMEOUT_CAP))
    low = desc.lower()
    if len(desc) >= 800 or any(k in low for k in _BIG_TASK_MARKERS):
        return CLAUDE_TIMEOUT_CAP
    return CLAUDE_RUN_TIMEOUT_SECONDS


# When routed through the pool, each model has its own quota and they get exhausted under load one
# at a time, then recover. So on RESOURCE_EXHAUSTED, fail over to the next model with quota instead
# of blocking the task. Order = the configured proxy_model first, then these. Override per-employee
# with profile `proxy_model_fallbacks`.
DEFAULT_PROXY_MODEL_FALLBACKS = ["gemini-3-flash-agent", "gemini-pro-agent", "claude-sonnet-4-6",
                                 "gemini-3.1-pro-high", "gemini-3.1-pro-low"]


def _quota_exhausted(text: str) -> bool:
    return "RESOURCE_EXHAUSTED" in (text or "")


# rc that means "the whole pool is out of quota for every model" — the employee genuinely can't work
# right now, so the caller fast-fails the task back to the dispatcher instead of making them wait.
POOL_QUOTA_EXHAUSTED_RC = 88


def pool_models_with_quota(base: str, token: str) -> set[str] | None:
    """Ask the pool (/api/accounts) which models still have ≥1 enabled, non-rate-limited account.
    The pool already tracks per-account/per-model rate limits + reset times and cools down bad
    accounts; we just read it to pick a model with quota. Returns None if the API can't be read
    (then we fall back to reactive failover instead of wrongly declaring exhaustion)."""
    try:
        req = urllib.request.Request(base.rstrip("/") + "/api/accounts", headers={"x-api-key": token})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None
    available: set[str] = set()
    for acct in (data.get("accounts") or []):
        if not acct.get("enabled") or acct.get("isInvalid"):
            continue
        for model, rl in (acct.get("modelRateLimits") or {}).items():
            if isinstance(rl, dict) and not rl.get("isRateLimited"):
                available.add(str(model))
    return available


# ---------------------------------------------------------------- persistent memory (opt-in)
# A task/conversation can carry a stable "memory key" so Claude reuses ONE session across calls and
# actually remembers prior turns (no re-scanning the repo each time). Empirically: `--session-id`
# CREATES a session; a second `--session-id` for the same id errors "already in use"; `--resume`
# continues it. So we create once (tracked by a marker file) and resume thereafter. No key → the
# old stateless `--no-session-persistence` path, so nothing changes for normal tasks.
MEMORY_NS = uuid.uuid5(uuid.NAMESPACE_URL, "company-kernel/claude-memory")
_MEMORY_DIRECTIVE = re.compile(r"^\s*(?:记忆会话|memory[-_ ]?session)\s*[:：]\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def parse_memory_key(text: str) -> str:
    """Extract a `记忆会话: <key>` / `memory-session: <key>` directive from a task description."""
    m = _MEMORY_DIRECTIVE.search(text or "")
    return m.group(1).strip() if m else ""


def memory_session_id(agent: str, memory_key: str) -> str:
    return str(uuid.uuid5(MEMORY_NS, f"{agent}:{memory_key}"))


def _memory_marker(agent: str, sid: str) -> Path:
    return ROOT / "employees" / agent / "sessions" / f"{sid}.json"


def memory_session_started(agent: str, sid: str) -> bool:
    return _memory_marker(agent, sid).exists()


def mark_memory_session(agent: str, sid: str, memory_key: str) -> None:
    p = _memory_marker(agent, sid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"session_id": sid, "memory_key": memory_key, "created_at": now()}, ensure_ascii=False), encoding="utf-8")


def claude_session_flags(agent: str, memory_key: str, attempt: int) -> tuple[list[str], str]:
    """Session CLI flags for one attempt. Returns (flags, session_id). attempt>0 means we're
    failing over models within one call — the session was already created on attempt 0, so resume."""
    if not memory_key:
        return ["--no-session-persistence"], ""
    sid = memory_session_id(agent, memory_key)
    if memory_session_started(agent, sid) or attempt > 0:
        return ["--resume", sid], sid
    return ["--session-id", sid], sid


_SESSION_IN_USE = re.compile(r"already in use|session id .*in use", re.IGNORECASE)


def _run_claude_once(cmd: list[str], workspace: Path, proxy_env: dict, limit: int) -> tuple[int, str, str]:
    """One `claude -p` subprocess run. Returns (rc, stdout, stderr). Timeout → rc 124 with a clear note."""
    try:
        cp = subprocess.run(cmd, cwd=str(workspace), text=True, capture_output=True,
                            env={**os.environ, **proxy_env}, timeout=limit)
        return cp.returncode, cp.stdout or "", cp.stderr or ""
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")) if exc.stdout else ""
        return 124, out, f"claude -p killed after exceeding {limit}s timeout (was hanging)"


def run_claude(prompt: Path, output: Path, workspace: Path, model: str, permission_mode: str, agent: str = "claude", timeout: int | None = None, memory_key: str = "") -> tuple[int, str]:
    proxy_env, model, route = resolve_claude_proxy(agent, model)
    # PER-EMPLOYEE 权限覆盖:profile.json 设了 permission_mode 就用它(例如 gemini QA 需要工具/浏览器 → bypassPermissions,
    # 否则 -p 模式工具被默认拒,只能输出文本)。未设的员工保持原 permission_mode 不变。
    fallbacks = list(DEFAULT_PROXY_MODEL_FALLBACKS)
    try:
        _prof = json.loads((ROOT / "employees" / agent / "profile.json").read_text(encoding="utf-8"))
        if str(_prof.get("permission_mode") or "").strip():
            permission_mode = str(_prof["permission_mode"]).strip()
        if isinstance(_prof.get("proxy_model_fallbacks"), list) and _prof["proxy_model_fallbacks"]:
            fallbacks = [str(m) for m in _prof["proxy_model_fallbacks"]]
    except (OSError, json.JSONDecodeError):
        pass
    prompt_text = prompt.read_text(encoding="utf-8")
    limit = timeout or CLAUDE_RUN_TIMEOUT_SECONDS
    # Through the proxy: proactively pick a model that has quota (per the pool's /api/accounts), in
    # preference order. If the pool says EVERY model is rate-limited on every account, fast-fail so
    # the caller can tell the dispatcher instead of making them wait. API unreachable → reactive failover.
    if proxy_env:
        ordered = [model] + [m for m in fallbacks if m != model]
        available = pool_models_with_quota(proxy_env.get("ANTHROPIC_BASE_URL", ""), proxy_env.get("ANTHROPIC_AUTH_TOKEN", "test"))
        if available is not None:
            usable = [m for m in ordered if m in available]
            if not usable:
                msg = ("ALL_QUOTA_EXHAUSTED: 代理池所有模型在所有启用账号上都被限速,"
                       f"员工「{agent}」此刻无法工作。")
                output.write_text(msg + "\n", encoding="utf-8")
                return POOL_QUOTA_EXHAUSTED_RC, f"claude -p [{route}] {msg}"
            models_to_try = usable
        else:
            models_to_try = ordered  # pool account API unreachable → try all, rely on reactive failover
    else:
        models_to_try = [model]
    # Resolve the real binary (absolute path) so we never hit a shell function/alias like the
    # interactive `claude () { command claude --bare ... }` wrapper (which ignores the OAuth login).
    claude_bin = shutil.which("claude") or "claude"
    rc, out, err, used = 1, "", "", model
    failover = []
    for attempt, m in enumerate(models_to_try):
        sess_flags, _sid = claude_session_flags(agent, memory_key, attempt)
        cmd = [claude_bin, "-p", prompt_text, *sess_flags, "--output-format", "text"]
        if m:
            cmd.extend(["--model", m])
        if permission_mode:
            cmd.extend(["--permission-mode", permission_mode])
        rc, out, err = _run_claude_once(cmd, workspace, proxy_env, limit)
        # Salvage a stale/locked memory session: a crashed/killed prior run can leave the session "already
        # in use", which would otherwise fail every later task. Rerun this attempt stateless so the task
        # still completes (loses continuity for this run; the session resumes once the lock clears).
        if memory_key and "--no-session-persistence" not in sess_flags and _SESSION_IN_USE.search(out + err):
            cmd_stateless = [claude_bin, "-p", prompt_text, "--no-session-persistence", "--output-format", "text"]
            if m:
                cmd_stateless.extend(["--model", m])
            if permission_mode:
                cmd_stateless.extend(["--permission-mode", permission_mode])
            rc, out, err = _run_claude_once(cmd_stateless, workspace, proxy_env, limit)
        used = m
        if not (proxy_env and _quota_exhausted(out + err)):
            break  # success, or a non-quota failure → stop trying other models
        failover.append(m)  # this model is exhausted → try the next one
    # In memory mode the session now exists (claude ran at least once) — mark it so the next
    # call resumes instead of re-creating (which errors "already in use").
    if memory_key:
        sid = memory_session_id(agent, memory_key)
        if not memory_session_started(agent, sid):
            mark_memory_session(agent, sid, memory_key)
    note = f" (failover past exhausted: {', '.join(failover)})" if failover else ""
    mem = " [memory]" if memory_key else ""
    output.write_text(out + ("\n\n## stderr\n\n" + err if err else ""), encoding="utf-8")
    return rc, " ".join(["claude", "-p", "<prompt>", "--model", used or "(default)", f"[{route}]{note}{mem}"])


def handle_direct(args: argparse.Namespace) -> int:
    """Direct reachability probe (used by verify-direct activation): run claude -p with the
    message and return its reply + a receipt, so the kernel can confirm the runtime is live."""
    if not shutil.which("claude"):
        emit({"ok": False, "error": "claude command not found", "agent": args.agent, "direct_message": True})
        return 1
    base = ROOT / "employees" / args.agent / "reports" / "direct"
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    prompt = base / f"direct-prompt-{stamp}.md"
    output = base / f"direct-output-{stamp}.md"
    prompt.write_text(args.direct_message, encoding="utf-8")
    workspace = Path(args.workspace).expanduser() if args.workspace else DEFAULT_WORKSPACE
    memory_key = getattr(args, "memory_session", "") or ""
    code, _ = run_claude(prompt, output, workspace, args.model, args.permission_mode, args.agent, memory_key=memory_key)
    reply = output.read_text(encoding="utf-8", errors="replace").strip() if output.exists() else ""
    receipt = base / f"direct-receipt-{stamp}.json"
    receipt.write_text(json.dumps({"agent": args.agent, "source": args.direct_source,
        "session_key": args.direct_session_key, "reply": reply, "created_at": now()}, ensure_ascii=False, indent=2), encoding="utf-8")
    run_companyctl(["heartbeat", "--agent", args.agent])
    emit({"ok": code == 0, "processed": 0, "agent": args.agent, "direct_message": True,
          "source": args.direct_source, "session_key": args.direct_session_key,
          "reply": reply, "receipt": str(receipt), "activation_eligible": True})
    return code


def process(args: argparse.Namespace) -> int:
    emp = employee(args.agent)
    if not emp:
        emit({"ok": False, "error": "unknown employee", "agent": args.agent})
        return 1
    if emp["runtime"] not in {"claude", "gemini"}:
        emit({"ok": False, "error": "employee runtime is not claude/gemini", "agent": args.agent, "runtime": emp["runtime"]})
        return 1
    if getattr(args, "direct_message", ""):
        return handle_direct(args)
    if args.execute and not shutil.which("claude"):
        emit({"ok": False, "error": "claude command not found"})
        return 1
    task = next_task(args.agent)
    if not task:
        run_companyctl(["heartbeat", "--agent", args.agent])
        emit({"ok": True, "processed": 0, "agent": args.agent, "note": "no submitted Claude task"})
        return 0
    workspace = Path(args.workspace or emp["workspace"] or DEFAULT_WORKSPACE).expanduser()
    artifact = paths(args.agent, task["id"])
    artifact["prompt"].write_text(build_prompt(task), encoding="utf-8")
    claim_code, claim_out, claim_err = run_companyctl(["task", "claim", "--agent", args.agent, "--task-id", task["id"]])
    if claim_code != 0:
        emit({"ok": False, "error": "claim failed", "stdout": claim_out, "stderr": claim_err})
        return claim_code
    if not args.execute:
        detail = "Claude adapter dry-run generated print prompt. Use --execute to run claude -p."
        write_report(artifact["report"], task, executed=False, status="completed", detail=detail, prompt=artifact["prompt"], output=artifact["output"])
        evidence_report = copy_report_to_task_evidence(task["id"], artifact["report"])
        done_code, done_out, done_err = run_companyctl(["task", "done", "--agent", args.agent, "--task-id", task["id"], "--summary", detail, "--evidence", str(evidence_report)])
        run_companyctl(["heartbeat", "--agent", args.agent])
        emit({"ok": done_code == 0, "processed": 1, "executed": False, "task_id": task["id"], "prompt": str(artifact["prompt"]), "report": str(artifact["report"]), "companyctl_stdout": done_out, "companyctl_stderr": done_err})
        return done_code
    memory_key = parse_memory_key(task["description"] or "")
    code, cmd = run_claude(artifact["prompt"], artifact["output"], workspace, args.model, args.permission_mode, args.agent, memory_key=memory_key, timeout=resolve_claude_timeout(task))
    if code == 0:
        detail = execution_detail(cmd, artifact["output"], success=True)
        write_report(artifact["report"], task, executed=True, status="completed", detail=detail, prompt=artifact["prompt"], output=artifact["output"])
        evidence_report = copy_report_to_task_evidence(task["id"], artifact["report"])
        done_code, done_out, done_err = run_companyctl(["task", "done", "--agent", args.agent, "--task-id", task["id"], "--summary", detail, "--evidence", str(evidence_report)])
    elif code == POOL_QUOTA_EXHAUSTED_RC:
        # the whole pool is out of quota → the employee genuinely can't work now. Don't make the
        # dispatcher wait: block with a clear reason AND message them so they can handle it.
        detail = ("员工暂时无法接活:代理池所有模型额度都耗尽(账号均被限速)。"
                  "此任务未执行,请改派、稍后重试,或在 :8080 池子补/换号。")
        write_report(artifact["report"], task, executed=True, status="blocked", detail=detail, prompt=artifact["prompt"], output=artifact["output"])
        done_code, done_out, done_err = run_companyctl(["task", "block", "--agent", args.agent, "--task-id", task["id"], "--blocker", detail])
        if task["source_agent"]:
            run_companyctl(["message", "send", "--from", args.agent, "--to", task["source_agent"],
                            "--body", f"⚠ 我({args.agent})暂时接不了活:{detail} 任务 {task['id']}「{task['title']}」未执行,请你处理。"])
    elif code == RUNTIME_TIMEOUT_RC and output_is_substantive(artifact["output"]):
        # Auto-judge: it timed out, BUT produced a full result before the wrap-down hung. For an
        # analysis/review runtime (claude/gemini/agy-via-proxy) the output IS the deliverable, so
        # accept it as completed rather than red-alarming a task whose work is actually done.
        detail = "⏱ 运行超时但已产出完整结果(收尾卡住被终止)—— 按已完成采纳。\n\n" + execution_detail(cmd, artifact["output"], success=True)
        write_report(artifact["report"], task, executed=True, status="completed", detail=detail, prompt=artifact["prompt"], output=artifact["output"])
        evidence_report = copy_report_to_task_evidence(task["id"], artifact["report"])
        done_code, done_out, done_err = run_companyctl(["task", "done", "--agent", args.agent, "--task-id", task["id"], "--summary", detail, "--evidence", str(evidence_report)])
        code = 0  # treat as success for the adapter's own exit + reporting
    else:
        timed_out_empty = code == RUNTIME_TIMEOUT_RC
        detail = (("⏱ 真超时/卡死:超时被终止且无有效产出,建议重试或调高超时。\n\n" if timed_out_empty else "")
                  + execution_detail(cmd, artifact["output"], exit_code=code, success=False))
        write_report(artifact["report"], task, executed=True, status="blocked", detail=detail, prompt=artifact["prompt"], output=artifact["output"])
        done_code, done_out, done_err = run_companyctl(["task", "block", "--agent", args.agent, "--task-id", task["id"], "--blocker", detail])
    run_companyctl(["heartbeat", "--agent", args.agent])
    emit({"ok": code == 0 and done_code == 0, "processed": 1, "executed": True, "task_id": task["id"], "claude_exit_code": code, "prompt": str(artifact["prompt"]), "output": str(artifact["output"]), "report": str(artifact["report"]), "companyctl_stdout": done_out, "companyctl_stderr": done_err})
    return done_code if done_code != 0 else code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Company Kernel Claude adapter")
    parser.add_argument("--agent", default="claude")
    parser.add_argument("--workspace", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--permission-mode", default="default")
    parser.add_argument("--execute", action="store_true", help="actually run claude -p; without this only writes prompt and report")
    parser.add_argument("--direct-message", default="", help="direct reachability probe: run claude -p with this and return the reply (used by verify-direct)")
    parser.add_argument("--direct-source", default="", help="source employee for the direct probe")
    parser.add_argument("--direct-session-key", default="", help="session key from the direct resolver")
    parser.add_argument("--memory-session", default="", help="stable memory key: reuse ONE claude session across calls so it remembers prior turns (used by conversations)")
    parser.add_argument("--timeout", type=int, default=120, help="direct probe timeout seconds")
    return parser


def main(argv: list[str] | None = None) -> int:
    return process(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
