"""The blocked-task Telegram alert is deduped so a task that blocks → retries → blocks again
doesn't flood the owner. Same task+category within the cooldown is suppressed; a changed category
(or a different task) alerts again."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from company_kernel import companyctl


class BlockNotifyDedupTest(unittest.TestCase):
    def test_dedups_same_task_and_category(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # patch ROOT so the dedup state file lands in the temp dir; auto-restored after the block
            with mock.patch.object(companyctl, "ROOT", Path(tmp)):
                self.assertTrue(companyctl._should_notify_block("t1", "credential"))   # first → alert
                self.assertFalse(companyctl._should_notify_block("t1", "credential"))  # repeat → suppressed
                self.assertTrue(companyctl._should_notify_block("t1", "timeout"))      # category changed → alert
                self.assertTrue(companyctl._should_notify_block("t2", "credential"))   # different task → alert


if __name__ == "__main__":
    unittest.main()
