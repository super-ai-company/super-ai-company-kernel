#!/usr/bin/env python3
from __future__ import annotations

from typing import Mapping, Sequence

REQUIRED_EXECUTION_FIELDS = ('owner', 'next_action', 'next_command', 'expected_completion_evidence')
STRICT_REQUEST_TYPES = {'blocker', 'cross_agent_handoff'}
STRICT_PRIORITIES = {'P1'}


def normalize_optional(value: object) -> str | None:
    text = str(value or '').strip()
    return text or None


def missing_required_fields(data: Mapping[str, object], fields: Sequence[str] = REQUIRED_EXECUTION_FIELDS) -> list[str]:
    missing: list[str] = []
    for field in fields:
        if not normalize_optional(data.get(field)):
            missing.append(field)
    return missing


def require_execution_fields(
    data: Mapping[str, object],
    *,
    fields: Sequence[str] = REQUIRED_EXECUTION_FIELDS,
    error_prefix: str = 'missing_required_fields',
) -> None:
    missing = missing_required_fields(data, fields)
    if missing:
        raise SystemExit(f"{error_prefix}:{','.join(missing)}")


def request_requires_strict_fields(request_type: str, priority: str) -> bool:
    return str(request_type or '').strip() in STRICT_REQUEST_TYPES or str(priority or '').strip() in STRICT_PRIORITIES
