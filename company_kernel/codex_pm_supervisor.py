from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import companyctl
from .db_paths import ensure_db_parent, resolve_db_path


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = resolve_db_path(ROOT)
SCHEMA = ROOT / "company_kernel" / "schema.sql"


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def emit(obj: dict[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def connect(db_path: Path | None = None, schema_path: Path | None = None) -> sqlite3.Connection:
    db_path = (db_path or DB_PATH).expanduser().resolve()
    schema_path = (schema_path or SCHEMA).expanduser().resolve()
    conn = sqlite3.connect(ensure_db_parent(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(schema_path.read_text(encoding="utf-8"))
    conn.commit()
    return conn


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_json(path: Path) -> dict[str, Any]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def employee(conn: sqlite3.Connection, agent: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM employees WHERE id = ?", (agent,)).fetchone()


def active_task(conn: sqlite3.Connection, agent: str, *, include_fixtures: bool = False) -> sqlite3.Row | None:
    # `acceptance-*` ids are self-test fixtures (communication_acceptance creates them with aged
    # timestamps to exercise the supervisor's own escalation path). In production they must NEVER be
    # supervised — otherwise the self-test leaks ghost "Codex 卡住" escalations to the owner's Telegram,
    # and since the fixture is deleted after the test, handling it 404s. The self-test opts in explicitly.
    fixture_clause = "" if include_fixtures else "AND id NOT LIKE 'acceptance-%'"
    return conn.execute(
        f"""
        SELECT * FROM tasks
        WHERE target_agent = ?
          AND status IN ('submitted', 'claimed')
          {fixture_clause}
        ORDER BY updated_at DESC, created_at DESC
        LIMIT 1
        """,
        (agent,),
    ).fetchone()


# Lifecycle order of progress states, used only as a final tie-breaker when
# mtime and the filename timestamp suffix are identical (synthetic/same-tick
# writes). In production each file carries a distinct microsecond suffix.
_STATE_RANK = {
    "received": 0,
    "acknowledged": 1,
    "in_progress": 2,
    "completed": 3,
    "blocked": 4,
    "stalled": 5,
    "failed": 6,
}


def _state_rank_from_name(stem: str) -> int:
    remainder = stem[len("progress_"):] if stem.startswith("progress_") else stem
    for state, rank in _STATE_RANK.items():
        if remainder == state or remainder.startswith(state + "_"):
            return rank
    return 99


def progress_sort_key(path: Path) -> tuple[float, str, int]:
    """Deterministic chronological key.

    Filesystem mtime resolution can be as coarse as 1s, so multiple progress
    files written in the same tick would tie and sort nondeterministically.
    The filename carries a microsecond timestamp suffix
    (``progress_<state>_<task_id>_<YYYYMMDD-HHMMSS-micros>.json``); we use it as
    the primary tie-breaker so order is stable regardless of mtime granularity,
    then fall back to lifecycle rank for fully synthetic same-instant writes.
    """
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    suffix = path.stem.rsplit("_", 1)[-1]
    return (mtime, suffix, _state_rank_from_name(path.stem))


def progress_files(workspace: Path) -> list[Path]:
    reports = workspace / "reports"
    if not reports.exists():
        return []
    return sorted(reports.glob("progress_*.json"), key=progress_sort_key, reverse=True)


def progress_task_id(data: dict[str, Any]) -> str:
    report = data.get("report") if isinstance(data.get("report"), dict) else {}
    return str(data.get("task_id") or report.get("task_id") or "").strip()


def latest_progress(workspace: Path, task_id: str = "") -> tuple[Path | None, dict[str, Any]]:
    for path in progress_files(workspace):
        data = load_json(path)
        report = data.get("report")
        if isinstance(report, dict):
            if task_id and progress_task_id(data) != task_id:
                continue
            return path, data
    return None, {}


def progress_history(workspace: Path, task_id: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not task_id:
        return items
    for path in sorted(progress_files(workspace), key=progress_sort_key):
        data = load_json(path)
        report = data.get("report")
        if not isinstance(report, dict):
            continue
        if progress_task_id(data) != task_id:
            continue
        items.append(
            {
                "task_id": task_id,
                "state": progress_state(data),
                "action": progress_action(data, ""),
                "created_at": progress_created_at(data, ""),
                "path": str(path.resolve()),
            }
        )
    return items


def progress_state(data: dict[str, Any]) -> str:
    report = data.get("report") if isinstance(data.get("report"), dict) else {}
    return str(report.get("state") or data.get("state") or "").strip()


def progress_layer(data: dict[str, Any]) -> str:
    state = progress_state(data).lower().replace("-", "_").replace(" ", "_")
    if state in {"received", "acknowledged", "ack", "claimed"}:
        return "received"
    if state in {"working", "in_progress", "actively_progressing", "active", "running"}:
        return "working"
    if state in {"waiting", "blocked_on_input_or_dependency", "awaiting_input", "awaiting_dependency", "pending_input"}:
        return "waiting"
    if state in {"blocked", "failed_to_progress", "error", "stalled", "failed"}:
        return "blocked"
    if state in {"done", "verified_complete", "completed", "complete", "success"}:
        return "done"
    return ""


def progress_created_at(data: dict[str, Any], fallback: str) -> str:
    report = data.get("report") if isinstance(data.get("report"), dict) else {}
    return str(report.get("created_at") or data.get("created_at") or fallback)


def progress_action(data: dict[str, Any], fallback: str) -> str:
    report = data.get("report") if isinstance(data.get("report"), dict) else {}
    action = str(report.get("action") or "").strip()
    return action or fallback


def short_text(value: Any, limit: int = 42) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if not text:
        return "未命名任务"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def write_pm_report(agent: str, result: dict[str, Any], report_root: Path | None = None) -> Path:
    report_root = (report_root or ROOT).expanduser().resolve()
    out_dir = report_root / "employees" / "hermes" / "reports" / "codex-pm"
    out_dir.mkdir(parents=True, exist_ok=True)
    task_id = str(result.get("task_id") or "no-task")
    out = out_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{task_id}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out


def close_task(conn: sqlite3.Connection, task: sqlite3.Row, status: str, summary: str, evidence: str, ts: str) -> None:
    if status == "completed":
        conn.execute(
            """
            UPDATE tasks
            SET status = 'completed', summary = ?, evidence_path = ?, blocker = '', updated_at = ?
            WHERE id = ?
            """,
            (summary, evidence, ts, task["id"]),
        )
    elif status in {"blocked", "stalled"}:
        conn.execute(
            """
            UPDATE tasks
            SET status = 'claimed', blocker = ?, evidence_path = ?, updated_at = ?
            WHERE id = ?
            """,
            (summary, evidence, ts, task["id"]),
        )
    conn.commit()


def queue_supervisor_notification(result: dict[str, Any]) -> dict[str, Any]:
    db_path = Path(str(result.get("db_path") or DB_PATH)).expanduser().resolve()
    schema_path = SCHEMA.expanduser().resolve()
    conn = connect(db_path=db_path, schema_path=schema_path)
    try:
        payload = {
            "kind": "supervisor_escalation",
            "agent_id": str(result.get("agent", "") or ""),
            "task_id": str(result.get("task_id", "") or ""),
            "trace_id": "",
            "trigger": "codex_pm_supervisor",
            "triggered_at": now(),
            "triggered_by": "hermes",
            "message": str(result.get("human_message") or result.get("blocker") or "").strip(),
            "summary": str(result.get("human_message") or result.get("blocker") or "").strip(),
            "reason": "sync supervisor notification failed",
            "delivery_status": "pending",
            "channel": "repo-only",
            "account": "",
            "target": "",
            "delivery_result": result.get("notification", {}),
        }
        event = companyctl.record_event(
            conn,
            "progress.notification",
            "hermes",
            task_id=payload["task_id"],
            payload=payload,
        )
        return {"event_type": "progress.notification", "event_id": event["id"], **payload}
    finally:
        conn.close()


ESCALATION_COOLDOWN_SECONDS = 6 * 3600  # re-remind the same stuck task at most once per 6h (else flood)
MAX_ESCALATIONS_PER_ISSUE = 3  # circuit breaker: after N reminders of the same stuck task, stop auto-notifying
MAX_ESCALATIONS_TOTAL = 6  # hard cap per task across ALL status churn — a task flipping submitted↔claimed↔
#                            blocked (e.g. a retry loop) must not re-arm the per-issue breaker and flood.


def _escalation_dedup_path() -> Path:
    # Resolve from ROOT at call time (not import time) so tests that patch ROOT get an
    # isolated state file instead of leaking into the live state/ dedup store.
    return ROOT / "state" / "supervisor-escalation-dedup.json"


def _load_escalation_dedup() -> dict:
    try:
        return json.loads(_escalation_dedup_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _escalation_fingerprint(task_id: str, status: str, message: str) -> str:
    # Fingerprint on task_id + status ONLY. The human message varies every tick (e.g. "超过 N 分钟无完成"
    # with a growing N), so including it would defeat dedup and keep flooding. Status change still re-notifies.
    return f"{task_id}|{status}"


def _should_escalate_now(task_id: str, fingerprint: str) -> bool:
    """Dedup: skip re-sending the same escalation within the cooldown window. The supervisor runs
    every daemon tick, so without this a single stuck task floods Telegram every few minutes."""
    if not task_id:
        return True
    entry = _load_escalation_dedup().get(task_id)
    if not entry:
        return True  # never notified
    if int(entry.get("total", 0)) >= MAX_ESCALATIONS_TOTAL:
        return False  # hard total cap per task — stop even if status churn keeps changing the fingerprint
    if entry.get("fingerprint") != fingerprint:
        return True  # the issue changed (e.g. running→blocked) — worth one re-notify (still under total cap)
    if int(entry.get("count", 0)) >= MAX_ESCALATIONS_PER_ISSUE:
        return False  # circuit breaker: reminded enough; stop auto-notifying, owner handles manually
    try:
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(entry.get("notified_at", ""))).total_seconds()
    except (ValueError, TypeError):
        return True
    return elapsed >= ESCALATION_COOLDOWN_SECONDS


def _escalation_count(task_id: str, fingerprint: str) -> int:
    entry = _load_escalation_dedup().get(task_id) or {}
    return int(entry.get("count", 0)) if entry.get("fingerprint") == fingerprint else 0


def _record_escalation(task_id: str, fingerprint: str) -> None:
    if not task_id:
        return
    state = _load_escalation_dedup()
    prev = state.get(task_id, {})
    count = int(prev.get("count", 0)) + 1 if prev.get("fingerprint") == fingerprint else 1
    total = int(prev.get("total", 0)) + 1  # always increments, across every status/fingerprint change
    state[task_id] = {"fingerprint": fingerprint, "notified_at": now(), "count": count, "total": total}
    path = _escalation_dedup_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def notify_if_escalation(result: dict[str, Any], notify: bool) -> dict[str, Any]:
    # Safe by default: real owner notifications only fire when the caller explicitly opts in (the
    # production CLI main(), which the daemon runs). Every test / acceptance script / programmatic
    # supervise_once call passes notify=False, so running the test suite — even with secrets loaded
    # (e.g. codex reviewing with full access) — can NEVER leak ghost escalations to the owner's Telegram.
    # notify is threaded as a parameter (not a module global) so concurrent/interleaved supervise_once
    # calls in one process can't cross-contaminate each other's send decision.
    if not notify:
        result["notification"] = {"ok": True, "skipped": True, "reason": "notify disabled (non-production context)"}
        return result
    if result.get("status") not in {"stalled", "blocked"}:
        return result
    message = str(result.get("human_message") or result.get("blocker") or "").strip()
    if not message:
        return result
    task_id = str(result.get("task_id") or "").strip()
    fingerprint = _escalation_fingerprint(task_id, str(result.get("status") or ""), message)
    if not _should_escalate_now(task_id, fingerprint):
        result["notification"] = {"ok": True, "skipped": True, "reason": "escalation deduped or circuit-broken (max reached)"}
        return result
    # On the final allowed reminder, tell the owner it won't auto-notify again — handle it manually.
    if _escalation_count(task_id, fingerprint) + 1 >= MAX_ESCALATIONS_PER_ISSUE:
        message += f"\n⚠️ 已自动提醒 {MAX_ESCALATIONS_PER_ISSUE} 次仍未解决，后续不再自动提醒。请人工处理：补全任务说明(绝对仓库路径/验收)后重开，或丢弃。"
    reply_markup = None
    if task_id:
        reply_markup = {"inline_keyboard": [[
            {"text": "🔧 让 agent 修", "callback_data": f"ck_fix:{task_id}"},
            {"text": "👤 我来", "callback_data": f"ck_mine:{task_id}"},
            {"text": "⏭ 跳过", "callback_data": f"ck_skip:{task_id}"},
        ]]}
    try:
        notification = companyctl.notification_send_result(
            message=message,
            subject="Company Kernel supervisor escalation",
            kind="error",
            reply_markup=reply_markup,
        )
    except Exception as exc:
        notification = {"ok": False, "error": str(exc)}
    result["notification"] = notification
    if notification.get("ok"):
        _record_escalation(task_id, fingerprint)  # remember so we don't re-flood within the cooldown
    else:
        result["queued_notification"] = queue_supervisor_notification(result)
    return result


def finalize_result(agent: str, result: dict[str, Any], report_root: Path, notify: bool = False) -> dict[str, Any]:
    notify_if_escalation(result, notify)
    result["report_path"] = str(write_pm_report(agent, result, report_root=report_root))
    return result


def supervise_once(
    agent: str = "codex",
    now_ts: str | None = None,
    stale_minutes: int = 15,
    close_completed: bool = True,
    *,
    db_path: Path | None = None,
    schema_path: Path | None = None,
    workspace: Path | None = None,
    report_root: Path | None = None,
    include_fixtures: bool = False,
    notify: bool = False,
) -> dict[str, Any]:
    ts = now_ts or now()
    db_path = (db_path or DB_PATH).expanduser().resolve()
    schema_path = (schema_path or SCHEMA).expanduser().resolve()
    report_root = (report_root or ROOT).expanduser().resolve()
    conn = connect(db_path=db_path, schema_path=schema_path)
    try:
        emp = employee(conn, agent)
        if not emp:
            result = {
                "ok": False,
                "status": "blocked",
                "agent": agent,
                "blocker": "unknown_codex_employee",
                "timestamp": ts,
                "db_path": str(db_path),
                "workspace": str(workspace.expanduser().resolve()) if workspace else "",
            }
            return finalize_result(agent, result, report_root, notify=notify)
        task = active_task(conn, agent, include_fixtures=include_fixtures)
        effective_workspace = workspace.expanduser().resolve() if workspace else Path(emp["workspace"]).expanduser().resolve()
        progress_path, progress = latest_progress(effective_workspace, str(task["id"]) if task else "")
        if not task:
            result = {
                "ok": True,
                "status": "idle",
                "agent": agent,
                "human_message": "Codex 当前没有待监督任务。",
                "timestamp": ts,
                "db_path": str(db_path),
                "workspace": str(effective_workspace),
            }
            return finalize_result(agent, result, report_root, notify=notify)
        task_title = short_text(task["title"])
        if not progress_path:
            # Grace period: a freshly-claimed/actively-running task hasn't written its progress file
            # YET — that's normal, not "stuck". Only escalate once it's been quiet past stale_minutes.
            # (This kills the premature "卡住 没有进度证据" pings on tasks that go on to complete.)
            try:
                task_age_min = (parse_time(ts) - parse_time(str(task["updated_at"] or task["created_at"]))).total_seconds() / 60
            except Exception:  # noqa: BLE001 — unparseable timestamp → fall through to the old escalate path
                task_age_min = stale_minutes + 1
            if task_age_min < stale_minutes:
                result = {
                    "ok": True,
                    "status": "in_progress",
                    "agent": agent,
                    "task_id": task["id"],
                    "human_message": f"Codex 正在进行：{task_title}（已开始 {int(task_age_min)} 分钟，宽限期内不打扰）",
                    "evidence_path": str(effective_workspace / "reports"),
                    "timestamp": ts,
                    "db_path": str(db_path),
                    "workspace": str(effective_workspace),
                    "progress_history": [],
                }
                return finalize_result(agent, result, report_root, notify=notify)
            result = {
                "ok": True,
                "status": "stalled",
                "agent": agent,
                "task_id": task["id"],
                "human_message": f"Codex 卡住：{task_title} 超过 {stale_minutes} 分钟没有进度证据（卡住，请你处理）",
                "evidence_path": str(effective_workspace / "reports"),
                "timestamp": ts,
                "db_path": str(db_path),
                "workspace": str(effective_workspace),
                "progress_history": [],
            }
            return finalize_result(agent, result, report_root, notify=notify)
        history = progress_history(effective_workspace, str(task["id"]))
        state = progress_state(progress)
        layer = progress_layer(progress)
        action = short_text(progress_action(progress, task_title))
        evidence = str(progress_path)
        if state == "completed":
            result = {
                "ok": True,
                "status": "completed",
                "agent": agent,
                "task_id": task["id"],
                "human_message": f"完成了 Codex 的 {task_title} 任务",
                "action": action,
                "evidence_path": evidence,
                "latest_progress_path": evidence,
                "progress_layer": layer,
                "progress_state": state,
                "timestamp": ts,
                "db_path": str(db_path),
                "workspace": str(effective_workspace),
                "progress_history": history,
            }
            if close_completed:
                close_task(conn, task, "completed", result["human_message"], evidence, ts)
            return finalize_result(agent, result, report_root, notify=notify)
        if state == "blocked":
            result = {
                "ok": True,
                "status": "blocked",
                "agent": agent,
                "task_id": task["id"],
                "human_message": f"Codex 阻塞：{task_title}（卡住，请你处理）",
                "action": action,
                "evidence_path": evidence,
                "latest_progress_path": evidence,
                "progress_layer": layer,
                "progress_state": state,
                "timestamp": ts,
                "db_path": str(db_path),
                "workspace": str(effective_workspace),
                "progress_history": history,
            }
            close_task(conn, task, "blocked", result["human_message"], evidence, ts)
            return finalize_result(agent, result, report_root, notify=notify)
        created = parse_time(progress_created_at(progress, ts))
        age_minutes = (parse_time(ts) - created).total_seconds() / 60
        if layer in {"received", "working"} and age_minutes > stale_minutes:
            result = {
                "ok": True,
                "status": "stalled",
                "agent": agent,
                "task_id": task["id"],
                "human_message": f"Codex 卡住：{task_title} 超过 {stale_minutes} 分钟无完成（卡住，请你处理）",
                "action": action,
                "evidence_path": evidence,
                "latest_progress_path": evidence,
                "progress_layer": layer,
                "progress_state": state,
                "timestamp": ts,
                "db_path": str(db_path),
                "workspace": str(effective_workspace),
                "progress_history": history,
            }
            close_task(conn, task, "stalled", result["human_message"], evidence, ts)
            return finalize_result(agent, result, report_root, notify=notify)
        result = {
            "ok": True,
            "status": state or "in_progress",
            "agent": agent,
            "task_id": task["id"],
            "human_message": f"Codex 正在处理 {task_title}",
            "action": action,
            "evidence_path": evidence,
            "latest_progress_path": evidence,
            "progress_layer": layer,
            "progress_state": state,
            "timestamp": ts,
            "db_path": str(db_path),
            "workspace": str(effective_workspace),
            "progress_history": history,
        }
        return finalize_result(agent, result, report_root, notify=notify)
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hermes PM supervisor for Codex execution progress.")
    parser.add_argument("--agent", default="codex")
    parser.add_argument("--stale-minutes", type=int, default=15)
    parser.add_argument("--no-close-completed", action="store_true")
    parser.add_argument("--db-path", default=str(DB_PATH))
    parser.add_argument("--schema-path", default=str(SCHEMA))
    parser.add_argument("--workspace", default="")
    parser.add_argument("--report-root", default=str(ROOT))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workspace = Path(args.workspace).expanduser() if args.workspace else None
    result = supervise_once(
        agent=args.agent,
        stale_minutes=args.stale_minutes,
        close_completed=not args.no_close_completed,
        db_path=Path(args.db_path),
        schema_path=Path(args.schema_path),
        workspace=workspace,
        report_root=Path(args.report_root),
        notify=True,  # production entrypoint (run by the daemon) — the ONLY path that may notify the owner
    )
    emit(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
