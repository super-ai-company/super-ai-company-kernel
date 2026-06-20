"""company_kernel.core — shared low-level primitives with NO dependency on companyctl or any domain
module. Domain modules (watchdog, …) and the companyctl facade both depend on core, never the reverse.

This is the dependency-inversion FOUNDATION of the phased companyctl.py split: core sits at the bottom
of the import graph, so future domain extractions can import what they need from core directly instead
of reaching back into the companyctl facade (the lazy-import workaround used in split phase 1).

First cut (admitted by the codex/claude/gemini evidence gate): the closed set of time/datetime helpers
— now / parse_time / parse_iso_datetime / seconds_since. They are pure value computations (no DB,
audit, IO). companyctl re-exports them, so every existing `companyctl.now(...)` caller is unchanged.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def future_seconds(seconds: int) -> str:
    return (datetime.now(timezone.utc).astimezone() + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def parse_time(value: str) -> datetime:
    raw = value.replace("Z", "+00:00")
    return datetime.fromisoformat(raw)


def parse_iso_datetime(value: str):
    if not value:
        return None
    return datetime.fromisoformat(value)


def seconds_since(value: str, now_value: str) -> int:
    parsed = parse_iso_datetime(value)
    current = parse_iso_datetime(now_value)
    if not parsed or not current:
        return 0
    return max(0, int((current - parsed).total_seconds()))
