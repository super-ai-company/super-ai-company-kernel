from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "company.sqlite"
SCHEMA = ROOT / "company_kernel" / "schema.sql"
APPROVAL_STATE_DIR = ROOT / "state" / "approvals"
APPROVAL_STATUSES = {"pending", "approved", "denied"}


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    conn.commit()
    return conn


def approval_detail(raw: str) -> dict:
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {"reason": raw}
    except json.JSONDecodeError:
        return {"reason": raw}


def normalize_approval(row: sqlite3.Row | dict) -> dict:
    obj = dict(row)
    obj["detail"] = approval_detail(obj.pop("reason", ""))
    return obj


def write_approval_state(approval: dict) -> str:
    status = approval["status"]
    for existing_status in APPROVAL_STATUSES:
        old = APPROVAL_STATE_DIR / existing_status / f"{approval['id']}.json"
        if old.exists() and existing_status != status:
            old.unlink()
    target_dir = APPROVAL_STATE_DIR / status
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{approval['id']}.json"
    path.write_text(json.dumps(approval, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


def audit(conn: sqlite3.Connection, actor: str, action: str, target: str = "", detail: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO audit_logs(actor, action, target, detail_json, created_at) VALUES (?, ?, ?, ?, ?)",
        (actor, action, target, json.dumps(detail or {}, ensure_ascii=False), now()),
    )
    conn.commit()


def require_employee(conn: sqlite3.Connection, employee_id: str) -> None:
    if not conn.execute("SELECT 1 FROM employees WHERE id = ?", (employee_id,)).fetchone():
        raise ValueError(f"unknown employee: {employee_id}")


def create_approval_request(
    conn: sqlite3.Connection,
    *,
    source: str,
    action: str,
    reason: str,
    target: str = "",
    risk: str = "P1",
    evidence: str = "",
    approval_id: str = "",
    metadata: dict | None = None,
) -> dict:
    require_employee(conn, source)
    aid = approval_id or f"approval-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    ts = now()
    detail = {
        "request_reason": reason,
        "target": target,
        "risk": risk,
        "evidence": evidence,
        "requested_by": source,
        "metadata": metadata or {},
    }
    conn.execute(
        """
        INSERT INTO approvals(id, source_agent, action, status, reason, created_at, updated_at)
        VALUES (?, ?, ?, 'pending', ?, ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (aid, source, action, json.dumps(detail, ensure_ascii=False), ts, ts),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM approvals WHERE id = ?", (aid,)).fetchone()
    approval = normalize_approval(row)
    path = write_approval_state(approval)
    audit(conn, source, "approval.request", aid, approval)
    return {"approval": approval, "file": path}


def approved_approval(conn: sqlite3.Connection, approval_id: str, action: str, source: str, target: str) -> dict | None:
    if not approval_id:
        return None
    row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    if not row:
        return None
    approval = normalize_approval(row)
    detail = approval["detail"]
    if approval["status"] != "approved":
        return None
    if approval["action"] != action:
        return None
    if detail.get("requested_by") and detail["requested_by"] != source:
        return None
    if target and detail.get("target") and detail["target"] != target:
        return None
    return approval


def find_matching_approval(conn: sqlite3.Connection, action: str, source: str, target: str, metadata: dict) -> dict | None:
    candidates = conn.execute(
        "SELECT * FROM approvals WHERE status = 'approved' AND source_agent = ? AND action = ? ORDER BY updated_at DESC",
        (source, action),
    ).fetchall()
    for row in candidates:
        approval = normalize_approval(row)
        detail = approval["detail"]
        approval_metadata = detail.get("metadata", {}) if isinstance(detail.get("metadata", {}), dict) else {}
        if target and detail.get("target") and detail["target"] != target:
            continue
        if metadata.get("task_id") and approval_metadata.get("task_id") and approval_metadata["task_id"] != metadata["task_id"]:
            continue
        if metadata.get("adapter") and approval_metadata.get("adapter") and approval_metadata["adapter"] != metadata["adapter"]:
            continue
        return approval
    return None


def require_approval(
    *,
    source: str,
    target: str,
    action: str,
    reason: str,
    risk: str,
    evidence: str,
    metadata: dict,
    approval_id: str = "",
) -> dict:
    conn = connect()
    approved = approved_approval(conn, approval_id, action, source, target) if approval_id else None
    if not approved:
        approved = find_matching_approval(conn, action, source, target, metadata)
    if approved:
        return {"allowed": True, "approval": approved}
    pending_id = approval_id or f"approval-{metadata.get('adapter', 'adapter')}-{metadata.get('task_id', datetime.now().strftime('%Y%m%d-%H%M%S'))}-{action}"
    request = create_approval_request(
        conn,
        source=source,
        action=action,
        reason=reason,
        target=target,
        risk=risk,
        evidence=evidence,
        approval_id=pending_id,
        metadata=metadata,
    )
    return {"allowed": False, "approval_request": request["approval"], "file": request["file"]}
