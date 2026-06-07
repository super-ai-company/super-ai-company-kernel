from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from company_kernel import communication_acceptance


class CommunicationAcceptanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "company_kernel").mkdir()
        source_schema = Path(__file__).resolve().parents[1] / "company_kernel" / "schema.sql"
        self.schema = self.root / "company_kernel" / "schema.sql"
        self.schema.write_text(source_schema.read_text(encoding="utf-8"), encoding="utf-8")
        self.db = self.root / "company.sqlite"
        self.workspace = self.root / "workspace" / "codex"
        self.workspace.mkdir(parents=True)
        self.output_dir = self.root / "reports" / "communication-acceptance"
        self.patchers = [
            mock.patch.object(communication_acceptance, "ROOT", self.root),
            mock.patch.object(communication_acceptance, "DB_PATH", self.db),
            mock.patch.object(communication_acceptance, "SCHEMA", self.schema),
            mock.patch.object(communication_acceptance, "DEFAULT_OUTPUT_DIR", self.output_dir),
        ]
        for patcher in self.patchers:
            patcher.start()
        self._init_db()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tmp.cleanup()

    def _init_db(self) -> None:
        conn = sqlite3.connect(self.db)
        conn.executescript(self.schema.read_text(encoding="utf-8"))
        now = "2026-06-07T00:00:00+07:00"
        employees = [
            ("main", "main", "operator", "openclaw", str(self.root / "workspace-xmanx"), "active"),
            ("hermes", "Hermes", "supervisor", "hermes", str(self.root / "employees" / "hermes"), "active"),
            ("codex", "Codex", "developer", "codex", str(self.workspace), "active"),
            ("antigravity", "Antigravity", "frontend-developer", "antigravity", str(self.root), "candidate"),
        ]
        conn.executemany(
            """
            INSERT INTO employees(id, name, role, runtime, workspace, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(eid, name, role, runtime, workspace, status, now, now) for eid, name, role, runtime, workspace, status in employees],
        )
        conn.commit()
        conn.close()

    def test_simulated_acceptance_requires_receipts_progress_and_reports(self) -> None:
        result = communication_acceptance.run_acceptance(
            simulate=True,
            direct_rounds=2,
            continuity_runs=3,
            output_dir=self.output_dir,
            now_ts="2026-06-07T00:10:00+07:00",
        )

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["mechanism_ok"], result)
        self.assertEqual("simulated", result["mode"])
        self.assertEqual(8, result["metrics"]["direct_total"])
        self.assertEqual(8, result["metrics"]["direct_passed"])
        self.assertEqual(3, result["metrics"]["continuity_total"])
        self.assertEqual(3, result["metrics"]["continuity_passed"])
        self.assertEqual("completed", result["routes"]["B_hermes_codex_pm"]["supervisor"]["status"])
        self.assertNotEqual(str(self.db.resolve()), result["routes"]["B_hermes_codex_pm"]["supervisor"]["db_path"])
        self.assertTrue(Path(result["acceptance_db"]).exists())
        self.assertEqual(["acknowledged", "in_progress", "completed"], result["routes"]["D_progress_visibility"]["states_seen"])
        self.assertEqual("stalled", result["routes"]["E_stale_blocked"]["mismatch_supervisor"]["status"])
        self.assertEqual("candidate_only", result["routes"]["F_antigravity_runtime"]["scope"])

        json_report = Path(result["reports"]["json"])
        md_report = Path(result["reports"]["markdown"])
        self.assertTrue(json_report.exists())
        self.assertTrue(md_report.exists())
        self.assertIn("人类可读", md_report.read_text(encoding="utf-8"))
        saved = json.loads(json_report.read_text(encoding="utf-8"))
        self.assertEqual(result["metrics"], saved["metrics"])
        conn = sqlite3.connect(self.db)
        try:
            leaked = conn.execute("SELECT COUNT(*) FROM tasks WHERE id LIKE 'acceptance-%'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(0, leaked)

    def test_direct_result_without_sender_visible_receipt_fails(self) -> None:
        payload = {
            "ok": True,
            "reply": "codex_DIRECT_OK",
            "receipt": None,
            "message": {"id": "msg-1"},
        }

        verdict = communication_acceptance.evaluate_direct_payload(payload, expected_token="codex_DIRECT_OK")

        self.assertFalse(verdict["passed"])
        self.assertEqual("missing_sender_visible_receipt", verdict["reason"])

    def test_wrong_task_id_completion_does_not_satisfy_stale_recovery(self) -> None:
        result = communication_acceptance.run_acceptance(
            simulate=True,
            direct_rounds=1,
            continuity_runs=1,
            output_dir=self.output_dir,
            now_ts="2026-06-07T00:10:00+07:00",
        )

        mismatch = result["routes"]["E_stale_blocked"]["mismatch_supervisor"]
        self.assertEqual("stalled", mismatch["status"])
        self.assertIn("没有进度证据", mismatch["human_message"])
        self.assertEqual([], mismatch["progress_history"])

    def test_compact_result_keeps_report_paths_and_key_metrics(self) -> None:
        result = communication_acceptance.run_acceptance(
            simulate=True,
            direct_rounds=1,
            continuity_runs=1,
            output_dir=self.output_dir,
            now_ts="2026-06-07T00:10:00+07:00",
        )

        compact = communication_acceptance.compact_result(result)

        self.assertTrue(compact["ok"])
        self.assertEqual("simulated", compact["mode"])
        self.assertEqual(4, compact["metrics"]["direct_total"])
        self.assertIn("json", compact["reports"])
        self.assertIn("本机员工协作通信机制", compact["human_summary"])
        self.assertNotIn("routes", compact)

    def test_openclaw_autonomous_evidence_is_read_only_and_summarizes_delivery(self) -> None:
        openclaw_root = self.root / "openclaw"
        supervisor = openclaw_root / "scripts" / "openclaw_agent_supervisor.py"
        delivery_loop = openclaw_root / "workspace-xmanx" / "scripts" / "supervisor_autonomous_delivery_loop.py"
        state_path = openclaw_root / "reports" / "openclaw-agent-supervisor-state.json"
        delivery_path = openclaw_root / "reports" / "openclaw-agent-supervisor-delivery-state.json"
        launch_agent = self.root / "Library" / "LaunchAgents" / "com.shift.ops-bus-worker.plist"
        for path in (supervisor, delivery_loop, state_path, delivery_path, launch_agent):
            path.parent.mkdir(parents=True, exist_ok=True)
        supervisor.write_text("# supervisor\n", encoding="utf-8")
        delivery_loop.write_text("# delivery loop\n", encoding="utf-8")
        launch_agent.write_text(
            f"""<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
  <key>Label</key><string>com.shift.ops-bus-worker</string>
  <key>ProgramArguments</key><array><string>python3</string><string>{supervisor}</string><string>--once</string></array>
  <key>StartInterval</key><integer>60</integer>
</dict></plist>
""",
            encoding="utf-8",
        )
        state_path.write_text(
            json.dumps(
                {
                    "ok": True,
                    "before": {"main": {"inbox": 0, "running": 0, "done": 1, "failed": 0}},
                    "after": {"main": {"inbox": 0, "running": 0, "done": 1, "failed": 0}},
                    "steps": [{"name": "task_bus_status", "ok": True, "returncode": 0}],
                }
            ),
            encoding="utf-8",
        )
        delivery_path.write_text(
            json.dumps(
                {
                    "ok": True,
                    "dry_run": False,
                    "suppressed": False,
                    "payload": {"ok": True, "messageId": "10997"},
                    "history": [{"messageId": "10995"}],
                }
            ),
            encoding="utf-8",
        )

        evidence = communication_acceptance.read_openclaw_autonomous_evidence(
            openclaw_root=openclaw_root,
            launch_agent_path=launch_agent,
            expected_message_ids=["10995", "10997"],
        )

        self.assertTrue(evidence["ok"], evidence)
        self.assertTrue(evidence["green"], evidence)
        self.assertTrue(evidence["read_only"], evidence)
        self.assertEqual([], evidence["expected_message_ids"]["missing"])
        self.assertEqual(["10995", "10997"], evidence["telegram_message_ids"])
        self.assertEqual(0, evidence["bus_totals"]["after"]["inbox"])
        self.assertTrue(evidence["launch_agent"]["references_supervisor"])
        self.assertFalse((self.root / "openclaw-agent-supervisor-state.json").exists())

    def test_openclaw_route_reports_not_green_without_failing_core_mechanism(self) -> None:
        openclaw_root = self.root / "openclaw"
        supervisor = openclaw_root / "scripts" / "openclaw_agent_supervisor.py"
        delivery_loop = openclaw_root / "workspace-xmanx" / "scripts" / "supervisor_autonomous_delivery_loop.py"
        state_path = openclaw_root / "reports" / "openclaw-agent-supervisor-state.json"
        delivery_path = openclaw_root / "reports" / "openclaw-agent-supervisor-delivery-state.json"
        launch_agent = self.root / "Library" / "LaunchAgents" / "com.shift.ops-bus-worker.plist"
        for path in (supervisor, delivery_loop, state_path, delivery_path, launch_agent):
            path.parent.mkdir(parents=True, exist_ok=True)
        supervisor.write_text("# supervisor\n", encoding="utf-8")
        delivery_loop.write_text("# delivery loop\n", encoding="utf-8")
        launch_agent.write_text(f"<plist><dict><string>{supervisor}</string></dict></plist>", encoding="utf-8")
        state_path.write_text(
            json.dumps(
                {
                    "ok": False,
                    "before": {"video-creator": {"inbox": 1, "running": 0, "done": 50, "failed": 0}},
                    "after": {"video-creator": {"inbox": 1, "running": 0, "done": 50, "failed": 0}},
                    "steps": [{"name": "agent_worker:video-creator", "ok": False, "returncode": "timeout"}],
                }
            ),
            encoding="utf-8",
        )
        delivery_path.write_text(
            json.dumps({"ok": True, "suppressed": True, "notify_reason": "supervisor_error_without_active_agents_suppressed"}),
            encoding="utf-8",
        )

        result = communication_acceptance.run_acceptance(
            simulate=True,
            direct_rounds=1,
            continuity_runs=1,
            output_dir=self.output_dir,
            now_ts="2026-06-07T00:10:00+07:00",
            include_openclaw_evidence=True,
            openclaw_root=openclaw_root,
            openclaw_launch_agent_path=launch_agent,
        )

        self.assertTrue(result["ok"], result)
        route = result["routes"]["G_openclaw_autonomous_delivery_readonly"]
        self.assertTrue(route["ok"], route)
        self.assertFalse(route["green"], route)
        self.assertIn("OpenClaw autonomous delivery evidence is readable but not green", "\n".join(result["risks"]))


if __name__ == "__main__":
    unittest.main()
