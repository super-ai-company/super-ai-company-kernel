"""Fault-tolerance watchdog — force-fails stuck/orphaned execution attempts so their tasks land in
the FAILURE list instead of hanging forever. Extracted from companyctl.py (the 13k-line monolith)
as the first step of a phased split; companyctl re-exports every public symbol here, so all existing
callers (`companyctl.reap_stuck_attempts_internal`, `companyctl.process_alive`, …) are unchanged.

Shared kernel primitives (connect/now/rows/record_event/audit/…) are referenced via the companyctl
module object at CALL time — NOT imported by name — so the companyctl↔watchdog import cycle resolves,
and tests that mock e.g. `companyctl.notification_send_result` still intercept the call here.
"""
from __future__ import annotations

import argparse
import os
import sqlite3

# NOTE: companyctl is imported LAZILY inside each function (not at module top). companyctl runs as
# `__main__` under `python -m company_kernel.companyctl`, so a top-level `from company_kernel import
# companyctl` here would re-import companyctl as a SECOND module mid-load and deadlock the re-export
# cycle. Importing it at call time (cached in sys.modules) sidesteps that entirely, and keeps mocks
# of `companyctl.<helper>` effective since we always go through the live module object.

# Hard ceiling the watchdog enforces no matter how generous a task's own policy is: a run past this
# is force-reaped. The adapter-level process-group timeout catches most hangs first (~30-75min); this
# is the backstop for attempts whose worker died/hung without ever finishing the attempt row.
WATCHDOG_GLOBAL_CAP_SECONDS = 5400  # 90 minutes
# Grace before the pid-liveness (orphan) check fires, so a just-started adapter whose pid stamp is
# milliseconds behind isn't misread as dead.
WATCHDOG_ORPHAN_GRACE_SECONDS = 120
TERMINAL_TASK_STATUSES = ("completed", "done", "blocked", "cancelled", "failed", "stale")
REAP_REASON_LABEL = {"runtime_exceeded": "运行超时", "worker_process_gone": "执行进程失联"}


