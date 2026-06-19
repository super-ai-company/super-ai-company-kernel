from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from company_kernel import codex_adapter, company_daemon


def iso(dt: datetime) -> str:
    return dt.astimezone().isoformat(timespec="seconds")


class DaemonWatchdogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "watchdog.json"
        self.addCleanup(self.tmp.cleanup)

    def _config(self, **overrides) -> dict:
        cfg = {
            "watchdog": {
                "enabled": True,
                "unclaimed_minutes": 10,
                "notify": "owner",
                "from": "openclaw-main",
                "max_alerts_per_tick": 5,
                **overrides,
            }
        }
        return cfg

    def test_watchdog_disabled_returns_empty(self) -> None:
        self.assertEqual([], company_daemon.check_unclaimed_tasks({"watchdog": {"enabled": False}}))
        self.assertEqual([], company_daemon.check_unclaimed_tasks({}))

    def test_watchdog_alerts_once_per_stale_task(self) -> None:
        stale_at = iso(datetime.now(timezone.utc) - timedelta(minutes=30))
        rows = [
            {"id": "task-stale-1", "target_agent": "codex", "title": "fix bug", "created_at": stale_at},
        ]

        class FakeConn:
            def execute(self, sql, params=()):
                class Cursor:
                    def fetchall(inner) -> list[dict]:
                        return rows

                return Cursor()

            def close(self):
                pass

        sent: list[list[str]] = []

        def fake_run_companyctl(*args: str) -> dict:
            sent.append(list(args))
            return {"command": list(args), "returncode": 0, "stdout": "{}", "stderr": ""}

        with mock.patch.object(company_daemon.companyctl, "connect", lambda: FakeConn()), \
                mock.patch.object(company_daemon, "run_companyctl", fake_run_companyctl), \
                mock.patch.object(company_daemon, "WATCHDOG_STATE_PATH", self.state_path):
            first = company_daemon.check_unclaimed_tasks(self._config())
            second = company_daemon.check_unclaimed_tasks(self._config())

        self.assertEqual(1, len(first))
        self.assertEqual([], second, "same task must not be alerted twice")
        self.assertEqual(1, len(sent))
        self.assertIn("task-stale-1", " ".join(sent[0]))
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertIn("task-stale-1", state)


class CodexTimeoutTest(unittest.TestCase):
    def test_run_codex_timeout_blocks_with_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            task_card = base / "card.md"
            task_card.write_text("# card\n", encoding="utf-8")
            output = base / "out.md"
            events = base / "events.jsonl"

            def fake_run(cmd, *, timeout=None, **kwargs):
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

            # run_codex now runs codex in its own process group via run_with_group_timeout (so a
            # timeout kills the WHOLE codex tree, not just the node shell). Mock that path.
            with mock.patch.object(codex_adapter, "run_with_group_timeout", fake_run), \
                    mock.patch.object(codex_adapter, "wrap_command", lambda cmd, **kw: ["codex", "exec"]):
                code, cmd = codex_adapter.run_codex(
                    task_card, base, output, events, "workspace-write", "", "none", "default", timeout_seconds=5
                )

            self.assertEqual(codex_adapter.TIMEOUT_EXIT_CODE, code)
            self.assertIn("timeout", output.read_text(encoding="utf-8"))
            event_lines = events.read_text(encoding="utf-8").strip().splitlines()
            self.assertTrue(any("adapter.timeout" in line for line in event_lines))


if __name__ == "__main__":
    unittest.main()


class AdapterRunNoiseTest(unittest.TestCase):
    """Non-actionable adapter ticks (no task, no work) must not be recorded as failures."""

    def test_no_task_no_work_run_is_not_recorded(self):
        recorded = {"called": False}

        class FakeConn:
            def execute(self, *a, **k):
                recorded["called"] = True
                class C:
                    def fetchone(self_): return [0]
                return C()
            def commit(self): pass
            def close(self): pass

        with mock.patch.object(company_daemon.companyctl, "connect", lambda: FakeConn()):
            company_daemon.record_adapter_run({"agent": "codex", "ok": False, "processed": 0, "runs": [], "at": iso(datetime.now(timezone.utc))})
        self.assertFalse(recorded["called"], "empty no-task run should be skipped, not inserted")

    def test_run_with_task_is_recorded(self):
        recorded = {"insert": False}

        class FakeConn:
            def execute(self, sql, *a, **k):
                if sql.strip().upper().startswith("INSERT"):
                    recorded["insert"] = True
                class C:
                    def fetchone(self_): return {"max_attempt": 0}
                return C()
            def commit(self): pass
            def close(self): pass

        state = {"agent": "codex", "ok": False, "processed": 1,
                 "runs": [{"parsed_stdout": {"task_id": "task-x"}}], "at": iso(datetime.now(timezone.utc)),
                 "retry_policy": {"max_attempts": 3}}
        with mock.patch.object(company_daemon.companyctl, "connect", lambda: FakeConn()), \
                mock.patch.object(company_daemon.companyctl, "trace_id_for_task", lambda c, t, x: "trace-x"):
            company_daemon.record_adapter_run(state)
        self.assertTrue(recorded["insert"], "run with a task_id must be recorded")


class ReconcileGateTest(unittest.TestCase):
    """maybe_reconcile_status only runs when enabled and the interval elapsed."""

    def test_disabled_returns_empty(self):
        self.assertEqual([], company_daemon.maybe_reconcile_status({}))
        self.assertEqual([], company_daemon.maybe_reconcile_status({"reconcile_status": {"enabled": False}}))

    def test_recent_run_is_skipped(self):
        import tempfile, os
        from pathlib import Path as P
        with tempfile.TemporaryDirectory() as tmp:
            sp = P(tmp) / "reconcile.json"
            sp.write_text(json.dumps({"at": iso(datetime.now(timezone.utc))}), encoding="utf-8")
            with mock.patch.object(company_daemon, "RECONCILE_STATE_PATH", sp):
                out = company_daemon.maybe_reconcile_status({"reconcile_status": {"enabled": True, "interval_hours": 6}})
        self.assertEqual([], out, "a run within the interval must be skipped")
