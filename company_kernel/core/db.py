"""company_kernel.core.db — DB query primitives with NO dependency on companyctl or any domain.

First DB cut (admitted only after the connect/rows gate SHRANK it twice): only `rows()`, the one
truly pure leaf — it operates solely on the caller-supplied connection (no module globals, no config,
no mock-patch anchor, stable call shape across 165 call sites). DB_PATH / SCHEMA / the connection
singleton + connect_readonly / close_open_connections deliberately STAY in companyctl for now: they
are entangled with config loading (load_global_config / resolve_kernel_paths) and are mock-patch
anchors in the test suite, so they need a config-injection / single-source redesign before they can
move here without reverse-importing companyctl or breaking `mock.patch.object(companyctl, "DB_PATH")`.
"""
from __future__ import annotations

import sqlite3


def rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
