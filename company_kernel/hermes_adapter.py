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
from . import project_memory
from .adapter_result import execution_detail
from .db_paths import ensure_db_parent, resolve_db_path
from .employee_comms import communication_protocol
from .sandboxing import wrap_command


ROOT = Path(os.environ.get("OPENCLAW_COMPANY_KERNEL_ROOT", Path(__file__).resolve().parents[1])).resolve()
DB_PATH = resolve_db_path(ROOT)
CODEX_AGENT = "codex"
DEFAULT_WORKSPACE = Path(
    os.environ.get("OPENCLAW_HERMES_WORKSPACE", str(Path.home() / ".hermes"))
).expanduser().resolve()


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(ensure_db_parent(DB_PATH))
    try:
        conn.row_factory = sqlite3.Row
        conn.executescript((ROOT / "company_kernel" / "schema.sql").read_text(encoding="utf-8"))
        conn.commit()
    except Exception:
        conn.close()  # don't leak a half-opened connection if schema bootstrap fails
        raise
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


def employee_workspace(agent: str) -> Path | None:
    row = employee(agent)
    if not row:
        return None
    workspace = str(row["workspace"] or "").strip()
    if not workspace:
        return None
    return Path(workspace).expanduser().resolve()


def run_codex_pm_supervisor() -> tuple[int, str, str]:
    workspace = employee_workspace(CODEX_AGENT)
    if workspace is None:
        return 1, "", f"missing workspace for {CODEX_AGENT}"
    env = {**os.environ, "OPENCLAW_COMPANY_KERNEL_ROOT": str(ROOT)}
    cmd = [
        str(ROOT / "bin" / "company-codex-pm-supervisor"),
        "--agent",
        CODEX_AGENT,
        "--db-path",
        str(DB_PATH),
        "--workspace",
        str(workspace),
        "--report-root",
        str(ROOT),
    ]
    cp = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True, env=env)
    return cp.returncode, cp.stdout, cp.stderr


def parse_json_output(raw: str) -> dict:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


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


def inbox_dir(agent: str) -> Path:
    return ROOT / "employees" / agent / "inbox"


def is_actionable_completion(notice: dict) -> bool:
    """A completion notice the Hermes brain should advance: a real task that finished or blocked.
    Loop-guarded — an `advance`/orchestration tick leaves no task_id, so it can never feed itself.
    """
    tid = str(notice.get("task_id") or "").strip()
    status = str(notice.get("status") or "").strip()
    return bool(tid) and status in ("completed", "done", "blocked", "cancelled")


def build_advance_prompt(notices: list[dict]) -> str:
    """One prompt that hands the Hermes brain everything that just finished, so it advances the plan
    in a single run — dispatch the next step, or summarize the round. No self-task is ever created.
    """
    lines = [
        "# Hermes 编排推进(完成回件触发)",
        "",
        "你是 Hermes,Super AI Company 的协调者。你派出去的任务有完成/受阻回件了,据此推进编排。",
        "硬规则:只做编排与汇总(派活 / 改派 / 汇总),绝不自己写代码,绝不改项目配置,绝不外发。",
        "派活用 MCP `dispatch_task`:开发派 `codex-cli`,审核派 `claude-cli`,汇总回业主派 `owner-shift`。",
        "已经处理过的别重复派。",
        "",
        "## 刚完成 / 受阻的回件",
    ]
    _verbs = {"blocked": "受阻", "cancelled": "已取消"}
    for n in notices:
        verb = _verbs.get(str(n.get("status")), "完成")
        who = n.get("done_by") or n.get("agent") or "?"
        body = str(n.get("summary") or n.get("blocker") or "").strip()
        lines.append(f"- 「{n.get('title', '')}」({n.get('task_id')})由 {who} {verb}:")
        lines.append(f"  {body or '(无摘要)'}")
    lines += [
        "",
        "## 你要做的(选其一或组合)",
        "- 中间步(开发完→该审核):派下一步给对应同事。",
        "- 整轮完成:把战报(做了什么/结论/关键风险/下一步)用一段话汇总给业主 owner-shift。",
        "- 受阻:判断改派、补输入还是上报业主。",
        "- 已取消:这任务不会再有结果,别再等——判断重派(换更合适的同事/补全信息)还是放弃并告知业主。",
    ]
    return "\n".join(lines)


