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
    {
        "id": "20260607_v3_file_flow_tables",
        "table": "task_workspaces",
        "table_sql": """
CREATE TABLE IF NOT EXISTS task_workspaces (
  task_id TEXT PRIMARY KEY,
  trace_id TEXT NOT NULL DEFAULT '',
  path TEXT NOT NULL,
  manifest_path TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
)""",
    },
    {
        "id": "20260607_execution_attempts_runtime",
        "table": "execution_attempts",
        "column": "runtime",
        "sql": "ALTER TABLE execution_attempts ADD COLUMN runtime TEXT NOT NULL DEFAULT ''",
    },
    {
        "id": "20260607_execution_attempts_pid",
        "table": "execution_attempts",
        "column": "pid",
        "sql": "ALTER TABLE execution_attempts ADD COLUMN pid TEXT NOT NULL DEFAULT ''",
    },
    {
        "id": "20260607_execution_attempts_session_key",
        "table": "execution_attempts",
        "column": "session_key",
        "sql": "ALTER TABLE execution_attempts ADD COLUMN session_key TEXT NOT NULL DEFAULT ''",
    },
    {
        "id": "20260607_execution_attempts_runtime_policy_json",
        "table": "execution_attempts",
        "column": "runtime_policy_json",
        "sql": "ALTER TABLE execution_attempts ADD COLUMN runtime_policy_json TEXT NOT NULL DEFAULT '{}'",
    },
    {
        "id": "20260607_execution_attempts_last_heartbeat_at",
        "table": "execution_attempts",
        "column": "last_heartbeat_at",
        "sql": "ALTER TABLE execution_attempts ADD COLUMN last_heartbeat_at TEXT NOT NULL DEFAULT ''",
    },
    {
        "id": "20260607_execution_attempts_last_progress_at",
        "table": "execution_attempts",
        "column": "last_progress_at",
        "sql": "ALTER TABLE execution_attempts ADD COLUMN last_progress_at TEXT NOT NULL DEFAULT ''",
    },
    {
        "id": "20260607_execution_attempts_cancel_requested_at",
        "table": "execution_attempts",
        "column": "cancel_requested_at",
        "sql": "ALTER TABLE execution_attempts ADD COLUMN cancel_requested_at TEXT NOT NULL DEFAULT ''",
    },
    {
        "id": "20260607_execution_attempts_supervisor_state_json",
        "table": "execution_attempts",
        "column": "supervisor_state_json",
        "sql": "ALTER TABLE execution_attempts ADD COLUMN supervisor_state_json TEXT NOT NULL DEFAULT '{}'",
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
        if migration.get("table_sql"):
            conn.execute(migration["table_sql"])
            for table_sql in V3_FILE_FLOW_TABLES:
                conn.execute(table_sql)
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(id, applied_at) VALUES (?, ?)",
                (migration["id"], now()),
            )
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


V3_FILE_FLOW_TABLES = [
    """
CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id TEXT PRIMARY KEY,
  trace_id TEXT NOT NULL DEFAULT '',
  task_id TEXT NOT NULL,
  parent_task_id TEXT NOT NULL DEFAULT '',
  employee_id TEXT NOT NULL,
  artifact_type TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL,
  path TEXT NOT NULL,
  mime_type TEXT NOT NULL DEFAULT '',
  stage TEXT NOT NULL DEFAULT 'draft',
  version INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'created',
  is_input INTEGER NOT NULL DEFAULT 0,
  is_output INTEGER NOT NULL DEFAULT 1,
  is_final INTEGER NOT NULL DEFAULT 0,
  summary TEXT NOT NULL DEFAULT '',
  checksum TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
)""",
    """
CREATE TABLE IF NOT EXISTS evidence (
  evidence_id TEXT PRIMARY KEY,
  trace_id TEXT NOT NULL DEFAULT '',
  task_id TEXT NOT NULL,
  employee_id TEXT NOT NULL,
  artifact_id TEXT NOT NULL DEFAULT '',
  type TEXT NOT NULL DEFAULT '',
  path_or_url TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  checksum TEXT NOT NULL DEFAULT '',
  is_final INTEGER NOT NULL DEFAULT 1,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
)""",
    """
CREATE TABLE IF NOT EXISTS handoffs (
  handoff_id TEXT PRIMARY KEY,
  trace_id TEXT NOT NULL DEFAULT '',
  from_task_id TEXT NOT NULL,
  to_task_id TEXT NOT NULL,
  from_employee_id TEXT NOT NULL,
  to_employee_id TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  artifacts_json TEXT NOT NULL DEFAULT '[]',
  known_issues TEXT NOT NULL DEFAULT '',
  next_steps TEXT NOT NULL DEFAULT '',
  required_actions TEXT NOT NULL DEFAULT '',
  acceptance_notes TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'created',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
)""",
    """
CREATE TABLE IF NOT EXISTS execution_attempts (
  attempt_id TEXT PRIMARY KEY,
  trace_id TEXT NOT NULL DEFAULT '',
  task_id TEXT NOT NULL,
  employee_id TEXT NOT NULL,
  adapter_type TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'running',
  runtime TEXT NOT NULL DEFAULT '',
  pid TEXT NOT NULL DEFAULT '',
  session_key TEXT NOT NULL DEFAULT '',
  runtime_policy_json TEXT NOT NULL DEFAULT '{}',
  last_heartbeat_at TEXT NOT NULL DEFAULT '',
  last_progress_at TEXT NOT NULL DEFAULT '',
  cancel_requested_at TEXT NOT NULL DEFAULT '',
  supervisor_state_json TEXT NOT NULL DEFAULT '{}',
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL DEFAULT '',
  error_message TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}'
)""",
    """
CREATE TABLE IF NOT EXISTS task_context_packages (
  context_id TEXT PRIMARY KEY,
  trace_id TEXT NOT NULL DEFAULT '',
  task_id TEXT NOT NULL,
  employee_id TEXT NOT NULL,
  context_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
)""",
]