def process_alive(pid: int) -> bool:
    """True if a process with this pid currently exists (signal-0 probe). The watchdog uses this to
    detect an ORPHANED attempt whose adapter process already died (e.g. the daemon was killed
    mid-run) — caught fast instead of waiting out the 90-min cap. pid reuse can only cause a false
    'alive' (a conservative miss), never a false 'dead', so this NEVER reaps a live run."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True  # exists but un-signalable, or unknown error → assume alive (conservative)
    return True


def reap_stuck_attempts_internal(conn: sqlite3.Connection, *, actor: str = "openclaw-main", now_ts: str | None = None) -> dict:
    """Fault-tolerance watchdog (free, pure SQL): force-fail an execution attempt that is either
    (a) running past its allowed runtime, or (b) ORPHANED — its stamped adapter pid is already dead
    while it's still 'running' (a crashed worker / killed daemon) — so its task lands in the FAILURE
    list with a real reason instead of hanging forever as 'claimed' while only the heartbeat looks
    stale (the symptom the owner sees). The pid check catches the orphan FAST (next tick) without
    waiting out the 90-min cap, and never false-positives a live run (a live process keeps a live pid).

    Closes the loop: attempt→stale, task→blocked('watchdog_reaped: ...'), dispatcher notified,
    `task.watchdog_reaped` event recorded. Race-safe: a conditional UPDATE means it acts only if it
    wins against a normally-finishing adapter (rowcount==1), so no double-reap. No auto-retry — a
    reaped task waits for the owner/dispatcher rather than burning budget re-running a hung task."""
    from company_kernel import companyctl  # lazy (see module note) — cached after first call
    current = now_ts or companyctl.now()
    reaped: list[dict] = []
    scan = companyctl.rows(conn, "SELECT * FROM execution_attempts WHERE status IN ('starting', 'running', 'correcting') ORDER BY started_at ASC")
    for attempt in scan:
        # A correcting attempt may be parked awaiting an owner decision / cancel-in-flight — don't
        # yank it on the same raw clock as a running one.
        if attempt.get("status") == "correcting" and attempt.get("cancel_requested_at"):
            continue
        # Orphaned leftover: the task already reached a terminal state (done/blocked/cancelled) while
        # this attempt was left 'running' (a race or a manual close-out). Just retire the stray attempt
        # — don't touch the task or re-notify, it already closed out. Distinct event from a real reap.
        task_status_row = conn.execute("SELECT status FROM tasks WHERE id = ?", (attempt.get("task_id"),)).fetchone()
        task_status = str(task_status_row["status"]) if task_status_row else ""
        if task_status in TERMINAL_TASK_STATUSES:
            # Re-confirm the task is STILL terminal inside the same atomic UPDATE (EXISTS) — otherwise a
            # task reopened/retried between this SELECT and the UPDATE, with the old attempt still
            # active, could be wrongly retired. rowcount==1 only if both still hold.
            cur = conn.execute(
                "UPDATE execution_attempts SET status = 'stale', finished_at = ?, error_message = ? "
                "WHERE attempt_id = ? AND status IN ('starting', 'running', 'correcting') "
                "AND EXISTS (SELECT 1 FROM tasks WHERE id = ? AND status IN ('completed', 'done', 'blocked', 'cancelled', 'failed', 'stale'))",
                (current, f"watchdog: orphaned attempt of already-{task_status} task, retired", attempt["attempt_id"], attempt.get("task_id")),
            )
            if cur.rowcount == 1:
                conn.commit()
                companyctl.record_event(conn, "task.attempt.reaped", actor, task_id=attempt.get("task_id", ""), trace_id=attempt.get("trace_id", ""),
                                        payload={"attempt_id": attempt["attempt_id"], "reason": "task_already_terminal", "task_status": task_status})
                reaped.append({"task_id": attempt.get("task_id", ""), "attempt_id": attempt["attempt_id"],
                               "employee_id": attempt.get("employee_id", ""), "reason": "task_already_terminal",
                               "task_status": task_status, "dispatcher_notified": None})
            continue
        policy = companyctl.attempt_json_field(attempt, "runtime_policy_json")
        max_runtime = int(policy.get("max_runtime_seconds", companyctl.DEFAULT_RUNTIME_POLICY["max_runtime_seconds"]) or companyctl.DEFAULT_RUNTIME_POLICY["max_runtime_seconds"])
        cap = min(max_runtime, WATCHDOG_GLOBAL_CAP_SECONDS)
        runtime_age = companyctl.seconds_since(attempt.get("started_at") or "", current)
        # Two independent reap triggers:
        #  • runtime_exceeded — ran past its cap (the universal backstop).
        #  • worker_process_gone — its stamped adapter pid is dead while it's still 'running' (the
        #    fast, reliable orphan signal for a crashed worker / killed daemon; never false-positives
        #    a live run since a live process keeps a live pid). Only after a short grace.
        reason, detail = "", ""
        if runtime_age >= cap:
            reason, detail = "runtime_exceeded", f"runtime_exceeded ({int(runtime_age)}s > {cap}s cap)"
        else:
            pid_raw = str(attempt.get("pid") or "").strip()
            if pid_raw.isdigit() and runtime_age >= WATCHDOG_ORPHAN_GRACE_SECONDS and not process_alive(int(pid_raw)):
                reason, detail = "worker_process_gone", f"worker_process_gone (pid {pid_raw} dead after {int(runtime_age)}s)"
        if not reason:
            continue
        attempt_id = attempt["attempt_id"]
        prev_status = attempt["status"]
        # Race-safe terminal transition: only the reaper that flips it from active wins; if an adapter
        # already finished it, rowcount==0 and we leave everything else alone.
        cur = conn.execute(
            "UPDATE execution_attempts SET status = 'stale', finished_at = ?, error_message = ? "
            "WHERE attempt_id = ? AND status IN ('starting', 'running', 'correcting')",
            (current, f"watchdog: {detail}, reaped", attempt_id),
        )
        if cur.rowcount != 1:
            continue
        task_id = attempt["task_id"]
        blocker = f"watchdog_reaped: {detail}"
        conn.execute(
            "UPDATE tasks SET status = 'blocked', blocker = ?, updated_at = ? "
            "WHERE id = ? AND status IN ('claimed', 'running', 'correcting', 'submitted', 'stale')",
            (blocker, current, task_id),
        )
        conn.commit()
        task_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        task = dict(task_row) if task_row else {"id": task_id, "target_agent": attempt.get("employee_id", ""), "source_agent": ""}
        notice = companyctl.deliver_completion_notice(conn, task, status="blocked", blocker=blocker, actor=actor)
        event = companyctl.record_event(
            conn, "task.watchdog_reaped", actor, task_id=task_id, trace_id=attempt.get("trace_id", ""),
            payload={"attempt_id": attempt_id, "employee_id": attempt.get("employee_id", ""), "reason": reason,
                     "runtime_age_seconds": int(runtime_age), "cap_seconds": cap, "pid": str(attempt.get("pid") or ""),
                     "previous_attempt_status": prev_status, "dispatcher_notified": bool(notice)},
        )
        companyctl.audit(conn, actor, "task.watchdog_reaped", task_id, {"attempt_id": attempt_id, "reason": reason, "runtime_age_seconds": int(runtime_age), "cap_seconds": cap, "event_id": event["id"]})
        reaped.append({"task_id": task_id, "attempt_id": attempt_id, "employee_id": attempt.get("employee_id", ""),
                       "reason": reason, "runtime_age_seconds": int(runtime_age), "cap_seconds": cap, "blocker": blocker,
                       "dispatcher_notified": notice or None})
    return {"scanned": len(scan), "reaped": reaped, "reaped_count": len(reaped)}


def notify_owner_of_reaps(reaped: list[dict]) -> list[dict]:
    """Tell the owner (Telegram) when the watchdog auto-reaps a task, so a self-healing reap isn't
    silent — the owner can review/re-dispatch. Best-effort: never raises; with no notification token
    configured it's a no-op. Production-only entry (the daemon passes --notify); tests call the
    internal reaper directly and never reach this, so no test ever sends a real alert."""
    from company_kernel import companyctl  # lazy (see module note)
    sent = []
    for item in reaped:
        reason = str(item.get("reason") or "")
        if reason == "task_already_terminal":
            continue  # housekeeping cleanup of a stray attempt — the task already closed out fine, not a failure
        label = REAP_REASON_LABEL.get(reason, "异常")
        msg = (f"任务「{str(item.get('task_id'))}」{label},已自动回收为受阻(blocked),请复核或改派。\n"
               f"员工 {item.get('employee_id', '')} · 运行 {item.get('runtime_age_seconds', 0)}s · {item.get('task_id', '')}")
        try:
            res = companyctl.notification_send_result(kind="error", subject="⏱ 任务被看门狗自动回收", message=msg)
        except Exception as exc:  # noqa: BLE001 — notification must never break the daemon tick
            res = {"ok": False, "error": str(exc)}
        sent.append({"task_id": item.get("task_id"), "notified": bool(res.get("ok"))})
    return sent


def cmd_watchdog_reap_stuck(args: argparse.Namespace) -> int:
    from company_kernel import companyctl  # lazy (see module note)
    conn = companyctl.connect()
    actor = companyctl.resolve_employee_alias(getattr(args, "by", "openclaw-main")) or "openclaw-main"
    result = reap_stuck_attempts_internal(conn, actor=actor)
    if getattr(args, "notify", False) and result.get("reaped"):
        result["owner_notifications"] = notify_owner_of_reaps(result["reaped"])
    companyctl.emit({"ok": True, **result})
    return 0
