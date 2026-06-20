from __future__ import annotations

import argparse
import contextlib
import fnmatch
import hashlib
import io
import json
import mimetypes
import os
import re
import shutil
import secrets
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db_paths import ensure_db_parent
from . import sandboxing
from . import project_memory
from .schema_migrations import ensure_schema_migrations
# Time/datetime primitives now live in company_kernel.core (split phase 0.5). core has NO dependency
# on companyctl, so this is a plain top-level import — no lazy-import workaround needed. Re-exported
# here so every existing `companyctl.now(...)` / `companyctl.seconds_since(...)` caller is unchanged.
from .core import now, future_seconds, new_trace_id, parse_time, parse_iso_datetime, seconds_since  # noqa: F401 (facade re-export)
from .core.db import rows  # noqa: F401 (facade re-export; DB query primitive)
from .core.events import record_event, audit, emit, trace_id_for_task  # noqa: F401 (facade re-export; event/audit/output primitives)
# Pure config-file readers now live in company_kernel.core.config (split: config-layer first cut).
# They take a resolved path and only read+parse JSON; the path globals + resolve_kernel_paths stay HERE
# as mock anchors. The old companyctl names below are kept as thin wrappers that assemble the path.
from .core import config as _core_config
# Notification SEND primitives now live in company_kernel.notify (split: notify-domain cut). They are
# pure transport (no DB, no config globals) and re-exported so every caller is unchanged. Notifications
# Dispatcher + the config-entangled trio (notification_settings/update_notification_settings/
# notification_send_result) stay here: they call the senders by bare name, and the suite patches
# `companyctl.send_*` to intercept — which only reaches lookups resolved in companyctl's namespace.
from .notify import (  # noqa: F401 (facade re-export; notification send primitives)
    resolve_notification_target, applescript_quote, send_macos_notification,
    send_telegram_notification, send_slack_webhook,
)
# Pure progress-transition helpers now live in company_kernel.progress (split: progress cut). Plain
# forward import (no wrapper); deliver_pending_progress_notifications still calls them through here.
# The fingerprint must stay byte-identical — dedup keys on it (guarded by a golden test).
from .progress import (  # noqa: F401 (facade re-export; progress notification helpers)
    PROGRESS_TRANSITION_MESSAGES, progress_notification_message,
    progress_notification_decision, progress_notification_fingerprint,
)
# Pure unit-economics estimators now live in company_kernel.economics (split: economics pure cut).
# Plain forward import (no wrapper); compute_economics/compute_cost_dashboard still call them here.
# pricing is estimation-only — these take the pricing/rates dict in, never read config.
from .economics import classify_task_type, estimate_task_cost, build_cost_dashboard, build_economics  # noqa: F401 (facade re-export)
# Pure approval-classification helpers now live in company_kernel.approval (split: approval pure cut).
# Plain forward — feeds both external qualified callers (company_dashboard.approval_control_summary)
# and companyctl's bare-name calls (normalize_approval→approval_detail, CLI→approval_control_summary).
from .approval import (  # noqa: F401 (facade re-export; approval pure cluster)
    HIGH_RISK_APPROVAL_ACTIONS, approval_detail, approval_is_high_risk, approval_control_summary,
)
# Pure parsing / field-extraction leaves now live in company_kernel.parsing (pure-leaf sweep batch 1).
# Plain forward; bare-name callers in companyctl resolve through it. parse_json_output joined because
# parse_openclaw_agent_reply depends on it (keeps parsing.py a clean leaf, no reverse import).
from .parsing import (  # noqa: F401 (facade re-export; pure parsing leaves)
    parse_json_arg, parse_json_output, parse_openclaw_agent_reply,
    _openclaw_native_result_task_id, _openclaw_native_result_agent,
    _openclaw_native_result_summary, _openclaw_native_result_evidence,
)

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
GLOBAL_CONFIG_PATH = Path("~/.gemini/antigravity/company_kernel_config.json")


def load_global_config(path: Path | None = None) -> dict:
    # Thin wrapper: resolve the path (env override / arg / default), then delegate to the pure reader.
    raw_path = str(os.environ.get("COMPANY_KERNEL_CONFIG_PATH", "") or "").strip()
    config_path = path or (Path(raw_path).expanduser() if raw_path else GLOBAL_CONFIG_PATH.expanduser())
    return _core_config.load_global_config(config_path)


def resolve_kernel_paths(default_root: Path) -> dict[str, Path | int | dict]:
    config = load_global_config()
    env_root = str(os.environ.get("OPENCLAW_COMPANY_KERNEL_ROOT", "") or "").strip()
    root_value = config.get("master_workspace_root") or env_root or str(default_root)
    root = Path(str(root_value)).expanduser().resolve()
    db_value = str(os.environ.get("COMPANY_KERNEL_DB_PATH", "") or config.get("database_path") or root / "company.sqlite")
    log_value = str(config.get("log_dir") or root / "logs")
    return {
        "config": config,
        "root": root,
        "db_path": Path(db_value).expanduser().resolve(),
        "employees_dir": root / "employees",
        "state_dir": root / "state",
        "rfc_dir": root / "rfcs",
        "config_dir": root / "config",
        "log_dir": Path(log_value).expanduser().resolve(),
        "gateway_port": int(config.get("gateway_port", 0) or 0),
    }


_KERNEL_PATHS = resolve_kernel_paths(DEFAULT_ROOT)
ROOT = Path(_KERNEL_PATHS["root"])


def resolve_db_path() -> Path:
    override = str(os.environ.get("COMPANY_KERNEL_DB_PATH", "") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    config_db = load_global_config().get("database_path")
    if config_db:
        return Path(str(config_db)).expanduser().resolve()
    return Path(_KERNEL_PATHS["db_path"])


DB_PATH = resolve_db_path()
EMPLOYEES_DIR = Path(_KERNEL_PATHS["employees_dir"])
STATE_DIR = Path(_KERNEL_PATHS["state_dir"])
RFC_DIR = Path(_KERNEL_PATHS["rfc_dir"])
CONFIG_DIR = Path(_KERNEL_PATHS["config_dir"])
WORKFLOW_DIR = CONFIG_DIR / "workflows"
SKILL_PACKAGES_DIR = ROOT / "skill-packages"
LAUNCHD_LABEL = "ai.openclaw.company-kernel.daemon"
LAUNCHD_TEMPLATE = CONFIG_DIR / "launchd" / f"{LAUNCHD_LABEL}.plist"
HOOKS_PATH = CONFIG_DIR / "hooks.json"
COMMUNICATIONS_PATH = CONFIG_DIR / "company_communications.json"
POLICY_PATH = CONFIG_DIR / "policy.json"
PROTECTED_PATHS_CONFIG = CONFIG_DIR / "protected_paths.json"
APPROVAL_STATE_DIR = STATE_DIR / "approvals"
FOLLOWUP_STATE_DIR = STATE_DIR / "followups"
SUPERVISOR_STATE_DIR = STATE_DIR / "supervisor"
TASK_WORKSPACE_ROOT = STATE_DIR / "task-workspaces"
SCHEMA = ROOT / "company_kernel" / "schema.sql"
_OPEN_CONNECTIONS: list[sqlite3.Connection] = []

EVIDENCE_DISPLAY_ALLOWED_NAMES = {"evidence", "reports", "artifacts", "final"}
EVIDENCE_DISPLAY_FORBIDDEN_PARTS = {".ssh", ".env", "config", "profile", "api_key", "api-key", "apikey", "secrets", "secret"}

KNOWN_RUNTIMES = {
    "openclaw": "OpenClaw runtime adapter",
    "hermes": "Hermes local runtime adapter",
    "codex": "Codex CLI / openclaw-codex-controller adapter",
    "claude": "Claude Code / Claude CLI adapter",
    "gemini": "Gemini via antigravity-claude-proxy (Claude-compatible, runs on the claude adapter)",
    "trae": "Trae IDE/Agent adapter",
    "antigravity": "Google Antigravity adapter",
    "skill": "Packaged Skill runtime adapter",
    "local": "Local script/manual adapter",
}

APPROVAL_STATUSES = {"pending", "approved", "denied"}

DEFAULT_ROUTE_APPROVAL_ACTIONS = {
    "payment": ["payment", "pay ", "付款", "支付", "打款"],
    "compensation": ["compensation", "赔偿", "赔付", "押金", "保险", "事故"],
    "salary": ["salary", "工资", "薪资"],
    "penalty": ["penalty", "处罚", "罚款"],
    "external_send": ["external send", "外发", "发送给客户", "发给客户", "发布", "publish"],
    "production_deploy": ["production deploy", "deploy", "上线", "生产部署"],
    "secret_change": ["secret", "token", "password", "密钥", "密码"],
    "kernel_change": ["kernel", "schema", "approval rule", "内核", "审批规则", "通信协议"],
}


PROGRESS_LAYER_DEFINITIONS = {
    "received": {
        "states": {"received", "acknowledged", "ack", "claimed"},
        "label": "acknowledged",
    },
    "working": {
        "states": {"working", "in_progress", "actively_progressing", "active", "running"},
        "label": "actively_progressing",
    },
    "waiting": {
        "states": {"waiting", "blocked_on_input_or_dependency", "awaiting_input", "awaiting_dependency", "pending_input"},
        "label": "blocked_on_input_or_dependency",
    },
    "blocked": {
        "states": {"blocked", "failed_to_progress", "error", "stalled", "failed"},
        "label": "failed_to_progress",
    },
    "done": {
        "states": {"done", "verified_complete", "completed", "complete", "success"},
        "label": "verified_complete",
    },
}

def normalize_progress_state(state: str, *, summary: str = "") -> dict[str, str]:
    raw_state = str(state or "").strip()
    normalized = raw_state.lower().replace("-", "_").replace(" ", "_")
    for layer, config in PROGRESS_LAYER_DEFINITIONS.items():
        if normalized in config["states"]:
            return {
                "layer": layer,
                "state": normalized,
                "label": str(config["label"]),
                "summary": str(summary or ""),
            }
    return {
        "layer": "",
        "state": normalized,
        "label": "",
        "summary": str(summary or ""),
    }


def extract_progress_payload(payload: object) -> dict[str, str]:
    if isinstance(payload, dict):
        nested = payload.get("progress")
        if isinstance(nested, dict):
            state = str(nested.get("state") or nested.get("status") or "")
            summary = str(nested.get("summary") or nested.get("message") or payload.get("summary") or "")
            return normalize_progress_state(state, summary=summary)
        report = payload.get("report")
        if isinstance(report, dict):
            state = str(report.get("state") or report.get("status") or payload.get("state") or payload.get("status") or "")
            summary = str(report.get("action") or report.get("summary") or payload.get("summary") or payload.get("message") or "")
            if state:
                return normalize_progress_state(state, summary=summary)
        state = str(payload.get("state") or payload.get("status") or "")
        summary = str(payload.get("summary") or payload.get("message") or "")
        if state:
            return normalize_progress_state(state, summary=summary)
    if isinstance(payload, str):
        return normalize_progress_state(payload)
    return normalize_progress_state("")


def active_task_for_agent(conn: sqlite3.Connection, agent: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM tasks
        WHERE target_agent = ?
          AND status IN ('submitted', 'claimed')
        ORDER BY updated_at DESC, created_at DESC
        LIMIT 1
        """,
        (agent,),
    ).fetchone()


def report_progress_task_id(payload: dict) -> str:
    report = payload.get("report") if isinstance(payload.get("report"), dict) else {}
    return str(payload.get("task_id") or report.get("task_id") or "").strip()


def workspace_progress_files(workspace: Path) -> list[Path]:
    reports = workspace / "reports"
    if not reports.exists():
        return []
    return sorted(reports.glob("progress_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)


def latest_workspace_progress(workspace: str, *, task_id: str = "") -> tuple[str, dict]:
    workspace_path = Path(workspace or "").expanduser()
    if not workspace_path.exists():
        return "", {}
    for path in workspace_progress_files(workspace_path):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        report = payload.get("report")
        if not isinstance(report, dict):
            continue
        if task_id and report_progress_task_id(payload) != task_id:
            continue
        return str(path.resolve()), payload
    return "", {}


def progress_bridge_metadata(conn: sqlite3.Connection, agent: str, workspace: str, metadata_payload: dict) -> dict:
    enriched = dict(metadata_payload)
    task_id = str(enriched.get("task_id", "") or "").strip()
    if not task_id:
        task = active_task_for_agent(conn, agent)
        if task:
            task_id = str(task["id"])
            enriched["task_id"] = task_id
    if not workspace:
        return enriched
    progress_path, progress_payload = latest_workspace_progress(workspace, task_id=task_id)
    if not progress_path:
        return enriched
    progress = extract_progress_payload(progress_payload)
    if progress.get("layer"):
        enriched["progress"] = {
            "layer": progress["layer"],
            "state": progress["state"],
            "label": progress["label"],
            "summary": progress["summary"],
        }
    enriched["latest_progress"] = {
        "task_id": task_id or report_progress_task_id(progress_payload),
        "path": progress_path,
        "layer": progress.get("layer", ""),
        "state": progress.get("state", ""),
        "label": progress.get("label", ""),
        "summary": progress.get("summary", ""),
    }
    return enriched



def has_progress_notification_fingerprint(conn: sqlite3.Connection, fingerprint: str) -> bool:
    if not fingerprint:
        return False
    for row in rows(
        conn,
        "SELECT payload_json FROM company_events WHERE event_type = 'progress.notification' ORDER BY created_at DESC LIMIT 200",
    ):
        try:
            payload = json.loads(row.get("payload_json", "{}") or "{}")
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict) and str(payload.get("fingerprint", "") or "") == fingerprint:
            return True
    return False


def maybe_record_progress_transition(conn: sqlite3.Connection, agent: str, previous_progress: dict[str, str], current_progress: dict[str, str], *, task_id: str = "", trace_id: str = "", source: str = "heartbeat") -> dict:
    previous_layer = previous_progress.get("layer", "")
    current_layer = current_progress.get("layer", "")
    if not previous_layer or not current_layer:
        return {"triggered": False}
    if previous_layer == current_layer and previous_progress.get("state", "") == current_progress.get("state", ""):
        return {"triggered": False}
    decision = progress_notification_decision(agent, previous_progress, current_progress, source=source)
    fingerprint = progress_notification_fingerprint(agent, previous_progress, current_progress, task_id=task_id)
    if has_progress_notification_fingerprint(conn, fingerprint):
        return {"triggered": False, "duplicate": True, "fingerprint": fingerprint}
    queue_item = {
        "kind": decision["kind"],
        "agent_id": agent,
        "task_id": task_id,
        "trace_id": trace_id,
        "trigger": source,
        "triggered_at": now(),
        "triggered_by": agent,
        "from_layer": decision["from_layer"],
        "from_state": decision["from_state"],
        "to_layer": decision["to_layer"],
        "to_state": decision["to_state"],
        "message": decision["message"],
        "summary": decision["summary"],
        "reason": decision["reason"],
        "delivery_status": "pending",
        "channel": "repo-only",
        "account": "",
        "target": "",
        "fingerprint": fingerprint,
    }
    event = record_event(conn, "progress.notification", agent, task_id=task_id, payload=queue_item, trace_id=trace_id)
    return {
        "triggered": True,
        **queue_item,
        "event_id": event["id"],
    }


def list_progress_notifications(conn: sqlite3.Connection, *, pending_only: bool = False, limit: int = 20) -> list[dict]:
    where = ["event_type = 'progress.notification'"]
    if pending_only:
        where.append("processed_at = ''")
    rows_out = rows(conn, f"SELECT * FROM company_events WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT ?", (limit,))
    items: list[dict] = []
    for row in rows_out:
        try:
            payload = json.loads(row.get("payload_json", "{}") or "{}")
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        items.append(
            {
                **payload,
                "event_id": row.get("id", ""),
                "created_at": row.get("created_at", ""),
                "processed_at": row.get("processed_at", ""),
                "pending": not bool(row.get("processed_at", "")),
                "trace_id": row.get("trace_id", ""),
                "source_agent": row.get("source_agent", ""),
            }
        )
    return items


def update_company_event_payload(conn: sqlite3.Connection, event_id: str, payload: dict, *, processed: bool = False) -> None:
    if processed:
        conn.execute(
            "UPDATE company_events SET payload_json = ?, processed_at = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), now(), event_id),
        )
    else:
        conn.execute(
            "UPDATE company_events SET payload_json = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), event_id),
        )
    conn.commit()


def progress_notification_delivery_enabled() -> bool:
    settings = notification_settings()
    employee_notifications = settings.get("employee_notifications", {})
    return bool(employee_notifications.get("enabled"))


def deliver_pending_progress_notifications(conn: sqlite3.Connection, *, limit: int = 20) -> dict:
    pending = list_progress_notifications(conn, pending_only=True, limit=limit)
    results: list[dict] = []
    counts = {"pending": len(pending), "sent": 0, "skipped": 0, "failed": 0}
    for item in pending:
        message = str(item.get("message", "") or "").strip()
        result = notification_send_result(message=message, kind="general")
        updated = dict(item)
        updated["delivery_attempted_at"] = now()
        updated["account"] = str(result.get("account", updated.get("account", "")) or "")
        updated["target"] = str(result.get("target", updated.get("target", "")) or "")
        updated["channel"] = str(result.get("platform", updated.get("channel", "")) or updated.get("channel", ""))
        updated["delivery_result"] = result
        updated["delivery_error"] = str(result.get("error", "") or result.get("reason", "") or "")
        if result.get("ok") and not result.get("skipped"):
            updated["delivery_status"] = "sent"
            updated["delivered_at"] = now()
            counts["sent"] += 1
        elif result.get("skipped"):
            updated["delivery_status"] = "skipped"
            updated["delivered_at"] = now()
            counts["skipped"] += 1
        else:
            updated["delivery_status"] = "failed"
            counts["failed"] += 1
        update_company_event_payload(conn, str(item.get("event_id", "") or ""), updated, processed=True)
        results.append(updated)
    return {"ok": True, "counts": counts, "items": results}


def supervisor_loop_result_path() -> Path:
    return SUPERVISOR_STATE_DIR / "latest_delivery_loop.json"


def load_latest_supervisor_loop_result() -> dict:
    return load_json_or_default(supervisor_loop_result_path(), {})


def save_latest_supervisor_loop_result(result: dict) -> str:
    path = supervisor_loop_result_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


def supervisor_delivery_decision(item: dict, *, escalate_after_attempts: int = 2) -> tuple[str, int]:
    attempts = int(item.get("supervisor_attempts", 0) or 0) + 1
    if attempts >= escalate_after_attempts:
        return "escalate_ready", attempts
    return "retry_ready", attempts


def run_supervisor_delivery_loop(conn: sqlite3.Connection, *, limit: int = 20, actor: str = "supervisor-loop") -> dict:
    started_at = now()
    pending_before = list_progress_notifications(conn, pending_only=True, limit=limit)
    delivery = deliver_pending_progress_notifications(conn, limit=limit)
    candidate_items: list[dict] = list(delivery.get("items", []))
    if not candidate_items:
        candidate_items = [
            item
            for item in list_progress_notifications(conn, pending_only=False, limit=limit)
            if str(item.get("delivery_status", "") or "") == "failed"
            and str(item.get("supervisor_decision", "") or "") != "escalate_ready"
        ]
    counts = {
        "scanned": len(candidate_items) if candidate_items else len(pending_before),
        "sent": int(delivery.get("counts", {}).get("sent", 0) or 0),
        "skipped": int(delivery.get("counts", {}).get("skipped", 0) or 0),
        "failed": 0,
        "retry_ready": 0,
        "escalate_ready": 0,
    }
    updated_items: list[dict] = []
    for item in candidate_items:
        if str(item.get("delivery_status", "") or "") != "failed":
            updated_items.append(item)
            continue
        counts["failed"] += 1
        decision, attempts = supervisor_delivery_decision(item)
        updated = dict(item)
        updated["supervisor_checked_at"] = now()
        updated["supervisor_decision"] = decision
        updated["supervisor_attempts"] = attempts
        updated["supervisor_summary"] = "待重试" if decision == "retry_ready" else "待升级"
        update_company_event_payload(conn, str(item.get("event_id", "") or ""), updated, processed=not bool(updated.get("pending")))
        counts[decision] += 1
        updated_items.append(updated)
    result = {
        "ok": True,
        "started_at": started_at,
        "completed_at": now(),
        "actor": actor,
        "counts": counts,
        "items": updated_items,
    }
    result["file"] = save_latest_supervisor_loop_result(result)
    return result


def sync_backlog_from_queue_file(conn: sqlite3.Connection) -> None:
    queue_path = ROOT / ".ops" / "super-ai-company-kernel-queue.json"
    if not queue_path.exists():
        return
    try:
        data = json.loads(queue_path.read_text(encoding="utf-8"))
    except Exception:
        return

    project_id = data.get("project")
    if not project_id:
        return

    backlog = data.get("backlog", [])
    ts = now()
    for item in backlog:
        item_id = item.get("id")
        if not item_id:
            continue

        db_task_id = item.get("task_id") or item_id

        # Check if task already exists in database
        exists = conn.execute("SELECT 1 FROM tasks WHERE id = ?", (db_task_id,)).fetchone()
        if not exists:
            source = "hermes"
            target = item.get("owner", "codex")
            if target == "hermes-main":
                target = "hermes"
            elif target == "hermes-main-rerouted-from-antigravity":
                target = "hermes"
            title = item.get("goal", db_task_id)
            desc = item.get("goal", "")
            priority = item_id.split('-')[0] if '-' in item_id else "P2"

            q_status = item.get("status")
            if q_status == "implemented_verified_workspace":
                db_status = "completed"
            elif q_status == "running":
                db_status = "claimed"
            elif q_status == "submitted":
                db_status = "submitted"
            else:
                db_status = q_status or "submitted"

            evidence_rel = item.get("evidence", "")
            evidence_abs = ""
            if evidence_rel:
                evidence_abs = str((ROOT / evidence_rel).resolve())

            # route imported backlog through the same normalization as a fresh dispatch (app→cli reroute
            # / executor lock / 记忆会话 stamp) so a synced pending task reaches a real cli worker and
            # carries project memory instead of stranding on a passive app.
            target, desc, _norm_err = normalize_submission(conn, target=target, description=desc)
            if _norm_err is not None:
                continue  # the project lock forbids this target — skip importing a forbidden task
            conn.execute(
                """
                INSERT INTO tasks (id, source_agent, target_agent, title, description, priority, status, claimed_by, summary, evidence_path, blocker, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (db_task_id, source, target, title, desc, priority, db_status, target, "Imported from queue backlog", evidence_abs, "", ts, ts)
            )

        # Link to project
        conn.execute(
            """
            INSERT OR IGNORE INTO project_tasks (project_id, task_id, created_at)
            VALUES (?, ?, ?)
            """,
            (project_id, db_task_id, ts)
        )


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(ensure_db_parent(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    ensure_schema_migrations(conn)
    sync_backlog_from_queue_file(conn)
    conn.commit()
    _OPEN_CONNECTIONS.append(conn)
    return conn


def connect_readonly() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    _OPEN_CONNECTIONS.append(conn)
    return conn


def close_open_connections() -> None:
    while _OPEN_CONNECTIONS:
        conn = _OPEN_CONNECTIONS.pop()
        with contextlib.suppress(sqlite3.Error):
            conn.close()


def database_integrity(conn: sqlite3.Connection) -> dict:
    """Cheap corruption check used by doctor's post-install/self-check. A healthy DB returns
    integrity=['ok'] and zero foreign-key violations. (Backup/restore lives in
    company_kernel/backup.py — this is only the doctor-facing structured report.)"""
    try:
        integ = [r[0] for r in conn.execute("PRAGMA integrity_check").fetchall()]
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.Error as exc:
        return {"ok": False, "integrity": [f"error: {exc}"], "foreign_key_violations": -1}
    return {"ok": integ == ["ok"] and not fk, "integrity": integ, "foreign_key_violations": len(fk)}


# Curated, owner-readable activity feed. The raw company_events ledger is mostly internal plumbing
# (tool.call / budget / runtime.session / artifact / task.attempt). This allowlist collapses it down
# to the events an operator actually wants to watch as ONE chronological "全公司动态" stream: who
# dispatched/ran/finished/blocked work, who messaged whom, who spoke in a meeting, approvals.
FEED_EVENT_ICON = {
    "message.send": "💬", "conversation.message": "🗣", "task.dispatched": "📤",
    "task.managed_run.started": "🛰", "task.done": "✅", "task.blocked": "⛔",
    "task.reopened": "♻️", "task.reassigned": "🔁", "task.retried": "🔁",
    "task.discarded": "🗑", "approval.requested": "🟡", "approval.approved": "👍",
    "progress.notification": "📣",
}


def _feed_snippet(payload: dict) -> str:
    body = str(payload.get("body") or payload.get("summary") or payload.get("message") or "").strip().replace("\n", " ")
    return (body[:50] + "…") if len(body) > 50 else body


def _feed_text(etype: str, actor: str, payload: dict, task_title: str, conv_title: str) -> str:
    snip = _feed_snippet(payload)
    tgt = payload.get("target_agent") or ""
    if etype == "task.dispatched":
        return f"{actor} 派单给 {tgt}「{task_title or payload.get('title','')}」"
    if etype == "message.send":
        return f"{actor} → {tgt}:{snip}" if tgt else f"{actor}:{snip}"
    if etype == "conversation.message":
        where = f"「{conv_title}」" if conv_title else "会议"
        return f"{actor} 在{where}发言:{snip}"
    if etype == "task.managed_run.started":
        return f"{actor} 开始执行「{task_title}」" if task_title else f"{actor} 开始执行任务"
    if etype == "task.done":
        return f"{actor} 完成「{task_title}」" + (f":{snip}" if snip else "")
    if etype == "task.blocked":
        return f"{actor} 受阻「{task_title}」" + (f":{snip}" if snip else "")
    if etype == "task.reopened":
        return f"{actor} 重开「{task_title}」"
    if etype in ("task.reassigned", "task.retried"):
        verb = "改派" if etype == "task.reassigned" else "重试"
        return f"{verb}「{task_title}」"
    if etype == "task.discarded":
        return f"{actor} 丢弃「{task_title}」"
    if etype == "approval.requested":
        return f"{actor} 申请审批「{task_title}」"
    if etype == "approval.approved":
        return f"批准「{task_title}」"
    if etype == "progress.notification":
        return f"进度上报:{snip}" if snip else "进度上报"
    return f"{actor} {etype}"


def company_feed(conn: sqlite3.Connection, limit: int = 40) -> list[dict]:
    """One human-readable, chronological stream of what the company is doing — the unified
    '谁派活/谁执行/谁开会/谁说了啥' view the console Overview shows so flows aren't scattered."""
    types = tuple(FEED_EVENT_ICON)
    qmarks = ",".join("?" * len(types))
    # id is the tiebreaker so same-second events keep a stable order across refreshes (ids embed a
    # monotonic timestamp+seq, so id DESC ≈ insertion order within a second).
    evs = rows(conn, f"SELECT * FROM company_events WHERE event_type IN ({qmarks}) "
                     f"ORDER BY created_at DESC, id DESC LIMIT ?", (*types, limit))
    parsed = []
    task_ids, conv_ids = set(), set()
    for e in evs:
        try:
            payload = json.loads(e["payload_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            payload = {}
        if e["task_id"]:
            task_ids.add(e["task_id"])
        if payload.get("conversation_id"):
            conv_ids.add(payload["conversation_id"])
        parsed.append((e, payload))
    titles, conv_titles = {}, {}
    if task_ids:
        qm = ",".join("?" * len(task_ids))
        for r in rows(conn, f"SELECT id, title FROM tasks WHERE id IN ({qm})", tuple(task_ids)):
            titles[r["id"]] = r["title"]
    if conv_ids:
        qm = ",".join("?" * len(conv_ids))
        for r in rows(conn, f"SELECT id, title FROM conversations WHERE id IN ({qm})", tuple(conv_ids)):
            conv_titles[r["id"]] = r["title"]
    out = []
    for e, payload in parsed:
        etype = e["event_type"]
        cid = payload.get("conversation_id", "")
        out.append({
            "ts": e["created_at"], "icon": FEED_EVENT_ICON.get(etype, "·"),
            "event_type": etype, "actor": e["source_agent"] or "",
            "text": _feed_text(etype, e["source_agent"] or "", payload, titles.get(e["task_id"], ""), conv_titles.get(cid, "")),
            "task_id": e["task_id"] or "", "conversation_id": cid,
        })
    return out


def company_priority_queue(conn: sqlite3.Connection, *, stale_minutes: int = 30, limit: int = 60) -> list[dict]:
    """The console's first-screen "what needs you" queue: pending approvals (top), then blocked tasks,
    then timed-out/stale tasks. Merged to one row per object (key = kind+id, highest severity wins),
    sorted by severity, then OLDEST-waiting first within a severity (the longest-waiting item surfaces),
    each carrying a primary in-place action set. Pure SQL — free."""
    now_dt = datetime.fromisoformat(now())

    def age_min(ts: object) -> int | None:
        try:
            return max(0, int((now_dt - datetime.fromisoformat(str(ts))).total_seconds() // 60))
        except (ValueError, TypeError):
            return None

    by_key: dict[tuple, dict] = {}

    def add(kind: str, oid: str, title: str, severity: int, label: str, ts: str, actions: list[str], target: str = "") -> None:
        key = (kind, oid)
        prev = by_key.get(key)
        if prev is None or severity < prev["severity"]:
            t = str(title or oid).replace("\n", " ").strip()
            by_key[key] = {"kind": kind, "id": oid, "title": (t[:60] + "…") if len(t) > 60 else t,
                           "severity": severity, "label": label, "ts": ts or "", "age_minutes": age_min(ts),
                           "actions": actions, "target": target or ""}

    for a in rows(conn, "SELECT * FROM approvals WHERE status='pending' ORDER BY created_at DESC LIMIT ?", (limit,)):
        add("approval", a["id"], a["action"] or a["reason"] or a["id"], 1, "待审批", a["created_at"], ["approve", "deny"], a["source_agent"])
    for t in rows(conn, "SELECT id,title,target_agent,updated_at,created_at FROM tasks WHERE status IN ('blocked','failed','interrupted') ORDER BY updated_at DESC LIMIT ?", (limit,)):
        add("task", t["id"], t["title"], 2, "阻塞", t["updated_at"] or t["created_at"], ["reopen", "read"], t["target_agent"])
    for t in rows(conn, "SELECT id,title,target_agent,updated_at,created_at,status FROM tasks WHERE status IN ('submitted','stale','stalled')"):
        ts = t["updated_at"] or t["created_at"]
        am = age_min(ts)
        if t["status"] in ("stale", "stalled") or (t["status"] == "submitted" and am is not None and am >= stale_minutes):
            add("task", t["id"], t["title"], 3, "超时", ts, ["nudge", "reassign", "read"], t["target_agent"])

    return sorted(by_key.values(), key=lambda x: (x["severity"], x["ts"]))[:limit]


def safe_path_token(value: str) -> str:
    token = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(value or ""))
    return token.strip("._-") or "task"


def require_task(conn: sqlite3.Connection, task_id: str) -> dict:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        raise SystemExit(f"task not found: {task_id}")
    return dict(row)


def workspace_dirs_for(path: Path) -> dict[str, str]:
    return {name: str(path / name) for name in ["input", "work", "artifacts", "evidence", "final"]}


def write_workspace_manifest(task_id: str, trace_id: str, path: Path) -> Path:
    manifest_path = path / "manifest.json"
    files = []
    if path.exists():
        for item in sorted(p for p in path.rglob("*") if p.is_file() and p.name != "manifest.json"):
            try:
                rel_path = str(item.relative_to(path))
            except ValueError:
                rel_path = str(item)
            files.append({"path": rel_path, "size": item.stat().st_size, "checksum": sha256_file(item)})
    manifest = {
        "task_id": task_id,
        "trace_id": trace_id,
        "workspace_path": str(path),
        "dirs": workspace_dirs_for(path),
        "files": files,
        "updated_at": now(),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def ensure_task_workspace(conn: sqlite3.Connection, task_id: str, trace_id: str = "") -> dict:
    require_task(conn, task_id)
    task_trace_id = trace_id or trace_id_for_task(conn, task_id)
    workspace_path = (TASK_WORKSPACE_ROOT / f"task_{safe_path_token(task_id)}").resolve()
    for subdir in ["input", "work", "artifacts", "evidence", "final"]:
        (workspace_path / subdir).mkdir(parents=True, exist_ok=True)
    manifest_path = write_workspace_manifest(task_id, task_trace_id, workspace_path)
    ts = now()
    conn.execute(
        """
        INSERT INTO task_workspaces(task_id, trace_id, path, manifest_path, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(task_id) DO UPDATE SET
          trace_id = excluded.trace_id,
          path = excluded.path,
          manifest_path = excluded.manifest_path,
          updated_at = excluded.updated_at
        """,
        (task_id, task_trace_id, str(workspace_path), str(manifest_path), ts, ts),
    )
    conn.commit()
    return {"task_id": task_id, "trace_id": task_trace_id, "path": str(workspace_path), "manifest_path": str(manifest_path), "dirs": workspace_dirs_for(workspace_path)}


def task_workspace(conn: sqlite3.Connection, task_id: str) -> dict:
    row = conn.execute("SELECT * FROM task_workspaces WHERE task_id = ?", (task_id,)).fetchone()
    if row:
        return dict(row)
    return ensure_task_workspace(conn, task_id, trace_id_for_task(conn, task_id))


def require_workspace_path(conn: sqlite3.Connection, task_id: str, raw_path: str) -> Path:
    workspace = task_workspace(conn, task_id)
    root = Path(os.path.realpath(Path(workspace["path"]).expanduser()))
    candidate = Path(os.path.realpath(Path(raw_path).expanduser()))
    def macos_var_aliases(path: Path) -> set[str]:
        text = str(path)
        aliases = {text}
        if text.startswith("/private/var/"):
            aliases.add(text.replace("/private/var/", "/var/", 1))
        elif text.startswith("/var/"):
            aliases.add(text.replace("/var/", "/private/var/", 1))
        return aliases

    root_aliases = macos_var_aliases(root)
    candidate_aliases = macos_var_aliases(candidate)
    allowed = False
    for root_alias in root_aliases:
        for candidate_alias in candidate_aliases:
            if candidate_alias == root_alias or candidate_alias.startswith(root_alias.rstrip("/") + "/"):
                allowed = True
                break
        if allowed:
            break
    if not allowed:
        raise ValueError(f"artifact path must be inside task workspace: {candidate}")
    return candidate


def evidence_display_roots() -> list[Path]:
    roots = [
        TASK_WORKSPACE_ROOT,
        STATE_DIR / "evidence",
        STATE_DIR / "reports",
        STATE_DIR / "artifacts",
        ROOT / "workspace",
        ROOT / "reports",
        ROOT / "artifacts",
        ROOT / "evidence",
    ]
    employees_dir = EMPLOYEES_DIR
    if employees_dir.exists():
        roots.extend(path / "reports" for path in employees_dir.iterdir() if path.is_dir())
    return [path.resolve() for path in roots]


def sanitize_evidence_path_for_display(raw_path: str) -> dict:
    value = str(raw_path or "").strip()
    policy = {
        "summary": "workspace/evidence/reports/artifacts/final only; absolute paths and secret/config paths stay hidden",
        "allowed_segments": sorted(EVIDENCE_DISPLAY_ALLOWED_NAMES),
        "forbidden_policy": "sensitive_path_tokens_redacted",
    }
    result = {
        "path": "",
        "relative_path": "",
        "basename": "",
        "exists": False,
        "allowed": False,
        "reason": "",
        "checksum": "",
        "absolute_path_exposed": False,
        "policy": policy,
    }
    if not value:
        result["reason"] = "empty"
        return result
    if "\x00" in value or ".." in Path(value).parts:
        result["reason"] = "unsafe path traversal"
        return result
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        result["reason"] = "unresolvable"
        return result
    result["exists"] = resolved.exists() and resolved.is_file()
    parts_lower = {part.lower() for part in resolved.parts}
    if parts_lower & EVIDENCE_DISPLAY_FORBIDDEN_PARTS:
        result["reason"] = "forbidden secret/config path"
        return result
    allowed_root = None
    relative = None
    for root in evidence_display_roots():
        try:
            rel = resolved.relative_to(root)
        except ValueError:
            continue
        allowed_root = root
        relative = rel
        break
    if allowed_root is None or relative is None:
        result["reason"] = "not in allowed evidence roots"
        return result
    if not relative.parts:
        result["reason"] = "evidence path points to root"
        return result
    if not (set(part.lower() for part in resolved.parts) & EVIDENCE_DISPLAY_ALLOWED_NAMES):
        result["reason"] = "missing evidence/reports/artifacts/final segment"
        return result
    result.update(
        {
            "path": str(relative),
            "relative_path": str(relative),
            "basename": resolved.name,
            "exists": resolved.exists() and resolved.is_file(),
            "allowed": True,
            "reason": "allowed",
            "checksum": sha256_file(resolved) if resolved.exists() and resolved.is_file() else "",
            "absolute_path_exposed": False,
        }
    )
    return result


SENSITIVE_LOG_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password|passwd|authorization|bearer)\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"(?i)(?<![A-Za-z0-9_])(sk-[A-Za-z0-9_\-]{12,})"),
    re.compile(r"(?i)(?<![A-Za-z0-9_])(xox[baprs]-[A-Za-z0-9\-]{12,})"),
]


def sanitize_log_text(raw: object, *, max_length: int = 1200) -> str:
    text = "" if raw is None else str(raw)
    if not text:
        return ""
    text = text.replace(str(Path.home()), "~")
    for env_key, env_value in os.environ.items():
        if not env_value or len(env_value) < 8:
            continue
        key_lower = env_key.lower()
        if any(marker in key_lower for marker in ("token", "secret", "password", "passwd", "api_key", "apikey", "authorization")):
            text = text.replace(env_value, "[REDACTED_ENV]")
    text = re.sub(r"(?<![\w.-])/[^\s\"']*(?:\.env|id_rsa|id_ed25519|config|profile|credentials|token)[^\s\"']*", "[REDACTED_PATH]", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<![\w.-])~[^\s\"']*(?:\.env|id_rsa|id_ed25519|config|profile|credentials|token)[^\s\"']*", "[REDACTED_PATH]", text, flags=re.IGNORECASE)
    for pattern in SENSITIVE_LOG_PATTERNS:
        text = pattern.sub(lambda match: f"{match.group(1)}=[REDACTED]" if match.lastindex and match.lastindex >= 2 else "[REDACTED]", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_length:
        text = text[: max_length - 1] + "…"
    return text


def sanitize_json_like(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): sanitize_json_like(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_json_like(item) for item in value]
    if isinstance(value, str):
        return sanitize_log_text(value)
    return value


def sha256_file(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def row_by_id(conn: sqlite3.Connection, table: str, id_column: str, item_id: str) -> dict:
    row = conn.execute(f"SELECT * FROM {table} WHERE {id_column} = ?", (item_id,)).fetchone()
    if not row:
        raise SystemExit(f"{table.rstrip('s')} not found: {item_id}")
    return dict(row)


def register_artifact_internal(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    employee_id: str,
    path: str,
    artifact_type: str,
    name: str = "",
    stage: str = "intermediate",
    summary: str = "",
    is_input: bool = False,
    is_final: bool = False,
    metadata: dict | None = None,
) -> dict:
    task = require_task(conn, task_id)
    require_employee(conn, employee_id)
    artifact_path = require_workspace_path(conn, task_id, path)
    artifact_name = name or artifact_path.name
    trace_id = trace_id_for_task(conn, task_id)
    ts = now()
    previous = rows(
        conn,
        "SELECT * FROM artifacts WHERE task_id = ? AND name = ? AND status NOT IN ('rejected', 'superseded') ORDER BY version ASC",
        (task_id, artifact_name),
    )
    version = (max([int(item.get("version") or 0) for item in previous] or [0]) + 1)
    superseded = []
    for item in previous:
        conn.execute("UPDATE artifacts SET status = 'superseded', updated_at = ? WHERE artifact_id = ?", (ts, item["artifact_id"]))
        superseded.append({**item, "status": "superseded"})
        record_event(conn, "artifact.superseded", employee_id, task_id=task_id, trace_id=trace_id, payload={"artifact_id": item["artifact_id"], "name": artifact_name, "new_version": version})
    artifact_id = f"artifact-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    workspace_path = Path(task_workspace(conn, task_id)["path"])
    snapshot_dir = workspace_path / "artifacts" / safe_path_token(artifact_name)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = snapshot_dir / f"v{version}-{artifact_path.name}"
    if artifact_path.resolve() != snapshot_path.resolve():
        shutil.copy2(artifact_path, snapshot_path)
    mime_type = mimetypes.guess_type(str(snapshot_path))[0] or "application/octet-stream"
    metadata_obj = {**(metadata or {}), "original_path": str(artifact_path)}
    artifact = {
        "artifact_id": artifact_id,
        "trace_id": trace_id,
        "task_id": task_id,
        "parent_task_id": str(task.get("parent_task_id", "") or task_metadata(conn, task_id).get("parent_task_id", "") or ""),
        "employee_id": employee_id,
        "artifact_type": artifact_type,
        "name": artifact_name,
        "path": str(snapshot_path),
        "mime_type": mime_type,
        "stage": stage,
        "version": version,
        "status": "created",
        "is_input": int(is_input),
        "is_output": 0 if is_input else 1,
        "is_final": int(is_final),
        "summary": summary,
        "checksum": sha256_file(snapshot_path),
        "metadata_json": json.dumps(metadata_obj, ensure_ascii=False),
        "created_at": ts,
        "updated_at": ts,
    }
    conn.execute(
        """
        INSERT INTO artifacts(
          artifact_id, trace_id, task_id, parent_task_id, employee_id, artifact_type, name, path,
          mime_type, stage, version, status, is_input, is_output, is_final, summary, checksum,
          metadata_json, created_at, updated_at
        ) VALUES (
          :artifact_id, :trace_id, :task_id, :parent_task_id, :employee_id, :artifact_type, :name, :path,
          :mime_type, :stage, :version, :status, :is_input, :is_output, :is_final, :summary, :checksum,
          :metadata_json, :created_at, :updated_at
        )
        """,
        artifact,
    )
    conn.commit()
    write_workspace_manifest(task_id, trace_id, workspace_path)
    if version > 1:
        record_event(conn, "artifact.updated", employee_id, task_id=task_id, trace_id=trace_id, payload={"artifact_id": artifact_id, "name": artifact_name, "version": version, "superseded": [item["artifact_id"] for item in superseded]})
    record_event(conn, "artifact.created", employee_id, task_id=task_id, trace_id=trace_id, payload={"artifact_id": artifact_id, "path": str(snapshot_path), "original_path": str(artifact_path), "name": artifact_name, "version": version, "stage": stage, "status": "created"})
    return {"artifact": artifact, "superseded": superseded}


def scan_artifacts_internal(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    employee_id: str,
    scan_dir: str,
    artifact_type: str,
    stage: str,
    summary: str,
    pattern: str = "*",
) -> dict:
    require_task(conn, task_id)
    require_employee(conn, employee_id)
    root = require_workspace_path(conn, task_id, scan_dir)
    if not root.exists() or not root.is_dir():
        raise ValueError(f"scan dir not found: {root}")
    artifacts = []
    for path in sorted(item for item in root.rglob(pattern) if item.is_file()):
        if path.name == "manifest.json":
            continue
        artifacts.append(
            register_artifact_internal(
                conn,
                task_id=task_id,
                employee_id=employee_id,
                path=str(path),
                artifact_type=artifact_type,
                name=path.name,
                stage=stage,
                summary=summary or f"auto-registered {path.name}",
                metadata={"registered_by": "artifact.scan", "scan_dir": str(root), "pattern": pattern},
            )
        )
    return {"task_id": task_id, "scan_dir": str(root), "artifacts": artifacts}


def approve_artifact_internal(conn: sqlite3.Connection, *, artifact_id: str, by: str, status: str = "approved", reason: str = "") -> dict:
    require_employee(conn, by)
    artifact = row_by_id(conn, "artifacts", "artifact_id", artifact_id)
    if status not in {"approved", "rejected"}:
        raise SystemExit("status must be approved or rejected")
    conn.execute("UPDATE artifacts SET status = ?, updated_at = ? WHERE artifact_id = ?", (status, now(), artifact_id))
    conn.commit()
    event_type = "artifact.approved" if status == "approved" else "artifact.rejected"
    event = record_event(conn, event_type, by, task_id=artifact["task_id"], trace_id=artifact["trace_id"], payload={"artifact_id": artifact_id, "reason": reason})
    updated = row_by_id(conn, "artifacts", "artifact_id", artifact_id)
    return {"artifact": updated, "event_id": event["id"]}


def use_artifact_internal(conn: sqlite3.Connection, *, task_id: str, artifact_id: str, employee_id: str, purpose: str = "") -> dict:
    require_task(conn, task_id)
    require_employee(conn, employee_id)
    artifact = row_by_id(conn, "artifacts", "artifact_id", artifact_id)
    if artifact["status"] in {"rejected", "superseded"}:
        raise SystemExit(f"artifact is not usable: {artifact_id} status={artifact['status']}")
    trace_id = trace_id_for_task(conn, task_id, artifact.get("trace_id", ""))
    event = record_event(conn, "artifact.used_by_task", employee_id, task_id=task_id, trace_id=trace_id, payload={"artifact_id": artifact_id, "from_task_id": artifact["task_id"], "path": artifact["path"], "purpose": purpose})
    return {"artifact": artifact, "event_id": event["id"]}


def promote_artifact_to_evidence_internal(conn: sqlite3.Connection, *, artifact_id: str, by: str, summary: str = "", evidence_type: str = "", attempt_id: str = "") -> dict:
    require_employee(conn, by)
    artifact = row_by_id(conn, "artifacts", "artifact_id", artifact_id)
    if artifact["status"] in {"rejected", "superseded"}:
        raise SystemExit(f"artifact cannot be promoted: {artifact_id} status={artifact['status']}")
    evidence_id = f"evidence-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    ts = now()
    evidence = {
        "evidence_id": evidence_id,
        "trace_id": artifact["trace_id"],
        "task_id": artifact["task_id"],
        "attempt_id": attempt_id,
        "employee_id": by,
        "artifact_id": artifact_id,
        "type": evidence_type or artifact["artifact_type"],
        "path_or_url": artifact["path"],
        "summary": summary or artifact["summary"],
        "checksum": artifact["checksum"],
        "is_final": 1,
        "metadata_json": json.dumps({"promoted_from_artifact": artifact_id}, ensure_ascii=False),
        "created_at": ts,
    }
    conn.execute(
        """
        INSERT INTO evidence(evidence_id, trace_id, task_id, attempt_id, employee_id, artifact_id, type, path_or_url, summary, checksum, is_final, metadata_json, created_at)
        VALUES (:evidence_id, :trace_id, :task_id, :attempt_id, :employee_id, :artifact_id, :type, :path_or_url, :summary, :checksum, :is_final, :metadata_json, :created_at)
        """,
        evidence,
    )
    conn.execute("UPDATE artifacts SET is_final = 1, updated_at = ? WHERE artifact_id = ?", (ts, artifact_id))
    conn.commit()
    event = record_event(conn, "artifact.promoted_to_evidence", by, task_id=artifact["task_id"], trace_id=artifact["trace_id"], payload={"artifact_id": artifact_id, "evidence_id": evidence_id, "path": artifact["path"], "attempt_id": attempt_id})
    return {"evidence": evidence, "event_id": event["id"]}


def create_handoff_internal(
    conn: sqlite3.Connection,
    *,
    from_task_id: str,
    to_task_id: str,
    from_employee_id: str,
    to_employee_id: str = "",
    summary: str,
    artifacts: list[str],
    known_issues: str = "",
    next_steps: str = "",
    required_actions: str = "",
    acceptance_notes: str = "",
) -> dict:
    require_task(conn, from_task_id)
    require_task(conn, to_task_id)
    require_employee(conn, from_employee_id)
    if to_employee_id:
        require_employee(conn, to_employee_id)
    for artifact_id in artifacts:
        row_by_id(conn, "artifacts", "artifact_id", artifact_id)
    trace_id = trace_id_for_task(conn, from_task_id)
    update_task_metadata(conn, to_task_id, {"trace_id": trace_id, "upstream_task_id": from_task_id})
    ensure_task_workspace(conn, to_task_id, trace_id)
    handoff_id = f"handoff-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    ts = now()
    handoff = {
        "handoff_id": handoff_id,
        "trace_id": trace_id,
        "from_task_id": from_task_id,
        "to_task_id": to_task_id,
        "from_employee_id": from_employee_id,
        "to_employee_id": to_employee_id,
        "summary": summary,
        "artifacts_json": json.dumps(artifacts, ensure_ascii=False),
        "known_issues": known_issues,
        "next_steps": next_steps,
        "required_actions": required_actions,
        "acceptance_notes": acceptance_notes,
        "status": "created",
        "created_at": ts,
        "updated_at": ts,
    }
    conn.execute(
        """
        INSERT INTO handoffs(handoff_id, trace_id, from_task_id, to_task_id, from_employee_id, to_employee_id, summary, artifacts_json, known_issues, next_steps, required_actions, acceptance_notes, status, created_at, updated_at)
        VALUES (:handoff_id, :trace_id, :from_task_id, :to_task_id, :from_employee_id, :to_employee_id, :summary, :artifacts_json, :known_issues, :next_steps, :required_actions, :acceptance_notes, :status, :created_at, :updated_at)
        """,
        handoff,
    )
    conn.commit()
    event = record_event(conn, "handoff.created", from_employee_id, task_id=from_task_id, trace_id=trace_id, payload={"handoff_id": handoff_id, "to_task_id": to_task_id, "artifacts": artifacts, "summary": summary})
    return {"handoff": handoff, "event_id": event["id"]}


def update_handoff_status_internal(conn: sqlite3.Connection, *, handoff_id: str, by: str, status: str, reason: str = "") -> dict:
    require_employee(conn, by)
    if status not in {"accepted", "rejected"}:
        raise SystemExit("status must be accepted or rejected")
    handoff = row_by_id(conn, "handoffs", "handoff_id", handoff_id)
    ts = now()
    conn.execute("UPDATE handoffs SET status = ?, updated_at = ? WHERE handoff_id = ?", (status, ts, handoff_id))
    from_task = require_task(conn, handoff["from_task_id"])
    if status == "rejected":
        conn.execute(
            "UPDATE tasks SET status = 'blocked', blocker = ?, updated_at = ? WHERE id = ?",
            (f"handoff rejected by {by}: {reason}", ts, handoff["from_task_id"]),
        )
    conn.commit()
    event = record_event(conn, f"handoff.{status}", by, task_id=handoff["from_task_id"], trace_id=handoff["trace_id"], payload={"handoff_id": handoff_id, "reason": reason, "to_task_id": handoff["to_task_id"]})
    return {"handoff": row_by_id(conn, "handoffs", "handoff_id", handoff_id), "from_task": dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (handoff["from_task_id"],)).fetchone()), "previous_from_task": from_task, "event_id": event["id"]}


def build_task_context_package_internal(conn: sqlite3.Connection, *, task_id: str, employee_id: str = "") -> dict:
    task = require_task(conn, task_id)
    if employee_id:
        require_employee(conn, employee_id)
    trace_id = trace_id_for_task(conn, task_id)
    handoffs = rows(conn, "SELECT * FROM handoffs WHERE to_task_id = ? AND status IN ('created', 'accepted') ORDER BY created_at ASC", (task_id,))
    artifact_ids: list[str] = []
    handoff_notes = []
    for handoff in handoffs:
        artifacts = parse_json_arg(handoff.get("artifacts_json", "") or "[]", [])
        artifact_ids.extend(str(item) for item in artifacts)
        handoff_notes.append(
            {
                "handoff_id": handoff["handoff_id"],
                "from_task_id": handoff["from_task_id"],
                "summary": handoff["summary"],
                "known_issues": handoff["known_issues"],
                "next_steps": handoff["next_steps"],
                "required_actions": handoff["required_actions"],
                "artifacts": artifacts,
            }
        )
    available_artifacts = []
    if artifact_ids:
        placeholders = ",".join("?" for _ in artifact_ids)
        artifact_rows = rows(
            conn,
            f"SELECT * FROM artifacts WHERE artifact_id IN ({placeholders}) AND status NOT IN ('rejected', 'superseded') ORDER BY created_at ASC",
            tuple(artifact_ids),
        )
        available_artifacts = [
            {
                "artifact_id": artifact["artifact_id"],
                "from_task_id": artifact["task_id"],
                "from_employee_id": artifact["employee_id"],
                "artifact_type": artifact["artifact_type"],
                "path": artifact["path"],
                "summary": artifact["summary"],
                "version": artifact["version"],
                "status": artifact["status"],
                "stage": artifact["stage"],
                "checksum": artifact["checksum"],
            }
            for artifact in artifact_rows
        ]
    context = {
        "task_id": task_id,
        "trace_id": trace_id,
        "employee_id": employee_id or task["target_agent"],
        "task": task,
        "workspace": task_workspace(conn, task_id),
        "available_artifacts": available_artifacts,
        "handoff_notes": handoff_notes,
        "created_at": now(),
    }
    package_id = f"context-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    conn.execute(
        "INSERT INTO task_context_packages(context_id, trace_id, task_id, employee_id, context_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (package_id, trace_id, task_id, context["employee_id"], json.dumps(context, ensure_ascii=False), context["created_at"]),
    )
    conn.commit()
    event = record_event(conn, "task.context_package.created", context["employee_id"], task_id=task_id, trace_id=trace_id, payload={"package_id": package_id, "available_artifacts": [item["artifact_id"] for item in available_artifacts], "handoffs": [item["handoff_id"] for item in handoff_notes]})
    conn.execute("UPDATE company_events SET processed_at = ? WHERE id = ?", (now(), event["id"]))
    conn.commit()
    return {"package_id": package_id, "context": context, "event_id": event["id"]}


DEFAULT_RUNTIME_POLICY = {
    "max_runtime_seconds": 36000,
    "heartbeat_interval_seconds": 60,
    "progress_interval_seconds": 300,
    "stale_after_seconds": 900,
    "supervisor_check_interval_seconds": 60,
    "max_corrections": 3,
    "max_retries": 1,
}


MANAGED_ATTEMPT_ACTIVE_STATUSES = {"starting", "running", "correcting"}


def managed_runtime_policy(**overrides: int) -> dict:
    policy = dict(DEFAULT_RUNTIME_POLICY)
    for key, value in overrides.items():
        if value is not None:
            policy[key] = int(value)
    return policy


def attempt_json_field(attempt: dict, field: str, default: dict | None = None) -> dict:
    try:
        parsed = json.loads(attempt.get(field, "") or "{}")
    except json.JSONDecodeError:
        parsed = {}
    return parsed if isinstance(parsed, dict) else (default or {})


def hydrate_execution_attempt(attempt: dict) -> dict:
    hydrated = dict(attempt)
    hydrated["runtime_policy"] = attempt_json_field(hydrated, "runtime_policy_json")
    hydrated["supervisor_state"] = attempt_json_field(hydrated, "supervisor_state_json")
    hydrated["metadata"] = attempt_json_field(hydrated, "metadata_json")
    return hydrated


def latest_attempt_for_task(conn: sqlite3.Connection, task_id: str, employee_id: str = "") -> dict | None:
    params: list[object] = [task_id]
    employee_clause = ""
    if employee_id:
        employee_clause = "AND employee_id = ?"
        params.append(employee_id)
    row = conn.execute(
        f"""
        SELECT * FROM execution_attempts
        WHERE task_id = ?
          {employee_clause}
        ORDER BY started_at DESC
        LIMIT 1
        """,
        params,
    ).fetchone()
    return dict(row) if row else None


def long_task_state_for_attempt(attempt: dict, *, generated_at: str | None = None) -> dict:
    policy = attempt_json_field(dict(attempt), "runtime_policy_json")
    heartbeat_interval = int(policy.get("heartbeat_interval_seconds", DEFAULT_RUNTIME_POLICY["heartbeat_interval_seconds"]) or DEFAULT_RUNTIME_POLICY["heartbeat_interval_seconds"])
    stale_after = int(policy.get("stale_after_seconds", DEFAULT_RUNTIME_POLICY["stale_after_seconds"]) or DEFAULT_RUNTIME_POLICY["stale_after_seconds"])
    max_runtime = int(policy.get("max_runtime_seconds", DEFAULT_RUNTIME_POLICY["max_runtime_seconds"]) or DEFAULT_RUNTIME_POLICY["max_runtime_seconds"])
    current = generated_at or now()
    heartbeat_age = seconds_since(str(attempt.get("last_heartbeat_at") or attempt.get("started_at") or ""), current)
    progress_age = seconds_since(str(attempt.get("last_progress_at") or attempt.get("started_at") or ""), current)
    runtime_age = seconds_since(str(attempt.get("started_at") or ""), current)
    heartbeat_warn_after = max(1, heartbeat_interval * 2)
    heartbeat_state = "stale" if heartbeat_age >= heartbeat_warn_after else "fresh"
    progress_state = "stagnant" if progress_age >= stale_after else "fresh"
    status = str(attempt.get("status") or "")
    if status == "correcting":
        state = "correcting"
    elif status in {"failed", "cancelled", "stale", "success"}:
        state = status
    elif heartbeat_state == "stale":
        state = "heartbeat_stale"
    elif progress_state == "stagnant":
        state = "progress_stagnant"
    else:
        state = "running"
    return {
        "long_task_state": state,
        "heartbeat_state": heartbeat_state,
        "progress_state": progress_state,
        "heartbeat_age_seconds": heartbeat_age,
        "progress_age_seconds": progress_age,
        "runtime_age_seconds": runtime_age,
        "heartbeat_warn_after_seconds": heartbeat_warn_after,
        "stale_after_seconds": stale_after,
        "max_runtime_seconds": max_runtime,
        "timeout_is_sync_wait_only": True,
    }


def start_execution_attempt_internal(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    employee_id: str,
    adapter_type: str = "local",
    metadata: dict | None = None,
    status: str = "running",
    runtime_policy: dict | None = None,
    pid: str = "",
    session_key: str = "",
    last_heartbeat_at: str = "",
    last_progress_at: str = "",
) -> dict:
    require_task(conn, task_id)
    require_employee(conn, employee_id)
    employee_row = conn.execute("SELECT * FROM employees WHERE id = ?", (employee_id,)).fetchone()
    trace_id = trace_id_for_task(conn, task_id)
    attempt_id = f"attempt-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    ts = now()
    # Reap any prior still-"active" attempt for this task+employee before opening a new one: a new
    # attempt means the previous was superseded (retry/restart). Otherwise the old row lingers forever
    # in 'running'/'starting' as a zombie that pollutes "what is this agent doing right now".
    _active_statuses = sorted(MANAGED_ATTEMPT_ACTIVE_STATUSES)
    conn.execute(
        f"UPDATE execution_attempts SET status='failed', finished_at=?, "
        f"error_message=CASE WHEN error_message='' THEN 'superseded by a newer attempt' ELSE error_message END "
        f"WHERE task_id=? AND employee_id=? AND status IN ({','.join('?' * len(_active_statuses))})",
        (ts, task_id, employee_id, *_active_statuses),
    )
    first_seen = last_heartbeat_at or ts
    first_progress = last_progress_at or ts
    attempt = {
        "attempt_id": attempt_id,
        "trace_id": trace_id,
        "task_id": task_id,
        "employee_id": employee_id,
        "adapter_type": adapter_type,
        "status": status,
        "runtime": employee_row["runtime"],
        "pid": str(pid or ""),
        "session_key": str(session_key or ""),
        "runtime_policy_json": json.dumps(runtime_policy or {}, ensure_ascii=False),
        "last_heartbeat_at": first_seen,
        "last_progress_at": first_progress,
        "cancel_requested_at": "",
        "supervisor_state_json": json.dumps({"corrections_requested": 0, "corrections_acknowledged": 0}, ensure_ascii=False),
        "started_at": ts,
        "finished_at": "",
        "error_message": "",
        "metadata_json": json.dumps(metadata or {}, ensure_ascii=False),
    }
    conn.execute(
        """
        INSERT INTO execution_attempts(
          attempt_id, trace_id, task_id, employee_id, adapter_type, status, runtime, pid, session_key,
          runtime_policy_json, last_heartbeat_at, last_progress_at, cancel_requested_at, supervisor_state_json,
          started_at, finished_at, error_message, metadata_json
        )
        VALUES (
          :attempt_id, :trace_id, :task_id, :employee_id, :adapter_type, :status, :runtime, :pid, :session_key,
          :runtime_policy_json, :last_heartbeat_at, :last_progress_at, :cancel_requested_at, :supervisor_state_json,
          :started_at, :finished_at, :error_message, :metadata_json
        )
        """,
        attempt,
    )
    conn.commit()
    event = record_event(conn, "task.attempt.started", employee_id, task_id=task_id, trace_id=trace_id, payload={"attempt_id": attempt_id, "adapter_type": adapter_type, "status": status, "runtime_policy": runtime_policy or {}})
    return {"attempt": attempt, "event_id": event["id"]}


def finish_execution_attempt_internal(conn: sqlite3.Connection, *, attempt_id: str, status: str, error: str = "") -> dict:
    if status not in {"success", "failed", "cancelled", "stale"}:
        raise SystemExit("status must be success, failed, cancelled, or stale")
    attempt = row_by_id(conn, "execution_attempts", "attempt_id", attempt_id)
    current_status = str(attempt.get("status") or "")
    if current_status in {"success", "failed", "cancelled", "stale"}:
        if current_status == status:
            return {"attempt": attempt, "event_id": "", "idempotent": True}
        # The watchdog (or a cancel) already reaped this attempt to a terminal state. A late adapter
        # finish arriving afterwards must NOT raise — that would crash the adapter and pollute
        # adapter_runs with a phantom failure. Accept it idempotently as a no-op and keep the reaper's
        # verdict (a reaped/cancelled attempt stays reaped/cancelled).
        return {"attempt": attempt, "event_id": "", "idempotent": True, "late_finish_ignored": True, "previous_status": current_status}
    conn.execute(
        "UPDATE execution_attempts SET status = ?, finished_at = ?, error_message = ? WHERE attempt_id = ?",
        (status, now(), error, attempt_id),
    )
    conn.commit()
    event = record_event(conn, "task.attempt.finished", attempt["employee_id"], task_id=attempt["task_id"], trace_id=attempt["trace_id"], payload={"attempt_id": attempt_id, "status": status, "error": error})
    return {"attempt": row_by_id(conn, "execution_attempts", "attempt_id", attempt_id), "event_id": event["id"]}


def runtime_session_row(conn: sqlite3.Connection, session_id: str) -> dict:
    return row_by_id(conn, "runtime_sessions", "session_id", session_id)


def hydrate_runtime_session(session: dict) -> dict:
    item = dict(session)
    try:
        metadata = json.loads(item.get("metadata_json", "") or "{}")
    except json.JSONDecodeError:
        metadata = {}
    item["metadata"] = metadata if isinstance(metadata, dict) else {}
    item.pop("metadata_json", None)
    return item


def list_runtime_sessions(conn: sqlite3.Connection, *, employee_id: str = "", task_id: str = "", trace_id: str = "", limit: int = 50) -> list[dict]:
    where = []
    params: list[object] = []
    if employee_id:
        where.append("employee_id = ?")
        params.append(employee_id)
    if task_id:
        where.append("task_id = ?")
        params.append(task_id)
    if trace_id:
        where.append("trace_id = ?")
        params.append(trace_id)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    safe_limit = max(1, min(int(limit or 50), 200))
    rows_out = rows(
        conn,
        f"SELECT * FROM runtime_sessions {clause} ORDER BY COALESCE(last_heartbeat_at, started_at) DESC, started_at DESC LIMIT ?",
        tuple([*params, safe_limit]),
    )
    return [hydrate_runtime_session(item) for item in rows_out]


def runtime_session_detail_bundle(conn: sqlite3.Connection, session_id: str) -> dict:
    safe_id = str(session_id or "").strip()
    if not safe_id or "/" in safe_id:
        return {"ok": False, "error": "invalid session_id", "session_id": safe_id}
    raw_session = conn.execute("SELECT * FROM runtime_sessions WHERE session_id = ?", (safe_id,)).fetchone()
    if not raw_session:
        return {"ok": False, "error": "runtime_session not found", "session_id": safe_id}
    session = hydrate_runtime_session(dict(raw_session))
    task_id = str(session.get("task_id") or "")
    attempt_id = str(session.get("attempt_id") or "")
    trace_id = str(session.get("trace_id") or "")
    employee_id = str(session.get("employee_id") or "")
    task_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone() if task_id else None
    attempt_row = conn.execute("SELECT * FROM execution_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone() if attempt_id else None
    events = rows(
        conn,
        """
        SELECT id, trace_id, event_type, source_agent, task_id, payload_json, created_at, processed_at
        FROM company_events
        WHERE payload_json LIKE ?
        ORDER BY created_at ASC
        LIMIT 50
        """,
        (f"%{safe_id}%",),
    )
    sanitized_events = []
    for event in events:
        raw_payload = event.get("payload_json", "")
        try:
            payload = json.loads(raw_payload or "{}")
        except json.JSONDecodeError:
            payload = {"raw": sanitize_log_text(raw_payload)}
        clean_payload = sanitize_json_like(payload)
        sanitized_events.append(
            {
                **event,
                "payload": clean_payload,
                "payload_json": json.dumps(clean_payload, ensure_ascii=False, sort_keys=True),
            }
        )
    task_payload = dict(task_row) if task_row else {}
    if task_payload:
        raw_evidence_path = task_payload.pop("evidence_path", "")
        task_payload["evidence"] = sanitize_evidence_path_for_display(raw_evidence_path)
    return {
        "ok": True,
        "source": "/v1/runtime-sessions/{session_id}",
        "runtime_session": session,
        "task": task_payload,
        "attempt": hydrate_execution_attempt(dict(attempt_row)) if attempt_row else {},
        "tool_calls": list_tool_calls(conn, session_id=safe_id, limit=100),
        "budget_summary": budget_summary(conn, task_id=task_id, employee_id=employee_id, trace_id=trace_id, attempt_id=attempt_id),
        "budget_events": list_budget_events(conn, task_id=task_id, employee_id=employee_id, trace_id=trace_id, attempt_id=attempt_id, limit=50),
        "evidence_records": task_evidence_records(conn, task_id) if task_id else [],
        "events": sanitized_events,
        "redaction_policy": sanitized_log_policy(),
    }


def start_runtime_session_internal(
    conn: sqlite3.Connection,
    *,
    session_id: str = "",
    employee_id: str,
    adapter_type: str = "",
    runtime_type: str = "",
    pid: str = "",
    session_key: str = "",
    task_id: str = "",
    attempt_id: str = "",
    metadata: dict | None = None,
) -> dict:
    require_employee(conn, employee_id)
    attempt = row_by_id(conn, "execution_attempts", "attempt_id", attempt_id) if attempt_id else {}
    if task_id:
        require_task(conn, task_id)
    resolved_task_id = task_id or str(attempt.get("task_id", "") or "")
    trace_id = trace_id_for_task(conn, resolved_task_id, str(attempt.get("trace_id", "") or ""))
    ts = now()
    session = {
        "session_id": session_id or f"session-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}",
        "trace_id": trace_id,
        "task_id": resolved_task_id,
        "attempt_id": attempt_id,
        "employee_id": employee_id,
        "adapter_type": adapter_type or str(attempt.get("adapter_type", "") or ""),
        "runtime_type": runtime_type,
        "pid": str(pid or ""),
        "session_key": str(session_key or attempt.get("session_key", "") or ""),
        "status": "active",
        "started_at": ts,
        "last_heartbeat_at": ts,
        "last_progress_at": "",
        "stopped_at": "",
        "metadata_json": json.dumps(metadata or {}, ensure_ascii=False),
    }
    conn.execute(
        """
        INSERT INTO runtime_sessions(
          session_id, trace_id, task_id, attempt_id, employee_id, adapter_type, runtime_type,
          pid, session_key, status, started_at, last_heartbeat_at, last_progress_at,
          stopped_at, metadata_json
        )
        VALUES (
          :session_id, :trace_id, :task_id, :attempt_id, :employee_id, :adapter_type, :runtime_type,
          :pid, :session_key, :status, :started_at, :last_heartbeat_at, :last_progress_at,
          :stopped_at, :metadata_json
        )
        ON CONFLICT(session_id) DO UPDATE SET
          trace_id = excluded.trace_id,
          task_id = excluded.task_id,
          attempt_id = excluded.attempt_id,
          employee_id = excluded.employee_id,
          adapter_type = excluded.adapter_type,
          runtime_type = excluded.runtime_type,
          pid = excluded.pid,
          session_key = excluded.session_key,
          status = excluded.status,
          last_heartbeat_at = excluded.last_heartbeat_at,
          metadata_json = excluded.metadata_json
        """,
        session,
    )
    conn.commit()
    event = record_event(conn, "runtime.session.started", employee_id, task_id=resolved_task_id, trace_id=trace_id, payload={"session_id": session["session_id"], "attempt_id": attempt_id, "adapter_type": session["adapter_type"], "runtime_type": runtime_type})
    audit(conn, employee_id, "runtime.session.start", session["session_id"], {"task_id": resolved_task_id, "attempt_id": attempt_id, "event_id": event["id"]})
    return {"session": hydrate_runtime_session(runtime_session_row(conn, session["session_id"])), "event_id": event["id"]}


def heartbeat_runtime_session_internal(conn: sqlite3.Connection, *, session_id: str, status: str = "active", progress: bool = False) -> dict:
    session = runtime_session_row(conn, session_id)
    ts = now()
    fields = ["status = ?", "last_heartbeat_at = ?"]
    params: list[object] = [status, ts]
    if progress:
        fields.append("last_progress_at = ?")
        params.append(ts)
    params.append(session_id)
    conn.execute(f"UPDATE runtime_sessions SET {', '.join(fields)} WHERE session_id = ?", tuple(params))
    if session.get("attempt_id"):
        attempt_fields = ["last_heartbeat_at = ?"]
        attempt_params: list[object] = [ts]
        if progress:
            attempt_fields.append("last_progress_at = ?")
            attempt_params.append(ts)
        attempt_params.append(session["attempt_id"])
        conn.execute(f"UPDATE execution_attempts SET {', '.join(attempt_fields)} WHERE attempt_id = ?", tuple(attempt_params))
    conn.commit()
    event_type = "runtime.session.progress" if progress else "runtime.session.heartbeat"
    event = record_event(conn, event_type, session["employee_id"], task_id=session.get("task_id", ""), trace_id=session.get("trace_id", ""), payload={"session_id": session_id, "attempt_id": session.get("attempt_id", ""), "status": status})
    return {"session": hydrate_runtime_session(runtime_session_row(conn, session_id)), "event_id": event["id"]}


def stop_runtime_session_internal(conn: sqlite3.Connection, *, session_id: str, status: str = "stopped", error: str = "") -> dict:
    if status not in {"stopped", "failed", "stale", "cancelled"}:
        raise SystemExit("status must be stopped, failed, stale, or cancelled")
    session = runtime_session_row(conn, session_id)
    ts = now()
    conn.execute("UPDATE runtime_sessions SET status = ?, stopped_at = ?, metadata_json = ? WHERE session_id = ?", (status, ts, json.dumps({**attempt_json_field(session, "metadata_json"), "stop_error": error}, ensure_ascii=False), session_id))
    conn.commit()
    event = record_event(conn, "runtime.session.stopped", session["employee_id"], task_id=session.get("task_id", ""), trace_id=session.get("trace_id", ""), payload={"session_id": session_id, "attempt_id": session.get("attempt_id", ""), "status": status, "error": error})
    audit(conn, session["employee_id"], "runtime.session.stop", session_id, {"status": status, "error": error, "event_id": event["id"]})
    return {"session": hydrate_runtime_session(runtime_session_row(conn, session_id)), "event_id": event["id"]}


def tool_call_row(conn: sqlite3.Connection, tool_call_id: str) -> dict:
    return row_by_id(conn, "agent_tool_calls", "tool_call_id", tool_call_id)


def hydrate_tool_call(tool_call: dict) -> dict:
    item = dict(tool_call)
    for source_field, target_field in [("input_json", "input"), ("output_json", "output"), ("metadata_json", "metadata")]:
        try:
            parsed = json.loads(item.get(source_field, "") or "{}")
        except json.JSONDecodeError:
            parsed = {}
        item[target_field] = parsed if isinstance(parsed, dict) else {}
        item.pop(source_field, None)
    item["input_summary"] = sanitize_log_text(item.get("input_summary", ""))
    item["output_summary"] = sanitize_log_text(item.get("output_summary", ""))
    item["error_message"] = sanitize_log_text(item.get("error_message", ""))
    item["sanitized"] = True
    item["raw_available"] = False
    item["redaction_policy"] = {
        **sanitized_log_policy(),
        "summary": "raw tool payload hidden; input/output/error summaries are sanitized before dashboard/API display",
    }
    return item


def list_tool_calls(conn: sqlite3.Connection, *, employee_id: str = "", task_id: str = "", trace_id: str = "", attempt_id: str = "", session_id: str = "", limit: int = 50) -> list[dict]:
    where = []
    params: list[object] = []
    for column, value in [("employee_id", employee_id), ("task_id", task_id), ("trace_id", trace_id), ("attempt_id", attempt_id), ("session_id", session_id)]:
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    safe_limit = max(1, min(int(limit or 50), 200))
    rows_out = rows(conn, f"SELECT * FROM agent_tool_calls {clause} ORDER BY started_at DESC, rowid DESC LIMIT ?", tuple([*params, safe_limit]))
    return [hydrate_tool_call(item) for item in rows_out]


def tool_call_detail_bundle(conn: sqlite3.Connection, tool_call_id: str) -> dict:
    safe_id = str(tool_call_id or "").strip()
    if not safe_id or "/" in safe_id:
        return {"ok": False, "error": "invalid tool_call_id", "tool_call_id": safe_id}
    raw_tool_call = conn.execute("SELECT * FROM agent_tool_calls WHERE tool_call_id = ?", (safe_id,)).fetchone()
    if not raw_tool_call:
        return {"ok": False, "error": "tool_call not found", "tool_call_id": safe_id}
    tool_call = hydrate_tool_call(dict(raw_tool_call))
    task_id = str(tool_call.get("task_id") or "")
    attempt_id = str(tool_call.get("attempt_id") or "")
    session_id = str(tool_call.get("session_id") or "")
    trace_id = str(tool_call.get("trace_id") or "")
    task_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone() if task_id else None
    attempt_row = conn.execute("SELECT * FROM execution_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone() if attempt_id else None
    session_row = conn.execute("SELECT * FROM runtime_sessions WHERE session_id = ?", (session_id,)).fetchone() if session_id else None
    events = rows(
        conn,
        """
        SELECT id, trace_id, event_type, source_agent, task_id, payload_json, created_at, processed_at
        FROM company_events
        WHERE payload_json LIKE ?
        ORDER BY created_at ASC
        LIMIT 50
        """,
        (f"%{safe_id}%",),
    )
    sanitized_events = []
    for event in events:
        raw_payload = event.get("payload_json", "")
        try:
            payload = json.loads(raw_payload or "{}")
        except json.JSONDecodeError:
            payload = {"raw": sanitize_log_text(raw_payload)}
        sanitized_events.append(
            {
                **event,
                "payload": sanitize_json_like(payload),
                "payload_json": json.dumps(sanitize_json_like(payload), ensure_ascii=False, sort_keys=True),
            }
        )
    evidence_records = task_evidence_records(conn, task_id) if task_id else []
    task_payload = dict(task_row) if task_row else {}
    if task_payload:
        raw_evidence_path = task_payload.pop("evidence_path", "")
        task_payload["evidence"] = sanitize_evidence_path_for_display(raw_evidence_path)
    return {
        "ok": True,
        "source": "/v1/tool-calls/{tool_call_id}",
        "tool_call": tool_call,
        "task": task_payload,
        "attempt": hydrate_execution_attempt(dict(attempt_row)) if attempt_row else {},
        "runtime_session": hydrate_runtime_session(dict(session_row)) if session_row else {},
        "budget_summary": budget_summary(conn, task_id=task_id, employee_id=str(tool_call.get("employee_id") or ""), trace_id=trace_id, attempt_id=attempt_id),
        "budget_events": list_budget_events(conn, task_id=task_id, employee_id=str(tool_call.get("employee_id") or ""), trace_id=trace_id, attempt_id=attempt_id, limit=50),
        "evidence_records": evidence_records,
        "events": sanitized_events,
        "redaction_policy": sanitized_log_policy(),
    }


def start_tool_call_internal(
    conn: sqlite3.Connection,
    *,
    tool_call_id: str = "",
    trace_id: str = "",
    task_id: str = "",
    attempt_id: str = "",
    employee_id: str,
    session_id: str = "",
    tool_name: str,
    tool_type: str = "other",
    input_summary: str = "",
    input_payload: dict | None = None,
    risk_level: str = "",
    approval_id: str = "",
    metadata: dict | None = None,
) -> dict:
    require_employee(conn, employee_id)
    if task_id:
        require_task(conn, task_id)
    attempt = row_by_id(conn, "execution_attempts", "attempt_id", attempt_id) if attempt_id else {}
    resolved_task_id = task_id or str(attempt.get("task_id", "") or "")
    resolved_trace_id = trace_id or trace_id_for_task(conn, resolved_task_id, str(attempt.get("trace_id", "") or ""))
    ts = now()
    tool_call = {
        "tool_call_id": tool_call_id or f"tool-call-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}",
        "trace_id": resolved_trace_id,
        "task_id": resolved_task_id,
        "attempt_id": attempt_id,
        "employee_id": employee_id,
        "session_id": session_id,
        "tool_name": tool_name,
        "tool_type": tool_type,
        "input_summary": input_summary,
        "input_json": json.dumps(input_payload or {}, ensure_ascii=False),
        "output_summary": "",
        "output_json": "{}",
        "status": "running",
        "risk_level": risk_level,
        "approval_id": approval_id,
        "started_at": ts,
        "finished_at": "",
        "error_message": "",
        "metadata_json": json.dumps(metadata or {}, ensure_ascii=False),
    }
    conn.execute(
        """
        INSERT INTO agent_tool_calls(
          tool_call_id, trace_id, task_id, attempt_id, employee_id, session_id,
          tool_name, tool_type, input_summary, input_json, output_summary, output_json,
          status, risk_level, approval_id, started_at, finished_at, error_message, metadata_json
        )
        VALUES (
          :tool_call_id, :trace_id, :task_id, :attempt_id, :employee_id, :session_id,
          :tool_name, :tool_type, :input_summary, :input_json, :output_summary, :output_json,
          :status, :risk_level, :approval_id, :started_at, :finished_at, :error_message, :metadata_json
        )
        """,
        tool_call,
    )
    conn.commit()
    event = record_event(conn, "tool.call.started", employee_id, task_id=resolved_task_id, trace_id=resolved_trace_id, payload={"tool_call_id": tool_call["tool_call_id"], "attempt_id": attempt_id, "session_id": session_id, "tool_name": tool_name, "tool_type": tool_type, "risk_level": risk_level})
    audit(conn, employee_id, "tool.call.start", tool_call["tool_call_id"], {"task_id": resolved_task_id, "attempt_id": attempt_id, "session_id": session_id, "event_id": event["id"]})
    return {"tool_call": hydrate_tool_call(tool_call_row(conn, tool_call["tool_call_id"])), "event_id": event["id"]}


def finish_tool_call_internal(conn: sqlite3.Connection, *, tool_call_id: str, status: str, output_summary: str = "", output_payload: dict | None = None, error: str = "") -> dict:
    if status not in {"success", "failed", "blocked", "cancelled"}:
        raise SystemExit("status must be success, failed, blocked, or cancelled")
    tool_call = tool_call_row(conn, tool_call_id)
    ts = now()
    conn.execute(
        "UPDATE agent_tool_calls SET status = ?, output_summary = ?, output_json = ?, finished_at = ?, error_message = ? WHERE tool_call_id = ?",
        (status, output_summary, json.dumps(output_payload or {}, ensure_ascii=False), ts, error, tool_call_id),
    )
    conn.commit()
    event = record_event(conn, f"tool.call.{status}", tool_call["employee_id"], task_id=tool_call.get("task_id", ""), trace_id=tool_call.get("trace_id", ""), payload={"tool_call_id": tool_call_id, "attempt_id": tool_call.get("attempt_id", ""), "session_id": tool_call.get("session_id", ""), "status": status})
    audit(conn, tool_call["employee_id"], "tool.call.finish", tool_call_id, {"status": status, "event_id": event["id"]})
    return {"tool_call": hydrate_tool_call(tool_call_row(conn, tool_call_id)), "event_id": event["id"]}


def hydrate_budget_event(event: dict) -> dict:
    item = dict(event)
    try:
        metadata = json.loads(item.get("metadata_json", "") or "{}")
    except json.JSONDecodeError:
        metadata = {}
    item["metadata"] = metadata if isinstance(metadata, dict) else {}
    item.pop("metadata_json", None)
    item["summary"] = sanitize_log_text(item.get("summary", ""))
    item["amount"] = float(item.get("amount") or 0)
    item["token_input"] = int(item.get("token_input") or 0)
    item["token_output"] = int(item.get("token_output") or 0)
    item["runtime_seconds"] = int(item.get("runtime_seconds") or 0)
    return item


def list_budget_events(conn: sqlite3.Connection, *, task_id: str = "", employee_id: str = "", trace_id: str = "", attempt_id: str = "", limit: int = 50) -> list[dict]:
    where = []
    params: list[object] = []
    for column, value in [("task_id", task_id), ("employee_id", employee_id), ("trace_id", trace_id), ("attempt_id", attempt_id)]:
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    safe_limit = max(1, min(int(limit or 50), 500))
    rows_out = rows(conn, f"SELECT * FROM budget_events {clause} ORDER BY created_at DESC LIMIT ?", tuple([*params, safe_limit]))
    return [hydrate_budget_event(item) for item in rows_out]


def budget_limit_summary(conn: sqlite3.Connection, *, task_id: str = "", employee_id: str = "", trace_id: str = "", attempt_id: str = "", total_amount: float = 0.0, currency: str = "USD") -> dict:
    where = []
    params: list[object] = []
    if task_id:
        where.append("(scope_type = 'task' AND scope_id = ?)")
        params.append(task_id)
    if employee_id:
        where.append("(scope_type = 'employee' AND scope_id = ?)")
        params.append(employee_id)
    if trace_id:
        where.append("(scope_type = 'trace' AND scope_id = ?)")
        params.append(trace_id)
    if attempt_id:
        where.append("(scope_type = 'attempt' AND scope_id = ?)")
        params.append(attempt_id)
    if not where:
        return {
            "configured": False,
            "scope_type": "",
            "scope_id": "",
            "currency": currency,
            "soft_limit": 0,
            "hard_limit": 0,
            "remaining_to_soft": None,
            "remaining_to_hard": None,
            "status": "not_configured",
        }
    account_rows = rows(
        conn,
        f"SELECT * FROM budget_accounts WHERE status != 'archived' AND ({' OR '.join(where)}) ORDER BY updated_at DESC LIMIT 1",
        tuple(params),
    )
    if not account_rows:
        return {
            "configured": False,
            "scope_type": "",
            "scope_id": "",
            "currency": currency,
            "soft_limit": 0,
            "hard_limit": 0,
            "remaining_to_soft": None,
            "remaining_to_hard": None,
            "status": "not_configured",
        }
    account = account_rows[0]
    soft_limit = float(account.get("soft_limit") or 0)
    hard_limit = float(account.get("hard_limit") or 0)
    remaining_to_soft = round(soft_limit - total_amount, 6) if soft_limit else None
    remaining_to_hard = round(hard_limit - total_amount, 6) if hard_limit else None
    status = "within_limit"
    if hard_limit and total_amount >= hard_limit:
        status = "hard_exceeded"
    elif soft_limit and total_amount >= soft_limit:
        status = "soft_exceeded"
    return {
        "configured": True,
        "budget_account_id": account.get("budget_account_id", ""),
        "scope_type": account.get("scope_type", ""),
        "scope_id": account.get("scope_id", ""),
        "currency": account.get("currency") or currency,
        "soft_limit": soft_limit,
        "hard_limit": hard_limit,
        "remaining_to_soft": remaining_to_soft,
        "remaining_to_hard": remaining_to_hard,
        "status": status,
    }


def budget_summary(conn: sqlite3.Connection, *, task_id: str = "", employee_id: str = "", trace_id: str = "", attempt_id: str = "") -> dict:
    events = list_budget_events(conn, task_id=task_id, employee_id=employee_id, trace_id=trace_id, attempt_id=attempt_id, limit=500)
    by_employee: dict[str, float] = {}
    by_task: dict[str, float] = {}
    by_project: dict[str, float] = {}
    by_cost_type: dict[str, float] = {}
    by_model: dict[str, float] = {}
    by_provider: dict[str, float] = {}
    by_currency: dict[str, float] = {}
    by_employee_by_currency: dict[str, dict[str, float]] = {}
    by_task_by_currency: dict[str, dict[str, float]] = {}
    by_task_event_count: dict[str, int] = {}
    by_task_token_input: dict[str, int] = {}
    by_task_token_output: dict[str, int] = {}
    by_task_runtime_seconds: dict[str, int] = {}
    by_project_by_currency: dict[str, dict[str, float]] = {}
    by_project_event_count: dict[str, int] = {}
    by_project_token_input: dict[str, int] = {}
    by_project_token_output: dict[str, int] = {}
    by_project_runtime_seconds: dict[str, int] = {}
    by_cost_type_by_currency: dict[str, dict[str, float]] = {}
    task_project_ids: dict[str, list[str]] = {}
    task_ids = sorted({str(item.get("task_id") or "") for item in events if str(item.get("task_id") or "")})
    if task_ids:
        placeholders = ",".join("?" for _ in task_ids)
        for row in rows(conn, f"SELECT project_id, task_id FROM project_tasks WHERE task_id IN ({placeholders}) ORDER BY project_id ASC", tuple(task_ids)):
            task_project_ids.setdefault(str(row["task_id"]), []).append(str(row["project_id"]))
    currencies = sorted({str(item.get("currency") or "USD") for item in events})
    for item in events:
        amount = float(item.get("amount") or 0)
        currency_key = str(item.get("currency") or "USD")
        employee_key = str(item.get("employee_id") or "")
        task_key = str(item.get("task_id") or "")
        cost_key = str(item.get("cost_type") or "unknown")
        model_key = str(item.get("model_name") or "")
        provider_key = str(item.get("provider") or "")
        token_input = int(item.get("token_input") or 0)
        token_output = int(item.get("token_output") or 0)
        runtime_seconds = int(item.get("runtime_seconds") or 0)
        by_currency[currency_key] = round(by_currency.get(currency_key, 0.0) + amount, 6)
        if employee_key:
            by_employee[employee_key] = round(by_employee.get(employee_key, 0.0) + amount, 6)
            employee_currency = by_employee_by_currency.setdefault(employee_key, {})
            employee_currency[currency_key] = round(employee_currency.get(currency_key, 0.0) + amount, 6)
        if task_key:
            by_task[task_key] = round(by_task.get(task_key, 0.0) + amount, 6)
            task_currency = by_task_by_currency.setdefault(task_key, {})
            task_currency[currency_key] = round(task_currency.get(currency_key, 0.0) + amount, 6)
            by_task_event_count[task_key] = by_task_event_count.get(task_key, 0) + 1
            by_task_token_input[task_key] = by_task_token_input.get(task_key, 0) + token_input
            by_task_token_output[task_key] = by_task_token_output.get(task_key, 0) + token_output
            by_task_runtime_seconds[task_key] = by_task_runtime_seconds.get(task_key, 0) + runtime_seconds
            for project_id in task_project_ids.get(task_key, []):
                by_project[project_id] = round(by_project.get(project_id, 0.0) + amount, 6)
                project_currency = by_project_by_currency.setdefault(project_id, {})
                project_currency[currency_key] = round(project_currency.get(currency_key, 0.0) + amount, 6)
                by_project_event_count[project_id] = by_project_event_count.get(project_id, 0) + 1
                by_project_token_input[project_id] = by_project_token_input.get(project_id, 0) + token_input
                by_project_token_output[project_id] = by_project_token_output.get(project_id, 0) + token_output
                by_project_runtime_seconds[project_id] = by_project_runtime_seconds.get(project_id, 0) + runtime_seconds
        by_cost_type[cost_key] = round(by_cost_type.get(cost_key, 0.0) + amount, 6)
        cost_currency = by_cost_type_by_currency.setdefault(cost_key, {})
        cost_currency[currency_key] = round(cost_currency.get(currency_key, 0.0) + amount, 6)
        if model_key:
            by_model[model_key] = round(by_model.get(model_key, 0.0) + amount, 6)
        if provider_key:
            by_provider[provider_key] = round(by_provider.get(provider_key, 0.0) + amount, 6)
    total_amount = round(sum(float(item.get("amount") or 0) for item in events), 6)
    currency = currencies[0] if len(currencies) == 1 else ("mixed" if currencies else "USD")
    limits = budget_limit_summary(conn, task_id=task_id, employee_id=employee_id, trace_id=trace_id, attempt_id=attempt_id, total_amount=total_amount, currency=currency)
    return {
        "event_count": len(events),
        "total_amount": total_amount,
        "total_amounts_by_currency": by_currency,
        "currency": currency,
        "currencies": currencies,
        "token_input": sum(int(item.get("token_input") or 0) for item in events),
        "token_output": sum(int(item.get("token_output") or 0) for item in events),
        "runtime_seconds": sum(int(item.get("runtime_seconds") or 0) for item in events),
        "by_currency": by_currency,
        "by_employee": by_employee,
        "by_employee_by_currency": by_employee_by_currency,
        "by_task": by_task,
        "by_task_by_currency": by_task_by_currency,
        "by_task_event_count": by_task_event_count,
        "by_task_token_input": by_task_token_input,
        "by_task_token_output": by_task_token_output,
        "by_task_runtime_seconds": by_task_runtime_seconds,
        "by_project": by_project,
        "by_project_by_currency": by_project_by_currency,
        "by_project_event_count": by_project_event_count,
        "by_project_token_input": by_project_token_input,
        "by_project_token_output": by_project_token_output,
        "by_project_runtime_seconds": by_project_runtime_seconds,
        "by_cost_type": by_cost_type,
        "by_cost_type_by_currency": by_cost_type_by_currency,
        "by_model": by_model,
        "by_provider": by_provider,
        "limit_status": limits["status"],
        "budget_limits": limits,
    }


def record_budget_event_internal(
    conn: sqlite3.Connection,
    *,
    budget_event_id: str = "",
    budget_account_id: str = "",
    trace_id: str = "",
    task_id: str = "",
    attempt_id: str = "",
    employee_id: str,
    cost_type: str,
    amount: float,
    currency: str = "USD",
    token_input: int = 0,
    token_output: int = 0,
    model_name: str = "",
    provider: str = "",
    runtime_seconds: int = 0,
    summary: str = "",
    metadata: dict | None = None,
) -> dict:
    if task_id:
        require_task(conn, task_id)
    if employee_id:
        require_employee(conn, employee_id)
    attempt = row_by_id(conn, "execution_attempts", "attempt_id", attempt_id) if attempt_id else {}
    resolved_task_id = task_id or str(attempt.get("task_id", "") or "")
    resolved_trace_id = trace_id or trace_id_for_task(conn, resolved_task_id, str(attempt.get("trace_id", "") or ""))
    ts = now()
    item = {
        "budget_event_id": budget_event_id or f"budget-event-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}",
        "budget_account_id": budget_account_id,
        "trace_id": resolved_trace_id,
        "task_id": resolved_task_id,
        "attempt_id": attempt_id,
        "employee_id": employee_id,
        "cost_type": cost_type,
        "amount": float(amount),
        "currency": currency or "USD",
        "token_input": int(token_input or 0),
        "token_output": int(token_output or 0),
        "model_name": model_name,
        "provider": provider,
        "runtime_seconds": int(runtime_seconds or 0),
        "summary": summary,
        "created_at": ts,
        "metadata_json": json.dumps(metadata or {}, ensure_ascii=False),
    }
    conn.execute(
        """
        INSERT INTO budget_events(
          budget_event_id, budget_account_id, trace_id, task_id, attempt_id, employee_id,
          cost_type, amount, currency, token_input, token_output, model_name, provider,
          runtime_seconds, summary, created_at, metadata_json
        )
        VALUES (
          :budget_event_id, :budget_account_id, :trace_id, :task_id, :attempt_id, :employee_id,
          :cost_type, :amount, :currency, :token_input, :token_output, :model_name, :provider,
          :runtime_seconds, :summary, :created_at, :metadata_json
        )
        """,
        item,
    )
    conn.commit()
    event = record_event(conn, "budget.spent", employee_id, task_id=resolved_task_id, trace_id=resolved_trace_id, payload={"budget_event_id": item["budget_event_id"], "attempt_id": attempt_id, "amount": item["amount"], "currency": item["currency"], "cost_type": cost_type, "token_input": item["token_input"], "token_output": item["token_output"]})
    audit(conn, employee_id, "budget.record", item["budget_event_id"], {"task_id": resolved_task_id, "attempt_id": attempt_id, "amount": item["amount"], "currency": item["currency"], "event_id": event["id"]})
    current_summary = budget_summary(conn, task_id=resolved_task_id, employee_id=employee_id, trace_id=resolved_trace_id, attempt_id=attempt_id)
    limits = current_summary.get("budget_limits", {}) if isinstance(current_summary.get("budget_limits"), dict) else {}
    approval: dict = {}
    approval_event: dict = {}
    if str(limits.get("status") or "") in {"soft_exceeded", "hard_exceeded"}:
        approval_id = f"budget-overrun-{resolved_task_id or item['budget_event_id']}"
        approval_result = create_approval_internal(
            conn,
            source=employee_id,
            action="budget_overrun",
            reason=f"Budget {limits.get('status')} for task={resolved_task_id or '-'} amount={current_summary.get('total_amount')} {current_summary.get('currency')}",
            target=employee_id,
            risk="P0" if limits.get("status") == "hard_exceeded" else "P1",
            approval_id=approval_id,
            metadata={
                "task_id": resolved_task_id,
                "trace_id": resolved_trace_id,
                "attempt_id": attempt_id,
                "budget_event_id": item["budget_event_id"],
                "budget_account_id": limits.get("budget_account_id", ""),
                "budget_amount": current_summary.get("total_amount", 0),
                "currency": current_summary.get("currency") or item["currency"],
                "limit_status": limits.get("status", ""),
                "soft_limit": limits.get("soft_limit", 0),
                "hard_limit": limits.get("hard_limit", 0),
            },
        )
        approval = approval_result.get("approval", {})
        approval_event = approval_result.get("event", {})
    return {
        "budget_event": hydrate_budget_event(row_by_id(conn, "budget_events", "budget_event_id", item["budget_event_id"])),
        "event_id": event["id"],
        "budget_limits": limits,
        "approval": approval,
        "approval_event": approval_event,
    }


def task_attempts(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    attempts = rows(conn, "SELECT * FROM execution_attempts WHERE task_id = ? ORDER BY started_at ASC", (task_id,))
    return [hydrate_execution_attempt(attempt) for attempt in attempts]


def task_attempt_history(attempts: list[dict]) -> dict:
    chain = []
    for index, attempt in enumerate(attempts):
        metadata = attempt.get("metadata", {}) if isinstance(attempt.get("metadata", {}), dict) else {}
        previous_attempt_id = str(metadata.get("previous_attempt_id", "") or "")
        reason = str(metadata.get("reason", "") or metadata.get("retry_reason", "") or "")
        chain.append(
            {
                "index": index + 1,
                "attempt_id": attempt.get("attempt_id", ""),
                "previous_attempt_id": previous_attempt_id,
                "trace_id": attempt.get("trace_id", ""),
                "employee_id": attempt.get("employee_id", ""),
                "adapter_type": attempt.get("adapter_type", ""),
                "status": attempt.get("status", ""),
                "reason": reason,
                "started_at": attempt.get("started_at", ""),
                "finished_at": attempt.get("finished_at", ""),
            }
        )
    latest = chain[-1] if chain else {}
    trace_id = str(latest.get("trace_id", "") or (chain[0].get("trace_id", "") if chain else ""))
    return {
        "total": len(chain),
        "trace_id": trace_id,
        "latest_attempt_id": latest.get("attempt_id", ""),
        "latest_status": latest.get("status", ""),
        "latest_employee_id": latest.get("employee_id", ""),
        "chain": chain,
        "recovery_summary": "old attempts retained; retry/reassign creates a new attempt with the same trace_id and previous_attempt_id.",
    }


def task_evidence_records(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    records = rows(
        conn,
        """
        SELECT evidence_id, trace_id, task_id, attempt_id, employee_id, artifact_id,
               type, path_or_url, summary, checksum, is_final, metadata_json, created_at
        FROM evidence
        WHERE task_id = ?
        ORDER BY created_at DESC
        """,
        (task_id,),
    )
    for record in records:
        raw_path = record.pop("path_or_url", "")
        record["display"] = sanitize_evidence_path_for_display(raw_path)
        metadata = parse_json_arg(record.get("metadata_json", "{}") or "{}", {})
        record["metadata"] = metadata if isinstance(metadata, dict) else {}
        record["acceptance_decision"] = evidence_acceptance_decision(record)
    return records


def task_control_plane_timeline(
    *,
    events: list[dict],
    attempts: list[dict],
    runtime_sessions: list[dict],
    tool_calls: list[dict],
    budget_events: list[dict],
    evidence_records: list[dict],
) -> list[dict]:
    timeline: list[dict] = []

    def add(item: dict) -> None:
        timestamp = str(item.get("timestamp") or "")
        if not timestamp:
            return
        timeline.append(item)

    for event in events:
        payload = parse_json_arg(event.get("payload_json", "{}") or "{}", {})
        if not isinstance(payload, dict):
            payload = {}
        add(
            {
                "kind": "task_event",
                "id": event.get("id", ""),
                "event_id": event.get("id", ""),
                "event_type": event.get("event_type", ""),
                "ledger_action": payload.get("ledger_action", ""),
                "task_id": event.get("task_id", ""),
                "trace_id": event.get("trace_id", ""),
                "employee_id": event.get("source_agent", ""),
                "attempt_id": payload.get("attempt_id", ""),
                "reason": payload.get("reason", ""),
                "status": event.get("event_type", ""),
                "timestamp": event.get("created_at", ""),
                "summary": sanitize_log_text(str(payload.get("message") or payload.get("summary") or event.get("event_type") or "")),
            }
        )
    for attempt in attempts:
        add(
            {
                "kind": "attempt",
                "id": attempt.get("attempt_id", ""),
                "attempt_id": attempt.get("attempt_id", ""),
                "task_id": attempt.get("task_id", ""),
                "trace_id": attempt.get("trace_id", ""),
                "employee_id": attempt.get("employee_id", ""),
                "status": attempt.get("status", ""),
                "timestamp": attempt.get("started_at", ""),
                "summary": f"attempt {attempt.get('status', '')} via {attempt.get('adapter_type', '')}".strip(),
            }
        )
        if attempt.get("finished_at"):
            add(
                {
                    "kind": "attempt",
                    "id": f"{attempt.get('attempt_id', '')}:finished",
                    "attempt_id": attempt.get("attempt_id", ""),
                    "task_id": attempt.get("task_id", ""),
                    "trace_id": attempt.get("trace_id", ""),
                    "employee_id": attempt.get("employee_id", ""),
                    "status": attempt.get("status", ""),
                    "timestamp": attempt.get("finished_at", ""),
                    "summary": f"attempt finished: {attempt.get('status', '')}",
                }
            )
    for session in runtime_sessions:
        add(
            {
                "kind": "runtime_session",
                "id": session.get("session_id", ""),
                "session_id": session.get("session_id", ""),
                "attempt_id": session.get("attempt_id", ""),
                "task_id": session.get("task_id", ""),
                "trace_id": session.get("trace_id", ""),
                "employee_id": session.get("employee_id", ""),
                "status": session.get("status", ""),
                "timestamp": session.get("started_at", ""),
                "summary": f"{session.get('runtime_type', '') or 'runtime'} session {session.get('status', '')}".strip(),
            }
        )
    for tool_call in tool_calls:
        add(
            {
                "kind": "tool_call",
                "id": tool_call.get("tool_call_id", ""),
                "tool_call_id": tool_call.get("tool_call_id", ""),
                "attempt_id": tool_call.get("attempt_id", ""),
                "session_id": tool_call.get("session_id", ""),
                "task_id": tool_call.get("task_id", ""),
                "trace_id": tool_call.get("trace_id", ""),
                "employee_id": tool_call.get("employee_id", ""),
                "status": tool_call.get("status", ""),
                "timestamp": tool_call.get("started_at", ""),
                "summary": sanitize_log_text(f"{tool_call.get('tool_name', '')}: {tool_call.get('output_summary') or tool_call.get('input_summary') or tool_call.get('error_message') or ''}"),
            }
        )
    for item in budget_events:
        add(
            {
                "kind": "budget_event",
                "id": item.get("budget_event_id", ""),
                "budget_event_id": item.get("budget_event_id", ""),
                "attempt_id": item.get("attempt_id", ""),
                "task_id": item.get("task_id", ""),
                "trace_id": item.get("trace_id", ""),
                "employee_id": item.get("employee_id", ""),
                "status": item.get("cost_type", ""),
                "timestamp": item.get("created_at", ""),
                "summary": f"{item.get('amount', 0)} {item.get('currency', 'USD')} · input={item.get('token_input', 0)} output={item.get('token_output', 0)} · {item.get('summary', '')}".strip(),
            }
        )
    for evidence in evidence_records:
        display = evidence.get("display", {}) if isinstance(evidence.get("display"), dict) else {}
        add(
            {
                "kind": "evidence",
                "id": evidence.get("evidence_id", ""),
                "evidence_id": evidence.get("evidence_id", ""),
                "attempt_id": evidence.get("attempt_id", ""),
                "task_id": evidence.get("task_id", ""),
                "trace_id": evidence.get("trace_id", ""),
                "employee_id": evidence.get("employee_id", ""),
                "status": "final" if evidence.get("is_final") else "draft",
                "timestamp": evidence.get("created_at", ""),
                "summary": sanitize_log_text(str(evidence.get("summary") or display.get("relative_path") or evidence.get("evidence_id") or "")),
            }
        )
    return sorted(timeline, key=lambda item: (str(item.get("timestamp") or ""), str(item.get("kind") or ""), str(item.get("id") or "")))


CONTROL_EVENT_ACTIONS = {
    "task.probe": "probe",
    "supervisor.correction_requested": "correction",
    "supervisor.cancel_requested": "cancel",
    "task.retrying": "retry",
    "task.reassigned": "reassign",
    "task.reopened": "reopen",
}


def task_control_action_summary(*, approvals: list[dict], events: list[dict], attempts: list[dict]) -> dict:
    pending_actions: set[str] = set()
    pending_rows = []
    for approval in approvals:
        if str(approval.get("status") or "") != "pending":
            continue
        action = str(approval.get("action") or "")
        normalized = action.removeprefix("task_control.").removeprefix("task.")
        pending_actions.add(normalized)
        detail = approval.get("detail", {}) if isinstance(approval.get("detail", {}), dict) else {}
        metadata = detail.get("metadata", {}) if isinstance(detail.get("metadata", {}), dict) else {}
        pending_rows.append(
            {
                "approval_id": approval.get("id", ""),
                "action": normalized,
                "raw_action": action,
                "attempt_id": metadata.get("attempt_id", ""),
                "risk": detail.get("risk", ""),
                "reason": detail.get("request_reason") or detail.get("reason") or "",
                "created_at": approval.get("created_at", ""),
            }
        )
    executed_rows = []
    for event in events:
        event_type = str(event.get("event_type") or "")
        action = CONTROL_EVENT_ACTIONS.get(event_type)
        if not action:
            continue
        payload = parse_json_arg(event.get("payload_json", "{}") or "{}", {})
        if not isinstance(payload, dict):
            payload = {}
        executed_rows.append(
            {
                "event_id": event.get("id", ""),
                "event_type": event_type,
                "action": action,
                "attempt_id": payload.get("attempt_id", ""),
                "by": event.get("source_agent", ""),
                "reason": payload.get("reason") or payload.get("message") or "",
                "created_at": event.get("created_at", ""),
            }
        )
    executed_rows.sort(key=lambda item: str(item.get("created_at") or ""))
    latest_executed = executed_rows[-1] if executed_rows else {}
    latest_attempt = attempts[-1] if attempts else {}
    latest_attempt_status = str(latest_attempt.get("status") or "")
    owner_next_action = "monitor task progress and evidence"
    if latest_executed.get("action") == "cancel" or latest_attempt_status == "cancelled":
        owner_next_action = "old attempt is cancelled; retry or reassign only after owner decision"
    elif pending_rows:
        owner_next_action = "review pending owner approvals before executing controls"
    elif latest_executed.get("action") == "correction":
        owner_next_action = "wait for worker correction ack or fresh progress"
    elif latest_executed.get("action") in {"retry", "reassign", "reopen"}:
        owner_next_action = "monitor new attempt heartbeat, progress, and evidence"
    return {
        "pending_owner_approvals": len(pending_rows),
        "pending_actions": sorted(pending_actions),
        "pending": pending_rows,
        "executed_control_actions": len(executed_rows),
        "latest_executed_action": latest_executed.get("action", ""),
        "latest_event_type": latest_executed.get("event_type", ""),
        "latest_event_id": latest_executed.get("event_id", ""),
        "latest_attempt_id": latest_attempt.get("attempt_id", ""),
        "latest_attempt_status": latest_attempt_status,
        "executed": executed_rows[-10:],
        "owner_next_action": owner_next_action,
        "summary": f"pending={len(pending_rows)} executed={len(executed_rows)} latest={latest_executed.get('action', '-') or '-'}",
    }


def owner_action_next_step(action: str, *, pending: bool = False) -> str:
    if pending:
        return "review owner approval request before real execution"
    if action == "probe":
        return "wait for worker response or escalate to Hermes correction if progress remains stagnant"
    if action == "correction":
        return "wait for worker correction ack or fresh progress"
    if action == "cancel":
        return "verify process stopped and ignore late evidence"
    if action in {"retry", "reassign", "reopen"}:
        return "monitor new attempt heartbeat, progress, and evidence"
    return "monitor task progress and evidence"


def task_owner_action_timeline(*, approvals: list[dict], events: list[dict]) -> list[dict]:
    timeline: list[dict] = []
    for approval in approvals:
        detail = approval.get("detail", {}) if isinstance(approval.get("detail", {}), dict) else {}
        metadata = detail.get("metadata", {}) if isinstance(detail.get("metadata", {}), dict) else {}
        action = str(approval.get("action") or "").removeprefix("task_control.").removeprefix("task.")
        timeline.append(
            {
                "kind": "pending_approval" if str(approval.get("status") or "") == "pending" else "approval",
                "action": action,
                "status": approval.get("status", ""),
                "approval_id": approval.get("id", ""),
                "task_id": metadata.get("task_id", ""),
                "attempt_id": metadata.get("attempt_id", ""),
                "actor": approval.get("source_agent", ""),
                "event_type": "approval.requested",
                "reason": detail.get("request_reason") or approval.get("reason", ""),
                "created_at": approval.get("created_at", ""),
                "requires_owner_approval": True,
                "owner_next_action": owner_action_next_step(action, pending=True),
            }
        )
    for event in events:
        event_type = str(event.get("event_type") or "")
        action = CONTROL_EVENT_ACTIONS.get(event_type)
        if not action:
            continue
        payload = parse_json_arg(event.get("payload_json", "{}") or "{}", {})
        if not isinstance(payload, dict):
            payload = {}
        requires_owner_approval = action in {"correction", "cancel", "retry", "reassign", "reopen"}
        timeline.append(
            {
                "kind": "executed_control",
                "action": action,
                "status": "executed",
                "event_id": event.get("id", ""),
                "event_type": event_type,
                "task_id": event.get("task_id", ""),
                "attempt_id": payload.get("attempt_id", ""),
                "actor": event.get("source_agent", ""),
                "reason": payload.get("reason") or payload.get("message") or "",
                "created_at": event.get("created_at", ""),
                "requires_owner_approval": requires_owner_approval,
                "owner_next_action": owner_action_next_step(action),
            }
        )
    kind_order = {"pending_approval": 0, "approval": 1, "executed_control": 2}
    return sorted(timeline, key=lambda item: (str(item.get("created_at") or ""), kind_order.get(str(item.get("kind") or ""), 9), str(item.get("action") or "")))


def task_completion_contract(task: dict, evidence_records: list[dict]) -> dict:
    status = str(task.get("status") or "").lower()
    done_like = status in {"completed", "done", "success"}
    final_records = [record for record in evidence_records if bool(record.get("is_final"))]
    safe_final_records = [record for record in final_records if bool((record.get("display") or {}).get("allowed"))]
    accepted_final_records = [record for record in safe_final_records if (record.get("acceptance_decision") or {}).get("status") == "accepted"]
    rejected_final_records = [record for record in final_records if (record.get("acceptance_decision") or {}).get("status") == "rejected"]
    legacy_evidence_path_present = bool(str(task.get("evidence_path") or "").strip())
    if not done_like:
        reason = "not_done"
        valid = True
        summary = "Task is not in a done-like state yet; final evidence is required before completion."
    elif final_records:
        reason = "final_evidence_present"
        valid = True
        summary = f"Task completion has {len(final_records)} task-bound final evidence record(s)."
    else:
        reason = "missing_final_evidence"
        valid = False
        summary = "Done-like task is invalid until task_id/attempt_id-bound final evidence is submitted."
    return {
        "done_like": done_like,
        "valid": valid,
        "reason": reason,
        "final_evidence_count": len(final_records),
        "safe_final_evidence_count": len(safe_final_records),
        "accepted_final_evidence_count": len(accepted_final_records),
        "rejected_final_evidence_count": len(rejected_final_records),
        "acceptance_status": "accepted" if accepted_final_records else ("rejected" if rejected_final_records and not accepted_final_records else "pending"),
        "legacy_evidence_path_present": legacy_evidence_path_present,
        "summary": summary,
    }


def task_ceo_acceptance_contract(
    *,
    task: dict,
    attempts: list[dict],
    runtime_sessions: list[dict],
    tool_calls: list[dict],
    budget_events: list[dict],
    evidence_records: list[dict],
    completion_contract: dict,
    approvals: list[dict],
    events: list[dict],
) -> dict:
    latest_attempt = attempts[-1] if attempts else {}
    if latest_attempt:
        # Always fill the derived long-task fields (long_task_state / heartbeat_state / progress_state)
        # as a base, letting the attempt's own stored fields win. Previously these were only computed
        # when long_task_state was absent, so an attempt that had long_task_state but no progress_state
        # left the key missing → an intermittent KeyError in the cockpit (depending on attempt state).
        latest_attempt = {**long_task_state_for_attempt(latest_attempt), **latest_attempt}
    metadata = parse_json_arg(task.get("metadata_json", "{}") or "{}", {})
    if not isinstance(metadata, dict):
        metadata = {}
    latest_state = str(latest_attempt.get("long_task_state") or latest_attempt.get("status") or task.get("status") or "unknown")
    done_like = bool(completion_contract.get("done_like"))
    completion_valid = bool(completion_contract.get("valid"))
    final_evidence_count = int(completion_contract.get("final_evidence_count") or 0)
    pending_approvals = [item for item in approvals if str(item.get("status") or "") == "pending"]
    failed_tools = [item for item in tool_calls if str(item.get("status") or "") in {"failed", "blocked", "cancelled"}]
    blocking_reasons: list[str] = []
    if done_like and not completion_valid:
        blocking_reasons.append(str(completion_contract.get("reason") or "completion_invalid"))
    if latest_state in {"blocked", "failed", "stale", "heartbeat_stale", "progress_stagnant", "cancelled"}:
        blocking_reasons.append(latest_state)
    if pending_approvals:
        blocking_reasons.append("pending_owner_approval")
    if failed_tools:
        blocking_reasons.append("failed_or_blocked_tool_calls")
    if task.get("id") and attempts and not runtime_sessions:
        blocking_reasons.append("runtime_session_missing")
    if attempts and not tool_calls and latest_state not in {"submitted", "queued", "claimed", "starting"}:
        blocking_reasons.append("tool_call_ledger_missing")

    status = "monitor"
    owner_next_action = "monitor task heartbeat, progress, tool calls, budget, and evidence"
    if done_like and completion_valid:
        status = "ready_for_owner_acceptance"
        owner_next_action = "review final evidence and accept delivery"
    if latest_state in {"submitted", "queued", "claimed", "starting", "running", "correcting"}:
        status = "needs_progress"
        owner_next_action = "monitor progress; send probe or correction if it becomes stagnant"
    if blocking_reasons:
        status = "blocked"
        owner_next_action = "resolve blocking reasons before accepting task"
    if "missing_final_evidence" in blocking_reasons:
        owner_next_action = "attach or promote task-bound final evidence before accepting completion"
    elif "pending_owner_approval" in blocking_reasons:
        owner_next_action = "review pending owner approvals before real execution"
    elif latest_state in {"progress_stagnant", "heartbeat_stale"}:
        owner_next_action = "send probe, inspect sanitized logs, or request Hermes correction"
    elif latest_state in {"blocked", "failed", "stale"}:
        owner_next_action = "review blocker, then retry, reassign, or request correction"
    elif latest_state == "cancelled":
        owner_next_action = "do not accept late evidence from cancelled attempt; retry or reassign if needed"

    control_events = [item for item in events if CONTROL_EVENT_ACTIONS.get(str(item.get("event_type") or ""))]
    return {
        "task_id": task.get("id", ""),
        "status": status,
        "current_state": latest_state,
        "current_attempt_id": latest_attempt.get("attempt_id", ""),
        "trace_id": latest_attempt.get("trace_id", "") or metadata.get("trace_id", ""),
        "ready_for_acceptance": status == "ready_for_owner_acceptance",
        "blocking_reasons": list(dict.fromkeys(reason for reason in blocking_reasons if reason)),
        "owner_next_action": owner_next_action,
        "ledger_counts": {
            "attempts": len(attempts),
            "runtime_sessions": len(runtime_sessions),
            "tool_calls": len(tool_calls),
            "failed_tool_calls": len(failed_tools),
            "budget_events": len(budget_events),
            "evidence_records": len(evidence_records),
            "final_evidence": final_evidence_count,
            "pending_approvals": len(pending_approvals),
            "control_events": len(control_events),
        },
        "truth_rules": {
            "completion_requires_final_evidence": True,
            "completion_requires_matching_task_id": True,
            "heartbeat_is_completion": False,
            "ack_is_completion": False,
            "stdout_is_completion": False,
            "cancelled_attempt_can_complete": False,
        },
    }


def task_supervisor_state(attempts: list[dict]) -> tuple[dict, dict]:
    state = {
        "corrections_requested": 0,
        "corrections_acknowledged": 0,
        "last_correction": {},
        "blocked_reason": "",
        "last_blocked_at": "",
    }
    latest = attempts[-1] if attempts else {}
    for attempt in attempts:
        supervisor_state = attempt.get("supervisor_state", {})
        if not isinstance(supervisor_state, dict):
            continue
        state["corrections_requested"] += int(supervisor_state.get("corrections_requested", 0) or 0)
        state["corrections_acknowledged"] += int(supervisor_state.get("corrections_acknowledged", 0) or 0)
        if supervisor_state.get("last_correction"):
            state["last_correction"] = supervisor_state["last_correction"]
        if supervisor_state.get("blocked_reason"):
            state["blocked_reason"] = supervisor_state["blocked_reason"]
        if supervisor_state.get("last_blocked_at"):
            state["last_blocked_at"] = supervisor_state["last_blocked_at"]
    summary = {
        "latest_attempt_id": latest.get("attempt_id", ""),
        "latest_attempt_status": latest.get("status", ""),
        "latest_employee_id": latest.get("employee_id", ""),
        "needs_ack": state["corrections_requested"] > state["corrections_acknowledged"],
        "correction_balance": state["corrections_requested"] - state["corrections_acknowledged"],
        "blocked": bool(state["blocked_reason"]),
    }
    return state, summary


def task_progress_events(events: list[dict]) -> list[dict]:
    progress_events = []
    for event in events:
        if event.get("event_type") != "task.progress":
            continue
        payload = parse_json_arg(event.get("payload_json", "{}") or "{}", {})
        if not isinstance(payload, dict):
            payload = {}
        progress_events.append(
            {
                "event_id": event.get("id", ""),
                "trace_id": event.get("trace_id", ""),
                "task_id": event.get("task_id", ""),
                "employee_id": payload.get("employee_id") or event.get("source_agent", ""),
                "attempt_id": payload.get("attempt_id", ""),
                "progress_state": payload.get("progress_state", ""),
                "progress_layer": payload.get("progress_layer", ""),
                "progress_label": payload.get("progress_label", ""),
                "message": payload.get("message", ""),
                "progress": payload.get("progress"),
                "payload": payload.get("payload", {}),
                "created_at": event.get("created_at", ""),
            }
        )
    return progress_events


def record_task_probe_internal(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    actor: str,
    attempt_id: str = "",
    message: str,
    reason: str = "",
) -> dict:
    actor = resolve_employee_alias(actor)
    require_employee(conn, actor)
    task = require_task(conn, task_id)
    attempt: dict = {}
    trace_id = str(task.get("trace_id") or "")
    if attempt_id:
        attempt = row_by_id(conn, "execution_attempts", "attempt_id", attempt_id)
        if attempt["task_id"] != task_id:
            raise SystemExit("attempt does not belong to task")
        trace_id = str(attempt.get("trace_id") or trace_id)
    payload = {
        "attempt_id": attempt_id,
        "by": actor,
        "message": message,
        "reason": reason or "progress_probe",
        "ledger_action": "task.probe",
        "non_mutating": True,
        "external_send": False,
    }
    event = record_event(conn, "task.probe", actor, task_id=task_id, trace_id=trace_id, payload=payload)
    audit(conn, actor, "task.probe", task_id, {"attempt_id": attempt_id, "reason": payload["reason"], "message": message, "event_id": event["id"]})
    return {"task_id": task_id, "attempt_id": attempt_id, "event_id": event["id"], "probe": payload, "attempt": attempt}


def task_correction_events(events: list[dict]) -> list[dict]:
    correction_events = []
    for event in events:
        event_type = str(event.get("event_type", ""))
        if event_type not in {"supervisor.correction_requested", "supervisor.correction_acknowledged"}:
            continue
        payload = parse_json_arg(event.get("payload_json", "{}") or "{}", {})
        if not isinstance(payload, dict):
            payload = {}
        correction_events.append(
            {
                "event_id": event.get("id", ""),
                "event_type": event_type,
                "task_id": event.get("task_id", ""),
                "trace_id": event.get("trace_id", ""),
                "source_agent": event.get("source_agent", ""),
                "attempt_id": str(payload.get("attempt_id", "") or ""),
                "message": str(payload.get("message", "") or ""),
                "ack": event_type == "supervisor.correction_acknowledged",
                "created_at": event.get("created_at", ""),
            }
        )
    return correction_events


def latest_active_attempt_for_task(conn: sqlite3.Connection, task_id: str, employee_id: str) -> dict | None:
    placeholders = ",".join("?" for _ in MANAGED_ATTEMPT_ACTIVE_STATUSES)
    params = [task_id, employee_id, *sorted(MANAGED_ATTEMPT_ACTIVE_STATUSES)]
    row = conn.execute(
        f"""
        SELECT * FROM execution_attempts
        WHERE task_id = ?
          AND employee_id = ?
          AND status IN ({placeholders})
        ORDER BY started_at DESC
        LIMIT 1
        """,
        params,
    ).fetchone()
    return dict(row) if row else None


def report_task_progress_internal(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    employee_id: str,
    attempt_id: str = "",
    progress_state: str = "in_progress",
    message: str = "",
    progress: int | None = None,
    payload: dict | None = None,
    created_at: str = "",
) -> dict:
    require_task(conn, task_id)
    require_employee(conn, employee_id)
    if attempt_id:
        attempt = row_by_id(conn, "execution_attempts", "attempt_id", attempt_id)
        if attempt["task_id"] != task_id:
            raise SystemExit(f"attempt does not belong to task: {attempt_id}")
        if attempt["employee_id"] != employee_id:
            raise SystemExit(f"attempt does not belong to employee: {attempt_id}")
        if attempt["status"] not in MANAGED_ATTEMPT_ACTIVE_STATUSES:
            raise SystemExit("attempt is not active")
    else:
        attempt = latest_active_attempt_for_task(conn, task_id, employee_id)
        if not attempt:
            raise SystemExit(f"active attempt not found for task {task_id} and employee {employee_id}")
        attempt_id = attempt["attempt_id"]
    ts = created_at or now()
    normalized = normalize_progress_state(progress_state, summary=message)
    next_status = "running" if attempt["status"] in {"starting", "correcting"} and normalized["layer"] in {"received", "working", "waiting"} else attempt["status"]
    update_fields = ["last_progress_at = ?", "last_heartbeat_at = ?"]
    params: list[object] = [ts, ts]
    if next_status != attempt["status"]:
        update_fields.append("status = ?")
        params.append(next_status)
    params.append(attempt_id)
    conn.execute(f"UPDATE execution_attempts SET {', '.join(update_fields)} WHERE attempt_id = ?", params)
    if normalized["layer"] in {"received", "working"} and next_status == "running":
        conn.execute("UPDATE tasks SET status = 'claimed', claimed_by = ?, blocker = '', updated_at = ? WHERE id = ?", (employee_id, ts, task_id))
    conn.commit()
    event_payload = {
        "attempt_id": attempt_id,
        "employee_id": employee_id,
        "progress_state": progress_state,
        "progress_layer": normalized.get("layer", ""),
        "progress_label": normalized.get("label", ""),
        "message": message,
        "progress": progress,
        "payload": payload or {},
    }
    event = record_event(conn, "task.progress", employee_id, task_id=task_id, trace_id=attempt["trace_id"], payload=event_payload)
    audit(conn, employee_id, "task.progress", task_id, {"attempt_id": attempt_id, "state": progress_state, "progress": progress, "event_id": event["id"]})
    return {"attempt": row_by_id(conn, "execution_attempts", "attempt_id", attempt_id), "event_id": event["id"], "progress": event_payload}


def cmd_task_run(args: argparse.Namespace) -> int:
    conn = connect()
    actor = resolve_employee_alias(args.by)
    agent = resolve_employee_alias(args.agent)
    require_employee(conn, actor)
    require_employee(conn, agent)
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
    if not task:
        emit({"ok": False, "error": "task not found", "task_id": args.task_id})
        return 2
    if agent != task["target_agent"] and agent != task["claimed_by"]:
        emit({"ok": False, "error": "agent is not assigned to task", "task_id": args.task_id, "agent": agent})
        return 2
    policy = managed_runtime_policy(
        max_runtime_seconds=args.max_runtime_seconds,
        heartbeat_interval_seconds=args.heartbeat_interval_seconds,
        progress_interval_seconds=args.progress_interval_seconds,
        stale_after_seconds=args.stale_after_seconds,
        supervisor_check_interval_seconds=args.supervisor_check_interval_seconds,
        max_corrections=args.max_corrections,
        max_retries=args.max_retries,
    )
    result = start_execution_attempt_internal(
        conn,
        task_id=args.task_id,
        employee_id=agent,
        adapter_type=args.adapter_type,
        metadata={"started_by": actor, "managed": True},
        status="starting",
        runtime_policy=policy,
        pid=args.pid,
        session_key=args.session_key,
    )
    conn.execute("UPDATE tasks SET status = 'claimed', claimed_by = ?, blocker = '', updated_at = ? WHERE id = ?", (agent, now(), args.task_id))
    conn.commit()
    event = record_event(conn, "task.managed_run.started", actor, task_id=args.task_id, trace_id=result["attempt"]["trace_id"], payload={"attempt_id": result["attempt"]["attempt_id"], "agent": agent, "runtime_policy": policy})
    audit(conn, actor, "task.run", args.task_id, {"attempt_id": result["attempt"]["attempt_id"], "agent": agent, "runtime_policy": policy, "event_id": event["id"]})
    emit({"ok": True, "task_id": args.task_id, "attempt": row_by_id(conn, "execution_attempts", "attempt_id", result["attempt"]["attempt_id"]), "runtime_policy": policy, "event_id": event["id"]})
    return 0


def cmd_task_attempts(args: argparse.Namespace) -> int:
    conn = connect()
    require_task(conn, args.task_id)
    emit({"ok": True, "task_id": args.task_id, "attempts": task_attempts(conn, args.task_id)})
    return 0


def cmd_task_progress(args: argparse.Namespace) -> int:
    conn = connect()
    agent = resolve_employee_alias(args.agent)
    try:
        payload = parse_json_arg(args.payload, {})
        if not isinstance(payload, dict):
            raise SystemExit("payload must be a JSON object")
        result = report_task_progress_internal(
            conn,
            task_id=args.task_id,
            employee_id=agent,
            attempt_id=args.attempt_id,
            progress_state=args.state,
            message=args.message,
            progress=args.progress,
            payload=payload,
            created_at=args.at,
        )
    except SystemExit as exc:
        emit({"ok": False, "error": str(exc), "task_id": args.task_id, "agent": agent, "attempt_id": args.attempt_id})
        return 2
    emit({"ok": True, "task_id": args.task_id, "agent": agent, **result})
    return 0


def cmd_task_probe(args: argparse.Namespace) -> int:
    conn = connect()
    actor = resolve_employee_alias(args.by)
    try:
        result = record_task_probe_internal(
            conn,
            task_id=args.task_id,
            actor=actor,
            attempt_id=args.attempt_id,
            message=args.message,
            reason=args.reason,
        )
    except SystemExit as exc:
        emit({"ok": False, "error": str(exc), "task_id": args.task_id, "by": actor, "attempt_id": args.attempt_id})
        return 2
    emit({"ok": True, "task_id": args.task_id, "by": actor, **result})
    return 0


def cmd_task_correct(args: argparse.Namespace) -> int:
    conn = connect()
    actor = resolve_employee_alias(args.by)
    require_employee(conn, actor)
    task = require_task(conn, args.task_id)
    attempt = row_by_id(conn, "execution_attempts", "attempt_id", args.attempt_id)
    if attempt["task_id"] != args.task_id:
        emit({"ok": False, "error": "attempt does not belong to task", "task_id": args.task_id, "attempt_id": args.attempt_id})
        return 2
    supervisor_state = attempt_json_field(attempt, "supervisor_state_json")
    ts = now()
    if args.ack:
        supervisor_state["corrections_acknowledged"] = int(supervisor_state.get("corrections_acknowledged", 0) or 0) + 1
        event_type = "supervisor.correction_acknowledged"
        status = "running"
    else:
        requested = int(supervisor_state.get("corrections_requested", 0) or 0)
        policy = attempt_json_field(attempt, "runtime_policy_json")
        max_corrections = int(policy.get("max_corrections", DEFAULT_RUNTIME_POLICY["max_corrections"]) or DEFAULT_RUNTIME_POLICY["max_corrections"])
        if requested >= max_corrections:
            reason = "max corrections exceeded"
            supervisor_state["blocked_reason"] = reason
            supervisor_state["last_blocked_at"] = ts
            conn.execute(
                "UPDATE execution_attempts SET status = 'failed', supervisor_state_json = ?, finished_at = ?, error_message = ? WHERE attempt_id = ?",
                (json.dumps(supervisor_state, ensure_ascii=False), ts, reason, args.attempt_id),
            )
            conn.execute("UPDATE tasks SET status = 'blocked', blocker = ?, updated_at = ? WHERE id = ?", (reason, ts, args.task_id))
            conn.commit()
            event = record_event(conn, "task.blocked", actor, task_id=args.task_id, trace_id=attempt["trace_id"], payload={"attempt_id": args.attempt_id, "reason": reason, "max_corrections": max_corrections, "message": args.message})
            audit(conn, actor, "task.blocked.max_corrections", args.task_id, {"attempt_id": args.attempt_id, "reason": reason, "event_id": event["id"]})
            emit({"ok": True, "task_id": args.task_id, "status": "blocked", "reason": reason, "attempt": row_by_id(conn, "execution_attempts", "attempt_id", args.attempt_id), "supervisor_state": supervisor_state, "event_id": event["id"]})
            return 0
        supervisor_state["corrections_requested"] = requested + 1
        supervisor_state["last_correction"] = {"by": actor, "message": args.message, "created_at": ts}
        event_type = "supervisor.correction_requested"
        status = "correcting"
    conn.execute(
        "UPDATE execution_attempts SET status = ?, supervisor_state_json = ?, last_heartbeat_at = ? WHERE attempt_id = ?",
        (status, json.dumps(supervisor_state, ensure_ascii=False), ts, args.attempt_id),
    )
    conn.commit()
    event = record_event(conn, event_type, actor, task_id=args.task_id, trace_id=attempt["trace_id"], payload={"attempt_id": args.attempt_id, "message": args.message})
    audit(conn, actor, event_type, args.task_id, {"attempt_id": args.attempt_id, "message": args.message, "event_id": event["id"]})
    emit({"ok": True, "task_id": args.task_id, "attempt": row_by_id(conn, "execution_attempts", "attempt_id", args.attempt_id), "supervisor_state": supervisor_state, "event_id": event["id"]})
    return 0


def cmd_task_cancel(args: argparse.Namespace) -> int:
    conn = connect()
    actor = resolve_employee_alias(args.by)
    require_employee(conn, actor)
    task = require_task(conn, args.task_id)
    attempt = row_by_id(conn, "execution_attempts", "attempt_id", args.attempt_id)
    if attempt["task_id"] != args.task_id:
        emit({"ok": False, "error": "attempt does not belong to task", "task_id": args.task_id, "attempt_id": args.attempt_id})
        return 2
    ts = now()
    conn.execute(
        "UPDATE execution_attempts SET status = 'cancelled', cancel_requested_at = ?, finished_at = ?, error_message = ? WHERE attempt_id = ?",
        (ts, ts, args.reason, args.attempt_id),
    )
    conn.execute("UPDATE tasks SET status = 'cancelled', blocker = ?, updated_at = ? WHERE id = ?", (args.reason, ts, args.task_id))
    conn.execute("DELETE FROM locks WHERE resource_key = ?", (f"task:{args.task_id}",))
    conn.commit()
    # Tell the dispatcher so they stop waiting on a task that will never finish.
    notice_path = deliver_completion_notice(
        conn, dict(task), status="cancelled", blocker=f"已取消(cancelled by {actor}): {args.reason}", actor=actor)
    event = record_event(conn, "supervisor.cancel_requested", actor, task_id=args.task_id, trace_id=attempt["trace_id"], payload={"attempt_id": args.attempt_id, "reason": args.reason, "pid": attempt.get("pid", "")})
    audit(conn, actor, "task.cancel", args.task_id, {"attempt_id": args.attempt_id, "reason": args.reason, "event_id": event["id"]})
    emit({"ok": True, "task_id": args.task_id, "status": "cancelled", "attempt": row_by_id(conn, "execution_attempts", "attempt_id", args.attempt_id), "event_id": event["id"], "dispatcher_notified": notice_path or None})
    return 0


def cmd_supervisor_scan_attempts(args: argparse.Namespace) -> int:
    conn = connect()
    actor = resolve_employee_alias(args.by)
    require_employee(conn, actor)
    current = args.now or now()
    scan_rows = rows(conn, "SELECT * FROM execution_attempts WHERE status IN ('starting', 'running', 'correcting') ORDER BY started_at ASC")
    results = []
    for attempt in scan_rows:
        policy = attempt_json_field(attempt, "runtime_policy_json")
        max_runtime = int(policy.get("max_runtime_seconds", DEFAULT_RUNTIME_POLICY["max_runtime_seconds"]) or DEFAULT_RUNTIME_POLICY["max_runtime_seconds"])
        runtime_age = seconds_since(attempt.get("started_at") or "", current)
        stale_after = int(policy.get("stale_after_seconds", DEFAULT_RUNTIME_POLICY["stale_after_seconds"]) or DEFAULT_RUNTIME_POLICY["stale_after_seconds"])
        heartbeat_interval = int(policy.get("heartbeat_interval_seconds", DEFAULT_RUNTIME_POLICY["heartbeat_interval_seconds"]) or DEFAULT_RUNTIME_POLICY["heartbeat_interval_seconds"])
        heartbeat_warn_after = max(1, heartbeat_interval * 2)
        last_heartbeat_at = attempt.get("last_heartbeat_at") or attempt.get("started_at")
        heartbeat_age = seconds_since(last_heartbeat_at, current)
        heartbeat_status = "ok"
        heartbeat_event_id = ""
        last_progress_at = attempt.get("last_progress_at") or attempt.get("started_at")
        progress_age = seconds_since(last_progress_at, current)
        status = attempt["status"]
        task_status = ""
        stale_reason = ""
        event_id = ""
        if heartbeat_age >= heartbeat_warn_after:
            heartbeat_status = "heartbeat_warning"
            event = record_event(
                conn,
                "employee.warning",
                actor,
                task_id=attempt["task_id"],
                trace_id=attempt["trace_id"],
                payload={
                    "attempt_id": attempt["attempt_id"],
                    "employee_id": attempt["employee_id"],
                    "reason": "heartbeat_stale",
                    "heartbeat_age_seconds": heartbeat_age,
                    "heartbeat_warn_after_seconds": heartbeat_warn_after,
                    "last_heartbeat_at": last_heartbeat_at,
                },
            )
            heartbeat_event_id = event["id"]
        if runtime_age >= max_runtime:
            status = "stale"
            task_status = "stale"
            stale_reason = "runtime_exceeded"
            error = f"max runtime exceeded for {runtime_age}s"
            conn.execute("UPDATE execution_attempts SET status = 'stale', finished_at = ?, error_message = ? WHERE attempt_id = ?", (current, error, attempt["attempt_id"]))
            conn.execute("UPDATE tasks SET status = 'stale', blocker = ?, updated_at = ? WHERE id = ?", (error, current, attempt["task_id"]))
            conn.commit()
            event = record_event(
                conn,
                "task.stale",
                actor,
                task_id=attempt["task_id"],
                trace_id=attempt["trace_id"],
                payload={"attempt_id": attempt["attempt_id"], "reason": stale_reason, "runtime_age_seconds": runtime_age, "max_runtime_seconds": max_runtime},
            )
            event_id = event["id"]
        elif progress_age >= stale_after:
            status = "stale"
            task_status = "stale"
            stale_reason = "progress_stale"
            conn.execute("UPDATE execution_attempts SET status = 'stale', finished_at = ?, error_message = ? WHERE attempt_id = ?", (current, f"no progress for {progress_age}s", attempt["attempt_id"]))
            conn.execute("UPDATE tasks SET status = 'stale', blocker = ?, updated_at = ? WHERE id = ?", (f"no progress for {progress_age}s", current, attempt["task_id"]))
            conn.commit()
            event = record_event(conn, "task.stale", actor, task_id=attempt["task_id"], trace_id=attempt["trace_id"], payload={"attempt_id": attempt["attempt_id"], "reason": stale_reason, "progress_age_seconds": progress_age, "stale_after_seconds": stale_after})
            event_id = event["id"]
        results.append({
            **attempt,
            "status": status,
            "task_status": task_status,
            "stale_reason": stale_reason,
            "runtime_age_seconds": runtime_age,
            "max_runtime_seconds": max_runtime,
            "heartbeat_status": heartbeat_status,
            "heartbeat_age_seconds": heartbeat_age,
            "heartbeat_warn_after_seconds": heartbeat_warn_after,
            "heartbeat_event_id": heartbeat_event_id,
            "progress_age_seconds": progress_age,
            "stale_after_seconds": stale_after,
            "event_id": event_id,
        })
    audit(conn, actor, "supervisor.scan_attempts", "execution_attempts", {"count": len(results), "now": current})
    emit({"ok": True, "by": actor, "now": current, "attempts": results})
    return 0


def has_v3_file_flow(conn: sqlite3.Connection, task_id: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM artifacts WHERE task_id = ? LIMIT 1", (task_id,)).fetchone() or conn.execute("SELECT 1 FROM evidence WHERE task_id = ? LIMIT 1", (task_id,)).fetchone())


def final_evidence_for_path(conn: sqlite3.Connection, task_id: str, evidence_path: str) -> dict | None:
    def path_variants(value: str) -> set[str]:
        variants = {str(value or "")}
        if value:
            try:
                variants.add(str(Path(value).resolve()))
            except (OSError, RuntimeError):
                pass
        return {item for item in variants if item}

    evidence_path_variants = path_variants(evidence_path)
    evidence_rows = rows(
        conn,
        """
        SELECT * FROM evidence
        WHERE task_id = ?
          AND is_final = 1
        ORDER BY created_at DESC
        """,
        (task_id,),
    )
    for evidence_row in evidence_rows:
        if path_variants(evidence_row.get("path_or_url", "")) & evidence_path_variants:
            return evidence_row
    artifact_rows = rows(conn, "SELECT artifact_id, metadata_json FROM artifacts WHERE task_id = ? AND is_final = 1", (task_id,))
    matching_artifact_ids = []
    for artifact in artifact_rows:
        metadata = parse_json_arg(artifact.get("metadata_json", ""), {})
        if isinstance(metadata, dict) and path_variants(str(metadata.get("original_path") or "")) & evidence_path_variants:
            matching_artifact_ids.append(artifact["artifact_id"])
    if not matching_artifact_ids:
        return None
    placeholders = ",".join("?" for _ in matching_artifact_ids)
    row = conn.execute(
        f"""
        SELECT * FROM evidence
        WHERE task_id = ?
          AND artifact_id IN ({placeholders})
          AND is_final = 1
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (task_id, *matching_artifact_ids),
    ).fetchone()
    return dict(row) if row else None


def auto_promote_workspace_evidence(conn: sqlite3.Connection, *, task_id: str, agent: str, evidence_path: str, summary: str) -> dict | None:
    try:
        path = require_workspace_path(conn, task_id, evidence_path)
    except ValueError:
        return None
    if not path.exists() or not path.is_file():
        return None
    existing = final_evidence_for_path(conn, task_id, str(path))
    if existing:
        return existing
    workspace_root = Path(task_workspace(conn, task_id)["path"]).resolve()
    try:
        rel_path = path.relative_to(workspace_root)
    except ValueError:
        return None
    if not rel_path.parts or rel_path.parts[0] not in {"evidence", "final"}:
        return None
    artifact = register_artifact_internal(
        conn,
        task_id=task_id,
        employee_id=agent,
        path=str(path),
        artifact_type=path.suffix.lstrip(".") or "file",
        name=path.name,
        stage="final",
        summary=summary or "auto-promoted task evidence",
        is_final=True,
        metadata={"registered_by": "task.done.auto_promote"},
    )
    promoted = promote_artifact_to_evidence_internal(conn, artifact_id=artifact["artifact"]["artifact_id"], by=agent, summary=summary)
    return promoted["evidence"]


def ensure_final_evidence_for_existing_path(conn: sqlite3.Connection, *, task_id: str, agent: str, evidence_path: str, summary: str) -> dict | None:
    path = Path(evidence_path)
    if not evidence_path or not path.exists() or not path.is_file():
        return None
    existing = final_evidence_for_path(conn, task_id, str(path))
    if existing:
        return existing
    promoted = auto_promote_workspace_evidence(conn, task_id=task_id, agent=agent, evidence_path=str(path), summary=summary)
    if promoted:
        return promoted
    workspace_root = Path(task_workspace(conn, task_id)["path"]).resolve()
    safe_name = safe_path_token(path.name or "runtime-verification-evidence.txt")
    copied_path = workspace_root / "evidence" / safe_name
    copied_path.parent.mkdir(parents=True, exist_ok=True)
    if copied_path.resolve() != path.resolve():
        copied_path.write_bytes(path.read_bytes())
    path = copied_path
    artifact = register_artifact_internal(
        conn,
        task_id=task_id,
        employee_id=agent,
        path=str(path),
        artifact_type=path.suffix.lstrip(".") or "file",
        name=path.name,
        stage="final",
        summary=summary or "runtime verification evidence",
        is_final=True,
        metadata={"registered_by": "runtime.verify_adapters.legacy_path", "original_path": str(Path(evidence_path))},
    )
    promoted = promote_artifact_to_evidence_internal(conn, artifact_id=artifact["artifact"]["artifact_id"], by=agent, summary=summary or "runtime verification evidence")
    return promoted["evidence"]


def ensure_runtime(conn: sqlite3.Connection, runtime: str) -> None:
    ts = now()
    conn.execute(
        """
        INSERT INTO employee_runtimes(runtime, command, status, notes, created_at, updated_at)
        VALUES (?, '', 'registered', ?, ?, ?)
        ON CONFLICT(runtime) DO UPDATE SET updated_at = excluded.updated_at
        """,
        (runtime, KNOWN_RUNTIMES.get(runtime, "Custom runtime adapter"), ts, ts),
    )
    conn.commit()


def runtime_registered(conn: sqlite3.Connection, runtime: str) -> bool:
    if runtime in KNOWN_RUNTIMES:
        return True
    return bool(conn.execute("SELECT 1 FROM employee_runtimes WHERE runtime = ? AND status != 'disabled'", (runtime,)).fetchone())


def require_runtime(conn: sqlite3.Connection, runtime: str) -> None:
    if not runtime_registered(conn, runtime):
        raise SystemExit(f"unknown runtime: {runtime}; run companyctl runtime register --runtime {runtime}")


def employee_paths(employee_id: str) -> dict[str, Path]:
    base = EMPLOYEES_DIR / employee_id
    return {
        "base": base,
        "profile": base / "profile.json",
        "capabilities": base / "capabilities.json",
        "rules": base / "rules.md",
        "permissions": base / "permissions.json",
        "heartbeat": base / "heartbeat.json",
        "inbox": base / "inbox",
        "outbox": base / "outbox",
        "reports": base / "reports",
    }


def load_communication_config() -> dict:
    # Thin wrapper: hand the COMMUNICATIONS_PATH anchor to the pure reader.
    return _core_config.load_communication_config(COMMUNICATIONS_PATH)


def write_communication_config(config: dict) -> None:
    COMMUNICATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    COMMUNICATIONS_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def notification_settings() -> dict:
    config = load_communication_config()
    notification = config.get("notification", {}) if isinstance(config.get("notification"), dict) else {}
    telegram_accounts = notification.get("telegram_accounts", {}) if isinstance(notification.get("telegram_accounts"), dict) else {}
    sanitized_accounts = {}
    for account_id, account in telegram_accounts.items():
        if not isinstance(account, dict):
            continue
        token_env = str(account.get("bot_token_env", "") or "")
        sanitized_accounts[str(account_id)] = {
            "channel": str(account.get("channel", "telegram") or "telegram"),
            "bot_token_env": token_env,
            "token_configured": bool(token_env and os.environ.get(token_env)),
            "default_target": str(account.get("default_target", "") or ""),
            "updated_at": str(account.get("updated_at", "") or ""),
        }
    employee_notifications = notification.get("employee_notifications", {}) if isinstance(notification.get("employee_notifications"), dict) else {}
    routes = notification.get("routes", {}) if isinstance(notification.get("routes"), dict) else {}
    sanitized_routes = {}
    for route_id, route in routes.items():
        if not isinstance(route, dict):
            continue
        sanitized_routes[str(route_id)] = {
            "enabled": bool(route.get("enabled", True)),
            "account": str(route.get("account", employee_notifications.get("account", "")) or ""),
            "target": str(route.get("target", employee_notifications.get("target", "")) or ""),
        }
    return {
        "ok": True,
        "file": str(COMMUNICATIONS_PATH),
        "employee_notifications": {
            "enabled": bool(employee_notifications.get("enabled", False)),
            "channel": str(employee_notifications.get("channel", "telegram") or "telegram"),
            "account": str(employee_notifications.get("account", "") or ""),
            "target": str(employee_notifications.get("target", "") or ""),
        },
        "routes": sanitized_routes,
        "telegram_accounts": sanitized_accounts,
    }


def update_notification_settings(payload: dict) -> dict:
    forbidden = {"token", "bot_token", "telegram_bot_token", "secret", "password"}
    if any(key in payload for key in forbidden):
        return {"ok": False, "error": "do not store Telegram bot token in config; set an environment variable and save only its name"}
    account_id = str(payload.get("telegram_account") or payload.get("account") or "employee-notify").strip()
    token_env = str(payload.get("telegram_bot_token_env") or payload.get("bot_token_env") or "").strip()
    target = str(payload.get("telegram_default_target") or payload.get("default_target") or payload.get("target") or "").strip()
    enabled = bool(payload.get("employee_notifications_enabled", payload.get("enabled", False)))
    if not account_id:
        return {"ok": False, "error": "telegram account id is required"}
    if not token_env:
        return {"ok": False, "error": "telegram bot token environment variable name is required"}
    config = load_communication_config()
    config.setdefault("version", 1)
    notification = config.setdefault("notification", {})
    accounts = notification.setdefault("telegram_accounts", {})
    accounts[account_id] = {
        "channel": "telegram",
        "bot_token_env": token_env,
        "default_target": target,
        "updated_at": now(),
    }
    notification["employee_notifications"] = {
        "enabled": enabled,
        "channel": "telegram",
        "account": account_id,
        "target": target,
        "updated_at": now(),
    }
    routes = notification.setdefault("routes", {})
    for route_id in ("approval", "error"):
        route = routes.setdefault(route_id, {})
        route.update({"enabled": enabled, "account": account_id, "target": target, "updated_at": now()})
    write_communication_config(config)
    return notification_settings()


class NotificationDispatcher:
    # Stays in companyctl (not notify.py): its methods call the senders by bare name, and the suite
    # patches companyctl.send_* to intercept — a patch that only reaches lookups in this namespace.
    def __init__(self, settings: dict):
        self.settings = settings or {}

    def send_macos_alert(self, title: str, body: str, kind: str = "general") -> dict:
        text = f"{title}\n{body}".strip() if title else body
        return send_macos_notification(text=text, title=title or "Company Kernel", subtitle=kind)

    def send_telegram_message(self, chat_id: str, text: str, account_id: str = "") -> dict:
        accounts = self.settings.get("telegram_accounts", {}) if isinstance(self.settings.get("telegram_accounts"), dict) else {}
        account = accounts.get(account_id, {}) if account_id else next(iter(accounts.values()), {})
        token_env = str(account.get("bot_token_env", "") or "")
        return send_telegram_notification(token=os.environ.get(token_env, ""), chat_id=chat_id, text=text)

    def send_slack_webhook(self, webhook_url: str, payload: dict) -> dict:
        return send_slack_webhook(webhook_url, payload)

    def send(self, target: str, *, title: str = "", body: str = "", kind: str = "general", account_id: str = "", dry_run: bool = False) -> dict:
        platform, address = resolve_notification_target(target)
        text = f"{title}\n{body}".strip() if title else body
        result = {"ok": True, "dry_run": dry_run, "platform": platform, "kind": kind, "target": target}
        if dry_run:
            return result
        if platform == "macos":
            return {**result, **self.send_macos_alert(title or "Company Kernel", body, kind)}
        if platform == "telegram":
            return {**result, **self.send_telegram_message(address, text, account_id)}
        if platform == "slack":
            webhooks = self.settings.get("slack_webhooks", {}) if isinstance(self.settings.get("slack_webhooks"), dict) else {}
            hook = webhooks.get(address, {}) if isinstance(webhooks.get(address, {}), dict) else {}
            webhook_env = str(hook.get("webhook_url_env", "") or "")
            return {**result, **self.send_slack_webhook(os.environ.get(webhook_env, ""), {"text": text, "kind": kind})}
        return {**result, "ok": False, "error": f"unsupported notification platform: {platform}"}


def notification_send_result(*, message: str, target: str = "", account_id: str = "", subject: str = "", kind: str = "general", dry_run: bool = False, reply_markup: dict | None = None) -> dict:
    settings = notification_settings()
    notifications = settings["employee_notifications"]
    route = settings.get("routes", {}).get(kind, {}) if isinstance(settings.get("routes"), dict) else {}
    if route and not route.get("enabled", True):
        return {"ok": True, "skipped": True, "reason": "notification route disabled", "kind": kind}
    account_id = account_id or route.get("account", "") or notifications.get("account", "")
    accounts = settings["telegram_accounts"]
    account = accounts.get(account_id)
    account_default_target = account.get("default_target", "") if isinstance(account, dict) else ""
    explicit_target = str(target or "").strip()
    if not explicit_target and not account:
        return {"ok": False, "error": "notification account is not configured", "account": account_id}
    target = explicit_target or route.get("target", "") or notifications.get("target", "") or account_default_target
    try:
        platform, chat_id = resolve_notification_target(target)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "account": account_id, "target": target}
    if platform != "macos" and not account:
        return {"ok": False, "error": "notification account is not configured", "account": account_id}
    if platform != "telegram":
        result = {"ok": True, "dry_run": dry_run, "platform": platform, "kind": kind, "account": account_id, "target": f"{platform}:{chat_id}", "token_env": "", "token_configured": True}
        try:
            sent = NotificationDispatcher(settings).send(f"{platform}:{chat_id}", title=subject or "Company Kernel", body=message, kind=kind, account_id=account_id, dry_run=dry_run)
        except (ValueError, OSError, subprocess.SubprocessError, urllib.error.URLError, TimeoutError) as exc:
            return {**result, "ok": False, "error": str(exc)}
        return {**result, **sent}
    token_env = str(account.get("bot_token_env", "") or "")
    token = os.environ.get(token_env, "")
    text = f"{subject}\n{message}".strip() if subject else message
    result = {
        "ok": True,
        "dry_run": dry_run,
        "platform": platform,
        "kind": kind,
        "account": account_id,
        "target": f"telegram:{chat_id}",
        "token_env": token_env,
        "token_configured": bool(token),
    }
    if dry_run:
        return result
    if not token:
        return {**result, "ok": False, "error": "telegram bot token environment variable is not set"}
    try:
        sent = send_telegram_notification(token=token, chat_id=chat_id, text=text, reply_markup=reply_markup)
    except (ValueError, urllib.error.URLError, TimeoutError) as exc:
        return {**result, "ok": False, "error": str(exc)}
    return {**result, **sent}


def normalize_employee_lookup(value: str) -> str:
    return " ".join(str(value or "").strip().split()).casefold()


def communication_name_aliases(employee_id: str, name: str) -> list[str]:
    aliases = []
    clean_name = " ".join(str(name or "").strip().split())
    if clean_name and clean_name != employee_id:
        aliases.append(clean_name)
        compact = clean_name.replace(" ", "-").lower()
        if compact and compact not in {employee_id, clean_name}:
            aliases.append(compact)
    return aliases


def resolve_employee_alias(employee_id: str, *, strict: bool = False) -> str:
    raw = str(employee_id or "").strip()
    if not raw:
        return raw
    config = load_communication_config()
    aliases = {str(key): str(value) for key, value in config.get("aliases", {}).items()}
    if raw in aliases:
        return aliases[raw]
    db = DB_PATH
    if db.exists():
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows_found = conn.execute("SELECT id, name FROM employees").fetchall()
            ids = {str(row["id"]): str(row["id"]) for row in rows_found}
            if raw in ids:
                return raw
            names = {normalize_employee_lookup(row["name"]): str(row["id"]) for row in rows_found if str(row["name"] or "").strip()}
            lowered_ids = {normalize_employee_lookup(row["id"]): str(row["id"]) for row in rows_found}
        finally:
            conn.close()
        normalized_aliases = {normalize_employee_lookup(key): value for key, value in aliases.items()}
        normalized = normalize_employee_lookup(raw)
        matches = []
        for lookup in (normalized_aliases, names, lowered_ids):
            if normalized in lookup:
                matches.append(lookup[normalized])
        matches = list(dict.fromkeys(matches))
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise SystemExit(f"ambiguous employee name or alias: {raw} -> {', '.join(matches)}")
    if strict:
        raise SystemExit(f"unknown employee name or alias: {raw}")
    return raw


def sync_employee_name_alias(employee_id: str, name: str, *, dry_run: bool = False) -> dict:
    config = load_communication_config()
    config.setdefault("version", 1)
    aliases = config.setdefault("aliases", {})
    employees = config.setdefault("employees", {})
    profile = employees.setdefault(employee_id, {})
    profile["display_name"] = name
    for alias in communication_name_aliases(employee_id, name):
        aliases[alias] = employee_id
    if not dry_run:
        write_communication_config(config)
    return {"aliases": {alias: employee_id for alias in communication_name_aliases(employee_id, name)}, "profile": profile}


def communication_list(config: dict, employee_id: str, key: str) -> list[str]:
    employee = config.get("employees", {}).get(employee_id, {})
    return [resolve_employee_alias(str(item)) for item in employee.get(key, [])]


def direct_reply_defaults(source: str, target: str) -> dict:
    config = load_communication_config()
    employee_defaults = config.get("employees", {}).get(target, {})
    source_defaults = config.get("employees", {}).get(source, {})
    profile_defaults = load_json_or_default(employee_paths(target)["profile"], {})

    merged: dict[str, object] = {}
    for candidate in (source_defaults, employee_defaults, profile_defaults):
        if not isinstance(candidate, dict):
            continue
        for key in ("default_user_reply_channel", "default_user_reply_account", "default_user_reply_to"):
            value = str(candidate.get(key, "") or "").strip()
            if value:
                merged[key] = value
        if candidate.get("default_user_reply_deliver") is not None:
            merged["default_user_reply_deliver"] = bool(candidate.get("default_user_reply_deliver"))
    return {
        "deliver": bool(merged.get("default_user_reply_deliver", False)),
        "reply_channel": str(merged.get("default_user_reply_channel", "") or ""),
        "reply_account": str(merged.get("default_user_reply_account", "") or ""),
        "reply_to": str(merged.get("default_user_reply_to", "") or ""),
    }


def communication_policy_decision(source: str, target: str, action: str) -> dict:
    config = load_communication_config()
    source = resolve_employee_alias(source)
    target = resolve_employee_alias(target)
    policy = config.get("policy", {})
    mode = policy.get("mode", "open")
    employees = config.get("employees", {})
    source_profile = employees.get(source, {})
    target_profile = employees.get(target, {})
    if source_profile.get("communication_paused"):
        return {"allowed": False, "mode": mode, "source": source, "target": target, "action": action, "reason": "source communication paused"}
    if target_profile.get("communication_paused"):
        return {"allowed": False, "mode": mode, "source": source, "target": target, "action": action, "reason": "target communication paused"}
    relation_key = "can_assign_to" if action == "task.submit" else "can_talk_to"
    blocked_key = "blocked_assign_to" if action == "task.submit" else "blocked_talk_to"
    blocked = communication_list(config, source, blocked_key)
    allowed = communication_list(config, source, relation_key)
    if target in blocked or "*" in blocked:
        return {"allowed": False, "mode": mode, "source": source, "target": target, "action": action, "reason": f"{blocked_key} blocks target"}
    if mode in {"strict", "allowlist"} and allowed and target not in allowed and "*" not in allowed:
        return {"allowed": False, "mode": mode, "source": source, "target": target, "action": action, "reason": f"{relation_key} does not include target"}
    return {"allowed": True, "mode": mode, "source": source, "target": target, "action": action, "reason": "allowed"}


def require_communication_allowed(source: str, target: str, action: str) -> dict:
    decision = communication_policy_decision(source, target, action)
    if not decision["allowed"]:
        raise SystemExit(f"communication denied: {decision['reason']} ({decision['source']} -> {decision['target']} {action})")
    return decision


def default_capabilities(profile: dict) -> dict:
    runtime = profile.get("runtime", "local")
    base = {
        "agent_id": profile.get("id", ""),
        "runtime": runtime,
        "role": profile.get("role", ""),
        "skills": [],
        "tools": [],
        "preferred_task_types": [],
        "handoff": {
            "can_receive_tasks": True,
            "can_send_messages": True,
            "requires_adapter": runtime not in {"local"},
        },
        "updated_at": now(),
    }
    presets = {
        "codex": {
            "skills": ["code-editing", "testing", "review", "git-workflow", "project-delivery"],
            "tools": ["codex exec", "shell", "apply_patch"],
            "preferred_task_types": ["engineering", "debugging", "test-fix", "repo-maintenance"],
        },
        "hermes": {
            "skills": ["local-automation", "browser-automation", "model-routing", "tool-orchestration"],
            "tools": ["hermes -z", "local tools"],
            "preferred_task_types": ["automation", "research", "ops-support"],
        },
        "openclaw": {
            "skills": ["business-ops", "agent-bus", "workspace-operations"],
            "tools": ["openclaw", "oc bus"],
            "preferred_task_types": ["business-agent-task", "line-ops", "workspace-task"],
        },
        "claude": {
            "skills": ["analysis", "documentation", "code-understanding"],
            "tools": ["claude -p"],
            "preferred_task_types": ["analysis", "documentation", "review"],
        },
        "trae": {
            "skills": ["ide-development", "code-editing"],
            "tools": ["trae chat"],
            "preferred_task_types": ["ide-coding", "implementation"],
        },
        "antigravity": {
            "skills": ["multi-agent-ide", "browser-workflow"],
            "tools": ["Antigravity app"],
            "preferred_task_types": ["gui-assisted-development", "browser-workflow"],
        },
    }
    preset = presets.get(runtime, {})
    base.update({k: preset.get(k, base[k]) for k in ("skills", "tools", "preferred_task_types")})
    return base


def load_json_or_default(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else default
    except json.JSONDecodeError:
        return default


def read_json_file_checked(path: Path) -> tuple[dict, str]:
    if not path.exists():
        return {}, "missing"
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}, "invalid_json"
    if not isinstance(obj, dict):
        return {}, "not_object"
    return obj, ""


def employee_capability_issues(conn: sqlite3.Connection) -> list[dict]:
    issues = []
    for employee in conn.execute("SELECT id, runtime FROM employees WHERE status = 'active' ORDER BY id").fetchall():
        paths = employee_paths(employee["id"])
        capabilities, cap_error = read_json_file_checked(paths["capabilities"])
        permissions, perm_error = read_json_file_checked(paths["permissions"])
        if cap_error:
            issues.append({"agent": employee["id"], "file": str(paths["capabilities"]), "reason": f"capabilities_{cap_error}"})
        else:
            for key in ("skills", "tools", "preferred_task_types"):
                if not isinstance(capabilities.get(key), list):
                    issues.append({"agent": employee["id"], "file": str(paths["capabilities"]), "reason": f"capabilities_{key}_not_list"})
            if not isinstance(capabilities.get("handoff", {}), dict):
                issues.append({"agent": employee["id"], "file": str(paths["capabilities"]), "reason": "capabilities_handoff_not_object"})
        if perm_error:
            issues.append({"agent": employee["id"], "file": str(paths["permissions"]), "reason": f"permissions_{perm_error}"})
        else:
            for key in ("can_submit_tasks", "can_claim_tasks", "can_modify_kernel"):
                if not isinstance(permissions.get(key), bool):
                    issues.append({"agent": employee["id"], "file": str(paths["permissions"]), "reason": f"permissions_{key}_not_bool"})
            if not isinstance(permissions.get("requires_approval_for", []), list):
                issues.append({"agent": employee["id"], "file": str(paths["permissions"]), "reason": "permissions_requires_approval_for_not_list"})
    return issues


def task_evidence_issues(conn: sqlite3.Connection) -> list[dict]:
    issues = []
    completed = rows(
        conn,
        """
        SELECT id, target_agent, evidence_path, updated_at
        FROM tasks
        WHERE status = 'completed'
        ORDER BY updated_at DESC
        LIMIT 100
        """,
    )
    for task in completed:
        evidence_path = str(task.get("evidence_path") or "")
        if not evidence_path:
            issues.append({"task_id": task["id"], "agent": task["target_agent"], "reason": "completed_without_evidence", "evidence_path": ""})
        elif not Path(evidence_path).exists():
            issues.append({"task_id": task["id"], "agent": task["target_agent"], "reason": "evidence_missing_on_disk", "evidence_path": evidence_path})
    blocked_without_blocker = rows(
        conn,
        """
        SELECT id, target_agent, updated_at
        FROM tasks
        WHERE status = 'blocked' AND TRIM(COALESCE(blocker, '')) = ''
        ORDER BY updated_at DESC
        LIMIT 100
        """,
    )
    for task in blocked_without_blocker:
        issues.append({"task_id": task["id"], "agent": task["target_agent"], "reason": "blocked_without_blocker", "evidence_path": ""})
    return issues


def daemon_last_run_path() -> Path:
    return STATE_DIR / "daemon" / "last-run.json"


def enabled_worker_agents() -> set:
    """Agent ids that have an ENABLED adapter worker in config/daemon.json.

    Used by the health check to distinguish 'stale' (a worker that should be alive but
    isn't) from 'idle' (an employee with no worker, expected to be quiet).
    """
    config_path = ROOT / "config" / "daemon.json"
    if not config_path.exists():
        return set()
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    agents = set()
    for worker in cfg.get("adapter_workers", []):
        if worker.get("enabled") and worker.get("agent"):
            agents.add(str(worker["agent"]))
    for agent in cfg.get("heartbeat_agents", []):
        if agent:
            agents.add(str(agent))
    return agents


def daemon_health(max_age_minutes: int = 75) -> dict:
    # 75 > the longest single adapter timeout (codex per-task cap is 60 min). The daemon beats a
    # liveness heartbeat before each adapter, so a busy daemon running a long task stays "fresh"
    # and doesn't trip a false 内核异常; only a genuinely hung/dead loop exceeds this. launchd
    # KeepAlive restarts a truly dead daemon in seconds, so this slow bound is just a backstop.
    path = daemon_last_run_path()
    if not path.exists():
        return {
            "ok": False,
            "state_file": str(path),
            "last_run_at": "",
            "age_minutes": None,
            "max_age_minutes": max_age_minutes,
            "reason": "missing_daemon_state",
        }
    state = load_json_or_default(path, {})
    last_run_at = str(state.get("at") or "")
    dt = parse_time(last_run_at) if last_run_at else None
    age_minutes = None if dt is None else int((datetime.now(timezone.utc).astimezone() - dt).total_seconds() // 60)
    stale = age_minutes is None or age_minutes > max_age_minutes
    return {
        "ok": bool(state.get("ok")) and not stale,
        "state_file": str(path),
        "last_run_at": last_run_at,
        "age_minutes": age_minutes,
        "max_age_minutes": max_age_minutes,
        "reason": "daemon_stale" if stale else ("" if state.get("ok") else "daemon_last_run_failed"),
    }


def launchd_health() -> dict:
    installed_path = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    installed = installed_path.exists()
    template_exists = LAUNCHD_TEMPLATE.exists()
    matches_template = False
    installed_root = ""
    warning = ""
    if installed and template_exists:
        installed_text = installed_path.read_text(encoding="utf-8")
        rendered_template = LAUNCHD_TEMPLATE.read_text(encoding="utf-8").replace("__COMPANY_KERNEL_ROOT__", str(ROOT))
        matches_template = rendered_template == installed_text
        try:
            import plistlib

            payload = plistlib.loads(installed_text.encode("utf-8"))
            args = payload.get("ProgramArguments") or []
            working_dir = str(payload.get("WorkingDirectory") or "")
            if working_dir:
                installed_root = str(Path(working_dir).expanduser().resolve())
            elif isinstance(args, list) and args:
                daemon_path = Path(str(args[0])).expanduser()
                if daemon_path.name == "company-daemon":
                    installed_root = str(daemon_path.parent.parent.resolve())
        except Exception:
            installed_root = ""
    current_root_path = ROOT.expanduser().resolve()
    current_root = str(current_root_path)
    database_isolated = bool(installed_root and Path(installed_root).expanduser().resolve() != current_root_path)
    if database_isolated:
        warning = "running_from_alternate_clone: current company.sqlite may be isolated from installed daemon root"
    return {
        "label": LAUNCHD_LABEL,
        "template": str(LAUNCHD_TEMPLATE),
        "template_exists": template_exists,
        "installed_path": str(installed_path),
        "installed": installed,
        "matches_template": matches_template,
        "installed_root": installed_root,
        "current_root": current_root,
        "database_isolated": database_isolated,
        "warning": warning,
        "recommended_interval_seconds": 180,
        "install_command": "bash bin/company-daemon-install-launchd",
        "uninstall_command": "bash bin/company-daemon-uninstall-launchd",
        "verify_command": "bin/companyctl doctor --summary",
    }


def openclaw_root() -> Path:
    env = os.environ.get("OPENCLAW_ROOT")
    if env:
        return Path(env).expanduser()
    return Path.home() / "openclaw"


def count_spool_files(spool_dir: Path) -> dict:
    pending = sorted(spool_dir.glob("*.json")) if spool_dir.exists() else []
    processing = sorted(spool_dir.glob("*.processing")) if spool_dir.exists() else []
    stale_processing = []
    cutoff = datetime.now(timezone.utc).astimezone() - timedelta(minutes=15)
    for path in processing:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).astimezone()
        if mtime < cutoff:
            stale_processing.append(str(path))
    return {
        "path": str(spool_dir),
        "exists": spool_dir.exists(),
        "pending": len(pending),
        "processing": len(processing),
        "stale_processing": len(stale_processing),
        "stale_processing_files": stale_processing[:10],
        "pending_files": [str(path) for path in pending[:10]],
    }


def openclaw_runtime_inventory(conn: sqlite3.Connection | None = None) -> dict:
    root = openclaw_root()
    agents_dir = root / "agents"
    telegram_dir = root / "telegram"
    registered = set()
    if conn is not None:
        registered = {str(row["id"]) for row in rows(conn, "SELECT id FROM employees")}
    registered_aliases = set(registered)
    for employee_id in registered:
        registered_aliases.add(employee_id.replace("-", "_"))
        registered_aliases.add(employee_id.replace("_", "-"))

    def canonical_openclaw_id(value: str) -> str:
        return value.replace("-", "_")

    def is_registered_openclaw_id(value: str) -> bool:
        canonical = canonical_openclaw_id(value)
        return value in registered_aliases or canonical in registered_aliases or canonical.replace("_", "-") in registered_aliases

    agent_dirs = {}
    if agents_dir.exists():
        for path in sorted(agents_dir.iterdir()):
            if not path.is_dir() or path.name.startswith("."):
                continue
            session_file = path / "sessions" / "sessions.json"
            session_payload = load_json_or_default(session_file, {})
            session_count = len(session_payload) if isinstance(session_payload, dict) else 0
            normalized_id = canonical_openclaw_id(path.name)
            agent_dirs[path.name] = {
                "id": path.name,
                "normalized_id": normalized_id,
                "path": str(path),
                "session_file": str(session_file),
                "session_file_exists": session_file.exists(),
                "session_count": session_count,
                "registered": is_registered_openclaw_id(path.name),
            }
    spools = {}
    if telegram_dir.exists():
        for spool_dir in sorted(telegram_dir.glob("ingress-spool-*")):
            account = spool_dir.name.removeprefix("ingress-spool-")
            normalized_id = canonical_openclaw_id(account)
            profile = count_spool_files(spool_dir)
            profile.update(
                {
                    "id": account,
                    "normalized_id": normalized_id,
                    "registered": is_registered_openclaw_id(account),
                }
            )
            spools[account] = profile
    discovered_ids = {canonical_openclaw_id(item["id"]) for item in agent_dirs.values()}
    discovered_ids.update(canonical_openclaw_id(item["id"]) for item in spools.values())
    discovered_ids.update(item["normalized_id"] for item in agent_dirs.values() if item.get("normalized_id"))
    discovered_ids.update(item["normalized_id"] for item in spools.values() if item.get("normalized_id"))
    missing = sorted(item for item in discovered_ids if item and not is_registered_openclaw_id(item))
    return {
        "openclaw_root": str(root),
        "agents_dir": str(agents_dir),
        "telegram_dir": str(telegram_dir),
        "registered_employee_ids": sorted(registered),
        "agent_dirs": agent_dirs,
        "telegram_spools": spools,
        "counts": {
            "agent_dirs": len(agent_dirs),
            "telegram_spools": len(spools),
            "registered": len(registered),
            "missing_registered": len(missing),
        },
        "missing_registered": missing,
        "note": "Read-only inventory. It discovers OpenClaw runtime agents/spools and marks whether they are registered in Company Kernel; it does not onboard or modify them.",
    }


def openclaw_runtime_inventory_summary(inventory: dict) -> dict:
    agent_dirs = inventory.get("agent_dirs") or {}
    telegram_spools = inventory.get("telegram_spools") or {}
    missing_registered = list(inventory.get("missing_registered") or [])
    session_total = sum(int(item.get("session_count") or 0) for item in agent_dirs.values() if isinstance(item, dict))
    spool_pending = sum(int(item.get("pending") or 0) for item in telegram_spools.values() if isinstance(item, dict))
    spool_processing = sum(int(item.get("processing") or 0) for item in telegram_spools.values() if isinstance(item, dict))
    stale_processing = sum(int(item.get("stale_processing") or 0) for item in telegram_spools.values() if isinstance(item, dict))
    recommended_actions = []
    if missing_registered:
        recommended_actions.append("register_discovered_openclaw_agents")
    if stale_processing:
        recommended_actions.append("inspect_stale_telegram_spool_processing")
    if spool_pending or spool_processing:
        recommended_actions.append("monitor_openclaw_telegram_spools")
    health = "attention_required" if recommended_actions else "green"
    return {
        "ok": True,
        "mode": "read_only",
        "health": health,
        "openclaw_root": inventory.get("openclaw_root", ""),
        "mutates_openclaw": False,
        "counts": {
            "agent_dirs": int((inventory.get("counts") or {}).get("agent_dirs") or len(agent_dirs)),
            "telegram_spools": int((inventory.get("counts") or {}).get("telegram_spools") or len(telegram_spools)),
            "registered": int((inventory.get("counts") or {}).get("registered") or len(inventory.get("registered_employee_ids") or [])),
            "missing_registered": len(missing_registered),
            "sessions": session_total,
            "spool_pending": spool_pending,
            "spool_processing": spool_processing,
            "stale_processing": stale_processing,
        },
        "missing_registered": missing_registered[:20],
        "recommended_actions": recommended_actions or ["no_action_required"],
        "note": "Owner-readable OpenClaw runtime summary. Full details are available without --summary.",
    }


def _openclaw_json_sample(path: Path) -> dict:
    payload = load_json_or_default(path, {})
    if not isinstance(payload, dict):
        return {"file": str(path), "parse_error": "json_root_not_object"}
    return {
        "file": str(path),
        "source_agent": str(payload.get("source_agent") or payload.get("source") or ""),
        "target_agent": str(payload.get("target_agent") or payload.get("target") or ""),
        "type": str(payload.get("type") or payload.get("task_type") or ""),
        "priority": str(payload.get("priority") or ""),
        "status": str(payload.get("status") or ""),
        "action": str(payload.get("action") or ""),
        "updated_at": str(payload.get("updated_at") or payload.get("created_at") or ""),
    }


def _openclaw_agent_bus_state(root: Path, *, sample_limit: int = 5) -> dict:
    bus_root = root / "ops" / "agent_bus"
    states: dict[str, dict] = {}
    counts: dict[str, int] = {}
    for state in ["inbox", "running", "done", "failed"]:
        state_root = bus_root / state
        agents: dict[str, dict] = {}
        total = 0
        if state_root.exists():
            for agent_dir in sorted(path for path in state_root.iterdir() if path.is_dir()):
                files = sorted(agent_dir.glob("*.json"))
                total += len(files)
                agents[agent_dir.name] = {
                    "count": len(files),
                    "samples": [_openclaw_json_sample(path) for path in files[:sample_limit]],
                }
        states[state] = agents
        counts[f"bus_{state}"] = total
    return {
        "root": str(bus_root),
        "exists": bus_root.exists(),
        "states": states,
        "counts": counts,
    }


def _openclaw_approval_state(root: Path, *, sample_limit: int = 5) -> dict:
    approvals_root = root / "ops" / "approvals"
    states: dict[str, dict] = {}
    counts: dict[str, int] = {}
    for state in ["pending", "approved", "denied", "archived", "notify_pending", "notify_done", "notify_failed"]:
        state_root = approvals_root / state
        files = sorted(state_root.glob("*.json")) if state_root.exists() else []
        states[state] = {
            "count": len(files),
            "samples": [_openclaw_json_sample(path) for path in files[:sample_limit]],
        }
        counts[f"approval_{state}"] = len(files)
    return {
        "root": str(approvals_root),
        "exists": approvals_root.exists(),
        "states": states,
        "counts": counts,
    }


def openclaw_native_status() -> dict:
    root = openclaw_root()
    bus = _openclaw_agent_bus_state(root)
    approvals = _openclaw_approval_state(root)
    supervisor_path = root / "reports" / "openclaw-agent-supervisor-state.json"
    supervisor_payload = load_json_or_default(supervisor_path, {})
    supervisor = {
        "path": str(supervisor_path),
        "exists": supervisor_path.exists(),
        "ok": bool(supervisor_payload.get("ok")) if isinstance(supervisor_payload, dict) else False,
        "last_run_at": str(supervisor_payload.get("last_run_at") or supervisor_payload.get("at") or "") if isinstance(supervisor_payload, dict) else "",
        "agents": supervisor_payload.get("agents", {}) if isinstance(supervisor_payload, dict) else {},
        "summary": supervisor_payload.get("summary", {}) if isinstance(supervisor_payload, dict) else {},
    }
    counts = {}
    counts.update(bus["counts"])
    counts.update(approvals["counts"])
    return {
        "ok": True,
        "mode": "read_only",
        "openclaw_root": str(root),
        "agent_bus": {
            "root": bus["root"],
            "exists": bus["exists"],
            **bus["states"],
        },
        "approvals": {
            "root": approvals["root"],
            "exists": approvals["exists"],
            **approvals["states"],
        },
        "supervisor": supervisor,
        "counts": counts,
        "safety": {
            "mutates_openclaw": False,
            "dispatch_policy": "Read-only status only. Future dispatch must use ops_task_bus.py submit and owner approval for real sends.",
            "forbidden": [
                "do not modify OpenClaw callback fast path",
                "do not enable legacy Telegram polling watcher",
                "do not bypass agent_bus for native OpenClaw employees",
            ],
        },
        "note": "OpenClaw Native Adapter status is read-only and maps native bus/approvals/supervisor into Company Kernel observability.",
    }


def openclaw_native_status_summary(status: dict) -> dict:
    counts = dict(status.get("counts") or {})
    attention_keys = [
        "bus_inbox",
        "bus_running",
        "bus_failed",
        "approval_pending",
        "approval_notify_pending",
        "approval_notify_failed",
    ]
    attention_total = sum(int(counts.get(key) or 0) for key in attention_keys)
    recommended_actions: list[str] = []
    if attention_total:
        recommended_actions.append("drain_openclaw_pending_items")
    if int(counts.get("approval_pending") or 0):
        recommended_actions.append("review_owner_approvals")
    if int(counts.get("approval_notify_pending") or 0) or int(counts.get("approval_notify_failed") or 0):
        recommended_actions.append("check_async_approval_notifications")
    if not recommended_actions:
        recommended_actions.append("no_action_required")
    supervisor = status.get("supervisor") if isinstance(status.get("supervisor"), dict) else {}
    return {
        "ok": bool(status.get("ok")),
        "mode": status.get("mode", "read_only"),
        "health": "attention_required" if attention_total else "green",
        "openclaw_root": status.get("openclaw_root", ""),
        "mutates_openclaw": bool(((status.get("safety") or {}).get("mutates_openclaw"))),
        "counts": {key: int(counts.get(key) or 0) for key in attention_keys},
        "supervisor": {
            "ok": bool(supervisor.get("ok")),
            "last_run_at": str(supervisor.get("last_run_at") or ""),
        },
        "recommended_actions": recommended_actions,
        "note": "Owner-readable OpenClaw native queue summary. Full details are available without --summary.",
    }


def openclaw_native_dispatch_plan(
    *,
    source: str,
    target: str,
    task_type: str,
    priority: str,
    goal: str,
    next_command: str,
    expected_evidence: str,
    rollback: str,
    task_id: str = "",
) -> dict:
    source = source.strip()
    target = target.strip()
    task_type = task_type.strip()
    priority = (priority.strip() or "P2").upper()
    goal = goal.strip()
    next_command = next_command.strip()
    expected_evidence = expected_evidence.strip()
    rollback = rollback.strip()
    task_id = task_id.strip()
    missing = [
        name
        for name, value in {
            "source": source,
            "target": target,
            "type": task_type,
            "goal": goal,
            "next_command": next_command,
            "expected_evidence": expected_evidence,
            "rollback": rollback,
        }.items()
        if not value
    ]
    if missing:
        return {
            "ok": False,
            "dry_run": True,
            "mutates_openclaw": False,
            "error": "missing required OpenClaw agent_bus dispatch fields",
            "missing": missing,
        }
    payload = {
        "source_agent": source,
        "target_agent": target,
        "type": task_type,
        "priority": priority,
        "payload": {
            "goal": goal,
            "next_command": next_command,
            "expected_evidence": expected_evidence,
            "origin": "company_kernel_openclaw_native_adapter",
        },
        "rollback": rollback,
    }
    if task_id:
        payload["payload"]["kernel_task_id"] = task_id
    submit_command_preview = (
        "python3 ~/openclaw/scripts/ops_task_bus.py submit "
        f"--source-agent {source} --target-agent {target} --type {task_type} --priority {priority} "
        "--payload '<payload-json>' --rollback '<rollback-text>'"
    )
    return {
        "ok": True,
        "dry_run": True,
        "mutates_openclaw": False,
        "openclaw_root": str(openclaw_root()),
        "payload": payload,
        "dispatch_contract": {
            "command": "ops_task_bus.py submit",
            "bus": "agent_bus",
            "allowed_execution": "owner_approved_only",
            "default_mode": "dry_run",
        },
        "submit_command_preview": submit_command_preview,
        "safety": {
            "does_not_write_bus": True,
            "does_not_call_openclaw_deliver": True,
            "does_not_touch_callback_or_watcher": True,
            "requires_owner_approval_for_execute": True,
        },
        "note": "Dry-run dispatch plan only. It prepares the official OpenClaw agent_bus submit contract but does not write OpenClaw files.",
    }


def openclaw_native_dispatch_execute(
    *,
    source: str,
    target: str,
    task_type: str,
    priority: str,
    goal: str,
    next_command: str,
    expected_evidence: str,
    rollback: str,
    approval_id: str = "",
    task_id: str = "",
) -> dict:
    plan = openclaw_native_dispatch_plan(
        source=source,
        target=target,
        task_type=task_type,
        priority=priority,
        goal=goal,
        next_command=next_command,
        expected_evidence=expected_evidence,
        rollback=rollback,
        task_id=task_id,
    )
    if not plan.get("ok"):
        return plan
    approval_action = "openclaw_native_dispatch"
    conn = connect()
    try:
        gate = approved_gate(conn, approval_id, approval_action, source.strip(), target.strip())
    finally:
        conn.close()
    if not gate.get("allowed"):
        return {
            "ok": False,
            "dry_run": True,
            "mutates_openclaw": False,
            "error": "owner approval required",
            "approval_action": approval_action,
            "approval_id": approval_id,
            "gate": gate,
            "plan": plan,
        }

    payload = json.loads(json.dumps(plan["payload"], ensure_ascii=False))
    dispatch_id = f"kernel-openclaw-dispatch-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    created_at = datetime.now(timezone.utc).isoformat()
    payload["payload"]["kernel_approval_id"] = approval_id
    payload["payload"]["kernel_dispatch_id"] = dispatch_id
    payload["payload"]["kernel_created_at"] = created_at
    inbox = openclaw_root() / "ops" / "agent_bus" / "inbox" / target.strip()
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / f"{dispatch_id}.json"
    payload["created_at"] = created_at
    payload["kernel_origin"] = "company_kernel_openclaw_native_adapter"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "ok": True,
        "dry_run": False,
        "mutates_openclaw": True,
        "execution_mode": "ops_task_bus_file_write",
        "approval_id": approval_id,
        "file": str(path),
        "payload": payload,
        "plan": plan,
        "safety": {
            "used_owner_approval": True,
            "does_not_call_openclaw_deliver": True,
            "does_not_touch_callback_or_watcher": True,
        },
    }


def _openclaw_native_result_blocker(payload: dict) -> str:
    nested = payload.get("payload", {}) if isinstance(payload.get("payload", {}), dict) else {}
    for key in ("blocker", "error", "reason", "message"):
        value = str(nested.get(key) or payload.get(key) or "").strip()
        if value:
            return value
    return "OpenClaw native failed result imported"


def _openclaw_native_imported_event(conn: sqlite3.Connection, path: Path) -> sqlite3.Row | None:
    needle = str(path)
    rows = conn.execute(
        """
        SELECT *
        FROM company_events
        WHERE event_type = 'openclaw_native.result_imported'
          AND payload_json LIKE ?
        ORDER BY created_at DESC
        """,
        (f"%{needle}%",),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if payload.get("file") == needle:
            return row
    return None


def openclaw_native_import_results(*, limit: int = 50, agent: str = "") -> dict:
    root = openclaw_root()
    bus_root = root / "ops" / "agent_bus"
    candidates: list[tuple[str, str, Path]] = []
    for state in ("done", "failed"):
        state_root = bus_root / state
        if not state_root.exists():
            continue
        agent_dirs = [state_root / agent] if agent else sorted(path for path in state_root.iterdir() if path.is_dir())
        for agent_dir in agent_dirs:
            if not agent_dir.exists():
                continue
            for path in sorted(agent_dir.glob("*.json")):
                candidates.append((state, agent_dir.name, path))
    processed: list[dict] = []
    skipped: list[dict] = []
    counts = {"processed": 0, "completed": 0, "blocked": 0, "skipped": 0, "already_imported": 0}
    conn = connect()
    try:
        for state, result_agent, path in candidates[: max(0, int(limit))]:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                skipped.append({"file": str(path), "reason": f"invalid json: {exc}"})
                counts["skipped"] += 1
                continue
            if not isinstance(payload, dict):
                skipped.append({"file": str(path), "reason": "payload is not object"})
                counts["skipped"] += 1
                continue
            task_id = _openclaw_native_result_task_id(payload)
            if not task_id:
                skipped.append({"file": str(path), "reason": "missing kernel_task_id"})
                counts["skipped"] += 1
                continue
            task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not task:
                skipped.append({"file": str(path), "reason": "kernel task not found", "task_id": task_id})
                counts["skipped"] += 1
                continue
            imported_event = _openclaw_native_imported_event(conn, path)
            if imported_event:
                skipped.append({"file": str(path), "reason": "already_imported", "task_id": task_id, "event_id": imported_event["id"]})
                counts["skipped"] += 1
                counts["already_imported"] += 1
                continue
            employee_id = _openclaw_native_result_agent(payload, result_agent)
            trace_id = trace_id_for_task(conn, task_id)
            existing = conn.execute(
                "SELECT evidence_id FROM evidence WHERE task_id = ? AND metadata_json LIKE ?",
                (task_id, f"%{str(path)}%"),
            ).fetchone()
            if state == "done":
                summary = _openclaw_native_result_summary(payload, state)
                evidence_path = _openclaw_native_result_evidence(payload, path)
                if not existing:
                    evidence_id = f"evidence-openclaw-native-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
                    conn.execute(
                        """
                        INSERT INTO evidence(evidence_id, trace_id, task_id, attempt_id, employee_id, artifact_id, type, path_or_url, summary, checksum, is_final, metadata_json, created_at)
                        VALUES (?, ?, ?, '', ?, '', 'openclaw_native_result', ?, ?, '', 1, ?, ?)
                        """,
                        (
                            evidence_id,
                            trace_id,
                            task_id,
                            employee_id,
                            evidence_path,
                            summary,
                            json.dumps({"openclaw_result_file": str(path), "openclaw_state": state, "raw": payload}, ensure_ascii=False),
                            now(),
                        ),
                    )
                conn.execute(
                    "UPDATE tasks SET status = 'completed', claimed_by = CASE WHEN claimed_by = '' THEN ? ELSE claimed_by END, summary = ?, evidence_path = ?, blocker = '', updated_at = ? WHERE id = ?",
                    (employee_id, summary, evidence_path, now(), task_id),
                )
                event_type = "openclaw_native.result_imported"
                counts["completed"] += 1
            else:
                blocker = _openclaw_native_result_blocker(payload)
                conn.execute(
                    "UPDATE tasks SET status = 'blocked', claimed_by = CASE WHEN claimed_by = '' THEN ? ELSE claimed_by END, blocker = ?, updated_at = ? WHERE id = ?",
                    (employee_id, blocker, now(), task_id),
                )
                record_event(conn, "task.blocked", employee_id, task_id=task_id, trace_id=trace_id, payload={"blocker": blocker, "openclaw_result_file": str(path)})
                event_type = "openclaw_native.result_imported"
                counts["blocked"] += 1
            event = record_event(
                conn,
                event_type,
                employee_id,
                task_id=task_id,
                trace_id=trace_id,
                payload={"state": state, "file": str(path), "payload": payload, "read_only_openclaw": True},
            )
            audit(conn, employee_id, event_type, task_id, {"state": state, "file": str(path), "event_id": event["id"]})
            conn.commit()
            counts["processed"] += 1
            processed.append({"task_id": task_id, "state": state, "employee_id": employee_id, "file": str(path), "event_id": event["id"]})
    finally:
        conn.close()
    return {
        "ok": True,
        "openclaw_root": str(root),
        "read_only_openclaw": True,
        "mutates_openclaw": False,
        "counts": counts,
        "processed": processed,
        "skipped": skipped,
        "note": "Imports OpenClaw native done/failed result files into Company Kernel ledger without moving or editing OpenClaw files.",
    }


def openclaw_guard_health(conn: sqlite3.Connection | None = None) -> dict:
    root = openclaw_root()
    telegram_dir = root / "telegram"
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    watcher_plist = launch_agents / "ai.openclaw.ops-telegram-approval-watcher.plist"
    watcher_disabled = launch_agents / "ai.openclaw.ops-telegram-approval-watcher.plist.disabled"
    spools = {}
    if telegram_dir.exists():
        for spool_dir in sorted(telegram_dir.glob("ingress-spool-*")):
            account = spool_dir.name.removeprefix("ingress-spool-")
            spools[account] = count_spool_files(spool_dir)
    backlog_accounts = {
        account: spool
        for account, spool in spools.items()
        if int(spool.get("pending", 0)) > 0 or int(spool.get("stale_processing", 0)) > 0
    }
    issues = []
    if watcher_plist.exists():
        issues.append("external_telegram_approval_watcher_enabled")
    if backlog_accounts:
        issues.append("telegram_ingress_spool_backlog")
    return {
        "ok": not issues,
        "issues": issues,
        "openclaw_root": str(root),
        "telegram_dir": str(telegram_dir),
        "external_approval_watcher": {
            "installed_path": str(watcher_plist),
            "installed": watcher_plist.exists(),
            "disabled_path": str(watcher_disabled),
            "disabled_file_exists": watcher_disabled.exists(),
            "risk": "conflicts_with_openclaw_telegram_getupdates" if watcher_plist.exists() else "",
        },
        "telegram_spools": spools,
        "runtime_inventory": openclaw_runtime_inventory(conn),
        "backlog_accounts": backlog_accounts,
        "note": "Read-only guard. It detects conditions that can break OpenClaw native Telegram routing; it does not start, stop, or poll Telegram.",
    }


ATTENDANCE_STATUSES = ("online", "session_missing", "worker_stalled", "heartbeat_disabled", "no_reply")
ATTENDANCE_CLASSIFICATION_GUIDE = {
    "online": "exact reply probe matched, or reply probing disabled with non-empty runtime session and clear ingress spool",
    "session_missing": "runtime session store exists but has no active session entries",
    "worker_stalled": "OpenClaw Telegram ingress spool has pending or processing files, so the worker is not continuously draining",
    "heartbeat_disabled": "no runtime session store and no Company Kernel heartbeat file",
    "no_reply": "employee has heartbeat/session metadata but no supported or successful reply path",
}


def attendance_session_candidates(employee_id: str) -> list[Path]:
    names = [employee_id, employee_id.replace("_", "-"), employee_id.replace("-", "_")]
    if employee_id == "openclaw-main":
        names.append("main")
    if employee_id == "nestcar":
        names.append("car-rental")
    if employee_id in {"hermes", "default"}:
        names.extend(["default", "hermes"])
    result = []
    seen = set()
    for name in names:
        path = openclaw_root() / "agents" / name / "sessions" / "sessions.json"
        if path not in seen:
            seen.add(path)
            result.append(path)
    return result


def attendance_session_probe(employee_id: str) -> dict:
    candidates = attendance_session_candidates(employee_id)
    for path in candidates:
        if not path.exists():
            continue
        payload = load_json_or_default(path, {})
        count = len(payload) if isinstance(payload, (dict, list)) else 0
        return {"path": str(path), "exists": True, "bytes": path.stat().st_size, "session_count": count}
    return {"path": str(candidates[0]), "exists": False, "bytes": 0, "session_count": 0}


def attendance_spool_candidates(employee_id: str) -> list[Path]:
    names = [employee_id, employee_id.replace("-", "_"), employee_id.replace("_", "-")]
    if employee_id in {"main", "openclaw-main"}:
        names.append("default")
    if employee_id in {"hermes", "default"}:
        names.append("default")
    result = []
    seen = set()
    for name in names:
        path = openclaw_root() / "telegram" / f"ingress-spool-{name}"
        if path not in seen:
            seen.add(path)
            result.append(path)
    return result


def attendance_spool_probe(employee_id: str, stale_minutes: int) -> dict:
    cutoff = datetime.now(timezone.utc).astimezone() - timedelta(minutes=stale_minutes)
    probe = {"paths": [], "pending": 0, "processing": 0, "stale_processing": 0, "files": []}
    for spool in attendance_spool_candidates(employee_id):
        if not spool.exists():
            continue
        probe["paths"].append(str(spool))
        for path in sorted(spool.iterdir()):
            if not path.is_file():
                continue
            if path.name.endswith(".json"):
                probe["pending"] += 1
                probe["files"].append(path.name)
            elif path.name.endswith(".json.processing"):
                probe["processing"] += 1
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).astimezone()
                age_seconds = max(0, int((datetime.now(timezone.utc).astimezone() - mtime).total_seconds()))
                if mtime < cutoff:
                    probe["stale_processing"] += 1
                probe["files"].append(f"{path.name}:age_seconds={age_seconds}")
    return probe


def attendance_agent_runtime_id(employee_id: str, runtime: str) -> str:
    if employee_id == "openclaw-main":
        return "main"
    if employee_id == "hermes" and runtime == "hermes":
        return "default"
    return employee_id


def attendance_reply_probe(employee_id: str, runtime: str, timeout: int) -> dict:
    expected = f"{employee_id} 在岗"
    if runtime in {"openclaw", "hermes"}:
        agent_runtime_id = attendance_agent_runtime_id(employee_id, runtime)
        cmd = ["openclaw", "agent", "--agent", agent_runtime_id, "--message", f"只回复 {expected}", "--timeout", str(timeout), "--json"]
        try:
            cp = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True, timeout=timeout + 10)
        except Exception as exc:
            return {"enabled": True, "ok": False, "agent_runtime_id": agent_runtime_id, "expected": expected, "reply": "", "exit_code": 127, "reason": str(exc)}
        reply = parse_openclaw_agent_reply(cp.stdout)
        return {
            "enabled": True,
            "ok": cp.returncode == 0 and reply == expected,
            "agent_runtime_id": agent_runtime_id,
            "expected": expected,
            "reply": reply,
            "exit_code": cp.returncode,
            "reason": "matched" if reply == expected else "reply_mismatch_or_empty",
            "stderr": cp.stderr[-2000:],
        }
    if runtime == "codex":
        command = ROOT / "bin" / "company-codex-adapter"
        try:
            cp = subprocess.run([str(command), "--agent", employee_id, "--attendance-probe"], cwd=str(ROOT), text=True, capture_output=True, timeout=timeout + 10)
        except Exception as exc:
            return {"enabled": True, "ok": False, "adapter": str(command), "expected": expected, "reply": "", "exit_code": 127, "reason": str(exc)}
        payload = parse_json_output(cp.stdout)
        ok = cp.returncode == 0 and bool(payload.get("ok"))
        return {
            "enabled": True,
            "ok": ok,
            "adapter": str(command),
            "expected": expected,
            "reply": expected if ok else "",
            "exit_code": cp.returncode,
            "reason": "adapter_heartbeat_matched" if ok else str(payload.get("error") or "adapter_failed"),
            "processed": payload.get("processed"),
            "stdout": payload,
            "stderr": cp.stderr[-2000:],
        }
    if runtime == "antigravity":
        command = ROOT / "bin" / "company-antigravity-adapter"
        try:
            cp = subprocess.run([str(command), "--agent", employee_id, "--attendance-probe", "--timeout", str(timeout)], cwd=str(ROOT), text=True, capture_output=True, timeout=timeout + 10)
        except Exception as exc:
            return {"enabled": True, "ok": False, "adapter": str(command), "expected": expected, "reply": "", "exit_code": 127, "reason": str(exc)}
        payload = parse_json_output(cp.stdout)
        reply = str(payload.get("reply") or "").strip()
        ok = cp.returncode == 0 and bool(payload.get("ok")) and reply == expected
        return {
            "enabled": True,
            "ok": ok,
            "adapter": str(command),
            "expected": expected,
            "reply": reply,
            "exit_code": cp.returncode,
            "reason": "adapter_heartbeat_matched" if ok else str(payload.get("error") or payload.get("blocker") or payload.get("reason") or "adapter_failed"),
            "processed": payload.get("processed"),
            "stdout": payload,
            "stderr": cp.stderr[-2000:],
        }
    return {"enabled": False, "ok": False, "reason": f"unsupported_runtime:{runtime}", "reply": "", "expected": expected}


def attendance_classify_employee(employee: dict, stale_minutes: int, *, probe_replies: bool = True, reply_timeout: int = 120) -> dict:
    employee_id = employee["id"]
    runtime = employee.get("runtime", "")
    session = attendance_session_probe(employee_id)
    spool = attendance_spool_probe(employee_id, stale_minutes)
    heartbeat = load_json_or_default(employee_paths(employee_id)["heartbeat"], {})
    reply = ""
    reply_probe = {"enabled": False, "ok": False, "reply": "", "reason": "disabled"}
    if int(spool.get("pending", 0)) > 0 or int(spool.get("processing", 0)) > 0:
        status = "worker_stalled"
        reason = "telegram_ingress_spool_not_drained"
    elif probe_replies:
        reply_probe = attendance_reply_probe(employee_id, runtime, reply_timeout)
        if reply_probe.get("ok"):
            status = "online"
            reason = "agent_reply_matched"
            reply = str(reply_probe.get("reply") or "")
        elif not session["exists"] and not heartbeat:
            status = "heartbeat_disabled"
            reason = "no_session_store_or_employee_heartbeat"
        elif session["exists"] and int(session.get("session_count", 0)) <= 0:
            status = "session_missing"
            reason = "session_store_empty"
        else:
            status = "no_reply"
            reason = str(reply_probe.get("reason") or "reply_probe_failed")
            reply = str(reply_probe.get("reply") or "")
    elif not session["exists"] and not heartbeat:
        status = "heartbeat_disabled"
        reason = "no_session_store_or_employee_heartbeat"
    elif session["exists"] and int(session.get("session_count", 0)) <= 0:
        status = "session_missing"
        reason = "session_store_empty"
    elif not session["exists"]:
        status = "no_reply"
        reason = "no_runtime_session_evidence"
    else:
        status = "online"
        reason = "session_store_has_active_entries_and_spool_clear"
        reply = f"{employee_id} 报到"
    return {
        "agent": employee_id,
        "name": employee.get("name", ""),
        "runtime": runtime,
        "employee_status": employee.get("status", ""),
        "status": status,
        "reply": reply,
        "reason": reason,
        "reply_probe": reply_probe,
        "session": session,
        "spool": spool,
        "heartbeat_file": str(employee_paths(employee_id)["heartbeat"]),
        "heartbeat_file_exists": bool(heartbeat),
    }


def cmd_attendance_sweep(args: argparse.Namespace) -> int:
    conn = connect_readonly()
    requested = set(parse_csv(args.agents))
    if requested:
        placeholders = ",".join("?" for _ in requested)
        query = f"SELECT * FROM employees WHERE id IN ({placeholders}) ORDER BY id"
        employees = [dict(row) for row in conn.execute(query, tuple(sorted(requested))).fetchall()]
        known = {row["id"] for row in employees}
        for missing in sorted(requested - known):
            employees.append({"id": missing, "name": missing, "runtime": "unknown", "status": "missing"})
    else:
        where = "" if args.include_candidates else "WHERE status = 'active'"
        employees = [dict(row) for row in conn.execute(f"SELECT * FROM employees {where} ORDER BY id").fetchall()]
    rows_out = [attendance_classify_employee(emp, args.stale_minutes, probe_replies=args.probe_replies, reply_timeout=args.reply_timeout) for emp in employees]
    counts = {status: 0 for status in ATTENDANCE_STATUSES}
    for row in rows_out:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    report = {
        "ok": bool(rows_out) and all(row["status"] == "online" for row in rows_out),
        "sweep_id": args.sweep_id or f"attendance-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
        "generated_at": now(),
        "source_agent": args.source,
        "counts": counts,
        "employees": rows_out,
        "evidence_rule": "online requires clear ingress spool plus exact agent reply when reply probing is enabled; employee_directory.status is reported but never sufficient",
        "classification_guide": ATTENDANCE_CLASSIFICATION_GUIDE,
    }
    report_dir = STATE_DIR / "attendance"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{report['sweep_id']}.json"
    latest_path = report_dir / "latest.json"
    report["evidence"] = {"json": str(report_path), "latest": str(latest_path)}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    latest_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    emit(report)
    return 0 if report["ok"] else 1


def load_latest_attendance() -> dict:
    return load_json_or_default(STATE_DIR / "attendance" / "latest.json", {})


def attendance_row_map(report: dict) -> dict[str, dict]:
    result = {}
    employees = report.get("employees", [])
    if not isinstance(employees, list):
        return result
    for item in employees:
        if isinstance(item, dict) and str(item.get("agent") or "").strip():
            result[str(item["agent"])] = item
    return result


def employee_has_managed_runtime_evidence(conn: sqlite3.Connection, employee_id: str) -> bool:
    row = conn.execute(
        """
        SELECT a.attempt_id, a.task_id, a.status, t.status AS task_status, t.evidence_path
        FROM execution_attempts a
        JOIN tasks t ON t.id = a.task_id
        WHERE a.employee_id = ?
          AND a.status = 'success'
          AND t.status = 'completed'
          AND t.evidence_path != ''
        ORDER BY a.started_at DESC
        LIMIT 1
        """,
        (employee_id,),
    ).fetchone()
    if not row:
        return False
    evidence_path = str(row["evidence_path"] or "")
    if not evidence_path or not Path(evidence_path).exists():
        return False
    evidence_row = conn.execute(
        """
        SELECT 1 FROM evidence
        WHERE task_id = ?
          AND employee_id = ?
          AND is_final = 1
        LIMIT 1
        """,
        (row["task_id"], employee_id),
    ).fetchone()
    return bool(evidence_row)


def employee_has_adapter_task_evidence(conn: sqlite3.Connection, employee_id: str) -> bool:
    row = conn.execute(
        """
        SELECT id, title, evidence_path
        FROM tasks
        WHERE target_agent = ?
          AND status = 'completed'
          AND evidence_path != ''
          AND title LIKE 'Runtime adapter dry-run check:%'
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (employee_id,),
    ).fetchone()
    if not row:
        return False
    evidence_path = str(row["evidence_path"] or "")
    return bool(evidence_path and Path(evidence_path).exists())


def employee_has_runtime_evidence(employee_id: str, conn: sqlite3.Connection | None = None) -> bool:
    latest = verified_direct_evidence_dir(employee_id) / "latest-runtime.json"
    payload = load_json_or_default(latest, {})
    if bool(payload.get("ok") and payload.get("activation_allowed")):
        verification = payload.get("verification") if isinstance(payload.get("verification"), dict) else {}
        response = verification.get("response") if isinstance(verification.get("response"), dict) else {}
        if "reply" in response:
            passed, _reason, _fields = runtime_reply_passed(str(response.get("reply") or ""))
            if passed:
                return True
        else:
            return True
    if conn is not None:
        return employee_has_managed_runtime_evidence(conn, employee_id) or employee_has_adapter_task_evidence(conn, employee_id)
    return False


def latest_attempt_for_employee(conn: sqlite3.Connection, employee_id: str) -> dict:
    row = conn.execute(
        """
        SELECT *
        FROM execution_attempts
        WHERE employee_id = ?
        ORDER BY started_at DESC
        LIMIT 1
        """,
        (employee_id,),
    ).fetchone()
    return dict(row) if row else {}


def employee_has_fresh_heartbeat(conn: sqlite3.Connection, employee_id: str, *, stale_minutes: int = 15) -> bool:
    row = conn.execute("SELECT last_seen_at FROM heartbeats WHERE agent_id = ?", (employee_id,)).fetchone()
    if not row:
        return False
    return parse_time(row["last_seen_at"]) >= datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)


def classify_agent_matrix_row(conn: sqlite3.Connection, employee: dict, attendance: dict) -> dict:
    employee_id = employee["id"]
    runtime = str(employee.get("runtime") or "")
    attendance_status = str(attendance.get("status") or "missing")
    runtime_ok = employee_has_runtime_evidence(employee_id, conn)
    direct_ok = employee_has_verified_direct_evidence(employee_id)
    latest_attempt = latest_attempt_for_employee(conn, employee_id)
    has_task_attempt = bool(latest_attempt)
    has_live_heartbeat = employee_has_fresh_heartbeat(conn, employee_id)
    has_live_attendance = attendance_status == "online"
    has_live_or_task_evidence = has_live_attendance or has_task_attempt
    employee_status = str(employee.get("status") or "")
    if employee_status == "missing":
        level = "no_reply"
        reason = "employee_not_registered"
    elif employee_status == "candidate":
        level = "candidate_only"
        reason = "candidate_requires_structured_runtime_evidence_before_activation"
    elif runtime == "skill" and employee_status == "active" and runtime_ok:
        level = "active_ready"
        reason = "skill_runtime_evidence_no_direct_chat_required"
    elif runtime != "openclaw" and employee_status == "active" and runtime_ok and has_live_or_task_evidence:
        level = "active_ready"
        reason = "adapter_runtime_evidence_no_openclaw_session_required"
    elif runtime != "openclaw" and employee_status == "active" and runtime_ok:
        level = "active_limited"
        reason = "runtime_evidence_without_live_task_or_direct_attendance"
    elif runtime == "openclaw" and employee_status == "active" and (runtime_ok or direct_ok):
        level = "active_ready"
        reason = "openclaw_direct_or_runtime_evidence_verified"
    elif attendance_status != "online":
        level = "no_reply"
        reason = f"attendance_{attendance_status}"
    elif latest_attempt.get("status") in {"failed", "stale"}:
        level = "unsafe"
        reason = f"latest_attempt_{latest_attempt['status']}"
    elif employee_status == "active" and runtime_ok:
        level = "active_ready"
        reason = "online_active_with_runtime_evidence"
    elif employee_status == "active":
        level = "online_only"
        reason = "online_but_runtime_task_evidence_missing"
    else:
        level = "task_unsupported"
        reason = f"employee_status_{employee_status}"
    return {
        "agent": employee_id,
        "name": employee.get("name", employee_id),
        "runtime": employee.get("runtime", ""),
        "employee_status": employee_status,
        "level": level,
        "reason": reason,
        "checks": {
            "attendance": attendance_status,
            "heartbeat": "fresh" if has_live_heartbeat else "missing_or_stale",
            "direct": "verified" if direct_ok else "not_verified",
            "runtime": "verified" if runtime_ok and (runtime == "skill" or runtime == "openclaw" or has_live_or_task_evidence) else ("verified_limited" if runtime_ok else "missing"),
            "task": "supported" if employee_status == "active" else "not_active",
            "progress": "observable" if latest_attempt else "not_checked",
            "evidence": "runtime_evidence" if runtime_ok else "missing",
            "stale": latest_attempt.get("status", "not_checked"),
        },
        "latest_attempt": latest_attempt,
        "attendance": attendance,
    }


def cmd_agent_matrix(args: argparse.Namespace) -> int:
    conn = connect()
    requested = parse_csv(args.agents)
    if requested:
        resolved_requested: list[tuple[str, str]] = []
        for employee_id in requested:
            resolved_requested.append((employee_id, resolve_employee_alias(employee_id)))
        canonical_ids = list(dict.fromkeys(resolved for _raw, resolved in resolved_requested))
        placeholders = ",".join("?" for _ in canonical_ids)
        known = {
            row["id"]: dict(row)
            for row in conn.execute(f"SELECT * FROM employees WHERE id IN ({placeholders}) ORDER BY id", tuple(canonical_ids)).fetchall()
        }
        employee_specs = []
        for requested_id, canonical_id in resolved_requested:
            employee = known.get(canonical_id, {"id": canonical_id, "name": requested_id, "runtime": "unknown", "status": "missing"})
            employee_specs.append((requested_id, canonical_id, employee))
    else:
        employee_specs = [(employee["id"], employee["id"], employee) for employee in rows(conn, "SELECT * FROM employees WHERE status IN ('active', 'candidate') ORDER BY id")]
    attendance_report = load_latest_attendance()
    attendance_by_agent = attendance_row_map(attendance_report)
    matrix_rows = []
    for requested_id, canonical_id, employee in employee_specs:
        attendance = attendance_by_agent.get(canonical_id, attendance_by_agent.get(requested_id, {}))
        row = classify_agent_matrix_row(conn, employee, attendance)
        if requested_id != canonical_id:
            row["requested_agent"] = requested_id
            row["canonical_agent"] = canonical_id
            row["alias_of"] = canonical_id
        matrix_rows.append(row)
    counts = {}
    for row in matrix_rows:
        counts[row["level"]] = counts.get(row["level"], 0) + 1
    report = {
        "ok": True,
        "generated_at": now(),
        "employees": matrix_rows,
        "counts": counts,
        "attendance_evidence": str(STATE_DIR / "attendance" / "latest.json"),
        "rule": "online attendance is not enough for active_ready; active_ready requires active employee plus structured runtime evidence",
    }
    emit(report)
    return 0


def skill_manifest_summary(manifest_path: Path) -> dict:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "id": manifest_path.parent.name,
            "name": manifest_path.parent.name,
            "manifest_path": str(manifest_path),
            "error": str(exc),
        }
    runtime = manifest.get("runtime", {}) if isinstance(manifest.get("runtime", {}), dict) else {}
    permissions = manifest.get("permissions", {}) if isinstance(manifest.get("permissions", {}), dict) else {}
    pricing = manifest.get("pricing", {}) if isinstance(manifest.get("pricing", {}), dict) else {}
    acceptance = manifest.get("acceptance", {}) if isinstance(manifest.get("acceptance", {}), dict) else {}
    return {
        "ok": True,
        "id": str(manifest.get("id") or manifest_path.parent.name),
        "name": str(manifest.get("name") or manifest_path.parent.name),
        "version": str(manifest.get("version") or ""),
        "description": str(manifest.get("description") or ""),
        "manifest_path": str(manifest_path),
        "runtime_type": str(runtime.get("type") or ""),
        "workspace_permission": str(permissions.get("workspace") or ""),
        "network": bool(permissions.get("network", False)),
        "secrets_count": len(permissions.get("secrets", []) if isinstance(permissions.get("secrets", []), list) else []),
        "pricing_unit": str(pricing.get("unit") or ""),
        "pricing_amount": pricing.get("amount", ""),
        "pricing_currency": str(pricing.get("currency") or ""),
        "final_artifact": str(acceptance.get("final_artifact") or ""),
        "evidence_required": bool(acceptance.get("evidence_required", False)),
    }


def skill_registry() -> dict:
    skills = []
    if SKILL_PACKAGES_DIR.exists():
        for manifest_path in sorted(SKILL_PACKAGES_DIR.glob("*/skill.json")):
            skills.append(skill_manifest_summary(manifest_path))
    return {
        "ok": True,
        "root": str(SKILL_PACKAGES_DIR),
        "count": len(skills),
        "skills": skills,
        "rule": "read-only registry; skill execution still requires Kernel task, authorized task workspace, artifact registration, and evidence promotion",
    }


def cmd_skill_list(args: argparse.Namespace) -> int:
    emit(skill_registry())
    return 0


def write_employee_capabilities(employee_id: str, profile: dict, *, dry_run: bool) -> str:
    path = employee_paths(employee_id)["capabilities"]
    if dry_run:
        return str(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(json.dumps(default_capabilities(profile), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


def write_employee_files(employee_id: str, profile: dict, *, dry_run: bool) -> dict:
    paths = employee_paths(employee_id)
    result = {k: str(v) for k, v in paths.items()}
    if dry_run:
        return result
    for key in ("inbox", "outbox", "reports"):
        paths[key].mkdir(parents=True, exist_ok=True)
    paths["profile"].write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_employee_capabilities(employee_id, profile, dry_run=False)
    if not paths["rules"].exists():
        from .employee_comms import default_onboarding_rules
        paths["rules"].write_text(
            default_onboarding_rules(employee_id, profile.get("role", ""), profile.get("runtime", "")) + "\n",
            encoding="utf-8",
        )
    if not paths["permissions"].exists():
        paths["permissions"].write_text(
            json.dumps(
                {
                    "can_submit_tasks": True,
                    "can_claim_tasks": True,
                    "can_modify_kernel": False,
                    "requires_approval_for": ["payment", "compensation", "salary", "penalty", "external_send"],
                    "updated_at": now(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    if not paths["heartbeat"].exists():
        paths["heartbeat"].write_text(json.dumps({"agent_id": employee_id, "status": "created", "updated_at": now()}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def cmd_employee_create(args: argparse.Namespace) -> int:
    conn = connect()
    try:
        require_runtime(conn, args.runtime)
    except SystemExit:
        conn.close()
        raise
    profile = {
        "id": args.id,
        "name": args.name,
        "role": args.role,
        "runtime": args.runtime,
        "workspace": args.workspace,
        "created_at": now(),
    }
    files = write_employee_files(args.id, profile, dry_run=args.dry_run)
    if args.dry_run:
        conn.close()
        emit({"ok": True, "dry_run": True, "employee": profile, "files": files})
        return 0
    ensure_runtime(conn, args.runtime)
    ts = now()
    conn.execute(
        """
        INSERT INTO employees(id, name, role, runtime, workspace, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'candidate', ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          name = excluded.name,
          role = excluded.role,
          runtime = excluded.runtime,
          workspace = excluded.workspace,
          status = CASE WHEN employees.status = 'active' THEN employees.status ELSE 'candidate' END,
          updated_at = excluded.updated_at
        """,
        (args.id, args.name, args.role, args.runtime, args.workspace, ts, ts),
    )
    communication = sync_employee_name_alias(args.id, args.name, dry_run=False)
    conn.commit()
    audit(conn, "companyctl", "employee.create", args.id, {**profile, "communication": communication})
    emit({"ok": True, "employee": profile, "files": files, "communication": communication})
    return 0


def clamp_audit_limit(limit: int | str | None) -> int:
    try:
        value = int(limit or 50)
    except (TypeError, ValueError):
        value = 50
    return max(1, min(value, 200))


def audit_evidence_records(conn: sqlite3.Connection, *, task_id: str = "", employee_id: str = "", limit: int | str | None = 50) -> list[dict]:
    sql = """
        SELECT evidence_id, trace_id, task_id, attempt_id, employee_id, artifact_id,
               type, path_or_url, summary, checksum, is_final, metadata_json, created_at
        FROM evidence
    """
    params: list[object] = []
    conditions = []
    if task_id:
        conditions.append("task_id = ?")
        params.append(task_id)
    if employee_id:
        conditions.append("employee_id = ?")
        params.append(employee_id)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(clamp_audit_limit(limit))
    evidence = rows(conn, sql, tuple(params))
    for item in evidence:
        raw_path = item.pop("path_or_url", "")
        item["display"] = sanitize_evidence_path_for_display(raw_path)
        item["is_final"] = bool(item.get("is_final"))
        metadata = parse_json_arg(item.get("metadata_json", "{}") or "{}", {})
        item["metadata"] = metadata if isinstance(metadata, dict) else {}
        item["acceptance_decision"] = evidence_acceptance_decision(item)
    return evidence


def evidence_acceptance_decision(item: dict) -> dict:
    metadata = item.get("metadata", {}) if isinstance(item.get("metadata", {}), dict) else parse_json_arg(str(item.get("metadata_json", "{}") or "{}"), {})
    if not isinstance(metadata, dict):
        metadata = {}
    decision = metadata.get("acceptance", {}) if isinstance(metadata.get("acceptance", {}), dict) else {}
    status = str(decision.get("status") or "pending")
    return {
        "status": status,
        "by": str(decision.get("by") or ""),
        "summary": sanitize_log_text(str(decision.get("summary") or "")),
        "reason": sanitize_log_text(str(decision.get("reason") or "")),
        "decided_at": str(decision.get("decided_at") or ""),
        "event_id": str(decision.get("event_id") or ""),
    }


def evidence_acceptance_context(item: dict, display: dict) -> dict:
    task_id = str(item.get("task_id") or "")
    attempt_id = str(item.get("attempt_id") or "")
    checksum = str(item.get("checksum") or "")
    preview_allowed = bool(display.get("allowed"))
    is_final = bool(item.get("is_final"))
    task_bound = bool(task_id)
    attempt_bound = bool(attempt_id)
    can_accept = preview_allowed and task_bound and attempt_bound and is_final
    decision = evidence_acceptance_decision(item)
    if not preview_allowed:
        state = "blocked_by_preview_policy"
    elif not task_bound or not attempt_bound:
        state = "missing_binding_context"
    elif not is_final:
        state = "unacceptable_draft"
    else:
        state = "reviewable_final_evidence"
    return {
        "evidence_id": str(item.get("evidence_id") or ""),
        "trace_id": str(item.get("trace_id") or ""),
        "task_id": task_id,
        "attempt_id": attempt_id,
        "employee_id": str(item.get("employee_id") or ""),
        "artifact_id": str(item.get("artifact_id") or ""),
        "is_final": is_final,
        "preview_allowed": preview_allowed,
        "task_bound": task_bound,
        "attempt_bound": attempt_bound,
        "checksum_status": "recorded" if checksum else "missing",
        "can_accept": can_accept,
        "state": state,
        "decision": decision,
        "accepted": decision.get("status") == "accepted",
        "rejected": decision.get("status") == "rejected",
        "summary": sanitize_log_text(str(item.get("summary") or "")),
    }


def decide_evidence_internal(conn: sqlite3.Connection, *, evidence_id: str, by: str, status: str, summary: str = "", reason: str = "") -> dict:
    actor = resolve_employee_alias(by)
    require_employee(conn, actor)
    if status not in {"accepted", "rejected"}:
        raise SystemExit("status must be accepted or rejected")
    evidence = row_by_id(conn, "evidence", "evidence_id", evidence_id)
    display = sanitize_evidence_path_for_display(str(evidence.get("path_or_url") or ""))
    acceptance = evidence_acceptance_context(evidence, display)
    if status == "accepted" and not acceptance.get("can_accept"):
        return {"ok": False, "error": "evidence is not acceptable", "acceptance": acceptance, "evidence_id": evidence_id}
    metadata = parse_json_arg(evidence.get("metadata_json", "{}") or "{}", {})
    if not isinstance(metadata, dict):
        metadata = {}
    event_type = "evidence.accepted" if status == "accepted" else "evidence.rejected"
    ts = now()
    decision = {
        "status": status,
        "by": actor,
        "summary": summary,
        "reason": reason,
        "decided_at": ts,
    }
    metadata["acceptance"] = decision
    conn.execute("UPDATE evidence SET metadata_json = ? WHERE evidence_id = ?", (json.dumps(metadata, ensure_ascii=False), evidence_id))
    conn.commit()
    event = record_event(
        conn,
        event_type,
        actor,
        task_id=str(evidence.get("task_id") or ""),
        trace_id=str(evidence.get("trace_id") or ""),
        payload={"evidence_id": evidence_id, "attempt_id": evidence.get("attempt_id", ""), "summary": summary, "reason": reason},
    )
    decision["event_id"] = event["id"]
    metadata["acceptance"] = decision
    conn.execute("UPDATE evidence SET metadata_json = ? WHERE evidence_id = ?", (json.dumps(metadata, ensure_ascii=False), evidence_id))
    audit(conn, actor, "evidence.accept" if status == "accepted" else "evidence.reject", evidence_id, {"task_id": evidence.get("task_id", ""), "attempt_id": evidence.get("attempt_id", ""), "event_id": event["id"], "summary": summary, "reason": reason})
    link_human_review_to_verifier(conn, str(evidence.get("task_id") or ""), status, actor)
    audit_row = conn.execute("SELECT id, actor, action, target, detail_json, created_at FROM audit_logs WHERE target = ? ORDER BY id DESC LIMIT 1", (evidence_id,)).fetchone()
    updated = row_by_id(conn, "evidence", "evidence_id", evidence_id)
    raw_path = updated.pop("path_or_url", "")
    updated["display"] = sanitize_evidence_path_for_display(raw_path)
    updated["is_final"] = bool(updated.get("is_final"))
    updated["metadata"] = metadata
    updated["acceptance_decision"] = evidence_acceptance_decision(updated)
    audit_payload = dict(audit_row) if audit_row else {}
    if audit_payload:
        audit_payload["detail"] = parse_json_arg(audit_payload.pop("detail_json", "{}") or "{}", {})
    return {"ok": True, "evidence": updated, "event": event, "audit": audit_payload}


def safe_evidence_content(conn: sqlite3.Connection, evidence_id: str, *, max_bytes: int = 65536) -> dict:
    safe_id = str(evidence_id or "").strip()
    if not safe_id or "/" in safe_id:
        display = sanitize_evidence_path_for_display("")
        return {"ok": False, "error": "invalid evidence id", "evidence_id": safe_id, "display": display, "acceptance": evidence_acceptance_context({"evidence_id": safe_id}, display), "content": {"text": ""}}
    record = conn.execute(
        """
        SELECT evidence_id, trace_id, task_id, attempt_id, employee_id, artifact_id,
               type, path_or_url, summary, checksum, is_final, metadata_json, created_at
        FROM evidence
        WHERE evidence_id = ?
        """,
        (safe_id,),
    ).fetchone()
    if not record:
        display = sanitize_evidence_path_for_display("")
        return {"ok": False, "error": "evidence not found", "evidence_id": safe_id, "display": display, "acceptance": evidence_acceptance_context({"evidence_id": safe_id}, display), "content": {"text": ""}}
    item = dict(record)
    raw_path = item.pop("path_or_url", "")
    metadata = parse_json_arg(item.get("metadata_json", "{}") or "{}", {})
    item["metadata"] = metadata if isinstance(metadata, dict) else {}
    display = sanitize_evidence_path_for_display(raw_path)
    payload = {
        "ok": bool(display.get("allowed")),
        "evidence": item,
        "display": display,
        "acceptance": evidence_acceptance_context(item, display),
        "content": {
            "text": "",
            "truncated": False,
            "bytes": 0,
            "mode": "blocked" if not display.get("allowed") else "text",
            "policy": "read-only sanitized evidence preview; no downloads and no absolute paths",
        },
    }
    if not display.get("allowed"):
        payload["error"] = display.get("reason") or "evidence path blocked by display policy"
        return payload
    candidate = Path(str(raw_path or "")).expanduser()
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    try:
        path = candidate.resolve()
    except OSError:
        payload["ok"] = False
        payload["error"] = "sanitized evidence file missing"
        payload["content"]["mode"] = "missing"
        return payload
    size = path.stat().st_size
    payload["content"]["bytes"] = size
    if size > max_bytes:
        payload["content"]["truncated"] = True
    data = path.read_bytes()[:max_bytes]
    try:
        payload["content"]["text"] = data.decode("utf-8")
    except UnicodeDecodeError:
        payload["content"]["mode"] = "binary"
        payload["content"]["text"] = ""
    return payload


def audit_artifact_records(conn: sqlite3.Connection, *, task_id: str = "", limit: int | str | None = 50) -> list[dict]:
    sql = """
        SELECT artifact_id, trace_id, task_id, parent_task_id, employee_id, artifact_type,
               name, path, mime_type, stage, version, status, is_input, is_output, is_final,
               summary, checksum, metadata_json, created_at, updated_at
        FROM artifacts
    """
    params: list[object] = []
    if task_id:
        sql += " WHERE task_id = ?"
        params.append(task_id)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(clamp_audit_limit(limit))
    artifacts = rows(conn, sql, tuple(params))
    for item in artifacts:
        raw_path = item.pop("path", "")
        item["display"] = sanitize_evidence_path_for_display(raw_path)
        item["is_input"] = bool(item.get("is_input"))
        item["is_output"] = bool(item.get("is_output"))
        item["is_final"] = bool(item.get("is_final"))
    return artifacts


def audit_handoff_records(conn: sqlite3.Connection, *, task_id: str = "", limit: int | str | None = 50) -> list[dict]:
    sql = """
        SELECT handoff_id, trace_id, from_task_id, to_task_id, from_employee_id, to_employee_id,
               summary, artifacts_json, known_issues, next_steps, required_actions,
               acceptance_notes, status, created_at, updated_at
        FROM handoffs
    """
    params: list[object] = []
    if task_id:
        sql += " WHERE from_task_id = ? OR to_task_id = ?"
        params.extend([task_id, task_id])
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(clamp_audit_limit(limit))
    handoffs = rows(conn, sql, tuple(params))
    for item in handoffs:
        item["artifacts"] = parse_json_arg(item.pop("artifacts_json", "") or "[]", [])
    return handoffs


def mermaid_node_id(raw: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", raw)


def trace_file_flow_graph(conn: sqlite3.Connection, trace_id: str) -> dict:
    safe_trace_id = str(trace_id or "").strip()
    artifact_rows = rows(
        conn,
        """
        SELECT artifact_id, task_id, employee_id, name, path, stage, status, version, is_final, summary, created_at
        FROM artifacts
        WHERE trace_id = ?
        ORDER BY created_at ASC, artifact_id ASC
        """,
        (safe_trace_id,),
    )
    handoff_rows = audit_handoff_records(conn, limit=200)
    handoff_rows = [item for item in handoff_rows if item.get("trace_id") == safe_trace_id]
    evidence_rows = rows(
        conn,
        """
        SELECT evidence_id, task_id, attempt_id, employee_id, artifact_id, path_or_url, summary, is_final, created_at
        FROM evidence
        WHERE trace_id = ?
        ORDER BY created_at ASC, evidence_id ASC
        """,
        (safe_trace_id,),
    )
    nodes: list[dict] = []
    edges: list[dict] = []
    task_ids = {str(row.get("task_id") or "") for row in artifact_rows}
    task_ids.update(str(row.get("task_id") or "") for row in evidence_rows)
    for handoff in handoff_rows:
        task_ids.add(str(handoff.get("from_task_id") or ""))
        task_ids.add(str(handoff.get("to_task_id") or ""))
    for metadata_row in rows(conn, "SELECT task_id, metadata_json FROM task_metadata ORDER BY task_id ASC"):
        metadata = parse_json_arg(metadata_row.get("metadata_json", "") or "{}", {})
        if metadata.get("trace_id") == safe_trace_id:
            task_ids.add(str(metadata_row.get("task_id") or ""))
    task_ids = {task_id for task_id in task_ids if task_id}
    task_rows = []
    if task_ids:
        placeholders = ",".join("?" for _ in task_ids)
        task_rows = rows(
            conn,
            f"""
            SELECT id, source_agent, target_agent, title, status, claimed_by, created_at, updated_at
            FROM tasks
            WHERE id IN ({placeholders})
            ORDER BY created_at ASC, id ASC
            """,
            tuple(sorted(task_ids)),
        )

    def add_node(node_id: str, kind: str, label: str, **extra: object) -> None:
        if any(node["id"] == node_id for node in nodes):
            return
        nodes.append({"id": node_id, "kind": kind, "label": label, **extra})

    for task in task_rows:
        add_node(
            f"task:{task['id']}",
            "task",
            str(task.get("title") or task["id"]),
            task_id=task["id"],
            status=task.get("status", ""),
            target_agent=task.get("target_agent", ""),
        )
    artifact_by_id = {}
    for artifact in artifact_rows:
        artifact_id = artifact["artifact_id"]
        artifact_by_id[artifact_id] = artifact
        display = sanitize_evidence_path_for_display(artifact.get("path", ""))
        add_node(
            f"artifact:{artifact_id}",
            "artifact",
            str(artifact.get("name") or artifact_id),
            artifact_id=artifact_id,
            task_id=artifact.get("task_id", ""),
            stage=artifact.get("stage", ""),
            status=artifact.get("status", ""),
            version=artifact.get("version", ""),
            display=display,
        )
        if artifact.get("task_id"):
            edges.append({"from": f"task:{artifact['task_id']}", "to": f"artifact:{artifact_id}", "label": "created artifact"})
    for handoff in handoff_rows:
        handoff_id = handoff["handoff_id"]
        add_node(
            f"handoff:{handoff_id}",
            "handoff",
            str(handoff.get("summary") or handoff_id),
            handoff_id=handoff_id,
            status=handoff.get("status", ""),
            from_task_id=handoff.get("from_task_id", ""),
            to_task_id=handoff.get("to_task_id", ""),
        )
        if handoff.get("from_task_id"):
            edges.append({"from": f"task:{handoff['from_task_id']}", "to": f"handoff:{handoff_id}", "label": "handoff"})
        if handoff.get("to_task_id"):
            edges.append({"from": f"handoff:{handoff_id}", "to": f"task:{handoff['to_task_id']}", "label": "accepted by"})
        for artifact_id in handoff.get("artifacts", []):
            if artifact_id in artifact_by_id:
                edges.append({"from": f"artifact:{artifact_id}", "to": f"handoff:{handoff_id}", "label": "included"})
    for evidence in evidence_rows:
        evidence_id = evidence["evidence_id"]
        display = sanitize_evidence_path_for_display(evidence.get("path_or_url", ""))
        add_node(
            f"evidence:{evidence_id}",
            "evidence",
            str(evidence.get("summary") or evidence_id),
            evidence_id=evidence_id,
            task_id=evidence.get("task_id", ""),
            artifact_id=evidence.get("artifact_id", ""),
            is_final=bool(evidence.get("is_final")),
            display=display,
        )
        if evidence.get("artifact_id"):
            edges.append({"from": f"artifact:{evidence['artifact_id']}", "to": f"evidence:{evidence_id}", "label": "promoted evidence"})
        elif evidence.get("task_id"):
            edges.append({"from": f"task:{evidence['task_id']}", "to": f"evidence:{evidence_id}", "label": "submitted evidence"})

    mermaid_lines = ["graph LR"]
    for node in nodes:
        label = str(node.get("label") or node["id"]).replace('"', "'")
        mermaid_lines.append(f'  {mermaid_node_id(node["id"])}["{label}"]')
    for edge in edges:
        label = str(edge.get("label") or "").replace('"', "'")
        mermaid_lines.append(f'  {mermaid_node_id(edge["from"])} -->|{label}| {mermaid_node_id(edge["to"])}')
    return {
        "ok": True,
        "kind": "trace_file_flow",
        "trace_id": safe_trace_id,
        "counts": {"tasks": len(task_rows), "artifacts": len(artifact_rows), "handoffs": len(handoff_rows), "evidence": len(evidence_rows), "nodes": len(nodes), "edges": len(edges)},
        "nodes": nodes,
        "edges": edges,
        "mermaid": "\n".join(mermaid_lines),
    }


def audit_failure_records(conn: sqlite3.Connection, *, task_id: str = "", limit: int | str | None = 50) -> list[dict]:
    safe_limit = clamp_audit_limit(limit)
    params: list[object] = []
    task_clause = ""
    if task_id:
        task_clause = " AND id = ?"
        params.append(task_id)
    failed_tasks = rows(
        conn,
        f"""
        SELECT id, source_agent, target_agent, status, blocker, summary, updated_at
        FROM tasks
        WHERE (status IN ('blocked', 'failed', 'stale') OR blocker != '')
          {task_clause}
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        tuple([*params, safe_limit]),
    )
    params = []
    attempt_clause = ""
    if task_id:
        attempt_clause = " AND task_id = ?"
        params.append(task_id)
    failed_attempts = rows(
        conn,
        f"""
        SELECT attempt_id, trace_id, task_id, employee_id, adapter_type, status,
               error_message, started_at, finished_at
        FROM execution_attempts
        WHERE status IN ('failed', 'stale', 'cancelled')
          {attempt_clause}
        ORDER BY COALESCE(finished_at, started_at) DESC
        LIMIT ?
        """,
        tuple([*params, safe_limit]),
    )
    params = []
    adapter_clause = ""
    if task_id:
        adapter_clause = " AND task_id = ?"
        params.append(task_id)
    failed_adapter_runs = rows(
        conn,
        f"""
        SELECT id, trace_id, agent_id, task_id, command, ok, processed, attempt,
               result_json, created_at, acknowledged_at, acknowledgement_reason
        FROM adapter_runs
        WHERE ok = 0
          {adapter_clause}
        ORDER BY created_at DESC
        LIMIT ?
        """,
        tuple([*params, safe_limit]),
    )
    failures = []
    for item in failed_tasks:
        failures.append(
            {
                "kind": "task",
                "id": item["id"],
                "task_id": item["id"],
                "agent_id": item.get("target_agent", ""),
                "status": item.get("status", ""),
                "message": sanitize_log_text(item.get("blocker") or item.get("summary") or ""),
                "created_at": item.get("updated_at", ""),
                "acknowledged": False,
            }
        )
    for item in failed_attempts:
        failures.append(
            {
                "kind": "attempt",
                "id": item["attempt_id"],
                "attempt_id": item["attempt_id"],
                "trace_id": item.get("trace_id", ""),
                "task_id": item.get("task_id", ""),
                "agent_id": item.get("employee_id", ""),
                "status": item.get("status", ""),
                "message": sanitize_log_text(item.get("error_message") or ""),
                "created_at": item.get("finished_at") or item.get("started_at") or "",
                "acknowledged": False,
            }
        )
    for item in failed_adapter_runs:
        raw_result_json = item.pop("result_json", "{}")
        try:
            result = json.loads(raw_result_json or "{}")
        except json.JSONDecodeError:
            result = {"raw": raw_result_json}
        summary = summarize_adapter_result(result)
        failures.append(
            {
                "kind": "adapter_run",
                "id": item["id"],
                "run_id": item["id"],
                "trace_id": item.get("trace_id", ""),
                "task_id": item.get("task_id", ""),
                "agent_id": item.get("agent_id", ""),
                "status": "failed",
                "message": summary.get("sanitized_log", ""),
                "created_at": item.get("created_at", ""),
                "acknowledged": bool(item.get("acknowledged_at")),
                "acknowledgement_reason": sanitize_log_text(item.get("acknowledgement_reason") or ""),
            }
        )
    failures.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return failures[:safe_limit]


def cmd_audit_evidence(args: argparse.Namespace) -> int:
    conn = connect_readonly()
    try:
        evidence = audit_evidence_records(conn, task_id=args.task_id, employee_id=args.employee_id, limit=args.limit)
    finally:
        conn.close()
    emit({"ok": True, "source": "companyctl audit evidence", "evidence": evidence})
    return 0


def cmd_audit_artifacts(args: argparse.Namespace) -> int:
    conn = connect_readonly()
    try:
        artifacts = audit_artifact_records(conn, task_id=args.task_id, limit=args.limit)
    finally:
        conn.close()
    emit({"ok": True, "source": "companyctl audit artifacts", "artifacts": artifacts})
    return 0


def cmd_audit_handoffs(args: argparse.Namespace) -> int:
    conn = connect_readonly()
    try:
        handoffs = audit_handoff_records(conn, task_id=args.task_id, limit=args.limit)
    finally:
        conn.close()
    emit({"ok": True, "source": "companyctl audit handoffs", "handoffs": handoffs})
    return 0


def cmd_audit_failures(args: argparse.Namespace) -> int:
    conn = connect_readonly()
    try:
        failures = audit_failure_records(conn, task_id=args.task_id, limit=args.limit)
    finally:
        conn.close()
    emit({"ok": True, "source": "companyctl audit failures", "failures": failures})
    return 0


def cmd_trace_timeline(args: argparse.Namespace) -> int:
    from . import company_trace

    conn = connect_readonly()
    try:
        trace_id = company_trace.resolve_trace_id(conn, args.trace_id, args.task_id)
        trace = company_trace.load_trace(conn, trace_id)
    finally:
        conn.close()
    emit(company_trace.safe_trace_payload(trace))
    return 0


def cmd_runtime_session_start(args: argparse.Namespace) -> int:
    conn = connect()
    try:
        payload = start_runtime_session_internal(
            conn,
            session_id=args.session_id,
            employee_id=args.employee,
            adapter_type=args.adapter_type,
            runtime_type=args.runtime_type,
            pid=args.pid,
            session_key=args.session_key,
            task_id=args.task_id,
            attempt_id=args.attempt_id,
        )
    finally:
        conn.close()
    emit({"ok": True, **payload})
    return 0


def cmd_runtime_session_heartbeat(args: argparse.Namespace) -> int:
    conn = connect()
    try:
        payload = heartbeat_runtime_session_internal(conn, session_id=args.session_id, status=args.status, progress=args.progress)
    finally:
        conn.close()
    emit({"ok": True, **payload})
    return 0


def cmd_runtime_session_stop(args: argparse.Namespace) -> int:
    conn = connect()
    try:
        payload = stop_runtime_session_internal(conn, session_id=args.session_id, status=args.status, error=args.error)
    finally:
        conn.close()
    emit({"ok": True, **payload})
    return 0


def cmd_runtime_session_list(args: argparse.Namespace) -> int:
    conn = connect_readonly()
    try:
        sessions = list_runtime_sessions(conn, employee_id=args.employee, task_id=args.task_id, trace_id=args.trace_id, limit=args.limit)
    finally:
        conn.close()
    emit({"ok": True, "runtime_sessions": sessions})
    return 0


def cmd_tool_call_start(args: argparse.Namespace) -> int:
    conn = connect()
    try:
        payload = start_tool_call_internal(
            conn,
            tool_call_id=args.tool_call_id,
            trace_id=args.trace_id,
            task_id=args.task_id,
            attempt_id=args.attempt_id,
            employee_id=args.employee,
            session_id=args.session_id,
            tool_name=args.tool_name,
            tool_type=args.tool_type,
            input_summary=args.input_summary,
            risk_level=args.risk_level,
            approval_id=args.approval_id,
        )
    finally:
        conn.close()
    emit({"ok": True, **payload})
    return 0


def cmd_tool_call_finish(args: argparse.Namespace) -> int:
    conn = connect()
    try:
        payload = finish_tool_call_internal(conn, tool_call_id=args.tool_call_id, status=args.status, output_summary=args.output_summary, error=args.error)
    finally:
        conn.close()
    emit({"ok": True, **payload})
    return 0


def cmd_tool_call_list(args: argparse.Namespace) -> int:
    conn = connect_readonly()
    try:
        tool_calls = list_tool_calls(conn, employee_id=args.employee, task_id=args.task_id, trace_id=args.trace_id, attempt_id=args.attempt_id, session_id=args.session_id, limit=args.limit)
    finally:
        conn.close()
    emit({"ok": True, "tool_calls": tool_calls})
    return 0


def cmd_budget_record(args: argparse.Namespace) -> int:
    conn = connect()
    try:
        payload = record_budget_event_internal(
            conn,
            budget_event_id=args.budget_event_id,
            budget_account_id=args.budget_account_id,
            task_id=args.task_id,
            trace_id=args.trace_id,
            attempt_id=args.attempt_id,
            employee_id=args.employee,
            cost_type=args.cost_type,
            amount=float(args.amount),
            currency=args.currency,
            token_input=args.token_input,
            token_output=args.token_output,
            model_name=args.model_name,
            provider=args.provider,
            runtime_seconds=args.runtime_seconds,
            summary=args.summary,
        )
    finally:
        conn.close()
    emit({"ok": True, **payload})
    return 0


def load_pricing_config() -> dict:
    # Thin wrapper: assemble the pricing path under ROOT, then delegate to the pure reader.
    return _core_config.load_pricing_config(ROOT / "config" / "pricing.json")


def compute_economics(conn: sqlite3.Connection) -> dict:
    """Per-task-type unit economics: revenue (result price) vs cost (from budget events) ->
    margin. This is survival-metric #1 for outcome-based pricing.

    Thin shell now (split: economics build-core cut B): load pricing, fetch completed tasks, aggregate
    budget_events per task_id (rows() already yields plain dicts), then delegate the bucketing to the
    pure economics.build_economics. Behaviour is golden-pinned identical."""
    pricing = load_pricing_config()
    tasks = rows(conn, "SELECT id, title, description, target_agent FROM tasks WHERE status = 'completed'")
    cost_by_task: dict = {}
    for ev in rows(conn, "SELECT task_id, amount, token_input, token_output, runtime_seconds FROM budget_events WHERE task_id != ''"):
        cost_by_task.setdefault(ev["task_id"], []).append(ev)
    return build_economics(tasks, cost_by_task, pricing)


def load_heartbeat_ages(conn: sqlite3.Connection, employee_ids) -> dict:
    """Shell loader for build_cost_dashboard: pre-fetch each employee's heartbeat age (minutes), so the
    pure core stays clock/DB-free. Values pass through verbatim — float minutes, None (never beat),
    or float('inf') (unparseable heartbeat); the core renders inf/None as null / off-duty."""
    return {eid: heartbeat_age_minutes(conn, eid) for eid in employee_ids}


def compute_cost_dashboard(conn: sqlite3.Connection, *, days: int = 14) -> dict:
    """The operating-cost story behind the product's core promise: employees on duty are
    FREE (internal comms + task checks are pure SQL, 0 token) — only *claimed execution*
    spends. Aggregates the budget ledger per employee + per day so a human can SEE who is
    on duty at zero cost vs where money actually went. Cost uses the same estimate as
    unit-economics (recorded amount > token estimate > runtime fallback) so the two views
    never disagree. See docs/ON_DUTY_COST_MODEL.md.

    Thin shell now (split: dashboard pure-core cut): gather the ledger + human-owner-filtered
    employees + pre-fetched heartbeat ages + pricing, then hand them to the pure
    economics.build_cost_dashboard. The is_human filter stays HERE on the (id, status) rows — exactly as
    before, so it still only catches id=='owner' (role/runtime columns aren't fetched). Behaviour is
    golden-pinned identical."""
    ledger = rows(conn, "SELECT employee_id, amount, token_input, token_output, runtime_seconds, "
                        "substr(created_at,1,10) AS day FROM budget_events WHERE employee_id != ''")
    employee_rows = [e for e in rows(conn, "SELECT id, status FROM employees ORDER BY id")
                     if not is_human_owner_employee(e)]
    heartbeat_ages = load_heartbeat_ages(conn, [e["id"] for e in employee_rows])
    return build_cost_dashboard(ledger, employee_rows, heartbeat_ages, load_pricing_config(),
                                off_duty_threshold=OFF_DUTY_HEARTBEAT_MINUTES, days=days)


def cmd_economics(args: argparse.Namespace) -> int:
    conn = connect_readonly()
    try:
        emit({"ok": True, **compute_economics(conn)})
    finally:
        conn.close()
    return 0


def cmd_cost(args: argparse.Namespace) -> int:
    conn = connect_readonly()
    try:
        emit({"ok": True, **compute_cost_dashboard(conn, days=max(1, int(getattr(args, "days", 14))))})
    finally:
        conn.close()
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    """Thin companyctl entrypoint over company_kernel.backup (single discoverable UX)."""
    from company_kernel import backup as backup_mod
    if getattr(args, "list", False):
        snaps = backup_mod.list_snapshots()
        emit({"ok": True, "count": len(snaps), "backup_dir": str(backup_mod.BACKUP_DIR),
              "snapshots": [{"path": str(s), "size_bytes": s.stat().st_size} for s in snaps]})
        return 0
    result = backup_mod.snapshot(keep=args.keep, label=args.label)
    emit({"action": "backup", **result})
    return 0 if result.get("ok") else 1


def cmd_restore(args: argparse.Namespace) -> int:
    from company_kernel import backup as backup_mod
    # restore() itself refuses without yes and snapshots the live DB first (pre-restore-*).
    result = backup_mod.restore(Path(args.src), yes=args.yes)
    emit({"action": "restore", **result})
    return 0 if result.get("ok") else (2 if "--yes" in str(result.get("error", "")) else 1)


def record_verifier_run_internal(conn: sqlite3.Connection, *, task_id: str, attempt_id: str,
                                 employee_id: str, kind: str, arg: str, result: str,
                                 agent_verdict: str, detail: str) -> dict:
    """Log one verifier judgment so its accuracy can be sampled against later human review."""
    verifier_run_id = f"verifier-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    ts = now()
    conn.execute(
        """INSERT INTO verifier_runs(verifier_run_id, task_id, attempt_id, employee_id, kind, arg,
             result, agent_verdict, detail, human_review, reviewed_by, reviewed_at, created_at)
           VALUES(?,?,?,?,?,?,?,?,?,'','','',?)""",
        (verifier_run_id, task_id or "", attempt_id or "", resolve_employee_alias(employee_id) if employee_id else "",
         (kind or "status").lower(), arg or "", (result or "").lower(), agent_verdict or "", detail or "", ts),
    )
    conn.commit()
    return {"verifier_run_id": verifier_run_id, "task_id": task_id, "kind": kind, "result": result, "created_at": ts}


def link_human_review_to_verifier(conn: sqlite3.Connection, task_id: str, status: str, reviewer: str) -> None:
    """When a human accepts/rejects a task's evidence, stamp the latest unreviewed verifier
    run for that task. This is the ground-truth signal for verifier accuracy."""
    if not task_id or status not in {"accepted", "rejected"}:
        return
    row = conn.execute(
        "SELECT verifier_run_id FROM verifier_runs WHERE task_id = ? AND human_review = '' ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if not row:
        return
    conn.execute(
        "UPDATE verifier_runs SET human_review = ?, reviewed_by = ?, reviewed_at = ? WHERE verifier_run_id = ?",
        (status, resolve_employee_alias(reviewer) if reviewer else "", now(), row["verifier_run_id"]),
    )
    conn.commit()


def compute_verifier_accuracy(conn: sqlite3.Connection) -> dict:
    """Per-kind sampling accuracy: how often the verifier's verdict agreed with the human
    review that later sampled it. A verifier 'pass' that a human accepts is correct; a 'pass'
    a human rejects is a false-positive (the worst case for outcome-based pay). A withhold
    (fail/needs_human/error) that a human rejects is correct; one a human accepts is a
    false-negative (verifier was too strict)."""
    rows_all = rows(conn, "SELECT kind, result, human_review FROM verifier_runs")
    buckets: dict = {}
    for r in rows_all:
        kind = str(r.get("kind") or "status")
        result = str(r.get("result") or "")
        review = str(r.get("human_review") or "")
        b = buckets.setdefault(kind, {
            "kind": kind, "total": 0, "pass": 0, "withhold": 0,
            "reviewed": 0, "correct": 0, "false_positive": 0, "false_negative": 0,
        })
        b["total"] += 1
        passed = result == "pass"
        if passed:
            b["pass"] += 1
        else:
            b["withhold"] += 1
        if review in {"accepted", "rejected"}:
            b["reviewed"] += 1
            if passed and review == "accepted":
                b["correct"] += 1
            elif not passed and review == "rejected":
                b["correct"] += 1
            elif passed and review == "rejected":
                b["false_positive"] += 1
            else:  # withheld but human accepted
                b["false_negative"] += 1
    out = []
    for b in sorted(buckets.values(), key=lambda x: x["kind"]):
        b["accuracy"] = round(b["correct"] / b["reviewed"], 4) if b["reviewed"] else None
        out.append(b)
    totals = {
        "total": sum(b["total"] for b in out),
        "reviewed": sum(b["reviewed"] for b in out),
        "correct": sum(b["correct"] for b in out),
        "false_positive": sum(b["false_positive"] for b in out),
        "false_negative": sum(b["false_negative"] for b in out),
    }
    totals["accuracy"] = round(totals["correct"] / totals["reviewed"], 4) if totals["reviewed"] else None
    return {"by_kind": out, "totals": totals}


def cmd_verifier_record(args: argparse.Namespace) -> int:
    conn = connect()
    try:
        payload = record_verifier_run_internal(
            conn, task_id=args.task_id, attempt_id=args.attempt_id, employee_id=args.employee,
            kind=args.kind, arg=args.arg, result=args.result, agent_verdict=args.agent_verdict,
            detail=args.detail,
        )
    finally:
        conn.close()
    emit({"ok": True, **payload})
    return 0


def cmd_verifier_accuracy(args: argparse.Namespace) -> int:
    conn = connect_readonly()
    try:
        emit({"ok": True, **compute_verifier_accuracy(conn)})
    finally:
        conn.close()
    return 0


def a2a_telegram_keyboard(request: dict) -> dict:
    """Build the Telegram inline-keyboard payload the operator's approval bot posts.
    callback_data encodes the decision + request id (Telegram caps callback_data at 64 bytes)."""
    rid = request["a2a_request_id"]
    text = (
        "🔐 Agent-to-Agent 申请\n"
        f"发起: {request['source_agent']} → {request['target_agent']}\n"
        f"动作: {request['action']}\n"
        f"请求ID: {rid}"
    )
    return {
        "text": text,
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "✅ 同意", "callback_data": f"a2a:approve:{rid}"},
                {"text": "❌ 拒绝", "callback_data": f"a2a:deny:{rid}"},
            ]]
        },
    }


def record_a2a_request_internal(conn: sqlite3.Connection, *, source_agent: str, target_agent: str,
                                action: str, payload: str) -> dict:
    """Queue an agent-to-agent request for owner approval. Default-deny: nothing crosses
    until a human (or rule) approves, and every decision is audited."""
    rid = f"a2a-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    ts = now()
    src = resolve_employee_alias(source_agent) if source_agent else ""
    tgt = resolve_employee_alias(target_agent) if target_agent else ""
    conn.execute(
        "INSERT INTO a2a_requests(a2a_request_id, source_agent, target_agent, action, payload, status, created_at) VALUES(?,?,?,?,?,'pending',?)",
        (rid, src, tgt, action or "", payload or "", ts),
    )
    conn.commit()
    record_event(conn, "a2a.requested", src, payload={"a2a_request_id": rid, "target_agent": tgt, "action": action})
    audit(conn, src, "a2a.request", rid, {"target_agent": tgt, "action": action})
    request = {"a2a_request_id": rid, "source_agent": src, "target_agent": tgt, "action": action, "payload": payload, "status": "pending", "created_at": ts}
    request["telegram"] = a2a_telegram_keyboard(request)
    return request


def decide_a2a_internal(conn: sqlite3.Connection, *, a2a_request_id: str, by: str, decision: str) -> dict:
    if decision not in {"approved", "denied"}:
        raise SystemExit("decision must be approved or denied")
    row = conn.execute("SELECT * FROM a2a_requests WHERE a2a_request_id = ?", (a2a_request_id,)).fetchone()
    if not row:
        return {"ok": False, "error": "a2a request not found", "a2a_request_id": a2a_request_id}
    if row["status"] != "pending":
        return {"ok": False, "error": f"already {row['status']}", "a2a_request_id": a2a_request_id, "status": row["status"]}
    actor = resolve_employee_alias(by) if by else ""
    ts = now()
    conn.execute("UPDATE a2a_requests SET status = ?, decided_by = ?, decided_at = ? WHERE a2a_request_id = ?", (decision, actor, ts, a2a_request_id))
    conn.commit()
    record_event(conn, "a2a.approved" if decision == "approved" else "a2a.denied", actor, payload={"a2a_request_id": a2a_request_id, "source_agent": row["source_agent"], "target_agent": row["target_agent"]})
    audit(conn, actor, "a2a.approve" if decision == "approved" else "a2a.deny", a2a_request_id, {"source_agent": row["source_agent"], "target_agent": row["target_agent"], "action": row["action"]})
    return {"ok": True, "a2a_request_id": a2a_request_id, "status": decision, "decided_by": actor, "decided_at": ts, "allowed": decision == "approved"}


def cmd_a2a_request(args: argparse.Namespace) -> int:
    conn = connect()
    try:
        payload = record_a2a_request_internal(conn, source_agent=args.source, target_agent=args.target, action=args.action, payload=args.payload)
    finally:
        conn.close()
    emit({"ok": True, **payload})
    return 0


def cmd_a2a_approve(args: argparse.Namespace) -> int:
    conn = connect()
    try:
        result = decide_a2a_internal(conn, a2a_request_id=args.request_id, by=args.by, decision="approved")
    finally:
        conn.close()
    emit(result)
    return 0 if result.get("ok") else 1


def cmd_a2a_deny(args: argparse.Namespace) -> int:
    conn = connect()
    try:
        result = decide_a2a_internal(conn, a2a_request_id=args.request_id, by=args.by, decision="denied")
    finally:
        conn.close()
    emit(result)
    return 0 if result.get("ok") else 1


def cmd_a2a_list(args: argparse.Namespace) -> int:
    conn = connect_readonly()
    try:
        where = "WHERE status = ?" if args.status else ""
        params = (args.status, args.limit) if args.status else (args.limit,)
        items = rows(conn, f"SELECT a2a_request_id, source_agent, target_agent, action, status, decided_by, created_at FROM a2a_requests {where} ORDER BY created_at DESC LIMIT ?", params)
    finally:
        conn.close()
    emit({"ok": True, "a2a_requests": items})
    return 0


def cmd_budget_summary(args: argparse.Namespace) -> int:
    conn = connect_readonly()
    try:
        summary = budget_summary(conn, task_id=args.task_id, employee_id=args.employee, trace_id=args.trace_id, attempt_id=args.attempt_id)
        events = list_budget_events(conn, task_id=args.task_id, employee_id=args.employee, trace_id=args.trace_id, attempt_id=args.attempt_id, limit=args.limit)
    finally:
        conn.close()
    emit({"ok": True, "summary": summary, "budget_events": events})
    return 0


TERMINAL_WORKSPACE_STATUSES = {"done", "failed", "cancelled"}


def directory_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for item in path.rglob("*"):
        if item.is_file() and not item.is_symlink():
            total += item.stat().st_size
    return total


def workspace_prune_preview(conn: sqlite3.Connection, *, older_than_days: int = 30, limit: int = 100) -> dict:
    safe_days = max(1, int(older_than_days or 30))
    safe_limit = max(1, min(int(limit or 100), 500))
    cutoff = datetime.now(timezone.utc).astimezone() - timedelta(days=safe_days)
    candidates = []
    for item in rows(
        conn,
        """
        SELECT tw.task_id, tw.trace_id, tw.path, tw.manifest_path, tw.updated_at,
               t.status, t.target_agent, t.title, t.updated_at AS task_updated_at
        FROM task_workspaces tw
        JOIN tasks t ON t.id = tw.task_id
        ORDER BY tw.updated_at ASC
        """,
    ):
        status = str(item.get("status") or "")
        if status not in TERMINAL_WORKSPACE_STATUSES:
            continue
        try:
            updated = parse_time(str(item.get("updated_at") or item.get("task_updated_at") or ""))
        except (TypeError, ValueError):
            continue
        if updated >= cutoff:
            continue
        workspace_path = Path(str(item.get("path") or "")).expanduser().resolve()
        try:
            relative = workspace_path.relative_to(TASK_WORKSPACE_ROOT.resolve())
        except ValueError:
            continue
        candidates.append(
            {
                "task_id": item["task_id"],
                "trace_id": item.get("trace_id", ""),
                "status": status,
                "target_agent": item.get("target_agent", ""),
                "title": item.get("title", ""),
                "updated_at": item.get("updated_at", ""),
                "age_days": max(0, int((datetime.now(timezone.utc).astimezone() - updated).total_seconds() // 86400)),
                "workspace": str(relative),
                "manifest": str(Path(str(item.get("manifest_path") or "")).name),
                "bytes": directory_size_bytes(workspace_path),
            }
        )
        if len(candidates) >= safe_limit:
            break
    return {
        "ok": True,
        "dry_run": True,
        "policy": {
            "mode": "preview_only",
            "older_than_days": safe_days,
            "terminal_statuses": sorted(TERMINAL_WORKSPACE_STATUSES),
            "root": str(TASK_WORKSPACE_ROOT),
        },
        "summary": {
            "candidates": len(candidates),
            "bytes_reclaimable": sum(int(item.get("bytes") or 0) for item in candidates),
        },
        "candidates": candidates,
    }


def cmd_workspace_prune(args: argparse.Namespace) -> int:
    if not args.dry_run:
        emit({"ok": False, "error": "workspace prune is preview-only in this phase; pass --dry-run"})
        return 2
    conn = connect_readonly()
    try:
        preview = workspace_prune_preview(conn, older_than_days=args.older_than_days, limit=args.limit)
    finally:
        conn.close()
    emit(preview)
    return 0


def is_human_owner_employee(employee: dict) -> bool:
    return employee.get("id") == "owner" or employee.get("role") == "human-owner" or employee.get("runtime") == "human"


def employee_backlog(conn: sqlite3.Connection, employee_id: str) -> dict:
    """Per-employee backlog so a slacking/stuck worker can't look 'normal' in the admin view.
    queued = assigned but not yet started; in_progress = claimed/being worked; stuck = blocked;
    inbox_files = notification backlog; unprocessed_events = their own events not yet consumed."""
    queued = conn.execute("SELECT COUNT(*) FROM tasks WHERE target_agent=? AND status='submitted'", (employee_id,)).fetchone()[0]
    in_progress = conn.execute("SELECT COUNT(*) FROM tasks WHERE target_agent=? AND status='claimed'", (employee_id,)).fetchone()[0]
    stuck = conn.execute("SELECT COUNT(*) FROM tasks WHERE target_agent=? AND status='blocked'", (employee_id,)).fetchone()[0]
    events = conn.execute("SELECT COUNT(*) FROM company_events WHERE source_agent=? AND processed_at=''", (employee_id,)).fetchone()[0]
    try:
        inbox_dir = employee_paths(employee_id)["inbox"]
        inbox_files = sum(1 for _ in inbox_dir.glob("*.json")) if inbox_dir.exists() else 0
    except OSError:
        inbox_files = 0
    return {
        "queued": queued,
        "in_progress": in_progress,
        "stuck": stuck,
        "unprocessed_events": events,
        "inbox_files": inbox_files,
        # "piled up" = work that is NOT actively progressing (queued + stuck). in_progress is fine.
        "piled_up": queued + stuck,
    }


def cmd_employee_list(_args: argparse.Namespace) -> int:
    conn = connect()
    employees = [employee for employee in rows(conn, "SELECT * FROM employees ORDER BY id") if not is_human_owner_employee(employee)]
    # attach the real unavailable reason (from profile) so consumers/console show WHY a
    # candidate isn't working, not just that it isn't.
    for emp in employees:
        if emp.get("status") != "active":
            profile = load_json_or_default(employee_paths(emp["id"])["profile"], {})
            reason = str(profile.get("unavailable_reason") or "")
            if reason:
                emp["unavailable_reason"] = reason
        emp["backlog"] = employee_backlog(conn, emp["id"])
    emit({"ok": True, "employees": employees})
    return 0


def cmd_inbox_prune(args: argparse.Namespace) -> int:
    conn = connect()
    if str(args.agent).lower() in ("all", "*", ""):
        agents = [row["id"] for row in rows(conn, "SELECT id FROM employees")]
    else:
        agents = [resolve_employee_alias(args.agent)]
    by_agent = {}
    total = 0
    for agent in agents:
        inbox = employee_paths(agent)["inbox"]
        if not inbox.exists():
            continue
        before = sum(1 for _ in inbox.glob("*.json"))
        removed = prune_inbox_dir(inbox, keep=args.keep)
        if before:
            by_agent[agent] = {"before": before, "removed": removed, "kept": before - removed}
        total += removed
    emit({"ok": True, "keep": args.keep, "removed_total": total, "by_agent": by_agent})
    return 0


def cmd_employee_update(args: argparse.Namespace) -> int:
    conn = connect()
    employee_id = resolve_employee_alias(args.id, strict=True)
    row = conn.execute("SELECT * FROM employees WHERE id = ?", (employee_id,)).fetchone()
    if not row:
        conn.close()
        emit({"ok": False, "error": "unknown employee", "employee_id": employee_id})
        return 1
    current = dict(row)
    runtime = args.runtime or current["runtime"]
    try:
        require_runtime(conn, runtime)
    except SystemExit:
        conn.close()
        raise
    updated = {
        **current,
        "name": args.name or current["name"],
        "role": args.role or current["role"],
        "runtime": runtime,
        "workspace": args.workspace or current["workspace"],
        "status": args.status or current["status"],
        "updated_at": now(),
    }
    if args.status == "active" and not (
        employee_has_verified_direct_evidence(employee_id) or employee_has_runtime_evidence(employee_id, conn)
    ):
        conn.close()
        emit(
            {
                "ok": False,
                "error": "employee activation requires verified direct communication or structured runtime evidence",
                "employee_id": employee_id,
                "status": current["status"],
                "required_command": f"bin/companyctl employee verify-direct --id {employee_id} --from main --rounds 3 --activate OR bin/companyctl runtime verify-adapters --agents {employee_id} --allow-candidate",
            }
        )
        return 2
    if args.dry_run:
        conn.close()
        emit({"ok": True, "dry_run": True, "changed": updated != current, "employee": updated})
        return 0
    conn.execute(
        """
        UPDATE employees
        SET name = ?, role = ?, runtime = ?, workspace = ?, status = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            updated["name"],
            updated["role"],
            updated["runtime"],
            updated["workspace"],
            updated["status"],
            updated["updated_at"],
            employee_id,
        ),
    )
    profile = {
        "id": employee_id,
        "name": updated["name"],
        "role": updated["role"],
        "runtime": updated["runtime"],
        "workspace": updated["workspace"],
        "status": updated["status"],
        "created_at": current.get("created_at", ""),
        "updated_at": updated["updated_at"],
    }
    current_profile = load_json_or_default(employee_paths(employee_id)["profile"], profile)
    for field in ("default_user_reply_channel", "default_user_reply_account", "default_user_reply_to"):
        value = getattr(args, field, None)
        if value not in {None, ""}:
            profile[field] = str(value)
        elif field in current_profile:
            profile[field] = current_profile[field]
    if getattr(args, "default_user_reply_deliver", None) is not None:
        profile["default_user_reply_deliver"] = bool(args.default_user_reply_deliver)
    elif "default_user_reply_deliver" in current_profile:
        profile["default_user_reply_deliver"] = bool(current_profile["default_user_reply_deliver"])
    files = write_employee_files(employee_id, profile, dry_run=False)
    communication = sync_employee_name_alias(employee_id, updated["name"], dry_run=False)
    conn.commit()
    audit(conn, "companyctl", "employee.update", employee_id, {"before": current, "after": updated, "files": files, "communication": communication})
    conn.close()
    emit({"ok": True, "changed": updated != current, "employee": updated, "files": files, "communication": communication})
    return 0


def employee_sandbox_profile(employee: dict, permissions: dict, profile_name: str = "default") -> dict:
    runtime = str(employee.get("runtime") or "")
    safe_profile = str(employee.get("sandbox_profile") or profile_name or "default")
    config = sandboxing.load_profiles()
    runtime_profiles = config.get("profiles", {}).get(runtime, {})
    configured = safe_profile in runtime_profiles or "default" in runtime_profiles
    profile = sandboxing.profile_for(runtime, safe_profile, config)
    requires_approval_for = permissions.get("requires_approval_for", [])
    if not isinstance(requires_approval_for, list):
        requires_approval_for = []
    readonly_paths = profile.get("readonly_paths", [])
    writable_paths = profile.get("writable_paths", [])
    return {
        "runtime": runtime,
        "profile": safe_profile,
        "source": "configured" if configured else "runtime_fallback",
        "isolation": str(profile.get("isolation") or "none"),
        "network": str(profile.get("network") or "default"),
        "image": str(profile.get("image") or ""),
        "workspace_scope": "workspace_only",
        "readonly_paths_count": len(readonly_paths) if isinstance(readonly_paths, list) else 0,
        "writable_paths_count": len(writable_paths) if isinstance(writable_paths, list) else 0,
        "permissions": {
            "can_submit_tasks": bool(permissions.get("can_submit_tasks", True)),
            "can_claim_tasks": bool(permissions.get("can_claim_tasks", True)),
            "can_modify_kernel": bool(permissions.get("can_modify_kernel", False)),
            "requires_approval_for": [str(item) for item in requires_approval_for],
        },
    }


def employee_ceo_work_contract(
    *,
    employee: dict,
    current_activity: dict,
    operational_summary: dict,
    runtime_sessions: list[dict],
    tool_calls: list[dict],
    budget_summary: dict,
    evidence_records: list[dict],
) -> dict:
    latest_session = runtime_sessions[0] if runtime_sessions else {}
    latest_tool = tool_calls[0] if tool_calls else {}
    latest_evidence = evidence_records[0] if evidence_records else {}
    failed_or_blocked_tools = [
        item
        for item in tool_calls
        if str(item.get("status") or "") in {"failed", "blocked", "cancelled"}
    ]
    final_evidence = [
        item
        for item in evidence_records
        if bool(item.get("is_final")) or str(item.get("stage") or "") == "final"
    ]
    warnings: list[str] = []
    current_state = str(operational_summary.get("current_state") or current_activity.get("long_task_state") or current_activity.get("task_status") or "idle")
    if current_state in {"blocked", "failed", "stale", "heartbeat_stale", "progress_stagnant"}:
        warnings.append(current_state)
    if failed_or_blocked_tools:
        warnings.append("failed_or_blocked_tool_calls")
    if current_activity.get("task_id") and not tool_calls:
        warnings.append("tool_call_ledger_missing")
    if current_state in {"completed", "done", "success"} and not final_evidence:
        warnings.append("completion_without_final_evidence")
    if str(employee.get("status") or "") == "candidate":
        warnings.append("candidate_not_active")

    mode = "idle"
    if current_activity.get("task_id"):
        mode = "monitor"
    if warnings:
        mode = "owner_attention"
    if current_state in {"blocked", "failed", "stale", "progress_stagnant", "heartbeat_stale"}:
        mode = "intervene"

    return {
        "employee_id": employee.get("id", ""),
        "employee_status": employee.get("status", ""),
        "runtime": employee.get("runtime", ""),
        "current_task": {
            "task_id": current_activity.get("task_id", ""),
            "title": current_activity.get("task_title", ""),
            "state": current_state or "idle",
            "task_status": current_activity.get("task_status", ""),
            "attempt_id": current_activity.get("attempt_id", ""),
            "attempt_status": current_activity.get("attempt_status", ""),
            "trace_id": current_activity.get("trace_id", ""),
            "session_id": latest_session.get("session_id", ""),
            "heartbeat_state": current_activity.get("heartbeat_state", ""),
            "progress_state": current_activity.get("progress_state", ""),
            "progress_age_seconds": current_activity.get("progress_age_seconds", 0),
            "latest_progress": current_activity.get("latest_progress", {}),
        },
        "runtime_sessions": {
            "total": len(runtime_sessions),
            "latest_session_id": latest_session.get("session_id", ""),
            "latest_status": latest_session.get("status", ""),
            "latest_runtime_type": latest_session.get("runtime_type", ""),
        },
        "tool_calls": {
            "total": len(tool_calls),
            "failed_or_blocked": len(failed_or_blocked_tools),
            "latest_tool_call_id": latest_tool.get("tool_call_id", ""),
            "latest_tool_name": latest_tool.get("tool_name", ""),
            "latest_status": latest_tool.get("status", ""),
        },
        "budget": {
            "event_count": budget_summary.get("event_count", 0),
            "currency": budget_summary.get("currency", "USD"),
            "total_amount": budget_summary.get("total_amount", 0),
            "token_input": budget_summary.get("token_input", 0),
            "token_output": budget_summary.get("token_output", 0),
            "runtime_seconds": budget_summary.get("runtime_seconds", 0),
            "limit_status": budget_summary.get("limit_status", "ok"),
        },
        "evidence": {
            "total": len(evidence_records),
            "final_count": len(final_evidence),
            "latest_evidence_id": latest_evidence.get("evidence_id", ""),
            "latest_summary": latest_evidence.get("summary", ""),
        },
        "owner_review": {
            "mode": mode,
            "warnings": warnings,
            "next_action": operational_summary.get("owner_next_action", "inspect employee work before assigning more tasks"),
        },
        "truth_rules": {
            "completion_requires_final_evidence": True,
            "heartbeat_is_completion": False,
            "chat_reply_is_completion": False,
            "stdout_is_completion": False,
            "candidate_can_auto_assign": False,
        },
    }


def employee_file_bundle(conn: sqlite3.Connection, employee_id: str) -> dict:
    employee_id = resolve_employee_alias(employee_id)
    row = conn.execute("SELECT * FROM employees WHERE id = ?", (employee_id,)).fetchone()
    if not row:
        raise SystemExit(f"unknown employee: {employee_id}")
    profile = dict(row)
    paths = employee_paths(employee_id)
    file_profile = load_json_or_default(paths["profile"], profile)
    write_employee_capabilities(employee_id, file_profile, dry_run=False)
    capabilities = load_json_or_default(paths["capabilities"], default_capabilities(file_profile))
    permissions = load_json_or_default(
        paths["permissions"],
        {
            "can_submit_tasks": True,
            "can_claim_tasks": True,
            "can_modify_kernel": False,
            "requires_approval_for": ["payment", "compensation", "salary", "penalty", "external_send"],
        },
    )
    heartbeat = load_json_or_default(paths["heartbeat"], {})
    sandbox_profile = employee_sandbox_profile(profile, permissions)
    task_rows = rows(
        conn,
        """
        SELECT *
        FROM tasks
        WHERE source_agent = ? OR target_agent = ? OR claimed_by = ?
        ORDER BY
          CASE
            WHEN LOWER(status) IN ('submitted', 'claimed', 'running', 'waiting_approval', 'blocked', 'stale', 'retrying') THEN 0
            ELSE 1
          END ASC,
          updated_at DESC,
          created_at DESC
        LIMIT 50
        """,
        (employee_id, employee_id, employee_id),
    )
    attempt_rows = [
        hydrate_execution_attempt(item)
        for item in rows(
            conn,
            "SELECT * FROM execution_attempts WHERE employee_id = ? ORDER BY started_at DESC, rowid DESC LIMIT 50",
            (employee_id,),
        )
    ]
    runtime_sessions = list_runtime_sessions(conn, employee_id=employee_id, limit=50)
    tool_calls = list_tool_calls(conn, employee_id=employee_id, limit=100)
    budget_events = list_budget_events(conn, employee_id=employee_id, limit=100)
    employee_budget_summary = budget_summary(conn, employee_id=employee_id)
    evidence_records = [item for item in audit_evidence_records(conn, limit=200) if item.get("employee_id") == employee_id][:50]
    task_rollups: dict[str, dict] = {}
    for task in task_rows:
        task_id = str(task.get("id") or "")
        task_rollups[task_id] = {
            "attempt_count": 0,
            "latest_attempt_id": "",
            "latest_attempt_status": "",
            "runtime_session_count": 0,
            "tool_call_count": 0,
            "failed_tool_call_count": 0,
            "latest_tool_call_id": "",
            "latest_tool_name": "",
            "latest_tool_status": "",
            "budget_event_count": 0,
            "budget_total": 0.0,
            "budget_currency": "",
            "token_input": 0,
            "token_output": 0,
            "runtime_seconds": 0,
            "evidence_count": 0,
            "has_final_evidence": False,
        }
    for attempt in attempt_rows:
        task_id = str(attempt.get("task_id") or "")
        if task_id in task_rollups:
            task_rollups[task_id]["attempt_count"] += 1
            if not task_rollups[task_id]["latest_attempt_id"]:
                task_rollups[task_id]["latest_attempt_id"] = str(attempt.get("attempt_id") or "")
                task_rollups[task_id]["latest_attempt_status"] = str(attempt.get("status") or "")
    for session in runtime_sessions:
        task_id = str(session.get("task_id") or "")
        if task_id in task_rollups:
            task_rollups[task_id]["runtime_session_count"] += 1
    for tool_call in tool_calls:
        task_id = str(tool_call.get("task_id") or "")
        if task_id in task_rollups:
            task_rollups[task_id]["tool_call_count"] += 1
            if str(tool_call.get("status") or "") in {"failed", "blocked", "cancelled"}:
                task_rollups[task_id]["failed_tool_call_count"] += 1
            if not task_rollups[task_id]["latest_tool_call_id"]:
                task_rollups[task_id]["latest_tool_call_id"] = str(tool_call.get("tool_call_id") or "")
                task_rollups[task_id]["latest_tool_name"] = str(tool_call.get("tool_name") or "")
                task_rollups[task_id]["latest_tool_status"] = str(tool_call.get("status") or "")
    for budget_event in budget_events:
        task_id = str(budget_event.get("task_id") or "")
        if task_id in task_rollups:
            rollup = task_rollups[task_id]
            rollup["budget_event_count"] += 1
            rollup["budget_total"] = round(float(rollup["budget_total"]) + float(budget_event.get("amount") or 0), 10)
            rollup["budget_currency"] = str(budget_event.get("currency") or rollup["budget_currency"] or "")
            rollup["token_input"] += int(budget_event.get("token_input") or 0)
            rollup["token_output"] += int(budget_event.get("token_output") or 0)
            rollup["runtime_seconds"] += int(budget_event.get("runtime_seconds") or 0)
    for evidence in evidence_records:
        task_id = str(evidence.get("task_id") or "")
        if task_id in task_rollups:
            task_rollups[task_id]["evidence_count"] += 1
            if bool(evidence.get("is_final")) or str(evidence.get("stage") or "") == "final":
                task_rollups[task_id]["has_final_evidence"] = True

    enriched_tasks = [{**dict(item), **task_rollups.get(str(item.get("id") or ""), {})} for item in task_rows]
    status_counts: dict[str, int] = {}
    def effective_task_status(task: dict) -> str:
        attempt_status = str(task.get("latest_attempt_status") or "").lower()
        if attempt_status in {"starting", "running", "correcting"}:
            return "running"
        if attempt_status in {"failed", "blocked", "stale", "cancelled", "success"}:
            return "completed" if attempt_status == "success" else attempt_status
        return str(task.get("status") or "unknown").lower() or "unknown"

    for task in enriched_tasks:
        status = effective_task_status(task)
        status_counts[status] = status_counts.get(status, 0) + 1
    def employee_task_owner_actions(task: dict) -> list[dict]:
        task_id = str(task.get("id") or "")
        attempt_id = str(task.get("latest_attempt_id") or "")
        status = effective_task_status(task)

        def action(action_id: str, label: str, api: str = "", *, method: str = "GET", requires_owner_approval: bool = False, dry_run_default: bool = True, dangerous: bool = False) -> dict:
            return {
                "id": action_id,
                "label": label,
                "api": api,
                "method": method,
                "task_id": task_id,
                "attempt_id": attempt_id,
                "requires_owner_approval": requires_owner_approval,
                "dry_run_default": dry_run_default,
                "dangerous": dangerous,
            }

        if status in {"submitted", "claimed", "running", "retrying"}:
            return [
                action("view_task", "View Task", f"/v1/tasks/{task_id}"),
                action("view_trace", "View Trace"),
            ]
        if status in {"blocked", "stale", "waiting_approval"}:
            return [
                action("view_task", "View Task", f"/v1/tasks/{task_id}"),
                action("request_correction", "Request Correction", f"/v1/tasks/{task_id}/correct", method="POST", requires_owner_approval=True),
                action("retry", "Retry", f"/v1/tasks/{task_id}/retry", method="POST", requires_owner_approval=True),
                action("reassign", "Reassign", f"/v1/tasks/{task_id}/reassign", method="POST", requires_owner_approval=True),
            ]
        if status in {"failed", "cancelled"}:
            return [
                action("view_task", "View Task", f"/v1/tasks/{task_id}"),
                action("retry", "Retry", f"/v1/tasks/{task_id}/retry", method="POST", requires_owner_approval=True),
                action("reassign", "Reassign", f"/v1/tasks/{task_id}/reassign", method="POST", requires_owner_approval=True),
            ]
        if status in {"completed", "done", "success"} and bool(task.get("has_final_evidence")):
            return [
                action("review_evidence", "Review Evidence", f"/v1/tasks/{task_id}"),
                action("view_trace", "View Trace"),
            ]
        if status in {"completed", "done", "success"}:
            return [
                action("review_task", "Review Task", f"/v1/tasks/{task_id}"),
                action("view_trace", "View Trace"),
            ]
        return [action("view_task", "View Task", f"/v1/tasks/{task_id}")]

    for task in enriched_tasks:
        task["owner_actions"] = employee_task_owner_actions(task)
    attention_counts = {
        "running_tasks": sum(1 for item in enriched_tasks if effective_task_status(item) in {"submitted", "claimed", "running", "retrying"}),
        "blocked_tasks": sum(1 for item in enriched_tasks if effective_task_status(item) in {"blocked", "stale", "waiting_approval"}),
        "failed_tasks": sum(1 for item in enriched_tasks if effective_task_status(item) in {"failed", "cancelled"}),
        "failed_tool_calls": sum(int(item.get("failed_tool_call_count") or 0) for item in enriched_tasks),
        "completion_invalid_tasks": sum(
            1
            for item in enriched_tasks
            if effective_task_status(item) in {"completed", "done", "success"} and not bool(item.get("has_final_evidence"))
        ),
        "tasks_with_final_evidence": sum(1 for item in enriched_tasks if bool(item.get("has_final_evidence"))),
    }
    work_history_summary = (
        f"{len(enriched_tasks)} tasks · {attention_counts['running_tasks']} running · "
        f"{attention_counts['blocked_tasks']} blocked · {attention_counts['failed_tasks']} failed · "
        f"{attention_counts['failed_tool_calls']} failed tool calls · {attention_counts['tasks_with_final_evidence']} final evidence"
    )
    current_tasks = [dict(item) for item in enriched_tasks if str(item.get("status", "")).lower() in {"submitted", "claimed", "running", "waiting_approval", "blocked", "stale", "retrying"}][:10]
    recent_tasks = [dict(item) for item in enriched_tasks[:10]]
    active_attempt_statuses = {"starting", "running", "correcting"}
    current_attempt = next((dict(item) for item in attempt_rows if str(item.get("status") or "") in active_attempt_statuses), {})
    current_task_id = str(current_attempt.get("task_id") or (current_tasks[0].get("id") if current_tasks else "") or "")
    current_task = next((dict(item) for item in enriched_tasks if str(item.get("id") or "") == current_task_id), {})
    latest_progress: dict = {}
    if current_task_id:
        progress_events = rows(
            conn,
            """
            SELECT *
            FROM company_events
            WHERE task_id = ? AND event_type = 'task.progress'
            ORDER BY created_at DESC
            LIMIT 20
            """,
            (current_task_id,),
        )
        for event in progress_events:
            try:
                payload = json.loads(event.get("payload_json", "{}") or "{}")
            except json.JSONDecodeError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            if current_attempt and str(payload.get("attempt_id") or "") != str(current_attempt.get("attempt_id") or ""):
                continue
            latest_progress = {
                "event_id": event.get("id", ""),
                "created_at": event.get("created_at", ""),
                "attempt_id": payload.get("attempt_id", ""),
                "progress_state": payload.get("progress_state", ""),
                "progress_layer": payload.get("progress_layer", ""),
                "progress_label": payload.get("progress_label", ""),
                "message": payload.get("message", ""),
                "progress": payload.get("progress"),
            }
            break
    current_trace_id = ""
    if current_attempt:
        current_trace_id = str(current_attempt.get("trace_id") or "")
    elif current_task_id:
        current_trace_id = trace_id_for_task(conn, current_task_id)
    current_activity = {
        "employee_id": employee_id,
        "task_id": current_task_id,
        "task_title": str(current_task.get("title") or ""),
        "task_status": str(current_task.get("status") or ""),
        "attempt_id": str(current_attempt.get("attempt_id") or ""),
        "attempt_status": str(current_attempt.get("status") or ""),
        "trace_id": current_trace_id,
        "active_task_count": len(current_tasks),
        "latest_progress": latest_progress,
    }
    if current_attempt:
        current_activity.update(long_task_state_for_attempt(current_attempt))
    failed_tool_call_count = sum(1 for item in tool_calls if str(item.get("status") or "") in {"failed", "blocked", "cancelled"})
    final_evidence_count = sum(1 for item in evidence_records if bool(item.get("is_final")) or str(item.get("stage") or "") == "final")
    owner_next_action = "idle: assign a task when work is available"
    if current_task_id:
        owner_next_action = "monitor current task progress, tool calls, budget, and evidence"
    current_state = str(current_activity.get("long_task_state") or current_activity.get("task_status") or current_activity.get("attempt_status") or "")
    if current_state in {"blocked", "failed", "stale", "heartbeat_stale", "progress_stagnant"}:
        owner_next_action = "review blocker, then correct, cancel, retry, or reassign"
    operational_summary = {
        "employee_id": employee_id,
        "current_task_id": current_task_id,
        "current_task_title": current_activity.get("task_title", ""),
        "current_attempt_id": str(current_attempt.get("attempt_id") or ""),
        "current_trace_id": current_trace_id,
        "current_state": current_state or "idle",
        "task_count": len(task_rows),
        "current_task_count": len(current_tasks),
        "attempt_count": len(attempt_rows),
        "runtime_session_count": len(runtime_sessions),
        "tool_call_count": len(tool_calls),
        "failed_tool_call_count": failed_tool_call_count,
        "budget_event_count": len(budget_events),
        "budget_total": employee_budget_summary.get("total_amount", 0),
        "budget_currency": employee_budget_summary.get("currency", "USD"),
        "token_input": employee_budget_summary.get("token_input", 0),
        "token_output": employee_budget_summary.get("token_output", 0),
        "runtime_seconds": employee_budget_summary.get("runtime_seconds", 0),
        "evidence_count": len(evidence_records),
        "final_evidence_count": final_evidence_count,
        "owner_next_action": owner_next_action,
    }
    ceo_work_contract = employee_ceo_work_contract(
        employee=profile,
        current_activity=current_activity,
        operational_summary=operational_summary,
        runtime_sessions=runtime_sessions,
        tool_calls=tool_calls,
        budget_summary=employee_budget_summary,
        evidence_records=evidence_records,
    )
    return {
        "employee": profile,
        "profile": file_profile,
        "capabilities": capabilities,
        "permissions": permissions,
        "sandbox_profile": sandbox_profile,
        "heartbeat": heartbeat,
        "work_history": {
            "current_tasks": current_tasks,
            "recent_tasks": recent_tasks,
            "tasks": enriched_tasks,
            "status_counts": status_counts,
            "attention_counts": attention_counts,
            "summary": work_history_summary,
            "counts": {
                "tasks": len(task_rows),
                "current_tasks": len(current_tasks),
                "attempts": len(attempt_rows),
                "runtime_sessions": len(runtime_sessions),
                "tool_calls": len(tool_calls),
                "budget_events": len(budget_events),
                "evidence_records": len(evidence_records),
            },
        },
        "attempts": attempt_rows,
        "current_activity": current_activity,
        "operational_summary": operational_summary,
        "ceo_work_contract": ceo_work_contract,
        "runtime_sessions": runtime_sessions,
        "tool_calls": tool_calls,
        "budget_events": budget_events,
        "budget_summary": employee_budget_summary,
        "evidence_records": evidence_records,
        "files": {key: str(value) for key, value in paths.items()},
    }


def cmd_employee_show(args: argparse.Namespace) -> int:
    conn = connect()
    employee_id = args.id or args.employee
    emit({"ok": True, **employee_file_bundle(conn, employee_id)})
    return 0


def parse_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def cmd_employee_capabilities(args: argparse.Namespace) -> int:
    conn = connect()
    bundle = employee_file_bundle(conn, args.id)
    path = Path(bundle["files"]["capabilities"])
    capabilities = bundle["capabilities"]
    changed = False
    if args.set_skills:
        capabilities["skills"] = parse_csv(args.set_skills)
        changed = True
    if args.add_skill:
        skills = list(capabilities.get("skills", []))
        for skill in args.add_skill:
            if skill not in skills:
                skills.append(skill)
        capabilities["skills"] = skills
        changed = True
    if args.set_tools:
        capabilities["tools"] = parse_csv(args.set_tools)
        changed = True
    if args.add_tool:
        tools = list(capabilities.get("tools", []))
        for tool in args.add_tool:
            if tool not in tools:
                tools.append(tool)
        capabilities["tools"] = tools
        changed = True
    if args.set_task_types:
        capabilities["preferred_task_types"] = parse_csv(args.set_task_types)
        changed = True
    if changed:
        capabilities["updated_at"] = now()
        path.write_text(json.dumps(capabilities, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        audit(conn, "companyctl", "employee.capabilities.update", args.id, {"file": str(path), "capabilities": capabilities})
    emit({"ok": True, "changed": changed, "agent": resolve_employee_alias(args.id), "capabilities": capabilities, "file": str(path)})
    return 0


def cmd_employee_permissions(args: argparse.Namespace) -> int:
    conn = connect()
    bundle = employee_file_bundle(conn, args.id)
    path = Path(bundle["files"]["permissions"])
    permissions = bundle["permissions"]
    changed = False
    for key in ("can_submit_tasks", "can_claim_tasks", "can_modify_kernel"):
        value = getattr(args, key)
        if value != "keep":
            permissions[key] = value == "true"
            changed = True
    if args.requires_approval_for:
        permissions["requires_approval_for"] = parse_csv(args.requires_approval_for)
        changed = True
    if changed:
        permissions["updated_at"] = now()
        path.write_text(json.dumps(permissions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        audit(conn, "companyctl", "employee.permissions.update", args.id, {"file": str(path), "permissions": permissions})
    emit({"ok": True, "changed": changed, "agent": resolve_employee_alias(args.id), "permissions": permissions, "file": str(path)})
    return 0


def match_employee_score(bundle: dict, required_skills: list[str], preferred_tools: list[str], task_type: str, runtime: str, role: str) -> dict:
    employee = bundle["employee"]
    capabilities = bundle["capabilities"]
    permissions = bundle["permissions"]
    if is_human_owner_employee(employee):
        return {"score": -1, "reasons": ["human owner not schedulable"]}
    if employee.get("status") != "active":
        return {"score": -1, "reasons": ["employee inactive"]}
    if not permissions.get("can_claim_tasks", True):
        return {"score": -1, "reasons": ["cannot claim tasks"]}
    score = 0
    reasons = []
    skills = set(str(item).lower() for item in capabilities.get("skills", []))
    tools = set(str(item).lower() for item in capabilities.get("tools", []))
    task_types = set(str(item).lower() for item in capabilities.get("preferred_task_types", []))
    for skill in required_skills:
        key = skill.lower()
        if key in skills:
            score += 5
            reasons.append(f"skill:{skill}")
        else:
            score -= 2
            reasons.append(f"missing_skill:{skill}")
    for tool in preferred_tools:
        key = tool.lower()
        if key in tools:
            score += 2
            reasons.append(f"tool:{tool}")
    if task_type:
        if task_type.lower() in task_types:
            score += 3
            reasons.append(f"task_type:{task_type}")
        else:
            score -= 1
            reasons.append(f"nonpreferred_task_type:{task_type}")
    if runtime:
        if employee.get("runtime") == runtime:
            score += 4
            reasons.append(f"runtime:{runtime}")
        else:
            score -= 3
            reasons.append(f"runtime_mismatch:{employee.get('runtime')}")
    if role:
        if employee.get("role") == role or capabilities.get("role") == role:
            score += 2
            reasons.append(f"role:{role}")
    if capabilities.get("handoff", {}).get("can_receive_tasks", True):
        score += 1
    return {"score": score, "reasons": reasons}


def employee_matches(conn: sqlite3.Connection, args: argparse.Namespace) -> list[dict]:
    required_skills = parse_csv(getattr(args, "skills", ""))
    preferred_tools = parse_csv(getattr(args, "tools", ""))
    task_type = getattr(args, "task_type", "")
    runtime = getattr(args, "runtime", "")
    role = getattr(args, "role", "")
    candidates = []
    for row in conn.execute("SELECT id FROM employees ORDER BY id").fetchall():
        bundle = employee_file_bundle(conn, row["id"])
        if is_human_owner_employee(bundle["employee"]):
            continue
        decision = match_employee_score(bundle, required_skills, preferred_tools, task_type, runtime, role)
        if decision["score"] < 0 and not getattr(args, "include_unavailable", False):
            continue
        candidates.append(
            {
                "agent": bundle["employee"]["id"],
                "name": bundle["employee"]["name"],
                "role": bundle["employee"]["role"],
                "runtime": bundle["employee"]["runtime"],
                "score": decision["score"],
                "reasons": decision["reasons"],
                "skills": bundle["capabilities"].get("skills", []),
                "tools": bundle["capabilities"].get("tools", []),
                "preferred_task_types": bundle["capabilities"].get("preferred_task_types", []),
            }
        )
    candidates.sort(key=lambda item: (-item["score"], item["agent"]))
    limit = int(getattr(args, "limit", 0) or 0)
    return candidates[:limit] if limit else candidates


def cmd_employee_match(args: argparse.Namespace) -> int:
    conn = connect()
    matches = employee_matches(conn, args)
    emit({"ok": True, "matches": matches})
    return 0


def upsert_employee(conn: sqlite3.Connection, employee_id: str, name: str, role: str, runtime: str, workspace: str, *, dry_run: bool) -> dict:
    profile = {
        "id": employee_id,
        "name": name,
        "role": role,
        "runtime": runtime,
        "workspace": workspace,
        "created_at": now(),
    }
    files = write_employee_files(employee_id, profile, dry_run=dry_run)
    if dry_run:
        return {"employee": profile, "files": files}
    ensure_runtime(conn, runtime)
    ts = now()
    conn.execute(
        """
        INSERT INTO employees(id, name, role, runtime, workspace, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'candidate', ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          name = excluded.name,
          role = excluded.role,
          runtime = excluded.runtime,
          workspace = excluded.workspace,
          status = CASE WHEN employees.status = 'active' THEN employees.status ELSE 'candidate' END,
          updated_at = excluded.updated_at
        """,
        (employee_id, name, role, runtime, workspace, ts, ts),
    )
    communication = sync_employee_name_alias(employee_id, name, dry_run=False)
    conn.commit()
    audit(conn, "companyctl", "employee.upsert", employee_id, {**profile, "communication": communication})
    return {"employee": profile, "files": files, "communication": communication}


def verified_direct_evidence_dir(employee_id: str) -> Path:
    return STATE_DIR / "employee-verification" / slug(employee_id)


def employee_has_verified_direct_evidence(employee_id: str) -> bool:
    latest = verified_direct_evidence_dir(employee_id) / "latest.json"
    if not latest.exists():
        return False
    try:
        payload = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(payload.get("ok") and int(payload.get("rounds_completed", 0) or 0) >= 2)


def direct_probe_body(agent_id: str, round_index: int) -> str:
    return f"员工通信验证第{round_index}轮：请只回复 {agent_id}_VERIFY_ROUND_{round_index}_OK"


def cmd_employee_set_unavailable(args: argparse.Namespace) -> int:
    conn = connect()
    result = mark_employee_unavailable(conn, resolve_employee_alias(args.id, strict=True), args.reason)
    conn.commit()
    emit({"ok": True, **result})
    return 0


def cmd_employee_verify_direct(args: argparse.Namespace) -> int:
    conn = connect()
    employee_id = resolve_employee_alias(args.id, strict=True)
    row = conn.execute("SELECT * FROM employees WHERE id = ?", (employee_id,)).fetchone()
    if not row:
        conn.close()
        emit({"ok": False, "error": "unknown employee", "employee_id": employee_id})
        return 1
    source = resolve_employee_alias(args.source, strict=True)
    require_employee(conn, source)
    rounds = int(args.rounds)
    if rounds < 2 or rounds > 4:
        conn.close()
        emit({"ok": False, "error": "rounds must be between 2 and 4", "rounds": rounds})
        return 2
    results = []
    ok = True
    for index in range(1, rounds + 1):
        expected = f"{employee_id}_VERIFY_ROUND_{index}_OK"
        direct_args = argparse.Namespace(
            source=source,
            target=employee_id,
            body=direct_probe_body(employee_id, index),
            message_id=f"verify-{slug(employee_id)}-{datetime.now().strftime('%Y%m%d%H%M%S')}-r{index}",
            timeout=args.timeout,
            session_key="",
            deliver=False,
            reply_channel="",
            reply_account="",
            reply_to="",
        )
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = cmd_message_direct(direct_args)
        try:
            payload = json.loads(captured.getvalue())
        except json.JSONDecodeError:
            payload = {"ok": False, "error": "invalid direct response", "raw": captured.getvalue()}
        reply = str(payload.get("reply") or "")
        activation_eligible = payload.get("activation_eligible", True) is not False
        passed = code == 0 and payload.get("ok") is True and expected in reply and bool(payload.get("receipt")) and activation_eligible
        if not passed:
            ok = False
        results.append({"round": index, "expected": expected, "passed": passed, "response": payload})
        if not passed and not args.continue_on_failure:
            break
    report = {
        "ok": ok and len(results) == rounds,
        "employee_id": employee_id,
        "source": source,
        "rounds_required": rounds,
        "rounds_completed": sum(1 for item in results if item["passed"]),
        "results": results,
        "generated_at": now(),
        "activation_allowed": ok and len(results) == rounds,
        "rule": "active status requires 2-4 verified direct rounds with receipt; heartbeat or generated brief is not enough",
    }
    evidence_dir = verified_direct_evidence_dir(employee_id)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    report_path = evidence_dir / f"verify-direct-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    latest_path = evidence_dir / "latest.json"
    report["evidence"] = {"json": str(report_path), "latest": str(latest_path)}
    if args.activate and report["activation_allowed"]:
        ts = now()
        conn.execute("UPDATE employees SET status = 'active', updated_at = ? WHERE id = ?", (ts, employee_id))
        conn.commit()
        profile_path = employee_paths(employee_id)["profile"]
        profile = load_json_or_default(profile_path, {})
        profile.update({"status": "active", "updated_at": ts})
        profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report["activated"] = True
    else:
        report["activated"] = False
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    latest_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit(conn, "companyctl", "employee.verify_direct", employee_id, report)
    conn.close()
    emit(report)
    return 0 if report["ok"] else 1


def runtime_verification_body(employee_id: str) -> str:
    readme_path = ROOT / "README.md"
    dashboard_path = ROOT / "company_kernel" / "company_dashboard.py"
    return "\n".join(
        [
            "员工上岗执行验收。请在当前项目内做只读检查，不要改文件。",
            f"任务：读取 {readme_path} 和 {dashboard_path}，判断你能否作为 Company Kernel 员工执行前端/GUI/代码检查任务。",
            "必须按以下字段逐行回复，不能只回 token：",
            "status: working|done|blocked",
            "current_action: <你实际读取/检查了什么>",
            "changed_files: -",
            "verification_run: <你实际执行或完成的只读检查>",
            "browser_check: <如果没跑浏览器就填 ->",
            "blocker: <没有阻塞填 ->",
            "eta: -",
            f"employee_id: {employee_id}",
        ]
    )


def parse_runtime_reply_fields(reply: str) -> dict[str, str]:
    fields = {}
    for line in str(reply or "").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        if key and key not in fields:
            fields[key] = value.strip()
    return fields


def runtime_reply_passed(reply: str) -> tuple[bool, str, dict[str, str]]:
    fields = parse_runtime_reply_fields(reply)
    status = fields.get("status", "").lower()
    blocker = fields.get("blocker", "")
    verification_run = fields.get("verification_run", "")
    if status != "done":
        return False, f"runtime_reply_not_done:{status or 'missing'}", fields
    if blocker and blocker != "-":
        return False, "runtime_reply_has_blocker", fields
    if not verification_run or verification_run == "-":
        return False, "runtime_reply_missing_verification_run", fields
    return True, "runtime_reply_done_with_evidence", fields


def cmd_employee_recover(args: argparse.Namespace) -> int:
    """Auto-heal: re-verify employees that were auto-downgraded to candidate due to an
    unavailability/runtime failure, and reactivate the ones that now pass verification (clearing
    the unavailable reason + resuming comms). A transient outage — e.g. the gemini proxy not being
    up at boot — therefore self-recovers instead of leaving the employee offline forever.
    Skips employees with profile auto_recover=false (e.g. claude, managed manually)."""
    conn = connect()
    candidates = []
    for emp in rows(conn, "SELECT * FROM employees WHERE status = 'candidate' ORDER BY id"):
        if is_human_owner_employee(emp):
            continue
        profile = load_json_or_default(employee_paths(emp["id"])["profile"], {})
        if not str(profile.get("unavailable_reason") or ""):
            continue  # intentionally candidate (never downgraded) — leave alone
        if profile.get("auto_recover", True) is False:
            continue  # opted out of auto-recovery (owner manages it)
        if emp["runtime"] not in KNOWN_RUNTIMES:
            continue  # unknown runtime can't be verified; needs registration first
        candidates.append(emp["id"])
    recovered, still_down = [], []
    for cid in candidates[: max(1, int(args.max))]:
        vargs = argparse.Namespace(id=cid, source=args.source, timeout=args.timeout, activate=True)
        cap = io.StringIO()
        try:
            with contextlib.redirect_stdout(cap):
                cmd_employee_verify_runtime(vargs)
            report = json.loads(cap.getvalue() or "{}")
        except (SystemExit, json.JSONDecodeError):
            report = {}
        if report.get("ok") and report.get("activated"):
            profile_path = employee_paths(cid)["profile"]
            profile = load_json_or_default(profile_path, {})
            profile.pop("unavailable_reason", None)
            profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            set_employee_communication_enabled(cid, True, dry_run=False)
            recovered.append(cid)
        else:
            still_down.append(cid)
    emit({"ok": True, "candidates": candidates, "recovered": recovered, "still_unavailable": still_down})
    return 0


def cmd_employee_verify_runtime(args: argparse.Namespace) -> int:
    conn = connect()
    employee_id = resolve_employee_alias(args.id, strict=True)
    row = conn.execute("SELECT * FROM employees WHERE id = ?", (employee_id,)).fetchone()
    if not row:
        conn.close()
        emit({"ok": False, "error": "unknown employee", "employee_id": employee_id})
        return 1
    source = resolve_employee_alias(args.source, strict=True)
    require_employee(conn, source)
    direct_args = argparse.Namespace(
        source=source,
        target=employee_id,
        body=runtime_verification_body(employee_id),
        message_id=f"verify-runtime-{slug(employee_id)}-{datetime.now().strftime('%Y%m%d%H%M%S')}",
        timeout=args.timeout,
        session_key="",
        deliver=False,
        reply_channel="",
        reply_account="",
        reply_to="",
    )
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        code = cmd_message_direct(direct_args)
    try:
        payload = json.loads(captured.getvalue())
    except json.JSONDecodeError:
        payload = {"ok": False, "error": "invalid direct response", "raw": captured.getvalue()}
    reply = str(payload.get("reply") or "")
    structured_ok, structured_reason, structured_fields = runtime_reply_passed(reply)
    verification = {
        "type": "execution_evidence",
        "passed": code == 0 and payload.get("ok") is True and payload.get("activation_eligible") is True and bool(payload.get("receipt")) and structured_ok,
        "reason": structured_reason,
        "parsed_fields": structured_fields,
        "response": payload,
    }
    report = {
        "ok": bool(verification["passed"]),
        "employee_id": employee_id,
        "source": source,
        "generated_at": now(),
        "verification": verification,
        "activation_allowed": bool(verification["passed"]),
        "rule": "runtime activation requires structured execution evidence; exact-token smoke is not enough",
    }
    evidence_dir = verified_direct_evidence_dir(employee_id)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    report_path = evidence_dir / f"verify-runtime-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    latest_path = evidence_dir / "latest-runtime.json"
    report["evidence"] = {"json": str(report_path), "latest": str(latest_path)}
    if args.activate and report["activation_allowed"]:
        ts = now()
        conn.execute("UPDATE employees SET status = 'active', updated_at = ? WHERE id = ?", (ts, employee_id))
        conn.commit()
        profile_path = employee_paths(employee_id)["profile"]
        profile = load_json_or_default(profile_path, {})
        profile.update({"status": "active", "updated_at": ts})
        profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report["activated"] = True
    else:
        report["activated"] = False
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    latest_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit(conn, "companyctl", "employee.verify_runtime", employee_id, report)
    conn.close()
    emit(report)
    return 0 if report["ok"] else 1


def cmd_employee_import_openclaw(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    if not config_path.exists():
        raise SystemExit(f"openclaw config not found: {config_path}")
    obj = json.loads(config_path.read_text(encoding="utf-8"))
    agents = obj.get("agents", {}).get("list", [])
    conn = connect()
    imported = []
    for agent in agents:
        agent_id = str(agent.get("id") or "").strip()
        if not agent_id:
            continue
        name = str(agent.get("identityName") or agent.get("name") or agent_id)
        workspace = str(agent.get("workspace") or "")
        role = "operator" if agent_id == "main" else "business-agent"
        imported.append(upsert_employee(conn, agent_id, name, role, "openclaw", workspace, dry_run=args.dry_run))
    emit({"ok": True, "dry_run": args.dry_run, "count": len(imported), "imported": imported})
    return 0


def load_openclaw_config_agents(config_path: Path) -> dict[str, dict]:
    if not config_path.exists():
        return {}
    obj = json.loads(config_path.read_text(encoding="utf-8"))
    agents = {}
    for agent in obj.get("agents", {}).get("list", []):
        agent_id = str(agent.get("id") or "").strip()
        if not agent_id:
            continue
        agents[agent_id] = dict(agent)
    return agents


def openclaw_employee_sync_plan(config_path: Path) -> list[dict]:
    config_agents = load_openclaw_config_agents(config_path)
    inventory = openclaw_runtime_inventory()
    planned: dict[str, dict] = {}
    for agent_id, agent in config_agents.items():
        name = str(agent.get("identityName") or agent.get("name") or agent_id)
        workspace = str(agent.get("workspace") or openclaw_root())
        planned[agent_id] = {
            "id": agent_id,
            "name": name,
            "role": "operator" if agent_id == "main" else "business-agent",
            "runtime": "openclaw",
            "workspace": workspace,
            "status": "active",
            "source": "openclaw_config",
        }
    # Only the openclaw config (agents.list) is the source of truth for who is a real agent.
    # A bare leftover directory under openclaw/agents/ (e.g. gpt5, claude-code, car-rental) is NOT
    # an employee — registering every dir produced phantom roster entries and stuck verify tasks.
    for agent in inventory.get("agent_dirs", {}).values():
        agent_id = str(agent.get("id") or "").strip()
        if not agent_id or agent_id in planned:
            continue
        if agent_id not in config_agents:
            continue  # directory exists but isn't a configured agent — skip, don't invent an employee
        planned[agent_id] = {
            "id": agent_id,
            "name": agent_id,
            "role": "runtime-agent",
            "runtime": "openclaw",
            "workspace": str(Path(agent.get("path") or openclaw_root())),
            "status": "candidate",
            "source": "openclaw_runtime_dir",
        }
    return sorted(planned.values(), key=lambda item: (item["status"] != "active", item["id"]))


def upsert_employee_with_status(conn: sqlite3.Connection, employee: dict, *, dry_run: bool) -> dict:
    status = str(employee.get("status") or "candidate")
    if status not in {"active", "candidate", "archived"}:
        status = "candidate"
    profile = {
        "id": employee["id"],
        "name": employee["name"],
        "role": employee["role"],
        "runtime": employee["runtime"],
        "workspace": employee["workspace"],
        "status": status,
        "source": employee.get("source", ""),
        "created_at": now(),
    }
    files = write_employee_files(employee["id"], profile, dry_run=dry_run)
    if dry_run:
        return {"employee": profile, "files": files}
    ensure_runtime(conn, employee["runtime"])
    ts = now()
    conn.execute(
        """
        INSERT INTO employees(id, name, role, runtime, workspace, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          name = excluded.name,
          role = excluded.role,
          runtime = excluded.runtime,
          workspace = excluded.workspace,
          status = CASE
            WHEN excluded.status = 'active' THEN 'active'
            WHEN employees.status = 'active' THEN employees.status
            ELSE excluded.status
          END,
          updated_at = excluded.updated_at
        """,
        (employee["id"], employee["name"], employee["role"], employee["runtime"], employee["workspace"], status, ts, ts),
    )
    communication = sync_employee_name_alias(employee["id"], employee["name"], dry_run=False)
    audit(conn, "companyctl", "employee.openclaw_sync", employee["id"], {**profile, "communication": communication})
    return {"employee": profile, "files": files, "communication": communication}


def cmd_employee_sync_openclaw_runtime(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    plan = openclaw_employee_sync_plan(config_path)
    conn = connect()
    synced = []
    skipped = []
    try:
        existing = {row["id"]: dict(row) for row in conn.execute("SELECT * FROM employees")}
        for employee in plan:
            if args.active_only and employee["status"] != "active":
                continue
            current = existing.get(employee["id"])
            if current and current.get("runtime") != "openclaw" and employee.get("source") == "openclaw_runtime_dir":
                skipped.append({"id": employee["id"], "reason": f"existing_runtime_{current.get('runtime')}"})
                continue
            synced.append(upsert_employee_with_status(conn, employee, dry_run=args.dry_run))
        if not args.dry_run:
            conn.commit()
    finally:
        conn.close()
    counts = {
        "active": sum(1 for item in plan if item["status"] == "active"),
        "candidate": sum(1 for item in plan if item["status"] == "candidate"),
        "synced": len(synced),
        "skipped": len(skipped),
    }
    emit({"ok": True, "dry_run": args.dry_run, "config": str(config_path), "counts": counts, "employees": [item["employee"] for item in synced], "skipped": skipped})
    return 0


def sync_openclaw_heartbeats(conn: sqlite3.Connection, *, dry_run: bool) -> dict:
    inventory = openclaw_runtime_inventory(conn)
    agent_dirs = inventory.get("agent_dirs", {})
    spools = inventory.get("telegram_spools", {})
    employees = rows(conn, "SELECT id, status FROM employees WHERE runtime = 'openclaw' ORDER BY id")
    synced = []
    skipped = []
    for employee in employees:
        employee_id = employee["id"]
        if employee["status"] != "active":
            skipped.append({"id": employee_id, "reason": f"status_{employee['status']}"})
            continue
        agent = agent_dirs.get(employee_id) or agent_dirs.get(employee_id.replace("_", "-"))
        spool = spools.get(employee_id) or spools.get(employee_id.replace("-", "_"))
        session_count = int((agent or {}).get("session_count", 0) or 0)
        spool_exists = bool((spool or {}).get("exists"))
        if not agent and not spool:
            skipped.append({"id": employee_id, "reason": "openclaw_runtime_not_found"})
            continue
        metadata = {
            "source": "openclaw-runtime-sync",
            "runtime_agent_found": bool(agent),
            "telegram_spool_found": spool_exists,
            "session_count": session_count,
            "spool_pending": int((spool or {}).get("pending", 0) or 0),
            "spool_processing": int((spool or {}).get("processing", 0) or 0),
            "note": "Read-only heartbeat derived from OpenClaw runtime inventory; it does not prove task completion.",
        }
        if not dry_run:
            heartbeat_internal(conn, employee_id, metadata)
        synced.append({"id": employee_id, **metadata})
    return {"ok": True, "dry_run": dry_run, "synced": synced, "skipped": skipped, "counts": {"synced": len(synced), "skipped": len(skipped)}}


def cmd_employee_sync_openclaw_heartbeats(args: argparse.Namespace) -> int:
    conn = connect()
    try:
        result = sync_openclaw_heartbeats(conn, dry_run=args.dry_run)
    finally:
        conn.close()
    emit(result)
    return 0


def update_employee_communication_profile(
    *,
    employee_id: str,
    name: str,
    role: str,
    alias: str,
    can_talk_to: list[str],
    can_assign_to: list[str],
    channel: str,
    handoff_mode: str,
    default_user_reply_channel: str,
    default_user_reply_account: str,
    default_user_reply_to: str,
    default_user_reply_deliver: bool | None,
    dry_run: bool,
) -> dict:
    config = load_communication_config()
    config.setdefault("version", 1)
    config.setdefault("policy", {"mode": "open"})
    aliases = config.setdefault("aliases", {})
    if alias:
        aliases[alias] = employee_id
    for name_alias in communication_name_aliases(employee_id, name):
        aliases[name_alias] = employee_id
    employees = config.setdefault("employees", {})
    profile = employees.setdefault(employee_id, {})
    profile.update(
        {
            "display_name": name,
            "role": role,
            "can_talk_to": [resolve_employee_alias(item) for item in can_talk_to],
            "can_assign_to": [resolve_employee_alias(item) for item in can_assign_to],
            "handoff_mode": handoff_mode,
        }
    )
    if default_user_reply_channel:
        profile["default_user_reply_channel"] = default_user_reply_channel
    if default_user_reply_account:
        profile["default_user_reply_account"] = default_user_reply_account
    if default_user_reply_to:
        profile["default_user_reply_to"] = default_user_reply_to
    if default_user_reply_deliver is not None:
        profile["default_user_reply_deliver"] = bool(default_user_reply_deliver)
    if channel:
        channels = config.setdefault("channels", {})
        channel_obj = channels.setdefault(channel, {"participants": [], "max_rounds_without_task": 20, "on_task_done": "continue_workflow"})
        participants = [resolve_employee_alias(item) for item in channel_obj.get("participants", [])]
        if employee_id not in participants:
            participants.append(employee_id)
        channel_obj["participants"] = participants
    if not dry_run:
        write_communication_config(config)
    return config


def set_employee_communication_enabled(employee_id: str, enabled: bool, *, dry_run: bool = False) -> dict:
    employee_id = resolve_employee_alias(employee_id)
    config = load_communication_config()
    config.setdefault("version", 1)
    config.setdefault("policy", {"mode": "open"})
    employees = config.setdefault("employees", {})
    profile = employees.setdefault(employee_id, {})
    if enabled:
        profile.pop("communication_paused", None)
        profile.pop("communication_paused_at", None)
    else:
        profile["communication_paused"] = True
        profile["communication_paused_at"] = now()
    if not dry_run:
        write_communication_config(config)
    return {
        "ok": True,
        "agent": employee_id,
        "communication_enabled": enabled,
        "communication_paused": not enabled,
        "profile": profile,
        "file": str(COMMUNICATIONS_PATH),
        "dry_run": dry_run,
    }


def mark_employee_unavailable(conn: sqlite3.Connection, employee_id: str, reason: str) -> dict:
    employee_id = resolve_employee_alias(employee_id)
    ts = now()
    row = conn.execute("SELECT status FROM employees WHERE id = ?", (employee_id,)).fetchone()
    previous_status = str(row["status"] if row else "")
    if previous_status != "active":
        audit(conn, "companyctl", "employee.unavailable_probe_failed", employee_id, {"reason": reason, "status": previous_status})
        return {"agent": employee_id, "status": previous_status or "unknown", "communication_paused": False, "reason": reason, "downgraded": False}
    conn.execute("UPDATE employees SET status = 'candidate', updated_at = ? WHERE id = ? AND status = 'active'", (ts, employee_id))
    result = set_employee_communication_enabled(employee_id, False, dry_run=False)
    paths = employee_paths(employee_id)
    profile = load_json_or_default(paths["profile"], {})
    profile["status"] = "candidate"
    profile["unavailable_reason"] = reason
    profile["updated_at"] = ts
    paths["profile"].write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit(conn, "companyctl", "employee.unavailable", employee_id, {"reason": reason, "communication": result})
    return {"agent": employee_id, "status": "candidate", "communication_paused": True, "reason": reason, "downgraded": True}


def workspace_is_managed(path: Path) -> bool:
    resolved = path.expanduser().resolve()
    root = ROOT.resolve()
    if resolved == root:
        return False
    try:
        resolved.relative_to(root)
        return True
    except ValueError:
        return False


def scaffold_employee_workspace(employee_id: str, name: str, role: str, runtime: str, workspace: str) -> list[str]:
    workspace_path = Path(workspace).expanduser().resolve()
    if not workspace_is_managed(workspace_path):
        return []
    workspace_path.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    if runtime == "hermes":
        files = {
            "SOUL.md": "\n".join(
                [
                    f"# {name} Persona",
                    "",
                    f"You are {name}, acting as `{role}` in the Super AI Company.",
                    "Use Company Kernel for task state, evidence, approvals, and communication.",
                    "",
                ]
            ),
            "AGENTS.md": "\n".join(
                [
                    f"# {name} Collaboration Rules",
                    "",
                    "- Communicate through `companyctl message` and `companyctl conversation`.",
                    "- Complete work with evidence or return a blocker.",
                    "- Request approval before high-risk external, payment, salary, penalty, or compensation actions.",
                    "",
                ]
            ),
        }
    elif runtime in {"codex", "local", "cursor"}:
        files = {
            "AGENTS.md": "\n".join(
                [
                    f"# {name} Execution Rules",
                    "",
                    "- Treat Company Kernel as the source of truth for tasks and evidence.",
                    "- Do not modify protected Company Kernel internals unless the task explicitly includes approval/RFC context.",
                    "- Run focused verification before marking tasks done.",
                    "",
                ]
            )
        }
    elif runtime == "openclaw":
        files = {
            "AGENTS.md": "\n".join(
                [
                    f"# {name} OpenClaw Employee Rules",
                    "",
                    "- Keep business state in the assigned OpenClaw workspace.",
                    "- Bridge work through Company Kernel tasks, messages, approvals, and evidence.",
                    "- Do not bypass high-risk approval gates.",
                    "",
                ]
            )
        }
    else:
        files = {
            "AGENTS.md": "\n".join(
                [
                    f"# {name} Employee Rules",
                    "",
                    "- Use Company Kernel for task state, messages, approvals, and evidence.",
                    "",
                ]
            )
        }
    for filename, content in files.items():
        target = workspace_path / filename
        if not target.exists():
            target.write_text(content, encoding="utf-8")
            written.append(str(target))
    return written


def cmd_employee_onboard(args: argparse.Namespace) -> int:
    conn = connect()
    try:
        require_runtime(conn, args.runtime)
    except SystemExit:
        conn.close()
        raise
    employee_id = resolve_employee_alias(args.id)
    result = upsert_employee(conn, employee_id, args.name, args.role, args.runtime, args.workspace, dry_run=args.dry_run)
    if args.default_user_reply_channel:
        result["employee"]["default_user_reply_channel"] = args.default_user_reply_channel
    if args.default_user_reply_account:
        result["employee"]["default_user_reply_account"] = args.default_user_reply_account
    if args.default_user_reply_to:
        result["employee"]["default_user_reply_to"] = args.default_user_reply_to
    if args.default_user_reply_deliver:
        result["employee"]["default_user_reply_deliver"] = True
    files = result["files"]
    capabilities = default_capabilities(result["employee"])
    if args.skills:
        capabilities["skills"] = parse_csv(args.skills)
    if args.tools:
        capabilities["tools"] = parse_csv(args.tools)
    if args.task_types:
        capabilities["preferred_task_types"] = parse_csv(args.task_types)
    if not args.dry_run:
        Path(files["capabilities"]).write_text(json.dumps(capabilities, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    scaffolded_files = [] if args.dry_run else scaffold_employee_workspace(employee_id, args.name, args.role, args.runtime, args.workspace)
    permissions = {
        "can_submit_tasks": not args.no_submit_tasks,
        "can_claim_tasks": not args.no_claim_tasks,
        "can_modify_kernel": args.can_modify_kernel,
        "requires_approval_for": parse_csv(args.requires_approval_for) or ["payment", "compensation", "salary", "penalty", "external_send"],
        "updated_at": now(),
    }
    if not args.dry_run:
        Path(files["permissions"]).write_text(json.dumps(permissions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    talk_targets = parse_csv(args.can_talk_to)
    assign_targets = parse_csv(args.can_assign_to)
    if args.open_communication:
        existing = [row["id"] for row in conn.execute("SELECT id FROM employees ORDER BY id").fetchall()] if not args.dry_run else []
        talk_targets = sorted(set(talk_targets + [item for item in existing if item != employee_id]))
        assign_targets = sorted(set(assign_targets + [item for item in existing if item != employee_id]))
    config = update_employee_communication_profile(
        employee_id=employee_id,
        name=args.name,
        role=args.role,
        alias=args.alias,
        can_talk_to=talk_targets,
        can_assign_to=assign_targets,
        channel=args.channel,
        handoff_mode=args.handoff_mode,
        default_user_reply_channel=args.default_user_reply_channel,
        default_user_reply_account=args.default_user_reply_account,
        default_user_reply_to=args.default_user_reply_to,
        default_user_reply_deliver=True if args.default_user_reply_deliver else None,
        dry_run=args.dry_run,
    )
    test_task = {}
    if args.create_test_task and not args.dry_run:
        inactive = require_active_employee(conn, employee_id, "employee.onboard.create_test_task")
        if inactive:
            test_task = {
                "blocked": True,
                "reason": "onboarding test task requires a verified active employee",
                "required_command": inactive["required_command"],
            }
        else:
            task = submit_task_internal(
                conn,
                source=args.test_source,
                target=employee_id,
                title=f"Onboarding test: {employee_id}",
                description="请领取此测试任务，写入 heartbeat，并用 evidence 或 blocker 回传结果。",
                priority="P3",
                task_id=args.test_task_id or f"task-onboard-{slug(employee_id)}",
                metadata={"onboarding": True},
            )
            test_task = task["task"]
    if not args.dry_run:
        audit(
            conn,
            "companyctl",
            "employee.onboard",
            employee_id,
            {
                "capabilities": capabilities,
                "permissions": permissions,
                "communication_file": str(COMMUNICATIONS_PATH),
                "test_task": test_task,
                "scaffolded_files": scaffolded_files,
            },
        )
    emit(
        {
            "ok": True,
            "dry_run": args.dry_run,
            "employee": result["employee"],
            "files": files,
            "capabilities": capabilities,
            "permissions": permissions,
            "communication": {
                "file": str(COMMUNICATIONS_PATH),
                "alias": args.alias,
                "can_talk_to": talk_targets,
                "can_assign_to": assign_targets,
                "channel": args.channel,
                "policy": config.get("policy", {}),
                "default_user_reply_channel": args.default_user_reply_channel,
                "default_user_reply_account": args.default_user_reply_account,
                "default_user_reply_to": args.default_user_reply_to,
                "default_user_reply_deliver": bool(args.default_user_reply_deliver),
            },
            "scaffolded_files": scaffolded_files,
            "test_task": test_task,
        }
    )
    return 0


def cmd_employee_offboard(args: argparse.Namespace) -> int:
    conn = connect()
    employee_id = resolve_employee_alias(args.id)
    row = conn.execute("SELECT * FROM employees WHERE id = ?", (employee_id,)).fetchone()
    if not row:
        conn.close()
        emit({"ok": False, "error": "unknown employee", "employee_id": employee_id})
        return 1
    employee = dict(row)
    managed_paths: list[Path] = []
    workspace = str(employee.get("workspace") or "")
    if workspace:
        workspace_path = Path(workspace).expanduser().resolve()
        if workspace_is_managed(workspace_path) and workspace_path.exists():
            managed_paths.append(workspace_path)
    employee_dir = EMPLOYEES_DIR / employee_id
    if employee_dir.exists():
        managed_paths.append(employee_dir)
    deleted_paths = sorted({str(path) for path in managed_paths})
    if args.dry_run:
        conn.close()
        emit({"ok": True, "dry_run": True, "action": "hard-delete" if args.hard_delete else "soft-delete", "employee": employee, "deleted_paths": deleted_paths})
        return 0
    ts = now()
    if args.hard_delete:
        import shutil

        for path in managed_paths:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        # Cancel the deleted employee's tasks: otherwise completed tasks whose evidence lived in the
        # now-removed employee dir trigger doctor's evidence_missing_on_disk and flag the kernel abnormal.
        conn.execute("UPDATE tasks SET status = 'cancelled', updated_at = ? WHERE target_agent = ? AND status != 'cancelled'", (ts, employee_id))
        conn.execute("DELETE FROM employees WHERE id = ?", (employee_id,))
        conn.execute("DELETE FROM heartbeats WHERE agent_id = ?", (employee_id,))
        action = "hard-delete"
    else:
        conn.execute("UPDATE employees SET status = 'archived', updated_at = ? WHERE id = ?", (ts, employee_id))
        action = "soft-delete"
    config = load_communication_config()
    config.get("employees", {}).pop(employee_id, None)
    for alias, target in list(config.get("aliases", {}).items()):
        if target == employee_id:
            config["aliases"].pop(alias, None)
    write_communication_config(config)
    conn.commit()
    audit(conn, "companyctl", "employee.offboard", employee_id, {"action": action, "employee": employee, "deleted_paths": deleted_paths})
    conn.close()
    emit({"ok": True, "action": action, "employee": employee, "deleted_paths": deleted_paths})
    return 0


def require_employee(conn: sqlite3.Connection, employee_id: str) -> None:
    if not conn.execute("SELECT 1 FROM employees WHERE id = ?", (employee_id,)).fetchone():
        raise SystemExit(f"unknown employee: {employee_id}")


def is_supervisor_employee(conn: sqlite3.Connection, employee_id: str) -> bool:
    row = conn.execute("SELECT role FROM employees WHERE id = ?", (employee_id,)).fetchone()
    if not row:
        return False
    return str(row["role"] or "").lower() in {"supervisor", "manager", "project-manager", "pm"}


def can_manage_task_recovery(conn: sqlite3.Connection, task: sqlite3.Row, actor: str) -> bool:
    participants = {task["source_agent"], task["target_agent"], task["claimed_by"]}
    if actor in participants or is_supervisor_employee(conn, actor):
        return True
    # The human owner can recover (reopen/retry/reassign) ANY task, not just ones they're in.
    row = conn.execute("SELECT * FROM employees WHERE id = ?", (actor,)).fetchone()
    return bool(row) and is_human_owner_employee(dict(row))


# A worker whose heartbeat went stale is effectively off duty even if its `status` field still says
# 'active'. Beyond this many minutes with no heartbeat, dispatch is refused so work isn't sent to a
# dead employee — the dispatcher learns immediately instead of the task rotting until the watchdog.
OFF_DUTY_HEARTBEAT_MINUTES = 15


def heartbeat_age_minutes(conn: sqlite3.Connection, employee_id: str) -> float | None:
    """Minutes since the employee's last heartbeat, or None if it has never heartbeated."""
    row = conn.execute("SELECT last_seen_at FROM heartbeats WHERE agent_id = ?", (employee_id,)).fetchone()
    if not row or not row["last_seen_at"]:
        return None
    try:
        last = datetime.fromisoformat(str(row["last_seen_at"]))
        cur = datetime.fromisoformat(now())
        return (cur - last).total_seconds() / 60
    except (ValueError, TypeError):
        return float("inf")  # a heartbeat row that won't parse is treated as off-duty (fail closed)


def require_active_employee(conn: sqlite3.Connection, employee_id: str, action: str) -> dict | None:
    row = conn.execute("SELECT * FROM employees WHERE id = ?", (employee_id,)).fetchone()
    if not row:
        raise SystemExit(f"unknown employee: {employee_id}")
    employee = dict(row)
    if employee.get("status") != "active":
        return {
            "ok": False,
            "error": "target employee is not active",
            "target": employee_id,
            "status": employee.get("status", ""),
            "action": action,
            "required_command": f"bin/companyctl employee verify-direct --id {employee_id} --from main --rounds 3 --activate",
        }
    if is_human_owner_employee(employee):
        return {"ok": False, "error": "human owner is not schedulable", "target": employee_id, "status": employee.get("status", ""), "action": action}
    # Real on-duty check (free, SQL): refuse if a once-heartbeating worker has gone silent past the
    # window — so colleagues don't dispatch to an employee that isn't actually running. A brand-new
    # employee that has never heartbeated is given the benefit of the doubt (no heartbeat row → skip).
    age = heartbeat_age_minutes(conn, employee_id)
    if age is not None and age > OFF_DUTY_HEARTBEAT_MINUTES:
        unparseable = age == float("inf")
        age_label = "心跳时间无法解析" if unparseable else f"{int(age)} 分钟前"
        return {
            "ok": False,
            "error": "target employee is off duty (stale heartbeat)",
            "target": employee_id,
            "status": employee.get("status", ""),
            "heartbeat_age_minutes": None if unparseable else int(age),
            "action": action,
            "hint": f"{employee_id} 上次心跳{age_label},守护 worker 可能没在跑;换个在岗员工或检查 daemon。",
        }
    return None


def parse_deliver_to(raw: str) -> dict:
    """Parse --deliver-to into a delivery spec dict for OpenClaw agents.
    Accepts JSON ({"channel":"line","group_code":"A3"}) or the shorthand
    'channel:group_code' (e.g. 'line:internal'). A value that looks like a LINE
    id (starts with C/U/R) is treated as an explicit target_id instead."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    parts = raw.split(":", 1)
    channel = parts[0].strip() or "line"
    rest = parts[1].strip() if len(parts) > 1 else ""
    if not rest:
        return {"channel": channel}
    if rest[:1] in {"C", "U", "R"} and len(rest) >= 20 and " " not in rest:
        return {"channel": channel, "target_id": rest}
    return {"channel": channel, "group_code": rest}


TASK_DISCARD_COOLDOWN_MINUTES = 60      # after a task is discarded, refuse re-creating the same one for this long


def submit_guards_on() -> bool:
    """Submit guardrails are ON in production; tests set COMPANY_KERNEL_SUBMIT_GUARDS=0 to opt out
    of the codex-workspace / duplicate / recently-discarded checks for fixtures."""
    return os.environ.get("COMPANY_KERNEL_SUBMIT_GUARDS", "1") != "0"


def normalize_task_title(title: str) -> str:
    return re.sub(r"\s+", "", str(title or "")).lower()


def codex_target_workspace_ok(conn: sqlite3.Connection, target: str, description: str) -> tuple[bool, str]:
    """codex executes inside a workspace; without a valid `工作区: /abs/path` it lands in /tmp and
    blocks. Only enforced for codex-runtime targets — other runtimes don't need a repo path."""
    row = conn.execute("SELECT runtime FROM employees WHERE id = ?", (target,)).fetchone()
    if not row or str(row["runtime"] or "") != "codex":
        return True, ""
    from company_kernel import codex_adapter  # lazy: codex_adapter imports companyctl
    match = codex_adapter.WORKSPACE_DIRECTIVE.search(str(description or ""))
    if not match:
        return False, "codex 任务必须在描述里写明 `工作区: /绝对路径`(代码仓库),否则会在 /tmp 空跑卡住"
    candidate = Path(match.group(1)).expanduser()
    if not candidate.is_absolute():
        return False, f"工作区必须是绝对路径,收到: {match.group(1)}"
    candidate = candidate.resolve()
    if candidate == ROOT or ROOT in candidate.parents:
        return False, "工作区不能指向内核目录(内核改动须走 RFC)"
    if not candidate.is_dir():
        return False, f"工作区目录不存在: {candidate}"
    return True, ""


# Reserved for communication_acceptance self-test fixtures. The PM supervisor exempts this id prefix
# from escalation, so a REAL task created with it would be silently never-escalated — block business
# submissions (CLI --task-id, intake payload) from claiming the namespace. Fixtures use a separate
# internal path (ensure_acceptance_task) that doesn't go through here, so they're unaffected.
RESERVED_TASK_ID_PREFIX = "acceptance-"


def validate_task_submission(conn: sqlite3.Connection, *, target: str, title: str, description: str, force: bool = False, task_id: str = "") -> dict | None:
    """Submit-time guardrails. Returns None if allowed, else a rejection dict. Prevents:
    1) non-executable codex tasks (no repo workspace) — they only ever block;
    2) duplicates of an already-active task;
    3) re-creating a task that was just discarded (the '丢弃了又出现' loop);
    4) business tasks claiming the reserved `acceptance-` self-test namespace (even under force)."""
    if task_id and str(task_id).startswith(RESERVED_TASK_ID_PREFIX):
        return {"ok": False, "error": f"task_id 前缀 '{RESERVED_TASK_ID_PREFIX}' 为自检夹具保留,业务任务不可使用", "task_id": task_id}
    if force or not submit_guards_on():
        return None
    target = resolve_employee_alias(target)
    ok, reason = codex_target_workspace_ok(conn, target, description)
    if not ok:
        return {"ok": False, "error": reason, "reason": "missing_workspace", "guard": "codex_workspace"}
    norm = normalize_task_title(title)
    if not norm:
        return None
    for row in conn.execute("SELECT id, title FROM tasks WHERE target_agent = ? AND status IN ('submitted','claimed','blocked')", (target,)):
        if normalize_task_title(row["title"]) == norm:
            return {"ok": False, "error": f"重复任务:已有进行中的同名任务 {row['id']},不重复创建", "reason": "duplicate_active", "existing": row["id"], "guard": "duplicate"}
    cutoff = (datetime.now(timezone.utc).astimezone() - timedelta(minutes=TASK_DISCARD_COOLDOWN_MINUTES)).isoformat()
    for row in conn.execute("SELECT id, title, updated_at FROM tasks WHERE target_agent = ? AND status = 'cancelled' AND updated_at >= ?", (target, cutoff)):
        if normalize_task_title(row["title"]) == norm:
            return {"ok": False, "error": f"该任务 {TASK_DISCARD_COOLDOWN_MINUTES} 分钟内被丢弃过({row['id']}),不自动重建。修好/拆细后用 --force 重派", "reason": "recently_discarded", "existing": row["id"], "guard": "discard_cooldown"}
    return None


def auto_triage_misdispatched_tasks(conn: sqlite3.Connection) -> dict:
    """Auto-discard tasks the kernel KNOWS can't execute (a codex task with no `工作区:` repo path
    would only run in /tmp and block), and feed back to the dispatcher — so a mis-dispatched order
    never sits 'successfully queued', it's rejected and reported at once. Runs every daemon tick as a
    backstop for anything that slipped in before the submit guard (or via reassign)."""
    if not submit_guards_on():  # same toggle as the submit guard; tests opt out
        return {"discarded": [], "count": 0}
    from company_kernel import codex_adapter
    discarded = []
    for task in rows(conn, "SELECT * FROM tasks WHERE status IN ('submitted','blocked')"):
        target = task["target_agent"]
        emp = conn.execute("SELECT runtime FROM employees WHERE id = ?", (target,)).fetchone()
        if not emp or str(emp["runtime"] or "") != "codex":
            continue
        # Only the clearest mis-dispatch: NO workspace directive at all. A present-but-bad path is left
        # for the owner to Fix (it may be transient), not auto-killed.
        if codex_adapter.WORKSPACE_DIRECTIVE.search(str(task["description"] or "")):
            continue
        reason = "派单错误:codex 任务未写 `工作区: /仓库路径`,无法执行,已自动放弃。请带绝对仓库路径重派。"
        ts = now()
        conn.execute(
            "UPDATE tasks SET status = 'cancelled', claimed_by = '', blocker = ?, updated_at = ? WHERE id = ?",
            (f"auto-discarded: {reason}", ts, task["id"]),
        )
        conn.execute("DELETE FROM locks WHERE resource_key = ?", (f"task:{task['id']}",))
        acknowledge_task_adapter_runs(conn, task["id"], "companyctl", "auto-discard mis-dispatch")
        event = record_event(conn, "task.auto_discarded", "companyctl", task_id=task["id"],
                             payload={"reason": reason, "source": task["source_agent"], "title": task["title"]})
        # feed back to the dispatcher's inbox so the agent that ordered it learns why
        try:
            inbox = employee_paths(task["source_agent"])["inbox"]
            inbox.mkdir(parents=True, exist_ok=True)
            (inbox / f"auto-discard-{task['id']}.json").write_text(
                json.dumps({"type": "task_auto_discarded", "task_id": task["id"], "title": task["title"],
                            "target": target, "reason": reason, "at": ts}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
        except OSError:
            pass
        audit(conn, "companyctl", "task.auto_discard", task["id"], {"reason": reason, "source": task["source_agent"], "event_id": event["id"]})
        discarded.append({"id": task["id"], "source": task["source_agent"], "title": task["title"], "target": target})
    conn.commit()
    return {"discarded": discarded, "count": len(discarded)}


def cmd_task_auto_triage(args: argparse.Namespace) -> int:
    conn = connect()
    result = auto_triage_misdispatched_tasks(conn)
    emit({"ok": True, **result})
    return 0


def normalize_submission(conn: sqlite3.Connection, *, target: str, description: str) -> tuple[str, str, dict | None]:
    """Shared submit normalization applied by EVERY entry point (cmd_task_submit, task route, hooks,
    workflow, split) so routing/memory rules can't be bypassed:
      - project executor lock: app↔cli remap, or block when the workspace's project forbids the target;
      - global reroute of a passive app employee to its ACTIVE cli twin (so dispatched work isn't stuck);
      - per-project `记忆会话:` stamp so the runtime resumes its project session instead of re-scanning.
    Returns (target, description, error). error is a dict the caller surfaces when the lock blocks the
    target. The caller runs approval/validation on the ORIGINAL description, then persists the returned
    (stamped) one."""
    target = resolve_employee_alias(target)
    desc = str(description or "")
    ws = ""
    m = re.search(r"(?:工作区|workspace)\s*[:：]\s*([^\s。，、；;,]+)", desc)
    if m:
        ws = m.group(1)
    pid = ""
    if ws:
        lock = project_memory.enforce_executor(conn, workspace=ws, target=target)
        if lock.get("blocked"):
            return target, desc, {"ok": False, "error": "project executor lock", "note": lock.get("note", ""), "target": target, "project_id": lock.get("project_id", "")}
        if lock.get("remapped"):
            target = lock["target"]
        pid = lock.get("project_id") or ""
    # A passive app employee never auto-claims — reroute to its active cli twin so the work actually runs.
    twin = project_memory.APP_CLI_PAIRS.get(target)
    if twin:
        trow = conn.execute("SELECT status FROM employees WHERE id = ?", (twin,)).fetchone()
        if trow and trow["status"] == "active":
            target = twin
    # Executor-lock memory fallback ONLY when the task carries no workspace at all (a context-less review
    # task); a task naming a non-project workspace must NOT be bound to the worker's locked project.
    if not pid and not ws:
        pid = project_memory.project_for_executor(conn, target) or ""
    if pid and not re.search(r"(?:记忆会话|memory-session)\s*[:：]", desc):
        desc = desc.rstrip() + f"\n记忆会话: {pid}"
    return target, desc, None


def cmd_task_submit(args: argparse.Namespace) -> int:
    conn = connect()
    source = resolve_employee_alias(args.source)
    target = resolve_employee_alias(args.target)
    # Shared normalization (executor lock / app→cli reroute / 记忆会话 stamp) used by EVERY submit path,
    # so routing & memory rules can't be bypassed. Approval + validation run on the ORIGINAL description
    # (so a project id is never misread as a risk keyword); the stamped one gets persisted / parked.
    _orig_desc = str(args.description or "")
    target, args.description, _norm_err = normalize_submission(conn, target=target, description=args.description)
    args.target = target
    if _norm_err is not None:
        emit(_norm_err)
        return 2
    require_employee(conn, source)
    require_employee(conn, target)
    rejection = validate_task_submission(conn, target=target, title=args.title, description=_orig_desc, force=getattr(args, "force_submit", False), task_id=getattr(args, "task_id", "") or "")
    if rejection:
        emit({**rejection, "target": target, "title": args.title})
        return 2
    inactive = require_active_employee(conn, target, "task.submit")
    if inactive:
        emit(inactive)
        return 2
    policy = require_communication_allowed(source, target, "task.submit")
    approval_action = detect_route_approval_action(args.title, _orig_desc, args.requires_approval)
    gate = route_approval_gate(conn, args, source, target, [{"agent": target, "reason": "direct_submit"}], approval_action)
    if not gate.get("allowed"):
        emit({"ok": False, "error": "approval required", "target": target, "approval_action": approval_action, "approval": gate["approval_request"], "approval_file": gate["file"]})
        return 2
    task_id = args.task_id or f"task-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    ts = now()
    conn.execute(
        """
        INSERT INTO tasks(id, source_agent, target_agent, title, description, priority, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 'submitted', ?, ?)
        """,
        (task_id, source, target, args.title, args.description, args.priority, ts, ts),
    )
    metadata = {"trace_id": new_trace_id(), "declared_changes": parse_csv(args.changed_files), "rfc": args.rfc, "approval": gate.get("approval")}
    deliver_to = parse_deliver_to(getattr(args, "deliver_to", ""))
    if deliver_to:
        metadata["deliver_to"] = deliver_to
    conn.execute(
        "INSERT OR REPLACE INTO task_metadata(task_id, metadata_json, updated_at) VALUES (?, ?, ?)",
        (task_id, json.dumps(metadata, ensure_ascii=False), ts),
    )
    conn.commit()
    workspace = ensure_task_workspace(conn, task_id, metadata["trace_id"])
    inbox = employee_paths(target)["inbox"]
    inbox.mkdir(parents=True, exist_ok=True)
    task_file = inbox / f"{task_id}.json"
    task = {
        "id": task_id,
        "source_agent": source,
        "target_agent": target,
        "title": args.title,
        "description": args.description,
        "priority": args.priority,
        "status": "submitted",
        "metadata": metadata,
        "workspace": workspace,
        "communication_policy": policy,
        "created_at": ts,
    }
    task_file.write_text(json.dumps(task, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Emit the dispatch event only AFTER inbox delivery succeeds (codex review) — so the feed's "已派单"
    # line never claims a dispatch that didn't actually reach the executor. Makes the dispatcher's work
    # visible without signalling success prematurely.
    record_event(conn, "task.dispatched", source, task_id=task_id, trace_id=metadata["trace_id"],
                 payload={"target_agent": target, "title": args.title, "priority": args.priority})
    audit(conn, source, "task.submit", task_id, task)
    emit({"ok": True, "task": task, "file": str(task_file)})
    return 0


def load_policy_config() -> dict:
    if not POLICY_PATH.exists():
        return {"route_approval": {"default_risk": "P1", "actions": DEFAULT_ROUTE_APPROVAL_ACTIONS}}
    obj = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    route = obj.setdefault("route_approval", {})
    route.setdefault("default_risk", "P1")
    route.setdefault("actions", DEFAULT_ROUTE_APPROVAL_ACTIONS)
    return obj


# A "token" in UI/design work means a *design token* (色彩/配色/spacing token), NOT a secret/auth
# token — so it must not trip the secret_change gate (that's a recurring false positive that buries
# real frontend tasks in the approval queue). Real secret language (api/auth token, 密钥, password,
# 密码) still gates. This only narrows the ambiguous bare word "token".
_DESIGN_TOKEN_RE = re.compile(
    r"(?:配色|设计|主题|样式|颜色|色彩|间距|字体|design|color|theme|style|spacing|typography|ui)\s*[-_]?\s*tokens?",
    re.IGNORECASE,
)


def detect_route_approval_action(title: str, description: str, explicit_action: str = "") -> str:
    if explicit_action:
        return explicit_action
    text = f"{title}\n{description}".lower()
    actions = load_policy_config().get("route_approval", {}).get("actions", DEFAULT_ROUTE_APPROVAL_ACTIONS)
    for action, keywords in actions.items():
        for keyword in keywords:
            normalized = keyword.lower().strip()
            if not normalized:
                continue
            search_text = text
            if action == "secret_change" and normalized == "token":
                search_text = _DESIGN_TOKEN_RE.sub(" ", text)  # design tokens aren't secrets
            if normalized.isascii() and re.search(rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])", search_text):
                return action
            if not normalized.isascii() and normalized in text:
                return action
    return ""


ROUTE_APPROVAL_MODES = {"manual", "auto_low_risk", "auto"}
# Genuinely dangerous route actions — money, secrets, production deploys, kernel self-changes.
# The middle tier (auto_low_risk) keeps THESE manual and auto-approves everything else.
HIGH_RISK_ROUTE_ACTIONS = {"payment", "compensation", "salary", "penalty", "secret_change", "production_deploy", "kernel_change"}


def route_approval_mode() -> str:
    """Owner-set approval posture. 'manual' (default) = gate high-risk routes for human approval;
    'auto_low_risk' = auto-approve everything EXCEPT the dangerous set (密钥/支付/部署仍人工);
    'auto' = full auto-approval (every route proceeds, recorded for audit). Read live from
    config/policy.json so a mode change takes effect without a restart."""
    mode = str(load_policy_config().get("route_approval", {}).get("mode", "manual")).lower().strip()
    return mode if mode in ROUTE_APPROVAL_MODES else "manual"


def route_action_auto_approved(action: str) -> bool:
    """Whether the current mode auto-approves this route action.
    manual → never; auto → always; auto_low_risk → all but HIGH_RISK_ROUTE_ACTIONS."""
    mode = route_approval_mode()
    if mode == "auto":
        return True
    if mode == "auto_low_risk":
        return action not in HIGH_RISK_ROUTE_ACTIONS
    return False


def route_approval_gate(conn: sqlite3.Connection, args: argparse.Namespace, source: str, target: str, matches: list[dict], approval_action: str) -> dict:
    if not approval_action:
        return {"allowed": True}
    task_id = args.task_id or f"route-{slug(args.title)}"
    approval_id = args.approval_id or f"approval-route-{slug(task_id)}-{slug(approval_action)}"
    gate = approved_gate(conn, approval_id, approval_action, source, target)
    if gate["allowed"]:
        return gate
    auto = route_action_auto_approved(approval_action)
    reason = (f"auto-approved (mode={route_approval_mode()}, owner-delegated): `{args.title}` → `{target}`" if auto
              else f"task route requires approval before assigning high-risk task `{args.title}` to `{target}`")
    result = create_approval_internal(
        conn,
        source=source,
        action=approval_action,
        reason=reason,
        target=target,
        risk=args.risk or load_policy_config().get("route_approval", {}).get("default_risk", "P1"),
        evidence="",
        approval_id=approval_id,
        notify=not auto,  # auto-approved → don't fire a stale "approval required" Telegram
        metadata={
            "route": True,
            "task_id": task_id,
            "title": args.title,
            "description": args.description,
            "target": target,
            "source": source,
            "priority": getattr(args, "priority", "P2") or "P2",
            "changed_files": getattr(args, "changed_files", "") or "",
            "rfc": getattr(args, "rfc", "") or "",
            "deliver_to": getattr(args, "deliver_to", "") or "",
            "matches": matches[:5],
        },
    )
    if auto:
        # AUTO mode: owner delegated full approval. Mark the just-created approval approved and let
        # the task through immediately — no pending item to click, nothing to vanish.
        ts = now()
        detail = normalize_approval(conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone())["detail"]
        detail.update({"decided_by": "auto", "decision": "approved", "decision_reason": "mode=auto", "decided_at": ts})
        conn.execute("UPDATE approvals SET status = 'approved', reason = ?, updated_at = ? WHERE id = ?",
                     (json.dumps(detail, ensure_ascii=False), ts, approval_id))
        conn.commit()
        audit(conn, source, "approval.auto_approved", approval_id, {"mode": "auto", "action": approval_action, "target": target})
        return {"allowed": True, "approval": approval_id, "auto_approved": True}
    return {"allowed": False, "approval_request": result["approval"], "file": result["file"]}


def cmd_task_route(args: argparse.Namespace) -> int:
    conn = connect()
    source = resolve_employee_alias(args.source)
    require_employee(conn, source)
    matches = employee_matches(conn, args)
    if not matches:
        emit({"ok": False, "error": "no matching employee", "criteria": {"skills": args.skills, "tools": args.tools, "task_type": args.task_type, "runtime": args.runtime, "role": args.role}})
        return 1
    target = matches[0]["agent"]
    approval_action = detect_route_approval_action(args.title, args.description, args.requires_approval)
    gate = route_approval_gate(conn, args, source, target, matches, approval_action)
    if not gate.get("allowed"):
        emit({"ok": False, "error": "approval required", "selected": matches[0], "approval_action": approval_action, "approval": gate["approval_request"], "approval_file": gate["file"]})
        return 2
    result = submit_task_internal(
        conn,
        source=source,
        target=target,
        title=args.title,
        description=args.description,
        priority=args.priority,
        task_id=args.task_id,
        metadata={
            "declared_changes": parse_csv(args.changed_files),
            "rfc": args.rfc,
            "route": {"criteria": {"skills": parse_csv(args.skills), "tools": parse_csv(args.tools), "task_type": args.task_type, "runtime": args.runtime, "role": args.role}, "matches": matches[:5], "approval": gate.get("approval")},
        },
    )
    audit(conn, source, "task.route", result["task"]["id"], {"target": target, "matches": matches[:5]})
    emit({"ok": True, "selected": matches[0], "matches": matches[:5], **result})
    return 0


def submit_task_internal(
    conn: sqlite3.Connection,
    *,
    source: str,
    target: str,
    title: str,
    description: str,
    priority: str = "P2",
    task_id: str = "",
    metadata: dict | None = None,
    allow_candidate: bool = False,
    force: bool = False,
) -> dict:
    source = resolve_employee_alias(source)
    # same normalization every CLI/hook/workflow/route submit gets — executor lock / app→cli reroute /
    # 记忆会话 stamp — so internal submitters can't bypass routing & memory binding.
    target, description, _norm_err = normalize_submission(conn, target=target, description=description)
    if _norm_err is not None:
        raise SystemExit(json.dumps(_norm_err, ensure_ascii=False))
    require_employee(conn, source)
    require_employee(conn, target)
    inactive = require_active_employee(conn, target, "task.submit")
    if inactive and not allow_candidate:
        raise SystemExit(json.dumps(inactive, ensure_ascii=False))
    rejection = validate_task_submission(conn, target=target, title=title, description=description, force=force, task_id=task_id)
    if rejection:
        raise SystemExit(json.dumps(rejection, ensure_ascii=False))
    policy = require_communication_allowed(source, target, "task.submit")
    tid = task_id or f"task-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    ts = now()
    conn.execute(
        """
        INSERT INTO tasks(id, source_agent, target_agent, title, description, priority, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 'submitted', ?, ?)
        """,
        (tid, source, target, title, description, priority, ts, ts),
    )
    task_metadata_obj = {**(metadata or {})}
    task_metadata_obj.setdefault("trace_id", new_trace_id())
    conn.execute(
        "INSERT OR REPLACE INTO task_metadata(task_id, metadata_json, updated_at) VALUES (?, ?, ?)",
        (tid, json.dumps(task_metadata_obj, ensure_ascii=False), ts),
    )
    conn.commit()
    workspace = ensure_task_workspace(conn, tid, task_metadata_obj["trace_id"])
    inbox = employee_paths(target)["inbox"]
    inbox.mkdir(parents=True, exist_ok=True)
    task_file = inbox / f"{tid}.json"
    task = {
        "id": tid,
        "source_agent": source,
        "target_agent": target,
        "title": title,
        "description": description,
        "priority": priority,
        "status": "submitted",
        "metadata": task_metadata_obj,
        "workspace": workspace,
        "communication_policy": policy,
        "created_at": ts,
    }
    task_file.write_text(json.dumps(task, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # dispatch event only after inbox delivery succeeds (codex review) — never claim a dispatch the
    # executor didn't actually receive. See cmd_task_submit.
    record_event(conn, "task.dispatched", source, task_id=tid, trace_id=task_metadata_obj["trace_id"],
                 payload={"target_agent": target, "title": title, "priority": priority})
    audit(conn, source, "task.submit", tid, task)
    return {"task": task, "file": str(task_file)}


def complete_task_internal(
    conn: sqlite3.Connection,
    *,
    agent: str,
    task_id: str,
    summary: str,
    evidence: str,
) -> dict:
    if not evidence.strip():
        raise ValueError("task evidence is required")
    task = require_task(conn, task_id)
    completable_statuses = {"submitted", "claimed", "running", "correcting"}
    if task["status"] not in completable_statuses:
        raise ValueError(f"task is not completable in status {task['status']}")
    auto_promote_workspace_evidence(conn, task_id=task_id, agent=agent, evidence_path=evidence, summary=summary)
    completed_attempt = conn.execute(
        """
        SELECT * FROM execution_attempts
        WHERE task_id = ?
          AND employee_id = ?
          AND status IN ('starting', 'running', 'correcting')
        ORDER BY started_at DESC
        LIMIT 1
        """,
        (task_id, agent),
    ).fetchone()
    if (completed_attempt or has_v3_file_flow(conn, task_id)) and not final_evidence_for_path(conn, task_id, evidence):
        raise ValueError("task done requires promoted final evidence for v3 file-flow tasks")
    cur = conn.execute(
        "UPDATE tasks SET status = 'completed', claimed_by = CASE WHEN claimed_by = '' THEN ? ELSE claimed_by END, summary = ?, evidence_path = ?, blocker = '', updated_at = ? WHERE id = ? AND (target_agent = ? OR claimed_by = ?)",
        (agent, summary, evidence, now(), task_id, agent, agent),
    )
    if cur.rowcount == 0:
        raise SystemExit(f"task not found or not owned by agent: {task_id}")
    completed_attempt_id = ""
    if completed_attempt:
        completed_attempt_id = completed_attempt["attempt_id"]
        conn.execute(
            "UPDATE execution_attempts SET status = 'success', finished_at = ?, error_message = '' WHERE attempt_id = ?",
            (now(), completed_attempt_id),
        )
        conn.execute(
            "UPDATE evidence SET attempt_id = ? WHERE task_id = ? AND is_final = 1 AND attempt_id = ''",
            (completed_attempt_id, task_id),
        )
    conn.execute("DELETE FROM locks WHERE resource_key = ?", (f"task:{task_id}",))
    # The task succeeded — so any earlier FAILED adapter attempts for it are no longer a standing
    # problem. Acknowledge them, or a task that completed after a retry leaves phantom
    # "任务失败/缺证据" noise in the attention badge forever.
    acknowledge_task_adapter_runs(conn, task_id, agent, "task completed")
    synced_plan_items = sync_project_plan_for_task(conn, task_id=task_id, task_status="completed", actor=agent)
    conn.commit()
    event = record_event(conn, "task.done", agent, task_id=task_id, payload={"summary": summary, "evidence": evidence, "attempt_id": completed_attempt_id})
    audit(conn, agent, "task.done", task_id, {"summary": summary, "evidence": evidence, "attempt_id": completed_attempt_id, "event_id": event["id"]})
    # event-driven feedback: notify the dispatcher's inbox so their always-on app can watch it
    notice_path = deliver_completion_notice(conn, dict(task), status="completed", summary=summary, evidence=evidence, actor=agent)
    # project memory: capture this completion into the project that owns the task's workspace
    try:
        project_memory.capture_task_outcome(conn, dict(task), kind="done", summary=summary, evidence=evidence)
    except Exception:
        pass
    return {"task_id": task_id, "status": "completed", "evidence": evidence, "attempt_id": completed_attempt_id, "event_id": event["id"], "synced_plan_items": synced_plan_items, "dispatcher_notified": notice_path or None}


def prune_inbox_dir(inbox: Path, keep: int = 100) -> int:
    """Keep only the newest `keep` notification files per inbox so they don't balloon into noise
    that buries the signal. These files are write-only delivery records — the real work queue is
    the DB, so pruning them loses nothing operational."""
    try:
        files = sorted((p for p in inbox.glob("*.json") if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return 0
    removed = 0
    for stale in files[keep:]:
        try:
            stale.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def write_task_inbox_file(task: dict) -> str:
    target = task["target_agent"]
    inbox = employee_paths(target)["inbox"]
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / f"{task['id']}.json"
    path.write_text(json.dumps(task, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    prune_inbox_dir(inbox)  # cap on write so inboxes never silently balloon
    return str(path)


def write_dispatcher_completion_notice(conn: sqlite3.Connection, task: dict, *, status: str,
                                       summary: str = "", evidence: str = "", blocker: str = "") -> str:
    """Drop a `result-<task>.json` into the DISPATCHER's (source_agent's) inbox when their dispatched
    task finishes — so an always-on app can WATCH its inbox dir (event-driven, e.g. fswatch) and react
    the instant a task completes, instead of polling. No-op when the source isn't a distinct
    registered employee (subtasks/system don't notify a human/app)."""
    source = str(task.get("source_agent") or "").strip()
    target = str(task.get("target_agent") or "").strip()
    if not source or source == target or source in {"system"}:
        return ""
    if not conn.execute("SELECT 1 FROM employees WHERE id = ?", (source,)).fetchone():
        return ""
    inbox = employee_paths(source)["inbox"]
    inbox.mkdir(parents=True, exist_ok=True)
    # tag the notice with its project (= the stamped 记忆会话 key) + trace, so a dispatcher consuming a
    # mixed inbox can group completions by project/orchestration instead of merging unrelated rounds.
    _pm = re.search(r"(?:记忆会话|memory-session)\s*[:：]\s*(\S+)", str(task.get("description") or ""))
    project_id = _pm.group(1).strip() if _pm else ""
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    notice = {
        "type": f"task.{status}", "task_id": task.get("id"), "title": task.get("title"),
        "done_by": target, "status": status, "summary": summary, "evidence_path": evidence,
        "blocker": blocker, "at": now(), "source_agent": source,
        "project_id": project_id, "trace_id": str(metadata.get("trace_id") or ""),
        "note": f"你派给 {target} 的任务「{task.get('title')}」已{ {'completed': '完成', 'done': '完成', 'cancelled': '取消'}.get(status, '受阻') }",
    }
    path = inbox / f"result-{task.get('id')}.json"
    # atomic write (temp + rename) so a concurrent reader (e.g. the hermes adapter draining notices)
    # never parses a half-written file. The temp name is dot-prefixed/.tmp so it won't match result-*.json.
    tmp = inbox / f".result-{task.get('id')}.json.tmp"
    tmp.write_text(json.dumps(notice, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    prune_inbox_dir(inbox)
    return str(path)


def deliver_completion_notice(conn: sqlite3.Connection, task: dict, *, status: str,
                              summary: str = "", evidence: str = "", blocker: str = "", actor: str = "") -> str:
    """Single uniform path for delivering the dispatcher's completion/blocked/cancelled notice.
    Closing the feedback loop must never (a) roll back a real state change that already committed,
    nor (b) vanish silently if delivery fails. So this never raises, but on failure records an
    observable `task.completion_notice_failed` event — a broken loop shows up instead of hiding.
    Used by every finish path (done/block/cancel/discard) so they behave identically."""
    try:
        return write_dispatcher_completion_notice(
            conn, dict(task), status=status, summary=summary, evidence=evidence, blocker=blocker)
    except Exception as exc:
        with contextlib.suppress(Exception):
            record_event(conn, "task.completion_notice_failed", actor or str(task.get("target_agent") or ""),
                         task_id=str(task.get("id") or ""),
                         payload={"status": status, "error": str(exc)[:300],
                                  "source_agent": str(task.get("source_agent") or "")})
            conn.commit()
        return ""


def task_with_children(conn: sqlite3.Connection, task_id: str) -> dict:
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        raise SystemExit(f"task not found: {task_id}")
    children = rows(
        conn,
        """
        SELECT t.*, tr.created_by AS relation_created_by, tr.created_at AS relation_created_at
        FROM task_relations tr
        JOIN tasks t ON t.id = tr.child_task_id
        WHERE tr.parent_task_id = ?
        ORDER BY tr.created_at ASC
        """,
        (task_id,),
    )
    return {"task": dict(task), "children": children}


def task_metadata(conn: sqlite3.Connection, task_id: str) -> dict:
    row = conn.execute("SELECT metadata_json FROM task_metadata WHERE task_id = ?", (task_id,)).fetchone()
    if not row:
        return {}
    try:
        parsed = json.loads(row["metadata_json"] or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def update_task_metadata(conn: sqlite3.Connection, task_id: str, patch: dict) -> dict:
    metadata = task_metadata(conn, task_id)
    metadata.update(patch)
    conn.execute(
        "INSERT OR REPLACE INTO task_metadata(task_id, metadata_json, updated_at) VALUES (?, ?, ?)",
        (task_id, json.dumps(metadata, ensure_ascii=False), now()),
    )
    return metadata


def sync_project_plan_for_task(conn: sqlite3.Connection, *, task_id: str, task_status: str, actor: str) -> list[dict]:
    plan_status = {"completed": "done", "blocked": "blocked", "submitted": "in_progress", "claimed": "in_progress"}.get(task_status)
    if not plan_status:
        return []
    ts = now()
    plan_items = rows(conn, "SELECT * FROM project_plan_items WHERE task_id = ? ORDER BY created_at ASC", (task_id,))
    updated = []
    for item in plan_items:
        if item["status"] in {"done", "completed", "cancelled"} and plan_status == "done":
            continue
        if task_status in {"submitted", "claimed"} and item["status"] != "blocked":
            continue
        if item["status"] == plan_status:
            continue
        conn.execute("UPDATE project_plan_items SET status = ?, updated_at = ? WHERE id = ?", (plan_status, ts, item["id"]))
        conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (ts, item["project_id"]))
        updated.append({**item, "status": plan_status, "updated_at": ts})
    if updated:
        audit(conn, actor, "project.plan_sync", task_id, {"task_status": task_status, "plan_status": plan_status, "plan_items": [item["id"] for item in updated]})
    return updated


def sync_project_plan_owner_for_task(conn: sqlite3.Connection, *, task_id: str, owner: str, actor: str) -> list[dict]:
    ts = now()
    plan_items = rows(conn, "SELECT * FROM project_plan_items WHERE task_id = ? ORDER BY created_at ASC", (task_id,))
    updated = []
    for item in plan_items:
        if item["owner_agent"] == owner:
            continue
        conn.execute("UPDATE project_plan_items SET owner_agent = ?, updated_at = ? WHERE id = ?", (owner, ts, item["id"]))
        conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (ts, item["project_id"]))
        updated.append({**item, "owner_agent": owner, "updated_at": ts})
    if updated:
        audit(conn, actor, "project.plan_owner_sync", task_id, {"owner": owner, "plan_items": [item["id"] for item in updated]})
    return updated


def guard_task_claim(conn: sqlite3.Connection, task: sqlite3.Row, agent: str) -> dict:
    metadata = task_metadata(conn, task["id"])
    declared = metadata.get("declared_changes", [])
    if isinstance(declared, str):
        declared = parse_csv(declared)
    rfc = str(metadata.get("rfc", "") or "")
    if rfc:
        declared = [path for path in declared if not normalize_repo_path(path).startswith("rfcs/")]
    if not declared:
        return {"allowed": True, "metadata": metadata, "checks": []}
    config = load_protected_paths_config()
    checks = [protected_path_decision(path, config) for path in declared]
    blocked = [check for check in checks if not check["allowed"]]
    if blocked and rfc:
        rfc_check = protected_path_decision(rfc, config)
        rfc_approval = approved_rfc_covers(conn, rfc, blocked)
        if rfc_approval["allowed"]:
            return {"allowed": True, "metadata": metadata, "checks": checks, "rfc": rfc, "rfc_approval": rfc_approval}
        return {"allowed": False, "metadata": metadata, "checks": checks, "blocked": blocked, "rfc": rfc, "rfc_check": rfc_check, "rfc_approval": rfc_approval, "reason": "protected changes require approved RFC"}
    if blocked:
        return {"allowed": False, "metadata": metadata, "checks": checks, "blocked": blocked, "reason": "protected changes require RFC"}
    return {"allowed": True, "metadata": metadata, "checks": checks}


def write_task_collection_report(parent_task: dict, children: list[dict], collector: str, summary: str) -> Path:
    report_dir = employee_paths(collector)["reports"] / parent_task["id"]
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / "task-collection-report.md"
    lines = [
        "# Task Collection Report",
        "",
        f"- parent_task: `{parent_task['id']}`",
        f"- collector: `{collector}`",
        f"- summary: {summary}",
        "",
        "## Children",
        "",
    ]
    for child in children:
        lines.extend(
            [
                f"### {child['id']}",
                "",
                f"- target: `{child['target_agent']}`",
                f"- status: `{child['status']}`",
                f"- summary: {child.get('summary') or ''}",
                f"- evidence: {child.get('evidence_path') or ''}",
                f"- blocker: {child.get('blocker') or ''}",
                "",
            ]
        )
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path



def contains_forbidden_secret_key(value: object) -> bool:
    forbidden = {"token", "bot_token", "telegram_bot_token", "secret", "password", "api_key", "authorization"}
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if any(name in lowered for name in forbidden):
                return True
            if contains_forbidden_secret_key(item):
                return True
    elif isinstance(value, list):
        return any(contains_forbidden_secret_key(item) for item in value)
    return False


def list_external_threads(conn: sqlite3.Connection, platform: str = "", owner_agent: str = "", limit: int = 50) -> list[dict]:
    clauses = []
    params: list[object] = []
    if platform:
        clauses.append("platform = ?")
        params.append(platform)
    if owner_agent:
        clauses.append("owner_agent = ?")
        params.append(owner_agent)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(limit)
    return rows(conn, f"SELECT * FROM external_threads{where} ORDER BY last_message_at DESC, updated_at DESC LIMIT ?", tuple(params))


def show_external_thread(conn: sqlite3.Connection, thread_id: str) -> dict:
    thread = conn.execute("SELECT * FROM external_threads WHERE id = ?", (thread_id,)).fetchone()
    if not thread:
        return {"ok": False, "error": "external thread not found", "thread_id": thread_id}
    messages = rows(conn, "SELECT * FROM external_messages WHERE thread_id = ? ORDER BY created_at ASC, id ASC", (thread_id,))
    return {"ok": True, "thread": dict(thread), "messages": messages}


def import_external_mirror(conn: sqlite3.Connection, payload: dict) -> dict:
    if contains_forbidden_secret_key(payload):
        return {"ok": False, "error": "external mirror import rejects secret/token/password fields; ingest sanitized payload only"}
    thread = dict(payload.get("thread") or {})
    messages = payload.get("messages") or []
    if not isinstance(messages, list):
        return {"ok": False, "error": "messages must be a list"}
    thread_id = str(thread.get("id") or payload.get("thread_id") or "").strip()
    platform = str(thread.get("platform") or payload.get("platform") or "telegram").strip()
    owner_agent = str(thread.get("owner_agent", payload.get("owner_agent", ""))).strip()
    bridge_agent = str(thread.get("bridge_agent", payload.get("bridge_agent", ""))).strip()
    if not thread_id:
        return {"ok": False, "error": "thread.id or thread_id is required"}
    if not owner_agent:
        return {"ok": False, "error": "thread.owner_agent or owner_agent is required"}
    if not bridge_agent:
        return {"ok": False, "error": "thread.bridge_agent or bridge_agent is required"}
    ts = now()
    last_message_at = str(thread.get("last_message_at") or max([str(m.get("created_at") or ts) for m in messages if isinstance(m, dict)] or [ts]))
    metadata = thread.get("metadata_json", thread.get("metadata", {}))
    if not isinstance(metadata, str):
        metadata = json.dumps(metadata or {}, ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO external_threads(id, platform, account_id, external_user_id, external_chat_id, owner_agent, bridge_agent, title, status, last_message_at, created_at, updated_at, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET platform=excluded.platform, account_id=excluded.account_id, external_user_id=excluded.external_user_id,
          external_chat_id=excluded.external_chat_id, owner_agent=excluded.owner_agent, bridge_agent=excluded.bridge_agent, title=excluded.title,
          status=excluded.status, last_message_at=excluded.last_message_at, updated_at=excluded.updated_at, metadata_json=excluded.metadata_json
        """,
        (
            thread_id,
            platform,
            str(thread.get("account_id", "")),
            str(thread.get("external_user_id", "")),
            str(thread.get("external_chat_id", "")),
            owner_agent,
            bridge_agent,
            str(thread.get("title", thread_id)),
            str(thread.get("status", "open")),
            last_message_at,
            str(thread.get("created_at", ts)),
            ts,
            metadata,
        ),
    )
    imported = []
    duplicate_messages = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        message_id = str(msg.get("id") or msg.get("message_id") or "").strip()
        if not message_id:
            continue
        existing_message = conn.execute("SELECT id FROM external_messages WHERE id = ?", (message_id,)).fetchone()
        if existing_message:
            duplicate_messages += 1
            continue
        msg_meta = msg.get("metadata_json", msg.get("metadata", {}))
        if not isinstance(msg_meta, str):
            msg_meta = json.dumps(msg_meta or {}, ensure_ascii=False)
        conn.execute(
            """
            INSERT INTO external_messages(id, thread_id, direction, platform, sender_kind, sender_id, body, raw_excerpt, evidence_path, source_event_id, created_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id, thread_id, str(msg.get("direction", "")), str(msg.get("platform", platform)), str(msg.get("sender_kind", "")),
                str(msg.get("sender_id", "")), str(msg.get("body", "")), str(msg.get("raw_excerpt", "")), str(msg.get("evidence_path", "")),
                str(msg.get("source_event_id", "")), str(msg.get("created_at", ts)), msg_meta,
            ),
        )
        company_message_id = str(msg.get("company_message_id", ""))
        conversation_message_id = str(msg.get("conversation_message_id", ""))
        if company_message_id or conversation_message_id:
            conn.execute(
                "INSERT OR IGNORE INTO external_message_links(external_message_id, company_message_id, conversation_message_id, created_at) VALUES (?, ?, ?, ?)",
                (message_id, company_message_id, conversation_message_id, ts),
            )
        imported.append(message_id)
    cursor_obj = payload.get("cursor")
    cursor = cursor_obj if isinstance(cursor_obj, dict) else {}
    cursor_id = str(cursor.get("id") or payload.get("cursor_id") or f"{platform}-{thread.get('account_id', '')}-{owner_agent}").strip("-")
    cursor_value = str(cursor.get("value") or cursor.get("cursor_value") or payload.get("cursor_value") or last_message_at)
    cursor_state = cursor.get("state_json", cursor.get("state", {}))
    if not isinstance(cursor_state, str):
        cursor_state = json.dumps(cursor_state or {}, ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO external_ingest_cursors(id, platform, account_id, bridge_agent, cursor_value, last_seen_at, state_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET platform=excluded.platform, account_id=excluded.account_id, bridge_agent=excluded.bridge_agent,
          cursor_value=excluded.cursor_value, last_seen_at=excluded.last_seen_at, state_json=excluded.state_json, updated_at=excluded.updated_at
        """,
        (cursor_id, platform, str(thread.get("account_id", "")), bridge_agent, cursor_value, last_message_at, cursor_state, ts),
    )
    conn.commit()
    audit(conn, "external-mirror", "import", thread_id, {"platform": platform, "messages": len(imported), "duplicates": duplicate_messages, "cursor_id": cursor_id})
    return {"ok": True, "thread_id": thread_id, "platform": platform, "cursor_id": cursor_id, "imported_messages": len(imported), "duplicate_messages": duplicate_messages, "messages": imported}


def cmd_external_threads(args: argparse.Namespace) -> int:
    conn = connect()
    emit({"ok": True, "threads": list_external_threads(conn, platform=args.platform, owner_agent=args.owner_agent, limit=args.limit)})
    return 0


def cmd_external_show(args: argparse.Namespace) -> int:
    conn = connect()
    result = show_external_thread(conn, args.thread_id)
    emit(result)
    return 0 if result.get("ok") else 1


def cmd_external_import(args: argparse.Namespace) -> int:
    payload = json.loads(args.payload) if args.payload else json.loads(Path(args.file).read_text(encoding="utf-8"))
    conn = connect()
    result = import_external_mirror(conn, payload)
    emit(result)
    return 0 if result.get("ok") else 2


def employee_offline_report_internal(conn: sqlite3.Connection, *, stale_minutes: int = 10, dormant_minutes: int = 1440) -> dict:
    """Classify active employees by heartbeat freshness:
      online   = seen within stale_minutes
      offline  = dropped recently (stale between stale_minutes and dormant_minutes) → worth alerting
      dormant  = never online, or stale > dormant_minutes (e.g. a logical operator like openclaw-main
                 with no running runtime) → NOT a live worker that 'dropped', so excluded from alerts.
    Only `offline` is alert-worthy; dormant positions shouldn't generate false 'unstable' notifications."""
    employees = [dict(r) for r in rows(conn, "SELECT * FROM employees WHERE status = 'active' ORDER BY id") if not is_human_owner_employee(dict(r))]
    online, offline, dormant = [], [], []
    cutoff_now = datetime.now(timezone.utc)
    for emp in employees:
        eid = emp["id"]
        row = conn.execute("SELECT last_seen_at FROM heartbeats WHERE agent_id = ?", (eid,)).fetchone()
        last = row["last_seen_at"] if row else ""
        entry = {"id": eid, "name": emp.get("name", eid), "runtime": emp.get("runtime", ""), "last_seen_at": last}
        if employee_has_fresh_heartbeat(conn, eid, stale_minutes=stale_minutes):
            online.append(entry)
            continue
        if eid in project_memory.APP_CLI_PAIRS:
            # interactive app employees (codex/claude/antigravity) run only when the owner opens them —
            # being "offline" is normal, not an outage, so never alert on them (treat as dormant). Their
            # CLI twins (codex-cli/claude-cli/agy) are the daemon workers that SHOULD stay online and
            # still get reported if they drop.
            dormant.append(entry)
            continue
        try:
            stale_min = (cutoff_now - parse_time(last)).total_seconds() / 60 if last else 10 ** 9
        except (ValueError, TypeError):
            stale_min = 10 ** 9
        (dormant if stale_min > dormant_minutes else offline).append(entry)
    return {"ok": True, "stale_minutes": stale_minutes, "dormant_minutes": dormant_minutes,
            "online": online, "offline": offline, "dormant": dormant,
            "counts": {"active": len(employees), "online": len(online), "offline": len(offline), "dormant": len(dormant)}}


OFFLINE_NOTIFY_DEDUP_PATH = STATE_DIR / "offline-notify-dedup.json"
OFFLINE_NOTIFY_COOLDOWN_SECONDS = 3600  # re-remind the same offline set at most once per hour


def _offline_notify_should_send(offline_ids: list[str], *, force: bool) -> bool:
    """Notify only when the offline set CHANGED or the cooldown elapsed — so a scheduled (every-tick)
    caller doesn't spam. Returns False to skip; the caller still gets the full report."""
    if force:
        return True
    current = ",".join(sorted(offline_ids))
    try:
        state = json.loads(OFFLINE_NOTIFY_DEDUP_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    if not offline_ids and not state.get("last_set"):
        return False  # all online, and was already all-online — nothing to say
    if state.get("last_set") == current:
        try:
            elapsed = (datetime.now(timezone.utc) - parse_time(state.get("last_at", ""))).total_seconds()
        except (ValueError, TypeError):
            elapsed = OFFLINE_NOTIFY_COOLDOWN_SECONDS + 1
        if elapsed < OFFLINE_NOTIFY_COOLDOWN_SECONDS:
            return False
    return True


def _offline_notify_record(offline_ids: list[str]) -> None:
    OFFLINE_NOTIFY_DEDUP_PATH.parent.mkdir(parents=True, exist_ok=True)
    OFFLINE_NOTIFY_DEDUP_PATH.write_text(json.dumps({"last_set": ",".join(sorted(offline_ids)), "last_at": now()}, ensure_ascii=False), encoding="utf-8")


def cmd_employee_offline_report(args: argparse.Namespace) -> int:
    conn = connect()
    report = employee_offline_report_internal(conn, stale_minutes=args.stale_minutes, dormant_minutes=getattr(args, "dormant_minutes", 1440))
    if args.notify:
        offline = report["offline"]
        offline_ids = [e["id"] for e in offline]
        # --dedup (scheduled callers) suppresses repeat notifications for an unchanged offline set
        if getattr(args, "dedup", False) and not _offline_notify_should_send(offline_ids, force=False):
            report["notification"] = {"ok": True, "skipped": True, "reason": "offline set unchanged within cooldown"}
        else:
            if offline:
                lines = "\n".join(f"• {e['name']}（{e['runtime']}）最后在线 {e['last_seen_at'] or '从未'}" for e in offline)
                msg = f"📴 离线员工 {len(offline)}/{report['counts']['active']}（{args.stale_minutes} 分钟无心跳）：\n{lines}"
            else:
                msg = f"✅ 全员在线（{report['counts']['active']}/{report['counts']['active']}）"
            report["notification"] = notification_send_result(kind="general", subject="Company Kernel 员工在线状态", message=msg)
            _offline_notify_record(offline_ids)
    emit(report)
    return 0


def mirror_owner_message_to_telegram(target: str, body: str, source: str = "") -> dict:
    """Owner-addressed messages (watchdog alerts, Hermes status reports) also get pushed to the owner's
    Telegram via the configured notification route — not just dropped in the inbox file, which the owner
    may never open. Gated to the owner only (inter-agent messages never mirror). Best-effort: returns a
    result but never raises, so a Telegram hiccup can't break message delivery."""
    try:
        owner = os.environ.get("COMPANY_KERNEL_OWNER", "owner")
        if resolve_employee_alias(target) != owner:
            return {"skipped": True}
        subject = f"📨 {source} → 你" if source else "📨 Company Kernel"
        return notification_send_result(message=str(body or ""), subject=subject, kind="error")
    except Exception as exc:  # never let notification failure break the message send
        return {"ok": False, "error": str(exc)}


def cmd_message_send(args: argparse.Namespace) -> int:
    conn = connect()
    result = send_message_internal(conn, source=args.source, target=args.target, body=args.body, message_id=args.message_id)
    telegram = mirror_owner_message_to_telegram(args.target, args.body, args.source)
    emit({"ok": True, **result, "telegram_mirror": telegram})
    return 0


def resolve_line_token(agent: str) -> str:
    """Resolve an OpenClaw agent's LINE channel access token from the environment (secrets.env)."""
    key = "LINE_" + agent.upper().replace("-", "_") + "_CHANNEL_ACCESS_TOKEN"
    return os.environ.get(key) or os.environ.get("LINE_DEFAULT_CHANNEL_ACCESS_TOKEN", "")


def send_line_push(token: str, to: str, text: str, timeout: int = 20) -> dict:
    """Push a text message directly to a LINE user/group/room via the Messaging API."""
    if not token:
        raise ValueError("LINE channel access token is not configured")
    if not to:
        raise ValueError("LINE target id is required")
    data = json.dumps({"to": to, "messages": [{"type": "text", "text": text}]}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request("https://api.line.me/v2/bot/message/push", data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")
    return {"ok": True, "platform": "line", "to": to, "response": body or "ok"}


def channel_send_internal(*, agent: str, channel: str, target_id: str, group_code: str, target_name: str, body: str, by: str = "owner") -> dict:
    """Pure outbound message to an external channel (LINE group / Telegram chat) — NOT a task, not an
    agent invocation. Delivers the text straight to the customer channel and records it for the console feed."""
    channel = (channel or "line").strip().lower()
    text = body.strip()
    if not text:
        return {"ok": False, "error": "empty message body"}
    if not target_id:
        return {"ok": False, "error": "missing target id"}
    if channel == "line":
        token = resolve_line_token(agent)
        try:
            sent = send_line_push(token, target_id, text)
        except (ValueError, urllib.error.URLError, TimeoutError, OSError) as exc:
            return {"ok": False, "error": str(exc), "channel": channel, "target_id": target_id}
    elif channel == "telegram":
        settings = notification_settings()
        account = settings.get("telegram_accounts", {}).get(agent) or settings.get("telegram_accounts", {}).get("default", {})
        token = os.environ.get(str(account.get("bot_token_env", "") or ""), "")
        try:
            sent = send_telegram_notification(token=token, chat_id=target_id, text=text)
        except (ValueError, urllib.error.URLError, TimeoutError) as exc:
            return {"ok": False, "error": str(exc), "channel": channel, "target_id": target_id}
    else:
        return {"ok": False, "error": f"unsupported channel: {channel}"}
    # record the outbound message so it shows in the console message history under the owning agent
    conn = connect()
    try:
        mid = f"msg-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        label = target_name or group_code or target_id
        conn.execute(
            "INSERT INTO messages(id, source_agent, target_agent, body, created_at) VALUES (?, ?, ?, ?, ?)",
            (mid, resolve_employee_alias(by), resolve_employee_alias(agent), f"[→ {channel} 群「{label}」] {text}", now()),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "channel": channel, "agent": agent, "target_id": target_id, "group": group_code, "target_name": target_name, "message_id": mid, "delivery": sent}


def cmd_message_channel_send(args: argparse.Namespace) -> int:
    agent = resolve_employee_alias(args.agent)
    # resolve group_code -> target_id via the agent's channel_target_registry (reuse openclaw_adapter logic)
    from company_kernel import openclaw_adapter
    target_id = args.target_id
    group_code = args.group_code
    target_name = ""
    channel = args.channel
    if not target_id and group_code:
        resolved = openclaw_adapter.resolve_deliver_to(agent, {"channel": channel, "group_code": group_code})
        if not resolved.get("resolved"):
            emit({"ok": False, "error": resolved.get("error", "group not found"), "agent": agent, "group_code": group_code})
            return 2
        target_id = resolved.get("target_id", "")
        target_name = resolved.get("target_name", "")
        channel = resolved.get("channel", channel)
    result = channel_send_internal(agent=agent, channel=channel, target_id=target_id, group_code=group_code, target_name=target_name, body=args.body, by=args.by)
    emit(result)
    return 0 if result.get("ok") else 2


def parse_openclaw_payload_text(stdout: str) -> str:
    payload = parse_json_output(stdout)
    result = payload.get("result") if isinstance(payload, dict) else {}
    payloads = result.get("payloads") if isinstance(result, dict) else []
    if isinstance(payloads, list):
        texts = [str(item.get("text", "")) for item in payloads if isinstance(item, dict) and item.get("text")]
        if texts:
            return "\n".join(texts)
    return str(payload.get("summary") or payload.get("reply") or "")


def direct_runtime_command(runtime: str, target: str, source: str, body: str, timeout: int, session_key: str, args: argparse.Namespace) -> tuple[list[str], str]:
    if runtime in {"openclaw", "hermes"}:
        agent_runtime_id = attendance_agent_runtime_id(target, runtime)
        cmd = ["openclaw", "agent", "--agent", agent_runtime_id, "--session-key", session_key, "--message", body, "--timeout", str(timeout), "--json"]
        if args.deliver:
            cmd.append("--deliver")
        if args.reply_channel:
            cmd.extend(["--reply-channel", args.reply_channel])
        if args.reply_to:
            cmd.extend(["--reply-to", args.reply_to])
        if args.reply_account:
            cmd.extend(["--reply-account", args.reply_account])
        return cmd, agent_runtime_id
    if runtime == "codex":
        return [
            str(ROOT / "bin" / "company-codex-adapter"),
            "--agent",
            target,
            "--direct-message",
            body,
            "--direct-source",
            source,
            "--direct-session-key",
            session_key,
        ], target
    if runtime == "antigravity":
        return [
            str(ROOT / "bin" / "company-antigravity-adapter"),
            "--agent",
            target,
            "--direct-message",
            body,
            "--direct-source",
            source,
            "--direct-session-key",
            session_key,
            "--timeout",
            str(timeout),
        ], target
    if runtime in {"claude", "trae"}:
        return [
            str(ROOT / "bin" / f"company-{runtime}-adapter"),
            "--agent",
            target,
            "--direct-message",
            body,
            "--direct-source",
            source,
            "--direct-session-key",
            session_key,
            "--timeout",
            str(timeout),
        ], target
    raise ValueError(f"direct send unsupported runtime: {runtime}")


def cmd_message_direct(args: argparse.Namespace) -> int:
    conn = connect()
    source = resolve_employee_alias(args.source)
    target = resolve_employee_alias(args.target)
    require_employee(conn, source)
    target_row = conn.execute("SELECT * FROM employees WHERE id = ?", (target,)).fetchone()
    if not target_row:
        raise SystemExit(f"unknown employee: {target}")
    target_employee = dict(target_row)
    require_communication_allowed(source, target, "message.direct")
    runtime = str(target_employee.get("runtime") or "")
    runtime_agent_id = attendance_agent_runtime_id(target, runtime)
    session_key = args.session_key or f"agent:{runtime_agent_id}:{source}"
    defaults = direct_reply_defaults(source, target)
    args.deliver = bool(args.deliver or defaults["deliver"])
    if not args.reply_channel:
        args.reply_channel = str(defaults["reply_channel"])
    if not args.reply_account:
        args.reply_account = str(defaults["reply_account"])
    if not args.reply_to:
        args.reply_to = str(defaults["reply_to"])
    try:
        cmd, agent_runtime_id = direct_runtime_command(runtime, target, source, args.body, args.timeout, session_key, args)
    except ValueError:
        emit({"ok": False, "error": "direct send unsupported runtime", "target": target, "runtime": runtime})
        return 2
    try:
        cp = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True, timeout=args.timeout + 10)
    except Exception as exc:
        unavailable = mark_employee_unavailable(conn, target, str(exc))
        conn.commit()
        emit({"ok": False, "error": str(exc), "target": target, "runtime": runtime, "session_key": session_key, "command": cmd, "employee_unavailable": unavailable})
        return 1
    adapter_payload = parse_json_output(cp.stdout)
    reply = parse_openclaw_payload_text(cp.stdout)
    message_record = send_message_internal(conn, source=source, target=target, body=args.body, message_id=args.message_id)
    receipt_record = None
    if cp.returncode == 0 and reply:
        receipt_id = f"{args.message_id}-receipt" if args.message_id else ""
        receipt_record = send_message_internal(
            conn,
            source=target,
            target=source,
            body=reply,
            message_id=receipt_id,
        )
    for event_id in [message_record.get("event_id"), receipt_record.get("event_id") if receipt_record else ""]:
        if event_id:
            conn.execute("UPDATE company_events SET processed_at = ? WHERE id = ?", (now(), event_id))
    conn.commit()
    delivery_status = adapter_payload.get("deliveryStatus") if isinstance(adapter_payload.get("deliveryStatus"), dict) else {}
    delivery_failed = bool(delivery_status.get("attempted") and delivery_status.get("succeeded") is False)
    ok = cp.returncode == 0 and not delivery_failed
    result = {
        "ok": ok,
        "source": source,
        "target": target,
        "runtime": runtime,
        "agent_runtime_id": agent_runtime_id,
        "session_key": session_key,
        "deliver": bool(args.deliver),
        "reply_channel": args.reply_channel,
        "reply_account": args.reply_account,
        "reply_to": args.reply_to,
        "reply": reply,
        "activation_eligible": adapter_payload.get("activation_eligible", True),
        "adapter_payload": adapter_payload,
        "exit_code": cp.returncode,
        "message": message_record["message"],
        "file": message_record["file"],
        "receipt": receipt_record["message"] if receipt_record else None,
        "receipt_file": receipt_record["file"] if receipt_record else "",
        "stderr": cp.stderr[-2000:],
    }
    adapter_blocked = bool(adapter_payload.get("blocked_execution")) and not delivery_failed
    if adapter_blocked:
        result["adapter_blocked"] = {
            "blocked_execution": True,
            "blocker": str(adapter_payload.get("blocker") or adapter_payload.get("error") or "adapter reported blocked execution"),
            "status_delivery": adapter_payload.get("status_delivery", {}),
        }
    elif cp.returncode != 0 or delivery_failed:
        reason = str(delivery_status.get("errorMessage") or cp.stderr[-1000:] or cp.stdout[-1000:] or f"direct command exit_code={cp.returncode}")
        result["employee_unavailable"] = mark_employee_unavailable(conn, target, reason)
        conn.commit()
    emit(result)
    return 0 if ok else 1


def cmd_message_list(args: argparse.Namespace) -> int:
    conn = connect()
    agent = resolve_employee_alias(args.agent)
    require_employee(conn, agent)
    emit(
        {
            "ok": True,
            "messages": rows(
                conn,
                "SELECT * FROM messages WHERE target_agent = ? OR source_agent = ? ORDER BY created_at DESC",
                (agent, agent),
            ),
        }
    )
    return 0


def cmd_followup_request(args: argparse.Namespace) -> int:
    source = resolve_employee_alias(args.source)
    target = resolve_employee_alias(args.target)
    followup_id = args.followup_id or f"followup-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    followup = {
        "id": followup_id,
        "source_agent": source,
        "target_agent": target,
        "question": args.question,
        "context": args.context,
        "deliver": bool(args.deliver),
        "reply_channel": args.reply_channel,
        "reply_account": args.reply_account,
        "reply_to": args.reply_to,
        "created_at": now(),
        "answered_at": "",
        "answer": "",
        "response_message_id": "",
    }
    path = save_followup(followup, "pending")
    if args.deliver:
        reply_args = argparse.Namespace(
            source=source,
            target=target,
            body=args.question,
            message_id=args.message_id or f"msg-{followup_id}",
            session_key=args.session_key,
            timeout=args.timeout,
            deliver=True,
            reply_channel=args.reply_channel,
            reply_account=args.reply_account,
            reply_to=args.reply_to,
        )
        cmd_message_direct(reply_args)
    emit({"ok": True, "followup": followup, "file": str(path)})
    return 0


def cmd_followup_reply(args: argparse.Namespace) -> int:
    followup, status, path = load_followup(args.followup_id)
    if status != "pending":
        emit({"ok": False, "error": f"followup is {status}", "followup": followup, "file": str(path)})
        return 1
    reply_args = argparse.Namespace(
        source=args.by,
        target=followup["source_agent"],
        body=args.answer,
        message_id=args.message_id or f"msg-{args.followup_id}-answer",
        session_key="",
        timeout=args.timeout,
        deliver=False,
        reply_channel="",
        reply_account="",
        reply_to="",
    )
    result_buf = io.StringIO()
    with contextlib.redirect_stdout(result_buf):
        code = cmd_message_direct(reply_args)
    result = json.loads(result_buf.getvalue()) if result_buf.getvalue().strip() else {}
    if code != 0:
        emit({"ok": False, "error": "followup reply delivery failed", "result": result, "followup": followup})
        return code
    followup["status"] = "answered"
    followup["answered_at"] = now()
    followup["answer"] = args.answer
    followup["response_message_id"] = result.get("message", {}).get("id", "")
    path.unlink(missing_ok=True)
    answered_path = save_followup(followup, "answered")
    emit({"ok": True, "followup": followup, "delivery": result, "file": str(answered_path)})
    return 0


def cmd_followup_show(args: argparse.Namespace) -> int:
    followup, status, path = load_followup(args.followup_id)
    followup["status"] = status
    emit({"ok": True, "followup": followup, "file": str(path)})
    return 0


def cmd_followup_list(args: argparse.Namespace) -> int:
    emit({"ok": True, "followups": list_followups(args.status)})
    return 0


USERS_CONFIG_PATH_NAME = "users.json"
RBAC_ROLES = ("viewer", "operator", "admin", "owner")


def users_config_path() -> Path:
    return ROOT / "config" / USERS_CONFIG_PATH_NAME


def load_users_config() -> dict:
    try:
        data = json.loads(users_config_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_users_config(config: dict) -> None:
    path = users_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)  # tokens inside — owner-only like secrets.env
    except OSError:
        pass


def cmd_user_add(args: argparse.Namespace) -> int:
    if args.role not in RBAC_ROLES:
        emit({"ok": False, "error": f"role must be one of {RBAC_ROLES}"})
        return 2
    config = load_users_config()
    tokens = config.setdefault("tokens", {})
    # one active token per user: drop any existing token for this user
    for tok in [t for t, info in tokens.items() if str((info or {}).get("user")) == args.user]:
        del tokens[tok]
    token = args.token or f"ck_{secrets.token_urlsafe(24)}"
    tokens[token] = {"user": args.user, "role": args.role, "created_at": now()}
    save_users_config(config)
    emit({"ok": True, "user": args.user, "role": args.role, "token": token,
          "note": "give this token to the user as the API Bearer token; it is stored only here (config/users.json, gitignored)"})
    return 0


def cmd_user_list(_args: argparse.Namespace) -> int:
    config = load_users_config()
    users = [{"user": (info or {}).get("user"), "role": (info or {}).get("role"), "created_at": (info or {}).get("created_at"),
              "token_hint": tok[:6] + "…"} for tok, info in (config.get("tokens") or {}).items()]
    emit({"ok": True, "rbac_enabled": bool(config.get("tokens")), "users": users})
    return 0


def cmd_user_remove(args: argparse.Namespace) -> int:
    config = load_users_config()
    tokens = config.get("tokens") or {}
    removed = [t for t, info in tokens.items() if str((info or {}).get("user")) == args.user]
    for t in removed:
        del tokens[t]
    save_users_config(config)
    emit({"ok": True, "user": args.user, "removed": len(removed)})
    return 0


def cmd_communication_pause(args: argparse.Namespace) -> int:
    agent = resolve_employee_alias(args.agent)
    result = set_employee_communication_enabled(agent, False, dry_run=False)
    emit({"ok": True, "paused": True, **result})
    return 0


def cmd_communication_resume(args: argparse.Namespace) -> int:
    agent = resolve_employee_alias(args.agent)
    result = set_employee_communication_enabled(agent, True, dry_run=False)
    emit({"ok": True, "paused": False, **result})
    return 0


def cmd_communication_show(args: argparse.Namespace) -> int:
    config = load_communication_config()
    employees = config.get("employees", {})
    source = resolve_employee_alias(args.agent) if args.agent else ""
    if source:
        info = employees.get(source, {})
        emit(
            {
                "ok": True,
                "policy": config.get("policy", {"mode": "open"}),
                "agent": source,
                "profile": info,
                "can_talk_to": communication_list(config, source, "can_talk_to"),
                "can_assign_to": communication_list(config, source, "can_assign_to"),
                "blocked_talk_to": communication_list(config, source, "blocked_talk_to"),
                "blocked_assign_to": communication_list(config, source, "blocked_assign_to"),
            }
        )
        return 0
    emit({"ok": True, "policy": config.get("policy", {"mode": "open"}), "aliases": config.get("aliases", {}), "employees": employees, "channels": config.get("channels", {})})
    return 0


def cmd_communication_check(args: argparse.Namespace) -> int:
    source = resolve_employee_alias(args.source)
    target = resolve_employee_alias(args.target)
    action = "task.submit" if args.action == "assign" else "message.send"
    decision = communication_policy_decision(source, target, action)
    emit({"ok": decision["allowed"], "decision": decision})
    return 0 if decision["allowed"] else 1


def cmd_notification_settings(_args: argparse.Namespace) -> int:
    emit(notification_settings())
    return 0


def cmd_notification_send(args: argparse.Namespace) -> int:
    result = notification_send_result(
        message=args.message,
        target=args.target,
        account_id=args.account,
        subject=args.subject,
        kind=args.kind,
        dry_run=args.dry_run,
    )
    emit(result)
    return 0 if result.get("ok") else 1


def cmd_supervisor_delivery_loop(args: argparse.Namespace) -> int:
    conn = connect()
    try:
        result = run_supervisor_delivery_loop(conn, limit=args.limit, actor=args.by)
    finally:
        conn.close()
    emit(result)
    return 0 if result.get("ok") else 1


def classify_policy_block(error: str) -> dict:
    text = str(error or "")
    if "tools.agentToAgent.allow" in text or "Agent-to-agent messaging denied" in text:
        return {
            "type": "agent_to_agent_denied",
            "approval_semantics": "not_user_popup_approvable",
            "reason": "tool policy denies direct agent-to-agent messaging before any command approval can be requested",
            "replacement": "Use Company Kernel direct messaging: bin/companyctl message direct --from <source> --to <target> --body '<message>'",
        }
    if "sessions_spawn" in text and "allowed: main" in text:
        return {
            "type": "session_spawn_denied",
            "approval_semantics": "not_user_popup_approvable",
            "reason": "current runtime only allows spawning main sessions, so specifying another agent is a policy error",
            "replacement": "Route through main or Company Kernel task/direct-message APIs instead of sessions_spawn --agent <target>.",
        }
    return {
        "type": "tool_policy_block",
        "approval_semantics": "unknown_or_not_popup_approvable",
        "reason": "external tool policy returned a blocker outside Company Kernel approval gates",
        "replacement": "Report the blocker through Company Kernel, then use an approved task or direct-message route.",
    }


def cmd_policy_block_report(args: argparse.Namespace) -> int:
    source = resolve_employee_alias(args.source) if args.source else ""
    target = resolve_employee_alias(args.target) if args.target else ""
    classification = classify_policy_block(args.error)
    payload = {
        "ok": False,
        "source": source,
        "target": target,
        "tool": args.tool,
        "operation": args.operation,
        "error": args.error,
        "classification": classification,
        "created_at": now(),
        "human_notice_required": True,
        "note": "This is a policy blocker, not a macOS/sudo/Codex approval popup. The operator must be notified instead of waiting for a nonexistent prompt.",
    }
    evidence_dir = STATE_DIR / "tool-policy-blocks"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    block_id = args.block_id or f"tool-policy-block-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    evidence_path = evidence_dir / f"{slug(block_id)}.json"
    payload["id"] = block_id
    payload["evidence"] = str(evidence_path)
    evidence_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    notification = notification_send_result(
        kind="error",
        subject=f"Company Kernel tool policy blocked: {classification['type']}",
        message=(
            f"source={source or '-'}\n"
            f"target={target or '-'}\n"
            f"tool={args.tool or '-'}\n"
            f"operation={args.operation or '-'}\n"
            f"error={args.error}\n"
            f"reason={classification['reason']}\n"
            f"replacement={classification['replacement']}\n"
            f"evidence={evidence_path}"
        ),
        dry_run=args.dry_run,
    )
    payload["notification"] = notification
    evidence_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    emit(payload)
    return 0


def cmd_policy_show(_args: argparse.Namespace) -> int:
    emit({"ok": True, "policy": load_policy_config(), "file": str(POLICY_PATH)})
    return 0


def load_protected_paths_config() -> dict:
    if not PROTECTED_PATHS_CONFIG.exists():
        return {
            "requires_rfc": True,
            "protected": ["company_kernel/**", "config/policy.json", "company.sqlite", "state/**", "employees/*/permissions.json"],
            "rfc_allowed": ["rfcs/**"],
        }
    return json.loads(PROTECTED_PATHS_CONFIG.read_text(encoding="utf-8"))


def normalize_repo_path(path: str) -> str:
    p = Path(path)
    if p.is_absolute():
        try:
            return p.resolve().relative_to(ROOT).as_posix()
        except ValueError:
            return p.as_posix().lstrip("/")
    return p.as_posix().lstrip("./")


def protected_path_decision(path: str, config: dict) -> dict:
    rel = normalize_repo_path(path)
    rfc_allowed = any(fnmatch.fnmatch(rel, pattern) for pattern in config.get("rfc_allowed", []))
    matched = [pattern for pattern in config.get("protected", []) if fnmatch.fnmatch(rel, pattern)]
    protected = bool(matched)
    allowed = not protected or rfc_allowed
    return {"path": rel, "protected": protected, "allowed": allowed, "matched": matched, "rfc_allowed": rfc_allowed}


def cmd_guard_check(args: argparse.Namespace) -> int:
    config = load_protected_paths_config()
    paths = list(args.path or []) + list(args.changed_file or [])
    if not paths:
        emit({"ok": True, "config": config, "checks": []})
        return 0
    checks = [protected_path_decision(path, config) for path in paths]
    blocked = [check for check in checks if not check["allowed"]]
    emit({"ok": not blocked, "requires_rfc": bool(config.get("requires_rfc", True)), "blocked": blocked, "checks": checks, "config_file": str(PROTECTED_PATHS_CONFIG)})
    return 1 if blocked else 0


def parse_participants(raw: str) -> list[str]:
    participants = []
    for item in raw.split(","):
        item = item.strip()
        if item and item not in participants:
            participants.append(item)
    return participants


def notify_conversation_participants(conversation_id: str, message: dict, participants: list[str]) -> dict[str, str]:
    files = {}
    for participant in participants:
        inbox = employee_paths(participant)["inbox"]
        inbox.mkdir(parents=True, exist_ok=True)
        path = inbox / f"{conversation_id}.{message['id']}.conversation.json"
        path.write_text(json.dumps({"type": "conversation_message", "conversation_id": conversation_id, "message": message}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        prune_inbox_dir(inbox)  # conversation notifications are the biggest source of inbox cruft
        files[participant] = str(path)
    return files


def conversation_start_internal(
    conn: sqlite3.Connection,
    *,
    source: str,
    participants: list[str],
    title: str,
    body: str,
    evidence: str = "",
    conversation_id: str = "",
    project_id: str = "",
) -> dict:
    source = resolve_employee_alias(source)
    participants = [resolve_employee_alias(participant) for participant in participants]
    require_employee(conn, source)
    if source not in participants:
        participants.insert(0, source)
    for participant in participants:
        require_employee(conn, participant)
        if participant != source:
            require_communication_allowed(source, participant, "conversation.start")
    cid = conversation_id or f"conv-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    message_id = f"cmsg-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    ts = now()
    conn.execute(
        """
        INSERT INTO conversations(id, title, created_by, participants_json, status, project_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'open', ?, ?, ?)
        """,
        (cid, title, source, json.dumps(participants, ensure_ascii=False), project_id, ts, ts),
    )
    conn.execute(
        """
        INSERT INTO conversation_messages(id, conversation_id, source_agent, body, evidence_path, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (message_id, cid, source, body, evidence, ts),
    )
    conn.commit()
    message = {"id": message_id, "source_agent": source, "body": body, "evidence_path": evidence, "created_at": ts}
    files = notify_conversation_participants(cid, message, participants)
    event = record_event(
        conn,
        "conversation.message",
        source,
        payload={"conversation_id": cid, "message_id": message_id, "participants": participants, "body": body, "evidence": evidence, "files": files, "is_start": True},
    )
    audit(conn, source, "conversation.start", cid, {"title": title, "participants": participants, "event_id": event["id"]})
    return {"conversation": {"id": cid, "title": title, "participants": participants, "status": "open"}, "message": message, "files": files, "event_id": event["id"]}


def meeting_request_internal(conn: sqlite3.Connection, *, requester: str, topic: str, participants,
                             question: str, project_id: str = "", mode: str = "discuss",
                             rounds: int = 1, synthesizer: str = "") -> dict:
    """An employee calls a quick meeting to settle a hard decision it can't decide alone. Creates the
    conversation and launches the autonomous run DETACHED — a meeting takes minutes (each turn spawns
    a runtime), so the requester must NOT block on it. The discussion + conclusion then show up in the
    Overview feed / Conversations tab / project memory; the requester polls meeting_result for the
    outcome. This is the agent-initiated counterpart to the owner/console 发起会议."""
    parts = participants if isinstance(participants, list) else [p.strip() for p in str(participants).split(",")]
    parts = [p for p in parts if p]
    if requester not in parts:
        parts.insert(0, requester)
    started = conversation_start_internal(conn, source=requester, participants=parts, title=topic,
                                          body=question, conversation_id="", project_id=project_id)
    cid = started["conversation"]["id"]
    argv = [str(ROOT / "bin" / "companyctl"), "conversation", "run", "--conversation-id", cid,
            "--mode", mode, "--rounds", str(int(rounds))]
    if synthesizer:
        argv += ["--synthesizer", synthesizer]
    if project_id:
        argv += ["--project", project_id]
    log_fh = None
    try:
        log_dir = ROOT / "logs" / "conversation-run"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_fh = open(log_dir / f"{cid}.log", "ab")
        subprocess.Popen(argv, cwd=str(ROOT), stdout=log_fh, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "conversation_id": cid, "error": f"会议已建但后台讨论未能启动:{exc}"}
    finally:
        if log_fh is not None:
            try:
                log_fh.close()
            except Exception:
                pass
    return {"ok": True, "conversation_id": cid, "status": "running", "participants": parts,
            "note": "讨论已在后台进行;用 `meeting result --conversation-id` 轮询结论,或控制台「对话」标签查看全过程。"}


def meeting_result_internal(conn: sqlite3.Connection, conversation_id: str) -> dict:
    """Read back a meeting's conclusion (the chair's 【方案/决策】/【会议纪要】/【站会汇总】) so a
    requesting agent can poll for the outcome of a meeting it started."""
    msgs = rows(conn, "SELECT * FROM conversation_messages WHERE conversation_id = ? ORDER BY created_at ASC", (conversation_id,))
    if not msgs:
        return {"ok": False, "conversation_id": conversation_id, "error": "无此会议或尚无发言"}
    conclusion = ""
    chair_failed = False
    for m in reversed(msgs):
        body = str(m["body"]).lstrip()
        if any(body.startswith(prefix) for prefix in CONVERSATION_SYNTH_PREFIX.values()):
            conclusion = str(m["body"])
            break
        # the chair tried but failed to write minutes — the meeting IS over (no verdict). Without this
        # the poller would wait forever thinking colleagues are still talking. Report done + failed.
        if body.startswith(MEETING_SYSNOTE_PREFIX) and MEETING_CHAIR_FAIL_MARK in body:
            chair_failed = True
            break
    status = "concluded" if conclusion else ("chair_failed" if chair_failed else "in_progress")
    return {"ok": True, "conversation_id": conversation_id, "done": bool(conclusion) or chair_failed,
            "status": status, "chair_failed": chair_failed, "conclusion": conclusion, "turns": len(msgs),
            "transcript": [{"speaker": m["source_agent"], "body": str(m["body"])[:400]} for m in msgs]}


def cmd_employee_install_integration(args: argparse.Namespace) -> int:
    """Install the company-kernel MCP + 'you are an employee' instructions into the agent runtime's
    own config, so it's truly on-duty (knows it can use the kernel), not just listed in the DB."""
    from company_kernel import integration_installer
    out = integration_installer.install_for_runtime(
        args.runtime, agent_id=getattr(args, "agent_id", "") or None, dry_run=bool(getattr(args, "dry_run", False)))
    emit(out)
    return 0 if out.get("ok") else 1


def cmd_employee_ensure_owner(args: argparse.Namespace) -> int:
    conn = connect()
    owner = ensure_human_owner(conn, owner_id=getattr(args, "owner_id", "owner") or "owner")
    emit({"ok": True, "owner": {"id": owner["id"], "name": owner["name"], "role": owner["role"]}})
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """Guided first-run setup — detect installed agent CLIs, offer to add them as employees, etc."""
    from company_kernel import init_wizard
    return init_wizard.run_init(args)


def cmd_meeting_request(args: argparse.Namespace) -> int:
    conn = connect()
    out = meeting_request_internal(conn, requester=args.source, topic=args.topic,
                                   participants=args.participants, question=args.question,
                                   project_id=getattr(args, "project", "") or "", mode=args.mode,
                                   rounds=args.rounds, synthesizer=args.synthesizer)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


def cmd_meeting_result(args: argparse.Namespace) -> int:
    conn = connect()
    out = meeting_result_internal(conn, args.conversation_id)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


def ensure_human_owner(conn: sqlite3.Connection, owner_id: str = "owner") -> dict:
    row = conn.execute("SELECT * FROM employees WHERE id = ?", (owner_id,)).fetchone()
    ts = now()
    workspace = str((ROOT / "employees" / owner_id).resolve())
    if row:
        conn.execute(
            "UPDATE employees SET name = ?, role = ?, runtime = ?, workspace = ?, status = 'active', updated_at = ? WHERE id = ?",
            ("Owner", "human-owner", "human", workspace, ts, owner_id),
        )
    else:
        conn.execute(
            "INSERT INTO employees(id, name, role, runtime, workspace, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
            (owner_id, "Owner", "human-owner", "human", workspace, ts, ts),
        )
    conn.commit()
    paths = employee_paths(owner_id)
    paths["base"].mkdir(parents=True, exist_ok=True)
    for key in ("inbox", "outbox", "reports"):
        paths[key].mkdir(parents=True, exist_ok=True)
    return dict(conn.execute("SELECT * FROM employees WHERE id = ?", (owner_id,)).fetchone())


def cmd_conversation_start(args: argparse.Namespace) -> int:
    conn = connect()
    if resolve_employee_alias(args.source) == "owner":
        ensure_human_owner(conn)
    participants = parse_participants(args.participants)
    result = conversation_start_internal(conn, source=args.source, participants=participants, title=args.title, body=args.body, evidence=args.evidence, conversation_id=args.conversation_id, project_id=getattr(args, "project", "") or "")
    emit({"ok": True, **result})
    return 0


def conversation_reply_internal(
    conn: sqlite3.Connection,
    *,
    source: str,
    conversation_id: str,
    body: str,
    evidence: str = "",
    message_id: str = "",
) -> dict:
    source = resolve_employee_alias(source)
    require_employee(conn, source)
    conv = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
    if not conv:
        raise SystemExit(f"conversation not found: {conversation_id}")
    participants = json.loads(conv["participants_json"])
    if source not in participants:
        if source == "owner":
            participants.insert(0, source)
            conn.execute(
                "UPDATE conversations SET participants_json = ? WHERE id = ?",
                (json.dumps(participants, ensure_ascii=False), conversation_id),
            )
        else:
            raise SystemExit(f"source is not a participant: {source}")
    mid = message_id or f"cmsg-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    ts = now()
    conn.execute(
        """
        INSERT INTO conversation_messages(id, conversation_id, source_agent, body, evidence_path, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (mid, conversation_id, source, body, evidence, ts),
    )
    conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (ts, conversation_id))
    conn.commit()
    message = {"id": mid, "source_agent": source, "body": body, "evidence_path": evidence, "created_at": ts}
    files = notify_conversation_participants(conversation_id, message, participants)
    event = record_event(
        conn,
        "conversation.message",
        source,
        payload={"conversation_id": conversation_id, "message_id": mid, "participants": participants, "body": body, "evidence": evidence, "files": files, "is_start": False},
    )
    audit(conn, source, "conversation.reply", conversation_id, {"message_id": mid, "event_id": event["id"]})
    return {"conversation_id": conversation_id, "message": message, "files": files, "event_id": event["id"]}


def conversation_join_internal(
    conn: sqlite3.Connection,
    *,
    agent: str,
    conversation_id: str,
) -> dict:
    agent = resolve_employee_alias(agent)
    if agent == "owner":
        ensure_human_owner(conn, agent)
    require_employee(conn, agent)
    conv = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
    if not conv:
        raise SystemExit(f"conversation not found: {conversation_id}")
    participants = json.loads(conv["participants_json"])
    joined = False
    if agent not in participants:
        participants.insert(0, agent) if agent == "owner" else participants.append(agent)
        conn.execute(
            "UPDATE conversations SET participants_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(participants, ensure_ascii=False), now(), conversation_id),
        )
        conn.commit()
        joined = True
    audit(conn, agent, "conversation.join", conversation_id, {"participants": participants, "joined": joined})
    return {"conversation": {"id": conversation_id, "title": conv["title"], "participants": participants, "status": conv["status"]}, "joined": joined}


def cmd_conversation_reply(args: argparse.Namespace) -> int:
    conn = connect()
    if resolve_employee_alias(args.source) == "owner":
        ensure_human_owner(conn)
    result = conversation_reply_internal(conn, source=args.source, conversation_id=args.conversation_id, body=args.body, evidence=args.evidence, message_id=args.message_id)
    emit({"ok": True, **result})
    return 0


def cmd_conversation_join(args: argparse.Namespace) -> int:
    conn = connect()
    result = conversation_join_internal(conn, agent=args.agent, conversation_id=args.conversation_id)
    emit({"ok": True, **result})
    return 0


def cmd_conversation_list(args: argparse.Namespace) -> int:
    conn = connect()
    if resolve_employee_alias(args.agent) == "owner":
        ensure_human_owner(conn)
    require_employee(conn, args.agent)
    all_rows = rows(conn, "SELECT * FROM conversations ORDER BY updated_at DESC")
    conversations = []
    for conv in all_rows:
        participants = json.loads(conv["participants_json"])
        if args.agent in participants:
            conv["participants"] = participants
            del conv["participants_json"]
            conversations.append(conv)
    emit({"ok": True, "conversations": conversations})
    return 0


def cmd_conversation_show(args: argparse.Namespace) -> int:
    conn = connect()
    conv = conn.execute("SELECT * FROM conversations WHERE id = ?", (args.conversation_id,)).fetchone()
    if not conv:
        emit({"ok": False, "error": "conversation not found", "conversation_id": args.conversation_id})
        return 1
    obj = dict(conv)
    obj["participants"] = json.loads(obj.pop("participants_json"))
    messages = rows(conn, "SELECT * FROM conversation_messages WHERE conversation_id = ? ORDER BY created_at ASC", (args.conversation_id,))
    emit({"ok": True, "conversation": obj, "messages": messages})
    return 0


# Runtimes that can be invoked headlessly to produce a discussion turn. gemini rides on
# the claude adapter bin (it is an Anthropic-compatible proxy runtime), codex/antigravity/trae
# each have their own adapter, openclaw/hermes go through the openclaw agent CLI.
CONVERSATION_RUNTIME_BINS = {
    "codex": "company-codex-adapter",
    "claude": "company-claude-adapter",
    "gemini": "company-claude-adapter",
    "antigravity": "company-antigravity-adapter",
    "trae": "company-trae-adapter",
}
CONVERSATION_RUNTIME_SUPPORTED = set(CONVERSATION_RUNTIME_BINS) | {"openclaw", "hermes"}


def conversation_invoke_runtime(conn: sqlite3.Connection, agent: str, prompt: str, timeout: int, memory_key: str = "") -> dict:
    """Invoke an employee's runtime headlessly with a discussion prompt and return its reply.
    Reuses the same adapter --direct-message path that direct messaging uses, so a participant
    speaks with its real model (codex=gpt-5.5, claude=Opus 4.8 native, gemini=proxy, etc.).
    When memory_key is set, claude/gemini reuse ONE session across the whole conversation so each
    participant natively remembers prior turns (less thread re-feed, no per-turn re-scan)."""
    row = conn.execute("SELECT * FROM employees WHERE id = ?", (agent,)).fetchone()
    if not row:
        return {"ok": False, "reply": "", "error": f"unknown employee: {agent}"}
    emp = dict(row)
    runtime = str(emp.get("runtime") or "")
    session_key = f"conversation:{agent}"
    if runtime in {"openclaw", "hermes"}:
        agent_runtime_id = attendance_agent_runtime_id(agent, runtime)
        cmd = ["openclaw", "agent", "--agent", agent_runtime_id, "--session-key", session_key,
               "--message", prompt, "--timeout", str(timeout), "--json"]
    elif runtime in CONVERSATION_RUNTIME_BINS:
        # codex's --direct-message path is execution-oriented (it does work + reports status);
        # for a discussion we want answer-only, so use its dedicated --converse-message mode.
        msg_flag = "--converse-message" if runtime == "codex" else "--direct-message"
        cmd = [str(ROOT / "bin" / CONVERSATION_RUNTIME_BINS[runtime]),
               "--agent", agent, msg_flag, prompt,
               "--direct-source", "owner", "--direct-session-key", session_key,
               "--timeout", str(timeout)]
        # claude/gemini (claude adapter), codex, and antigravity(agy) all support persistent memory
        # sessions now — each participant natively remembers prior turns of THIS conversation.
        if memory_key and runtime in {"claude", "gemini", "codex", "antigravity"}:
            cmd.extend(["--memory-session", f"conv:{memory_key}:{agent}"])
    else:
        return {"ok": False, "reply": "", "error": f"unsupported runtime: {runtime}", "runtime": runtime}
    try:
        cp = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True, timeout=timeout + 20)
    except Exception as exc:
        return {"ok": False, "reply": "", "error": str(exc), "runtime": runtime}
    reply = (parse_openclaw_payload_text(cp.stdout) or "").strip()
    return {
        "ok": cp.returncode == 0 and bool(reply),
        "reply": reply,
        "runtime": runtime,
        "exit_code": cp.returncode,
        "stderr": cp.stderr[-800:],
    }


# System notes (a speaker/chair failure surfaced INTO the thread for visibility) carry this prefix.
# They are shown in the console but MUST be excluded from the context fed to later speakers and the
# synthesizer — otherwise a "⚠️ codex hit 529" line leaks into the minutes and next-round reasoning.
MEETING_SYSNOTE_PREFIX = "⚠️〔会议系统〕"
# Shared marker for the chair-failed-to-synthesize note. Used at BOTH the write site (the failure note)
# and the read site (meeting_result detecting a concluded-but-verdict-less meeting), so changing the
# wording can't silently break the "done despite no minutes" detection.
MEETING_CHAIR_FAIL_MARK = "主持人未能出纪要"


def conversation_thread_text(conn: sqlite3.Connection, conversation_id: str, limit: int = 40) -> str:
    msgs = rows(conn, "SELECT * FROM conversation_messages WHERE conversation_id = ? ORDER BY created_at ASC", (conversation_id,))
    real = [m for m in msgs if not str(m["body"]).startswith(MEETING_SYSNOTE_PREFIX)]
    return "\n".join(f"{m['source_agent']}: {m['body']}" for m in real[-limit:])


# Meeting modes. "meeting" = sync goals/norms (each employee confirms understanding, raises
# blockers, claims action items; chair writes minutes). "standup" = progress/blockers sync.
# "discuss" = open debate that converges to a plan (design review / proposal vetting).
CONVERSATION_MODES = {"meeting", "discuss", "standup"}
CONVERSATION_SYNTH_PREFIX = {"meeting": "【会议纪要】", "standup": "【站会汇总】", "discuss": "【方案/决策】"}


def conversation_speaker_prompt(mode: str, spk: str, title: str, thread: str) -> str:
    ctx = f"议题：{title}\n\n到目前为止的记录：\n{thread or '（暂无发言，你较早发言）'}\n\n"
    if mode == "meeting":
        return (
            f"你是公司内部员工「{spk}」，正在参加一场公司内部会议（同步目标 / 规范 / 流程）。\n"
            f"{ctx}"
            f"请以 {spk} 的身份简短发言（3-6 句）：\n"
            f"1) 用你自己的话复述你对本次目标/规范的关键理解，证明你确实听懂了；\n"
            f"2) 提出与你职责相关的疑问、风险或执行障碍；\n"
            f"3) 认领与你相关的行动项（我负责什么、大致何时完成）。\n"
            f"不要空泛附和、不要客套。只输出你的发言正文，不加前缀或署名。"
        )
    if mode == "standup":
        return (
            f"你是公司内部员工「{spk}」，正在参加团队站会。\n"
            f"{ctx}"
            f"请以 {spk} 的身份简短同步（3-5 句）：1) 最近进展；2) 当前阻塞 / 风险；"
            f"3) 下一步计划；4) 需要谁协助。只输出你的发言正文，不加前缀或署名。"
        )
    return (
        f"你是公司内部员工「{spk}」，正在参与一场多员工内部讨论。\n"
        f"{ctx}"
        f"请以 {spk} 的身份简短发言（3-6 句）：提出你的观点、对他人观点的质疑或补充，"
        f"推动讨论向「可执行的方案/决策」收敛。不要重复别人已说过的话，不要客套。"
        f"只输出你的发言正文，不加前缀或署名。"
    )


def conversation_synth_prompt(mode: str, synth: str, title: str, thread: str) -> str:
    if mode == "meeting":
        role, body = "会议主持人", (
            "请输出结构化【会议纪要】，包含：\n"
            "1) 已对齐的目标 / 规范（明确结论）；\n"
            "2) 行动项清单（谁 · 做什么 · 大致何时）——尽量具体到可以直接派成任务；\n"
            "3) 待澄清 / 未决问题；\n"
            "4) 对齐确认（哪些员工已明确理解并认领；是否有人未表态或有异议）。"
        )
    elif mode == "standup":
        role, body = "站会主持人", (
            "请输出【站会汇总】，包含：\n"
            "1) 整体进度概览；2) 阻塞清单（谁卡在哪、需要什么）；"
            "3) 今日需协调 / 决策的事项；4) 风险提示。"
        )
    else:
        role, body = "讨论主持人", (
            "请综合各方发言，输出一份清晰的最终方案 / 决策，包含：\n"
            "1) 结论 / 决策；2) 关键理由；3) 可执行的下一步（谁做什么）；4) 仍存在的风险或待确认项。"
        )
    return (
        f"你是「{synth}」，本次{role}，负责收口。\n"
        f"议题：{title}\n\n完整记录：\n{thread}\n\n{body}\n"
        f"简洁、可执行、结构化。只输出正文，不加多余解释。"
    )


def meeting_capable_path() -> Path:
    # Resolved at call time (not import) so tests patching ROOT stay isolated.
    return ROOT / "state" / "meeting-capable.json"


def load_meeting_capable() -> dict:
    try:
        data = json.loads(meeting_capable_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_meeting_capable(data: dict) -> None:
    path = meeting_capable_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def probe_meeting_capability(conn: sqlite3.Connection, agent: str, timeout: int = 90) -> dict:
    """Invoke an employee with a tiny self-check prompt and record whether it genuinely
    replies. This is the authoritative 'can this employee join a meeting' signal — an
    on-demand openclaw agent may have no fresh heartbeat yet still answer when invoked."""
    prompt = "公司内部参会能力自检：请用一句话确认你能参加公司内部会议并发言（例如：我可以参会）。只输出这一句话。"
    res = conversation_invoke_runtime(conn, agent, prompt, timeout)
    return {
        "capable": bool(res.get("ok") and res.get("reply")),
        "runtime": res.get("runtime", ""),
        "checked_at": now(),
        "reply": (res.get("reply") or "")[:200],
        "error": res.get("error", ""),
        "exit_code": res.get("exit_code"),
    }


def conversation_candidate_agents(conn: sqlite3.Connection, *, active_only: bool = True) -> list[str]:
    out: list[str] = []
    for emp in rows(conn, "SELECT * FROM employees ORDER BY id"):
        if is_human_owner_employee(emp):
            continue
        if active_only and emp.get("status") != "active":
            continue
        if str(emp.get("runtime") or "") not in CONVERSATION_RUNTIME_SUPPORTED:
            continue
        out.append(emp["id"])
    return out


def conversation_probe_internal(conn: sqlite3.Connection, agents: list[str], *, timeout: int = 90, persist: bool = True) -> dict:
    state = load_meeting_capable() if persist else {}
    results: dict[str, dict] = {}
    for agent in agents:
        aid = resolve_employee_alias(agent)
        rec = probe_meeting_capability(conn, aid, timeout)
        results[aid] = rec
        state[aid] = rec
    if persist:
        save_meeting_capable(state)
    return results


def cmd_conversation_probe(args: argparse.Namespace) -> int:
    conn = connect()
    raw = (args.participants or "active").strip().lower()
    if raw in {"all", "active", ""}:
        agents = conversation_candidate_agents(conn, active_only=raw != "all")
    else:
        agents = parse_participants(args.participants)
    if not agents:
        emit({"ok": False, "error": "no candidate employees to probe"})
        return 1
    results = conversation_probe_internal(conn, agents, timeout=args.timeout, persist=not args.no_persist)
    capable = sorted(a for a, r in results.items() if r.get("capable"))
    incapable = sorted(a for a, r in results.items() if not r.get("capable"))
    emit({"ok": True, "probed": len(results), "capable": capable, "incapable": incapable, "results": results})
    return 0


def conversation_run_internal(
    conn: sqlite3.Connection,
    *,
    conversation_id: str,
    rounds: int = 2,
    timeout: int = 180,
    synthesizer: str = "",
    mode: str = "meeting",
    gate_capable: bool = True,
) -> dict:
    mode = mode if mode in CONVERSATION_MODES else "meeting"
    conv_row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
    if not conv_row:
        raise SystemExit(f"conversation not found: {conversation_id}")
    conv = dict(conv_row)
    title = conv.get("title") or conversation_id
    participants = json.loads(conv["participants_json"])
    rounds = max(1, int(rounds))
    # memory → meeting: if this conversation is tied to a project, every participant reads its shared
    # memory first, so they build on settled decisions instead of re-litigating them.
    project_id = str(conv.get("project_id") or "")
    project_digest = project_memory.digest_for_project(conn, project_id) if project_id else ""
    memory_preamble = (f"\n\n【本项目共享记忆 · 发言前先读】\n{project_digest}\n"
                       "请基于上面已沉淀的决策/约定/风险发言,不要重复或推翻已确认的结论(除非你有新证据)。\n") if project_digest else ""

    speakers: list[str] = []
    skipped: list[dict] = []
    for pid in participants:
        emp_row = conn.execute("SELECT * FROM employees WHERE id = ?", (pid,)).fetchone()
        if not emp_row:
            skipped.append({"agent": pid, "reason": "unknown"})
            continue
        emp = dict(emp_row)
        if is_human_owner_employee(emp):
            continue
        runtime = str(emp.get("runtime") or "")
        if runtime not in CONVERSATION_RUNTIME_SUPPORTED:
            skipped.append({"agent": pid, "reason": f"runtime {runtime or '?'} not invokable"})
            continue
        if emp.get("status") != "active":
            skipped.append({"agent": pid, "reason": f"status {emp.get('status')}"})
            continue
        speakers.append(pid)

    # Only admit employees that have genuinely demonstrated they can participate (a passing
    # `conversation probe`). Unknowns are probed lazily on first use so a never-tested employee
    # still works and self-populates the allowlist. Failures are excluded with a clear reason.
    if gate_capable and speakers:
        capable = load_meeting_capable()
        admitted: list[str] = []
        dirty = False
        for spk in speakers:
            rec = capable.get(spk)
            if rec is None:
                rec = probe_meeting_capability(conn, spk, timeout=min(timeout, 90))
                capable[spk] = rec
                dirty = True
            if rec.get("capable"):
                admitted.append(spk)
            else:
                reason = "未通过参会探测" + (f"：{rec.get('error')}" if rec.get("error") else "")
                skipped.append({"agent": spk, "reason": reason})
        if dirty:
            save_meeting_capable(capable)
        speakers = admitted
    if not speakers:
        raise SystemExit(f"no employee passed the participation check for {conversation_id} (run: companyctl conversation probe)")

    synth = resolve_employee_alias(synthesizer) if synthesizer else ""
    if synth and synth not in participants:
        conversation_join_internal(conn, agent=synth, conversation_id=conversation_id)
        participants.append(synth)
    if not synth:
        synth = "hermes" if "hermes" in speakers else speakers[-1]
    # The chair must itself be able to reply, or the minutes never get written. If the chosen
    # chair can't participate, fall back to a capable admitted speaker (prefer hermes).
    if gate_capable and synth not in speakers:
        cap = load_meeting_capable().get(synth)
        if cap is None:
            cap = probe_meeting_capability(conn, synth, timeout=min(timeout, 90))
            store = load_meeting_capable(); store[synth] = cap; save_meeting_capable(store)
        if not cap.get("capable"):
            fallback = "hermes" if "hermes" in speakers else speakers[-1]
            skipped.append({"agent": synth, "reason": f"主持人未通过参会探测，改由 {fallback} 出纪要"})
            synth = fallback

    transcript: list[dict] = []
    for rnd in range(1, rounds + 1):
        for spk in speakers:
            thread = conversation_thread_text(conn, conversation_id)
            prompt = conversation_speaker_prompt(mode, spk, title, thread) + memory_preamble
            res = conversation_invoke_runtime(conn, spk, prompt, timeout, memory_key=conversation_id)
            if res.get("ok") and res.get("reply"):
                conversation_reply_internal(conn, source=spk, conversation_id=conversation_id, body=res["reply"])
                transcript.append({"round": rnd, "speaker": spk, "ok": True, "reply": res["reply"]})
            else:
                reason = (res.get("error") or f"exit_code={res.get('exit_code')}")
                # surface the failure INSIDE the meeting so a partial/empty meeting is never silent — the
                # owner sees "codex-cli hit 529/timeout" instead of a blank room. The MEETING_SYSNOTE_PREFIX
                # keeps it out of later speakers'/synth context (so it can't pollute the minutes).
                conversation_reply_internal(conn, source=spk, conversation_id=conversation_id,
                                            body=f"{MEETING_SYSNOTE_PREFIX} {spk} 本轮未能发言:{str(reason)[:140]}(已跳过,可稍后重跑本会)")
                transcript.append({"round": rnd, "speaker": spk, "ok": False,
                                   "error": reason, "stderr": res.get("stderr", "")})

    # Durably exclude a participant that failed EVERY round (its runtime is unreliable headless):
    # drop it from the meeting-capable cache so the NEXT meeting re-probes it and skips it until it
    # genuinely recovers — "不可靠的别参会", without permanently banning it on a single transient blip.
    if gate_capable:
        succeeded = {t["speaker"] for t in transcript if t.get("ok")}
        broken = [s for s in speakers if s not in succeeded]
        if broken:
            store = load_meeting_capable()
            if any(store.pop(b, None) is not None for b in broken):
                save_meeting_capable(store)

    prefix = CONVERSATION_SYNTH_PREFIX.get(mode, "【方案/决策】")
    # Synthesis with FALLBACK so a chair that dies at runtime (e.g. a slow/unreliable runtime timing
    # out) never leaves the meeting verdict-less. Order: the designated chair first, then the meeting
    # INITIATOR (created_by) — "谁发起谁收口" — then any other speaker who took part. Each must be an
    # admitted speaker. First one to produce minutes wins.
    initiator = str(conv.get("created_by") or "")
    synth_chain: list[str] = []
    for cand in [synth, initiator, *speakers]:
        if cand and (cand == synth or cand in speakers) and cand not in synth_chain:
            synth_chain.append(cand)
    final_plan = ""
    captured_memory = None
    used_synth = synth
    for idx, cand in enumerate(synth_chain):
        thread = conversation_thread_text(conn, conversation_id, limit=80)
        synth_prompt = conversation_synth_prompt(mode, cand, title, thread) + memory_preamble
        synth_res = conversation_invoke_runtime(conn, cand, synth_prompt, timeout, memory_key=conversation_id)
        if synth_res.get("ok") and synth_res.get("reply"):
            final_plan = synth_res["reply"]
            used_synth = cand
            if idx > 0:  # a fallback stepped in — make the handoff visible (kept out of the minutes' context)
                who = "发起人" if cand == initiator else "参会者"
                conversation_reply_internal(conn, source=cand, conversation_id=conversation_id,
                                            body=f"{MEETING_SYSNOTE_PREFIX} 主持人 {synth} 未能出纪要,改由{who} {cand} 收口")
            conversation_reply_internal(conn, source=cand, conversation_id=conversation_id,
                                        body=f"{prefix}\n{final_plan}")
            # meeting → memory: store the synthesized conclusion into the project memory bank so the
            # meeting's output is remembered, not lost. No-op if the conversation isn't tied to a project.
            try:
                entry = project_memory.capture_meeting_conclusion(
                    conn, project_id=project_id, title=title, conclusion=final_plan,
                    conversation_id=conversation_id, synthesizer=cand, mode=mode)
                captured_memory = entry["id"] if entry else None
            except Exception:
                captured_memory = None
            break
        s_reason = synth_res.get("error") or f"synthesis exit_code={synth_res.get('exit_code')}"
        transcript.append({"round": rounds + 1, "speaker": cand, "ok": False,
                           "error": s_reason, "stderr": synth_res.get("stderr", "")})
    if not final_plan:
        # every candidate failed — the meeting is over without a verdict; say so (done, not hanging).
        conversation_reply_internal(conn, source=synth, conversation_id=conversation_id,
                                    body=f"{MEETING_SYSNOTE_PREFIX} {MEETING_CHAIR_FAIL_MARK}:主席与发起人均未能出纪要 —— 稍后重跑或换主持人")
    synth = used_synth

    audit(conn, synth, "conversation.run", conversation_id,
          {"mode": mode, "rounds": rounds, "speakers": speakers, "synthesizer": synth, "turns": len(transcript)})
    return {
        "conversation_id": conversation_id,
        "title": title,
        "mode": mode,
        "rounds": rounds,
        "speakers": speakers,
        "synthesizer": synth,
        "skipped": skipped,
        "transcript": transcript,
        "final_plan": final_plan,
        "minutes_prefix": prefix,
        "project_id": project_id,
        "captured_memory": captured_memory,
    }


def cmd_conversation_run(args: argparse.Namespace) -> int:
    conn = connect()
    override = getattr(args, "project", "") or ""
    if override:  # let `conversation run --project X` set/override the bank this meeting feeds
        conn.execute("UPDATE conversations SET project_id = ?, updated_at = ? WHERE id = ?",
                     (override, now(), args.conversation_id))
        conn.commit()
    try:
        result = conversation_run_internal(
            conn,
            conversation_id=args.conversation_id,
            rounds=args.rounds,
            timeout=args.timeout,
            synthesizer=args.synthesizer,
            mode=args.mode,
            gate_capable=getattr(args, "gate_capable", True),
        )
    except SystemExit as exc:
        emit({"ok": False, "error": str(exc), "conversation_id": args.conversation_id})
        return 1
    ok = bool(result.get("final_plan")) or any(turn.get("ok") for turn in result.get("transcript", []))
    emit({"ok": ok, **result})
    return 0 if ok else 1


def task_conversation_ids(metadata: dict) -> list[str]:
    values = metadata.get("conversation_ids", [])
    if isinstance(values, str):
        values = [values] if values else []
    return [str(value) for value in values if str(value)]


def task_conversation_summary(conn: sqlite3.Connection, metadata: dict) -> dict:
    items = []
    total_messages = 0
    for conversation_id in task_conversation_ids(metadata):
        conv = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        if not conv:
            continue
        try:
            participants = json.loads(conv["participants_json"] or "[]")
        except json.JSONDecodeError:
            participants = []
        message_rows = rows(conn, "SELECT * FROM conversation_messages WHERE conversation_id = ? ORDER BY created_at ASC", (conversation_id,))
        total_messages += len(message_rows)
        latest = message_rows[-1] if message_rows else {}
        items.append(
            {
                "conversation_id": conversation_id,
                "title": conv["title"],
                "status": conv["status"],
                "participants": participants,
                "message_count": len(message_rows),
                "latest_message": latest.get("body", "") if latest else "",
                "latest_source_agent": latest.get("source_agent", "") if latest else "",
                "latest_at": latest.get("created_at", "") if latest else conv["updated_at"],
            }
        )
    return {"counts": {"conversations": len(items), "messages": total_messages}, "items": items}


def cmd_task_discuss(args: argparse.Namespace) -> int:
    conn = connect()
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
    if not task:
        emit({"ok": False, "error": "task not found", "task_id": args.task_id})
        return 1
    source = resolve_employee_alias(args.source or task["source_agent"])
    extra_participants = [resolve_employee_alias(item) for item in parse_participants(args.participants)]
    participants = []
    for participant in [source, task["source_agent"], task["target_agent"], task["claimed_by"], *extra_participants]:
        participant = resolve_employee_alias(str(participant or ""))
        if participant and participant not in participants:
            participants.append(participant)
    title = args.title or f"Task discussion: {task['id']} - {task['title']}"
    body = args.body or f"Discuss task `{task['id']}`: {task['title']}"
    conversation_id = args.conversation_id or f"conv-task-{slug(task['id'])}-{uuid.uuid4().hex[:6]}"
    result = conversation_start_internal(conn, source=source, participants=participants, title=title, body=body, evidence=args.evidence, conversation_id=conversation_id)
    metadata = task_metadata(conn, task["id"])
    conversation_ids = task_conversation_ids(metadata)
    if result["conversation"]["id"] not in conversation_ids:
        conversation_ids.append(result["conversation"]["id"])
    update_task_metadata(conn, task["id"], {"conversation_ids": conversation_ids})
    audit(conn, source, "task.discuss", task["id"], {"conversation_id": result["conversation"]["id"], "participants": participants})
    emit({"ok": True, "task_id": task["id"], **result, "conversation_ids": conversation_ids})
    return 0


def cmd_task_conversations(args: argparse.Namespace) -> int:
    conn = connect()
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
    if not task:
        emit({"ok": False, "error": "task not found", "task_id": args.task_id})
        return 1
    conversation_ids = task_conversation_ids(task_metadata(conn, args.task_id))
    conversations = []
    for conversation_id in conversation_ids:
        conv = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        if not conv:
            continue
        obj = dict(conv)
        obj["participants"] = json.loads(obj.pop("participants_json"))
        obj["messages"] = rows(conn, "SELECT * FROM conversation_messages WHERE conversation_id = ? ORDER BY created_at ASC", (conversation_id,))
        conversations.append(obj)
    emit({"ok": True, "task_id": args.task_id, "conversation_ids": conversation_ids, "conversations": conversations})
    return 0


def followup_paths(status: str = "pending") -> Path:
    return FOLLOWUP_STATE_DIR / status


def save_followup(followup: dict, status: str) -> Path:
    target_dir = followup_paths(status)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{followup['id']}.json"
    path.write_text(json.dumps(followup, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def load_followup(followup_id: str) -> tuple[dict, str, Path]:
    for status in ("pending", "answered", "cancelled"):
        path = followup_paths(status) / f"{followup_id}.json"
        if path.exists():
            return load_json_file(path), status, path
    raise SystemExit(f"followup not found: {followup_id}")


def list_followups(status_filter: str = "all") -> list[dict]:
    items: list[dict] = []
    statuses = [status_filter] if status_filter != "all" else ["pending", "answered", "cancelled"]
    for status in statuses:
        directory = followup_paths(status)
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.json")):
            item = load_json_file(path)
            item["status"] = status
            item["file"] = str(path)
            items.append(item)
    items.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return items


def resolve_workflow_path(name_or_path: str) -> Path:
    direct = Path(name_or_path)
    if direct.exists():
        return direct
    candidate = WORKFLOW_DIR / name_or_path
    if candidate.exists():
        return candidate
    if not name_or_path.endswith(".json"):
        candidate = WORKFLOW_DIR / f"{name_or_path}.json"
        if candidate.exists():
            return candidate
    raise SystemExit(f"workflow not found: {name_or_path}")


def workflow_assert_employee(conn: sqlite3.Connection, employee_id: str, dry_run: bool) -> None:
    if conn.execute("SELECT 1 FROM employees WHERE id = ?", (employee_id,)).fetchone():
        return
    if dry_run:
        return
    raise SystemExit(f"unknown employee in workflow: {employee_id}")


def workflow_render(text: str, context: dict) -> str:
    rendered = text
    for key, value in context.items():
        if isinstance(value, (str, int, float)):
            rendered = rendered.replace("{{" + key + "}}", str(value))
    return rendered


def workflow_report_path(agent: str, run_id: str, name: str) -> Path:
    safe_name = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in name).strip("-") or "report"
    report_dir = employee_paths(agent)["reports"] / run_id
    report_dir.mkdir(parents=True, exist_ok=True)
    return report_dir / f"{safe_name}.md"


def workflow_write_event(run_id: str, event: dict) -> Path:
    run_dir = STATE_DIR / "workflow-runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    event_path = run_dir / "events.jsonl"
    with event_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event_path


def cmd_workflow_validate(args: argparse.Namespace) -> int:
    conn = connect()
    workflow_path = resolve_workflow_path(args.workflow)
    workflow = load_json_file(workflow_path)
    employees = [resolve_employee_alias(employee_id) for employee_id in workflow.get("employees", [])]
    steps = workflow.get("steps", [])
    missing = []
    for employee_id in employees:
        if not conn.execute("SELECT 1 FROM employees WHERE id = ?", (employee_id,)).fetchone():
            missing.append(employee_id)
    emit({"ok": not missing, "workflow": str(workflow_path), "employees": employees, "missing_employees": missing, "steps": len(steps)})
    return 0 if not missing else 1


def cmd_workflow_run(args: argparse.Namespace) -> int:
    conn = connect()
    workflow_path = resolve_workflow_path(args.workflow)
    workflow = load_json_file(workflow_path)
    run_id = args.run_id or f"wf-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    context: dict = {
        "run_id": run_id,
        "workflow_id": workflow.get("id", workflow_path.stem),
        "topic": args.topic or workflow.get("topic", ""),
    }
    aliases = load_communication_config().get("aliases", {})
    employees = [str(aliases.get(employee_id, employee_id)) for employee_id in workflow.get("employees", [])]
    for employee_id in employees:
        workflow_assert_employee(conn, employee_id, args.dry_run)
    max_steps = args.max_steps or len(workflow.get("steps", []))
    events = []
    for index, step in enumerate(workflow.get("steps", [])[:max_steps], start=1):
        event = {"index": index, "type": step.get("type", ""), "at": now()}
        step_type = step.get("type")
        if step_type == "conversation_start":
            source = resolve_employee_alias(step["from"])
            participants = [resolve_employee_alias(p) for p in step["participants"]]
            conversation_id = workflow_render(step.get("conversation_id") or f"{workflow.get('id', 'workflow')}-{run_id}", context)
            body = workflow_render(step["body"], context)
            title = workflow_render(step.get("title", workflow.get("title", "Workflow conversation")), context)
            if not args.dry_run:
                conversation_start_internal(conn, source=source, participants=participants, title=title, body=body, evidence="", conversation_id=conversation_id)
            event.update({"source": source, "participants": participants, "conversation_id": conversation_id, "body": body})
            context["conversation_id"] = conversation_id
        elif step_type == "conversation_reply":
            source = resolve_employee_alias(step["from"])
            conversation_id = workflow_render(step.get("conversation_id", "{{conversation_id}}"), context)
            body = workflow_render(step["body"], context)
            if not args.dry_run:
                conversation_reply_internal(conn, source=source, conversation_id=conversation_id, body=body, evidence="", message_id=step.get("message_id", ""))
            event.update({"source": source, "conversation_id": conversation_id, "body": body})
        elif step_type == "task_submit":
            source_alias = step["from"]
            target_alias = step["to"]
            source = resolve_employee_alias(source_alias)
            target = resolve_employee_alias(target_alias)
            title = workflow_render(step["title"], context)
            description = workflow_render(step.get("description", ""), context)
            task_id = workflow_render(step.get("task_id") or f"{run_id}-{target}-{index}", context)
            if not args.dry_run:
                result = submit_task_internal(conn, source=source, target=target, title=title, description=description, priority=step.get("priority", "P2"), task_id=task_id, metadata={"workflow_run_id": run_id, "step": index})
                event.update(result)
            event.update({"source": source, "target": target, "task_id": task_id, "title": title})
            context[f"{target}_task_id"] = task_id
            context[f"{target_alias}_task_id"] = task_id
            context["last_task_id"] = task_id
        elif step_type == "task_execute":
            agent = resolve_employee_alias(step["agent"])
            task_id = workflow_render(step.get("task_id", "{{last_task_id}}"), context)
            summary = workflow_render(step.get("summary", "完成"), context)
            report = workflow_render(step.get("report", summary), context)
            evidence_path = workflow_report_path(agent, run_id, step.get("evidence_name", task_id))
            if not args.dry_run:
                evidence_path.write_text(report + "\n", encoding="utf-8")
                complete_task_internal(conn, agent=agent, task_id=task_id, summary=summary, evidence=str(evidence_path))
            event.update({"agent": agent, "task_id": task_id, "summary": summary, "evidence": str(evidence_path)})
            context["last_evidence"] = str(evidence_path)
        elif step_type == "heartbeat":
            agent = resolve_employee_alias(step["agent"])
            if not args.dry_run:
                heartbeat_internal(conn, agent, {"source": "workflow", "run_id": run_id, "step": index})
            event.update({"agent": agent})
        else:
            raise SystemExit(f"unknown workflow step type: {step_type}")
        event_path = workflow_write_event(run_id, event)
        events.append(event)
    audit(conn, "companyctl", "workflow.run", run_id, {"workflow": str(workflow_path), "dry_run": args.dry_run, "events": len(events)})
    emit({"ok": True, "dry_run": args.dry_run, "run_id": run_id, "workflow": str(workflow_path), "events": events, "event_log": str(event_path) if events else ""})
    return 0


def load_hooks_config() -> dict:
    if not HOOKS_PATH.exists():
        return {"hooks": []}
    return json.loads(HOOKS_PATH.read_text(encoding="utf-8"))


def event_payload(event: sqlite3.Row | dict) -> dict:
    raw = event["payload_json"] if isinstance(event, sqlite3.Row) else event.get("payload_json", "{}")
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}


def hook_matches(conn: sqlite3.Connection, hook: dict, event: sqlite3.Row) -> bool:
    if not hook.get("enabled", True):
        return False
    payload = event_payload(event)
    match = hook.get("match", {})
    if match.get("event_type") and match["event_type"] != event["event_type"]:
        return False
    if match.get("source_agent") and resolve_employee_alias(match["source_agent"]) != event["source_agent"]:
        return False
    if match.get("task_id") and match["task_id"] != event["task_id"]:
        return False
    if match.get("conversation_id") and match["conversation_id"] != payload.get("conversation_id", ""):
        return False
    if match.get("participant"):
        expected_participant = resolve_employee_alias(match["participant"])
        participants = [resolve_employee_alias(str(item)) for item in payload.get("participants", [])]
        if expected_participant not in participants:
            return False
    if match.get("body_contains"):
        required = match["body_contains"]
        if isinstance(required, str):
            required = [required]
        body = str(payload.get("body", ""))
        if not any(str(item) in body for item in required):
            return False
    if match.get("skip_child_tasks") and event["task_id"]:
        relation = conn.execute("SELECT 1 FROM task_relations WHERE child_task_id = ?", (event["task_id"],)).fetchone()
        if relation:
            return False
    target_agent = match.get("target_agent")
    if target_agent:
        expected = resolve_employee_alias(target_agent)
        actual = ""
        if event["task_id"]:
            task = conn.execute("SELECT target_agent FROM tasks WHERE id = ?", (event["task_id"],)).fetchone()
            actual = task["target_agent"] if task else ""
        else:
            participants = [resolve_employee_alias(str(item)) for item in payload.get("participants", [])]
            actual = resolve_employee_alias(str(payload.get("target_agent", ""))) if payload.get("target_agent") else ""
        if expected != actual and expected not in participants:
            return False
    return True


def render_hook_text(text: str, event: sqlite3.Row, payload: dict, extra: dict | None = None) -> str:
    context = {
        "event_id": event["id"],
        "event_type": event["event_type"],
        "source_agent": event["source_agent"],
        "task_id": event["task_id"],
        "summary": payload.get("summary", ""),
        "evidence": payload.get("evidence", ""),
        "message_id": payload.get("message_id", ""),
        "conversation_id": payload.get("conversation_id", ""),
        "participants": ",".join(str(item) for item in payload.get("participants", [])),
        "target_agent": payload.get("target_agent", ""),
        "body": payload.get("body", ""),
    }
    if extra:
        context.update(extra)
    return workflow_render(text, context)


def send_message_internal(conn: sqlite3.Connection, *, source: str, target: str, body: str, message_id: str = "") -> dict:
    source = resolve_employee_alias(source)
    target = resolve_employee_alias(target)
    require_employee(conn, source)
    require_employee(conn, target)
    policy = require_communication_allowed(source, target, "message.send")
    mid = message_id or f"msg-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    ts = now()
    conn.execute(
        "INSERT INTO messages(id, source_agent, target_agent, body, created_at) VALUES (?, ?, ?, ?, ?)",
        (mid, source, target, body, ts),
    )
    conn.commit()
    inbox = employee_paths(target)["inbox"]
    inbox.mkdir(parents=True, exist_ok=True)
    message = {
        "id": mid,
        "source_agent": source,
        "target_agent": target,
        "body": body,
        "created_at": ts,
        "type": "message",
        "communication_policy": policy,
    }
    message_file = inbox / f"{mid}.message.json"
    message_file.write_text(json.dumps(message, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    event = record_event(
        conn,
        "message.send",
        source,
        payload={"message_id": mid, "target_agent": target, "body": body, "file": str(message_file)},
    )
    audit(conn, source, "message.send", mid, {**message, "event_id": event["id"]})
    return {"message": message, "file": str(message_file), "event_id": event["id"]}


def slug(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in value).strip("-") or "item"


def approved_gate(conn: sqlite3.Connection, approval_id: str, approval_action: str, source: str, target: str) -> dict:
    if not approval_id:
        return {"allowed": False, "reason": "missing approval_id"}
    row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    if not row:
        return {"allowed": False, "reason": "approval not found", "approval_id": approval_id}
    approval = normalize_approval(row)
    detail = approval["detail"]
    if approval["status"] != "approved":
        return {"allowed": False, "reason": f"approval is {approval['status']}", "approval": approval}
    if approval["action"] != approval_action:
        return {"allowed": False, "reason": "approval action mismatch", "approval": approval}
    if detail.get("requested_by") and detail["requested_by"] != source:
        return {"allowed": False, "reason": "approval requester mismatch", "approval": approval}
    if target and detail.get("target") and detail["target"] != target:
        return {"allowed": False, "reason": "approval target mismatch", "approval": approval}
    return {"allowed": True, "approval": approval}


def find_matching_approved_approval(conn: sqlite3.Connection, approval_action: str, source: str, target: str, event: sqlite3.Row, hook_id: str) -> dict | None:
    candidates = conn.execute(
        "SELECT * FROM approvals WHERE status = 'approved' AND source_agent = ? AND action = ? ORDER BY updated_at DESC",
        (source, approval_action),
    ).fetchall()
    for row in candidates:
        approval = normalize_approval(row)
        detail = approval["detail"]
        metadata = detail.get("metadata", {}) if isinstance(detail.get("metadata", {}), dict) else {}
        if target and detail.get("target") and detail["target"] != target:
            continue
        if metadata.get("event_id") and metadata["event_id"] != event["id"]:
            continue
        if metadata.get("hook_id") and metadata["hook_id"] != hook_id:
            continue
        return approval
    return None


def approval_gate_for_hook_action(conn: sqlite3.Connection, hook_id: str, action: dict, event: sqlite3.Row, payload: dict) -> dict:
    approval_action = action.get("requires_approval", "")
    if not approval_action:
        return {"allowed": True}
    source = resolve_employee_alias(action.get("from") or action.get("agent") or event["source_agent"])
    target = resolve_employee_alias(action.get("to") or action.get("target") or "")
    approval_id = render_hook_text(action.get("approval_id", ""), event, payload)
    if not approval_id:
        approved = find_matching_approved_approval(conn, approval_action, source, target, event, hook_id)
        if approved:
            return {"allowed": True, "approval": approved}
    gate = approved_gate(conn, approval_id, approval_action, source, target)
    if gate["allowed"]:
        return gate
    pending_id = action.get("pending_approval_id") or f"approval-hook-{slug(event['id'])}-{slug(hook_id)}-{slug(approval_action)}"
    reason = action.get("approval_reason") or f"Hook {hook_id} requires approval for {approval_action} before processing event {event['id']}"
    result = create_approval_internal(
        conn,
        source=source,
        action=approval_action,
        reason=render_hook_text(reason, event, payload),
        target=target,
        risk=action.get("risk", "P1"),
        evidence=payload.get("evidence", ""),
        approval_id=render_hook_text(pending_id, event, payload),
        metadata={"hook_id": hook_id, "event_id": event["id"], "task_id": event["task_id"]},
    )
    return {"allowed": False, "reason": gate.get("reason", "approval required"), "approval_request": result["approval"], "file": result["file"]}


def run_hook_action(conn: sqlite3.Connection, action: dict, event: sqlite3.Row, payload: dict) -> dict:
    action_type = action.get("type")
    if action_type == "message":
        source = resolve_employee_alias(action["from"])
        target = resolve_employee_alias(action["to"])
        body = render_hook_text(action["body"], event, payload)
        return {"type": "message", **send_message_internal(conn, source=source, target=target, body=body)}
    if action_type == "task_submit":
        source = resolve_employee_alias(action["from"])
        target = resolve_employee_alias(action["to"])
        title = render_hook_text(action["title"], event, payload)
        description = render_hook_text(action.get("description", ""), event, payload)
        task_id = render_hook_text(action.get("task_id", ""), event, payload) or ""
        return {"type": "task_submit", **submit_task_internal(conn, source=source, target=target, title=title, description=description, priority=action.get("priority", "P2"), task_id=task_id, metadata={"hook_event_id": event["id"]})}
    if action_type == "conversation_reply":
        source = resolve_employee_alias(action["from"])
        conversation_id = render_hook_text(action["conversation_id"], event, payload)
        body = render_hook_text(action["body"], event, payload)
        evidence = render_hook_text(action.get("evidence", ""), event, payload)
        message_id = render_hook_text(action.get("message_id", ""), event, payload)
        return {"type": "conversation_reply", **conversation_reply_internal(conn, source=source, conversation_id=conversation_id, body=body, evidence=evidence, message_id=message_id)}
    if action_type == "heartbeat":
        agent = resolve_employee_alias(action["agent"])
        return {"type": "heartbeat", "heartbeat": heartbeat_internal(conn, agent, {"source": "hook", "event_id": event["id"]})}
    raise SystemExit(f"unknown hook action type: {action_type}")


def cmd_scheduler_run(args: argparse.Namespace) -> int:
    conn = connect()
    hooks = load_hooks_config().get("hooks", [])
    pending = conn.execute(
        "SELECT * FROM company_events WHERE processed_at = '' ORDER BY created_at ASC LIMIT ?",
        (args.limit,),
    ).fetchall()
    processed = []
    for event in pending:
        payload = event_payload(event)
        matched_hooks = [hook for hook in hooks if hook_matches(conn, hook, event)]
        actions = []
        blocked = []
        if not args.dry_run:
            for hook in matched_hooks:
                hook_id = hook.get("id", "")
                for action_index, action in enumerate(hook.get("actions", []), start=1):
                    prior = conn.execute(
                        "SELECT * FROM hook_action_runs WHERE event_id = ? AND hook_id = ? AND action_index = ? AND status = 'completed'",
                        (event["id"], hook_id, action_index),
                    ).fetchone()
                    if prior:
                        actions.append({"hook": hook_id, "action_index": action_index, "skipped": "already_completed"})
                        continue
                    gate = approval_gate_for_hook_action(conn, hook_id, action, event, payload)
                    if not gate.get("allowed"):
                        blocked.append({"hook": hook_id, "action_index": action_index, "action": action.get("type", ""), "gate": gate})
                        continue
                    result = run_hook_action(conn, action, event, payload)
                    run_id = f"har-{slug(event['id'])}-{slug(hook_id)}-{action_index}"
                    conn.execute(
                        """
                        INSERT INTO hook_action_runs(id, event_id, hook_id, action_index, status, result_json, created_at)
                        VALUES (?, ?, ?, ?, 'completed', ?, ?)
                        ON CONFLICT(event_id, hook_id, action_index) DO UPDATE SET
                          status = excluded.status,
                          result_json = excluded.result_json
                        """,
                        (run_id, event["id"], hook_id, action_index, json.dumps(result, ensure_ascii=False), now()),
                    )
                    conn.commit()
                    actions.append({"hook": hook_id, "action_index": action_index, "result": result})
            if not blocked:
                conn.execute("UPDATE company_events SET processed_at = ? WHERE id = ?", (now(), event["id"]))
                conn.commit()
        processed.append({"event_id": event["id"], "event_type": event["event_type"], "task_id": event["task_id"], "matched_hooks": [h.get("id", "") for h in matched_hooks], "blocked": blocked, "actions": actions})
    audit(conn, "companyctl", "scheduler.run", "", {"dry_run": args.dry_run, "events": len(processed)})
    emit({"ok": True, "dry_run": args.dry_run, "events": processed})
    return 0


def cmd_scheduler_events(args: argparse.Namespace) -> int:
    conn = connect()
    where = "WHERE processed_at = ''" if args.pending else ""
    emit({"ok": True, "events": rows(conn, f"SELECT * FROM company_events {where} ORDER BY created_at DESC LIMIT ?", (args.limit,))})
    return 0


def cmd_scheduler_skip_event(args: argparse.Namespace) -> int:
    conn = connect()
    actor = resolve_employee_alias(args.by)
    require_employee(conn, actor)
    event = conn.execute("SELECT * FROM company_events WHERE id = ?", (args.event_id,)).fetchone()
    if not event:
        emit({"ok": False, "error": "event not found", "event_id": args.event_id})
        return 2
    if event["processed_at"]:
        emit({"ok": True, "skipped": False, "reason": "event already processed", "event": dict(event)})
        return 0
    ts = now()
    conn.execute("UPDATE company_events SET processed_at = ? WHERE id = ?", (ts, args.event_id))
    conn.commit()
    updated = conn.execute("SELECT * FROM company_events WHERE id = ?", (args.event_id,)).fetchone()
    audit(conn, actor, "scheduler.skip_event", args.event_id, {"reason": args.reason, "event_type": event["event_type"], "source_agent": event["source_agent"]})
    emit({"ok": True, "skipped": True, "event": dict(updated), "reason": args.reason})
    return 0


def normalize_approval(row: sqlite3.Row | dict) -> dict:
    obj = dict(row)
    obj["detail"] = approval_detail(obj.pop("reason", ""))
    detail = obj["detail"] if isinstance(obj.get("detail"), dict) else {}
    if detail.get("evidence"):
        detail["evidence_display"] = sanitize_evidence_path_for_display(str(detail.get("evidence") or ""))
        detail["evidence"] = sanitize_log_text(detail.get("evidence", ""))
    if detail.get("request_reason"):
        detail["request_reason"] = sanitize_log_text(detail.get("request_reason", ""))
    obj["safety"] = {
        "dry_run": bool(detail.get("dry_run", False)),
        "external_send_executed": bool(detail.get("external_send_executed", False)),
        "resolution_mode": str(detail.get("resolution_mode") or ("mock" if detail.get("mock_resolve") else "")),
        "requires_owner_approval": True,
        "summary": str(detail.get("safety_note") or ("no external delivery executed" if detail.get("mock_resolve") else "owner approval required before real external delivery")),
    }
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


def rfc_state_path(status: str, rfc_id: str) -> Path:
    return STATE_DIR / "rfcs" / status / f"{rfc_id}.json"


def normalize_rfc(row: sqlite3.Row | dict) -> dict:
    obj = dict(row)
    try:
        obj["target_paths"] = json.loads(obj.pop("target_paths_json", "[]") or "[]")
    except json.JSONDecodeError:
        obj["target_paths"] = []
    return obj


def write_rfc_state(rfc: dict) -> str:
    status = rfc["status"]
    for existing in ("pending", "approved", "denied"):
        old = rfc_state_path(existing, rfc["id"])
        if old.exists() and existing != status:
            old.unlink()
    path = rfc_state_path(status, rfc["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rfc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


def rfc_file_path(rfc_id: str) -> Path:
    safe = slug(rfc_id)
    return RFC_DIR / f"{safe}.md"


def rfc_by_ref(conn: sqlite3.Connection, ref: str) -> dict | None:
    rel = normalize_repo_path(ref)
    row = conn.execute("SELECT * FROM rfcs WHERE id = ? OR file_path = ?", (ref, rel)).fetchone()
    return normalize_rfc(row) if row else None


def approved_rfc_covers(conn: sqlite3.Connection, rfc_ref: str, blocked_paths: list[dict]) -> dict:
    rfc = rfc_by_ref(conn, rfc_ref)
    if not rfc:
        return {"allowed": False, "reason": "rfc not found", "rfc": rfc_ref}
    if rfc["status"] != "approved":
        return {"allowed": False, "reason": f"rfc is {rfc['status']}", "rfc": rfc}
    targets = [normalize_repo_path(path) for path in rfc.get("target_paths", [])]
    missing = []
    for blocked in blocked_paths:
        path = blocked["path"]
        if not any(fnmatch.fnmatch(path, target) or fnmatch.fnmatch(path, target.rstrip("/") + "/**") for target in targets):
            missing.append(path)
    if missing:
        return {"allowed": False, "reason": "rfc does not cover protected paths", "missing": missing, "rfc": rfc}
    return {"allowed": True, "rfc": rfc}


def cmd_rfc_create(args: argparse.Namespace) -> int:
    conn = connect()
    author = resolve_employee_alias(args.by)
    require_employee(conn, author)
    targets = [normalize_repo_path(path) for path in parse_csv(args.paths)]
    rfc_id = args.rfc_id or f"rfc-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    path = Path(args.file) if args.file else rfc_file_path(rfc_id)
    if not path.is_absolute():
        path = ROOT / path
    rel_file = normalize_repo_path(str(path))
    ts = now()
    body = "\n".join(
        [
            f"# RFC: {args.title}",
            "",
            f"- id: `{rfc_id}`",
            f"- author: `{author}`",
            "- status: `pending`",
            f"- target_paths: `{', '.join(targets)}`",
            "",
            "## Problem",
            "",
            args.reason,
            "",
            "## Proposed Change",
            "",
            args.proposal or "Pending detailed proposal.",
            "",
            "## Rollback",
            "",
            args.rollback or "Restore previous protected files and rerun companyctl doctor.",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or args.overwrite:
        path.write_text(body, encoding="utf-8")
    conn.execute(
        """
        INSERT INTO rfcs(id, title, author_agent, status, target_paths_json, reason, file_path, created_at, updated_at)
        VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          title = excluded.title,
          target_paths_json = excluded.target_paths_json,
          reason = excluded.reason,
          file_path = excluded.file_path,
          updated_at = excluded.updated_at
        """,
        (rfc_id, args.title, author, json.dumps(targets, ensure_ascii=False), args.reason, rel_file, ts, ts),
    )
    conn.commit()
    rfc = normalize_rfc(conn.execute("SELECT * FROM rfcs WHERE id = ?", (rfc_id,)).fetchone())
    state_file = write_rfc_state(rfc)
    audit(conn, author, "rfc.create", rfc_id, rfc)
    emit({"ok": True, "rfc": rfc, "file": str(path), "state_file": state_file})
    return 0


def cmd_rfc_list(args: argparse.Namespace) -> int:
    conn = connect()
    where = "" if args.status == "all" else "WHERE status = ?"
    params = () if args.status == "all" else (args.status,)
    rfcs = [normalize_rfc(row) for row in conn.execute(f"SELECT * FROM rfcs {where} ORDER BY updated_at DESC", params).fetchall()]
    emit({"ok": True, "rfcs": rfcs})
    return 0


def cmd_rfc_show(args: argparse.Namespace) -> int:
    conn = connect()
    rfc = rfc_by_ref(conn, args.rfc)
    if not rfc:
        emit({"ok": False, "error": "rfc not found", "rfc": args.rfc})
        return 1
    emit({"ok": True, "rfc": rfc})
    return 0


def decide_rfc(args: argparse.Namespace, status: str) -> int:
    conn = connect()
    actor = resolve_employee_alias(args.by)
    require_employee(conn, actor)
    current = rfc_by_ref(conn, args.rfc)
    if not current:
        emit({"ok": False, "error": "rfc not found", "rfc": args.rfc})
        return 1
    ts = now()
    conn.execute(
        "UPDATE rfcs SET status = ?, decision_by = ?, decision_reason = ?, updated_at = ? WHERE id = ?",
        (status, actor, args.reason, ts, current["id"]),
    )
    conn.commit()
    rfc = normalize_rfc(conn.execute("SELECT * FROM rfcs WHERE id = ?", (current["id"],)).fetchone())
    state_file = write_rfc_state(rfc)
    audit(conn, actor, f"rfc.{status}", current["id"], rfc)
    emit({"ok": True, "rfc": rfc, "state_file": state_file})
    return 0


def cmd_rfc_approve(args: argparse.Namespace) -> int:
    return decide_rfc(args, "approved")


def cmd_rfc_deny(args: argparse.Namespace) -> int:
    return decide_rfc(args, "denied")


def cmd_approval_request(args: argparse.Namespace) -> int:
    conn = connect()
    metadata = {"task_id": args.task_id} if args.task_id else None
    result = create_approval_internal(
        conn,
        source=args.source,
        action=args.action,
        reason=args.reason,
        target=args.target,
        risk=args.risk,
        evidence=args.evidence,
        approval_id=args.approval_id,
        metadata=metadata,
    )
    emit({"ok": True, **result})
    return 0


def create_approval_internal(
    conn: sqlite3.Connection,
    *,
    source: str,
    action: str,
    reason: str,
    target: str = "",
    risk: str = "",
    evidence: str = "",
    approval_id: str = "",
    metadata: dict | None = None,
    notify: bool = True,
) -> dict:
    source = resolve_employee_alias(source)
    require_employee(conn, source)
    aid = approval_id or f"approval-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    ts = now()
    detail = {
        "request_reason": reason,
        "target": resolve_employee_alias(target) if target else "",
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
    # auto mode: the approval is created only for the audit trail and gets approved immediately,
    # so there is nothing for the owner to do — don't record an "approval.requested" event and
    # don't ping Telegram with a "approval required" message (that's the stale-notification bug).
    if not notify:
        return {"approval": approval, "file": path, "notification": None, "event": None}
    approval_event = record_event(
        conn,
        "approval.requested",
        source,
        task_id=str((metadata or {}).get("task_id", "") or ""),
        trace_id=str((metadata or {}).get("trace_id", "") or ""),
        payload={
            "approval_id": aid,
            "action": action,
            "target": detail["target"],
            "risk": risk,
            "metadata": metadata or {},
        },
    )
    approval_event_processed_at = now()
    conn.execute("UPDATE company_events SET processed_at = ? WHERE id = ?", (approval_event_processed_at, approval_event["id"]))
    conn.commit()
    approval_event["processed_at"] = approval_event_processed_at
    notification = notification_send_result(
        kind="approval",
        subject=f"Company Kernel approval required: {aid}",
        message=f"source={source}\naction={action}\nrisk={risk or '-'}\ntarget={detail['target'] or '-'}\nreason={reason}\nevidence={evidence or '-'}",
        reply_markup={"inline_keyboard": [[
            {"text": "✅ 批准 Approve", "callback_data": f"ck_approve:{aid}"},
            {"text": "❌ 拒绝 Deny", "callback_data": f"ck_deny:{aid}"},
        ]]},
    )
    return {"approval": approval, "file": path, "notification": notification, "event": approval_event}


def cmd_approval_list(args: argparse.Namespace) -> int:
    conn = connect()
    clauses = []
    params: list[str | int] = []
    if args.status != "all":
        clauses.append("status = ?")
        params.append(args.status)
    if args.agent:
        clauses.append("source_agent = ?")
        params.append(resolve_employee_alias(args.agent))
    if args.action:
        clauses.append("action = ?")
        params.append(args.action)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(args.limit)
    approvals = [normalize_approval(r) for r in conn.execute(f"SELECT * FROM approvals {where} ORDER BY updated_at DESC LIMIT ?", tuple(params)).fetchall()]
    emit({"ok": True, "approvals": approvals, "approval_control_summary": approval_control_summary(approvals)})
    return 0


def approval_detail_bundle(conn: sqlite3.Connection, approval_id: str) -> dict:
    safe_id = str(approval_id or "").strip()
    if not safe_id or "/" in safe_id:
        return {"ok": False, "error": "invalid approval_id", "approval_id": safe_id}
    row = conn.execute("SELECT * FROM approvals WHERE id = ?", (safe_id,)).fetchone()
    if not row:
        return {"ok": False, "error": "approval not found", "approval_id": safe_id}
    approval = normalize_approval(row)
    detail = approval.get("detail", {}) if isinstance(approval.get("detail"), dict) else {}
    metadata = detail.get("metadata", {}) if isinstance(detail.get("metadata"), dict) else {}
    task_id = str(metadata.get("task_id") or "")
    trace_id = str(metadata.get("trace_id") or "")
    task_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone() if task_id else None
    task_payload = dict(task_row) if task_row else {}
    if task_payload:
        raw_evidence_path = task_payload.pop("evidence_path", "")
        task_payload["evidence"] = sanitize_evidence_path_for_display(raw_evidence_path)
        if not trace_id:
            trace_id = trace_id_for_task(conn, task_id, "")
    event_rows = rows(
        conn,
        """
        SELECT id, trace_id, event_type, source_agent, task_id, payload_json, created_at, processed_at
        FROM company_events
        WHERE payload_json LIKE ? OR task_id = ?
        ORDER BY created_at ASC
        LIMIT 100
        """,
        (f"%{safe_id}%", task_id),
    )
    sanitized_events = []
    for event in event_rows:
        raw_payload = event.get("payload_json", "")
        try:
            payload = json.loads(raw_payload or "{}")
        except json.JSONDecodeError:
            payload = {"raw": sanitize_log_text(raw_payload)}
        clean_payload = sanitize_json_like(payload)
        sanitized_events.append(
            {
                **event,
                "payload": clean_payload,
                "payload_json": json.dumps(clean_payload, ensure_ascii=False, sort_keys=True),
            }
        )
    audit_rows = rows(conn, "SELECT id, actor, action, target, detail_json, created_at FROM audit_logs WHERE target = ? ORDER BY created_at ASC LIMIT 100", (safe_id,))
    sanitized_audit = []
    for item in audit_rows:
        raw_detail = item.pop("detail_json", "{}")
        try:
            parsed = json.loads(raw_detail or "{}")
        except json.JSONDecodeError:
            parsed = {"raw": sanitize_log_text(raw_detail)}
        sanitized_audit.append({**item, "detail": sanitize_json_like(parsed)})
    return {
        "ok": True,
        "source": "/v1/approvals/{approval_id}",
        "approval": approval,
        "task": task_payload,
        "events": sanitized_events,
        "audit_logs": sanitized_audit,
        "approval_control_summary": approval_control_summary([approval]),
        "evidence_records": task_evidence_records(conn, task_id) if task_id else [],
        "budget_summary": budget_summary(conn, task_id=task_id, trace_id=trace_id),
        "redaction_policy": sanitized_log_policy(),
    }


def cmd_approval_show(args: argparse.Namespace) -> int:
    conn = connect()
    payload = approval_detail_bundle(conn, args.approval_id)
    if not payload.get("ok"):
        emit(payload)
        return 1
    emit(payload)
    return 0


def materialize_route_task(conn: sqlite3.Connection, approval: dict, actor: str) -> dict | None:
    """When a route-gated approval is approved, create the task it was holding back.

    The gate (`route_approval_gate`) blocks high-risk submits and parks the full task
    (title/description/target/...) in the approval's metadata instead of creating it. On
    approval we must actually materialize that task, otherwise it silently vanishes —
    the owner approved, but nothing ever became executable. Idempotent: re-approving an
    already-materialized approval is a no-op.
    """
    detail = approval.get("detail", {}) if isinstance(approval.get("detail"), dict) else {}
    meta = detail.get("metadata", {}) if isinstance(detail.get("metadata"), dict) else {}
    if not meta.get("route") or not meta.get("title") or not meta.get("target"):
        return None  # not a route task approval (e.g. budget / external_send) — nothing to create
    if detail.get("materialized_task_id"):
        return None  # already materialized (idempotent re-approval)
    source = resolve_employee_alias(meta.get("source") or approval.get("source_agent") or "")
    target = resolve_employee_alias(meta["target"])
    # Apply the SAME submit normalization (executor lock / app→cli reroute / 记忆会话 stamp) the direct
    # submit paths use — an approved route task must not bypass routing & memory binding either.
    target, description, _norm_err = normalize_submission(conn, target=target, description=meta.get("description", ""))
    if _norm_err is not None:
        return None  # the project lock forbids this target — don't materialize an unrunnable task
    try:
        require_employee(conn, source)
        require_employee(conn, target)
    except SystemExit:
        return None
    task_id = f"task-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    ts = now()
    priority = meta.get("priority") or "P2"
    conn.execute(
        """
        INSERT INTO tasks(id, source_agent, target_agent, title, description, priority, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 'submitted', ?, ?)
        """,
        (task_id, source, target, meta["title"], description, priority, ts, ts),
    )
    metadata = {
        "trace_id": new_trace_id(),
        "declared_changes": parse_csv(meta.get("changed_files", "")),
        "rfc": meta.get("rfc", ""),
        "approval": approval.get("id"),
        "materialized_from_approval": approval.get("id"),
    }
    deliver_to = parse_deliver_to(meta.get("deliver_to", ""))
    if deliver_to:
        metadata["deliver_to"] = deliver_to
    conn.execute(
        "INSERT OR REPLACE INTO task_metadata(task_id, metadata_json, updated_at) VALUES (?, ?, ?)",
        (task_id, json.dumps(metadata, ensure_ascii=False), ts),
    )
    # stamp the approval so we never double-create
    detail["materialized_task_id"] = task_id
    conn.execute(
        "UPDATE approvals SET reason = ?, updated_at = ? WHERE id = ?",
        (json.dumps(detail, ensure_ascii=False), ts, approval["id"]),
    )
    conn.commit()
    workspace = ensure_task_workspace(conn, task_id, metadata["trace_id"])
    inbox = employee_paths(target)["inbox"]
    inbox.mkdir(parents=True, exist_ok=True)
    task = {
        "id": task_id,
        "source_agent": source,
        "target_agent": target,
        "title": meta["title"],
        "description": description,
        "priority": priority,
        "status": "submitted",
        "metadata": metadata,
        "workspace": workspace,
        "created_at": ts,
    }
    (inbox / f"{task_id}.json").write_text(json.dumps(task, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit(conn, actor, "task.materialized_from_approval", task_id, {"approval_id": approval.get("id"), "target": target})
    record_event(conn, "task.materialized_from_approval", actor, task_id=task_id,
                 payload={"approval_id": approval.get("id"), "target": target, "title": meta["title"]})
    return task


def decide_approval(args: argparse.Namespace, status: str) -> int:
    conn = connect()
    actor = resolve_employee_alias(args.by)
    require_employee(conn, actor)
    row = conn.execute("SELECT * FROM approvals WHERE id = ?", (args.approval_id,)).fetchone()
    if not row:
        emit({"ok": False, "error": "approval not found", "approval_id": args.approval_id})
        return 1
    current = normalize_approval(row)
    detail = current["detail"]
    detail.update(
        {
            "decided_by": actor,
            "decision": status,
            "decision_reason": args.reason,
            "decided_at": now(),
        }
    )
    if status == "resolved":
        detail.update(
            {
                "mock_resolve": True,
                "dry_run": True,
                "external_send_executed": False,
                "resolution_mode": "mock",
                "safety_note": "Mock resolve only records an owner decision; it never triggers Telegram/OpenClaw/external_send delivery.",
            }
        )
    conn.execute(
        "UPDATE approvals SET status = ?, reason = ?, updated_at = ? WHERE id = ?",
        (status, json.dumps(detail, ensure_ascii=False), now(), args.approval_id),
    )
    conn.commit()
    approval = normalize_approval(conn.execute("SELECT * FROM approvals WHERE id = ?", (args.approval_id,)).fetchone())
    path = write_approval_state(approval)
    audit(conn, actor, f"approval.{status}", args.approval_id, approval)
    event_type = f"approval.{status}"
    event = record_event(
        conn,
        event_type,
        actor,
        task_id=str(detail.get("metadata", {}).get("task_id", "") or ""),
        payload={
            "approval_id": args.approval_id,
            "status": status,
            "reason": args.reason,
            "dry_run": bool(detail.get("dry_run", False)),
            "external_send_executed": bool(detail.get("external_send_executed", False)),
        },
    )
    materialized = materialize_route_task(conn, approval, actor) if status == "approved" else None
    # project memory: a real owner approval/denial is a decision worth remembering (auto-approvals skipped)
    if status in {"approved", "denied"}:
        try:
            meta = detail.get("metadata", {}) if isinstance(detail.get("metadata"), dict) else {}
            project_memory.capture_approval_decision(conn, metadata=meta, action=str(approval.get("action") or ""),
                                                     decision=status, actor=actor, reason=args.reason)
        except Exception:
            pass
    out = {"ok": True, "approval": approval, "file": path, "event": event}
    if materialized:
        out["materialized_task"] = materialized
    emit(out)
    return 0


def cmd_approval_mode(args: argparse.Namespace) -> int:
    """Get or set the owner's approval posture (backend for the console settings UI).
    --set manual = gate high-risk routes for human approval (default, safest).
    --set auto   = full auto-approval (owner-delegated); every route proceeds + is recorded."""
    conn = connect()
    if getattr(args, "set", ""):
        mode = str(args.set).lower().strip()
        if mode not in ROUTE_APPROVAL_MODES:
            emit({"ok": False, "error": f"mode must be one of {sorted(ROUTE_APPROVAL_MODES)}", "got": args.set})
            return 2
        cfg = {}
        if POLICY_PATH.exists():
            try:
                cfg = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                cfg = {}
        cfg.setdefault("route_approval", {})["mode"] = mode
        POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
        POLICY_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        actor = resolve_employee_alias(getattr(args, "by", "") or "owner")
        try:
            audit(conn, actor, "approval.mode_set", mode, {"mode": mode})
        except SystemExit:
            pass
    emit({"ok": True, "mode": route_approval_mode(), "modes": sorted(ROUTE_APPROVAL_MODES),
          "high_risk_actions": sorted(HIGH_RISK_ROUTE_ACTIONS),
          "note": "auto=全部放行;auto_low_risk=中间档(密钥/支付/部署仍人工);manual=全人工"})
    return 0


def cmd_membank_create(args: argparse.Namespace) -> int:
    conn = connect()
    lead = resolve_employee_alias(args.lead) if args.lead else "hermes"
    project = project_memory.create_project(conn, project_id=args.id, name=args.name, workspace=args.workspace, lead_agent=lead)
    audit(conn, "owner", "project.create", args.id, {"workspace": args.workspace, "lead": lead})
    emit({"ok": True, "project": project})
    return 0


def cmd_membank_list(args: argparse.Namespace) -> int:
    emit({"ok": True, "projects": project_memory.list_projects(connect())})
    return 0


def cmd_membank_show(args: argparse.Namespace) -> int:
    conn = connect()
    project = project_memory.get_project(conn, args.id)
    if not project:
        emit({"ok": False, "error": "unknown project", "id": args.id})
        return 1
    project["executors"] = project_memory.project_executors(conn, args.id)  # parsed list for the UI
    _mf = project_memory.memory_file_path(project)
    project["digest_file"] = str(_mf) if _mf else ""  # where the digest lives in the project's own dir
    roster = [{"id": r["id"], "runtime": r["runtime"], "status": r["status"]}
              for r in rows(conn, "SELECT id, runtime, status FROM employees WHERE status='active' ORDER BY id")]
    emit({"ok": True, "project": project, "memory": project_memory.recall(conn, project_id=args.id, limit=args.limit),
          "roster": roster})
    return 0


def cmd_membank_set_executors(args: argparse.Namespace) -> int:
    conn = connect()
    execs = [resolve_employee_alias(x) for x in parse_csv(args.executors)] if args.executors else []
    try:
        result = project_memory.set_executors(conn, project_id=args.id, executors=execs)
    except ValueError as exc:
        emit({"ok": False, "error": str(exc)})
        return 1
    audit(conn, "owner", "project.set_executors", args.id, {"executors": result["executors"]})
    emit(result)
    return 0


def cmd_memory_remember(args: argparse.Namespace) -> int:
    conn = connect()
    try:
        entry = project_memory.remember(
            conn, project_id=args.project, title=args.title, body=args.body, entry_type=args.type,
            author_agent=resolve_employee_alias(args.by) if args.by else "", source_task_id=args.task_id,
            evidence_path=args.evidence, importance=args.importance,
        )
    except ValueError as exc:
        emit({"ok": False, "error": str(exc)})
        return 1
    emit({"ok": True, "entry": entry})
    return 0


def cmd_memory_recall(args: argparse.Namespace) -> int:
    conn = connect()
    if not project_memory.get_project(conn, args.project):
        emit({"ok": False, "error": "unknown project", "project": args.project})
        return 1
    emit({"ok": True, "project": args.project,
          "entries": project_memory.recall(conn, project_id=args.project, query=args.query, limit=args.limit)})
    return 0


def cmd_memory_curate(args: argparse.Namespace) -> int:
    conn = connect()
    try:
        result = project_memory.curate(conn, project_id=args.project, actor=resolve_employee_alias(args.by) if args.by else "")
    except ValueError as exc:
        emit({"ok": False, "error": str(exc)})
        return 1
    audit(conn, result.get("curated_by") or "hermes", "memory.curate", args.project, {"superseded": result["superseded"], "active": result["active_entries"]})
    emit(result)
    return 0


def cmd_memory_archive(args: argparse.Namespace) -> int:
    conn = connect()
    result = project_memory.archive_entry(conn, entry_id=args.entry_id, actor=resolve_employee_alias(args.by) if args.by else "owner")
    if not result:
        emit({"ok": False, "error": "entry not found", "entry_id": args.entry_id})
        return 1
    project_memory.curate(conn, project_id=result["project_id"], actor="memory-curator")  # rebuild digest without it
    emit(result)
    return 0


def cmd_memory_curate_all(args: argparse.Namespace) -> int:
    emit(project_memory.curate_all(connect(), actor="memory-curator"))
    return 0


def cmd_approval_auto_sweep(args: argparse.Namespace) -> int:
    """Belt-and-suspenders for auto mode: approve + materialize EVERY pending *route* approval, so
    nothing a stray path created can sit blocking. Only touches route-dispatch approvals (not
    owner-control/budget ones). No-op unless mode=auto. The daemon runs this every tick."""
    conn = connect()
    if route_approval_mode() == "manual":
        emit({"ok": True, "mode": "manual", "swept": 0, "note": "manual mode — no-op"})
        return 0
    swept = []
    for r in conn.execute("SELECT * FROM approvals WHERE status = 'pending'").fetchall():
        ap = normalize_approval(r)
        meta = ap["detail"].get("metadata", {}) if isinstance(ap["detail"].get("metadata"), dict) else {}
        if not (meta.get("route") and meta.get("title") and meta.get("target")):
            continue  # leave owner-control / budget approvals alone
        if not route_action_auto_approved(ap["action"]):
            continue  # middle tier: don't sweep high-risk (密钥/支付/部署) — owner still decides those
        ts = now()
        detail = ap["detail"]
        detail.update({"decided_by": "auto-sweep", "decision": "approved", "decision_reason": "mode=auto", "decided_at": ts})
        conn.execute("UPDATE approvals SET status = 'approved', reason = ?, updated_at = ? WHERE id = ?",
                     (json.dumps(detail, ensure_ascii=False), ts, ap["id"]))
        conn.commit()
        fresh = normalize_approval(conn.execute("SELECT * FROM approvals WHERE id = ?", (ap["id"],)).fetchone())
        task = materialize_route_task(conn, fresh, "auto-sweep")
        swept.append({"approval": ap["id"], "task": (task or {}).get("id")})
    if swept:
        audit(conn, "owner", "approval.auto_swept", str(len(swept)), {"count": len(swept)})
    emit({"ok": True, "mode": "auto", "swept": len(swept), "tasks": swept})
    return 0


def cmd_approval_approve(args: argparse.Namespace) -> int:
    return decide_approval(args, "approved")


def cmd_approval_deny(args: argparse.Namespace) -> int:
    return decide_approval(args, "denied")


def cmd_approval_resolve(args: argparse.Namespace) -> int:
    if not args.mock:
        emit({"ok": False, "error": "resolve requires --mock; real external execution must use explicit approve/deny plus a delivery worker"})
        return 2
    return decide_approval(args, "resolved")


def classify_blocker(text: str) -> dict:
    """Triage a blocker into {category, label, reason, action} so the console/Telegram can tell the
    owner WHY it stuck and what to do (retry / configure / reassign / discard) — fault tolerance is
    not just 'don't crash', it's giving the owner a clear, actionable error result."""
    raw = (text or "").strip()
    low = raw.lower()
    reason = raw
    for marker in ("verdict: blocked —", "verdict: blocked -", "blocked —", "blocked:", "blocker:"):
        i = low.find(marker)
        if i >= 0:
            reason = raw[i + len(marker):].strip()
            break
    # strip the technical runtime tail (command=…, exit_code, output paths) so the owner sees the cause
    reason = re.split(r"runtime execution failed|\n\n|\.?\s*command=|\s*exit_code=", reason)[0].strip()[:220]

    def res(cat: str, label: str, action: str) -> dict:
        return {"category": cat, "label": label, "reason": reason or label, "action": action}

    if ("超时" in low or "timeout" in low) and ("已产出" in low or "可能已完成" in low or "请复核" in low):
        return res("timeout_review", "超时但有产出(待复核)", "复核产出/改动:像完成就接受,否则重试/调高超时")
    if "exit_code=124" in low or "timeout" in low or "killed after" in low or "超时" in low:
        return res("timeout", "执行超时", "可重试;大任务调高超时或换更快模型")
    if any(k in low for k in ("resource_exhausted", "all_quota", "rate limit", "rate-limited", "quota", "额度")):
        return res("quota", "额度耗尽", "稍后重试,或在 :8080 池补/换号")
    if any(k in low for k in ("401", "403", "422", "unauthorized", "credential", "凭证", "lan_order_token", "token is empty", "lan token", "secret")):
        return res("credential", "缺凭证/鉴权失败", "配好凭证/token 后『修复并重开』,或『丢弃』")
    if any(k in low for k in ("真机", "mumu", "网段", "subnet", "设备", "device")):
        return res("owner_env", "需你的设备/环境", "搭好环境后重开,或『丢弃』")
    if any(k in low for k in ("工作区", "no repo", "empty workspace", "missing workspace", "无工作区")):
        return res("missing_input", "缺工作区/输入", "补绝对仓库路径后『修复并重开』")
    return res("runtime_error", "执行失败", "看详情决定『重试 / 改派 / 丢弃』")


def cmd_task_list(args: argparse.Namespace) -> int:
    conn = connect()
    where = ""
    params: tuple = ()
    if args.agent:
        agent = resolve_employee_alias(args.agent)
        where = "WHERE target_agent = ? OR source_agent = ? OR claimed_by = ?"
        params = (agent, agent, agent)
    tasks = rows(conn, f"SELECT * FROM tasks {where} ORDER BY created_at DESC", params)
    generated_at = now()
    for task in tasks:
        attempt = latest_attempt_for_task(conn, task["id"])
        hydrated_attempt = hydrate_execution_attempt(attempt) if attempt else {}
        task["current_attempt"] = hydrated_attempt
        if attempt:
            task.update(long_task_state_for_attempt(hydrated_attempt, generated_at=generated_at))
        if task.get("blocker") and str(task.get("status") or "") in {"blocked", "failed", "stalled", "interrupted"}:
            task["blocker_triage"] = classify_blocker(str(task["blocker"]))
    emit({"ok": True, "tasks": tasks})
    return 0


def task_approvals(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    matched = []
    for row in conn.execute("SELECT * FROM approvals ORDER BY updated_at DESC").fetchall():
        approval = normalize_approval(row)
        detail = approval.get("detail", {})
        metadata = detail.get("metadata", {}) if isinstance(detail.get("metadata", {}), dict) else {}
        if metadata.get("task_id") == task_id or task_id in json.dumps(detail, ensure_ascii=False):
            matched.append(approval)
    return matched


def task_control_context_bundle(conn: sqlite3.Connection, task_id: str) -> dict:
    safe_task_id = str(task_id or "").strip()
    if not safe_task_id or "/" in safe_task_id:
        return {"ok": False, "error": "invalid task_id", "task_id": safe_task_id}
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (safe_task_id,)).fetchone()
    if not task:
        return {"ok": False, "error": "task not found", "task_id": safe_task_id}
    task_payload = dict(task)
    raw_evidence_path = task_payload.pop("evidence_path", "")
    task_payload["evidence"] = sanitize_evidence_path_for_display(raw_evidence_path)
    event_rows = rows(conn, "SELECT id, trace_id, event_type, source_agent, task_id, payload_json, created_at, processed_at FROM company_events WHERE task_id = ? ORDER BY created_at ASC LIMIT 200", (safe_task_id,))
    sanitized_events = []
    for event in event_rows:
        raw_payload = event.get("payload_json", "")
        try:
            payload = json.loads(raw_payload or "{}")
        except json.JSONDecodeError:
            payload = {"raw": sanitize_log_text(raw_payload)}
        clean_payload = sanitize_json_like(payload)
        sanitized_events.append({**event, "payload": clean_payload, "payload_json": json.dumps(clean_payload, ensure_ascii=False, sort_keys=True)})
    audit_rows = rows(conn, "SELECT id, actor, action, target, detail_json, created_at FROM audit_logs WHERE target = ? ORDER BY created_at ASC LIMIT 200", (safe_task_id,))
    sanitized_audit = []
    for item in audit_rows:
        raw_detail = item.pop("detail_json", "{}")
        try:
            parsed = json.loads(raw_detail or "{}")
        except json.JSONDecodeError:
            parsed = {"raw": sanitize_log_text(raw_detail)}
        sanitized_audit.append({**item, "detail": sanitize_json_like(parsed)})
    attempts = task_attempts(conn, safe_task_id)
    trace_id = trace_id_for_task(conn, safe_task_id, "")
    evidence_records = task_evidence_records(conn, safe_task_id)
    completion_contract = task_completion_contract(task_payload, evidence_records)
    runtime_sessions = list_runtime_sessions(conn, task_id=safe_task_id, limit=50)
    tool_calls = list_tool_calls(conn, task_id=safe_task_id, limit=100)
    budget_events = list_budget_events(conn, task_id=safe_task_id, trace_id=trace_id, limit=100)
    approvals = task_approvals(conn, safe_task_id)
    return {
        "ok": True,
        "source": "task_control_context",
        "task": task_payload,
        "attempts": attempts,
        "attempt_history": task_attempt_history(attempts),
        "runtime_sessions": runtime_sessions,
        "tool_calls": tool_calls,
        "events": sanitized_events,
        "audit_logs": sanitized_audit,
        "budget_summary": budget_summary(conn, task_id=safe_task_id, trace_id=trace_id),
        "budget_events": budget_events,
        "evidence_records": evidence_records,
        "completion_contract": completion_contract,
        "ceo_acceptance_contract": task_ceo_acceptance_contract(
            task=task_payload,
            attempts=attempts,
            runtime_sessions=runtime_sessions,
            tool_calls=tool_calls,
            budget_events=budget_events,
            evidence_records=evidence_records,
            completion_contract=completion_contract,
            approvals=approvals,
            events=sanitized_events,
        ),
        "approvals": approvals,
        "redaction_policy": sanitized_log_policy(),
    }


def cmd_task_show(args: argparse.Namespace) -> int:
    conn = connect()
    task_id = args.task_id
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        emit({"ok": False, "error": "task not found", "task_id": task_id})
        return 1
    metadata = task_metadata(conn, task_id)
    children = rows(
        conn,
        """
        SELECT tr.parent_task_id, tr.child_task_id, tr.relation_type, tr.created_by, tr.created_at,
               t.status, t.target_agent, t.claimed_by, t.summary, t.evidence_path, t.blocker, t.updated_at
        FROM task_relations tr
        JOIN tasks t ON t.id = tr.child_task_id
        WHERE tr.parent_task_id = ?
        ORDER BY tr.created_at ASC
        """,
        (task_id,),
    )
    parents = rows(
        conn,
        """
        SELECT tr.parent_task_id, tr.child_task_id, tr.relation_type, tr.created_by, tr.created_at,
               t.status, t.target_agent, t.claimed_by, t.summary, t.evidence_path, t.blocker, t.updated_at
        FROM task_relations tr
        JOIN tasks t ON t.id = tr.parent_task_id
        WHERE tr.child_task_id = ?
        ORDER BY tr.created_at ASC
        """,
        (task_id,),
    )
    events = rows(conn, "SELECT * FROM company_events WHERE task_id = ? ORDER BY created_at ASC", (task_id,))
    event_ids = [event["id"] for event in events]
    hook_runs: list[dict] = []
    if event_ids:
        placeholders = ",".join("?" for _ in event_ids)
        hook_runs = rows(conn, f"SELECT * FROM hook_action_runs WHERE event_id IN ({placeholders}) ORDER BY created_at ASC", tuple(event_ids))
        for run in hook_runs:
            try:
                run["result"] = json.loads(run.pop("result_json", "{}") or "{}")
            except json.JSONDecodeError:
                run["result"] = {}
    lock = conn.execute("SELECT * FROM locks WHERE resource_key = ?", (f"task:{task_id}",)).fetchone()
    task_obj = dict(task)
    evidence_path = task_obj.get("evidence_path", "")
    evidence = sanitize_evidence_path_for_display(evidence_path)
    audit_rows = rows(conn, "SELECT * FROM audit_logs WHERE target = ? ORDER BY created_at ASC", (task_id,))
    attempts = task_attempts(conn, task_id)
    attempt_history = task_attempt_history(attempts)
    evidence_records = task_evidence_records(conn, task_id)
    completion_contract = task_completion_contract(task_obj, evidence_records)
    runtime_sessions = list_runtime_sessions(conn, task_id=task_id, limit=50)
    tool_calls = list_tool_calls(conn, task_id=task_id, limit=100)
    budget_events = list_budget_events(conn, task_id=task_id, limit=100)
    task_budget_summary = budget_summary(conn, task_id=task_id)
    control_plane_timeline = task_control_plane_timeline(
        events=events,
        attempts=attempts,
        runtime_sessions=runtime_sessions,
        tool_calls=tool_calls,
        budget_events=budget_events,
        evidence_records=evidence_records,
    )
    projects = task_project_costs(conn, task_id)
    sanitized_logs = []
    log_policy = sanitized_log_policy()
    for run in rows(
        conn,
        """
        SELECT id, trace_id, agent_id, task_id, command, ok, processed, attempt, result_json, created_at
        FROM adapter_runs
        WHERE task_id = ?
        ORDER BY created_at DESC
        LIMIT 20
        """,
        (task_id,),
    ):
        raw_result_json = run.pop("result_json", "{}")
        try:
            result = json.loads(raw_result_json or "{}")
        except json.JSONDecodeError:
            result = {"raw": raw_result_json}
        summary = summarize_adapter_result(result)
        sanitized_logs.append(
            {
                "run_id": run["id"],
                "trace_id": run.get("trace_id", ""),
                "agent_id": run.get("agent_id", ""),
                "task_id": run.get("task_id", ""),
                "command": run.get("command", ""),
                "ok": bool(run.get("ok")),
                "processed": bool(run.get("processed")),
                "attempt": run.get("attempt", 0),
                "created_at": run.get("created_at", ""),
                "sanitized_log": summary.get("sanitized_log", ""),
                "progress_file": summary.get("progress_file", ""),
                "raw_available": False,
                "log_policy": log_policy,
            }
        )
    supervisor_state, correction_summary = task_supervisor_state(attempts)
    if task_obj.get("blocker") and str(task_obj.get("status") or "") in {"blocked", "failed", "stalled", "interrupted"}:
        task_obj["blocker_triage"] = classify_blocker(str(task_obj["blocker"]))
    approvals = task_approvals(conn, task_id)
    ceo_acceptance_contract = task_ceo_acceptance_contract(
        task=task_obj,
        attempts=attempts,
        runtime_sessions=runtime_sessions,
        tool_calls=tool_calls,
        budget_events=budget_events,
        evidence_records=evidence_records,
        completion_contract=completion_contract,
        approvals=approvals,
        events=events,
    )
    emit(
        {
            "ok": True,
            "task": task_obj,
            "metadata": metadata,
            "evidence": evidence,
            "evidence_records": evidence_records,
            "completion_contract": completion_contract,
            "ceo_acceptance_contract": ceo_acceptance_contract,
            "completion_invalid": bool(completion_contract.get("done_like")) and not bool(completion_contract.get("valid")),
            "completion_invalid_reason": "" if bool(completion_contract.get("valid")) else str(completion_contract.get("reason", "")),
            "final_evidence_count": int(completion_contract.get("final_evidence_count") or 0),
            "blocker": task_obj.get("blocker", ""),
            "parents": parents,
            "children": children,
            "events": events,
            "hook_runs": hook_runs,
            "attempts": attempts,
            "attempt_history": attempt_history,
            "runtime_sessions": runtime_sessions,
            "tool_calls": tool_calls,
            "budget_events": budget_events,
            "budget_summary": task_budget_summary,
            "control_plane_timeline": control_plane_timeline,
            "projects": projects,
            "conversation_summary": task_conversation_summary(conn, metadata),
            "progress_events": task_progress_events(events),
            "correction_events": task_correction_events(events),
            "sanitized_logs": sanitized_logs,
            "supervisor_state": supervisor_state,
            "correction_summary": correction_summary,
            "approvals": approvals,
            "control_action_summary": task_control_action_summary(approvals=approvals, events=events, attempts=attempts),
            "owner_action_timeline": task_owner_action_timeline(approvals=approvals, events=events),
            "lock": dict(lock) if lock else {},
            "audit_logs": audit_rows,
        }
    )
    return 0


def cmd_task_children(args: argparse.Namespace) -> int:
    conn = connect()
    result = task_with_children(conn, args.task_id)
    emit({"ok": True, **result})
    return 0


def cmd_openclaw_native_status(args: argparse.Namespace) -> int:
    status = openclaw_native_status()
    emit(openclaw_native_status_summary(status) if args.summary else status)
    return 0


def cmd_openclaw_dispatch_plan(args: argparse.Namespace) -> int:
    result = openclaw_native_dispatch_plan(
        source=args.source,
        target=args.target,
        task_type=args.type,
        priority=args.priority,
        goal=args.goal,
        next_command=args.next_command,
        expected_evidence=args.expected_evidence,
        rollback=args.rollback,
        task_id=args.task_id,
    )
    emit(result)
    return 0 if result.get("ok") else 1


def cmd_openclaw_dispatch_execute(args: argparse.Namespace) -> int:
    result = openclaw_native_dispatch_execute(
        source=args.source,
        target=args.target,
        task_type=args.type,
        priority=args.priority,
        goal=args.goal,
        next_command=args.next_command,
        expected_evidence=args.expected_evidence,
        rollback=args.rollback,
        approval_id=args.approval_id,
        task_id=args.task_id,
    )
    emit(result)
    return 0 if result.get("ok") else 1


def cmd_openclaw_import_results(args: argparse.Namespace) -> int:
    result = openclaw_native_import_results(limit=args.limit, agent=args.agent)
    emit(result)
    return 0 if result.get("ok") else 1


def parse_split_item(raw: str) -> dict:
    parts = raw.split("|", 3)
    if len(parts) < 2:
        raise SystemExit("split item must be target|title or target|title|description|priority")
    return {
        "target": parts[0].strip(),
        "title": parts[1].strip(),
        "description": parts[2].strip() if len(parts) >= 3 else "",
        "priority": parts[3].strip() if len(parts) >= 4 and parts[3].strip() else "P2",
    }


def split_items_from_plan(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_items = payload.get("items", payload if isinstance(payload, list) else [])
    if not isinstance(raw_items, list):
        raise SystemExit("split plan must be a JSON list or an object with items")
    items = []
    for index, item in enumerate(raw_items, start=1):
        if not isinstance(item, dict):
            raise SystemExit(f"split plan item {index} must be an object")
        target = str(item.get("target") or item.get("to") or "").strip()
        title = str(item.get("title") or "").strip()
        if not target or not title:
            raise SystemExit(f"split plan item {index} requires target and title")
        items.append(
            {
                "target": target,
                "title": title,
                "description": str(item.get("description") or "").strip(),
                "priority": str(item.get("priority") or "P2").strip() or "P2",
            }
        )
    return items


def cmd_task_split(args: argparse.Namespace) -> int:
    conn = connect()
    splitter = resolve_employee_alias(args.by)
    require_employee(conn, splitter)
    parent = conn.execute("SELECT * FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
    if not parent:
        emit({"ok": False, "error": "parent task not found", "task_id": args.task_id})
        return 1
    if splitter not in {parent["source_agent"], parent["target_agent"], parent["claimed_by"]}:
        emit({"ok": False, "error": "splitter is not related to parent task", "task_id": args.task_id, "by": splitter})
        return 1
    split_items = [parse_split_item(raw_item) for raw_item in args.item]
    if args.plan:
        split_items.extend(split_items_from_plan(Path(args.plan)))
    if not split_items:
        emit({"ok": False, "error": "no split items", "task_id": args.task_id})
        return 1
    created = []
    for index, item in enumerate(split_items, start=1):
        target = resolve_employee_alias(item["target"])
        child_id = args.child_id_prefix + f"-{index:02d}" if args.child_id_prefix else f"{args.task_id}-sub-{index:02d}"
        result = submit_task_internal(
            conn,
            source=splitter,
            target=target,
            title=item["title"],
            description=item["description"],
            priority=item["priority"],
            task_id=child_id,
            metadata={"parent_task_id": args.task_id, "split_by": splitter, "split_index": index},
        )
        conn.execute(
            "INSERT OR IGNORE INTO task_relations(parent_task_id, child_task_id, relation_type, created_by, created_at) VALUES (?, ?, 'subtask', ?, ?)",
            (args.task_id, result["task"]["id"], splitter, now()),
        )
        conn.commit()
        created.append(result)
    child_ids = [item["task"]["id"] for item in created]
    event = record_event(conn, "task.split", splitter, task_id=args.task_id, payload={"children": child_ids, "plan": args.plan})
    conn.execute("UPDATE company_events SET processed_at = ? WHERE id = ?", (now(), event["id"]))
    conn.commit()
    audit(conn, splitter, "task.split", args.task_id, {"children": child_ids, "plan": args.plan, "event_id": event["id"]})
    emit({"ok": True, "parent_task_id": args.task_id, "children": created, "event_id": event["id"]})
    return 0


def cmd_task_collect(args: argparse.Namespace) -> int:
    conn = connect()
    collector = resolve_employee_alias(args.agent)
    require_employee(conn, collector)
    result = task_with_children(conn, args.task_id)
    parent = result["task"]
    children = result["children"]
    if collector not in {parent["source_agent"], parent["target_agent"], parent["claimed_by"]}:
        emit({"ok": False, "error": "collector is not related to parent task", "task_id": args.task_id, "agent": collector})
        return 1
    if not children:
        emit({"ok": False, "error": "parent task has no children", "task_id": args.task_id})
        return 1
    incomplete = [task for task in children if task["status"] != "completed"]
    missing_evidence = [task for task in children if task["status"] == "completed" and not task.get("evidence_path")]
    missing_files = [task for task in children if task.get("evidence_path") and not Path(task["evidence_path"]).exists()]
    if (incomplete or missing_evidence or missing_files) and not args.force:
        emit(
            {
                "ok": False,
                "error": "children are not ready to collect",
                "incomplete": incomplete,
                "missing_evidence": missing_evidence,
                "missing_files": missing_files,
            }
        )
        return 1
    summary = args.summary or f"Collected {len(children)} child tasks."
    evidence = str(Path(args.evidence)) if args.evidence else str(write_task_collection_report(parent, children, collector, summary))
    completed = complete_task_internal(conn, agent=collector, task_id=args.task_id, summary=summary, evidence=evidence)
    audit(conn, collector, "task.collect", args.task_id, {"children": [task["id"] for task in children], "evidence": evidence})
    emit({"ok": True, "parent_task_id": args.task_id, "children": children, "collection": completed})
    return 0


def cmd_task_claim(args: argparse.Namespace) -> int:
    conn = connect()
    agent = resolve_employee_alias(args.agent)
    require_employee(conn, agent)
    if args.task_id:
        task = conn.execute("SELECT * FROM tasks WHERE id = ? AND target_agent = ?", (args.task_id, agent)).fetchone()
    else:
        task = conn.execute(
            "SELECT * FROM tasks WHERE target_agent = ? AND status = 'submitted' ORDER BY created_at LIMIT 1",
            (agent,),
        ).fetchone()
    if not task:
        emit({"ok": False, "error": "no claimable task", "agent": agent})
        return 1
    guard = guard_task_claim(conn, task, agent)
    if not guard["allowed"]:
        audit(conn, agent, "task.claim.blocked_by_guard", task["id"], guard)
        emit({"ok": False, "error": "guard blocked task claim", "agent": agent, "task_id": task["id"], "guard": guard})
        return 2
    ts = now()
    lease_until = future_seconds(args.lease_seconds)
    conn.execute(
        "UPDATE tasks SET status = 'claimed', claimed_by = ?, updated_at = ? WHERE id = ?",
        (agent, ts, task["id"]),
    )
    conn.execute(
        """
        INSERT INTO locks(resource_key, owner_agent, lease_until, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(resource_key) DO UPDATE SET
          owner_agent = excluded.owner_agent,
          lease_until = excluded.lease_until,
          updated_at = excluded.updated_at
        """,
        (f"task:{task['id']}", agent, lease_until, ts, ts),
    )
    conn.commit()
    audit(conn, agent, "task.claim", task["id"], {"lease_until": lease_until})
    updated = dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task["id"],)).fetchone())
    workspace = ensure_task_workspace(conn, task["id"], trace_id_for_task(conn, task["id"]))
    context_package = build_task_context_package_internal(conn, task_id=task["id"], employee_id=agent)
    emit({"ok": True, "task": {**updated, "metadata": task_metadata(conn, task["id"]), "workspace": workspace}, "context_package": context_package})
    return 0


def read_evidence_text(evidence_path: str, max_chars: int = 20000) -> tuple[str, str]:
    """Read a task's evidence/report file so the owner can see results in the console instead
    of digging through files. Path-safe: only reads files inside the kernel tree."""
    if not evidence_path:
        return "", "no evidence path recorded"
    try:
        path = Path(evidence_path).expanduser().resolve()
    except (OSError, RuntimeError):
        return "", "evidence path unresolvable"
    if not (path == ROOT or ROOT in path.parents):
        return "", f"evidence outside kernel: {evidence_path}"
    if path.is_dir():
        # if a dir was recorded, surface the newest report-like file inside it
        candidates = sorted(path.glob("**/*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            return "", f"evidence dir has no report file: {path}"
        path = candidates[0]
    if not path.is_file():
        return "", f"evidence file missing: {path}"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return "", f"cannot read evidence: {exc}"
    return text[:max_chars], ""


def completed_report_rows(conn: sqlite3.Connection, *, limit: int = 40, include_blocked: bool = True) -> list[dict]:
    """Top-level (non-subtask) tasks that have finished — the owner-facing 'what got done' feed.
    Subtasks spawned between agents are excluded so the owner sees only the work they care about."""
    statuses = ("completed", "blocked") if include_blocked else ("completed",)
    placeholders = ",".join("?" for _ in statuses)
    rows_out = rows(
        conn,
        f"""
        SELECT id, title, status, source_agent, target_agent, summary, evidence_path, blocker, updated_at
        FROM tasks
        WHERE status IN ({placeholders})
          AND id NOT IN (SELECT child_task_id FROM task_relations)
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (*statuses, limit),
    )
    return rows_out


def cmd_task_report(args: argparse.Namespace) -> int:
    conn = connect()
    if not args.task_id:
        items = completed_report_rows(conn, limit=args.limit, include_blocked=not args.completed_only)
        emit({"ok": True, "reports": items})
        return 0
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
    if not row:
        emit({"ok": False, "error": "task not found", "task_id": args.task_id})
        return 1
    task = dict(row)
    text, note = read_evidence_text(task.get("evidence_path") or "")
    emit({
        "ok": True,
        "task_id": task["id"],
        "title": task.get("title"),
        "status": task.get("status"),
        "source_agent": task.get("source_agent"),
        "target_agent": task.get("target_agent"),
        "summary": task.get("summary"),
        "blocker": task.get("blocker"),
        "evidence_path": task.get("evidence_path"),
        "report_text": text,
        "note": note,
        "updated_at": task.get("updated_at"),
    })
    return 0


def cmd_task_done(args: argparse.Namespace) -> int:
    conn = connect()
    agent = resolve_employee_alias(args.agent)
    try:
        result = complete_task_internal(conn, agent=agent, task_id=args.task_id, summary=args.summary, evidence=args.evidence)
    except ValueError as exc:
        emit({"ok": False, "error": str(exc), "task_id": args.task_id})
        return 2
    except SystemExit:
        emit({"ok": False, "error": "task not found or not owned by agent", "task_id": args.task_id})
        return 1
    emit({"ok": True, **result})
    return 0


def cmd_task_artifact_register(args: argparse.Namespace) -> int:
    conn = connect()
    employee = resolve_employee_alias(args.employee)
    try:
        result = register_artifact_internal(
            conn,
            task_id=args.task_id,
            employee_id=employee,
            path=args.path,
            artifact_type=args.type,
            name=args.name,
            stage=args.stage,
            summary=args.summary,
            is_input=args.input,
            is_final=args.final,
            metadata=parse_json_arg(args.metadata, {}),
        )
    except ValueError as exc:
        emit({"ok": False, "error": str(exc), "task_id": args.task_id, "path": args.path})
        return 2
    emit({"ok": True, **result})
    return 0


def cmd_task_artifact_scan(args: argparse.Namespace) -> int:
    conn = connect()
    employee = resolve_employee_alias(args.employee)
    try:
        result = scan_artifacts_internal(
            conn,
            task_id=args.task_id,
            employee_id=employee,
            scan_dir=args.dir,
            artifact_type=args.type,
            stage=args.stage,
            summary=args.summary,
            pattern=args.pattern,
        )
    except ValueError as exc:
        emit({"ok": False, "error": str(exc), "task_id": args.task_id, "dir": args.dir})
        return 2
    emit({"ok": True, **result})
    return 0


def cmd_task_artifact_approve(args: argparse.Namespace) -> int:
    conn = connect()
    actor = resolve_employee_alias(args.by)
    result = approve_artifact_internal(conn, artifact_id=args.artifact_id, by=actor, status=args.status, reason=args.reason or args.summary)
    emit({"ok": True, **result})
    return 0


def cmd_task_artifact_use(args: argparse.Namespace) -> int:
    conn = connect()
    employee = resolve_employee_alias(args.employee)
    result = use_artifact_internal(conn, task_id=args.task_id, artifact_id=args.artifact_id, employee_id=employee, purpose=args.purpose or args.summary)
    emit({"ok": True, **result})
    return 0


def cmd_task_evidence_promote(args: argparse.Namespace) -> int:
    conn = connect()
    actor = resolve_employee_alias(args.by or args.employee)
    result = promote_artifact_to_evidence_internal(conn, artifact_id=args.artifact_id, by=actor, summary=args.summary, evidence_type=args.type)
    emit({"ok": True, **result})
    return 0


def cmd_task_evidence_accept(args: argparse.Namespace) -> int:
    conn = connect()
    result = decide_evidence_internal(conn, evidence_id=args.evidence_id, by=args.by, status="accepted", summary=args.summary)
    emit(result)
    return 0 if result.get("ok") else 1


def cmd_task_evidence_reject(args: argparse.Namespace) -> int:
    conn = connect()
    result = decide_evidence_internal(conn, evidence_id=args.evidence_id, by=args.by, status="rejected", summary=args.summary, reason=args.reason)
    emit(result)
    return 0 if result.get("ok") else 1


def cmd_task_handoff_create(args: argparse.Namespace) -> int:
    conn = connect()
    from_employee = resolve_employee_alias(args.from_employee)
    to_employee = resolve_employee_alias(args.to_employee) if args.to_employee else ""
    result = create_handoff_internal(
        conn,
        from_task_id=args.from_task,
        to_task_id=args.to_task,
        from_employee_id=from_employee,
        to_employee_id=to_employee,
        summary=args.summary,
        artifacts=args.artifact,
        known_issues=args.known_issues,
        next_steps=args.next_steps,
        required_actions=args.required_actions,
        acceptance_notes=args.acceptance_notes,
    )
    emit({"ok": True, **result})
    return 0


def cmd_task_handoff_status(args: argparse.Namespace) -> int:
    conn = connect()
    actor = resolve_employee_alias(args.by)
    result = update_handoff_status_internal(conn, handoff_id=args.handoff_id, by=actor, status=args.handoff_status, reason=args.reason)
    emit({"ok": True, **result})
    return 0


def cmd_task_context(args: argparse.Namespace) -> int:
    conn = connect()
    employee = resolve_employee_alias(args.employee) if args.employee else ""
    result = build_task_context_package_internal(conn, task_id=args.task_id, employee_id=employee)
    emit({"ok": True, **result})
    return 0


def cmd_task_attempt_start(args: argparse.Namespace) -> int:
    conn = connect()
    employee = resolve_employee_alias(args.employee)
    result = start_execution_attempt_internal(conn, task_id=args.task_id, employee_id=employee, adapter_type=args.adapter_type, metadata=parse_json_arg(args.metadata, {}))
    emit({"ok": True, **result})
    return 0


def cmd_task_attempt_finish(args: argparse.Namespace) -> int:
    conn = connect()
    try:
        result = finish_execution_attempt_internal(conn, attempt_id=args.attempt_id, status=args.status, error=args.error)
    except ValueError as exc:
        emit({"ok": False, "error": str(exc), "attempt_id": args.attempt_id})
        return 2
    emit({"ok": True, **result})
    return 0


BLOCK_NOTIFY_COOLDOWN_SECONDS = 6 * 3600  # re-alert the owner about the same task+kind at most once / 6h


def _should_notify_block(task_id: str, category: str) -> bool:
    """Dedup the blocked-task Telegram: skip if this task already alerted with the same blocker
    category within the cooldown (so a block→retry→block loop doesn't flood). A changed category
    re-alerts. Best-effort; any error → notify (fail open)."""
    path = ROOT / "state" / "block-notify-dedup.json"
    try:
        store = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        store = {}
    entry = store.get(task_id)
    if entry and entry.get("category") == category:
        try:
            if (datetime.now(timezone.utc) - parse_time(entry.get("at", ""))).total_seconds() < BLOCK_NOTIFY_COOLDOWN_SECONDS:
                return False
        except Exception:  # noqa: BLE001
            pass
    store[task_id] = {"category": category, "at": now()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return True


def cmd_task_block(args: argparse.Namespace) -> int:
    conn = connect()
    agent = resolve_employee_alias(args.agent)
    task = conn.execute("SELECT * FROM tasks WHERE id = ? AND (target_agent = ? OR claimed_by = ?)", (args.task_id, agent, agent)).fetchone()
    if not task:
        emit({"ok": False, "error": "task not found or not owned by agent", "task_id": args.task_id})
        return 1
    conn.execute(
        "UPDATE tasks SET status = 'blocked', blocker = ?, updated_at = ? WHERE id = ? AND (target_agent = ? OR claimed_by = ?)",
        (args.blocker, now(), args.task_id, agent, agent),
    )
    conn.execute("DELETE FROM locks WHERE resource_key = ?", (f"task:{args.task_id}",))
    synced_plan_items = sync_project_plan_for_task(conn, task_id=args.task_id, task_status="blocked", actor=agent)
    conn.commit()
    event = record_event(conn, "task.blocked", agent, task_id=args.task_id, trace_id=trace_id_for_task(conn, args.task_id), payload={"blocker": args.blocker})
    audit(conn, agent, "task.block", args.task_id, {"blocker": args.blocker, "event_id": event["id"]})
    notice_path = deliver_completion_notice(conn, dict(task), status="blocked", blocker=args.blocker, actor=agent)
    try:
        project_memory.capture_task_outcome(conn, dict(task), kind="blocked", blocker=args.blocker)
    except Exception:
        pass
    # push the triage to the owner's Telegram: WHY it stuck + the suggested action + one-tap retry/discard.
    # Deduped so a task that blocks → retries → blocks again doesn't spam the same alert repeatedly.
    try:
        tri = classify_blocker(args.blocker)
        if _should_notify_block(args.task_id, tri["category"]):
            notification_send_result(
            kind="error",
            subject=f"⛔ 任务受阻:{str(task['title'])[:42]}",
            message=(f"[{tri['label']}] {tri['reason']}\n👉 {tri['action']}\n"
                     f"{task.get('source_agent','')} → {task.get('target_agent','')} · {args.task_id}"),
            reply_markup={"inline_keyboard": [[
                {"text": "🔧 重试", "callback_data": f"ck_fix:{args.task_id}"},
                {"text": "🗑 丢弃", "callback_data": f"ck_discard:{args.task_id}"},
                {"text": "⏭ 先放着", "callback_data": f"ck_skip:{args.task_id}"},
            ]]},
        )
    except Exception:
        pass
    emit({"ok": True, "task_id": args.task_id, "status": "blocked", "blocker": args.blocker, "event_id": event["id"], "synced_plan_items": synced_plan_items, "dispatcher_notified": notice_path or None})
    return 0


def cmd_task_retry(args: argparse.Namespace) -> int:
    conn = connect()
    actor = resolve_employee_alias(args.by)
    require_employee(conn, actor)
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
    if not task:
        emit({"ok": False, "error": "task not found", "task_id": args.task_id})
        return 2
    if not can_manage_task_recovery(conn, task, actor):
        emit({"ok": False, "error": "actor cannot retry task", "task_id": args.task_id, "by": actor})
        return 2
    ts = now()
    target = task["target_agent"]
    previous_attempt = latest_attempt_for_task(conn, args.task_id, target) or latest_attempt_for_task(conn, args.task_id)
    inherited_policy = attempt_json_field(previous_attempt, "runtime_policy_json") if previous_attempt else {}
    previous_attempt_id = previous_attempt["attempt_id"] if previous_attempt else ""
    conn.execute("UPDATE tasks SET status = 'claimed', claimed_by = ?, blocker = '', updated_at = ? WHERE id = ?", (target, ts, args.task_id))
    conn.execute("DELETE FROM locks WHERE resource_key = ?", (f"task:{args.task_id}",))
    metadata = update_task_metadata(conn, args.task_id, {"recovery": {"retry_requested_by": actor, "retry_reason": args.reason, "retried_at": ts}})
    conn.commit()
    attempt = start_execution_attempt_internal(
        conn,
        task_id=args.task_id,
        employee_id=target,
        adapter_type="retry",
        status="starting",
        runtime_policy=inherited_policy,
        metadata={"reason": args.reason, "by": actor, "previous_attempt_id": previous_attempt_id},
    )
    event = record_event(conn, "task.retrying", actor, task_id=args.task_id, payload={"reason": args.reason, "attempt_id": attempt["attempt"]["attempt_id"], "previous_attempt_id": previous_attempt_id})
    audit(conn, actor, "task.retry", args.task_id, {"reason": args.reason, "event_id": event["id"], "attempt_id": attempt["attempt"]["attempt_id"]})
    emit({"ok": True, "task_id": args.task_id, "status": "claimed", "task": dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (args.task_id,)).fetchone()), "metadata": metadata, "attempt": hydrate_execution_attempt(row_by_id(conn, "execution_attempts", "attempt_id", attempt["attempt"]["attempt_id"])), "event_id": event["id"]})
    return 0


def cmd_task_reopen(args: argparse.Namespace) -> int:
    conn = connect()
    actor = resolve_employee_alias(args.by)
    require_employee(conn, actor)
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
    if not task:
        emit({"ok": False, "error": "task not found", "task_id": args.task_id})
        return 2
    if not can_manage_task_recovery(conn, task, actor):
        emit({"ok": False, "error": "actor cannot reopen task", "task_id": args.task_id, "by": actor})
        return 2
    claimed_by = "" if args.status == "submitted" else task["claimed_by"]
    # Optional corrected brief: fix the root cause (e.g. add the absolute repo path) before re-queueing,
    # otherwise a context-starved task just re-blocks on the next attempt.
    new_description = getattr(args, "description", "") or ""
    if new_description.strip():
        conn.execute(
            "UPDATE tasks SET status = ?, claimed_by = ?, blocker = '', description = ?, updated_at = ? WHERE id = ?",
            (args.status, claimed_by, new_description, now(), args.task_id),
        )
    else:
        conn.execute(
            "UPDATE tasks SET status = ?, claimed_by = ?, blocker = '', updated_at = ? WHERE id = ?",
            (args.status, claimed_by, now(), args.task_id),
        )
    conn.execute("DELETE FROM locks WHERE resource_key = ?", (f"task:{args.task_id}",))
    synced_plan_items = sync_project_plan_for_task(conn, task_id=args.task_id, task_status=args.status, actor=actor)
    event = record_event(conn, "task.reopened", actor, task_id=args.task_id, payload={"reason": args.reason, "status": args.status})
    conn.commit()
    updated = dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (args.task_id,)).fetchone())
    task_file = write_task_inbox_file(updated)
    audit(conn, actor, "task.reopen", args.task_id, {"reason": args.reason, "status": args.status, "file": task_file, "event_id": event["id"]})
    emit({"ok": True, "task": updated, "file": task_file, "synced_plan_items": synced_plan_items, "event_id": event["id"]})
    return 0


def acknowledge_task_adapter_runs(conn: sqlite3.Connection, task_id: str, by: str, reason: str) -> int:
    """Mark a task's unacknowledged failed adapter runs as acknowledged. Called when the task is
    handled (discarded/cancelled) so a resolved task doesn't leave a stale 'failed run' flag behind."""
    cur = conn.execute(
        """
        UPDATE adapter_runs
        SET acknowledged_at = ?, acknowledged_by = ?, acknowledgement_reason = ?
        WHERE task_id = ? AND ok = 0 AND acknowledged_at = ''
        """,
        (now(), by, reason, task_id),
    )
    return cur.rowcount


def cmd_task_discard(args: argparse.Namespace) -> int:
    """Owner/supervisor drops a stuck task off the board (status=cancelled) without retrying it.
    For tasks whose info is too incomplete to act on — discard instead of letting them linger blocked."""
    conn = connect()
    actor = resolve_employee_alias(args.by)
    require_employee(conn, actor)
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
    if not task:
        emit({"ok": False, "error": "task not found", "task_id": args.task_id})
        return 2
    if not can_manage_task_recovery(conn, task, actor):
        emit({"ok": False, "error": "actor cannot discard task", "task_id": args.task_id, "by": actor})
        return 2
    conn.execute(
        "UPDATE tasks SET status = 'cancelled', claimed_by = '', blocker = ?, updated_at = ? WHERE id = ?",
        (f"discarded by {actor}: {args.reason}", now(), args.task_id),
    )
    acked_runs = acknowledge_task_adapter_runs(conn, args.task_id, actor, f"task discarded: {args.reason}")
    conn.execute("DELETE FROM locks WHERE resource_key = ?", (f"task:{args.task_id}",))
    sync_project_plan_for_task(conn, task_id=args.task_id, task_status="cancelled", actor=actor)
    event = record_event(conn, "task.discarded", actor, task_id=args.task_id, payload={"reason": args.reason})
    conn.commit()
    updated = dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (args.task_id,)).fetchone())
    # Tell the dispatcher their task was dropped — so they stop waiting and can re-dispatch or move on
    # (same result-*.json channel as completions; Hermes consumes 'cancelled' and re-plans).
    notice_path = deliver_completion_notice(
        conn, updated, status="cancelled", blocker=f"已取消(discarded by {actor}): {args.reason}", actor=actor)
    audit(conn, actor, "task.discard", args.task_id, {"reason": args.reason, "event_id": event["id"], "acked_adapter_runs": acked_runs})
    emit({"ok": True, "task": updated, "event_id": event["id"], "acknowledged_adapter_runs": acked_runs,
          "dispatcher_notified": notice_path or None})
    return 0


def cmd_task_reassign(args: argparse.Namespace) -> int:
    conn = connect()
    actor = resolve_employee_alias(args.by)
    target = resolve_employee_alias(args.to)
    require_employee(conn, actor)
    require_employee(conn, target)
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
    if not task:
        emit({"ok": False, "error": "task not found", "task_id": args.task_id})
        return 2
    if not can_manage_task_recovery(conn, task, actor):
        emit({"ok": False, "error": "actor cannot reassign task", "task_id": args.task_id, "by": actor})
        return 2
    # normalize the new target + the task's description (app→cli reroute / executor lock / 记忆会话) just
    # like a fresh dispatch — a reassign must not re-strand work on a passive app or drop project memory.
    target, _new_desc, _norm_err = normalize_submission(conn, target=target, description=task["description"] or "")
    if _norm_err is not None:
        emit(_norm_err)
        return 2
    # same real-on-duty gate as a fresh dispatch (active + fresh heartbeat) — a reassign must not send
    # recovered work to an off-duty/dead worker either.
    inactive = require_active_employee(conn, target, "task.reassign")
    if inactive:
        emit({**inactive, "task_id": args.task_id})
        return 2
    policy = require_communication_allowed(actor, target, "task.submit")
    previous_target = task["target_agent"]
    previous_attempt = latest_attempt_for_task(conn, args.task_id)
    previous_attempt_id = previous_attempt["attempt_id"] if previous_attempt else ""
    conn.execute(
        "UPDATE tasks SET target_agent = ?, description = ?, status = 'submitted', claimed_by = '', blocker = '', updated_at = ? WHERE id = ?",
        (target, _new_desc, now(), args.task_id),
    )
    conn.execute("DELETE FROM locks WHERE resource_key = ?", (f"task:{args.task_id}",))
    synced_plan_items = sync_project_plan_owner_for_task(conn, task_id=args.task_id, owner=target, actor=actor)
    conn.commit()
    updated = dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (args.task_id,)).fetchone())
    attempt_result = start_execution_attempt_internal(
        conn,
        task_id=args.task_id,
        employee_id=target,
        adapter_type="reassign",
        metadata={"reason": args.reason, "by": actor, "previous_attempt_id": previous_attempt_id},
    )
    attempt = hydrate_execution_attempt(row_by_id(conn, "execution_attempts", "attempt_id", attempt_result["attempt"]["attempt_id"]))
    event = record_event(
        conn,
        "task.reassigned",
        actor,
        task_id=args.task_id,
        trace_id=attempt["trace_id"],
        payload={"from": previous_target, "to": target, "reason": args.reason, "attempt_id": attempt["attempt_id"], "previous_attempt_id": previous_attempt_id},
    )
    task_file = write_task_inbox_file({**updated, "metadata": task_metadata(conn, args.task_id), "communication_policy": policy})
    audit(conn, actor, "task.reassign", args.task_id, {"from": previous_target, "to": target, "reason": args.reason, "file": task_file, "attempt_id": attempt["attempt_id"], "previous_attempt_id": previous_attempt_id, "event_id": event["id"]})
    emit({"ok": True, "task": updated, "file": task_file, "attempt": attempt, "event_id": event["id"], "synced_plan_items": synced_plan_items})
    return 0


def parse_acceptance(raw: str) -> list[str]:
    items = []
    for item in raw.split(";"):
        item = item.strip()
        if item:
            items.append(item)
    return items


def cmd_project_create(args: argparse.Namespace) -> int:
    conn = connect()
    owner = resolve_employee_alias(args.owner)
    require_employee(conn, owner)
    project_id = args.project_id or f"project-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    ts = now()
    project = {
        "id": project_id,
        "title": args.title,
        "goal": args.goal,
        "owner_agent": owner,
        "status": args.status,
        "acceptance": parse_acceptance(args.acceptance),
        "created_at": ts,
        "updated_at": ts,
    }
    conn.execute(
        """
        INSERT INTO projects(id, title, goal, owner_agent, status, acceptance_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (project_id, args.title, args.goal, owner, args.status, json.dumps(project["acceptance"], ensure_ascii=False), ts, ts),
    )
    conn.commit()
    audit(conn, owner, "project.create", project_id, project)
    emit({"ok": True, "project": project})
    return 0


def normalize_project(row: sqlite3.Row | dict) -> dict:
    obj = dict(row)
    try:
        obj["acceptance"] = json.loads(obj.pop("acceptance_json", "[]") or "[]")
    except json.JSONDecodeError:
        obj["acceptance"] = []
    return obj


def cmd_project_list(args: argparse.Namespace) -> int:
    conn = connect()
    where = ""
    params: tuple = ()
    if args.status != "all":
        where = "WHERE status = ?"
        params = (args.status,)
    projects = [normalize_project(row) for row in conn.execute(f"SELECT * FROM projects {where} ORDER BY updated_at DESC", params).fetchall()]
    emit({"ok": True, "projects": projects})
    return 0


def cmd_project_show(args: argparse.Namespace) -> int:
    conn = connect()
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (args.project_id,)).fetchone()
    if not row:
        emit({"ok": False, "error": "project not found", "project_id": args.project_id})
        return 1
    project = normalize_project(row)
    tasks = rows(
        conn,
        """
        SELECT t.*
        FROM project_tasks pt
        JOIN tasks t ON t.id = pt.task_id
        WHERE pt.project_id = ?
        ORDER BY t.updated_at DESC
        """,
        (args.project_id,),
    )
    emit({"ok": True, "project": project, "tasks": tasks, "plan_items": project_plan_items(conn, args.project_id)})
    return 0


def cmd_project_link_task(args: argparse.Namespace) -> int:
    conn = connect()
    if not conn.execute("SELECT 1 FROM projects WHERE id = ?", (args.project_id,)).fetchone():
        emit({"ok": False, "error": "project not found", "project_id": args.project_id})
        return 1
    if not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (args.task_id,)).fetchone():
        emit({"ok": False, "error": "task not found", "task_id": args.task_id})
        return 1
    conn.execute(
        "INSERT OR IGNORE INTO project_tasks(project_id, task_id, created_at) VALUES (?, ?, ?)",
        (args.project_id, args.task_id, now()),
    )
    conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now(), args.project_id))
    conn.commit()
    audit(conn, "companyctl", "project.link_task", args.project_id, {"task_id": args.task_id})
    emit({"ok": True, "project_id": args.project_id, "task_id": args.task_id})
    return 0


def cmd_project_plan_add(args: argparse.Namespace) -> int:
    conn = connect()
    if not project_exists(conn, args.project_id):
        emit({"ok": False, "error": "project not found", "project_id": args.project_id})
        return 1
    if args.task_id and not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (args.task_id,)).fetchone():
        emit({"ok": False, "error": "task not found", "task_id": args.task_id})
        return 1
    owner = resolve_employee_alias(args.owner) if args.owner else ""
    if owner:
        require_employee(conn, owner)
    plan_id = args.plan_id or f"plan-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    ts = now()
    item = {
        "id": plan_id,
        "project_id": args.project_id,
        "title": args.title,
        "task_id": args.task_id,
        "status": args.status,
        "owner_agent": owner,
        "due_at": args.due_at,
        "created_at": ts,
        "updated_at": ts,
    }
    conn.execute(
        """
        INSERT INTO project_plan_items(id, project_id, title, task_id, status, owner_agent, due_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (plan_id, args.project_id, args.title, args.task_id, args.status, owner, args.due_at, ts, ts),
    )
    if args.task_id:
        conn.execute(
            "INSERT OR IGNORE INTO project_tasks(project_id, task_id, created_at) VALUES (?, ?, ?)",
            (args.project_id, args.task_id, ts),
        )
    conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (ts, args.project_id))
    conn.commit()
    audit(conn, owner or "companyctl", "project.plan_add", args.project_id, item)
    emit({"ok": True, "plan_item": item})
    return 0


def cmd_project_plan_list(args: argparse.Namespace) -> int:
    conn = connect()
    if not project_exists(conn, args.project_id):
        emit({"ok": False, "error": "project not found", "project_id": args.project_id})
        return 1
    emit({"ok": True, "project_id": args.project_id, "plan_items": project_plan_items(conn, args.project_id)})
    return 0


def cmd_project_plan_status(args: argparse.Namespace) -> int:
    conn = connect()
    if not project_exists(conn, args.project_id):
        emit({"ok": False, "error": "project not found", "project_id": args.project_id})
        return 1
    ts = now()
    cur = conn.execute(
        """
        UPDATE project_plan_items
        SET status = ?, updated_at = ?
        WHERE project_id = ? AND id = ?
        """,
        (args.status, ts, args.project_id, args.plan_id),
    )
    if cur.rowcount == 0:
        emit({"ok": False, "error": "plan item not found", "project_id": args.project_id, "plan_id": args.plan_id})
        return 1
    conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (ts, args.project_id))
    conn.commit()
    item = conn.execute("SELECT * FROM project_plan_items WHERE project_id = ? AND id = ?", (args.project_id, args.plan_id)).fetchone()
    plan_item = dict(item) if item else {}
    audit(conn, "companyctl", "project.plan_status", args.project_id, {"plan_id": args.plan_id, "status": args.status})
    emit({"ok": True, "plan_item": plan_item})
    return 0


def cmd_project_status(args: argparse.Namespace) -> int:
    conn = connect()
    if args.status not in {"active", "paused", "completed", "blocked"}:
        raise SystemExit(f"unknown project status: {args.status}")
    if args.status == "completed":
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (args.project_id,)).fetchone()
        if not row:
            emit({"ok": False, "error": "project not found", "project_id": args.project_id})
            return 1
        review = project_review_internal(conn, row, args.project_id)["review"]
        emit(
            {
                "ok": False,
                "error": "project is not ready to complete; use project accept after review passes",
                "project_id": args.project_id,
                "review": review,
            }
        )
        return 1
    cur = conn.execute("UPDATE projects SET status = ?, updated_at = ? WHERE id = ?", (args.status, now(), args.project_id))
    if cur.rowcount == 0:
        emit({"ok": False, "error": "project not found", "project_id": args.project_id})
        return 1
    conn.commit()
    audit(conn, "companyctl", "project.status", args.project_id, {"status": args.status})
    emit({"ok": True, "project_id": args.project_id, "status": args.status})
    return 0


def project_tasks(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    return rows(
        conn,
        """
        SELECT t.*
        FROM project_tasks pt
        JOIN tasks t ON t.id = pt.task_id
        WHERE pt.project_id = ?
        ORDER BY t.updated_at DESC
        """,
        (project_id,),
    )


def task_project_costs(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    projects = rows(
        conn,
        """
        SELECT p.*
        FROM project_tasks pt
        JOIN projects p ON p.id = pt.project_id
        WHERE pt.task_id = ?
        ORDER BY p.updated_at DESC
        """,
        (task_id,),
    )
    summary = budget_summary(conn)
    by_currency = summary.get("by_project_by_currency", {}) if isinstance(summary.get("by_project_by_currency"), dict) else {}
    by_events = summary.get("by_project_event_count", {}) if isinstance(summary.get("by_project_event_count"), dict) else {}
    by_input = summary.get("by_project_token_input", {}) if isinstance(summary.get("by_project_token_input"), dict) else {}
    by_output = summary.get("by_project_token_output", {}) if isinstance(summary.get("by_project_token_output"), dict) else {}
    by_runtime = summary.get("by_project_runtime_seconds", {}) if isinstance(summary.get("by_project_runtime_seconds"), dict) else {}
    enriched = []
    for project in projects:
        project_id = str(project.get("id") or "")
        project_currency = by_currency.get(project_id, {}) if isinstance(by_currency.get(project_id, {}), dict) else {}
        enriched.append(
            {
                **project,
                "budget_by_currency": project_currency,
                "budget_total": round(sum(float(amount or 0) for amount in project_currency.values()), 6),
                "budget_currency": next(iter(project_currency.keys()), "USD") if len(project_currency) <= 1 else "mixed",
                "budget_event_count": int(by_events.get(project_id, 0) or 0),
                "token_input": int(by_input.get(project_id, 0) or 0),
                "token_output": int(by_output.get(project_id, 0) or 0),
                "runtime_seconds": int(by_runtime.get(project_id, 0) or 0),
            }
        )
    return enriched


def project_plan_items(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    return rows(
        conn,
        """
        SELECT ppi.*,
               COALESCE(t.status, '') AS task_status,
               COALESCE(t.evidence_path, '') AS task_evidence_path,
               COALESCE(t.blocker, '') AS task_blocker
        FROM project_plan_items ppi
        LEFT JOIN tasks t ON t.id = ppi.task_id
        WHERE ppi.project_id = ?
        ORDER BY ppi.created_at ASC
        """,
        (project_id,),
    )


def project_exists(conn: sqlite3.Connection, project_id: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone())


def project_review_internal(conn: sqlite3.Connection, project_row: sqlite3.Row | dict, project_id: str) -> dict:
    project = normalize_project(project_row)
    tasks = project_tasks(conn, project_id)
    plan_items = project_plan_items(conn, project_id)
    total = len(tasks)
    completed = [task for task in tasks if task["status"] == "completed"]
    blocked = [task for task in tasks if task["status"] == "blocked"]
    open_tasks = [task for task in tasks if task["status"] not in {"completed", "blocked"}]
    open_plan_items = [item for item in plan_items if item["status"] not in {"done", "completed", "cancelled"}]
    completed_without_evidence = [task for task in completed if not task.get("evidence_path")]
    evidence_missing_on_disk = []
    for task in completed:
        ep = task.get("evidence_path")
        if ep:
            if Path(ep).exists():
                continue
            if final_evidence_for_path(conn, task["id"], ep):
                continue
            evidence_missing_on_disk.append(task)
    ready = total > 0 and not open_tasks and not blocked and not open_plan_items and not completed_without_evidence and not evidence_missing_on_disk
    review = {
        "project_id": project_id,
        "ready_to_complete": ready,
        "task_counts": {
            "total": total,
            "completed": len(completed),
            "blocked": len(blocked),
            "open": len(open_tasks),
            "completed_without_evidence": len(completed_without_evidence),
            "evidence_missing_on_disk": len(evidence_missing_on_disk),
        },
        "plan_counts": {
            "total": len(plan_items),
            "open": len(open_plan_items),
            "done": len(plan_items) - len(open_plan_items),
        },
        "acceptance_checklist": [{"item": item, "status": "manual_review_required"} for item in project["acceptance"]],
        "open_plan_items": open_plan_items,
        "blocked_tasks": blocked,
        "open_tasks": open_tasks,
        "completed_without_evidence": completed_without_evidence,
        "evidence_missing_on_disk": evidence_missing_on_disk,
    }
    return {"project": project, "tasks": tasks, "plan_items": plan_items, "review": review}


def cmd_project_review(args: argparse.Namespace) -> int:
    conn = connect()
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (args.project_id,)).fetchone()
    if not row:
        emit({"ok": False, "error": "project not found", "project_id": args.project_id})
        return 1
    result = project_review_internal(conn, row, args.project_id)
    review = result["review"]
    audit(conn, "companyctl", "project.review", args.project_id, review)
    emit({"ok": True, "project": result["project"], "review": review})
    return 0


def cmd_project_accept(args: argparse.Namespace) -> int:
    conn = connect()
    actor = resolve_employee_alias(args.by)
    require_employee(conn, actor)
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (args.project_id,)).fetchone()
    if not row:
        emit({"ok": False, "error": "project not found", "project_id": args.project_id})
        return 1
    if row["status"] == "completed" and not args.force:
        emit({"ok": False, "error": "project already completed", "project_id": args.project_id})
        return 1
    result = project_review_internal(conn, row, args.project_id)
    review = result["review"]
    if not review["ready_to_complete"] and not args.force:
        emit({"ok": False, "error": "project is not ready to complete", "review": review})
        return 1
    acceptance_id = f"pacc-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    ts = now()
    acceptance = {
        "id": acceptance_id,
        "project_id": args.project_id,
        "accepted_by": actor,
        "summary": args.summary,
        "review": review,
        "created_at": ts,
        "force": args.force,
    }
    conn.execute(
        "INSERT INTO project_acceptances(id, project_id, accepted_by, summary, review_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (acceptance_id, args.project_id, actor, args.summary, json.dumps(review, ensure_ascii=False), ts),
    )
    conn.execute("UPDATE projects SET status = 'completed', updated_at = ? WHERE id = ?", (ts, args.project_id))
    conn.commit()
    path = STATE_DIR / "project-acceptances" / f"{acceptance_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(acceptance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit(conn, actor, "project.accept", args.project_id, acceptance)
    emit({"ok": True, "acceptance": acceptance, "file": str(path)})
    return 0


def cmd_lock_acquire(args: argparse.Namespace) -> int:
    conn = connect()
    owner = resolve_employee_alias(args.agent)
    require_employee(conn, owner)
    ts = now()
    lease_until = future_seconds(args.lease_seconds)
    existing = conn.execute("SELECT * FROM locks WHERE resource_key = ?", (args.resource,)).fetchone()
    if existing and parse_time(existing["lease_until"]) > datetime.now(timezone.utc).astimezone() and existing["owner_agent"] != owner:
        emit({"ok": False, "error": "lock held", "lock": dict(existing)})
        return 1
    conn.execute(
        """
        INSERT INTO locks(resource_key, owner_agent, lease_until, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(resource_key) DO UPDATE SET
          owner_agent = excluded.owner_agent,
          lease_until = excluded.lease_until,
          updated_at = excluded.updated_at
        """,
        (args.resource, owner, lease_until, ts, ts),
    )
    conn.commit()
    lock = dict(conn.execute("SELECT * FROM locks WHERE resource_key = ?", (args.resource,)).fetchone())
    audit(conn, owner, "lock.acquire", args.resource, lock)
    emit({"ok": True, "lock": lock})
    return 0


def cmd_lock_release(args: argparse.Namespace) -> int:
    conn = connect()
    owner = resolve_employee_alias(args.agent)
    lock = conn.execute("SELECT * FROM locks WHERE resource_key = ?", (args.resource,)).fetchone()
    if not lock:
        emit({"ok": True, "released": False, "resource": args.resource})
        return 0
    if lock["owner_agent"] != owner and not args.force:
        emit({"ok": False, "error": "lock owned by another agent", "lock": dict(lock)})
        return 1
    conn.execute("DELETE FROM locks WHERE resource_key = ?", (args.resource,))
    conn.commit()
    audit(conn, owner, "lock.release", args.resource, {"force": args.force})
    emit({"ok": True, "released": True, "resource": args.resource})
    return 0


def cmd_lock_list(args: argparse.Namespace) -> int:
    conn = connect()
    where = "WHERE owner_agent = ?" if args.agent else ""
    params = (resolve_employee_alias(args.agent),) if args.agent else ()
    emit({"ok": True, "locks": rows(conn, f"SELECT * FROM locks {where} ORDER BY updated_at DESC", params)})
    return 0


def unlock_stale(conn: sqlite3.Connection) -> list[dict]:
    current = datetime.now(timezone.utc).astimezone()
    stale = []
    for lock in conn.execute("SELECT * FROM locks").fetchall():
        if parse_time(lock["lease_until"]) <= current:
            stale.append(dict(lock))
            conn.execute("DELETE FROM locks WHERE id = ?", (lock["id"],))
    conn.commit()
    return stale


def cmd_lock_unlock_stale(_args: argparse.Namespace) -> int:
    conn = connect()
    stale = unlock_stale(conn)
    audit(conn, "companyctl", "lock.unlock_stale", "", {"count": len(stale), "locks": stale})
    emit({"ok": True, "unlocked": stale})
    return 0


def reset_stale_claims(conn: sqlite3.Connection) -> list[dict]:
    current = datetime.now(timezone.utc).astimezone()
    reset = []
    for task in conn.execute("SELECT * FROM tasks WHERE status = 'claimed'").fetchall():
        lock = conn.execute("SELECT * FROM locks WHERE resource_key = ?", (f"task:{task['id']}",)).fetchone()
        stale = not lock or parse_time(lock["lease_until"]) <= current
        if not stale:
            continue
        before = dict(task)
        conn.execute(
            "UPDATE tasks SET status = 'submitted', claimed_by = '', updated_at = ? WHERE id = ?",
            (now(), task["id"]),
        )
        conn.execute("DELETE FROM locks WHERE resource_key = ?", (f"task:{task['id']}",))
        reset.append(before)
    conn.commit()
    return reset


def cmd_repair_reset_stale_claims(_args: argparse.Namespace) -> int:
    conn = connect()
    unlocked = unlock_stale(conn)
    reset = reset_stale_claims(conn)
    audit(conn, "companyctl", "repair.reset_stale_claims", "", {"unlocked": unlocked, "reset": reset})
    emit({"ok": True, "unlocked_locks": unlocked, "reset_tasks": reset})
    return 0


# ── 看门狗(watchdog/reap/孤儿检测/owner告警)已拆到 company_kernel/watchdog.py ──
# 公共符号在本文件末尾 `from .watchdog import (...)` 处 re-export,外部调用方无感知。


def heartbeat_internal(conn: sqlite3.Connection, agent: str, metadata: dict | None = None) -> dict:
    emp = conn.execute("SELECT * FROM employees WHERE id = ?", (agent,)).fetchone()
    runtime = emp["runtime"] if emp else ""
    workspace = emp["workspace"] if emp else ""
    previous_row = conn.execute("SELECT metadata_json FROM heartbeats WHERE agent_id = ?", (agent,)).fetchone()
    previous_metadata: dict[str, object] = {}
    if previous_row:
        try:
            previous_metadata = json.loads(previous_row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            previous_metadata = {}
    previous_progress = extract_progress_payload(previous_metadata)
    ts = now()
    metadata_payload = dict(metadata or {"source": "companyctl"})
    metadata_payload = progress_bridge_metadata(conn, agent, workspace, metadata_payload)
    progress = extract_progress_payload(metadata_payload)
    if progress.get("layer"):
        metadata_payload["progress"] = {
            "layer": progress["layer"],
            "state": progress["state"],
            "label": progress["label"],
            "summary": progress["summary"],
        }
    conn.execute(
        """
        INSERT INTO heartbeats(agent_id, runtime, workspace, status, last_seen_at, metadata_json)
        VALUES (?, ?, ?, 'alive', ?, ?)
        ON CONFLICT(agent_id) DO UPDATE SET
          runtime = excluded.runtime,
          workspace = excluded.workspace,
          status = 'alive',
          last_seen_at = excluded.last_seen_at,
          metadata_json = excluded.metadata_json
        """,
        (agent, runtime, workspace, ts, json.dumps(metadata_payload, ensure_ascii=False)),
    )
    conn.commit()
    hb = {"agent_id": agent, "runtime": runtime, "workspace": workspace, "status": "alive", "last_seen_at": ts}
    if progress.get("layer"):
        hb["progress"] = progress
    latest_progress = metadata_payload.get("latest_progress")
    if isinstance(latest_progress, dict) and latest_progress.get("path"):
        hb["latest_progress"] = latest_progress
    progress_notification = maybe_record_progress_transition(
        conn,
        agent,
        previous_progress,
        progress,
        task_id=str(metadata_payload.get("task_id", "") or ""),
        trace_id=str(metadata_payload.get("trace_id", "") or ""),
        source=str(metadata_payload.get("source", "heartbeat") or "heartbeat"),
    )
    if progress_notification.get("triggered"):
        hb["progress_notification"] = progress_notification
    if emp:
        p = employee_paths(agent)["heartbeat"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(hb, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit(conn, agent, "heartbeat", agent, hb)
    return hb


def touch_heartbeat_internal(conn: sqlite3.Connection, agent: str) -> str:
    """Lightweight liveness refresh: bump an existing heartbeat's last_seen_at only. Unlike
    heartbeat_internal it does NO progress-bridge IO, file write, audit, or progress-transition
    notification — so the daemon's keepalive thread can call it every few minutes for every worker
    during a long tick (keeping them 'on duty') without generating noise or cost. Pure SQL, free.
    Does not create a row for an agent that never heartbeated (a never-started worker isn't 'alive')."""
    ts = now()
    conn.execute("UPDATE heartbeats SET last_seen_at = ? WHERE agent_id = ?", (ts, agent))
    return ts


def cmd_heartbeat(args: argparse.Namespace) -> int:
    conn = connect()
    hb = heartbeat_internal(conn, args.agent)
    emit({"ok": True, "heartbeat": hb})
    return 0


def check_command(cmd: str) -> dict:
    path = shutil.which(cmd)
    return {"command": cmd, "available": bool(path), "path": path or ""}


def cmd_runtime_register(args: argparse.Namespace) -> int:
    conn = connect()
    runtime = args.runtime.strip()
    if not runtime:
        raise SystemExit("runtime is required")
    ts = now()
    conn.execute(
        """
        INSERT INTO employee_runtimes(runtime, command, status, notes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(runtime) DO UPDATE SET
          command = excluded.command,
          status = excluded.status,
          notes = excluded.notes,
          updated_at = excluded.updated_at
        """,
        (runtime, args.command, args.status, args.notes or KNOWN_RUNTIMES.get(runtime, "Custom runtime adapter"), ts, ts),
    )
    conn.commit()
    row = dict(conn.execute("SELECT * FROM employee_runtimes WHERE runtime = ?", (runtime,)).fetchone())
    audit(conn, "companyctl", "runtime.register", runtime, row)
    emit({"ok": True, "runtime": row})
    return 0


def cmd_runtime_list(args: argparse.Namespace) -> int:
    try:
        conn = connect_readonly()
        registered = {row["runtime"]: dict(row) for row in conn.execute("SELECT * FROM employee_runtimes ORDER BY runtime").fetchall()}
        conn.close()
    except sqlite3.OperationalError:
        registered = {}
    ts = now()
    for runtime, notes in KNOWN_RUNTIMES.items():
        registered.setdefault(
            runtime,
            {"runtime": runtime, "command": "", "status": "registered", "notes": notes, "created_at": ts, "updated_at": ts},
        )
    emit({"ok": True, "runtimes": [registered[key] for key in sorted(registered)]})
    return 0


def cmd_runtime_test(args: argparse.Namespace) -> int:
    checks: list[dict] = []
    if args.runtime == "openclaw":
        checks.append(check_command("openclaw"))
        oc = openclaw_root() / "scripts" / "oc"
        checks.append({"command": str(oc), "available": oc.exists(), "path": str(oc) if oc.exists() else ""})
    elif args.runtime == "hermes":
        checks.append(check_command("hermes"))
        hermes_home = Path(os.environ.get("OPENCLAW_HERMES_WORKSPACE", os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))).expanduser()
        checks.append({"path": str(hermes_home), "available": hermes_home.exists()})
    elif args.runtime == "codex":
        checks.append(check_command("codex"))
        codex_home = Path(os.environ.get("OPENCLAW_CODEX_WORKSPACE", os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))).expanduser()
        checks.append({"path": str(codex_home), "available": codex_home.exists()})
    elif args.runtime == "claude":
        checks.append(check_command("claude"))
    elif args.runtime == "trae":
        checks.append(check_command("trae"))
    elif args.runtime == "antigravity":
        checks.append(check_command("antigravity"))
    else:
        try:
            conn = connect_readonly()
            row = conn.execute("SELECT * FROM employee_runtimes WHERE runtime = ?", (args.runtime,)).fetchone()
            conn.close()
        except sqlite3.OperationalError:
            row = None
        if not row:
            checks.append({"runtime": args.runtime, "available": False, "reason": "runtime_not_registered"})
        elif row["status"] == "disabled":
            checks.append({"runtime": args.runtime, "available": False, "reason": "runtime_disabled"})
        elif row["command"]:
            checks.append(check_command(row["command"].split()[0]))
        else:
            checks.append({"runtime": args.runtime, "available": True, "reason": "registered_without_probe_command"})
    ok = any(c.get("available") for c in checks)
    emit({"ok": ok, "runtime": args.runtime, "checks": checks})
    return 0 if ok else 1


ADAPTER_COMMANDS = {
    "openclaw": "company-openclaw-adapter",
    "hermes": "company-hermes-adapter",
    "codex": "company-codex-adapter",
    "claude": "company-claude-adapter",
    "trae": "company-trae-adapter",
    "antigravity": "company-antigravity-adapter",
    "skill": "company-skill-package-worker",
}


def adapter_verify_agents(conn: sqlite3.Connection, requested: list[str]) -> list[dict]:
    clauses = ["runtime IN (%s)" % ",".join("?" for _ in ADAPTER_COMMANDS)]
    params: list[str] = list(ADAPTER_COMMANDS)
    if requested:
        resolved = [resolve_employee_alias(agent) for agent in requested]
        clauses.append("id IN (%s)" % ",".join("?" for _ in resolved))
        params.extend(resolved)
    sql = f"SELECT * FROM employees WHERE {' AND '.join(clauses)} ORDER BY runtime, id"
    return rows(conn, sql, tuple(params))


def load_json_file(path: Path) -> dict:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def skill_package_for_employee(conn: sqlite3.Connection, employee_id: str) -> str:
    employee = conn.execute("SELECT workspace FROM employees WHERE id = ?", (employee_id,)).fetchone()
    candidates: list[str] = []
    if employee:
        workspace = Path(str(employee["workspace"] or "")).expanduser()
        for path in [workspace / "profile.json", workspace / "capabilities.json", EMPLOYEES_DIR / employee_id / "profile.json", EMPLOYEES_DIR / employee_id / "capabilities.json"]:
            data = load_json_file(path)
            for key in ("skill_package", "skill_package_path", "package", "package_path"):
                value = str(data.get(key) or "").strip()
                if value:
                    candidates.append(value)
            for section_key in ("skill", "package", "runtime"):
                section = data.get(section_key)
                if isinstance(section, dict):
                    for key in ("manifest", "manifest_path", "skill_json", "path"):
                        value = str(section.get(key) or "").strip()
                        if value:
                            candidates.append(value)
    for raw in candidates:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (ROOT / path).resolve()
        if path.is_dir():
            path = path / "skill.json"
        if path.exists():
            return str(path)

    manifests = sorted(SKILL_PACKAGES_DIR.glob("*/skill.json"))
    matches = []
    token = employee_id.removesuffix("-skill")
    for manifest_path in manifests:
        manifest = load_json_file(manifest_path)
        manifest_employee = str(manifest.get("employee_id") or "").strip()
        manifest_id = str(manifest.get("id") or manifest_path.parent.name).strip()
        if manifest_employee == employee_id or manifest_id in {employee_id, token, manifest_path.parent.name} or token == manifest_path.parent.name:
            matches.append(manifest_path)
    if len(matches) == 1:
        return str(matches[0])
    if not matches and len(manifests) == 1:
        return str(manifests[0])
    return ""


def run_companyctl_json(args: list[str]) -> tuple[int, dict, str]:
    cp = subprocess.run([str(ROOT / "bin" / "companyctl"), *args], cwd=str(ROOT), text=True, capture_output=True)
    return cp.returncode, parse_json_output(cp.stdout), cp.stderr


def cmd_runtime_ack_adapter_run(args: argparse.Namespace) -> int:
    conn = connect()
    actor = resolve_employee_alias(args.by)
    require_employee(conn, actor)
    run = conn.execute("SELECT * FROM adapter_runs WHERE id = ?", (args.run_id,)).fetchone()
    if not run:
        emit({"ok": False, "error": "adapter run not found", "run_id": args.run_id})
        return 1
    ts = now()
    conn.execute(
        """
        UPDATE adapter_runs
        SET acknowledged_at = ?, acknowledged_by = ?, acknowledgement_reason = ?
        WHERE id = ?
        """,
        (ts, actor, args.reason, args.run_id),
    )
    conn.commit()
    updated = conn.execute("SELECT * FROM adapter_runs WHERE id = ?", (args.run_id,)).fetchone()
    result = dict(updated) if updated else {}
    audit(conn, actor, "runtime.ack_adapter_run", args.run_id, {"reason": args.reason, "adapter_run": result})
    emit({"ok": True, "adapter_run": result})
    return 0


def cmd_runtime_adapter_runs(args: argparse.Namespace) -> int:
    conn = connect()
    where = []
    params: list[object] = []
    if args.agent:
        where.append("agent_id = ?")
        params.append(resolve_employee_alias(args.agent))
    if args.status == "failed":
        where.append("ok = 0")
    elif args.status == "ok":
        where.append("ok = 1")
    if args.unacknowledged_only:
        where.append("acknowledged_at = ''")
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    adapter_runs = rows(
        conn,
        f"""
        SELECT id, trace_id, agent_id, task_id, command, ok, processed, attempt, next_retry_at,
               acknowledged_at, acknowledged_by, acknowledgement_reason, result_json, created_at
        FROM adapter_runs
        {where_sql}
        ORDER BY created_at DESC
        LIMIT ?
        """,
        tuple([*params, args.limit]),
    )
    for adapter_run in adapter_runs:
        raw_result_json = adapter_run.pop("result_json", "{}")
        try:
            result = json.loads(raw_result_json or "{}")
        except json.JSONDecodeError:
            result = {"raw": raw_result_json}
        adapter_run["sanitized_log"] = summarize_adapter_result(result).get("sanitized_log", "")
    emit({"ok": True, "adapter_runs": adapter_runs})
    return 0


def summarize_adapter_result(result: dict) -> dict:
    runs = []
    log_parts = [
        str(result.get("stdout", "")),
        str(result.get("stderr", "")),
        str(result.get("companyctl_stdout", "")),
        str(result.get("companyctl_stderr", "")),
        str(result.get("reply", "")),
        str(result.get("blocker", "")),
        str(result.get("error", "")),
    ]
    for run in result.get("runs", []):
        if not isinstance(run, dict):
            continue
        command_result = run.get("result", {})
        parsed = run.get("parsed_stdout", {})
        if isinstance(command_result, dict):
            log_parts.extend([str(command_result.get("stdout", "")), str(command_result.get("stderr", ""))])
        if isinstance(parsed, dict):
            log_parts.extend([str(parsed.get("summary", "")), str(parsed.get("blocker", "")), str(parsed.get("error", ""))])
        report_path = str(parsed.get("report", "")) if isinstance(parsed, dict) else ""
        progress_state = ""
        progress_task_id = ""
        progress_report_error = ""
        if report_path:
            try:
                resolved_report = Path(report_path).expanduser().resolve()
                repo_root = Path(ROOT).resolve()
                try:
                    resolved_report.relative_to(repo_root)
                except ValueError:
                    progress_report_error = "outside_repo"
                else:
                    progress_payload = json.loads(resolved_report.read_text(encoding="utf-8"))
                    if isinstance(progress_payload, dict):
                        progress_task_id = str(progress_payload.get("task_id", ""))
                        report = progress_payload.get("report")
                        if isinstance(report, dict):
                            progress_state = str(report.get("state", ""))
            except (OSError, json.JSONDecodeError):
                progress_report_error = "unreadable"
        progress = normalize_progress_state(progress_state)
        runs.append(
            {
                "index": run.get("index", ""),
                "returncode": command_result.get("returncode", "") if isinstance(command_result, dict) else "",
                "task_id": parsed.get("task_id", "") if isinstance(parsed, dict) else "",
                "status": parsed.get("status", "") if isinstance(parsed, dict) else "",
                "processed": parsed.get("processed", "") if isinstance(parsed, dict) else "",
                "report": report_path,
                "progress_state": progress_state,
                "progress_layer": progress.get("layer", ""),
                "progress_label": progress.get("label", ""),
                "progress_task_id": progress_task_id,
                "progress_report_error": progress_report_error,
            }
        )
    return {
        "ok": result.get("ok", False),
        "agent": result.get("agent", ""),
        "command": result.get("command", ""),
        "processed": result.get("processed", 0),
        "at": result.get("at", ""),
        "state_file": result.get("state_file", ""),
        "sanitized_log": sanitize_log_text("\n".join(part for part in log_parts if part), max_length=1600),
        "runs": runs,
    }


def sanitized_log_policy() -> dict:
    return {
        "mode": "sanitized_only",
        "summary": "raw stdout/stderr hidden; secrets and sensitive paths are redacted before dashboard/API display",
        "source_fields": [
            "stdout",
            "stderr",
            "companyctl_stdout",
            "companyctl_stderr",
            "reply",
            "blocker",
            "error",
            "runs.result.stdout",
            "runs.result.stderr",
            "runs.parsed_stdout.summary",
        ],
    }


def cmd_runtime_adapter_run_show(args: argparse.Namespace) -> int:
    conn = connect()
    run = conn.execute("SELECT * FROM adapter_runs WHERE id = ?", (args.run_id,)).fetchone()
    if not run:
        emit({"ok": False, "error": "adapter run not found", "run_id": args.run_id})
        return 1
    adapter_run = dict(run)
    try:
        result = json.loads(adapter_run.get("result_json", "{}") or "{}")
    except json.JSONDecodeError:
        result = {"raw": adapter_run.get("result_json", "")}
    if args.summary:
        adapter_summary = {k: v for k, v in adapter_run.items() if k != "result_json"}
        emit({"ok": True, "adapter_run": adapter_summary, "result_summary": summarize_adapter_result(result)})
        return 0
    emit({"ok": True, "adapter_run": adapter_run, "result": result})
    return 0


def adapter_run_task_id(adapter_run: sqlite3.Row | dict) -> str:
    structured = dict(adapter_run).get("task_id", "")
    if structured:
        return str(structured)
    try:
        result = json.loads((dict(adapter_run).get("result_json") or "{}"))
    except json.JSONDecodeError:
        return ""
    if isinstance(result, dict):
        for run in result.get("runs", []):
            if isinstance(run, dict):
                parsed = run.get("parsed_stdout", {})
                if isinstance(parsed, dict) and parsed.get("task_id"):
                    return str(parsed["task_id"])
    return ""


def cmd_runtime_retry_adapter_run(args: argparse.Namespace) -> int:
    conn = connect()
    actor = resolve_employee_alias(args.by)
    require_employee(conn, actor)
    run = conn.execute("SELECT * FROM adapter_runs WHERE id = ?", (args.run_id,)).fetchone()
    if not run:
        emit({"ok": False, "error": "adapter run not found", "run_id": args.run_id})
        return 1
    if run["ok"]:
        emit({"ok": False, "error": "adapter run did not fail", "run_id": args.run_id})
        return 1
    task_id = args.task_id or adapter_run_task_id(run)
    if not task_id:
        emit({"ok": False, "error": "task id not found in adapter run; pass --task-id", "run_id": args.run_id})
        return 1
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        emit({"ok": False, "error": "task not found", "task_id": task_id})
        return 1
    ts = now()
    conn.execute(
        """
        UPDATE tasks
        SET status = 'submitted', claimed_by = '', blocker = '', updated_at = ?
        WHERE id = ?
        """,
        (ts, task_id),
    )
    conn.execute("DELETE FROM locks WHERE resource_key = ?", (f"task:{task_id}",))
    conn.execute(
        "UPDATE company_events SET processed_at = ? WHERE task_id = ? AND event_type = 'task.blocked' AND processed_at = ''",
        (ts, task_id),
    )
    conn.execute(
        """
        UPDATE adapter_runs
        SET acknowledged_at = ?, acknowledged_by = ?, acknowledgement_reason = ?
        WHERE id = ?
        """,
        (ts, actor, f"retry requested: {args.reason}", args.run_id),
    )
    metadata = update_task_metadata(
        conn,
        task_id,
        {
            "recovery": {
                "retry_adapter_run": args.run_id,
                "retry_requested_by": actor,
                "retry_reason": args.reason,
                "retried_at": ts,
            }
        },
    )
    conn.commit()
    event = record_event(conn, "task.retried", actor, task_id=task_id, payload={"adapter_run_id": args.run_id, "reason": args.reason})
    conn.execute("UPDATE company_events SET processed_at = ? WHERE id = ?", (now(), event["id"]))
    conn.commit()
    audit(conn, actor, "runtime.retry_adapter_run", task_id, {"reason": args.reason, "adapter_run_id": args.run_id, "event_id": event["id"]})
    emit({"ok": True, "run_id": args.run_id, "task_id": task_id, "status": "submitted", "metadata": metadata, "event_id": event["id"]})
    return 0


def resolve_runtime_verify_source(conn: sqlite3.Connection, requested: str = "") -> str:
    requested = (requested or "").strip()
    if requested:
        row = conn.execute("SELECT id FROM employees WHERE id = ?", (requested,)).fetchone()
        if row:
            return requested
        raise ValueError(f"unknown source employee: {requested}")
    for candidate in ("openclaw-main", "main", "owner"):
        row = conn.execute("SELECT id FROM employees WHERE id = ?", (candidate,)).fetchone()
        if row:
            return candidate
    row = conn.execute("SELECT id FROM employees WHERE status = 'active' ORDER BY id LIMIT 1").fetchone()
    if row:
        return row["id"]
    raise ValueError("no source employee available for runtime verification")


def cmd_runtime_verify_adapters(args: argparse.Namespace) -> int:
    conn = connect()
    try:
        source = resolve_runtime_verify_source(conn, args.source)
    except ValueError as exc:
        emit({"ok": False, "error": str(exc)})
        return 1
    agents = adapter_verify_agents(conn, parse_csv(args.agents))
    results = []
    for emp in agents:
        runtime = emp["runtime"]
        command = ADAPTER_COMMANDS.get(runtime, "")
        task_id = args.task_id_prefix + f"-{emp['id']}"
        title = f"Runtime adapter dry-run check: {emp['id']}"
        result = {
            "agent": emp["id"],
            "runtime": runtime,
            "command": command,
            "task_id": task_id,
            "ok": False,
        }
        if not command:
            result["error"] = "no adapter command"
            results.append(result)
            continue
        existing = conn.execute("SELECT status, evidence_path FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not existing:
            if args.allow_candidate:
                try:
                    submit_payload = submit_task_internal(
                        conn,
                        source=source,
                        target=emp["id"],
                        task_id=task_id,
                        title=title,
                        description="Adapter dry-run check task. Adapter must claim, write evidence, complete, and heartbeat.",
                        priority="P3",
                        metadata={"runtime_verify": True, "allow_candidate": True},
                        allow_candidate=True,
                    )
                except SystemExit as exc:
                    result.update({"error": "task submit failed", "submit_stdout": parse_json_output(str(exc)), "submit_stderr": ""})
                    results.append(result)
                    continue
                result["candidate_verification"] = True
            else:
                submit_code, submit_payload, submit_stderr = run_companyctl_json(
                    [
                        "task",
                        "submit",
                        "--from",
                        source,
                        "--to",
                        emp["id"],
                        "--task-id",
                        task_id,
                        "--title",
                        title,
                        "--description",
                        "Adapter dry-run check task. Adapter must claim, write evidence, complete, and heartbeat.",
                        "--priority",
                        "P3",
                    ]
                )
                if submit_code != 0:
                    result.update({"error": "task submit failed", "submit_stdout": submit_payload, "submit_stderr": submit_stderr})
                    results.append(result)
                    continue
                conn.close()
                conn = connect()
        cmd = [str(ROOT / "bin" / command), "--agent", emp["id"]]
        if runtime == "skill":
            package = skill_package_for_employee(conn, emp["id"])
            if not package:
                result["error"] = "skill package not found for employee"
                results.append(result)
                continue
            result["package"] = package
            cmd.extend(["--package", package])
        if args.execute:
            cmd.append("--execute")
        active_attempt = conn.execute(
            """
            SELECT * FROM execution_attempts
            WHERE task_id = ?
              AND employee_id = ?
              AND status IN ('starting', 'running', 'correcting')
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (task_id, emp["id"]),
        ).fetchone()
        if not active_attempt:
            active_attempt = start_execution_attempt_internal(
                conn,
                task_id=task_id,
                employee_id=emp["id"],
                adapter_type="runtime-verify",
                metadata={"runtime_verify": True, "command": command},
                status="running",
            )["attempt"]
        cp = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True)
        current = conn.execute("SELECT status, evidence_path, blocker FROM tasks WHERE id = ?", (task_id,)).fetchone()
        hb = conn.execute("SELECT last_seen_at FROM heartbeats WHERE agent_id = ?", (emp["id"],)).fetchone()
        evidence = current["evidence_path"] if current else ""
        final_evidence_record = ensure_final_evidence_for_existing_path(conn, task_id=task_id, agent=emp["id"], evidence_path=evidence, summary="runtime verification evidence") if current and evidence else None
        final_evidence = bool(final_evidence_record)
        result.update(
            {
                "exit_code": cp.returncode,
                "stdout": parse_json_output(cp.stdout),
                "stderr": cp.stderr,
                "task_status": current["status"] if current else "",
                "evidence": evidence,
                "evidence_exists": bool(evidence and Path(evidence).exists()),
                "final_evidence": final_evidence,
                "final_evidence_id": final_evidence_record.get("evidence_id", "") if isinstance(final_evidence_record, dict) else "",
                "blocker": current["blocker"] if current else "",
                "heartbeat": hb["last_seen_at"] if hb else "",
            }
        )
        result["ok"] = cp.returncode == 0 and result["task_status"] == "completed" and result["evidence_exists"] and final_evidence and bool(result["heartbeat"])
        results.append(result)
    scheduler_result = {}
    if args.run_scheduler:
        cp = subprocess.run(
            [str(ROOT / "bin" / "companyctl"), "scheduler", "run", "--limit", str(max(20, len(results) * 2))],
            cwd=str(ROOT),
            text=True,
            capture_output=True,
        )
        scheduler_result = {
            "exit_code": cp.returncode,
            "stdout": parse_json_output(cp.stdout),
            "stderr": cp.stderr,
        }
    ok = all(item["ok"] for item in results) if results else False
    if args.run_scheduler and scheduler_result.get("exit_code") != 0:
        ok = False
    audit_error = ""
    try:
        audit(conn, "companyctl", "runtime.verify_adapters", "", {"execute": args.execute, "source": source, "agents": [r["agent"] for r in results], "ok": ok, "scheduler": scheduler_result})
    except sqlite3.OperationalError as exc:
        audit_error = str(exc)
        if "readonly" not in audit_error.lower():
            raise
    emit({"ok": ok, "execute": args.execute, "source": source, "count": len(results), "results": results, "scheduler": scheduler_result, "audit_error": audit_error})
    return 0 if ok else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    if args.summary:
        conn = connect_readonly()
    else:
        conn = connect()
        for runtime in KNOWN_RUNTIMES:
            ensure_runtime(conn, runtime)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        RFC_DIR.mkdir(parents=True, exist_ok=True)
        APPROVAL_STATE_DIR.mkdir(parents=True, exist_ok=True)
        EMPLOYEES_DIR.mkdir(parents=True, exist_ok=True)
    try:
        counts = {
            "employees": conn.execute("SELECT COUNT(*) FROM employees").fetchone()[0],
            "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            "task_metadata": conn.execute("SELECT COUNT(*) FROM task_metadata").fetchone()[0],
            "task_relations": conn.execute("SELECT COUNT(*) FROM task_relations").fetchone()[0],
            "projects": conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0],
            "active_projects": conn.execute("SELECT COUNT(*) FROM projects WHERE status = 'active'").fetchone()[0],
            "completed_projects": conn.execute("SELECT COUNT(*) FROM projects WHERE status = 'completed'").fetchone()[0],
            "project_acceptances": conn.execute("SELECT COUNT(*) FROM project_acceptances").fetchone()[0],
            "claimed_tasks": conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'claimed'").fetchone()[0],
            "locks": conn.execute("SELECT COUNT(*) FROM locks").fetchone()[0],
            "stale_locks": sum(1 for row in conn.execute("SELECT lease_until FROM locks").fetchall() if parse_time(row["lease_until"]) <= datetime.now(timezone.utc).astimezone()),
            "conversations": conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
            "conversation_messages": conn.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0],
            "messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            "heartbeats": conn.execute("SELECT COUNT(*) FROM heartbeats").fetchone()[0],
            "runtimes": conn.execute("SELECT COUNT(*) FROM employee_runtimes").fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM company_events").fetchone()[0],
            "pending_events": conn.execute("SELECT COUNT(*) FROM company_events WHERE processed_at = ''").fetchone()[0],
            "approvals": conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0],
            "pending_approvals": conn.execute("SELECT COUNT(*) FROM approvals WHERE status = 'pending'").fetchone()[0],
            "rfcs": conn.execute("SELECT COUNT(*) FROM rfcs").fetchone()[0],
            "pending_rfcs": conn.execute("SELECT COUNT(*) FROM rfcs WHERE status = 'pending'").fetchone()[0],
            "hook_action_runs": conn.execute("SELECT COUNT(*) FROM hook_action_runs").fetchone()[0],
            "adapter_runs": conn.execute("SELECT COUNT(*) FROM adapter_runs").fetchone()[0],
            "failed_adapter_runs": conn.execute("SELECT COUNT(*) FROM adapter_runs WHERE ok = 0 AND acknowledged_at = ''").fetchone()[0],
        }
        heartbeat_cutoff = datetime.now(timezone.utc).astimezone() - timedelta(minutes=15)
        active_ai_employee_filter = """
            e.status = 'active'
            AND COALESCE(e.runtime, '') != 'human'
            AND COALESCE(e.role, '') NOT IN ('human-owner', 'owner')
        """
        missing_heartbeats = rows(
            conn,
            f"""
            SELECT e.id, e.runtime
            FROM employees e
            LEFT JOIN heartbeats h ON h.agent_id = e.id
            WHERE {active_ai_employee_filter} AND h.agent_id IS NULL
            ORDER BY e.id
            """,
        )
        # Only employees with an ENABLED daemon worker are EXPECTED to be alive; an idle
        # business agent with no worker that simply isn't heartbeating is "idle", not a fault.
        worker_agents = enabled_worker_agents()
        stale_heartbeats = []
        idle_employees = []
        for row in conn.execute(
            f"""
            SELECT e.id, e.runtime, h.last_seen_at
            FROM employees e
            JOIN heartbeats h ON h.agent_id = e.id
            WHERE {active_ai_employee_filter}
            ORDER BY e.id
            """
        ).fetchall():
            if parse_time(row["last_seen_at"]) < heartbeat_cutoff:
                if row["id"] in worker_agents:
                    stale_heartbeats.append(dict(row))
                else:
                    idle_employees.append(dict(row))
        pending = {
            "events": rows(conn, "SELECT id, event_type, source_agent, task_id, created_at FROM company_events WHERE processed_at = '' ORDER BY created_at ASC LIMIT 20"),
            "approvals": rows(conn, "SELECT id, source_agent, action, status, updated_at FROM approvals WHERE status = 'pending' ORDER BY updated_at ASC LIMIT 20"),
            "rfcs": rows(conn, "SELECT id, author_agent, status, updated_at FROM rfcs WHERE status = 'pending' ORDER BY updated_at ASC LIMIT 20"),
        }
        claimed_tasks = rows(conn, "SELECT id, target_agent, claimed_by, updated_at FROM tasks WHERE status = 'claimed' ORDER BY updated_at ASC LIMIT 20")
        failed_adapter_runs = rows(
            conn,
            """
            SELECT id, agent_id, task_id, command, processed, created_at
            FROM adapter_runs
            WHERE ok = 0 AND acknowledged_at = ''
            ORDER BY created_at DESC
            LIMIT 20
            """,
        )
        capability_issues = employee_capability_issues(conn)
        evidence_issues = task_evidence_issues(conn)
        stale_locks = []
        for lock in conn.execute("SELECT * FROM locks ORDER BY updated_at ASC").fetchall():
            if parse_time(lock["lease_until"]) <= datetime.now(timezone.utc).astimezone():
                stale_locks.append(dict(lock))
        daemon = daemon_health()
        launchd = launchd_health()
        openclaw_guard = openclaw_guard_health(conn)
        # DB corruption check is the bedrock of "装完先自检能跑通": a corrupt company.sqlite
        # silently rots everything. integrity_check is too heavy for the per-tick --summary
        # alert path, so it only runs in full doctor mode.
        db_integrity = None if args.summary else database_integrity(conn)
        # 三层分级,让"内核异常"只在内核真的坏了时才红:
        #   issues    = 基础设施故障(守护死、配置坏、锁死)→ 内核异常(红, ok=False)
        #   attention = 任务级问题(某个任务失败/缺证据)→ 需你处理,但内核没坏;另有 Stuck/回报面板呈现
        #   warnings  = 短暂积压(守护忙导致心跳/事件暂积)→ 黄"忙"
        # 单个任务失败不等于内核坏 —— 否则总有某个任务出问题,徽章永远红。
        issues = []
        attention = []
        warnings = []
        if not daemon["ok"]:
            issues.append(daemon["reason"] or "daemon_unhealthy")
        if args.strict_launchd and not launchd["installed"]:
            issues.append("launchd_not_installed")
        if args.strict_launchd and launchd["installed"] and not launchd["matches_template"]:
            issues.append("launchd_template_mismatch")
        if args.strict_openclaw and not openclaw_guard["ok"]:
            issues.extend(openclaw_guard["issues"])
        if missing_heartbeats:
            issues.append("missing_heartbeats")
        if stale_heartbeats:
            warnings.append("stale_heartbeats")  # quiet/busy agent, not a kernel fault; offline-report handles real outages
        # pending_events 宽限期:刚产生、正在被守护进程 scheduler 清理的事件不算异常;
        # 滞留超过 10 分钟的也只算 warning —— 守护忙于长任务时事件会暂时积压,真正卡死另由其它信号体现。
        event_grace_cutoff = datetime.now(timezone.utc).astimezone() - timedelta(minutes=10)
        aged_pending_events = [e for e in pending["events"] if parse_time(e["created_at"]) <= event_grace_cutoff]
        if aged_pending_events:
            warnings.append("pending_events")
        # 任务级问题:某个 worker 跑挂了 / 某个任务缺证据。内核本身没坏,任务已在 Stuck/完成回报面板可见。
        # 不该让"内核"徽章因为单个任务出问题就发红 —— 归入 attention(待处理),不翻 ok。
        if failed_adapter_runs:
            attention.append("adapter_failures")
        if evidence_issues:
            attention.append("task_evidence_issues")
        # 待审批 / 待 RFC 是正常的"待办队列",不是内核故障:控制台已有独立的"待审批"提示,不计入 issues。
        if capability_issues:
            issues.append("employee_capability_issues")  # 员工配置坏 = 基础设施问题
        if stale_locks:
            issues.append("stale_locks")  # 锁死会卡住调度 = 基础设施问题
        if db_integrity and not db_integrity["ok"]:
            issues.append("db_integrity")  # 数据库损坏 = 最底层基础设施故障
        health = {
            "ok": not issues,
            "issues": issues,
            "db_integrity": db_integrity,
            "attention": attention,
            "attention_count": len(failed_adapter_runs) + len(evidence_issues),
            "warnings": warnings,
            "heartbeat_stale_minutes": 15,
            "missing_heartbeats": missing_heartbeats,
            "stale_heartbeats": stale_heartbeats,
            "idle_employees": idle_employees,
            "pending": pending,
            "claimed_tasks": claimed_tasks,
            "failed_adapter_runs": failed_adapter_runs,
            "capability_issues": capability_issues,
            "evidence_issues": evidence_issues,
            "stale_locks": stale_locks,
            "daemon": daemon,
            "launchd": launchd,
            "openclaw_guard": openclaw_guard,
        }
        if args.summary:
            emit(
                {
                    "ok": health["ok"],
                    "issues": issues,
                    "attention": attention,
                    "attention_count": len(failed_adapter_runs) + len(evidence_issues),
                    "warnings": warnings,
                    "counts": {
                        "employees": counts["employees"],
                        "active_projects": counts["active_projects"],
                        "claimed_tasks": counts["claimed_tasks"],
                        "pending_events": counts["pending_events"],
                        "pending_approvals": counts["pending_approvals"],
                        "pending_rfcs": counts["pending_rfcs"],
                        "heartbeats": counts["heartbeats"],
                        "adapter_runs": counts["adapter_runs"],
                        "failed_adapter_runs": counts["failed_adapter_runs"],
                        "capability_issues": len(capability_issues),
                        "task_evidence_issues": len(evidence_issues),
                    },
                    "heartbeat": {
                        "stale_minutes": health["heartbeat_stale_minutes"],
                        "missing": len(missing_heartbeats),
                        "stale": len(stale_heartbeats),
                        "missing_agents": [row["id"] for row in missing_heartbeats],
                        "stale_agents": [row["id"] for row in stale_heartbeats],
                    },
                    "adapters": {
                        "failed_unacknowledged": len(failed_adapter_runs),
                        "failed_run_ids": [row["id"] for row in failed_adapter_runs],
                    },
                    "capabilities": {
                        "issues": len(capability_issues),
                        "agents": sorted({row["agent"] for row in capability_issues}),
                    },
                    "evidence": {
                        "issues": len(evidence_issues),
                        "tasks": [row["task_id"] for row in evidence_issues[:20]],
                    },
                    "daemon": daemon,
                    "launchd": launchd,
                    "openclaw_guard": openclaw_guard,
                }
            )
            return 0 if health["ok"] else 1
        emit({"ok": health["ok"], "root": str(ROOT), "db": str(DB_PATH), "counts": counts, "health": health})
        return 0 if health["ok"] else 1
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="companyctl", description="Company Kernel command interface")
    sub = parser.add_subparsers(dest="cmd", required=True)

    openclaw_cmd = sub.add_parser("openclaw")
    openclaw_sub = openclaw_cmd.add_subparsers(dest="openclaw_cmd", required=True)
    openclaw_native = openclaw_sub.add_parser("native-status")
    openclaw_native.add_argument("--summary", action="store_true")
    openclaw_native.set_defaults(func=cmd_openclaw_native_status)
    openclaw_dispatch = openclaw_sub.add_parser("dispatch-plan")
    openclaw_dispatch.add_argument("--source", required=True)
    openclaw_dispatch.add_argument("--target", required=True)
    openclaw_dispatch.add_argument("--type", required=True)
    openclaw_dispatch.add_argument("--priority", default="P2")
    openclaw_dispatch.add_argument("--goal", required=True)
    openclaw_dispatch.add_argument("--next-command", required=True)
    openclaw_dispatch.add_argument("--expected-evidence", required=True)
    openclaw_dispatch.add_argument("--rollback", required=True)
    openclaw_dispatch.add_argument("--task-id", default="")
    openclaw_dispatch.set_defaults(func=cmd_openclaw_dispatch_plan)
    openclaw_execute = openclaw_sub.add_parser("dispatch-execute")
    openclaw_execute.add_argument("--source", required=True)
    openclaw_execute.add_argument("--target", required=True)
    openclaw_execute.add_argument("--type", required=True)
    openclaw_execute.add_argument("--priority", default="P2")
    openclaw_execute.add_argument("--goal", required=True)
    openclaw_execute.add_argument("--next-command", required=True)
    openclaw_execute.add_argument("--expected-evidence", required=True)
    openclaw_execute.add_argument("--rollback", required=True)
    openclaw_execute.add_argument("--approval-id", default="")
    openclaw_execute.add_argument("--task-id", default="")
    openclaw_execute.set_defaults(func=cmd_openclaw_dispatch_execute)
    openclaw_import = openclaw_sub.add_parser("import-results")
    openclaw_import.add_argument("--limit", type=int, default=50)
    openclaw_import.add_argument("--agent", default="")
    openclaw_import.set_defaults(func=cmd_openclaw_import_results)

    emp = sub.add_parser("employee")
    emp_sub = emp.add_subparsers(dest="employee_cmd", required=True)
    emp_create = emp_sub.add_parser("create")
    emp_create.add_argument("--id", required=True)
    emp_create.add_argument("--name", required=True)
    emp_create.add_argument("--role", required=True)
    emp_create.add_argument("--runtime", required=True)
    emp_create.add_argument("--workspace", required=True)
    emp_create.add_argument("--dry-run", action="store_true")
    emp_create.set_defaults(func=cmd_employee_create)
    emp_list = emp_sub.add_parser("list")
    emp_list.set_defaults(func=cmd_employee_list)
    emp_show = emp_sub.add_parser("show")
    emp_show.add_argument("employee", nargs="?")
    emp_show.add_argument("--id", default="")
    emp_show.set_defaults(func=cmd_employee_show)
    emp_update = emp_sub.add_parser("update")
    emp_update.add_argument("--id", required=True)
    emp_update.add_argument("--name", default="")
    emp_update.add_argument("--role", default="")
    emp_update.add_argument("--runtime", default="")
    emp_update.add_argument("--workspace", default="")
    emp_update.add_argument("--status", choices=["active", "candidate", "archived"], default="")
    emp_update.add_argument("--default-user-reply-channel", default="")
    emp_update.add_argument("--default-user-reply-account", default="")
    emp_update.add_argument("--default-user-reply-to", default="")
    emp_update.add_argument("--default-user-reply-deliver", action=argparse.BooleanOptionalAction, default=None)
    emp_update.add_argument("--dry-run", action="store_true")
    emp_update.set_defaults(func=cmd_employee_update)
    emp_verify_direct = emp_sub.add_parser("verify-direct")
    emp_verify_direct.add_argument("--id", required=True)
    emp_verify_direct.add_argument("--from", dest="source", default="main")
    emp_verify_direct.add_argument("--rounds", type=int, default=3, choices=[2, 3, 4])
    emp_verify_direct.add_argument("--timeout", type=int, default=120)
    emp_verify_direct.add_argument("--activate", action="store_true")
    emp_verify_direct.add_argument("--continue-on-failure", action="store_true")
    emp_verify_direct.set_defaults(func=cmd_employee_verify_direct)
    emp_set_unavailable = emp_sub.add_parser("set-unavailable")
    emp_set_unavailable.add_argument("--id", required=True)
    emp_set_unavailable.add_argument("--reason", required=True)
    emp_set_unavailable.set_defaults(func=cmd_employee_set_unavailable)
    emp_verify_runtime = emp_sub.add_parser("verify-runtime")
    emp_verify_runtime.add_argument("--id", required=True)
    emp_verify_runtime.add_argument("--from", dest="source", default="main")
    emp_verify_runtime.add_argument("--timeout", type=int, default=180)
    emp_verify_runtime.add_argument("--activate", action="store_true")
    emp_verify_runtime.set_defaults(func=cmd_employee_verify_runtime)
    emp_recover = emp_sub.add_parser("recover", help="re-verify auto-downgraded employees and reactivate the ones that respond")
    emp_recover.add_argument("--from", dest="source", default="main")
    emp_recover.add_argument("--timeout", type=int, default=60)
    emp_recover.add_argument("--max", type=int, default=3, help="max employees to re-verify per run")
    emp_recover.set_defaults(func=cmd_employee_recover)
    emp_capabilities = emp_sub.add_parser("capabilities")
    emp_capabilities.add_argument("--id", required=True)
    emp_capabilities.add_argument("--set-skills", default="", help="comma-separated replacement list")
    emp_capabilities.add_argument("--add-skill", action="append", default=[])
    emp_capabilities.add_argument("--set-tools", default="", help="comma-separated replacement list")
    emp_capabilities.add_argument("--add-tool", action="append", default=[])
    emp_capabilities.add_argument("--set-task-types", default="", help="comma-separated replacement list")
    emp_capabilities.set_defaults(func=cmd_employee_capabilities)
    emp_permissions = emp_sub.add_parser("permissions")
    emp_permissions.add_argument("--id", required=True)
    emp_permissions.add_argument("--can-submit-tasks", choices=["keep", "true", "false"], default="keep")
    emp_permissions.add_argument("--can-claim-tasks", choices=["keep", "true", "false"], default="keep")
    emp_permissions.add_argument("--can-modify-kernel", choices=["keep", "true", "false"], default="keep")
    emp_permissions.add_argument("--requires-approval-for", default="", help="comma-separated replacement list")
    emp_permissions.set_defaults(func=cmd_employee_permissions)
    emp_match = emp_sub.add_parser("match")
    emp_match.add_argument("--skills", default="", help="comma-separated required skills")
    emp_match.add_argument("--tools", default="", help="comma-separated preferred tools")
    emp_match.add_argument("--task-type", default="")
    emp_match.add_argument("--runtime", default="")
    emp_match.add_argument("--role", default="")
    emp_match.add_argument("--limit", type=int, default=10)
    emp_match.add_argument("--include-unavailable", action="store_true")
    emp_match.set_defaults(func=cmd_employee_match)
    emp_import_openclaw = emp_sub.add_parser("import-openclaw")
    emp_import_openclaw.add_argument("--config", default=str(openclaw_root() / "openclaw.json"))
    emp_import_openclaw.add_argument("--dry-run", action="store_true")
    emp_import_openclaw.set_defaults(func=cmd_employee_import_openclaw)
    emp_sync_openclaw = emp_sub.add_parser("sync-openclaw-runtime")
    emp_sync_openclaw.add_argument("--config", default=str(openclaw_root() / "openclaw.json"))
    emp_sync_openclaw.add_argument("--active-only", action="store_true", help="only sync agents declared in openclaw.json as active employees")
    emp_sync_openclaw.add_argument("--dry-run", action="store_true")
    emp_sync_openclaw.set_defaults(func=cmd_employee_sync_openclaw_runtime)
    emp_sync_openclaw_hb = emp_sub.add_parser("sync-openclaw-heartbeats")
    emp_sync_openclaw_hb.add_argument("--dry-run", action="store_true")
    emp_sync_openclaw_hb.set_defaults(func=cmd_employee_sync_openclaw_heartbeats)
    emp_onboard = emp_sub.add_parser("onboard")
    emp_onboard.add_argument("--id", required=True)
    emp_onboard.add_argument("--name", required=True)
    emp_onboard.add_argument("--role", required=True)
    emp_onboard.add_argument("--runtime", required=True)
    emp_onboard.add_argument("--workspace", required=True)
    emp_onboard.add_argument("--alias", default="")
    emp_onboard.add_argument("--skills", default="", help="comma-separated skills")
    emp_onboard.add_argument("--tools", default="", help="comma-separated tools")
    emp_onboard.add_argument("--task-types", default="", help="comma-separated preferred task types")
    emp_onboard.add_argument("--can-talk-to", default="", help="comma-separated employee ids or aliases")
    emp_onboard.add_argument("--can-assign-to", default="", help="comma-separated employee ids or aliases")
    emp_onboard.add_argument("--open-communication", action="store_true", help="allow communication with all currently registered employees")
    emp_onboard.add_argument("--channel", default="")
    emp_onboard.add_argument("--handoff-mode", default="task_or_hook")
    emp_onboard.add_argument("--default-user-reply-channel", default="")
    emp_onboard.add_argument("--default-user-reply-account", default="")
    emp_onboard.add_argument("--default-user-reply-to", default="")
    emp_onboard.add_argument("--default-user-reply-deliver", action="store_true")
    emp_onboard.add_argument("--requires-approval-for", default="payment,compensation,salary,penalty,external_send")
    emp_onboard.add_argument("--no-submit-tasks", action="store_true")
    emp_onboard.add_argument("--no-claim-tasks", action="store_true")
    emp_onboard.add_argument("--can-modify-kernel", action="store_true")
    emp_onboard.add_argument("--create-test-task", action="store_true")
    emp_onboard.add_argument("--test-source", default="openclaw-main")
    emp_onboard.add_argument("--test-task-id", default="")
    emp_onboard.add_argument("--dry-run", action="store_true")
    emp_onboard.set_defaults(func=cmd_employee_onboard)
    emp_offboard = emp_sub.add_parser("offboard")
    emp_offboard.add_argument("--id", required=True)
    emp_offboard.add_argument("--hard-delete", action="store_true", help="delete only Company Kernel-managed employee files/workspace")
    emp_offboard.add_argument("--dry-run", action="store_true")
    emp_offboard.set_defaults(func=cmd_employee_offboard)
    emp_integration = emp_sub.add_parser("install-integration", help="install company-kernel MCP + employee instructions into an agent runtime's own config (codex/claude/gemini) so it's truly on-duty")
    emp_integration.add_argument("--runtime", required=True, help="codex / claude / gemini / antigravity")
    emp_integration.add_argument("--agent-id", default="", help="employee id the agent acts as (default: the runtime name)")
    emp_integration.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    emp_integration.set_defaults(func=cmd_employee_install_integration)
    emp_ensure_owner = emp_sub.add_parser("ensure-owner", help="create the human owner employee if missing (idempotent; used by `init`)")
    emp_ensure_owner.add_argument("--owner-id", default="owner")
    emp_ensure_owner.set_defaults(func=cmd_employee_ensure_owner)
    emp_offline = emp_sub.add_parser("offline-report", help="list active employees that are offline (stale heartbeat), optionally notify")
    emp_offline.add_argument("--stale-minutes", type=int, default=10)
    emp_offline.add_argument("--dormant-minutes", type=int, default=1440, help="beyond this a stale employee is 'dormant' (logical/never-running), not an alertable drop")
    emp_offline.add_argument("--notify", action="store_true", help="send a Telegram summary of offline employees")
    emp_offline.add_argument("--dedup", action="store_true", help="for scheduled callers: only notify when the offline set changes or hourly")
    emp_offline.set_defaults(func=cmd_employee_offline_report)

    skill = sub.add_parser("skill")
    skill_sub = skill.add_subparsers(dest="skill_cmd", required=True)
    skill_list = skill_sub.add_parser("list")
    skill_list.set_defaults(func=cmd_skill_list)

    attendance = sub.add_parser("attendance")
    attendance_sub = attendance.add_subparsers(dest="attendance_cmd", required=True)
    attendance_sweep = attendance_sub.add_parser("sweep")
    attendance_sweep.add_argument("--source", default="main")
    attendance_sweep.add_argument("--agents", default="", help="comma-separated employee ids; default active employees")
    attendance_sweep.add_argument("--sweep-id", default="")
    attendance_sweep.add_argument("--include-candidates", action="store_true")
    attendance_sweep.add_argument("--stale-minutes", type=int, default=15)
    attendance_sweep.add_argument("--probe-replies", action=argparse.BooleanOptionalAction, default=True, help="ask each supported runtime to reply <agent_id> 在岗")
    attendance_sweep.add_argument("--reply-timeout", type=int, default=120)
    attendance_sweep.set_defaults(func=cmd_attendance_sweep)

    agent_matrix = sub.add_parser("agent-matrix")
    agent_matrix.add_argument("--agents", default="", help="comma-separated employee ids; default active/candidate employees")
    agent_matrix.set_defaults(func=cmd_agent_matrix)

    task = sub.add_parser("task")
    task_sub = task.add_subparsers(dest="task_cmd", required=True)
    task_submit = task_sub.add_parser("submit")
    task_submit.add_argument("--from", dest="source", required=True)
    task_submit.add_argument("--to", dest="target", required=True)
    task_submit.add_argument("--title", required=True)
    task_submit.add_argument("--description", default="")
    task_submit.add_argument("--priority", default="P2")
    task_submit.add_argument("--task-id", default="")
    task_submit.add_argument("--changed-files", default="", help="comma-separated paths the task expects to modify")
    task_submit.add_argument("--rfc", default="", help="RFC path approving protected changes")
    task_submit.add_argument("--requires-approval", default="", help="force approval action before direct submit, e.g. external_send")
    task_submit.add_argument("--approval-id", default="", help="approved approval id for high-risk direct submit")
    task_submit.add_argument("--risk", default="P1")
    task_submit.add_argument("--deliver-to", default="", help="reply delivery target for OpenClaw agents, e.g. 'line:internal' (channel:group_code) or JSON {\"channel\":\"line\",\"group_code\":\"A3\"}")
    task_submit.add_argument("--force", dest="force_submit", action="store_true", help="bypass submit guards (codex-workspace / duplicate / recently-discarded)")
    task_submit.set_defaults(func=cmd_task_submit)
    task_route = task_sub.add_parser("route")
    task_route.add_argument("--from", dest="source", required=True)
    task_route.add_argument("--title", required=True)
    task_route.add_argument("--description", default="")
    task_route.add_argument("--priority", default="P2")
    task_route.add_argument("--task-id", default="")
    task_route.add_argument("--skills", default="", help="comma-separated required skills")
    task_route.add_argument("--tools", default="", help="comma-separated preferred tools")
    task_route.add_argument("--task-type", default="")
    task_route.add_argument("--runtime", default="")
    task_route.add_argument("--role", default="")
    task_route.add_argument("--limit", type=int, default=10)
    task_route.add_argument("--include-unavailable", action="store_true")
    task_route.add_argument("--requires-approval", default="", help="force approval action before routing, e.g. external_send")
    task_route.add_argument("--approval-id", default="", help="approved approval id for high-risk route")
    task_route.add_argument("--risk", default="P1")
    task_route.add_argument("--changed-files", default="", help="comma-separated paths the task expects to modify")
    task_route.add_argument("--rfc", default="", help="RFC path approving protected changes")
    task_route.set_defaults(func=cmd_task_route)
    task_list = task_sub.add_parser("list")
    task_list.add_argument("--agent", default="")
    task_list.set_defaults(func=cmd_task_list)
    task_show = task_sub.add_parser("show")
    task_show.add_argument("--task-id", required=True)
    task_show.set_defaults(func=cmd_task_show)
    task_children = task_sub.add_parser("children")
    task_children.add_argument("--task-id", required=True)
    task_children.set_defaults(func=cmd_task_children)
    task_split = task_sub.add_parser("split")
    task_split.add_argument("--task-id", required=True)
    task_split.add_argument("--by", required=True)
    task_split.add_argument("--item", action="append", default=[], help="target|title|description|priority; repeat for multiple child tasks")
    task_split.add_argument("--plan", default="", help="JSON list or object with items for long-task decomposition")
    task_split.add_argument("--child-id-prefix", default="")
    task_split.set_defaults(func=cmd_task_split)
    task_collect = task_sub.add_parser("collect")
    task_collect.add_argument("--task-id", required=True)
    task_collect.add_argument("--agent", required=True)
    task_collect.add_argument("--summary", default="")
    task_collect.add_argument("--evidence", default="")
    task_collect.add_argument("--force", action="store_true")
    task_collect.set_defaults(func=cmd_task_collect)
    task_discuss = task_sub.add_parser("discuss")
    task_discuss.add_argument("--task-id", required=True)
    task_discuss.add_argument("--from", dest="source", default="")
    task_discuss.add_argument("--participants", default="", help="comma-separated extra participants")
    task_discuss.add_argument("--title", default="")
    task_discuss.add_argument("--body", default="")
    task_discuss.add_argument("--evidence", default="")
    task_discuss.add_argument("--conversation-id", default="")
    task_discuss.set_defaults(func=cmd_task_discuss)
    task_conversations = task_sub.add_parser("conversations")
    task_conversations.add_argument("--task-id", required=True)
    task_conversations.set_defaults(func=cmd_task_conversations)
    task_claim = task_sub.add_parser("claim")
    task_claim.add_argument("--agent", required=True)
    task_claim.add_argument("--task-id", default="")
    task_claim.add_argument("--lease-seconds", type=int, default=1800)
    task_claim.set_defaults(func=cmd_task_claim)
    task_done = task_sub.add_parser("done")
    task_done.add_argument("--agent", required=True)
    task_done.add_argument("--task-id", required=True)
    task_done.add_argument("--summary", required=True)
    task_done.add_argument("--evidence", required=True)
    task_done.set_defaults(func=cmd_task_done)
    task_auto_triage = task_sub.add_parser("auto-triage", help="auto-discard mis-dispatched tasks (e.g. codex with no 工作区:) and notify the dispatcher")
    task_auto_triage.set_defaults(func=cmd_task_auto_triage)
    task_report = task_sub.add_parser("report", help="owner-facing results feed: list completed top-level tasks, or read one task's report")
    task_report.add_argument("--task-id", default="", help="omit to list completed top-level tasks; provide to read that task's report content")
    task_report.add_argument("--limit", type=int, default=40)
    task_report.add_argument("--completed-only", action="store_true", help="exclude blocked tasks from the list")
    task_report.set_defaults(func=cmd_task_report)
    task_artifact = task_sub.add_parser("artifact")
    task_artifact_sub = task_artifact.add_subparsers(dest="artifact_cmd", required=True)
    task_artifact_register = task_artifact_sub.add_parser("register")
    task_artifact_register.add_argument("--task-id", required=True)
    task_artifact_register.add_argument("--employee", required=True)
    task_artifact_register.add_argument("--path", required=True)
    task_artifact_register.add_argument("--type", required=True)
    task_artifact_register.add_argument("--name", default="")
    task_artifact_register.add_argument("--stage", choices=["draft", "intermediate", "final"], default="intermediate")
    task_artifact_register.add_argument("--summary", default="")
    task_artifact_register.add_argument("--input", action="store_true")
    task_artifact_register.add_argument("--final", action="store_true")
    task_artifact_register.add_argument("--metadata", default="")
    task_artifact_register.set_defaults(func=cmd_task_artifact_register)
    task_artifact_scan = task_artifact_sub.add_parser("scan")
    task_artifact_scan.add_argument("--task-id", required=True)
    task_artifact_scan.add_argument("--employee", required=True)
    task_artifact_scan.add_argument("--dir", required=True)
    task_artifact_scan.add_argument("--type", required=True)
    task_artifact_scan.add_argument("--stage", choices=["draft", "intermediate", "final"], default="intermediate")
    task_artifact_scan.add_argument("--summary", default="")
    task_artifact_scan.add_argument("--pattern", default="*")
    task_artifact_scan.set_defaults(func=cmd_task_artifact_scan)
    task_artifact_approve = task_artifact_sub.add_parser("approve")
    task_artifact_approve.add_argument("--artifact-id", required=True)
    task_artifact_approve.add_argument("--by", required=True)
    task_artifact_approve.add_argument("--status", choices=["approved", "rejected"], default="approved")
    task_artifact_approve.add_argument("--reason", default="")
    task_artifact_approve.add_argument("--summary", default="")
    task_artifact_approve.set_defaults(func=cmd_task_artifact_approve)
    task_artifact_use = task_artifact_sub.add_parser("use")
    task_artifact_use.add_argument("--task-id", required=True)
    task_artifact_use.add_argument("--artifact-id", required=True)
    task_artifact_use.add_argument("--employee", required=True)
    task_artifact_use.add_argument("--purpose", default="")
    task_artifact_use.add_argument("--summary", default="")
    task_artifact_use.set_defaults(func=cmd_task_artifact_use)
    task_evidence = task_sub.add_parser("evidence")
    task_evidence_sub = task_evidence.add_subparsers(dest="evidence_cmd", required=True)
    task_evidence_promote = task_evidence_sub.add_parser("promote")
    task_evidence_promote.add_argument("--artifact-id", required=True)
    task_evidence_promote.add_argument("--by", default="")
    task_evidence_promote.add_argument("--employee", default="")
    task_evidence_promote.add_argument("--summary", default="")
    task_evidence_promote.add_argument("--type", default="")
    task_evidence_promote.set_defaults(func=cmd_task_evidence_promote)
    task_evidence_accept = task_evidence_sub.add_parser("accept")
    task_evidence_accept.add_argument("--evidence-id", required=True)
    task_evidence_accept.add_argument("--by", required=True)
    task_evidence_accept.add_argument("--summary", default="")
    task_evidence_accept.set_defaults(func=cmd_task_evidence_accept)
    task_evidence_reject = task_evidence_sub.add_parser("reject")
    task_evidence_reject.add_argument("--evidence-id", required=True)
    task_evidence_reject.add_argument("--by", required=True)
    task_evidence_reject.add_argument("--summary", default="")
    task_evidence_reject.add_argument("--reason", default="")
    task_evidence_reject.set_defaults(func=cmd_task_evidence_reject)
    task_handoff = task_sub.add_parser("handoff")
    task_handoff_sub = task_handoff.add_subparsers(dest="handoff_cmd", required=True)
    task_handoff_create = task_handoff_sub.add_parser("create")
    task_handoff_create.add_argument("--from-task", required=True)
    task_handoff_create.add_argument("--to-task", required=True)
    task_handoff_create.add_argument("--from-employee", required=True)
    task_handoff_create.add_argument("--to-employee", default="")
    task_handoff_create.add_argument("--summary", required=True)
    task_handoff_create.add_argument("--artifact", action="append", default=[])
    task_handoff_create.add_argument("--known-issues", default="")
    task_handoff_create.add_argument("--next-steps", default="")
    task_handoff_create.add_argument("--required-actions", default="")
    task_handoff_create.add_argument("--acceptance-notes", default="")
    task_handoff_create.set_defaults(func=cmd_task_handoff_create)
    task_handoff_accept = task_handoff_sub.add_parser("accept")
    task_handoff_accept.add_argument("--handoff-id", required=True)
    task_handoff_accept.add_argument("--by", required=True)
    task_handoff_accept.add_argument("--reason", default="")
    task_handoff_accept.set_defaults(func=cmd_task_handoff_status, handoff_status="accepted")
    task_handoff_reject = task_handoff_sub.add_parser("reject")
    task_handoff_reject.add_argument("--handoff-id", required=True)
    task_handoff_reject.add_argument("--by", required=True)
    task_handoff_reject.add_argument("--reason", required=True)
    task_handoff_reject.set_defaults(func=cmd_task_handoff_status, handoff_status="rejected")
    task_context = task_sub.add_parser("context")
    task_context.add_argument("--task-id", required=True)
    task_context.add_argument("--employee", default="")
    task_context.set_defaults(func=cmd_task_context)
    task_run = task_sub.add_parser("run")
    task_run.add_argument("--task-id", required=True)
    task_run.add_argument("--agent", required=True)
    task_run.add_argument("--by", required=True)
    task_run.add_argument("--adapter-type", default="managed")
    task_run.add_argument("--pid", default="")
    task_run.add_argument("--session-key", default="")
    task_run.add_argument("--max-runtime-seconds", type=int, default=DEFAULT_RUNTIME_POLICY["max_runtime_seconds"])
    task_run.add_argument("--heartbeat-interval-seconds", type=int, default=DEFAULT_RUNTIME_POLICY["heartbeat_interval_seconds"])
    task_run.add_argument("--progress-interval-seconds", type=int, default=DEFAULT_RUNTIME_POLICY["progress_interval_seconds"])
    task_run.add_argument("--stale-after-seconds", type=int, default=DEFAULT_RUNTIME_POLICY["stale_after_seconds"])
    task_run.add_argument("--supervisor-check-interval-seconds", type=int, default=DEFAULT_RUNTIME_POLICY["supervisor_check_interval_seconds"])
    task_run.add_argument("--max-corrections", type=int, default=DEFAULT_RUNTIME_POLICY["max_corrections"])
    task_run.add_argument("--max-retries", type=int, default=DEFAULT_RUNTIME_POLICY["max_retries"])
    task_run.set_defaults(func=cmd_task_run)
    task_attempts = task_sub.add_parser("attempts")
    task_attempts.add_argument("--task-id", required=True)
    task_attempts.set_defaults(func=cmd_task_attempts)
    task_progress = task_sub.add_parser("progress")
    task_progress.add_argument("--task-id", required=True)
    task_progress.add_argument("--agent", required=True)
    task_progress.add_argument("--attempt-id", default="")
    task_progress.add_argument("--state", default="in_progress")
    task_progress.add_argument("--message", required=True)
    task_progress.add_argument("--progress", type=int)
    task_progress.add_argument("--payload", default="")
    task_progress.add_argument("--at", default="")
    task_progress.set_defaults(func=cmd_task_progress)
    task_probe = task_sub.add_parser("probe")
    task_probe.add_argument("--task-id", required=True)
    task_probe.add_argument("--by", required=True)
    task_probe.add_argument("--attempt-id", default="")
    task_probe.add_argument("--message", required=True)
    task_probe.add_argument("--reason", default="progress_probe")
    task_probe.set_defaults(func=cmd_task_probe)
    task_correct = task_sub.add_parser("correct")
    task_correct.add_argument("--task-id", required=True)
    task_correct.add_argument("--attempt-id", required=True)
    task_correct.add_argument("--by", required=True)
    task_correct.add_argument("--message", required=True)
    task_correct.add_argument("--ack", action="store_true")
    task_correct.set_defaults(func=cmd_task_correct)
    task_cancel = task_sub.add_parser("cancel")
    task_cancel.add_argument("--task-id", required=True)
    task_cancel.add_argument("--attempt-id", required=True)
    task_cancel.add_argument("--by", required=True)
    task_cancel.add_argument("--reason", required=True)
    task_cancel.set_defaults(func=cmd_task_cancel)
    task_attempt = task_sub.add_parser("attempt")
    task_attempt_sub = task_attempt.add_subparsers(dest="attempt_cmd", required=True)
    task_attempt_start = task_attempt_sub.add_parser("start")
    task_attempt_start.add_argument("--task-id", required=True)
    task_attempt_start.add_argument("--employee", required=True)
    task_attempt_start.add_argument("--adapter-type", default="local")
    task_attempt_start.add_argument("--metadata", default="")
    task_attempt_start.set_defaults(func=cmd_task_attempt_start)
    task_attempt_finish = task_attempt_sub.add_parser("finish")
    task_attempt_finish.add_argument("--attempt-id", required=True)
    task_attempt_finish.add_argument("--status", choices=["success", "failed", "cancelled", "stale"], required=True)
    task_attempt_finish.add_argument("--error", default="")
    task_attempt_finish.set_defaults(func=cmd_task_attempt_finish)
    task_block = task_sub.add_parser("block")
    task_block.add_argument("--agent", required=True)
    task_block.add_argument("--task-id", required=True)
    task_block.add_argument("--blocker", required=True)
    task_block.set_defaults(func=cmd_task_block)
    task_retry = task_sub.add_parser("retry")
    task_retry.add_argument("--task-id", required=True)
    task_retry.add_argument("--by", required=True)
    task_retry.add_argument("--reason", required=True)
    task_retry.set_defaults(func=cmd_task_retry)
    task_reopen = task_sub.add_parser("reopen")
    task_reopen.add_argument("--task-id", required=True)
    task_reopen.add_argument("--by", required=True)
    task_reopen.add_argument("--reason", required=True)
    task_reopen.add_argument("--status", choices=["submitted", "claimed"], default="submitted")
    task_reopen.add_argument("--description", default="", help="corrected/augmented task brief (e.g. add absolute repo path) to fix the block before re-queueing")
    task_reopen.set_defaults(func=cmd_task_reopen)
    task_discard = task_sub.add_parser("discard")
    task_discard.add_argument("--task-id", required=True)
    task_discard.add_argument("--by", required=True)
    task_discard.add_argument("--reason", default="owner discarded")
    task_discard.set_defaults(func=cmd_task_discard)
    task_reassign = task_sub.add_parser("reassign")
    task_reassign.add_argument("--task-id", required=True)
    task_reassign.add_argument("--by", required=True)
    task_reassign.add_argument("--to", required=True)
    task_reassign.add_argument("--reason", required=True)
    task_reassign.set_defaults(func=cmd_task_reassign)

    project = sub.add_parser("project")
    project_sub = project.add_subparsers(dest="project_cmd", required=True)
    project_create = project_sub.add_parser("create")
    project_create.add_argument("--project-id", default="")
    project_create.add_argument("--title", required=True)
    project_create.add_argument("--goal", default="")
    project_create.add_argument("--owner", required=True)
    project_create.add_argument("--status", default="active")
    project_create.add_argument("--acceptance", default="", help="semicolon-separated acceptance criteria")
    project_create.set_defaults(func=cmd_project_create)
    project_list = project_sub.add_parser("list")
    project_list.add_argument("--status", default="active", choices=["active", "paused", "completed", "blocked", "all"])
    project_list.set_defaults(func=cmd_project_list)
    project_show = project_sub.add_parser("show")
    project_show.add_argument("--project-id", required=True)
    project_show.set_defaults(func=cmd_project_show)
    project_link_task = project_sub.add_parser("link-task")
    project_link_task.add_argument("--project-id", required=True)
    project_link_task.add_argument("--task-id", required=True)
    project_link_task.set_defaults(func=cmd_project_link_task)
    project_plan_add = project_sub.add_parser("plan-add")
    project_plan_add.add_argument("--project-id", required=True)
    project_plan_add.add_argument("--title", required=True)
    project_plan_add.add_argument("--status", default="planned", choices=["planned", "in_progress", "done", "completed", "blocked", "cancelled"])
    project_plan_add.add_argument("--owner", default="")
    project_plan_add.add_argument("--due-at", default="")
    project_plan_add.add_argument("--task-id", default="")
    project_plan_add.add_argument("--plan-id", default="")
    project_plan_add.set_defaults(func=cmd_project_plan_add)
    project_plan_list = project_sub.add_parser("plan-list")
    project_plan_list.add_argument("--project-id", required=True)
    project_plan_list.set_defaults(func=cmd_project_plan_list)
    project_plan_status = project_sub.add_parser("plan-status")
    project_plan_status.add_argument("--project-id", required=True)
    project_plan_status.add_argument("--plan-id", required=True)
    project_plan_status.add_argument("--status", required=True, choices=["planned", "in_progress", "done", "completed", "blocked", "cancelled"])
    project_plan_status.set_defaults(func=cmd_project_plan_status)
    project_status = project_sub.add_parser("status")
    project_status.add_argument("--project-id", required=True)
    project_status.add_argument("--status", required=True)
    project_status.set_defaults(func=cmd_project_status)
    project_review = project_sub.add_parser("review")
    project_review.add_argument("--project-id", required=True)
    project_review.set_defaults(func=cmd_project_review)
    project_accept = project_sub.add_parser("accept")
    project_accept.add_argument("--project-id", required=True)
    project_accept.add_argument("--by", required=True)
    project_accept.add_argument("--summary", required=True)
    project_accept.add_argument("--force", action="store_true")
    project_accept.set_defaults(func=cmd_project_accept)


    external = sub.add_parser("external")
    external_sub = external.add_subparsers(dest="external_cmd", required=True)
    external_threads = external_sub.add_parser("threads")
    external_threads.add_argument("--platform", default="")
    external_threads.add_argument("--owner-agent", default="")
    external_threads.add_argument("--limit", type=int, default=50)
    external_threads.set_defaults(func=cmd_external_threads)
    external_show = external_sub.add_parser("show")
    external_show.add_argument("--thread-id", required=True)
    external_show.set_defaults(func=cmd_external_show)
    external_import = external_sub.add_parser("import")
    external_import.add_argument("--payload", default="")
    external_import.add_argument("--file", default="")
    external_import.set_defaults(func=cmd_external_import)

    inbox = sub.add_parser("inbox", help="maintenance for employee inbox notification files (write-only; not the work queue)")
    inbox_sub = inbox.add_subparsers(dest="inbox_cmd", required=True)
    inbox_prune = inbox_sub.add_parser("prune", help="trim old notification files so inboxes don't balloon into noise")
    inbox_prune.add_argument("--agent", default="all", help="'all' (default) or an employee id")
    inbox_prune.add_argument("--keep", type=int, default=80, help="newest N files to keep per inbox")
    inbox_prune.set_defaults(func=cmd_inbox_prune)
    message = sub.add_parser("message")
    message_sub = message.add_subparsers(dest="message_cmd", required=True)
    message_send = message_sub.add_parser("send")
    message_send.add_argument("--from", dest="source", required=True)
    message_send.add_argument("--to", dest="target", required=True)
    message_send.add_argument("--body", required=True)
    message_send.add_argument("--message-id", default="")
    message_send.set_defaults(func=cmd_message_send)
    message_channel = message_sub.add_parser("channel-send", help="push a pure text message to an external channel (LINE/Telegram customer group)")
    message_channel.add_argument("--agent", required=True, help="owning agent (its channel token is used), e.g. nestcar")
    message_channel.add_argument("--channel", default="line")
    message_channel.add_argument("--group-code", default="", help="group code from the agent's channel_target_registry, e.g. A3")
    message_channel.add_argument("--target-id", default="", help="explicit channel target id (overrides group-code)")
    message_channel.add_argument("--body", required=True)
    message_channel.add_argument("--by", default="owner")
    message_channel.set_defaults(func=cmd_message_channel_send)
    message_direct = message_sub.add_parser("direct")
    message_direct.add_argument("--from", dest="source", required=True)
    message_direct.add_argument("--to", dest="target", required=True)
    message_direct.add_argument("--body", required=True)
    message_direct.add_argument("--message-id", default="")
    message_direct.add_argument("--session-key", default="")
    message_direct.add_argument("--timeout", type=int, default=120)
    message_direct.add_argument("--deliver", action="store_true")
    message_direct.add_argument("--reply-channel", default="")
    message_direct.add_argument("--reply-to", default="")
    message_direct.add_argument("--reply-account", default="")
    message_direct.set_defaults(func=cmd_message_direct)
    message_list = message_sub.add_parser("list")
    message_list.add_argument("--agent", required=True)
    message_list.set_defaults(func=cmd_message_list)

    followup = sub.add_parser("followup")
    followup_sub = followup.add_subparsers(dest="followup_cmd", required=True)
    followup_request = followup_sub.add_parser("request")
    followup_request.add_argument("--from", dest="source", required=True)
    followup_request.add_argument("--to", dest="target", required=True)
    followup_request.add_argument("--question", required=True)
    followup_request.add_argument("--context", default="")
    followup_request.add_argument("--followup-id", default="")
    followup_request.add_argument("--message-id", default="")
    followup_request.add_argument("--session-key", default="")
    followup_request.add_argument("--timeout", type=int, default=120)
    followup_request.add_argument("--deliver", action="store_true")
    followup_request.add_argument("--reply-channel", default="")
    followup_request.add_argument("--reply-account", default="")
    followup_request.add_argument("--reply-to", default="")
    followup_request.set_defaults(func=cmd_followup_request)
    followup_reply = followup_sub.add_parser("reply")
    followup_reply.add_argument("--followup-id", required=True)
    followup_reply.add_argument("--by", required=True)
    followup_reply.add_argument("--answer", required=True)
    followup_reply.add_argument("--message-id", default="")
    followup_reply.add_argument("--timeout", type=int, default=120)
    followup_reply.set_defaults(func=cmd_followup_reply)
    followup_show = followup_sub.add_parser("show")
    followup_show.add_argument("--followup-id", required=True)
    followup_show.set_defaults(func=cmd_followup_show)
    followup_list = followup_sub.add_parser("list")
    followup_list.add_argument("--status", choices=["pending", "answered", "cancelled", "all"], default="all")
    followup_list.set_defaults(func=cmd_followup_list)

    conversation = sub.add_parser("conversation")
    conversation_sub = conversation.add_subparsers(dest="conversation_cmd", required=True)
    conversation_start = conversation_sub.add_parser("start")
    conversation_start.add_argument("--from", dest="source", required=True)
    conversation_start.add_argument("--participants", required=True, help="comma-separated employee ids")
    conversation_start.add_argument("--title", required=True)
    conversation_start.add_argument("--body", required=True)
    conversation_start.add_argument("--evidence", default="")
    conversation_start.add_argument("--conversation-id", default="")
    conversation_start.add_argument("--project", default="", help="tie this meeting to a project memory bank: read its digest + store the conclusion back")
    conversation_start.set_defaults(func=cmd_conversation_start)
    conversation_reply = conversation_sub.add_parser("reply")
    conversation_reply.add_argument("--from", dest="source", required=True)
    conversation_reply.add_argument("--conversation-id", required=True)
    conversation_reply.add_argument("--body", required=True)
    conversation_reply.add_argument("--evidence", default="")
    conversation_reply.add_argument("--message-id", default="")
    conversation_reply.set_defaults(func=cmd_conversation_reply)
    conversation_join = conversation_sub.add_parser("join")
    conversation_join.add_argument("--agent", default="owner")
    conversation_join.add_argument("--conversation-id", required=True)
    conversation_join.set_defaults(func=cmd_conversation_join)
    conversation_list = conversation_sub.add_parser("list")
    conversation_list.add_argument("--agent", required=True)
    conversation_list.set_defaults(func=cmd_conversation_list)
    conversation_show = conversation_sub.add_parser("show")
    conversation_show.add_argument("--conversation-id", required=True)
    conversation_show.set_defaults(func=cmd_conversation_show)
    conversation_run = conversation_sub.add_parser("run", help="run an autonomous multi-employee meeting/discussion that converges to minutes/a plan")
    conversation_run.add_argument("--conversation-id", required=True)
    conversation_run.add_argument("--mode", choices=sorted(CONVERSATION_MODES), default="meeting", help="meeting=sync goals/norms→minutes, discuss=debate→plan, standup=progress/blockers")
    conversation_run.add_argument("--rounds", type=int, default=2, help="speaking rounds before the chair synthesizes")
    conversation_run.add_argument("--timeout", type=int, default=180, help="per-turn runtime timeout seconds")
    conversation_run.add_argument("--synthesizer", default="", help="chair employee who opens summary/minutes (default: hermes if present)")
    conversation_run.add_argument("--no-gate", dest="gate_capable", action="store_false", help="skip the participation allowlist gate (admit any active runtime-capable participant)")
    conversation_run.add_argument("--project", default="", help="tie/override this meeting's project memory bank (read digest + store conclusion)")
    conversation_run.set_defaults(func=cmd_conversation_run, gate_capable=True)
    conversation_probe = conversation_sub.add_parser("probe", help="test which employees can genuinely join a meeting and persist the allowlist")
    conversation_probe.add_argument("--participants", default="active", help="'active' (default), 'all', or comma-separated employee ids")
    conversation_probe.add_argument("--timeout", type=int, default=90, help="per-probe runtime timeout seconds")
    conversation_probe.add_argument("--no-persist", action="store_true", help="do not write results to the meeting-capable allowlist")
    conversation_probe.set_defaults(func=cmd_conversation_probe)

    watchdog_p = sub.add_parser("watchdog", help="fault-tolerance watchdog")
    watchdog_sub = watchdog_p.add_subparsers(dest="watchdog_cmd", required=True)
    reap_stuck = watchdog_sub.add_parser("reap-stuck", help="force-fail attempts running past their runtime cap or whose worker process died → task blocked + dispatcher notified (so stuck work lands in the failure list instead of hanging)")
    reap_stuck.add_argument("--by", default="openclaw-main", help="actor recorded for the reap (default openclaw-main)")
    reap_stuck.add_argument("--notify", action="store_true", help="also alert the owner (Telegram) for each auto-reaped task; the daemon passes this in production")
    reap_stuck.set_defaults(func=cmd_watchdog_reap_stuck)

    init_p = sub.add_parser("init", help="guided first-run setup: detect installed agent CLIs, add them as employees, print next steps")
    init_p.add_argument("--yes", action="store_true", help="non-interactive: auto-accept detected runtimes")
    init_p.add_argument("--execute", action="store_true", help="also enable autonomous execution on added workers (off by default)")
    init_p.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    init_p.set_defaults(func=cmd_init)

    meeting = sub.add_parser("meeting", help="employee-initiated meetings: an agent calls colleagues to settle a hard decision (async)")
    meeting_sub = meeting.add_subparsers(dest="meeting_cmd", required=True)
    meeting_req = meeting_sub.add_parser("request", help="ask colleagues for a quick discussion on a decision; runs in the background")
    meeting_req.add_argument("--from", dest="source", required=True, help="your employee id (the requester)")
    meeting_req.add_argument("--topic", required=True, help="short meeting title")
    meeting_req.add_argument("--participants", required=True, help="comma-separated colleague ids to invite")
    meeting_req.add_argument("--question", required=True, help="the decision/question to settle")
    meeting_req.add_argument("--mode", choices=sorted(CONVERSATION_MODES), default="discuss")
    meeting_req.add_argument("--rounds", type=int, default=1)
    meeting_req.add_argument("--synthesizer", default="", help="chair who writes the conclusion (default: hermes if present)")
    meeting_req.add_argument("--project", default="", help="tie to a project memory bank (read digest + store conclusion)")
    meeting_req.set_defaults(func=cmd_meeting_request)
    meeting_res = meeting_sub.add_parser("result", help="read back a meeting's conclusion (poll after requesting)")
    meeting_res.add_argument("--conversation-id", required=True)
    meeting_res.set_defaults(func=cmd_meeting_result)

    user = sub.add_parser("user", help="manage human API users + roles (RBAC: viewer/operator/admin/owner)")
    user_sub = user.add_subparsers(dest="user_cmd", required=True)
    user_add = user_sub.add_parser("add", help="add/replace a user, prints a bearer token")
    user_add.add_argument("--user", required=True)
    user_add.add_argument("--role", required=True, choices=list(RBAC_ROLES))
    user_add.add_argument("--token", default="", help="set a specific token (default: generate)")
    user_add.set_defaults(func=cmd_user_add)
    user_list = user_sub.add_parser("list")
    user_list.set_defaults(func=cmd_user_list)
    user_remove = user_sub.add_parser("remove")
    user_remove.add_argument("--user", required=True)
    user_remove.set_defaults(func=cmd_user_remove)
    communication = sub.add_parser("communication")
    communication_sub = communication.add_subparsers(dest="communication_cmd", required=True)
    communication_show = communication_sub.add_parser("show")
    communication_show.add_argument("--agent", default="")
    communication_show.set_defaults(func=cmd_communication_show)
    communication_check = communication_sub.add_parser("check")
    communication_check.add_argument("--from", dest="source", required=True)
    communication_check.add_argument("--to", dest="target", required=True)
    communication_check.add_argument("--action", choices=["talk", "assign"], default="talk")
    communication_check.set_defaults(func=cmd_communication_check)
    communication_pause = communication_sub.add_parser("pause", help="stop an employee from dispatching tasks / sending messages (reversible)")
    communication_pause.add_argument("--agent", required=True)
    communication_pause.set_defaults(func=cmd_communication_pause)
    communication_resume = communication_sub.add_parser("resume", help="re-enable an employee's dispatching / messaging")
    communication_resume.add_argument("--agent", required=True)
    communication_resume.set_defaults(func=cmd_communication_resume)

    notification = sub.add_parser("notification")
    notification_sub = notification.add_subparsers(dest="notification_cmd", required=True)
    notification_settings_cmd = notification_sub.add_parser("settings")
    notification_settings_cmd.set_defaults(func=cmd_notification_settings)
    notification_send = notification_sub.add_parser("send")
    notification_send.add_argument("--message", required=True)
    notification_send.add_argument("--target", default="")
    notification_send.add_argument("--account", default="")
    notification_send.add_argument("--subject", default="")
    notification_send.add_argument("--kind", choices=["general", "approval", "error"], default="general")
    notification_send.add_argument("--dry-run", action="store_true")
    notification_send.set_defaults(func=cmd_notification_send)

    supervisor = sub.add_parser("supervisor")
    supervisor_sub = supervisor.add_subparsers(dest="supervisor_cmd", required=True)
    supervisor_loop = supervisor_sub.add_parser("delivery-loop")
    supervisor_loop.add_argument("--limit", type=int, default=20)
    supervisor_loop.add_argument("--by", default="supervisor-loop")
    supervisor_loop.set_defaults(func=cmd_supervisor_delivery_loop)
    supervisor_scan = supervisor_sub.add_parser("scan-attempts")
    supervisor_scan.add_argument("--by", default="hermes")
    supervisor_scan.add_argument("--now", default="")
    supervisor_scan.set_defaults(func=cmd_supervisor_scan_attempts)

    policy = sub.add_parser("policy")
    policy_sub = policy.add_subparsers(dest="policy_cmd", required=True)
    policy_show = policy_sub.add_parser("show")
    policy_show.set_defaults(func=cmd_policy_show)
    policy_block = policy_sub.add_parser("block-report")
    policy_block.add_argument("--source", default="")
    policy_block.add_argument("--target", default="")
    policy_block.add_argument("--tool", default="")
    policy_block.add_argument("--operation", default="")
    policy_block.add_argument("--error", required=True)
    policy_block.add_argument("--block-id", default="")
    policy_block.add_argument("--dry-run", action="store_true")
    policy_block.set_defaults(func=cmd_policy_block_report)

    guard = sub.add_parser("guard")
    guard_sub = guard.add_subparsers(dest="guard_cmd", required=True)
    guard_check = guard_sub.add_parser("check")
    guard_check.add_argument("--path", action="append", default=[])
    guard_check.add_argument("--changed-file", action="append", default=[])
    guard_check.set_defaults(func=cmd_guard_check)

    rfc = sub.add_parser("rfc")
    rfc_sub = rfc.add_subparsers(dest="rfc_cmd", required=True)
    rfc_create = rfc_sub.add_parser("create")
    rfc_create.add_argument("--rfc-id", default="")
    rfc_create.add_argument("--title", required=True)
    rfc_create.add_argument("--by", required=True)
    rfc_create.add_argument("--paths", required=True, help="comma-separated protected paths this RFC covers")
    rfc_create.add_argument("--reason", required=True)
    rfc_create.add_argument("--proposal", default="")
    rfc_create.add_argument("--rollback", default="")
    rfc_create.add_argument("--file", default="")
    rfc_create.add_argument("--overwrite", action="store_true")
    rfc_create.set_defaults(func=cmd_rfc_create)
    rfc_list = rfc_sub.add_parser("list")
    rfc_list.add_argument("--status", choices=["pending", "approved", "denied", "all"], default="pending")
    rfc_list.set_defaults(func=cmd_rfc_list)
    rfc_show = rfc_sub.add_parser("show")
    rfc_show.add_argument("--rfc", required=True)
    rfc_show.set_defaults(func=cmd_rfc_show)
    rfc_approve = rfc_sub.add_parser("approve")
    rfc_approve.add_argument("--rfc", required=True)
    rfc_approve.add_argument("--by", required=True)
    rfc_approve.add_argument("--reason", default="")
    rfc_approve.set_defaults(func=cmd_rfc_approve)
    rfc_deny = rfc_sub.add_parser("deny")
    rfc_deny.add_argument("--rfc", required=True)
    rfc_deny.add_argument("--by", required=True)
    rfc_deny.add_argument("--reason", default="")
    rfc_deny.set_defaults(func=cmd_rfc_deny)

    workflow = sub.add_parser("workflow")
    workflow_sub = workflow.add_subparsers(dest="workflow_cmd", required=True)
    workflow_validate = workflow_sub.add_parser("validate")
    workflow_validate.add_argument("--workflow", required=True)
    workflow_validate.set_defaults(func=cmd_workflow_validate)
    workflow_run = workflow_sub.add_parser("run")
    workflow_run.add_argument("--workflow", required=True)
    workflow_run.add_argument("--topic", default="")
    workflow_run.add_argument("--run-id", default="")
    workflow_run.add_argument("--max-steps", type=int, default=0)
    workflow_run.add_argument("--dry-run", action="store_true")
    workflow_run.set_defaults(func=cmd_workflow_run)

    scheduler = sub.add_parser("scheduler")
    scheduler_sub = scheduler.add_subparsers(dest="scheduler_cmd", required=True)
    scheduler_run = scheduler_sub.add_parser("run")
    scheduler_run.add_argument("--limit", type=int, default=20)
    scheduler_run.add_argument("--dry-run", action="store_true")
    scheduler_run.set_defaults(func=cmd_scheduler_run)
    scheduler_events = scheduler_sub.add_parser("events")
    scheduler_events.add_argument("--limit", type=int, default=20)
    scheduler_events.add_argument("--pending", action="store_true")
    scheduler_events.set_defaults(func=cmd_scheduler_events)
    scheduler_skip_event = scheduler_sub.add_parser("skip-event")
    scheduler_skip_event.add_argument("--event-id", required=True)
    scheduler_skip_event.add_argument("--by", required=True)
    scheduler_skip_event.add_argument("--reason", required=True)
    scheduler_skip_event.set_defaults(func=cmd_scheduler_skip_event)

    memory = sub.add_parser("memory", help="project memory bank: shared, curated, per-project memory")
    memory_sub = memory.add_subparsers(dest="memory_cmd", required=True)
    memory_project = memory_sub.add_parser("project", help="manage memory-bank projects")
    memory_project_sub = memory_project.add_subparsers(dest="memory_project_cmd", required=True)
    project_create = memory_project_sub.add_parser("create")
    project_create.add_argument("--id", required=True)
    project_create.add_argument("--name", default="")
    project_create.add_argument("--workspace", default="", help="repo/workspace path this project maps to")
    project_create.add_argument("--lead", default="hermes", help="memory 主负责人 (curator)")
    project_create.set_defaults(func=cmd_membank_create)
    project_list = memory_project_sub.add_parser("list")
    project_list.set_defaults(func=cmd_membank_list)
    project_show = memory_project_sub.add_parser("show")
    project_show.add_argument("--id", required=True)
    project_show.add_argument("--limit", type=int, default=50)
    project_show.set_defaults(func=cmd_membank_show)
    project_execs = memory_project_sub.add_parser("set-executors", help="lock who may work this project (comma-separated employee ids; empty = unlock)")
    project_execs.add_argument("--id", required=True)
    project_execs.add_argument("--executors", default="", help="e.g. codex-cli,claude-cli,agy  (空=解锁,谁都能接)")
    project_execs.set_defaults(func=cmd_membank_set_executors)
    memory_remember = memory_sub.add_parser("remember")
    memory_remember.add_argument("--project", required=True)
    memory_remember.add_argument("--title", required=True)
    memory_remember.add_argument("--body", default="")
    memory_remember.add_argument("--type", default="fact", choices=sorted(project_memory.ENTRY_TYPES))
    memory_remember.add_argument("--by", default="")
    memory_remember.add_argument("--task-id", default="")
    memory_remember.add_argument("--evidence", default="")
    memory_remember.add_argument("--importance", type=int, default=1)
    memory_remember.set_defaults(func=cmd_memory_remember)
    memory_recall = memory_sub.add_parser("recall")
    memory_recall.add_argument("--project", required=True)
    memory_recall.add_argument("--query", default="")
    memory_recall.add_argument("--limit", type=int, default=50)
    memory_recall.set_defaults(func=cmd_memory_recall)
    memory_curate = memory_sub.add_parser("curate", help="the lead's pass: dedup + rebuild digest")
    memory_curate.add_argument("--project", required=True)
    memory_curate.add_argument("--by", default="")
    memory_curate.set_defaults(func=cmd_memory_curate)
    memory_curate_all = memory_sub.add_parser("curate-all", help="curate every project with new memory (daemon)")
    memory_curate_all.set_defaults(func=cmd_memory_curate_all)
    memory_archive = memory_sub.add_parser("archive", help="retire a memory entry (manual curation)")
    memory_archive.add_argument("--entry-id", required=True)
    memory_archive.add_argument("--by", default="")
    memory_archive.set_defaults(func=cmd_memory_archive)

    approval = sub.add_parser("approval")
    approval_sub = approval.add_subparsers(dest="approval_cmd", required=True)
    approval_request = approval_sub.add_parser("request")
    approval_request.add_argument("--from", dest="source", required=True)
    approval_request.add_argument("--action", required=True)
    approval_request.add_argument("--reason", required=True)
    approval_request.add_argument("--target", default="")
    approval_request.add_argument("--risk", default="")
    approval_request.add_argument("--evidence", default="")
    approval_request.add_argument("--task-id", default="")
    approval_request.add_argument("--approval-id", default="")
    approval_request.set_defaults(func=cmd_approval_request)
    approval_list = approval_sub.add_parser("list")
    approval_list.add_argument("--status", choices=["pending", "approved", "denied", "resolved", "all"], default="pending")
    approval_list.add_argument("--agent", default="")
    approval_list.add_argument("--action", default="")
    approval_list.add_argument("--limit", type=int, default=50)
    approval_list.set_defaults(func=cmd_approval_list)
    approval_show = approval_sub.add_parser("show")
    approval_show.add_argument("--approval-id", required=True)
    approval_show.set_defaults(func=cmd_approval_show)
    approval_mode = approval_sub.add_parser("mode", help="get/set approval posture: manual (gate) or auto (full auto-approve, owner-delegated)")
    approval_mode.add_argument("--set", default="", choices=["", "manual", "auto_low_risk", "auto"], help="set the mode; omit to just show current")
    approval_mode.add_argument("--by", default="owner", help="who is changing the mode (audit)")
    approval_mode.set_defaults(func=cmd_approval_mode)
    approval_auto_sweep = approval_sub.add_parser("auto-sweep", help="auto mode safety net: approve+materialize all pending route approvals (no-op unless mode=auto)")
    approval_auto_sweep.set_defaults(func=cmd_approval_auto_sweep)
    approval_approve = approval_sub.add_parser("approve")
    approval_approve.add_argument("--approval-id", required=True)
    approval_approve.add_argument("--by", required=True)
    approval_approve.add_argument("--reason", default="")
    approval_approve.set_defaults(func=cmd_approval_approve)
    approval_deny = approval_sub.add_parser("deny")
    approval_deny.add_argument("--approval-id", required=True)
    approval_deny.add_argument("--by", required=True)
    approval_deny.add_argument("--reason", default="")
    approval_deny.set_defaults(func=cmd_approval_deny)
    approval_resolve = approval_sub.add_parser("resolve")
    approval_resolve.add_argument("--approval-id", required=True)
    approval_resolve.add_argument("--by", required=True)
    approval_resolve.add_argument("--reason", default="")
    approval_resolve.add_argument("--mock", action="store_true", help="record a dry-run/mock resolution without triggering external delivery")
    approval_resolve.set_defaults(func=cmd_approval_resolve)

    audit_parser = sub.add_parser("audit")
    audit_sub = audit_parser.add_subparsers(dest="audit_cmd", required=True)
    audit_evidence = audit_sub.add_parser("evidence")
    audit_evidence.add_argument("--task-id", default="")
    audit_evidence.add_argument("--employee-id", "--employee", default="")
    audit_evidence.add_argument("--limit", type=int, default=50)
    audit_evidence.set_defaults(func=cmd_audit_evidence)
    audit_artifacts = audit_sub.add_parser("artifacts")
    audit_artifacts.add_argument("--task-id", default="")
    audit_artifacts.add_argument("--limit", type=int, default=50)
    audit_artifacts.set_defaults(func=cmd_audit_artifacts)
    audit_handoffs = audit_sub.add_parser("handoffs")
    audit_handoffs.add_argument("--task-id", default="")
    audit_handoffs.add_argument("--limit", type=int, default=50)
    audit_handoffs.set_defaults(func=cmd_audit_handoffs)
    audit_failures = audit_sub.add_parser("failures")
    audit_failures.add_argument("--task-id", default="")
    audit_failures.add_argument("--limit", type=int, default=50)
    audit_failures.set_defaults(func=cmd_audit_failures)

    trace_parser = sub.add_parser("trace")
    trace_sub = trace_parser.add_subparsers(dest="trace_cmd", required=True)
    trace_timeline = trace_sub.add_parser("timeline")
    trace_timeline.add_argument("--trace-id", default="")
    trace_timeline.add_argument("--task-id", default="")
    trace_timeline.set_defaults(func=cmd_trace_timeline)

    workspace = sub.add_parser("workspace")
    workspace_sub = workspace.add_subparsers(dest="workspace_cmd", required=True)
    workspace_prune = workspace_sub.add_parser("prune")
    workspace_prune.add_argument("--dry-run", action="store_true")
    workspace_prune.add_argument("--older-than-days", type=int, default=30)
    workspace_prune.add_argument("--limit", type=int, default=100)
    workspace_prune.set_defaults(func=cmd_workspace_prune)

    lock = sub.add_parser("lock")
    lock_sub = lock.add_subparsers(dest="lock_cmd", required=True)
    lock_acquire = lock_sub.add_parser("acquire")
    lock_acquire.add_argument("--agent", required=True)
    lock_acquire.add_argument("--resource", required=True)
    lock_acquire.add_argument("--lease-seconds", type=int, default=1800)
    lock_acquire.set_defaults(func=cmd_lock_acquire)
    lock_release = lock_sub.add_parser("release")
    lock_release.add_argument("--agent", required=True)
    lock_release.add_argument("--resource", required=True)
    lock_release.add_argument("--force", action="store_true")
    lock_release.set_defaults(func=cmd_lock_release)
    lock_list = lock_sub.add_parser("list")
    lock_list.add_argument("--agent", default="")
    lock_list.set_defaults(func=cmd_lock_list)
    lock_unlock_stale = lock_sub.add_parser("unlock-stale")
    lock_unlock_stale.set_defaults(func=cmd_lock_unlock_stale)

    repair = sub.add_parser("repair")
    repair_sub = repair.add_subparsers(dest="repair_cmd", required=True)
    repair_reset_stale_claims = repair_sub.add_parser("reset-stale-claims")
    repair_reset_stale_claims.set_defaults(func=cmd_repair_reset_stale_claims)

    hb = sub.add_parser("heartbeat")
    hb.add_argument("--agent", required=True)
    hb.set_defaults(func=cmd_heartbeat)

    runtime = sub.add_parser("runtime")
    runtime_sub = runtime.add_subparsers(dest="runtime_cmd", required=True)
    runtime_register = runtime_sub.add_parser("register")
    runtime_register.add_argument("--runtime", required=True)
    runtime_register.add_argument("--command", default="")
    runtime_register.add_argument("--status", choices=["registered", "disabled"], default="registered")
    runtime_register.add_argument("--notes", default="")
    runtime_register.set_defaults(func=cmd_runtime_register)
    runtime_list = runtime_sub.add_parser("list")
    runtime_list.set_defaults(func=cmd_runtime_list)
    runtime_test = runtime_sub.add_parser("test")
    runtime_test.add_argument("--runtime", required=True)
    runtime_test.set_defaults(func=cmd_runtime_test)
    runtime_verify_adapters = runtime_sub.add_parser("verify-adapters")
    runtime_verify_adapters.add_argument("--agents", default="", help="comma-separated employee ids; defaults to all adapter-backed employees")
    runtime_verify_adapters.add_argument("--source", default="", help="source employee for verification tasks; default auto-detects openclaw-main/main")
    runtime_verify_adapters.add_argument("--task-id-prefix", default="task-runtime-verify")
    runtime_verify_adapters.add_argument("--allow-candidate", action="store_true", help="allow safe dry-run verification tasks for candidate employees without enabling normal scheduling")
    runtime_verify_adapters.add_argument("--execute", action="store_true", help="run real adapter execution; default is safe dry-run")
    runtime_verify_adapters.add_argument("--run-scheduler", action=argparse.BooleanOptionalAction, default=True, help="process generated events after adapter verification")
    runtime_verify_adapters.set_defaults(func=cmd_runtime_verify_adapters)
    runtime_adapter_runs = runtime_sub.add_parser("adapter-runs")
    runtime_adapter_runs.add_argument("--agent", default="")
    runtime_adapter_runs.add_argument("--status", choices=["all", "ok", "failed"], default="all")
    runtime_adapter_runs.add_argument("--unacknowledged-only", action="store_true")
    runtime_adapter_runs.add_argument("--limit", type=int, default=20)
    runtime_adapter_runs.set_defaults(func=cmd_runtime_adapter_runs)
    runtime_adapter_run_show = runtime_sub.add_parser("adapter-run")
    runtime_adapter_run_sub = runtime_adapter_run_show.add_subparsers(dest="adapter_run_cmd", required=True)
    runtime_adapter_run_show_cmd = runtime_adapter_run_sub.add_parser("show")
    runtime_adapter_run_show_cmd.add_argument("--run-id", required=True)
    runtime_adapter_run_show_cmd.add_argument("--summary", action="store_true", help="omit raw result_json/stdout and return compact fields for alerts")
    runtime_adapter_run_show_cmd.set_defaults(func=cmd_runtime_adapter_run_show)
    runtime_ack_adapter_run = runtime_sub.add_parser("ack-adapter-run")
    runtime_ack_adapter_run.add_argument("--run-id", required=True)
    runtime_ack_adapter_run.add_argument("--by", required=True)
    runtime_ack_adapter_run.add_argument("--reason", required=True)
    runtime_ack_adapter_run.set_defaults(func=cmd_runtime_ack_adapter_run)
    runtime_retry_adapter_run = runtime_sub.add_parser("retry-adapter-run")
    runtime_retry_adapter_run.add_argument("--run-id", required=True)
    runtime_retry_adapter_run.add_argument("--by", required=True)
    runtime_retry_adapter_run.add_argument("--reason", required=True)
    runtime_retry_adapter_run.add_argument("--task-id", default="")
    runtime_retry_adapter_run.set_defaults(func=cmd_runtime_retry_adapter_run)
    runtime_session = runtime_sub.add_parser("session")
    runtime_session_sub = runtime_session.add_subparsers(dest="runtime_session_cmd", required=True)
    runtime_session_start = runtime_session_sub.add_parser("start")
    runtime_session_start.add_argument("--session-id", default="")
    runtime_session_start.add_argument("--employee", required=True)
    runtime_session_start.add_argument("--adapter-type", default="")
    runtime_session_start.add_argument("--runtime-type", default="")
    runtime_session_start.add_argument("--pid", default="")
    runtime_session_start.add_argument("--session-key", default="")
    runtime_session_start.add_argument("--task-id", default="")
    runtime_session_start.add_argument("--attempt-id", default="")
    runtime_session_start.set_defaults(func=cmd_runtime_session_start)
    runtime_session_heartbeat = runtime_session_sub.add_parser("heartbeat")
    runtime_session_heartbeat.add_argument("--session-id", required=True)
    runtime_session_heartbeat.add_argument("--status", default="active")
    runtime_session_heartbeat.add_argument("--progress", action="store_true")
    runtime_session_heartbeat.set_defaults(func=cmd_runtime_session_heartbeat)
    runtime_session_stop = runtime_session_sub.add_parser("stop")
    runtime_session_stop.add_argument("--session-id", required=True)
    runtime_session_stop.add_argument("--status", choices=["stopped", "failed", "stale", "cancelled"], default="stopped")
    runtime_session_stop.add_argument("--error", default="")
    runtime_session_stop.set_defaults(func=cmd_runtime_session_stop)
    runtime_session_list = runtime_session_sub.add_parser("list")
    runtime_session_list.add_argument("--employee", default="")
    runtime_session_list.add_argument("--task-id", default="")
    runtime_session_list.add_argument("--trace-id", default="")
    runtime_session_list.add_argument("--limit", type=int, default=50)
    runtime_session_list.set_defaults(func=cmd_runtime_session_list)

    tool_call = sub.add_parser("tool-call")
    tool_call_sub = tool_call.add_subparsers(dest="tool_call_cmd", required=True)
    tool_call_start = tool_call_sub.add_parser("start")
    tool_call_start.add_argument("--tool-call-id", default="")
    tool_call_start.add_argument("--trace-id", default="")
    tool_call_start.add_argument("--task-id", default="")
    tool_call_start.add_argument("--attempt-id", default="")
    tool_call_start.add_argument("--employee", required=True)
    tool_call_start.add_argument("--session-id", default="")
    tool_call_start.add_argument("--tool-name", required=True)
    tool_call_start.add_argument("--tool-type", default="other")
    tool_call_start.add_argument("--input-summary", default="")
    tool_call_start.add_argument("--risk-level", default="")
    tool_call_start.add_argument("--approval-id", default="")
    tool_call_start.set_defaults(func=cmd_tool_call_start)
    tool_call_finish = tool_call_sub.add_parser("finish")
    tool_call_finish.add_argument("--tool-call-id", required=True)
    tool_call_finish.add_argument("--status", choices=["success", "failed", "blocked", "cancelled"], required=True)
    tool_call_finish.add_argument("--output-summary", default="")
    tool_call_finish.add_argument("--error", default="")
    tool_call_finish.set_defaults(func=cmd_tool_call_finish)
    tool_call_list = tool_call_sub.add_parser("list")
    tool_call_list.add_argument("--employee", default="")
    tool_call_list.add_argument("--task-id", default="")
    tool_call_list.add_argument("--trace-id", default="")
    tool_call_list.add_argument("--attempt-id", default="")
    tool_call_list.add_argument("--session-id", default="")
    tool_call_list.add_argument("--limit", type=int, default=50)
    tool_call_list.set_defaults(func=cmd_tool_call_list)

    economics = sub.add_parser("economics")
    economics.set_defaults(func=cmd_economics)

    cost = sub.add_parser("cost", help="operating-cost dashboard: who is on duty free vs who spent (per employee + per day)")
    cost.add_argument("--days", type=int, default=14, help="per-day trend window (default 14)")
    cost.set_defaults(func=cmd_cost)

    backup = sub.add_parser("backup", help="consistent SQLite snapshot (online backup API + integrity check + rolling prune)")
    backup.add_argument("--keep", type=int, default=14, help="rolling snapshots to retain (default 14)")
    backup.add_argument("--label", default="", help="optional label appended to the snapshot filename")
    backup.add_argument("--list", action="store_true", help="list existing snapshots instead of creating one")
    backup.set_defaults(func=cmd_backup)

    restore = sub.add_parser("restore", help="restore the DB from a snapshot (validates source, snapshots current first)")
    restore.add_argument("src", help="snapshot file to restore from")
    restore.add_argument("--yes", action="store_true", help="confirm overwrite — required (refuses without it)")
    restore.set_defaults(func=cmd_restore)

    verifier = sub.add_parser("verifier")
    verifier_sub = verifier.add_subparsers(dest="verifier_cmd", required=True)
    verifier_record = verifier_sub.add_parser("record")
    verifier_record.add_argument("--task-id", default="")
    verifier_record.add_argument("--attempt-id", default="")
    verifier_record.add_argument("--employee", default="")
    verifier_record.add_argument("--kind", default="status")
    verifier_record.add_argument("--arg", default="")
    verifier_record.add_argument("--result", default="")
    verifier_record.add_argument("--agent-verdict", default="")
    verifier_record.add_argument("--detail", default="")
    verifier_record.set_defaults(func=cmd_verifier_record)

    verifier_accuracy = sub.add_parser("verifier-accuracy")
    verifier_accuracy.set_defaults(func=cmd_verifier_accuracy)

    a2a = sub.add_parser("a2a")
    a2a_sub = a2a.add_subparsers(dest="a2a_cmd", required=True)
    a2a_request = a2a_sub.add_parser("request")
    a2a_request.add_argument("--source", required=True)
    a2a_request.add_argument("--target", required=True)
    a2a_request.add_argument("--action", default="")
    a2a_request.add_argument("--payload", default="")
    a2a_request.set_defaults(func=cmd_a2a_request)
    a2a_approve = a2a_sub.add_parser("approve")
    a2a_approve.add_argument("--request-id", required=True)
    a2a_approve.add_argument("--by", default="")
    a2a_approve.set_defaults(func=cmd_a2a_approve)
    a2a_deny = a2a_sub.add_parser("deny")
    a2a_deny.add_argument("--request-id", required=True)
    a2a_deny.add_argument("--by", default="")
    a2a_deny.set_defaults(func=cmd_a2a_deny)
    a2a_list = a2a_sub.add_parser("list")
    a2a_list.add_argument("--status", default="")
    a2a_list.add_argument("--limit", type=int, default=50)
    a2a_list.set_defaults(func=cmd_a2a_list)

    budget = sub.add_parser("budget")
    budget_sub = budget.add_subparsers(dest="budget_cmd", required=True)
    budget_record = budget_sub.add_parser("record")
    budget_record.add_argument("--budget-event-id", default="")
    budget_record.add_argument("--budget-account-id", default="")
    budget_record.add_argument("--task-id", default="")
    budget_record.add_argument("--trace-id", default="")
    budget_record.add_argument("--attempt-id", default="")
    budget_record.add_argument("--employee", required=True)
    budget_record.add_argument("--cost-type", required=True)
    budget_record.add_argument("--amount", required=True)
    budget_record.add_argument("--currency", default="USD")
    budget_record.add_argument("--token-input", type=int, default=0)
    budget_record.add_argument("--token-output", type=int, default=0)
    budget_record.add_argument("--model-name", default="")
    budget_record.add_argument("--provider", default="")
    budget_record.add_argument("--runtime-seconds", type=int, default=0)
    budget_record.add_argument("--summary", default="")
    budget_record.set_defaults(func=cmd_budget_record)
    budget_summary_cmd = budget_sub.add_parser("summary")
    budget_summary_cmd.add_argument("--task-id", default="")
    budget_summary_cmd.add_argument("--trace-id", default="")
    budget_summary_cmd.add_argument("--attempt-id", default="")
    budget_summary_cmd.add_argument("--employee", default="")
    budget_summary_cmd.add_argument("--limit", type=int, default=50)
    budget_summary_cmd.set_defaults(func=cmd_budget_summary)

    doctor = sub.add_parser("doctor")
    doctor.add_argument("--summary", action="store_true", help="return compact health counts for low-token alert checks")
    doctor.add_argument("--strict-launchd", action="store_true", help="fail health check when the launchd agent is not installed")
    doctor.add_argument("--strict-openclaw", action="store_true", help="fail health check when OpenClaw-native Telegram safety guard detects conflicts or stuck ingress spools")
    doctor.set_defaults(func=cmd_doctor)
    return parser


# ── facade re-export: the watchdog domain now lives in company_kernel/watchdog.py ──
# Imported at the END of this module (after every shared primitive watchdog.py references at call
# time is defined) so the companyctl↔watchdog cycle resolves. Re-exporting keeps every existing
# caller — `companyctl.reap_stuck_attempts_internal`, `companyctl.process_alive`, the build_parser
# `func=cmd_watchdog_reap_stuck` binding, etc. — working with zero changes.
from .watchdog import (  # noqa: E402
    WATCHDOG_GLOBAL_CAP_SECONDS,
    WATCHDOG_ORPHAN_GRACE_SECONDS,
    TERMINAL_TASK_STATUSES,
    REAP_REASON_LABEL,
    process_alive,
    reap_stuck_attempts_internal,
    notify_owner_of_reaps,
    cmd_watchdog_reap_stuck,
)


def main(argv: list[str] | None = None) -> int:
    try:
        parser = build_parser()
        args = parser.parse_args(argv)
        return args.func(args)
    finally:
        close_open_connections()


if __name__ == "__main__":
    # `python -m company_kernel.companyctl` runs THIS file as `__main__`. Split-out domain modules
    # (watchdog.py, …) do a lazy `from company_kernel import companyctl`, which would otherwise import
    # a SECOND, separate copy of this module under its real name — leaving two divergent module objects
    # (split _OPEN_CONNECTIONS cleanup, mocks, and globals across both). Alias __main__ as the canonical
    # name BEFORE any domain function runs, so the lazy import reuses this exact module. (codex review.)
    import sys as _sys
    _sys.modules.setdefault("company_kernel.companyctl", _sys.modules["__main__"])
    try:
        import company_kernel as _pkg
        if getattr(_pkg, "companyctl", None) is not _sys.modules["__main__"]:
            _pkg.companyctl = _sys.modules["__main__"]
    except Exception:
        pass
    # Restore default SIGPIPE so `companyctl … | head` (or any truncating pipe) exits quietly like a
    # normal Unix tool instead of dumping a BrokenPipeError traceback. Only when run as the CLI — the
    # in-process test/daemon callers go through main() directly and keep Python's default handling.
    try:
        import signal as _signal
        _signal.signal(_signal.SIGPIPE, _signal.SIG_DFL)
    except (AttributeError, ValueError, OSError):
        pass
    raise SystemExit(main())
