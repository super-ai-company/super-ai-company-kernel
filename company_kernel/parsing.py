"""company_kernel.parsing — pure parsing / field-extraction leaves, with NO dependency on companyctl,
any domain module, the DB, config, or IO. First themed batch of the pure-leaf sweep (meeting
conv-20260620-150322-901c56): the JSON-arg / JSON-output parsers and the OpenClaw native-result field
extractors. They operate solely on the passed-in string / dict / Path and stdlib.

companyctl forwards these names with a plain `from .parsing import ...` (no wrapper) — every existing
`companyctl.parse_json_arg(...)` / bare-name caller is unchanged. Nothing patches or qualified-imports
these, so no mock-anchor stays behind. parse_json_output joined the batch because
parse_openclaw_agent_reply calls it — moving the dependency too keeps parsing.py a clean leaf (it must
never import companyctl back).
"""
from __future__ import annotations

import json
from pathlib import Path


def parse_json_arg(raw: str, default: object) -> object:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid json: {exc}") from exc


def parse_json_output(raw: str) -> dict:
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {"raw": raw}
    except json.JSONDecodeError:
        return {"raw": raw}


def parse_openclaw_agent_reply(stdout: str) -> str:
    payload = parse_json_output(stdout)
    result = payload.get("result") if isinstance(payload, dict) else {}
    payloads = result.get("payloads") if isinstance(result, dict) else []
    if isinstance(payloads, list):
        for item in payloads:
            if isinstance(item, dict) and str(item.get("text") or "").strip():
                return str(item["text"]).strip()
    meta = result.get("meta") if isinstance(result, dict) else {}
    if isinstance(meta, dict):
        for key in ("finalAssistantVisibleText", "finalAssistantRawText"):
            if str(meta.get(key) or "").strip():
                return str(meta[key]).strip()
    return ""


def _openclaw_native_result_task_id(payload: dict) -> str:
    nested = payload.get("payload", {}) if isinstance(payload.get("payload", {}), dict) else {}
    for key in ("kernel_task_id", "task_id"):
        value = str(nested.get(key) or payload.get(key) or "").strip()
        if value:
            return value
    return ""


def _openclaw_native_result_agent(payload: dict, fallback: str = "") -> str:
    for key in ("source_agent", "employee_id", "agent"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    nested = payload.get("payload", {}) if isinstance(payload.get("payload", {}), dict) else {}
    for key in ("source_agent", "employee_id", "agent"):
        value = str(nested.get(key) or "").strip()
        if value:
            return value
    return fallback


def _openclaw_native_result_summary(payload: dict, state: str) -> str:
    nested = payload.get("payload", {}) if isinstance(payload.get("payload", {}), dict) else {}
    for key in ("summary", "message", "result", "receipt"):
        value = str(nested.get(key) or payload.get(key) or "").strip()
        if value:
            return value
    return f"OpenClaw native {state} result imported"


def _openclaw_native_result_evidence(payload: dict, source_file: Path) -> str:
    nested = payload.get("payload", {}) if isinstance(payload.get("payload", {}), dict) else {}
    for key in ("evidence_path", "evidence", "report_path", "path"):
        value = str(nested.get(key) or payload.get(key) or "").strip()
        if value:
            return value
    return str(source_file)
