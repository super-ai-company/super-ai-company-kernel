from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .db_paths import ensure_db_parent, resolve_db_path


ROOT = Path(os.environ.get("OPENCLAW_COMPANY_KERNEL_ROOT", Path(__file__).resolve().parents[1])).resolve()
DB_PATH = resolve_db_path(ROOT)
SCHEMA = ROOT / "company_kernel" / "schema.sql"
APPROVAL_STATE_DIR = ROOT / "state" / "approvals"
APPROVAL_STATUSES = {"pending", "approved", "denied"}
POLICY_PATH = ROOT / "config" / "policy.json"


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(ensure_db_parent(DB_PATH))
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


def load_policy_config() -> dict:
    if not POLICY_PATH.exists():
        return {}
    try:
        payload = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


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


def metadata_matches(expected: dict, actual: dict) -> bool:
    for key, value in expected.items():
        if str(actual.get(key, "")) != str(value):
            return False
    return True


def evaluate_auto_approval_rules(action: str, source: str, target: str, metadata: dict, risk: str = "") -> dict | None:
    route = load_policy_config().get("route_approval", {})
    rules = route.get("auto_approval_rules", []) if isinstance(route, dict) else []
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("enabled") is False:
            continue
        if rule.get("action") and str(rule["action"]) != action:
            continue
        if rule.get("source") and str(rule["source"]) != source:
            continue
        if rule.get("target") and str(rule["target"]) != target:
            continue
        priority = str(metadata.get("priority", "") or "")
        if priority and priority in {str(item) for item in rule.get("priority_not_in", [])}:
            continue
        if risk and risk in {str(item) for item in rule.get("risk_not_in", [])}:
            continue
        expected_metadata = rule.get("metadata", {})
        if isinstance(expected_metadata, dict) and not metadata_matches(expected_metadata, metadata):
            continue
        return rule
    return None


def create_auto_approval(
    conn: sqlite3.Connection,
    *,
    source: str,
    action: str,
    reason: str,
    target: str,
    risk: str,
    evidence: str,
    metadata: dict,
    rule: dict,
) -> dict:
    aid = f"approval-auto-{rule.get('id', 'rule')}-{metadata.get('task_id', uuid.uuid4().hex[:6])}-{action}"
    ts = now()
    detail = {
        "request_reason": reason,
        "target": target,
        "risk": risk,
        "evidence": evidence,
        "requested_by": source,
        "metadata": metadata,
        "approval_mode": "auto_approved",
        "auto_rule_id": str(rule.get("id", "")),
    }
    conn.execute(
        """
        INSERT INTO approvals(id, source_agent, action, status, reason, created_at, updated_at)
        VALUES (?, ?, ?, 'approved', ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET status = 'approved', reason = excluded.reason, updated_at = excluded.updated_at
        """,
        (aid, source, action, json.dumps(detail, ensure_ascii=False), ts, ts),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM approvals WHERE id = ?", (aid,)).fetchone()
    approval = normalize_approval(row)
    path = write_approval_state(approval)
    audit(conn, source, "approval.auto_approved", aid, approval)
    approval["file"] = path
    return approval


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
    try:
        approved = approved_approval(conn, approval_id, action, source, target) if approval_id else None
        if not approved:
            approved = find_matching_approval(conn, action, source, target, metadata)
        if approved:
            return {"allowed": True, "approval": approved}
        auto_rule = evaluate_auto_approval_rules(action, source, target, metadata, risk)
        if auto_rule:
            approval = create_auto_approval(conn, source=source, action=action, reason=reason, target=target, risk=risk, evidence=evidence, metadata=metadata, rule=auto_rule)
            return {"allowed": True, "approval": approval}
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
    finally:
        conn.close()
