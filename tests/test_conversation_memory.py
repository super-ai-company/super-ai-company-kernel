"""B: conversations are memory-native for claude/gemini.

conversation_invoke_runtime must pass --memory-session (keyed by the conversation) to the claude
adapter so each participant natively remembers prior turns, but must NOT pass it to codex (whose
adapter doesn't support it yet — it would error on an unknown flag).
"""
from __future__ import annotations

import unittest
from unittest import mock

from company_kernel import companyctl


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, row):
        self._row = row

    def execute(self, *_a, **_k):
        return _FakeCursor(self._row)


class ConversationMemoryTest(unittest.TestCase):
    def _capture_cmd(self, runtime):
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return mock.Mock(returncode=0, stdout="hi", stderr="")

        conn = _FakeConn({"id": "claude" if runtime != "codex" else "codex", "runtime": runtime})
        with mock.patch.object(companyctl.subprocess, "run", side_effect=fake_run):
            companyctl.conversation_invoke_runtime(conn, "claude" if runtime != "codex" else "codex",
                                                   "say hi", 30, memory_key="meeting-7")
        return captured["cmd"]

    def test_claude_conversation_gets_memory_session(self) -> None:
        cmd = self._capture_cmd("claude")
        self.assertIn("--memory-session", cmd)
        self.assertIn("conv:meeting-7:claude", cmd)

    def test_gemini_conversation_gets_memory_session(self) -> None:
        cmd = self._capture_cmd("gemini")
        self.assertIn("--memory-session", cmd)

    def test_codex_conversation_has_no_memory_flag(self) -> None:
        cmd = self._capture_cmd("codex")
        self.assertNotIn("--memory-session", cmd)

    def test_no_memory_key_means_no_flag(self) -> None:
        captured = {}
        conn = _FakeConn({"id": "claude", "runtime": "claude"})
        with mock.patch.object(companyctl.subprocess, "run",
                               side_effect=lambda cmd, **kw: captured.__setitem__("cmd", cmd) or mock.Mock(returncode=0, stdout="hi", stderr="")):
            companyctl.conversation_invoke_runtime(conn, "claude", "hi", 30)  # no memory_key
        self.assertNotIn("--memory-session", captured["cmd"])


if __name__ == "__main__":
    unittest.main()
