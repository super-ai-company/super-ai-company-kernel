"""Codex adapter persistent-memory sessions (opt-in).

codex can't name a session on creation, so memory mode runs WITHOUT --ephemeral (persisting the
rollout file), captures the new session UUID via a snapshot-diff, and resumes it next time. No
memory_key → unchanged ephemeral behavior. Ambiguous capture → stay stateless (never wrong-resume).
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class CodexMemorySessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["OPENCLAW_COMPANY_KERNEL_ROOT"] = str(self.root)
        import importlib
        from company_kernel import codex_adapter
        self.ca = importlib.reload(codex_adapter)

    def tearDown(self) -> None:
        os.environ.pop("OPENCLAW_COMPANY_KERNEL_ROOT", None)
        self.tmp.cleanup()

    def test_default_command_is_ephemeral(self) -> None:
        cmd = self.ca.build_codex_command(self.root, self.root / "o.txt", "read-only", "")
        self.assertIn("--ephemeral", cmd)

    def test_memory_first_run_persists_not_ephemeral(self) -> None:
        cmd = self.ca.build_codex_command(self.root, self.root / "o.txt", "read-only", "", persist=True)
        self.assertNotIn("--ephemeral", cmd)

    def test_resume_command_shape(self) -> None:
        cmd = self.ca.build_codex_resume_command(self.root / "o.txt", "", "uuid-123")
        self.assertEqual(["exec", "resume"], cmd[1:3])
        self.assertIn("uuid-123", cmd)
        self.assertNotIn("-s", cmd)  # resume inherits the session's sandbox

    def test_uuid_extracted_from_rollout_name(self) -> None:
        u = self.ca._uuid_from_rollout("/x/rollout-2026-06-17T01-02-03-019ed205-6a53-7f33-9363-458218b11541.jsonl")
        self.assertEqual("019ed205-6a53-7f33-9363-458218b11541", u)

    def test_store_then_resume_roundtrip(self) -> None:
        self.assertEqual("", self.ca.codex_memory_session("codex", "projX"))
        self.ca.store_codex_memory_session("codex", "projX", "sid-abc")
        self.assertEqual("sid-abc", self.ca.codex_memory_session("codex", "projX"))
        self.assertEqual("", self.ca.codex_memory_session("codex", "other"))

    def test_run_codex_memory_captures_new_session_then_resumes(self) -> None:
        sess = self.root / "sessions" / "2026" / "06" / "17"
        sess.mkdir(parents=True)
        os.environ["CODEX_HOME"] = str(self.root)
        try:
            card = self.root / "card.md"; card.write_text("hi", encoding="utf-8")
            out = self.root / "out.md"; ev = self.root / "ev.jsonl"
            captured = []

            def fake_run(cmd, **kw):
                captured.append(cmd)
                # first call (create): simulate codex writing a new rollout file
                if not any("resume" in c for c in cmd):
                    (sess / "rollout-2026-06-17T01-02-03-019ed205-6a53-7f33-9363-458218b11541.jsonl").write_text("{}", encoding="utf-8")
                return mock.Mock(returncode=0)

            with mock.patch.object(self.ca, "wrap_command", side_effect=lambda base, **kw: base), \
                 mock.patch.object(self.ca.subprocess, "run", side_effect=fake_run):
                self.ca.run_codex(card, self.root, out, ev, "read-only", "", "", "", memory_key="conv-1", agent="codex")
                self.ca.run_codex(card, self.root, out, ev, "read-only", "", "", "", memory_key="conv-1", agent="codex")

            self.assertNotIn("resume", " ".join(captured[0]))   # first run creates (persisted, not ephemeral)
            self.assertNotIn("--ephemeral", captured[0])
            self.assertIn("resume", " ".join(captured[1]))       # second run resumes the captured session
            self.assertIn("019ed205-6a53-7f33-9363-458218b11541", captured[1])
        finally:
            os.environ.pop("CODEX_HOME", None)

    def test_run_codex_without_key_is_ephemeral_and_unchanged(self) -> None:
        card = self.root / "card.md"; card.write_text("hi", encoding="utf-8")
        out = self.root / "out.md"; ev = self.root / "ev.jsonl"
        captured = []
        with mock.patch.object(self.ca, "wrap_command", side_effect=lambda base, **kw: base), \
             mock.patch.object(self.ca.subprocess, "run", side_effect=lambda cmd, **kw: captured.append(cmd) or mock.Mock(returncode=0)):
            self.ca.run_codex(card, self.root, out, ev, "read-only", "", "", "")
        self.assertIn("--ephemeral", captured[0])
        self.assertNotIn("resume", " ".join(captured[0]))


if __name__ == "__main__":
    unittest.main()
