"""Offline-employee reporting must not cry wolf about interactive app employees.

codex/claude/antigravity apps run only when the owner opens them, so a stale heartbeat is normal — not
an outage. They are classified dormant (never alerted). Their CLI twins (codex-cli/claude-cli/agy) are
daemon workers that SHOULD stay online, so those still surface as offline when they drop.
"""
from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from company_kernel import companyctl as c

SCHEMA = Path(__file__).resolve().parents[1] / "company_kernel" / "schema.sql"


class OfflineReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA.read_text(encoding="utf-8"))

    def _emp(self, eid: str, runtime: str) -> None:
        ts = "2026-01-01T00:00:00+00:00"
        self.conn.execute(
            "INSERT INTO employees(id,name,role,runtime,workspace,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,'active',?,?)", (eid, eid, "worker", runtime, "/tmp/" + eid, ts, ts))

    def _heartbeat(self, eid: str, minutes_ago: float) -> None:
        seen = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
        self.conn.execute("INSERT INTO heartbeats(agent_id,last_seen_at) VALUES (?,?)", (eid, seen))

    def test_app_employees_are_dormant_cli_twins_alert(self) -> None:
        self._emp("codex", "codex")
        self._emp("codex-cli", "codex")
        self._heartbeat("codex", 60)       # app stale 60 min — normal, not an outage
        self._heartbeat("codex-cli", 60)   # cli worker stale 60 min — a real drop
        self.conn.commit()

        rep = c.employee_offline_report_internal(self.conn, stale_minutes=10, dormant_minutes=1440)
        offline = {e["id"] for e in rep["offline"]}
        dormant = {e["id"] for e in rep["dormant"]}

        self.assertNotIn("codex", offline)     # app must never trigger an offline alert
        self.assertIn("codex", dormant)        # ...it's dormant instead
        self.assertIn("codex-cli", offline)    # the daemon worker still surfaces when it drops

    def test_fresh_app_still_counts_online(self) -> None:
        self._emp("claude", "claude")
        self._heartbeat("claude", 1)           # just seen → online regardless of being an app
        self.conn.commit()
        rep = c.employee_offline_report_internal(self.conn, stale_minutes=10)
        self.assertIn("claude", {e["id"] for e in rep["online"]})
        self.assertEqual([], rep["offline"])


if __name__ == "__main__":
    unittest.main()
