from __future__ import annotations

import contextlib
import io
import json
import plistlib
import sqlite3
import subprocess
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest import mock

from company_kernel import antigravity_adapter
from company_kernel import api_gateway
from company_kernel import api_grpc
from company_kernel import api_rpc
from company_kernel import company_daemon
from company_kernel import company_dashboard
from company_kernel import company_service_smoke
from company_kernel import company_trace
from company_kernel import companyctl
from company_kernel import codex_adapter
from company_kernel import openclaw_adapter
from company_kernel import policy_guard
from company_kernel import sandboxing
from company_kernel import schema_migrations


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
        (root / "bin").mkdir()
        for executable in ["companyctl", "company-adapter-worker", "company-codex-adapter", "company-openclaw-adapter", "company-trace", "company-api-rpc", "company-api-grpc", "company-service-smoke"]:
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
            mock.patch.object(openclaw_adapter, "ROOT", root),
            mock.patch.object(openclaw_adapter, "DB_PATH", root / "company.sqlite"),
            mock.patch.object(openclaw_adapter, "OPENCLAW_ROOT", root / "openclaw"),
            mock.patch.object(antigravity_adapter, "ROOT", root),
            mock.patch.object(antigravity_adapter, "DB_PATH", root / "company.sqlite"),
            mock.patch.object(antigravity_adapter, "APP_PATH", root / "Applications" / "Antigravity.app"),
            mock.patch.object(policy_guard, "ROOT", root),
            mock.patch.object(policy_guard, "DB_PATH", root / "company.sqlite"),
            mock.patch.object(policy_guard, "SCHEMA", root / "company_kernel" / "schema.sql"),
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

        def tracked_connect() -> sqlite3.Connection:
            conn = sqlite3.connect(companyctl.DB_PATH)
            conn.row_factory = sqlite3.Row
            conn.executescript(companyctl.SCHEMA.read_text(encoding="utf-8"))
            conn.commit()
            companyctl._TEST_OPEN_CONNECTIONS.append(conn)
            return conn

        self.connect_patcher = mock.patch.object(companyctl, "connect", tracked_connect)
        self.connect_patcher.start()
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

    def tearDown(self) -> None:
        self.connect_patcher.stop()
        for conn in getattr(companyctl, "_TEST_OPEN_CONNECTIONS", []):
            conn.close()
        companyctl._TEST_OPEN_CONNECTIONS.clear()
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
        code, created = run_cli("employee", "create", "--id", "main", "--name", "main", "--role", "operator", "--runtime", "openclaw", "--workspace", str(self.root / "workspace" / "main"))
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

    def test_dashboard_renders_conversations_and_pending_events(self) -> None:
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
            code = company_dashboard.main(["--output", str(output)])
        self.assertEqual(0, code)
        html = output.read_text(encoding="utf-8")
        self.assertIn("Conversations", html)
        self.assertIn("Pending Events", html)
        self.assertIn("conv-dashboard-001", html)
        self.assertIn("conversation.message", html)
        self.assertIn("Runtime Health", html)
        self.assertIn("daemon", html)
        self.assertIn("launchd", html)
        self.assertIn("missing_daemon_state", html)
        self.assertIn("local-automation", html)
        self.assertIn("ops-support", html)
        self.assertIn("Needs Attention", html)

    def test_dashboard_distinguishes_active_online_from_candidate_heartbeat(self) -> None:
        code, hermes = run_cli("employee", "create", "--id", "hermes", "--name", "Hermes", "--role", "supervisor", "--runtime", "hermes", "--workspace", str(self.root / "hermes"))
        self.assertEqual(code, 0, hermes)
        code, cursor = run_cli("employee", "onboard", "--id", "cursor", "--name", "Cursor", "--role", "developer", "--runtime", "local", "--workspace", str(self.root / "employees" / "cursor"))
        self.assertEqual(code, 0, cursor)
        conn = companyctl.connect()
        try:
            conn.execute("UPDATE employees SET status = 'candidate' WHERE id = 'cursor'")
            conn.commit()
            companyctl.heartbeat_internal(conn, "hermes", {"source": "test"})
            companyctl.heartbeat_internal(conn, "cursor", {"source": "old-candidate"})
        finally:
            conn.close()

        output = self.root / "state" / "dashboard-employees.html"
        with contextlib.redirect_stdout(io.StringIO()):
            code = company_dashboard.main(["--output", str(output)])
        self.assertEqual(0, code)
        html = output.read_text(encoding="utf-8")
        self.assertIn("<td>hermes</td><td>active</td><td>online</td><td>yes</td>", html)
        self.assertIn("<td>cursor</td><td>candidate</td><td>candidate</td><td>no</td>", html)
        self.assertIn("active_employees", html)
        self.assertIn("candidate_employees", html)
        self.assertIn("employee-manager", html)
        self.assertIn("/v1/employees/onboard", html)
        self.assertIn("offboardEmployee", html)
        self.assertIn("checkCompanyApi", html)
        self.assertIn("/v1/health", html)
        self.assertIn("API offline", html)
        self.assertIn("editEmployee", html)
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
<script>
  window.kernelSummary = {"counts":{"employees":1},"employees":[{"id":"old"}]};
  window.dbPath = "/Users/owner/Documents/anti/company.sqlite";
  function confirmAgentOnboarding() {
    summaryData.employees.push(generatedRecruitData);
    summaryData.counts.employees = summaryData.employees.length;
  }
  function executeEmployeeOffboard() {
    if (isSimulationMode) {
      if (mode === 'hard') {}
    }
  }
  document.getElementById('db-path-label').innerText = isSimulationMode ? 'simulation://gateway.company.internal' : 'https://gateway.company.internal';
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
        self.assertIn(str(self.root / "company.sqlite"), html)
        self.assertNotIn("/Users/owner/Documents/anti/company.sqlite", html)
        self.assertIn('"employees": 7', html)
        self.assertIn('"id": "hermes"', html)
        self.assertIn("window.companyApiBase", html)
        self.assertIn("companyApiGet", html)
        self.assertIn("checkCompanyApi", html)
        self.assertIn("/v1/health", html)
        self.assertIn("API OFFLINE", html)
        self.assertIn("/v1/attendance/latest", html)
        self.assertIn("realOnboardGeneratedEmployee", html)
        self.assertIn("realOffboardEmployee", html)
        self.assertIn("openEditEmployeeProfile", html)
        self.assertIn("realUpdateEmployeeProfile", html)
        self.assertIn("'PATCH'", html)
        self.assertIn("'DELETE'", html)

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
            code = company_dashboard.main(["--output", str(output)])
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
            code = company_dashboard.main(["--output", str(output)])
        self.assertEqual(0, code)
        html = output.read_text(encoding="utf-8")
        self.assertIn("task-approval-metadata", html)
        self.assertIn("<td>1</td><td>approval task</td>", html)

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
            code = company_dashboard.main(["--output", str(output)])
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
        self.assertEqual(7, healthy_summary["counts"]["heartbeats"])
        self.assertEqual(0, healthy_summary["counts"]["capability_issues"])
        self.assertEqual(0, healthy_summary["counts"]["task_evidence_issues"])
        self.assertEqual(0, healthy_summary["capabilities"]["issues"])
        self.assertEqual(0, healthy_summary["evidence"]["issues"])
        self.assertTrue(healthy_summary["daemon"]["ok"])
        self.assertTrue(healthy_summary["launchd"]["template_exists"])
        self.assertEqual("ai.openclaw.company-kernel.daemon", healthy_summary["launchd"]["label"])
        self.assertEqual(300, healthy_summary["launchd"]["recommended_interval_seconds"])
        self.assertEqual("bash bin/company-daemon-install-launchd", healthy_summary["launchd"]["install_command"])
        self.assertEqual("bin/companyctl doctor --summary", healthy_summary["launchd"]["verify_command"])
        self.assertFalse(healthy_summary["launchd"]["matches_template"])
        self.assertTrue(healthy_summary["openclaw_guard"]["ok"])
        self.assertEqual([], healthy_summary["openclaw_guard"]["issues"])

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
        installed.write_text(companyctl.LAUNCHD_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
        code, strict_installed = run_cli("doctor", "--summary", "--strict-launchd")
        self.assertEqual(code, 0, strict_installed)
        self.assertTrue(strict_installed["launchd"]["installed"])
        self.assertTrue(strict_installed["launchd"]["matches_template"])

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
        self.assertEqual(1, len(detail["events"]))
        self.assertEqual(1, len(detail["approvals"]))
        self.assertEqual("approval-publish-task-video-001", detail["approvals"][0]["id"])

        code, approvals = run_cli("approval", "list", "--status", "pending")
        self.assertEqual(code, 0, approvals)
        self.assertEqual("approval-publish-task-video-001", approvals["approvals"][0]["id"])

        code, pending = run_cli("scheduler", "events", "--pending")
        self.assertEqual(code, 0, pending)
        self.assertEqual(1, len(pending["events"]))

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
            code = company_dashboard.main(["--output", str(output)])
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

    def test_api_gateway_exposes_health_tasks_messages_and_heartbeats(self) -> None:
        status, descriptor = api_gateway.route_get("/v1", {})
        self.assertEqual(200, status, descriptor)
        self.assertIn("conversations", descriptor["capabilities"])
        self.assertIn("approvals", descriptor["capabilities"])
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

        status, sent = api_gateway.route_post("/v1/messages", {"from": "hermes", "to": "codex", "body": "REST ping", "message_id": "msg-api-gateway"})
        self.assertEqual(201, status, sent)
        status, messages = api_gateway.route_get("/v1/messages", {"agent": ["codex"]})
        self.assertEqual(200, status, messages)
        self.assertIn("msg-api-gateway", [message["id"] for message in messages["messages"]])

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

        code, submitted = run_cli("task", "submit", "--from", "openclaw-main", "--to", "codex", "--task-id", "task-api-adapter-retry", "--title", "adapter retry")
        self.assertEqual(code, 0, submitted)
        code, blocked = run_cli("task", "block", "--agent", "codex", "--task-id", "task-api-adapter-retry", "--blocker", "adapter failed")
        self.assertEqual(code, 0, blocked)
        conn = companyctl.connect()
        try:
            conn.execute(
                """
                INSERT INTO adapter_runs(id, trace_id, agent_id, task_id, command, ok, processed, attempt, next_retry_at, result_json, created_at)
                VALUES ('adapter-run-api-retry', ?, 'codex', 'task-api-adapter-retry', 'company-codex-adapter', 0, 1, 1, '2000-01-01T00:00:00+00:00', '{}', ?)
                """,
                (submitted["task"]["metadata"]["trace_id"], companyctl.now()),
            )
            conn.commit()
        finally:
            conn.close()

        status, run = api_gateway.route_get("/v1/adapter-runs/adapter-run-api-retry", {"summary": ["true"]})
        self.assertEqual(200, status, run)
        self.assertEqual("adapter-run-api-retry", run["adapter_run"]["id"])
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
        self.assertEqual(200, status, project_status)
        self.assertEqual("completed", project_status["status"])
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
        status, employees = api_gateway.route_get("/v1/employees", {})
        self.assertEqual(200, status, employees)
        self.assertIn("cursor-dev", [item["id"] for item in employees["employees"]])
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
            {"name": "Cursor Reviewer", "role": "reviewer", "status": "candidate"},
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

        status, patched = api_gateway.route_patch(
            "/v1/employees/cursor-dev",
            {"name": "Cursor API Employee", "role": "developer", "status": "active"},
        )
        self.assertEqual(200, status, patched)
        self.assertEqual("Cursor API Employee", patched["employee"]["name"])
        self.assertEqual("active", patched["employee"]["status"])

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
                "create_test_task": "true",
            },
        )
        self.assertEqual(201, status, onboarded)
        self.assertEqual("api-reviewer", onboarded["employee"]["id"])
        self.assertTrue((managed_workspace / "SOUL.md").exists())
        self.assertIn(str((managed_workspace / "SOUL.md").resolve()), onboarded["scaffolded_files"])
        self.assertEqual("task-onboard-api-reviewer", onboarded["test_task"]["id"])

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
                target = cmd[cmd.index("--to") + 1]
                title = cmd[cmd.index("--title") + 1]
                with companyctl.connect() as conn:
                    companyctl.submit_task_internal(
                        conn,
                        source="openclaw-main",
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
        self.assertTrue(verified["results"][0]["evidence_exists"])
        self.assertEqual("completed", verified["results"][0]["task_status"])

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
        self.assertEqual("task-openclaw-dry-run", payload["company_kernel_task_id"])
        self.assertEqual("openclaw-main", payload["company_kernel_source_agent"])
        self.assertEqual("检查车辆任务", payload["summary"])

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
            calls.append([source, target, priority, payload["company_kernel_task_id"]])
            out = json.dumps({"ok": True, "file": str(self.root / "openclaw" / "ops" / "agent_bus" / f"{payload['company_kernel_task_id']}.json")}, ensure_ascii=False)
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

    def test_employee_onboard_writes_config_and_creates_test_task(self) -> None:
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
            "--create-test-task",
        )
        self.assertEqual(code, 0, onboard)
        self.assertEqual("reviewer", onboard["employee"]["id"])
        self.assertEqual(["review", "qa"], onboard["capabilities"]["skills"])
        self.assertEqual("task-onboard-reviewer", onboard["test_task"]["id"])

        communication = json.loads((self.root / "config" / "company_communications.json").read_text(encoding="utf-8"))
        self.assertEqual("reviewer", communication["aliases"]["rev"])
        self.assertEqual(["video-ops", "codex"], communication["employees"]["reviewer"]["can_talk_to"])
        self.assertIn("reviewer", communication["channels"]["engineering"]["participants"])

        code, sent = run_cli("message", "send", "--from", "ops", "--to", "rev", "--body", "欢迎入职")
        self.assertEqual(code, 0, sent)
        self.assertEqual("reviewer", sent["message"]["target_agent"])

        code, claimed = run_cli("task", "claim", "--agent", "rev", "--task-id", "task-onboard-reviewer")
        self.assertEqual(code, 0, claimed)

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

    def test_daemon_records_adapter_runs_for_dashboard(self) -> None:
        code, submitted = run_cli("task", "submit", "--from", "openclaw-main", "--to", "codex", "--task-id", "task-adapter-run-dashboard", "--title", "adapter run dashboard")
        self.assertEqual(code, 0, submitted)
        trace_id = submitted["task"]["metadata"]["trace_id"]
        state = {
            "ok": True,
            "agent": "codex",
            "command": "company-codex-adapter",
            "processed": 1,
            "runs": [{"parsed_stdout": {"task_id": "task-adapter-run-dashboard"}}],
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
        code, shown_summary = run_cli("runtime", "adapter-run", "show", "--run-id", run_id, "--summary")
        self.assertEqual(code, 0, shown_summary)
        self.assertNotIn("result_json", shown_summary["adapter_run"])
        self.assertNotIn("result", shown_summary)
        self.assertEqual("task-adapter-run-dashboard", shown_summary["result_summary"]["runs"][0]["task_id"])
        code, failed = run_cli("runtime", "adapter-runs", "--status", "failed", "--unacknowledged-only")
        self.assertEqual(code, 0, failed)
        self.assertEqual([], failed["adapter_runs"])

        output = self.root / "state" / "dashboard-adapter-runs.html"
        with contextlib.redirect_stdout(io.StringIO()):
            code = company_dashboard.main(["--output", str(output)])
        self.assertEqual(0, code)
        html = output.read_text(encoding="utf-8")
        self.assertIn("Adapter Runs", html)
        self.assertIn("task-adapter-run-dashboard", html)
        self.assertIn("company-codex-adapter", html)
        self.assertIn(str(self.root / "state" / "daemon" / "workers" / "codex.json"), html)

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

    def test_daemon_summary_omits_raw_command_output(self) -> None:
        state = {
            "ok": False,
            "at": "2026-06-03T05:30:00+07:00",
            "state_file": str(self.root / "state" / "daemon" / "last-run.json"),
            "results": [
                {"step": "repair.reset-stale-claims", "result": {"returncode": 0, "stdout": "large repair output"}},
                {"step": "scheduler.run", "result": {"returncode": 0, "stdout": "large scheduler output"}},
                {"step": "heartbeat.main", "result": {"returncode": 0, "stdout": "large heartbeat output"}},
                {"step": "adapter.codex", "result": {"returncode": 1, "stdout": "large adapter output"}},
            ],
        }
        summary = company_daemon.summarize_state(state)
        self.assertFalse(summary["ok"])
        self.assertEqual({"steps": 4, "heartbeats": 1, "adapters": 1, "repair": 1, "scheduler": 1, "failed": 1}, summary["counts"])
        self.assertEqual(["main"], summary["heartbeat_agents"])
        self.assertEqual(["adapter.codex"], summary["failed_steps"])
        self.assertNotIn("results", summary)

    def test_launchd_plist_runs_daemon_under_doctor_threshold(self) -> None:
        plist_path = Path(__file__).resolve().parents[1] / "config" / "launchd" / "ai.openclaw.company-kernel.daemon.plist"
        payload = plistlib.loads(plist_path.read_bytes())
        self.assertEqual("ai.openclaw.company-kernel.daemon", payload["Label"])
        self.assertEqual(300, payload["StartInterval"])
        self.assertLess(payload["StartInterval"], 10 * 60)
        self.assertEqual(
            ["/Users/owner/openclaw/company-kernel/bin/company-daemon", "--once", "--summary"],
            payload["ProgramArguments"],
        )
        self.assertTrue(payload["RunAtLoad"])


if __name__ == "__main__":
    unittest.main()
