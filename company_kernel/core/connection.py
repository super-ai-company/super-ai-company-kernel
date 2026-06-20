"""company_kernel.core.connection — the connection-abstraction leaf for the conn-decoupling phase
(contract /tmp/ck-conn/conn-contract.md, roadmap meeting conv-20260620-171433-801d08).

Goal: replace the ad-hoc `conn = connect_readonly(); try: ...; finally: conn.close()` scattered across
companyctl with short-lived, auto-closing connections obtained through context managers, so a command
never leaks or long-holds a connection (release-gate: idle connections = 0).

This is a LEAF (no companyctl import). companyctl injects how to find the DB after import via
set_path_provider(lambda: DB_PATH) — the provider reads companyctl's live DB_PATH global, so existing
`mock.patch.object(companyctl, "DB_PATH", ...)` test anchors still steer these connections. A
contextvars override (with_db_path) is reserved for future sandbox/tenant isolation per gemini's spec.
"""
from __future__ import annotations

import contextlib
import contextvars
import sqlite3
from collections.abc import Callable, Iterator

_path_provider: Callable[[], object] | None = None
_path_override: contextvars.ContextVar = contextvars.ContextVar("ck_db_path_override", default=None)


def set_path_provider(provider: Callable[[], object]) -> None:
    """companyctl calls this once after import: set_path_provider(lambda: DB_PATH). The provider is
    read LIVE on every connection, so patching companyctl.DB_PATH keeps steering us (mock anchor safe)."""
    global _path_provider
    _path_provider = provider


def resolve_db_path() -> str:
    override = _path_override.get()
    if override is not None:
        return str(override)
    if _path_provider is None:
        raise RuntimeError("company_kernel.core.connection path provider not set")
    return str(_path_provider())


@contextlib.contextmanager
def with_db_path(path: str) -> Iterator[None]:
    """Temporary per-context DB path (sandbox/tenant isolation). contextvars-scoped, reset on exit."""
    token = _path_override.set(str(path))
    try:
        yield
    finally:
        _path_override.reset(token)


@contextlib.contextmanager
def read_connection() -> Iterator[sqlite3.Connection]:
    """Short-lived READ-ONLY connection; closed on exit no matter what (no leak, no long-hold)."""
    conn = sqlite3.connect(f"file:{resolve_db_path()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Short-lived WRITE connection in an explicit transaction: commit on clean exit, rollback on any
    exception (no half-writes), always closed. Write paths must go through this."""
    conn = sqlite3.connect(resolve_db_path())
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