def _project_digest_for_notices(notices: list[dict]) -> str:
    """Shared project-memory digest for the project these completions belong to, so Hermes decides
    with the SAME curated knowledge (decisions/conventions/outcomes) that codex/claude already get
    injected — i.e. all three share one project memory. Best-effort; never blocks the tick."""
    conn = None
    try:
        conn = connect()
        for n in notices:
            tid = n.get("task_id")
            if not tid:
                continue
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
            if row:
                block = project_memory.digest_block_for_task(conn, dict(row))
                if block:
                    return block
        return ""
    except Exception:
        return ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def report_progress_to_owner(agent: str, actionable: list[dict], output_path: Path) -> bool:
    """After Hermes advances a phase, push a concise progress line to the owner — which the message
    mirror forwards to the owner's Telegram — so every phase's outcome reaches the owner's phone, not
    just stall alerts. Pure block/cancel batches are skipped (the watchdog already alerts those).
    Best-effort: never raises, never blocks the tick. Returns True if a progress note was sent."""
    try:
        if not any(str(n.get("status")) in ("completed", "done") for n in actionable):
            return False
        owner = os.environ.get("COMPANY_KERNEL_OWNER", "owner-shift")
        titles = "、".join(f"「{str(n.get('title', ''))[:24]}」" for n in actionable[:3])
        snippet = ""
        try:
            snippet = output_path.read_text(encoding="utf-8").strip()[:400]
        except OSError:
            pass
        body = f"📊 进度:{titles} 已完成,hermes 已推进下一步。\n{snippet}".strip()
        run_companyctl(["message", "send", "--from", agent, "--to", owner, "--body", body])
        return True
    except Exception:
        return False


def advance_from_completions(args: argparse.Namespace, workspace: Path) -> dict | None:
    """Taskless orchestration tick: if completion notices are waiting, run the Hermes brain directly
    on them so it advances the plan — WITHOUT creating any self-task on the board (the only visible
    tasks stay the real dev/review work Hermes dispatches). Archives consumed notices. Returns a
    result dict, or None when nothing is waiting (event-gated — no polling, no clutter).
    """
    inbox = inbox_dir(args.agent)
    if not inbox.exists():
        return None
    paths = sorted(inbox.glob("result-*.json"), key=lambda x: x.stat().st_mtime)
    if not paths:
        return None
    notices = []
    for p in paths:
        try:
            n = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            n = {}
        n["__path"] = p
        notices.append(n)
    actionable = [n for n in notices if is_actionable_completion(n)]
    archive = inbox / "processed"
    archive.mkdir(parents=True, exist_ok=True)

    def _archive(p: Path) -> None:
        try:
            p.rename(archive / p.name)
        except OSError:
            pass

    if not actionable:
        for n in notices:
            _archive(n["__path"])  # clear non-actionable stragglers so the inbox stays clean
        return None
    if not args.execute:
        # dry-run: don't lose the completions — leave them for a real --execute tick
        return {"advanced": [n.get("task_id") for n in actionable], "executed": False,
                "note": "dry-run; left notices for an --execute tick"}
    # clear non-actionable stragglers (already-processed markers, malformed files) so the inbox stays clean
    for n in notices:
        if not is_actionable_completion(n):
            _archive(n["__path"])
    # Group actionable completions BY PROJECT so independent projects / orchestration rounds never get
    # merged into one brain run (which would cross-pollute the next-step dispatch, the summary, and the
    # injected digest). Each group advances on its own, with its own project memory and output files.
    groups: dict[str, list[dict]] = {}
    for n in actionable:
        groups.setdefault(str(n.get("project_id") or ""), []).append(n)
    base = ROOT / "employees" / args.agent / "reports" / "advance"
    base.mkdir(parents=True, exist_ok=True)
    advanced: list = []
    reported_any = False
    any_retry = False
    for pid, group in groups.items():
        # unique batch id (project + earliest task) so rapid/concurrent ticks never overwrite each other's
        # prompt/output, keeping progress + failure evidence traceable per batch.
        gid = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{pid or 'noproj'}-{sorted(str(n.get('task_id') or '') for n in group)[0]}")[:80]
        prompt_path = base / f"advance-{gid}-prompt.md"
        output_path = base / f"advance-{gid}-output.md"
        digest = _project_digest_for_notices(group)
        prompt_text = build_advance_prompt(group)
        if digest:
            prompt_text = digest.rstrip() + "\n\n" + prompt_text  # same project memory codex/claude get
        prompt_path.write_text(prompt_text, encoding="utf-8")
        code, _cmd = run_hermes(prompt_path, output_path, workspace, args.model, args.provider, args.isolation, args.sandbox_profile)
        if code != 0:
            # this group's brain run failed (crash / timeout / 529) — leave ITS notices for the next tick
            # to retry; other project groups still proceed independently.
            any_retry = True
            continue
        if report_progress_to_owner(args.agent, group, output_path):
            reported_any = True
        for n in group:
            _archive(n["__path"])  # consumed only after a successful brain run for this group
        advanced.extend(n.get("task_id") for n in group)
    return {"advanced": advanced, "executed": True, "groups": len(groups),
            "owner_progress_sent": reported_any, "retry_pending": any_retry}


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
            communication_protocol(task["target_agent"], "hermes"),
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
    target = evidence_dir / f"hermes-adapter-report-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
    target.write_bytes(report.read_bytes())
    return target


