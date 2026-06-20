"""company_kernel.textutil — pure text / slug / row-normalization leaves, with NO dependency on
companyctl, any domain module, the DB, config, or IO. Long-tail pure-leaf batch: small string/dict
transforms each verified by reading to touch only stdlib (re/json) and their argument.

companyctl forwards these names with a plain `from .textutil import ...` (no wrapper) — every existing
bare-name / companyctl.X caller is unchanged. Nothing patches or qualified-imports these, so no
mock-anchor stays behind. normalize_rfc / normalize_project take a sqlite3.Row OR dict but only
`dict(row)` + json-decode a column — pure transforms, no query — so they belong here, not in a DB module.
"""
from __future__ import annotations

import json
import re


def slug(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in value).strip("-") or "item"


def mermaid_node_id(raw: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", raw)


def clamp_audit_limit(limit: int | str | None) -> int:
    try:
        value = int(limit or 50)
    except (TypeError, ValueError):
        value = 50
    return max(1, min(value, 200))


def normalize_task_title(title: str) -> str:
    return re.sub(r"\s+", "", str(title or "")).lower()


def normalize_rfc(row) -> dict:
    obj = dict(row)
    try:
        obj["target_paths"] = json.loads(obj.pop("target_paths_json", "[]") or "[]")
    except json.JSONDecodeError:
        obj["target_paths"] = []
    return obj


def normalize_project(row) -> dict:
    obj = dict(row)
    try:
        obj["acceptance"] = json.loads(obj.pop("acceptance_json", "[]") or "[]")
    except json.JSONDecodeError:
        obj["acceptance"] = []
    return obj


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


def parse_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def parse_participants(raw: str) -> list[str]:
    participants = []
    for item in raw.split(","):
        item = item.strip()
        if item and item not in participants:
            participants.append(item)
    return participants


def parse_acceptance(raw: str) -> list[str]:
    items = []
    for item in raw.split(";"):
        item = item.strip()
        if item:
            items.append(item)
    return items


def normalize_employee_lookup(value: str) -> str:
    return " ".join(str(value or "").strip().split()).casefold()


def safe_path_token(value: str) -> str:
    token = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(value or ""))
    return token.strip("._-") or "task"


def communication_name_aliases(employee_id: str, name: str) -> list[str]:
    aliases = []
    clean_name = " ".join(str(name or "").strip().split())
    if clean_name and clean_name != employee_id:
        aliases.append(clean_name)
        compact = clean_name.replace(" ", "-").lower()
        if compact and compact not in {employee_id, clean_name}:
            aliases.append(compact)
    return aliases


def report_progress_task_id(payload: dict) -> str:
    report = payload.get("report") if isinstance(payload.get("report"), dict) else {}
    return str(payload.get("task_id") or report.get("task_id") or "").strip()


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


def direct_probe_body(agent_id: str, round_index: int) -> str:
    return f"员工通信验证第{round_index}轮：请只回复 {agent_id}_VERIFY_ROUND_{round_index}_OK"
