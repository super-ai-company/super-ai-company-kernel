from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import plistlib
import re
import runpy
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from company_kernel import antigravity_adapter
from company_kernel import api_gateway
from company_kernel import api_grpc
from company_kernel import api_rpc
from company_kernel import adapter_worker
from company_kernel import claude_adapter
from company_kernel import company_daemon
from company_kernel import company_local_smoke
from company_kernel import company_dashboard
from company_kernel import company_service_smoke
from company_kernel import company_trace
from company_kernel import companyctl
from company_kernel import communication_acceptance
from company_kernel import codex_adapter
from company_kernel import codex_pm_supervisor
from company_kernel import db_paths
from company_kernel import hermes_adapter
from company_kernel import openclaw_adapter
from company_kernel import policy_guard
from company_kernel import sandboxing
from company_kernel import schema_migrations
from company_kernel import skill_package_worker
from company_kernel import trae_adapter


def run_cli(*args: str) -> tuple[int, dict]:
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = companyctl.main(list(args))
    finally:
        for conn in getattr(companyctl, "_TEST_OPEN_CONNECTIONS", []):
            conn.close()
        if hasattr(companyctl, "_TEST_OPEN_CONNECTIONS"):
            companyctl._TEST_OPEN_CONNECTIONS.clear()
    output = buf.getvalue().strip()
    return code, json.loads(output) if output else {}


class CompanyKernelCoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.root = root
        (root / "home" / "Library" / "LaunchAgents").mkdir(parents=True, exist_ok=True)
        (root / "company_kernel").mkdir()
        source_pkg = Path(__file__).resolve().parents[1] / "company_kernel"
        for source_file in source_pkg.glob("*.py"):
            (root / "company_kernel" / source_file.name).write_text(source_file.read_text(encoding="utf-8"), encoding="utf-8")
        (root / "company_kernel" / "schema.sql").write_text(companyctl.SCHEMA.read_text(encoding="utf-8"), encoding="utf-8")
        (root / "dashboard_templates").mkdir()
        source_templates = Path(__file__).resolve().parents[1] / "dashboard_templates"
        for source_file in source_templates.glob("*.html"):
            (root / "dashboard_templates" / source_file.name).write_text(source_file.read_text(encoding="utf-8"), encoding="utf-8")
        (root / "bin").mkdir()
        for executable in ["companyctl", "company-adapter-worker", "company-skill-package-worker", "company-codex-adapter", "company-openclaw-adapter", "company-trace", "company-api-rpc", "company-api-grpc", "company-service-smoke", "company-local-smoke"]:
            target = root / "bin" / executable
            target.write_text((Path(__file__).resolve().parents[1] / "bin" / executable).read_text(encoding="utf-8"), encoding="utf-8")
            target.chmod(0o755)
        (root / "config").mkdir()
        (root / "config" / "launchd").mkdir()
        (root / "config" / "launchd" / "ai.openclaw.company-kernel.daemon.plist").write_text(
            (Path(__file__).resolve().parents[1] / "config" / "launchd" / "ai.openclaw.company-kernel.daemon.plist").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (root / "config" / "company_communications.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "policy": {"mode": "open"},
                    "aliases": {"ops": "video-ops", "maker": "video-creator", "publisher": "video-publisher"},
                    "employees": {},
                    "channels": {},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (root / "config" / "hooks.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "hooks": [
                        {
                            "id": "maker-question-reply",
                            "enabled": True,
                            "match": {
                                "event_type": "message.send",
                                "source_agent": "maker",
                                "target_agent": "ops",
                                "body_contains": ["创作什么视频"],
                            },
                            "actions": [
                                {
                                    "type": "message",
                                    "from": "ops",
                                    "to": "maker",
                                    "body": "请创作搞笑中文竖版视频，消息 {{message_id}} 已收到。",
                                },
                                {"type": "heartbeat", "agent": "maker"},
                            ],
                        },
                        {
                            "id": "maker-conversation-question-reply",
                            "enabled": True,
                            "match": {
                                "event_type": "conversation.message",
                                "source_agent": "maker",
                                "target_agent": "ops",
                                "body_contains": ["创作什么视频"],
                            },
                            "actions": [
                                {
                                    "type": "conversation_reply",
                                    "from": "ops",
                                    "conversation_id": "{{conversation_id}}",
                                    "body": "会话 {{conversation_id}} 收到，请创作中文竖版视频。",
                                }
                            ],
                        },
                        {
                            "id": "maker-done-publish-approval",
                            "enabled": True,
                            "match": {"event_type": "task.done", "source_agent": "maker", "target_agent": "maker"},
                            "actions": [
                                {
                                    "type": "task_submit",
                                    "from": "ops",
                                    "to": "publisher",
                                    "requires_approval": "external_send",
                                    "pending_approval_id": "approval-publish-{{task_id}}",
                                    "approval_reason": "maker 完成 {{task_id}} 后需要发布审批",
                                    "title": "发布 maker 完成的视频",
                                    "description": "证据：{{evidence}}",
                                    "priority": "P1",
                                }
                            ],
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (root / "config" / "protected_paths.json").write_text(
            json.dumps(
                {
                    "requires_rfc": True,
                    "protected": ["company_kernel/**", "config/policy.json"],
                    "rfc_allowed": ["rfcs/**"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (root / "config" / "policy.json").write_text(json.dumps({"route_approval": {"actions": {}}}), encoding="utf-8")
        self.patchers = [
            mock.patch.object(company_daemon, "ROOT", root),
            mock.patch.object(company_daemon, "CONFIG_PATH", root / "config" / "daemon.json"),
            mock.patch.object(company_daemon, "STATE_DIR", root / "state" / "daemon"),
            mock.patch.object(company_daemon, "LOG_PATH", root / "logs" / "daemon.log"),
            mock.patch.object(company_dashboard, "ROOT", root),
            mock.patch.object(company_dashboard, "DB_PATH", root / "company.sqlite"),
            mock.patch.object(company_dashboard, "SCHEMA", root / "company_kernel" / "schema.sql"),
            mock.patch.object(company_dashboard, "DEFAULT_OUTPUT", root / "state" / "dashboard.html"),
            mock.patch.object(company_local_smoke, "ROOT", root),
            mock.patch.object(company_local_smoke, "STATE_DIR", root / "state" / "local-smoke"),
            mock.patch.object(company_local_smoke.company_service_smoke.api_gateway.companyctl, "ROOT", root),
            mock.patch.object(company_local_smoke.company_service_smoke.api_gateway.companyctl, "DB_PATH", root / "company.sqlite"),
            mock.patch.object(company_local_smoke.company_service_smoke.api_gateway.companyctl, "EMPLOYEES_DIR", root / "employees"),
            mock.patch.object(company_local_smoke.company_service_smoke.api_gateway.companyctl, "STATE_DIR", root / "state"),
            mock.patch.object(company_local_smoke.company_service_smoke.api_gateway.companyctl, "RFC_DIR", root / "rfcs"),
            mock.patch.object(company_local_smoke.company_service_smoke.api_gateway.companyctl, "CONFIG_DIR", root / "config"),
            mock.patch.object(company_local_smoke.company_service_smoke.api_gateway.companyctl, "WORKFLOW_DIR", root / "config" / "workflows"),
            mock.patch.object(company_local_smoke.company_service_smoke.api_gateway.companyctl, "LAUNCHD_TEMPLATE", root / "config" / "launchd" / "ai.openclaw.company-kernel.daemon.plist"),
            mock.patch.object(company_local_smoke.company_service_smoke.api_gateway.companyctl, "HOOKS_PATH", root / "config" / "hooks.json"),
            mock.patch.object(company_local_smoke.company_service_smoke.api_gateway.companyctl, "COMMUNICATIONS_PATH", root / "config" / "company_communications.json"),
            mock.patch.object(company_local_smoke.company_service_smoke.api_gateway.companyctl, "POLICY_PATH", root / "config" / "policy.json"),
            mock.patch.object(company_local_smoke.company_service_smoke.api_gateway.companyctl, "PROTECTED_PATHS_CONFIG", root / "config" / "protected_paths.json"),
            mock.patch.object(company_local_smoke.company_service_smoke.api_gateway.companyctl, "APPROVAL_STATE_DIR", root / "state" / "approvals"),
            mock.patch.object(company_local_smoke.company_service_smoke.api_gateway.companyctl, "SCHEMA", root / "company_kernel" / "schema.sql"),
            mock.patch.object(company_trace, "ROOT", root),
            mock.patch.object(company_trace, "DB_PATH", root / "company.sqlite"),
            mock.patch.object(company_trace, "SCHEMA", root / "company_kernel" / "schema.sql"),
            mock.patch.object(company_trace, "DEFAULT_OUTPUT_DIR", root / "state" / "traces"),
            mock.patch.object(company_service_smoke.api_gateway.companyctl, "ROOT", root),
            mock.patch.object(company_service_smoke.api_gateway.companyctl, "DB_PATH", root / "company.sqlite"),
            mock.patch.object(company_service_smoke.api_gateway.companyctl, "EMPLOYEES_DIR", root / "employees"),
            mock.patch.object(company_service_smoke.api_gateway.companyctl, "STATE_DIR", root / "state"),
            mock.patch.object(company_service_smoke.api_gateway.companyctl, "RFC_DIR", root / "rfcs"),
            mock.patch.object(company_service_smoke.api_gateway.companyctl, "CONFIG_DIR", root / "config"),
            mock.patch.object(company_service_smoke.api_gateway.companyctl, "WORKFLOW_DIR", root / "config" / "workflows"),
            mock.patch.object(company_service_smoke.api_gateway.companyctl, "LAUNCHD_TEMPLATE", root / "config" / "launchd" / "ai.openclaw.company-kernel.daemon.plist"),
            mock.patch.object(company_service_smoke.api_gateway.companyctl, "HOOKS_PATH", root / "config" / "hooks.json"),
            mock.patch.object(company_service_smoke.api_gateway.companyctl, "COMMUNICATIONS_PATH", root / "config" / "company_communications.json"),
            mock.patch.object(company_service_smoke.api_gateway.companyctl, "POLICY_PATH", root / "config" / "policy.json"),
            mock.patch.object(company_service_smoke.api_gateway.companyctl, "PROTECTED_PATHS_CONFIG", root / "config" / "protected_paths.json"),
            mock.patch.object(company_service_smoke.api_gateway.companyctl, "APPROVAL_STATE_DIR", root / "state" / "approvals"),
            mock.patch.object(company_service_smoke.api_gateway.companyctl, "SCHEMA", root / "company_kernel" / "schema.sql"),
            mock.patch.object(codex_adapter, "ROOT", root),
            mock.patch.object(codex_adapter, "DB_PATH", root / "company.sqlite"),
            mock.patch.object(codex_adapter, "DEFAULT_WORKSPACE", root / "workspace" / "codex"),
            mock.patch.object(hermes_adapter, "ROOT", root),
            mock.patch.object(hermes_adapter, "DB_PATH", root / "company.sqlite"),
            mock.patch.object(hermes_adapter, "DEFAULT_WORKSPACE", root / "workspace" / "hermes"),
            mock.patch.object(openclaw_adapter, "ROOT", root),
            mock.patch.object(openclaw_adapter, "DB_PATH", root / "company.sqlite"),
            mock.patch.object(openclaw_adapter, "OPENCLAW_ROOT", root / "openclaw"),
            mock.patch.object(antigravity_adapter, "ROOT", root),
            mock.patch.object(antigravity_adapter, "DB_PATH", root / "company.sqlite"),
            mock.patch.object(antigravity_adapter, "APP_PATH", root / "Applications" / "Antigravity.app"),
            mock.patch.object(policy_guard, "ROOT", root),
            mock.patch.object(policy_guard, "DB_PATH", root / "company.sqlite"),
            mock.patch.object(policy_guard, "SCHEMA", root / "company_kernel" / "schema.sql"),
            mock.patch.object(policy_guard, "POLICY_PATH", root / "config" / "policy.json"),
            mock.patch.object(policy_guard, "APPROVAL_STATE_DIR", root / "state" / "approvals"),
            mock.patch.object(api_gateway.companyctl, "ROOT", root),
            mock.patch.object(api_gateway.companyctl, "DB_PATH", root / "company.sqlite"),
            mock.patch.object(api_gateway.companyctl, "EMPLOYEES_DIR", root / "employees"),
            mock.patch.object(api_gateway.companyctl, "STATE_DIR", root / "state"),
            mock.patch.object(api_gateway.companyctl, "RFC_DIR", root / "rfcs"),
            mock.patch.object(api_gateway.companyctl, "CONFIG_DIR", root / "config"),
            mock.patch.object(api_gateway.companyctl, "WORKFLOW_DIR", root / "config" / "workflows"),
            mock.patch.object(api_gateway.companyctl, "LAUNCHD_TEMPLATE", root / "config" / "launchd" / "ai.openclaw.company-kernel.daemon.plist"),
            mock.patch.object(api_gateway.companyctl, "HOOKS_PATH", root / "config" / "hooks.json"),
            mock.patch.object(api_gateway.companyctl, "COMMUNICATIONS_PATH", root / "config" / "company_communications.json"),
            mock.patch.object(api_gateway.companyctl, "POLICY_PATH", root / "config" / "policy.json"),
            mock.patch.object(api_gateway.companyctl, "PROTECTED_PATHS_CONFIG", root / "config" / "protected_paths.json"),
            mock.patch.object(api_gateway.companyctl, "APPROVAL_STATE_DIR", root / "state" / "approvals"),
            mock.patch.object(api_gateway.companyctl, "SCHEMA", root / "company_kernel" / "schema.sql"),
            mock.patch.object(companyctl, "ROOT", root),
            mock.patch.object(companyctl, "DB_PATH", root / "company.sqlite"),
            mock.patch.object(companyctl, "EMPLOYEES_DIR", root / "employees"),
            mock.patch.object(companyctl, "STATE_DIR", root / "state"),
            mock.patch.object(companyctl, "RFC_DIR", root / "rfcs"),
            mock.patch.object(companyctl, "CONFIG_DIR", root / "config"),
            mock.patch.object(companyctl, "SKILL_PACKAGES_DIR", root / "skill-packages"),
            mock.patch.object(companyctl, "WORKFLOW_DIR", root / "config" / "workflows"),
            mock.patch.object(companyctl, "LAUNCHD_TEMPLATE", root / "config" / "launchd" / "ai.openclaw.company-kernel.daemon.plist"),
            mock.patch.object(companyctl, "HOOKS_PATH", root / "config" / "hooks.json"),
            mock.patch.object(companyctl, "COMMUNICATIONS_PATH", root / "config" / "company_communications.json"),
            mock.patch.object(companyctl, "POLICY_PATH", root / "config" / "policy.json"),
            mock.patch.object(companyctl, "PROTECTED_PATHS_CONFIG", root / "config" / "protected_paths.json"),
            mock.patch.object(companyctl, "APPROVAL_STATE_DIR", root / "state" / "approvals"),
            mock.patch.object(companyctl, "SCHEMA", root / "company_kernel" / "schema.sql"),
            mock.patch.dict("os.environ", {"HOME": str(root / "home"), "OPENCLAW_COMPANY_KERNEL_ROOT": str(root), "OPENCLAW_ROOT": str(root / "openclaw")}),
        ]
        for patcher in self.patchers:
            patcher.start()
        companyctl._TEST_OPEN_CONNECTIONS = []
        self._original_sqlite_connect = sqlite3.connect

        def tracked_sqlite_connect(*args, **kwargs):
            conn = self._original_sqlite_connect(*args, **kwargs)
            companyctl._TEST_OPEN_CONNECTIONS.append(conn)
            return conn

        self.sqlite_connect_patcher = mock.patch.object(sqlite3, "connect", tracked_sqlite_connect)
        self.sqlite_connect_patcher.start()

        def track_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
            return conn

        def tracked_connect() -> sqlite3.Connection:
            conn = sqlite3.connect(companyctl.DB_PATH)
            conn.row_factory = sqlite3.Row
            conn.executescript(companyctl.SCHEMA.read_text(encoding="utf-8"))
            companyctl.sync_backlog_from_queue_file(conn)
            conn.commit()
            return track_connection(conn)

        def wrap_connect(fn):
            def tracked_module_connect(*args, **kwargs):
                return track_connection(fn(*args, **kwargs))

            return tracked_module_connect

        self.connect_patcher = mock.patch.object(companyctl, "connect", tracked_connect)
        self.connect_patcher.start()
        self.module_connect_patchers = []
        self.fake_adapter_outputs: dict[str, dict] = {}
        for employee_id, role in [
            ("video-ops", "producer"),
            ("video-creator", "maker"),
            ("video-publisher", "publisher"),
            ("codex", "developer"),
            ("openclaw-main", "supervisor"),
            ("hermes", "supervisor"),
            ("nestcar", "business-agent"),
        ]:
            runtime = "hermes" if employee_id == "hermes" else "openclaw" if employee_id == "nestcar" else "codex" if employee_id == "codex" else "local"
            code, obj = run_cli(
                "employee",
                "create",
                "--id",
                employee_id,
                "--name",
                employee_id,
                "--role",
                role,
                "--runtime",
                runtime,
                "--workspace",
                str(root / "workspace" / employee_id),
            )
            self.assertEqual(code, 0, obj)
        self.mark_active("video-ops", "video-creator", "video-publisher", "codex", "openclaw-main", "hermes", "nestcar")

    def write_skill_manifest(self, skill_id: str = "ecommerce-copy-demo") -> Path:
        package_dir = self.root / "skill-packages" / skill_id
        package_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "id": skill_id,
            "name": "Ecommerce Copy Demo",
            "version": "0.1.0",
            "description": "Generate ecommerce listing copy inside the authorized task workspace.",
            "input_schema": {"type": "object", "properties": {"product_name": {"type": "string"}}},
            "output_schema": {"type": "object", "properties": {"summary_path": {"type": "string"}}, "required": ["summary_path"]},
            "runtime": {"type": "local-script", "command": "python3 run.py"},
            "permissions": {"workspace": "task", "network": False, "secrets": []},
            "pricing": {"unit": "task", "amount": 10, "currency": "USD"},
            "acceptance": {"final_artifact": "final/listing-summary.md", "evidence_required": True},
        }
        manifest_path = package_dir / "skill.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest_path

    def mark_active(self, *employee_ids: str) -> None:
        conn = companyctl.connect()
        try:
            for employee_id in employee_ids:
                conn.execute("UPDATE employees SET status = 'active' WHERE id = ?", (employee_id,))
            conn.commit()
        finally:
            conn.close()

    def tearDown(self) -> None:
        for patcher in reversed(getattr(self, "module_connect_patchers", [])):
            patcher.stop()
        self.connect_patcher.stop()
        for conn in getattr(companyctl, "_TEST_OPEN_CONNECTIONS", []):
            with contextlib.suppress(sqlite3.Error):
                conn.close()
        companyctl._TEST_OPEN_CONNECTIONS.clear()
        self.sqlite_connect_patcher.stop()
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tmp.cleanup()

    def test_schema_migrations_record_project_plan_task_id_upgrade(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(
                """
                CREATE TABLE project_plan_items (
                  id TEXT PRIMARY KEY,
                  project_id TEXT NOT NULL,
                  title TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'planned',
                  owner_agent TEXT NOT NULL DEFAULT '',
                  due_at TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE adapter_runs (
                  id TEXT PRIMARY KEY,
                  agent_id TEXT NOT NULL,
                  command TEXT NOT NULL DEFAULT '',
                  ok INTEGER NOT NULL DEFAULT 0,
                  processed INTEGER NOT NULL DEFAULT 0,
                  result_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE company_events (
                  id TEXT PRIMARY KEY,
                  event_type TEXT NOT NULL,
                  source_agent TEXT NOT NULL,
                  task_id TEXT NOT NULL DEFAULT '',
                  payload_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL,
                  processed_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                INSERT INTO adapter_runs(id, agent_id, command, ok, processed, result_json, created_at)
                VALUES (?, 'codex', 'company-adapter-worker', 1, 1, ?, '2026-06-03T05:00:00+07:00')
                """,
                (
                    "adapter-run-backfill-task",
                    json.dumps({"runs": [{"parsed_stdout": {"task_id": "task-backfilled-adapter-run"}}]}, ensure_ascii=False),
                ),
            )
            schema_migrations.ensure_schema_migrations(conn)
            schema_migrations.ensure_schema_migrations(conn)

            columns = {row["name"] for row in conn.execute("PRAGMA table_info(project_plan_items)").fetchall()}
            self.assertIn("task_id", columns)
            adapter_columns = {row["name"] for row in conn.execute("PRAGMA table_info(adapter_runs)").fetchall()}
            self.assertIn("task_id", adapter_columns)
            self.assertIn("acknowledged_at", adapter_columns)
            self.assertIn("acknowledged_by", adapter_columns)
            self.assertIn("acknowledgement_reason", adapter_columns)
            self.assertIn("trace_id", adapter_columns)
            self.assertIn("attempt", adapter_columns)
            self.assertIn("next_retry_at", adapter_columns)
            event_columns = {row["name"] for row in conn.execute("PRAGMA table_info(company_events)").fetchall()}
            self.assertIn("trace_id", event_columns)
            evidence_columns = {row["name"] for row in conn.execute("PRAGMA table_info(evidence)").fetchall()}
            self.assertIn("attempt_id", evidence_columns)
            runtime_session_columns = {row["name"] for row in conn.execute("PRAGMA table_info(runtime_sessions)").fetchall()}
            self.assertIn("session_id", runtime_session_columns)
            self.assertIn("attempt_id", runtime_session_columns)
            tool_call_columns = {row["name"] for row in conn.execute("PRAGMA table_info(agent_tool_calls)").fetchall()}
            self.assertIn("tool_call_id", tool_call_columns)
            self.assertIn("attempt_id", tool_call_columns)
            budget_event_columns = {row["name"] for row in conn.execute("PRAGMA table_info(budget_events)").fetchall()}
            self.assertIn("budget_event_id", budget_event_columns)
            self.assertIn("amount", budget_event_columns)
            self.assertIn("token_input", budget_event_columns)
            backfilled = conn.execute("SELECT task_id FROM adapter_runs WHERE id = 'adapter-run-backfill-task'").fetchone()
            self.assertEqual("task-backfilled-adapter-run", backfilled["task_id"])
            migrations = conn.execute("SELECT id FROM schema_migrations ORDER BY id").fetchall()
            self.assertEqual(
                [
                    "20260603_adapter_runs_acknowledged_at",
                    "20260603_adapter_runs_acknowledged_by",
                    "20260603_adapter_runs_acknowledgement_reason",
                    "20260603_adapter_runs_attempt",
                    "20260603_adapter_runs_backfill_task_id",
                    "20260603_adapter_runs_next_retry_at",
                    "20260603_adapter_runs_task_id",
                    "20260603_adapter_runs_trace_id",
                    "20260603_company_events_trace_id",
                    "20260603_project_plan_items_task_id",
                    "20260607_execution_attempts_cancel_requested_at",
                    "20260607_execution_attempts_last_heartbeat_at",
                    "20260607_execution_attempts_last_progress_at",
                    "20260607_execution_attempts_pid",
                    "20260607_execution_attempts_runtime",
                    "20260607_execution_attempts_runtime_policy_json",
                    "20260607_execution_attempts_session_key",
                    "20260607_execution_attempts_supervisor_state_json",
                    "20260607_v3_file_flow_tables",
                    "20260608_evidence_attempt_id",
                    "20260609_agent_tool_calls",
                    "20260609_budget_accounts",
                    "20260609_budget_events",
                    "20260609_runtime_sessions",
                ],
                [row["id"] for row in migrations],
            )
        finally:
            conn.close()

    def test_message_event_hook_replies_and_does_not_loop(self) -> None:
        code, sent = run_cli("message", "send", "--from", "maker", "--to", "ops", "--body", "我应该创作什么视频？", "--message-id", "msg-question-001")
        self.assertEqual(code, 0, sent)
        self.assertIn("event_id", sent)

        code, scheduled = run_cli("scheduler", "run", "--limit", "5")
        self.assertEqual(code, 0, scheduled)
        self.assertEqual(["maker-question-reply"], scheduled["events"][0]["matched_hooks"])
        self.assertEqual(2, len(scheduled["events"][0]["actions"]))

        code, drained = run_cli("scheduler", "run", "--limit", "5")
        self.assertEqual(code, 0, drained)
        self.assertEqual([], drained["events"][0]["matched_hooks"])

        code, messages = run_cli("message", "list", "--agent", "maker")
        self.assertEqual(code, 0, messages)
        bodies = [message["body"] for message in messages["messages"]]
        self.assertTrue(any("搞笑中文竖版视频" in body for body in bodies))

        code, pending = run_cli("scheduler", "events", "--pending")
        self.assertEqual(code, 0, pending)
        self.assertEqual([], pending["events"])

    def test_message_direct_uses_openclaw_session_key(self) -> None:
        code, created = run_cli("employee", "create", "--id", "main", "--name", "main", "--role", "operator", "--runtime", "local", "--workspace", str(self.root / "workspace" / "main"))
        self.assertEqual(0, code, created)
        code, created = run_cli("employee", "create", "--id", "nestcar", "--name", "NestCar", "--role", "business-agent", "--runtime", "openclaw", "--workspace", str(self.root / "workspace" / "nestcar"))
        self.assertEqual(0, code, created)
        calls = []

        def fake_run(cmd, cwd=None, text=None, capture_output=None, timeout=None):
            calls.append(cmd)

            class Result:
                returncode = 0
                stdout = json.dumps({"result": {"payloads": [{"text": "NESTCAR_DIRECT_OK"}]}})
                stderr = ""

            return Result()

        with mock.patch.object(companyctl.subprocess, "run", side_effect=fake_run):
            code, sent = run_cli(
                "message",
                "direct",
                "--from",
                "main",
                "--to",
                "nestcar",
                "--body",
                "只回复 NESTCAR_DIRECT_OK",
                "--message-id",
                "msg-direct-nestcar",
            )
        self.assertEqual(0, code, sent)
        self.assertEqual("NESTCAR_DIRECT_OK", sent["reply"])
        self.assertEqual("agent:nestcar:main", sent["session_key"])
        self.assertIn("--session-key", calls[0])
        self.assertIn("agent:nestcar:main", calls[0])
        self.assertEqual("nestcar", sent["message"]["target_agent"])

    def test_message_direct_maps_hermes_employee_to_default_runtime_session(self) -> None:
        code, created = run_cli("employee", "create", "--id", "main", "--name", "main", "--role", "operator", "--runtime", "openclaw", "--workspace", str(self.root / "workspace" / "main"))
        self.assertEqual(0, code, created)
        code, created = run_cli("employee", "create", "--id", "hermes", "--name", "Hermes", "--role", "supervisor", "--runtime", "hermes", "--workspace", str(self.root / "workspace" / "hermes"))
        self.assertEqual(0, code, created)
        calls = []

        def fake_run(cmd, cwd=None, text=None, capture_output=None, timeout=None):
            calls.append(cmd)

            class Result:
                returncode = 0
                stdout = json.dumps({"result": {"payloads": [{"text": "HERMES_DIRECT_OK"}]}})
                stderr = ""

            return Result()

        with mock.patch.object(companyctl.subprocess, "run", side_effect=fake_run):
            code, sent = run_cli(
                "message",
                "direct",
                "--from",
                "main",
                "--to",
                "hermes",
                "--body",
                "只回复 HERMES_DIRECT_OK",
                "--message-id",
                "msg-direct-hermes",
            )
        self.assertEqual(0, code, sent)
        self.assertEqual("HERMES_DIRECT_OK", sent["reply"])
        self.assertEqual("default", sent["agent_runtime_id"])
        self.assertEqual("agent:default:main", sent["session_key"])
        self.assertIn("--agent", calls[0])
        self.assertIn("default", calls[0])
        self.assertIn("agent:default:main", calls[0])
        self.assertNotIn("agent:hermes:main", calls[0])
        self.assertEqual("hermes", sent["message"]["target_agent"])

    def test_employee_name_is_high_priority_routing_identity(self) -> None:
        code, created = run_cli("employee", "create", "--id", "main", "--name", "main", "--role", "operator", "--runtime", "openclaw", "--workspace", str(self.root / "workspace" / "main"))
        self.assertEqual(0, code, created)
        code, created = run_cli("employee", "create", "--id", "hermes", "--name", "Hermes", "--role", "supervisor", "--runtime", "hermes", "--workspace", str(self.root / "workspace" / "hermes"))
        self.assertEqual(0, code, created)
        code, renamed = run_cli("employee", "update", "--id", "Hermes", "--name", "Hermes Supervisor")
        self.assertEqual(0, code, renamed)
        self.assertEqual("hermes", renamed["employee"]["id"])
        self.assertEqual({"Hermes Supervisor": "hermes", "hermes-supervisor": "hermes"}, renamed["communication"]["aliases"])

        code, shown = run_cli("employee", "show", "Hermes Supervisor")
        self.assertEqual(0, code, shown)
        self.assertEqual("hermes", shown["employee"]["id"])

        calls = []

        def fake_run(cmd, cwd=None, text=None, capture_output=None, timeout=None):
            calls.append(cmd)

            class Result:
                returncode = 0
                stdout = json.dumps({"result": {"payloads": [{"text": "RENAMED_HERMES_OK"}]}})
                stderr = ""

            return Result()

        with mock.patch.object(companyctl.subprocess, "run", side_effect=fake_run):
            code, sent = run_cli(
                "message",
                "direct",
                "--from",
                "main",
                "--to",
                "Hermes Supervisor",
                "--body",
                "只回复 RENAMED_HERMES_OK",
                "--message-id",
                "msg-direct-renamed-hermes",
            )
        self.assertEqual(0, code, sent)
        self.assertEqual("hermes", sent["target"])
        self.assertEqual("hermes", sent["message"]["target_agent"])

    def test_employee_activation_requires_verified_direct_rounds(self) -> None:
        code, created = run_cli("employee", "create", "--id", "main", "--name", "main", "--role", "operator", "--runtime", "openclaw", "--workspace", str(self.root / "workspace" / "main"))
        self.assertEqual(0, code, created)
        self.assertEqual("candidate", created["employee"].get("status", "candidate"))
        code, created = run_cli("employee", "create", "--id", "new-codex", "--name", "New Codex", "--role", "developer", "--runtime", "codex", "--workspace", str(self.root / "workspace" / "new-codex"))
        self.assertEqual(0, code, created)
        code, blocked = run_cli("employee", "update", "--id", "new-codex", "--status", "active")
        self.assertEqual(2, code, blocked)
        self.assertEqual("employee activation requires verified direct communication or structured runtime evidence", blocked["error"])

        code, verified = run_cli("employee", "verify-direct", "--id", "new-codex", "--from", "main", "--rounds", "2", "--activate")
        self.assertEqual(0, code, verified)
        self.assertTrue(verified["ok"])
        self.assertTrue(verified["activated"])
        self.assertEqual(2, verified["rounds_completed"])
        self.assertTrue(Path(verified["evidence"]["latest"]).exists())
        code, shown = run_cli("employee", "show", "new-codex")
        self.assertEqual(0, code, shown)
        self.assertEqual("active", shown["employee"]["status"])
        self.assertEqual("active", shown["profile"]["status"])

    def test_message_direct_uses_default_telegram_reply_bridge(self) -> None:
        code, created = run_cli("employee", "create", "--id", "main", "--name", "main", "--role", "operator", "--runtime", "openclaw", "--workspace", str(self.root / "workspace" / "main"))
        self.assertEqual(0, code, created)
        code, created = run_cli("employee", "create", "--id", "nestcar", "--name", "NestCar", "--role", "business-agent", "--runtime", "openclaw", "--workspace", str(self.root / "workspace" / "nestcar"))
        self.assertEqual(0, code, created)
        config = json.loads((self.root / "config" / "company_communications.json").read_text(encoding="utf-8"))
        config.setdefault("employees", {}).setdefault("nestcar", {}).update(
            {
                "default_user_reply_deliver": True,
                "default_user_reply_channel": "telegram",
                "default_user_reply_account": "default",
                "default_user_reply_to": "current",
            }
        )
        (self.root / "config" / "company_communications.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        calls = []

        def fake_run(cmd, cwd=None, text=None, capture_output=None, timeout=None):
            calls.append(cmd)

            class Result:
                returncode = 0
                stdout = json.dumps({"result": {"payloads": [{"text": "NESTCAR_DEFAULT_BRIDGE_OK"}]}})
                stderr = ""

            return Result()

        with mock.patch.object(companyctl.subprocess, "run", side_effect=fake_run):
            code, sent = run_cli(
                "message",
                "direct",
                "--from",
                "main",
                "--to",
                "nestcar",
                "--body",
                "只回复 NESTCAR_DEFAULT_BRIDGE_OK",
                "--message-id",
                "msg-direct-nestcar-default-bridge",
            )
        self.assertEqual(0, code, sent)
        self.assertTrue(sent["deliver"])
        self.assertEqual("telegram", sent["reply_channel"])
        self.assertEqual("default", sent["reply_account"])
        self.assertEqual("current", sent["reply_to"])
        self.assertIn("--deliver", calls[0])
        self.assertIn("--reply-channel", calls[0])
        self.assertIn("telegram", calls[0])
        self.assertIn("--reply-account", calls[0])
        self.assertIn("default", calls[0])
        self.assertIn("--reply-to", calls[0])
        self.assertIn("current", calls[0])

    def test_api_can_pause_and_resume_employee_communication(self) -> None:
        status, paused = api_gateway.route_post("/v1/employees/nestcar/communication", {"enabled": False})
        self.assertEqual(200, status, paused)
        self.assertTrue(paused["communication_paused"])

        decision = companyctl.communication_policy_decision("main", "nestcar", "message.send")
        self.assertFalse(decision["allowed"])
        self.assertEqual("target communication paused", decision["reason"])

        reverse_decision = companyctl.communication_policy_decision("nestcar", "main", "message.send")
        self.assertFalse(reverse_decision["allowed"])
        self.assertEqual("source communication paused", reverse_decision["reason"])

        status, resumed = api_gateway.route_post("/v1/employees/nestcar/communication", {"enabled": True})
        self.assertEqual(200, status, resumed)
        self.assertTrue(resumed["communication_enabled"])
        self.assertTrue(companyctl.communication_policy_decision("main", "nestcar", "message.send")["allowed"])

    def test_notification_settings_store_env_var_not_telegram_token(self) -> None:
        status, rejected = api_gateway.route_post(
            "/v1/settings/notification",
            {"telegram_account": "employee-notify", "bot_token": "123456:secret"},
        )
        self.assertEqual(HTTPStatus.BAD_REQUEST, status)
        self.assertIn("do not store Telegram bot token", rejected["error"])

        with mock.patch.dict("os.environ", {"COMPANY_EMPLOYEE_TELEGRAM_BOT_TOKEN": "123456:secret"}):
            status, saved = api_gateway.route_post(
                "/v1/settings/notification",
                {
                    "telegram_account": "employee-notify",
                    "telegram_bot_token_env": "COMPANY_EMPLOYEE_TELEGRAM_BOT_TOKEN",
                    "telegram_default_target": "telegram:shift",
                    "employee_notifications_enabled": "true",
                },
            )
            self.assertEqual(HTTPStatus.OK, status, saved)
            account = saved["telegram_accounts"]["employee-notify"]
            self.assertEqual("COMPANY_EMPLOYEE_TELEGRAM_BOT_TOKEN", account["bot_token_env"])
            self.assertTrue(account["token_configured"])
            self.assertNotIn("123456:secret", json.dumps(saved, ensure_ascii=False))

            status, loaded = api_gateway.route_get("/v1/settings/notification", {})
            self.assertEqual(HTTPStatus.OK, status, loaded)
            self.assertTrue(loaded["employee_notifications"]["enabled"])
            self.assertEqual("employee-notify", loaded["employee_notifications"]["account"])
            self.assertNotIn("123456:secret", json.dumps(loaded, ensure_ascii=False))

        config_text = (self.root / "config" / "company_communications.json").read_text(encoding="utf-8")
        self.assertIn("COMPANY_EMPLOYEE_TELEGRAM_BOT_TOKEN", config_text)
        self.assertNotIn("123456:secret", config_text)

    def test_company_kernel_db_path_env_overrides_repo_db(self) -> None:
        external_db = self.root / "global-state" / "company.sqlite"
        with mock.patch.dict("os.environ", {"COMPANY_KERNEL_DB_PATH": str(external_db)}):
            resolved = companyctl.resolve_db_path()
        self.assertEqual(external_db.resolve(), resolved)
        self.assertNotEqual((self.root / "company.sqlite").resolve(), resolved)
        resolved.parent.mkdir(parents=True, exist_ok=True)

        with mock.patch.object(companyctl, "DB_PATH", resolved):
            conn = companyctl.connect()
            try:
                conn.execute(
                    "INSERT INTO employees (id, name, role, runtime, workspace, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("global-db-agent", "global-db-agent", "tester", "local", str(self.root), "active", companyctl.now(), companyctl.now()),
                )
                conn.commit()
            finally:
                conn.close()

            readonly = companyctl.connect_readonly()
            try:
                row = readonly.execute("SELECT id FROM employees WHERE id = ?", ("global-db-agent",)).fetchone()
            finally:
                readonly.close()
        self.assertIsNotNone(row)
        self.assertTrue(external_db.exists())

    def test_antigravity_adapter_db_path_env_overrides_repo_db(self) -> None:
        external_db = self.root / "global-state" / "company.sqlite"
        with mock.patch.dict("os.environ", {"COMPANY_KERNEL_DB_PATH": str(external_db)}):
            resolved = antigravity_adapter.resolve_db_path()
        self.assertEqual(external_db.resolve(), resolved)
        self.assertNotEqual((self.root / "company.sqlite").resolve(), resolved)

    def test_runtime_modules_share_global_db_path_override(self) -> None:
        external_db = self.root / "global-state" / "company.sqlite"
        modules = [
            adapter_worker,
            claude_adapter,
            codex_adapter,
            codex_pm_supervisor,
            communication_acceptance,
            company_dashboard,
            hermes_adapter,
            openclaw_adapter,
            policy_guard,
            skill_package_worker,
            trae_adapter,
        ]
        with mock.patch.dict("os.environ", {"COMPANY_KERNEL_DB_PATH": str(external_db)}):
            for module in modules:
                if hasattr(module, "db_path"):
                    resolved = module.db_path()
                else:
                    resolved = module.resolve_db_path(module.ROOT)
                self.assertEqual(external_db.resolve(), resolved, module.__name__)

    def test_companyctl_loads_user_global_config_paths(self) -> None:
        config_path = self.root / ".gemini" / "antigravity" / "company_kernel_config.json"
        master_root = self.root / "master-workspace"
        payload = {
            "database_path": str(self.root / "global-state" / "company.sqlite"),
            "master_workspace_root": str(master_root),
            "log_dir": str(self.root / "global-logs"),
            "gateway_port": 8799,
        }
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with mock.patch.dict("os.environ", {"COMPANY_KERNEL_CONFIG_PATH": str(config_path)}, clear=False):
            loaded = companyctl.load_global_config()
            paths = companyctl.resolve_kernel_paths(self.root)
        self.assertEqual(Path(payload["database_path"]).resolve(), paths["db_path"])
        self.assertEqual(master_root.resolve(), paths["root"])
        self.assertEqual((master_root / "employees").resolve(), paths["employees_dir"])
        self.assertEqual(Path(payload["log_dir"]).resolve(), paths["log_dir"])
        self.assertEqual(8799, loaded["gateway_port"])

    def test_db_paths_loads_user_global_config_database_path(self) -> None:
        config_path = self.root / ".gemini" / "antigravity" / "company_kernel_config.json"
        global_db = self.root / "global-state" / "company.sqlite"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps({"database_path": str(global_db)}, ensure_ascii=False), encoding="utf-8")

        with mock.patch.dict("os.environ", {"COMPANY_KERNEL_CONFIG_PATH": str(config_path)}, clear=False):
            resolved = db_paths.resolve_db_path(self.root)

        self.assertEqual(str(global_db.resolve()), str(resolved.resolve()))

    def test_notification_send_uses_env_token_and_returns_message_id(self) -> None:
        status, saved = api_gateway.route_post(
            "/v1/settings/notification",
            {
                "telegram_account": "employee-notify",
                "telegram_bot_token_env": "COMPANY_EMPLOYEE_TELEGRAM_BOT_TOKEN",
                "telegram_default_target": "telegram:<operator-chat-id>",
                "employee_notifications_enabled": "true",
            },
        )
        self.assertEqual(HTTPStatus.OK, status, saved)
        requests = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"ok": True, "result": {"message_id": 115, "chat": {"id": 123456789}}}).encode("utf-8")

        def fake_urlopen(request, timeout=None):
            requests.append(request)
            return FakeResponse()

        with mock.patch.dict("os.environ", {"COMPANY_EMPLOYEE_TELEGRAM_BOT_TOKEN": "123456:secret"}), mock.patch.object(companyctl.urllib.request, "urlopen", side_effect=fake_urlopen):
            code, sent = run_cli("notification", "send", "--kind", "error", "--message", "notify smoke")
        self.assertEqual(0, code, sent)
        self.assertEqual("115", str(sent["message_id"]))
        self.assertEqual("error", sent["kind"])
        self.assertEqual("telegram:<operator-chat-id>", sent["target"])
        self.assertNotIn("123456:secret", json.dumps(sent, ensure_ascii=False))
        self.assertIn("123456:secret", requests[0].full_url)
        self.assertNotIn("123456:secret", (self.root / "config" / "company_communications.json").read_text(encoding="utf-8"))

    def test_notification_send_api_supports_error_dry_run(self) -> None:
        status, saved = api_gateway.route_post(
            "/v1/settings/notification",
            {
                "telegram_account": "employee-notify",
                "telegram_bot_token_env": "COMPANY_EMPLOYEE_TELEGRAM_BOT_TOKEN",
                "telegram_default_target": "telegram:<operator-chat-id>",
                "employee_notifications_enabled": "true",
            },
        )
        self.assertEqual(HTTPStatus.OK, status, saved)
        status, sent = api_gateway.route_post("/v1/notifications/send", {"kind": "error", "subject": "Smoke error", "message": "adapter failed", "dry_run": True})
        self.assertEqual(HTTPStatus.OK, status, sent)
        self.assertTrue(sent["dry_run"])
        self.assertEqual("error", sent["kind"])
        self.assertEqual("telegram:<operator-chat-id>", sent["target"])

    def test_notification_send_supports_macos_without_telegram_account(self) -> None:
        calls = []

        def fake_macos_notification(**kwargs):
            calls.append(kwargs)
            return {"ok": True, "platform": "macos", "message_id": "mocked"}

        with mock.patch.object(companyctl, "send_macos_notification", side_effect=fake_macos_notification):
            code, sent = run_cli("notification", "send", "--target", "macos", "--kind", "error", "--subject", "Agent stalled", "--message", "codex stalled")
        self.assertEqual(0, code, sent)
        self.assertEqual("macos", sent["platform"])
        self.assertEqual("macos:default", sent["target"])
        self.assertEqual("mocked", sent["message_id"])
        self.assertEqual("Agent stalled\ncodex stalled", calls[0]["text"])

    def test_notification_dispatcher_routes_macos_slack_and_telegram(self) -> None:
        calls = {"macos": [], "telegram": [], "slack": []}

        def fake_macos(**kwargs):
            calls["macos"].append(kwargs)
            return {"ok": True, "platform": "macos", "message_id": "macos-ok"}

        def fake_telegram(**kwargs):
            calls["telegram"].append(kwargs)
            return {"ok": True, "platform": "telegram", "message_id": 217}

        def fake_slack(webhook_url: str, payload: dict):
            calls["slack"].append({"webhook_url": webhook_url, "payload": payload})
            return {"ok": True, "platform": "slack", "message_id": "slack-ok"}

        dispatcher = companyctl.NotificationDispatcher(
            {
                "telegram_accounts": {"ops": {"bot_token_env": "TELEGRAM_TOKEN"}},
                "slack_webhooks": {"ops": {"webhook_url_env": "SLACK_WEBHOOK"}},
            }
        )
        with (
            mock.patch.object(companyctl, "send_macos_notification", side_effect=fake_macos),
            mock.patch.object(companyctl, "send_telegram_notification", side_effect=fake_telegram),
            mock.patch.object(companyctl, "send_slack_webhook", side_effect=fake_slack),
            mock.patch.dict("os.environ", {"TELEGRAM_TOKEN": "telegram-secret", "SLACK_WEBHOOK": "https://hooks.example/secret"}),
        ):
            macos = dispatcher.send("macos:default", title="Alert", body="body", kind="error")
            telegram = dispatcher.send("telegram:12345", title="Alert", body="body", kind="error", account_id="ops")
            slack = dispatcher.send("slack:ops", title="Alert", body="body", kind="error")
        self.assertEqual("macos-ok", macos["message_id"])
        self.assertEqual(217, telegram["message_id"])
        self.assertEqual("slack-ok", slack["message_id"])
        self.assertEqual("telegram-secret", calls["telegram"][0]["token"])
        self.assertEqual("https://hooks.example/secret", calls["slack"][0]["webhook_url"])

    def test_macos_notification_uses_applescript_safe_unicode_quote(self) -> None:
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return mock.Mock(returncode=0)

        with mock.patch.object(companyctl.subprocess, "run", side_effect=fake_run):
            result = companyctl.send_macos_notification(text='本机通知 "ok"', title="Company Kernel", subtitle="error")
        self.assertTrue(result["ok"])
        script = calls[0][0][-1]
        self.assertIn('display notification "本机通知 \\"ok\\""', script)
        self.assertIn('with title "Company Kernel"', script)
        self.assertIn('subtitle "error"', script)

    def test_approval_request_notifies_operator_route(self) -> None:
        code, created = run_cli("employee", "create", "--id", "main", "--name", "main", "--role", "operator", "--runtime", "openclaw", "--workspace", str(self.root / "workspace" / "main"))
        self.assertEqual(0, code, created)
        api_gateway.route_post(
            "/v1/settings/notification",
            {
                "telegram_account": "employee-notify",
                "telegram_bot_token_env": "COMPANY_EMPLOYEE_TELEGRAM_BOT_TOKEN",
                "telegram_default_target": "telegram:<operator-chat-id>",
                "employee_notifications_enabled": "true",
            },
        )

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"ok": True, "result": {"message_id": 116, "chat": {"id": 123456789}}}).encode("utf-8")

        with mock.patch.dict("os.environ", {"COMPANY_EMPLOYEE_TELEGRAM_BOT_TOKEN": "123456:secret"}), mock.patch.object(companyctl.urllib.request, "urlopen", return_value=FakeResponse()):
            code, approval = run_cli("approval", "request", "--from", "main", "--action", "external_send", "--reason", "publish requires human approval")
        self.assertEqual(0, code, approval)
        self.assertEqual("approval", approval["notification"]["kind"])
        self.assertEqual("116", str(approval["notification"]["message_id"]))

    def test_tool_policy_block_report_explains_no_popup_and_notifies_error_route(self) -> None:
        code, created = run_cli("employee", "create", "--id", "openclaw-main", "--name", "OpenClaw Main", "--role", "operator", "--runtime", "openclaw", "--workspace", str(self.root / "workspace" / "main"))
        self.assertEqual(0, code, created)
        code, created = run_cli("employee", "create", "--id", "codex", "--name", "Codex", "--role", "developer", "--runtime", "codex", "--workspace", str(self.root / "workspace" / "codex"))
        self.assertEqual(0, code, created)
        api_gateway.route_post(
            "/v1/settings/notification",
            {
                "telegram_account": "employee-notify",
                "telegram_bot_token_env": "COMPANY_EMPLOYEE_TELEGRAM_BOT_TOKEN",
                "telegram_default_target": "telegram:<operator-chat-id>",
                "employee_notifications_enabled": "true",
            },
        )
        code, report = run_cli(
            "policy",
            "block-report",
            "--source",
            "openclaw-main",
            "--target",
            "codex",
            "--tool",
            "sessions_send",
            "--operation",
            "agent-to-agent-message",
            "--error",
            "Agent-to-agent messaging denied by tools.agentToAgent.allow.",
            "--dry-run",
        )
        self.assertEqual(0, code, report)
        self.assertFalse(report["ok"])
        self.assertEqual("agent_to_agent_denied", report["classification"]["type"])
        self.assertEqual("not_user_popup_approvable", report["classification"]["approval_semantics"])
        self.assertIn("companyctl message direct", report["classification"]["replacement"])
        self.assertTrue(report["notification"]["dry_run"])
        self.assertEqual("error", report["notification"]["kind"])
        self.assertTrue(Path(report["evidence"]).exists())

    def test_policy_block_report_api_classifies_sessions_spawn_denial(self) -> None:
        status, report = api_gateway.route_post(
            "/v1/policy-blocks/report",
            {
                "source": "openclaw-main",
                "target": "codex",
                "tool": "sessions_spawn",
                "operation": "spawn-agent-session",
                "error": "agentId is not allowed for sessions_spawn (allowed: main)",
                "dry_run": True,
            },
        )
        self.assertEqual(HTTPStatus.OK, status, report)
        self.assertEqual("session_spawn_denied", report["classification"]["type"])
        self.assertEqual("not_user_popup_approvable", report["classification"]["approval_semantics"])

    def test_human_owner_can_use_real_conversation_api_without_being_schedulable(self) -> None:
        code, codex = run_cli("employee", "create", "--id", "codex", "--name", "Codex", "--role", "developer", "--runtime", "codex", "--workspace", str(self.root / "workspace" / "codex"))
        self.assertEqual(0, code, codex)
        code, trae = run_cli("employee", "create", "--id", "trae", "--name", "Trae", "--role", "developer", "--runtime", "trae", "--workspace", str(self.root / "workspace" / "trae"))
        self.assertEqual(0, code, trae)

        status, started = api_gateway.route_post(
            "/v1/conversations",
            {
                "from": "owner-shift",
                "participants": "owner-shift,codex,trae",
                "conversation_id": "conv-owner-group",
                "title": "后台群聊测试",
                "body": "请 Codex 和 Trae 一起确认方案",
            },
        )
        self.assertEqual(201, status, started)
        self.assertEqual(["owner-shift", "codex", "trae"], started["conversation"]["participants"])

        status, replied = api_gateway.route_post(
            "/v1/conversations/conv-owner-group/reply",
            {"from": "owner-shift", "body": "补充一条真实回复", "message_id": "msg-owner-reply"},
        )
        self.assertEqual(201, status, replied)

        status, shown = api_gateway.route_get("/v1/conversations/conv-owner-group", {})
        self.assertEqual(200, status)
        self.assertEqual(["请 Codex 和 Trae 一起确认方案", "补充一条真实回复"], [message["body"] for message in shown["messages"]])

        status, agent_started = api_gateway.route_post(
            "/v1/conversations",
            {
                "from": "codex",
                "participants": "codex,trae",
                "conversation_id": "conv-agent-agent",
                "title": "agent pair discussion",
                "body": "agent-only message",
            },
        )
        self.assertEqual(201, status, agent_started)
        self.assertEqual(["codex", "trae"], agent_started["conversation"]["participants"])

        status, joined = api_gateway.route_post("/v1/conversations/conv-agent-agent/join", {"agent": "owner-shift"})
        self.assertEqual(200, status, joined)
        self.assertTrue(joined["joined"])
        self.assertEqual(["owner-shift", "codex", "trae"], joined["conversation"]["participants"])

        status, owner_reply = api_gateway.route_post(
            "/v1/conversations/conv-agent-agent/reply",
            {"from": "owner-shift", "body": "管理员插入会话", "message_id": "msg-owner-inserted"},
        )
        self.assertEqual(201, status, owner_reply)

        status, employees = api_gateway.route_get("/v1/employees", {})
        self.assertEqual(200, status)
        self.assertNotIn("owner-shift", [employee["id"] for employee in employees["employees"]])

        status, matches = api_gateway.route_post("/v1/employees/match", {"include_unavailable": "true"})
        self.assertEqual(200, status)
        self.assertNotIn("owner-shift", [match["agent"] for match in matches["matches"]])

    def test_followup_request_and_reply_resume_direct_delivery(self) -> None:
        code, created = run_cli("employee", "create", "--id", "main", "--name", "main", "--role", "operator", "--runtime", "openclaw", "--workspace", str(self.root / "workspace" / "main"))
        self.assertEqual(0, code, created)
        code, created = run_cli("employee", "create", "--id", "nestcar", "--name", "NestCar", "--role", "business-agent", "--runtime", "openclaw", "--workspace", str(self.root / "workspace" / "nestcar"))
        self.assertEqual(0, code, created)
        calls = []

        def fake_run(cmd, cwd=None, text=None, capture_output=None, timeout=None):
            calls.append(cmd)

            class Result:
                returncode = 0
                stdout = json.dumps({"result": {"payloads": [{"text": "FOLLOWUP_DELIVERED"}]}})
                stderr = ""

            return Result()

        with mock.patch.object(companyctl.subprocess, "run", side_effect=fake_run):
            code, requested = run_cli(
                "followup",
                "request",
                "--from",
                "nestcar",
                "--to",
                "main",
                "--question",
                "请补充车牌号",
                "--followup-id",
                "followup-nestcar-plate",
            )
            self.assertEqual(0, code, requested)
            code, answered = run_cli(
                "followup",
                "reply",
                "--followup-id",
                "followup-nestcar-plate",
                "--by",
                "main",
                "--answer",
                "车牌号是 ABC-123",
            )
        self.assertEqual(0, code, answered)
        self.assertEqual("answered", answered["followup"]["status"])
        self.assertEqual("车牌号是 ABC-123", answered["followup"]["answer"])
        self.assertEqual("nestcar", answered["delivery"]["target"])
        self.assertTrue(Path(answered["file"]).exists())
        self.assertIn("车牌号是 ABC-123", json.dumps(answered, ensure_ascii=False))

    def test_message_direct_uses_codex_adapter_without_claiming_tasks(self) -> None:
        code, created = run_cli("employee", "create", "--id", "main", "--name", "main", "--role", "operator", "--runtime", "openclaw", "--workspace", str(self.root / "workspace" / "main"))
        self.assertEqual(0, code, created)
        code, created = run_cli("employee", "create", "--id", "codex", "--name", "Codex", "--role", "developer", "--runtime", "codex", "--workspace", str(self.root / "workspace" / "codex"))
        self.assertEqual(0, code, created)
        calls = []

        def fake_run(cmd, cwd=None, text=None, capture_output=None, timeout=None):
            calls.append(cmd)

            class Result:
                returncode = 0
                stdout = json.dumps({"ok": True, "agent": "codex", "direct_message": True, "reply": "CODEX_DIRECT_OK", "progress_report": str(self.root / "employees" / "codex" / "reports" / "direct" / "progress_acknowledged_test.json")})
                stderr = ""

            return Result()

        with mock.patch.object(companyctl.subprocess, "run", side_effect=fake_run):
            code, sent = run_cli(
                "message",
                "direct",
                "--from",
                "main",
                "--to",
                "codex",
                "--body",
                "只回复 CODEX_DIRECT_OK",
                "--message-id",
                "msg-direct-codex",
            )
        self.assertEqual(0, code, sent)
        self.assertEqual("CODEX_DIRECT_OK", sent["reply"])
        self.assertEqual("agent:codex:main", sent["session_key"])
        self.assertEqual("codex", sent["receipt"]["source_agent"])
        self.assertEqual("main", sent["receipt"]["target_agent"])
        self.assertEqual("CODEX_DIRECT_OK", sent["receipt"]["body"])
        self.assertTrue(Path(sent["receipt_file"]).exists())
        self.assertIn("company-codex-adapter", calls[0][0])
        self.assertIn("--direct-message", calls[0])
        self.assertIn("--direct-source", calls[0])
        self.assertIn("--direct-session-key", calls[0])
        self.assertEqual("codex", sent["message"]["target_agent"])
        with sqlite3.connect(self.root / "company.sqlite") as conn:
            conn.row_factory = sqlite3.Row
            direct_events = conn.execute("SELECT event_type, processed_at FROM company_events WHERE event_type = 'message.send' ORDER BY created_at").fetchall()
        self.assertEqual(2, len(direct_events))
        self.assertTrue(all(row["processed_at"] for row in direct_events))

        code, normal = run_cli("message", "send", "--from", "main", "--to", "codex", "--body", "普通消息")
        self.assertEqual(0, code, normal)
        with sqlite3.connect(self.root / "company.sqlite") as conn:
            pending = conn.execute("SELECT COUNT(*) FROM company_events WHERE event_type = 'message.send' AND processed_at = ''").fetchone()[0]
        self.assertEqual(1, pending)

    def test_codex_direct_message_writes_progress_report(self) -> None:
        code, created = run_cli(
            "employee",
            "create",
            "--id",
            "main",
            "--name",
            "main",
            "--role",
            "operator",
            "--runtime",
            "openclaw",
            "--workspace",
            str(self.root / "workspace" / "main"),
        )
        self.assertEqual(code, 0, created)
        code, created = run_cli(
            "employee",
            "create",
            "--id",
            "codex",
            "--name",
            "Codex",
            "--role",
            "developer",
            "--runtime",
            "codex",
            "--workspace",
            str(self.root / "workspace" / "codex"),
        )
        self.assertEqual(code, 0, created)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = codex_adapter.main(["--agent", "codex", "--direct-source", "main", "--direct-session-key", "agent:codex:main", "--direct-message", "只回复 CODEX_PROGRESS_OK"])
        self.assertEqual(0, code)
        payload = json.loads(buf.getvalue())
        self.assertEqual("CODEX_PROGRESS_OK", payload["reply"])
        report = Path(payload["progress_report"])
        self.assertTrue(report.exists())
        report_payload = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual("acknowledged", report_payload["state"])
        self.assertEqual("main", report_payload["source"])

    def test_progress_report_helper_cli_writes_expected_json(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "progress_report.py"
        out_dir = self.root / "workspace" / "codex" / "reports"
        argv = [
            str(script),
            "--state",
            "completed",
            "--project",
            "codex",
            "--action",
            "completed direct task",
            "--checking",
            "python3 -m unittest OK",
            "--risks",
            "none",
            "--task-id",
            "task-progress-helper-001",
            "--out-dir",
            str(out_dir),
        ]
        stdout = io.StringIO()
        old_argv = sys.argv[:]
        with contextlib.redirect_stdout(stdout):
            try:
                sys.argv = argv
                runpy.run_path(str(script), run_name="__main__")
            except SystemExit as exc:
                self.assertEqual(0, exc.code)
            finally:
                sys.argv = old_argv
        result = json.loads(stdout.getvalue())
        report = Path(result["path"])
        self.assertTrue(report.exists())
        payload = json.loads(report.read_text(encoding="utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual("task-progress-helper-001", payload["task_id"])
        self.assertEqual("completed", payload["report"]["state"])
        self.assertEqual("codex", payload["report"]["project"])
        self.assertEqual("completed direct task", payload["report"]["action"])
        self.assertEqual("python3 -m unittest OK", payload["report"]["checking"])
        self.assertEqual("none", payload["report"]["risks"])
        self.assertEqual(str(out_dir.parent.resolve()), payload["report"]["targets"])
        self.assertIn("task-progress-helper-001", report.name)

    def test_codex_direct_execution_runs_worker_and_writes_repo_progress(self) -> None:
        workspace = self.root / "workspace" / "codex"
        (workspace / "scripts").mkdir(parents=True, exist_ok=True)
        helper = Path(__file__).resolve().parents[1] / "scripts" / "progress_report.py"
        (workspace / "scripts" / "progress_report.py").write_text(helper.read_text(encoding="utf-8"), encoding="utf-8")
        code, created = run_cli(
            "employee",
            "create",
            "--id",
            "main",
            "--name",
            "main",
            "--role",
            "operator",
            "--runtime",
            "openclaw",
            "--workspace",
            str(self.root / "workspace" / "main"),
        )
        self.assertEqual(code, 0, created)
        code, created = run_cli(
            "employee",
            "create",
            "--id",
            "codex",
            "--name",
            "Codex",
            "--role",
            "developer",
            "--runtime",
            "codex",
            "--workspace",
            str(workspace),
        )
        self.assertEqual(code, 0, created)

        calls: list[dict] = []

        def fake_run_codex(task_card: Path, workspace_arg: Path, output: Path, events: Path, sandbox: str, model: str, isolation: str, sandbox_profile: str) -> tuple[int, str]:
            calls.append({"task_card": task_card, "workspace": workspace_arg, "sandbox": sandbox})
            task_text = task_card.read_text(encoding="utf-8")
            self.assertIn("Mandatory communication loop", task_text)
            self.assertIn("Required final output", task_text)
            old_cwd = Path.cwd()
            old_argv = sys.argv[:]
            os.chdir(workspace_arg)
            try:
                sys.argv = [
                    "progress_report.py",
                    "--state",
                    "completed",
                    "--project",
                    workspace_arg.name,
                    "--action",
                    "completed direct task",
                    "--checking",
                    "worker wrote progress helper output",
                    "--out-dir",
                    "reports",
                ]
                with contextlib.redirect_stdout(io.StringIO()):
                    runpy.run_path(str(workspace_arg / "scripts" / "progress_report.py"), run_name="__main__")
            except SystemExit as exc:
                self.assertEqual(0, exc.code)
            finally:
                sys.argv = old_argv
                os.chdir(old_cwd)
            output.write_text(
                "\n".join(
                    [
                        "status: done",
                        "current_action: implemented requested fix",
                        "changed_files: README.md",
                        "verification_run: python3 -m unittest discover -s tests -v OK",
                        "blocker: -",
                        "eta: -",
                    ]
                ),
                encoding="utf-8",
            )
            events.write_text(json.dumps({"event": "done"}, ensure_ascii=False) + "\n", encoding="utf-8")
            return 0, f"codex exec -C {workspace_arg} -s {sandbox}"

        captured = io.StringIO()
        with mock.patch.object(codex_adapter.shutil, "which", lambda name: "/usr/local/bin/codex"), mock.patch.object(codex_adapter, "run_codex", fake_run_codex), contextlib.redirect_stdout(captured):
            code = codex_adapter.main(
                [
                    "--agent",
                    "codex",
                    "--direct-source",
                    "main",
                    "--direct-session-key",
                    "agent:codex:main",
                    "--direct-message",
                    "请在这个 repo 修复 openclaw 控制 codex 的闭环，并运行测试。",
                ]
            )
        payload = json.loads(captured.getvalue())
        self.assertEqual(0, code, payload)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual("execution", payload["direct_mode"])
        self.assertEqual(1, payload["processed"])
        self.assertIn("status: done", payload["reply"])
        self.assertIn("changed_files: README.md", payload["reply"])
        self.assertEqual("workspace-write", calls[0]["sandbox"])
        workspace_reports = sorted((workspace / "reports").glob("progress_*.json"))
        self.assertGreaterEqual(len(workspace_reports), 3)
        states = [json.loads(path.read_text(encoding="utf-8"))["report"]["state"] for path in workspace_reports]
        self.assertIn("acknowledged", states)
        self.assertIn("in_progress", states)
        self.assertIn("completed", states)
        self.assertTrue(payload["working_delivery"]["ok"], payload)
        code, main_messages = run_cli("message", "list", "--agent", "main")
        self.assertEqual(0, code, main_messages)
        self.assertTrue(any(message["source_agent"] == "codex" and "status: working" in message["body"] for message in main_messages["messages"]))
        adapter_report = Path(payload["progress_report"])
        self.assertTrue(adapter_report.exists())
        adapter_payload = json.loads(adapter_report.read_text(encoding="utf-8"))
        self.assertEqual("completed", adapter_payload["state"])

    def test_message_direct_uses_antigravity_adapter_and_returns_receipt(self) -> None:
        code, created = run_cli("employee", "create", "--id", "main", "--name", "main", "--role", "operator", "--runtime", "openclaw", "--workspace", str(self.root / "workspace" / "main"))
        self.assertEqual(0, code, created)
        code, created = run_cli("employee", "create", "--id", "antigravity", "--name", "Antigravity", "--role", "developer", "--runtime", "antigravity", "--workspace", str(self.root / "workspace" / "antigravity"))
        self.assertEqual(0, code, created)
        calls = []

        def fake_run(cmd, cwd=None, text=None, capture_output=None, timeout=None):
            calls.append(cmd)

            class Result:
                returncode = 0
                stdout = json.dumps({"ok": True, "agent": "antigravity", "direct_message": True, "reply": "ANTIGRAVITY_DIRECT_OK", "activation_eligible": False})
                stderr = ""

            return Result()

        with mock.patch.object(companyctl.subprocess, "run", side_effect=fake_run):
            code, sent = run_cli(
                "message",
                "direct",
                "--from",
                "main",
                "--to",
                "Antigravity",
                "--body",
                "请查看每个页面并给出前端优化。只回复 ANTIGRAVITY_DIRECT_OK",
                "--message-id",
                "msg-direct-antigravity",
            )
        self.assertEqual(0, code, sent)
        self.assertEqual("ANTIGRAVITY_DIRECT_OK", sent["reply"])
        self.assertFalse(sent["activation_eligible"])
        self.assertEqual("agent:antigravity:main", sent["session_key"])
        self.assertEqual("antigravity", sent["receipt"]["source_agent"])
        self.assertEqual("main", sent["receipt"]["target_agent"])
        self.assertTrue(Path(sent["receipt_file"]).exists())
        self.assertIn("company-antigravity-adapter", calls[0][0])

    def test_antigravity_brief_ack_cannot_activate_employee(self) -> None:
        code, created = run_cli("employee", "create", "--id", "main", "--name", "main", "--role", "operator", "--runtime", "openclaw", "--workspace", str(self.root / "workspace" / "main"))
        self.assertEqual(0, code, created)
        code, created = run_cli("employee", "create", "--id", "antigravity", "--name", "Antigravity", "--role", "developer", "--runtime", "antigravity", "--workspace", str(self.root / "workspace" / "antigravity"))
        self.assertEqual(0, code, created)
        calls = []

        def fake_run(cmd, cwd=None, text=None, capture_output=None, timeout=None):
            calls.append(cmd)

            class Result:
                returncode = 0
                stdout = json.dumps({"ok": False, "agent": "antigravity", "direct_message": True, "reply": "ANTIGRAVITY_DIRECT_OK", "activation_eligible": False})
                stderr = ""

            return Result()

        with mock.patch.object(companyctl.subprocess, "run", side_effect=fake_run):
            code, verified = run_cli("employee", "verify-direct", "--id", "antigravity", "--from", "main", "--rounds", "2", "--activate")
        self.assertEqual(1, code, verified)
        self.assertFalse(verified["ok"])
        self.assertFalse(verified["activation_allowed"])
        self.assertEqual(0, verified["rounds_completed"])
        code, shown = run_cli("employee", "show", "antigravity")
        self.assertEqual(0, code, shown)
        self.assertEqual("candidate", shown["employee"]["status"])

    def test_antigravity_cli_direct_rounds_do_not_activate_without_execution_evidence(self) -> None:
        code, created = run_cli("employee", "create", "--id", "main", "--name", "main", "--role", "operator", "--runtime", "openclaw", "--workspace", str(self.root / "workspace" / "main"))
        self.assertEqual(0, code, created)
        code, created = run_cli("employee", "create", "--id", "antigravity", "--name", "Antigravity", "--role", "developer", "--runtime", "antigravity", "--workspace", str(self.root / "workspace" / "antigravity"))
        self.assertEqual(0, code, created)

        def fake_run(cmd, cwd=None, text=None, capture_output=None, timeout=None):
            expected = ""
            if "--direct-message" in cmd:
                body = cmd[cmd.index("--direct-message") + 1]
                expected = body.rsplit(" ", 1)[-1]

            class Result:
                returncode = 0
                stdout = json.dumps({"ok": True, "agent": "antigravity", "direct_message": True, "reply": expected, "activation_eligible": False})
                stderr = ""

            return Result()

        with mock.patch.object(companyctl.subprocess, "run", side_effect=fake_run):
            code, verified = run_cli("employee", "verify-direct", "--id", "antigravity", "--from", "main", "--rounds", "3", "--activate")
        self.assertEqual(1, code, verified)
        self.assertFalse(verified["ok"])
        self.assertEqual(0, verified["rounds_completed"])
        self.assertFalse(verified["activated"])
        code, shown = run_cli("employee", "show", "antigravity")
        self.assertEqual(0, code, shown)
        self.assertEqual("candidate", shown["employee"]["status"])

    def test_antigravity_runtime_execution_evidence_can_activate_employee(self) -> None:
        code, created = run_cli("employee", "create", "--id", "main", "--name", "main", "--role", "operator", "--runtime", "openclaw", "--workspace", str(self.root / "workspace" / "main"))
        self.assertEqual(0, code, created)
        code, created = run_cli("employee", "create", "--id", "antigravity", "--name", "Antigravity", "--role", "developer", "--runtime", "antigravity", "--workspace", str(self.root / "workspace" / "antigravity"))
        self.assertEqual(0, code, created)
        structured_reply = "\n".join(
            [
                "status: done",
                "current_action: inspected README.md and dashboard code",
                "changed_files: -",
                "verification_run: agy print inspected README.md and company_dashboard.py",
                "browser_check: -",
                "blocker: -",
                "eta: -",
            ]
        )

        def fake_run(cmd, cwd=None, text=None, capture_output=None, timeout=None):
            self.assertIn("company-antigravity-adapter", cmd[0])
            self.assertIn("--direct-message", cmd)

            class Result:
                returncode = 0
                stdout = json.dumps({"ok": True, "agent": "antigravity", "direct_message": True, "reply": structured_reply, "activation_eligible": True})
                stderr = ""

            return Result()

        with mock.patch.object(companyctl.subprocess, "run", side_effect=fake_run):
            code, verified = run_cli("employee", "verify-runtime", "--id", "antigravity", "--from", "main", "--activate")
        self.assertEqual(0, code, verified)
        self.assertTrue(verified["ok"])
        self.assertTrue(verified["activated"])
        self.assertEqual("execution_evidence", verified["verification"]["type"])
        code, shown = run_cli("employee", "show", "antigravity")
        self.assertEqual(0, code, shown)
        self.assertEqual("active", shown["employee"]["status"])

    def test_runtime_verification_blocked_reply_cannot_activate_employee(self) -> None:
        code, created = run_cli("employee", "create", "--id", "main", "--name", "main", "--role", "operator", "--runtime", "openclaw", "--workspace", str(self.root / "workspace" / "main"))
        self.assertEqual(0, code, created)
        code, created = run_cli("employee", "create", "--id", "antigravity", "--name", "Antigravity", "--role", "developer", "--runtime", "antigravity", "--workspace", str(self.root / "workspace" / "antigravity"))
        self.assertEqual(0, code, created)
        blocked_reply = "\n".join(
            [
                "status: blocked",
                "current_action: tried to inspect files",
                "changed_files: -",
                "verification_run: find README.md company_dashboard.py",
                "browser_check: -",
                "blocker: missing target files",
                "eta: -",
            ]
        )

        def fake_run(cmd, cwd=None, text=None, capture_output=None, timeout=None):
            class Result:
                returncode = 0
                stdout = json.dumps({"ok": True, "agent": "antigravity", "direct_message": True, "reply": blocked_reply, "activation_eligible": True})
                stderr = ""

            return Result()

        with mock.patch.object(companyctl.subprocess, "run", side_effect=fake_run):
            code, verified = run_cli("employee", "verify-runtime", "--id", "antigravity", "--from", "main", "--activate")
        self.assertEqual(1, code, verified)
        self.assertFalse(verified["ok"])
        self.assertFalse(verified["activation_allowed"])
        self.assertIn("runtime_reply_not_done", verified["verification"]["reason"])
        self.assertFalse(verified["activated"])
        code, shown = run_cli("employee", "show", "antigravity")
        self.assertEqual(0, code, shown)
        self.assertEqual("candidate", shown["employee"]["status"])

    def test_candidate_runtime_verification_failure_does_not_pause_candidate(self) -> None:
        code, created = run_cli("employee", "create", "--id", "main", "--name", "main", "--role", "operator", "--runtime", "openclaw", "--workspace", str(self.root / "workspace" / "main"))
        self.assertEqual(0, code, created)
        code, created = run_cli("employee", "create", "--id", "antigravity", "--name", "Antigravity", "--role", "developer", "--runtime", "antigravity", "--workspace", str(self.root / "workspace" / "antigravity"))
        self.assertEqual(0, code, created)

        def fake_run(cmd, cwd=None, text=None, capture_output=None, timeout=None):
            self.assertIn("--timeout", cmd)
            self.assertEqual("240", cmd[cmd.index("--timeout") + 1])

            class Result:
                returncode = 1
                stdout = json.dumps({"ok": False, "reply": "", "blocker": "runtime timeout"})
                stderr = "runtime timeout"

            return Result()

        with mock.patch.object(companyctl.subprocess, "run", side_effect=fake_run):
            code, verified = run_cli("employee", "verify-runtime", "--id", "antigravity", "--from", "main", "--timeout", "240", "--activate")
        self.assertEqual(1, code, verified)
        self.assertFalse(verified["ok"])
        code, shown = run_cli("employee", "show", "antigravity")
        self.assertEqual(0, code, shown)
        self.assertEqual("candidate", shown["employee"]["status"])
        self.assertNotIn("unavailable_reason", shown["profile"])

    def test_task_submit_rejects_candidate_employee(self) -> None:
        code, created = run_cli("employee", "create", "--id", "main", "--name", "main", "--role", "operator", "--runtime", "openclaw", "--workspace", str(self.root / "workspace" / "main"))
        self.assertEqual(0, code, created)
        code, created = run_cli("employee", "create", "--id", "antigravity", "--name", "Antigravity", "--role", "developer", "--runtime", "antigravity", "--workspace", str(self.root / "workspace" / "antigravity"))
        self.assertEqual(0, code, created)
        code, submitted = run_cli("task", "submit", "--from", "main", "--to", "antigravity", "--task-id", "task-candidate-rejected", "--title", "candidate must not receive work")
        self.assertEqual(2, code, submitted)
        self.assertEqual("target employee is not active", submitted["error"])

    def test_direct_failure_marks_active_employee_unavailable(self) -> None:
        code, created = run_cli("employee", "create", "--id", "main", "--name", "main", "--role", "operator", "--runtime", "openclaw", "--workspace", str(self.root / "workspace" / "main"))
        self.assertEqual(0, code, created)
        code, created = run_cli("employee", "create", "--id", "codex", "--name", "Codex", "--role", "developer", "--runtime", "codex", "--workspace", str(self.root / "workspace" / "codex"))
        self.assertEqual(0, code, created)
        conn = companyctl.connect()
        try:
            conn.execute("UPDATE employees SET status = 'active' WHERE id = 'codex'")
            conn.commit()
        finally:
            conn.close()

        def fake_run(cmd, cwd=None, text=None, capture_output=None, timeout=None):
            class Result:
                returncode = 7
                stdout = ""
                stderr = "model quota exhausted"

            return Result()

        with mock.patch.object(companyctl.subprocess, "run", side_effect=fake_run):
            code, sent = run_cli("message", "direct", "--from", "main", "--to", "codex", "--body", "ping")
        self.assertEqual(1, code, sent)
        self.assertEqual("candidate", sent["employee_unavailable"]["status"])
        self.assertTrue(sent["employee_unavailable"]["communication_paused"])
        code, shown = run_cli("employee", "show", "codex")
        self.assertEqual(0, code, shown)
        self.assertEqual("candidate", shown["employee"]["status"])
        self.assertEqual("model quota exhausted", shown["profile"]["unavailable_reason"])

    def test_direct_delivery_failure_marks_active_employee_unavailable(self) -> None:
        code, created = run_cli("employee", "create", "--id", "main", "--name", "main", "--role", "operator", "--runtime", "openclaw", "--workspace", str(self.root / "workspace" / "main"))
        self.assertEqual(0, code, created)
        code, created = run_cli("employee", "create", "--id", "hermes-delivery", "--name", "Hermes Delivery", "--role", "supervisor", "--runtime", "hermes", "--workspace", str(self.root / "workspace" / "hermes-delivery"))
        self.assertEqual(0, code, created)
        self.mark_active("hermes-delivery")

        def fake_run(cmd, cwd=None, text=None, capture_output=None, timeout=None):
            class Result:
                returncode = 0
                stdout = json.dumps(
                    {
                        "result": {"payloads": [{"text": "HERMES_DELIVERY_ACK"}]},
                        "deliveryStatus": {"attempted": True, "succeeded": False, "errorMessage": "telegram delivery failed"},
                    }
                )
                stderr = ""

            return Result()

        with mock.patch.object(companyctl.subprocess, "run", side_effect=fake_run):
            code, sent = run_cli("message", "direct", "--from", "main", "--to", "hermes-delivery", "--body", "ping")
        self.assertEqual(1, code, sent)
        self.assertFalse(sent["ok"])
        self.assertEqual("HERMES_DELIVERY_ACK", sent["reply"])
        self.assertEqual("candidate", sent["employee_unavailable"]["status"])
        self.assertTrue(sent["employee_unavailable"]["communication_paused"])
        code, shown = run_cli("employee", "show", "hermes-delivery")
        self.assertEqual(0, code, shown)
        self.assertEqual("candidate", shown["employee"]["status"])
        self.assertEqual("telegram delivery failed", shown["profile"]["unavailable_reason"])

    def test_antigravity_blocked_execution_does_not_downgrade_active_employee(self) -> None:
        code, created = run_cli("employee", "create", "--id", "main", "--name", "main", "--role", "operator", "--runtime", "openclaw", "--workspace", str(self.root / "workspace" / "main"))
        self.assertEqual(0, code, created)
        code, created = run_cli("employee", "create", "--id", "antigravity", "--name", "Antigravity", "--role", "developer", "--runtime", "antigravity", "--workspace", str(self.root / "workspace" / "antigravity"))
        self.assertEqual(0, code, created)
        self.mark_active("antigravity")

        def fake_run(cmd, cwd=None, text=None, capture_output=None, timeout=None):
            class Result:
                returncode = 1
                stdout = json.dumps(
                    {
                        "ok": False,
                        "direct_message": True,
                        "reply": "status: blocked\ncurrent_action: Agy returned planning-only output\nchanged_files: -\nverification_run: adapter report\nblocker: planning_only_or_timeout",
                        "blocked_execution": True,
                        "blocker": "planning_only_or_timeout",
                        "status_delivery": {"ok": True},
                    }
                )
                stderr = ""

            return Result()

        with mock.patch.object(companyctl.subprocess, "run", side_effect=fake_run):
            code, sent = run_cli("message", "direct", "--from", "main", "--to", "antigravity", "--body", "评测 dashboard")
        self.assertEqual(1, code, sent)
        self.assertFalse(sent["ok"])
        self.assertTrue(sent["adapter_blocked"]["blocked_execution"])
        self.assertNotIn("employee_unavailable", sent)
        code, shown = run_cli("employee", "show", "antigravity")
        self.assertEqual(0, code, shown)
        self.assertEqual("active", shown["employee"]["status"])
        self.assertNotIn("unavailable_reason", shown["profile"])

    def test_dashboard_renders_conversations_and_pending_events(self) -> None:
        code, created = run_cli("employee", "create", "--id", "main", "--name", "main", "--role", "operator", "--runtime", "openclaw", "--workspace", str(self.root / "workspace" / "main"))
        self.assertEqual(code, 0, created)
        code, started = run_cli(
            "conversation",
            "start",
            "--from",
            "maker",
            "--participants",
            "maker,ops",
            "--title",
            "Dashboard 会话",
            "--body",
            "dashboard smoke",
            "--conversation-id",
            "conv-dashboard-001",
        )
        self.assertEqual(code, 0, started)

        output = self.root / "state" / "dashboard.html"
        with contextlib.redirect_stdout(io.StringIO()):
            code = companyctl.main([
                "followup",
                "request",
                "--from",
                "nestcar",
                "--to",
                "main",
                "--question",
                "请补充本次还车里程",
                "--followup-id",
                "followup-dashboard-001",
            ])
        self.assertEqual(0, code)
        def fake_followup_reply(_args) -> int:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "message": {"id": "msg-followup-dashboard-001-answer"},
                        "reply": "ack",
                        "file": str(self.root / "employees" / "nestcar" / "inbox" / "msg-followup-dashboard-001-answer.message.json"),
                    },
                    ensure_ascii=False,
                )
            )
            return 0

        with mock.patch.object(companyctl, "cmd_message_direct", side_effect=fake_followup_reply):
            with contextlib.redirect_stdout(io.StringIO()):
                code = companyctl.main([
                    "followup",
                    "reply",
                    "--followup-id",
                    "followup-dashboard-001",
                    "--by",
                    "main",
                    "--answer",
                    "本次还车里程是 10234 km",
                ])
        self.assertEqual(0, code)
        conn = companyctl.connect()
        try:
            conn.execute(
                """
                INSERT INTO adapter_runs(id, trace_id, agent_id, task_id, command, ok, processed, attempt, next_retry_at, result_json, created_at)
                VALUES ('adapter-run-dashboard-trace', 'trace-dashboard-live', 'codex', 'task-dashboard-trace', 'company-codex-adapter', 1, 1, 1, '', '{}', ?)
                """,
                (companyctl.now(),),
            )
            conn.commit()
            summary = company_dashboard.load_summary(conn)
        finally:
            conn.close()
        self.assertIn("trace-dashboard-live", [trace["trace_id"] for trace in summary["traces"]])
        dashboard_trace = next(trace for trace in summary["traces"] if trace["trace_id"] == "trace-dashboard-live")
        self.assertIn("counts", dashboard_trace)
        self.assertEqual(1, dashboard_trace["counts"]["adapter_runs"])
        with contextlib.redirect_stdout(io.StringIO()):
            code = company_dashboard.main(["--output", str(output), "--variant", "basic"])
        self.assertEqual(0, code)
        html = output.read_text(encoding="utf-8")
        self.assertIn("Conversations", html)
        self.assertIn("Pending Events", html)
        self.assertIn("Followups", html)
        self.assertIn("followup-dashboard-001", html)
        self.assertIn("本次还车里程是 10234 km", html)
        self.assertIn("conv-dashboard-001", html)
        self.assertIn("conversation.message", html)
        self.assertIn("Runtime Health", html)
        self.assertIn("daemon", html)
        self.assertIn("launchd", html)
        self.assertIn("missing_daemon_state", html)
        self.assertIn("local-automation", html)
        self.assertIn("ops-support", html)
        self.assertIn("Needs Attention", html)
        self.assertIn("trace-dashboard-live", html)
        self.assertIn("company-codex-adapter", html)

    def test_dashboard_auto_variant_prefers_advanced_live_template(self) -> None:
        output = self.root / "state" / "dashboard-auto.html"
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = company_dashboard.main(["--output", str(output)])
        self.assertEqual(0, code)
        payload = json.loads(stdout.getvalue())
        self.assertEqual("advanced", payload["variant"])
        html = output.read_text(encoding="utf-8")
        self.assertIn("window.companyApiBase", html)
        bootstrap_match = re.search(r'atob\((".*?")\)', html)
        self.assertIsNotNone(bootstrap_match)
        bootstrap_b64 = json.loads(bootstrap_match.group(1))
        bootstrap_payload = json.loads(base64.b64decode(bootstrap_b64).decode("utf-8"))
        self.assertEqual("api-first-lightweight", bootstrap_payload["bootstrap_mode"])
        self.assertEqual([], bootstrap_payload["employees"])
        self.assertEqual([], bootstrap_payload["tasks"])
        self.assertIn("kernel-summary-debug", html)
        debug_match = re.search(r"kernel-summary-debug (.*?) -->", html)
        self.assertIsNotNone(debug_match)
        debug_payload = json.loads(debug_match.group(1))
        self.assertEqual({"generated_at", "counts", "api_base"}, set(debug_payload))
        self.assertNotIn("direct_messages_recent", debug_match.group(1))
        self.assertNotIn("\"employees\": [", debug_match.group(1))
        self.assertNotIn("\"tasks\": [", debug_match.group(1))
        first_script_match = re.search(r"<script>(.*?)</script>", html, flags=re.S)
        self.assertIsNotNone(first_script_match)
        self.assertNotIn("<!--", first_script_match.group(1))
        self.assertLess(html.index("</script>"), html.index("kernel-summary-debug"))
        self.assertNotIn("本次还车里程是 10234 km", html)
        self.assertNotIn("conv-dashboard-001", html)
        self.assertIn("/v1/telemetry/traces", html)
        self.assertIn("/v1/messages/recent-direct", html)
        self.assertIn("Live SQLite + OpenClaw Runtime", html)
        self.assertIn("Company Event Ledger", html)
        self.assertIn("/v1/events", html)
        self.assertIn("company-events-tbody", html)
        self.assertIn("window.showDetails", html)
        self.assertIn("showStoredDetails", html)
        self.assertIn("dashboardDetailStore", html)
        self.assertIn("summarizeRawPayload", html)
        self.assertIn("expandRawDetail", html)
        self.assertIn("Full raw payload is stored off-DOM", html)
        self.assertIn("data-testid=\"raw-json-expand\"", html)
        self.assertNotIn("<summary>Raw JSON</summary>", html)
        self.assertNotIn("JSON.stringify(event).replace", html)
        self.assertIn("Read-only live event stream", html)
        self.assertIn("refreshKernelEventConsole", html)
        self.assertIn("Evidence Records", html)
        self.assertIn("evidenceRecordsSummary", html)
        self.assertIn("Evidence Content Preview", html)
        self.assertIn("Latest Attempt Summary", html)
        self.assertIn("Attempt History Summary", html)
        self.assertIn("Evidence Records Summary", html)
        self.assertIn("Sanitized Logs Summary", html)
        self.assertIn("display.relative_path", html)
        self.assertIn("display.absolute_path_exposed", html)
        self.assertIn("absolute_path_exposed=${String(!!display.absolute_path_exposed)}", html)
        self.assertIn("Excludes human owner", html)
        self.assertIn("Includes human owner records", html)
        self.assertNotIn("terminalLogs", html)
        self.assertNotIn("startTerminalSimulation", html)
        self.assertNotIn("executeTerminalCommand", html)
        self.assertNotIn("toggleSimulationMode", html)
        self.assertNotIn("Scenario Seeder", html)
        self.assertNotIn("Simulation: Normal", html)
        self.assertIn("/v1/dashboard/cockpit", html)
        self.assertIn("/v1/events/stream", html)
        self.assertIn("sse-status-chip", html)
        self.assertIn("setSseStatus", html)
        self.assertIn("live-data-chip", html)
        self.assertIn("setLiveDataStatus('ok', `Live data: tasks=${taskRows.length} employees=${(employees.employees || []).length} events=${(events.events || []).length}`)", html)
        self.assertIn("Live data: API refresh failed", html)
        self.assertIn("SSE / REST fallback", html)
        self.assertIn("savedAutoRefresh !== 'false'", html)
        self.assertIn("window.addEventListener('DOMContentLoaded', async () =>", html)
        self.assertIn("const initialRefreshOk = await refreshLiveDashboardFromApi();", html)
        self.assertIn("Initial REST refresh failed; dashboard is showing cached/bootstrap data only.", html)
        self.assertIn("Progress Stagnant", html)
        self.assertIn("stagnantTaskGuidance", html)
        self.assertIn('data-testid="stagnant-guidance-static"', html)
        self.assertIn("员工仍在线，但 15 分钟没有新进度。可继续等待、发送探针、查看日志或请求 Hermes 纠偏。", html)
        self.assertIn("Sanitized Logs only show cleaned attempt summaries, not raw stdout/stderr.", html)
        self.assertIn("Send Probe", html)
        self.assertIn("AI Employee Cockpit", html)
        self.assertIn('rel="icon" href="data:image/svg+xml', html)
        self.assertIn("counts.employees_online", html)
        self.assertIn("counts.employees_total", html)
        self.assertIn("registry_reconciliation", html)
        self.assertIn("registeredTotal", html)
        self.assertIn("schedulableTotal", html)
        self.assertIn("human owner excluded", html)
        self.assertIn("counts.employees_abnormal", html)
        self.assertIn("counts.running_tasks", html)
        self.assertIn("cockpit.task_cards || cockpit.long_tasks", html)
        self.assertIn("Completion Invalid", html)
        self.assertIn("Runtime · sessions=", html)
        self.assertIn("Tools · calls=", html)
        self.assertIn("Budget ·", html)
        self.assertIn("counts.done_tasks", html)
        self.assertIn("counts.evidence_issues", html)
        self.assertIn("counts.blocked_tasks", html)
        self.assertIn("counts.chat_task_bound", html)
        self.assertIn("counts.chat_work_relevant", html)
        self.assertIn("counts.chat_handshake_or_idle", html)
        self.assertIn("counts.awaiting_approval_tasks", html)
        self.assertIn("data-message-id", html)
        self.assertIn("data-task-context", html)
        self.assertIn("data-chat-classification", html)
        self.assertIn('role="dialog"', html)
        self.assertIn('aria-label="Close task detail modal"', html)
        self.assertIn('data-testid="details-modal-close"', html)
        self.assertIn('data-testid="recruiter-drawer-close"', html)
        self.assertIn('class="details-render-target"', html)
        self.assertIn("body.modal-open", html)
        self.assertIn("document.body.classList.add('modal-open')", html)
        self.assertIn("document.body.classList.remove('modal-open')", html)
        self.assertIn("position: sticky;", html)
        self.assertIn("z-index: 10000;", html)
        self.assertIn("z-index: 30;", html)
        self.assertIn("pointer-events: auto;", html)
        self.assertIn('event.target.closest(\'[data-testid="details-modal-close"]\')', html)
        self.assertIn("if (![x1, y1, x2, y2].every(Number.isFinite)) return;", html)
        self.assertIn("if (!Number.isFinite(len) || len < 1) return;", html)
        self.assertIn("if (![cx, cy].every(Number.isFinite)) return;", html)
        self.assertIn("background: #f8fafc;", html)
        self.assertIn("max-height: calc(100vh - 128px);", html)
        self.assertIn("overscroll-behavior: contain;", html)
        self.assertIn("overflow-wrap: anywhere;", html)
        self.assertIn("word-break: break-word;", html)
        self.assertIn('class="detail-text"', html)
        self.assertNotIn('<pre><code id="modal-code"></code></pre>', html)
        self.assertIn("ledgerConsistencySummary", html)
        self.assertIn("Ledger Consistency", html)
        self.assertIn("API / CLI / Dashboard read the same Company Kernel ledger", html)
        self.assertIn("counts.employee_status_counts", html)
        self.assertIn("counts.readiness_counts", html)
        self.assertIn("employeeStatusCounts.busy", html)
        self.assertIn("employeeStatusCounts['active-limited']", html)
        self.assertIn("employeeStatusCounts.candidate", html)
        self.assertIn("readinessCounts.active_ready", html)
        self.assertIn("readinessCounts.online_only", html)
        self.assertEqual(5, html.count('class="nav-btn'))
        self.assertIn("Cockpit Console", html)
        self.assertIn("AI Fleet & Skills", html)
        self.assertNotIn('id="tab-projects"', html)
        self.assertNotIn('id="tab-events"', html)
        self.assertNotIn('id="tab-telemetry"', html)
        self.assertNotIn('id="panel-projects" class="tab-pane"', html)
        self.assertNotIn('id="panel-events" class="tab-pane"', html)
        self.assertNotIn('id="panel-telemetry" class="tab-pane"', html)
        self.assertIn(".embedded-panel {\n      display: none;", html)
        self.assertIn("Marketplace Preview Disabled", html)
        self.assertIn("local-only", html)
        self.assertIn("No payment, no public rental, no remote node onboarding", html)
        self.assertNotIn("Enable Public Rental", html)
        self.assertIn("Sandbox Profile Matrix", html)
        self.assertIn("sandbox-profile-matrix-tbody", html)
        self.assertIn("renderSandboxProfileMatrix", html)
        self.assertIn("workspace_scope", html)
        self.assertIn("requires_approval_for", html)
        self.assertIn("Audit, Approvals & Evidence", html)
        self.assertIn("audit-hub-links", html)
        self.assertIn("Audit Hub", html)
        self.assertIn("Event Ledger", html)
        self.assertIn("Trace Timeline", html)
        self.assertIn("Task Supervisor Chain", html)
        self.assertIn("taskSupervisorChainSummary", html)
        self.assertIn("Correction State", html)
        self.assertIn("Correction Events", html)
        self.assertIn('id="attempt-history-container"', html)
        self.assertIn("function renderAttemptHistorySummary()", html)
        self.assertIn("function attemptHistoryRows(summary)", html)
        self.assertIn("Retry and reassign create new attempt_id records", html)
        self.assertIn("renderAttemptHistorySummary();", html)
        self.assertIn("data-attempt-id", html)
        self.assertIn("viewEvidenceContent", html)
        self.assertIn("/v1/evidence/", html)
        self.assertIn("/safe-preview", html)
        self.assertIn("View Safe Evidence", html)
        self.assertIn("Safe Evidence: ${evidenceId}", html)
        self.assertIn("Absolute Path Exposed", html)
        self.assertIn("(blocked by evidence whitelist policy)", html)
        self.assertIn("correctionEventsSummary", html)
        self.assertIn("Supervisor State", html)
        self.assertIn("Latest Attempt", html)
        self.assertIn("Attempt History", html)
        self.assertIn("Attempt Lineage", html)
        self.assertIn("Attempt Recovery Chain", html)
        self.assertIn("payload.attempt_history", html)
        self.assertIn("attemptHistory.recovery_summary", html)
        self.assertIn("attemptHistory.chain", html)
        self.assertIn("Heartbeat / Progress", html)
        self.assertIn("Runtime Policy", html)
        self.assertIn("Long Task State", html)
        self.assertIn("task.long_task_state || latestAttempt.long_task_state || task.status", html)
        self.assertIn("longTaskStatusContractSummary", html)
        self.assertIn("timeout is sync wait only", html)
        self.assertIn("Task Decision", html)
        self.assertIn("taskDecisionSummary", html)
        self.assertIn("Recommended action:", html)
        self.assertIn("Done is valid only with task_id/attempt_id bound final evidence.", html)
        self.assertIn("log_policy", html)
        self.assertIn("raw_available", html)
        self.assertIn("raw stdout/stderr hidden", html)
        self.assertIn("Heartbeat State", html)
        self.assertIn("Progress State", html)
        self.assertIn("Progress Events", html)
        self.assertIn("Latest Progress", html)
        self.assertIn("latestProgressSummary", html)
        self.assertIn("hasLatestProgress(item)", html)
        self.assertIn("item.latest_progress", html)
        self.assertNotIn("item.latest_progress ? `<span>Latest Progress", html)
        self.assertIn("heartbeat=${escapeHtml(item.heartbeat_state || '-')}", html)
        self.assertIn("progress_state=${escapeHtml(item.progress_state || '-')}", html)
        self.assertIn("progress=${escapeHtml(item.progress ?? '-')}", html)
        self.assertIn("owner_attention", html)
        self.assertIn("Supervisor Activity", html)
        self.assertIn("cockpit-supervisor-activity", html)
        self.assertIn("renderSupervisorActivity", html)
        self.assertIn("correction_pending_ack", html)
        self.assertIn("Hermes supervised corrections and stagnant checks appear here", html)
        self.assertIn("verifyRuntimeEvidence", html)
        self.assertIn("/v1/agent-matrix?agents=", html)
        self.assertIn("direct=${checks.direct || '-'}", html)
        self.assertIn("progress=${checks.progress || '-'}", html)
        self.assertIn("stale=${checks.stale || '-'}", html)
        self.assertIn("normalizedAgent", html)
        self.assertIn("candidate.replace(/_/g, '-') === normalizedAgent", html)
        self.assertIn("returned=${returned}", html)
        self.assertIn("installOwnerAttentionActionHandlers", html)
        self.assertIn("owner-attention-action", html)
        self.assertIn("data-employee-id", html)
        self.assertIn("window.handleOwnerAttentionAction", html)
        self.assertIn("window.verifyRuntimeEvidence(employeeId)", html)
        self.assertIn("liveRefreshInFlight", html)
        self.assertIn("liveRefreshQueued", html)
        self.assertIn("Recent Evidence", html)
        self.assertIn("promoted evidence", html)
        self.assertIn("legacy task evidence_path", html)
        self.assertIn("cockpit-recent-evidence", html)
        self.assertIn("audit-evidence-tbody", html)
        self.assertIn("/v1/evidence?limit=50", html)
        self.assertIn("refreshAuditEvidenceTable", html)
        self.assertIn("audit-artifacts-tbody", html)
        self.assertIn("/v1/artifacts?limit=50", html)
        self.assertIn("refreshAuditArtifactsTable", html)
        self.assertIn("audit-handoffs-tbody", html)
        self.assertIn("/v1/handoffs?limit=50", html)
        self.assertIn("refreshAuditHandoffsTable", html)
        self.assertIn("audit-failures-tbody", html)
        self.assertIn("/v1/failures?limit=50", html)
        self.assertIn("refreshAuditFailuresTable", html)
        self.assertIn('data-record-row="true"', html)
        self.assertIn('data-empty-row="true"', html)
        self.assertIn('<tr data-empty-row="true"><td colspan="7" style="color:var(--text-muted);text-align:center;padding:24px;">No handoffs.</td></tr>', html)
        self.assertIn("Workspace Retention Dry-run", html)
        self.assertIn("/v1/workspaces/prune?dry_run=true", html)
        self.assertIn("refreshWorkspacePrunePreview", html)
        self.assertIn("dry-run only; no files are deleted", html)
        self.assertIn("summary.bytes_reclaimable", html)
        self.assertIn("policy.older_than_days", html)
        self.assertIn("item.workspace", html)
        self.assertIn("absolute_path_exposed", html)
        self.assertIn("display.policy.summary", html)
        self.assertIn("workspace/evidence/reports/artifacts/final only", html)
        self.assertIn("long_task_state || task.status", html)
        self.assertIn("employee_readiness", html)
        self.assertIn("Verify runtime evidence", html)
        self.assertIn("Runtime Check Result", html)
        self.assertIn('data-testid="runtime-check-result"', html)
        self.assertIn("renderRuntimeCheckResult", html)
        self.assertIn("attendance=${checks.attendance || '-'}", html)
        self.assertIn("Keep candidate", html)
        self.assertIn("结构化 execution evidence", html)
        self.assertIn("handleOwnerAttentionAction", html)
        self.assertIn("viewTaskLogs(taskId)", html)
        self.assertIn("recordWaitDecision(taskId, attemptId)", html)
        self.assertIn("Waiting is recorded locally only; no external send is triggered.", html)
        self.assertIn("function isTerminalTaskState(task, attempt)", html)
        self.assertIn("function taskStateMetaSummary(task)", html)
        self.assertIn("final=${finalState} · evidence=${hasEvidence ? 'present' : 'missing'}", html)
        self.assertIn("return `task=${taskStatus} · heartbeat=${heartbeat} · progress=${progress}`;", html)
        self.assertIn("data-action-id", html)
        self.assertIn("item.correction", html)
        self.assertIn("needs_ack", html)
        self.assertIn("last_message", html)
        self.assertIn("approval_action", html)
        self.assertIn("risk=${item.risk}", html)
        self.assertIn("correctTaskAttempt(taskId, attemptId)", html)
        self.assertIn("cancelTaskAttempt(taskId, attemptId)", html)
        self.assertIn('data-ledger-action="task.correct"', html)
        self.assertIn('data-ledger-action="task.cancel"', html)
        self.assertIn('data-ledger-action="task.probe"', html)
        self.assertIn('data-requires-owner-approval="true"', html)
        self.assertIn('data-dangerous="true"', html)
        self.assertIn("retryTask(taskId)", html)
        self.assertIn("reassignTask(taskId", html)
        self.assertIn("Conversation Summary", html)
        self.assertIn("conversation_summary", html)
        self.assertIn("Approval Safety", html)
        self.assertIn("approvalSafetySummary", html)
        self.assertIn("counts.employee_status_counts", html)
        self.assertIn("counts.readiness_counts", html)
        self.assertIn("active_ready", html)
        self.assertIn("online_only", html)
        self.assertIn("submitTaskToEmployee", html)
        self.assertIn("No chat; task/evidence only", html)

    def test_dashboard_hides_direct_button_for_skill_worker(self) -> None:
        code, skill = run_cli("employee", "create", "--id", "image-copy-skill", "--name", "Image Copy Skill", "--role", "skill-worker", "--runtime", "skill", "--workspace", str(self.root / "workspace" / "image-copy-skill"))
        self.assertEqual(0, code, skill)
        self.mark_active("image-copy-skill")
        code, heartbeat = run_cli("heartbeat", "--agent", "image-copy-skill")
        self.assertEqual(0, code, heartbeat)
        verification_dir = self.root / "state" / "employee-verification" / "image-copy-skill"
        verification_dir.mkdir(parents=True)
        (verification_dir / "latest-runtime.json").write_text(json.dumps({"ok": True, "activation_allowed": True}, ensure_ascii=False), encoding="utf-8")
        output = self.root / "state" / "dashboard-skill-worker.html"
        with contextlib.redirect_stdout(io.StringIO()):
            code = company_dashboard.main(["--output", str(output), "--variant", "basic"])
        self.assertEqual(0, code)
        html = output.read_text(encoding="utf-8")
        row = html.split("<td>image-copy-skill</td>", 1)[1].split("</tr>", 1)[0]
        self.assertNotIn("directMessageEmployee('image-copy-skill')", row)
        self.assertIn("No chat; task/evidence only", row)
        self.assertIn("submitTaskToEmployee('image-copy-skill')", row)

    def test_cockpit_api_sanitizes_evidence_and_exposes_long_task_state(self) -> None:
        code, created = run_cli("employee", "create", "--id", "main", "--name", "main", "--role", "operator", "--runtime", "openclaw", "--workspace", str(self.root / "workspace" / "main"))
        self.assertEqual(0, code, created)
        code, created = run_cli("employee", "create", "--id", "codex-cockpit", "--name", "codex-cockpit", "--role", "engineer", "--runtime", "codex", "--workspace", str(self.root / "workspace" / "codex-cockpit"))
        self.assertEqual(0, code, created)
        code, created = run_cli("employee", "create", "--id", "agy-cockpit", "--name", "agy-cockpit", "--role", "designer", "--runtime", "antigravity", "--workspace", str(self.root / "workspace" / "agy-cockpit"))
        self.assertEqual(0, code, created)
        conn = companyctl.connect()
        try:
            conn.execute("UPDATE employees SET status = 'active', updated_at = ? WHERE id = ?", (companyctl.now(), "main"))
            conn.execute("UPDATE employees SET status = 'active', updated_at = ? WHERE id = ?", (companyctl.now(), "codex-cockpit"))
            conn.commit()
        finally:
            conn.close()
        for employee_id in ["main", "codex-cockpit"]:
            code, heartbeat = run_cli("heartbeat", "--agent", employee_id)
            self.assertEqual(0, code, heartbeat)
        verification_dir = self.root / "state" / "employee-verification" / "codex-cockpit"
        verification_dir.mkdir(parents=True)
        (verification_dir / "latest-runtime.json").write_text(json.dumps({"ok": True, "activation_allowed": True}, ensure_ascii=False), encoding="utf-8")
        code, submitted = run_cli("task", "submit", "--from", "main", "--to", "codex-cockpit", "--task-id", "task-cockpit-long", "--title", "Cockpit long task")
        self.assertEqual(0, code, submitted)
        code, submitted_blocked = run_cli("task", "submit", "--from", "main", "--to", "codex-cockpit", "--task-id", "task-cockpit-blocked", "--title", "Cockpit blocked task")
        self.assertEqual(0, code, submitted_blocked)
        code, blocked = run_cli("task", "block", "--agent", "codex-cockpit", "--task-id", "task-cockpit-blocked", "--blocker", "needs owner input")
        self.assertEqual(0, code, blocked)
        code, submitted_done = run_cli("task", "submit", "--from", "main", "--to", "codex-cockpit", "--task-id", "task-cockpit-done", "--title", "Cockpit done task")
        self.assertEqual(0, code, submitted_done)
        code, submitted_missing_evidence = run_cli("task", "submit", "--from", "main", "--to", "codex-cockpit", "--task-id", "task-cockpit-done-missing-evidence", "--title", "Cockpit missing evidence")
        self.assertEqual(0, code, submitted_missing_evidence)
        code, submitted_approval = run_cli("task", "submit", "--from", "main", "--to", "codex-cockpit", "--task-id", "task-cockpit-awaiting-approval", "--title", "Cockpit approval task")
        self.assertEqual(0, code, submitted_approval)
        code, project = run_cli("project", "create", "--project-id", "project-cockpit-cost", "--title", "Cockpit Cost", "--goal", "Expose project spend in CEO cockpit", "--owner", "main")
        self.assertEqual(0, code, project)
        code, linked = run_cli("project", "link-task", "--project-id", "project-cockpit-cost", "--task-id", "task-cockpit-long")
        self.assertEqual(0, code, linked)
        code, approval = run_cli(
            "approval",
            "request",
            "--from",
            "main",
            "--action",
            "external_send",
            "--reason",
            "approval bound to cockpit task",
            "--target",
            "codex-cockpit",
            "--risk",
            "P1",
            "--task-id",
            "task-cockpit-awaiting-approval",
            "--approval-id",
            "approval-cockpit-awaiting",
        )
        self.assertEqual(0, code, approval)
        code, budget_approval = run_cli(
            "approval",
            "request",
            "--from",
            "main",
            "--action",
            "budget_overrun",
            "--reason",
            "task budget exceeded hard limit",
            "--target",
            "codex-cockpit",
            "--risk",
            "P0",
            "--task-id",
            "task-cockpit-long",
            "--approval-id",
            "approval-cockpit-budget-overrun",
        )
        self.assertEqual(0, code, budget_approval)
        conn = companyctl.connect()
        try:
            approval_row = conn.execute("SELECT reason FROM approvals WHERE id = ?", ("approval-cockpit-budget-overrun",)).fetchone()
            detail = json.loads(approval_row["reason"])
            detail["metadata"].update({"budget_amount": 1.2, "currency": "USD", "limit_status": "hard_exceeded", "hard_limit": 1.0})
            conn.execute("UPDATE approvals SET reason = ? WHERE id = ?", (json.dumps(detail, ensure_ascii=False), "approval-cockpit-budget-overrun"))
            conn.commit()
        finally:
            conn.close()
        conn = companyctl.connect()
        try:
            workspace = companyctl.ensure_task_workspace(conn, "task-cockpit-long")
            evidence_path = Path(workspace["path"]) / "evidence" / "result.md"
            evidence_path.write_text("safe evidence\n", encoding="utf-8")
            conn.execute("UPDATE tasks SET evidence_path = ?, status = 'claimed', claimed_by = 'codex-cockpit', updated_at = ? WHERE id = ?", (str(evidence_path), companyctl.now(), "task-cockpit-long"))
            done_workspace = companyctl.ensure_task_workspace(conn, "task-cockpit-done")
            done_evidence = Path(done_workspace["path"]) / "evidence" / "done.md"
            done_evidence.write_text("done evidence\n", encoding="utf-8")
            final_artifact_path = Path(done_workspace["path"]) / "final" / "done-final.md"
            final_artifact_path.write_text("promoted final evidence\n", encoding="utf-8")
            final_artifact = companyctl.register_artifact_internal(
                conn,
                task_id="task-cockpit-done",
                employee_id="codex-cockpit",
                path=str(final_artifact_path),
                artifact_type="md",
                name="done-final.md",
                stage="final",
                summary="promoted cockpit final evidence",
                is_final=True,
            )
            promoted = companyctl.promote_artifact_to_evidence_internal(
                conn,
                artifact_id=final_artifact["artifact"]["artifact_id"],
                by="codex-cockpit",
                summary="promoted cockpit final evidence",
            )
            conn.execute("UPDATE tasks SET evidence_path = ?, status = 'completed', claimed_by = 'codex-cockpit', summary = 'done', updated_at = ? WHERE id = ?", (str(done_evidence), companyctl.now(), "task-cockpit-done"))
            conn.execute("UPDATE tasks SET status = 'completed', claimed_by = 'codex-cockpit', summary = 'missing evidence', updated_at = ? WHERE id = ?", (companyctl.now(), "task-cockpit-done-missing-evidence"))
            conn.commit()
        finally:
            conn.close()

        code, run = run_cli("task", "run", "--task-id", "task-cockpit-long", "--agent", "codex-cockpit", "--by", "main", "--stale-after-seconds", "900")
        self.assertEqual(0, code, run)
        attempt_id = run["attempt"]["attempt_id"]
        code, progress = run_cli("task", "progress", "--task-id", "task-cockpit-long", "--agent", "codex-cockpit", "--attempt-id", attempt_id, "--state", "in_progress", "--message", "正在整理最终 evidence 包", "--progress", "42")
        self.assertEqual(0, code, progress)
        code, corrected = run_cli("task", "correct", "--task-id", "task-cockpit-long", "--attempt-id", attempt_id, "--by", "hermes", "--message", "请收口 evidence，不要继续扩散")
        self.assertEqual(0, code, corrected)
        code, session = run_cli(
            "runtime",
            "session",
            "start",
            "--session-id",
            "session-cockpit-long",
            "--employee",
            "codex-cockpit",
            "--adapter-type",
            "codex",
            "--runtime-type",
            "cli",
            "--pid",
            "9876",
            "--session-key",
            "codex-cockpit-session",
            "--task-id",
            "task-cockpit-long",
            "--attempt-id",
            attempt_id,
        )
        self.assertEqual(0, code, session)
        code, tool = run_cli(
            "tool-call",
            "start",
            "--tool-call-id",
            "tool-cockpit-long-shell",
            "--task-id",
            "task-cockpit-long",
            "--attempt-id",
            attempt_id,
            "--employee",
            "codex-cockpit",
            "--session-id",
            "session-cockpit-long",
            "--tool-name",
            "shell",
            "--tool-type",
            "shell",
            "--input-summary",
            "run evidence packaging check",
            "--risk-level",
            "low",
        )
        self.assertEqual(0, code, tool)
        code, finished_tool = run_cli(
            "tool-call",
            "finish",
            "--tool-call-id",
            "tool-cockpit-long-shell",
            "--status",
            "success",
            "--output-summary",
            "evidence packaging check passed",
        )
        self.assertEqual(0, code, finished_tool)
        code, budget = run_cli(
            "budget",
            "record",
            "--budget-event-id",
            "budget-cockpit-project-cost",
            "--task-id",
            "task-cockpit-long",
            "--attempt-id",
            attempt_id,
            "--employee",
            "codex-cockpit",
            "--cost-type",
            "model_api",
            "--amount",
            "1.2",
            "--currency",
            "USD",
            "--token-input",
            "3000",
            "--token-output",
            "700",
            "--runtime-seconds",
            "120",
            "--summary",
            "cockpit project cost",
        )
        self.assertEqual(0, code, budget)
        conn = companyctl.connect()
        try:
            current = datetime.now(timezone.utc).astimezone()
            old = (current - timedelta(minutes=20)).isoformat(timespec="seconds")
            fresh = current.isoformat(timespec="seconds")
            conn.execute("UPDATE execution_attempts SET last_progress_at = ?, last_heartbeat_at = ? WHERE attempt_id = ?", (old, fresh, attempt_id))
            conn.execute("UPDATE tasks SET evidence_path = ? WHERE id = ?", (str(self.root / ".ssh" / "id_rsa"), "task-secret-evidence"))
            conn.commit()
        finally:
            conn.close()

        status, cockpit = api_gateway.route_get("/v1/dashboard/cockpit", {})
        self.assertEqual(200, status, cockpit)
        self.assertTrue(cockpit["ok"])
        self.assertIn("doctor", cockpit)
        self.assertFalse(cockpit["doctor"]["ok"])
        self.assertEqual(1, cockpit["doctor"]["exit_code"])
        self.assertEqual(len(cockpit["doctor"]["issues"]), cockpit["doctor"]["issue_count"])
        self.assertIn("task_evidence_issues", cockpit["doctor"]["issues"])
        self.assertIn("generated_at", cockpit["doctor"])
        conn = companyctl.connect()
        try:
            employee_total = conn.execute("SELECT COUNT(*) AS count FROM employees").fetchone()["count"]
        finally:
            conn.close()
        self.assertEqual(employee_total, cockpit["counts"]["employees_total"])
        self.assertEqual(2, cockpit["counts"]["employees_online"])
        self.assertGreaterEqual(cockpit["counts"]["employees_abnormal"], 1)
        self.assertEqual(employee_total, cockpit["employee_counts"]["total"])
        self.assertEqual(2, cockpit["employee_counts"]["online"])
        self.assertGreaterEqual(cockpit["employee_counts"]["abnormal"], 1)
        self.assertEqual(cockpit["counts"]["employee_status_counts"], cockpit["employee_counts"]["status_counts"])
        self.assertEqual(cockpit["counts"]["readiness_counts"], cockpit["employee_counts"]["readiness_counts"])
        self.assertEqual(1, cockpit["counts"]["employee_status_counts"]["busy"])
        self.assertGreaterEqual(cockpit["counts"]["employee_status_counts"]["active"], 1)
        self.assertGreaterEqual(cockpit["counts"]["employee_status_counts"]["abnormal"], 1)
        self.assertEqual(1, cockpit["counts"]["readiness_counts"]["active_ready"])
        self.assertGreaterEqual(
            sum(cockpit["counts"]["readiness_counts"].get(level, 0) for level in ["active_limited", "online_only", "candidate_only"]),
            1,
        )
        self.assertEqual(2, cockpit["counts"]["done_tasks"])
        self.assertEqual(1, cockpit["counts"]["evidence_issues"])
        self.assertEqual(2, cockpit["counts"]["awaiting_approval_tasks"])
        self.assertEqual("single_company_kernel_ledger", cockpit["ledger_consistency"]["source"])
        self.assertEqual(["api", "cli", "dashboard"], cockpit["ledger_consistency"]["surfaces"])
        self.assertEqual("API / CLI / Dashboard read the same Company Kernel ledger", cockpit["ledger_consistency"]["summary"])
        cockpit_employees = {item["id"]: item for item in cockpit["employees"]}
        self.assertEqual("busy", cockpit_employees["codex-cockpit"]["status"])
        self.assertEqual("active_ready", cockpit_employees["codex-cockpit"]["readiness_level"])
        self.assertIn("runtime_evidence", cockpit_employees["codex-cockpit"]["readiness_reason"])
        self.assertEqual("candidate", cockpit_employees["agy-cockpit"]["status"])
        self.assertIn("active_limited_reasons", cockpit)
        self.assertIn("agy-cockpit", cockpit["active_limited_reasons"])
        self.assertIn("candidate_requires_structured_runtime_evidence", cockpit["active_limited_reasons"]["agy-cockpit"])
        agy_attention = next(item for item in cockpit["owner_attention"] if item["kind"] == "employee_readiness" and item["employee_id"] == "agy-cockpit")
        self.assertEqual("candidate_only", agy_attention["state"])
        self.assertEqual("agy-cockpit", agy_attention["title"])
        self.assertEqual("agy-cockpit", agy_attention["display_name"])
        self.assertEqual("agy-cockpit", agy_attention["target_agent"])
        self.assertEqual("antigravity", agy_attention["runtime"])
        self.assertIn("结构化 execution evidence", agy_attention["message"])
        self.assertEqual(["verify_runtime", "view_employee", "keep_candidate"], [action["id"] for action in agy_attention["actions"]])
        self.assertEqual("GET", agy_attention["actions"][0]["method"])
        self.assertFalse(agy_attention["actions"][0]["requires_owner_approval"])
        self.assertFalse(agy_attention["actions"][0]["dangerous"])
        long_task = next(item for item in cockpit["long_tasks"] if item["task_id"] == "task-cockpit-long")
        self.assertEqual("correcting", long_task["long_task_state"])
        self.assertEqual("fresh", long_task["heartbeat_state"])
        self.assertEqual("stagnant", long_task["progress_state"])
        self.assertEqual("in_progress", long_task["latest_progress"]["progress_state"])
        self.assertEqual(42, long_task["latest_progress"]["progress"])
        self.assertIn("正在整理最终 evidence 包", long_task["latest_progress"]["message"])
        self.assertEqual(attempt_id, long_task["latest_progress"]["attempt_id"])
        self.assertTrue(long_task["correction"]["needs_ack"])
        self.assertEqual("hermes", long_task["correction"]["last_by"])
        self.assertIn("请收口 evidence", long_task["correction"]["last_message"])
        self.assertTrue(long_task["evidence"]["allowed"])
        self.assertFalse(long_task["evidence"]["absolute_path_exposed"])
        self.assertIn("evidence/result.md", long_task["evidence"]["relative_path"])
        self.assertEqual(1, cockpit["counts"]["recent_evidence"])
        self.assertEqual(2, cockpit["counts"]["legacy_task_evidence"])
        self.assertEqual(1, cockpit["counts"]["completion_invalid_tasks"])
        self.assertIn("task_cards", cockpit)
        task_cards = {item["task_id"]: item for item in cockpit["task_cards"]}
        long_card = task_cards["task-cockpit-long"]
        self.assertEqual("correcting", long_card["state"])
        self.assertEqual("correcting", long_card["long_task_state"])
        self.assertEqual("stagnant", long_card["progress_state"])
        self.assertEqual(attempt_id, long_card["attempt_id"])
        self.assertEqual("codex-cockpit", long_card["employee_id"])
        self.assertEqual(42, long_card["latest_progress"]["progress"])
        self.assertTrue(long_card["evidence"]["allowed"])
        self.assertFalse(long_card["completion_invalid"])
        self.assertEqual(1, long_card["runtime_summary"]["session_count"])
        self.assertEqual(1, long_card["runtime_summary"]["active_session_count"])
        self.assertEqual("session-cockpit-long", long_card["runtime_summary"]["latest_session_id"])
        self.assertEqual("cli", long_card["runtime_summary"]["latest_runtime_type"])
        self.assertEqual(1, long_card["tool_summary"]["tool_call_count"])
        self.assertEqual(0, long_card["tool_summary"]["failed_tool_call_count"])
        self.assertEqual("shell", long_card["tool_summary"]["latest_tool_name"])
        self.assertEqual("success", long_card["tool_summary"]["latest_tool_status"])
        self.assertEqual(1.2, long_card["budget_summary"]["total_amount"])
        self.assertEqual("USD", long_card["budget_summary"]["currency"])
        self.assertEqual(3000, long_card["budget_summary"]["token_input"])
        self.assertEqual(700, long_card["budget_summary"]["token_output"])
        self.assertEqual(120, long_card["budget_summary"]["runtime_seconds"])
        self.assertEqual(["send_correction", "view_logs", "wait", "cancel_attempt"], [action["id"] for action in long_card["actions"]])
        blocked_card = task_cards["task-cockpit-blocked"]
        self.assertEqual("blocked", blocked_card["state"])
        self.assertEqual("needs owner input", blocked_card["blocker"])
        self.assertEqual(["send_correction", "view_logs", "retry", "reassign"], [action["id"] for action in blocked_card["actions"]])
        invalid_card = task_cards["task-cockpit-done-missing-evidence"]
        self.assertEqual("completion_invalid", invalid_card["state"])
        self.assertTrue(invalid_card["completion_invalid"])
        self.assertEqual("missing_final_evidence", invalid_card["completion_invalid_reason"])
        self.assertEqual(0, invalid_card["final_evidence_count"])
        self.assertEqual(["review_task", "view_trace"], [action["id"] for action in invalid_card["actions"]])
        project_cost = next(item for item in cockpit["projects"] if item["id"] == "project-cockpit-cost")
        self.assertEqual({"USD": 1.2}, project_cost["budget_by_currency"])
        self.assertEqual(1, project_cost["budget_event_count"])
        self.assertEqual(3000, project_cost["token_input"])
        self.assertEqual(700, project_cost["token_output"])
        self.assertEqual(120, project_cost["runtime_seconds"])
        invalid_task = next(item for item in cockpit["completion_invalid_tasks"] if item["task_id"] == "task-cockpit-done-missing-evidence")
        self.assertTrue(invalid_task["completion_invalid"])
        self.assertEqual("missing_final_evidence", invalid_task["completion_invalid_reason"])
        self.assertEqual(0, invalid_task["final_evidence_count"])
        self.assertEqual(["review_task", "view_trace"], [action["id"] for action in invalid_task["actions"]])
        recent_evidence = next(item for item in cockpit["recent_evidence"] if item["task_id"] == "task-cockpit-done")
        self.assertEqual(promoted["evidence"]["evidence_id"], recent_evidence["evidence_id"])
        self.assertTrue(recent_evidence["evidence"]["allowed"])
        self.assertFalse(recent_evidence["evidence"]["absolute_path_exposed"])
        self.assertIn("artifacts/done-final.md", recent_evidence["evidence"]["relative_path"])
        legacy_evidence = next(item for item in cockpit["legacy_task_evidence"] if item["task_id"] == "task-cockpit-long")
        self.assertTrue(legacy_evidence["evidence"]["allowed"])
        self.assertIn("evidence/result.md", legacy_evidence["evidence"]["relative_path"])
        attention = next(item for item in cockpit["owner_attention"] if item["task_id"] == "task-cockpit-long" and item["kind"] == "stagnant_task")
        self.assertEqual("stagnant_task", attention["kind"])
        self.assertEqual("correcting", attention["state"])
        self.assertEqual("codex-cockpit", attention["target_agent"])
        self.assertEqual(attempt_id, attention["attempt_id"])
        self.assertTrue(attention["correction"]["needs_ack"])
        self.assertEqual("hermes", attention["correction"]["last_by"])
        self.assertIn("请收口 evidence", attention["correction"]["last_message"])
        self.assertIn("Hermes 已发纠偏", attention["message"])
        self.assertIn("supervisor_activity", cockpit)
        supervisor_item = next(item for item in cockpit["supervisor_activity"] if item["task_id"] == "task-cockpit-long")
        self.assertEqual("correction_pending_ack", supervisor_item["kind"])
        self.assertEqual("Hermes", supervisor_item["supervisor"])
        self.assertEqual("codex-cockpit", supervisor_item["target_agent"])
        self.assertEqual(attempt_id, supervisor_item["attempt_id"])
        self.assertIn("请收口 evidence", supervisor_item["message"])
        self.assertEqual(
            ["send_correction", "view_logs", "wait", "cancel_attempt"],
            [action["id"] for action in attention["actions"]],
        )
        action_meta = {action["id"]: action for action in attention["actions"]}
        self.assertEqual("POST", action_meta["send_correction"]["method"])
        self.assertTrue(action_meta["send_correction"]["requires_owner_approval"])
        self.assertEqual("GET", action_meta["view_logs"]["method"])
        self.assertEqual("none", action_meta["wait"]["method"])
        self.assertTrue(action_meta["cancel_attempt"]["dangerous"])
        self.assertTrue(all(action["task_id"] == "task-cockpit-long" for action in attention["actions"]))
        self.assertTrue(all(action["attempt_id"] == attempt_id for action in attention["actions"]))
        blocked_attention = next(item for item in cockpit["owner_attention"] if item["task_id"] == "task-cockpit-blocked" and item["kind"] == "blocked_task")
        self.assertEqual(["send_correction", "view_logs", "retry", "reassign"], [action["id"] for action in blocked_attention["actions"]])
        self.assertTrue(all(action["task_id"] == "task-cockpit-blocked" for action in blocked_attention["actions"]))
        approval_attention = next(item for item in cockpit["owner_attention"] if item["approval_id"] == "approval-cockpit-awaiting")
        self.assertEqual("approval", approval_attention["kind"])
        self.assertEqual("task-cockpit-awaiting-approval", approval_attention["task_id"])
        self.assertEqual("codex-cockpit", approval_attention["target_agent"])
        self.assertEqual("external_send", approval_attention["approval_action"])
        self.assertEqual("P1", approval_attention["risk"])
        self.assertIn("approval bound to cockpit task", approval_attention["message"])
        self.assertEqual(["approve", "deny", "mock_resolve"], [action["id"] for action in approval_attention["actions"]])
        approval_action_meta = {action["id"]: action for action in approval_attention["actions"]}
        self.assertTrue(approval_action_meta["approve"]["requires_owner_approval"])
        self.assertFalse(approval_action_meta["approve"]["dry_run_default"])
        self.assertTrue(approval_action_meta["mock_resolve"]["dry_run_default"])
        self.assertTrue(approval_action_meta["deny"]["requires_owner_approval"])
        self.assertTrue(all(action["task_id"] == "task-cockpit-awaiting-approval" for action in approval_attention["actions"]))
        self.assertTrue(all(action["approval_id"] == "approval-cockpit-awaiting" for action in approval_attention["actions"]))
        budget_attention = next(item for item in cockpit["owner_attention"] if item["approval_id"] == "approval-cockpit-budget-overrun")
        self.assertEqual("approval", budget_attention["kind"])
        self.assertEqual("budget_overrun", budget_attention["approval_action"])
        self.assertEqual("P0", budget_attention["risk"])
        self.assertEqual("hard_exceeded", budget_attention["budget"]["limit_status"])
        self.assertEqual(1.2, budget_attention["budget"]["amount"])
        self.assertEqual("USD", budget_attention["budget"]["currency"])
        self.assertIn("预算超限", budget_attention["message"])
        self.assertTrue(all(action["requires_owner_approval"] for action in budget_attention["actions"] if action["id"] in {"approve", "deny"}))
        evidence_attention = next(item for item in cockpit["owner_attention"] if item["task_id"] == "task-cockpit-done-missing-evidence" and item["kind"] == "evidence_issue")
        self.assertEqual("evidence_issue", evidence_attention["kind"])
        self.assertEqual("blocked", evidence_attention["state"])
        self.assertEqual("completed_without_evidence", evidence_attention["reason"])
        self.assertIn("done 但缺少 final evidence", evidence_attention["message"])
        self.assertEqual(["review_task", "view_trace"], [action["id"] for action in evidence_attention["actions"]])

        status, shown = api_gateway.route_get("/v1/tasks/task-cockpit-long", {})
        self.assertEqual(200, status, shown)
        self.assertTrue(shown["evidence"]["allowed"])
        self.assertFalse(shown["evidence"]["absolute_path_exposed"])
        self.assertNotIn(str(self.root), json.dumps(shown["evidence"], ensure_ascii=False))

    def test_cockpit_ignores_orphan_active_attempts_on_finished_tasks(self) -> None:
        code, created = run_cli("employee", "create", "--id", "main", "--name", "main", "--role", "operator", "--runtime", "openclaw", "--workspace", str(self.root / "workspace" / "main"))
        self.assertEqual(0, code, created)
        code, created = run_cli("employee", "create", "--id", "skill-worker", "--name", "Skill Worker", "--role", "skill-worker", "--runtime", "skill", "--workspace", str(self.root / "workspace" / "skill-worker"))
        self.assertEqual(0, code, created)
        conn = companyctl.connect()
        try:
            conn.execute("UPDATE employees SET status = 'active', updated_at = ? WHERE id = ?", (companyctl.now(), "skill-worker"))
            conn.commit()
        finally:
            conn.close()
        code, heartbeat = run_cli("heartbeat", "--agent", "skill-worker")
        self.assertEqual(0, code, heartbeat)
        task_id = "task-finished-orphan-attempt"
        code, submitted = run_cli("task", "submit", "--from", "main", "--to", "skill-worker", "--task-id", task_id, "--title", "Finished task with old attempt")
        self.assertEqual(0, code, submitted)
        code, run = run_cli("task", "run", "--task-id", task_id, "--agent", "skill-worker", "--by", "main", "--adapter-type", "retry")
        self.assertEqual(0, code, run)
        conn = companyctl.connect()
        try:
            workspace = companyctl.ensure_task_workspace(conn, task_id)
            final_path = Path(workspace["path"]) / "final" / "result.md"
            final_path.write_text("finished task evidence\n", encoding="utf-8")
            artifact = companyctl.register_artifact_internal(
                conn,
                task_id=task_id,
                employee_id="skill-worker",
                artifact_type="text",
                path=str(final_path),
                name="result.md",
                stage="final",
                summary="finished task evidence",
                is_final=True,
            )
            companyctl.promote_artifact_to_evidence_internal(
                conn,
                artifact_id=artifact["artifact"]["artifact_id"],
                by="skill-worker",
                summary="finished task evidence",
            )
            conn.execute("UPDATE tasks SET status = 'completed', claimed_by = 'skill-worker', summary = 'done', updated_at = ? WHERE id = ?", (companyctl.now(), task_id))
            conn.commit()
        finally:
            conn.close()

        status, cockpit = api_gateway.route_get("/v1/dashboard/cockpit", {})
        self.assertEqual(200, status, cockpit)
        self.assertEqual(0, cockpit["counts"]["active_attempts"])
        employee = next(item for item in cockpit["employees"] if item["id"] == "skill-worker")
        self.assertEqual("active", employee["status"])
        self.assertEqual({}, employee["current_attempt"])

    def test_api_gateway_lists_sanitized_evidence_records_and_filters_by_employee(self) -> None:
        code, created = run_cli("employee", "create", "--id", "main", "--name", "main", "--role", "operator", "--runtime", "openclaw", "--workspace", str(self.root / "workspace" / "main"))
        self.assertEqual(0, code, created)
        for employee_id in ("writer", "qa"):
            code, created = run_cli("employee", "create", "--id", employee_id, "--name", employee_id, "--role", employee_id, "--runtime", "local", "--workspace", str(self.root / "workspace" / employee_id))
            self.assertEqual(0, code, created)
            self.mark_active(employee_id)
        code, submitted = run_cli("task", "submit", "--from", "main", "--to", "writer", "--task-id", "task-api-evidence-list", "--title", "Evidence list")
        self.assertEqual(0, code, submitted)
        code, qa_submitted = run_cli("task", "submit", "--from", "main", "--to", "qa", "--task-id", "task-api-evidence-list-qa", "--title", "Evidence list QA")
        self.assertEqual(0, code, qa_submitted)
        conn = companyctl.connect()
        try:
            workspace = companyctl.ensure_task_workspace(conn, "task-api-evidence-list")
            final_path = Path(workspace["path"]) / "final" / "delivery.md"
            final_path.write_text("final delivery\n", encoding="utf-8")
            artifact = companyctl.register_artifact_internal(
                conn,
                task_id="task-api-evidence-list",
                employee_id="writer",
                artifact_type="text",
                path=str(final_path),
                summary="final delivery",
                stage="final",
                is_final=True,
            )["artifact"]
            promoted = companyctl.promote_artifact_to_evidence_internal(conn, artifact_id=artifact["artifact_id"], by="writer", summary="safe final evidence")
            qa_workspace = companyctl.ensure_task_workspace(conn, "task-api-evidence-list-qa")
            qa_path = Path(qa_workspace["path"]) / "final" / "qa-delivery.md"
            qa_path.write_text("qa delivery\n", encoding="utf-8")
            qa_artifact = companyctl.register_artifact_internal(
                conn,
                task_id="task-api-evidence-list-qa",
                employee_id="qa",
                artifact_type="text",
                path=str(qa_path),
                summary="qa final delivery",
                stage="final",
                is_final=True,
            )["artifact"]
            qa_promoted = companyctl.promote_artifact_to_evidence_internal(conn, artifact_id=qa_artifact["artifact_id"], by="qa", summary="qa safe final evidence")
            conn.commit()
        finally:
            conn.close()

        status, evidence = api_gateway.route_get("/v1/evidence", {"limit": ["10"]})
        self.assertEqual(200, status, evidence)
        self.assertTrue(evidence["ok"])
        self.assertIn("/v1/evidence", evidence["source"])
        item = next(row for row in evidence["evidence"] if row["evidence_id"] == promoted["evidence"]["evidence_id"])
        self.assertEqual("task-api-evidence-list", item["task_id"])
        self.assertTrue(item["is_final"])
        self.assertTrue(item["display"]["allowed"])
        self.assertFalse(item["display"]["absolute_path_exposed"])
        self.assertEqual(["artifacts", "evidence", "final", "reports"], item["display"]["policy"]["allowed_segments"])
        self.assertEqual("sensitive_path_tokens_redacted", item["display"]["policy"]["forbidden_policy"])
        self.assertIn("workspace/evidence/reports/artifacts/final only", item["display"]["policy"]["summary"])
        payload_json = json.dumps(item, ensure_ascii=False)
        self.assertIn("artifacts/delivery.md", payload_json)
        self.assertIn("v1-delivery.md", payload_json)
        self.assertNotIn(str(self.root), payload_json)
        self.assertNotIn("path_or_url", payload_json)

        status, writer_evidence = api_gateway.route_get("/v1/evidence", {"employee_id": ["writer"], "limit": ["10"]})
        self.assertEqual(200, status, writer_evidence)
        self.assertTrue(writer_evidence["ok"])
        writer_ids = [row["evidence_id"] for row in writer_evidence["evidence"]]
        self.assertIn(promoted["evidence"]["evidence_id"], writer_ids)
        self.assertNotIn(qa_promoted["evidence"]["evidence_id"], writer_ids)
        self.assertTrue(all(row["employee_id"] == "writer" for row in writer_evidence["evidence"]))
        self.assertEqual("writer", writer_evidence["filters"]["employee_id"])

    def test_api_gateway_lists_sanitized_artifacts_and_handoffs(self) -> None:
        for employee_id, role in [("main", "operator"), ("writer", "writer"), ("qa", "qa")]:
            code, created = run_cli("employee", "create", "--id", employee_id, "--name", employee_id, "--role", role, "--runtime", "local", "--workspace", str(self.root / "workspace" / employee_id))
            self.assertEqual(0, code, created)
            if employee_id != "main":
                self.mark_active(employee_id)
        code, submitted = run_cli("task", "submit", "--from", "main", "--to", "writer", "--task-id", "task-api-artifact-source", "--title", "Artifact source")
        self.assertEqual(0, code, submitted)
        code, qa_task = run_cli("task", "submit", "--from", "main", "--to", "qa", "--task-id", "task-api-artifact-qa", "--title", "Artifact QA")
        self.assertEqual(0, code, qa_task)
        conn = companyctl.connect()
        try:
            workspace = companyctl.ensure_task_workspace(conn, "task-api-artifact-source")
            report_path = Path(workspace["path"]) / "work" / "brief.md"
            report_path.write_text("brief artifact\n", encoding="utf-8")
            artifact = companyctl.register_artifact_internal(
                conn,
                task_id="task-api-artifact-source",
                employee_id="writer",
                path=str(report_path),
                artifact_type="markdown",
                summary="brief artifact",
                stage="intermediate",
            )["artifact"]
            handoff = companyctl.create_handoff_internal(
                conn,
                from_task_id="task-api-artifact-source",
                to_task_id="task-api-artifact-qa",
                from_employee_id="writer",
                to_employee_id="qa",
                summary="handoff to qa",
                artifacts=[artifact["artifact_id"]],
                known_issues="none",
                next_steps="review artifact",
                required_actions="accept or reject",
                acceptance_notes="qa notes",
            )["handoff"]
            conn.commit()
        finally:
            conn.close()

        status, artifacts = api_gateway.route_get("/v1/artifacts", {"limit": ["10"]})
        self.assertEqual(200, status, artifacts)
        artifact_item = next(item for item in artifacts["artifacts"] if item["artifact_id"] == artifact["artifact_id"])
        self.assertEqual("task-api-artifact-source", artifact_item["task_id"])
        self.assertTrue(artifact_item["display"]["allowed"])
        self.assertFalse(artifact_item["display"]["absolute_path_exposed"])
        artifact_json = json.dumps(artifact_item, ensure_ascii=False)
        self.assertIn("artifacts/brief.md", artifact_json)
        self.assertNotIn(str(self.root), artifact_json)
        self.assertNotIn("path", artifact_item)

        status, handoffs = api_gateway.route_get("/v1/handoffs", {"limit": ["10"]})
        self.assertEqual(200, status, handoffs)
        handoff_item = next(item for item in handoffs["handoffs"] if item["handoff_id"] == handoff["handoff_id"])
        self.assertEqual("task-api-artifact-source", handoff_item["from_task_id"])
        self.assertEqual("task-api-artifact-qa", handoff_item["to_task_id"])
        self.assertEqual([artifact["artifact_id"]], handoff_item["artifacts"])
        self.assertNotIn("artifacts_json", handoff_item)

    def test_api_gateway_lists_sanitized_failure_records(self) -> None:
        for employee_id, role in [("main", "operator"), ("codex", "developer")]:
            code, created = run_cli("employee", "create", "--id", employee_id, "--name", employee_id, "--role", role, "--runtime", "local", "--workspace", str(self.root / "workspace" / employee_id))
            self.assertEqual(0, code, created)
            if employee_id != "main":
                self.mark_active(employee_id)
        code, submitted = run_cli("task", "submit", "--from", "main", "--to", "codex", "--task-id", "task-api-failure-ledger", "--title", "Failure ledger")
        self.assertEqual(0, code, submitted)
        code, run = run_cli("task", "run", "--task-id", "task-api-failure-ledger", "--agent", "codex", "--by", "hermes")
        self.assertEqual(0, code, run)
        secret = "sk-testSECRET1234567890"
        code, finished = run_cli("task", "attempt", "finish", "--attempt-id", run["attempt"]["attempt_id"], "--status", "failed", "--error", f"failed with token={secret} at {self.root / '.env'}")
        self.assertEqual(0, code, finished)
        code, blocked = run_cli("task", "block", "--agent", "codex", "--task-id", "task-api-failure-ledger", "--blocker", f"blocked with api_key={secret} /Users/shift/.ssh/id_rsa")
        self.assertEqual(0, code, blocked)
        conn = companyctl.connect()
        try:
            conn.execute(
                """
                INSERT INTO adapter_runs(id, trace_id, agent_id, task_id, command, ok, processed, attempt, result_json, created_at)
                VALUES ('adapter-run-failure-ledger', ?, 'codex', 'task-api-failure-ledger', 'company-codex-adapter', 0, 0, 1, ?, ?)
                """,
                (
                    submitted["task"]["metadata"]["trace_id"],
                    json.dumps({"stderr": f"api_key={secret} reading /Users/shift/.ssh/id_rsa", "stdout": "safe failure context"}, ensure_ascii=False),
                    companyctl.now(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        status, failures = api_gateway.route_get("/v1/failures", {"limit": ["20"]})
        self.assertEqual(200, status, failures)
        self.assertTrue(failures["ok"])
        kinds = [item["kind"] for item in failures["failures"]]
        self.assertIn("task", kinds)
        self.assertIn("attempt", kinds)
        self.assertIn("adapter_run", kinds)
        failures_json = json.dumps(failures, ensure_ascii=False)
        self.assertIn("task-api-failure-ledger", failures_json)
        self.assertIn("safe failure context", failures_json)
        self.assertNotIn(secret, failures_json)
        self.assertNotIn("id_rsa", failures_json)
        self.assertNotIn(".env", failures_json)

    def test_cli_audit_ledgers_match_api_sanitized_records(self) -> None:
        for employee_id, role in [("main", "operator"), ("writer", "writer"), ("qa", "qa"), ("codex", "developer")]:
            code, created = run_cli("employee", "create", "--id", employee_id, "--name", employee_id, "--role", role, "--runtime", "local", "--workspace", str(self.root / "workspace" / employee_id))
            self.assertEqual(0, code, created)
            if employee_id != "main":
                self.mark_active(employee_id)
        code, source_task = run_cli("task", "submit", "--from", "main", "--to", "writer", "--task-id", "task-cli-audit-source", "--title", "CLI audit source")
        self.assertEqual(0, code, source_task)
        code, qa_task = run_cli("task", "submit", "--from", "main", "--to", "qa", "--task-id", "task-cli-audit-qa", "--title", "CLI audit qa")
        self.assertEqual(0, code, qa_task)
        code, failure_task = run_cli("task", "submit", "--from", "main", "--to", "codex", "--task-id", "task-cli-audit-failure", "--title", "CLI audit failure")
        self.assertEqual(0, code, failure_task)
        code, run = run_cli("task", "run", "--task-id", "task-cli-audit-failure", "--agent", "codex", "--by", "hermes")
        self.assertEqual(0, code, run)
        secret = "sk-cliAuditSECRET1234567890"
        code, finished = run_cli("task", "attempt", "finish", "--attempt-id", run["attempt"]["attempt_id"], "--status", "failed", "--error", f"failed with token={secret} at {self.root / '.env'}")
        self.assertEqual(0, code, finished)
        code, blocked = run_cli("task", "block", "--agent", "codex", "--task-id", "task-cli-audit-failure", "--blocker", f"blocked with api_key={secret} /Users/shift/.ssh/id_rsa")
        self.assertEqual(0, code, blocked)

        conn = companyctl.connect()
        try:
            workspace = companyctl.ensure_task_workspace(conn, "task-cli-audit-source")
            final_path = Path(workspace["path"]) / "final" / "delivery.md"
            final_path.write_text("delivery\n", encoding="utf-8")
            artifact = companyctl.register_artifact_internal(
                conn,
                task_id="task-cli-audit-source",
                employee_id="writer",
                path=str(final_path),
                artifact_type="markdown",
                summary="delivery artifact",
                stage="final",
                is_final=True,
            )["artifact"]
            handoff = companyctl.create_handoff_internal(
                conn,
                from_task_id="task-cli-audit-source",
                to_task_id="task-cli-audit-qa",
                from_employee_id="writer",
                to_employee_id="qa",
                summary="handoff for cli audit",
                artifacts=[artifact["artifact_id"]],
                next_steps="review",
            )["handoff"]
            evidence = companyctl.promote_artifact_to_evidence_internal(conn, artifact_id=artifact["artifact_id"], by="writer", summary="delivery evidence")["evidence"]
            conn.execute(
                """
                INSERT INTO adapter_runs(id, trace_id, agent_id, task_id, command, ok, processed, attempt, result_json, created_at)
                VALUES ('adapter-run-cli-audit-failure', ?, 'codex', 'task-cli-audit-failure', 'company-codex-adapter', 0, 0, 1, ?, ?)
                """,
                (
                    failure_task["task"]["metadata"]["trace_id"],
                    json.dumps({"stderr": f"api_key={secret} reading /Users/shift/.ssh/id_rsa", "stdout": "safe cli failure context"}, ensure_ascii=False),
                    companyctl.now(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        for command, payload_key, api_path in [
            ("evidence", "evidence", "/v1/evidence"),
            ("artifacts", "artifacts", "/v1/artifacts"),
            ("handoffs", "handoffs", "/v1/handoffs"),
            ("failures", "failures", "/v1/failures"),
        ]:
            code, cli_payload = run_cli("audit", command, "--limit", "20")
            self.assertEqual(0, code, cli_payload)
            status, api_payload = api_gateway.route_get(api_path, {"limit": ["20"]})
            self.assertEqual(200, status, api_payload)
            self.assertEqual(api_payload[payload_key], cli_payload[payload_key])

        code, cli_evidence = run_cli("audit", "evidence", "--task-id", "task-cli-audit-source", "--limit", "20")
        self.assertEqual(0, code, cli_evidence)
        self.assertEqual([evidence["evidence_id"]], [item["evidence_id"] for item in cli_evidence["evidence"]])
        code, cli_artifacts = run_cli("audit", "artifacts", "--task-id", "task-cli-audit-source", "--limit", "20")
        self.assertEqual(0, code, cli_artifacts)
        self.assertEqual([artifact["artifact_id"]], [item["artifact_id"] for item in cli_artifacts["artifacts"]])
        code, cli_handoffs = run_cli("audit", "handoffs", "--task-id", "task-cli-audit-source", "--limit", "20")
        self.assertEqual(0, code, cli_handoffs)
        self.assertEqual([handoff["handoff_id"]], [item["handoff_id"] for item in cli_handoffs["handoffs"]])
        code, cli_failures = run_cli("audit", "failures", "--task-id", "task-cli-audit-failure", "--limit", "20")
        self.assertEqual(0, code, cli_failures)
        payload_json = json.dumps({**cli_evidence, **cli_artifacts, **cli_handoffs, **cli_failures}, ensure_ascii=False)
        self.assertIn("task-cli-audit-source", payload_json)
        self.assertIn("safe cli failure context", payload_json)
        self.assertNotIn(str(self.root), payload_json)
        self.assertNotIn(secret, payload_json)
        self.assertNotIn("id_rsa", payload_json)
        self.assertNotIn(".env", payload_json)
        self.assertNotIn("path_or_url", payload_json)
        self.assertNotIn("artifacts_json", payload_json)

    def test_api_gateway_streams_company_events_as_sse(self) -> None:
        conn = companyctl.connect()
        try:
            companyctl.record_event(
                conn,
                "task.progress",
                "codex",
                task_id="task-sse",
                payload={
                    "message": "stream me api_key=sk-test-secret",
                    "path": str(self.root / ".ssh" / "id_rsa"),
                    "files": [str(self.root / ".env")],
                },
                trace_id="trace-sse",
            )
        finally:
            conn.close()

        handler = object.__new__(api_gateway.ApiHandler)
        handler.headers = {}
        handler.wfile = io.BytesIO()
        sent = []

        def fake_send_response(code):
            sent.append(("status", code))

        def fake_send_header(name, value):
            sent.append((name, value))

        handler.send_response = fake_send_response
        handler.send_header = fake_send_header
        handler.end_headers = lambda: sent.append(("end", ""))
        handler.server = SimpleNamespace(quiet=True)

        with mock.patch.object(api_gateway.time, "sleep", return_value=None):
            handler.stream_events({"max_cycles": ["1"], "poll_seconds": ["1"], "limit": ["5"]})

        output = handler.wfile.getvalue().decode("utf-8")
        self.assertIn(("status", HTTPStatus.OK), sent)
        self.assertIn(("Content-Type", "text/event-stream; charset=utf-8"), sent)
        self.assertIn("event: stream_status", output)
        self.assertIn("event: company_event", output)
        self.assertIn("task.progress", output)
        self.assertIn("single_company_kernel_ledger", output)
        self.assertIn("sync_wait_window", output)
        self.assertIn("task_failure_decided_by_attempt_evidence", output)
        self.assertNotIn("/Users/shift", output)
        self.assertNotIn("id_rsa", output)
        self.assertNotIn(".env", output)
        self.assertNotIn("sk-test-secret", output)

    def test_api_gateway_sse_resumes_after_last_event_id_without_replaying_old_events(self) -> None:
        conn = companyctl.connect()
        try:
            first = companyctl.record_event(conn, "task.progress", "codex", task_id="task-sse-resume", payload={"message": "old event"}, trace_id="trace-sse-resume")
            second = companyctl.record_event(conn, "task.progress", "codex", task_id="task-sse-resume", payload={"message": "new event"}, trace_id="trace-sse-resume")
        finally:
            conn.close()

        handler = object.__new__(api_gateway.ApiHandler)
        handler.headers = {"Last-Event-ID": first["id"]}
        handler.wfile = io.BytesIO()
        sent = []
        handler.send_response = lambda code: sent.append(("status", code))
        handler.send_header = lambda name, value: sent.append((name, value))
        handler.end_headers = lambda: sent.append(("end", ""))
        handler.server = SimpleNamespace(quiet=True)

        with mock.patch.object(api_gateway.time, "sleep", return_value=None):
            handler.stream_events({"max_cycles": ["1"], "poll_seconds": ["0"], "limit": ["5"]})

        output = handler.wfile.getvalue().decode("utf-8")
        self.assertIn("event: stream_status", output)
        self.assertIn("timeout_is_sync_wait_only", output)
        self.assertIn(f"id: {second['id']}", output)
        self.assertIn("new event", output)
        self.assertNotIn(f"id: {first['id']}", output)
        self.assertNotIn("old event", output)

    def test_evidence_sanitizer_blocks_absolute_secret_paths(self) -> None:
        secret = self.root / ".ssh" / "id_rsa"
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.write_text("secret\n", encoding="utf-8")

        result = companyctl.sanitize_evidence_path_for_display(str(secret))

        self.assertFalse(result["allowed"])
        self.assertTrue(result["exists"])
        self.assertEqual("", result["relative_path"])
        self.assertEqual("forbidden secret/config path", result["reason"])
        self.assertFalse(result["absolute_path_exposed"])

    def test_log_sanitizer_redacts_secrets_and_sensitive_paths(self) -> None:
        secret = "sk-testSECRET1234567890"
        raw = (
            f"api_key={secret} stdout /Users/shift/.ssh/id_rsa "
            f"{self.root / '.env'} normal progress token=plain-secret-value"
        )

        redacted = companyctl.sanitize_log_text(raw)

        self.assertIn("api_key=[REDACTED]", redacted)
        self.assertIn("token=[REDACTED]", redacted)
        self.assertIn("normal progress", redacted)
        self.assertNotIn(secret, redacted)
        self.assertNotIn("plain-secret-value", redacted)
        self.assertNotIn("id_rsa", redacted)
        self.assertNotIn(".env", redacted)
        self.assertIn("[REDACTED_PATH]", redacted)

    def test_log_sanitizer_does_not_redact_task_ids_containing_sk(self) -> None:
        task_id = "task-20260607-043615-365dea"

        redacted = companyctl.sanitize_log_text(f"completed task_id={task_id}")

        self.assertIn(task_id, redacted)

    def test_advanced_dashboard_counts_visible_ai_employees_not_human_owner(self) -> None:
        summary = {
            "generated_at": companyctl.now(),
            "counts": {"employees": 2, "active_employees": 2, "candidate_employees": 0, "archived_employees": 0},
            "employees": [
                {
                    "id": "owner-shift",
                    "name": "Shift Shen",
                    "role": "human-owner",
                    "runtime": "human",
                    "status": "active",
                    "workspace": str(self.root / "workspace" / "owner-shift"),
                    "last_seen_at": "",
                    "current_attempt": {},
                },
                {
                    "id": "codex",
                    "name": "Codex",
                    "role": "developer",
                    "runtime": "codex",
                    "status": "active",
                    "workspace": str(self.root / "workspace" / "codex"),
                    "last_seen_at": companyctl.now(),
                    "current_attempt": {},
                },
            ],
            "tasks": [],
            "direct_messages_recent": [],
            "external_threads": [],
            "adapter_runs": [],
            "progress_notifications_recent": [],
            "supervisor_loop": {},
            "internal_watchdog": {},
        }
        prepared = company_dashboard.advanced_summary(summary)
        self.assertEqual(1, prepared["counts"]["employees"])
        self.assertEqual(1, prepared["counts"]["active_employees"])
        self.assertEqual(["codex"], [employee["id"] for employee in prepared["employees"]])
        cockpit = company_dashboard.build_cockpit_summary(prepared)
        self.assertEqual(2, cockpit["registry_reconciliation"]["registered_total"])
        self.assertEqual(1, cockpit["registry_reconciliation"]["schedulable_total"])
        self.assertEqual(1, cockpit["registry_reconciliation"]["excluded_human_owners"])
        self.assertEqual(["owner-shift"], cockpit["registry_reconciliation"]["excluded_employee_ids"])
        self.assertEqual(1, cockpit["counts"]["employees_total"])
        self.assertEqual(2, cockpit["counts"]["registered_employees_total"])
        self.assertEqual(1, cockpit["counts"]["excluded_human_owners"])

    def test_dashboard_auto_variant_falls_back_to_basic_when_template_missing(self) -> None:
        output = self.root / "state" / "dashboard-auto-fallback.html"
        missing_template = self.root / "dashboard_templates" / "missing-dashboard.html"
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = company_dashboard.main(["--output", str(output), "--template", str(missing_template)])
        self.assertEqual(0, code)
        payload = json.loads(stdout.getvalue())
        self.assertEqual("basic", payload["variant"])
        html = output.read_text(encoding="utf-8")
        self.assertIn("Company Kernel Dashboard", html)
        self.assertNotIn("window.companyApiBase", html)

    def test_dashboard_cli_output_exposes_ledger_consistency(self) -> None:
        output = self.root / "state" / "dashboard-cli-ledger.html"
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = company_dashboard.main(["--output", str(output), "--variant", "advanced"])
        self.assertEqual(0, code)
        payload = json.loads(stdout.getvalue())
        self.assertEqual("advanced", payload["variant"])
        self.assertEqual("single_company_kernel_ledger", payload["ledger_consistency"]["source"])
        self.assertEqual(["api", "cli", "dashboard"], payload["ledger_consistency"]["surfaces"])
        self.assertEqual("API / CLI / Dashboard read the same Company Kernel ledger", payload["ledger_consistency"]["summary"])

    def test_dashboard_summary_includes_direct_messages_recent(self) -> None:
        code, created = run_cli("employee", "create", "--id", "main", "--name", "main", "--role", "operator", "--runtime", "openclaw", "--workspace", str(self.root / "workspace" / "main"))
        self.assertEqual(code, 0, created)
        code, worker = run_cli("employee", "create", "--id", "worker-x", "--name", "worker-x", "--role", "operator", "--runtime", "local", "--workspace", str(self.root / "workspace" / "worker-x"))
        self.assertEqual(code, 0, worker)
        conn = companyctl.connect()
        try:
            conn.execute(
                """
                INSERT INTO messages(id, source_agent, target_agent, body, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("msg-dashboard-direct-001", "main", "worker-x", "dashboard direct ping", "2026-06-04T22:45:00+07:00"),
            )
            conn.commit()
            summary = company_dashboard.load_summary(conn)
        finally:
            conn.close()

        self.assertIn("direct_messages_recent", summary)
        self.assertEqual(1, len(summary["direct_messages_recent"]))
        recent = summary["direct_messages_recent"][0]
        self.assertEqual("msg-dashboard-direct-001", recent["id"])
        self.assertEqual("main", recent["source_agent"])
        self.assertEqual("worker-x", recent["target_agent"])
        self.assertEqual("dashboard direct ping", recent["body"])
        self.assertEqual("", recent["evidence_path"])
        self.assertEqual("2026-06-04T22:45:00+07:00", recent["created_at"])
        self.assertEqual("", recent["task_context"])
        self.assertFalse(recent["task_bound"])
        self.assertFalse(recent["low_signal"])
        self.assertEqual("work_relevant", recent["chat_classification"])
        status, api_payload = api_gateway.route_get("/v1/messages/recent-direct", {"limit": ["5"]})
        self.assertEqual(200, int(status))
        self.assertEqual(summary["direct_messages_recent"], api_payload["direct_messages_recent"])

    def test_external_mirror_bridge_contract_requires_owner_bridge_and_cursor_idempotent(self) -> None:
        missing_owner = {
            "thread": {"id": "ext-missing-owner", "platform": "telegram", "bridge_agent": "telegram-bridge"},
            "messages": [],
        }
        status, rejected = api_gateway.route_post("/v1/external-mirror/import", missing_owner)
        self.assertEqual(400, int(status))
        self.assertIn("owner_agent", rejected["error"])

        payload = {
            "thread": {
                "id": "ext-telegram-hermes-002",
                "platform": "telegram",
                "account_id": "home",
                "external_chat_id": "CHAT_ID_PLACEHOLDER",
                "owner_agent": "hermes",
                "bridge_agent": "telegram-bridge",
                "title": "Shift ↔ Hermes",
            },
            "cursor": {"id": "telegram-home-hermes", "value": "cursor-001", "state": {"page": 1}},
            "messages": [
                {
                    "id": "ext-msg-002",
                    "direction": "inbound",
                    "platform": "telegram",
                    "sender_kind": "user",
                    "sender_id": "shift",
                    "body": "继续开发",
                    "created_at": "2026-06-05T01:45:00+07:00",
                    "company_message_id": "",
                    "conversation_message_id": "",
                }
            ],
        }
        status, imported = api_gateway.route_post("/v1/external-mirror/import", payload)
        self.assertEqual(201, int(status))
        self.assertEqual(1, imported["imported_messages"])
        self.assertEqual("telegram-home-hermes", imported["cursor_id"])
        status, imported_again = api_gateway.route_post("/v1/external-mirror/import", payload)
        self.assertEqual(201, int(status))
        self.assertEqual(0, imported_again["imported_messages"])
        conn = companyctl.connect()
        try:
            cursor = conn.execute("SELECT * FROM external_ingest_cursors WHERE id = ?", ("telegram-home-hermes",)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(cursor)
        self.assertEqual("cursor-001", cursor["cursor_value"])

    def test_adapter_run_summary_reads_progress_report_state_and_task_id(self) -> None:
        code, main = run_cli("employee", "create", "--id", "main", "--name", "main", "--role", "operator", "--runtime", "local", "--workspace", str(self.root / "workspace" / "main"))
        self.assertEqual(code, 0, main)
        progress_dir = self.root / "employees" / "codex" / "reports" / "task-adapter-progress"
        progress_dir.mkdir(parents=True, exist_ok=True)
        progress_path = progress_dir / "progress_in_progress_20260605.json"
        progress_path.write_text(
            json.dumps(
                {
                    "ok": True,
                    "task_id": "task-adapter-progress",
                    "report": {"state": "in_progress", "project": "super-ai-company-kernel", "action": "testing adapter progress"},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        conn = companyctl.connect()
        try:
            conn.execute(
                """
                INSERT INTO tasks(id, source_agent, target_agent, title, description, priority, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("task-adapter-progress", "main", "codex", "adapter progress", "", "P2", "blocked", "2026-06-05T01:00:00+07:00", "2026-06-05T01:00:00+07:00"),
            )
            conn.execute(
                """
                INSERT INTO adapter_runs(id, agent_id, task_id, command, ok, processed, result_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "adapter-run-progress-001",
                    "codex",
                    "task-adapter-progress",
                    "company-codex-adapter",
                    0,
                    1,
                    json.dumps({"runs": [{"index": 0, "parsed_stdout": {"task_id": "task-adapter-progress", "status": "blocked", "report": str(progress_path)}}]}, ensure_ascii=False),
                    "2026-06-05T01:02:00+07:00",
                ),
            )
            conn.commit()
        finally:
            conn.close()
        code, shown = run_cli("runtime", "adapter-run", "show", "--run-id", "adapter-run-progress-001", "--summary")
        self.assertEqual(0, code, shown)
        run_summary = shown["result_summary"]["runs"][0]
        self.assertEqual(str(progress_path), run_summary["report"])
        self.assertEqual("in_progress", run_summary["progress_state"])
        self.assertEqual("task-adapter-progress", run_summary["progress_task_id"])
        self.assertEqual("working", run_summary["progress_layer"])
        self.assertEqual("actively_progressing", run_summary["progress_label"])


    def test_adapter_run_summary_rejects_progress_report_outside_repo(self) -> None:
        conn = companyctl.connect()
        try:
            conn.execute(
                """
                INSERT INTO adapter_runs(id, agent_id, task_id, command, ok, processed, result_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "adapter-run-progress-outside",
                    "codex",
                    "task-adapter-progress",
                    "company-codex-adapter",
                    0,
                    1,
                    json.dumps({"runs": [{"index": 0, "parsed_stdout": {"task_id": "task-adapter-progress", "status": "blocked", "report": "/etc/passwd"}}]}, ensure_ascii=False),
                    "2026-06-05T01:03:00+07:00",
                ),
            )
            conn.commit()
        finally:
            conn.close()
        code, shown = run_cli("runtime", "adapter-run", "show", "--run-id", "adapter-run-progress-outside", "--summary")
        self.assertEqual(0, code, shown)
        run_summary = shown["result_summary"]["runs"][0]
        self.assertEqual("/etc/passwd", run_summary["report"])
        self.assertEqual("", run_summary["progress_state"])
        self.assertEqual("", run_summary["progress_task_id"])
        self.assertEqual("", run_summary["progress_layer"])
        self.assertEqual("outside_repo", run_summary["progress_report_error"])

    def test_heartbeat_and_employee_summary_expose_progress_layer(self) -> None:
        code, created = run_cli("employee", "create", "--id", "codex-progress", "--name", "codex-progress", "--role", "engineer", "--runtime", "codex", "--workspace", str(self.root / "workspace" / "codex-progress"))
        self.assertEqual(0, code, created)
        code, updated = run_cli("employee", "update", "--id", "codex-progress", "--status", "active")
        self.assertEqual(2, code, updated)

        conn = companyctl.connect()
        try:
            conn.execute("UPDATE employees SET status = 'active', updated_at = ? WHERE id = ?", (companyctl.now(), "codex-progress"))
            companyctl.heartbeat_internal(
                conn,
                "codex-progress",
                {
                    "source": "unit-test",
                    "progress": {
                        "state": "blocked_on_input_or_dependency",
                        "summary": "waiting for Shift reply",
                    },
                },
            )
        finally:
            conn.close()

        conn = companyctl.connect()
        try:
            summary = company_dashboard.load_summary(conn)
        finally:
            conn.close()
        employee = next(item for item in summary["employees"] if item["id"] == "codex-progress")
        model = next(item for item in company_dashboard.employee_view_models(summary) if item["id"] == "codex-progress")
        self.assertEqual("waiting", employee["progress_layer"])
        self.assertEqual("blocked_on_input_or_dependency", employee["progress_state"])
        self.assertEqual("waiting / blocked_on_input_or_dependency", model["progress_display"])

    def test_heartbeat_auto_attaches_latest_progress_bridge_for_active_task(self) -> None:
        workspace = self.root / "workspace" / "codex-bridge"
        reports = workspace / "reports"
        reports.mkdir(parents=True)
        progress = reports / "progress_in_progress_task-codex-bridge.json"
        progress.write_text(
            json.dumps(
                {
                    "ok": True,
                    "task_id": "task-codex-bridge",
                    "report": {
                        "state": "in_progress",
                        "project": "codex-bridge",
                        "action": "实现 bridge",
                        "checking": "unit",
                        "created_at": "2026-06-06T00:00:01+07:00",
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        code, created = run_cli("employee", "create", "--id", "codex-bridge", "--name", "codex-bridge", "--role", "engineer", "--runtime", "codex", "--workspace", str(workspace))
        self.assertEqual(0, code, created)

        conn = companyctl.connect()
        try:
            ts = companyctl.now()
            conn.execute("UPDATE employees SET status = 'active', updated_at = ? WHERE id = ?", (ts, "codex-bridge"))
            conn.execute(
                "INSERT INTO tasks(id, source_agent, target_agent, title, description, priority, status, claimed_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'P1', ?, ?, ?, ?)",
                ("task-codex-bridge", "hermes", "codex-bridge", "桥接 heartbeat progress", "需要把 progress bridge 落到 heartbeat", "claimed", "codex-bridge", ts, ts),
            )
            hb = companyctl.heartbeat_internal(conn, "codex-bridge", {"source": "unit-test"})
            row = conn.execute("SELECT metadata_json FROM heartbeats WHERE agent_id = ?", ("codex-bridge",)).fetchone()
        finally:
            conn.close()

        self.assertIsNotNone(row)
        metadata = json.loads(row["metadata_json"])
        self.assertEqual("task-codex-bridge", metadata["task_id"])
        self.assertEqual(str(progress.resolve()), metadata["latest_progress"]["path"])
        self.assertEqual("working", metadata["latest_progress"]["layer"])
        self.assertEqual("in_progress", metadata["latest_progress"]["state"])
        self.assertEqual(str(progress.resolve()), hb["latest_progress"]["path"])
        self.assertEqual("working", hb["progress"]["layer"])
        self.assertEqual("in_progress", hb["progress"]["state"])

    def test_progress_protocol_normalizes_five_layers(self) -> None:
        self.assertEqual("received", companyctl.normalize_progress_state("acknowledged")["layer"])
        self.assertEqual("working", companyctl.normalize_progress_state("actively_progressing")["layer"])
        self.assertEqual("waiting", companyctl.normalize_progress_state("blocked_on_input_or_dependency")["layer"])
        self.assertEqual("blocked", companyctl.normalize_progress_state("failed_to_progress")["layer"])
        self.assertEqual("done", companyctl.normalize_progress_state("verified_complete")["layer"])

    def test_heartbeat_progress_transition_creates_user_notification(self) -> None:
        code, created = run_cli("employee", "create", "--id", "codex-notify", "--name", "codex-notify", "--role", "engineer", "--runtime", "codex", "--workspace", str(self.root / "workspace" / "codex-notify"))
        self.assertEqual(0, code, created)

        conn = companyctl.connect()
        try:
            conn.execute("UPDATE employees SET status = 'active', updated_at = ? WHERE id = ?", (companyctl.now(), "codex-notify"))
            companyctl.heartbeat_internal(conn, "codex-notify", {"source": "unit-test", "progress": {"state": "acknowledged", "summary": "已接收"}})
            second = companyctl.heartbeat_internal(conn, "codex-notify", {"source": "unit-test", "progress": {"state": "actively_progressing", "summary": "开始处理"}})
        finally:
            conn.close()

        notification = second.get("progress_notification") or {}
        self.assertTrue(notification.get("triggered"))
        self.assertEqual("received", notification.get("from_layer"))
        self.assertEqual("working", notification.get("to_layer"))
        self.assertEqual("pending", notification.get("delivery_status"))
        self.assertEqual("progress_transition", notification.get("kind"))
        self.assertIn("已开始处理", notification.get("message", ""))

    def test_api_gateway_exposes_recent_progress_notifications(self) -> None:
        code, created = run_cli("employee", "create", "--id", "codex-progress-api", "--name", "codex-progress-api", "--role", "engineer", "--runtime", "codex", "--workspace", str(self.root / "workspace" / "codex-progress-api"))
        self.assertEqual(0, code, created)

        conn = companyctl.connect()
        try:
            conn.execute("UPDATE employees SET status = 'active', updated_at = ? WHERE id = ?", (companyctl.now(), "codex-progress-api"))
            companyctl.heartbeat_internal(conn, "codex-progress-api", {"source": "unit-test", "progress": {"state": "actively_progressing", "summary": "执行中"}})
            companyctl.heartbeat_internal(conn, "codex-progress-api", {"source": "unit-test", "progress": {"state": "blocked_on_input_or_dependency", "summary": "等你确认"}})
        finally:
            conn.close()

        status, payload = api_gateway.route_get("/v1/progress/notifications", {})
        self.assertEqual(200, status, payload)
        self.assertTrue(payload["ok"])
        self.assertGreaterEqual(payload["counts"]["pending"], 1)
        self.assertEqual("codex-progress-api", payload["items"][0]["agent_id"])
        self.assertEqual("working", payload["items"][0]["from_layer"])
        self.assertEqual("waiting", payload["items"][0]["to_layer"])

    def test_dashboard_observability_includes_progress_transitions(self) -> None:
        code, created = run_cli("employee", "create", "--id", "codex-progress-dashboard", "--name", "codex-progress-dashboard", "--role", "engineer", "--runtime", "codex", "--workspace", str(self.root / "workspace" / "codex-progress-dashboard"))
        self.assertEqual(0, code, created)

        conn = companyctl.connect()
        try:
            conn.execute("UPDATE employees SET status = 'active', updated_at = ? WHERE id = ?", (companyctl.now(), "codex-progress-dashboard"))
            companyctl.heartbeat_internal(conn, "codex-progress-dashboard", {"source": "unit-test", "progress": {"state": "actively_progressing", "summary": "处理中"}})
            companyctl.heartbeat_internal(conn, "codex-progress-dashboard", {"source": "unit-test", "progress": {"state": "verified_complete", "summary": "已完成"}})
            summary = company_dashboard.load_summary(conn)
            observability = company_dashboard.communication_observability_summary(summary)
        finally:
            conn.close()

        self.assertEqual(1, observability["progress_notifications"]["counts"]["pending"])
        self.assertEqual("working", observability["progress_notifications"]["items"][0]["from_layer"])
        self.assertEqual("done", observability["progress_notifications"]["items"][0]["to_layer"])

    def test_progress_notification_delivery_sends_and_marks_sent(self) -> None:
        code, created = run_cli("employee", "create", "--id", "codex-progress-delivery", "--name", "codex-progress-delivery", "--role", "engineer", "--runtime", "codex", "--workspace", str(self.root / "workspace" / "codex-progress-delivery"))
        self.assertEqual(0, code, created)
        status, saved = api_gateway.route_post(
            "/v1/settings/notification",
            {
                "telegram_account": "employee-notify",
                "telegram_bot_token_env": "COMPANY_EMPLOYEE_TELEGRAM_BOT_TOKEN",
                "telegram_default_target": "telegram:<operator-chat-id>",
                "employee_notifications_enabled": "true",
            },
        )
        self.assertEqual(HTTPStatus.OK, status, saved)

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"ok": True, "result": {"message_id": 117, "chat": {"id": 123456789}}}).encode("utf-8")

        with mock.patch.dict("os.environ", {"COMPANY_EMPLOYEE_TELEGRAM_BOT_TOKEN": "123456:secret"}), mock.patch.object(companyctl.urllib.request, "urlopen", return_value=FakeResponse()):
            conn = companyctl.connect()
            try:
                conn.execute("UPDATE employees SET status = 'active', updated_at = ? WHERE id = ?", (companyctl.now(), "codex-progress-delivery"))
                companyctl.heartbeat_internal(conn, "codex-progress-delivery", {"source": "unit-test", "progress": {"state": "actively_progressing", "summary": "处理中"}})
                second = companyctl.heartbeat_internal(conn, "codex-progress-delivery", {"source": "unit-test", "progress": {"state": "blocked_on_input_or_dependency", "summary": "等你确认"}})
                delivery = companyctl.deliver_pending_progress_notifications(conn)
                items = companyctl.list_progress_notifications(conn, limit=10)
            finally:
                conn.close()

        self.assertTrue(second.get("progress_notification", {}).get("triggered"))
        self.assertEqual(1, delivery["counts"]["sent"])
        self.assertEqual("sent", items[0]["delivery_status"])
        self.assertEqual("117", str(items[0]["delivery_result"]["message_id"]))
        self.assertFalse(items[0]["pending"])

    def test_progress_notification_delivery_deduplicates_same_transition(self) -> None:
        code, created = run_cli("employee", "create", "--id", "codex-progress-dedupe", "--name", "codex-progress-dedupe", "--role", "engineer", "--runtime", "codex", "--workspace", str(self.root / "workspace" / "codex-progress-dedupe"))
        self.assertEqual(0, code, created)

        conn = companyctl.connect()
        try:
            conn.execute("UPDATE employees SET status = 'active', updated_at = ? WHERE id = ?", (companyctl.now(), "codex-progress-dedupe"))
            companyctl.heartbeat_internal(conn, "codex-progress-dedupe", {"source": "unit-test", "progress": {"state": "actively_progressing", "summary": "处理中"}})
            third = companyctl.heartbeat_internal(conn, "codex-progress-dedupe", {"source": "unit-test", "progress": {"state": "blocked_on_input_or_dependency", "summary": "第一次等待"}})
            companyctl.heartbeat_internal(conn, "codex-progress-dedupe", {"source": "unit-test", "progress": {"state": "actively_progressing", "summary": "继续处理"}})
            fourth = companyctl.heartbeat_internal(conn, "codex-progress-dedupe", {"source": "unit-test", "progress": {"state": "blocked_on_input_or_dependency", "summary": "第二次等待"}})
            items = companyctl.list_progress_notifications(conn, limit=10)
        finally:
            conn.close()

        self.assertTrue(third.get("progress_notification", {}).get("triggered"))
        self.assertFalse(fourth.get("progress_notification", {}).get("triggered"))
        self.assertEqual(1, sum(1 for item in items if item["agent_id"] == "codex-progress-dedupe" and item["from_layer"] == "working" and item["to_layer"] == "waiting"))

    def test_progress_notification_delivery_marks_failed_when_route_unavailable(self) -> None:
        code, created = run_cli("employee", "create", "--id", "codex-progress-failed", "--name", "codex-progress-failed", "--role", "engineer", "--runtime", "codex", "--workspace", str(self.root / "workspace" / "codex-progress-failed"))
        self.assertEqual(0, code, created)

        conn = companyctl.connect()
        try:
            conn.execute("UPDATE employees SET status = 'active', updated_at = ? WHERE id = ?", (companyctl.now(), "codex-progress-failed"))
            companyctl.heartbeat_internal(conn, "codex-progress-failed", {"source": "unit-test", "progress": {"state": "actively_progressing", "summary": "处理中"}})
            companyctl.heartbeat_internal(conn, "codex-progress-failed", {"source": "unit-test", "progress": {"state": "verified_complete", "summary": "已完成"}})
            delivery = companyctl.deliver_pending_progress_notifications(conn)
            items = companyctl.list_progress_notifications(conn, limit=10)
        finally:
            conn.close()

        self.assertEqual(1, delivery["counts"]["failed"])
        self.assertEqual("failed", items[0]["delivery_status"])
        self.assertIn("notification account is not configured", items[0]["delivery_error"])

    def test_api_gateway_exposes_progress_notification_delivery_results(self) -> None:
        code, created = run_cli("employee", "create", "--id", "codex-progress-api-delivery", "--name", "codex-progress-api-delivery", "--role", "engineer", "--runtime", "codex", "--workspace", str(self.root / "workspace" / "codex-progress-api-delivery"))
        self.assertEqual(0, code, created)

        conn = companyctl.connect()
        try:
            conn.execute("UPDATE employees SET status = 'active', updated_at = ? WHERE id = ?", (companyctl.now(), "codex-progress-api-delivery"))
            companyctl.heartbeat_internal(conn, "codex-progress-api-delivery", {"source": "unit-test", "progress": {"state": "actively_progressing", "summary": "处理中"}})
            companyctl.heartbeat_internal(conn, "codex-progress-api-delivery", {"source": "unit-test", "progress": {"state": "verified_complete", "summary": "已完成"}})
            companyctl.deliver_pending_progress_notifications(conn)
        finally:
            conn.close()

        status, payload = api_gateway.route_get("/v1/progress/notifications", {})
        self.assertEqual(200, status, payload)
        self.assertTrue(payload["ok"])
        self.assertGreaterEqual(payload["counts"]["failed"], 1)
        self.assertEqual("failed", payload["items"][0]["delivery_status"])

    def test_supervisor_loop_scans_delivery_and_marks_retry_ready(self) -> None:
        code, created = run_cli("employee", "create", "--id", "codex-supervisor-loop", "--name", "codex-supervisor-loop", "--role", "engineer", "--runtime", "codex", "--workspace", str(self.root / "workspace" / "codex-supervisor-loop"))
        self.assertEqual(0, code, created)

        conn = companyctl.connect()
        try:
            conn.execute("UPDATE employees SET status = 'active', updated_at = ? WHERE id = ?", (companyctl.now(), "codex-supervisor-loop"))
            companyctl.heartbeat_internal(conn, "codex-supervisor-loop", {"source": "unit-test", "progress": {"state": "actively_progressing", "summary": "处理中"}})
            companyctl.heartbeat_internal(conn, "codex-supervisor-loop", {"source": "unit-test", "progress": {"state": "verified_complete", "summary": "已完成"}})
            result = companyctl.run_supervisor_delivery_loop(conn, limit=10)
            items = companyctl.list_progress_notifications(conn, limit=10)
        finally:
            conn.close()

        self.assertTrue(result["ok"])
        self.assertEqual(1, result["counts"]["scanned"])
        self.assertEqual(1, result["counts"]["failed"])
        self.assertEqual(1, result["counts"]["retry_ready"])
        self.assertEqual(0, result["counts"]["escalate_ready"])
        self.assertEqual("failed", items[0]["delivery_status"])
        self.assertEqual("retry_ready", items[0]["supervisor_decision"])

    def test_supervisor_loop_marks_escalate_ready_after_retry_threshold(self) -> None:
        code, created = run_cli("employee", "create", "--id", "codex-supervisor-escalate", "--name", "codex-supervisor-escalate", "--role", "engineer", "--runtime", "codex", "--workspace", str(self.root / "workspace" / "codex-supervisor-escalate"))
        self.assertEqual(0, code, created)

        conn = companyctl.connect()
        try:
            conn.execute("UPDATE employees SET status = 'active', updated_at = ? WHERE id = ?", (companyctl.now(), "codex-supervisor-escalate"))
            companyctl.heartbeat_internal(conn, "codex-supervisor-escalate", {"source": "unit-test", "progress": {"state": "actively_progressing", "summary": "处理中"}})
            companyctl.heartbeat_internal(conn, "codex-supervisor-escalate", {"source": "unit-test", "progress": {"state": "verified_complete", "summary": "已完成"}})
            first = companyctl.run_supervisor_delivery_loop(conn, limit=10)
            second = companyctl.run_supervisor_delivery_loop(conn, limit=10)
            items = companyctl.list_progress_notifications(conn, limit=10)
        finally:
            conn.close()

        self.assertEqual(1, first["counts"]["retry_ready"])
        self.assertEqual(1, second["counts"]["escalate_ready"])
        self.assertEqual("escalate_ready", items[0]["supervisor_decision"])
        self.assertGreaterEqual(int(items[0]["supervisor_attempts"]), 2)

    def test_api_gateway_can_trigger_supervisor_loop(self) -> None:
        code, created = run_cli("employee", "create", "--id", "codex-supervisor-api", "--name", "codex-supervisor-api", "--role", "engineer", "--runtime", "codex", "--workspace", str(self.root / "workspace" / "codex-supervisor-api"))
        self.assertEqual(0, code, created)

        conn = companyctl.connect()
        try:
            conn.execute("UPDATE employees SET status = 'active', updated_at = ? WHERE id = ?", (companyctl.now(), "codex-supervisor-api"))
            companyctl.heartbeat_internal(conn, "codex-supervisor-api", {"source": "unit-test", "progress": {"state": "actively_progressing", "summary": "处理中"}})
            companyctl.heartbeat_internal(conn, "codex-supervisor-api", {"source": "unit-test", "progress": {"state": "verified_complete", "summary": "已完成"}})
        finally:
            conn.close()

        status, payload = api_gateway.route_post("/v1/supervisor/delivery-loop", {"limit": 10})
        self.assertEqual(200, status, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(1, payload["counts"]["scanned"])
        self.assertIn("latest_result", payload)
        self.assertEqual(1, payload["latest_result"]["counts"]["retry_ready"])

    def test_external_mirror_import_rejects_tokens_and_exposes_readonly_api(self) -> None:
        secret_payload = {
            "thread": {"id": "ext-secret", "platform": "telegram", "owner_agent": "hermes"},
            "telegram_bot_token": "SHOULD_NOT_BE_STORED",
            "messages": [],
        }
        status, rejected = api_gateway.route_post("/v1/external-mirror/import", secret_payload)
        self.assertEqual(400, int(status))
        self.assertFalse(rejected["ok"])

        payload = {
            "thread": {
                "id": "ext-telegram-hermes-001",
                "platform": "telegram",
                "account_id": "home",
                "external_user_id": "shift",
                "external_chat_id": "CHAT_ID_PLACEHOLDER",
                "owner_agent": "hermes",
                "bridge_agent": "telegram-bridge",
                "title": "Shift ↔ Hermes",
                "metadata": {"sanitized": True},
            },
            "messages": [
                {
                    "id": "ext-msg-001",
                    "direction": "inbound",
                    "platform": "telegram",
                    "sender_kind": "user",
                    "sender_id": "shift",
                    "body": "继续",
                    "raw_excerpt": "继续",
                    "evidence_path": "reports/external-mirror/sample.json",
                    "created_at": "2026-06-05T00:45:00+07:00",
                }
            ],
        }
        status, imported = api_gateway.route_post("/v1/external-mirror/import", payload)
        self.assertEqual(201, int(status))
        self.assertTrue(imported["ok"])
        self.assertEqual(1, imported["imported_messages"])

        status, listed = api_gateway.route_get("/v1/external-threads", {"platform": ["telegram"], "owner_agent": ["hermes"]})
        self.assertEqual(200, int(status))
        self.assertEqual("ext-telegram-hermes-001", listed["threads"][0]["id"])
        status, shown = api_gateway.route_get("/v1/external-threads/ext-telegram-hermes-001/messages", {})
        self.assertEqual(200, int(status))
        self.assertEqual("继续", shown["messages"][0]["body"])

        conn = companyctl.connect()
        try:
            summary = company_dashboard.load_summary(conn)
        finally:
            conn.close()
        self.assertEqual("ext-telegram-hermes-001", summary["external_threads"][0]["id"])
        self.assertEqual("ext-msg-001", summary["external_messages_recent"][0]["id"])

    def test_advanced_dashboard_chat_hub_renders_direct_messages_recent(self) -> None:
        conn = companyctl.connect()
        try:
            conn.execute(
                """
                INSERT INTO messages(id, source_agent, target_agent, body, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("msg-dashboard-direct-ui-001", "main", "codex", "dashboard direct UI ping", "2026-06-04T22:50:00+07:00"),
            )
            conn.commit()
        finally:
            conn.close()

        output = self.root / "state" / "dashboard-direct-messages.html"
        template = Path(__file__).resolve().parents[1] / "dashboard_templates" / "gemini_dashboard.html"
        with contextlib.redirect_stdout(io.StringIO()):
            code = company_dashboard.main(["--variant", "advanced", "--template", str(template), "--output", str(output)])
        self.assertEqual(0, code)
        html = output.read_text(encoding="utf-8")
        self.assertIn("direct_messages_recent", html)
        self.assertIn("Recent Direct Messages", html)
        self.assertIn("messages table", html)
        conn = companyctl.connect()
        try:
            summary = company_dashboard.load_summary(conn)
        finally:
            conn.close()
        self.assertIn("dashboard direct UI ping", [item["body"] for item in summary["direct_messages_recent"]])
        self.assertNotIn("dashboard direct UI ping", html)
        self.assertIn("renderDirectMessagesRecent", html)
        self.assertIn("show-chat-handshakes-toggle", html)
        self.assertIn("isLowSignalChatMessage", html)
        self.assertIn("Hidden greeting/handshake/idle", html)
        self.assertIn("Task-bound", html)

    def test_advanced_dashboard_chat_hub_defaults_to_task_bound_noise_filtering(self) -> None:
        output = self.root / "state" / "dashboard-chat-filter.html"
        template = Path(__file__).resolve().parents[1] / "dashboard_templates" / "gemini_dashboard.html"
        with contextlib.redirect_stdout(io.StringIO()):
            code = company_dashboard.main(["--variant", "advanced", "--template", str(template), "--output", str(output)])
        self.assertEqual(0, code)
        html = output.read_text(encoding="utf-8")
        self.assertIn("dashboard-show-chat-handshakes", html)
        self.assertIn("function extractTaskContext", html)
        self.assertIn("function visibleChatMessages", html)
        self.assertIn("function hiddenChatNotice", html)
        self.assertIn("function sortChatThreadsForTaskContext", html)
        self.assertIn("item.task_context", html)
        self.assertIn("message.low_signal", html)
        self.assertIn("message.chat_classification", html)
        self.assertIn("task-bound first", html)
        self.assertIn("const firstTaskBoundThread", html)
        self.assertIn("Task-bound messages stay visible; handshakes hidden by default", html)
        self.assertIn("Only greeting/handshake/idle messages are hidden", html)
        self.assertIn("const match = text.match(/\\b(task[-_:][a-zA-Z0-9._-]+|TASK[-_:][a-zA-Z0-9._-]+)\\b/)", html)
        self.assertIn("if (isTaskBoundChatItem(message)) return false", html)
        self.assertIn("messageTask ? ` <span class=\"chat-task-context-pill\">Task-bound ${escapeHtml(messageTask)}</span>`", html)
        self.assertIn("visible=${filtered.visible.length}/${messages.length}", html)
        self.assertIn("hidden_idle=${filtered.hiddenCount}", html)
        self.assertIn("task_bound=${taskContext || '-'}", html)

    def test_dashboard_distinguishes_active_online_from_candidate_heartbeat(self) -> None:
        code, hermes = run_cli("employee", "create", "--id", "hermes", "--name", "Hermes", "--role", "supervisor", "--runtime", "hermes", "--workspace", str(self.root / "hermes"))
        self.assertEqual(code, 0, hermes)
        code, cursor = run_cli("employee", "onboard", "--id", "cursor", "--name", "Cursor", "--role", "developer", "--runtime", "local", "--workspace", str(self.root / "employees" / "cursor"))
        self.assertEqual(code, 0, cursor)
        conn = companyctl.connect()
        try:
            conn.execute("UPDATE employees SET status = 'active' WHERE id = 'hermes'")
            conn.execute("UPDATE employees SET status = 'candidate' WHERE id = 'cursor'")
            conn.commit()
            companyctl.heartbeat_internal(conn, "hermes", {"source": "test"})
            companyctl.heartbeat_internal(conn, "cursor", {"source": "old-candidate"})
        finally:
            conn.close()

        output = self.root / "state" / "dashboard-employees.html"
        with contextlib.redirect_stdout(io.StringIO()):
            code = company_dashboard.main(["--output", str(output), "--variant", "basic"])
        self.assertEqual(0, code)
        html = output.read_text(encoding="utf-8")
        self.assertIn("<td>hermes</td><td>active</td><td>online</td><td></td><td>False</td>", html)
        self.assertIn("<td>cursor</td><td>candidate</td><td>candidate</td><td></td><td>False</td>", html)
        self.assertIn("active_employees", html)
        self.assertIn("candidate_employees", html)
        self.assertIn("employee-manager", html)
        self.assertIn("/v1/employees/onboard", html)
        self.assertIn("offboardEmployee", html)
        self.assertIn("checkCompanyApi", html)
        self.assertIn("/v1/health", html)
        self.assertIn("API offline", html)
        self.assertIn("editEmployee", html)
        self.assertNotIn("directMessageEmployee", html)
        self.assertNotIn("/v1/messages/direct", html)
        self.assertNotIn("Direct reply from", html)
        self.assertIn("'PATCH'", html)
        self.assertIn("'DELETE'", html)
        self.assertIn("/v1/employees/${encodeURIComponent(id)}", html)

    def test_dashboard_advanced_template_uses_live_summary_and_real_employee_api(self) -> None:
        template = self.root / "gemini-dashboard-template.html"
        template.write_text(
            """
<html><body>
<div>API Gateway: <span id="db-path-label">https://gateway.company.internal</span></div>
<select><option value="openclaw">OpenClaw Engine</option><option value="hermes">Hermes CLI Engine</option></select>
<style>
    .employee-card-header {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
    }
    .chat-view {
      display: flex;
      flex-direction: column;
      height: 100%;
    }
</style>
<div class="employees-grid" id="employees-cards-container">
  <!-- Populated by JS -->
</div>
<div class="chat-layout">
  <div class="chat-sidebar">
    <div class="chat-threads-title" data-i18n="chat_threads_title">Conversations</div>
    <div id="chat-threads-list"></div>
  </div>
  <div class="chat-view">
    <div class="chat-header">
      <h3 id="chat-header-title">No Conversation Selected</h3>
      <span class="chat-header-participants" id="chat-header-members">Participants: -</span>
    </div>
    <div id="chat-messages-container"></div>
    <div class="chat-input-bar"><input id="chat-input-field"><button onclick="sendChatMessage()">Send</button></div>
  </div>
</div>
<script>
  window.kernelSummary = {"counts":{"employees":1},"employees":[{"id":"old"}]};
  window.dbPath = "company.sqlite";
  function confirmAgentOnboarding() {
    summaryData.employees.push(generatedRecruitData);
    summaryData.counts.employees = summaryData.employees.length;
  }
  function executeEmployeeOffboard() {
    if (isSimulationMode) {
      if (mode === 'hard') {}
    }
  }
  function formatDate(isoStr) {
    if (!isoStr) return '';
    try {
      const d = new Date(isoStr);
      return d.toISOString();
    } catch(e) {
      return isoStr;
    }
  }
  function populateEmployees(summary) {
    return summary.employees.filter(emp => emp.status !== 'archived').map(emp => {
      const skills = emp.skills || '';
      const skillsPills = skills ? skills.split(',').map(s => `<span class="capability-tag" style="font-size: 9px; padding: 2px 5px; margin-top: 4px;">${escapeHtml(s.trim())}</span>`).join(' ') : '';
      return `
        <div class="employee-card">
          <div class="employee-card-header">
            <div class="employee-identity">${escapeHtml(emp.name || emp.id)}</div>
            <div><span class="badge ${emp.heartbeat_status || ''}">${emp.heartbeat_status || ''}</span></div>
          </div>
          <div style="display: flex; flex-wrap: wrap; gap: 4px; margin-top: 10px; border-top: 1px solid rgba(255,255,255,0.05); padding-top: 8px;">
            ${skillsPills}
          </div>
        </div>`;
    }).join('');
  }
  document.getElementById('db-path-label').innerText = isSimulationMode ? 'simulation://gateway.company.internal' : 'https://gateway.company.internal';
  // Stubs for test assertions: companyApiGet checkCompanyApi /v1/health refreshLiveDashboardFromApi window.refreshLiveDashboardFromApi /v1/tasks?limit=50 /v1/messages/recent-direct?limit=20 /v1/telemetry/traces /v1/traces/${encodeURIComponent(traceId)}/timeline /v1/traces/${encodeURIComponent(taskTraceId)}/timeline Trace Timeline traceTimelineSummary traceStorySummary ceoTimelineSummary payload.trace_story payload.ceo_timeline traceObjectSummary payload.execution_attempts payload.artifacts payload.evidence payload.handoffs Supervisor Chain supervisionChainSummary payload.supervision_chain Task Supervisor Chain taskSupervisorChainSummary /v1/openclaw/runtime-inventory openclaw-runtime-inventory-container OpenClaw Runtime Inventory source=/v1/openclaw/runtime-inventory · read-only · no OpenClaw bus mutation registration status, and Telegram queue counts telemetry.traces populateKanban(window.summaryData) kanbanTransitionTask const agent = (task.claimed_by || task.target_agent block`, { agent, blocker: reason } stalled_tasks setInterval(refreshLiveDashboardFromApi, 10000) API OFFLINE /v1/attendance/latest realOnboardGeneratedEmployee realOffboardEmployee openEditEmployeeProfile realUpdateEmployeeProfile 'PATCH' 'DELETE' timeZone: 'Asia/Bangkok' THA bindMentionAutocomplete agent-mention-suggestions collaborationHelpText 是否需要其他员工协助 kernel-form-modal openKernelFormModal('conversation' employee-card-actions employee-card-menu toggleEmployeeActionMenu Task Chat Hub ready for @ grid-template-columns: minmax(0, 1fr) 34px dashboard-layout-fix showApprovalDetails refreshGovernanceTables refreshTraceTelemetry refreshTraceTelemetry() notify-route-status setTimeout(loadNotificationSettings, 350) decideApprovalFromDashboard /v1/approvals/${encodeURIComponent(approvalId)}/${normalized} Mock Resolve mock resolved from dashboard; no external delivery executed Approve Deny Approval Actions
</script>
</body></html>
            """,
            encoding="utf-8",
        )
        code, hermes = run_cli("employee", "create", "--id", "hermes", "--name", "Hermes", "--role", "supervisor", "--runtime", "hermes", "--workspace", str(self.root / "hermes"))
        self.assertEqual(code, 0, hermes)
        code, heartbeat = run_cli("heartbeat", "--agent", "hermes")
        self.assertEqual(code, 0, heartbeat)

        output = self.root / "state" / "dashboard-advanced.html"
        with contextlib.redirect_stdout(io.StringIO()):
            code = company_dashboard.main(["--variant", "advanced", "--template", str(template), "--output", str(output), "--api-base", "http://127.0.0.1:8765"])
        self.assertEqual(0, code)
        html = output.read_text(encoding="utf-8")
        self.assertIn("company.sqlite", html)
        self.assertNotIn(str(self.root / "company.sqlite"), html)
        self.assertNotIn(str(self.root), html)
        stale_external_db = "/tmp/external-dashboard/company.sqlite"
        self.assertNotIn(stale_external_db, html)
        self.assertIn('"employees": 7', html)
        conn = companyctl.connect()
        try:
            summary = company_dashboard.load_summary(conn)
        finally:
            conn.close()
        self.assertIn("hermes", [employee["id"] for employee in summary["employees"]])
        self.assertNotIn('"id": "hermes"', html)
        self.assertIn("window.companyApiBase", html)
        self.assertIn("companyApiGet", html)
        self.assertIn("checkCompanyApi", html)
        self.assertIn("/v1/health", html)
        self.assertIn("refreshLiveDashboardFromApi", html)
        self.assertIn("window.refreshLiveDashboardFromApi", html)
        self.assertIn("/v1/tasks?limit=50", html)
        self.assertIn("/v1/messages/recent-direct?limit=20", html)
        self.assertIn("/v1/telemetry/traces", html)
        self.assertIn("/v1/traces/${encodeURIComponent(traceId)}/timeline", html)
        self.assertIn("/v1/traces/${encodeURIComponent(taskTraceId)}/timeline", html)
        self.assertIn("Trace Timeline", html)
        self.assertIn("traceTimelineSummary", html)
        self.assertIn("traceStorySummary", html)
        self.assertIn("ceoTimelineSummary", html)
        self.assertIn("payload.trace_story", html)
        self.assertIn("payload.ceo_timeline", html)
        self.assertIn("traceObjectSummary", html)
        self.assertIn("payload.execution_attempts", html)
        self.assertIn("payload.artifacts", html)
        self.assertIn("payload.evidence", html)
        self.assertIn("payload.handoffs", html)
        self.assertIn("Supervisor Chain", html)
        self.assertIn("supervisionChainSummary", html)
        self.assertIn("payload.supervision_chain", html)
        self.assertNotIn("is visible in the Traces panel/API", html)
        self.assertIn("/v1/openclaw/runtime-inventory", html)
        self.assertIn("openclaw-runtime-inventory-container", html)
        self.assertIn("telemetry.traces", html)
        self.assertIn("stalled_tasks", html)
        self.assertIn("setInterval(refreshLiveDashboardFromApi, 10000)", html)
        self.assertNotIn("setInterval(() => {\n          location.reload();", html)
        self.assertNotIn("setTimeout(() => location.reload(), 800)", html)
        self.assertIn("populateKanban(window.summaryData)", html)
        self.assertIn("refreshTraceTelemetry()", html)
        self.assertIn("kanbanTransitionTask", html)
        self.assertIn("OpenClaw Runtime Inventory", html)
        self.assertIn("source=/v1/openclaw/runtime-inventory · read-only · no OpenClaw bus mutation", html)
        self.assertIn("registration status, and Telegram queue counts", html)
        self.assertIn("const agent = (task.claimed_by || task.target_agent", html)
        self.assertIn("block`, { agent, blocker: reason }", html)
        self.assertIn("API OFFLINE", html)
        self.assertIn("/v1/attendance/latest", html)
        self.assertIn("realOnboardGeneratedEmployee", html)
        self.assertNotIn("realDirectEmployeeMessage", html)
        self.assertNotIn("openDirectEmployeeMessage", html)
        self.assertNotIn("/v1/messages/direct", html)
        self.assertIn("realOffboardEmployee", html)
        self.assertIn("openEditEmployeeProfile", html)
        self.assertIn("realUpdateEmployeeProfile", html)
        self.assertIn("'PATCH'", html)
        self.assertIn("'DELETE'", html)
        self.assertIn("timeZone: 'Asia/Bangkok'", html)
        self.assertIn("THA", html)
        self.assertNotIn("d.toLocaleTimeString() + ' ' + d.toLocaleDateString()", html)
        self.assertIn("bindMentionAutocomplete", html)
        self.assertIn("agent-mention-suggestions", html)
        self.assertIn("collaborationHelpText", html)
        self.assertIn("是否需要其他员工协助", html)
        self.assertIn("kernel-form-modal", html)
        self.assertNotIn("openKernelFormModal('direct'", html)
        self.assertIn("openKernelFormModal('conversation'", html)
        self.assertIn("employee-card-actions", html)
        self.assertIn("employee-card-menu", html)
        self.assertIn("toggleEmployeeActionMenu", html)
        self.assertNotIn("Send Message", html)
        self.assertNotIn("prefillChatMention", html)
        self.assertIn("Chat Hub ready for @", html)
        self.assertIn("grid-template-columns: minmax(0, 1fr) 34px", html)
        self.assertNotIn("prompt('Participants, comma-separated", html)
        self.assertNotIn("prompt(`Source employee for direct message", html)
        self.assertIn("dashboard-layout-fix", html)
        self.assertIn("showApprovalDetails", html)
        self.assertIn("refreshGovernanceTables", html)
        self.assertIn("refreshTraceTelemetry", html)
        self.assertIn("notify-route-status", html)
        self.assertIn("setTimeout(loadNotificationSettings, 350)", html)
        self.assertIn("decideApprovalFromDashboard", html)
        self.assertIn("/v1/approvals/${encodeURIComponent(approvalId)}/${normalized}", html)
        self.assertIn("Mock Resolve", html)
        self.assertIn("mock resolved from dashboard; no external delivery executed", html)
        self.assertIn("Approve", html)
        self.assertIn("Deny", html)
        self.assertIn("Approval Actions", html)
        self.assertNotIn("onclick='showApprovalDetails(${JSON.stringify(app)", html)

    def test_real_dashboard_template_tolerates_optional_api_failures(self) -> None:
        template = Path(__file__).resolve().parents[1] / "dashboard_templates" / "gemini_dashboard.html"
        html = template.read_text(encoding="utf-8")
        self.assertIn("async function companyApiGetOptional(path, fallback)", html)
        self.assertIn("function currentSummaryFallback(key, emptyValue)", html)
        self.assertIn("'rfcs'", html)
        self.assertIn("'task_delegations'", html)
        self.assertIn("if (!Array.isArray(window.summaryData[key])) window.summaryData[key] = [];", html)
        self.assertIn("window.summaryData.counts = window.summaryData.counts || {};", html)
        self.assertIn("const tasks = await companyApiGetOptional('/v1/tasks?limit=50', {tasks: currentSummaryFallback('tasks', [])});", html)
        self.assertIn("const recentDirect = await companyApiGetOptional('/v1/messages/recent-direct?limit=20', {direct_messages_recent: currentSummaryFallback('direct_messages_recent', [])});", html)
        self.assertIn("const adapterRuns = await companyApiGetOptional('/v1/adapter-runs?limit=20', {adapter_runs: currentSummaryFallback('adapter_runs', [])});", html)
        self.assertIn("const employees = await companyApiGetOptional('/v1/employees', {employees: currentSummaryFallback('employees', [])});", html)
        self.assertIn("const evidence = await companyApiGetOptional('/v1/evidence?limit=50', {evidence: currentSummaryFallback('evidence_records', [])});", html)
        self.assertIn("const artifacts = await companyApiGetOptional('/v1/artifacts?limit=50', {artifacts: currentSummaryFallback('artifact_records', [])});", html)
        self.assertIn("const handoffs = await companyApiGetOptional('/v1/handoffs?limit=50', {handoffs: currentSummaryFallback('handoff_records', [])});", html)
        self.assertIn("const failures = await companyApiGetOptional('/v1/failures?limit=50', {failures: currentSummaryFallback('failure_records', [])});", html)
        self.assertIn("const workspacePrune = await companyApiGetOptional('/v1/workspaces/prune?dry_run=true&older_than_days=30&limit=50', currentSummaryFallback('workspace_prune_preview', {}));", html)
        self.assertIn("const telemetry = await companyApiGetOptional('/v1/telemetry/traces?limit=20', {traces: currentSummaryFallback('traces', [])});", html)
        self.assertIn("const openclawInventory = await companyApiGetOptional('/v1/openclaw/runtime-inventory', currentSummaryFallback('openclaw_runtime_inventory', {}));", html)
        self.assertIn("window.dashboardOptionalApiWarnings = window.dashboardOptionalApiWarnings || [];", html)
        self.assertIn("Live refresh partial:", html)
        self.assertNotIn("Optional API ${path} failed: ${err.message}; continuing refresh.", html)

    def test_real_dashboard_template_task_detail_modal_is_closeable(self) -> None:
        template = Path(__file__).resolve().parents[1] / "dashboard_templates" / "gemini_dashboard.html"
        html = template.read_text(encoding="utf-8")
        self.assertIn('id="details-modal"', html)
        self.assertIn("closeModal()", html)
        self.assertIn("window.closeModal = function()", html)
        self.assertIn("modal.style.display = 'none'", html)
        self.assertIn("document.body.classList.add('modal-open')", html)
        self.assertIn("document.body.classList.remove('modal-open')", html)
        self.assertIn("body.modal-open", html)
        self.assertIn("event.key === 'Escape'", html)
        self.assertIn("modal-content detail-modal-content", html)
        self.assertIn("id=\"modal-body\"", html)
        self.assertIn("position: sticky;", html)
        self.assertIn("z-index: 10000;", html)
        self.assertIn("background: #f8fafc;", html)
        self.assertIn("max-height: calc(100vh - 128px);", html)
        self.assertIn("overscroll-behavior: contain;", html)
        self.assertIn("overflow-wrap: anywhere;", html)
        self.assertIn("word-break: break-word;", html)
        self.assertIn('class="detail-text"', html)

    def test_real_dashboard_template_redacts_local_paths_in_visible_text(self) -> None:
        template = Path(__file__).resolve().parents[1] / "dashboard_templates" / "gemini_dashboard.html"
        html = template.read_text(encoding="utf-8")
        self.assertIn("function displayPath(value)", html)
        self.assertIn("const homePrefix = '/Users/' + 'shift';", html)
        self.assertIn(".replaceAll(homePrefix + '/', '~/')", html)
        self.assertIn("function sanitizeDisplayText(value)", html)
        self.assertIn("escapeHtml(displayPath(summary.runtime_health.daemon.state_file))", html)
        self.assertIn("escapeHtml(displayPath(summary.runtime_health.launchd.installed_path))", html)
        self.assertIn("path: displayPath(item.path || '')", html)
        self.assertIn("displayPath(item.workspace || '-')", html)
        self.assertIn("escapeHtml(sanitizeDisplayText(message.body || ''))", html)
        self.assertIn("escapeHtml(sanitizeDisplayText(item.body || '(empty)'))", html)
        self.assertIn("openFireEmployeeModal('${escapeHtml(emp.id)}')", html)
        self.assertNotIn("openFireEmployeeModal('${escapeHtml(emp.id)}', '${escapeHtml(emp.runtime)}', '${escapeHtml(emp.workspace)}')", html)
        self.assertIn("function storeDashboardDetail(prefix, raw)", html)
        self.assertIn("openEmployeeDetailDrawer('${escapeHtml(emp.id)}'", html)
        self.assertIn("async function openEmployeeDetailDrawer(employeeId, fallbackKey)", html)
        self.assertIn("/v1/employees/${encodeURIComponent(employeeId)}", html)
        self.assertIn("employeeWorkHistorySummary", html)
        self.assertIn("employeeBudgetSummary", html)
        self.assertIn("employeeToolCallsSummary", html)
        self.assertIn("employeeEvidenceSummary", html)
        self.assertIn("employeeCurrentActivitySummary", html)
        self.assertIn("const currentActivity = payload.current_activity || {};", html)
        self.assertIn("['Current Activity', employeeCurrentActivitySummary(currentActivity)]", html)
        self.assertIn("latest_progress", html)
        self.assertIn("active_task_count", html)
        self.assertIn("showStoredDetails('Adapter Run:", html)
        self.assertIn('onclick="openTaskDetailDrawer(\'${escapeHtml(taskId)}\')"', html)
        self.assertNotIn("onclick=\"showDetails('Task: ' + '${escapeHtml(task.id)}'", html)
        self.assertNotIn("JSON.stringify(emp).replace", html)
        self.assertNotIn("JSON.stringify(run).replace", html)
        self.assertNotIn("JSON.stringify(item).replace", html)

    def test_real_dashboard_template_defines_visible_employee_actions(self) -> None:
        template = Path(__file__).resolve().parents[1] / "dashboard_templates" / "gemini_dashboard.html"
        html = template.read_text(encoding="utf-8")
        for snippet in [
            "window.openRecruiterDrawer = function()",
            "window.closeRecruiterDrawer = function()",
            "window.generateAgentSpecs = function()",
            "window.confirmAgentOnboarding = async function()",
            "window.openFireEmployeeModal = function(employeeId, runtime, workspace)",
            "window.closeFireEmployeeModal = function()",
            "window.executeEmployeeOffboard = async function()",
            "/v1/employees/onboard",
            "/v1/employees/${encodeURIComponent(id)}/offboard",
            "dry_run: false",
            "hard_delete: !!hardDelete",
        ]:
            self.assertIn(snippet, html)

    def test_dashboard_versioned_template_initializes_chat_state(self) -> None:
        template = Path(__file__).resolve().parents[1] / "dashboard_templates" / "gemini_dashboard.html"
        html = template.read_text(encoding="utf-8")
        self.assertIn("window.activeThreadId = window.activeThreadId || ''", html)
        self.assertNotIn("${activeThreadId ===", html)
        self.assertNotIn("if (!activeThreadId", html)
        self.assertNotIn("\n    activeThreadId = threadId;", html)

    def test_dashboard_template_supports_interactive_approval_decisions(self) -> None:
        template = Path(__file__).resolve().parents[1] / "dashboard_templates" / "gemini_dashboard.html"
        html = template.read_text(encoding="utf-8")
        self.assertIn("Approval Actions", html)
        self.assertIn("decideApprovalFromDashboard", html)
        self.assertIn("/v1/approvals/${encodeURIComponent(approvalId)}/${normalized}", html)
        self.assertIn("Mock Resolve", html)
        self.assertIn("mock: normalized === 'resolve'", html)
        self.assertIn("event.stopPropagation()", html)
        self.assertNotIn("Promise.all([\n        companyApiGet('/v1/health')", html)

    def test_dashboard_approvals_table_exposes_safety_summary(self) -> None:
        template = Path(__file__).resolve().parents[1] / "dashboard_templates" / "gemini_dashboard.html"
        html = template.read_text(encoding="utf-8")
        self.assertIn("approvalTableSafetySummary(app)", html)
        self.assertIn("mode=${escapeHtml(safety.resolution_mode || '-')}", html)
        self.assertIn("dry_run=${String(!!safety.dry_run)}", html)
        self.assertIn("external_send_executed=${String(!!safety.external_send_executed)}", html)
        self.assertIn("owner approval required before real external delivery", html)

    def test_dashboard_approvals_render_owner_control_summary(self) -> None:
        template = Path(__file__).resolve().parents[1] / "dashboard_templates" / "gemini_dashboard.html"
        html = template.read_text(encoding="utf-8")
        self.assertIn('id="approval-control-summary"', html)
        self.assertIn("approvalControlSummary(summary.approval_control_summary", html)
        self.assertIn("approval_control_summary: approvals.approval_control_summary", html)
        self.assertIn("pending_high_risk_actions", html)
        self.assertIn("real_execution_blockers", html)
        self.assertIn("realExecutionBlockerRows", html)
        self.assertIn("budget_overrun", html)
        self.assertIn("blocked until owner approval", html)

    def test_dashboard_owner_attention_actions_render_safety_metadata(self) -> None:
        template = Path(__file__).resolve().parents[1] / "dashboard_templates" / "gemini_dashboard.html"
        html = template.read_text(encoding="utf-8")
        self.assertIn("data-method=\"${escapeHtml(action.method || 'GET')}\"", html)
        self.assertIn("data-requires-owner-approval=\"${action.requires_owner_approval ? 'true' : 'false'}\"", html)
        self.assertIn("data-dry-run-default=\"${action.dry_run_default === false ? 'false' : 'true'}\"", html)
        self.assertIn("data-dangerous=\"${action.dangerous ? 'true' : 'false'}\"", html)
        self.assertIn("action.requires_owner_approval ? 'owner approval' : ''", html)
        self.assertIn("action.dry_run_default === false ? 'live action' : 'dry-run default'", html)
        self.assertIn("action.dangerous ? 'danger' : ''", html)

    def test_dashboard_cockpit_renders_runtime_tool_call_and_budget_panels(self) -> None:
        template = Path(__file__).resolve().parents[1] / "dashboard_templates" / "gemini_dashboard.html"
        html = template.read_text(encoding="utf-8")
        for snippet in [
            'id="cockpit-runtime-sessions"',
            'id="cockpit-tool-calls"',
            'id="cockpit-budget-summary"',
            "const runtimeSessions = document.getElementById('cockpit-runtime-sessions');",
            "const toolCalls = document.getElementById('cockpit-tool-calls');",
            "const budgetSummary = document.getElementById('cockpit-budget-summary');",
            "cockpit.runtime_sessions",
            "cockpit.tool_calls",
            "cockpit.budget_summary",
            "counts.active_runtime_sessions",
            "counts.running_tool_calls",
            "counts.estimated_cost",
            "budgetBreakdownRows",
            "budget.by_task",
            "budget.by_cost_type",
            "budget.by_model",
            "budget.by_provider",
        ]:
            self.assertIn(snippet, html)

    def test_dashboard_cockpit_enforces_mvp_ui_data_contract(self) -> None:
        template = Path(__file__).resolve().parents[1] / "dashboard_templates" / "gemini_dashboard.html"
        html = template.read_text(encoding="utf-8")
        for snippet in [
            "Mixed currencies: totals are ledger sums, not converted values.",
            "per-currency ledger rows",
            "showHydratedToolCallDetails",
            "No /v1/tool-calls/{tool_call_id} detail endpoint in MVP",
            "employeeEvidenceClientSideFilter",
            "Employee evidence history is backend-filtered by /v1/evidence?employee_id= when available.",
            "backend evidence filter supports employee_id",
            "item.by_currency || item.total_amounts_by_currency || item.currency_totals",
            "No kill or archive session action in MVP",
            "POST /v1/tasks/{task_id}/reopen",
            "completion_invalid_tasks",
        ]:
            self.assertIn(snippet, html)

    def test_dashboard_renders_task_evidence_blocker_and_approval_counts(self) -> None:
        code, submitted = run_cli("task", "submit", "--from", "ops", "--to", "maker", "--task-id", "task-dashboard-blocked", "--title", "blocked task")
        self.assertEqual(code, 0, submitted)
        code, blocked = run_cli("task", "block", "--agent", "maker", "--task-id", "task-dashboard-blocked", "--blocker", "waiting for input")
        self.assertEqual(code, 0, blocked)

        code, submitted_done = run_cli("task", "submit", "--from", "ops", "--to", "maker", "--task-id", "task-dashboard-done", "--title", "done task")
        self.assertEqual(code, 0, submitted_done)
        evidence = self.root / "dashboard-evidence.md"
        evidence.write_text("dashboard evidence\n", encoding="utf-8")
        code, done = run_cli("task", "done", "--agent", "maker", "--task-id", "task-dashboard-done", "--summary", "done", "--evidence", str(evidence))
        self.assertEqual(code, 0, done)

        conn = companyctl.connect()
        try:
            companyctl.create_approval_internal(
                conn,
                source="ops",
                action="external_send",
                reason="structured approval without task id in reason",
                approval_id="approval-dashboard-task",
                metadata={"task_id": "task-dashboard-done"},
            )
            conn.execute(
                """
                INSERT INTO tasks(id, source_agent, target_agent, title, description, priority, status, claimed_by, summary, evidence_path, blocker, created_at, updated_at)
                VALUES ('task-dashboard-missing-evidence', 'ops', 'maker', 'missing evidence dashboard', '', 'P2', 'completed', 'maker', 'bad', '', '', ?, ?)
                """,
                (companyctl.now(), companyctl.now()),
            )
            conn.commit()
        finally:
            conn.close()

        output = self.root / "state" / "dashboard-tasks.html"
        with contextlib.redirect_stdout(io.StringIO()):
            code = company_dashboard.main(["--output", str(output), "--variant", "basic"])
        self.assertEqual(0, code)
        html = output.read_text(encoding="utf-8")
        self.assertIn("<th>evidence</th>", html)
        self.assertIn("<th>blocker</th>", html)
        self.assertIn("<th>approvals</th>", html)
        self.assertIn("Evidence Health", html)
        self.assertIn("task-dashboard-missing-evidence", html)
        self.assertIn("completed_without_evidence", html)
        self.assertIn("task-dashboard-blocked", html)
        self.assertIn("waiting for input", html)
        self.assertIn("task-dashboard-done", html)
        self.assertIn("<td>yes</td>", html)
        self.assertIn("<td>1</td><td>done task</td>", html)

    def test_approval_request_can_bind_to_task_metadata(self) -> None:
        code, submitted = run_cli("task", "submit", "--from", "ops", "--to", "maker", "--task-id", "task-approval-metadata", "--title", "approval task")
        self.assertEqual(code, 0, submitted)

        code, approval = run_cli(
            "approval",
            "request",
            "--from",
            "ops",
            "--action",
            "external_send",
            "--reason",
            "manual approval without embedded id",
            "--task-id",
            "task-approval-metadata",
            "--approval-id",
            "approval-task-metadata",
        )
        self.assertEqual(code, 0, approval)

        code, task = run_cli("task", "show", "--task-id", "task-approval-metadata")
        self.assertEqual(code, 0, task)
        self.assertEqual(["approval-task-metadata"], [item["id"] for item in task["approvals"]])
        self.assertEqual("task-approval-metadata", task["approvals"][0]["detail"]["metadata"]["task_id"])

        output = self.root / "state" / "dashboard-approval-metadata.html"
        with contextlib.redirect_stdout(io.StringIO()):
            code = company_dashboard.main(["--output", str(output), "--variant", "basic"])
        self.assertEqual(0, code)
        html = output.read_text(encoding="utf-8")
        self.assertIn("task-approval-metadata", html)
        self.assertIn("<td>1</td><td>approval task</td>", html)

    def test_approval_mock_resolve_is_dry_run_and_records_event(self) -> None:
        code, submitted = run_cli("task", "submit", "--from", "ops", "--to", "maker", "--task-id", "task-approval-mock-resolve", "--title", "approval mock resolve task")
        self.assertEqual(code, 0, submitted)
        code, approval = run_cli(
            "approval",
            "request",
            "--from",
            "ops",
            "--action",
            "external_send",
            "--reason",
            "manual approval dry run",
            "--target",
            "maker",
            "--task-id",
            "task-approval-mock-resolve",
            "--approval-id",
            "approval-mock-resolve",
        )
        self.assertEqual(code, 0, approval)

        code, rejected = run_cli("approval", "resolve", "--approval-id", "approval-mock-resolve", "--by", "ops", "--reason", "missing mock flag")
        self.assertEqual(2, code)
        self.assertIn("requires --mock", rejected["error"])
        code, resolved = run_cli("approval", "resolve", "--approval-id", "approval-mock-resolve", "--by", "ops", "--reason", "mock only", "--mock")
        self.assertEqual(0, code, resolved)
        self.assertEqual("resolved", resolved["approval"]["status"])
        self.assertTrue(resolved["approval"]["detail"]["mock_resolve"])
        self.assertTrue(resolved["approval"]["detail"]["dry_run"])
        self.assertFalse(resolved["approval"]["detail"]["external_send_executed"])
        self.assertEqual("mock", resolved["approval"]["safety"]["resolution_mode"])
        self.assertTrue(resolved["approval"]["safety"]["dry_run"])
        self.assertFalse(resolved["approval"]["safety"]["external_send_executed"])
        self.assertIn("never triggers", resolved["approval"]["safety"]["summary"])
        self.assertEqual("approval.resolved", resolved["event"]["event_type"])
        self.assertEqual("task-approval-mock-resolve", resolved["event"]["task_id"])

        code, listed = run_cli("approval", "list", "--status", "resolved")
        self.assertEqual(0, code, listed)
        self.assertEqual(["approval-mock-resolve"], [item["id"] for item in listed["approvals"]])
        self.assertEqual("mock", listed["approvals"][0]["safety"]["resolution_mode"])
        code, task = run_cli("task", "show", "--task-id", "task-approval-mock-resolve")
        self.assertEqual(0, code, task)
        self.assertEqual("mock", task["approvals"][0]["safety"]["resolution_mode"])

    def test_approval_list_exposes_owner_control_summary_for_high_risk_actions(self) -> None:
        code, submitted = run_cli("task", "submit", "--from", "ops", "--to", "maker", "--task-id", "task-approval-control", "--title", "approval control task")
        self.assertEqual(code, 0, submitted)
        code, pending = run_cli(
            "approval",
            "request",
            "--from",
            "ops",
            "--action",
            "external_send",
            "--reason",
            "real customer send needs owner approval",
            "--target",
            "maker",
            "--risk",
            "P1",
            "--task-id",
            "task-approval-control",
            "--approval-id",
            "approval-control-pending",
        )
        self.assertEqual(code, 0, pending)
        code, rule_change = run_cli(
            "approval",
            "request",
            "--from",
            "ops",
            "--action",
            "rule_change",
            "--reason",
            "kernel rule change needs owner approval",
            "--risk",
            "P0",
            "--approval-id",
            "approval-control-rule-change",
        )
        self.assertEqual(code, 0, rule_change)
        code, budget_overrun = run_cli(
            "approval",
            "request",
            "--from",
            "ops",
            "--action",
            "budget_overrun",
            "--reason",
            "task spend exceeded hard limit",
            "--risk",
            "P0",
            "--task-id",
            "task-approval-control",
            "--approval-id",
            "approval-control-budget-overrun",
        )
        self.assertEqual(code, 0, budget_overrun)
        code, mock = run_cli(
            "approval",
            "request",
            "--from",
            "ops",
            "--action",
            "external_send",
            "--reason",
            "mock customer send",
            "--target",
            "maker",
            "--risk",
            "P1",
            "--task-id",
            "task-approval-control",
            "--approval-id",
            "approval-control-mock",
        )
        self.assertEqual(code, 0, mock)
        code, resolved = run_cli("approval", "resolve", "--approval-id", "approval-control-mock", "--by", "ops", "--reason", "mock only", "--mock")
        self.assertEqual(code, 0, resolved)

        status, listed = api_gateway.route_get("/v1/approvals", {"status": ["all"], "limit": ["10"]})
        self.assertEqual(200, status, listed)
        summary = listed["approval_control_summary"]
        self.assertEqual(4, summary["total"])
        self.assertEqual(3, summary["by_status"]["pending"])
        self.assertEqual(1, summary["by_status"]["resolved"])
        self.assertEqual(["budget_overrun", "external_send", "rule_change"], summary["high_risk_actions"])
        self.assertEqual(["budget_overrun", "external_send", "rule_change"], summary["pending_high_risk_actions"])
        self.assertEqual(1, summary["dry_run_resolved"])
        self.assertEqual(0, summary["external_send_executed"])
        self.assertEqual(2, summary["real_execution_blockers"]["external_send"])
        self.assertEqual(1, summary["real_execution_blockers"]["budget_overrun"])
        self.assertTrue(summary["real_external_send_requires_owner_approval"])
        self.assertIn("blocked until owner approval", summary["summary"])

    def test_dashboard_renders_project_goal_acceptance_review_and_retro(self) -> None:
        code, project = run_cli(
            "project",
            "create",
            "--project-id",
            "project-dashboard-governance",
            "--title",
            "Project Governance",
            "--goal",
            "Make project state visible",
            "--owner",
            "ops",
            "--acceptance",
            "goal visible;review ready",
        )
        self.assertEqual(code, 0, project)

        code, submitted = run_cli("task", "submit", "--from", "ops", "--to", "maker", "--task-id", "task-project-dashboard", "--title", "project task")
        self.assertEqual(code, 0, submitted)
        evidence = self.root / "project-dashboard-evidence.md"
        evidence.write_text("project evidence\n", encoding="utf-8")
        code, done = run_cli("task", "done", "--agent", "maker", "--task-id", "task-project-dashboard", "--summary", "done", "--evidence", str(evidence))
        self.assertEqual(code, 0, done)

        code, linked = run_cli("project", "link-task", "--project-id", "project-dashboard-governance", "--task-id", "task-project-dashboard")
        self.assertEqual(code, 0, linked)

        code, plan = run_cli(
            "project",
            "plan-add",
            "--project-id",
            "project-dashboard-governance",
            "--title",
            "Ship dashboard governance view",
            "--status",
            "planned",
            "--owner",
            "ops",
            "--task-id",
            "task-project-dashboard",
            "--plan-id",
            "plan-dashboard-governance",
        )
        self.assertEqual(code, 0, plan)
        code, plan_list = run_cli("project", "plan-list", "--project-id", "project-dashboard-governance")
        self.assertEqual(code, 0, plan_list)
        self.assertEqual(["plan-dashboard-governance"], [item["id"] for item in plan_list["plan_items"]])
        self.assertEqual(["task-project-dashboard"], [item["task_id"] for item in plan_list["plan_items"]])
        self.assertEqual(["completed"], [item["task_status"] for item in plan_list["plan_items"]])
        code, shown = run_cli("project", "show", "--project-id", "project-dashboard-governance")
        self.assertEqual(code, 0, shown)
        self.assertEqual(["Ship dashboard governance view"], [item["title"] for item in shown["plan_items"]])
        self.assertEqual(["task-project-dashboard"], [item["task_id"] for item in shown["plan_items"]])
        code, missing_task_plan = run_cli(
            "project",
            "plan-add",
            "--project-id",
            "project-dashboard-governance",
            "--title",
            "missing task plan",
            "--task-id",
            "task-missing-project-plan",
        )
        self.assertEqual(code, 1, missing_task_plan)
        self.assertEqual("task not found", missing_task_plan["error"])

        code, blocked_accept = run_cli("project", "accept", "--project-id", "project-dashboard-governance", "--by", "ops", "--summary", "should wait")
        self.assertEqual(code, 1, blocked_accept)
        self.assertEqual(1, blocked_accept["review"]["plan_counts"]["open"])

        code, plan_done = run_cli(
            "project",
            "plan-add",
            "--project-id",
            "project-dashboard-governance",
            "--title",
            "Retrospective captured",
            "--status",
            "done",
            "--owner",
            "ops",
            "--plan-id",
            "plan-dashboard-retro",
        )
        self.assertEqual(code, 0, plan_done)
        code, plan_status = run_cli(
            "project",
            "plan-status",
            "--project-id",
            "project-dashboard-governance",
            "--plan-id",
            "plan-dashboard-governance",
            "--status",
            "done",
        )
        self.assertEqual(code, 0, plan_status)

        code, accepted = run_cli("project", "accept", "--project-id", "project-dashboard-governance", "--by", "ops", "--summary", "retro: shipped with evidence")
        self.assertEqual(code, 0, accepted)

        output = self.root / "state" / "dashboard-projects.html"
        with contextlib.redirect_stdout(io.StringIO()):
            code = company_dashboard.main(["--output", str(output), "--variant", "basic"])
        self.assertEqual(0, code)
        html = output.read_text(encoding="utf-8")
        self.assertIn("<th>review</th>", html)
        self.assertIn("<th>goal</th>", html)
        self.assertIn("<th>acceptance</th>", html)
        self.assertIn("<th>retro</th>", html)
        self.assertIn("project-dashboard-governance", html)
        self.assertIn("Make project state visible", html)
        self.assertIn("goal visible; review ready", html)
        self.assertIn("retro: shipped with evidence", html)
        self.assertIn("done:Ship dashboard governance view [task-project-dashboard/completed]; done:Retrospective captured", html)
        self.assertIn("<td>ready</td><td>done:Ship dashboard governance view [task-project-dashboard/completed]; done:Retrospective captured</td><td>0</td>", html)

    def test_project_status_completed_requires_review_ready(self) -> None:
        code, project = run_cli(
            "project",
            "create",
            "--project-id",
            "project-status-guard",
            "--title",
            "Project Status Guard",
            "--owner",
            "ops",
            "--acceptance",
            "real evidence attached",
        )
        self.assertEqual(code, 0, project)

        code, blocked = run_cli("project", "status", "--project-id", "project-status-guard", "--status", "completed")
        self.assertEqual(code, 1, blocked)
        self.assertEqual("project is not ready to complete; use project accept after review passes", blocked["error"])
        self.assertFalse(blocked["review"]["ready_to_complete"])
        self.assertEqual(0, blocked["review"]["task_counts"]["total"])

        code, shown = run_cli("project", "show", "--project-id", "project-status-guard")
        self.assertEqual(code, 0, shown)
        self.assertEqual("active", shown["project"]["status"])

    def test_project_backlog_sync_from_queue_file(self) -> None:
        # Create a mock project in the DB first
        run_cli("project", "create", "--project-id", "super-ai-company-kernel", "--title", "Super Kernel", "--owner", "ops")

        # Create mock queue file
        ops_dir = self.root / ".ops"
        ops_dir.mkdir(parents=True, exist_ok=True)
        queue_file = ops_dir / "super-ai-company-kernel-queue.json"

        # Create reports dir and evidence file on disk
        reports_dir = self.root / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        evidence_file = reports_dir / "test-evidence.md"
        evidence_file.write_text("my evidence content", encoding="utf-8")

        queue_data = {
            "project": "super-ai-company-kernel",
            "backlog": [
                {
                    "id": "P1-test-sync-completed",
                    "owner": "codex",
                    "status": "implemented_verified_workspace",
                    "goal": "Test sync goal completed",
                    "evidence": "reports/test-evidence.md"
                },
                {
                    "id": "P1-test-sync-submitted",
                    "owner": "hermes-main",
                    "status": "submitted",
                    "goal": "Test sync goal submitted"
                }
            ]
        }
        queue_file.write_text(json.dumps(queue_data), encoding="utf-8")

        # Running any CLI command should trigger connection and sync
        code, employees = run_cli("employee", "list")
        self.assertEqual(code, 0)

        # Check that the tasks were synced and linked
        code, shown = run_cli("project", "show", "--project-id", "super-ai-company-kernel")
        self.assertEqual(code, 0)
        self.assertEqual(2, len(shown["tasks"]))

        tasks_by_id = {t["id"]: t for t in shown["tasks"]}
        self.assertIn("P1-test-sync-completed", tasks_by_id)
        self.assertIn("P1-test-sync-submitted", tasks_by_id)

        completed_task = tasks_by_id["P1-test-sync-completed"]
        self.assertEqual("completed", completed_task["status"])
        self.assertEqual("codex", completed_task["claimed_by"])
        self.assertEqual(str(evidence_file.resolve()), completed_task["evidence_path"])

        submitted_task = tasks_by_id["P1-test-sync-submitted"]
        self.assertEqual("submitted", submitted_task["status"])
        self.assertEqual("hermes", submitted_task["target_agent"]) # mapped hermes-main -> hermes

        # Test the project review fallback to DB evidence table
        # Let's delete the physical file from disk
        evidence_file.unlink()

        # Run review; it should fail because file does not exist on disk and is not in DB evidence table
        code, review_res = run_cli("project", "review", "--project-id", "super-ai-company-kernel")
        self.assertEqual(code, 0)
        self.assertFalse(review_res["review"]["ready_to_complete"])
        self.assertEqual(1, len(review_res["review"]["evidence_missing_on_disk"]))

        # Now, let's insert the evidence into the database `evidence` table
        conn = companyctl.connect()
        conn.execute(
            """
            INSERT INTO evidence (evidence_id, task_id, employee_id, path_or_url, summary, is_final, created_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            ("ev-1", "P1-test-sync-completed", "codex", str(evidence_file.resolve()), "db evidence summary", "2026-06-07T12:00:00")
        )
        conn.commit()
        conn.close()

        # Run review again; it should no longer complain about missing evidence!
        code, review_res2 = run_cli("project", "review", "--project-id", "super-ai-company-kernel")
        self.assertEqual(code, 0)
        self.assertEqual(0, len(review_res2["review"]["evidence_missing_on_disk"]))

    def test_project_plan_items_sync_from_task_status(self) -> None:
        code, project = run_cli("project", "create", "--project-id", "project-plan-sync", "--title", "Plan Sync", "--owner", "ops")
        self.assertEqual(code, 0, project)

        code, submitted_done = run_cli("task", "submit", "--from", "ops", "--to", "maker", "--task-id", "task-plan-sync-done", "--title", "done task")
        self.assertEqual(code, 0, submitted_done)
        code, plan_done = run_cli(
            "project",
            "plan-add",
            "--project-id",
            "project-plan-sync",
            "--title",
            "Done task plan",
            "--status",
            "planned",
            "--owner",
            "maker",
            "--task-id",
            "task-plan-sync-done",
            "--plan-id",
            "plan-sync-done",
        )
        self.assertEqual(code, 0, plan_done)
        evidence = self.root / "plan-sync-evidence.md"
        evidence.write_text("done\n", encoding="utf-8")
        code, done = run_cli("task", "done", "--agent", "maker", "--task-id", "task-plan-sync-done", "--summary", "done", "--evidence", str(evidence))
        self.assertEqual(code, 0, done)
        self.assertEqual(["plan-sync-done"], [item["id"] for item in done["synced_plan_items"]])
        self.assertEqual(["done"], [item["status"] for item in done["synced_plan_items"]])

        code, submitted_blocked = run_cli("task", "submit", "--from", "ops", "--to", "maker", "--task-id", "task-plan-sync-blocked", "--title", "blocked task")
        self.assertEqual(code, 0, submitted_blocked)
        code, plan_blocked = run_cli(
            "project",
            "plan-add",
            "--project-id",
            "project-plan-sync",
            "--title",
            "Blocked task plan",
            "--status",
            "in_progress",
            "--owner",
            "maker",
            "--task-id",
            "task-plan-sync-blocked",
            "--plan-id",
            "plan-sync-blocked",
        )
        self.assertEqual(code, 0, plan_blocked)
        code, blocked = run_cli("task", "block", "--agent", "maker", "--task-id", "task-plan-sync-blocked", "--blocker", "waiting")
        self.assertEqual(code, 0, blocked)
        self.assertEqual(["plan-sync-blocked"], [item["id"] for item in blocked["synced_plan_items"]])
        self.assertEqual(["blocked"], [item["status"] for item in blocked["synced_plan_items"]])

        code, reopened = run_cli("task", "reopen", "--by", "ops", "--task-id", "task-plan-sync-blocked", "--reason", "input provided")
        self.assertEqual(code, 0, reopened)
        self.assertEqual(["plan-sync-blocked"], [item["id"] for item in reopened["synced_plan_items"]])
        self.assertEqual(["in_progress"], [item["status"] for item in reopened["synced_plan_items"]])

        code, plan_list = run_cli("project", "plan-list", "--project-id", "project-plan-sync")
        self.assertEqual(code, 0, plan_list)
        statuses = {item["id"]: item["status"] for item in plan_list["plan_items"]}
        self.assertEqual("done", statuses["plan-sync-done"])
        self.assertEqual("in_progress", statuses["plan-sync-blocked"])

    def test_doctor_reports_health_issues(self) -> None:
        for agent in ["video-ops", "video-creator", "video-publisher", "codex", "openclaw-main", "hermes", "nestcar"]:
            code, heartbeat = run_cli("heartbeat", "--agent", agent)
            self.assertEqual(code, 0, heartbeat)
        code, runtime = run_cli("runtime", "register", "--runtime", "human", "--command", "manual-human-owner")
        self.assertEqual(code, 0, runtime)
        code, owner = run_cli(
            "employee",
            "create",
            "--id",
            "owner-shift",
            "--name",
            "Shift Shen",
            "--role",
            "human-owner",
            "--runtime",
            "human",
            "--workspace",
            str(self.root / "workspace" / "owner-shift"),
        )
        self.assertEqual(code, 0, owner)
        daemon_state = self.root / "state" / "daemon" / "last-run.json"
        daemon_state.parent.mkdir(parents=True, exist_ok=True)
        daemon_state.write_text(json.dumps({"ok": True, "at": companyctl.now(), "results": []}, ensure_ascii=False), encoding="utf-8")

        conn = companyctl.connect_readonly()
        try:
            runtime_count = conn.execute("SELECT COUNT(*) FROM employee_runtimes").fetchone()[0]
        finally:
            conn.close()
        code, readonly_summary = run_cli("doctor", "--summary")
        self.assertEqual(code, 0, readonly_summary)
        conn = companyctl.connect_readonly()
        try:
            runtime_count_after_summary = conn.execute("SELECT COUNT(*) FROM employee_runtimes").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(runtime_count, runtime_count_after_summary)

        code, healthy = run_cli("doctor")
        self.assertEqual(code, 0, healthy)
        self.assertTrue(healthy["health"]["ok"])
        self.assertEqual([], healthy["health"]["issues"])
        code, healthy_summary = run_cli("doctor", "--summary")
        self.assertEqual(code, 0, healthy_summary)
        self.assertTrue(healthy_summary["ok"])
        self.assertEqual([], healthy_summary["issues"])
        self.assertEqual(0, healthy_summary["heartbeat"]["missing"])
        self.assertEqual(0, healthy_summary["heartbeat"]["stale"])
        self.assertNotIn("owner-shift", healthy_summary["heartbeat"]["missing_agents"])
        self.assertNotIn("owner-shift", healthy_summary["heartbeat"]["stale_agents"])
        self.assertEqual(7, healthy_summary["counts"]["heartbeats"])
        self.assertEqual(0, healthy_summary["counts"]["capability_issues"])
        self.assertEqual(0, healthy_summary["counts"]["task_evidence_issues"])
        self.assertEqual(0, healthy_summary["capabilities"]["issues"])
        self.assertEqual(0, healthy_summary["evidence"]["issues"])
        self.assertTrue(healthy_summary["daemon"]["ok"])
        self.assertTrue(healthy_summary["launchd"]["template_exists"])
        self.assertEqual("ai.openclaw.company-kernel.daemon", healthy_summary["launchd"]["label"])
        self.assertEqual(180, healthy_summary["launchd"]["recommended_interval_seconds"])
        self.assertEqual("bash bin/company-daemon-install-launchd", healthy_summary["launchd"]["install_command"])
        self.assertEqual("bin/companyctl doctor --summary", healthy_summary["launchd"]["verify_command"])
        self.assertFalse(healthy_summary["launchd"]["matches_template"])
        self.assertTrue(healthy_summary["openclaw_guard"]["ok"])
        self.assertEqual([], healthy_summary["openclaw_guard"]["issues"])
        self.assertIn("runtime_inventory", healthy_summary["openclaw_guard"])
        self.assertIn("registered_employee_ids", healthy_summary["openclaw_guard"]["runtime_inventory"])
        self.assertIn("codex", healthy_summary["openclaw_guard"]["runtime_inventory"]["registered_employee_ids"])
        self.assertGreaterEqual(healthy_summary["openclaw_guard"]["runtime_inventory"]["counts"]["registered"], 1)

        openclaw_root = self.root / "openclaw"
        nestcar_spool = openclaw_root / "telegram" / "ingress-spool-nestcar"
        nestcar_spool.mkdir(parents=True, exist_ok=True)
        (nestcar_spool / "0000000784356111.json").write_text('{"updateId":784356111}', encoding="utf-8")
        code, non_strict_openclaw = run_cli("doctor", "--summary")
        self.assertEqual(code, 0, non_strict_openclaw)
        self.assertFalse(non_strict_openclaw["openclaw_guard"]["ok"])
        self.assertIn("telegram_ingress_spool_backlog", non_strict_openclaw["openclaw_guard"]["issues"])
        self.assertEqual(1, non_strict_openclaw["openclaw_guard"]["telegram_spools"]["nestcar"]["pending"])
        code, strict_openclaw = run_cli("doctor", "--summary", "--strict-openclaw")
        self.assertEqual(code, 1, strict_openclaw)
        self.assertIn("telegram_ingress_spool_backlog", strict_openclaw["issues"])
        (nestcar_spool / "0000000784356111.json").unlink()

        watcher = Path.home() / "Library" / "LaunchAgents" / "ai.openclaw.ops-telegram-approval-watcher.plist"
        watcher.parent.mkdir(parents=True, exist_ok=True)
        watcher.write_text("<plist><dict></dict></plist>", encoding="utf-8")
        code, watcher_guard = run_cli("doctor", "--summary", "--strict-openclaw")
        self.assertEqual(code, 1, watcher_guard)
        self.assertIn("external_telegram_approval_watcher_enabled", watcher_guard["issues"])
        self.assertTrue(watcher_guard["openclaw_guard"]["external_approval_watcher"]["installed"])
        watcher.unlink()

        code, strict_launchd = run_cli("doctor", "--summary", "--strict-launchd")
        self.assertEqual(code, 1, strict_launchd)
        self.assertIn("launchd_not_installed", strict_launchd["issues"])
        self.assertFalse(strict_launchd["launchd"]["installed"])

        installed = Path.home() / "Library" / "LaunchAgents" / "ai.openclaw.company-kernel.daemon.plist"
        installed.parent.mkdir(parents=True, exist_ok=True)
        installed.write_text(companyctl.LAUNCHD_TEMPLATE.read_text(encoding="utf-8").replace("__COMPANY_KERNEL_ROOT__", str(self.root)), encoding="utf-8")
        code, strict_installed = run_cli("doctor", "--summary", "--strict-launchd")
        self.assertEqual(code, 0, strict_installed)
        self.assertTrue(strict_installed["launchd"]["installed"])
        self.assertTrue(strict_installed["launchd"]["matches_template"])
        self.assertEqual(str(self.root.resolve()), strict_installed["launchd"]["installed_root"])
        self.assertEqual(str(self.root.resolve()), strict_installed["launchd"]["current_root"])
        self.assertFalse(strict_installed["launchd"]["database_isolated"])

        alternate_root = self.root / "clone"
        installed.write_text(companyctl.LAUNCHD_TEMPLATE.read_text(encoding="utf-8").replace("__COMPANY_KERNEL_ROOT__", str(alternate_root)), encoding="utf-8")
        code, clone_diag = run_cli("doctor", "--summary")
        self.assertEqual(code, 0, clone_diag)
        self.assertFalse(clone_diag["launchd"]["matches_template"])
        self.assertEqual(str(alternate_root.resolve()), clone_diag["launchd"]["installed_root"])
        self.assertEqual(str(self.root.resolve()), clone_diag["launchd"]["current_root"])
        self.assertTrue(clone_diag["launchd"]["database_isolated"])
        self.assertIn("running_from_alternate_clone", clone_diag["launchd"]["warning"])

        installed.write_text("<plist><dict><key>Label</key><string>old</string></dict></plist>", encoding="utf-8")
        code, strict_mismatch = run_cli("doctor", "--summary", "--strict-launchd")
        self.assertEqual(code, 1, strict_mismatch)
        self.assertIn("launchd_template_mismatch", strict_mismatch["issues"])

        daemon_state.write_text(json.dumps({"ok": True, "at": "2020-01-01T00:00:00+00:00", "results": []}, ensure_ascii=False), encoding="utf-8")
        code, stale_daemon = run_cli("doctor", "--summary")
        self.assertEqual(code, 1, stale_daemon)
        self.assertIn("daemon_stale", stale_daemon["issues"])
        self.assertFalse(stale_daemon["daemon"]["ok"])
        daemon_state.write_text(json.dumps({"ok": True, "at": companyctl.now(), "results": []}, ensure_ascii=False), encoding="utf-8")
        conn = companyctl.connect()
        try:
            conn.execute("UPDATE employees SET status = 'active' WHERE id IN ('codex', 'video-ops', 'video-creator', 'maker')")
            conn.commit()
        finally:
            conn.close()

        conn = companyctl.connect()
        try:
            conn.execute(
                """
                INSERT INTO adapter_runs(id, agent_id, command, ok, processed, result_json, created_at)
                VALUES ('adapter-run-failed-doctor', 'codex', 'company-adapter-worker', 0, 0, '{}', '2026-06-03T04:45:00+07:00')
                """
            )
            conn.commit()
        finally:
            conn.close()

        code, adapter_unhealthy = run_cli("doctor")
        self.assertEqual(code, 1, adapter_unhealthy)
        self.assertIn("adapter_failures", adapter_unhealthy["health"]["issues"])
        self.assertEqual("adapter-run-failed-doctor", adapter_unhealthy["health"]["failed_adapter_runs"][0]["id"])

        code, acked = run_cli("runtime", "ack-adapter-run", "--run-id", "adapter-run-failed-doctor", "--by", "ops", "--reason", "known test failure")
        self.assertEqual(code, 0, acked)
        self.assertEqual("video-ops", acked["adapter_run"]["acknowledged_by"])
        self.assertEqual("known test failure", acked["adapter_run"]["acknowledgement_reason"])
        code, healthy_after_ack = run_cli("doctor")
        self.assertEqual(code, 0, healthy_after_ack)
        self.assertNotIn("adapter_failures", healthy_after_ack["health"]["issues"])

        code, submitted_retry = run_cli("task", "submit", "--from", "ops", "--to", "maker", "--task-id", "task-adapter-retry-doctor", "--title", "retry target")
        self.assertEqual(code, 0, submitted_retry)
        code, blocked_retry = run_cli("task", "block", "--agent", "maker", "--task-id", "task-adapter-retry-doctor", "--blocker", "adapter failed")
        self.assertEqual(code, 0, blocked_retry)
        conn = companyctl.connect()
        try:
            conn.execute(
                """
                INSERT INTO adapter_runs(id, agent_id, task_id, command, ok, processed, result_json, created_at)
                VALUES (?, 'video-creator', 'task-adapter-retry-doctor', 'company-adapter-worker', 0, 1, '{}', '2026-06-03T04:46:00+07:00')
                """,
                ("adapter-run-retry-doctor",),
            )
            conn.commit()
        finally:
            conn.close()

        code, retried = run_cli("runtime", "retry-adapter-run", "--run-id", "adapter-run-retry-doctor", "--by", "ops", "--reason", "retry after fix")
        self.assertEqual(code, 0, retried)
        self.assertEqual("task-adapter-retry-doctor", retried["task_id"])
        self.assertEqual("adapter-run-retry-doctor", retried["metadata"]["recovery"]["retry_adapter_run"])
        self.assertEqual("video-ops", retried["metadata"]["recovery"]["retry_requested_by"])
        self.assertEqual("retry after fix", retried["metadata"]["recovery"]["retry_reason"])
        self.assertTrue(retried["event_id"].startswith("evt-"))
        code, retry_task = run_cli("task", "show", "--task-id", "task-adapter-retry-doctor")
        self.assertEqual(code, 0, retry_task)
        self.assertEqual("submitted", retry_task["task"]["status"])
        self.assertEqual("", retry_task["task"]["claimed_by"])
        self.assertEqual("", retry_task["task"]["blocker"])
        self.assertEqual("adapter-run-retry-doctor", retry_task["metadata"]["recovery"]["retry_adapter_run"])
        self.assertEqual("task.retried", retry_task["events"][-1]["event_type"])
        self.assertTrue(any(row["action"] == "runtime.retry_adapter_run" for row in retry_task["audit_logs"]))
        code, healthy_after_retry = run_cli("doctor")
        self.assertEqual(code, 0, healthy_after_retry)
        self.assertNotIn("adapter_failures", healthy_after_retry["health"]["issues"])

        bad_capabilities = self.root / "employees" / "codex" / "capabilities.json"
        bad_capabilities.write_text("{bad json\n", encoding="utf-8")
        code, capability_unhealthy = run_cli("doctor", "--summary")
        self.assertEqual(code, 1, capability_unhealthy)
        self.assertIn("employee_capability_issues", capability_unhealthy["issues"])
        self.assertEqual(["codex"], capability_unhealthy["capabilities"]["agents"])
        bad_capabilities.write_text(
            json.dumps(companyctl.default_capabilities({"id": "codex", "role": "developer", "runtime": "codex"}), ensure_ascii=False),
            encoding="utf-8",
        )

        conn = companyctl.connect()
        try:
            conn.execute(
                """
                INSERT INTO tasks(id, source_agent, target_agent, title, description, priority, status, claimed_by, summary, evidence_path, blocker, created_at, updated_at)
                VALUES ('task-missing-evidence-doctor', 'ops', 'maker', 'missing evidence', '', 'P2', 'completed', 'maker', 'bad', '', '', ?, ?)
                """,
                (companyctl.now(), companyctl.now()),
            )
            conn.commit()
        finally:
            conn.close()
        code, evidence_unhealthy = run_cli("doctor", "--summary")
        self.assertEqual(code, 1, evidence_unhealthy)
        self.assertIn("task_evidence_issues", evidence_unhealthy["issues"])
        self.assertEqual(["task-missing-evidence-doctor"], evidence_unhealthy["evidence"]["tasks"])
        conn = companyctl.connect()
        try:
            conn.execute("DELETE FROM tasks WHERE id = 'task-missing-evidence-doctor'")
            conn.commit()
        finally:
            conn.close()

        code, started = run_cli(
            "conversation",
            "start",
            "--from",
            "maker",
            "--participants",
            "maker,ops",
            "--title",
            "doctor pending",
            "--body",
            "pending event",
            "--conversation-id",
            "conv-doctor-pending-001",
        )
        self.assertEqual(code, 0, started)

        code, unhealthy = run_cli("doctor")
        self.assertEqual(code, 1, unhealthy)
        self.assertFalse(unhealthy["health"]["ok"])
        self.assertIn("pending_events", unhealthy["health"]["issues"])
        self.assertEqual(started["event_id"], unhealthy["health"]["pending"]["events"][0]["id"])

    def test_scheduler_skip_event_marks_pending_event_processed(self) -> None:
        code, started = run_cli(
            "conversation",
            "start",
            "--from",
            "maker",
            "--participants",
            "maker,ops",
            "--title",
            "skip event",
            "--body",
            "skip smoke",
            "--conversation-id",
            "conv-skip-001",
        )
        self.assertEqual(code, 0, started)
        event_id = started["event_id"]

        code, pending = run_cli("scheduler", "events", "--pending")
        self.assertEqual(code, 0, pending)
        self.assertEqual([event_id], [event["id"] for event in pending["events"]])

        code, skipped = run_cli("scheduler", "skip-event", "--event-id", event_id, "--by", "ops", "--reason", "test cleanup")
        self.assertEqual(code, 0, skipped)
        self.assertTrue(skipped["skipped"])
        self.assertTrue(skipped["event"]["processed_at"])

        code, pending_after = run_cli("scheduler", "events", "--pending")
        self.assertEqual(code, 0, pending_after)
        self.assertEqual([], pending_after["events"])

    def test_conversation_messages_emit_events_and_hooks_can_reply(self) -> None:
        code, started = run_cli(
            "conversation",
            "start",
            "--from",
            "maker",
            "--participants",
            "maker,ops",
            "--title",
            "多轮创作讨论",
            "--body",
            "我应该创作什么视频？",
            "--conversation-id",
            "conv-hook-001",
        )
        self.assertEqual(code, 0, started)
        self.assertIn("event_id", started)

        code, scheduled = run_cli("scheduler", "run", "--limit", "5")
        self.assertEqual(code, 0, scheduled)
        self.assertEqual(["maker-conversation-question-reply"], scheduled["events"][0]["matched_hooks"])
        self.assertEqual(1, len(scheduled["events"][0]["actions"]))

        code, detail = run_cli("conversation", "show", "--conversation-id", "conv-hook-001")
        self.assertEqual(code, 0, detail)
        self.assertEqual(2, len(detail["messages"]))
        self.assertEqual("video-ops", detail["messages"][1]["source_agent"])
        self.assertIn("中文竖版视频", detail["messages"][1]["body"])

        code, pending = run_cli("scheduler", "events", "--pending")
        self.assertEqual(code, 0, pending)
        pending_event_types = [event["event_type"] for event in pending["events"]]
        self.assertEqual(["conversation.message"], pending_event_types)

        code, drained = run_cli("scheduler", "run", "--limit", "5")
        self.assertEqual(code, 0, drained)
        self.assertEqual([], drained["events"][0]["matched_hooks"])

    def test_task_done_requires_evidence_and_hook_approval_gate_blocks_publish(self) -> None:
        code, submitted = run_cli(
            "task",
            "submit",
            "--from",
            "ops",
            "--to",
            "maker",
            "--task-id",
            "task-video-001",
            "--title",
            "创作视频",
        )
        self.assertEqual(code, 0, submitted)
        code, claimed = run_cli("task", "claim", "--agent", "maker", "--task-id", "task-video-001")
        self.assertEqual(code, 0, claimed)

        code, rejected = run_cli("task", "done", "--agent", "maker", "--task-id", "task-video-001", "--summary", "done", "--evidence", "")
        self.assertEqual(code, 2, rejected)
        self.assertEqual("task evidence is required", rejected["error"])

        evidence = self.root / "evidence.md"
        evidence.write_text("ok\n", encoding="utf-8")
        code, done = run_cli("task", "done", "--agent", "maker", "--task-id", "task-video-001", "--summary", "done", "--evidence", str(evidence))
        self.assertEqual(code, 0, done)

        code, scheduled = run_cli("scheduler", "run")
        self.assertEqual(code, 0, scheduled)
        self.assertEqual(["maker-done-publish-approval"], scheduled["events"][0]["matched_hooks"])
        self.assertEqual(1, len(scheduled["events"][0]["blocked"]))

        code, detail = run_cli("task", "show", "--task-id", "task-video-001")
        self.assertEqual(code, 0, detail)
        self.assertEqual("completed", detail["task"]["status"])
        self.assertTrue(detail["evidence"]["exists"])
        self.assertIn("task.done", [event["event_type"] for event in detail["events"]])
        self.assertEqual(1, len(detail["approvals"]))
        self.assertEqual("approval-publish-task-video-001", detail["approvals"][0]["id"])

        code, approvals = run_cli("approval", "list", "--status", "pending")
        self.assertEqual(code, 0, approvals)
        self.assertEqual("approval-publish-task-video-001", approvals["approvals"][0]["id"])

        code, pending = run_cli("scheduler", "events", "--pending")
        self.assertEqual(code, 0, pending)
        pending_event_types = [event["event_type"] for event in pending["events"]]
        self.assertIn("task.done", pending_event_types)
        self.assertNotIn("approval.requested", pending_event_types)

        code, approval = run_cli("approval", "approve", "--approval-id", "approval-publish-task-video-001", "--by", "ops", "--reason", "允许发布")
        self.assertEqual(code, 0, approval)
        code, resumed = run_cli("scheduler", "run")
        self.assertEqual(code, 0, resumed)
        self.assertEqual(1, len(resumed["events"][0]["actions"]))

        code, tasks = run_cli("task", "list", "--agent", "publisher")
        self.assertEqual(code, 0, tasks)
        self.assertTrue(any(task["title"] == "发布 maker 完成的视频" for task in tasks["tasks"]))

    def test_task_reopen_and_reassign_restore_blocked_work(self) -> None:
        code, submitted = run_cli("task", "submit", "--from", "ops", "--to", "maker", "--task-id", "task-recover-001", "--title", "恢复任务")
        self.assertEqual(code, 0, submitted)
        code, blocked = run_cli("task", "block", "--agent", "maker", "--task-id", "task-recover-001", "--blocker", "missing input")
        self.assertEqual(code, 0, blocked)

        code, reopened = run_cli("task", "reopen", "--task-id", "task-recover-001", "--by", "ops", "--reason", "input provided")
        self.assertEqual(code, 0, reopened)
        self.assertEqual("submitted", reopened["task"]["status"])
        self.assertEqual("", reopened["task"]["blocker"])
        self.assertEqual("", reopened["task"]["claimed_by"])
        self.assertTrue(Path(reopened["file"]).exists())

        code, submitted2 = run_cli("task", "submit", "--from", "ops", "--to", "maker", "--task-id", "task-reassign-001", "--title", "改派任务")
        self.assertEqual(code, 0, submitted2)
        code, project = run_cli("project", "create", "--project-id", "project-reassign-sync", "--title", "Reassign Sync", "--owner", "ops")
        self.assertEqual(code, 0, project)
        code, plan = run_cli(
            "project",
            "plan-add",
            "--project-id",
            "project-reassign-sync",
            "--title",
            "Reassign owner plan",
            "--owner",
            "maker",
            "--task-id",
            "task-reassign-001",
            "--plan-id",
            "plan-reassign-sync",
        )
        self.assertEqual(code, 0, plan)
        code, blocked2 = run_cli("task", "block", "--agent", "maker", "--task-id", "task-reassign-001", "--blocker", "needs engineering")
        self.assertEqual(code, 0, blocked2)

        code, reassigned = run_cli("task", "reassign", "--task-id", "task-reassign-001", "--by", "ops", "--to", "codex", "--reason", "needs code")
        self.assertEqual(code, 0, reassigned)
        self.assertEqual("codex", reassigned["task"]["target_agent"])
        self.assertEqual("submitted", reassigned["task"]["status"])
        self.assertEqual("", reassigned["task"]["blocker"])
        self.assertEqual("", reassigned["task"]["claimed_by"])
        self.assertEqual(["plan-reassign-sync"], [item["id"] for item in reassigned["synced_plan_items"]])
        self.assertEqual(["codex"], [item["owner_agent"] for item in reassigned["synced_plan_items"]])
        self.assertTrue(Path(reassigned["file"]).exists())

        code, plan_list = run_cli("project", "plan-list", "--project-id", "project-reassign-sync")
        self.assertEqual(code, 0, plan_list)
        self.assertEqual(["codex"], [item["owner_agent"] for item in plan_list["plan_items"]])

    def test_task_discussion_binds_conversation_to_task(self) -> None:
        code, submitted = run_cli("task", "submit", "--from", "ops", "--to", "maker", "--task-id", "task-discuss-001", "--title", "协作任务")
        self.assertEqual(code, 0, submitted)

        code, discussed = run_cli(
            "task",
            "discuss",
            "--task-id",
            "task-discuss-001",
            "--from",
            "ops",
            "--participants",
            "codex",
            "--body",
            "请 maker 和 codex 讨论执行方案",
            "--conversation-id",
            "conv-task-discuss-001",
        )
        self.assertEqual(code, 0, discussed)
        self.assertEqual("conv-task-discuss-001", discussed["conversation"]["id"])
        self.assertEqual(["video-ops", "video-creator", "codex"], discussed["conversation"]["participants"])
        self.assertEqual(["conv-task-discuss-001"], discussed["conversation_ids"])

        code, conversations = run_cli("task", "conversations", "--task-id", "task-discuss-001")
        self.assertEqual(code, 0, conversations)
        self.assertEqual(["conv-task-discuss-001"], conversations["conversation_ids"])
        self.assertEqual(["请 maker 和 codex 讨论执行方案"], [message["body"] for message in conversations["conversations"][0]["messages"]])

        code, shown = run_cli("task", "show", "--task-id", "task-discuss-001")
        self.assertEqual(code, 0, shown)
        self.assertEqual(["conv-task-discuss-001"], shown["metadata"]["conversation_ids"])
        self.assertEqual(1, shown["conversation_summary"]["counts"]["conversations"])
        self.assertEqual(1, shown["conversation_summary"]["counts"]["messages"])
        self.assertEqual("conv-task-discuss-001", shown["conversation_summary"]["items"][0]["conversation_id"])
        self.assertEqual("请 maker 和 codex 讨论执行方案", shown["conversation_summary"]["items"][0]["latest_message"])
        self.assertTrue(any(row["action"] == "task.discuss" for row in shown["audit_logs"]))

    def test_task_split_plan_collects_long_task_with_evidence(self) -> None:
        code, submitted = run_cli("task", "submit", "--from", "ops", "--to", "maker", "--task-id", "task-long-001", "--title", "长任务")
        self.assertEqual(code, 0, submitted)
        plan = self.root / "split-plan.json"
        plan.write_text(
            json.dumps(
                {
                    "items": [
                        {"target": "maker", "title": "子任务 A", "description": "完成 A", "priority": "P1"},
                        {"target": "publisher", "title": "子任务 B", "description": "完成 B", "priority": "P2"},
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        code, split = run_cli("task", "split", "--task-id", "task-long-001", "--by", "maker", "--plan", str(plan), "--child-id-prefix", "task-long-001-child")
        self.assertEqual(code, 0, split)
        self.assertTrue(split["event_id"].startswith("evt-"))
        self.assertEqual(["task-long-001-child-01", "task-long-001-child-02"], [item["task"]["id"] for item in split["children"]])

        code, shown = run_cli("task", "show", "--task-id", "task-long-001")
        self.assertEqual(code, 0, shown)
        self.assertEqual("task.split", shown["events"][-1]["event_type"])
        self.assertEqual(2, len(shown["children"]))

        code, child = run_cli("task", "show", "--task-id", "task-long-001-child-01")
        self.assertEqual(code, 0, child)
        self.assertEqual("task-long-001", child["metadata"]["parent_task_id"])
        self.assertEqual("video-creator", child["metadata"]["split_by"])
        self.assertEqual(1, child["metadata"]["split_index"])
        self.assertEqual("task-long-001", child["parents"][0]["parent_task_id"])

        evidence_a = self.root / "child-a.md"
        evidence_b = self.root / "child-b.md"
        evidence_a.write_text("A ok\n", encoding="utf-8")
        evidence_b.write_text("B ok\n", encoding="utf-8")
        code, done_a = run_cli("task", "done", "--agent", "maker", "--task-id", "task-long-001-child-01", "--summary", "A done", "--evidence", str(evidence_a))
        self.assertEqual(code, 0, done_a)
        code, done_b = run_cli("task", "done", "--agent", "publisher", "--task-id", "task-long-001-child-02", "--summary", "B done", "--evidence", str(evidence_b))
        self.assertEqual(code, 0, done_b)

        code, collected = run_cli("task", "collect", "--task-id", "task-long-001", "--agent", "maker", "--summary", "全部完成")
        self.assertEqual(code, 0, collected)
        self.assertEqual("completed", collected["collection"]["status"])
        self.assertTrue(Path(collected["collection"]["evidence"]).exists())

        output = self.root / "state" / "dashboard-long-task.html"
        with contextlib.redirect_stdout(io.StringIO()):
            code = company_dashboard.main(["--output", str(output), "--variant", "basic"])
        self.assertEqual(code, 0)
        html = output.read_text(encoding="utf-8")
        self.assertIn("Long Task Delegation", html)
        self.assertIn("task-long-001", html)
        self.assertIn("2/2", html)
        self.assertIn("task-long-001-child-01/video-creator/completed", html)
        self.assertIn("task-long-001-child-02/video-publisher/completed", html)

    def test_task_trace_id_flows_into_events(self) -> None:
        evidence = self.root / "evidence" / "trace.md"
        evidence.parent.mkdir(exist_ok=True)
        evidence.write_text("trace evidence", encoding="utf-8")

        code, submitted = run_cli("task", "submit", "--from", "openclaw-main", "--to", "codex", "--task-id", "task-trace-flow", "--title", "trace flow")
        self.assertEqual(code, 0, submitted)
        trace_id = submitted["task"]["metadata"]["trace_id"]
        self.assertTrue(trace_id.startswith("trace-"))

        code, claimed = run_cli("task", "claim", "--agent", "codex", "--task-id", "task-trace-flow")
        self.assertEqual(code, 0, claimed)
        code, done = run_cli("task", "done", "--agent", "codex", "--task-id", "task-trace-flow", "--summary", "done", "--evidence", str(evidence))
        self.assertEqual(code, 0, done)

        conn = companyctl.connect()
        try:
            row = conn.execute("SELECT trace_id, event_type, task_id FROM company_events WHERE id = ?", (done["event_id"],)).fetchone()
            self.assertEqual(trace_id, row["trace_id"])
            self.assertEqual("task.done", row["event_type"])
            self.assertEqual("task-trace-flow", row["task_id"])
        finally:
            conn.close()

    def test_codex_adapter_attendance_probe_does_not_claim_task(self) -> None:
        code, created = run_cli(
            "employee",
            "create",
            "--id",
            "codex",
            "--name",
            "Codex",
            "--role",
            "developer",
            "--runtime",
            "codex",
            "--workspace",
            str(self.root / "workspace" / "codex"),
        )
        self.assertEqual(code, 0, created)
        code, created = run_cli(
            "employee",
            "create",
            "--id",
            "main",
            "--name",
            "main",
            "--role",
            "agent",
            "--runtime",
            "openclaw",
            "--workspace",
            str(self.root / "workspace" / "main"),
        )
        self.assertEqual(code, 0, created)
        code, submitted = run_cli("task", "submit", "--from", "main", "--to", "codex", "--task-id", "task-codex-attendance-side-effect", "--title", "must stay submitted")
        self.assertEqual(code, 0, submitted)
        code = codex_adapter.main(["--agent", "codex", "--attendance-probe"])
        self.assertEqual(0, code)
        code, shown = run_cli("task", "show", "--task-id", "task-codex-attendance-side-effect")
        self.assertEqual(code, 0, shown)
        self.assertEqual("submitted", shown["task"]["status"])

    def test_company_trace_exports_trace_timeline(self) -> None:
        evidence = self.root / "evidence" / "trace-export.md"
        evidence.parent.mkdir(exist_ok=True)
        evidence.write_text("trace export evidence", encoding="utf-8")

        code, submitted = run_cli("task", "submit", "--from", "openclaw-main", "--to", "codex", "--task-id", "task-trace-export", "--title", "trace export")
        self.assertEqual(code, 0, submitted)
        trace_id = submitted["task"]["metadata"]["trace_id"]
        code, claimed = run_cli("task", "claim", "--agent", "codex", "--task-id", "task-trace-export")
        self.assertEqual(code, 0, claimed)
        code, done = run_cli("task", "done", "--agent", "codex", "--task-id", "task-trace-export", "--summary", "done", "--evidence", str(evidence))
        self.assertEqual(code, 0, done)

        conn = companyctl.connect()
        try:
            conn.execute(
                """
                INSERT INTO adapter_runs(id, trace_id, agent_id, task_id, command, ok, processed, attempt, next_retry_at, result_json, created_at)
                VALUES ('adapter-run-trace-export', ?, 'codex', 'task-trace-export', 'company-codex-adapter', 1, 1, 1, '', '{}', ?)
                """,
                (trace_id, companyctl.now()),
            )
            conn.commit()
            trace = company_trace.load_trace(conn, trace_id)
            self.assertEqual(trace_id, trace["trace_id"])
            self.assertEqual(["task-trace-export"], [task["id"] for task in trace["tasks"]])
            self.assertEqual(["adapter-run-trace-export"], [run["id"] for run in trace["adapter_runs"]])
            self.assertTrue(any(item["kind"] == "event" and item["label"] == "task.done" for item in trace["timeline"]))
        finally:
            conn.close()

        files = company_trace.write_outputs(trace)
        html = Path(files["html"])
        json_file = Path(files["json"])
        self.assertTrue(html.exists())
        self.assertTrue(json_file.exists())
        self.assertIn(trace_id, html.read_text(encoding="utf-8"))
        self.assertIn("task-trace-export", html.read_text(encoding="utf-8"))
        exported = json.loads(json_file.read_text(encoding="utf-8"))
        self.assertEqual(trace_id, exported["trace_id"])

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = company_trace.main(["--task-id", "task-trace-export", "--json-only"])
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(trace_id, payload["trace_id"])
        self.assertGreaterEqual(len(payload["timeline"]), 3)

    def test_api_and_cli_expose_sanitized_trace_timeline(self) -> None:
        for employee_id, role in [("main", "operator"), ("hermes", "supervisor"), ("codex", "developer"), ("qa", "qa")]:
            code, created = run_cli("employee", "create", "--id", employee_id, "--name", employee_id, "--role", role, "--runtime", "local", "--workspace", str(self.root / "workspace" / employee_id))
            self.assertEqual(0, code, created)
            if employee_id not in {"main", "hermes"}:
                self.mark_active(employee_id)
        code, submitted = run_cli("task", "submit", "--from", "main", "--to", "codex", "--task-id", "task-trace-api", "--title", "Trace API")
        self.assertEqual(0, code, submitted)
        trace_id = submitted["task"]["metadata"]["trace_id"]
        code, qa_task = run_cli("task", "submit", "--from", "main", "--to", "qa", "--task-id", "task-trace-api-qa", "--title", "Trace API QA")
        self.assertEqual(0, code, qa_task)
        code, running = run_cli("task", "run", "--task-id", "task-trace-api", "--agent", "codex", "--by", "hermes")
        self.assertEqual(0, code, running)
        attempt_id = running["attempt"]["attempt_id"]
        code, corrected = run_cli("task", "correct", "--task-id", "task-trace-api", "--attempt-id", attempt_id, "--by", "hermes", "--message", "请聚焦 evidence")
        self.assertEqual(0, code, corrected)
        code, acked = run_cli("task", "correct", "--task-id", "task-trace-api", "--attempt-id", attempt_id, "--by", "codex", "--message", "收到纠偏", "--ack")
        self.assertEqual(0, code, acked)

        conn = companyctl.connect()
        secret = "sk-traceTimelineSECRET1234567890"
        try:
            workspace = companyctl.ensure_task_workspace(conn, "task-trace-api")
            final_path = Path(workspace["path"]) / "final" / "trace-delivery.md"
            final_path.write_text("trace delivery\n", encoding="utf-8")
            artifact = companyctl.register_artifact_internal(
                conn,
                task_id="task-trace-api",
                employee_id="codex",
                path=str(final_path),
                artifact_type="markdown",
                summary="trace delivery",
                stage="final",
                is_final=True,
            )["artifact"]
            created_handoff = companyctl.create_handoff_internal(
                conn,
                from_task_id="task-trace-api",
                to_task_id="task-trace-api-qa",
                from_employee_id="codex",
                to_employee_id="qa",
                summary="handoff trace artifact",
                artifacts=[artifact["artifact_id"]],
            )
            companyctl.promote_artifact_to_evidence_internal(conn, artifact_id=artifact["artifact_id"], by="codex", summary="")
            conn.execute(
                """
                INSERT INTO adapter_runs(id, trace_id, agent_id, task_id, command, ok, processed, attempt, next_retry_at, result_json, created_at)
                VALUES ('adapter-run-trace-api', ?, 'codex', 'task-trace-api', 'company-codex-adapter', 0, 1, 1, '', ?, ?)
                """,
                (
                    trace_id,
                    json.dumps({"stderr": f"api_key={secret} reading /Users/shift/.ssh/id_rsa", "stdout": "safe trace output"}, ensure_ascii=False),
                    companyctl.now(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        status, api_payload = api_gateway.route_get(f"/v1/traces/{trace_id}/timeline", {})
        self.assertEqual(HTTPStatus.OK, status, api_payload)
        self.assertTrue(api_payload["ok"])
        code, cli_payload = run_cli("trace", "timeline", "--trace-id", trace_id)
        self.assertEqual(0, code, cli_payload)
        self.assertEqual(api_payload["timeline"], cli_payload["timeline"])
        self.assertEqual(api_payload["counts"], cli_payload["counts"])
        self.assertEqual(api_payload["counts"]["execution_attempts"], len(api_payload["execution_attempts"]))
        self.assertEqual(api_payload["counts"]["artifacts"], len(api_payload["artifacts"]))
        self.assertEqual(api_payload["counts"]["handoffs"], len(api_payload["handoffs"]))
        self.assertEqual(api_payload["counts"]["evidence"], len(api_payload["evidence"]))
        self.assertEqual(attempt_id, api_payload["execution_attempts"][0]["attempt_id"])
        self.assertEqual(artifact["artifact_id"], api_payload["artifacts"][0]["artifact_id"])
        self.assertEqual(created_handoff["handoff"]["handoff_id"], api_payload["handoffs"][0]["handoff_id"])
        self.assertTrue(api_payload["evidence"][0]["display"]["allowed"])
        self.assertFalse(api_payload["evidence"][0]["display"]["absolute_path_exposed"])
        timeline_kinds = [item["kind"] for item in api_payload["timeline"]]
        self.assertIn("attempt", timeline_kinds)
        self.assertIn("event", timeline_kinds)
        self.assertIn("artifact", timeline_kinds)
        self.assertIn("handoff", timeline_kinds)
        self.assertIn("evidence", timeline_kinds)
        correction_items = [item for item in api_payload["timeline"] if item.get("label") in {"supervisor.correction_requested", "supervisor.correction_acknowledged"}]
        self.assertEqual(2, len(correction_items))
        self.assertEqual(["correction_requested", "correction_acknowledged"], [item["action"] for item in correction_items])
        self.assertEqual(["hermes", "codex"], [item["actor"] for item in correction_items])
        self.assertEqual(["codex", "hermes"], [item["target"] for item in correction_items])
        self.assertTrue(all(item["attempt_id"] == attempt_id for item in correction_items))
        self.assertEqual(2, len(api_payload["supervision_chain"]))
        self.assertEqual(
            ["hermes -> codex · correction_requested · task-trace-api", "codex -> hermes · correction_acknowledged · task-trace-api"],
            [item["summary"] for item in api_payload["supervision_chain"]],
        )
        self.assertIn("trace_story", api_payload)
        self.assertEqual("incomplete", api_payload["trace_story"]["state"])
        self.assertIn("task.created", api_payload["trace_story"]["required_chain"])
        self.assertIn("tool.call.started", api_payload["trace_story"]["required_chain"])
        self.assertIn("artifact.created", api_payload["trace_story"]["observed_stages"])
        self.assertIn("handoff.created", api_payload["trace_story"]["observed_stages"])
        self.assertIn("evidence.promoted", api_payload["trace_story"]["observed_stages"])
        self.assertIn("tool.call.started", api_payload["trace_story"]["missing_required"])
        self.assertTrue(api_payload["trace_story"]["has_failure_or_recovery"])
        self.assertTrue(api_payload["ceo_timeline"])
        ceo_text = json.dumps(api_payload["ceo_timeline"], ensure_ascii=False)
        self.assertIn("supervisor.correction_requested", ceo_text)
        self.assertIn("artifact.created", ceo_text)
        self.assertIn("adapter failed", ceo_text)
        payload_json = json.dumps(api_payload, ensure_ascii=False)
        self.assertIn("safe trace output", payload_json)
        self.assertIn("trace-delivery.md", payload_json)
        self.assertNotIn(str(self.root), payload_json)
        self.assertNotIn(secret, payload_json)
        self.assertNotIn("id_rsa", payload_json)
        self.assertNotIn("path_or_url", payload_json)

    def test_runtime_sessions_and_tool_calls_are_first_class_trace_records(self) -> None:
        code, submitted = run_cli("task", "submit", "--from", "openclaw-main", "--to", "codex", "--task-id", "task-control-plane-ledger", "--title", "Control plane ledger")
        self.assertEqual(0, code, submitted)
        trace_id = submitted["task"]["metadata"]["trace_id"]
        code, running = run_cli("task", "run", "--task-id", "task-control-plane-ledger", "--agent", "codex", "--by", "hermes", "--adapter-type", "codex")
        self.assertEqual(0, code, running)
        attempt_id = running["attempt"]["attempt_id"]

        code, session = run_cli(
            "runtime",
            "session",
            "start",
            "--session-id",
            "session-control-plane-ledger",
            "--employee",
            "codex",
            "--adapter-type",
            "codex",
            "--runtime-type",
            "cli",
            "--pid",
            "4242",
            "--session-key",
            "codex-plan-session",
            "--task-id",
            "task-control-plane-ledger",
            "--attempt-id",
            attempt_id,
        )
        self.assertEqual(0, code, session)
        self.assertEqual("active", session["session"]["status"])
        self.assertEqual(trace_id, session["session"]["trace_id"])

        code, heartbeat = run_cli("runtime", "session", "heartbeat", "--session-id", "session-control-plane-ledger", "--status", "active")
        self.assertEqual(0, code, heartbeat)
        self.assertEqual("active", heartbeat["session"]["status"])
        self.assertNotEqual("", heartbeat["session"]["last_heartbeat_at"])

        secret = "sk-toolCallSECRET1234567890"
        code, tool = run_cli(
            "tool-call",
            "start",
            "--tool-call-id",
            "tool-call-control-plane-ledger",
            "--trace-id",
            trace_id,
            "--task-id",
            "task-control-plane-ledger",
            "--attempt-id",
            attempt_id,
            "--employee",
            "codex",
            "--session-id",
            "session-control-plane-ledger",
            "--tool-name",
            "shell",
            "--tool-type",
            "shell",
            "--input-summary",
            f"run tests with token={secret}",
            "--risk-level",
            "low",
        )
        self.assertEqual(0, code, tool)
        self.assertEqual("running", tool["tool_call"]["status"])

        code, finished_tool = run_cli(
            "tool-call",
            "finish",
            "--tool-call-id",
            "tool-call-control-plane-ledger",
            "--status",
            "success",
            "--output-summary",
            f"tests passed; inspected /Users/shift/.ssh/id_rsa; token={secret}",
        )
        self.assertEqual(0, code, finished_tool)
        self.assertEqual("success", finished_tool["tool_call"]["status"])

        status, sessions = api_gateway.route_get("/v1/runtime-sessions", {"employee_id": ["codex"]})
        self.assertEqual(HTTPStatus.OK, status, sessions)
        self.assertTrue(sessions["ok"])
        self.assertEqual(["session-control-plane-ledger"], [item["session_id"] for item in sessions["runtime_sessions"]])

        status, tool_calls = api_gateway.route_get("/v1/tool-calls", {"task_id": ["task-control-plane-ledger"]})
        self.assertEqual(HTTPStatus.OK, status, tool_calls)
        self.assertTrue(tool_calls["ok"])
        self.assertEqual(["tool-call-control-plane-ledger"], [item["tool_call_id"] for item in tool_calls["tool_calls"]])
        api_tool_call = tool_calls["tool_calls"][0]
        self.assertTrue(api_tool_call["sanitized"])
        self.assertFalse(api_tool_call["raw_available"])
        self.assertEqual("sanitized_only", api_tool_call["redaction_policy"]["mode"])
        self.assertIn("raw tool payload hidden", api_tool_call["redaction_policy"]["summary"])
        status, session_tool_calls = api_gateway.route_get("/v1/tool-calls", {"session_id": ["session-control-plane-ledger"]})
        self.assertEqual(HTTPStatus.OK, status, session_tool_calls)
        self.assertEqual(["tool-call-control-plane-ledger"], [item["tool_call_id"] for item in session_tool_calls["tool_calls"]])
        status, other_session_tool_calls = api_gateway.route_get("/v1/tool-calls", {"session_id": ["session-control-plane-other"]})
        self.assertEqual(HTTPStatus.OK, status, other_session_tool_calls)
        self.assertEqual([], other_session_tool_calls["tool_calls"])
        tool_payload = json.dumps(tool_calls, ensure_ascii=False)
        self.assertNotIn(secret, tool_payload)
        self.assertNotIn("id_rsa", tool_payload)

        status, api_payload = api_gateway.route_get(f"/v1/traces/{trace_id}/timeline", {})
        self.assertEqual(HTTPStatus.OK, status, api_payload)
        self.assertEqual(1, api_payload["counts"]["runtime_sessions"])
        self.assertEqual(1, api_payload["counts"]["tool_calls"])
        kinds = [item["kind"] for item in api_payload["timeline"]]
        self.assertIn("runtime_session", kinds)
        self.assertIn("tool_call", kinds)
        trace_payload = json.dumps(api_payload, ensure_ascii=False)
        self.assertIn("tool-call-control-plane-ledger", trace_payload)
        self.assertIn("session-control-plane-ledger", trace_payload)
        self.assertNotIn(secret, trace_payload)
        self.assertNotIn("id_rsa", trace_payload)
        self.assertNotIn("result_json", trace_payload)

        status, cockpit = api_gateway.route_get("/v1/dashboard/cockpit", {})
        self.assertEqual(HTTPStatus.OK, status, cockpit)
        self.assertEqual(1, cockpit["counts"]["runtime_sessions"])
        self.assertEqual(1, cockpit["counts"]["active_runtime_sessions"])
        self.assertEqual(1, cockpit["counts"]["tool_calls"])
        self.assertEqual(0, cockpit["counts"]["running_tool_calls"])
        self.assertEqual(["session-control-plane-ledger"], [item["session_id"] for item in cockpit["runtime_sessions"]])
        self.assertEqual(["tool-call-control-plane-ledger"], [item["tool_call_id"] for item in cockpit["tool_calls"]])

    def test_budget_center_records_costs_in_trace_and_cockpit(self) -> None:
        code, submitted = run_cli("task", "submit", "--from", "openclaw-main", "--to", "codex", "--task-id", "task-budget-ledger", "--title", "Budget ledger")
        self.assertEqual(0, code, submitted)
        trace_id = submitted["task"]["metadata"]["trace_id"]
        code, running = run_cli("task", "run", "--task-id", "task-budget-ledger", "--agent", "codex", "--by", "hermes", "--adapter-type", "codex")
        self.assertEqual(0, code, running)
        attempt_id = running["attempt"]["attempt_id"]

        code, budget = run_cli(
            "budget",
            "record",
            "--budget-event-id",
            "budget-event-ledger",
            "--task-id",
            "task-budget-ledger",
            "--attempt-id",
            attempt_id,
            "--employee",
            "codex",
            "--cost-type",
            "model_api",
            "--amount",
            "0.42",
            "--currency",
            "USD",
            "--token-input",
            "1200",
            "--token-output",
            "340",
            "--model-name",
            "gpt-5",
            "--provider",
            "openai",
            "--runtime-seconds",
            "75",
            "--summary",
            "budget smoke cost",
        )
        self.assertEqual(0, code, budget)
        self.assertEqual("budget-event-ledger", budget["budget_event"]["budget_event_id"])
        self.assertEqual(trace_id, budget["budget_event"]["trace_id"])

        code, summary = run_cli("budget", "summary", "--task-id", "task-budget-ledger")
        self.assertEqual(0, code, summary)
        self.assertEqual(1, summary["summary"]["event_count"])
        self.assertEqual(0.42, summary["summary"]["total_amount"])
        self.assertEqual(1200, summary["summary"]["token_input"])
        self.assertEqual(340, summary["summary"]["token_output"])
        self.assertEqual(75, summary["summary"]["runtime_seconds"])

        status, budget_events = api_gateway.route_get("/v1/budget-events", {"task_id": ["task-budget-ledger"]})
        self.assertEqual(HTTPStatus.OK, status, budget_events)
        self.assertTrue(budget_events["ok"])
        self.assertEqual(["budget-event-ledger"], [item["budget_event_id"] for item in budget_events["budget_events"]])

        status, budget_summary = api_gateway.route_get("/v1/budget-summary", {"task_id": ["task-budget-ledger"]})
        self.assertEqual(HTTPStatus.OK, status, budget_summary)
        self.assertEqual(0.42, budget_summary["summary"]["total_amount"])
        self.assertEqual({"codex": 0.42}, budget_summary["summary"]["by_employee"])
        self.assertEqual({"task-budget-ledger": 0.42}, budget_summary["summary"]["by_task"])
        self.assertEqual({"model_api": 0.42}, budget_summary["summary"]["by_cost_type"])
        self.assertEqual({"gpt-5": 0.42}, budget_summary["summary"]["by_model"])
        self.assertEqual({"openai": 0.42}, budget_summary["summary"]["by_provider"])

        status, trace = api_gateway.route_get(f"/v1/traces/{trace_id}/timeline", {})
        self.assertEqual(HTTPStatus.OK, status, trace)
        self.assertEqual(1, trace["counts"]["budget_events"])
        self.assertIn("budget_event", [item["kind"] for item in trace["timeline"]])

        status, cockpit = api_gateway.route_get("/v1/dashboard/cockpit", {})
        self.assertEqual(HTTPStatus.OK, status, cockpit)
        self.assertEqual(1, cockpit["counts"]["budget_events"])
        self.assertEqual(0.42, cockpit["budget_summary"]["total_amount"])
        self.assertEqual(1200, cockpit["budget_summary"]["token_input"])
        self.assertEqual(340, cockpit["budget_summary"]["token_output"])
        self.assertEqual({"gpt-5": 0.42}, cockpit["budget_summary"]["by_model"])
        self.assertEqual({"openai": 0.42}, cockpit["budget_summary"]["by_provider"])

    def test_budget_summary_reports_soft_and_hard_limit_status(self) -> None:
        with sqlite3.connect(self.root / "company.sqlite") as conn:
            conn.execute(
                """
                INSERT INTO budget_accounts(
                  budget_account_id, scope_type, scope_id, currency, soft_limit, hard_limit, status, created_at, updated_at, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "budget-account-task-limit",
                    "task",
                    "task-budget-limit",
                    "USD",
                    0.5,
                    1.0,
                    "active",
                    datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                    datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                    "{}",
                ),
            )
            conn.commit()

        code, submitted = run_cli("task", "submit", "--from", "openclaw-main", "--to", "codex", "--task-id", "task-budget-limit", "--title", "Budget limit")
        self.assertEqual(0, code, submitted)
        code, budget = run_cli(
            "budget",
            "record",
            "--budget-event-id",
            "budget-event-limit",
            "--budget-account-id",
            "budget-account-task-limit",
            "--task-id",
            "task-budget-limit",
            "--employee",
            "codex",
            "--cost-type",
            "model_api",
            "--amount",
            "0.75",
            "--currency",
            "USD",
            "--summary",
            "soft limit crossed",
        )
        self.assertEqual(0, code, budget)

        status, budget_summary = api_gateway.route_get("/v1/budget-summary", {"task_id": ["task-budget-limit"]})
        self.assertEqual(HTTPStatus.OK, status, budget_summary)
        self.assertEqual("soft_exceeded", budget_summary["summary"]["limit_status"])
        self.assertEqual(0.5, budget_summary["summary"]["budget_limits"]["soft_limit"])
        self.assertEqual(1.0, budget_summary["summary"]["budget_limits"]["hard_limit"])
        self.assertEqual(0.25, budget_summary["summary"]["budget_limits"]["remaining_to_hard"])

    def test_budget_record_auto_requests_owner_approval_when_hard_limit_exceeded(self) -> None:
        with sqlite3.connect(self.root / "company.sqlite") as conn:
            conn.execute(
                """
                INSERT INTO budget_accounts(
                  budget_account_id, scope_type, scope_id, currency, soft_limit, hard_limit, status, created_at, updated_at, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "budget-account-auto-approval",
                    "task",
                    "task-budget-auto-approval",
                    "USD",
                    0.5,
                    1.0,
                    "active",
                    datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                    datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                    "{}",
                ),
            )
            conn.commit()

        code, submitted = run_cli("task", "submit", "--from", "openclaw-main", "--to", "codex", "--task-id", "task-budget-auto-approval", "--title", "Budget auto approval")
        self.assertEqual(0, code, submitted)
        code, budget = run_cli(
            "budget",
            "record",
            "--budget-event-id",
            "budget-event-auto-approval",
            "--budget-account-id",
            "budget-account-auto-approval",
            "--task-id",
            "task-budget-auto-approval",
            "--employee",
            "codex",
            "--cost-type",
            "model_api",
            "--amount",
            "1.25",
            "--currency",
            "USD",
            "--summary",
            "hard limit crossed",
        )
        self.assertEqual(0, code, budget)
        self.assertEqual("hard_exceeded", budget["budget_limits"]["status"])
        self.assertEqual("budget_overrun", budget["approval"]["action"])
        self.assertEqual("pending", budget["approval"]["status"])
        self.assertEqual("task-budget-auto-approval", budget["approval"]["detail"]["metadata"]["task_id"])
        self.assertEqual(1.25, budget["approval"]["detail"]["metadata"]["budget_amount"])
        self.assertEqual(1.0, budget["approval"]["detail"]["metadata"]["hard_limit"])
        self.assertEqual("approval.requested", budget["approval_event"]["event_type"])
        self.assertEqual("task-budget-auto-approval", budget["approval_event"]["task_id"])
        self.assertTrue(budget["approval_event"]["processed_at"])

        status, approvals = api_gateway.route_get("/v1/approvals", {"status": ["pending"], "action": ["budget_overrun"]})
        self.assertEqual(HTTPStatus.OK, status, approvals)
        self.assertEqual(["budget-overrun-task-budget-auto-approval"], [item["id"] for item in approvals["approvals"]])
        self.assertEqual(1, approvals["approval_control_summary"]["real_execution_blockers"]["budget_overrun"])
        trace_id = submitted["task"]["metadata"]["trace_id"]
        status, trace = api_gateway.route_get(f"/v1/traces/{trace_id}/timeline", {})
        self.assertEqual(HTTPStatus.OK, status, trace)
        timeline = trace["timeline"]
        self.assertIn("budget_event", [item["kind"] for item in timeline])
        approval_events = [item for item in timeline if item.get("label") == "approval.requested"]
        self.assertEqual(["budget-overrun-task-budget-auto-approval"], [item["approval_id"] for item in approval_events])
        self.assertEqual(["budget_overrun"], [item["approval_action"] for item in approval_events])
        ceo_approval_items = [item for item in trace["ceo_timeline"] if item.get("approval_id") == "budget-overrun-task-budget-auto-approval"]
        self.assertEqual(["critical"], [item["severity"] for item in ceo_approval_items])
        self.assertEqual(["owner approval required"], [item["recommended_action"] for item in ceo_approval_items])
        self.assertIn("budget_overrun", ceo_approval_items[0]["summary"])

    def test_budget_summary_reports_mixed_currency_ledger_rows(self) -> None:
        code, submitted = run_cli("task", "submit", "--from", "openclaw-main", "--to", "codex", "--task-id", "task-budget-mixed", "--title", "Mixed currency budget")
        self.assertEqual(0, code, submitted)
        for event_id, amount, currency in [
            ("budget-event-mixed-usd", "1.25", "USD"),
            ("budget-event-mixed-thb", "40", "THB"),
        ]:
            code, budget = run_cli(
                "budget",
                "record",
                "--budget-event-id",
                event_id,
                "--task-id",
                "task-budget-mixed",
                "--employee",
                "codex",
                "--cost-type",
                "model_api",
                "--amount",
                amount,
                "--currency",
                currency,
                "--summary",
                f"mixed currency {currency}",
            )
            self.assertEqual(0, code, budget)

        status, budget_summary = api_gateway.route_get("/v1/budget-summary", {"task_id": ["task-budget-mixed"]})
        self.assertEqual(HTTPStatus.OK, status, budget_summary)
        summary = budget_summary["summary"]
        self.assertEqual("mixed", summary["currency"])
        self.assertEqual({"THB": 40.0, "USD": 1.25}, summary["total_amounts_by_currency"])
        self.assertEqual({"THB": 40.0, "USD": 1.25}, summary["by_currency"])
        self.assertEqual({"codex": {"THB": 40.0, "USD": 1.25}}, summary["by_employee_by_currency"])
        self.assertEqual({"task-budget-mixed": {"THB": 40.0, "USD": 1.25}}, summary["by_task_by_currency"])

    def test_budget_summary_rolls_up_project_costs(self) -> None:
        code, project = run_cli("project", "create", "--project-id", "project-budget-rollup", "--title", "Budget rollup", "--goal", "Track owner-visible project spend", "--owner", "openclaw-main")
        self.assertEqual(0, code, project)
        for task_id, amount, currency in [
            ("task-project-budget-copy", "0.25", "USD"),
            ("task-project-budget-image", "8", "THB"),
        ]:
            code, submitted = run_cli("task", "submit", "--from", "openclaw-main", "--to", "codex", "--task-id", task_id, "--title", f"Project budget {task_id}")
            self.assertEqual(0, code, submitted)
            code, linked = run_cli("project", "link-task", "--project-id", "project-budget-rollup", "--task-id", task_id)
            self.assertEqual(0, code, linked)
            code, budget = run_cli(
                "budget",
                "record",
                "--budget-event-id",
                f"budget-{task_id}",
                "--task-id",
                task_id,
                "--employee",
                "codex",
                "--cost-type",
                "model_api",
                "--amount",
                amount,
                "--currency",
                currency,
                "--token-input",
                "100",
                "--token-output",
                "25",
                "--summary",
                f"project cost {task_id}",
            )
            self.assertEqual(0, code, budget)

        status, budget_summary = api_gateway.route_get("/v1/budget-summary", {})
        self.assertEqual(HTTPStatus.OK, status, budget_summary)
        summary = budget_summary["summary"]
        self.assertEqual({"THB": 8.0, "USD": 0.25}, summary["by_project_by_currency"]["project-budget-rollup"])
        self.assertEqual(2, summary["by_project_event_count"]["project-budget-rollup"])
        self.assertEqual(200, summary["by_project_token_input"]["project-budget-rollup"])
        self.assertEqual(50, summary["by_project_token_output"]["project-budget-rollup"])

        code, cli_summary = run_cli("budget", "summary")
        self.assertEqual(0, code, cli_summary)
        self.assertEqual({"THB": 8.0, "USD": 0.25}, cli_summary["summary"]["by_project_by_currency"]["project-budget-rollup"])

    def test_task_detail_includes_runtime_tool_call_and_budget_ledgers(self) -> None:
        code, project = run_cli("project", "create", "--project-id", "project-task-detail-cost", "--title", "Task Detail Cost", "--goal", "Show task project spend", "--owner", "openclaw-main")
        self.assertEqual(0, code, project)
        code, submitted = run_cli("task", "submit", "--from", "openclaw-main", "--to", "codex", "--task-id", "task-detail-control-plane", "--title", "Task detail control plane")
        self.assertEqual(0, code, submitted)
        code, linked = run_cli("project", "link-task", "--project-id", "project-task-detail-cost", "--task-id", "task-detail-control-plane")
        self.assertEqual(0, code, linked)
        code, running = run_cli("task", "run", "--task-id", "task-detail-control-plane", "--agent", "codex", "--by", "hermes", "--adapter-type", "codex")
        self.assertEqual(0, code, running)
        attempt_id = running["attempt"]["attempt_id"]
        trace_id = running["attempt"]["trace_id"]
        code, session = run_cli(
            "runtime",
            "session",
            "start",
            "--session-id",
            "session-task-detail-control-plane",
            "--task-id",
            "task-detail-control-plane",
            "--attempt-id",
            attempt_id,
            "--employee",
            "codex",
            "--adapter-type",
            "codex",
            "--runtime-type",
            "cli",
        )
        self.assertEqual(0, code, session)
        code, tool = run_cli(
            "tool-call",
            "start",
            "--tool-call-id",
            "tool-task-detail-control-plane",
            "--task-id",
            "task-detail-control-plane",
            "--attempt-id",
            attempt_id,
            "--employee",
            "codex",
            "--session-id",
            "session-task-detail-control-plane",
            "--tool-name",
            "shell",
            "--tool-type",
            "shell",
            "--input-summary",
            "inspect task detail",
        )
        self.assertEqual(0, code, tool)
        code, finished_tool = run_cli(
            "tool-call",
            "finish",
            "--tool-call-id",
            "tool-task-detail-control-plane",
            "--status",
            "success",
            "--output-summary",
            "task detail inspected",
        )
        self.assertEqual(0, code, finished_tool)
        code, budget = run_cli(
            "budget",
            "record",
            "--budget-event-id",
            "budget-task-detail-control-plane",
            "--task-id",
            "task-detail-control-plane",
            "--attempt-id",
            attempt_id,
            "--employee",
            "codex",
            "--cost-type",
            "model_api",
            "--amount",
            "0.25",
            "--currency",
            "USD",
            "--token-input",
            "500",
            "--token-output",
            "120",
            "--runtime-seconds",
            "30",
            "--summary",
            "task detail budget",
        )
        self.assertEqual(0, code, budget)

        code, shown = run_cli("task", "show", "--task-id", "task-detail-control-plane")
        self.assertEqual(0, code, shown)
        self.assertEqual(["session-task-detail-control-plane"], [item["session_id"] for item in shown["runtime_sessions"]])
        self.assertEqual(["tool-task-detail-control-plane"], [item["tool_call_id"] for item in shown["tool_calls"]])
        self.assertEqual(["budget-task-detail-control-plane"], [item["budget_event_id"] for item in shown["budget_events"]])
        self.assertEqual(0.25, shown["budget_summary"]["total_amount"])
        self.assertEqual(500, shown["budget_summary"]["token_input"])
        self.assertEqual(["project-task-detail-cost"], [item["id"] for item in shown["projects"]])
        self.assertEqual({"USD": 0.25}, shown["projects"][0]["budget_by_currency"])
        self.assertEqual(1, shown["projects"][0]["budget_event_count"])
        self.assertEqual(trace_id, shown["runtime_sessions"][0]["trace_id"])

        status, api_payload = api_gateway.route_get("/v1/tasks/task-detail-control-plane", {})
        self.assertEqual(HTTPStatus.OK, status, api_payload)
        self.assertEqual(["tool-task-detail-control-plane"], [item["tool_call_id"] for item in api_payload["tool_calls"]])
        self.assertEqual(0.25, api_payload["budget_summary"]["total_amount"])
        self.assertEqual(["project-task-detail-cost"], [item["id"] for item in api_payload["projects"]])
        self.assertEqual({"USD": 0.25}, api_payload["projects"][0]["budget_by_currency"])

    def test_task_detail_marks_completed_task_without_final_evidence_invalid(self) -> None:
        with sqlite3.connect(self.root / "company.sqlite") as conn:
            conn.execute(
                """
                INSERT INTO tasks(id, source_agent, target_agent, title, description, priority, status, claimed_by, summary, evidence_path, blocker, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "task-completed-missing-final-evidence",
                    "main",
                    "codex",
                    "Missing final evidence",
                    "",
                    "P1",
                    "completed",
                    "codex",
                    "claimed done without final evidence",
                    "",
                    "",
                    companyctl.now(),
                    companyctl.now(),
                ),
            )
            conn.commit()

        status, shown = api_gateway.route_get("/v1/tasks/task-completed-missing-final-evidence", {})
        self.assertEqual(HTTPStatus.OK, status, shown)
        self.assertFalse(shown["completion_contract"]["valid"])
        self.assertEqual("missing_final_evidence", shown["completion_contract"]["reason"])
        self.assertEqual(0, shown["completion_contract"]["final_evidence_count"])
        self.assertIn("final evidence", shown["completion_contract"]["summary"])
        self.assertTrue(shown["completion_invalid"])
        self.assertEqual("missing_final_evidence", shown["completion_invalid_reason"])
        self.assertEqual(0, shown["final_evidence_count"])

    def test_dashboard_task_detail_drawer_renders_control_plane_ledgers(self) -> None:
        template = Path(__file__).resolve().parents[1] / "dashboard_templates" / "gemini_dashboard.html"
        html = template.read_text(encoding="utf-8")
        for snippet in [
            "const runtimeSessions = payload.runtime_sessions || [];",
            "const toolCalls = payload.tool_calls || [];",
            "const budgetSummary = payload.budget_summary || {};",
            "const budgetEvents = payload.budget_events || [];",
            "const completionContract = payload.completion_contract || {};",
            "['Runtime Sessions', runtimeSessionsSummary(runtimeSessions)]",
            "['Tool Calls', toolCallsSummary(toolCalls)]",
            "['Budget Summary', budgetSummaryDetail(budgetSummary)]",
            "['Budget Events', budgetEventsSummary(budgetEvents)]",
            "['Completion Contract', completionContractSummary(completionContract)]",
            "function runtimeSessionsSummary",
            "function toolCallsSummary",
            "const sanitized = item.sanitized === true;",
            "[Raw output redacted for safety]",
            "raw_available=${String(!!item.raw_available)}",
            "function budgetSummaryDetail",
            "budgetLimitStatusSummary",
            "function budgetEventsSummary",
            "function completionContractSummary",
        ]:
            self.assertIn(snippet, html)

    def test_v3_workspace_artifact_handoff_attempt_and_trace_flow(self) -> None:
        for employee_id, role in [("manager", "supervisor"), ("writer", "copywriter"), ("qa", "qa")]:
            code, created = run_cli("employee", "create", "--id", employee_id, "--name", employee_id, "--role", role, "--runtime", "local", "--workspace", str(self.root / "workspace" / employee_id))
            self.assertEqual(code, 0, created)
        with sqlite3.connect(self.root / "company.sqlite") as conn:
            conn.execute("UPDATE employees SET status = 'active' WHERE id IN ('manager', 'writer', 'qa')")
            conn.commit()

        code, submitted = run_cli("task", "submit", "--from", "manager", "--to", "writer", "--task-id", "task-v3-parent", "--title", "商品资料包", "--description", "生成电商上架资料包")
        self.assertEqual(code, 0, submitted)
        trace_id = submitted["task"]["metadata"]["trace_id"]
        workspace = Path(submitted["task"]["workspace"]["path"])
        for child in ["input", "work", "artifacts", "evidence", "final"]:
            self.assertTrue((workspace / child).is_dir(), child)
        self.assertTrue((workspace / "manifest.json").exists())

        artifact_file = workspace / "work" / "title.json"
        artifact_file.write_text('{"title":"A"}\n', encoding="utf-8")
        code, artifact_v1 = run_cli("task", "artifact", "register", "--task-id", "task-v3-parent", "--employee", "writer", "--path", str(artifact_file), "--type", "json", "--name", "title.json", "--stage", "draft", "--summary", "标题草稿")
        self.assertEqual(code, 0, artifact_v1)
        artifact_file.write_text('{"title":"B"}\n', encoding="utf-8")
        code, artifact_v2 = run_cli("task", "artifact", "register", "--task-id", "task-v3-parent", "--employee", "writer", "--path", str(artifact_file), "--type", "json", "--name", "title.json", "--stage", "intermediate", "--summary", "标题第二版")
        self.assertEqual(code, 0, artifact_v2)
        self.assertEqual(2, artifact_v2["artifact"]["version"])
        self.assertEqual("superseded", artifact_v2["superseded"][0]["status"])
        self.assertEqual('{"title":"A"}\n', Path(artifact_v1["artifact"]["path"]).read_text(encoding="utf-8"))
        self.assertEqual('{"title":"B"}\n', Path(artifact_v2["artifact"]["path"]).read_text(encoding="utf-8"))

        code, approved = run_cli("task", "artifact", "approve", "--artifact-id", artifact_v2["artifact"]["artifact_id"], "--by", "manager", "--summary", "可用于质检")
        self.assertEqual(code, 0, approved)
        code, child = run_cli("task", "submit", "--from", "manager", "--to", "qa", "--task-id", "task-v3-qa", "--title", "质检", "--description", "检查标题")
        self.assertEqual(code, 0, child)
        code, handoff = run_cli("task", "handoff", "create", "--from-task", "task-v3-parent", "--to-task", "task-v3-qa", "--from-employee", "writer", "--to-employee", "qa", "--summary", "标题交给质检", "--artifact", artifact_v2["artifact"]["artifact_id"], "--next-steps", "检查标题是否可用")
        self.assertEqual(code, 0, handoff)
        code, context = run_cli("task", "context", "--task-id", "task-v3-qa", "--employee", "qa")
        self.assertEqual(code, 0, context)
        self.assertEqual([artifact_v2["artifact"]["artifact_id"]], [item["artifact_id"] for item in context["context"]["available_artifacts"]])
        self.assertEqual("标题交给质检", context["context"]["handoff_notes"][0]["summary"])
        code, used = run_cli("task", "artifact", "use", "--task-id", "task-v3-qa", "--employee", "qa", "--artifact-id", artifact_v2["artifact"]["artifact_id"], "--summary", "质检读取标题")
        self.assertEqual(code, 0, used)

        code, attempt = run_cli("task", "attempt", "start", "--task-id", "task-v3-qa", "--employee", "qa", "--adapter-type", "local")
        self.assertEqual(code, 0, attempt)
        self.assertEqual(trace_id, attempt["attempt"]["trace_id"])
        code, attempt_done = run_cli("task", "attempt", "finish", "--attempt-id", attempt["attempt"]["attempt_id"], "--status", "success")
        self.assertEqual(code, 0, attempt_done)

        final_file = workspace / "final" / "package.zip"
        final_file.write_text("zip bytes\n", encoding="utf-8")
        code, final_artifact = run_cli("task", "artifact", "register", "--task-id", "task-v3-parent", "--employee", "writer", "--path", str(final_file), "--type", "zip", "--name", "package.zip", "--stage", "final", "--summary", "最终资料包", "--final")
        self.assertEqual(code, 0, final_artifact)
        code, promoted = run_cli("task", "evidence", "promote", "--artifact-id", final_artifact["artifact"]["artifact_id"], "--employee", "writer", "--summary", "最终交付证据")
        self.assertEqual(code, 0, promoted)
        code, done = run_cli("task", "done", "--agent", "writer", "--task-id", "task-v3-parent", "--summary", "已完成", "--evidence", promoted["evidence"]["path_or_url"])
        self.assertEqual(code, 0, done)

        with sqlite3.connect(self.root / "company.sqlite") as conn:
            conn.row_factory = sqlite3.Row
            event_types = [row["event_type"] for row in conn.execute("SELECT event_type FROM company_events WHERE trace_id = ? ORDER BY created_at", (trace_id,))]
            trace = company_trace.load_trace(conn, trace_id)
        self.assertIn("artifact.created", event_types)
        self.assertIn("artifact.superseded", event_types)
        self.assertIn("artifact.used_by_task", event_types)
        self.assertIn("handoff.created", event_types)
        self.assertIn("artifact.promoted_to_evidence", event_types)
        self.assertEqual(3, len(trace["artifacts"]))
        self.assertEqual(1, len(trace["handoffs"]))
        self.assertEqual(1, len(trace["evidence"]))
        self.assertEqual(1, len(trace["execution_attempts"]))

    def test_workspace_prune_dry_run_lists_only_old_terminal_task_workspaces(self) -> None:
        for employee_id, role in [("main", "operator"), ("writer", "writer")]:
            code, created = run_cli("employee", "create", "--id", employee_id, "--name", employee_id, "--role", role, "--runtime", "local", "--workspace", str(self.root / "workspace" / employee_id))
            self.assertEqual(0, code, created)
            if employee_id != "main":
                self.mark_active(employee_id)
        code, old_task = run_cli("task", "submit", "--from", "main", "--to", "writer", "--task-id", "task-prune-old-done", "--title", "Old done")
        self.assertEqual(0, code, old_task)
        code, running_task = run_cli("task", "submit", "--from", "main", "--to", "writer", "--task-id", "task-prune-running", "--title", "Running")
        self.assertEqual(0, code, running_task)
        conn = companyctl.connect()
        try:
            old_workspace = companyctl.ensure_task_workspace(conn, "task-prune-old-done")
            running_workspace = companyctl.ensure_task_workspace(conn, "task-prune-running")
            old_file = Path(old_workspace["path"]) / "work" / "old.txt"
            running_file = Path(running_workspace["path"]) / "work" / "running.txt"
            old_file.write_text("old\n", encoding="utf-8")
            running_file.write_text("running\n", encoding="utf-8")
            old_ts = "2026-01-01T00:00:00+07:00"
            conn.execute("UPDATE tasks SET status = 'done', updated_at = ? WHERE id = ?", (old_ts, "task-prune-old-done"))
            conn.execute("UPDATE task_workspaces SET updated_at = ? WHERE task_id = ?", (old_ts, "task-prune-old-done"))
            conn.commit()
        finally:
            conn.close()

        code, preview = run_cli("workspace", "prune", "--dry-run", "--older-than-days", "30")
        self.assertEqual(0, code, preview)
        self.assertTrue(preview["dry_run"])
        self.assertEqual(["task-prune-old-done"], [item["task_id"] for item in preview["candidates"]])
        self.assertGreater(preview["summary"]["bytes_reclaimable"], 0)
        self.assertTrue(old_file.exists())
        self.assertTrue(running_file.exists())
        status, api_preview = api_gateway.route_get("/v1/workspaces/prune", {"dry_run": ["true"], "older_than_days": ["30"]})
        self.assertEqual(HTTPStatus.OK, status, api_preview)
        self.assertEqual(preview["candidates"], api_preview["candidates"])
        status, rejected = api_gateway.route_get("/v1/workspaces/prune", {"older_than_days": ["30"]})
        self.assertEqual(HTTPStatus.BAD_REQUEST, status, rejected)
        self.assertFalse(rejected["ok"])
        payload_json = json.dumps(api_preview, ensure_ascii=False)
        self.assertNotIn("task-prune-running", payload_json)
        self.assertNotIn(str(self.root / "workspace" / "writer"), payload_json)

    def test_dashboard_trace_counts_include_v3_file_flow(self) -> None:
        for employee_id in ["manager", "writer", "qa"]:
            code, created = run_cli("employee", "create", "--id", employee_id, "--name", employee_id, "--role", "agent", "--runtime", "local", "--workspace", str(self.root / "workspace" / employee_id))
            self.assertEqual(code, 0, created)
        with sqlite3.connect(self.root / "company.sqlite") as conn:
            conn.execute("UPDATE employees SET status = 'active' WHERE id IN ('manager', 'writer', 'qa')")
            conn.commit()

        code, submitted = run_cli("task", "submit", "--from", "manager", "--to", "writer", "--task-id", "task-v3-dashboard", "--title", "Dashboard v3")
        self.assertEqual(code, 0, submitted)
        workspace = Path(submitted["task"]["workspace"]["path"])
        artifact_file = workspace / "work" / "brief.md"
        artifact_file.write_text("brief\n", encoding="utf-8")
        code, artifact = run_cli("task", "artifact", "register", "--task-id", "task-v3-dashboard", "--employee", "writer", "--path", str(artifact_file), "--type", "markdown", "--summary", "brief")
        self.assertEqual(code, 0, artifact)
        code, child = run_cli("task", "submit", "--from", "manager", "--to", "qa", "--task-id", "task-v3-dashboard-qa", "--title", "QA")
        self.assertEqual(code, 0, child)
        code, handoff = run_cli("task", "handoff", "create", "--from-task", "task-v3-dashboard", "--to-task", "task-v3-dashboard-qa", "--from-employee", "writer", "--to-employee", "qa", "--summary", "dashboard handoff", "--artifact", artifact["artifact"]["artifact_id"])
        self.assertEqual(code, 0, handoff)
        code, attempt = run_cli("task", "attempt", "start", "--task-id", "task-v3-dashboard-qa", "--employee", "qa")
        self.assertEqual(code, 0, attempt)
        code, evidence = run_cli("task", "evidence", "promote", "--artifact-id", artifact["artifact"]["artifact_id"], "--employee", "writer", "--summary", "dashboard evidence")
        self.assertEqual(code, 0, evidence)

        conn = companyctl.connect()
        try:
            summary = company_dashboard.load_summary(conn)
        finally:
            conn.close()
        trace = next(item for item in summary["traces"] if item["trace_id"] == submitted["task"]["metadata"]["trace_id"])
        self.assertEqual(1, trace["counts"]["artifacts"])
        self.assertEqual(1, trace["counts"]["handoffs"])
        self.assertEqual(1, trace["counts"]["evidence"])
        self.assertEqual(1, trace["counts"]["execution_attempts"])
        evidence_span = next(span for span in trace["spans"] if span["name"].startswith("evidence."))
        self.assertIn("attempt_id", evidence_span)

        status, graph = api_gateway.route_get(f"/v1/traces/{submitted['task']['metadata']['trace_id']}/file-flow", {})
        self.assertEqual(HTTPStatus.OK, status, graph)
        self.assertEqual(submitted["task"]["metadata"]["trace_id"], graph["trace_id"])
        self.assertEqual("trace_file_flow", graph["kind"])
        self.assertIn("task:task-v3-dashboard", [node["id"] for node in graph["nodes"]])
        self.assertIn(f"artifact:{artifact['artifact']['artifact_id']}", [node["id"] for node in graph["nodes"]])
        self.assertIn(f"handoff:{handoff['handoff']['handoff_id']}", [node["id"] for node in graph["nodes"]])
        self.assertIn(f"evidence:{evidence['evidence']['evidence_id']}", [node["id"] for node in graph["nodes"]])
        edge_labels = [edge["label"] for edge in graph["edges"]]
        self.assertIn("created artifact", edge_labels)
        self.assertIn("handoff", edge_labels)
        self.assertIn("promoted evidence", edge_labels)
        self.assertIn("graph LR", graph["mermaid"])

        output = self.root / "state" / "file-flow-dashboard.html"
        with contextlib.redirect_stdout(io.StringIO()):
            code = company_dashboard.main(["--output", str(output), "--variant", "advanced"])
        self.assertEqual(0, code)
        html = output.read_text(encoding="utf-8")
        self.assertIn("File Flow Graph", html)
        self.assertIn("trace-file-flow-container", html)
        self.assertIn("/v1/traces/${encodeURIComponent(traceId)}/file-flow", html)
        self.assertIn("Handoff Artifact Flow", html)
        self.assertIn("fileFlowNarrative", html)
        self.assertIn("created artifact -> handoff -> promoted evidence", html)
        self.assertNotIn(submitted["task"]["metadata"]["trace_id"], html)

    def test_dashboard_trace_spans_highlight_supervisor_corrections(self) -> None:
        for employee_id, role in [("main", "operator"), ("hermes", "supervisor"), ("codex", "developer")]:
            code, created = run_cli("employee", "create", "--id", employee_id, "--name", employee_id, "--role", role, "--runtime", "local", "--workspace", str(self.root / "workspace" / employee_id))
            self.assertEqual(code, 0, created)
        self.mark_active("codex")
        code, submitted = run_cli("task", "submit", "--from", "main", "--to", "codex", "--task-id", "task-trace-correction", "--title", "Trace correction")
        self.assertEqual(0, code, submitted)
        code, running = run_cli("task", "run", "--task-id", "task-trace-correction", "--agent", "codex", "--by", "hermes")
        self.assertEqual(0, code, running)
        attempt_id = running["attempt"]["attempt_id"]
        code, corrected = run_cli("task", "correct", "--task-id", "task-trace-correction", "--attempt-id", attempt_id, "--by", "hermes", "--message", "请只交付 evidence，不要继续闲聊")
        self.assertEqual(0, code, corrected)
        code, acked = run_cli("task", "correct", "--task-id", "task-trace-correction", "--attempt-id", attempt_id, "--by", "codex", "--message", "收到纠偏", "--ack")
        self.assertEqual(0, code, acked)

        conn = companyctl.connect()
        try:
            summary = company_dashboard.load_summary(conn)
        finally:
            conn.close()
        trace = next(item for item in summary["traces"] if item["trace_id"] == submitted["task"]["metadata"]["trace_id"])
        correction_spans = [span for span in trace["spans"] if span["name"].startswith("supervisor.correction.")]
        self.assertEqual(["supervisor.correction.requested", "supervisor.correction.acknowledged"], [span["name"] for span in correction_spans])
        self.assertEqual(["hermes", "codex"], [span["service"] for span in correction_spans])
        self.assertTrue(all(span["attempt_id"] == attempt_id for span in correction_spans))
        self.assertTrue(all("correction_direction" in span for span in correction_spans))
        self.assertEqual(["supervisor_to_worker", "worker_to_supervisor"], [span["correction_direction"] for span in correction_spans])
        output = self.root / "state" / "trace-correction-dashboard.html"
        with contextlib.redirect_stdout(io.StringIO()):
            code = company_dashboard.main(["--output", str(output), "--variant", "advanced"])
        self.assertEqual(0, code)
        html = output.read_text(encoding="utf-8")
        self.assertIn("traceCorrectionSummary", html)
        self.assertIn("Supervisor Corrections", html)
        self.assertIn("span.correction_direction", html)
        self.assertNotIn("请只交付 evidence，不要继续闲聊", html)

    def test_v3_handoff_reject_scan_and_recovery_attempts(self) -> None:
        for employee_id, role in [("manager", "supervisor"), ("writer", "copywriter"), ("qa", "qa"), ("backup", "copywriter")]:
            code, created = run_cli("employee", "create", "--id", employee_id, "--name", employee_id, "--role", role, "--runtime", "local", "--workspace", str(self.root / "workspace" / employee_id))
            self.assertEqual(code, 0, created)
        with sqlite3.connect(self.root / "company.sqlite") as conn:
            conn.execute("UPDATE employees SET status = 'active' WHERE id IN ('manager', 'writer', 'qa', 'backup')")
            conn.commit()

        code, submitted = run_cli("task", "submit", "--from", "manager", "--to", "writer", "--task-id", "task-v3-recovery", "--title", "资料包", "--description", "验证自动登记和恢复")
        self.assertEqual(code, 0, submitted)
        trace_id = submitted["task"]["metadata"]["trace_id"]
        workspace = Path(submitted["task"]["workspace"]["path"])
        draft = workspace / "work" / "draft.md"
        draft.write_text("draft one\n", encoding="utf-8")
        code, scanned = run_cli("task", "artifact", "scan", "--task-id", "task-v3-recovery", "--employee", "writer", "--dir", str(workspace / "work"), "--type", "markdown", "--stage", "draft", "--summary", "自动登记过程文件")
        self.assertEqual(code, 0, scanned)
        self.assertEqual(1, len(scanned["artifacts"]))
        first_artifact_id = scanned["artifacts"][0]["artifact"]["artifact_id"]
        draft.write_text("draft two\n", encoding="utf-8")
        code, rescanned = run_cli("task", "artifact", "scan", "--task-id", "task-v3-recovery", "--employee", "writer", "--dir", str(workspace / "work"), "--type", "markdown", "--stage", "intermediate", "--summary", "自动登记更新")
        self.assertEqual(code, 0, rescanned)
        second_artifact = rescanned["artifacts"][0]["artifact"]
        self.assertEqual(2, second_artifact["version"])
        self.assertNotEqual(first_artifact_id, second_artifact["artifact_id"])
        code, approved = run_cli("task", "artifact", "approve", "--artifact-id", second_artifact["artifact_id"], "--by", "manager")
        self.assertEqual(code, 0, approved)

        code, child = run_cli("task", "submit", "--from", "manager", "--to", "qa", "--task-id", "task-v3-recovery-qa", "--title", "质检", "--description", "验证拒绝交接")
        self.assertEqual(code, 0, child)
        code, handoff = run_cli("task", "handoff", "create", "--from-task", "task-v3-recovery", "--to-task", "task-v3-recovery-qa", "--from-employee", "writer", "--to-employee", "qa", "--summary", "交给质检", "--artifact", second_artifact["artifact_id"])
        self.assertEqual(code, 0, handoff)
        code, rejected = run_cli("task", "handoff", "reject", "--handoff-id", handoff["handoff"]["handoff_id"], "--by", "qa", "--reason", "缺少验收说明")
        self.assertEqual(code, 0, rejected)
        self.assertEqual("blocked", rejected["from_task"]["status"])

        code, retry = run_cli("task", "retry", "--task-id", "task-v3-recovery", "--by", "manager", "--reason", "补交接说明")
        self.assertEqual(code, 0, retry)
        code, reassigned = run_cli("task", "reassign", "--task-id", "task-v3-recovery", "--by", "manager", "--to", "backup", "--reason", "换员工补齐")
        self.assertEqual(code, 0, reassigned)
        self.assertEqual("backup", reassigned["task"]["target_agent"])

        code, bad_done = run_cli("task", "done", "--agent", "backup", "--task-id", "task-v3-recovery", "--summary", "不能直接完成", "--evidence", str(draft))
        self.assertEqual(code, 2, bad_done)
        self.assertIn("promoted final evidence", bad_done["error"])

        final_file = workspace / "final" / "final.md"
        final_file.write_text("final\n", encoding="utf-8")
        code, final_artifact = run_cli("task", "artifact", "register", "--task-id", "task-v3-recovery", "--employee", "backup", "--path", str(final_file), "--type", "markdown", "--stage", "final", "--summary", "最终文件", "--final")
        self.assertEqual(code, 0, final_artifact)
        code, promoted = run_cli("task", "evidence", "promote", "--artifact-id", final_artifact["artifact"]["artifact_id"], "--employee", "backup", "--summary", "最终证据")
        self.assertEqual(code, 0, promoted)
        code, done = run_cli("task", "done", "--agent", "backup", "--task-id", "task-v3-recovery", "--summary", "已完成", "--evidence", promoted["evidence"]["path_or_url"])
        self.assertEqual(code, 0, done)

        with sqlite3.connect(self.root / "company.sqlite") as conn:
            conn.row_factory = sqlite3.Row
            event_types = [row["event_type"] for row in conn.execute("SELECT event_type FROM company_events WHERE trace_id = ? ORDER BY created_at", (trace_id,))]
            attempts = [dict(row) for row in conn.execute("SELECT * FROM execution_attempts WHERE trace_id = ? ORDER BY started_at", (trace_id,))]
            done_payload = conn.execute("SELECT payload_json FROM company_events WHERE trace_id = ? AND event_type = 'task.done' ORDER BY created_at DESC LIMIT 1", (trace_id,)).fetchone()["payload_json"]
        self.assertIn("artifact.updated", event_types)
        self.assertIn("handoff.rejected", event_types)
        self.assertIn("task.retrying", event_types)
        self.assertTrue(any(item["adapter_type"] == "retry" and item["status"] == "starting" for item in attempts))
        backup_attempt = next(item for item in attempts if item["employee_id"] == "backup" and item["adapter_type"] == "reassign")
        self.assertEqual("success", backup_attempt["status"])
        self.assertEqual(backup_attempt["attempt_id"], json.loads(done_payload)["attempt_id"])

    def test_managed_attempt_policy_correction_stale_and_cancel(self) -> None:
        for employee_id, role in [("main", "operator"), ("hermes", "supervisor"), ("codex", "developer")]:
            code, created = run_cli("employee", "create", "--id", employee_id, "--name", employee_id, "--role", role, "--runtime", "local", "--workspace", str(self.root / "workspace" / employee_id))
            self.assertEqual(code, 0, created)
        self.mark_active("codex")
        code, submitted = run_cli("task", "submit", "--from", "main", "--to", "codex", "--task-id", "task-long-managed", "--title", "Long managed task")
        self.assertEqual(0, code, submitted)
        code, run = run_cli(
            "task",
            "run",
            "--task-id",
            "task-long-managed",
            "--agent",
            "codex",
            "--by",
            "hermes",
            "--max-runtime-seconds",
            "36000",
            "--heartbeat-interval-seconds",
            "60",
            "--progress-interval-seconds",
            "300",
            "--stale-after-seconds",
            "1",
            "--supervisor-check-interval-seconds",
            "60",
            "--max-corrections",
            "2",
            "--max-retries",
            "1",
            "--session-key",
            "agent:codex:hermes",
            "--pid",
            "12345",
        )
        self.assertEqual(0, code, run)
        attempt_id = run["attempt"]["attempt_id"]
        self.assertEqual("starting", run["attempt"]["status"])
        self.assertEqual("12345", run["attempt"]["pid"])
        self.assertEqual(36000, run["runtime_policy"]["max_runtime_seconds"])
        code, corrected = run_cli("task", "correct", "--task-id", "task-long-managed", "--attempt-id", attempt_id, "--by", "hermes", "--message", "请回到 README 总结任务")
        self.assertEqual(0, code, corrected)
        self.assertEqual("correcting", corrected["attempt"]["status"])
        code, acked = run_cli("task", "correct", "--task-id", "task-long-managed", "--attempt-id", attempt_id, "--by", "codex", "--ack", "--message", "已收到纠偏")
        self.assertEqual(0, code, acked)
        self.assertEqual("running", acked["attempt"]["status"])
        self.assertEqual(1, acked["supervisor_state"]["corrections_acknowledged"])
        stale_now = (datetime.fromisoformat(acked["attempt"]["last_progress_at"]) + timedelta(seconds=2)).isoformat(timespec="seconds")
        code, stale = run_cli("supervisor", "scan-attempts", "--by", "hermes", "--now", stale_now)
        self.assertEqual(0, code, stale)
        item = next(item for item in stale["attempts"] if item["attempt_id"] == attempt_id)
        self.assertEqual("stale", item["status"])
        self.assertEqual("stale", item["task_status"])
        code, cancelled = run_cli("task", "cancel", "--task-id", "task-long-managed", "--attempt-id", attempt_id, "--by", "hermes", "--reason", "用户停止")
        self.assertEqual(0, code, cancelled)
        self.assertEqual("cancelled", cancelled["attempt"]["status"])
        code, late_success = run_cli("task", "attempt", "finish", "--attempt-id", attempt_id, "--status", "success")
        self.assertEqual(2, code, late_success)
        self.assertIn("terminal", late_success["error"])
        code, shown = run_cli("task", "show", "--task-id", "task-long-managed")
        self.assertEqual(0, code, shown)
        self.assertEqual("cancelled", shown["task"]["status"])
        self.assertEqual(attempt_id, shown["attempts"][0]["attempt_id"])
        self.assertEqual("cancelled", shown["attempts"][0]["status"])
        self.assertEqual(1, shown["supervisor_state"]["corrections_requested"])
        self.assertEqual(1, shown["supervisor_state"]["corrections_acknowledged"])
        self.assertEqual("cancelled", shown["correction_summary"]["latest_attempt_status"])
        self.assertEqual(attempt_id, shown["correction_summary"]["latest_attempt_id"])
        self.assertEqual(
            ["supervisor.correction_requested", "supervisor.correction_acknowledged"],
            [item["event_type"] for item in shown["correction_events"]],
        )
        self.assertEqual(["hermes", "codex"], [item["source_agent"] for item in shown["correction_events"]])
        self.assertTrue(all(item["attempt_id"] == attempt_id for item in shown["correction_events"]))
        self.assertEqual(["请回到 README 总结任务", "已收到纠偏"], [item["message"] for item in shown["correction_events"]])

        status, api_shown = api_gateway.route_get("/v1/tasks/task-long-managed", {})
        self.assertEqual(200, status, api_shown)
        self.assertEqual(shown["supervisor_state"], api_shown["supervisor_state"])
        self.assertEqual(shown["correction_summary"], api_shown["correction_summary"])
        self.assertEqual(shown["correction_events"], api_shown["correction_events"])

        code, trace = run_cli("trace", "timeline", "--task-id", "task-long-managed")
        self.assertEqual(0, code, trace)
        cancel_item = next(item for item in trace["timeline"] if item["kind"] == "event" and item["label"] == "supervisor.cancel_requested")
        self.assertEqual(attempt_id, cancel_item["attempt_id"])
        self.assertEqual("用户停止", cancel_item["reason"])
        ceo_cancel_item = next(item for item in trace["ceo_timeline"] if item["stage"] == "supervisor.cancel_requested")
        self.assertEqual(attempt_id, ceo_cancel_item["attempt_id"])
        self.assertIn("用户停止", ceo_cancel_item["summary"])

    def test_task_reassign_writes_event_attempt_audit_and_trace(self) -> None:
        for employee_id, role in [("main", "operator"), ("hermes", "supervisor"), ("codex", "developer"), ("backup", "developer")]:
            code, created = run_cli("employee", "create", "--id", employee_id, "--name", employee_id, "--role", role, "--runtime", "local", "--workspace", str(self.root / "workspace" / employee_id))
            self.assertEqual(code, 0, created)
            self.mark_active(employee_id)
        code, submitted = run_cli("task", "submit", "--from", "main", "--to", "codex", "--task-id", "task-reassign-ledger", "--title", "Reassign ledger task")
        self.assertEqual(0, code, submitted)
        with companyctl.connect() as conn:
            trace_id = companyctl.trace_id_for_task(conn, "task-reassign-ledger")
        code, run = run_cli("task", "run", "--task-id", "task-reassign-ledger", "--agent", "codex", "--by", "hermes")
        self.assertEqual(0, code, run)

        code, reassigned = run_cli("task", "reassign", "--task-id", "task-reassign-ledger", "--by", "hermes", "--to", "backup", "--reason", "codex is blocked")
        self.assertEqual(0, code, reassigned)
        self.assertEqual("backup", reassigned["task"]["target_agent"])
        self.assertEqual("submitted", reassigned["task"]["status"])
        self.assertEqual(trace_id, reassigned["attempt"]["trace_id"])
        self.assertEqual("reassign", reassigned["attempt"]["adapter_type"])
        self.assertEqual("backup", reassigned["attempt"]["employee_id"])
        self.assertEqual("codex is blocked", reassigned["attempt"]["metadata"]["reason"])
        self.assertEqual("hermes", reassigned["attempt"]["metadata"]["by"])
        self.assertEqual(run["attempt"]["attempt_id"], reassigned["attempt"]["metadata"]["previous_attempt_id"])
        self.assertIn("event_id", reassigned)

        with companyctl.connect() as conn:
            event = conn.execute("SELECT * FROM company_events WHERE id = ?", (reassigned["event_id"],)).fetchone()
            audit_row = conn.execute("SELECT * FROM audit_logs WHERE action = 'task.reassign' ORDER BY created_at DESC LIMIT 1").fetchone()
        self.assertIsNotNone(event)
        self.assertEqual("task.reassigned", event["event_type"])
        self.assertEqual("hermes", event["source_agent"])
        self.assertEqual("task-reassign-ledger", event["task_id"])
        self.assertEqual(trace_id, event["trace_id"])
        event_payload = json.loads(event["payload_json"])
        self.assertEqual("codex", event_payload["from"])
        self.assertEqual("backup", event_payload["to"])
        self.assertEqual("codex is blocked", event_payload["reason"])
        self.assertEqual(reassigned["attempt"]["attempt_id"], event_payload["attempt_id"])
        self.assertEqual(run["attempt"]["attempt_id"], event_payload["previous_attempt_id"])
        self.assertIsNotNone(audit_row)
        self.assertEqual("hermes", audit_row["actor"])
        self.assertEqual("task-reassign-ledger", audit_row["target"])
        audit_payload = json.loads(audit_row["detail_json"])
        self.assertEqual(reassigned["event_id"], audit_payload["event_id"])
        self.assertEqual(reassigned["attempt"]["attempt_id"], audit_payload["attempt_id"])

        code, trace = run_cli("trace", "timeline", "--trace-id", trace_id)
        self.assertEqual(0, code, trace)
        trace_event = next(item for item in trace["timeline"] if item["kind"] == "event" and item["label"] == "task.reassigned" and item["task_id"] == "task-reassign-ledger")
        self.assertEqual(reassigned["attempt"]["attempt_id"], trace_event["attempt_id"])
        self.assertEqual(run["attempt"]["attempt_id"], trace_event["previous_attempt_id"])
        self.assertEqual("codex", trace_event["from_employee"])
        self.assertEqual("backup", trace_event["to_employee"])
        self.assertEqual("codex is blocked", trace_event["reason"])
        ceo_event = next(item for item in trace["ceo_timeline"] if item["stage"] == "task.reassigned")
        self.assertEqual(reassigned["attempt"]["attempt_id"], ceo_event["attempt_id"])
        self.assertEqual(run["attempt"]["attempt_id"], ceo_event["previous_attempt_id"])
        self.assertIn("codex -> backup", ceo_event["summary"])

    def test_managed_attempt_done_requires_promoted_final_evidence(self) -> None:
        for employee_id, role in [("main", "operator"), ("codex", "developer")]:
            code, created = run_cli("employee", "create", "--id", employee_id, "--name", employee_id, "--role", role, "--runtime", "local", "--workspace", str(self.root / "workspace" / employee_id))
            self.assertEqual(0, code, created)
            self.mark_active(employee_id)
        code, submitted = run_cli("task", "submit", "--from", "main", "--to", "codex", "--task-id", "task-managed-evidence", "--title", "Managed evidence gate")
        self.assertEqual(0, code, submitted)
        code, run = run_cli("task", "run", "--task-id", "task-managed-evidence", "--agent", "codex", "--by", "main")
        self.assertEqual(0, code, run)
        with companyctl.connect() as conn:
            workspace = companyctl.ensure_task_workspace(conn, "task-managed-evidence")
            draft = Path(workspace["path"]) / "work" / "draft.md"
            draft.write_text("draft only\n", encoding="utf-8")

        code, rejected = run_cli("task", "done", "--agent", "codex", "--task-id", "task-managed-evidence", "--summary", "draft done", "--evidence", str(draft))
        self.assertEqual(2, code, rejected)
        self.assertIn("promoted final evidence", rejected["error"])
        with companyctl.connect() as conn:
            task = conn.execute("SELECT status, evidence_path FROM tasks WHERE id = 'task-managed-evidence'").fetchone()
            attempt = conn.execute("SELECT status FROM execution_attempts WHERE attempt_id = ?", (run["attempt"]["attempt_id"],)).fetchone()
        self.assertEqual("claimed", task["status"])
        self.assertEqual("", task["evidence_path"])
        self.assertEqual("starting", attempt["status"])

    def test_managed_attempt_progress_refresh_prevents_stale_until_progress_stops(self) -> None:
        for employee_id, role in [("main", "operator"), ("hermes", "supervisor"), ("codex", "developer")]:
            code, created = run_cli("employee", "create", "--id", employee_id, "--name", employee_id, "--role", role, "--runtime", "local", "--workspace", str(self.root / "workspace" / employee_id))
            self.assertEqual(code, 0, created)
        self.mark_active("codex")
        code, submitted = run_cli("task", "submit", "--from", "main", "--to", "codex", "--task-id", "task-progress-managed", "--title", "Managed progress task")
        self.assertEqual(0, code, submitted)
        code, run = run_cli("task", "run", "--task-id", "task-progress-managed", "--agent", "codex", "--by", "hermes", "--stale-after-seconds", "5")
        self.assertEqual(0, code, run)
        attempt_id = run["attempt"]["attempt_id"]
        progress_at = (datetime.fromisoformat(run["attempt"]["last_progress_at"]) + timedelta(seconds=4)).isoformat(timespec="seconds")
        code, progress = run_cli(
            "task",
            "progress",
            "--task-id",
            "task-progress-managed",
            "--agent",
            "codex",
            "--attempt-id",
            attempt_id,
            "--state",
            "in_progress",
            "--message",
            "已完成第一段读取",
            "--progress",
            "25",
            "--payload",
            '{"step":"readme"}',
            "--at",
            progress_at,
        )
        self.assertEqual(0, code, progress)
        self.assertEqual("running", progress["attempt"]["status"])
        self.assertEqual(progress_at, progress["attempt"]["last_progress_at"])
        self.assertEqual("working", progress["progress"]["progress_layer"])
        safe_scan_at = (datetime.fromisoformat(progress_at) + timedelta(seconds=4)).isoformat(timespec="seconds")
        code, safe_scan = run_cli("supervisor", "scan-attempts", "--by", "hermes", "--now", safe_scan_at)
        self.assertEqual(0, code, safe_scan)
        safe_item = next(item for item in safe_scan["attempts"] if item["attempt_id"] == attempt_id)
        self.assertEqual("running", safe_item["status"])
        stale_scan_at = (datetime.fromisoformat(progress_at) + timedelta(seconds=6)).isoformat(timespec="seconds")
        code, stale_scan = run_cli("supervisor", "scan-attempts", "--by", "hermes", "--now", stale_scan_at)
        self.assertEqual(0, code, stale_scan)
        stale_item = next(item for item in stale_scan["attempts"] if item["attempt_id"] == attempt_id)
        self.assertEqual("stale", stale_item["status"])
        with sqlite3.connect(self.root / "company.sqlite") as conn:
            conn.row_factory = sqlite3.Row
            event = conn.execute("SELECT * FROM company_events WHERE event_type = 'task.progress' AND task_id = ?", ("task-progress-managed",)).fetchone()
            self.assertIsNotNone(event)
            payload = json.loads(event["payload_json"])
        self.assertEqual(attempt_id, payload["attempt_id"])
        self.assertEqual("in_progress", payload["progress_state"])
        self.assertEqual(25, payload["progress"])
        code, shown = run_cli("task", "show", "--task-id", "task-progress-managed")
        self.assertEqual(0, code, shown)
        self.assertEqual(attempt_id, shown["progress_events"][0]["attempt_id"])
        self.assertEqual("in_progress", shown["progress_events"][0]["progress_state"])
        self.assertEqual(25, shown["progress_events"][0]["progress"])
        self.assertEqual("已完成第一段读取", shown["progress_events"][0]["message"])
        status, api_shown = api_gateway.route_get("/v1/tasks/task-progress-managed", {})
        self.assertEqual(200, status, api_shown)
        self.assertEqual(shown["progress_events"], api_shown["progress_events"])

    def test_retry_after_stale_starts_new_managed_attempt_with_same_trace(self) -> None:
        for employee_id, role in [("main", "operator"), ("hermes", "supervisor"), ("codex", "developer")]:
            code, created = run_cli("employee", "create", "--id", employee_id, "--name", employee_id, "--role", role, "--runtime", "local", "--workspace", str(self.root / "workspace" / employee_id))
            self.assertEqual(code, 0, created)
        self.mark_active("codex")
        code, submitted = run_cli("task", "submit", "--from", "main", "--to", "codex", "--task-id", "task-retry-managed", "--title", "Retry managed task")
        self.assertEqual(0, code, submitted)
        code, run = run_cli(
            "task",
            "run",
            "--task-id",
            "task-retry-managed",
            "--agent",
            "codex",
            "--by",
            "hermes",
            "--max-runtime-seconds",
            "7200",
            "--stale-after-seconds",
            "1",
            "--max-retries",
            "2",
        )
        self.assertEqual(0, code, run)
        original_attempt_id = run["attempt"]["attempt_id"]
        trace_id = run["attempt"]["trace_id"]
        stale_scan_at = (datetime.fromisoformat(run["attempt"]["last_progress_at"]) + timedelta(seconds=2)).isoformat(timespec="seconds")
        code, stale_scan = run_cli("supervisor", "scan-attempts", "--by", "hermes", "--now", stale_scan_at)
        self.assertEqual(0, code, stale_scan)
        stale_item = next(item for item in stale_scan["attempts"] if item["attempt_id"] == original_attempt_id)
        self.assertEqual("stale", stale_item["status"])

        code, retry = run_cli("task", "retry", "--task-id", "task-retry-managed", "--by", "hermes", "--reason", "resume after stale")
        self.assertEqual(0, code, retry)
        retry_attempt = retry["attempt"]
        self.assertNotEqual(original_attempt_id, retry_attempt["attempt_id"])
        self.assertEqual(trace_id, retry_attempt["trace_id"])
        self.assertEqual("starting", retry_attempt["status"])
        self.assertEqual(7200, retry_attempt["runtime_policy"]["max_runtime_seconds"])
        self.assertEqual(2, retry_attempt["runtime_policy"]["max_retries"])
        self.assertEqual(original_attempt_id, retry_attempt["metadata"]["previous_attempt_id"])
        self.assertEqual("claimed", retry["task"]["status"])
        self.assertEqual("codex", retry["task"]["claimed_by"])

        code, shown = run_cli("task", "show", "--task-id", "task-retry-managed")
        self.assertEqual(0, code, shown)
        attempts = shown["attempts"]
        self.assertTrue(any(item["attempt_id"] == original_attempt_id and item["status"] == "stale" for item in attempts))
        self.assertTrue(any(item["attempt_id"] == retry_attempt["attempt_id"] and item["status"] == "starting" for item in attempts))
        self.assertEqual(2, shown["attempt_history"]["total"])
        self.assertEqual(retry_attempt["attempt_id"], shown["attempt_history"]["latest_attempt_id"])
        self.assertEqual(trace_id, shown["attempt_history"]["trace_id"])
        self.assertEqual([original_attempt_id, retry_attempt["attempt_id"]], [item["attempt_id"] for item in shown["attempt_history"]["chain"]])
        self.assertEqual("", shown["attempt_history"]["chain"][0]["previous_attempt_id"])
        self.assertEqual(original_attempt_id, shown["attempt_history"]["chain"][1]["previous_attempt_id"])
        self.assertEqual("resume after stale", shown["attempt_history"]["chain"][1]["reason"])
        self.assertIn("old attempts retained", shown["attempt_history"]["recovery_summary"])
        status, api_shown = api_gateway.route_get("/v1/tasks/task-retry-managed", {})
        self.assertEqual(200, status, api_shown)
        self.assertEqual(shown["attempt_history"], api_shown["attempt_history"])
        event = next(item for item in shown["events"] if item["event_type"] == "task.retrying")
        payload = event["payload_json"] if isinstance(event["payload_json"], dict) else json.loads(event["payload_json"])
        self.assertEqual(retry_attempt["attempt_id"], payload["attempt_id"])
        self.assertEqual(original_attempt_id, payload["previous_attempt_id"])
        conn = companyctl.connect()
        try:
            summary = company_dashboard.load_summary(conn)
        finally:
            conn.close()
        trace = next(item for item in summary["traces"] if item["trace_id"] == trace_id)
        retry_span = next(span for span in trace["spans"] if span.get("attempt_id") == retry_attempt["attempt_id"])
        self.assertEqual(original_attempt_id, retry_span["previous_attempt_id"])
        self.assertEqual([original_attempt_id, retry_attempt["attempt_id"]], retry_span["attempt_chain"])
        self.assertEqual("retry", retry_span["adapter_type"])
        code, trace_payload = run_cli("trace", "timeline", "--trace-id", trace_id)
        self.assertEqual(0, code, trace_payload)
        retry_item = next(item for item in trace_payload["timeline"] if item["kind"] == "event" and item["label"] == "task.retrying")
        self.assertEqual(retry_attempt["attempt_id"], retry_item["attempt_id"])
        self.assertEqual(original_attempt_id, retry_item["previous_attempt_id"])
        self.assertEqual("resume after stale", retry_item["reason"])
        ceo_retry_item = next(item for item in trace_payload["ceo_timeline"] if item["stage"] == "task.retrying")
        self.assertEqual(retry_attempt["attempt_id"], ceo_retry_item["attempt_id"])
        self.assertEqual(original_attempt_id, ceo_retry_item["previous_attempt_id"])
        self.assertIn("resume after stale", ceo_retry_item["summary"])

    def test_supervisor_scan_warns_on_missing_heartbeat_without_marking_progress_stale(self) -> None:
        for employee_id, role in [("main", "operator"), ("hermes", "supervisor"), ("codex", "developer")]:
            code, created = run_cli("employee", "create", "--id", employee_id, "--name", employee_id, "--role", role, "--runtime", "local", "--workspace", str(self.root / "workspace" / employee_id))
            self.assertEqual(code, 0, created)
        self.mark_active("codex")
        code, submitted = run_cli("task", "submit", "--from", "main", "--to", "codex", "--task-id", "task-heartbeat-warning", "--title", "Heartbeat warning task")
        self.assertEqual(0, code, submitted)
        code, run = run_cli(
            "task",
            "run",
            "--task-id",
            "task-heartbeat-warning",
            "--agent",
            "codex",
            "--by",
            "hermes",
            "--heartbeat-interval-seconds",
            "5",
            "--stale-after-seconds",
            "60",
        )
        self.assertEqual(0, code, run)
        attempt_id = run["attempt"]["attempt_id"]
        base = datetime.fromisoformat(run["attempt"]["started_at"])
        stale_heartbeat_at = (base - timedelta(seconds=20)).isoformat(timespec="seconds")
        fresh_progress_at = (base + timedelta(seconds=1)).isoformat(timespec="seconds")
        with sqlite3.connect(self.root / "company.sqlite") as conn:
            conn.execute(
                "UPDATE execution_attempts SET status = 'running', last_heartbeat_at = ?, last_progress_at = ? WHERE attempt_id = ?",
                (stale_heartbeat_at, fresh_progress_at, attempt_id),
            )
            conn.commit()
        scan_at = (base + timedelta(seconds=2)).isoformat(timespec="seconds")
        code, scanned = run_cli("supervisor", "scan-attempts", "--by", "hermes", "--now", scan_at)
        self.assertEqual(0, code, scanned)
        item = next(item for item in scanned["attempts"] if item["attempt_id"] == attempt_id)
        self.assertEqual("running", item["status"])
        self.assertEqual("heartbeat_warning", item["heartbeat_status"])
        self.assertEqual(22, item["heartbeat_age_seconds"])
        self.assertEqual("", item["task_status"])
        with sqlite3.connect(self.root / "company.sqlite") as conn:
            conn.row_factory = sqlite3.Row
            warning = conn.execute("SELECT * FROM company_events WHERE task_id = 'task-heartbeat-warning' AND event_type = 'employee.warning'").fetchone()
            attempt = conn.execute("SELECT * FROM execution_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
            task = conn.execute("SELECT * FROM tasks WHERE id = 'task-heartbeat-warning'").fetchone()
        self.assertIsNotNone(warning)
        payload = json.loads(warning["payload_json"])
        self.assertEqual("codex", payload["employee_id"])
        self.assertEqual(attempt_id, payload["attempt_id"])
        self.assertEqual("heartbeat_stale", payload["reason"])
        self.assertEqual("running", attempt["status"])
        self.assertEqual("claimed", task["status"])

    def test_correction_limit_blocks_attempt_instead_of_looping_forever(self) -> None:
        for employee_id, role in [("main", "operator"), ("hermes", "supervisor"), ("codex", "developer")]:
            code, created = run_cli("employee", "create", "--id", employee_id, "--name", employee_id, "--role", role, "--runtime", "local", "--workspace", str(self.root / "workspace" / employee_id))
            self.assertEqual(code, 0, created)
        self.mark_active("codex")
        code, submitted = run_cli("task", "submit", "--from", "main", "--to", "codex", "--task-id", "task-correction-limit", "--title", "Correction limit task")
        self.assertEqual(0, code, submitted)
        code, run = run_cli("task", "run", "--task-id", "task-correction-limit", "--agent", "codex", "--by", "hermes", "--max-corrections", "1")
        self.assertEqual(0, code, run)
        attempt_id = run["attempt"]["attempt_id"]
        code, first = run_cli("task", "correct", "--task-id", "task-correction-limit", "--attempt-id", attempt_id, "--by", "hermes", "--message", "第一次纠偏")
        self.assertEqual(0, code, first)
        self.assertEqual("correcting", first["attempt"]["status"])
        self.assertEqual(1, first["supervisor_state"]["corrections_requested"])
        code, second = run_cli("task", "correct", "--task-id", "task-correction-limit", "--attempt-id", attempt_id, "--by", "hermes", "--message", "第二次纠偏")
        self.assertEqual(0, code, second)
        self.assertEqual("blocked", second["status"])
        self.assertEqual("max corrections exceeded", second["reason"])
        with sqlite3.connect(self.root / "company.sqlite") as conn:
            conn.row_factory = sqlite3.Row
            attempt = conn.execute("SELECT * FROM execution_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
            task = conn.execute("SELECT * FROM tasks WHERE id = 'task-correction-limit'").fetchone()
            blocked_event = conn.execute("SELECT * FROM company_events WHERE task_id = 'task-correction-limit' AND event_type = 'task.blocked'").fetchone()
            correction_events = conn.execute("SELECT COUNT(*) AS count FROM company_events WHERE task_id = 'task-correction-limit' AND event_type = 'supervisor.correction_requested'").fetchone()["count"]
        self.assertEqual("failed", attempt["status"])
        self.assertEqual("max corrections exceeded", attempt["error_message"])
        self.assertEqual("blocked", task["status"])
        self.assertEqual("max corrections exceeded", task["blocker"])
        self.assertIsNotNone(blocked_event)
        self.assertEqual(1, correction_events)

    def test_supervisor_scan_stales_attempt_when_max_runtime_exceeded(self) -> None:
        for employee_id, role in [("main", "operator"), ("hermes", "supervisor"), ("codex", "developer")]:
            code, created = run_cli("employee", "create", "--id", employee_id, "--name", employee_id, "--role", role, "--runtime", "local", "--workspace", str(self.root / "workspace" / employee_id))
            self.assertEqual(code, 0, created)
        self.mark_active("codex")
        code, submitted = run_cli("task", "submit", "--from", "main", "--to", "codex", "--task-id", "task-runtime-limit", "--title", "Runtime limit task")
        self.assertEqual(0, code, submitted)
        code, run = run_cli(
            "task",
            "run",
            "--task-id",
            "task-runtime-limit",
            "--agent",
            "codex",
            "--by",
            "hermes",
            "--max-runtime-seconds",
            "10",
            "--stale-after-seconds",
            "300",
            "--heartbeat-interval-seconds",
            "60",
        )
        self.assertEqual(0, code, run)
        attempt_id = run["attempt"]["attempt_id"]
        base = datetime.fromisoformat(run["attempt"]["started_at"])
        old_started_at = (base - timedelta(seconds=20)).isoformat(timespec="seconds")
        fresh_seen_at = (base + timedelta(seconds=1)).isoformat(timespec="seconds")
        with sqlite3.connect(self.root / "company.sqlite") as conn:
            conn.execute(
                "UPDATE execution_attempts SET status = 'running', started_at = ?, last_heartbeat_at = ?, last_progress_at = ? WHERE attempt_id = ?",
                (old_started_at, fresh_seen_at, fresh_seen_at, attempt_id),
            )
            conn.commit()
        scan_at = (base + timedelta(seconds=2)).isoformat(timespec="seconds")
        code, scanned = run_cli("supervisor", "scan-attempts", "--by", "hermes", "--now", scan_at)
        self.assertEqual(0, code, scanned)
        item = next(item for item in scanned["attempts"] if item["attempt_id"] == attempt_id)
        self.assertEqual("stale", item["status"])
        self.assertEqual("stale", item["task_status"])
        self.assertEqual("runtime_exceeded", item["stale_reason"])
        self.assertEqual(22, item["runtime_age_seconds"])
        with sqlite3.connect(self.root / "company.sqlite") as conn:
            conn.row_factory = sqlite3.Row
            attempt = conn.execute("SELECT * FROM execution_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
            task = conn.execute("SELECT * FROM tasks WHERE id = 'task-runtime-limit'").fetchone()
            event = conn.execute("SELECT * FROM company_events WHERE task_id = 'task-runtime-limit' AND event_type = 'task.stale'").fetchone()
        self.assertEqual("stale", attempt["status"])
        self.assertEqual("max runtime exceeded for 22s", attempt["error_message"])
        self.assertEqual("stale", task["status"])
        self.assertEqual("max runtime exceeded for 22s", task["blocker"])
        payload = json.loads(event["payload_json"])
        self.assertEqual("runtime_exceeded", payload["reason"])
        self.assertEqual(10, payload["max_runtime_seconds"])

    def test_cancelled_attempt_rejects_late_progress(self) -> None:
        for employee_id, role in [("main", "operator"), ("hermes", "supervisor"), ("codex", "developer")]:
            code, created = run_cli("employee", "create", "--id", employee_id, "--name", employee_id, "--role", role, "--runtime", "local", "--workspace", str(self.root / "workspace" / employee_id))
            self.assertEqual(code, 0, created)
        self.mark_active("codex")
        code, submitted = run_cli("task", "submit", "--from", "main", "--to", "codex", "--task-id", "task-cancel-late-progress", "--title", "Cancel late progress task")
        self.assertEqual(0, code, submitted)
        code, run = run_cli("task", "run", "--task-id", "task-cancel-late-progress", "--agent", "codex", "--by", "hermes")
        self.assertEqual(0, code, run)
        attempt_id = run["attempt"]["attempt_id"]
        code, cancelled = run_cli("task", "cancel", "--task-id", "task-cancel-late-progress", "--attempt-id", attempt_id, "--by", "hermes", "--reason", "用户停止")
        self.assertEqual(0, code, cancelled)
        code, late_progress = run_cli(
            "task",
            "progress",
            "--task-id",
            "task-cancel-late-progress",
            "--agent",
            "codex",
            "--attempt-id",
            attempt_id,
            "--state",
            "completed",
            "--message",
            "旧进程迟到完成",
            "--progress",
            "100",
        )
        self.assertEqual(2, code, late_progress)
        self.assertEqual("attempt is not active", late_progress["error"])
        with sqlite3.connect(self.root / "company.sqlite") as conn:
            conn.row_factory = sqlite3.Row
            attempt = conn.execute("SELECT * FROM execution_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
            task = conn.execute("SELECT * FROM tasks WHERE id = 'task-cancel-late-progress'").fetchone()
            progress_events = conn.execute("SELECT COUNT(*) AS count FROM company_events WHERE task_id = 'task-cancel-late-progress' AND event_type = 'task.progress'").fetchone()["count"]
        self.assertEqual("cancelled", attempt["status"])
        self.assertEqual("cancelled", task["status"])
        self.assertEqual(0, progress_events)

    def test_cancelled_task_rejects_late_done_evidence(self) -> None:
        for employee_id, role in [("main", "operator"), ("hermes", "supervisor"), ("codex", "developer")]:
            code, created = run_cli("employee", "create", "--id", employee_id, "--name", employee_id, "--role", role, "--runtime", "local", "--workspace", str(self.root / "workspace" / employee_id))
            self.assertEqual(code, 0, created)
        self.mark_active("codex")
        code, submitted = run_cli("task", "submit", "--from", "main", "--to", "codex", "--task-id", "task-cancel-late-done", "--title", "Cancel late done task")
        self.assertEqual(0, code, submitted)
        workspace = Path(submitted["task"]["workspace"]["path"])
        evidence = workspace / "evidence" / "late-done.txt"
        evidence.write_text("late evidence from old process\n", encoding="utf-8")
        code, run = run_cli("task", "run", "--task-id", "task-cancel-late-done", "--agent", "codex", "--by", "hermes")
        self.assertEqual(0, code, run)
        attempt_id = run["attempt"]["attempt_id"]
        code, cancelled = run_cli("task", "cancel", "--task-id", "task-cancel-late-done", "--attempt-id", attempt_id, "--by", "hermes", "--reason", "用户停止")
        self.assertEqual(0, code, cancelled)
        code, late_done = run_cli("task", "done", "--agent", "codex", "--task-id", "task-cancel-late-done", "--summary", "旧进程迟到完成", "--evidence", str(evidence))
        self.assertEqual(2, code, late_done)
        self.assertEqual("task is not completable in status cancelled", late_done["error"])
        with sqlite3.connect(self.root / "company.sqlite") as conn:
            conn.row_factory = sqlite3.Row
            task = conn.execute("SELECT * FROM tasks WHERE id = 'task-cancel-late-done'").fetchone()
            done_events = conn.execute("SELECT COUNT(*) AS count FROM company_events WHERE task_id = 'task-cancel-late-done' AND event_type = 'task.done'").fetchone()["count"]
            evidence_rows = conn.execute("SELECT COUNT(*) AS count FROM evidence WHERE task_id = 'task-cancel-late-done'").fetchone()["count"]
        self.assertEqual("cancelled", task["status"])
        self.assertEqual("", task["evidence_path"])
        self.assertEqual(0, done_events)
        self.assertEqual(0, evidence_rows)

    def test_task_done_closes_current_managed_attempt_as_success(self) -> None:
        for employee_id, role in [("main", "operator"), ("codex", "developer")]:
            code, created = run_cli("employee", "create", "--id", employee_id, "--name", employee_id, "--role", role, "--runtime", "local", "--workspace", str(self.root / "workspace" / employee_id))
            self.assertEqual(code, 0, created)
        self.mark_active("codex")
        code, submitted = run_cli("task", "submit", "--from", "main", "--to", "codex", "--task-id", "task-done-closes-attempt", "--title", "Done closes attempt")
        self.assertEqual(0, code, submitted)
        workspace = Path(submitted["task"]["workspace"]["path"])
        evidence = workspace / "evidence" / "result.md"
        evidence.write_text("done closes attempt evidence\n", encoding="utf-8")
        code, run = run_cli("task", "run", "--task-id", "task-done-closes-attempt", "--agent", "codex", "--by", "main")
        self.assertEqual(0, code, run)
        attempt_id = run["attempt"]["attempt_id"]

        code, done = run_cli("task", "done", "--agent", "codex", "--task-id", "task-done-closes-attempt", "--summary", "完成并关闭 attempt", "--evidence", str(evidence))
        self.assertEqual(0, code, done)
        code, shown = run_cli("task", "show", "--task-id", "task-done-closes-attempt")
        self.assertEqual(0, code, shown)
        self.assertEqual("completed", shown["task"]["status"])
        attempt = next(item for item in shown["attempts"] if item["attempt_id"] == attempt_id)
        self.assertEqual("success", attempt["status"])
        self.assertTrue(attempt["finished_at"])
        self.assertEqual("", attempt["error_message"])
        done_event = next(item for item in shown["events"] if item["event_type"] == "task.done")
        self.assertEqual(run["attempt"]["trace_id"], done_event["trace_id"])
        self.assertEqual(attempt_id, json.loads(done_event["payload_json"])["attempt_id"])
        evidence_record = next(item for item in shown["evidence_records"] if item["task_id"] == "task-done-closes-attempt")
        self.assertEqual(attempt_id, evidence_record["attempt_id"])
        self.assertEqual("codex", evidence_record["employee_id"])
        self.assertEqual(1, evidence_record["is_final"])
        self.assertIn("display", evidence_record)
        self.assertTrue(evidence_record["display"]["allowed"])
        self.assertFalse(evidence_record["display"]["absolute_path_exposed"])
        self.assertNotIn(str(self.root), json.dumps(evidence_record, ensure_ascii=False))
        self.assertNotIn("path_or_url", evidence_record)
        with sqlite3.connect(self.root / "company.sqlite") as conn:
            conn.row_factory = sqlite3.Row
            evidence_row = conn.execute("SELECT * FROM evidence WHERE task_id = ? AND is_final = 1", ("task-done-closes-attempt",)).fetchone()
        self.assertIsNotNone(evidence_row)
        self.assertEqual(attempt_id, evidence_row["attempt_id"])

    def test_task_done_accepts_promoted_evidence_when_cli_uses_original_relative_path(self) -> None:
        for employee_id, role in [("main", "operator"), ("codex", "developer")]:
            code, created = run_cli("employee", "create", "--id", employee_id, "--name", employee_id, "--role", role, "--runtime", "local", "--workspace", str(self.root / "workspace" / employee_id))
            self.assertEqual(code, 0, created)
        self.mark_active("codex")
        task_id = "task-done-relative-promoted-evidence"
        code, submitted = run_cli("task", "submit", "--from", "main", "--to", "codex", "--task-id", task_id, "--title", "relative promoted evidence")
        self.assertEqual(0, code, submitted)
        workspace = Path(submitted["task"]["workspace"]["path"])
        evidence_file = workspace / "evidence" / "relative.md"
        evidence_file.parent.mkdir(parents=True, exist_ok=True)
        evidence_file.write_text("relative promoted evidence\n", encoding="utf-8")
        relative_evidence = os.path.relpath(evidence_file, Path.cwd())
        code, claimed = run_cli("task", "claim", "--task-id", task_id, "--agent", "codex")
        self.assertEqual(0, code, claimed)
        code, attempt = run_cli("task", "attempt", "start", "--task-id", task_id, "--employee", "codex", "--adapter-type", "codex")
        self.assertEqual(0, code, attempt)
        code, artifact = run_cli("task", "artifact", "register", "--task-id", task_id, "--employee", "codex", "--path", str(evidence_file), "--type", "md", "--name", "relative.md", "--stage", "final", "--summary", "relative final", "--final")
        self.assertEqual(0, code, artifact)
        code, promoted = run_cli("task", "evidence", "promote", "--artifact-id", artifact["artifact"]["artifact_id"], "--employee", "codex", "--summary", "relative promoted")
        self.assertEqual(0, code, promoted)

        code, done = run_cli("task", "done", "--agent", "codex", "--task-id", task_id, "--summary", "done with relative path", "--evidence", relative_evidence)
        self.assertEqual(0, code, done)
        self.assertEqual(attempt["attempt"]["attempt_id"], done["attempt_id"])

    def test_managed_attempt_api_control_endpoints(self) -> None:
        for employee_id, role in [("main", "operator"), ("hermes", "supervisor"), ("codex", "developer")]:
            code, created = run_cli("employee", "create", "--id", employee_id, "--name", employee_id, "--role", role, "--runtime", "local", "--workspace", str(self.root / "workspace" / employee_id))
            self.assertEqual(code, 0, created)
        self.mark_active("codex")
        code, submitted = run_cli("task", "submit", "--from", "main", "--to", "codex", "--task-id", "task-long-api", "--title", "Long API task")
        self.assertEqual(0, code, submitted)
        status, run = api_gateway.route_post(
            "/v1/tasks/task-long-api/run",
            {"agent": "codex", "by": "hermes", "max_runtime_seconds": 36000, "stale_after_seconds": 900, "session_key": "agent:codex:hermes"},
        )
        self.assertEqual(200, status, run)
        attempt_id = run["attempt"]["attempt_id"]
        conn = companyctl.connect()
        try:
            current = datetime.now(timezone.utc).astimezone()
            old = (current - timedelta(minutes=20)).isoformat(timespec="seconds")
            fresh = current.isoformat(timespec="seconds")
            conn.execute("UPDATE execution_attempts SET last_progress_at = ?, last_heartbeat_at = ? WHERE attempt_id = ?", (old, fresh, attempt_id))
            conn.commit()
        finally:
            conn.close()
        code, listed_cli = run_cli("task", "list")
        self.assertEqual(0, code, listed_cli)
        listed_cli_task = next(item for item in listed_cli["tasks"] if item["id"] == "task-long-api")
        self.assertEqual(attempt_id, listed_cli_task["current_attempt"]["attempt_id"])
        self.assertEqual("starting", listed_cli_task["current_attempt"]["status"])
        self.assertEqual("task-long-api", listed_cli_task["current_attempt"]["task_id"])
        self.assertEqual("progress_stagnant", listed_cli_task["long_task_state"])
        self.assertEqual("fresh", listed_cli_task["heartbeat_state"])
        self.assertEqual("stagnant", listed_cli_task["progress_state"])
        status, listed_api = api_gateway.route_get("/v1/tasks", {})
        self.assertEqual(200, status, listed_api)
        listed_api_task = next(item for item in listed_api["tasks"] if item["id"] == "task-long-api")
        self.assertEqual(attempt_id, listed_api_task["current_attempt"]["attempt_id"])
        self.assertEqual("starting", listed_api_task["current_attempt"]["status"])
        self.assertEqual("task-long-api", listed_api_task["current_attempt"]["task_id"])
        self.assertEqual("progress_stagnant", listed_api_task["long_task_state"])
        self.assertEqual("fresh", listed_api_task["heartbeat_state"])
        self.assertEqual("stagnant", listed_api_task["progress_state"])
        status, progressed = api_gateway.route_post("/v1/tasks/task-long-api/progress", {"agent": "codex", "attempt_id": attempt_id, "state": "acknowledged", "message": "已收到", "progress": 5, "payload": {"source": "api-test"}})
        self.assertEqual(200, status, progressed)
        self.assertEqual("running", progressed["attempt"]["status"])
        self.assertEqual("received", progressed["progress"]["progress_layer"])
        status, corrected = api_gateway.route_post("/v1/tasks/task-long-api/correct", {"attempt_id": attempt_id, "by": "hermes", "message": "继续按计划执行"})
        self.assertEqual(200, status, corrected)
        status, attempts = api_gateway.route_get("/v1/tasks/task-long-api/attempts", {})
        self.assertEqual(200, status, attempts)
        self.assertEqual(attempt_id, attempts["attempts"][0]["attempt_id"])
        status, cancelled = api_gateway.route_post("/v1/tasks/task-long-api/cancel", {"attempt_id": attempt_id, "by": "hermes", "reason": "测试取消"})
        self.assertEqual(200, status, cancelled)
        self.assertEqual("cancelled", cancelled["attempt"]["status"])
        status, retry = api_gateway.route_post("/v1/tasks/task-long-api/retry", {"by": "hermes", "reason": "API retry after cancel"})
        self.assertEqual(200, status, retry)
        self.assertNotEqual(attempt_id, retry["attempt"]["attempt_id"])
        self.assertEqual("starting", retry["attempt"]["status"])
        self.assertEqual(run["attempt"]["trace_id"], retry["attempt"]["trace_id"])
        self.assertEqual(attempt_id, retry["attempt"]["metadata"]["previous_attempt_id"])
        self.assertEqual("claimed", retry["task"]["status"])

    def test_dashboard_employee_cards_include_managed_attempt_state(self) -> None:
        for employee_id, role in [("main", "operator"), ("hermes", "supervisor"), ("codex", "developer")]:
            code, created = run_cli("employee", "create", "--id", employee_id, "--name", employee_id, "--role", role, "--runtime", "local", "--workspace", str(self.root / "workspace" / employee_id))
            self.assertEqual(code, 0, created)
        self.mark_active("codex")
        code, submitted = run_cli("task", "submit", "--from", "main", "--to", "codex", "--task-id", "task-long-dashboard", "--title", "Long dashboard task")
        self.assertEqual(0, code, submitted)
        code, run = run_cli("task", "run", "--task-id", "task-long-dashboard", "--agent", "codex", "--by", "hermes")
        self.assertEqual(0, code, run)
        conn = companyctl.connect()
        try:
            summary = company_dashboard.load_summary(conn)
        finally:
            conn.close()
        codex = next(item for item in summary["employees"] if item["id"] == "codex")
        self.assertEqual("task-long-dashboard", codex["current_attempt"]["task_id"])
        self.assertEqual("Long dashboard task", codex["current_attempt"]["task_title"])
        self.assertEqual("starting", codex["current_attempt"]["status"])
        self.assertEqual(run["attempt"]["attempt_id"], codex["current_attempt"]["attempt_id"])
        models = company_dashboard.employee_view_models(summary)
        codex_model = next(item for item in models if item["id"] == "codex")
        self.assertEqual("Long dashboard task", codex_model["current_task_title"])
        self.assertEqual("task-long-dashboard", codex_model["current_task_id"])

    def test_dashboard_employee_view_models_include_readiness_badges(self) -> None:
        for employee_id, role, runtime in [
            ("main", "operator", "openclaw"),
            ("codex", "developer", "codex"),
            ("antigravity", "developer", "antigravity"),
        ]:
            code, created = run_cli("employee", "create", "--id", employee_id, "--name", employee_id, "--role", role, "--runtime", runtime, "--workspace", str(self.root / "workspace" / employee_id))
            self.assertEqual(0, code, created)
            self.mark_active(employee_id)
            code, heartbeat = run_cli("heartbeat", "--agent", employee_id)
            self.assertEqual(0, code, heartbeat)
        verification_dir = self.root / "state" / "employee-verification" / "codex"
        verification_dir.mkdir(parents=True)
        (verification_dir / "latest-runtime.json").write_text(json.dumps({"ok": True, "activation_allowed": True}, ensure_ascii=False), encoding="utf-8")
        attendance_dir = self.root / "state" / "attendance"
        attendance_dir.mkdir(parents=True)
        (attendance_dir / "latest.json").write_text(
            json.dumps(
                {
                    "ok": True,
                    "employees": [
                        {"agent": "main", "status": "online"},
                        {"agent": "codex", "status": "online"},
                        {"agent": "antigravity", "status": "online"},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        conn = companyctl.connect()
        try:
            summary = company_dashboard.load_summary(conn)
            models = company_dashboard.employee_view_models(summary)
        finally:
            conn.close()
        by_id = {item["id"]: item for item in models}
        self.assertEqual("active_ready", by_id["codex"]["readiness_level"])
        self.assertEqual("online_only", by_id["main"]["readiness_level"])
        self.assertEqual("online_only", by_id["antigravity"]["readiness_level"])
        self.assertTrue(by_id["codex"]["schedulable"])
        self.assertFalse(by_id["main"]["schedulable"])
        self.assertFalse(by_id["antigravity"]["schedulable"])
        self.assertIn("runtime_evidence", by_id["codex"]["readiness_reason"])
        self.assertEqual("default", by_id["codex"]["sandbox_profile"]["profile"])
        self.assertEqual("none", by_id["codex"]["sandbox_profile"]["isolation"])
        self.assertEqual("none", by_id["codex"]["sandbox_profile"]["network"])
        self.assertEqual("workspace_only", by_id["codex"]["sandbox_profile"]["workspace_scope"])
        self.assertTrue(by_id["codex"]["sandbox_profile"]["permissions"]["can_claim_tasks"])
        self.assertIn("external_send", by_id["codex"]["sandbox_profile"]["permissions"]["requires_approval_for"])
        self.assertEqual("runtime_fallback", by_id["antigravity"]["sandbox_profile"]["source"])
        code, shown = run_cli("employee", "show", "--id", "codex")
        self.assertEqual(0, code, shown)
        self.assertEqual(by_id["codex"]["sandbox_profile"], shown["sandbox_profile"])
        status, employees_payload = api_gateway.route_get("/v1/employees", {})
        self.assertEqual(HTTPStatus.OK, status, employees_payload)
        api_codex = next(item for item in employees_payload["employees"] if item["id"] == "codex")
        self.assertEqual(by_id["codex"]["sandbox_profile"], api_codex["sandbox_profile"])
        output = self.root / "state" / "sandbox-dashboard.html"
        with contextlib.redirect_stdout(io.StringIO()):
            code = company_dashboard.main(["--output", str(output), "--variant", "advanced"])
        self.assertEqual(0, code)
        html = output.read_text(encoding="utf-8")
        self.assertIn("sandbox-profile-chip", html)
        self.assertIn("Schedulable", html)
        self.assertIn("readiness_reason", html)
        self.assertIn("active_ready", html)
        self.assertIn("online_only", html)
        self.assertIn("readinessBadgeLabel", html)
        self.assertIn("Ready to schedule", html)
        self.assertIn("Limited / review", html)
        self.assertIn("Not schedulable", html)
        self.assertIn("reason=${escapeHtml(shortText(reason, 90))}", html)

    def test_dashboard_readiness_does_not_treat_heartbeat_as_direct_attendance(self) -> None:
        code, created = run_cli("employee", "create", "--id", "claude-code", "--name", "Claude Code", "--role", "runtime-agent", "--runtime", "claude", "--workspace", str(self.root / "workspace" / "claude-code"))
        self.assertEqual(0, code, created)
        self.mark_active("claude-code")
        code, heartbeat = run_cli("heartbeat", "--agent", "claude-code")
        self.assertEqual(0, code, heartbeat)
        verification_dir = self.root / "state" / "employee-verification" / "claude-code"
        verification_dir.mkdir(parents=True)
        (verification_dir / "latest-runtime.json").write_text(json.dumps({"ok": True, "activation_allowed": True}, ensure_ascii=False), encoding="utf-8")
        attendance_dir = self.root / "state" / "attendance"
        attendance_dir.mkdir(parents=True)
        (attendance_dir / "latest.json").write_text(json.dumps({"ok": True, "employees": []}, ensure_ascii=False), encoding="utf-8")

        with companyctl.connect() as conn:
            summary = company_dashboard.load_summary(conn)
            models = company_dashboard.employee_view_models(summary)
        row = next(item for item in models if item["id"] == "claude-code")
        self.assertEqual("active_limited", row["readiness_level"])
        self.assertEqual("runtime_evidence_without_live_task_or_direct_attendance", row["readiness_reason"])
        self.assertFalse(row["schedulable"])

    def test_skill_registry_lists_packages_in_cli_api_and_dashboard(self) -> None:
        manifest_path = self.write_skill_manifest()
        code, listed = run_cli("skill", "list")
        self.assertEqual(0, code, listed)
        self.assertEqual([str(manifest_path)], [item["manifest_path"] for item in listed["skills"]])
        skill = listed["skills"][0]
        self.assertEqual("ecommerce-copy-demo", skill["id"])
        self.assertEqual("Ecommerce Copy Demo", skill["name"])
        self.assertEqual("local-script", skill["runtime_type"])
        self.assertEqual("task", skill["workspace_permission"])
        self.assertEqual("final/listing-summary.md", skill["final_artifact"])
        self.assertEqual("task", skill["pricing_unit"])
        self.assertTrue(skill["evidence_required"])

        status, api_payload = api_gateway.route_get("/v1/skills", {})
        self.assertEqual(HTTPStatus.OK, status, api_payload)
        self.assertEqual(listed["skills"], api_payload["skills"])

        output = self.root / "state" / "skill-registry-dashboard.html"
        with contextlib.redirect_stdout(io.StringIO()):
            code = company_dashboard.main(["--output", str(output), "--variant", "advanced"])
        self.assertEqual(0, code)
        html = output.read_text(encoding="utf-8")
        self.assertIn("Skill Registry", html)
        self.assertIn("skill-registry-container", html)
        self.assertNotIn("ecommerce-copy-demo", html)

    def test_dashboard_renders_managed_task_control_buttons(self) -> None:
        for employee_id, role in [("main", "operator"), ("hermes", "supervisor"), ("codex", "developer")]:
            code, created = run_cli("employee", "create", "--id", employee_id, "--name", employee_id, "--role", role, "--runtime", "local", "--workspace", str(self.root / "workspace" / employee_id))
            self.assertEqual(code, 0, created)
            self.mark_active(employee_id)
        code, submitted = run_cli("task", "submit", "--from", "main", "--to", "codex", "--task-id", "task-dashboard-controls", "--title", "Dashboard controls")
        self.assertEqual(0, code, submitted)
        code, run = run_cli("task", "run", "--task-id", "task-dashboard-controls", "--agent", "codex", "--by", "hermes")
        self.assertEqual(0, code, run)
        output = self.root / "state" / "dashboard-controls.html"
        with contextlib.redirect_stdout(io.StringIO()):
            code = company_dashboard.main(["--output", str(output), "--variant", "advanced"])
        self.assertEqual(0, code)
        html = output.read_text(encoding="utf-8")
        self.assertIn('<body class="dashboard-layout-fix">', html)
        self.assertIn("Tasks & Workflows", html)
        self.assertIn("openTaskDetailDrawer", html)
        status, tasks_payload = api_gateway.route_get("/v1/tasks", {"limit": ["50"]})
        self.assertEqual(HTTPStatus.OK, status, tasks_payload)
        self.assertIn("task-dashboard-controls", [task["id"] for task in tasks_payload["tasks"]])
        self.assertNotIn("task-dashboard-controls", html)
        self.assertNotIn(run["attempt"]["attempt_id"], html)
        self.assertIn("correctTaskAttempt", html)
        self.assertIn("cancelTaskAttempt", html)
        self.assertIn("recordWaitDecision", html)
        self.assertIn("View Logs", html)
        self.assertIn("Send Probe <small>ledger only</small>", html)
        self.assertIn("retryTask", html)
        self.assertIn("reassignTask", html)
        self.assertIn("function buildApprovalReason(detail)", html)
        self.assertIn("async function requestDashboardApproval(action, taskId, attemptId, by, reason, extra)", html)
        self.assertIn("companyApiPost('/v1/approvals', payload)", html)
        self.assertIn("Dashboard records approval first; real execution must be owner-approved explicitly.", html)
        self.assertIn("ownerApproved === true", html)
        self.assertIn("executeApprovedDashboardAction", html)
        self.assertIn("action === 'task.correct'", html)
        self.assertIn("approval.detail", html)
        self.assertIn("Object.assign({}, legacyDetail, apiDetail)", html)
        self.assertIn("metadata.task_id || requestDetail.task_id", html)
        self.assertIn("parseApprovalRequestReason", html)
        self.assertIn("requestDetail.correction_message", html)
        self.assertIn("Owner-approved correction recorded", html)
        self.assertIn("Owner-approved cancellation recorded", html)
        self.assertIn("Owner-approved retry recorded", html)
        self.assertIn("Owner-approved reassign recorded", html)
        self.assertIn("action === 'task.cancel'", html)
        self.assertIn("action === 'task.retry'", html)
        self.assertIn("action === 'task.reassign'", html)
        self.assertIn("Requesting owner approval for correction", html)
        self.assertIn("Requesting owner approval for cancellation", html)
        self.assertIn("Requesting owner approval for retry", html)
        self.assertIn("Requesting owner approval for reassign", html)
        self.assertIn("const isDone = isTerminalTaskState(task, attempt)", html)
        self.assertIn("const isObservable = isRunning || ['heartbeat_stale', 'progress_stagnant', 'stale', 'correcting'].includes(longTaskState);", html)
        self.assertIn("if (isObservable && !isDone && !isCancelled)", html)
        self.assertIn("if (isRecoverable && !isDone)", html)
        self.assertIn("handleOwnerAttentionAction('${escapeHtml(taskId)}', '${escapeHtml(attemptId)}', '', 'review_evidence')", html)
        self.assertIn("viewTaskTrace('", html)
        self.assertIn("/v1/tasks/${encodeURIComponent(taskId)}/correct", html)
        self.assertIn("/v1/tasks/${encodeURIComponent(taskId)}/cancel", html)
        self.assertIn("/v1/tasks/${encodeURIComponent(taskId)}/retry", html)
        self.assertIn("/v1/tasks/${encodeURIComponent(taskId)}/reassign", html)
        self.assertIn("Attempt History", html)
        self.assertIn("Attempt Lineage", html)
        self.assertIn("Attempt Recovery Chain", html)
        self.assertIn("old attempts are retained", html)
        self.assertIn("retry/reassign creates a new attempt", html)
        self.assertIn("previous_attempt_id", html)
        self.assertIn("Sanitized Logs", html)
        self.assertIn("sanitizedLogsSummary", html)
        self.assertIn("Log Policy", html)
        self.assertIn("adapterRunLogPolicySummary", html)
        self.assertIn("readiness-badge", html)

    def test_agent_matrix_reports_employee_readiness_levels(self) -> None:
        for employee_id, role, runtime, status in [
            ("main", "operator", "openclaw", "active"),
            ("codex", "developer", "codex", "active"),
            ("antigravity", "developer", "antigravity", "candidate"),
        ]:
            code, created = run_cli("employee", "create", "--id", employee_id, "--name", employee_id, "--role", role, "--runtime", runtime, "--workspace", str(self.root / "workspace" / employee_id))
            self.assertEqual(0, code, created)
            if status == "active":
                self.mark_active(employee_id)
        verification_dir = self.root / "state" / "employee-verification" / "codex"
        verification_dir.mkdir(parents=True)
        (verification_dir / "latest-runtime.json").write_text(json.dumps({"ok": True, "activation_allowed": True}, ensure_ascii=False), encoding="utf-8")
        attendance_dir = self.root / "state" / "attendance"
        attendance_dir.mkdir(parents=True)
        (attendance_dir / "latest.json").write_text(
            json.dumps(
                {
                    "ok": False,
                    "employees": [
                        {"agent": "main", "status": "online", "reply": "main 在岗"},
                        {"agent": "codex", "status": "online", "reply": "codex 在岗"},
                        {"agent": "antigravity", "status": "online", "reply": "antigravity 在岗"},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        code, matrix = run_cli("agent-matrix", "--agents", "main,codex,antigravity")
        self.assertEqual(0, code, matrix)
        rows = {item["agent"]: item for item in matrix["employees"]}
        self.assertEqual("online_only", rows["main"]["level"])
        self.assertEqual("active_ready", rows["codex"]["level"])
        self.assertEqual("candidate_only", rows["antigravity"]["level"])
        self.assertEqual("online", rows["codex"]["checks"]["attendance"])

        status, api_matrix = api_gateway.route_get("/v1/agent-matrix", {"agents": ["main,codex,antigravity"]})
        self.assertEqual(200, status, api_matrix)
        api_rows = {item["agent"]: item for item in api_matrix["employees"]}
        self.assertEqual("active_ready", api_rows["codex"]["level"])
        self.assertEqual("candidate_only", api_rows["antigravity"]["level"])

    def test_agent_matrix_accepts_successful_managed_attempt_as_runtime_evidence(self) -> None:
        workspace = self.root / "workspace" / "antigravity"
        for employee_id, role, runtime in [
            ("main", "operator", "openclaw"),
            ("hermes", "supervisor", "hermes"),
            ("antigravity", "developer", "antigravity"),
        ]:
            code, created = run_cli("employee", "create", "--id", employee_id, "--name", employee_id, "--role", role, "--runtime", runtime, "--workspace", str(workspace if employee_id == "antigravity" else self.root / "workspace" / employee_id))
            self.assertEqual(0, code, created)
            self.mark_active(employee_id)
        attendance_dir = self.root / "state" / "attendance"
        attendance_dir.mkdir(parents=True)
        (attendance_dir / "latest.json").write_text(
            json.dumps(
                {
                    "ok": True,
                    "employees": [
                        {"agent": "main", "status": "online", "reply": "main 在岗"},
                        {"agent": "hermes", "status": "online", "reply": "hermes 在岗"},
                        {"agent": "antigravity", "status": "online", "reply": "antigravity 在岗"},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        code, submitted = run_cli("task", "submit", "--from", "main", "--to", "antigravity", "--task-id", "task-antigravity-matrix-evidence", "--title", "Matrix evidence")
        self.assertEqual(0, code, submitted)
        structured_reply = "\n".join(
            [
                "status: done",
                "current_action: completed structured matrix evidence task",
                "changed_files: employees/antigravity/reports/task-antigravity-matrix-evidence/antigravity-managed-attempt.json",
                "verification_run: managed attempt unit check passed",
                "browser_check: -",
                "blocker: -",
                "eta: -",
            ]
        )
        with mock.patch.object(antigravity_adapter, "run_agy_print", return_value=(0, structured_reply, "")):
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                code = antigravity_adapter.main(["--agent", "antigravity", "--managed-attempt", "--by", "hermes"])
        self.assertEqual(0, code, json.loads(captured.getvalue()))

        code, matrix = run_cli("agent-matrix", "--agents", "antigravity")
        self.assertEqual(0, code, matrix)
        row = matrix["employees"][0]
        self.assertEqual("active_ready", row["level"])
        self.assertEqual("verified", row["checks"]["runtime"])
        self.assertEqual("runtime_evidence", row["checks"]["evidence"])
        self.assertEqual("success", row["latest_attempt"]["status"])

    def test_agent_matrix_rejects_path_only_success_attempt_without_final_evidence(self) -> None:
        for employee_id, role, runtime in [
            ("main", "operator", "openclaw"),
            ("codex", "developer", "codex"),
        ]:
            code, created = run_cli("employee", "create", "--id", employee_id, "--name", employee_id, "--role", role, "--runtime", runtime, "--workspace", str(self.root / "workspace" / employee_id))
            self.assertEqual(0, code, created)
            self.mark_active(employee_id)
        attendance_dir = self.root / "state" / "attendance"
        attendance_dir.mkdir(parents=True)
        (attendance_dir / "latest.json").write_text(
            json.dumps({"ok": True, "employees": [{"agent": "codex", "status": "online", "reply": "codex 在岗"}]}, ensure_ascii=False),
            encoding="utf-8",
        )
        code, submitted = run_cli("task", "submit", "--from", "main", "--to", "codex", "--task-id", "task-path-only-success", "--title", "Path only success")
        self.assertEqual(0, code, submitted)
        code, run = run_cli("task", "run", "--task-id", "task-path-only-success", "--agent", "codex", "--by", "main")
        self.assertEqual(0, code, run)
        evidence_path = self.root / "state" / "reports" / "path-only.md"
        evidence_path.parent.mkdir(parents=True)
        evidence_path.write_text("path-only evidence\n", encoding="utf-8")
        with companyctl.connect() as conn:
            conn.execute(
                "UPDATE tasks SET status = 'completed', evidence_path = ?, summary = 'legacy path only', updated_at = ? WHERE id = ?",
                (str(evidence_path), companyctl.now(), "task-path-only-success"),
            )
            conn.execute(
                "UPDATE execution_attempts SET status = 'success', finished_at = ? WHERE attempt_id = ?",
                (companyctl.now(), run["attempt"]["attempt_id"]),
            )
            conn.commit()

        code, matrix = run_cli("agent-matrix", "--agents", "codex")
        self.assertEqual(0, code, matrix)
        row = matrix["employees"][0]
        self.assertEqual("online_only", row["level"])
        self.assertEqual("missing", row["checks"]["evidence"])
        self.assertEqual("success", row["latest_attempt"]["status"])

    def test_agent_matrix_accepts_runtime_verify_adapter_task_evidence(self) -> None:
        for employee_id, role, runtime in [
            ("main", "operator", "openclaw"),
            ("nestcar", "business-agent", "openclaw"),
        ]:
            code, created = run_cli("employee", "create", "--id", employee_id, "--name", employee_id, "--role", role, "--runtime", runtime, "--workspace", str(self.root / "workspace" / employee_id))
            self.assertEqual(0, code, created)
            self.mark_active(employee_id)
        attendance_dir = self.root / "state" / "attendance"
        attendance_dir.mkdir(parents=True)
        (attendance_dir / "latest.json").write_text(
            json.dumps({"ok": True, "employees": [{"agent": "nestcar", "status": "online", "reply": "nestcar 在岗"}]}, ensure_ascii=False),
            encoding="utf-8",
        )
        task_id = "task-openclaw-verify-local-nestcar"
        code, submitted = run_cli(
            "task",
            "submit",
            "--from",
            "main",
            "--to",
            "nestcar",
            "--task-id",
            task_id,
            "--title",
            "Runtime adapter dry-run check: nestcar",
            "--description",
            "Adapter dry-run check task only.",
        )
        self.assertEqual(0, code, submitted)
        evidence_path = self.root / "employees" / "nestcar" / "reports" / task_id / "openclaw-adapter-report.md"
        evidence_path.parent.mkdir(parents=True)
        evidence_path.write_text("adapter evidence\n", encoding="utf-8")
        code, done = run_cli("task", "done", "--agent", "nestcar", "--task-id", task_id, "--summary", "adapter evidence", "--evidence", str(evidence_path))
        self.assertEqual(0, code, done)

        code, matrix = run_cli("agent-matrix", "--agents", "nestcar")
        self.assertEqual(0, code, matrix)
        row = matrix["employees"][0]
        self.assertEqual("active_ready", row["level"])
        self.assertEqual("runtime_evidence", row["checks"]["evidence"])

    def test_agent_matrix_does_not_mark_adapter_ready_from_verification_and_heartbeat_only(self) -> None:
        code, employee = run_cli(
            "employee",
            "create",
            "--id",
            "claude-code",
            "--name",
            "Claude Code",
            "--role",
            "runtime-agent",
            "--runtime",
            "claude",
            "--workspace",
            str(self.root / "workspace" / "claude-code"),
        )
        self.assertEqual(0, code, employee)
        self.mark_active("claude-code")
        verification_dir = self.root / "state" / "employee-verification" / "claude-code"
        verification_dir.mkdir(parents=True)
        (verification_dir / "latest-runtime.json").write_text(
            json.dumps({"ok": True, "activation_allowed": True}, ensure_ascii=False),
            encoding="utf-8",
        )
        attendance_dir = self.root / "state" / "attendance"
        attendance_dir.mkdir(parents=True)
        (attendance_dir / "latest.json").write_text(json.dumps({"ok": True, "employees": []}, ensure_ascii=False), encoding="utf-8")
        with companyctl.connect() as conn:
            companyctl.heartbeat_internal(conn, "claude-code", {"source": "adapter-heartbeat-only"})

        code, matrix = run_cli("agent-matrix", "--agents", "claude-code")
        self.assertEqual(0, code, matrix)
        row = matrix["employees"][0]
        self.assertEqual("active_limited", row["level"])
        self.assertEqual("runtime_evidence_without_live_task_or_direct_attendance", row["reason"])
        self.assertEqual("verified_limited", row["checks"]["runtime"])
        self.assertEqual("fresh", row["checks"]["heartbeat"])

    def test_agent_matrix_marks_skill_ready_from_runtime_evidence_without_direct_chat(self) -> None:
        code, runtime = run_cli("runtime", "register", "--runtime", "skill", "--command", "company-skill-package-worker", "--notes", "Skill Package runtime")
        self.assertEqual(0, code, runtime)
        code, employee = run_cli("employee", "create", "--id", "image-copy-skill", "--name", "Image Copy Skill", "--role", "skill-worker", "--runtime", "skill", "--workspace", str(self.root / "workspace" / "image-copy-skill"))
        self.assertEqual(0, code, employee)
        self.mark_active("image-copy-skill")
        attendance_dir = self.root / "state" / "attendance"
        attendance_dir.mkdir(parents=True)
        (attendance_dir / "latest.json").write_text(
            json.dumps({"ok": False, "employees": [{"agent": "image-copy-skill", "status": "no_reply", "reason": "unsupported_runtime:skill"}]}, ensure_ascii=False),
            encoding="utf-8",
        )
        task_id = "task-runtime-skill-evidence"
        code, submitted = run_cli("task", "submit", "--from", "openclaw-main", "--to", "image-copy-skill", "--task-id", task_id, "--title", "Runtime adapter dry-run check: image-copy-skill")
        self.assertEqual(0, code, submitted)
        code, run = run_cli("task", "run", "--task-id", task_id, "--agent", "image-copy-skill", "--by", "hermes", "--adapter-type", "skill")
        self.assertEqual(0, code, run)
        workspace = Path(submitted["task"]["workspace"]["path"])
        final_path = workspace / "final" / "result.md"
        final_path.parent.mkdir(parents=True, exist_ok=True)
        final_path.write_text("skill output\n", encoding="utf-8")
        code, artifact = run_cli("task", "artifact", "register", "--task-id", task_id, "--employee", "image-copy-skill", "--path", str(final_path), "--type", "md", "--stage", "final", "--final", "--summary", "skill output")
        self.assertEqual(0, code, artifact)
        code, approved = run_cli("task", "artifact", "approve", "--artifact-id", artifact["artifact"]["artifact_id"], "--by", "image-copy-skill", "--reason", "test")
        self.assertEqual(0, code, approved)
        code, evidence = run_cli("task", "evidence", "promote", "--artifact-id", artifact["artifact"]["artifact_id"], "--by", "image-copy-skill", "--summary", "skill evidence")
        self.assertEqual(0, code, evidence)
        code, done = run_cli("task", "done", "--agent", "image-copy-skill", "--task-id", task_id, "--summary", "skill done", "--evidence", evidence["evidence"]["path_or_url"])
        self.assertEqual(0, code, done)
        code, finish = run_cli("task", "attempt", "finish", "--attempt-id", run["attempt"]["attempt_id"], "--status", "success")
        self.assertEqual(0, code, finish)

        code, matrix = run_cli("agent-matrix", "--agents", "image-copy-skill")
        self.assertEqual(0, code, matrix)
        row = matrix["employees"][0]
        self.assertEqual("active_ready", row["level"])
        self.assertEqual("skill_runtime_evidence_no_direct_chat_required", row["reason"])

    def test_v3_claim_returns_context_and_done_auto_promotes_workspace_evidence(self) -> None:
        for employee_id in ["manager", "writer"]:
            code, created = run_cli("employee", "create", "--id", employee_id, "--name", employee_id, "--role", "agent", "--runtime", "local", "--workspace", str(self.root / "workspace" / employee_id))
            self.assertEqual(code, 0, created)
        with sqlite3.connect(self.root / "company.sqlite") as conn:
            conn.execute("UPDATE employees SET status = 'active' WHERE id IN ('manager', 'writer')")
            conn.commit()

        code, submitted = run_cli("task", "submit", "--from", "manager", "--to", "writer", "--task-id", "task-v3-claim-context", "--title", "claim context")
        self.assertEqual(code, 0, submitted)
        code, claimed = run_cli("task", "claim", "--agent", "writer", "--task-id", "task-v3-claim-context")
        self.assertEqual(code, 0, claimed)
        self.assertIn("context_package", claimed)
        self.assertEqual("task-v3-claim-context", claimed["context_package"]["context"]["task_id"])
        self.assertTrue(Path(claimed["task"]["workspace"]["path"]).exists())

        evidence_path = Path(claimed["task"]["workspace"]["path"]) / "evidence" / "adapter-report.md"
        evidence_path.write_text("adapter evidence\n", encoding="utf-8")
        code, done = run_cli("task", "done", "--agent", "writer", "--task-id", "task-v3-claim-context", "--summary", "done through adapter-compatible path", "--evidence", str(evidence_path))
        self.assertEqual(code, 0, done)
        with sqlite3.connect(self.root / "company.sqlite") as conn:
            conn.row_factory = sqlite3.Row
            artifact = conn.execute("SELECT * FROM artifacts WHERE task_id = 'task-v3-claim-context' AND name = 'adapter-report.md'").fetchone()
            evidence = conn.execute("SELECT * FROM evidence WHERE task_id = 'task-v3-claim-context' AND artifact_id = ?", (artifact["artifact_id"],)).fetchone()
        self.assertIsNotNone(artifact)
        self.assertIsNotNone(evidence)
        self.assertEqual(str(evidence_path), json.loads(artifact["metadata_json"])["original_path"])
        self.assertEqual(1, evidence["is_final"])

    def test_local_smoke_generates_usability_report(self) -> None:
        for employee_id, runtime in [("main", "openclaw"), ("nestcar", "openclaw"), ("codex", "codex")]:
            code, created = run_cli("employee", "create", "--id", employee_id, "--name", employee_id, "--role", "agent", "--runtime", runtime, "--workspace", str(self.root / "workspace" / employee_id))
            self.assertEqual(0, code, created)
        calls = []

        def fake_run(cmd, cwd=None, text=None, capture_output=None, timeout=None):
            calls.append(cmd)

            class Result:
                returncode = 0
                stderr = ""

            result = Result()
            if "company-dashboard" in cmd[0]:
                output_path = Path(cmd[cmd.index("--output") + 1])
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text("<html>/v1/messages/direct</html>", encoding="utf-8")
                result.stdout = json.dumps({"ok": True, "output": str(output_path)})
            elif "attendance" in cmd:
                result.stdout = json.dumps({
                    "ok": True,
                    "counts": {"online": 2, "session_missing": 0, "worker_stalled": 0, "heartbeat_disabled": 0, "no_reply": 0},
                    "employees": [
                        {"agent": "nestcar", "status": "online"},
                        {"agent": "codex", "status": "online"},
                    ],
                    "evidence": {"json": str(self.root / "state" / "attendance" / "smoke.json")},
                })
            elif "direct" in cmd:
                target = cmd[cmd.index("--to") + 1]
                result.stdout = json.dumps({"ok": True, "target": target, "session_key": f"agent:{target}:main", "reply": f"{target}_OK", "file": str(self.root / "employees" / target / "inbox" / "smoke.json")})
            else:
                result.stdout = json.dumps({"ok": True})
            return result

        with mock.patch.object(company_local_smoke.company_service_smoke, "run_smoke", return_value={"ok": True}), mock.patch.object(company_local_smoke.subprocess, "run", side_effect=fake_run):
            report = company_local_smoke.run_local_smoke("nestcar,codex", "main", "nestcar,codex", 10)
        self.assertTrue(report["ok"], report)
        self.assertEqual(["nestcar", "codex"], [item["agent_id"] for item in report["direct_matrix"]])
        self.assertTrue(Path(report["evidence"]["latest"]).exists())
        self.assertTrue((self.root / "state" / "dashboard.html").exists())
        self.assertTrue(any("attendance" in cmd for cmd in calls))
        self.assertTrue(any("direct" in cmd for cmd in calls))

    def test_local_smoke_can_verify_skill_closed_loop_ledgers(self) -> None:
        calls = []

        def fake_run(cmd, cwd=None, text=None, capture_output=None, timeout=None):
            calls.append(cmd)

            class Result:
                returncode = 0
                stderr = ""

            result = Result()
            if "company-skill-package-worker" in cmd[0]:
                result.stdout = json.dumps({
                    "ok": True,
                    "status": "completed",
                    "task_id": "task-local-smoke-skill",
                    "attempt": {"attempt_id": "attempt-local-smoke-skill", "status": "success"},
                    "runtime_session": {"session_id": "skill-session-ecommerce-copy-skill-task-local-smoke-skill"},
                    "tool_call_id": "skill-tool-ecommerce-copy-skill-task-local-smoke-skill",
                    "artifact": {"artifact_id": "artifact-local-smoke-skill"},
                    "evidence": {"evidence_id": "evidence-local-smoke-skill", "path_or_url": "final/listing-summary.md"},
                })
            elif "task" in cmd and "show" in cmd:
                result.stdout = json.dumps({
                    "ok": True,
                    "task": {"id": "task-local-smoke-skill", "status": "completed"},
                    "completion_contract": {"valid": True, "final_evidence_count": 1},
                    "attempts": [{"attempt_id": "attempt-local-smoke-skill", "status": "success"}],
                    "runtime_sessions": [{"session_id": "skill-session-ecommerce-copy-skill-task-local-smoke-skill"}],
                    "tool_calls": [{"tool_call_id": "skill-tool-ecommerce-copy-skill-task-local-smoke-skill"}],
                    "budget_summary": {"event_count": 1, "total_amount": 10, "currency": "USD"},
                    "evidence_records": [{"evidence_id": "evidence-local-smoke-skill"}],
                    "handoffs": [{"handoff_id": "handoff-local-smoke-skill"}],
                })
            elif "trace" in cmd and "timeline" in cmd:
                result.stdout = json.dumps({"ok": True, "timeline": [{"kind": "tool_call"}, {"kind": "budget_event"}, {"kind": "evidence"}, {"kind": "handoff"}]})
            else:
                result.stdout = json.dumps({"ok": True, "task": {"id": "task-local-smoke-skill", "metadata": {"trace_id": "trace-local-smoke-skill"}}})
            return result

        with mock.patch.object(company_local_smoke.subprocess, "run", side_effect=fake_run):
            result = company_local_smoke.run_skill_closed_loop_smoke(source="main", agent="ecommerce-copy-skill", package="skill-packages/ecommerce-copy-demo/skill.json", timeout=30)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["task_id"].startswith("task-local-smoke-skill-"))
        self.assertEqual("completed", result["task_status"])
        self.assertEqual(1, result["counts"]["attempts"])
        self.assertEqual(1, result["counts"]["runtime_sessions"])
        self.assertEqual(1, result["counts"]["tool_calls"])
        self.assertEqual(1, result["counts"]["budget_events"])
        self.assertEqual(1, result["counts"]["evidence"])
        self.assertEqual(1, result["counts"]["handoffs"])
        self.assertTrue(result["completion_contract"]["valid"])
        self.assertEqual(["budget_event", "evidence", "handoff", "tool_call"], result["trace_kinds"])
        self.assertTrue(any("company-skill-package-worker" in cmd[0] for cmd in calls))

    def test_api_gateway_exposes_health_tasks_messages_and_heartbeats(self) -> None:
        status, descriptor = api_gateway.route_get("/v1", {})
        self.assertEqual(200, status, descriptor)
        self.assertIn("conversations", descriptor["capabilities"])
        self.assertIn("approvals", descriptor["capabilities"])
        self.assertIn("skills", descriptor["capabilities"])
        self.assertIn("sse_events", descriptor["capabilities"])
        self.assertIn("trace_file_flow", descriptor["capabilities"])
        self.assertTrue(descriptor["protocols"]["rest"])
        self.assertTrue(descriptor["protocols"]["json_rpc"])
        self.assertEqual("optional-grpcio", descriptor["protocols"]["grpc"])
        self.assertEqual("/rpc", descriptor["links"]["rpc"])
        self.assertFalse(descriptor["governance"]["direct_sqlite_writes"])
        self.assertTrue(descriptor["governance"]["high_risk_requires_approval"])
        status, openapi = api_gateway.route_get("/v1/openapi.json", {})
        self.assertEqual(200, status, openapi)
        self.assertEqual("3.1.0", openapi["openapi"])
        self.assertIn("/v1/tasks", openapi["paths"])
        self.assertIn("/v1/conversations/{conversation_id}/reply", openapi["paths"])
        self.assertIn("/v1/approvals/{approval_id}/approve", openapi["paths"])
        self.assertIn("/v1/approvals/{approval_id}/resolve", openapi["paths"])
        self.assertIn("/v1/evidence", openapi["paths"])
        evidence_query_names = {
            parameter["name"]
            for parameter in openapi["paths"]["/v1/evidence"]["get"]["parameters"]
        }
        self.assertIn("employee_id", evidence_query_names)
        self.assertIn("/v1/evidence/{evidence_id}/content", openapi["paths"])
        self.assertIn("/v1/evidence/{evidence_id}/safe-preview", openapi["paths"])
        self.assertIn("/v1/artifacts", openapi["paths"])
        self.assertIn("/v1/handoffs", openapi["paths"])
        self.assertIn("/v1/failures", openapi["paths"])
        doctor_query_names = {
            parameter["name"]
            for parameter in openapi["paths"]["/v1/doctor"]["get"]["parameters"]
        }
        self.assertIn("strict_launchd", doctor_query_names)
        self.assertIn("strict_openclaw", doctor_query_names)

        handler = object.__new__(api_gateway.ApiHandler)
        sent_headers = []
        handler.send_header = lambda key, value: sent_headers.append((key, value))  # type: ignore[method-assign]
        handler.send_cors_headers()
        self.assertIn(("Access-Control-Allow-Origin", "*"), sent_headers)
        self.assertIn(("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS"), sent_headers)

        handler = object.__new__(api_gateway.ApiHandler)
        handler.path = "/v1/tasks?limit=50"
        handler.wfile = io.BytesIO()
        handler.headers = {}
        sent = []
        handler.send_response = lambda code: sent.append(("status", code))
        handler.send_header = lambda name, value: sent.append((name, value))
        handler.end_headers = lambda: sent.append(("end", ""))
        handler.server = SimpleNamespace(quiet=True)
        with mock.patch.object(api_gateway, "route_get", side_effect=sqlite3.OperationalError("disk I/O error /Users/shift/.env")):
            handler.do_GET()
        output = handler.wfile.getvalue().decode("utf-8")
        self.assertIn(("status", HTTPStatus.INTERNAL_SERVER_ERROR), sent)
        self.assertIn('"ok": false', output)
        self.assertIn('"path": "/v1/tasks"', output)
        self.assertNotIn("/Users/shift", output)
        self.assertNotIn(".env", output)

        for agent in ["video-ops", "video-creator", "video-publisher", "codex", "openclaw-main", "hermes", "nestcar"]:
            status, heartbeat = api_gateway.route_post("/v1/heartbeats", {"agent": agent})
            self.assertEqual(201, status, heartbeat)
        daemon_state = self.root / "state" / "daemon" / "last-run.json"
        daemon_state.parent.mkdir(parents=True, exist_ok=True)
        daemon_state.write_text(json.dumps({"ok": True, "at": companyctl.now(), "results": []}, ensure_ascii=False), encoding="utf-8")

        openclaw_root = self.root / "openclaw"
        nestcar_spool = openclaw_root / "telegram" / "ingress-spool-nestcar"
        nestcar_spool.mkdir(parents=True, exist_ok=True)
        (nestcar_spool / "0000000784356111.json").write_text('{"updateId":784356111}', encoding="utf-8")
        status, non_strict_doctor = api_gateway.route_get("/v1/doctor", {})
        self.assertEqual(200, status, non_strict_doctor)
        self.assertEqual(0, non_strict_doctor["exit_code"])
        self.assertFalse(non_strict_doctor["openclaw_guard"]["ok"])
        status, strict_openclaw_doctor = api_gateway.route_get("/v1/doctor", {"strict_openclaw": ["true"]})
        self.assertEqual(400, status, strict_openclaw_doctor)
        self.assertEqual(1, strict_openclaw_doctor["exit_code"])
        self.assertIn("telegram_ingress_spool_backlog", strict_openclaw_doctor["issues"])
        (nestcar_spool / "0000000784356111.json").unlink()

        status, health = api_gateway.route_get("/v1/health", {})
        self.assertEqual(200, status, health)
        self.assertTrue(health["ok"])

        status, approval = api_gateway.route_post(
            "/v1/approvals",
            {
                "from": "openclaw-main",
                "action": "kernel_change",
                "reason": "health endpoint should stay reachable while approvals are pending",
                "approval_id": "approval-api-health-pending",
            },
        )
        self.assertEqual(201, status, approval)
        status, health_with_pending = api_gateway.route_get("/v1/health", {})
        self.assertEqual(200, status, health_with_pending)
        self.assertFalse(health_with_pending["ok"])
        self.assertEqual(1, health_with_pending["exit_code"])
        self.assertIn("pending_approvals", health_with_pending["issues"])

        status, submitted = api_gateway.route_post(
            "/v1/tasks",
            {
                "from": "openclaw-main",
                "to": "codex",
                "task_id": "task-api-gateway",
                "title": "API Gateway task",
                "description": "created through REST",
            },
        )
        self.assertEqual(201, status, submitted)
        self.assertEqual("task-api-gateway", submitted["task"]["id"])
        self.assertTrue(submitted["task"]["metadata"]["trace_id"].startswith("trace-"))

        status, shown = api_gateway.route_get("/v1/tasks/task-api-gateway", {})
        self.assertEqual(200, status, shown)
        self.assertEqual("task-api-gateway", shown["task"]["id"])

        evidence = self.root / "evidence" / "api-task-done.md"
        evidence.parent.mkdir(exist_ok=True)
        evidence.write_text("api task done evidence", encoding="utf-8")
        status, claimed = api_gateway.route_post("/v1/tasks/task-api-gateway/claim", {"agent": "codex", "lease_seconds": "60"})
        self.assertEqual(200, status, claimed)
        self.assertEqual("claimed", claimed["task"]["status"])
        status, done = api_gateway.route_post("/v1/tasks/task-api-gateway/done", {"agent": "codex", "summary": "completed through REST", "evidence": str(evidence)})
        self.assertEqual(200, status, done)
        self.assertEqual("completed", done["status"])

        status, submitted_block = api_gateway.route_post(
            "/v1/tasks",
            {
                "from": "openclaw-main",
                "to": "codex",
                "task_id": "task-api-gateway-block",
                "title": "API Gateway block task",
                "description": "blocked through REST",
            },
        )
        self.assertEqual(201, status, submitted_block)
        status, claimed_block = api_gateway.route_post("/v1/tasks/task-api-gateway-block/claim", {"agent": "codex"})
        self.assertEqual(200, status, claimed_block)
        status, blocked = api_gateway.route_post("/v1/tasks/task-api-gateway-block/block", {"agent": "codex", "blocker": "blocked through REST"})
        self.assertEqual(200, status, blocked)
        self.assertEqual("blocked", blocked["status"])
        status, reopened = api_gateway.route_post("/v1/tasks/task-api-gateway-block/reopen", {"by": "codex", "reason": "blocker cleared through REST"})
        self.assertEqual(200, status, reopened)
        self.assertEqual("submitted", reopened["task"]["status"])
        status, reassigned = api_gateway.route_post("/v1/tasks/task-api-gateway-block/reassign", {"by": "openclaw-main", "to": "hermes", "reason": "better owner through REST"})
        self.assertEqual(200, status, reassigned)
        self.assertEqual("hermes", reassigned["task"]["target_agent"])
        self.assertEqual("submitted", reassigned["task"]["status"])
        self.assertEqual("", reassigned["task"]["blocker"])

        status, task_conversation = api_gateway.route_post(
            "/v1/tasks/task-api-gateway-block/conversations",
            {
                "from": "openclaw-main",
                "participants": "codex,hermes",
                "conversation_id": "conv-api-task-gateway-block",
                "body": "discuss reassigned task",
            },
        )
        self.assertEqual(201, status, task_conversation)
        self.assertEqual("conv-api-task-gateway-block", task_conversation["conversation"]["id"])
        status, task_conversations = api_gateway.route_get("/v1/tasks/task-api-gateway-block/conversations", {})
        self.assertEqual(200, status, task_conversations)
        self.assertEqual(["conv-api-task-gateway-block"], task_conversations["conversation_ids"])
        self.assertEqual(["discuss reassigned task"], [message["body"] for message in task_conversations["conversations"][0]["messages"]])
        status, task_detail_with_conversation = api_gateway.route_get("/v1/tasks/task-api-gateway-block", {})
        self.assertEqual(200, status, task_detail_with_conversation)
        self.assertEqual(1, task_detail_with_conversation["conversation_summary"]["counts"]["conversations"])
        self.assertEqual(1, task_detail_with_conversation["conversation_summary"]["counts"]["messages"])
        self.assertEqual("conv-api-task-gateway-block", task_detail_with_conversation["conversation_summary"]["items"][0]["conversation_id"])
        self.assertEqual("discuss reassigned task", task_detail_with_conversation["conversation_summary"]["items"][0]["latest_message"])

        status, sent = api_gateway.route_post("/v1/messages", {"from": "hermes", "to": "codex", "body": "REST ping", "message_id": "msg-api-gateway"})
        self.assertEqual(201, status, sent)
        status, messages = api_gateway.route_get("/v1/messages", {"agent": ["codex"]})
        self.assertEqual(200, status, messages)
        self.assertIn("msg-api-gateway", [message["id"] for message in messages["messages"]])
        direct_calls = []

        def fake_direct_run(cmd, cwd=None, text=None, capture_output=None, timeout=None):
            direct_calls.append(cmd)

            class Result:
                returncode = 0
                stdout = json.dumps({"ok": True, "agent": "codex", "direct_message": True, "reply": "API_CODEX_DIRECT_OK"})
                stderr = ""

            return Result()

        with mock.patch.object(companyctl.subprocess, "run", side_effect=fake_direct_run):
            status, direct = api_gateway.route_post(
                "/v1/messages/direct",
                {
                    "from": "openclaw-main",
                    "to": "codex",
                    "body": "只回复：API_CODEX_DIRECT_OK",
                    "message_id": "msg-api-direct-codex",
                    "timeout": "60",
                },
            )
        self.assertEqual(201, status, direct)
        self.assertEqual("API_CODEX_DIRECT_OK", direct["reply"])
        self.assertEqual("agent:codex:openclaw-main", direct["session_key"])
        self.assertEqual("codex", direct["message"]["target_agent"])
        self.assertIn("company-codex-adapter", direct_calls[0][0])
        self.assertIn("--direct-message", direct_calls[0])
        self.assertIn("--direct-source", direct_calls[0])
        self.assertIn("--direct-session-key", direct_calls[0])

        followup_calls = []

        def fake_followup_run(cmd, cwd=None, text=None, capture_output=None, timeout=None):
            followup_calls.append(cmd)

            class Result:
                returncode = 0
                stdout = json.dumps({"result": {"payloads": [{"text": "FOLLOWUP_API_OK"}]}})
                stderr = ""

            return Result()

        with mock.patch.object(companyctl.subprocess, "run", side_effect=fake_followup_run):
            status, followup = api_gateway.route_post(
                "/v1/followups",
                {"from": "nestcar", "to": "openclaw-main", "question": "请提供还车里程", "followup_id": "followup-api-mileage"},
            )
            self.assertEqual(201, status, followup)
            status, followup_shown = api_gateway.route_get("/v1/followups/followup-api-mileage", {})
            self.assertEqual(200, status, followup_shown)
            status, followup_answered = api_gateway.route_post(
                "/v1/followups/followup-api-mileage/reply",
                {"by": "openclaw-main", "answer": "里程是 10234"},
            )
        self.assertEqual(200, status, followup_answered)
        self.assertEqual("answered", followup_answered["followup"]["status"])
        self.assertEqual("nestcar", followup_answered["delivery"]["target"])
        self.assertTrue(followup_calls)

    def test_api_gateway_evidence_content_uses_whitelist_and_hides_absolute_paths(self) -> None:
        safe_path = self.root / "evidence" / "safe-report.md"
        safe_path.parent.mkdir(parents=True, exist_ok=True)
        safe_path.write_text("safe evidence body\n", encoding="utf-8")
        secret_path = self.root / ".env"
        secret_path.write_text("API_KEY=secret\n", encoding="utf-8")
        with sqlite3.connect(self.root / "company.sqlite") as conn:
            conn.execute(
                """
                INSERT INTO evidence(evidence_id, trace_id, task_id, attempt_id, employee_id, artifact_id, type, path_or_url, summary, checksum, is_final, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("evidence-safe-content", "trace-safe-content", "task-safe-content", "attempt-safe-content", "codex", "", "text", str(safe_path), "safe summary", "", 1, "{}", companyctl.now()),
            )
            conn.execute(
                """
                INSERT INTO evidence(evidence_id, trace_id, task_id, attempt_id, employee_id, artifact_id, type, path_or_url, summary, checksum, is_final, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("evidence-secret-content", "trace-secret-content", "task-secret-content", "attempt-secret-content", "codex", "", "text", str(secret_path), "secret summary", "", 1, "{}", companyctl.now()),
            )
            conn.commit()

        status, safe = api_gateway.route_get("/v1/evidence/evidence-safe-content/content", {})
        self.assertEqual(200, status, safe)
        self.assertTrue(safe["display"]["allowed"])
        self.assertFalse(safe["display"]["absolute_path_exposed"])
        self.assertEqual("safe evidence body\n", safe["content"]["text"])
        self.assertIn("safe-report.md", safe["display"]["relative_path"])
        self.assertNotIn(str(self.root), json.dumps(safe, ensure_ascii=False))

        status, safe_alias = api_gateway.route_get("/v1/evidence/evidence-safe-content/safe-preview", {})
        self.assertEqual(200, status, safe_alias)
        self.assertTrue(safe_alias["display"]["allowed"])
        self.assertEqual("safe evidence body\n", safe_alias["content"]["text"])
        self.assertEqual(safe["display"]["relative_path"], safe_alias["display"]["relative_path"])
        self.assertNotIn(str(self.root), json.dumps(safe_alias, ensure_ascii=False))

        status, blocked = api_gateway.route_get("/v1/evidence/evidence-secret-content/content", {})
        self.assertEqual(403, status, blocked)
        self.assertFalse(blocked["display"]["allowed"])
        self.assertFalse(blocked["display"]["absolute_path_exposed"])
        self.assertEqual("", blocked["content"]["text"])
        self.assertNotIn("API_KEY", json.dumps(blocked, ensure_ascii=False))
        self.assertNotIn(str(secret_path), json.dumps(blocked, ensure_ascii=False))

        status, blocked_alias = api_gateway.route_get("/v1/evidence/evidence-secret-content/safe-preview", {})
        self.assertEqual(403, status, blocked_alias)
        self.assertFalse(blocked_alias["display"]["allowed"])
        self.assertEqual("", blocked_alias["content"]["text"])
        self.assertNotIn("API_KEY", json.dumps(blocked_alias, ensure_ascii=False))
        self.assertNotIn(str(secret_path), json.dumps(blocked_alias, ensure_ascii=False))

    def test_api_gateway_exposes_conversations_approvals_and_adapter_run_recovery(self) -> None:
        status, started = api_gateway.route_post(
            "/v1/conversations",
            {
                "from": "hermes",
                "participants": "hermes,codex,openclaw-main",
                "conversation_id": "conv-api-gateway",
                "title": "API Gateway discussion",
                "body": "please discuss",
            },
        )
        self.assertEqual(201, status, started)
        self.assertEqual("conv-api-gateway", started["conversation"]["id"])
        status, replied = api_gateway.route_post("/v1/conversations/conv-api-gateway/reply", {"from": "codex", "body": "ack", "message_id": "msg-api-conv-reply"})
        self.assertEqual(201, status, replied)
        status, conversations = api_gateway.route_get("/v1/conversations", {"agent": ["openclaw-main"]})
        self.assertEqual(200, status, conversations)
        self.assertIn("conv-api-gateway", [conversation["id"] for conversation in conversations["conversations"]])
        status, shown = api_gateway.route_get("/v1/conversations/conv-api-gateway", {})
        self.assertEqual(200, status, shown)
        self.assertEqual(["please discuss", "ack"], [message["body"] for message in shown["messages"]])

        status, approval = api_gateway.route_post(
            "/v1/approvals",
            {
                "from": "hermes",
                "action": "external_send",
                "reason": "customer send needs approval",
                "target": "nestcar",
                "risk": "P1",
                "approval_id": "approval-api-gateway",
                "task_id": "task-api-gateway-risk",
            },
        )
        self.assertEqual(201, status, approval)
        self.assertEqual("pending", approval["approval"]["status"])
        status, listed = api_gateway.route_get("/v1/approvals", {"status": ["pending"], "agent": ["hermes"]})
        self.assertEqual(200, status, listed)
        self.assertIn("approval-api-gateway", [item["id"] for item in listed["approvals"]])
        status, approved = api_gateway.route_post("/v1/approvals/approval-api-gateway/approve", {"by": "openclaw-main", "reason": "approved through API"})
        self.assertEqual(200, status, approved)
        self.assertEqual("approved", approved["approval"]["status"])
        status, shown_approval = api_gateway.route_get("/v1/approvals/approval-api-gateway", {})
        self.assertEqual(200, status, shown_approval)
        self.assertEqual("approved", shown_approval["approval"]["status"])

        status, mock_approval = api_gateway.route_post(
            "/v1/approvals",
            {
                "from": "hermes",
                "action": "external_send",
                "reason": "mock customer send",
                "target": "nestcar",
                "risk": "P1",
                "approval_id": "approval-api-gateway-mock",
                "task_id": "task-api-gateway-mock",
            },
        )
        self.assertEqual(201, status, mock_approval)
        status, resolved = api_gateway.route_post(
            "/v1/approvals/approval-api-gateway-mock/resolve",
            {"by": "openclaw-main", "reason": "mock resolved through API", "mock": True},
        )
        self.assertEqual(200, status, resolved)
        self.assertEqual("resolved", resolved["approval"]["status"])
        self.assertTrue(resolved["approval"]["detail"]["mock_resolve"])
        self.assertTrue(resolved["approval"]["detail"]["dry_run"])
        self.assertFalse(resolved["approval"]["detail"]["external_send_executed"])
        self.assertEqual("approval.resolved", resolved["event"]["event_type"])
        status, resolved_list = api_gateway.route_get("/v1/approvals", {"status": ["resolved"], "agent": ["hermes"]})
        self.assertEqual(200, status, resolved_list)
        self.assertIn("approval-api-gateway-mock", [item["id"] for item in resolved_list["approvals"]])

        code, submitted = run_cli("task", "submit", "--from", "openclaw-main", "--to", "codex", "--task-id", "task-api-adapter-retry", "--title", "adapter retry")
        self.assertEqual(code, 0, submitted)
        code, blocked = run_cli("task", "block", "--agent", "codex", "--task-id", "task-api-adapter-retry", "--blocker", "adapter failed")
        self.assertEqual(code, 0, blocked)
        secret = "sk-testSECRET1234567890"
        adapter_result = {
            "ok": False,
            "agent": "codex",
            "command": "company-codex-adapter",
            "stdout": f"api_key={secret} reading /Users/shift/.ssh/id_rsa",
            "stderr": f"token={secret} blocked on {self.root / '.env'}",
            "runs": [
                {
                    "result": {"stdout": f"authorization={secret}", "stderr": "/Users/shift/project/profile.json"},
                    "parsed_stdout": {"task_id": "task-api-adapter-retry", "summary": "safe progress context"},
                }
            ],
        }
        conn = companyctl.connect()
        try:
            conn.execute(
                """
                INSERT INTO adapter_runs(id, trace_id, agent_id, task_id, command, ok, processed, attempt, next_retry_at, result_json, created_at)
                VALUES ('adapter-run-api-retry', ?, 'codex', 'task-api-adapter-retry', 'company-codex-adapter', 0, 1, 1, '2000-01-01T00:00:00+00:00', ?, ?)
                """,
                (submitted["task"]["metadata"]["trace_id"], json.dumps(adapter_result), companyctl.now()),
            )
            conn.commit()
        finally:
            conn.close()

        status, run = api_gateway.route_get("/v1/adapter-runs/adapter-run-api-retry", {"summary": ["true"]})
        self.assertEqual(200, status, run)
        self.assertEqual("adapter-run-api-retry", run["adapter_run"]["id"])
        result_summary_json = json.dumps(run["result_summary"], ensure_ascii=False)
        self.assertIn("sanitized_log", run["result_summary"])
        self.assertIn("safe progress context", result_summary_json)
        self.assertNotIn(secret, result_summary_json)
        self.assertNotIn("id_rsa", result_summary_json)
        self.assertNotIn(".env", result_summary_json)
        self.assertNotIn("profile.json", result_summary_json)
        status, listed_runs = api_gateway.route_get("/v1/adapter-runs", {"limit": ["5"]})
        self.assertEqual(200, status, listed_runs)
        listed_run = next(item for item in listed_runs["adapter_runs"] if item["id"] == "adapter-run-api-retry")
        listed_run_json = json.dumps(listed_run, ensure_ascii=False)
        self.assertIn("sanitized_log", listed_run)
        self.assertNotIn("result_json", listed_run)
        self.assertNotIn(secret, listed_run_json)
        self.assertNotIn("id_rsa", listed_run_json)
        status, task_detail = api_gateway.route_get("/v1/tasks/task-api-adapter-retry", {})
        self.assertEqual(200, status, task_detail)
        detail_json = json.dumps(task_detail, ensure_ascii=False)
        self.assertIn("sanitized_logs", task_detail)
        self.assertIn("adapter-run-api-retry", [item["run_id"] for item in task_detail["sanitized_logs"]])
        log_item = next(item for item in task_detail["sanitized_logs"] if item["run_id"] == "adapter-run-api-retry")
        self.assertFalse(log_item["raw_available"])
        self.assertEqual("sanitized_only", log_item["log_policy"]["mode"])
        self.assertIn("stdout", log_item["log_policy"]["source_fields"])
        self.assertIn("stderr", log_item["log_policy"]["source_fields"])
        self.assertIn("raw stdout/stderr hidden", log_item["log_policy"]["summary"])
        self.assertIn("safe progress context", detail_json)
        self.assertNotIn(secret, detail_json)
        self.assertNotIn("id_rsa", detail_json)
        self.assertNotIn(".env", detail_json)
        self.assertNotIn("profile.json", detail_json)
        status, retry = api_gateway.route_post("/v1/adapter-runs/adapter-run-api-retry/retry", {"by": "openclaw-main", "reason": "retry through API"})
        self.assertEqual(200, status, retry)
        self.assertEqual("task-api-adapter-retry", retry["task_id"])
        self.assertEqual("submitted", retry["status"])
        status, acked = api_gateway.route_post("/v1/adapter-runs/adapter-run-api-retry/ack", {"by": "openclaw-main", "reason": "ack through API"})
        self.assertEqual(200, status, acked)
        self.assertEqual("openclaw-main", acked["adapter_run"]["acknowledged_by"])
        status, failed = api_gateway.route_get("/v1/adapter-runs", {"status": ["failed"], "unacknowledged_only": ["true"]})
        self.assertEqual(200, status, failed)
        self.assertNotIn("adapter-run-api-retry", [item["id"] for item in failed["adapter_runs"]])

        status, events = api_gateway.route_get("/v1/events", {"limit": ["5"]})
        self.assertEqual(200, status, events)
        self.assertGreaterEqual(len(events["events"]), 1)
        self.assertIn("event_type", events["events"][0])
        self.assertIn("source_agent", events["events"][0])
        self.assertIn("processed_at", events["events"][0])
        conn = companyctl.connect()
        try:
            companyctl.record_event(
                conn,
                "artifact.created",
                "codex",
                task_id="task-api-event-sanitize",
                payload={
                    "path": str(self.root / ".ssh" / "id_rsa"),
                    "message": "api_key=sk-test-secret and /Users/shift/project/.env",
                },
                trace_id="trace-api-event-sanitize",
            )
        finally:
            conn.close()
        status, sanitized_events = api_gateway.route_get("/v1/events", {"limit": ["1"]})
        self.assertEqual(200, status, sanitized_events)
        event_json = json.dumps(sanitized_events, ensure_ascii=False)
        self.assertNotIn("/Users/shift", event_json)
        self.assertNotIn("id_rsa", event_json)
        self.assertNotIn(".env", event_json)
        self.assertNotIn("sk-test-secret", event_json)
        self.assertIn("payload", sanitized_events["events"][0])

    def test_api_gateway_exposes_communication_observability_summary(self) -> None:
        code, handshake = run_cli("message", "send", "--from", "openclaw-main", "--to", "codex", "--body", "handshake")
        self.assertEqual(code, 0, handshake)
        code, sent_a = run_cli("message", "send", "--from", "openclaw-main", "--to", "codex", "--body", "请确认 task-api-observability progress evidence")
        self.assertEqual(code, 0, sent_a)
        code, sent_b = run_cli("message", "send", "--from", "codex", "--to", "openclaw-main", "--body", "已同步 external mirror")
        self.assertEqual(code, 0, sent_b)
        status, direct = api_gateway.route_get("/v1/messages/recent-direct", {"limit": ["3"]})
        self.assertEqual(200, status, direct)
        self.assertEqual(3, len(direct["direct_messages_recent"]))
        task_message = next(item for item in direct["direct_messages_recent"] if "task-api-observability" in item["body"])
        self.assertEqual("task-api-observability", task_message["task_context"])
        self.assertTrue(task_message["task_bound"])
        self.assertFalse(task_message["low_signal"])
        handshake_message = next(item for item in direct["direct_messages_recent"] if item["body"] == "handshake")
        self.assertFalse(handshake_message["task_bound"])
        self.assertTrue(handshake_message["low_signal"])
        self.assertEqual("handshake_or_idle", handshake_message["chat_classification"])

        imported_at = companyctl.now()
        status, imported = api_gateway.route_post(
            "/v1/external-mirror/import",
            {
                "thread": {
                    "id": "tg-owner-codex",
                    "platform": "telegram",
                    "owner_agent": "openclaw-main",
                    "bridge_agent": "codex",
                    "external_user_id": "tg-user-42",
                    "external_title": "Shift Telegram Mirror",
                    "last_message_at": imported_at,
                    "cursor": "cursor-001",
                    "metadata": {"source": "telegram", "mirror_kind": "operator"},
                },
                "messages": [
                    {
                        "id": "ext-msg-001",
                        "thread_id": "tg-owner-codex",
                        "direction": "inbound",
                        "agent_id": "openclaw-main",
                        "external_message_id": "telegram-1001",
                        "body": "外部消息已进入 mirror",
                        "created_at": imported_at,
                        "metadata": {"import_batch": "batch-1"},
                    }
                ],
            },
        )
        self.assertEqual(201, status, imported)

        code, submitted = run_cli("task", "submit", "--from", "openclaw-main", "--to", "codex", "--task-id", "task-api-observability", "--title", "communication observability")
        self.assertEqual(code, 0, submitted)
        trace_id = submitted["task"]["metadata"]["trace_id"]
        conn = companyctl.connect()
        try:
            conn.execute(
                """
                INSERT INTO adapter_runs(id, trace_id, agent_id, task_id, command, ok, processed, attempt, next_retry_at, result_json, created_at)
                VALUES ('adapter-run-observability', ?, 'codex', 'task-api-observability', 'company-codex-adapter', 1, 1, 2, '', ?, ?)
                """,
                (
                    trace_id,
                    json.dumps(
                        {
                            "state_file": str(self.root / "state" / "daemon" / "workers" / "codex.json"),
                            "runs": [
                                {
                                    "task_id": "task-api-observability",
                                    "parsed_stdout": {"task_id": "task-api-observability", "progress_file": "reports/adapter-progress.json"},
                                }
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    imported_at,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        status, payload = api_gateway.route_get("/v1/dashboard/communication-observability", {})
        self.assertEqual(200, status, payload)
        self.assertTrue(payload["ok"])
        direct_bodies = [item["body"] for item in payload["direct_messages"]["items"]]
        self.assertIn("已同步 external mirror", direct_bodies)
        self.assertIn("请确认 task-api-observability progress evidence", direct_bodies)
        self.assertGreaterEqual(payload["direct_messages"]["counts"]["total"], 2)
        self.assertEqual(1, payload["direct_messages"]["counts"]["task_bound"])
        self.assertEqual(1, payload["direct_messages"]["counts"]["handshake_or_idle"])
        self.assertGreaterEqual(payload["direct_messages"]["counts"]["work_relevant"], 1)
        status, cockpit = api_gateway.route_get("/v1/dashboard/cockpit", {})
        self.assertEqual(200, status, cockpit)
        self.assertEqual(payload["direct_messages"]["counts"]["task_bound"], cockpit["counts"]["chat_task_bound"])
        self.assertEqual(payload["direct_messages"]["counts"]["handshake_or_idle"], cockpit["counts"]["chat_handshake_or_idle"])
        self.assertGreaterEqual(cockpit["counts"]["chat_work_relevant"], 1)
        self.assertEqual(1, payload["external_mirror"]["counts"]["threads"])
        self.assertEqual("telegram", payload["external_mirror"]["threads"][0]["platform"])
        self.assertEqual("codex", payload["external_mirror"]["threads"][0]["bridge_agent"])
        self.assertEqual(1, payload["adapter_runs"]["counts"]["total"])
        self.assertEqual("reports/adapter-progress.json", payload["adapter_runs"]["items"][0]["progress_file"])
        self.assertIn("internal_watchdog", payload)
        self.assertEqual(1, payload["internal_watchdog"]["counts"]["open_tasks"])

    def test_api_gateway_exposes_live_telemetry_traces(self) -> None:
        conn = companyctl.connect()
        try:
            conn.execute(
                """
                INSERT INTO adapter_runs(id, trace_id, agent_id, task_id, command, ok, processed, attempt, next_retry_at, result_json, created_at)
                VALUES ('adapter-run-live-telemetry', 'trace-live-telemetry', 'codex', 'task-live-telemetry', 'company-codex-adapter', 1, 1, 1, '', '{}', ?)
                """,
                (companyctl.now(),),
            )
            conn.commit()
        finally:
            conn.close()
        status, payload = api_gateway.route_get("/v1/telemetry/traces", {"limit": ["20"]})
        self.assertEqual(HTTPStatus.OK, status, payload)
        self.assertTrue(payload["ok"])
        self.assertIn("trace-live-telemetry", [trace["trace_id"] for trace in payload["traces"]])

    def test_api_gateway_exposes_openclaw_runtime_inventory(self) -> None:
        (self.root / "openclaw" / "agents" / "market-agent" / "sessions").mkdir(parents=True)
        (self.root / "openclaw" / "agents" / "market-agent" / "sessions" / "sessions.json").write_text('{"s1": {}}', encoding="utf-8")
        spool = self.root / "openclaw" / "telegram" / "ingress-spool-market_agent"
        spool.mkdir(parents=True)
        status, payload = api_gateway.route_get("/v1/openclaw/runtime-inventory", {})
        self.assertEqual(HTTPStatus.OK, status, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(1, payload["agent_dirs"]["market-agent"]["session_count"])
        self.assertEqual(1, payload["counts"]["telegram_spools"])
        self.assertIn("market_agent", payload["missing_registered"])
        self.assertNotIn("market-agent", payload["missing_registered"])
        self.assertEqual(1, payload["counts"]["missing_registered"])

    def test_employee_sync_openclaw_runtime_registers_config_agents_and_runtime_candidates(self) -> None:
        config = self.root / "openclaw" / "openclaw.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            json.dumps(
                {
                    "agents": {
                        "list": [
                            {"id": "main", "name": "main", "workspace": str(self.root / "workspace-xmanx")},
                            {"id": "nestcar", "name": "car-rental", "workspace": str(self.root / "workspace-nestcar")},
                        ]
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        for agent_id in ("main", "nestcar", "runtime-only"):
            sessions = self.root / "openclaw" / "agents" / agent_id / "sessions"
            sessions.mkdir(parents=True, exist_ok=True)
            (sessions / "sessions.json").write_text('{"s1": {}}', encoding="utf-8")

        code, synced = run_cli("employee", "sync-openclaw-runtime", "--config", str(config))
        self.assertEqual(0, code, synced)
        self.assertEqual(2, synced["counts"]["active"])
        self.assertEqual(1, synced["counts"]["candidate"])

        conn = companyctl.connect_readonly()
        try:
            employees = {row["id"]: dict(row) for row in conn.execute("SELECT * FROM employees WHERE id IN ('main', 'nestcar', 'runtime-only')")}
        finally:
            conn.close()
        self.assertEqual("active", employees["main"]["status"])
        self.assertEqual("active", employees["nestcar"]["status"])
        self.assertEqual("candidate", employees["runtime-only"]["status"])
        self.assertEqual("openclaw", employees["nestcar"]["runtime"])

        status, payload = api_gateway.route_get("/v1/openclaw/runtime-inventory", {})
        self.assertEqual(HTTPStatus.OK, status, payload)
        self.assertEqual(0, payload["counts"]["missing_registered"])
        self.assertTrue(payload["agent_dirs"]["runtime-only"]["registered"])

    def test_employee_sync_openclaw_heartbeats_marks_active_runtime_agents_seen(self) -> None:
        config = self.root / "openclaw" / "openclaw.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            json.dumps({"agents": {"list": [{"id": "nestcar", "name": "car-rental", "workspace": str(self.root / "workspace-nestcar")}]}}),
            encoding="utf-8",
        )
        sessions = self.root / "openclaw" / "agents" / "nestcar" / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / "sessions.json").write_text('{"s1": {}}', encoding="utf-8")
        spool = self.root / "openclaw" / "telegram" / "ingress-spool-nestcar"
        spool.mkdir(parents=True, exist_ok=True)

        code, synced = run_cli("employee", "sync-openclaw-runtime", "--config", str(config))
        self.assertEqual(0, code, synced)
        code, heartbeat = run_cli("employee", "sync-openclaw-heartbeats")
        self.assertEqual(0, code, heartbeat)
        self.assertEqual(1, heartbeat["counts"]["synced"])

        conn = companyctl.connect_readonly()
        try:
            row = conn.execute("SELECT metadata_json FROM heartbeats WHERE agent_id = 'nestcar'").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        metadata = json.loads(row["metadata_json"])
        self.assertEqual("openclaw-runtime-sync", metadata["source"])
        self.assertEqual(1, metadata["session_count"])

    def test_api_gateway_exposes_internal_watchdog_for_no_receipt_messages_and_open_tasks(self) -> None:
        code, sent = run_cli("message", "send", "--from", "openclaw-main", "--to", "nestcar", "--body", "请处理内部任务但没有回执")
        self.assertEqual(0, code, sent)
        code, unrelated = run_cli("message", "send", "--from", "nestcar", "--to", "openclaw-main", "--body", "这是一条无关回复，不算 receipt")
        self.assertEqual(0, code, unrelated)
        code, submitted = run_cli("task", "submit", "--from", "openclaw-main", "--to", "nestcar", "--task-id", "task-watchdog-open", "--title", "watchdog should flag open task")
        self.assertEqual(0, code, submitted)

        status, payload = api_gateway.route_get("/v1/dashboard/internal-watchdog", {})
        self.assertEqual(200, status, payload)
        watchdog = payload["internal_watchdog"]
        self.assertGreaterEqual(watchdog["counts"]["no_receipt_messages"], 1)
        self.assertEqual(1, watchdog["counts"]["open_tasks"])
        self.assertEqual("no_receipt", watchdog["no_receipt_messages"][0]["status"])
        self.assertEqual("task-watchdog-open", watchdog["open_tasks"][0]["id"])
        self.assertEqual("unclaimed", watchdog["open_tasks"][0]["watchdog_status"])

        status, dry = api_gateway.route_post("/v1/dashboard/internal-watchdog/remediate", {"source": "openclaw-main", "dry_run": True, "escalate_existing": False})
        self.assertEqual(200, status, dry)
        self.assertTrue(dry["dry_run"])
        self.assertEqual(watchdog["counts"]["no_receipt_messages"] + watchdog["counts"]["open_tasks"], dry["actions_planned"])
        self.assertEqual(0, dry["actions_created"])

        status, applied = api_gateway.route_post("/v1/dashboard/internal-watchdog/remediate", {"source": "openclaw-main", "dry_run": False, "escalate_existing": False})
        self.assertEqual(200, status, applied)
        self.assertFalse(applied["dry_run"])
        self.assertEqual(dry["actions_planned"], applied["actions_planned"])
        self.assertGreaterEqual(applied["actions_created"], 1)
        self.assertLessEqual(applied["actions_created"], applied["actions_planned"])
        self.assertTrue((companyctl.followup_paths("pending") / f"remediate-no-receipt-{sent['message']['id']}.json").exists())
        self.assertTrue((companyctl.followup_paths("pending") / "remediate-open-task-task-watchdog-open.json").exists())

        status, escalated = api_gateway.route_post("/v1/dashboard/internal-watchdog/remediate", {"source": "openclaw-main", "dry_run": False, "escalate_to": "hermes", "reroute_to": "codex"})
        self.assertEqual(200, status, escalated)
        self.assertGreaterEqual(escalated["escalations_planned"], 1)
        self.assertLessEqual(escalated["escalations_planned"], escalated["actions_planned"])
        self.assertGreaterEqual(escalated["escalations_created"], 1)
        self.assertLessEqual(escalated["escalations_created"], escalated["escalations_planned"])
        self.assertGreaterEqual(escalated["reroutes_planned"], 1)
        self.assertLessEqual(escalated["reroutes_planned"], escalated["actions_planned"])
        self.assertGreaterEqual(escalated["reroutes_created"], 1)
        self.assertLessEqual(escalated["reroutes_created"], escalated["reroutes_planned"])
        self.assertTrue((companyctl.followup_paths("pending") / f"escalate-remediate-no-receipt-{sent['message']['id']}.json").exists())
        self.assertTrue((companyctl.followup_paths("pending") / "escalate-remediate-open-task-task-watchdog-open.json").exists())
        self.assertTrue((companyctl.followup_paths("pending") / f"reroute-remediate-no-receipt-{sent['message']['id']}.json").exists())
        self.assertTrue(company_dashboard.remediation_followup_exists("reroute-remediate-open-task-task-watchdog-open"))
        reroute_open_path = next(path for status in ("pending", "answered", "cancelled") for path in [companyctl.followup_paths(status) / "reroute-remediate-open-task-task-watchdog-open.json"] if path.exists())
        self.assertIn("candidate_new_owner: codex", reroute_open_path.read_text(encoding="utf-8"))

        reroute_path = next(path for status in ("pending", "answered", "cancelled") for path in [companyctl.followup_paths(status) / "reroute-remediate-open-task-task-watchdog-open.json"] if path.exists())
        reroute_followup = json.loads(reroute_path.read_text(encoding="utf-8"))
        reroute_path.unlink()
        reroute_followup["status"] = "answered"
        reroute_followup["answer"] = "decision: reroute\nnew_owner: codex\nreason: target stalled\nevidence_path: state/watchdog.json\nnext_action: create rerouted task\nrollback: close rerouted task"
        reroute_followup["answered_at"] = companyctl.now()
        companyctl.save_followup(reroute_followup, "answered")

        status, reroute_dry = api_gateway.route_post("/v1/dashboard/internal-watchdog/apply-reroutes", {"by": "hermes", "dry_run": True})
        self.assertEqual(200, status, reroute_dry)
        self.assertEqual(1, reroute_dry["actions_planned"])
        self.assertEqual(0, reroute_dry["reroutes_applied"])

        status, reroute_apply = api_gateway.route_post("/v1/dashboard/internal-watchdog/apply-reroutes", {"by": "hermes", "dry_run": False})
        self.assertEqual(200, status, reroute_apply)
        self.assertEqual(1, reroute_apply["reroutes_applied"])
        code, new_task = run_cli("task", "show", "--task-id", "rerouted-task-watchdog-open")
        self.assertEqual(0, code, new_task)
        self.assertEqual("codex", new_task["task"]["target_agent"])
        code, original_task = run_cli("task", "show", "--task-id", "task-watchdog-open")
        self.assertEqual(0, code, original_task)
        self.assertEqual("blocked", original_task["task"]["status"])
    def test_advanced_dashboard_renders_communication_observability_panels(self) -> None:
        code, sent = run_cli("message", "send", "--from", "openclaw-main", "--to", "codex", "--body", "Dashboard should show this direct message")
        self.assertEqual(code, 0, sent)
        imported_at = companyctl.now()
        status, imported = api_gateway.route_post(
            "/v1/external-mirror/import",
            {
                "thread": {
                    "id": "tg-dashboard-observability",
                    "platform": "telegram",
                    "owner_agent": "openclaw-main",
                    "bridge_agent": "cursor",
                    "external_user_id": "tg-dashboard-user",
                    "external_title": "Dashboard Mirror",
                    "last_message_at": imported_at,
                    "cursor": "cursor-dashboard",
                    "metadata": {"source": "telegram"},
                },
                "messages": [
                    {
                        "id": "ext-dashboard-001",
                        "thread_id": "tg-dashboard-observability",
                        "direction": "outbound",
                        "agent_id": "codex",
                        "external_message_id": "telegram-2002",
                        "body": "mirror reply delivered",
                        "created_at": imported_at,
                        "metadata": {"sync": "ok"},
                    }
                ],
            },
        )
        self.assertEqual(201, status, imported)
        conn = companyctl.connect()
        try:
            conn.execute(
                """
                INSERT INTO adapter_runs(id, trace_id, agent_id, task_id, command, ok, processed, attempt, next_retry_at, result_json, created_at)
                VALUES ('adapter-run-dashboard-observability', '', 'codex', 'task-dashboard-observability', 'company-codex-adapter', 1, 1, 1, '', ?, ?)
                """,
                (
                    json.dumps(
                        {
                            "state_file": str(self.root / "state" / "daemon" / "workers" / "codex.json"),
                            "runs": [
                                {
                                    "task_id": "task-dashboard-observability",
                                    "parsed_stdout": {"progress_file": "reports/dashboard-progress.json", "summary": "ok"},
                                }
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    imported_at,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        output = self.root / "state" / "dashboard-communication-observability.html"
        with contextlib.redirect_stdout(io.StringIO()):
            code = company_dashboard.main(["--output", str(output), "--variant", "advanced"])
        self.assertEqual(0, code)
        html = output.read_text(encoding="utf-8")
        self.assertIn("Communication Observatory", html)
        self.assertIn("External Mirror Sync", html)
        self.assertIn("Adapter Run Summary", html)
        self.assertIn("Internal Receipt Watchdog", html)
        conn = companyctl.connect()
        try:
            summary = company_dashboard.load_summary(conn)
            observability = company_dashboard.communication_observability_summary(summary)
        finally:
            conn.close()
        self.assertIn("Dashboard should show this direct message", [item["body"] for item in observability["direct_messages"]["items"]])
        self.assertIn("tg-dashboard-observability", [item["id"] for item in observability["external_mirror"]["threads"]])
        self.assertIn("reports/dashboard-progress.json", json.dumps(observability, ensure_ascii=False))
        self.assertNotIn("Dashboard should show this direct message", html)
        self.assertNotIn("tg-dashboard-observability", html)
        self.assertNotIn("reports/dashboard-progress.json", html)

    def test_advanced_dashboard_renders_supervisor_loop_panel(self) -> None:
        code, created = run_cli("employee", "create", "--id", "codex-dashboard-supervisor", "--name", "codex-dashboard-supervisor", "--role", "engineer", "--runtime", "codex", "--workspace", str(self.root / "workspace" / "codex-dashboard-supervisor"))
        self.assertEqual(0, code, created)

        conn = companyctl.connect()
        try:
            conn.execute("UPDATE employees SET status = 'active', updated_at = ? WHERE id = ?", (companyctl.now(), "codex-dashboard-supervisor"))
            companyctl.heartbeat_internal(conn, "codex-dashboard-supervisor", {"source": "unit-test", "progress": {"state": "actively_progressing", "summary": "处理中"}})
            companyctl.heartbeat_internal(conn, "codex-dashboard-supervisor", {"source": "unit-test", "progress": {"state": "verified_complete", "summary": "已完成"}})
            companyctl.run_supervisor_delivery_loop(conn, limit=10, actor="hermes")
        finally:
            conn.close()

        output = self.root / "state" / "dashboard-supervisor-loop.html"
        with contextlib.redirect_stdout(io.StringIO()):
            code = company_dashboard.main(["--output", str(output), "--variant", "advanced"])
        self.assertEqual(0, code)
        html = output.read_text(encoding="utf-8")
        self.assertIn("Autonomous Supervisor Loop", html)
        self.assertIn("latest supervisor loop result", html)
        self.assertIn("retry_ready", html)

    def test_api_gateway_exposes_project_governance(self) -> None:
        status, project = api_gateway.route_post(
            "/v1/projects",
            {
                "project_id": "project-api-gateway",
                "title": "API Gateway Project",
                "goal": "verify REST project governance",
                "owner": "openclaw-main",
                "acceptance": "plan tracked;task linked",
            },
        )
        self.assertEqual(201, status, project)
        self.assertEqual("project-api-gateway", project["project"]["id"])
        self.assertEqual(["plan tracked", "task linked"], project["project"]["acceptance"])

        status, projects = api_gateway.route_get("/v1/projects", {"status": ["active"]})
        self.assertEqual(200, status, projects)
        self.assertIn("project-api-gateway", [item["id"] for item in projects["projects"]])

        status, submitted = api_gateway.route_post(
            "/v1/tasks",
            {"from": "openclaw-main", "to": "codex", "task_id": "task-api-project", "title": "project task", "description": "link through REST"},
        )
        self.assertEqual(201, status, submitted)
        status, linked = api_gateway.route_post("/v1/projects/project-api-gateway/tasks", {"task_id": "task-api-project"})
        self.assertEqual(200, status, linked)
        self.assertEqual("task-api-project", linked["task_id"])

        status, plan = api_gateway.route_post(
            "/v1/projects/project-api-gateway/plan-items",
            {"plan_id": "plan-api-project", "title": "Ship REST project API", "owner": "codex", "task_id": "task-api-project"},
        )
        self.assertEqual(201, status, plan)
        self.assertEqual("plan-api-project", plan["plan_item"]["id"])
        status, plan_status = api_gateway.route_post("/v1/projects/project-api-gateway/plan-items/plan-api-project/status", {"status": "done"})
        self.assertEqual(200, status, plan_status)
        self.assertEqual("done", plan_status["plan_item"]["status"])
        status, project_status = api_gateway.route_post("/v1/projects/project-api-gateway/status", {"status": "completed"})
        self.assertEqual(400, status, project_status)
        self.assertEqual("project is not ready to complete; use project accept after review passes", project_status["error"])
        self.assertIn("review", project_status)
        status, review = api_gateway.route_get("/v1/projects/project-api-gateway/review", {})
        self.assertEqual(200, status, review)
        self.assertEqual("project-api-gateway", review["review"]["project_id"])
        self.assertIn("ready_to_complete", review["review"])
        status, accepted = api_gateway.route_post("/v1/projects/project-api-gateway/accept", {"by": "openclaw-main", "summary": "accepted through REST", "force": "true"})
        self.assertEqual(200, status, accepted)
        self.assertEqual("project-api-gateway", accepted["acceptance"]["project_id"])
        self.assertTrue(accepted["acceptance"]["force"])
        self.assertTrue(Path(accepted["file"]).exists())

        status, shown = api_gateway.route_get("/v1/projects/project-api-gateway", {})
        self.assertEqual(200, status, shown)
        self.assertEqual("completed", shown["project"]["status"])
        self.assertEqual(["task-api-project"], [task["id"] for task in shown["tasks"]])
        self.assertEqual(["plan-api-project"], [item["id"] for item in shown["plan_items"]])

    def test_api_gateway_exposes_locks(self) -> None:
        status, acquired = api_gateway.route_post("/v1/locks/acquire", {"agent": "codex", "resource": "task:api-lock", "lease_seconds": "60"})
        self.assertEqual(201, status, acquired)
        self.assertEqual("task:api-lock", acquired["lock"]["resource_key"])
        self.assertEqual("codex", acquired["lock"]["owner_agent"])
        status, locks = api_gateway.route_get("/v1/locks", {"agent": ["codex"]})
        self.assertEqual(200, status, locks)
        self.assertIn("task:api-lock", [lock["resource_key"] for lock in locks["locks"]])
        status, released = api_gateway.route_post("/v1/locks/release", {"agent": "codex", "resource": "task:api-lock"})
        self.assertEqual(200, status, released)
        self.assertTrue(released["released"])

        status, stale_lock = api_gateway.route_post("/v1/locks/acquire", {"agent": "codex", "resource": "task:api-stale-lock", "lease_seconds": "0"})
        self.assertEqual(201, status, stale_lock)
        status, unlocked = api_gateway.route_post("/v1/locks/unlock-stale", {})
        self.assertEqual(200, status, unlocked)
        self.assertIn("task:api-stale-lock", [lock["resource_key"] for lock in unlocked["unlocked"]])

    def test_api_gateway_exposes_employee_and_runtime_management(self) -> None:
        status, runtime = api_gateway.route_post("/v1/runtimes", {"runtime": "cursor", "command": "cursor-agent", "notes": "Cursor adapter placeholder"})
        self.assertEqual(201, status, runtime)
        self.assertEqual("cursor", runtime["runtime"]["runtime"])
        status, runtimes = api_gateway.route_get("/v1/runtimes", {})
        self.assertEqual(200, status, runtimes)
        self.assertIn("cursor", [item["runtime"] for item in runtimes["runtimes"]])

        workspace = self.root / "workspace" / "cursor-dev"
        status, employee = api_gateway.route_post(
            "/v1/employees",
            {"id": "cursor-dev", "name": "Cursor Dev", "role": "developer", "runtime": "cursor", "workspace": str(workspace)},
        )
        self.assertEqual(201, status, employee)
        self.assertEqual("cursor-dev", employee["employee"]["id"])
        conn = companyctl.connect()
        try:
            conn.execute("UPDATE employees SET status = 'active' WHERE id = 'cursor-dev'")
            conn.commit()
        finally:
            conn.close()
        status, employees = api_gateway.route_get("/v1/employees", {})
        self.assertEqual(200, status, employees)
        self.assertIn("cursor-dev", [item["id"] for item in employees["employees"]])
        self.assertIn("heartbeat_status", employees["employees"][0])
        self.assertIn("last_seen_at", employees["employees"][0])
        self.assertIn("kernel_state", employees["employees"][0])
        status, shown = api_gateway.route_get("/v1/employees/cursor-dev", {})
        self.assertEqual(200, status, shown)
        self.assertEqual("cursor", shown["employee"]["runtime"])
        self.assertTrue(Path(shown["files"]["profile"]).exists())
        self.assertTrue(Path(shown["files"]["capabilities"]).exists())
        self.assertFalse(shown["permissions"]["can_modify_kernel"])

        status, capabilities = api_gateway.route_post(
            "/v1/employees/cursor-dev/capabilities",
            {"set_skills": "engineering,review", "add_tool": ["cursor", "git"], "set_task_types": "code,review"},
        )
        self.assertEqual(200, status, capabilities)
        self.assertEqual(["engineering", "review"], capabilities["capabilities"]["skills"])
        self.assertIn("cursor", capabilities["capabilities"]["tools"])
        self.assertEqual(["code", "review"], capabilities["capabilities"]["preferred_task_types"])

        status, permissions = api_gateway.route_post(
            "/v1/employees/cursor-dev/permissions",
            {"can_submit_tasks": "false", "can_claim_tasks": "true", "requires_approval_for": "external_send,payment"},
        )
        self.assertEqual(200, status, permissions)
        self.assertFalse(permissions["permissions"]["can_submit_tasks"])
        self.assertTrue(permissions["permissions"]["can_claim_tasks"])
        self.assertEqual(["external_send", "payment"], permissions["permissions"]["requires_approval_for"])

        status, shown_updated = api_gateway.route_get("/v1/employees/cursor-dev", {})
        self.assertEqual(200, status, shown_updated)
        self.assertIn("engineering", shown_updated["capabilities"]["skills"])
        self.assertFalse(shown_updated["permissions"]["can_submit_tasks"])

        status, matched = api_gateway.route_post(
            "/v1/employees/match",
            {"skills": "engineering", "tools": "cursor", "task_type": "code", "runtime": "cursor", "limit": "3"},
        )
        self.assertEqual(200, status, matched)
        self.assertEqual("cursor-dev", matched["matches"][0]["agent"])
        status, match_get = api_gateway.route_get("/v1/employees/match", {})
        self.assertEqual(HTTPStatus.METHOD_NOT_ALLOWED, status)
        self.assertEqual("use POST", match_get["error"])

        status, routed = api_gateway.route_post(
            "/v1/tasks/route",
            {
                "from": "openclaw-main",
                "task_id": "task-api-route-cursor",
                "title": "Route API engineering task",
                "description": "select by capability through REST",
                "skills": "engineering",
                "tools": "cursor",
                "task_type": "code",
                "runtime": "cursor",
            },
        )
        self.assertEqual(201, status, routed)
        self.assertEqual("cursor-dev", routed["selected"]["agent"])
        self.assertEqual("cursor-dev", routed["task"]["target_agent"])
        self.assertEqual("task-api-route-cursor", routed["task"]["id"])
        self.assertEqual("cursor-dev", routed["task"]["metadata"]["route"]["matches"][0]["agent"])

        status, approval_route = api_gateway.route_post(
            "/v1/tasks/route",
            {
                "from": "openclaw-main",
                "task_id": "task-api-route-payment",
                "title": "Route payment task",
                "description": "payment requires approval before assignment",
                "skills": "engineering",
                "runtime": "cursor",
                "requires_approval": "payment",
            },
        )
        self.assertEqual(202, status, approval_route)
        self.assertFalse(approval_route["ok"])
        self.assertEqual("approval required", approval_route["error"])
        self.assertEqual("payment", approval_route["approval_action"])
        self.assertEqual("pending", approval_route["approval"]["status"])

        status, profile = api_gateway.route_post(
            "/v1/employees/cursor-dev/profile",
            {"name": "Cursor Reviewer", "role": "reviewer", "status": "candidate", "default_user_reply_channel": "telegram", "default_user_reply_account": "default", "default_user_reply_to": "current", "default_user_reply_deliver": "true"},
        )
        self.assertEqual(200, status, profile)
        self.assertTrue(profile["changed"])
        self.assertEqual("Cursor Reviewer", profile["employee"]["name"])
        self.assertEqual("reviewer", profile["employee"]["role"])
        self.assertEqual("candidate", profile["employee"]["status"])

        status, shown_profile = api_gateway.route_get("/v1/employees/cursor-dev", {})
        self.assertEqual(200, status, shown_profile)
        self.assertEqual("Cursor Reviewer", shown_profile["employee"]["name"])
        self.assertEqual("candidate", shown_profile["employee"]["status"])
        self.assertEqual("telegram", shown_profile["profile"]["default_user_reply_channel"])
        self.assertEqual("default", shown_profile["profile"]["default_user_reply_account"])
        self.assertEqual("current", shown_profile["profile"]["default_user_reply_to"])
        self.assertTrue(shown_profile["profile"]["default_user_reply_deliver"])

        status, patched = api_gateway.route_patch(
            "/v1/employees/cursor-dev",
            {"name": "Cursor API Employee", "role": "developer", "status": "active"},
        )
        self.assertEqual(HTTPStatus.BAD_REQUEST, status, patched)
        self.assertEqual("employee activation requires verified direct communication or structured runtime evidence", patched["error"])

        status, patched_candidate = api_gateway.route_patch(
            "/v1/employees/cursor-dev",
            {"name": "Cursor API Employee", "role": "developer", "status": "candidate"},
        )
        self.assertEqual(200, status, patched_candidate)
        self.assertEqual("Cursor API Employee", patched_candidate["employee"]["name"])
        self.assertEqual("candidate", patched_candidate["employee"]["status"])

        managed_workspace = self.root / "employees" / "api-reviewer"
        status, onboarded = api_gateway.route_post(
            "/v1/employees/onboard",
            {
                "id": "api-reviewer",
                "name": "API Reviewer",
                "role": "reviewer",
                "runtime": "hermes",
                "workspace": str(managed_workspace),
                "alias": "api-review",
                "skills": "review,qa",
                "can_talk_to": "codex",
                "default_user_reply_deliver": "true",
                "default_user_reply_channel": "telegram",
                "default_user_reply_account": "default",
                "default_user_reply_to": "current",
                "create_test_task": "true",
            },
        )
        self.assertEqual(201, status, onboarded)
        self.assertEqual("api-reviewer", onboarded["employee"]["id"])
        self.assertTrue((managed_workspace / "SOUL.md").exists())
        self.assertIn(str((managed_workspace / "SOUL.md").resolve()), onboarded["scaffolded_files"])
        self.assertTrue(onboarded["test_task"]["blocked"])
        self.assertEqual("onboarding test task requires a verified active employee", onboarded["test_task"]["reason"])
        self.assertIn("employee verify-direct", onboarded["test_task"]["required_command"])
        self.assertTrue(onboarded["communication"]["default_user_reply_deliver"])
        self.assertEqual("telegram", onboarded["communication"]["default_user_reply_channel"])

        status, offboard_dry = api_gateway.route_post("/v1/employees/api-review/offboard", {"dry_run": "true"})
        self.assertEqual(200, status, offboard_dry)
        self.assertTrue(offboard_dry["dry_run"])
        status, offboarded = api_gateway.route_delete("/v1/employees/api-review", {})
        self.assertEqual(200, status, offboarded)
        self.assertEqual("soft-delete", offboarded["action"])

        status, attendance = api_gateway.route_post(
            "/v1/attendance/sweep",
            {"source": "main", "agents": "api-reviewer", "sweep_id": "api-attendance", "probe_replies": "false"},
        )
        self.assertEqual(202, status, attendance)
        self.assertEqual("api-attendance", attendance["sweep_id"])
        self.assertIn(attendance["employees"][0]["status"], {"heartbeat_disabled", "no_reply"})
        status, attendance_latest = api_gateway.route_get("/v1/attendance/latest", {})
        self.assertEqual(200, status, attendance_latest)
        self.assertEqual("api-attendance", attendance_latest["sweep_id"])

    def test_employee_detail_includes_work_history_control_plane_and_costs(self) -> None:
        code, source = run_cli("employee", "create", "--id", "ops", "--name", "Ops", "--role", "owner", "--runtime", "local", "--workspace", str(self.root / "workspace" / "ops"))
        self.assertEqual(0, code, source)
        code, created = run_cli("employee", "create", "--id", "employee-detail-agent", "--name", "Employee Detail Agent", "--role", "developer", "--runtime", "codex", "--workspace", str(self.root / "workspace" / "employee-detail-agent"))
        self.assertEqual(0, code, created)
        with sqlite3.connect(self.root / "company.sqlite") as conn:
            conn.execute("UPDATE employees SET status = 'active' WHERE id = 'employee-detail-agent'")
            conn.commit()

        code, submitted = run_cli("task", "submit", "--from", "ops", "--to", "employee-detail-agent", "--task-id", "task-employee-detail", "--title", "Employee detail work")
        self.assertEqual(0, code, submitted)
        code, running = run_cli("task", "run", "--task-id", "task-employee-detail", "--agent", "employee-detail-agent", "--by", "hermes", "--adapter-type", "codex")
        self.assertEqual(0, code, running)
        attempt_id = running["attempt"]["attempt_id"]
        code, session = run_cli("runtime", "session", "start", "--session-id", "session-employee-detail", "--task-id", "task-employee-detail", "--attempt-id", attempt_id, "--employee", "employee-detail-agent", "--adapter-type", "codex", "--runtime-type", "cli")
        self.assertEqual(0, code, session)
        code, tool = run_cli("tool-call", "start", "--tool-call-id", "tool-employee-detail", "--task-id", "task-employee-detail", "--attempt-id", attempt_id, "--employee", "employee-detail-agent", "--session-id", "session-employee-detail", "--tool-name", "shell", "--tool-type", "shell", "--input-summary", "employee detail command")
        self.assertEqual(0, code, tool)
        code, finished_tool = run_cli("tool-call", "finish", "--tool-call-id", "tool-employee-detail", "--status", "success", "--output-summary", "employee detail command completed")
        self.assertEqual(0, code, finished_tool)
        code, budget = run_cli("budget", "record", "--budget-event-id", "budget-employee-detail", "--task-id", "task-employee-detail", "--attempt-id", attempt_id, "--employee", "employee-detail-agent", "--cost-type", "model_api", "--amount", "0.33", "--currency", "USD", "--token-input", "900", "--token-output", "210", "--runtime-seconds", "45", "--summary", "employee detail cost")
        self.assertEqual(0, code, budget)
        task_workspace = Path(submitted["task"]["workspace"]["path"])
        evidence_file = task_workspace / "final" / "detail-evidence.md"
        evidence_file.parent.mkdir(parents=True, exist_ok=True)
        evidence_file.write_text("employee detail evidence\n", encoding="utf-8")
        code, artifact = run_cli("task", "artifact", "register", "--task-id", "task-employee-detail", "--employee", "employee-detail-agent", "--path", str(evidence_file), "--type", "markdown", "--stage", "final", "--summary", "employee detail artifact")
        self.assertEqual(0, code, artifact)
        code, promoted = run_cli("task", "evidence", "promote", "--artifact-id", artifact["artifact"]["artifact_id"], "--employee", "employee-detail-agent", "--summary", "employee detail final evidence")
        self.assertEqual(0, code, promoted)
        code, done = run_cli("task", "done", "--agent", "employee-detail-agent", "--task-id", "task-employee-detail", "--summary", "done", "--evidence", str(evidence_file))
        self.assertEqual(0, code, done)
        code, current_task = run_cli("task", "submit", "--from", "ops", "--to", "employee-detail-agent", "--task-id", "task-employee-current", "--title", "Current employee work")
        self.assertEqual(0, code, current_task)
        code, current_run = run_cli("task", "run", "--task-id", "task-employee-current", "--agent", "employee-detail-agent", "--by", "hermes", "--adapter-type", "codex")
        self.assertEqual(0, code, current_run)
        current_attempt_id = current_run["attempt"]["attempt_id"]
        code, current_progress = run_cli("task", "progress", "--task-id", "task-employee-current", "--agent", "employee-detail-agent", "--attempt-id", current_attempt_id, "--state", "in_progress", "--message", "currently processing employee detail view", "--progress", "42")
        self.assertEqual(0, code, current_progress)

        status, shown = api_gateway.route_get("/v1/employees/employee-detail-agent", {})
        self.assertEqual(HTTPStatus.OK, status, shown)
        self.assertEqual("employee-detail-agent", shown["employee"]["id"])
        self.assertEqual(["task-employee-current", "task-employee-detail"], [item["id"] for item in shown["work_history"]["tasks"]])
        self.assertEqual("task-employee-current", shown["work_history"]["recent_tasks"][0]["id"])
        task_rollup = next(item for item in shown["work_history"]["tasks"] if item["id"] == "task-employee-detail")
        self.assertEqual(1, task_rollup["attempt_count"])
        self.assertEqual(1, task_rollup["runtime_session_count"])
        self.assertEqual(1, task_rollup["tool_call_count"])
        self.assertEqual(1, task_rollup["budget_event_count"])
        self.assertEqual(1, task_rollup["evidence_count"])
        self.assertEqual(0.33, task_rollup["budget_total"])
        self.assertEqual("USD", task_rollup["budget_currency"])
        self.assertEqual(900, task_rollup["token_input"])
        self.assertEqual(210, task_rollup["token_output"])
        self.assertEqual(attempt_id, task_rollup["latest_attempt_id"])
        self.assertEqual("success", task_rollup["latest_attempt_status"])
        self.assertEqual(["session-employee-detail"], [item["session_id"] for item in shown["runtime_sessions"]])
        self.assertEqual(["tool-employee-detail"], [item["tool_call_id"] for item in shown["tool_calls"]])
        self.assertEqual(["budget-employee-detail"], [item["budget_event_id"] for item in shown["budget_events"]])
        self.assertEqual(0.33, shown["budget_summary"]["total_amount"])
        self.assertEqual(900, shown["budget_summary"]["token_input"])
        self.assertEqual(["task-employee-detail"], [item["task_id"] for item in shown["evidence_records"]])
        self.assertEqual(current_attempt_id, shown["current_activity"]["attempt_id"])
        self.assertEqual("task-employee-current", shown["current_activity"]["task_id"])
        self.assertEqual("Current employee work", shown["current_activity"]["task_title"])
        self.assertEqual("running", shown["current_activity"]["long_task_state"])
        self.assertEqual("fresh", shown["current_activity"]["progress_state"])
        self.assertEqual("in_progress", shown["current_activity"]["latest_progress"]["progress_state"])
        self.assertEqual("currently processing employee detail view", shown["current_activity"]["latest_progress"]["message"])
        self.assertEqual(42, shown["current_activity"]["latest_progress"]["progress"])
        self.assertEqual(1, shown["current_activity"]["active_task_count"])
        self.assertEqual(current_attempt_id, shown["attempts"][0]["attempt_id"])

    def test_api_rpc_routes_rest_contract_without_direct_sqlite_access(self) -> None:
        described = api_rpc.handle_rpc({"jsonrpc": "2.0", "id": "describe", "method": "company.describe", "params": {}})
        self.assertEqual("describe", described["id"])
        self.assertTrue(described["result"]["protocols"]["rest"])
        self.assertTrue(described["result"]["protocols"]["json_rpc"])
        self.assertEqual("optional-grpcio", described["result"]["protocols"]["grpc"])
        self.assertEqual("companyctl", described["result"]["governance"]["state_writer"])

        created = api_rpc.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": "create-runtime",
                "method": "company.post",
                "params": {"path": "/v1/runtimes", "body": {"runtime": "remote-codex", "command": "ssh codex-worker", "notes": "remote worker"}},
            }
        )
        self.assertEqual(201, created["result"]["status"], created)
        self.assertEqual("remote-codex", created["result"]["body"]["runtime"]["runtime"])

        listed = api_rpc.handle_rpc(
            {"jsonrpc": "2.0", "id": "list-runtimes", "method": "company.get", "params": {"path": "/v1/runtimes", "query": {}}}
        )
        self.assertEqual(200, listed["result"]["status"], listed)
        self.assertIn("remote-codex", [runtime["runtime"] for runtime in listed["result"]["body"]["runtimes"]])

        with self.assertRaises(api_rpc.RpcError) as missing_path:
            api_rpc.handle_rpc({"jsonrpc": "2.0", "id": "bad", "method": "company.post", "params": {"body": {}}})
        self.assertEqual(-32602, missing_path.exception.code)

    def test_api_grpc_service_routes_governed_contract_without_direct_sqlite_access(self) -> None:
        service = api_grpc.CompanyKernelService()
        described = service.Describe(api_grpc.DescribeRequest())
        self.assertEqual(200, described.status)
        described_body = json.loads(described.body_json)
        self.assertEqual("optional-grpcio", described_body["protocols"]["grpc"])
        self.assertEqual("companyctl", described_body["governance"]["state_writer"])

        created = service.Post(
            api_grpc.RouteRequest(
                path="/v1/runtimes",
                body_json=json.dumps({"runtime": "grpc-worker", "command": "ssh grpc-worker", "notes": "remote grpc worker"}, ensure_ascii=False),
            )
        )
        self.assertEqual(201, created.status, created)
        self.assertEqual("grpc-worker", json.loads(created.body_json)["runtime"]["runtime"])

        listed = service.Get(api_grpc.RouteRequest(path="/v1/runtimes"))
        self.assertEqual(200, listed.status)
        self.assertIn("grpc-worker", [runtime["runtime"] for runtime in json.loads(listed.body_json)["runtimes"]])

        bad = service.Post(api_grpc.RouteRequest(path="/v1/runtimes", body_json="[]"))
        self.assertEqual(400, bad.status)
        self.assertEqual("body_json must decode to object", json.loads(bad.body_json)["error"])

    def test_api_grpc_generic_handlers_route_json_payloads(self) -> None:
        class FakeGrpc:
            @staticmethod
            def unary_unary_rpc_method_handler(handler, request_deserializer, response_serializer):
                return {"handler": handler, "request_deserializer": request_deserializer, "response_serializer": response_serializer}

            @staticmethod
            def method_handlers_generic_handler(service_name, handlers):
                return {"service_name": service_name, "handlers": handlers}

        class FakeServer:
            def __init__(self) -> None:
                self.handlers = ()

            def add_generic_rpc_handlers(self, handlers):
                self.handlers = handlers

        server = FakeServer()
        api_grpc.add_generic_service(server, FakeGrpc, api_grpc.CompanyKernelService())
        self.assertEqual("company.kernel.v1.CompanyKernel", server.handlers[0]["service_name"])

        request = json.dumps({"path": "/v1/runtimes", "body_json": json.dumps({"runtime": "generic-grpc"}, ensure_ascii=False)}, ensure_ascii=False).encode("utf-8")
        raw = server.handlers[0]["handlers"]["Post"]["handler"](request)
        response = json.loads(raw.decode("utf-8"))
        self.assertEqual(201, response["status"])
        self.assertEqual("generic-grpc", json.loads(response["body_json"])["runtime"]["runtime"])

        raw_describe = server.handlers[0]["handlers"]["Describe"]["handler"](b"{}", object())
        described = json.loads(raw_describe.decode("utf-8"))
        described_body = json.loads(described["body_json"])
        self.assertEqual(200, described["status"])
        self.assertEqual("Company Kernel API Gateway", described_body["name"])

    def test_service_smoke_starts_rest_and_rpc_ports(self) -> None:
        for agent in ["video-ops", "video-creator", "video-publisher", "codex", "openclaw-main", "hermes", "nestcar"]:
            code, heartbeat = run_cli("heartbeat", "--agent", agent)
            self.assertEqual(code, 0, heartbeat)
        daemon_state = self.root / "state" / "daemon" / "last-run.json"
        daemon_state.parent.mkdir(parents=True, exist_ok=True)
        daemon_state.write_text(json.dumps({"ok": True, "at": companyctl.now(), "results": []}, ensure_ascii=False), encoding="utf-8")

        with mock.patch.object(company_service_smoke, "free_port", side_effect=[41001, 41002]), mock.patch.object(company_service_smoke, "start_thread"), mock.patch.object(
            company_service_smoke,
            "get_json",
            side_effect=[{"ok": True, "issues": []}, {"ok": True}],
        ), mock.patch.object(company_service_smoke, "post_json", return_value={"result": {"status": 200}}):
            result = company_service_smoke.run_smoke()
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["rest"]["ok"])
        self.assertTrue(result["rpc"]["describe_ok"])
        self.assertEqual(200, result["rpc"]["health_status"])
        self.assertIn(result["grpc"]["check"], {"ready", "grpcio_not_installed"})

    def test_service_smoke_handles_health_http_error_payload(self) -> None:
        with mock.patch.object(company_service_smoke, "free_port", side_effect=[41011, 41012]), mock.patch.object(company_service_smoke, "start_thread"), mock.patch.object(
            company_service_smoke,
            "get_json",
            side_effect=[{"ok": False, "issues": ["pending_events"], "http_status": 400}, {"ok": True}],
        ), mock.patch.object(company_service_smoke, "post_json", return_value={"result": {"status": 200}}):
            result = company_service_smoke.run_smoke()
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["rest"]["ok"])
        self.assertEqual(400, result["rest"]["http_status"])
        self.assertEqual(["pending_events"], result["rest"]["issues"])

    def test_sandboxing_wraps_codex_and_hermes_commands_without_executing_container(self) -> None:
        workspace = self.root / "workspace" / "codex"
        profile_config = {
            "profiles": {
                "codex": {
                    "default": {
                        "image": "codex-test:latest",
                        "network": "none",
                        "readonly_paths": [str(self.root / "readonly")],
                        "writable_paths": [str(workspace)],
                    }
                },
                "hermes": {"default": {"image": "hermes-test:latest", "network": "bridge", "readonly_paths": [], "writable_paths": []}},
            }
        }
        codex_cmd = sandboxing.wrap_command(["codex", "exec", "-"], runtime="codex", workspace=workspace, isolation="docker", config=profile_config)
        self.assertEqual("docker", codex_cmd[0])
        self.assertIn("codex-test:latest", codex_cmd)
        self.assertIn("--network", codex_cmd)
        self.assertIn("none", codex_cmd)
        self.assertIn(f"{workspace}:{workspace}:rw", codex_cmd)
        self.assertIn(f"{self.root / 'readonly'}:{self.root / 'readonly'}:ro", codex_cmd)

        hermes_cmd = sandboxing.wrap_command(["hermes", "-z", "prompt"], runtime="hermes", workspace=self.root / "workspace" / "hermes", isolation="firejail", config=profile_config)
        self.assertEqual("firejail", hermes_cmd[0])
        self.assertIn("--private=" + str(self.root / "workspace" / "hermes"), hermes_cmd)
        self.assertEqual(["codex", "exec"], sandboxing.wrap_command(["codex", "exec"], runtime="codex", workspace=workspace, isolation="none")[:2])

    def test_direct_task_submit_requires_approval_for_high_risk_actions(self) -> None:
        code, blocked = run_cli(
            "task",
            "submit",
            "--from",
            "ops",
            "--to",
            "publisher",
            "--task-id",
            "task-direct-submit-approval",
            "--title",
            "发布客户通知",
            "--description",
            "需要外发给客户",
            "--requires-approval",
            "external_send",
        )
        self.assertEqual(code, 2, blocked)
        self.assertEqual("approval required", blocked["error"])
        approval_id = blocked["approval"]["id"]
        self.assertEqual("external_send", blocked["approval_action"])

        code, missing_task = run_cli("task", "show", "--task-id", "task-direct-submit-approval")
        self.assertEqual(code, 1, missing_task)

        code, approved = run_cli("approval", "approve", "--approval-id", approval_id, "--by", "ops", "--reason", "允许测试外发")
        self.assertEqual(code, 0, approved)
        code, submitted = run_cli(
            "task",
            "submit",
            "--from",
            "ops",
            "--to",
            "publisher",
            "--task-id",
            "task-direct-submit-approval",
            "--title",
            "发布客户通知",
            "--description",
            "需要外发给客户",
            "--approval-id",
            approval_id,
            "--requires-approval",
            "external_send",
        )
        self.assertEqual(code, 0, submitted)
        self.assertEqual(approval_id, submitted["task"]["metadata"]["approval"]["id"])

    def test_direct_task_submit_reuses_deterministic_approved_route_gate(self) -> None:
        task_id = "task-direct-submit-auto-approval"
        code, blocked = run_cli(
            "task",
            "submit",
            "--from",
            "ops",
            "--to",
            "publisher",
            "--task-id",
            task_id,
            "--title",
            "发布客户通知",
            "--description",
            "需要外发给客户",
            "--requires-approval",
            "external_send",
        )
        self.assertEqual(code, 2, blocked)
        approval_id = blocked["approval"]["id"]

        code, approved = run_cli("approval", "approve", "--approval-id", approval_id, "--by", "ops", "--reason", "允许测试外发")
        self.assertEqual(code, 0, approved)
        code, submitted = run_cli(
            "task",
            "submit",
            "--from",
            "ops",
            "--to",
            "publisher",
            "--task-id",
            task_id,
            "--title",
            "发布客户通知",
            "--description",
            "需要外发给客户",
            "--requires-approval",
            "external_send",
        )
        self.assertEqual(code, 0, submitted)
        self.assertEqual(approval_id, submitted["task"]["metadata"]["approval"]["id"])

    def test_route_approval_keyword_does_not_match_employee_id_substrings(self) -> None:
        code, created = run_cli("employee", "create", "--id", "main", "--name", "main", "--role", "operator", "--runtime", "openclaw", "--workspace", str(self.root / "workspace" / "main"))
        self.assertEqual(0, code, created)
        code, submitted = run_cli(
            "task",
            "submit",
            "--from",
            "main",
            "--to",
            "video-publisher",
            "--task-id",
            "task-video-publisher-name-check",
            "--title",
            "Runtime adapter dry-run check: video-publisher",
            "--description",
            "Adapter dry-run check task only; no external publish action.",
        )
        self.assertEqual(0, code, submitted)

    def test_policy_auto_approval_allows_low_risk_openclaw_external_send(self) -> None:
        policy_path = self.root / "config" / "policy.json"
        policy_path.write_text(
            json.dumps(
                {
                    "route_approval": {
                        "actions": {"external_send": ["publish"]},
                        "auto_approval_rules": [
                            {
                                "id": "nestcar-low-risk-fetch",
                                "enabled": True,
                                "action": "external_send",
                                "source": "main",
                                "target": "nestcar",
                                "metadata": {"adapter": "openclaw", "task_type": "data_fetch"},
                                "priority_not_in": ["P1"],
                                "risk_not_in": ["P1"],
                            }
                        ],
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        code, created = run_cli("employee", "create", "--id", "main", "--name", "main", "--role", "operator", "--runtime", "openclaw", "--workspace", str(self.root / "workspace" / "main"))
        self.assertEqual(0, code, created)
        with policy_guard.connect() as conn:
            gate = policy_guard.require_approval(
                source="main",
                target="nestcar",
                action="external_send",
                reason="low risk data fetch",
                risk="P3",
                evidence="payload.json",
                metadata={"adapter": "openclaw", "task_id": "task-auto-ok", "task_type": "data_fetch", "priority": "P3"},
            )
            rows = conn.execute("SELECT * FROM approvals WHERE status = 'approved' AND action = 'external_send'").fetchall()
        self.assertTrue(gate["allowed"], gate)
        self.assertEqual("auto_approved", gate["approval"]["detail"]["approval_mode"])
        self.assertEqual("nestcar-low-risk-fetch", gate["approval"]["detail"]["auto_rule_id"])
        self.assertEqual(1, len(rows))

    def test_custom_runtime_can_be_registered_and_onboarded_without_code_changes(self) -> None:
        with self.assertRaises(SystemExit):
            companyctl.main(["employee", "create", "--id", "cursor-agent", "--name", "Cursor", "--role", "developer", "--runtime", "cursor", "--workspace", str(self.root / "cursor")])

        code, registered = run_cli("runtime", "register", "--runtime", "cursor", "--notes", "Cursor IDE adapter placeholder")
        self.assertEqual(code, 0, registered)
        self.assertEqual("cursor", registered["runtime"]["runtime"])
        code, runtimes = run_cli("runtime", "list")
        self.assertEqual(code, 0, runtimes)
        self.assertIn("cursor", [item["runtime"] for item in runtimes["runtimes"]])
        code, runtime_test = run_cli("runtime", "test", "--runtime", "cursor")
        self.assertEqual(code, 0, runtime_test)
        self.assertTrue(runtime_test["ok"])

        code, onboarded = run_cli(
            "employee",
            "onboard",
            "--id",
            "cursor-agent",
            "--name",
            "Cursor",
            "--role",
            "developer",
            "--runtime",
            "cursor",
            "--workspace",
            str(self.root / "cursor"),
            "--skills",
            "ide-development",
            "--tools",
            "cursor",
            "--task-types",
            "implementation",
        )
        self.assertEqual(code, 0, onboarded)
        self.assertEqual("cursor", onboarded["employee"]["runtime"])
        self.assertIn("ide-development", onboarded["capabilities"]["skills"])

    def test_protected_task_requires_approved_rfc_before_claim(self) -> None:
        code, submitted = run_cli(
            "task",
            "submit",
            "--from",
            "openclaw-main",
            "--to",
            "codex",
            "--task-id",
            "task-protected-001",
            "--title",
            "修改策略",
            "--changed-files",
            "config/policy.json",
        )
        self.assertEqual(code, 0, submitted)
        code, blocked = run_cli("task", "claim", "--agent", "codex", "--task-id", "task-protected-001")
        self.assertEqual(code, 2, blocked)

        code, rfc = run_cli("rfc", "create", "--rfc-id", "rfc-policy-001", "--title", "修改策略", "--by", "codex", "--paths", "config/policy.json", "--reason", "测试保护区")
        self.assertEqual(code, 0, rfc)
        code, approved = run_cli("rfc", "approve", "--rfc", "rfc-policy-001", "--by", "openclaw-main", "--reason", "批准测试")
        self.assertEqual(code, 0, approved)

        code, submitted_with_rfc = run_cli(
            "task",
            "submit",
            "--from",
            "openclaw-main",
            "--to",
            "codex",
            "--task-id",
            "task-protected-002",
            "--title",
            "修改策略",
            "--changed-files",
            "config/policy.json",
            "--rfc",
            "rfc-policy-001",
        )
        self.assertEqual(code, 0, submitted_with_rfc)
        code, claimed = run_cli("task", "claim", "--agent", "codex", "--task-id", "task-protected-002")
        self.assertEqual(code, 0, claimed)

    def test_runtime_verify_adapters_creates_task_and_requires_evidence_and_heartbeat(self) -> None:
        def fake_run(cmd: list[str], cwd: str, text: bool, capture_output: bool) -> subprocess.CompletedProcess:
            if cmd[1:3] == ["task", "submit"]:
                task_id = cmd[cmd.index("--task-id") + 1]
                source = cmd[cmd.index("--from") + 1]
                target = cmd[cmd.index("--to") + 1]
                title = cmd[cmd.index("--title") + 1]
                with companyctl.connect() as conn:
                    companyctl.submit_task_internal(
                        conn,
                        source=source,
                        target=target,
                        task_id=task_id,
                        title=title,
                        description="adapter verification",
                        priority="P3",
                        metadata={"runtime_verify": True},
                    )
                stdout = json.dumps({"ok": True, "task": {"id": task_id}}, ensure_ascii=False)
                return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
            if cmd[1:3] == ["scheduler", "run"]:
                stdout = json.dumps({"ok": True, "dry_run": False, "events": []}, ensure_ascii=False)
                return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
            self.assertIn("company-hermes-adapter", cmd[0])
            task_id = "task-runtime-test-hermes"
            report = self.root / "employees" / "hermes" / "reports" / task_id / "hermes-adapter-report.md"
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("adapter ok\n", encoding="utf-8")
            with companyctl.connect() as conn:
                conn.execute(
                    "UPDATE tasks SET status = 'completed', evidence_path = ?, summary = 'adapter ok', updated_at = ? WHERE id = ?",
                    (str(report), companyctl.now(), task_id),
                )
                conn.commit()
                companyctl.heartbeat_internal(conn, "hermes", {"source": "test-adapter"})
            stdout = json.dumps({"ok": True, "processed": 1, "task_id": task_id, "report": str(report)}, ensure_ascii=False)
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

        with mock.patch.object(companyctl.subprocess, "run", fake_run):
            code, verified = run_cli("runtime", "verify-adapters", "--agents", "hermes", "--task-id-prefix", "task-runtime-test")
        self.assertEqual(code, 0, verified)
        self.assertTrue(verified["ok"])
        self.assertEqual(1, verified["count"])

    def test_runtime_verify_adapters_auto_detects_main_source_when_openclaw_main_missing(self) -> None:
        with companyctl.connect() as conn:
            conn.execute("DELETE FROM employees WHERE id = 'openclaw-main'")
            conn.execute(
                """
                INSERT OR REPLACE INTO employees (
                  id, name, role, runtime, workspace, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "main",
                    "main",
                    "supervisor",
                    "openclaw",
                    str(self.root / "workspace" / "main"),
                    "active",
                    companyctl.now(),
                    companyctl.now(),
                ),
            )
            conn.commit()
        submitted_sources: list[str] = []

        def fake_run(cmd: list[str], cwd: str, text: bool, capture_output: bool) -> subprocess.CompletedProcess:
            if cmd[1:3] == ["task", "submit"]:
                task_id = cmd[cmd.index("--task-id") + 1]
                source = cmd[cmd.index("--from") + 1]
                target = cmd[cmd.index("--to") + 1]
                submitted_sources.append(source)
                with companyctl.connect() as conn:
                    companyctl.submit_task_internal(
                        conn,
                        source=source,
                        target=target,
                        task_id=task_id,
                        title="adapter verification",
                        description="adapter verification",
                        priority="P3",
                        metadata={"runtime_verify": True},
                    )
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"ok": True}, ensure_ascii=False), stderr="")
            if cmd[1:3] == ["scheduler", "run"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"ok": True, "events": []}, ensure_ascii=False), stderr="")
            task_id = "task-runtime-main-source-hermes"
            report = self.root / "employees" / "hermes" / "reports" / task_id / "hermes-adapter-report.md"
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("adapter ok\n", encoding="utf-8")
            with companyctl.connect() as conn:
                conn.execute(
                    "UPDATE tasks SET status = 'completed', evidence_path = ?, summary = 'adapter ok', updated_at = ? WHERE id = ?",
                    (str(report), companyctl.now(), task_id),
                )
                conn.commit()
                companyctl.heartbeat_internal(conn, "hermes", {"source": "test-adapter"})
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"ok": True, "task_id": task_id}, ensure_ascii=False), stderr="")

        with mock.patch.object(companyctl.subprocess, "run", fake_run):
            code, verified = run_cli("runtime", "verify-adapters", "--agents", "hermes", "--task-id-prefix", "task-runtime-main-source")
        self.assertEqual(code, 0, verified)
        self.assertEqual("main", verified["source"])
        self.assertEqual(["main"], submitted_sources)

    def test_runtime_verify_adapters_passes_skill_package_manifest(self) -> None:
        code, runtime = run_cli("runtime", "register", "--runtime", "skill", "--command", "company-skill-package-worker", "--notes", "Skill Package runtime")
        self.assertEqual(0, code, runtime)
        code, employee = run_cli(
            "employee",
            "create",
            "--id",
            "image-copy-skill",
            "--name",
            "Image Copy Skill",
            "--role",
            "skill-worker",
            "--runtime",
            "skill",
            "--workspace",
            str(self.root / "employees" / "image-copy-skill"),
        )
        self.assertEqual(0, code, employee)
        self.mark_active("image-copy-skill")
        package_dir = self.root / "skill-packages" / "image-copy"
        package_dir.mkdir(parents=True)
        manifest_path = package_dir / "skill.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "id": "image-copy",
                    "name": "Image copy package",
                    "version": "0.1.0",
                    "employee_id": "image-copy-skill",
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                    "runtime": {"type": "local-script", "command": "python3 -c \"import os; from pathlib import Path; root=Path(os.environ['TASK_WORKSPACE']); (root/'final').mkdir(exist_ok=True); (root/'final/result.md').write_text('runtime verify skill output', encoding='utf-8')\""},
                    "permissions": {"workspace": "task"},
                    "pricing": {"unit": "task", "amount": 10, "currency": "USD"},
                    "acceptance": {"final_artifact": "final/result.md"},
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        code, verified = run_cli("runtime", "verify-adapters", "--agents", "image-copy-skill", "--task-id-prefix", "task-runtime-skill")
        self.assertEqual(0, code, verified)
        self.assertTrue(verified["ok"], verified)
        self.assertEqual(str(manifest_path), verified["results"][0]["package"])
        self.assertTrue(verified["results"][0]["evidence_exists"])
        self.assertTrue(verified["results"][0]["evidence_exists"])
        self.assertEqual("completed", verified["results"][0]["task_status"])

    def test_runtime_verify_adapters_can_verify_candidate_without_enabling_task_submit(self) -> None:
        code, runtime = run_cli("runtime", "register", "--runtime", "claude", "--command", "company-claude-adapter", "--notes", "Claude runtime")
        self.assertEqual(0, code, runtime)
        code, employee = run_cli(
            "employee",
            "create",
            "--id",
            "claude-code",
            "--name",
            "Claude Code",
            "--role",
            "developer",
            "--runtime",
            "claude",
            "--workspace",
            str(self.root / "workspace" / "claude-code"),
        )
        self.assertEqual(0, code, employee)
        code, blocked = run_cli("task", "submit", "--from", "openclaw-main", "--to", "claude-code", "--task-id", "task-candidate-normal-submit", "--title", "normal submit remains blocked")
        self.assertEqual(2, code, blocked)

        def fake_run(cmd: list[str], cwd: str, text: bool, capture_output: bool) -> subprocess.CompletedProcess:
            if cmd[1:3] == ["scheduler", "run"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"ok": True, "events": []}, ensure_ascii=False), stderr="")
            task_id = "task-runtime-candidate-claude-claude-code"
            with companyctl.connect() as conn:
                workspace = companyctl.ensure_task_workspace(conn, task_id, companyctl.trace_id_for_task(conn, task_id))
            report = Path(workspace["path"]) / "final" / "claude-adapter-report.md"
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("claude candidate adapter evidence\n", encoding="utf-8")
            with companyctl.connect() as conn:
                companyctl.complete_task_internal(conn, agent="claude-code", task_id=task_id, summary="claude adapter evidence", evidence=str(report))
                companyctl.heartbeat_internal(conn, "claude-code", {"source": "candidate-adapter-test"})
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"ok": True, "processed": 1, "task_id": task_id, "report": str(report)}, ensure_ascii=False), stderr="")

        with mock.patch.object(companyctl.subprocess, "run", fake_run):
            code, verified = run_cli("runtime", "verify-adapters", "--agents", "claude-code", "--task-id-prefix", "task-runtime-candidate-claude", "--allow-candidate")
        self.assertEqual(0, code, verified)
        self.assertTrue(verified["ok"], verified)
        self.assertTrue(verified["results"][0]["candidate_verification"])
        self.assertTrue(verified["results"][0]["evidence_exists"])
        self.assertTrue(verified["results"][0]["final_evidence"])
        code, still_blocked = run_cli("task", "submit", "--from", "openclaw-main", "--to", "claude-code", "--task-id", "task-candidate-normal-submit-2", "--title", "normal submit still blocked")
        self.assertEqual(2, code, still_blocked)
        code, activated = run_cli("employee", "update", "--id", "claude-code", "--status", "active")
        self.assertEqual(0, code, activated)
        self.assertEqual("active", activated["employee"]["status"])
        code, matrix = run_cli("agent-matrix", "--agents", "claude-code")
        self.assertEqual(0, code, matrix)
        self.assertEqual("active_ready", matrix["employees"][0]["level"])
        self.assertEqual("adapter_runtime_evidence_no_openclaw_session_required", matrix["employees"][0]["reason"])

    def test_agent_matrix_resolves_requested_alias_to_canonical_employee(self) -> None:
        config_path = self.root / "config" / "company_communications.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config.setdefault("aliases", {})["car-rental"] = "nestcar"
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        code, created = run_cli(
            "employee",
            "create",
            "--id",
            "nestcar",
            "--name",
            "NestCar",
            "--role",
            "business-agent",
            "--runtime",
            "openclaw",
            "--workspace",
            str(self.root / "workspace" / "nestcar"),
        )
        self.assertEqual(0, code, created)
        code, verified = run_cli("employee", "verify-direct", "--id", "nestcar", "--from", "openclaw-main", "--rounds", "2", "--activate")
        self.assertEqual(0, code, verified)

        code, matrix = run_cli("agent-matrix", "--agents", "car-rental")
        self.assertEqual(0, code, matrix)
        row = matrix["employees"][0]
        self.assertEqual("nestcar", row["agent"])
        self.assertEqual("car-rental", row["requested_agent"])
        self.assertEqual("nestcar", row["alias_of"])
        self.assertEqual("active_ready", row["level"])

    def test_dashboard_employee_view_skips_alias_duplicate_rows(self) -> None:
        config_path = self.root / "config" / "company_communications.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config.setdefault("aliases", {})["car-rental"] = "nestcar"
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        summary = {
            "generated_at": companyctl.now(),
            "employees": [
                {
                    "id": "car-rental",
                    "name": "car-rental",
                    "role": "runtime-agent",
                    "runtime": "openclaw",
                    "employee_status": "candidate",
                    "workspace": str(self.root / "agents" / "car-rental"),
                    "heartbeat_status": "missing",
                    "last_seen_at": "",
                    "heartbeat_metadata_json": "{}",
                    "submitted_tasks": 0,
                    "claimed_tasks": 0,
                },
                {
                    "id": "nestcar",
                    "name": "car-rental",
                    "role": "business-agent",
                    "runtime": "openclaw",
                    "employee_status": "active",
                    "workspace": str(self.root / "workspace" / "nestcar"),
                    "heartbeat_status": "alive",
                    "last_seen_at": companyctl.now(),
                    "heartbeat_metadata_json": "{}",
                    "submitted_tasks": 0,
                    "claimed_tasks": 0,
                },
            ],
        }
        employees = company_dashboard.employee_view_models(summary)
        self.assertEqual(["nestcar"], [employee["id"] for employee in employees])

    def test_hermes_adapter_runs_codex_pm_supervisor_with_dev_roots_before_heartbeat(self) -> None:
        codex_workspace = self.root / "workspace" / "codex-dev"
        codex_workspace.mkdir(parents=True, exist_ok=True)
        with companyctl.connect() as conn:
            conn.execute("UPDATE employees SET workspace = ? WHERE id = 'codex'", (str(codex_workspace),))
            conn.commit()

        calls: list[list[str]] = []

        def fake_run(cmd: list[str], cwd: str, text: bool, capture_output: bool, env: dict[str, str]) -> subprocess.CompletedProcess:
            calls.append(cmd)
            if Path(cmd[0]).name == "company-codex-pm-supervisor":
                stdout = json.dumps(
                    {
                        "ok": True,
                        "status": "idle",
                        "db_path": str((self.root / "company.sqlite").resolve()),
                        "workspace": str(codex_workspace.resolve()),
                        "report_path": str((self.root / "employees" / "hermes" / "reports" / "codex-pm" / "report.json").resolve()),
                    },
                    ensure_ascii=False,
                )
                return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
            if Path(cmd[0]).name == "companyctl":
                stdout = json.dumps({"ok": True, "heartbeat": {"agent_id": "hermes"}}, ensure_ascii=False)
                return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
            self.fail(f"unexpected command: {cmd}")

        captured = io.StringIO()
        with mock.patch.object(hermes_adapter.subprocess, "run", fake_run), contextlib.redirect_stdout(captured):
            exit_code = hermes_adapter.main(["--agent", "hermes"])

        self.assertEqual(0, exit_code)
        payload = json.loads(captured.getvalue())
        self.assertEqual(0, payload["codex_pm_supervisor"]["exit_code"])
        self.assertEqual(str(codex_workspace.resolve()), payload["codex_pm_supervisor"]["result"]["workspace"])
        pm_cmd = calls[0]
        self.assertEqual("company-codex-pm-supervisor", Path(pm_cmd[0]).name)
        self.assertEqual(["--agent", "codex"], pm_cmd[1:3])
        self.assertEqual("--db-path", pm_cmd[3])
        self.assertEqual((self.root / "company.sqlite").resolve(), Path(pm_cmd[4]).resolve())
        self.assertEqual("--workspace", pm_cmd[5])
        self.assertEqual(codex_workspace.resolve(), Path(pm_cmd[6]).resolve())
        self.assertEqual("--report-root", pm_cmd[7])
        self.assertEqual(self.root.resolve(), Path(pm_cmd[8]).resolve())
        self.assertEqual("companyctl", Path(calls[1][0]).name)
        self.assertEqual(["heartbeat", "--agent", "hermes"], calls[1][1:])

    def test_hermes_adapter_exposes_progress_bridge_from_pm_supervisor_result(self) -> None:
        codex_workspace = self.root / "workspace" / "codex-dev-bridge"
        codex_workspace.mkdir(parents=True, exist_ok=True)
        progress = codex_workspace / "reports" / "progress_in_progress_task-codex-20260606-heartbeat-progress-bridge.json"
        progress.parent.mkdir(parents=True, exist_ok=True)
        progress.write_text("{}", encoding="utf-8")

        def fake_run(cmd: list[str], cwd: str, text: bool, capture_output: bool, env: dict[str, str]) -> subprocess.CompletedProcess:
            if Path(cmd[0]).name == "company-codex-pm-supervisor":
                stdout = json.dumps(
                    {
                        "ok": True,
                        "status": "in_progress",
                        "task_id": "task-codex-20260606-heartbeat-progress-bridge",
                        "workspace": str(codex_workspace.resolve()),
                        "latest_progress_path": str(progress.resolve()),
                        "progress_layer": "working",
                        "progress_state": "in_progress",
                    },
                    ensure_ascii=False,
                )
                return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
            if Path(cmd[0]).name == "companyctl":
                stdout = json.dumps({"ok": True, "heartbeat": {"agent_id": "hermes"}}, ensure_ascii=False)
                return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
            self.fail(f"unexpected command: {cmd}")

        captured = io.StringIO()
        with mock.patch.object(hermes_adapter.subprocess, "run", fake_run), contextlib.redirect_stdout(captured):
            exit_code = hermes_adapter.main(["--agent", "hermes"])

        self.assertEqual(0, exit_code)
        payload = json.loads(captured.getvalue())
        pm_result = payload["codex_pm_supervisor"]["result"]
        self.assertEqual("task-codex-20260606-heartbeat-progress-bridge", pm_result["task_id"])
        self.assertEqual(str(progress.resolve()), pm_result["latest_progress_path"])
        self.assertEqual("working", pm_result["progress_layer"])
        self.assertEqual("in_progress", pm_result["progress_state"])

    def test_openclaw_adapter_dry_run_writes_payload_and_evidence(self) -> None:
        code, submitted = run_cli(
            "task",
            "submit",
            "--from",
            "openclaw-main",
            "--to",
            "nestcar",
            "--task-id",
            "task-openclaw-dry-run",
            "--title",
            "检查车辆任务",
            "--description",
            "请检查今天车辆任务",
        )
        self.assertEqual(code, 0, submitted)

        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = openclaw_adapter.main(["--agent", "nestcar"])
        result = json.loads(captured.getvalue())
        self.assertEqual(0, code, result)
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["executed"])
        self.assertTrue(Path(result["payload"]).exists())
        self.assertTrue(Path(result["report"]).exists())

        payload = json.loads(Path(result["payload"]).read_text(encoding="utf-8"))
        self.assertEqual("task-openclaw-dry-run", payload["task_id"])
        self.assertEqual("openclaw-main", payload["source_agent"])
        self.assertEqual("nestcar", payload["target_agent"])
        self.assertEqual("检查车辆任务", payload["goal"])
        self.assertEqual("openclaw-main", payload["reply_to_agent"])
        self.assertEqual("company-kernel-message", payload["reply_surface"])
        self.assertIn("claimed", payload["expected_receipts"])
        self.assertIn("report_path", payload["evidence_required"])

        code, task = run_cli("task", "show", "--task-id", "task-openclaw-dry-run")
        self.assertEqual(code, 0, task)
        self.assertEqual("completed", task["task"]["status"])
        self.assertEqual(result["report"], task["task"]["evidence_path"])
        self.assertTrue((self.root / "employees" / "nestcar" / "heartbeat.json").exists())

    def test_openclaw_adapter_execute_requires_approval_then_submits_bus(self) -> None:
        task_id = "task-openclaw-execute"
        code, submitted = run_cli(
            "task",
            "submit",
            "--from",
            "openclaw-main",
            "--to",
            "nestcar",
            "--task-id",
            task_id,
            "--title",
            "提交旧 bus",
            "--description",
            "验证 Company Kernel 到 OpenClaw legacy bus",
        )
        self.assertEqual(code, 0, submitted)

        blocked_out = io.StringIO()
        with contextlib.redirect_stdout(blocked_out):
            blocked_code = openclaw_adapter.main(["--agent", "nestcar", "--execute"])
        blocked = json.loads(blocked_out.getvalue())
        self.assertEqual(2, blocked_code, blocked)
        self.assertFalse(blocked["ok"])
        self.assertTrue(blocked["blocked_by_approval"])
        approval_id = blocked["approval"]["id"]

        code, task_after_block = run_cli("task", "show", "--task-id", task_id)
        self.assertEqual(code, 0, task_after_block)
        self.assertEqual("claimed", task_after_block["task"]["status"])
        self.assertEqual("nestcar", task_after_block["task"]["claimed_by"])

        code, approved = run_cli("approval", "approve", "--approval-id", approval_id, "--by", "openclaw-main", "--reason", "测试批准")
        self.assertEqual(code, 0, approved)

        calls: list[list[str]] = []

        def fake_submit(source: str, target: str, priority: str, payload: dict) -> tuple[int, str, str]:
            calls.append([source, target, priority, payload["task_id"]])
            out = json.dumps({"ok": True, "file": str(self.root / "openclaw" / "ops" / "agent_bus" / f"{payload['task_id']}.json")}, ensure_ascii=False)
            return 0, out, ""

        executed_out = io.StringIO()
        with mock.patch.object(openclaw_adapter, "submit_openclaw", fake_submit), contextlib.redirect_stdout(executed_out):
            executed_code = openclaw_adapter.main(["--agent", "nestcar", "--execute", "--approval-id", approval_id])
        executed = json.loads(executed_out.getvalue())
        self.assertEqual(0, executed_code, executed)
        self.assertTrue(executed["ok"], executed)
        self.assertTrue(executed["executed"])
        self.assertEqual([["main", "nestcar", "P2", task_id]], calls)

        code, task = run_cli("task", "show", "--task-id", task_id)
        self.assertEqual(code, 0, task)
        self.assertEqual("completed", task["task"]["status"])
        self.assertEqual(executed["report"], task["task"]["evidence_path"])
        self.assertIn("Submitted Company Kernel task to OpenClaw legacy bus", Path(executed["report"]).read_text(encoding="utf-8"))

    def test_openclaw_adapter_execute_uses_auto_approval_for_low_risk_data_fetch(self) -> None:
        (self.root / "config" / "policy.json").write_text(
            json.dumps(
                {
                    "route_approval": {
                        "auto_approval_rules": [
                            {
                                "id": "nestcar-low-risk-fetch",
                                "enabled": True,
                                "action": "external_send",
                                "source": "main",
                                "target": "nestcar",
                                "metadata": {"adapter": "openclaw", "task_type": "data_fetch"},
                                "priority_not_in": ["P1"],
                                "risk_not_in": ["P1"],
                            }
                        ]
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        task_id = "task-openclaw-auto-approval"
        code, main = run_cli("employee", "create", "--id", "main", "--name", "main", "--role", "operator", "--runtime", "openclaw", "--workspace", str(self.root / "workspace" / "main"))
        self.assertEqual(0, code, main)
        code, submitted = run_cli(
            "task",
            "submit",
            "--from",
            "main",
            "--to",
            "nestcar",
            "--task-id",
            task_id,
            "--title",
            "低风险抓取",
            "--description",
            "data fetch through OpenClaw",
            "--priority",
            "P3",
        )
        self.assertEqual(0, code, submitted)
        with companyctl.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO task_metadata(task_id, metadata_json, updated_at) VALUES (?, ?, ?)",
                (task_id, json.dumps({"trace_id": submitted["task"]["metadata"]["trace_id"], "task_type": "data_fetch"}, ensure_ascii=False), companyctl.now()),
            )
            conn.commit()

        calls: list[list[str]] = []

        def fake_submit(source: str, target: str, priority: str, payload: dict) -> tuple[int, str, str]:
            calls.append([source, target, priority, payload["task_id"]])
            return 0, json.dumps({"ok": True, "file": str(self.root / "openclaw" / "bus" / f"{payload['task_id']}.json")}, ensure_ascii=False), ""

        captured = io.StringIO()
        with mock.patch.object(openclaw_adapter, "submit_openclaw", fake_submit), contextlib.redirect_stdout(captured):
            code = openclaw_adapter.main(["--agent", "nestcar", "--execute"])
        result = json.loads(captured.getvalue())
        self.assertEqual(0, code, result)
        self.assertTrue(result["ok"], result)
        self.assertEqual([["main", "nestcar", "P3", task_id]], calls)
        with policy_guard.connect() as conn:
            approval = conn.execute("SELECT * FROM approvals WHERE source_agent = 'main' AND action = 'external_send' AND status = 'approved'").fetchone()
        self.assertIsNotNone(approval)
        self.assertEqual("auto_approved", json.loads(approval["reason"])["approval_mode"])

    def test_openclaw_adapter_execute_reports_missing_oc_as_blocker(self) -> None:
        task_id = "task-openclaw-missing-oc"
        code, main = run_cli("employee", "create", "--id", "main", "--name", "main", "--role", "operator", "--runtime", "openclaw", "--workspace", str(self.root / "workspace" / "main"))
        self.assertEqual(0, code, main)
        code, submitted = run_cli(
            "task",
            "submit",
            "--from",
            "main",
            "--to",
            "nestcar",
            "--task-id",
            task_id,
            "--title",
            "Submit real OpenClaw task",
            "--description",
            "should block clearly when oc is missing",
            "--priority",
            "P3",
        )
        self.assertEqual(0, code, submitted)
        code, approval = run_cli("approval", "request", "--from", "main", "--action", "external_send", "--target", "nestcar", "--risk", "P3", "--reason", "allow OpenClaw bridge")
        self.assertEqual(0, code, approval)
        code, approved = run_cli("approval", "approve", "--approval-id", approval["approval"]["id"], "--by", "main", "--reason", "test")
        self.assertEqual(0, code, approved)

        missing_oc = self.root / "openclaw" / "scripts" / "oc"
        captured = io.StringIO()
        with mock.patch.object(openclaw_adapter, "OPENCLAW_ROOT", self.root / "openclaw"), contextlib.redirect_stdout(captured):
            code = openclaw_adapter.main(["--agent", "nestcar", "--execute", "--approval-id", approval["approval"]["id"]])
        result = json.loads(captured.getvalue())
        self.assertEqual(1, code, result)
        self.assertFalse(result["ok"])
        self.assertEqual("blocked", result["status"])
        self.assertIn(str(missing_oc), result["blocker"])
        self.assertIn("OpenClaw executable not found", result["blocker"])
        code, task = run_cli("task", "show", "--task-id", task_id)
        self.assertEqual(0, code, task)
        self.assertEqual("blocked", task["task"]["status"])

    def test_codex_adapter_dry_run_writes_task_card_and_evidence(self) -> None:
        code, submitted = run_cli(
            "task",
            "submit",
            "--from",
            "openclaw-main",
            "--to",
            "codex",
            "--task-id",
            "task-codex-dry-run",
            "--title",
            "修复项目脚本",
            "--description",
            "生成 task card 并回传 evidence",
        )
        self.assertEqual(code, 0, submitted)

        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = codex_adapter.main(["--agent", "codex"])
        result = json.loads(captured.getvalue())
        self.assertEqual(0, code, result)
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["executed"])
        self.assertTrue(Path(result["task_card"]).exists())
        self.assertTrue(Path(result["report"]).exists())

        task_card = Path(result["task_card"]).read_text(encoding="utf-8")
        self.assertIn("修复项目脚本", task_card)
        self.assertIn("task-codex-dry-run", task_card)
        self.assertIn(str(self.root / "workspace" / "codex"), task_card)

        code, task = run_cli("task", "show", "--task-id", "task-codex-dry-run")
        self.assertEqual(code, 0, task)
        self.assertEqual("completed", task["task"]["status"])
        self.assertEqual(result["report"], task["task"]["evidence_path"])
        self.assertTrue((self.root / "employees" / "codex" / "heartbeat.json").exists())

    def test_codex_adapter_execute_success_completes_task(self) -> None:
        task_id = "task-codex-execute-ok"
        code, submitted = run_cli(
            "task",
            "submit",
            "--from",
            "openclaw-main",
            "--to",
            "codex",
            "--task-id",
            task_id,
            "--title",
            "运行 codex exec",
            "--description",
            "mock 成功路径",
        )
        self.assertEqual(code, 0, submitted)

        calls: list[dict] = []

        def fake_run_codex(task_card: Path, workspace: Path, output: Path, events: Path, sandbox: str, model: str, isolation: str, sandbox_profile: str) -> tuple[int, str]:
            calls.append({"task_card": task_card, "workspace": workspace, "output": output, "events": events, "sandbox": sandbox, "model": model, "isolation": isolation, "sandbox_profile": sandbox_profile})
            output.write_text("codex completed\n", encoding="utf-8")
            events.write_text(json.dumps({"event": "done"}, ensure_ascii=False) + "\n", encoding="utf-8")
            return 0, f"codex exec -C {workspace} -s {sandbox}"

        captured = io.StringIO()
        with mock.patch.object(codex_adapter.shutil, "which", lambda name: "/usr/local/bin/codex"), mock.patch.object(codex_adapter, "run_codex", fake_run_codex), contextlib.redirect_stdout(captured):
            code = codex_adapter.main(["--agent", "codex", "--execute", "--sandbox", "workspace-write", "--model", "gpt-test", "--isolation", "docker", "--sandbox-profile", "default"])
        result = json.loads(captured.getvalue())
        self.assertEqual(0, code, result)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["executed"])
        self.assertEqual(0, result["codex_exit_code"])
        self.assertEqual("workspace-write", calls[0]["sandbox"])
        self.assertEqual("gpt-test", calls[0]["model"])
        self.assertEqual("docker", calls[0]["isolation"])
        self.assertEqual("default", calls[0]["sandbox_profile"])
        self.assertTrue(Path(result["last_message"]).exists())
        self.assertTrue(Path(result["events"]).exists())

        code, task = run_cli("task", "show", "--task-id", task_id)
        self.assertEqual(code, 0, task)
        self.assertEqual("completed", task["task"]["status"])
        self.assertEqual(result["report"], task["task"]["evidence_path"])
        self.assertIn("runtime execution completed", task["task"]["summary"])
        self.assertIn("codex completed", task["task"]["summary"])
        self.assertIn("Runtime output summary", Path(result["report"]).read_text(encoding="utf-8"))

    def test_codex_adapter_execute_failure_blocks_task_with_report(self) -> None:
        task_id = "task-codex-execute-fail"
        code, submitted = run_cli(
            "task",
            "submit",
            "--from",
            "openclaw-main",
            "--to",
            "codex",
            "--task-id",
            task_id,
            "--title",
            "运行 codex exec 失败",
            "--description",
            "mock 失败路径",
        )
        self.assertEqual(code, 0, submitted)

        def fake_run_codex(task_card: Path, workspace: Path, output: Path, events: Path, sandbox: str, model: str, isolation: str, sandbox_profile: str) -> tuple[int, str]:
            output.write_text("codex failed\n", encoding="utf-8")
            events.write_text(json.dumps({"event": "error"}, ensure_ascii=False) + "\n", encoding="utf-8")
            return 7, f"codex exec -C {workspace} -s {sandbox}"

        captured = io.StringIO()
        with mock.patch.object(codex_adapter.shutil, "which", lambda name: "/usr/local/bin/codex"), mock.patch.object(codex_adapter, "run_codex", fake_run_codex), contextlib.redirect_stdout(captured):
            code = codex_adapter.main(["--agent", "codex", "--execute"])
        result = json.loads(captured.getvalue())
        self.assertEqual(7, code, result)
        self.assertFalse(result["ok"])
        self.assertEqual(7, result["codex_exit_code"])

        code, task = run_cli("task", "show", "--task-id", task_id)
        self.assertEqual(code, 0, task)
        self.assertEqual("blocked", task["task"]["status"])
        self.assertIn("runtime execution failed exit_code=7", task["task"]["blocker"])
        self.assertIn("codex failed", task["task"]["blocker"])
        self.assertIn("Runtime output summary", Path(result["report"]).read_text(encoding="utf-8"))

    def test_antigravity_adapter_can_return_gui_results(self) -> None:
        workspace = self.root / "workspace" / "antigravity"
        code, employee = run_cli(
            "employee",
            "create",
            "--id",
            "antigravity",
            "--name",
            "Antigravity",
            "--role",
            "ide-agent",
            "--runtime",
            "antigravity",
            "--workspace",
            str(workspace),
        )
        self.assertEqual(code, 0, employee)
        self.mark_active("antigravity")

        task_id = "task-antigravity-return-ok"
        code, submitted = run_cli("task", "submit", "--from", "openclaw-main", "--to", "antigravity", "--task-id", task_id, "--title", "GUI flow")
        self.assertEqual(code, 0, submitted)
        code, claimed = run_cli("task", "claim", "--agent", "antigravity", "--task-id", task_id)
        self.assertEqual(code, 0, claimed)

        evidence = self.root / "antigravity-evidence.md"
        evidence.write_text("GUI result evidence\n", encoding="utf-8")
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = antigravity_adapter.main(["--agent", "antigravity", "--complete", "--task-id", task_id, "--summary", "GUI task finished", "--evidence", str(evidence)])
        result = json.loads(captured.getvalue())
        self.assertEqual(0, code, result)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["returned"])
        self.assertTrue(Path(result["report"]).exists())

        code, task = run_cli("task", "show", "--task-id", task_id)
        self.assertEqual(code, 0, task)
        self.assertEqual("completed", task["task"]["status"])
        self.assertEqual("GUI task finished", task["task"]["summary"])
        self.assertEqual(str(evidence), task["task"]["evidence_path"])

        blocked_id = "task-antigravity-return-blocked"
        code, submitted_blocked = run_cli("task", "submit", "--from", "openclaw-main", "--to", "antigravity", "--task-id", blocked_id, "--title", "GUI blocked")
        self.assertEqual(code, 0, submitted_blocked)
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = antigravity_adapter.main(["--agent", "antigravity", "--block", "--task-id", blocked_id, "--blocker", "GUI login required"])
        blocked = json.loads(captured.getvalue())
        self.assertEqual(0, code, blocked)
        self.assertEqual("blocked", blocked["status"])

        code, blocked_task = run_cli("task", "show", "--task-id", blocked_id)
        self.assertEqual(code, 0, blocked_task)
        self.assertEqual("blocked", blocked_task["task"]["status"])
        self.assertEqual("GUI login required", blocked_task["task"]["blocker"])

    def test_antigravity_managed_attempt_records_progress_and_evidence(self) -> None:
        workspace = self.root / "workspace" / "antigravity"
        for employee_id, role, runtime in [
            ("main", "operator", "openclaw"),
            ("hermes", "supervisor", "hermes"),
            ("antigravity", "ide-agent", "antigravity"),
        ]:
            code, employee = run_cli(
                "employee",
                "create",
                "--id",
                employee_id,
                "--name",
                employee_id,
                "--role",
                role,
                "--runtime",
                runtime,
                "--workspace",
                str(workspace if employee_id == "antigravity" else self.root / "workspace" / employee_id),
            )
            self.assertEqual(code, 0, employee)
            self.mark_active(employee_id)
        task_id = "task-antigravity-managed-ok"
        code, submitted = run_cli("task", "submit", "--from", "main", "--to", "antigravity", "--task-id", task_id, "--title", "Managed Antigravity task", "--description", "Inspect dashboard and return structured evidence")
        self.assertEqual(code, 0, submitted)
        structured_reply = "\n".join(
            [
                "status: done",
                "current_action: inspected dashboard task controls and API retry path",
                "changed_files: company_kernel/company_dashboard.py",
                "verification_run: pytest tests/test_company_kernel_core.py::CompanyKernelCoreTest::test_dashboard_renders_managed_task_control_buttons -q passed",
                "browser_check: not needed for adapter unit test",
                "blocker: -",
                "eta: -",
            ]
        )
        captured = io.StringIO()
        with mock.patch.object(antigravity_adapter, "run_agy_print", return_value=(0, structured_reply, "")), contextlib.redirect_stdout(captured):
            code = antigravity_adapter.main(["--agent", "antigravity", "--managed-attempt", "--by", "hermes", "--timeout", "180"])
        result = json.loads(captured.getvalue())
        self.assertEqual(0, code, result)
        self.assertTrue(result["ok"], result)
        self.assertEqual(task_id, result["task_id"])
        self.assertTrue(result["managed_attempt"])
        self.assertEqual("success", result["attempt"]["status"])
        self.assertEqual("completed", result["task"]["status"])
        self.assertTrue(Path(result["evidence"]).exists())

        code, shown = run_cli("task", "show", "--task-id", task_id)
        self.assertEqual(0, code, shown)
        self.assertEqual("completed", shown["task"]["status"])
        self.assertEqual(1, len(shown["attempts"]))
        self.assertEqual("success", shown["attempts"][0]["status"])
        event_types = [event["event_type"] for event in shown["events"]]
        self.assertIn("task.progress", event_types)
        self.assertIn("task.done", event_types)

    def test_antigravity_managed_attempt_blocks_without_structured_evidence(self) -> None:
        workspace = self.root / "workspace" / "antigravity"
        for employee_id, role, runtime in [
            ("main", "operator", "openclaw"),
            ("hermes", "supervisor", "hermes"),
            ("antigravity", "ide-agent", "antigravity"),
        ]:
            code, employee = run_cli(
                "employee",
                "create",
                "--id",
                employee_id,
                "--name",
                employee_id,
                "--role",
                role,
                "--runtime",
                runtime,
                "--workspace",
                str(workspace if employee_id == "antigravity" else self.root / "workspace" / employee_id),
            )
            self.assertEqual(code, 0, employee)
            self.mark_active(employee_id)
        task_id = "task-antigravity-managed-blocked"
        code, submitted = run_cli("task", "submit", "--from", "main", "--to", "antigravity", "--task-id", task_id, "--title", "Managed Antigravity blocked")
        self.assertEqual(code, 0, submitted)
        blocked_reply = "\n".join(
            [
                "status: blocked",
                "current_action: tried to inspect dashboard",
                "changed_files: -",
                "verification_run: agy print failed because login expired",
                "browser_check: -",
                "blocker: Antigravity login expired",
                "eta: unknown",
            ]
        )
        captured = io.StringIO()
        with mock.patch.object(antigravity_adapter, "run_agy_print", return_value=(0, blocked_reply, "")), contextlib.redirect_stdout(captured):
            code = antigravity_adapter.main(["--agent", "antigravity", "--managed-attempt", "--by", "hermes"])
        result = json.loads(captured.getvalue())
        self.assertEqual(1, code, result)
        self.assertFalse(result["ok"])
        self.assertEqual("blocked", result["status"])
        self.assertIn("Antigravity login expired", result["blocker"])
        self.assertEqual("failed", result["attempt"]["status"])

        code, shown = run_cli("task", "show", "--task-id", task_id)
        self.assertEqual(0, code, shown)
        self.assertEqual("blocked", shown["task"]["status"])
        self.assertEqual("", shown["task"]["evidence_path"])
        self.assertEqual(1, len(shown["attempts"]))
        self.assertEqual("failed", shown["attempts"][0]["status"])
        event_types = [event["event_type"] for event in shown["events"]]
        self.assertIn("task.progress", event_types)
        self.assertIn("task.blocked", event_types)
        self.assertNotIn("task.done", event_types)

    def test_antigravity_adapter_direct_message_writes_gui_brief(self) -> None:
        workspace = self.root / "workspace" / "antigravity"
        code, employee = run_cli(
            "employee",
            "create",
            "--id",
            "main",
            "--name",
            "main",
            "--role",
            "operator",
            "--runtime",
            "openclaw",
            "--workspace",
            str(self.root / "workspace" / "main"),
        )
        self.assertEqual(code, 0, employee)
        code, employee = run_cli(
            "employee",
            "create",
            "--id",
            "antigravity",
            "--name",
            "Antigravity",
            "--role",
            "developer",
            "--runtime",
            "antigravity",
            "--workspace",
            str(workspace),
        )
        self.assertEqual(code, 0, employee)
        captured = io.StringIO()
        with mock.patch.object(antigravity_adapter, "run_agy_print", return_value=(127, "", "agy command not found")), contextlib.redirect_stdout(captured):
            code = antigravity_adapter.main(
                [
                    "--agent",
                    "antigravity",
                    "--direct-source",
                    "main",
                    "--direct-session-key",
                    "agent:antigravity:main",
                    "--direct-message",
                    "请查看每个页面并给出前端优化。只回复 ANTIGRAVITY_BRIEF_OK",
                ]
            )
        result = json.loads(captured.getvalue())
        self.assertEqual(1, code, result)
        self.assertFalse(result["ok"])
        self.assertEqual("ANTIGRAVITY_BRIEF_OK", result["reply"])
        self.assertTrue(Path(result["brief"]).exists())
        self.assertTrue(Path(result["report"]).exists())
        self.assertTrue(result["blocked_execution"])
        self.assertFalse(result["activation_eligible"])
        self.assertTrue(result["status_delivery"]["ok"], result)
        code, main_messages = run_cli("message", "list", "--agent", "main")
        self.assertEqual(0, code, main_messages)
        self.assertTrue(any(message["source_agent"] == "antigravity" and "status: blocked" in message["body"] for message in main_messages["messages"]))

    def test_antigravity_direct_requires_exact_lightweight_token(self) -> None:
        workspace = self.root / "workspace" / "antigravity"
        code, employee = run_cli("employee", "create", "--id", "main", "--name", "main", "--role", "operator", "--runtime", "openclaw", "--workspace", str(self.root / "workspace" / "main"))
        self.assertEqual(code, 0, employee)
        code, employee = run_cli("employee", "create", "--id", "antigravity", "--name", "Antigravity", "--role", "developer", "--runtime", "antigravity", "--workspace", str(workspace))
        self.assertEqual(code, 0, employee)
        captured = io.StringIO()
        with mock.patch.object(antigravity_adapter, "run_agy_print", return_value=(0, "我收到 ANTIGRAVITY_DIRECT_OK", "")), contextlib.redirect_stdout(captured):
            code = antigravity_adapter.main(["--agent", "antigravity", "--direct-source", "main", "--direct-session-key", "agent:antigravity:main", "--direct-message", "只回复：ANTIGRAVITY_DIRECT_OK"])
        result = json.loads(captured.getvalue())
        self.assertEqual(1, code, result)
        self.assertFalse(result["ok"])
        self.assertFalse(result["activation_eligible"])
        self.assertIn("expected exact token", result["blocker"])
        self.assertTrue(result["blocked_execution"])

    def test_antigravity_readonly_done_allows_no_changed_files_with_verification(self) -> None:
        reply = "\n".join(
            [
                "status: done",
                "current_action: read README.md and company_dashboard.py",
                "changed_files: -",
                "verification_run: python3 -m py_compile company_kernel/company_dashboard.py passed",
                "browser_check: -",
                "blocker: -",
                "eta: -",
            ]
        )
        validation = antigravity_adapter.validate_agy_reply(
            message="员工上岗执行验收。请在当前项目内做只读检查，不要改文件。",
            reply=reply,
            before_files=[],
            after_files=[],
        )
        self.assertTrue(validation["ok"], validation)
        self.assertTrue(validation["activation_eligible"], validation)

    def test_antigravity_planning_only_timeout_reply_is_specific_blocker(self) -> None:
        reply = "\n".join(
            [
                "I will inspect the dashboard template.",
                "I will run tests.",
                "Error: timed out waiting for response",
            ]
        )
        validation = antigravity_adapter.validate_agy_reply(
            message="请评测 dashboard 并返回结构化 evidence",
            reply=reply,
            before_files=[],
            after_files=[],
        )
        self.assertFalse(validation["ok"], validation)
        self.assertEqual("blocked", validation["status"])
        self.assertIn("planning_only_or_timeout", validation["blocker"])

    def test_antigravity_managed_prompt_requires_concrete_verification(self) -> None:
        conn = antigravity_adapter.connect()
        try:
            conn.execute(
                "INSERT INTO tasks (id, source_agent, target_agent, title, description, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "task-antigravity-prompt-contract",
                    "main",
                    "antigravity",
                    "Inspect dashboard retry button",
                    "Read dashboard code and verify retry endpoint wiring.",
                    "submitted",
                    "2026-06-07T00:00:00+00:00",
                    "2026-06-07T00:00:00+00:00",
                ),
            )
            conn.commit()
            task = conn.execute("SELECT * FROM tasks WHERE id = ?", ("task-antigravity-prompt-contract",)).fetchone()
        finally:
            conn.close()
        prompt = antigravity_adapter.build_managed_task_prompt(task)
        self.assertIn("verification_run must be a concrete command", prompt)
        self.assertIn("python3 -m py_compile company_kernel/company_dashboard.py", prompt)
        self.assertIn("status: blocked", prompt)

    def test_antigravity_complex_direct_blocks_stale_context_without_evidence(self) -> None:
        workspace = self.root / "workspace" / "antigravity"
        code, employee = run_cli("employee", "create", "--id", "main", "--name", "main", "--role", "operator", "--runtime", "openclaw", "--workspace", str(self.root / "workspace" / "main"))
        self.assertEqual(code, 0, employee)
        code, employee = run_cli("employee", "create", "--id", "antigravity", "--name", "Antigravity", "--role", "developer", "--runtime", "antigravity", "--workspace", str(workspace))
        self.assertEqual(code, 0, employee)
        stale_reply = "\n".join(
            [
                "status: done",
                "current_action: approved approval-route-task-hermes and got HERMES_LOCAL_VERIFY_OK",
                "changed_files: -",
                "verification_run: -",
                "browser_check: -",
                "blocker: -",
            ]
        )
        captured = io.StringIO()
        with mock.patch.object(antigravity_adapter, "run_agy_print", return_value=(0, stale_reply, "")), contextlib.redirect_stdout(captured):
            code = antigravity_adapter.main(["--agent", "antigravity", "--direct-source", "main", "--direct-session-key", "agent:antigravity:main", "--direct-message", "请重构 dashboard 前端并运行浏览器验证"])
        result = json.loads(captured.getvalue())
        self.assertEqual(1, code, result)
        self.assertFalse(result["ok"])
        self.assertFalse(result["activation_eligible"])
        self.assertIn("blocked_context_mismatch", result["blocker"])
        self.assertEqual("execution", result["validation"]["mode"])
        self.assertTrue(result["blocked_execution"])

    def test_attendance_sweep_uses_session_and_spool_evidence(self) -> None:
        for agent in ("main", "nestcar", "codex"):
            code, created = run_cli(
                "employee",
                "create",
                "--id",
                agent,
                "--name",
                agent,
                "--role",
                "agent",
                "--runtime",
                "openclaw" if agent in {"main", "nestcar"} else "codex",
                "--workspace",
                str(self.root / "workspace" / agent),
            )
            self.assertEqual(code, 0, created)

        (self.root / "openclaw" / "agents" / "main" / "sessions").mkdir(parents=True)
        (self.root / "openclaw" / "agents" / "nestcar" / "sessions").mkdir(parents=True)
        (self.root / "openclaw" / "agents" / "codex" / "sessions").mkdir(parents=True)
        (self.root / "openclaw" / "agents" / "main" / "sessions" / "sessions.json").write_text('{"s1": {}}', encoding="utf-8")
        (self.root / "openclaw" / "agents" / "nestcar" / "sessions" / "sessions.json").write_text('{"s1": {}}', encoding="utf-8")
        (self.root / "openclaw" / "agents" / "codex" / "sessions" / "sessions.json").write_text('{}', encoding="utf-8")
        spool = self.root / "openclaw" / "telegram" / "ingress-spool-nestcar"
        spool.mkdir(parents=True)
        (spool / "0000000001.json.processing").write_text('{"update_id": 1}', encoding="utf-8")

        code, swept = run_cli("attendance", "sweep", "--source", "main", "--agents", "main,nestcar,codex", "--sweep-id", "attendance-test", "--no-probe-replies")
        self.assertEqual(code, 1, swept)
        rows = {row["agent"]: row for row in swept["employees"]}
        self.assertEqual("online", rows["main"]["status"])
        self.assertEqual("main 报到", rows["main"]["reply"])
        self.assertEqual("worker_stalled", rows["nestcar"]["status"])
        self.assertEqual("session_missing", rows["codex"]["status"])
        self.assertEqual(1, swept["counts"]["online"])
        self.assertEqual(1, swept["counts"]["worker_stalled"])
        self.assertTrue(Path(swept["evidence"]["json"]).exists())

    def test_attendance_sweep_can_require_exact_agent_reply(self) -> None:
        code, created = run_cli(
            "employee",
            "create",
            "--id",
            "main",
            "--name",
            "main",
            "--role",
            "agent",
            "--runtime",
            "openclaw",
            "--workspace",
            str(self.root / "workspace" / "main"),
        )
        self.assertEqual(code, 0, created)
        session_dir = self.root / "openclaw" / "agents" / "main" / "sessions"
        session_dir.mkdir(parents=True)
        (session_dir / "sessions.json").write_text('{"s1": {}}', encoding="utf-8")

        def fake_run(cmd, cwd=None, text=None, capture_output=None, timeout=None):
            class Result:
                returncode = 0
                stdout = json.dumps({"result": {"payloads": [{"text": "main 在岗"}]}})
                stderr = ""
            return Result()

        with mock.patch.object(companyctl.subprocess, "run", side_effect=fake_run):
            code, swept = run_cli("attendance", "sweep", "--source", "main", "--agents", "main", "--sweep-id", "attendance-reply-test")
        self.assertEqual(code, 0, swept)
        row = swept["employees"][0]
        self.assertEqual("online", row["status"])
        self.assertEqual("main 在岗", row["reply"])
        self.assertEqual("agent_reply_matched", row["reason"])
        self.assertTrue(row["reply_probe"]["ok"])
        self.assertTrue(Path(swept["evidence"]["latest"]).exists())
        self.assertIn("no_reply", swept["classification_guide"])

    def test_attendance_sweep_can_mark_codex_online_via_adapter(self) -> None:
        code, created = run_cli(
            "employee",
            "create",
            "--id",
            "codex",
            "--name",
            "Codex",
            "--role",
            "developer",
            "--runtime",
            "codex",
            "--workspace",
            str(self.root / "workspace" / "codex"),
        )
        self.assertEqual(code, 0, created)
        session_dir = self.root / "openclaw" / "agents" / "codex" / "sessions"
        session_dir.mkdir(parents=True)
        (session_dir / "sessions.json").write_text('{}', encoding="utf-8")

        def fake_run(cmd, cwd=None, text=None, capture_output=None, timeout=None):
            self.assertIn("--attendance-probe", cmd)
            class Result:
                returncode = 0
                stdout = json.dumps({"ok": True, "processed": 0, "agent": "codex", "attendance_probe": True, "reply": "codex 在岗"})
                stderr = ""
            return Result()

        with mock.patch.object(companyctl.subprocess, "run", side_effect=fake_run):
            code, swept = run_cli("attendance", "sweep", "--source", "main", "--agents", "codex", "--sweep-id", "attendance-codex-test")
        self.assertEqual(code, 0, swept)
        row = swept["employees"][0]
        self.assertEqual("online", row["status"])
        self.assertEqual("codex 在岗", row["reply"])
        self.assertEqual("adapter_heartbeat_matched", row["reply_probe"]["reason"])

    def test_attendance_sweep_can_mark_antigravity_online_via_adapter(self) -> None:
        code, created = run_cli(
            "employee",
            "create",
            "--id",
            "antigravity",
            "--name",
            "Antigravity",
            "--role",
            "developer",
            "--runtime",
            "antigravity",
            "--workspace",
            str(self.root / "workspace" / "antigravity"),
        )
        self.assertEqual(code, 0, created)

        def fake_run(cmd, cwd=None, text=None, capture_output=None, timeout=None):
            self.assertIn("company-antigravity-adapter", cmd[0])
            self.assertIn("--attendance-probe", cmd)

            class Result:
                returncode = 0
                stdout = json.dumps({"ok": True, "processed": 0, "agent": "antigravity", "attendance_probe": True, "reply": "antigravity 在岗"})
                stderr = ""

            return Result()

        with mock.patch.object(companyctl.subprocess, "run", side_effect=fake_run):
            code, swept = run_cli("attendance", "sweep", "--source", "main", "--agents", "antigravity", "--sweep-id", "attendance-antigravity-test")
        self.assertEqual(code, 0, swept)
        row = swept["employees"][0]
        self.assertEqual("online", row["status"])
        self.assertEqual("antigravity 在岗", row["reply"])
        self.assertEqual("adapter_heartbeat_matched", row["reply_probe"]["reason"])

    def test_employee_onboard_writes_config_and_blocks_test_task_until_verified(self) -> None:
        code, onboard = run_cli(
            "employee",
            "onboard",
            "--id",
            "reviewer",
            "--name",
            "Reviewer",
            "--role",
            "reviewer",
            "--runtime",
            "local",
            "--workspace",
            str(self.root / "workspace" / "reviewer"),
            "--alias",
            "rev",
            "--skills",
            "review,qa",
            "--tools",
            "companyctl",
            "--task-types",
            "review",
            "--can-talk-to",
            "ops,codex",
            "--can-assign-to",
            "codex",
            "--channel",
            "engineering",
            "--default-user-reply-deliver",
            "--default-user-reply-channel",
            "telegram",
            "--default-user-reply-account",
            "default",
            "--default-user-reply-to",
            "current",
            "--create-test-task",
        )
        self.assertEqual(code, 0, onboard)
        self.assertEqual("reviewer", onboard["employee"]["id"])
        self.assertEqual(["review", "qa"], onboard["capabilities"]["skills"])
        self.assertTrue(onboard["test_task"]["blocked"])
        self.assertEqual("onboarding test task requires a verified active employee", onboard["test_task"]["reason"])
        self.assertIn("employee verify-direct", onboard["test_task"]["required_command"])

        communication = json.loads((self.root / "config" / "company_communications.json").read_text(encoding="utf-8"))
        self.assertEqual("reviewer", communication["aliases"]["rev"])
        self.assertEqual(["video-ops", "codex"], communication["employees"]["reviewer"]["can_talk_to"])
        self.assertIn("reviewer", communication["channels"]["engineering"]["participants"])
        self.assertTrue(communication["employees"]["reviewer"]["default_user_reply_deliver"])
        self.assertEqual("telegram", communication["employees"]["reviewer"]["default_user_reply_channel"])
        self.assertEqual("default", communication["employees"]["reviewer"]["default_user_reply_account"])
        self.assertEqual("current", communication["employees"]["reviewer"]["default_user_reply_to"])

        code, sent = run_cli("message", "send", "--from", "ops", "--to", "rev", "--body", "欢迎入职")
        self.assertEqual(code, 0, sent)
        self.assertEqual("reviewer", sent["message"]["target_agent"])

        code, claimed = run_cli("task", "claim", "--agent", "rev", "--task-id", "task-onboard-reviewer")
        self.assertEqual(code, 1, claimed)
        self.assertEqual("no claimable task", claimed["error"])

    def test_employee_update_changes_profile_through_companyctl(self) -> None:
        workspace = self.root / "employees" / "profile-reviewer"
        code, created = run_cli(
            "employee",
            "create",
            "--id",
            "profile-reviewer",
            "--name",
            "Profile Reviewer",
            "--role",
            "reviewer",
            "--runtime",
            "local",
            "--workspace",
            str(workspace),
        )
        self.assertEqual(code, 0, created)
        code, updated = run_cli(
            "employee",
            "update",
            "--id",
            "profile-reviewer",
            "--name",
            "Profile QA",
            "--role",
            "qa",
            "--status",
            "candidate",
        )
        self.assertEqual(code, 0, updated)
        self.assertTrue(updated["changed"])
        self.assertEqual("Profile QA", updated["employee"]["name"])
        self.assertEqual("qa", updated["employee"]["role"])
        self.assertEqual("candidate", updated["employee"]["status"])
        profile = json.loads((self.root / "employees" / "profile-reviewer" / "profile.json").read_text(encoding="utf-8"))
        self.assertEqual("Profile QA", profile["name"])
        self.assertEqual("candidate", profile["status"])

    def test_employee_onboard_scaffolds_managed_workspace_and_offboards_safely(self) -> None:
        workspace = self.root / "employees" / "reviewer-scaffold"
        code, onboard = run_cli(
            "employee",
            "onboard",
            "--id",
            "reviewer-scaffold",
            "--name",
            "Scaffold Reviewer",
            "--role",
            "code-reviewer",
            "--runtime",
            "hermes",
            "--workspace",
            str(workspace),
            "--alias",
            "scaffold-reviewer",
        )
        self.assertEqual(code, 0, onboard)
        self.assertTrue((workspace / "SOUL.md").exists())
        self.assertTrue((workspace / "AGENTS.md").exists())
        self.assertIn("Scaffold Reviewer", (workspace / "SOUL.md").read_text(encoding="utf-8"))
        self.assertIn(str((workspace / "SOUL.md").resolve()), onboard["scaffolded_files"])

        code, dry = run_cli("employee", "offboard", "--id", "scaffold-reviewer", "--dry-run")
        self.assertEqual(code, 0, dry)
        self.assertTrue(dry["dry_run"])
        self.assertEqual("soft-delete", dry["action"])

        code, soft = run_cli("employee", "offboard", "--id", "scaffold-reviewer")
        self.assertEqual(code, 0, soft)
        self.assertEqual("soft-delete", soft["action"])
        conn = companyctl.connect()
        try:
            row = conn.execute("SELECT status FROM employees WHERE id = 'reviewer-scaffold'").fetchone()
            self.assertEqual("archived", row["status"])
        finally:
            conn.close()
        self.assertTrue(workspace.exists())
        config = json.loads((self.root / "config" / "company_communications.json").read_text(encoding="utf-8"))
        self.assertNotIn("scaffold-reviewer", config["aliases"])
        self.assertNotIn("reviewer-scaffold", config["employees"])

        code, hard_dry = run_cli("employee", "offboard", "--id", "reviewer-scaffold", "--hard-delete", "--dry-run")
        self.assertEqual(code, 0, hard_dry)
        self.assertIn(str(workspace), hard_dry["deleted_paths"])

        code, hard = run_cli("employee", "offboard", "--id", "reviewer-scaffold", "--hard-delete")
        self.assertEqual(code, 0, hard)
        self.assertEqual("hard-delete", hard["action"])
        self.assertFalse(workspace.exists())
        conn = companyctl.connect()
        try:
            row = conn.execute("SELECT * FROM employees WHERE id = 'reviewer-scaffold'").fetchone()
            self.assertIsNone(row)
        finally:
            conn.close()

    def test_employee_onboard_does_not_scaffold_external_workspace(self) -> None:
        external = Path("/private/tmp/company-kernel-external-agent")
        code, onboard = run_cli(
            "employee",
            "onboard",
            "--id",
            "external-agent",
            "--name",
            "External Agent",
            "--role",
            "external",
            "--runtime",
            "local",
            "--workspace",
            str(external),
        )
        self.assertEqual(code, 0, onboard)
        self.assertEqual([], onboard["scaffolded_files"])
        self.assertFalse((external / "AGENTS.md").exists())

    def test_daemon_uses_global_config_master_workspace_root(self) -> None:
        master_root = self.root / "master-daemon-root"
        config_path = self.root / ".gemini" / "antigravity" / "company_kernel_config.json"
        (master_root / "config").mkdir(parents=True)
        (master_root / "logs").mkdir()
        (master_root / "state" / "daemon").mkdir(parents=True)
        (master_root / "config" / "daemon.json").write_text(json.dumps({"heartbeat_agents": ["main"]}), encoding="utf-8")
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            json.dumps(
                {
                    "master_workspace_root": str(master_root),
                    "database_path": str(master_root / "company.sqlite"),
                    "log_dir": str(master_root / "logs"),
                    "gateway_port": 8780,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with mock.patch.dict("os.environ", {"COMPANY_KERNEL_CONFIG_PATH": str(config_path)}, clear=False):
            paths = company_daemon.resolve_daemon_paths()
            loaded = company_daemon.load_config(paths["config_path"])
        self.assertEqual(master_root.resolve(), paths["root"])
        self.assertEqual((master_root / "config" / "daemon.json").resolve(), paths["config_path"])
        self.assertEqual((master_root / "state" / "daemon").resolve(), paths["state_dir"])
        self.assertEqual((master_root / "logs" / "daemon.log").resolve(), paths["log_path"])
        self.assertEqual(["main"], loaded["heartbeat_agents"])

    def test_daemon_resolves_runtime_heartbeat_agents_without_duplicates(self) -> None:
        for employee_id in ["main", "nestcar"]:
            code, obj = run_cli(
                "employee",
                "create",
                "--id",
                employee_id,
                "--name",
                employee_id,
                "--role",
                "business-agent",
                "--runtime",
                "openclaw",
                "--workspace",
                str(self.root / "workspace" / employee_id),
            )
            self.assertEqual(code, 0, obj)
        code, inactive = run_cli(
            "employee",
            "create",
            "--id",
            "retired",
            "--name",
            "retired",
            "--role",
            "business-agent",
            "--runtime",
            "openclaw",
            "--workspace",
            str(self.root / "workspace" / "retired"),
        )
        self.assertEqual(code, 0, inactive)
        code, reviewer = run_cli(
            "employee",
            "create",
            "--id",
            "reviewer-runtime",
            "--name",
            "reviewer-runtime",
            "--role",
            "reviewer",
            "--runtime",
            "local",
            "--workspace",
            str(self.root / "workspace" / "reviewer-runtime"),
        )
        self.assertEqual(code, 0, reviewer)
        conn = companyctl.connect()
        try:
            conn.execute("UPDATE employees SET status = 'active' WHERE id IN ('nestcar', 'openclaw-main', 'main', 'reviewer-runtime')")
            conn.execute("UPDATE employees SET status = 'inactive' WHERE id = 'retired'")
            conn.commit()
        finally:
            conn.close()

        agents = company_daemon.resolve_heartbeat_agents({"heartbeat_agents": ["openclaw-main", "main"], "heartbeat_runtimes": ["openclaw"]})
        self.assertEqual(["openclaw-main", "main", "nestcar"], agents)
        wildcard_agents = company_daemon.resolve_heartbeat_agents({"heartbeat_agents": ["openclaw-main"], "heartbeat_runtimes": ["*"]})
        self.assertIn("reviewer-runtime", wildcard_agents)
        self.assertIn("nestcar", wildcard_agents)
        self.assertNotIn("retired", wildcard_agents)
        self.assertEqual(1, wildcard_agents.count("openclaw-main"))

    def test_daemon_tick_can_sync_openclaw_runtime_and_heartbeats(self) -> None:
        calls = []

        def fake_run_companyctl(*args: str) -> dict:
            calls.append(args)
            return {"returncode": 0, "stdout": json.dumps({"ok": True}), "stderr": ""}

        with mock.patch.object(company_daemon, "run_companyctl", side_effect=fake_run_companyctl):
            state = company_daemon.tick(
                {
                    "sync_openclaw_runtime": True,
                    "sync_openclaw_heartbeats": True,
                    "run_repair": False,
                    "run_scheduler": False,
                    "run_supervisor_delivery_loop": False,
                    "run_retries": False,
                    "heartbeat_agents": [],
                    "heartbeat_runtimes": [],
                    "adapter_workers": [],
                }
            )
        self.assertTrue(state["ok"])
        self.assertEqual(("employee", "sync-openclaw-runtime"), calls[0])
        self.assertEqual(("employee", "sync-openclaw-heartbeats"), calls[1])
        summary = company_daemon.summarize_state(state)
        self.assertEqual(2, summary["counts"]["openclaw_sync"])

    def test_daemon_records_adapter_runs_for_dashboard(self) -> None:
        code, submitted = run_cli("task", "submit", "--from", "openclaw-main", "--to", "codex", "--task-id", "task-adapter-run-dashboard", "--title", "adapter run dashboard")
        self.assertEqual(code, 0, submitted)
        trace_id = submitted["task"]["metadata"]["trace_id"]
        state = {
            "ok": True,
            "agent": "codex",
            "command": "company-codex-adapter",
            "processed": 1,
            "stdout": "api_key=sk-dashboardSECRET1234567890 reading /Users/shift/.ssh/id_rsa",
            "stderr": f"token=sk-dashboardSECRET1234567890 blocked on {self.root / '.env'}",
            "runs": [
                {
                    "result": {"stdout": "authorization=sk-dashboardSECRET1234567890", "stderr": "/Users/shift/project/profile.json"},
                    "parsed_stdout": {"task_id": "task-adapter-run-dashboard", "summary": "safe dashboard progress"},
                }
            ],
            "at": "2026-06-03T04:30:00+07:00",
            "state_file": str(self.root / "state" / "daemon" / "workers" / "codex.json"),
        }
        company_daemon.record_adapter_run(state)

        conn = companyctl.connect()
        try:
            rows = conn.execute("SELECT * FROM adapter_runs").fetchall()
            self.assertEqual(1, len(rows))
            self.assertEqual("codex", rows[0]["agent_id"])
            self.assertEqual("task-adapter-run-dashboard", rows[0]["task_id"])
            self.assertEqual(trace_id, rows[0]["trace_id"])
            self.assertEqual("company-codex-adapter", rows[0]["command"])
            self.assertEqual(1, rows[0]["ok"])
            self.assertEqual(1, rows[0]["processed"])
            self.assertEqual(1, rows[0]["attempt"])
            self.assertEqual("", rows[0]["next_retry_at"])
        finally:
            conn.close()

        code, listed = run_cli("runtime", "adapter-runs", "--agent", "codex", "--status", "ok")
        self.assertEqual(code, 0, listed)
        self.assertEqual(["task-adapter-run-dashboard"], [run["task_id"] for run in listed["adapter_runs"]])
        self.assertEqual([trace_id], [run["trace_id"] for run in listed["adapter_runs"]])
        run_id = listed["adapter_runs"][0]["id"]
        code, shown = run_cli("runtime", "adapter-run", "show", "--run-id", run_id)
        self.assertEqual(code, 0, shown)
        self.assertEqual("task-adapter-run-dashboard", shown["adapter_run"]["task_id"])
        self.assertEqual("task-adapter-run-dashboard", shown["result"]["runs"][0]["parsed_stdout"]["task_id"])
        self.assertIn("sk-dashboardSECRET1234567890", json.dumps(shown, ensure_ascii=False))
        code, shown_summary = run_cli("runtime", "adapter-run", "show", "--run-id", run_id, "--summary")
        self.assertEqual(code, 0, shown_summary)
        self.assertNotIn("result_json", shown_summary["adapter_run"])
        self.assertNotIn("result", shown_summary)
        self.assertEqual("task-adapter-run-dashboard", shown_summary["result_summary"]["runs"][0]["task_id"])
        summary_json = json.dumps(shown_summary, ensure_ascii=False)
        self.assertIn("sanitized_log", shown_summary["result_summary"])
        self.assertIn("safe dashboard progress", summary_json)
        self.assertNotIn("sk-dashboardSECRET1234567890", summary_json)
        self.assertNotIn("id_rsa", summary_json)
        self.assertNotIn(".env", summary_json)
        self.assertNotIn("profile.json", summary_json)
        code, failed = run_cli("runtime", "adapter-runs", "--status", "failed", "--unacknowledged-only")
        self.assertEqual(code, 0, failed)
        self.assertEqual([], failed["adapter_runs"])

        output = self.root / "state" / "dashboard-adapter-runs.html"
        with contextlib.redirect_stdout(io.StringIO()):
            code = company_dashboard.main(["--output", str(output), "--variant", "basic"])
        self.assertEqual(0, code)
        html = output.read_text(encoding="utf-8")
        self.assertIn("Adapter Runs", html)
        self.assertIn("task-adapter-run-dashboard", html)
        self.assertIn("company-codex-adapter", html)
        self.assertIn("safe dashboard progress", html)
        self.assertIn("sanitized_log", html)
        self.assertNotIn("sk-dashboardSECRET1234567890", html)
        self.assertNotIn("id_rsa", html)
        self.assertNotIn(".env", html)
        self.assertNotIn("profile.json", html)

    def test_daemon_worker_processes_task_end_to_end(self) -> None:
        code, submitted = run_cli(
            "task",
            "submit",
            "--from",
            "openclaw-main",
            "--to",
            "codex",
            "--task-id",
            "task-daemon-worker-e2e",
            "--title",
            "daemon worker e2e",
            "--description",
            "daemon should claim, write evidence, complete, heartbeat, and record adapter run",
        )
        self.assertEqual(code, 0, submitted)

        config = {
            "version": 1,
            "run_repair": False,
            "run_scheduler": False,
            "heartbeat_agents": [],
            "adapter_workers": [
                {
                    "agent": "codex",
                    "enabled": True,
                    "command": "company-adapter-worker",
                    "args": ["--dry-run"],
                    "max_tasks_per_tick": 1,
                }
            ],
        }
        state = company_daemon.tick(config)

        self.assertTrue(state["ok"], state)
        adapter_steps = [item for item in state["results"] if item["step"] == "adapter.codex"]
        self.assertEqual(1, len(adapter_steps), state)
        parsed = json.loads(adapter_steps[0]["result"]["stdout"])
        self.assertEqual(1, parsed["processed"])
        self.assertEqual("task-daemon-worker-e2e", parsed["runs"][0]["parsed_stdout"]["task_id"])

        code, task = run_cli("task", "show", "--task-id", "task-daemon-worker-e2e")
        self.assertEqual(code, 0, task)
        self.assertEqual("completed", task["task"]["status"])
        self.assertEqual("codex", task["task"]["claimed_by"])
        evidence = Path(task["task"]["evidence_path"])
        self.assertTrue(evidence.exists(), task)
        self.assertIn("Dry-run adapter acknowledged runtime", evidence.read_text(encoding="utf-8"))

        code, runs = run_cli("runtime", "adapter-runs", "--agent", "codex", "--status", "ok")
        self.assertEqual(code, 0, runs)
        self.assertEqual(["task-daemon-worker-e2e"], [run["task_id"] for run in runs["adapter_runs"]])
        self.assertTrue((self.root / "employees" / "codex" / "heartbeat.json").exists())
        self.assertTrue((self.root / "state" / "daemon" / "workers" / "codex.json").exists())

    def test_daemon_retries_due_failed_adapter_run(self) -> None:
        code, submitted = run_cli("task", "submit", "--from", "openclaw-main", "--to", "codex", "--task-id", "task-auto-retry", "--title", "auto retry")
        self.assertEqual(code, 0, submitted)
        code, blocked = run_cli("task", "block", "--agent", "codex", "--task-id", "task-auto-retry", "--blocker", "adapter failed")
        self.assertEqual(code, 0, blocked)
        trace_id = submitted["task"]["metadata"]["trace_id"]
        conn = companyctl.connect()
        try:
            conn.execute(
                """
                INSERT INTO adapter_runs(id, trace_id, agent_id, task_id, command, ok, processed, attempt, next_retry_at, result_json, created_at)
                VALUES ('adapter-run-auto-retry', ?, 'codex', 'task-auto-retry', 'company-codex-adapter', 0, 1, 1, '2000-01-01T00:00:00+00:00', '{}', ?)
                """,
                (trace_id, companyctl.now()),
            )
            conn.commit()
        finally:
            conn.close()

        state = company_daemon.tick({"version": 1, "run_repair": False, "run_scheduler": False, "heartbeat_agents": [], "run_retries": True})
        self.assertTrue(state["ok"], state)
        self.assertEqual(["retry.adapter-run"], [item["step"] for item in state["results"]])

        code, task = run_cli("task", "show", "--task-id", "task-auto-retry")
        self.assertEqual(code, 0, task)
        self.assertEqual("submitted", task["task"]["status"])
        self.assertEqual("adapter-run-auto-retry", task["metadata"]["recovery"]["retry_adapter_run"])
        code, run = run_cli("runtime", "adapter-run", "show", "--run-id", "adapter-run-auto-retry", "--summary")
        self.assertEqual(code, 0, run)
        self.assertEqual(trace_id, run["adapter_run"]["trace_id"])
        self.assertEqual("openclaw-main", run["adapter_run"]["acknowledged_by"])

    def test_daemon_enable_worker_temporarily_overrides_config(self) -> None:
        config_path = self.root / "config" / "daemon-worker-enable.json"
        config_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "run_repair": False,
                    "run_scheduler": False,
                    "heartbeat_agents": [],
                    "adapter_workers": [
                        {"agent": "codex", "enabled": False, "command": "company-adapter-worker", "args": ["--dry-run"]},
                        {"agent": "hermes", "enabled": False, "command": "company-hermes-adapter", "args": []},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        seen_configs = []

        def fake_tick(config: dict) -> dict:
            seen_configs.append(config)
            return {"ok": True, "at": "2026-06-03T04:40:00+07:00", "results": []}

        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(company_daemon, "tick", fake_tick):
            code = company_daemon.main(["--config", str(config_path), "--once", "--enable-worker", "codex"])
        self.assertEqual(0, code)
        workers = {worker["agent"]: worker["enabled"] for worker in seen_configs[0]["adapter_workers"]}
        self.assertEqual({"codex": True, "hermes": False}, workers)
        saved = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertFalse(saved["adapter_workers"][0]["enabled"])

        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), mock.patch.object(company_daemon, "tick", fake_tick):
            code = company_daemon.main(["--config", str(config_path), "--once", "--summary"])
        self.assertEqual(0, code)
        summary = json.loads(captured.getvalue())
        self.assertEqual(0, summary["counts"]["steps"])
        self.assertNotIn("results", summary)

    def test_daemon_enable_worker_creates_runtime_specific_temporary_worker(self) -> None:
        conn = companyctl.connect()
        try:
            conn.execute("UPDATE employees SET status = 'active' WHERE id = 'hermes'")
            conn.commit()
        finally:
            conn.close()
        config_path = self.root / "config" / "daemon-worker-temporary.json"
        config_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "run_repair": False,
                    "run_scheduler": False,
                    "heartbeat_agents": [],
                    "adapter_workers": [
                        {"agent": "codex", "enabled": False, "command": "company-adapter-worker", "args": ["--dry-run"]}
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        seen_configs = []

        def fake_tick(config: dict) -> dict:
            seen_configs.append(config)
            return {"ok": True, "at": "2026-06-03T04:41:00+07:00", "results": []}

        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(company_daemon, "tick", fake_tick):
            code = company_daemon.main(["--config", str(config_path), "--once", "--enable-worker", "hermes"])
        self.assertEqual(0, code)
        workers = {worker["agent"]: worker for worker in seen_configs[0]["adapter_workers"]}
        self.assertTrue(workers["hermes"]["enabled"])
        self.assertTrue(workers["hermes"]["temporary"])
        self.assertEqual("company-hermes-adapter", workers["hermes"]["command"])
        self.assertEqual([], workers["hermes"]["args"])
        self.assertEqual("hermes", workers["hermes"]["runtime"])
        saved = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(["codex"], [worker["agent"] for worker in saved["adapter_workers"]])

        with self.assertRaisesRegex(SystemExit, "unknown or inactive worker"):
            company_daemon.main(["--config", str(config_path), "--once", "--enable-worker", "missing-agent"])

    def test_daemon_enable_worker_falls_back_to_generic_worker_for_local_runtime(self) -> None:
        conn = companyctl.connect()
        try:
            conn.execute("UPDATE employees SET status = 'active' WHERE id = 'video-ops'")
            conn.commit()
        finally:
            conn.close()
        config_path = self.root / "config" / "daemon-worker-local.json"
        config_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "run_repair": False,
                    "run_scheduler": False,
                    "heartbeat_agents": [],
                    "adapter_workers": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        seen_configs = []

        def fake_tick(config: dict) -> dict:
            seen_configs.append(config)
            return {"ok": True, "at": "2026-06-03T04:41:30+07:00", "results": []}

        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(company_daemon, "tick", fake_tick):
            code = company_daemon.main(["--config", str(config_path), "--once", "--enable-worker", "video-ops"])
        self.assertEqual(0, code)
        worker = seen_configs[0]["adapter_workers"][0]
        self.assertEqual("video-ops", worker["agent"])
        self.assertEqual("local", worker["runtime"])
        self.assertEqual("company-adapter-worker", worker["command"])
        self.assertEqual(["--dry-run"], worker["args"])

    def test_daemon_enable_worker_uses_skill_package_worker_for_skill_runtime(self) -> None:
        code, runtime = run_cli("runtime", "register", "--runtime", "skill", "--command", "company-skill-package-worker", "--notes", "Skill Package runtime")
        self.assertEqual(0, code, runtime)
        code, employee = run_cli(
            "employee",
            "create",
            "--id",
            "packaged-skill",
            "--name",
            "Packaged Skill",
            "--role",
            "skill-worker",
            "--runtime",
            "skill",
            "--workspace",
            str(self.root / "workspace" / "packaged-skill"),
        )
        self.assertEqual(0, code, employee)
        self.mark_active("packaged-skill")
        config_path = self.root / "config" / "daemon-worker-skill.json"
        config_path.write_text(json.dumps({"version": 1, "run_repair": False, "run_scheduler": False, "heartbeat_agents": [], "adapter_workers": []}, ensure_ascii=False), encoding="utf-8")
        seen_configs = []

        def fake_tick(config: dict) -> dict:
            seen_configs.append(config)
            return {"ok": True, "at": "2026-06-03T04:41:30+07:00", "results": []}

        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(company_daemon, "tick", fake_tick):
            code = company_daemon.main(["--config", str(config_path), "--once", "--enable-worker", "packaged-skill"])
        self.assertEqual(0, code)
        worker = seen_configs[0]["adapter_workers"][0]
        self.assertEqual("packaged-skill", worker["agent"])
        self.assertEqual("skill", worker["runtime"])
        self.assertEqual("company-skill-package-worker", worker["command"])
        self.assertEqual([], worker["args"])

    def test_skill_package_worker_runs_manifest_and_promotes_evidence(self) -> None:
        code, runtime = run_cli("runtime", "register", "--runtime", "skill", "--command", "company-skill-package-worker", "--notes", "Skill Package runtime")
        self.assertEqual(0, code, runtime)
        code, employee = run_cli(
            "employee",
            "create",
            "--id",
            "image-copy-skill",
            "--name",
            "Image Copy Skill",
            "--role",
            "skill-worker",
            "--runtime",
            "skill",
            "--workspace",
            str(self.root / "workspace" / "image-copy-skill"),
        )
        self.assertEqual(0, code, employee)
        self.mark_active("image-copy-skill")
        package_dir = self.root / "skill-packages" / "image-copy"
        package_dir.mkdir(parents=True)
        manifest_path = package_dir / "skill.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "id": "image-copy",
                    "name": "Image copy package",
                    "version": "0.1.0",
                    "employee_id": "image-copy-skill",
                    "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}}},
                    "output_schema": {"type": "object", "properties": {"summary": {"type": "string"}}},
                    "runtime": {"type": "local-script", "command": "python3 -c \"import os; from pathlib import Path; root=Path(os.environ['TASK_WORKSPACE']); (root/'final').mkdir(exist_ok=True); (root/'final/result.md').write_text('skill output for '+os.environ['TASK_ID'], encoding='utf-8')\""},
                    "permissions": {"workspace": "task"},
                    "pricing": {"unit": "task", "amount": 10, "currency": "USD"},
                    "acceptance": {"final_artifact": "final/result.md"},
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        task_id = "task-skill-package-run"
        code, submitted = run_cli(
            "task",
            "submit",
            "--from",
            "video-ops",
            "--to",
            "image-copy-skill",
            "--task-id",
            task_id,
            "--title",
            "Run image copy skill",
            "--description",
            "Use the packaged skill and return final evidence.",
        )
        self.assertEqual(0, code, submitted)
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = skill_package_worker.main(["--agent", "image-copy-skill", "--package", str(manifest_path)])
        result = json.loads(captured.getvalue())
        self.assertEqual(0, code, result)
        self.assertTrue(result["ok"], result)
        self.assertEqual(task_id, result["task_id"])
        self.assertEqual("completed", result["status"])
        self.assertTrue(result["artifact"]["artifact_id"])
        self.assertTrue(result["evidence"]["evidence_id"])

        code, shown = run_cli("task", "show", "--task-id", task_id)
        self.assertEqual(0, code, shown)
        self.assertEqual("completed", shown["task"]["status"])
        self.assertTrue(shown["evidence"]["exists"])
        conn = companyctl.connect()
        try:
            artifact_count = conn.execute("SELECT COUNT(*) FROM artifacts WHERE task_id = ?", (task_id,)).fetchone()[0]
            evidence_count = conn.execute("SELECT COUNT(*) FROM evidence WHERE task_id = ?", (task_id,)).fetchone()[0]
            tool_call_count = conn.execute("SELECT COUNT(*) FROM agent_tool_calls WHERE task_id = ? AND employee_id = ?", (task_id, "image-copy-skill")).fetchone()[0]
            budget_event_count = conn.execute("SELECT COUNT(*) FROM budget_events WHERE task_id = ? AND employee_id = ?", (task_id, "image-copy-skill")).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(1, artifact_count)
        self.assertEqual(1, evidence_count)
        self.assertEqual(1, tool_call_count)
        self.assertEqual(1, budget_event_count)
        status, trace = api_gateway.route_get(f"/v1/traces/{submitted['task']['metadata']['trace_id']}/timeline", {})
        self.assertEqual(HTTPStatus.OK, status, trace)
        self.assertIn("tool_call", [item["kind"] for item in trace["timeline"]])
        self.assertIn("budget_event", [item["kind"] for item in trace["timeline"]])

    def test_skill_package_worker_can_continue_claimed_retry_task(self) -> None:
        code, runtime = run_cli("runtime", "register", "--runtime", "skill", "--command", "company-skill-package-worker", "--notes", "Skill Package runtime")
        self.assertEqual(0, code, runtime)
        code, employee = run_cli(
            "employee",
            "create",
            "--id",
            "retry-skill",
            "--name",
            "Retry Skill",
            "--role",
            "skill-worker",
            "--runtime",
            "skill",
            "--workspace",
            str(self.root / "workspace" / "retry-skill"),
        )
        self.assertEqual(0, code, employee)
        self.mark_active("retry-skill")
        package_dir = self.root / "skill-packages" / "retry-skill"
        package_dir.mkdir(parents=True)
        manifest_path = package_dir / "skill.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "id": "retry-skill",
                    "name": "Retry Skill",
                    "version": "0.1.0",
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                    "runtime": {"type": "local-script", "command": "python3 -c \"import os; from pathlib import Path; root=Path(os.environ['TASK_WORKSPACE']); (root/'final').mkdir(exist_ok=True); (root/'final/result.md').write_text('retry ok', encoding='utf-8')\""},
                    "permissions": {"workspace": "task"},
                    "pricing": {"unit": "task", "amount": 1, "currency": "USD"},
                    "acceptance": {"final_artifact": "final/result.md"},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        task_id = "task-skill-claimed-retry"
        code, submitted = run_cli("task", "submit", "--from", "video-ops", "--to", "retry-skill", "--task-id", task_id, "--title", "Retry skill task")
        self.assertEqual(0, code, submitted)
        code, claimed = run_cli("task", "claim", "--agent", "retry-skill", "--task-id", task_id)
        self.assertEqual(0, code, claimed)
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = skill_package_worker.main(["--agent", "retry-skill", "--package", str(manifest_path)])
        result = json.loads(captured.getvalue())
        self.assertEqual(0, code, result)
        self.assertEqual("completed", result["status"])
        self.assertEqual(task_id, result["task_id"])
        code, attempts = run_cli("task", "attempts", "--task-id", task_id)
        self.assertEqual(0, code, attempts)
        self.assertEqual(1, len(attempts["attempts"]))
        self.assertEqual("success", attempts["attempts"][0]["status"])

    def test_skill_package_worker_reuses_retry_attempt_without_leaving_starting_attempt(self) -> None:
        code, runtime = run_cli("runtime", "register", "--runtime", "skill", "--command", "company-skill-package-worker", "--notes", "Skill Package runtime")
        self.assertEqual(0, code, runtime)
        code, employee = run_cli(
            "employee",
            "create",
            "--id",
            "retry-attempt-skill",
            "--name",
            "Retry Attempt Skill",
            "--role",
            "skill-worker",
            "--runtime",
            "skill",
            "--workspace",
            str(self.root / "workspace" / "retry-attempt-skill"),
        )
        self.assertEqual(0, code, employee)
        self.mark_active("retry-attempt-skill")
        package_dir = self.root / "skill-packages" / "retry-attempt-skill"
        package_dir.mkdir(parents=True)
        manifest_path = package_dir / "skill.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "id": "retry-attempt-skill",
                    "name": "Retry Attempt Skill",
                    "version": "0.1.0",
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                    "runtime": {"type": "local-script", "command": "python3 -c \"import os; from pathlib import Path; root=Path(os.environ['TASK_WORKSPACE']); (root/'final').mkdir(exist_ok=True); (root/'final/result.md').write_text('retry attempt ok', encoding='utf-8')\""},
                    "permissions": {"workspace": "task"},
                    "pricing": {"unit": "task", "amount": 1, "currency": "USD"},
                    "acceptance": {"final_artifact": "final/result.md"},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        task_id = "task-skill-retry-attempt"
        code, submitted = run_cli("task", "submit", "--from", "video-ops", "--to", "retry-attempt-skill", "--task-id", task_id, "--title", "Retry attempt skill task")
        self.assertEqual(0, code, submitted)
        code, first_run = run_cli("task", "run", "--task-id", task_id, "--agent", "retry-attempt-skill", "--by", "video-ops", "--adapter-type", "skill")
        self.assertEqual(0, code, first_run)
        first_attempt_id = first_run["attempt"]["attempt_id"]
        code, finished = run_cli("task", "attempt", "finish", "--attempt-id", first_attempt_id, "--status", "failed", "--error", "synthetic failure")
        self.assertEqual(0, code, finished)
        code, blocked = run_cli("task", "block", "--agent", "retry-attempt-skill", "--task-id", task_id, "--blocker", "synthetic failure")
        self.assertEqual(0, code, blocked)
        code, retry = run_cli("task", "retry", "--task-id", task_id, "--by", "video-ops", "--reason", "try again")
        self.assertEqual(0, code, retry)
        retry_attempt_id = retry["attempt"]["attempt_id"]

        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = skill_package_worker.main(["--agent", "retry-attempt-skill", "--package", str(manifest_path)])
        result = json.loads(captured.getvalue())
        self.assertEqual(0, code, result)
        self.assertEqual("completed", result["status"])
        self.assertEqual(retry_attempt_id, result["attempt"]["attempt_id"])

        code, attempts = run_cli("task", "attempts", "--task-id", task_id)
        self.assertEqual(0, code, attempts)
        self.assertEqual(["failed", "success"], [attempt["status"] for attempt in attempts["attempts"]])
        self.assertFalse(any(attempt["status"] in {"starting", "running", "correcting", "cancelling"} for attempt in attempts["attempts"]))

    def test_daemon_summary_omits_raw_command_output(self) -> None:
        state = {
            "ok": False,
            "at": "2026-06-03T05:30:00+07:00",
            "state_file": str(self.root / "state" / "daemon" / "last-run.json"),
            "results": [
                {"step": "repair.reset-stale-claims", "result": {"returncode": 0, "stdout": "large repair output"}},
                {"step": "scheduler.run", "result": {"returncode": 0, "stdout": "large scheduler output"}},
                {"step": "supervisor.delivery-loop", "result": {"returncode": 0, "stdout": "large supervisor output"}},
                {"step": "heartbeat.main", "result": {"returncode": 0, "stdout": "large heartbeat output"}},
                {"step": "adapter.codex", "result": {"returncode": 1, "stdout": "large adapter output"}},
            ],
        }
        summary = company_daemon.summarize_state(state)
        self.assertFalse(summary["ok"])
        self.assertEqual({"steps": 5, "heartbeats": 1, "adapters": 1, "repair": 1, "scheduler": 1, "supervisor": 1, "openclaw_sync": 0, "failed": 1}, summary["counts"])
        self.assertEqual(["main"], summary["heartbeat_agents"])
        self.assertEqual(["adapter.codex"], summary["failed_steps"])
        self.assertNotIn("results", summary)

    def test_launchd_plist_runs_daemon_under_doctor_threshold(self) -> None:
        plist_path = Path(__file__).resolve().parents[1] / "config" / "launchd" / "ai.openclaw.company-kernel.daemon.plist"
        payload = plistlib.loads(plist_path.read_bytes())
        self.assertEqual("ai.openclaw.company-kernel.daemon", payload["Label"])
        self.assertEqual(180, payload["StartInterval"])
        self.assertLessEqual(payload["StartInterval"], 3 * 60)
        self.assertLess(payload["StartInterval"], 10 * 60)
        self.assertEqual(
            ["__COMPANY_KERNEL_ROOT__/bin/company-daemon", "--once", "--summary"],
            payload["ProgramArguments"],
        )
        self.assertTrue(payload["RunAtLoad"])

    def test_launchd_service_plists_pin_api_and_dashboard_ports(self) -> None:
        launchd_dir = Path(__file__).resolve().parents[1] / "config" / "launchd"
        api = plistlib.loads((launchd_dir / "ai.openclaw.company-kernel.api.plist").read_bytes())
        dashboard = plistlib.loads((launchd_dir / "ai.openclaw.company-kernel.dashboard.plist").read_bytes())
        self.assertEqual("ai.openclaw.company-kernel.api", api["Label"])
        self.assertEqual("ai.openclaw.company-kernel.dashboard", dashboard["Label"])
        self.assertEqual(
            ["__COMPANY_KERNEL_ROOT__/bin/company-api-gateway", "--host", "127.0.0.1", "--port", "8765", "--quiet"],
            api["ProgramArguments"],
        )
        self.assertEqual(["__COMPANY_KERNEL_ROOT__/bin/company-dashboard-server"], dashboard["ProgramArguments"])
        self.assertTrue(api["KeepAlive"])
        self.assertTrue(dashboard["KeepAlive"])
        self.assertTrue(api["RunAtLoad"])
        self.assertTrue(dashboard["RunAtLoad"])

    def test_openclaw_bootstrap_scanner_maps_hermes_to_default_runtime(self) -> None:
        code, runtime = run_cli("runtime", "register", "--runtime", "human", "--command", "", "--status", "registered")
        self.assertEqual(0, code, runtime)
        code, owner = run_cli("employee", "create", "--id", "owner-shift", "--name", "Shift", "--role", "owner", "--runtime", "human", "--workspace", str(self.root / "employees" / "owner-shift"))
        self.assertEqual(0, code, owner)
        script = Path(__file__).resolve().parents[1] / "skills" / "openclaw-local-agent-bootstrap" / "scripts" / "scan_install.py"
        cp = subprocess.run(
            [sys.executable, str(script), "--kernel-root", str(self.root)],
            cwd=str(self.root),
            text=True,
            capture_output=True,
            check=True,
        )
        report = json.loads(cp.stdout)
        hermes = next(item for item in report["employees"] if item["agent_id"] == "hermes")
        owner = next(item for item in report["employees"] if item["agent_id"] == "owner-shift")
        self.assertTrue(report["coordination"]["closed_loop_required"])
        self.assertTrue(report["employee_directory"]["rename_supported"])
        self.assertIn("rename_command", report["employee_directory"]["all"][0])
        self.assertEqual(["id", "alias", "name", "display_name"], hermes["identity"]["lookup_priority"])
        self.assertTrue(report["handshake"]["required"])
        self.assertEqual("hermes", report["handshake"]["installer_agent"])
        self.assertEqual("hermes", report["handshake"]["validation_admin"])
        self.assertEqual("hermes", report["handshake"]["approval_validation_admin"])
        codex_plan = next(item for item in report["handshake"]["plan"] if item["to"] == "codex")
        self.assertEqual(3, codex_plan["rounds"])
        self.assertEqual(3, codex_plan["required_success"])
        self.assertIn("workspace", codex_plan["messages"][1])
        self.assertNotIn("hermes", [item["to"] for item in report["handshake"]["plan"]])
        self.assertNotIn("owner-shift", [item["to"] for item in report["handshake"]["plan"]])
        self.assertEqual("default", hermes["runtime"]["runtime_agent_id"])
        self.assertIn("available_commands", hermes["runtime"])
        self.assertEqual("agent:default:<source>", hermes["communication"]["session_key"])
        self.assertTrue(hermes["communication"]["ack_required"])
        self.assertTrue(hermes["coordination"]["human_notification_required"])
        self.assertEqual(0, hermes["communication"]["pending_inbox_messages"])
        self.assertEqual("human-owner", owner["status"])
        self.assertEqual(["owner-shift"], [item["agent_id"] for item in report["human_owners"]])

    def test_openclaw_bootstrap_scanner_can_execute_handshake_rounds(self) -> None:
        script = Path(__file__).resolve().parents[1] / "skills" / "openclaw-local-agent-bootstrap" / "scripts" / "scan_install.py"
        scanner = runpy.run_path(str(script), run_name="scanner_for_test")
        plan = scanner["build_handshake_plan"](
            [
                {"agent_id": "codex", "status": "active"},
                {"agent_id": "main", "status": "active"},
                {"agent_id": "owner-shift", "status": "human-owner"},
            ],
            "codex",
            2,
        )
        self.assertEqual(["main"], [item["to"] for item in plan])
        self.assertEqual(2, plan[0]["rounds"])
        calls = []

        def fake_run(cmd, cwd=None, text=None, capture_output=None, timeout=None):
            calls.append(cmd)

            class Result:
                returncode = 0
                stdout = json.dumps({"ok": True, "reply": "HANDSHAKE_OK"})
                stderr = ""

            return Result()

        with mock.patch.object(scanner["subprocess"], "run", side_effect=fake_run):
            results = scanner["run_handshake"](self.root, plan, 5)
        self.assertTrue(results[0]["ok"])
        self.assertEqual(2, results[0]["rounds_completed"])
        self.assertTrue(any("message" in call and "direct" in call for call in calls))

    def test_openclaw_bootstrap_scanner_discovers_and_applies_candidates(self) -> None:
        openclaw_root = self.root / "openclaw"
        (openclaw_root / "scripts").mkdir(parents=True)
        (openclaw_root / "scripts" / "oc").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        acme_workspace = openclaw_root / "workspace-acmeops"
        acme_workspace.mkdir()
        (acme_workspace / "AGENTS.md").write_text("AcmeOps business agent rules\n", encoding="utf-8")
        script = Path(__file__).resolve().parents[1] / "skills" / "openclaw-local-agent-bootstrap" / "scripts" / "scan_install.py"

        cp = subprocess.run(
            [sys.executable, str(script), "--openclaw-root", str(openclaw_root), "--kernel-root", str(self.root)],
            cwd=str(self.root),
            text=True,
            capture_output=True,
            check=True,
        )
        report = json.loads(cp.stdout)
        discovered = {item["agent_id"]: item for item in report["discovered_candidates"]}
        self.assertIn("acmeops", discovered)
        self.assertEqual("openclaw", discovered["acmeops"]["runtime"])
        self.assertIn("status candidate", discovered["acmeops"]["recommended_command"])

        applied = subprocess.run(
            [sys.executable, str(script), "--openclaw-root", str(openclaw_root), "--kernel-root", str(self.root), "--apply"],
            cwd=str(self.root),
            text=True,
            capture_output=True,
            check=True,
        )
        applied_report = json.loads(applied.stdout)
        acmeops = next(item for item in applied_report["employees"] if item["agent_id"] == "acmeops")
        self.assertEqual("candidate", acmeops["status"])
        self.assertEqual("openclaw", acmeops["runtime"]["type"])
        self.assertIn("available_commands", acmeops["runtime"])
        self.assertEqual("agent:acmeops:<source>", acmeops["communication"]["session_key"])


if __name__ == "__main__":
    unittest.main()
