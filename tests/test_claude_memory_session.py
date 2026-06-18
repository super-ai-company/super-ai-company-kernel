"""Claude adapter persistent-memory sessions (opt-in).

A task/conversation can carry a stable memory key so Claude reuses ONE session across calls and
remembers prior turns instead of re-scanning each time. No key → the old stateless path is
unchanged. These tests pin the create-once-then-resume behavior and the directive parsing.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class ClaudeMemorySessionTest(unittest.TestCase):
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

    def test_no_key_stays_stateless(self) -> None:
        flags, sid = self.ca.claude_session_flags("claude", "", 0)
        self.assertEqual(["--no-session-persistence"], flags)
        self.assertEqual("", sid)

    def test_first_call_creates_then_resumes(self) -> None:
        flags, sid = self.ca.claude_session_flags("claude", "projX", 0)
        self.assertEqual(["--session-id", sid], flags)
        self.assertEqual(sid, self.ca.memory_session_id("claude", "projX"))  # stable/deterministic
        # within the same call, failover attempts must resume (session already created on attempt 0)
        self.assertEqual(["--resume", sid], self.ca.claude_session_flags("claude", "projX", 1)[0])
        # once marked, later calls resume
        self.ca.mark_memory_session("claude", sid, "projX")
        self.assertEqual(["--resume", sid], self.ca.claude_session_flags("claude", "projX", 0)[0])

    def test_keys_and_agents_get_distinct_sessions(self) -> None:
        a = self.ca.memory_session_id("claude", "projX")
        b = self.ca.memory_session_id("claude", "projY")
        c = self.ca.memory_session_id("gemini", "projX")
        self.assertEqual(3, len({a, b, c}))

    def test_directive_parsing(self) -> None:
        self.assertEqual("damov4-sync", self.ca.parse_memory_key("intro\n记忆会话: damov4-sync\nrest"))
        self.assertEqual("projY", self.ca.parse_memory_key("memory-session: projY"))
        self.assertEqual("projZ", self.ca.parse_memory_key("Memory_Session：projZ"))  # full-width colon, case-insens
        self.assertEqual("", self.ca.parse_memory_key("no directive here"))

    def test_run_claude_builds_session_cmd_and_marks(self) -> None:
        prompt = self.root / "p.md"; prompt.write_text("hi", encoding="utf-8")
        out = self.root / "o.md"
        captured = []

        def fake_run(cmd, **kw):
            captured.append(cmd)
            return mock.Mock(returncode=0, stdout="ok", stderr="")

        with mock.patch.object(self.ca, "resolve_claude_proxy", return_value=({}, "", "native")), \
             mock.patch.object(self.ca, "subprocess") as sp:
            sp.run.side_effect = fake_run
            # first run: creates the session
            self.ca.run_claude(prompt, out, self.root, "", "", "claude", memory_key="conv-1")
            # second run: must resume (no "already in use" error)
            self.ca.run_claude(prompt, out, self.root, "", "", "claude", memory_key="conv-1")

        self.assertIn("--session-id", captured[0])
        self.assertNotIn("--no-session-persistence", captured[0])
        self.assertIn("--resume", captured[1])
        self.assertNotIn("--session-id", captured[1])

    def test_run_claude_without_key_is_stateless(self) -> None:
        prompt = self.root / "p.md"; prompt.write_text("hi", encoding="utf-8")
        out = self.root / "o.md"
        captured = []
        with mock.patch.object(self.ca, "resolve_claude_proxy", return_value=({}, "", "native")), \
             mock.patch.object(self.ca, "subprocess") as sp:
            sp.run.side_effect = lambda cmd, **kw: captured.append(cmd) or mock.Mock(returncode=0, stdout="ok", stderr="")
            self.ca.run_claude(prompt, out, self.root, "", "", "claude")
        self.assertIn("--no-session-persistence", captured[0])
        self.assertNotIn("--session-id", captured[0])
        self.assertNotIn("--resume", captured[0])


    def test_session_in_use_salvages_to_stateless(self) -> None:
        # A stale/locked memory session ("already in use") must NOT fail the task — rerun stateless once.
        prompt = self.root / "p.md"; prompt.write_text("hi", encoding="utf-8")
        out = self.root / "o.md"
        captured = []

        def fake_run(cmd, **kw):
            captured.append(cmd)
            if "--no-session-persistence" in cmd:
                return mock.Mock(returncode=0, stdout="completed", stderr="")
            return mock.Mock(returncode=1, stdout="", stderr="Error: Session ID abc is already in use")

        with mock.patch.object(self.ca, "resolve_claude_proxy", return_value=({}, "", "native")), \
             mock.patch.object(self.ca, "subprocess") as sp:
            sp.run.side_effect = fake_run
            rc, _ = self.ca.run_claude(prompt, out, self.root, "", "", "claude", memory_key="conv-x")

        self.assertEqual(0, rc)                                   # salvaged, not a failed task
        self.assertIn("--session-id", captured[0])               # first tried the (locked) session
        self.assertIn("--no-session-persistence", captured[1])   # then retried stateless and succeeded


if __name__ == "__main__":
    unittest.main()
