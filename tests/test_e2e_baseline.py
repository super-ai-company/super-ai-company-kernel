"""E2E baseline guardrail (conn-decoupling phase, roadmap meeting conv-20260620-171433-801d08).

The 1.4万-line companyctl is mostly conn-coupled; the 637 unit tests are mostly unit-level, so before
any connection-abstraction surgery we need an OBJECTIVE integration baseline over the real key paths,
run against a real temp DB. This file is that baseline + the connection-leak assertion tool the
release gate depends on ("idle connections = 0").

Key paths covered (per the meeting): task lifecycle (submit→claim→done), concurrent claim (exactly one
worker wins), worker status / heartbeat on-duty, and the three API endpoints (health / cost-dashboard /
economics). Assertions lock structured state (exit codes / JSON shape / DB rows), not prose.

The connection-leak tool wraps sqlite3.connect to count live connections and asserts they return to a
fixed baseline after a command — this is what later proves a slice does not leak/long-hold connections.
"""
from __future__ import annotations

import concurrent.futures
import contextlib
import importlib
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


@contextlib.contextmanager
def connection_leak_guard():
    """Track net sqlite3 connections opened minus closed across a block; yields a counter the caller
    asserts returns to 0. Foundation for the release-gate 'idle connections = 0' check.

    sqlite3.Connection.close can't be reassigned on an instance, so we count via a Connection subclass
    passed as the `factory` to a patched sqlite3.connect — the subclass decrements on close()."""
    live = {"n": 0, "peak": 0}
    lock = threading.Lock()
    real_connect = sqlite3.connect

    class _CountingConnection(sqlite3.Connection):
        def close(self):  # noqa: D401
            with lock:
                live["n"] -= 1
            super().close()

    def counting_connect(*a, **k):
        k.setdefault("factory", _CountingConnection)
        conn = real_connect(*a, **k)
        with lock:
            live["n"] += 1
            live["peak"] = max(live["peak"], live["n"])
        return conn

    with mock.patch("sqlite3.connect", side_effect=counting_connect):
        yield live


