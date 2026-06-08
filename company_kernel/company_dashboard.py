from __future__ import annotations

import argparse
import base64
import html
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import companyctl
from .db_paths import ensure_db_parent, resolve_db_path
from .schema_migrations import ensure_schema_migrations


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = resolve_db_path(ROOT)
SCHEMA = ROOT / "company_kernel" / "schema.sql"
DEFAULT_OUTPUT = ROOT / "state" / "dashboard.html"
ADVANCED_TEMPLATE_CANDIDATES = [
    ROOT / "dashboard_templates" / "gemini_dashboard.html",
]


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(ensure_db_parent(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    ensure_schema_migrations(conn)
    conn.commit()
    return conn


def rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def e(value: object) -> str:
    return html.escape("" if value is None else str(value))


def status_counts(conn: sqlite3.Connection, table: str) -> dict[str, int]:
    return {row["status"]: int(row["count"]) for row in rows(conn, f"SELECT status, COUNT(*) AS count FROM {table} GROUP BY status")}


def parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def minutes_since(value: str, generated_at: str) -> int | None:
    timestamp = parse_time(value)
    generated = parse_time(generated_at)
    if not timestamp or not generated:
        return None
    return max(0, int((generated - timestamp).total_seconds() // 60))


def seconds_since(value: str, generated_at: str) -> int | None:
    timestamp = parse_time(value)
    generated = parse_time(generated_at)
    if not timestamp or not generated:
        return None
    return max(0, int((generated - timestamp).total_seconds()))


def milliseconds_between(start: str, end: str) -> int:
    start_dt = parse_time(start)
    end_dt = parse_time(end)
    if not start_dt or not end_dt:
        return 1
    return max(1, int((end_dt - start_dt).total_seconds() * 1000))


def build_traces(conn: sqlite3.Connection, *, limit: int = 20) -> list[dict]:
    trace_ids = [
        row["trace_id"]
        for row in rows(
            conn,
            """
            SELECT trace_id, MAX(created_at) AS last_seen
            FROM (
              SELECT trace_id, created_at FROM company_events WHERE trace_id != ''
              UNION ALL
              SELECT trace_id, created_at FROM adapter_runs WHERE trace_id != ''
              UNION ALL
              SELECT trace_id, created_at FROM artifacts WHERE trace_id != ''
              UNION ALL
              SELECT trace_id, created_at FROM evidence WHERE trace_id != ''
              UNION ALL
              SELECT trace_id, created_at FROM handoffs WHERE trace_id != ''
              UNION ALL
              SELECT trace_id, started_at AS created_at FROM execution_attempts WHERE trace_id != ''
            )
            GROUP BY trace_id
            ORDER BY last_seen DESC
            LIMIT ?
            """,
            (limit,),
        )
    ]
    traces = []
    for trace_id in trace_ids:
        event_rows = rows(
            conn,
            """
            SELECT id, event_type, source_agent, task_id, payload_json, created_at, processed_at
            FROM company_events
            WHERE trace_id = ?
            ORDER BY created_at ASC
            """,
            (trace_id,),
        )
        run_rows = rows(
            conn,
            """
            SELECT id, agent_id, command, task_id, ok, processed, created_at
            FROM adapter_runs
            WHERE trace_id = ?
            ORDER BY created_at ASC
            """,
            (trace_id,),
        )
        artifact_rows = rows(conn, "SELECT artifact_id, task_id, employee_id, name, stage, status, version, created_at FROM artifacts WHERE trace_id = ? ORDER BY created_at ASC", (trace_id,))
        evidence_rows = rows(conn, "SELECT evidence_id, task_id, attempt_id, employee_id, summary, path_or_url, is_final, created_at FROM evidence WHERE trace_id = ? ORDER BY created_at ASC", (trace_id,))
        handoff_rows = rows(conn, "SELECT handoff_id, from_task_id, to_task_id, from_employee_id, status, summary, created_at FROM handoffs WHERE trace_id = ? ORDER BY created_at ASC", (trace_id,))
        attempt_rows = rows(
            conn,
            """
            SELECT attempt_id, task_id, employee_id, adapter_type, status,
                   runtime_policy_json, metadata_json, started_at, finished_at
            FROM execution_attempts
            WHERE trace_id = ?
            ORDER BY started_at ASC
            """,
            (trace_id,),
        )
        timestamps = [item["created_at"] for item in [*event_rows, *run_rows, *artifact_rows, *evidence_rows, *handoff_rows] if item.get("created_at")]
        timestamps.extend(item["started_at"] for item in attempt_rows if item.get("started_at"))
        timestamps.extend(item["finished_at"] for item in attempt_rows if item.get("finished_at"))
        if not timestamps:
            continue
        start = min(timestamps)
        end = max([*(item.get("processed_at") for item in event_rows if item.get("processed_at")), *timestamps])
        duration = max(1, milliseconds_between(start, end))
        spans = []
        for event_index, event in enumerate(event_rows):
            span_start = milliseconds_between(start, event["created_at"])
            span_duration = milliseconds_between(event["created_at"], event.get("processed_at") or event["created_at"])
            event_name = event["event_type"]
            correction_direction = ""
            if event_name == "supervisor.correction_requested":
                event_name = "supervisor.correction.requested"
                correction_direction = "supervisor_to_worker"
            elif event_name == "supervisor.correction_acknowledged":
                event_name = "supervisor.correction.acknowledged"
                correction_direction = "worker_to_supervisor"
            span = {
                "name": event_name,
                "service": event["source_agent"] or "event",
                "duration_ms": max(12, span_duration),
                "start_ms": span_start,
                "event_id": event["id"],
                "task_id": event["task_id"],
                "created_at": event["created_at"],
                "processed_at": event.get("processed_at", ""),
                "event_sequence": event_index,
            }
            if correction_direction:
                try:
                    payload = json.loads(event.get("payload_json", "{}") or "{}")
                except json.JSONDecodeError:
                    payload = {}
                span["attempt_id"] = str(payload.get("attempt_id", "") or "")
                span["correction_direction"] = correction_direction
                span["label"] = str(payload.get("message", "") or event["event_type"])
            spans.append(
                span
            )
        for run in run_rows:
            span_start = milliseconds_between(start, run["created_at"])
            spans.append(
                {
                    "name": run["command"] or "adapter.run",
                    "service": run["agent_id"] or "adapter",
                    "duration_ms": 80 if run.get("processed") else 24,
                    "start_ms": span_start,
                    "adapter_run_id": run["id"],
                    "task_id": run["task_id"],
                    "ok": bool(run["ok"]),
                    "processed": bool(run["processed"]),
                    "created_at": run["created_at"],
                }
            )
        for artifact in artifact_rows:
            spans.append(
                {
                    "name": f"artifact.{artifact['status']}",
                    "service": artifact["employee_id"] or "artifact",
                    "duration_ms": 24,
                    "start_ms": milliseconds_between(start, artifact["created_at"]),
                    "artifact_id": artifact["artifact_id"],
                    "task_id": artifact["task_id"],
                    "created_at": artifact["created_at"],
                    "label": f"{artifact['name']} v{artifact['version']} {artifact['stage']}",
                }
            )
        for item in evidence_rows:
            spans.append(
                {
                    "name": "evidence.final" if item.get("is_final") else "evidence.created",
                    "service": item["employee_id"] or "evidence",
                    "duration_ms": 24,
                    "start_ms": milliseconds_between(start, item["created_at"]),
                    "evidence_id": item["evidence_id"],
                    "task_id": item["task_id"],
                    "attempt_id": item.get("attempt_id", ""),
                    "created_at": item["created_at"],
                    "label": item["summary"] or item["path_or_url"],
                }
            )
        for handoff in handoff_rows:
            spans.append(
                {
                    "name": f"handoff.{handoff['status']}",
                    "service": handoff["from_employee_id"] or "handoff",
                    "duration_ms": 24,
                    "start_ms": milliseconds_between(start, handoff["created_at"]),
                    "handoff_id": handoff["handoff_id"],
                    "task_id": handoff["from_task_id"],
                    "created_at": handoff["created_at"],
                    "label": f"{handoff['from_task_id']} -> {handoff['to_task_id']}: {handoff['summary']}",
                }
            )
        for attempt in attempt_rows:
            metadata = companyctl.attempt_json_field(attempt, "metadata_json")
            runtime_policy = companyctl.attempt_json_field(attempt, "runtime_policy_json")
            previous_attempt_id = str(metadata.get("previous_attempt_id", "") or "")
            attempt_chain = [previous_attempt_id, attempt["attempt_id"]] if previous_attempt_id else [attempt["attempt_id"]]
            spans.append(
                {
                    "name": f"attempt.{attempt['status']}",
                    "service": attempt["employee_id"] or "attempt",
                    "duration_ms": milliseconds_between(attempt["started_at"], attempt.get("finished_at") or attempt["started_at"]) if attempt.get("finished_at") else 24,
                    "start_ms": milliseconds_between(start, attempt["started_at"]),
                    "attempt_id": attempt["attempt_id"],
                    "task_id": attempt["task_id"],
                    "adapter_type": attempt["adapter_type"],
                    "previous_attempt_id": previous_attempt_id,
                    "attempt_chain": attempt_chain,
                    "runtime_policy": runtime_policy,
                    "created_at": attempt["started_at"],
                    "label": attempt["adapter_type"],
                }
            )
        spans.sort(key=lambda item: (item.get("start_ms", 0), item.get("event_sequence", 999999), item.get("name", "")))
        first = spans[0] if spans else {}
        traces.append(
            {
                "trace_id": trace_id,
                "title": first.get("task_id") or first.get("name") or trace_id,
                "duration_ms": max(duration, max((span["start_ms"] + span["duration_ms"] for span in spans), default=1)),
                "spans": spans,
                "counts": {
                    "events": len(event_rows),
                    "adapter_runs": len(run_rows),
                    "artifacts": len(artifact_rows),
                    "handoffs": len(handoff_rows),
                    "evidence": len(evidence_rows),
                    "execution_attempts": len(attempt_rows),
                },
                "started_at": start,
                "updated_at": end,
            }
        )
    return traces


def long_task_state(attempt: dict, *, generated_at: str) -> dict:
    return companyctl.long_task_state_for_attempt(attempt, generated_at=generated_at)


def build_cockpit_summary(summary: dict) -> dict:
    generated_at = str(summary.get("generated_at") or now())
    employees = summary.get("employees", [])
    active_attempts = summary.get("active_attempts", [])
    chat_counts = chat_classification_counts(summary.get("direct_messages_recent", []))
    tasks_by_id = {str(task.get("id", "")): task for task in summary.get("tasks", [])}
    progress_by_task = {}
    for progress in companyctl.task_progress_events(summary.get("events", [])):
        task_id = str(progress.get("task_id", ""))
        if task_id and task_id not in progress_by_task:
            progress_by_task[task_id] = progress
    long_tasks = []
    for attempt in active_attempts:
        task_id = str(attempt.get("task_id", ""))
        task = tasks_by_id.get(task_id, {})
        state = long_task_state(attempt, generated_at=generated_at)
        evidence = companyctl.sanitize_evidence_path_for_display(str(task.get("evidence_path") or ""))
        supervisor_state = companyctl.attempt_json_field(dict(attempt), "supervisor_state_json")
        last_correction = supervisor_state.get("last_correction", {}) if isinstance(supervisor_state.get("last_correction", {}), dict) else {}
        requested = int(supervisor_state.get("corrections_requested", 0) or 0)
        acknowledged = int(supervisor_state.get("corrections_acknowledged", 0) or 0)
        correction = {
            "requested": requested,
            "acknowledged": acknowledged,
            "needs_ack": requested > acknowledged,
            "last_by": str(last_correction.get("by", "") or ""),
            "last_message": str(last_correction.get("message", "") or ""),
            "last_created_at": str(last_correction.get("created_at", "") or ""),
        }
        long_tasks.append(
            {
                "task_id": attempt.get("task_id", ""),
                "title": task.get("title", ""),
                "target_agent": task.get("target_agent", attempt.get("employee_id", "")),
                "attempt_id": attempt.get("attempt_id", ""),
                "trace_id": attempt.get("trace_id", ""),
                "attempt_status": attempt.get("status", ""),
                "task_status": task.get("status", ""),
                "started_at": attempt.get("started_at", ""),
                "last_heartbeat_at": attempt.get("last_heartbeat_at", ""),
                "last_progress_at": attempt.get("last_progress_at", ""),
                "blocker": task.get("blocker", "") or attempt.get("error_message", ""),
                "evidence": evidence,
                "correction": correction,
                "latest_progress": progress_by_task.get(task_id, {}),
                **state,
            }
        )
    pending_approvals = [item for item in summary.get("approvals", []) if str(item.get("status", "")).lower() == "pending"]
    approval_details = {}
    pending_approval_task_ids = set()
    for approval in pending_approvals:
        raw = approval.get("reason", "")
        try:
            detail = json.loads(raw or "{}")
        except json.JSONDecodeError:
            detail = {}
        if isinstance(detail, dict):
            approval_details[str(approval.get("id", ""))] = detail
            metadata = detail.get("metadata", {})
            if isinstance(metadata, dict) and metadata.get("task_id"):
                pending_approval_task_ids.add(str(metadata["task_id"]))
    legacy_task_evidence = []
    for task in summary.get("tasks", []):
        evidence = companyctl.sanitize_evidence_path_for_display(str(task.get("evidence_path") or ""))
        if evidence.get("allowed"):
            legacy_task_evidence.append(
                {
                    "task_id": task.get("id", ""),
                    "title": task.get("title", ""),
                    "status": task.get("status", ""),
                    "target_agent": task.get("target_agent", ""),
                    "updated_at": task.get("updated_at", ""),
                    "evidence": evidence,
                }
            )
    recent_evidence = []
    for item in summary.get("evidence_records", []):
        evidence = item.get("display") if isinstance(item.get("display"), dict) else companyctl.sanitize_evidence_path_for_display(str(item.get("path_or_url") or ""))
        if evidence.get("allowed") and item.get("is_final"):
            recent_evidence.append(
                {
                    "evidence_id": item.get("evidence_id", ""),
                    "task_id": item.get("task_id", ""),
                    "title": tasks_by_id.get(str(item.get("task_id", "")), {}).get("title", ""),
                    "status": tasks_by_id.get(str(item.get("task_id", "")), {}).get("status", ""),
                    "target_agent": item.get("employee_id", ""),
                    "updated_at": item.get("created_at", ""),
                    "summary": item.get("summary", ""),
                    "artifact_id": item.get("artifact_id", ""),
                    "attempt_id": item.get("attempt_id", ""),
                    "evidence": evidence,
                }
            )
    owner_attention = []
    def attention_actions(kind: str, *, task_id: str = "", attempt_id: str = "", approval_id: str = "") -> list[dict]:
        base = {"task_id": task_id, "attempt_id": attempt_id, "approval_id": approval_id}
        def action(action_id: str, label: str, api: str, *, method: str = "GET", requires_owner_approval: bool = False, dry_run_default: bool = True, dangerous: bool = False) -> dict:
            return {
                **base,
                "id": action_id,
                "label": label,
                "api": api,
                "method": method,
                "requires_owner_approval": requires_owner_approval,
                "dry_run_default": dry_run_default,
                "dangerous": dangerous,
            }
        if kind == "stagnant_task":
            return [
                action("send_correction", "Request Hermes correction", f"/v1/tasks/{task_id}/correct", method="POST", requires_owner_approval=True),
                action("view_logs", "View sanitized logs", f"/v1/tasks/{task_id}"),
                action("wait", "Keep waiting", "", method="none"),
                action("cancel_attempt", "Cancel attempt", f"/v1/tasks/{task_id}/cancel", method="POST", requires_owner_approval=True, dangerous=True),
            ]
        if kind == "blocked_task":
            return [
                action("send_correction", "Send correction", f"/v1/tasks/{task_id}/correct", method="POST", requires_owner_approval=True),
                action("view_logs", "View sanitized logs", f"/v1/tasks/{task_id}"),
                action("retry", "Retry / reassign", f"/v1/tasks/{task_id}/retry", method="POST", requires_owner_approval=True),
                action("reassign", "Reassign employee", f"/v1/tasks/{task_id}/reassign", method="POST", requires_owner_approval=True),
            ]
        if kind == "approval":
            return [
                action("approve", "Approve", f"/v1/approvals/{approval_id}/approve", method="POST", requires_owner_approval=True, dry_run_default=False, dangerous=True),
                action("deny", "Deny", f"/v1/approvals/{approval_id}/deny", method="POST", requires_owner_approval=True),
                action("mock_resolve", "Mock resolve dry-run", f"/v1/approvals/{approval_id}/resolve", method="POST"),
            ]
        if kind == "evidence":
            return [
                action("review_evidence", "Review evidence", f"/v1/tasks/{task_id}"),
                action("view_trace", "View trace", ""),
            ]
        if kind == "evidence_issue":
            return [
                action("review_task", "Review task", f"/v1/tasks/{task_id}"),
                action("view_trace", "View trace", ""),
            ]
        if kind == "employee_readiness":
            return [
                action("verify_runtime", "Verify runtime evidence", "/v1/agent-matrix", method="POST", requires_owner_approval=True),
                action("view_employee", "View employee", ""),
                action("keep_candidate", "Keep candidate", "", method="none"),
            ]
        return []

    for item in long_tasks:
        state = str(item.get("long_task_state") or "")
        if state in {"progress_stagnant", "correcting"}:
            task_id = str(item.get("task_id", ""))
            attempt_id = str(item.get("attempt_id", ""))
            correction = item.get("correction", {}) if isinstance(item.get("correction", {}), dict) else {}
            message = "员工仍在线，但 15 分钟没有新进度。可继续等待、发送探针、查看日志或请求 Hermes 纠偏。"
            if correction.get("needs_ack"):
                last_by = correction.get("last_by") or "Hermes"
                if str(last_by).lower() == "hermes":
                    last_by = "Hermes"
                last_message = correction.get("last_message") or "纠偏已发出"
                message = f"{last_by} 已发纠偏，等待员工确认：{last_message}"
            owner_attention.append(
                {
                    "kind": "stagnant_task",
                    "state": "correcting" if correction.get("needs_ack") else state,
                    "approval_id": "",
                    "task_id": task_id,
                    "approval_action": "",
                    "risk": "",
                    "title": item.get("title", ""),
                    "target_agent": item.get("target_agent", ""),
                    "attempt_id": attempt_id,
                    "trace_id": item.get("trace_id", ""),
                    "message": message,
                    "correction": correction,
                    "updated_at": item.get("last_progress_at") or item.get("started_at", ""),
                    "actions": attention_actions("stagnant_task", task_id=task_id, attempt_id=attempt_id),
                }
            )
        elif state in {"heartbeat_stale", "blocked", "failed", "stale"}:
            task_id = str(item.get("task_id", ""))
            attempt_id = str(item.get("attempt_id", ""))
            owner_attention.append(
                {
                    "kind": "blocked_task",
                    "state": state,
                    "approval_id": "",
                    "task_id": task_id,
                    "approval_action": "",
                    "risk": "",
                    "title": item.get("title", ""),
                    "target_agent": item.get("target_agent", ""),
                    "attempt_id": attempt_id,
                    "trace_id": item.get("trace_id", ""),
                    "message": item.get("blocker") or "任务需要人工检查：查看日志、纠偏、取消或重新分配。",
                    "updated_at": item.get("last_progress_at") or item.get("started_at", ""),
                    "actions": attention_actions("blocked_task", task_id=task_id, attempt_id=attempt_id),
                }
            )
    supervisor_activity = []
    for item in long_tasks:
        correction = item.get("correction", {}) if isinstance(item.get("correction", {}), dict) else {}
        if correction.get("needs_ack"):
            last_by = str(correction.get("last_by") or "Hermes")
            supervisor = "Hermes" if last_by.lower() == "hermes" else last_by
            supervisor_activity.append(
                {
                    "kind": "correction_pending_ack",
                    "supervisor": supervisor,
                    "target_agent": item.get("target_agent", ""),
                    "task_id": item.get("task_id", ""),
                    "attempt_id": item.get("attempt_id", ""),
                    "trace_id": item.get("trace_id", ""),
                    "state": item.get("long_task_state", ""),
                    "message": correction.get("last_message") or "纠偏已发出，等待员工确认。",
                    "updated_at": correction.get("last_created_at") or item.get("last_progress_at") or item.get("started_at", ""),
                }
            )
        elif item.get("long_task_state") == "progress_stagnant":
            supervisor_activity.append(
                {
                    "kind": "stagnant_check",
                    "supervisor": "Hermes",
                    "target_agent": item.get("target_agent", ""),
                    "task_id": item.get("task_id", ""),
                    "attempt_id": item.get("attempt_id", ""),
                    "trace_id": item.get("trace_id", ""),
                    "state": item.get("long_task_state", ""),
                    "message": "heartbeat fresh but progress stagnant; correction or probe recommended.",
                    "updated_at": item.get("last_progress_at") or item.get("started_at", ""),
                }
            )
    supervisor_loop = summary.get("supervisor_loop", {}) if isinstance(summary.get("supervisor_loop", {}), dict) else {}
    if supervisor_loop:
        loop_counts = supervisor_loop.get("counts", {}) if isinstance(supervisor_loop.get("counts", {}), dict) else {}
        supervisor_activity.append(
            {
                "kind": "supervisor_loop",
                "supervisor": str(supervisor_loop.get("actor") or "Hermes"),
                "target_agent": "",
                "task_id": "",
                "attempt_id": "",
                "trace_id": "",
                "state": "observed",
                "message": f"latest supervisor loop scanned={loop_counts.get('scanned', 0)} sent={loop_counts.get('sent', 0)} failed={loop_counts.get('failed', 0)}",
                "updated_at": str(supervisor_loop.get("completed_at") or ""),
            }
        )
    supervisor_activity.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    for task in summary.get("tasks", []):
        status = str(task.get("status", "")).lower()
        if status in {"blocked", "failed", "stale"}:
            task_id = str(task.get("id", ""))
            owner_attention.append(
                {
                    "kind": "blocked_task",
                    "state": status,
                    "approval_id": "",
                    "task_id": task_id,
                    "approval_action": "",
                    "risk": "",
                    "title": task.get("title", ""),
                    "target_agent": task.get("target_agent", ""),
                    "attempt_id": "",
                    "trace_id": task.get("trace_id", ""),
                    "message": task.get("blocker") or task.get("summary") or "任务已阻塞或失败，需要人工处理。",
                    "updated_at": task.get("updated_at", ""),
                    "actions": attention_actions("blocked_task", task_id=task_id),
                }
            )
    for item in pending_approvals[:10]:
        approval_id = str(item.get("id", ""))
        detail = approval_details.get(approval_id, {})
        metadata = detail.get("metadata", {}) if isinstance(detail.get("metadata", {}), dict) else {}
        task_id = str(metadata.get("task_id") or item.get("task_id", "") or "")
        target_agent = str(detail.get("target") or item.get("target_agent", "") or "")
        risk = str(detail.get("risk") or item.get("risk", "") or "")
        request_reason = str(detail.get("request_reason") or item.get("reason", "") or "")
        owner_attention.append(
            {
                "kind": "approval",
                "state": "blocked",
                "approval_id": approval_id,
                "task_id": task_id,
                "approval_action": item.get("action", ""),
                "risk": risk,
                "title": item.get("action") or item.get("id", ""),
                "target_agent": target_agent,
                "attempt_id": "",
                "trace_id": "",
                "message": f"需要 owner approval；真实外部发送保持 dry-run，直到人工批准。{request_reason}".strip(),
                "updated_at": item.get("updated_at", ""),
                "actions": attention_actions("approval", task_id=task_id, approval_id=approval_id),
            }
        )
    for item in recent_evidence[:10]:
        task_id = str(item.get("task_id", ""))
        owner_attention.append(
            {
                "kind": "evidence",
                "state": "success",
                "approval_id": "",
                "task_id": task_id,
                "approval_action": "",
                "risk": "",
                "title": item.get("title", ""),
                "target_agent": item.get("target_agent", ""),
                "attempt_id": "",
                "trace_id": "",
                "message": f"Final evidence 可验收：{item.get('evidence', {}).get('relative_path', '')}",
                "updated_at": item.get("updated_at", ""),
                "evidence": item.get("evidence", {}),
                "actions": attention_actions("evidence", task_id=task_id),
            }
        )
    evidence_issues = summary.get("evidence_health", {}).get("issues", [])
    if not isinstance(evidence_issues, list):
        evidence_issues = []
    for issue in evidence_issues[:10]:
        task_id = str(issue.get("task_id", ""))
        owner_attention.append(
            {
                "kind": "evidence_issue",
                "state": "blocked",
                "approval_id": "",
                "task_id": task_id,
                "approval_action": "",
                "risk": "P0",
                "title": task_id,
                "target_agent": issue.get("agent", ""),
                "attempt_id": "",
                "trace_id": "",
                "reason": issue.get("reason", ""),
                "message": f"任务 done 但缺少 final evidence：{issue.get('reason', '')}",
                "updated_at": "",
                "actions": attention_actions("evidence_issue", task_id=task_id),
            }
        )
    for employee in employees:
        employee_id = str(employee.get("id", "") or "")
        employee_status = str(employee.get("employee_status") or employee.get("status") or "")
        readiness_level = str(employee.get("readiness_level", "") or "")
        if employee_status == "candidate":
            readiness_level = "candidate_only"
        elif not readiness_level:
            if employee_status == "active":
                readiness_level = "online_only"
            elif employee_status:
                readiness_level = "task_unsupported"
        if readiness_level not in {"candidate_only", "online_only", "unsafe", "task_unsupported"}:
            continue
        runtime = str(employee.get("runtime", "") or "")
        reason = str(employee.get("readiness_reason", "") or "")
        if not reason and readiness_level == "candidate_only":
            reason = "candidate_requires_structured_runtime_evidence_before_activation"
        if readiness_level == "candidate_only":
            message = "员工仍是 candidate，只可评审或 smoke；必须有结构化 execution evidence 后才能参与自动派工。"
        elif readiness_level == "online_only":
            message = "员工在线但缺少 runtime/task/evidence 闭环；不要把在线心跳当作可交付能力。"
        elif readiness_level == "unsafe":
            message = "员工 readiness unsafe；禁止自动派工，先修复安全或能力证据。"
        else:
            message = "员工暂不支持任务闭环；只能保留观察或人工评审。"
        owner_attention.append(
            {
                "kind": "employee_readiness",
                "state": readiness_level,
                "approval_id": "",
                "task_id": "",
                "employee_id": employee_id,
                "approval_action": "",
                "risk": "P1",
                "title": employee.get("name") or employee_id,
                "target_agent": employee_id,
                "runtime": runtime,
                "attempt_id": "",
                "trace_id": "",
                "message": f"{message} reason={reason}".strip(),
                "updated_at": employee.get("last_seen_at") or generated_at,
                "actions": attention_actions("employee_readiness"),
            }
        )
    owner_attention.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    employee_states = []
    for employee in employees:
        status = str(employee.get("employee_status") or employee.get("status") or "")
        heartbeat = str(employee.get("heartbeat_status") or "missing")
        active_state = status
        if status == "active" and employee.get("current_attempt"):
            active_state = "busy"
        elif status == "active" and employee.get("runtime") == "antigravity":
            active_state = "active-limited"
        if heartbeat in {"stale", "missing", "offline"}:
            active_state = "abnormal" if status == "active" else status
        employee_states.append(
            {
                "id": employee.get("id", ""),
                "name": employee.get("name", ""),
                "runtime": employee.get("runtime", ""),
                "role": employee.get("role", ""),
                "status": active_state,
                "employee_status": status,
                "readiness_level": employee.get("readiness_level", active_state),
                "readiness_reason": employee.get("readiness_reason", ""),
                "readiness_checks": employee.get("readiness_checks", {}),
                "heartbeat_status": heartbeat,
                "progress_layer": employee.get("progress_layer", ""),
                "progress_state": employee.get("progress_state", ""),
                "current_attempt": employee.get("current_attempt", {}),
                "last_seen_at": employee.get("last_seen_at", ""),
            }
        )
    employees_total = len(employee_states)
    employees_online = sum(
        1
        for item in employee_states
        if str(item.get("heartbeat_status") or "") not in {"", "missing", "offline"}
        and seconds_since(str(item.get("last_seen_at") or ""), generated_at) < 15 * 60
    )
    employees_abnormal = sum(1 for item in employee_states if str(item.get("status") or "") == "abnormal")
    employee_status_counts: dict[str, int] = {}
    readiness_counts: dict[str, int] = {}
    for item in employee_states:
        employee_status = str(item.get("status") or "unknown")
        readiness_level = str(item.get("readiness_level") or "unknown")
        employee_status_counts[employee_status] = employee_status_counts.get(employee_status, 0) + 1
        readiness_counts[readiness_level] = readiness_counts.get(readiness_level, 0) + 1
    return {
        "ok": True,
        "generated_at": generated_at,
        "refresh": {"mode": "rest_polling", "interval_seconds": 10, "sse_reserved": True, "websocket": False},
        "ledger_consistency": {
            "source": "single_company_kernel_ledger",
            "surfaces": ["api", "cli", "dashboard"],
            "summary": "API / CLI / Dashboard read the same Company Kernel ledger",
        },
        "status_contract": {
            "timeout": "sync_wait_only",
            "progress_stagnant": "heartbeat fresh but no progress beyond stale_after_seconds; do not auto-cancel",
            "correction_binding": "task_id + attempt_id",
        },
        "counts": {
            "employees": employees_total,
            "employees_total": employees_total,
            "employees_online": employees_online,
            "employees_abnormal": employees_abnormal,
            "employee_status_counts": employee_status_counts,
            "readiness_counts": readiness_counts,
            "active_attempts": len(active_attempts),
            "running_tasks": sum(1 for item in summary.get("tasks", []) if str(item.get("status", "")).lower() in {"claimed", "running"}),
            "stagnant_tasks": sum(1 for item in long_tasks if item.get("long_task_state") == "progress_stagnant"),
            "blocked_tasks": sum(1 for item in summary.get("tasks", []) if str(item.get("status", "")).lower() in {"blocked", "failed", "stale"}),
            "done_tasks": sum(1 for item in summary.get("tasks", []) if str(item.get("status", "")).lower() in {"completed", "done"}),
            "awaiting_approval_tasks": sum(
                1
                for item in summary.get("tasks", [])
                if str(item.get("id", "")) in pending_approval_task_ids
                and str(item.get("status", "")).lower() not in {"completed", "done", "cancelled"}
            ),
            "pending_approvals": len(pending_approvals),
            "recent_evidence": len(recent_evidence),
            "legacy_task_evidence": len(legacy_task_evidence),
            "evidence_issues": len(evidence_issues),
            "chat_task_bound": chat_counts["task_bound"],
            "chat_work_relevant": chat_counts["work_relevant"],
            "chat_handshake_or_idle": chat_counts["handshake_or_idle"],
        },
        "employees": employee_states,
        "long_tasks": long_tasks,
        "supervisor_activity": supervisor_activity[:10],
        "owner_attention": owner_attention[:20],
        "pending_approvals": pending_approvals[:10],
        "recent_evidence": recent_evidence[:10],
        "legacy_task_evidence": legacy_task_evidence[:10],
    }


def recent_direct_messages(conn: sqlite3.Connection, *, limit: int = 20) -> list[dict]:
    messages = rows(
        conn,
        """
        SELECT id,
               source_agent,
               target_agent,
               body,
               '' AS evidence_path,
               created_at
        FROM messages
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    )
    for message in messages:
        task_context = extract_task_context_from_chat_item(message)
        low_signal = is_low_signal_chat_message(message, task_context=task_context)
        message["task_context"] = task_context
        message["task_bound"] = bool(task_context)
        message["low_signal"] = low_signal
        message["chat_classification"] = "task_bound" if task_context else ("handshake_or_idle" if low_signal else "work_relevant")
    return messages


def extract_task_context_from_chat_item(item: dict) -> str:
    direct = str(item.get("task_id") or item.get("taskId") or "").strip()
    if direct:
        return direct
    text = " ".join(str(item.get(key, "") or "") for key in ("title", "body", "evidence_path", "id", "conversation_id"))
    match = re.search(r"\b(task[-_:][A-Za-z0-9._-]+|TASK[-_:][A-Za-z0-9._-]+)\b", text)
    return match.group(1) if match else ""


def is_low_signal_chat_message(item: dict, *, task_context: str = "") -> bool:
    body = str(item.get("body") or "").strip()
    if not body:
        return True
    if task_context:
        return False
    if re.search(r"(attempt_id|task_id|trace_id|evidence|artifact|handoff|progress|blocked|failed|completed|done|stale|correction|approval)", body, re.IGNORECASE):
        return False
    if len(body) > 160:
        return False
    if re.search(r"^(hi|hello|hey|ping|pong|ack|ok|okay|thanks|thank you|收到|在|在线|你好|谢谢|感谢|早上好|晚上好|direct channel opened|dashboard direct ui ping|rest ping|smoke|handshake|greeting|idle|round\s*\d+|direct_ok|message direct ok)[\s.!。！]*$", body, re.IGNORECASE):
        return True
    return bool(re.search(r"\b(handshake|greeting|idle chatter|direct_ok|message direct ok)\b", body, re.IGNORECASE))


def chat_classification_counts(items: list[dict]) -> dict[str, int]:
    counts = {"task_bound": 0, "work_relevant": 0, "handshake_or_idle": 0}
    for item in items:
        classification = str(item.get("chat_classification") or "").strip()
        if not classification:
            task_context = extract_task_context_from_chat_item(item)
            classification = "task_bound" if task_context else ("handshake_or_idle" if is_low_signal_chat_message(item, task_context=task_context) else "work_relevant")
        if classification not in counts:
            classification = "work_relevant"
        counts[classification] += 1
    return counts


def internal_communication_watchdog(conn: sqlite3.Connection, *, generated_at: str, limit: int = 20) -> dict:
    """Find internal messages/tasks that were delivered but have no visible work receipt yet."""
    message_rows = rows(
        conn,
        """
        SELECT m.id,
               m.source_agent,
               m.target_agent,
               m.body,
               m.created_at,
               COALESCE((
                 SELECT r.id
                 FROM messages r
                 WHERE r.source_agent = m.target_agent
                   AND r.target_agent = m.source_agent
                   AND r.created_at >= m.created_at
                   AND (
                     r.id = m.id || '-receipt'
                     OR r.body LIKE '%original_message_id: ' || m.id || '%'
                   )
                 ORDER BY r.created_at ASC, r.id ASC
                 LIMIT 1
               ), '') AS receipt_id,
               COALESCE((
                 SELECT r.created_at
                 FROM messages r
                 WHERE r.source_agent = m.target_agent
                   AND r.target_agent = m.source_agent
                   AND r.created_at >= m.created_at
                   AND (
                     r.id = m.id || '-receipt'
                     OR r.body LIKE '%original_message_id: ' || m.id || '%'
                   )
                 ORDER BY r.created_at ASC, r.id ASC
                 LIMIT 1
               ), '') AS receipt_at
        FROM messages m
        WHERE m.source_agent != m.target_agent
        ORDER BY m.created_at DESC, m.id DESC
        LIMIT ?
        """,
        (limit,),
    )
    no_receipt = []
    for item in message_rows:
        if item.get("receipt_id"):
            continue
        age = minutes_since(str(item.get("created_at", "")), generated_at)
        item["age_min"] = age
        item["status"] = "no_receipt"
        item["reason"] = "message_delivered_but_no_reverse_receipt"
        no_receipt.append(item)

    task_rows = rows(
        conn,
        """
        SELECT id, source_agent, target_agent, title, status, claimed_by, evidence_path, created_at, updated_at
        FROM tasks
        WHERE status IN ('submitted', 'claimed')
        ORDER BY updated_at DESC, created_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    stalled_tasks = []
    for task in task_rows:
        age = minutes_since(str(task.get("updated_at") or task.get("created_at") or ""), generated_at)
        task["age_min"] = age
        if task.get("status") == "submitted":
            task["watchdog_status"] = "unclaimed"
            task["reason"] = "task_submitted_but_not_claimed"
        else:
            task["watchdog_status"] = "claimed_no_final_receipt"
            task["reason"] = "task_claimed_but_not_done_or_blocked"
        stalled_tasks.append(task)

    return {
        "counts": {
            "messages_checked": len(message_rows),
            "no_receipt_messages": len(no_receipt),
            "open_tasks": len(stalled_tasks),
            "remediation_candidates": len(no_receipt) + len(stalled_tasks),
        },
        "no_receipt_messages": no_receipt[:8],
        "open_tasks": stalled_tasks[:8],
    }


def remediation_followup_exists(followup_id: str) -> bool:
    return any((companyctl.followup_paths(status) / f"{followup_id}.json").exists() for status in ("pending", "answered", "cancelled"))


def parse_key_value_text(text: str) -> dict:
    parsed = {}
    for line in str(text or "").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        normalized = key.strip().lower().replace("-", "_").replace(" ", "_")
        if normalized:
            parsed[normalized] = value.strip()
    return parsed


def reroute_candidate_from_followup(followup: dict) -> str:
    answer = parse_key_value_text(str(followup.get("answer", "")))
    if answer.get("new_owner"):
        return answer["new_owner"]
    if answer.get("candidate_new_owner"):
        return answer["candidate_new_owner"]
    try:
        context = json.loads(followup.get("context", "{}") or "{}")
    except json.JSONDecodeError:
        context = {}
    candidate = context.get("candidate_new_owner")
    if candidate:
        return str(candidate)
    parent = context.get("parent_action", {}) if isinstance(context, dict) else {}
    if isinstance(parent, dict) and parent.get("candidate_new_owner"):
        return str(parent["candidate_new_owner"])
    return "codex"


def original_task_or_message_from_followup(followup: dict) -> tuple[str, str, dict]:
    try:
        context = json.loads(followup.get("context", "{}") or "{}")
    except json.JSONDecodeError:
        context = {}
    item = context.get("watchdog_item", {}) if isinstance(context, dict) else {}
    if not isinstance(item, dict):
        item = {}
    if item.get("id") and item.get("title") is not None:
        return "task", str(item.get("id", "")), item
    if item.get("id"):
        return "message", str(item.get("id", "")), item
    answer = parse_key_value_text(str(followup.get("answer", "")))
    if answer.get("task_id"):
        return "task", answer["task_id"], item
    if answer.get("original_message_id"):
        return "message", answer["original_message_id"], item
    return "unknown", "", item


def apply_reroute_decisions(conn: sqlite3.Connection, *, by: str = "hermes", dry_run: bool = True) -> dict:
    actions = []
    for followup in companyctl.list_followups("answered"):
        followup_id = str(followup.get("id", ""))
        if not followup_id.startswith("reroute-"):
            continue
        answer = parse_key_value_text(str(followup.get("answer", "")))
        decision = str(answer.get("decision", "")).strip().lower()
        if decision != "reroute":
            actions.append({"followup_id": followup_id, "decision": decision or "missing", "status": "skipped"})
            continue
        new_owner = companyctl.resolve_employee_alias(answer.get("new_owner") or reroute_candidate_from_followup(followup))
        item_kind, original_id, item = original_task_or_message_from_followup(followup)
        if not original_id:
            actions.append({"followup_id": followup_id, "decision": decision, "status": "blocked", "reason": "missing_original_id"})
            continue
        new_task_id = f"rerouted-{original_id}".replace("/", "-")
        existing = conn.execute("SELECT * FROM tasks WHERE id = ?", (new_task_id,)).fetchone()
        title = f"Rerouted: {item.get('title') or item.get('body') or original_id}"[:180]
        description = "\n".join(
            [
                "# Rerouted Internal Work",
                f"- original_kind: {item_kind}",
                f"- original_id: {original_id}",
                f"- stalled_target: {item.get('target_agent', '')}",
                f"- reroute_decision_followup: {followup_id}",
                f"- reason: {answer.get('reason', followup.get('answer', ''))}",
                "",
                "## Original Context",
                json.dumps(item, ensure_ascii=False, indent=2),
            ]
        )
        action = {
            "followup_id": followup_id,
            "decision": decision,
            "original_kind": item_kind,
            "original_id": original_id,
            "new_owner": new_owner,
            "new_task_id": new_task_id,
            "dry_run": dry_run,
            "already_exists": bool(existing),
        }
        if not dry_run and not existing:
            submitted = companyctl.submit_task_internal(
                conn,
                source=by,
                target=new_owner,
                title=title,
                description=description,
                priority="P2",
                task_id=new_task_id,
                metadata={"rerouted_from": original_id, "reroute_followup_id": followup_id, "original_kind": item_kind},
            )
            action["new_task"] = submitted["task"]
            action["file"] = submitted["file"]
            if item_kind == "task":
                ts = now()
                conn.execute(
                    "UPDATE tasks SET status = 'blocked', blocker = ?, updated_at = ? WHERE id = ? AND status IN ('submitted', 'claimed')",
                    (f"rerouted_to:{new_owner}; new_task:{new_task_id}; followup:{followup_id}", ts, original_id),
                )
                companyctl.update_task_metadata(conn, original_id, {"rerouted_to": new_owner, "rerouted_task_id": new_task_id, "reroute_followup_id": followup_id})
                companyctl.sync_project_plan_for_task(conn, task_id=original_id, task_status="blocked", actor=by)
                conn.commit()
        actions.append(action)
    return {
        "ok": True,
        "dry_run": dry_run,
        "by": by,
        "actions": actions,
        "actions_planned": len(actions),
        "reroutes_applied": len([action for action in actions if action.get("new_task")]),
    }


def remediate_internal_watchdog(
    conn: sqlite3.Connection,
    *,
    source_agent: str = "main",
    dry_run: bool = True,
    deliver: bool = False,
    escalate_to: str = "hermes",
    escalate_existing: bool = True,
    reroute_to: str = "codex",
    create_reroute_plan: bool = True,
) -> dict:
    generated_at = now()
    watchdog = internal_communication_watchdog(conn, generated_at=generated_at, limit=20)
    actions = []

    def maybe_escalate(original_action: dict, item: dict) -> None:
        if not original_action.get("already_exists") or not escalate_existing:
            return
        escalation_id = f"escalate-{original_action['followup_id']}"
        escalation_exists = remediation_followup_exists(escalation_id)
        escalation_question = "\n".join(
            [
                "status: watchdog_escalation",
                f"stalled_followup_id: {original_action['followup_id']}",
                f"stalled_target: {original_action.get('to', '')}",
                f"reason: {original_action.get('reason', '')}",
                "required_action: 请监督/改派/阻断该内部任务；返回 owner、next_action、evidence_path。",
                f"context: {json.dumps(item, ensure_ascii=False)}",
            ]
        )
        escalation = {
            "kind": "escalation",
            "reason": "existing_followup_still_unresolved",
            "followup_id": escalation_id,
            "from": source_agent,
            "to": escalate_to,
            "question": escalation_question,
            "already_exists": escalation_exists,
            "parent_followup_id": original_action["followup_id"],
        }
        if not dry_run and not escalation_exists:
            followup = {
                "id": escalation_id,
                "source_agent": source_agent,
                "target_agent": escalate_to,
                "question": escalation_question,
                "context": json.dumps({"watchdog_item": item, "parent_action": original_action}, ensure_ascii=False),
                "deliver": bool(deliver),
                "reply_channel": "",
                "reply_account": "",
                "reply_to": "",
                "created_at": generated_at,
                "answered_at": "",
                "answer": "",
                "response_message_id": "",
            }
            path = companyctl.save_followup(followup, "pending")
            escalation["file"] = str(path)
        actions.append(escalation)

        if create_reroute_plan:
            reroute_id = f"reroute-{original_action['followup_id']}"
            reroute_exists = remediation_followup_exists(reroute_id)
            reroute_question = "\n".join(
                [
                    "status: reroute_decision_required",
                    f"stalled_followup_id: {original_action['followup_id']}",
                    f"stalled_target: {original_action.get('to', '')}",
                    f"candidate_new_owner: {reroute_to}",
                    "decision_required: continue_original | reroute | block | ask_human",
                    "required_output: decision/new_owner/reason/evidence_path/next_action/rollback.",
                    f"context: {json.dumps(item, ensure_ascii=False)}",
                ]
            )
            reroute = {
                "kind": "reroute_decision",
                "reason": "stalled_after_followup_needs_owner_decision",
                "followup_id": reroute_id,
                "from": source_agent,
                "to": escalate_to,
                "candidate_new_owner": reroute_to,
                "question": reroute_question,
                "already_exists": reroute_exists,
                "parent_followup_id": original_action["followup_id"],
            }
            if not dry_run and not reroute_exists:
                followup = {
                    "id": reroute_id,
                    "source_agent": source_agent,
                    "target_agent": escalate_to,
                    "question": reroute_question,
                    "context": json.dumps({"watchdog_item": item, "parent_action": original_action, "candidate_new_owner": reroute_to}, ensure_ascii=False),
                    "deliver": bool(deliver),
                    "reply_channel": "",
                    "reply_account": "",
                    "reply_to": "",
                    "created_at": generated_at,
                    "answered_at": "",
                    "answer": "",
                    "response_message_id": "",
                }
                path = companyctl.save_followup(followup, "pending")
                reroute["file"] = str(path)
            actions.append(reroute)

    for item in watchdog.get("no_receipt_messages", []):
        message_id = str(item.get("id", "message")).replace("/", "-")
        followup_id = f"remediate-no-receipt-{message_id}"
        question = "\n".join(
            [
                "status: no_receipt_followup",
                f"original_message_id: {item.get('id', '')}",
                f"from: {item.get('source_agent', '')}",
                f"to: {item.get('target_agent', '')}",
                "required_reply: 请返回 claimed/working/done/blocked；如果不能执行，返回 blocker/tried/evidence/next_action。",
                f"original_body: {item.get('body', '')}",
            ]
        )
        action = {"kind": "followup", "reason": item.get("reason", "no_receipt"), "followup_id": followup_id, "from": source_agent, "to": item.get("target_agent", ""), "question": question, "already_exists": remediation_followup_exists(followup_id)}
        if not dry_run and not action["already_exists"]:
            followup = {
                "id": followup_id,
                "source_agent": source_agent,
                "target_agent": item.get("target_agent", ""),
                "question": question,
                "context": json.dumps({"watchdog_item": item}, ensure_ascii=False),
                "deliver": bool(deliver),
                "reply_channel": "",
                "reply_account": "",
                "reply_to": "",
                "created_at": generated_at,
                "answered_at": "",
                "answer": "",
                "response_message_id": "",
            }
            path = companyctl.save_followup(followup, "pending")
            action["file"] = str(path)
        actions.append(action)
        maybe_escalate(action, item)

    for task in watchdog.get("open_tasks", []):
        task_id = str(task.get("id", "task")).replace("/", "-")
        followup_id = f"remediate-open-task-{task_id}"
        question = "\n".join(
            [
                "status: open_task_followup",
                f"task_id: {task.get('id', '')}",
                f"task_status: {task.get('status', '')}",
                f"watchdog_status: {task.get('watchdog_status', '')}",
                "required_reply: claim/start/finish or block this task; return evidence_path, exit_code/stdout/stderr, and next_action.",
                f"title: {task.get('title', '')}",
            ]
        )
        action = {"kind": "followup", "reason": task.get("reason", "open_task"), "followup_id": followup_id, "from": source_agent, "to": task.get("target_agent", ""), "question": question, "already_exists": remediation_followup_exists(followup_id)}
        if not dry_run and not action["already_exists"]:
            followup = {
                "id": followup_id,
                "source_agent": source_agent,
                "target_agent": task.get("target_agent", ""),
                "question": question,
                "context": json.dumps({"watchdog_item": task}, ensure_ascii=False),
                "deliver": bool(deliver),
                "reply_channel": "",
                "reply_account": "",
                "reply_to": "",
                "created_at": generated_at,
                "answered_at": "",
                "answer": "",
                "response_message_id": "",
            }
            path = companyctl.save_followup(followup, "pending")
            action["file"] = str(path)
        actions.append(action)
        maybe_escalate(action, task)

    return {
        "ok": True,
        "dry_run": dry_run,
        "deliver": deliver,
        "source_agent": source_agent,
        "escalate_to": escalate_to,
        "escalate_existing": escalate_existing,
        "reroute_to": reroute_to,
        "create_reroute_plan": create_reroute_plan,
        "generated_at": generated_at,
        "watchdog_counts": watchdog.get("counts", {}),
        "actions": actions,
        "actions_created": len([action for action in actions if action.get("file")]),
        "actions_planned": len(actions),
        "escalations_planned": len([action for action in actions if action.get("kind") == "escalation"]),
        "escalations_created": len([action for action in actions if action.get("kind") == "escalation" and action.get("file")]),
        "reroutes_planned": len([action for action in actions if action.get("kind") == "reroute_decision"]),
        "reroutes_created": len([action for action in actions if action.get("kind") == "reroute_decision" and action.get("file")]),
    }


def safe_repo_relative_path(value: str) -> str:
    if not value:
        return ""
    try:
        path = Path(value)
    except (TypeError, ValueError):
        return ""
    if path.is_absolute():
        try:
            resolved = path.resolve(strict=False)
            root_resolved = ROOT.resolve(strict=False)
            relative = resolved.relative_to(root_resolved)
            return relative.as_posix()
        except (RuntimeError, ValueError):
            return ""
    normalized = path.as_posix().lstrip("./")
    if normalized.startswith(".."):
        return ""
    return normalized


def progress_from_report_path(report_path: str) -> dict[str, str]:
    if not report_path:
        return companyctl.extract_progress_payload({})
    try:
        resolved = Path(report_path).expanduser().resolve()
        resolved.relative_to(ROOT.resolve(strict=False))
        payload = json.loads(resolved.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return companyctl.extract_progress_payload(payload.get("report", payload))
    except (OSError, ValueError, json.JSONDecodeError):
        return companyctl.extract_progress_payload({})
    return companyctl.extract_progress_payload({})


def communication_observability_summary(summary: dict) -> dict:
    direct_items = []
    for item in summary.get("direct_messages_recent", [])[:8]:
        direct_items.append(
            {
                "id": item.get("id", ""),
                "source_agent": item.get("source_agent", ""),
                "target_agent": item.get("target_agent", ""),
                "body": item.get("body", ""),
                "task_context": item.get("task_context", ""),
                "task_bound": bool(item.get("task_bound")),
                "low_signal": bool(item.get("low_signal")),
                "chat_classification": item.get("chat_classification", ""),
                "created_at": item.get("created_at", ""),
            }
        )

    external_threads = []
    for thread in summary.get("external_threads", [])[:8]:
        external_threads.append(
            {
                "id": thread.get("id", ""),
                "platform": thread.get("platform", ""),
                "owner_agent": thread.get("owner_agent", ""),
                "bridge_agent": thread.get("bridge_agent", ""),
                "external_title": thread.get("external_title", ""),
                "cursor": thread.get("cursor", ""),
                "last_message_at": thread.get("last_message_at", ""),
            }
        )

    adapter_items = []
    ok_count = 0
    failed_count = 0
    for run in summary.get("adapter_runs", [])[:8]:
        try:
            result = run.get("_result")
            if not isinstance(result, dict):
                result = json.loads(run.get("result_json", "{}") or "{}")
        except json.JSONDecodeError:
            result = {}
        parsed_runs = result.get("runs", []) if isinstance(result, dict) else []
        first_parsed = parsed_runs[0] if parsed_runs and isinstance(parsed_runs[0], dict) else {}
        parsed_stdout = first_parsed.get("parsed_stdout", {}) if isinstance(first_parsed, dict) else {}
        report_path = str(parsed_stdout.get("report", "")) if isinstance(parsed_stdout, dict) else ""
        progress = progress_from_report_path(report_path)
        if run.get("ok"):
            ok_count += 1
        else:
            failed_count += 1
        adapter_items.append(
            {
                "id": run.get("id", ""),
                "agent_id": run.get("agent_id", ""),
                "task_id": run.get("task_id", ""),
                "command": run.get("command", ""),
                "ok": bool(run.get("ok")),
                "processed": bool(run.get("processed")),
                "attempt": run.get("attempt", 0),
                "created_at": run.get("created_at", ""),
                "next_retry_at": run.get("next_retry_at", ""),
                "state_file": safe_repo_relative_path(result.get("state_file", "") if isinstance(result, dict) else ""),
                "progress_file": safe_repo_relative_path(parsed_stdout.get("progress_file", "") if isinstance(parsed_stdout, dict) else ""),
                "progress_layer": progress.get("layer", ""),
                "progress_state": progress.get("state", ""),
                "progress_label": progress.get("label", ""),
                "summary": parsed_stdout.get("summary", "") if isinstance(parsed_stdout, dict) else "",
                "sanitized_log": run.get("sanitized_log") or companyctl.summarize_adapter_result(result).get("sanitized_log", ""),
            }
        )

    progress_items = []
    pending_count = 0
    sent_count = 0
    skipped_count = 0
    failed_count = 0
    recent_progress = summary.get("progress_notifications_recent", [])[:8]
    for item in recent_progress:
        if item.get("pending"):
            pending_count += 1
        status = str(item.get("delivery_status", "") or "")
        if status == "sent":
            sent_count += 1
        elif status == "skipped":
            skipped_count += 1
        elif status == "failed":
            failed_count += 1
        progress_items.append(
            {
                "event_id": item.get("event_id", ""),
                "agent_id": item.get("agent_id", ""),
                "from_layer": item.get("from_layer", ""),
                "from_state": item.get("from_state", ""),
                "to_layer": item.get("to_layer", ""),
                "to_state": item.get("to_state", ""),
                "message": item.get("message", ""),
                "reason": item.get("reason", ""),
                "delivery_status": item.get("delivery_status", ""),
                "delivery_error": item.get("delivery_error", ""),
                "delivered_at": item.get("delivered_at", ""),
                "created_at": item.get("created_at", ""),
                "pending": bool(item.get("pending")),
                "supervisor_decision": item.get("supervisor_decision", ""),
                "supervisor_attempts": item.get("supervisor_attempts", 0),
                "supervisor_summary": item.get("supervisor_summary", ""),
            }
        )

    supervisor_loop = summary.get("supervisor_loop", {}) if isinstance(summary.get("supervisor_loop", {}), dict) else {}

    return {
        "generated_at": summary.get("generated_at", ""),
        "direct_messages": {
            "counts": {"total": len(summary.get("direct_messages_recent", [])), "shown": len(direct_items), **chat_classification_counts(summary.get("direct_messages_recent", []))},
            "items": direct_items,
        },
        "external_mirror": {
            "counts": {
                "threads": len(summary.get("external_threads", [])),
                "messages": len(summary.get("external_messages_recent", [])),
            },
            "threads": external_threads,
        },
        "adapter_runs": {
            "counts": {"total": len(summary.get("adapter_runs", [])), "shown": len(adapter_items), "ok": ok_count, "failed": failed_count},
            "items": adapter_items,
        },
        "progress_notifications": {
            "counts": {"total": len(summary.get("progress_notifications_recent", [])), "pending": pending_count, "sent": sent_count, "skipped": skipped_count, "failed": failed_count, "shown": len(progress_items)},
            "items": progress_items,
        },
        "supervisor_loop": {
            "latest_result": supervisor_loop,
            "counts": supervisor_loop.get("counts", {}) if isinstance(supervisor_loop, dict) else {},
        },
        "internal_watchdog": summary.get("internal_watchdog", {"counts": {}, "no_receipt_messages": [], "open_tasks": []}),
    }


def public_summary(summary: dict) -> dict:
    cleaned = dict(summary)
    cleaned_runs = []
    for run in cleaned.get("adapter_runs", []):
        run_copy = dict(run)
        run_copy.pop("result_json", None)
        run_copy.pop("_result", None)
        cleaned_runs.append(run_copy)
    cleaned["adapter_runs"] = cleaned_runs
    return cleaned


def load_summary(conn: sqlite3.Connection) -> dict:
    generated_at = now()
    direct_messages_recent = recent_direct_messages(conn, limit=20)
    conversation_rows = rows(
        conn,
        """
        SELECT c.id, c.title, c.created_by, c.status, c.updated_at,
               c.participants_json,
               COUNT(cm.id) AS message_count,
               COALESCE(MAX(cm.created_at), c.created_at) AS last_message_at
        FROM conversations c
        LEFT JOIN conversation_messages cm ON cm.conversation_id = c.id
        GROUP BY c.id
        ORDER BY c.updated_at DESC
        LIMIT 20
        """,
    )
    for conversation in conversation_rows:
        conversation["messages"] = rows(
            conn,
            """
            SELECT id, source_agent, body, evidence_path, created_at
            FROM conversation_messages
            WHERE conversation_id = ?
            ORDER BY created_at ASC
            LIMIT 80
            """,
            (conversation["id"],),
        )
    employee_rows = rows(
        conn,
        """
        SELECT e.id, e.name, e.role, e.runtime, e.status AS employee_status, e.workspace,
               COALESCE(h.status, 'missing') AS heartbeat_status,
               COALESCE(h.last_seen_at, '') AS last_seen_at,
               COALESCE(h.metadata_json, '{}') AS heartbeat_metadata_json,
               COALESCE(
                 (SELECT COUNT(*) FROM tasks t WHERE t.target_agent = e.id AND t.status = 'submitted'),
                 0
               ) AS submitted_tasks,
               COALESCE(
                 (SELECT COUNT(*) FROM tasks t WHERE t.target_agent = e.id AND t.status = 'claimed'),
                 0
               ) AS claimed_tasks
        FROM employees e
        LEFT JOIN heartbeats h ON h.agent_id = e.id
        ORDER BY
          CASE e.status WHEN 'active' THEN 0 WHEN 'candidate' THEN 1 ELSE 2 END,
          e.id
        """,
    )
    for employee in employee_rows:
        try:
            heartbeat_metadata = json.loads(employee.get("heartbeat_metadata_json", "{}") or "{}")
        except json.JSONDecodeError:
            heartbeat_metadata = {}
        progress = companyctl.extract_progress_payload(heartbeat_metadata)
        employee["progress_layer"] = progress.get("layer", "")
        employee["progress_state"] = progress.get("state", "")
        employee["progress_label"] = progress.get("label", "")
        employee["progress_summary"] = progress.get("summary", "")
        current_attempt = conn.execute(
            """
            SELECT attempt_id, trace_id, task_id, employee_id, adapter_type, runtime, pid, session_key,
                   status, runtime_policy_json, metadata_json, supervisor_state_json,
                   last_heartbeat_at, last_progress_at, started_at, finished_at, error_message
            FROM execution_attempts
            WHERE employee_id = ?
              AND status IN ('starting', 'running', 'correcting')
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (employee["id"],),
        ).fetchone()
        employee["current_attempt"] = dict(current_attempt) if current_attempt else {}

    adapter_runs = rows(conn, "SELECT * FROM adapter_runs ORDER BY created_at DESC LIMIT 20")
    for adapter_run in adapter_runs:
        raw_result = adapter_run.get("result_json", "{}")
        try:
            result = json.loads(raw_result or "{}")
        except json.JSONDecodeError:
            result = {"raw": raw_result}
        adapter_run["_result"] = result
        adapter_run["sanitized_log"] = companyctl.summarize_adapter_result(result).get("sanitized_log", "")

    return {
        "generated_at": generated_at,
        "runtime_health": {
            "daemon": companyctl.daemon_health(),
            "launchd": companyctl.launchd_health(),
            "openclaw_inventory": companyctl.openclaw_runtime_inventory(conn),
        },
        "evidence_health": {
            "issues": companyctl.task_evidence_issues(conn),
        },
        "counts": {
            "employees": scalar(conn, "SELECT COUNT(*) FROM employees"),
            "active_employees": scalar(conn, "SELECT COUNT(*) FROM employees WHERE status = 'active'"),
            "candidate_employees": scalar(conn, "SELECT COUNT(*) FROM employees WHERE status = 'candidate'"),
            "archived_employees": scalar(conn, "SELECT COUNT(*) FROM employees WHERE status = 'archived'"),
            "projects": scalar(conn, "SELECT COUNT(*) FROM projects"),
            "active_projects": scalar(conn, "SELECT COUNT(*) FROM projects WHERE status = 'active'"),
            "completed_projects": scalar(conn, "SELECT COUNT(*) FROM projects WHERE status = 'completed'"),
            "tasks": scalar(conn, "SELECT COUNT(*) FROM tasks"),
            "conversations": scalar(conn, "SELECT COUNT(*) FROM conversations"),
            "open_conversations": scalar(conn, "SELECT COUNT(*) FROM conversations WHERE status = 'open'"),
            "submitted_tasks": scalar(conn, "SELECT COUNT(*) FROM tasks WHERE status = 'submitted'"),
            "claimed_tasks": scalar(conn, "SELECT COUNT(*) FROM tasks WHERE status = 'claimed'"),
            "blocked_tasks": scalar(conn, "SELECT COUNT(*) FROM tasks WHERE status = 'blocked'"),
            "pending_approvals": scalar(conn, "SELECT COUNT(*) FROM approvals WHERE status = 'pending'"),
            "rfcs": scalar(conn, "SELECT COUNT(*) FROM rfcs"),
            "pending_rfcs": scalar(conn, "SELECT COUNT(*) FROM rfcs WHERE status = 'pending'"),
            "pending_events": scalar(conn, "SELECT COUNT(*) FROM company_events WHERE processed_at = ''"),
            "locks": scalar(conn, "SELECT COUNT(*) FROM locks"),
            "adapter_runs": scalar(conn, "SELECT COUNT(*) FROM adapter_runs"),
        },
        "task_status": status_counts(conn, "tasks"),
        "project_status": status_counts(conn, "projects"),
        "approval_status": status_counts(conn, "approvals"),
        "rfc_status": status_counts(conn, "rfcs"),
        "direct_messages_recent": direct_messages_recent,
        "progress_notifications_recent": companyctl.list_progress_notifications(conn, limit=20),
        "supervisor_loop": companyctl.load_latest_supervisor_loop_result(),
        "internal_watchdog": internal_communication_watchdog(conn, generated_at=generated_at, limit=20),
        "external_threads": rows(conn, "SELECT * FROM external_threads ORDER BY last_message_at DESC, updated_at DESC LIMIT 20"),
        "external_messages_recent": rows(conn, "SELECT * FROM external_messages ORDER BY created_at DESC, id DESC LIMIT 20"),
        "employees": employee_rows,
        "projects": rows(
            conn,
            """
            SELECT p.*,
                   COUNT(pt.task_id) AS task_count,
                   COALESCE(SUM(CASE WHEN t.status = 'completed' THEN 1 ELSE 0 END), 0) AS completed_tasks,
                   COALESCE(SUM(CASE WHEN t.status = 'blocked' THEN 1 ELSE 0 END), 0) AS blocked_tasks,
                   COALESCE(SUM(CASE WHEN t.status NOT IN ('completed', 'blocked') THEN 1 ELSE 0 END), 0) AS open_tasks,
                   (SELECT COUNT(*) FROM project_acceptances pa WHERE pa.project_id = p.id) AS acceptance_count,
                   (SELECT COUNT(*) FROM project_plan_items ppi WHERE ppi.project_id = p.id) AS plan_item_count,
                   (SELECT COUNT(*) FROM project_plan_items ppi WHERE ppi.project_id = p.id AND ppi.status NOT IN ('done', 'completed', 'cancelled')) AS open_plan_items,
                   COALESCE(
                       (
                         SELECT GROUP_CONCAT(
                           ppi.status || ':' || ppi.title ||
                           CASE WHEN ppi.task_id != '' THEN ' [' || ppi.task_id || '/' || COALESCE(t2.status, 'missing') || ']' ELSE '' END,
                           '; '
                         )
                         FROM project_plan_items ppi
                         LEFT JOIN tasks t2 ON t2.id = ppi.task_id
                         WHERE ppi.project_id = p.id
                         ORDER BY ppi.created_at ASC
                       ),
                       ''
                   ) AS plan_items,
                   COALESCE((SELECT pa.summary FROM project_acceptances pa WHERE pa.project_id = p.id ORDER BY pa.created_at DESC LIMIT 1), '') AS latest_acceptance_summary
            FROM projects p
            LEFT JOIN project_tasks pt ON pt.project_id = p.id
            LEFT JOIN tasks t ON t.id = pt.task_id
            GROUP BY p.id
            ORDER BY p.updated_at DESC
            LIMIT 20
            """,
        ),
        "tasks": rows(
            conn,
            """
            SELECT t.*
            FROM tasks t
            ORDER BY t.updated_at DESC, t.created_at DESC
            LIMIT 30
            """,
        ),
        "task_delegations": rows(
            conn,
            """
            SELECT parent.id AS parent_id,
                   parent.title AS parent_title,
                   parent.status AS parent_status,
                   parent.target_agent AS parent_owner,
                   COUNT(child.id) AS child_count,
                   COALESCE(SUM(CASE WHEN child.status = 'completed' THEN 1 ELSE 0 END), 0) AS completed_children,
                   COALESCE(SUM(CASE WHEN child.status = 'blocked' THEN 1 ELSE 0 END), 0) AS blocked_children,
                   COALESCE(SUM(CASE WHEN child.status NOT IN ('completed', 'blocked') THEN 1 ELSE 0 END), 0) AS open_children,
                   COALESCE(
                       GROUP_CONCAT(child.id || '/' || child.target_agent || '/' || child.status, '; '),
                       ''
                   ) AS child_summary,
                   MAX(child.updated_at) AS latest_child_update
            FROM task_relations tr
            JOIN tasks parent ON parent.id = tr.parent_task_id
            JOIN tasks child ON child.id = tr.child_task_id
            GROUP BY parent.id
            ORDER BY latest_child_update DESC, parent.updated_at DESC
            LIMIT 20
            """,
        ),
        "conversations": conversation_rows,
        "approvals": rows(conn, "SELECT * FROM approvals ORDER BY updated_at DESC LIMIT 20"),
        "rfcs": rows(conn, "SELECT * FROM rfcs ORDER BY updated_at DESC LIMIT 20"),
        "followups": companyctl.list_followups("all")[:20],
        "pending_events": rows(conn, "SELECT * FROM company_events WHERE processed_at = '' ORDER BY created_at ASC LIMIT 20"),
        "events": rows(conn, "SELECT * FROM company_events ORDER BY created_at DESC LIMIT 20"),
        "adapter_runs": adapter_runs,
        "evidence_records": companyctl.audit_evidence_records(conn, limit=50),
        "artifact_records": companyctl.audit_artifact_records(conn, limit=50),
        "handoff_records": companyctl.audit_handoff_records(conn, limit=50),
        "failure_records": companyctl.audit_failure_records(conn, limit=50),
        "active_attempts": rows(
            conn,
            """
            SELECT attempt_id, trace_id, task_id, employee_id, adapter_type, runtime, pid, session_key,
                   status, runtime_policy_json, metadata_json, supervisor_state_json,
                   last_heartbeat_at, last_progress_at, started_at, finished_at, error_message
            FROM execution_attempts
            WHERE status IN ('starting', 'running', 'correcting')
            ORDER BY started_at DESC
            LIMIT 50
            """,
        ),
        "locks": rows(conn, "SELECT * FROM locks ORDER BY updated_at DESC"),
        "traces": build_traces(conn),
    }


def pills(counts: dict[str, int]) -> str:
    return "".join(f"<span class='pill'>{e(k)}: <strong>{v}</strong></span>" for k, v in counts.items())


def render_table(headers: list[str], items: list[dict], fields: list[str]) -> str:
    head = "".join(f"<th>{e(h)}</th>" for h in headers)
    body = []
    for item in items:
        body.append("<tr>" + "".join(f"<td>{e(item.get(field, ''))}</td>" for field in fields) + "</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def render_employee_table(items: list[dict]) -> str:
    headers = ["id", "status", "kernel_state", "progress", "schedulable", "role", "runtime", "heartbeat", "age_min", "backlog", "skills", "tools", "task_types", "last_seen", "actions"]
    fields = ["id", "employee_status", "kernel_state", "progress_display", "schedulable", "role", "runtime", "heartbeat_status", "heartbeat_age_minutes", "backlog", "skills", "tools", "task_types", "last_seen_at"]
    head = "".join(f"<th>{e(header)}</th>" for header in headers)
    body = []
    for item in items:
        employee_id = e(item.get("id", ""))
        cells = "".join(f"<td>{e(item.get(field, ''))}</td>" for field in fields)
        actions = (
            "<td>"
            f"<button type='button' onclick=\"directMessageEmployee('{employee_id}')\">Direct</button> "
            f"<button type='button' onclick=\"editEmployee('{employee_id}')\">Edit</button> "
            f"<button class='danger-button' type='button' onclick=\"offboardEmployee('{employee_id}', false)\">Archive</button>"
            "</td>"
        )
        body.append(f"<tr>{cells}{actions}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def render_task_table(items: list[dict]) -> str:
    headers = ["id", "source", "target", "priority", "status", "claimed_by", "attempt", "evidence", "blocker", "approvals", "title", "updated", "actions"]
    fields = ["id", "source_agent", "target_agent", "priority", "status", "claimed_by", "attempt_display", "evidence", "blocker_detail", "approval_count", "title", "updated_at"]
    head = "".join(f"<th>{e(header)}</th>" for header in headers)
    body = []
    for item in items:
        task_id = e(item.get("id", ""))
        attempt_id = e(item.get("attempt_id", ""))
        trace_id = e(item.get("attempt_trace_id", "") or item.get("trace_id", ""))
        target = e(item.get("target_agent", ""))
        cells = "".join(f"<td>{e(item.get(field, ''))}</td>" for field in fields)
        if attempt_id:
            managed_actions = (
                f"<button type='button' onclick=\"correctTaskAttempt('{task_id}', '{attempt_id}')\">Correct</button> "
                f"<button class='danger-button' type='button' onclick=\"cancelTaskAttempt('{task_id}', '{attempt_id}')\">Cancel</button> "
            )
        else:
            managed_actions = ""
        actions = (
            "<td>"
            f"{managed_actions}"
            f"<button type='button' onclick=\"retryTask('{task_id}')\">Retry</button> "
            f"<button type='button' onclick=\"reassignTask('{task_id}', '{target}')\">Reassign</button> "
            f"<button type='button' onclick=\"viewTaskTrace('{trace_id}')\">Trace</button>"
            "</td>"
        )
        body.append(f"<tr>{cells}{actions}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def employee_view_models(summary: dict) -> list[dict]:
    employees = []
    communication_config = companyctl.load_communication_config()
    communication_profiles = communication_config.get("employees", {})
    conn = companyctl.connect_readonly()
    try:
        for employee in summary["employees"]:
            if employee.get("id") == "owner" or employee.get("role") == "human-owner" or employee.get("runtime") == "human":
                continue
            capabilities = companyctl.load_json_or_default(companyctl.employee_paths(employee["id"])["capabilities"], {})
            permissions = companyctl.load_json_or_default(
                companyctl.employee_paths(employee["id"])["permissions"],
                {
                    "can_submit_tasks": True,
                    "can_claim_tasks": True,
                    "can_modify_kernel": False,
                    "requires_approval_for": ["payment", "compensation", "salary", "penalty", "external_send"],
                },
            )
            skills = capabilities.get("skills", [])
            tools = capabilities.get("tools", [])
            task_types = capabilities.get("preferred_task_types", [])
            communication_profile = communication_profiles.get(employee["id"], {})
            try:
                heartbeat_metadata = json.loads(employee.get("heartbeat_metadata_json", "{}") or "{}")
            except json.JSONDecodeError:
                heartbeat_metadata = {}
            heartbeat_progress = companyctl.extract_progress_payload(heartbeat_metadata)
            age = minutes_since(employee.get("last_seen_at", ""), summary["generated_at"])
            employee_status = employee.get("employee_status") or employee.get("status", "")
            heartbeat_status = employee.get("heartbeat_status", "missing")
            if employee_status != "active":
                kernel_state = employee_status
            elif heartbeat_status == "missing":
                kernel_state = "missing_heartbeat"
            elif age is not None and age > 15:
                kernel_state = "stale_heartbeat"
            else:
                kernel_state = "online"
            readiness = companyctl.classify_agent_matrix_row(
                conn,
                {"id": employee["id"], "name": employee.get("name", employee["id"]), "runtime": employee.get("runtime", ""), "status": employee_status},
                {"status": "online" if kernel_state == "online" else "missing"},
            )
            schedulable = readiness.get("level") == "active_ready"
            employees.append(
                {
                    **employee,
                    "status": employee_status,
                    "employee_status": employee_status,
                    "kernel_state": kernel_state,
                    "schedulable": schedulable,
                    "readiness_level": readiness.get("level", ""),
                    "readiness_reason": readiness.get("reason", ""),
                    "readiness_checks": readiness.get("checks", {}),
                    "sandbox_profile": companyctl.employee_sandbox_profile(employee, permissions),
                    "heartbeat_age_minutes": "" if age is None else age,
                    "communication_paused": bool(communication_profile.get("communication_paused")),
                    "communication_status": "paused" if communication_profile.get("communication_paused") else "enabled",
                    "backlog": f"{employee.get('submitted_tasks', 0)} submitted, {employee.get('claimed_tasks', 0)} claimed",
                    "progress_layer": heartbeat_progress.get("layer", ""),
                    "progress_state": heartbeat_progress.get("state", ""),
                    "progress_label": heartbeat_progress.get("label", ""),
                    "progress_display": f"{heartbeat_progress.get('layer', '')} / {heartbeat_progress.get('state', '')}".strip(" /") if heartbeat_progress.get("layer") or heartbeat_progress.get("state") else "",
                    "skills": ", ".join(str(item) for item in skills[:4]) if isinstance(skills, list) else "invalid",
                    "tools": ", ".join(str(item) for item in tools[:4]) if isinstance(tools, list) else "invalid",
                    "task_types": ", ".join(str(item) for item in task_types[:4]) if isinstance(task_types, list) else "invalid",
                }
            )
    finally:
        conn.close()
    return employees


def approval_task_id(approval: dict) -> str:
    raw = approval.get("reason", "")
    try:
        detail = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return ""
    if not isinstance(detail, dict):
        return ""
    metadata = detail.get("metadata", {})
    if isinstance(metadata, dict) and metadata.get("task_id"):
        return str(metadata["task_id"])
    return ""


def task_approval_counts(tasks: list[dict], approvals: list[dict]) -> dict[str, int]:
    task_ids = {str(task["id"]) for task in tasks}
    counts = {task_id: 0 for task_id in task_ids}
    for approval in approvals:
        structured_task_id = approval_task_id(approval)
        if structured_task_id in counts:
            counts[structured_task_id] += 1
            continue
        raw = approval.get("reason", "")
        for task_id in task_ids:
            if task_id in raw:
                counts[task_id] += 1
                break
    return counts


def render(summary: dict) -> str:
    counts = summary["counts"]
    danger = (
        counts["pending_approvals"]
        or counts["pending_rfcs"]
        or counts["pending_events"]
        or counts["claimed_tasks"]
        or counts["locks"]
        or summary["evidence_health"]["issues"]
        or not summary["runtime_health"]["daemon"].get("ok")
    )
    state = "attention" if danger else "normal"
    approvals = []
    for approval in summary["approvals"]:
        detail = approval.get("reason", "")
        try:
            detail_obj = json.loads(detail or "{}")
            detail = detail_obj.get("request_reason", detail)
        except json.JSONDecodeError:
            pass
        approvals.append({**approval, "reason": detail})
    approval_counts = task_approval_counts(summary["tasks"], summary["approvals"])
    projects = []
    for project in summary["projects"]:
        try:
            acceptance = json.loads(project.get("acceptance_json", "[]") or "[]")
        except json.JSONDecodeError:
            acceptance = []
        open_tasks = int(project.get("open_tasks", 0) or 0)
        blocked_tasks = int(project.get("blocked_tasks", 0) or 0)
        task_count = int(project.get("task_count", 0) or 0)
        open_plan_items = int(project.get("open_plan_items", 0) or 0)
        ready = bool(task_count and not open_tasks and not blocked_tasks and not open_plan_items)
        projects.append(
            {
                **project,
                "acceptance": "; ".join(str(item) for item in acceptance),
                "review_state": "ready" if ready else "blocked" if blocked_tasks else "in_progress",
                "plan": project.get("plan_items") or f"{project.get('completed_tasks', 0)}/{task_count} done, {open_tasks} open, {blocked_tasks} blocked",
            }
        )
    tasks = []
    attempts_by_task = {str(item.get("task_id", "")): item for item in summary.get("active_attempts", [])}
    for task in summary["tasks"]:
        attempt = attempts_by_task.get(str(task["id"]), {})
        attempt_display = ""
        if attempt:
            attempt_display = f"{attempt.get('status', '')}: {attempt.get('attempt_id', '')}"
        tasks.append(
            {
                **task,
                "evidence": "yes" if task.get("evidence_path") else "",
                "blocker_detail": task.get("blocker", ""),
                "approval_count": approval_counts.get(str(task["id"]), 0),
                "attempt_id": attempt.get("attempt_id", ""),
                "attempt_status": attempt.get("status", ""),
                "attempt_trace_id": attempt.get("trace_id", ""),
                "attempt_display": attempt_display,
            }
        )
    task_delegations = []
    for item in summary["task_delegations"]:
        child_count = int(item.get("child_count", 0) or 0)
        completed = int(item.get("completed_children", 0) or 0)
        blocked = int(item.get("blocked_children", 0) or 0)
        open_children = int(item.get("open_children", 0) or 0)
        task_delegations.append(
            {
                **item,
                "progress": f"{completed}/{child_count}",
                "review_state": "ready" if child_count and completed == child_count else "blocked" if blocked else "in_progress",
                "open_blocked": f"{open_children} open, {blocked} blocked",
            }
        )
    rfcs = []
    for rfc in summary["rfcs"]:
        try:
            target_paths = json.loads(rfc.get("target_paths_json", "[]") or "[]")
        except json.JSONDecodeError:
            target_paths = []
        rfcs.append({**rfc, "target_paths": ", ".join(str(path) for path in target_paths)})
    conversations = []
    for conversation in summary["conversations"]:
        try:
            participants = json.loads(conversation.get("participants_json", "[]") or "[]")
        except json.JSONDecodeError:
            participants = []
        conversations.append({**conversation, "participants": ", ".join(str(participant) for participant in participants)})
    adapter_runs = []
    for run in summary["adapter_runs"]:
        try:
            result = run.get("_result")
            if not isinstance(result, dict):
                result = json.loads(run.get("result_json", "{}") or "{}")
        except json.JSONDecodeError:
            result = {}
        progress = companyctl.extract_progress_payload({})
        parsed_runs = result.get("runs", []) if isinstance(result, dict) else []
        first_parsed = parsed_runs[0] if parsed_runs and isinstance(parsed_runs[0], dict) else {}
        parsed_stdout = first_parsed.get("parsed_stdout", {}) if isinstance(first_parsed, dict) else {}
        report_path = str(parsed_stdout.get("report", "")) if isinstance(parsed_stdout, dict) else ""
        progress = progress_from_report_path(report_path)
        adapter_runs.append(
            {
                **run,
                "ok_text": "yes" if run.get("ok") else "no",
                "state_file": run.get("state_file") or result.get("state_file", ""),
                "progress_layer": progress.get("layer", ""),
                "progress_state": progress.get("state", ""),
                "sanitized_log": run.get("sanitized_log") or companyctl.summarize_adapter_result(result).get("sanitized_log", ""),
            }
        )
    runtime_health = [
        {"name": "daemon", "path": summary["runtime_health"]["daemon"].get("state_file", ""), **summary["runtime_health"]["daemon"]},
        {"name": "launchd", "path": summary["runtime_health"]["launchd"].get("template", ""), **summary["runtime_health"]["launchd"]},
    ]
    evidence_health = []
    for issue in summary["evidence_health"]["issues"]:
        evidence_health.append(
            {
                **issue,
                "path": issue.get("evidence_path", ""),
            }
        )
    employees = employee_view_models(summary)
    return f"""<!doctype html>
<html lang="zh">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Company Kernel Dashboard</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f7f7f4; color: #202124; }}
    header {{ padding: 24px 28px 14px; border-bottom: 1px solid #ddd9cf; background: #fff; }}
    h1 {{ margin: 0 0 6px; font-size: 24px; }}
    h2 {{ margin: 24px 0 10px; font-size: 17px; }}
    main {{ padding: 18px 28px 36px; }}
    .meta {{ color: #68665f; font-size: 13px; }}
    .status {{ display: inline-block; padding: 4px 10px; border-radius: 999px; font-size: 13px; background: {"#fff0d6" if state == "attention" else "#e6f4ea"}; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-top: 16px; }}
    .metric {{ background: #fff; border: 1px solid #dedbd2; border-radius: 8px; padding: 12px; }}
    .metric strong {{ display: block; font-size: 24px; margin-top: 4px; }}
    .pill {{ display: inline-block; margin: 0 8px 8px 0; padding: 6px 10px; background: #fff; border: 1px solid #dedbd2; border-radius: 999px; font-size: 13px; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #dedbd2; border-radius: 8px; overflow: hidden; }}
    th, td {{ padding: 9px 10px; border-bottom: 1px solid #ece8df; text-align: left; vertical-align: top; font-size: 13px; }}
    th {{ background: #efede7; color: #4d4a43; font-weight: 600; }}
    tr:last-child td {{ border-bottom: 0; }}
    td {{ max-width: 360px; overflow-wrap: anywhere; }}
    .toolbar {{ display: flex; align-items: flex-end; gap: 10px; flex-wrap: wrap; margin: 10px 0 14px; padding: 12px; background: #fff; border: 1px solid #dedbd2; border-radius: 8px; }}
    .toolbar label {{ display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: #68665f; }}
    .toolbar input, .toolbar select {{ min-width: 130px; padding: 7px 8px; border: 1px solid #cfcabe; border-radius: 6px; background: #fff; color: #202124; }}
    .toolbar button, .danger-button {{ padding: 8px 10px; border: 1px solid #bdb7aa; border-radius: 6px; background: #202124; color: #fff; cursor: pointer; }}
    .danger-button {{ background: #9f1d1d; border-color: #9f1d1d; }}
    .api-note {{ margin: 8px 0 0; color: #68665f; font-size: 12px; }}
    .api-status {{ margin-top: 8px; font-size: 13px; color: #3f6f32; }}
  </style>
</head>
<body>
  <header>
    <h1>Company Kernel Dashboard</h1>
    <div class="meta">Generated at {e(summary["generated_at"])} from {e(DB_PATH)}</div>
    <div style="margin-top:10px"><span class="status">{'Needs Attention' if state == 'attention' else 'Normal'}</span></div>
  </header>
  <main>
    <section class="grid">
      {''.join(f"<div class='metric'>{e(k)}<strong>{v}</strong></div>" for k, v in counts.items())}
    </section>
    <h2>Task Status</h2>
    <div>{pills(summary["task_status"])}</div>
    <h2>Project Status</h2>
    <div>{pills(summary["project_status"])}</div>
    <h2>Approval Status</h2>
    <div>{pills(summary["approval_status"])}</div>
    <h2>RFC Status</h2>
    <div>{pills(summary["rfc_status"])}</div>
    <h2>Runtime Health</h2>
    {render_table(["name", "ok", "installed", "age", "reason", "state/template", "install", "verify"], runtime_health, ["name", "ok", "installed", "age_minutes", "reason", "path", "install_command", "verify_command"])}
    <h2>Evidence Health</h2>
    {render_table(["task", "agent", "reason", "path"], evidence_health, ["task_id", "agent", "reason", "path"])}
    <h2>Employees</h2>
    <div class="toolbar" id="employee-manager">
      <label>API Gateway
        <input id="api-base" value="http://127.0.0.1:8765">
      </label>
      <label>ID
        <input id="employee-id" placeholder="e.g. nestcar-helper">
      </label>
      <label>Name
        <input id="employee-name" placeholder="Display name">
      </label>
      <label>Role
        <input id="employee-role" value="business-agent">
      </label>
      <label>Runtime
        <select id="employee-runtime">
          <option value="openclaw">openclaw</option>
          <option value="hermes">hermes</option>
          <option value="codex">codex</option>
          <option value="claude">claude</option>
          <option value="trae">trae</option>
          <option value="antigravity">antigravity</option>
          <option value="local">local</option>
        </select>
      </label>
      <label>Workspace
        <input id="employee-workspace" placeholder="$OPENCLAW_COMPANY_KERNEL_ROOT/employees/<id>">
      </label>
      <label>Skills
        <input id="employee-skills" placeholder="ops,review">
      </label>
      <button type="button" onclick="checkCompanyApi()">Check API</button>
      <button type="button" onclick="onboardEmployee()">Onboard</button>
    </div>
    <div class="api-note">Employee actions call Company Kernel REST API and then reload this static dashboard. Start with: <code>bin/company-api-gateway --quiet</code></div>
    <div class="api-status" id="employee-api-status"></div>
    <h2>Notification Settings</h2>
    <div class="toolbar" id="notification-settings">
      <label>Telegram Account
        <input id="notify-telegram-account" value="employee-notify">
      </label>
      <label>Bot Token Env Var
        <input id="notify-telegram-token-env" value="COMPANY_EMPLOYEE_TELEGRAM_BOT_TOKEN">
      </label>
      <label>Default Target
        <input id="notify-telegram-target" placeholder="telegram chat/user target">
      </label>
      <label>Enabled
        <select id="notify-enabled">
          <option value="true">true</option>
          <option value="false">false</option>
        </select>
      </label>
      <button type="button" onclick="saveNotificationSettings()">Save Notification</button>
      <button type="button" onclick="loadNotificationSettings()">Load Notification</button>
    </div>
    <div class="api-note">Do not paste bot tokens here. Export the token in the environment variable shown above before starting the API/daemon.</div>
    {render_employee_table(employees)}
    <h2>Projects</h2>
    {render_table(["id", "owner", "status", "review", "plan", "open_plan", "accepted", "goal", "acceptance", "retro", "title", "updated"], projects, ["id", "owner_agent", "status", "review_state", "plan", "open_plan_items", "acceptance_count", "goal", "acceptance", "latest_acceptance_summary", "title", "updated_at"])}
    <h2>Recent Tasks</h2>
    {render_task_table(tasks)}
    <h2>Long Task Delegation</h2>
    {render_table(["parent", "owner", "status", "review", "progress", "open/blocked", "children", "latest_child_update"], task_delegations, ["parent_id", "parent_owner", "parent_status", "review_state", "progress", "open_blocked", "child_summary", "latest_child_update"])}
    <h2>Conversations</h2>
    {render_table(["id", "status", "created_by", "participants", "messages", "last_message", "title"], conversations, ["id", "status", "created_by", "participants", "message_count", "last_message_at", "title"])}
    <h2>Approvals</h2>
    {render_table(["id", "source", "action", "status", "reason", "updated"], approvals, ["id", "source_agent", "action", "status", "reason", "updated_at"])}
    <h2>RFCs</h2>
    {render_table(["id", "author", "status", "paths", "reason", "decision_by", "updated"], rfcs, ["id", "author_agent", "status", "target_paths", "reason", "decision_by", "updated_at"])}
    <h2>Followups</h2>
    {render_table(["id", "status", "source", "target", "question", "answer", "answered_at"], summary["followups"], ["id", "status", "source_agent", "target_agent", "question", "answer", "answered_at"])}
    <h2>Events</h2>
    <h2>Pending Events</h2>
    {render_table(["id", "trace", "type", "source", "task", "created"], summary["pending_events"], ["id", "trace_id", "event_type", "source_agent", "task_id", "created_at"])}
    <h2>Recent Events</h2>
    {render_table(["id", "trace", "type", "source", "task", "processed_at", "created"], summary["events"], ["id", "trace_id", "event_type", "source_agent", "task_id", "processed_at", "created_at"])}
    <h2>Adapter Runs</h2>
    {render_table(["id", "trace", "agent", "task", "command", "ok", "progress_layer", "progress_state", "processed", "attempt", "next_retry", "ack_by", "ack_reason", "sanitized_log", "state_file", "created"], adapter_runs, ["id", "trace_id", "agent_id", "task_id", "command", "ok_text", "progress_layer", "progress_state", "processed", "attempt", "next_retry_at", "acknowledged_by", "acknowledgement_reason", "sanitized_log", "state_file", "created_at"])}
    <h2>Locks</h2>
    {render_table(["resource", "owner", "lease_until", "updated"], summary["locks"], ["resource_key", "owner_agent", "lease_until", "updated_at"])}
  </main>
  <script>
    function apiBase() {{
      return (document.getElementById('api-base').value || 'http://127.0.0.1:8765').replace(/\\/$/, '');
    }}
    function setEmployeeApiStatus(text, isError) {{
      const el = document.getElementById('employee-api-status');
      el.textContent = text;
      el.style.color = isError ? '#9f1d1d' : '#3f6f32';
    }}
    async function getCompanyApi(path) {{
      const res = await fetch(apiBase() + path, {{method: 'GET'}});
      const data = await res.json();
      if (!res.ok || data.ok === false) {{
        throw new Error(data.error || data.message || JSON.stringify(data));
      }}
      return data;
    }}
    async function checkCompanyApi() {{
      setEmployeeApiStatus(`Checking ${{apiBase()}}/v1/health...`, false);
      try {{
        const data = await getCompanyApi('/v1/health');
        const employees = data.counts ? data.counts.employees : 'unknown';
        setEmployeeApiStatus(`API online. employees=${{employees}}`, false);
        return true;
      }} catch (err) {{
        setEmployeeApiStatus(`API offline: ${{err.message}}. Start: bin/company-api-gateway --quiet`, true);
        return false;
      }}
    }}
    async function callCompanyApi(path, payload, method) {{
      const res = await fetch(apiBase() + path, {{
        method: method || 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(payload || {{}})
      }});
      const data = await res.json();
      if (!res.ok || data.ok === false) {{
        throw new Error(data.error || data.message || JSON.stringify(data));
      }}
      return data;
    }}
    async function onboardEmployee() {{
      const id = document.getElementById('employee-id').value.trim();
      const name = document.getElementById('employee-name').value.trim() || id;
      const role = document.getElementById('employee-role').value.trim() || 'business-agent';
      const runtime = document.getElementById('employee-runtime').value;
      const workspace = document.getElementById('employee-workspace').value.trim() || `${{window.companyKernelRoot || '.'}}/employees/${{id}}`;
      const skills = document.getElementById('employee-skills').value.trim();
      if (!id) {{
        setEmployeeApiStatus('employee id is required', true);
        return;
      }}
      setEmployeeApiStatus(`Onboarding ${{id}}...`, false);
      try {{
        await callCompanyApi('/v1/employees/onboard', {{
          id, name, role, runtime, workspace, skills,
          open_communication: true,
          create_test_task: false
        }});
        setEmployeeApiStatus(`Onboarded ${{id}}. Reloading dashboard...`, false);
        setTimeout(() => location.reload(), 800);
      }} catch (err) {{
        setEmployeeApiStatus(`Onboard failed: ${{err.message}}`, true);
      }}
    }}
    async function loadNotificationSettings() {{
      setEmployeeApiStatus('Loading notification settings...', false);
      try {{
        const data = await getCompanyApi('/v1/settings/notification');
        const cfg = data.employee_notifications || {{}};
        const account = cfg.account || 'employee-notify';
        const telegram = (data.telegram_accounts || {{}})[account] || {{}};
        document.getElementById('notify-telegram-account').value = account;
        document.getElementById('notify-telegram-token-env').value = telegram.bot_token_env || 'COMPANY_EMPLOYEE_TELEGRAM_BOT_TOKEN';
        document.getElementById('notify-telegram-target').value = cfg.target || telegram.default_target || '';
        document.getElementById('notify-enabled').value = cfg.enabled ? 'true' : 'false';
        setEmployeeApiStatus(`Notification loaded. token_configured=${{telegram.token_configured ? 'yes' : 'no'}}`, false);
      }} catch (err) {{
        setEmployeeApiStatus(`Notification load failed: ${{err.message}}`, true);
      }}
    }}
    async function saveNotificationSettings() {{
      const payload = {{
        telegram_account: document.getElementById('notify-telegram-account').value.trim() || 'employee-notify',
        telegram_bot_token_env: document.getElementById('notify-telegram-token-env').value.trim(),
        telegram_default_target: document.getElementById('notify-telegram-target').value.trim(),
        employee_notifications_enabled: document.getElementById('notify-enabled').value === 'true'
      }};
      if (!payload.telegram_bot_token_env) {{
        setEmployeeApiStatus('Bot token env var is required. Do not paste the token itself.', true);
        return;
      }}
      setEmployeeApiStatus('Saving notification settings...', false);
      try {{
        await callCompanyApi('/v1/settings/notification', payload, 'POST');
        setEmployeeApiStatus('Notification settings saved without storing token.', false);
      }} catch (err) {{
        setEmployeeApiStatus(`Notification save failed: ${{err.message}}`, true);
      }}
    }}
    async function directMessageEmployee(id) {{
      if (!id) return;
      const source = prompt(`Source employee for direct message to ${{id}}`, 'main');
      if (source === null) return;
      const body = prompt(`Message to ${{id}}`, `只回复：${{id}}_DIRECT_OK`);
      if (body === null) return;
      setEmployeeApiStatus(`Direct messaging ${{id}}...`, false);
      try {{
        const result = await callCompanyApi('/v1/messages/direct', {{from: source || 'main', to: id, body}}, 'POST');
        setEmployeeApiStatus(`Direct reply from ${{id}}: ${{result.reply || '(empty)'}}; evidence=${{result.file || 'n/a'}}`, false);
      }} catch (err) {{
        setEmployeeApiStatus(`Direct failed: ${{err.message}}`, true);
      }}
    }}
    async function editEmployee(id) {{
      if (!id) return;
      const row = Array.from(document.querySelectorAll('tbody tr')).find((candidate) => candidate.firstElementChild && candidate.firstElementChild.textContent === id);
      const currentStatus = row && row.children[1] ? row.children[1].textContent : '';
      const currentRole = row && row.children[4] ? row.children[4].textContent : '';
      const currentRuntime = row && row.children[5] ? row.children[5].textContent : '';
      const name = prompt(`Name for ${{id}} (blank keeps current)`, '');
      if (name === null) return;
      const role = prompt(`Role for ${{id}}`, currentRole || 'business-agent');
      if (role === null) return;
      const runtime = prompt(`Runtime for ${{id}}`, currentRuntime || 'local');
      if (runtime === null) return;
      const status = prompt(`Status for ${{id}}: active, candidate, archived`, currentStatus || 'active');
      if (status === null) return;
      if (!['active', 'candidate', 'archived'].includes(status)) {{
        setEmployeeApiStatus(`Invalid status: ${{status}}`, true);
        return;
      }}
      setEmployeeApiStatus(`Updating ${{id}}...`, false);
      try {{
        await callCompanyApi(`/v1/employees/${{encodeURIComponent(id)}}`, {{name, role, runtime, status}}, 'PATCH');
        setEmployeeApiStatus(`Updated ${{id}}. Reloading dashboard...`, false);
        setTimeout(() => location.reload(), 800);
      }} catch (err) {{
        setEmployeeApiStatus(`Update failed: ${{err.message}}`, true);
      }}
    }}
    async function offboardEmployee(id, hardDelete) {{
      if (!id) return;
      const action = hardDelete ? 'hard delete' : 'archive';
      if (!confirm(`${{action}} employee "${{id}}"?`)) return;
      setEmployeeApiStatus(`Offboarding ${{id}}...`, false);
      try {{
        await callCompanyApi(`/v1/employees/${{encodeURIComponent(id)}}`, {{hard_delete: !!hardDelete}}, 'DELETE');
        setEmployeeApiStatus(`Offboarded ${{id}}. Reloading dashboard...`, false);
        setTimeout(() => location.reload(), 800);
      }} catch (err) {{
        setEmployeeApiStatus(`Offboard failed: ${{err.message}}`, true);
      }}
    }}
    async function correctTaskAttempt(taskId, attemptId) {{
      const by = prompt(`Supervisor correcting ${{taskId}}`, 'hermes');
      if (by === null) return;
      const message = prompt('Correction message', '请回到原任务目标，更新 progress，并说明 blocker/evidence。');
      if (message === null) return;
      setEmployeeApiStatus(`Sending correction for ${{taskId}}...`, false);
      try {{
        await callCompanyApi(`/v1/tasks/${{encodeURIComponent(taskId)}}/correct`, {{attempt_id: attemptId, by: by || 'hermes', message}}, 'POST');
        setEmployeeApiStatus(`Correction sent for ${{taskId}}. Reloading dashboard...`, false);
        setTimeout(() => location.reload(), 800);
      }} catch (err) {{
        setEmployeeApiStatus(`Correction failed: ${{err.message}}`, true);
      }}
    }}
    async function cancelTaskAttempt(taskId, attemptId) {{
      const by = prompt(`Who cancels ${{taskId}}?`, 'hermes');
      if (by === null) return;
      const reason = prompt('Cancel reason', '用户停止或 supervisor 判定需要停止');
      if (reason === null) return;
      if (!confirm(`Cancel attempt ${{attemptId}} for task ${{taskId}}?`)) return;
      setEmployeeApiStatus(`Cancelling ${{taskId}}...`, false);
      try {{
        await callCompanyApi(`/v1/tasks/${{encodeURIComponent(taskId)}}/cancel`, {{attempt_id: attemptId, by: by || 'hermes', reason}}, 'POST');
        setEmployeeApiStatus(`Cancelled ${{taskId}}. Reloading dashboard...`, false);
        setTimeout(() => location.reload(), 800);
      }} catch (err) {{
        setEmployeeApiStatus(`Cancel failed: ${{err.message}}`, true);
      }}
    }}
    async function retryTask(taskId) {{
      const by = prompt(`Who retries ${{taskId}}?`, 'hermes');
      if (by === null) return;
      const reason = prompt('Retry reason', '修复后重试');
      if (reason === null) return;
      setEmployeeApiStatus(`Retrying ${{taskId}}...`, false);
      try {{
        await callCompanyApi(`/v1/tasks/${{encodeURIComponent(taskId)}}/retry`, {{by: by || 'hermes', reason}}, 'POST');
        setEmployeeApiStatus(`Retry attempt started for ${{taskId}}. Reloading dashboard...`, false);
        setTimeout(() => location.reload(), 800);
      }} catch (err) {{
        setEmployeeApiStatus(`Retry failed: ${{err.message}}`, true);
      }}
    }}
    async function reassignTask(taskId, currentTarget) {{
      const by = prompt(`Who reassigns ${{taskId}}?`, 'hermes');
      if (by === null) return;
      const to = prompt('New employee id', currentTarget || 'codex');
      if (to === null || !to.trim()) return;
      const reason = prompt('Reassign reason', '更适合该员工执行');
      if (reason === null) return;
      setEmployeeApiStatus(`Reassigning ${{taskId}} to ${{to}}...`, false);
      try {{
        await callCompanyApi(`/v1/tasks/${{encodeURIComponent(taskId)}}/reassign`, {{by: by || 'hermes', to, reason}}, 'POST');
        setEmployeeApiStatus(`Reassigned ${{taskId}}. Reloading dashboard...`, false);
        setTimeout(() => location.reload(), 800);
      }} catch (err) {{
        setEmployeeApiStatus(`Reassign failed: ${{err.message}}`, true);
      }}
    }}
    function traceTimelineSummary(timeline) {{
      const rows = Array.isArray(timeline) ? timeline : [];
      if (!rows.length) return '-';
      return rows.map(item => {{
        const id = item.event_id || item.run_id || item.artifact_id || item.evidence_id || item.handoff_id || item.attempt_id || '';
        const display = item.display && item.display.relative_path ? ` · ${{item.display.relative_path}}` : '';
        const log = item.sanitized_log ? ` · ${{item.sanitized_log}}` : '';
        return `${{item.at || '-'}} · ${{item.kind || '-'}} · ${{item.status || '-'}} · ${{item.task_id || '-'}} · ${{item.label || id}}${{display}}${{log}}`;
      }}).join('\\n');
    }}
    async function viewTaskTrace(traceId) {{
      if (!traceId) {{
        setEmployeeApiStatus('No trace_id for this task yet.', true);
        return;
      }}
      setEmployeeApiStatus(`Loading Trace Timeline ${{traceId}}...`, false);
      try {{
        const payload = await companyApiGet(`/v1/traces/${{encodeURIComponent(traceId)}}/timeline`);
        renderDetails(`Trace Timeline: ${{traceId}}`, [
          ['Trace ID', payload.trace_id || traceId],
          ['Counts', payload.counts || {{}}],
          ['Tasks', (payload.tasks || []).map(task => `${{task.id || '-'}} · ${{task.status || '-'}} · ${{task.target_agent || '-'}}`).join('\\n') || '-'],
          ['Timeline', traceTimelineSummary(payload.timeline || [])],
          ['Raw', payload],
        ], payload);
        setEmployeeApiStatus(`Trace Timeline loaded for ${{traceId}}.`, false);
      }} catch (err) {{
        setEmployeeApiStatus(`Trace Timeline failed: ${{err.message}}`, true);
      }}
    }}
    window.addEventListener('DOMContentLoaded', checkCompanyApi);
  </script>
</body>
</html>
"""


def advanced_summary(summary: dict) -> dict:
    prepared = public_summary(summary)
    employees = employee_view_models(summary)
    counts = dict(summary.get("counts", {}))
    counts["employees"] = len(employees)
    counts["active_employees"] = sum(1 for employee in employees if employee.get("employee_status") == "active")
    counts["candidate_employees"] = sum(1 for employee in employees if employee.get("employee_status") == "candidate")
    counts["archived_employees"] = sum(1 for employee in employees if employee.get("employee_status") == "archived")
    prepared["counts"] = counts
    prepared["employees"] = employees
    prepared["skill_registry"] = companyctl.skill_registry()
    prepared["communication_observability"] = communication_observability_summary(summary)
    prepared["openclaw_runtime_inventory"] = summary.get("runtime_health", {}).get("openclaw_inventory", {})
    prepared["cockpit"] = build_cockpit_summary({**summary, "employees": employees})
    return prepared


def load_advanced_template(path: str = "") -> tuple[Path | None, str]:
    if path:
        candidates = [Path(path)]
    else:
        candidates = [ROOT / "dashboard_templates" / "gemini_dashboard.html"]
    for candidate in candidates:
        if candidate.exists():
            return candidate, candidate.read_text(encoding="utf-8")
    return None, ""


def inject_advanced_dashboard(template: str, summary: dict, *, db_path: Path, api_base: str) -> str:
    payload = json.dumps(summary, ensure_ascii=False)
    payload_b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    html_text = template

    def append_before_body(text: str, insertion: str) -> str:
        idx = text.lower().rfind("</body>")
        if idx == -1:
            return text + insertion
        return text[:idx] + insertion + text[idx:]

    # Inject metadata and summary window payload before the first <script> tag or </body>
    injection = (
        f"<script>\n"
        f"  window.kernelSummary = JSON.parse(decodeURIComponent(escape(atob({json.dumps(payload_b64)}))));\n"
        f"  window.dbPath = {json.dumps(str(db_path), ensure_ascii=False)};\n"
        f"  window.companyApiBase = {json.dumps(api_base, ensure_ascii=False)};\n"
        f"  window.companyKernelRoot = {json.dumps(str(ROOT), ensure_ascii=False)};\n"
        f"</script>\n"
    )

    idx = html_text.find("<script>")
    if idx != -1:
        html_text = html_text[:idx] + injection + html_text[idx:]
    else:
        html_text = append_before_body(html_text, injection)

    if "kernel-summary-debug" not in html_text:
        html_text = html_text.replace("</script>", f'  <!-- kernel-summary-debug {payload} -->\n</script>', 1)

    return html_text


def run(args: argparse.Namespace) -> int:
    conn = connect()
    try:
        summary = load_summary(conn)
    finally:
        conn.close()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    template_path = None
    variant = args.variant
    if variant in {"auto", "advanced"}:
        template_path, template = load_advanced_template(args.template)
        if template:
            prepared_summary = advanced_summary(summary)
            output.write_text(inject_advanced_dashboard(template, prepared_summary, db_path=DB_PATH, api_base=args.api_base), encoding="utf-8")
            variant = "advanced"
        elif variant == "advanced":
            raise SystemExit("advanced dashboard template not found")
        else:
            prepared_summary = summary
            output.write_text(render(summary), encoding="utf-8")
            variant = "basic"
    else:
        prepared_summary = summary
        output.write_text(render(summary), encoding="utf-8")
        variant = "basic"
    print(
        json.dumps(
            {
                "ok": True,
                "output": str(output),
                "variant": variant,
                "template": str(template_path or ""),
                "counts": prepared_summary["counts"],
                "ledger_consistency": prepared_summary.get("cockpit", {}).get("ledger_consistency", {}),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate Company Kernel static dashboard")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--variant", choices=["auto", "basic", "advanced"], default="auto")
    parser.add_argument("--template", default="")
    parser.add_argument("--api-base", default="http://127.0.0.1:8765")
    return parser


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
