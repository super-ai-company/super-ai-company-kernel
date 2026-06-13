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


def rec(conn, task_id, kind, result, **kw):
    return companyctl.record_verifier_run_internal(
        conn, task_id=task_id, attempt_id=kw.get("attempt_id", ""), employee_id="codex",
        kind=kind, arg=kw.get("arg", ""), result=result, agent_verdict="completed",
        detail=kw.get("detail", ""),
    )


class VerifierAccuracyTest(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_conn()
        self.addCleanup(self.conn.close)

    def test_record_and_counts_without_review(self):
        rec(self.conn, "t1", "numeric", "pass")
        rec(self.conn, "t2", "test", "fail")
        rec(self.conn, "t3", "human", "needs_human")
        acc = companyctl.compute_verifier_accuracy(self.conn)
        kinds = {b["kind"]: b for b in acc["by_kind"]}
        self.assertEqual(1, kinds["numeric"]["pass"])
        self.assertEqual(1, kinds["test"]["withhold"])
        self.assertEqual(1, kinds["human"]["withhold"])
        self.assertEqual(3, acc["totals"]["total"])
        self.assertEqual(0, acc["totals"]["reviewed"])
        self.assertIsNone(acc["totals"]["accuracy"])

    def test_human_review_links_to_latest_unreviewed_run_for_task(self):
        rec(self.conn, "t1", "numeric", "pass")
        companyctl.link_human_review_to_verifier(self.conn, "t1", "accepted", "owner")
        row = self.conn.execute("SELECT human_review, reviewed_at FROM verifier_runs WHERE task_id='t1'").fetchone()
        self.assertEqual("accepted", row["human_review"])
        self.assertTrue(row["reviewed_at"])

    def test_pass_accepted_is_correct(self):
        rec(self.conn, "t1", "numeric", "pass")
        companyctl.link_human_review_to_verifier(self.conn, "t1", "accepted", "owner")
        acc = companyctl.compute_verifier_accuracy(self.conn)
        self.assertEqual(1.0, acc["totals"]["accuracy"])
        self.assertEqual(1, acc["totals"]["correct"])

    def test_pass_rejected_is_false_positive(self):
        rec(self.conn, "t1", "numeric", "pass")
        companyctl.link_human_review_to_verifier(self.conn, "t1", "rejected", "owner")
        acc = companyctl.compute_verifier_accuracy(self.conn)
        self.assertEqual(1, acc["totals"]["false_positive"])
        self.assertEqual(0.0, acc["totals"]["accuracy"])

    def test_withhold_rejected_is_correct(self):
        rec(self.conn, "t1", "test", "fail")
        companyctl.link_human_review_to_verifier(self.conn, "t1", "rejected", "owner")
        acc = companyctl.compute_verifier_accuracy(self.conn)
        self.assertEqual(1, acc["totals"]["correct"])

    def test_withhold_accepted_is_false_negative(self):
        rec(self.conn, "t1", "human", "needs_human")
        companyctl.link_human_review_to_verifier(self.conn, "t1", "accepted", "owner")
        acc = companyctl.compute_verifier_accuracy(self.conn)
        self.assertEqual(1, acc["totals"]["false_negative"])

    def test_link_noop_when_no_run_or_bad_status(self):
        # no run for this task -> silently no-op
        companyctl.link_human_review_to_verifier(self.conn, "missing", "accepted", "owner")
        rec(self.conn, "t1", "numeric", "pass")
        companyctl.link_human_review_to_verifier(self.conn, "t1", "draft", "owner")  # invalid status
        row = self.conn.execute("SELECT human_review FROM verifier_runs WHERE task_id='t1'").fetchone()
        self.assertEqual("", row["human_review"])


if __name__ == "__main__":
    unittest.main()