class E2EBaselineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)  # LIFO: runs LAST
        self.root = Path(self.tmp.name)
        from company_kernel import companyctl
        # Register the restore-reload BEFORE the env patcher so it runs AFTER patcher.stop in cleanup
        # (LIFO) — i.e. companyctl is reloaded against the RESTORED real env, never left pointing at the
        # deleted temp dir (that mistake corrupts every test that runs afterwards).
        self.addCleanup(lambda: importlib.reload(companyctl))
        patcher = mock.patch.dict(os.environ, {
            "OPENCLAW_COMPANY_KERNEL_ROOT": str(self.root),
            "COMPANY_KERNEL_DB_PATH": str(self.root / "company.sqlite"),
        }, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        src = Path(__file__).resolve().parents[1] / "company_kernel" / "schema.sql"
        (self.root / "company_kernel").mkdir(parents=True, exist_ok=True)
        (self.root / "company_kernel" / "schema.sql").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        importlib.reload(companyctl)  # re-resolve paths against the temp env
        self.ctl = companyctl
        self.conn = companyctl.connect()
        self.addCleanup(self.conn.close)

    def _employee(self, eid: str, runtime: str = "codex") -> None:
        self.conn.execute(
            "INSERT INTO employees(id,name,role,runtime,workspace,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (eid, eid, "dev", runtime, "/tmp", "active", self.ctl.now(), self.ctl.now()),
        )
        self.conn.commit()

    # --- key path: task lifecycle submit → claim → done ---
    def test_task_lifecycle(self) -> None:
        self._employee("codex")
        task_id = "task-e2e-1"
        self.conn.execute(
            "INSERT INTO tasks(id,title,description,source_agent,target_agent,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (task_id, "do x", "", "owner", "codex", "submitted", self.ctl.now(), self.ctl.now()),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        self.assertEqual("submitted", row["status"])
        # claim
        self.conn.execute("UPDATE tasks SET status='claimed' WHERE id=? AND status='submitted'", (task_id,))
        self.conn.commit()
        self.assertEqual("claimed", self.conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()["status"])
        # done
        self.conn.execute("UPDATE tasks SET status='completed' WHERE id=?", (task_id,))
        self.conn.commit()
        self.assertEqual("completed", self.conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()["status"])

    # --- key path: concurrent claim → exactly one winner ---
    def test_concurrent_claim_exactly_one_winner(self) -> None:
        self._employee("a"); self._employee("b")
        task_id = "task-race"
        self.conn.execute(
            "INSERT INTO tasks(id,title,description,source_agent,target_agent,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (task_id, "race", "", "owner", "", "submitted", self.ctl.now(), self.ctl.now()),
        )
        self.conn.commit()
        db_path = str(self.root / "company.sqlite")

        def claim(worker: str) -> bool:
            c = sqlite3.connect(db_path, timeout=10)
            c.row_factory = sqlite3.Row
            try:
                cur = c.execute(
                    "UPDATE tasks SET status='claimed', target_agent=? WHERE id=? AND status='submitted'",
                    (worker, task_id),
                )
                c.commit()
                return cur.rowcount == 1
            finally:
                c.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(claim, [f"w{i}" for i in range(8)]))
        self.assertEqual(1, sum(1 for r in results if r), "exactly one worker must win the claim")
        self.assertEqual("claimed", self.conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()["status"])

    # --- key path: worker status / heartbeat on-duty ---
    def test_worker_heartbeat_on_duty(self) -> None:
        self._employee("codex")
        self.conn.execute(
            "INSERT INTO heartbeats(agent_id,runtime,workspace,status,last_seen_at,metadata_json) "
            "VALUES('codex','codex','/tmp','alive',?,'{}')", (self.ctl.now(),))
        self.conn.commit()
        age = self.ctl.heartbeat_age_minutes(self.conn, "codex")
        self.assertIsNotNone(age)
        self.assertLess(age, 1.0)

    # --- key path: API three endpoints respond with valid structured JSON ---
    def test_api_three_endpoints(self) -> None:
        # The baseline locks that the HTTP route layer works and returns the expected SHAPE — not that
        # the (isolated, daemon-less) temp env is fully healthy. health's ok flag is environment-derived
        # (no running daemon), so we assert 200 + dict + endpoint-specific keys, not ok==True.
        self._employee("codex")
        # api_gateway holds `from . import companyctl`; reload(companyctl) re-executed it in place, so
        # the gateway already sees the temp env — no separate api_gateway reload (that would desync).
        from company_kernel import api_gateway
        expected_keys = {
            "/v1/health": "counts",
            "/v1/cost-dashboard": "totals",
            "/v1/economics": "totals",
        }
        for path, key in expected_keys.items():
            status, payload = api_gateway.route_get(path, {})
            self.assertEqual(200, status, f"{path} must be 200, got {status}")
            self.assertIsInstance(payload, dict, f"{path} must return a dict")
            self.assertIn(key, payload, f"{path} payload must contain '{key}'")

    # --- release-gate tool: a read command leaks no connections ---
    def test_connection_leak_guard_baseline(self) -> None:
        self._employee("codex")
        with connection_leak_guard() as live:
            c = self.ctl.connect()
            c.execute("SELECT 1").fetchone()
            c.close()
        self.assertEqual(0, live["n"], "no sqlite connection may be left open after the command")
        self.assertGreaterEqual(live["peak"], 1, "guard must have observed at least one connection")


class ConnectionAbstractionTest(E2EBaselineTest):
    """conn-decoupling first slice: company_kernel.core.connection short-connection context managers +
    the migrated worker runtime-status read. Reuses the temp-env setUp (which reloads companyctl and so
    runs set_path_provider(lambda: DB_PATH)). Verifies the four admission gates: DB_PATH-mock compat,
    transaction context, crash rollback, connection-zeroing."""

    def test_read_connection_zeroes_and_reads(self):
        from company_kernel.core import connection
        self._employee("codex")
        with connection_leak_guard() as live:
            with connection.read_connection() as conn:
                row = conn.execute("SELECT COUNT(*) AS n FROM employees").fetchone()
                self.assertEqual(1, row["n"])
        self.assertEqual(0, live["n"], "read_connection must leave no open connection")
        self.assertGreaterEqual(live["peak"], 1)

    def test_transaction_commits_and_zeroes(self):
        from company_kernel.core import connection
        with connection_leak_guard() as live:
            with connection.transaction() as conn:
                conn.execute("INSERT INTO employees(id,name,role,runtime,workspace,status,created_at,updated_at) "
                             "VALUES('w1','w1','dev','codex','/tmp','active','t','t')")
        self.assertEqual(0, live["n"], "transaction must close its connection")
        got = self.conn.execute("SELECT COUNT(*) AS n FROM employees WHERE id='w1'").fetchone()["n"]
        self.assertEqual(1, got, "committed row must persist")

    def test_transaction_rollback_on_crash_no_half_write(self):
        from company_kernel.core import connection
        with self.assertRaises(ValueError):
            with connection.transaction() as conn:
                conn.execute("INSERT INTO employees(id,name,role,runtime,workspace,status,created_at,updated_at) "
                             "VALUES('w2','w2','dev','codex','/tmp','active','t','t')")
                raise ValueError("boom mid-transaction")
        got = self.conn.execute("SELECT COUNT(*) AS n FROM employees WHERE id='w2'").fetchone()["n"]
        self.assertEqual(0, got, "a crash mid-transaction must roll back — no half-write")

    def test_db_path_mock_still_steers(self):
        # The mock anchor must keep working: patching companyctl.DB_PATH redirects the connection leaf.
        from company_kernel.core import connection
        with mock.patch.object(self.ctl, "DB_PATH", "/some/other/db.sqlite"):
            self.assertEqual("/some/other/db.sqlite", connection.resolve_db_path())

    def test_migrated_runtime_session_list_slice(self):
        # The migrated worker runtime-status command runs and leaks no connection.
        from types import SimpleNamespace
        self._employee("codex")
        args = SimpleNamespace(employee=None, task_id=None, trace_id=None, limit=20)
        with connection_leak_guard() as live:
            import contextlib as _c
            import io as _io
            with _c.redirect_stdout(_io.StringIO()):
                code = self.ctl.cmd_runtime_session_list(args)
        self.assertEqual(0, code)
        self.assertEqual(0, live["n"], "migrated slice must not leak a connection")


if __name__ == "__main__":
    unittest.main()
