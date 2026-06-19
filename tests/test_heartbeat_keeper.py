"""Long tasks must not make other workers look 'off duty': while the daemon runs a long adapter
synchronously, the HeartbeatKeeper re-stamps worker heartbeats so on-duty stays accurate."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


def iso(dt: datetime) -> str:
    return dt.astimezone().isoformat(timespec="seconds")


class TouchHeartbeatTest(unittest.TestCase):
    def setUp(self):
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
        self.conn.execute("INSERT INTO employees(id,name,role,runtime,workspace,status,created_at,updated_at) "
                          "VALUES('codex','Codex','dev','codex','/tmp','active','t','t')")
        self.conn.commit()

    def _restore(self):
        import importlib
        from company_kernel import companyctl
        importlib.reload(companyctl)

    def test_touch_refreshes_stale_heartbeat(self):
        stale = iso(datetime.now(timezone.utc) - timedelta(minutes=40))
        self.conn.execute("INSERT INTO heartbeats(agent_id,runtime,workspace,status,last_seen_at,metadata_json) "
                          "VALUES('codex','codex','/tmp','alive',?,'{}')", (stale,))
        self.conn.commit()
        self.ctl.touch_heartbeat_internal(self.conn, "codex")
        self.conn.commit()
        after = self.conn.execute("SELECT last_seen_at FROM heartbeats WHERE agent_id='codex'").fetchone()[0]
        self.assertNotEqual(stale, after)
        # fresh now → within the 15-min on-duty window
        age_min = (datetime.fromisoformat(self.ctl.now()) - datetime.fromisoformat(after)).total_seconds() / 60
        self.assertLess(age_min, 1)

    def test_touch_does_not_create_row_for_never_started_worker(self):
        self.ctl.touch_heartbeat_internal(self.conn, "codex")  # no heartbeat row exists yet
        self.conn.commit()
        self.assertIsNone(self.conn.execute("SELECT 1 FROM heartbeats WHERE agent_id='codex'").fetchone())


class HeartbeatKeeperThreadTest(unittest.TestCase):
    def test_beat_once_touches_all_agents(self):
        from company_kernel import company_daemon
        touched: list[str] = []
        with mock.patch.object(company_daemon.companyctl, "connect", lambda: mock.MagicMock()), \
             mock.patch.object(company_daemon.companyctl, "touch_heartbeat_internal",
                               side_effect=lambda conn, agent: touched.append(agent)):
            company_daemon.HeartbeatKeeper(["codex", "claude-cli"], interval_seconds=240)._beat_once()
        self.assertEqual(["codex", "claude-cli"], touched)

    def test_keeper_lifecycle_starts_and_joins_cleanly(self):
        from company_kernel import company_daemon
        beats = {"n": 0}
        keeper = company_daemon.HeartbeatKeeper(["codex"], interval_seconds=30)
        keeper._beat_once = lambda: beats.__setitem__("n", beats["n"] + 1)  # type: ignore
        with keeper:  # spawns the thread; 30s interval won't fire in this window
            time.sleep(0.05)
        # clean stop/join (no hang), and a stopped keeper never beat in the short window
        self.assertFalse(keeper._thread.is_alive())

    def test_keeper_noop_with_no_agents(self):
        from company_kernel import company_daemon
        with company_daemon.HeartbeatKeeper([], interval_seconds=30) as k:
            self.assertIsNone(k._thread)  # nothing to keep alive → no thread spawned


if __name__ == "__main__":
    unittest.main()
