"""company_kernel.core.events — the event/audit/output primitives, with NO dependency on companyctl
or any domain (and crucially none on connect()/DB_PATH: every function takes the caller's `conn`).

Moved as one group (gate-approved: record_event/audit/emit/trace_id_for_task) so the dependency
boundary is clean — they depend only on the core time/uuid primitives (now/new_trace_id), stdlib,
the caller-supplied connection, and each other. companyctl re-exports them explicitly (no `*`) so all
~576 call sites are unchanged, and `companyctl.trace_id_for_task` stays a working mock-patch anchor.

emit() gains an optional `stream` (default None → resolved to sys.stdout at call time): production
behaviour is byte-for-byte identical, and a redirect_stdout/fake stream in tests still works.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from datetime import datetime

from company_kernel.core import new_trace_id, now


def emit(obj: dict, stream=None) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2), file=stream if stream is not None else sys.stdout)


def audit(conn: sqlite3.Connection, actor: str, action: str, target: str = "", detail: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO audit_logs(actor, action, target, detail_json, created_at) VALUES (?, ?, ?, ?, ?)",
        (actor, action, target, json.dumps(detail or {}, ensure_ascii=False), now()),
    )
    conn.commit()


def trace_id_for_task(conn: sqlite3.Connection, task_id: str = "", fallback: str = "") -> str:
    if not task_id:
        return fallback or new_trace_id()
    row = conn.execute("SELECT metadata_json FROM task_metadata WHERE task_id = ?", (task_id,)).fetchone()
    if row:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            metadata = {}
        trace_id = str(metadata.get("trace_id", "") or "")
        if trace_id:
            return trace_id
    return fallback or new_trace_id()


def record_event(conn: sqlite3.Connection, event_type: str, source_agent: str, *, task_id: str = "", payload: dict | None = None, trace_id: str = "") -> dict:
    event_id = f"evt-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    ts = now()
    event_trace_id = trace_id or trace_id_for_task(conn, task_id)
    event = {
        "id": event_id,
        "trace_id": event_trace_id,
        "event_type": event_type,
        "source_agent": source_agent,
        "task_id": task_id,
        "payload": payload or {},
        "created_at": ts,
    }
    conn.execute(
        """
        INSERT INTO company_events(id, trace_id, event_type, source_agent, task_id, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (event_id, event_trace_id, event_type, source_agent, task_id, json.dumps(payload or {}, ensure_ascii=False), ts),
    )
    conn.commit()
    return event
