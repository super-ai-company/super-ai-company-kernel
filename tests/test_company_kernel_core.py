from __future__ import annotations

import contextlib
import io
import json
import plistlib
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from company_kernel import company_daemon
from company_kernel import company_dashboard
from company_kernel import companyctl
from company_kernel import openclaw_adapter
from company_kernel import policy_guard
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
        (root / "company_kernel").mkdir()
        source_pkg = Path(__file__).resolve().parents[1] / "company_kernel"
        for source_file in source_pkg.glob("*.py"):
            (root / "company_kernel" / source_file.name).write_text(source_file.read_text(encoding="utf-8"), encoding="utf-8")
        (root / "company_kernel" / "schema.sql").write_text(companyctl.SCHEMA.read_text(encoding="utf-8"), encoding="utf-8")
        (root / "bin").mkdir()
        for executable in ["companyctl", "company-adapter-worker", "company-openclaw-adapter"]:
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
            mock.patch.object(openclaw_adapter, "ROOT", root),
            mock.patch.object(openclaw_adapter, "DB_PATH", root / "company.sqlite"),
            mock.patch.object(openclaw_adapter, "OPENCLAW_ROOT", root / "openclaw"),
            mock.patch.object(policy_guard, "ROOT", root),
            mock.patch.object(policy_guard, "DB_PATH", root / "company.sqlite"),
            mock.patch.object(policy_guard, "SCHEMA", root / "company_kernel" / "schema.sql"),
            mock.patch.object(policy_guard, "APPROVAL_STATE_DIR", root / "state" / "approvals"),
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
            mock.patch.dict("os.environ", {"HOME": str(root / "home"), "OPENCLAW_COMPANY_KERNEL_ROOT": str(root)}),
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
            runtime = "hermes" if employee_id == "hermes" else "openclaw" if employee_id == "nestcar" else "local"
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
            backfilled = conn.execute("SELECT task_id FROM adapter_runs WHERE id = 'adapter-run-backfill-task'").fetchone()
            self.assertEqual("task-backfilled-adapter-run", backfilled["task_id"])
            migrations = conn.execute("SELECT id FROM schema_migrations ORDER BY id").fetchall()
            self.assertEqual(
                [
                    "20260603_adapter_runs_acknowledged_at",
                    "20260603_adapter_runs_acknowledged_by",
                    "20260603_adapter_runs_acknowledgement_reason",
                    "20260603_adapter_runs_backfill_task_id",
                    "20260603_adapter_runs_task_id",
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
        code, blocked2 = run_cli("task", "block", "--agent", "maker", "--task-id", "task-reassign-001", "--blocker", "needs engineering")
        self.assertEqual(code, 0, blocked2)

        code, reassigned = run_cli("task", "reassign", "--task-id", "task-reassign-001", "--by", "ops", "--to", "codex", "--reason", "needs code")
        self.assertEqual(code, 0, reassigned)
        self.assertEqual("codex", reassigned["task"]["target_agent"])
        self.assertEqual("submitted", reassigned["task"]["status"])
        self.assertEqual("", reassigned["task"]["blocker"])
        self.assertEqual("", reassigned["task"]["claimed_by"])
        self.assertTrue(Path(reassigned["file"]).exists())

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
        self.assertEqual("reviewer", claimed["task"]["claimed_by"])

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
            self.assertEqual("company-codex-adapter", rows[0]["command"])
            self.assertEqual(1, rows[0]["ok"])
            self.assertEqual(1, rows[0]["processed"])
        finally:
            conn.close()

        code, listed = run_cli("runtime", "adapter-runs", "--agent", "codex", "--status", "ok")
        self.assertEqual(code, 0, listed)
        self.assertEqual(["task-adapter-run-dashboard"], [run["task_id"] for run in listed["adapter_runs"]])
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
            ["/Users/shift/openclaw/company-kernel/bin/company-daemon", "--once", "--summary"],
            payload["ProgramArguments"],
        )
        self.assertTrue(payload["RunAtLoad"])


if __name__ == "__main__":
    unittest.main()
