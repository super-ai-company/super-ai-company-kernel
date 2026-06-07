from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from company_kernel import codex_pm_supervisor


class CodexPmSupervisorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "company_kernel").mkdir()
        source_schema = Path(__file__).resolve().parents[1] / "company_kernel" / "schema.sql"
        self.schema = self.root / "company_kernel" / "schema.sql"
        self.schema.write_text(source_schema.read_text(encoding="utf-8"), encoding="utf-8")
        self.workspace = self.root / "workspace" / "codex"
        self.workspace.mkdir(parents=True)
        self.db = self.root / "company.sqlite"
        self.patchers = [
            mock.patch.object(codex_pm_supervisor, "ROOT", self.root),
            mock.patch.object(codex_pm_supervisor, "DB_PATH", self.db),
            mock.patch.object(codex_pm_supervisor, "SCHEMA", self.schema),
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
        now = "2026-06-06T00:00:00+07:00"
        conn.execute(
            "INSERT INTO employees(id, name, role, runtime, workspace, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
            ("codex", "Codex", "developer", "codex", str(self.workspace), now, now),
        )
        conn.execute(
            "INSERT INTO employees(id, name, role, runtime, workspace, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
            ("hermes", "Hermes", "supervisor", "hermes", str(self.root / "hermes"), now, now),
        )
        conn.execute(
            "INSERT INTO tasks(id, source_agent, target_agent, title, description, priority, status, claimed_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'P2', ?, ?, ?, ?)",
            ("task-codex-1", "hermes", "codex", "修复首页按钮", "需要真实验证", "claimed", "codex", now, now),
        )
        conn.commit()
        conn.close()

    def _write_progress(self, state: str, action: str = "修复首页按钮") -> Path:
        reports = self.workspace / "reports"
        reports.mkdir(exist_ok=True)
        path = reports / f"progress_{state}_20260606-000000.json"
        path.write_text(
            json.dumps(
                {
                    "ok": True,
                    "task_id": "task-codex-1",
                    "report": {
                        "state": state,
                        "project": "codex",
                        "action": action,
                        "checking": "pytest passed",
                        "created_at": "2026-06-06T00:00:01+07:00",
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def test_supervisor_returns_phase_chain_for_same_task(self) -> None:
        self._write_progress("acknowledged", action="已接收任务")
        self._write_progress("in_progress", action="正在实现")
        completed = self._write_progress("completed", action="完成验证")

        result = codex_pm_supervisor.supervise_once(agent="codex", now_ts="2026-06-06T00:00:10+07:00")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["evidence_path"], str(completed.resolve()))
        self.assertEqual(result["latest_progress_path"], str(completed.resolve()))
        self.assertEqual(result["progress_layer"], "done")
        self.assertEqual(result["progress_state"], "completed")
        self.assertEqual(
            ["acknowledged", "in_progress", "completed"],
            [item["state"] for item in result["progress_history"]],
        )
        self.assertEqual("task-codex-1", result["progress_history"][0]["task_id"])

    def test_unrelated_completed_progress_does_not_complete_current_task(self) -> None:
        reports = self.workspace / "reports"
        reports.mkdir()
        path = reports / "progress_completed_unrelated.json"
        path.write_text(
            json.dumps(
                {
                    "ok": True,
                    "task_id": "some-other-task",
                    "report": {
                        "state": "completed",
                        "project": "codex",
                        "action": "旧任务",
                        "checking": "old evidence",
                        "created_at": "2026-06-06T00:00:01+07:00",
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        result = codex_pm_supervisor.supervise_once(agent="codex", now_ts="2026-06-06T00:40:00+07:00", stale_minutes=10)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "stalled")
        self.assertIn("没有进度证据", result["human_message"])

    def test_completed_progress_accepts_codex_task(self) -> None:
        progress = self._write_progress("completed")
        result = codex_pm_supervisor.supervise_once(agent="codex", now_ts="2026-06-06T00:00:10+07:00")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "completed")
        self.assertIn("完成了 Codex 的 修复首页按钮 任务", result["human_message"])
        self.assertEqual(result["evidence_path"], str(progress.resolve()))

    def test_stale_in_progress_becomes_stalled(self) -> None:
        progress = self._write_progress("in_progress")
        result = codex_pm_supervisor.supervise_once(agent="codex", now_ts="2026-06-06T00:40:00+07:00", stale_minutes=10)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "stalled")
        self.assertIn("Codex 卡住", result["human_message"])
        self.assertEqual(result["evidence_path"], str(progress.resolve()))

    def test_stale_working_alias_becomes_stalled(self) -> None:
        progress = self._write_progress("working")
        result = codex_pm_supervisor.supervise_once(agent="codex", now_ts="2026-06-06T00:40:00+07:00", stale_minutes=10)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "stalled")
        self.assertEqual("working", result["progress_layer"])
        self.assertEqual("working", result["progress_state"])
        self.assertEqual(result["evidence_path"], str(progress.resolve()))

    def test_workspace_override_reads_progress_from_explicit_dev_workspace(self) -> None:
        explicit_workspace = self.root / "dev-workspace" / "codex"
        explicit_workspace.mkdir(parents=True)
        reports = explicit_workspace / "reports"
        reports.mkdir()
        progress = reports / "progress_completed_explicit.json"
        progress.write_text(
            json.dumps(
                {
                    "ok": True,
                    "task_id": "task-codex-1",
                    "report": {
                        "state": "completed",
                        "project": "codex",
                        "action": "显式开发工作区任务",
                        "checking": "explicit workspace matched",
                        "created_at": "2026-06-06T00:00:01+07:00",
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE employees SET workspace = ? WHERE id = 'codex'", (str(self.root / "wrong-workspace"),))
        conn.commit()
        conn.close()

        result = codex_pm_supervisor.supervise_once(
            agent="codex",
            now_ts="2026-06-06T00:00:10+07:00",
            workspace=explicit_workspace,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["evidence_path"], str(progress.resolve()))
        self.assertEqual(result["workspace"], str(explicit_workspace.resolve()))

    def test_cli_flags_override_db_workspace_and_report_root(self) -> None:
        explicit_root = self.root / "portable-runtime"
        explicit_root.mkdir()
        explicit_db = explicit_root / "company.sqlite"
        explicit_schema = explicit_root / "company_kernel"
        explicit_schema.mkdir()
        (explicit_schema / "schema.sql").write_text(self.schema.read_text(encoding="utf-8"), encoding="utf-8")
        explicit_workspace = self.root / "portable-runtime-workspace" / "codex"
        explicit_workspace.mkdir(parents=True)
        reports = explicit_workspace / "reports"
        reports.mkdir()
        progress = reports / "progress_completed_cli.json"
        progress.write_text(
            json.dumps(
                {
                    "ok": True,
                    "task_id": "task-codex-2",
                    "report": {
                        "state": "completed",
                        "project": "codex",
                        "action": "CLI 覆盖链路",
                        "checking": "cli override matched",
                        "created_at": "2026-06-06T00:00:01+07:00",
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        conn = sqlite3.connect(explicit_db)
        conn.executescript(self.schema.read_text(encoding="utf-8"))
        now = "2026-06-06T00:00:00+07:00"
        conn.execute(
            "INSERT INTO employees(id, name, role, runtime, workspace, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
            ("codex", "Codex", "developer", "codex", str(self.root / "ignored-workspace"), now, now),
        )
        conn.execute(
            "INSERT INTO tasks(id, source_agent, target_agent, title, description, priority, status, claimed_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'P1', ?, ?, ?, ?)",
            ("task-codex-2", "hermes", "codex", "接入 dev workspace", "需要显式覆盖", "claimed", "codex", now, now),
        )
        conn.commit()
        conn.close()

        stdout = StringIO()
        with redirect_stdout(stdout):
            exit_code = codex_pm_supervisor.main(
                [
                    "--agent",
                    "codex",
                    "--db-path",
                    str(explicit_db),
                    "--workspace",
                    str(explicit_workspace),
                    "--report-root",
                    str(explicit_root),
                ]
            )

        self.assertEqual(0, exit_code)
        payload = json.loads(stdout.getvalue())
        self.assertEqual("completed", payload["status"])
        self.assertEqual(str(progress.resolve()), payload["evidence_path"])
        self.assertEqual(str(explicit_workspace.resolve()), payload["workspace"])
        self.assertEqual(str(explicit_db.resolve()), payload["db_path"])
        self.assertTrue(payload["report_path"].startswith(str((explicit_root / "employees" / "hermes" / "reports" / "codex-pm").resolve())))


if __name__ == "__main__":
    unittest.main()
