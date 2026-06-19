"""When a dispatched task finishes, the kernel drops a result-<task>.json into the DISPATCHER's inbox
so an always-on app can watch its inbox (event-driven) instead of polling."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class DispatcherNoticeTest(unittest.TestCase):
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
        self.conn = companyctl.connect()
        self.addCleanup(self.conn.close)
        self.conn.execute("INSERT INTO employees(id,name,role,runtime,workspace,status,created_at,updated_at) "
                          "VALUES('codex','Codex','dev','codex','/tmp','active','t','t')")
        self.conn.commit()

    def _restore(self):
        import importlib
        from company_kernel import companyctl
        importlib.reload(companyctl)

    def _task(self, **kw):
        base = {"id": "t1", "title": "审核X", "source_agent": "codex", "target_agent": "antigravity"}
        base.update(kw); return base

    def test_writes_notice_to_dispatcher_inbox(self):
        path = self.ctl.write_dispatcher_completion_notice(
            self.conn, self._task(), status="completed", summary="3 条建议", evidence="/e.txt")
        self.assertTrue(path)
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        self.assertEqual("completed", d["status"])
        self.assertEqual("antigravity", d["done_by"])     # who did the work
        self.assertIn("已完成", d["note"])
        self.assertEqual("3 条建议", d["summary"])
        self.assertTrue(path.endswith("codex/inbox/result-t1.json"))  # lands in the DISPATCHER's inbox

    def test_noop_when_source_not_registered(self):
        path = self.ctl.write_dispatcher_completion_notice(
            self.conn, self._task(source_agent="ghost"), status="completed")
        self.assertEqual("", path)

    def test_noop_when_source_equals_target(self):
        path = self.ctl.write_dispatcher_completion_notice(
            self.conn, self._task(source_agent="codex", target_agent="codex"), status="completed")
        self.assertEqual("", path)

    def test_blocked_notice(self):
        path = self.ctl.write_dispatcher_completion_notice(
            self.conn, self._task(), status="blocked", blocker="缺依赖")
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        self.assertEqual("blocked", d["status"])
        self.assertEqual("缺依赖", d["blocker"])
        self.assertIn("受阻", d["note"])

    def test_deliver_helper_returns_path_on_success(self):
        path = self.ctl.deliver_completion_notice(
            self.conn, self._task(), status="completed", summary="ok", evidence="/e.txt", actor="antigravity")
        self.assertTrue(path.endswith("codex/inbox/result-t1.json"))

    def test_deliver_helper_never_raises_and_records_failure(self):
        # Simulate a delivery failure (e.g. disk/permission error during write): the helper must
        # NOT propagate — a finished task must not be rolled back by a notification hiccup — but the
        # failure must leave an observable event so a broken loop is visible, not silent.
        with mock.patch.object(self.ctl, "write_dispatcher_completion_notice", side_effect=OSError("disk full")):
            path = self.ctl.deliver_completion_notice(
                self.conn, self._task(), status="completed", actor="antigravity")
        self.assertEqual("", path)
        row = self.conn.execute(
            "SELECT COUNT(*) FROM company_events WHERE event_type = 'task.completion_notice_failed' AND task_id = 't1'"
        ).fetchone()[0]
        self.assertEqual(1, row, "a failed delivery must record an observable event")


if __name__ == "__main__":
    unittest.main()
