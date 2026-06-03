from __future__ import annotations

import sqlite3
import json
from datetime import datetime, timezone


MIGRATIONS = (
    {
        "id": "20260603_project_plan_items_task_id",
        "table": "project_plan_items",
        "column": "task_id",
        "sql": "ALTER TABLE project_plan_items ADD COLUMN task_id TEXT NOT NULL DEFAULT ''",
    },
    {
        "id": "20260603_adapter_runs_acknowledged_at",
        "table": "adapter_runs",
        "column": "acknowledged_at",
        "sql": "ALTER TABLE adapter_runs ADD COLUMN acknowledged_at TEXT NOT NULL DEFAULT ''",
    },
    {
        "id": "20260603_adapter_runs_task_id",
        "table": "adapter_runs",
        "column": "task_id",
        "sql": "ALTER TABLE adapter_runs ADD COLUMN task_id TEXT NOT NULL DEFAULT ''",
    },
    {
        "id": "20260603_adapter_runs_acknowledged_by",
        "table": "adapter_runs",
        "column": "acknowledged_by",
        "sql": "ALTER TABLE adapter_runs ADD COLUMN acknowledged_by TEXT NOT NULL DEFAULT ''",
    },
    {
        "id": "20260603_adapter_runs_acknowledgement_reason",
        "table": "adapter_runs",
        "column": "acknowledgement_reason",
        "sql": "ALTER TABLE adapter_runs ADD COLUMN acknowledgement_reason TEXT NOT NULL DEFAULT ''",
    },
    {
        "id": "20260603_adapter_runs_backfill_task_id",
        "table": "adapter_runs",
        "column": "task_id",
        "backfill": "adapter_runs.task_id",
    },
    {
        "id": "20260603_company_events_trace_id",
        "table": "company_events",
        "column": "trace_id",
        "sql": "ALTER TABLE company_events ADD COLUMN trace_id TEXT NOT NULL DEFAULT ''",
    },
    {
        "id": "20260603_adapter_runs_trace_id",
        "table": "adapter_runs",
        "column": "trace_id",
        "sql": "ALTER TABLE adapter_runs ADD COLUMN trace_id TEXT NOT NULL DEFAULT ''",
    },
    {
        "id": "20260603_adapter_runs_attempt",
        "table": "adapter_runs",
        "column": "attempt",
        "sql": "ALTER TABLE adapter_runs ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1",
    },
    {
        "id": "20260603_adapter_runs_next_retry_at",
        "table": "adapter_runs",
        "column": "next_retry_at",
        "sql": "ALTER TABLE adapter_runs ADD COLUMN next_retry_at TEXT NOT NULL DEFAULT ''",
    },
)


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def ensure_schema_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          id TEXT PRIMARY KEY,
          applied_at TEXT NOT NULL
        )
        """
    )
    for migration in MIGRATIONS:
        if conn.execute("SELECT 1 FROM schema_migrations WHERE id = ?", (migration["id"],)).fetchone():
            continue
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({migration['table']})").fetchall()}
        if not columns:
            continue
        if migration.get("sql") and migration["column"] not in columns:
            conn.execute(migration["sql"])
        if migration.get("backfill") == "adapter_runs.task_id":
            backfill_adapter_run_task_ids(conn)
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(id, applied_at) VALUES (?, ?)",
            (migration["id"], now()),
        )


def adapter_run_task_id_from_json(raw: str) -> str:
    try:
        result = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return ""
    if not isinstance(result, dict):
        return ""
    for run in result.get("runs", []):
        if isinstance(run, dict):
            parsed = run.get("parsed_stdout", {})
            if isinstance(parsed, dict) and parsed.get("task_id"):
                return str(parsed["task_id"])
    return ""


def backfill_adapter_run_task_ids(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT id, result_json FROM adapter_runs WHERE task_id = ''").fetchall()
    for row in rows:
        task_id = adapter_run_task_id_from_json(row["result_json"])
        if task_id:
            conn.execute("UPDATE adapter_runs SET task_id = ? WHERE id = ?", (task_id, row["id"]))
