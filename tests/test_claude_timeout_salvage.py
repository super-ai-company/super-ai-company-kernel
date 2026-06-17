"""Auto-judge a runtime timeout for the claude/gemini/agy-via-proxy adapter: if a full result was
produced before the wrap-down hung, accept it (it executed); if there's no real output, it's a dead
hang → block. This is what stops a task whose work is actually DONE from being red-alarmed as failed.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path


class ClaudeTimeoutSalvageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["OPENCLAW_COMPANY_KERNEL_ROOT"] = str(self.root)
        import importlib
        from company_kernel import claude_adapter
        self.ca = importlib.reload(claude_adapter)

    def tearDown(self) -> None:
        os.environ.pop("OPENCLAW_COMPANY_KERNEL_ROOT", None)
        self.tmp.cleanup()

    def _out(self, text: str) -> Path:
        p = self.root / "o.md"
        p.write_text(text, encoding="utf-8")
        return p

    def test_substantive_output_is_recognised_as_executed(self) -> None:
        full = "已完成顾客自助端统一导航方案并修复全部 HTML 页面。" * 10 + "\n所有修改均已镜像同步至 Android app assets。"
        self.assertTrue(self.ca.output_is_substantive(self._out(full)))

    def test_empty_or_timeout_note_only_is_a_real_hang(self) -> None:
        self.assertFalse(self.ca.output_is_substantive(self._out("")))
        self.assertFalse(self.ca.output_is_substantive(self._out("\n\n## stderr\n\nclaude -p killed after exceeding 1800s timeout (was hanging)")))
        self.assertFalse(self.ca.output_is_substantive(self._out("error: something")))

    def test_timeout_rc_constant(self) -> None:
        self.assertEqual(124, self.ca.RUNTIME_TIMEOUT_RC)


if __name__ == "__main__":
    unittest.main()
