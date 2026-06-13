from __future__ import annotations

import sqlite3
import unittest

from company_kernel import companyctl


def fresh_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(companyctl.SCHEMA.read_text(encoding="utf-8"))
    conn.commit()
    return conn


class A2AApprovalTest(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_conn()
        self.addCleanup(self.conn.close)

    def req(self):
        return companyctl.record_a2a_request_internal(
            self.conn, source_agent="hermes", target_agent="codex",
            action="sessions_send", payload="round-trip",
        )

    def test_request_is_pending_and_default_deny(self):
        r = self.req()
        self.assertEqual("pending", r["status"])
        self.assertTrue(r["a2a_request_id"].startswith("a2a-"))

    def test_request_emits_telegram_keyboard(self):
        r = self.req()
        kb = r["telegram"]["reply_markup"]["inline_keyboard"][0]
        self.assertEqual(2, len(kb))
        self.assertEqual(f"a2a:approve:{r['a2a_request_id']}", kb[0]["callback_data"])
        self.assertEqual(f"a2a:deny:{r['a2a_request_id']}", kb[1]["callback_data"])
        self.assertLessEqual(len(kb[0]["callback_data"].encode("utf-8")), 64)

    def test_approve_sets_allowed(self):
        r = self.req()
        d = companyctl.decide_a2a_internal(self.conn, a2a_request_id=r["a2a_request_id"], by="owner-shift", decision="approved")
        self.assertTrue(d["ok"])
        self.assertTrue(d["allowed"])
        self.assertEqual("approved", d["status"])

    def test_deny_blocks(self):
        r = self.req()
        d = companyctl.decide_a2a_internal(self.conn, a2a_request_id=r["a2a_request_id"], by="owner-shift", decision="denied")
        self.assertTrue(d["ok"])
        self.assertFalse(d["allowed"])

    def test_double_decision_rejected(self):
        r = self.req()
        companyctl.decide_a2a_internal(self.conn, a2a_request_id=r["a2a_request_id"], by="owner-shift", decision="approved")
        again = companyctl.decide_a2a_internal(self.conn, a2a_request_id=r["a2a_request_id"], by="owner-shift", decision="denied")
        self.assertFalse(again["ok"])
        self.assertIn("already", again["error"])

    def test_decision_is_audited(self):
        r = self.req()
        companyctl.decide_a2a_internal(self.conn, a2a_request_id=r["a2a_request_id"], by="owner-shift", decision="approved")
        n = self.conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action = 'a2a.approve'").fetchone()[0]
        self.assertEqual(1, n)

    def test_unknown_request(self):
        d = companyctl.decide_a2a_internal(self.conn, a2a_request_id="nope", by="owner-shift", decision="approved")
        self.assertFalse(d["ok"])


if __name__ == "__main__":
    unittest.main()
