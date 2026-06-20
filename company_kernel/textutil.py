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