def build_hermes_command(prompt: Path, model: str, provider: str) -> list[str]:
    # absolute binary so we never hit a shell function/alias wrapper (subprocess won't read shell
    # functions, but resolving the path is explicit and immune to PATH-script wrappers too)
    cmd = [shutil.which("hermes") or "hermes", "-z", prompt.read_text(encoding="utf-8")]
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
    pm_code, pm_out, pm_err = run_codex_pm_supervisor()
    pm_result = parse_json_output(pm_out)
    emp = employee(args.agent)
    if not emp:
        emit(
            {
                "ok": False,
                "error": "unknown employee",
                "agent": args.agent,
                "codex_pm_supervisor": {"exit_code": pm_code, "stdout": pm_out, "stderr": pm_err, "result": pm_result},
            }
        )
        return 1
    if emp["runtime"] != "hermes":
        emit(
            {
                "ok": False,
                "error": "employee runtime is not hermes",
                "agent": args.agent,
                "runtime": emp["runtime"],
                "codex_pm_supervisor": {"exit_code": pm_code, "stdout": pm_out, "stderr": pm_err, "result": pm_result},
            }
        )
        return 1
    if args.execute and not shutil.which("hermes"):
        emit(
            {
                "ok": False,
                "error": "hermes command not found",
                "codex_pm_supervisor": {"exit_code": pm_code, "stdout": pm_out, "stderr": pm_err, "result": pm_result},
            }
        )
        return 1
    workspace = Path(args.workspace or emp["workspace"] or DEFAULT_WORKSPACE).expanduser()
    # Taskless orchestration tick: if a dispatched step just finished, run the Hermes brain on those
    # completion notices directly — advance the plan (dispatch next step / summarize) with NO
    # self-task on the board. Event-gated: returns None when nothing finished.
    advanced = advance_from_completions(args, workspace)
    if advanced is not None:
        run_companyctl(["heartbeat", "--agent", args.agent])
        emit(
            {
                "ok": True,
                "processed": 0,
                "advanced_from_completions": advanced,
                "agent": args.agent,
                "note": "advanced plan from completion notices",
                "codex_pm_supervisor": {"exit_code": pm_code, "stdout": pm_out, "stderr": pm_err, "result": pm_result},
            }
        )
        return 0
    task = next_task(args.agent)
    if not task:
        run_companyctl(["heartbeat", "--agent", args.agent])
        emit(
            {
                "ok": True,
                "processed": 0,
                "agent": args.agent,
                "note": "no submitted Hermes task",
                "codex_pm_supervisor": {"exit_code": pm_code, "stdout": pm_out, "stderr": pm_err, "result": pm_result},
            }
        )
        return 0
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
        emit(
            {
                "ok": done_code == 0,
                "processed": 1,
                "executed": False,
                "task_id": task["id"],
                "prompt": str(artifact["prompt"]),
                "report": str(artifact["report"]),
                "companyctl_stdout": done_out,
                "companyctl_stderr": done_err,
                "codex_pm_supervisor": {"exit_code": pm_code, "stdout": pm_out, "stderr": pm_err, "result": pm_result},
            }
        )
        return done_code
    run_code, run_payload, run_err = run_companyctl_json(["task", "run", "--task-id", task["id"], "--agent", args.agent, "--by", args.agent, "--adapter-type", "hermes", "--session-key", f"hermes:{task['id']}"])
    if run_code != 0:
        emit({"ok": False, "error": "attempt start failed", "task_id": task["id"], "companyctl": run_payload, "stderr": run_err[-1000:]})
        return run_code
    attempt = run_payload["attempt"]
    attempt_id = attempt["attempt_id"]
    trace_id = str(attempt.get("trace_id", ""))
    session_id = f"hermes-session-{args.agent}-{task['id']}"
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
            "hermes",
            "--runtime-type",
            "cli",
            "--session-key",
            f"hermes:{task['id']}",
            "--task-id",
            task["id"],
            "--attempt-id",
            attempt_id,
        ]
    )
    if session_code != 0:
        emit({"ok": False, "error": "runtime session start failed", "task_id": task["id"], "attempt": attempt, "companyctl": session_payload, "stderr": session_err[-1000:]})
        return session_code
    tool_call_id = f"hermes-tool-{args.agent}-{task['id']}"
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
            "hermes.oneshot",
            "--tool-type",
            "cli",
            "--input-summary",
            f"hermes -z provider={args.provider or '-'} model={args.model or '-'}",
            "--risk-level",
            "low",
        ]
    )
    run_companyctl(["task", "progress", "--task-id", task["id"], "--agent", args.agent, "--attempt-id", attempt_id, "--state", "acknowledged", "--message", "Hermes adapter acknowledged managed execution", "--progress", "5"])
    started_monotonic = time.monotonic()
    code, cmd = run_hermes(artifact["prompt"], artifact["output"], workspace, args.model, args.provider, args.isolation, args.sandbox_profile)
    runtime_seconds = max(0, int(round(time.monotonic() - started_monotonic)))
    if code == 0:
        detail = execution_detail(cmd, artifact["output"], success=True)
        write_report(artifact["report"], task, executed=True, status="completed", detail=detail, prompt=artifact["prompt"], output=artifact["output"])
        evidence_report = copy_report_to_task_evidence(task["id"], artifact["report"])
        done_code, done_out, done_err = run_companyctl(["task", "done", "--agent", args.agent, "--task-id", task["id"], "--summary", detail, "--evidence", str(evidence_report)])
        tool_status = "success"
        attempt_status = "success"
        session_status = "stopped"
    else:
        detail = execution_detail(cmd, artifact["output"], exit_code=code, success=False)
        write_report(artifact["report"], task, executed=True, status="blocked", detail=detail, prompt=artifact["prompt"], output=artifact["output"])
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
            "hermes_runtime",
            "--amount",
            "0",
            "--currency",
            "USD",
            "--model-name",
            args.model or "",
            "--provider",
            args.provider or "hermes",
            "--runtime-seconds",
            str(runtime_seconds),
            "--summary",
            f"hermes -z exit_code={code}",
        ]
    )
    _, finish_payload, finish_err = run_companyctl_json(["task", "attempt", "finish", "--attempt-id", attempt_id, "--status", attempt_status, "--error", "" if code == 0 else detail[:500]])
    _, stopped_session, _ = run_companyctl_json(["runtime", "session", "stop", "--session-id", session_id, "--status", session_status, "--error", "" if code == 0 else detail[:500]])
    run_companyctl(["heartbeat", "--agent", args.agent])
    emit(
        {
            "ok": code == 0 and done_code == 0,
            "processed": 1,
            "executed": True,
            "task_id": task["id"],
            "hermes_exit_code": code,
            "attempt": finish_payload.get("attempt", attempt),
            "runtime_session": stopped_session.get("session", session_payload.get("session", {})),
            "tool_call": tool_payload.get("tool_call", {}),
            "budget_event": budget_payload.get("budget_event", {}),
            "prompt": str(artifact["prompt"]),
            "output": str(artifact["output"]),
            "report": str(artifact["report"]),
            "companyctl_stdout": done_out,
            "companyctl_stderr": done_err,
            "companyctl_finish_stderr": finish_err[-1000:],
            "codex_pm_supervisor": {"exit_code": pm_code, "stdout": pm_out, "stderr": pm_err, "result": pm_result},
        }
    )
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
