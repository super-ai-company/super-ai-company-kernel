"""Fault-tolerance watchdog: an execution attempt running past its runtime cap is force-failed so
the task lands in the FAILURE list (blocked + dispatcher notice) instead of hanging forever."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


def iso(dt: datetime) -> str:
    return dt.astimezone().isoformat(timespec="seconds")


class ReapStuckAttemptsTest(unittest.TestCase):
    def setUp(self):
        # Registered FIRST so it runs LAST (LIFO) — i.e. AFTER the env patcher restores — so the
        # reload rebinds companyctl to the real DB_PATH and this test can't pollute later tests.
        self.addCleanup(self._restore)
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {
            "OPENCLAW_COMPANY_KERNEL_ROOT": str(self.root),
            "COMPANY_KERNEL_DB_PATH": str(self.root / "company.sqlite"),
        }, clear=False)
        patcher.start(); self.addCleanup(patcher.stop)
        src = Path(__file__).resolve().parents[1] / "company_kernel" / "schema.sql"
        (self.root / "company_kernel").mkdir(parents=True, exist_ok=True)
        (self.root / "company_kernel" / "schema.sql").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        import importlib
        from company_kernel import companyctl
        importlib.reload(companyctl)
        self.ctl = companyctl
        self.conn = companyctl.connect(); self.addCleanup(self.conn.close)
        for eid in ("codex", "antigravity"):
            self.conn.execute("INSERT INTO employees(id,name,role,runtime,workspace,status,created_at,updated_at) "
                              "VALUES(?,?,'dev','codex','/tmp','active','t','t')", (eid, eid))
        self.conn.commit()

    def _restore(self):
        import importlib
        from company_kernel import companyctl
        importlib.reload(companyctl)

    def _task_with_attempt(self, *, started_minutes_ago: int, attempt_status="running", task_status="claimed", source="antigravity", target="codex", tid="t-stuck", aid="a-stuck", pid=""):
        started = iso(datetime.now(timezone.utc) - timedelta(minutes=started_minutes_ago))
        self.conn.execute("INSERT INTO tasks(id,source_agent,target_agent,title,description,priority,status,claimed_by,created_at,updated_at) "
                          "VALUES(?,?,?,?,'','P2',?,?,'t','t')", (tid, source, target, "long task", task_status, target))
        self.conn.execute(
            "INSERT INTO execution_attempts(attempt_id,task_id,employee_id,adapter_type,status,started_at,runtime_policy_json,pid) "
            "VALUES(?,?,?,'codex',?,?,?,?)",
            (aid, tid, target, attempt_status, started, '{"max_runtime_seconds": 1800}', str(pid)))
        self.conn.commit()
        return tid, aid

    def test_reaps_attempt_past_cap_to_blocked_with_notice(self):
        tid, aid = self._task_with_attempt(started_minutes_ago=120)  # 2h > 30min policy cap
        result = self.ctl.reap_stuck_attempts_internal(self.conn, actor="openclaw-main")
        self.assertEqual(1, result["reaped_count"], result)
        # attempt → stale (terminal)
        att = self.conn.execute("SELECT status FROM execution_attempts WHERE attempt_id=?", (aid,)).fetchone()[0]
        self.assertEqual("stale", att)
        # task → blocked with watchdog_reaped blocker (shows in completed/blocked failure list)
        row = self.conn.execute("SELECT status, blocker FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual("blocked", row[0])
        self.assertIn("watchdog_reaped", row[1])
        # event recorded
        ev = self.conn.execute("SELECT COUNT(*) FROM company_events WHERE event_type='task.watchdog_reaped' AND task_id=?", (tid,)).fetchone()[0]
        self.assertEqual(1, ev)
        # dispatcher (source_agent) got the result notice
        notice = self.root / "employees" / "antigravity" / "inbox" / f"result-{tid}.json"
        self.assertTrue(notice.exists(), "dispatcher must be notified the stuck task was reaped")

    def test_fresh_attempt_within_cap_is_left_alone(self):
        tid, aid = self._task_with_attempt(started_minutes_ago=5)  # well within 30min
        result = self.ctl.reap_stuck_attempts_internal(self.conn, actor="openclaw-main")
        self.assertEqual(0, result["reaped_count"])
        self.assertEqual("running", self.conn.execute("SELECT status FROM execution_attempts WHERE attempt_id=?", (aid,)).fetchone()[0])
        self.assertEqual("claimed", self.conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0])

    def test_reaped_task_appears_in_failure_report(self):
        tid, _ = self._task_with_attempt(started_minutes_ago=120)
        self.ctl.reap_stuck_attempts_internal(self.conn, actor="openclaw-main")
        reports = self.ctl.completed_report_rows(self.conn, limit=40, include_blocked=True)
        self.assertIn(tid, [r["id"] for r in reports], "reaped task must surface in the owner failure feed")

    def test_late_finish_after_reap_is_idempotent_not_crash(self):
        # The adapter that was hung finally returns and calls attempt finish — must be a no-op, not raise.
        _, aid = self._task_with_attempt(started_minutes_ago=120)
        self.ctl.reap_stuck_attempts_internal(self.conn, actor="openclaw-main")
        res = self.ctl.finish_execution_attempt_internal(self.conn, attempt_id=aid, status="success")
        self.assertTrue(res.get("late_finish_ignored"))
        self.assertEqual("stale", self.conn.execute("SELECT status FROM execution_attempts WHERE attempt_id=?", (aid,)).fetchone()[0])

    def test_orphan_reaped_when_pid_dead_before_runtime_cap(self):
        # 10 min in (well under the 30-min cap) but its adapter pid is dead → orphan, reaped fast.
        dead_pid = 2_000_000_000  # implausibly-high pid → not alive
        tid, aid = self._task_with_attempt(started_minutes_ago=10, pid=dead_pid, tid="t-orphan", aid="a-orphan")
        result = self.ctl.reap_stuck_attempts_internal(self.conn, actor="openclaw-main")
        self.assertEqual(1, result["reaped_count"], result)
        self.assertEqual("worker_process_gone", result["reaped"][0]["reason"])
        self.assertIn("worker_process_gone", self.conn.execute("SELECT blocker FROM tasks WHERE id=?", (tid,)).fetchone()[0])

    def test_live_pid_within_cap_is_not_reaped(self):
        # Our own pid is alive → a within-cap attempt owned by a live process must NOT be reaped.
        tid, aid = self._task_with_attempt(started_minutes_ago=10, pid=os.getpid(), tid="t-live", aid="a-live")
        result = self.ctl.reap_stuck_attempts_internal(self.conn, actor="openclaw-main")
        self.assertEqual(0, result["reaped_count"], result)
        self.assertEqual("running", self.conn.execute("SELECT status FROM execution_attempts WHERE attempt_id=?", (aid,)).fetchone()[0])

    def test_dead_pid_within_grace_is_not_reaped(self):
        # Dead pid but only 1 min in (< 120s orphan grace) → don't race a just-started adapter.
        tid, aid = self._task_with_attempt(started_minutes_ago=1, pid=2_000_000_000, tid="t-grace", aid="a-grace")
        result = self.ctl.reap_stuck_attempts_internal(self.conn, actor="openclaw-main")
        self.assertEqual(0, result["reaped_count"], result)

    def test_process_alive_helper(self):
        self.assertTrue(self.ctl.process_alive(os.getpid()))
        self.assertFalse(self.ctl.process_alive(2_000_000_000))
        self.assertFalse(self.ctl.process_alive(0))

    def test_idempotent_no_double_reap(self):
        tid, _ = self._task_with_attempt(started_minutes_ago=120)
        self.ctl.reap_stuck_attempts_internal(self.conn, actor="openclaw-main")
        second = self.ctl.reap_stuck_attempts_internal(self.conn, actor="openclaw-main")
        self.assertEqual(0, second["reaped_count"], "an already-reaped attempt must not be reaped again")


if __name__ == "__main__":
    unittest.main()
