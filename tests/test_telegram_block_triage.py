"""Telegram one-tap handling of blocked tasks: the ck_discard callback (new) discards, and a
blocked task pushes the triage (reason + action) to the owner's Telegram with retry/discard buttons.
"""
from __future__ import annotations

import unittest
from unittest import mock

from company_kernel import telegram_approval_poll as poll


class TelegramBlockCallbackTest(unittest.TestCase):
    def test_ck_discard_runs_task_discard(self) -> None:
        with mock.patch.object(poll, "run_companyctl", return_value=(0, {"ok": True})) as rc:
            ok, label = poll.handle_callback("ck_discard:task-123")
        self.assertTrue(ok)
        self.assertIn("丢弃", label)
        argv = rc.call_args.args[0]
        self.assertEqual(["task", "discard"], argv[:2])
        self.assertIn("task-123", argv)

    def test_ck_fix_reopens(self) -> None:
        with mock.patch.object(poll, "run_companyctl", return_value=(0, {"ok": True})) as rc:
            ok, _ = poll.handle_callback("ck_fix:task-9")
        self.assertTrue(ok)
        self.assertEqual(["task", "reopen"], rc.call_args.args[0][:2])

    def test_unknown_callback_rejected(self) -> None:
        ok, _ = poll.handle_callback("ck_bogus:x")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
