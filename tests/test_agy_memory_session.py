"""Antigravity (agy) persistent-memory sessions (opt-in).

agy persists each conversation as a <UUID>.db file and `--print` resumes one via `--conversation
<UUID>`. agy can't name a conversation on creation, so memory mode snapshots the conversations dir
around the run, captures the one new UUID, and resumes it next time. Ambiguous capture → stateless.
No memory_key → unchanged behavior.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class AgyMemorySessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["OPENCLAW_COMPANY_KERNEL_ROOT"] = str(self.root)
        self.home = self.root / "gemini"
        (self.home / "antigravity-cli" / "conversations").mkdir(parents=True)
        os.environ["ANTIGRAVITY_HOME"] = str(self.home)
        import importlib
        from company_kernel import antigravity_adapter
        self.aa = importlib.reload(antigravity_adapter)

    def tearDown(self) -> None:
        for k in ("OPENCLAW_COMPANY_KERNEL_ROOT", "ANTIGRAVITY_HOME"):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def test_conversation_id_extracted_from_db_name(self) -> None:
        self.assertEqual(
            "cfae915e-f215-4f77-9fb6-5078e25dfec5",
            self.aa._agy_conversation_id("/x/cfae915e-f215-4f77-9fb6-5078e25dfec5.db"),
        )
        self.assertEqual("", self.aa._agy_conversation_id("/x/not-a-uuid.db"))

    def test_store_then_resume_roundtrip(self) -> None:
        self.assertEqual("", self.aa.agy_memory_session("antigravity", "projX"))
        self.aa.store_agy_memory_session("antigravity", "projX", "cid-1")
        self.assertEqual("cid-1", self.aa.agy_memory_session("antigravity", "projX"))
        self.assertEqual("", self.aa.agy_memory_session("antigravity", "other"))

    def test_memory_captures_then_resumes(self) -> None:
        conv_dir = self.home / "antigravity-cli" / "conversations"
        captured = []

        def fake_run(cmd, **kw):
            captured.append(cmd)
            if "--conversation" not in cmd:  # first run (create): simulate agy writing a new conv db
                (conv_dir / "cfae915e-f215-4f77-9fb6-5078e25dfec5.db").write_text("x", encoding="utf-8")
            return mock.Mock(returncode=0, stdout="ok", stderr="")

        with mock.patch.object(self.aa.shutil, "which", return_value="/usr/bin/agy"), \
             mock.patch.object(self.aa.subprocess, "run", side_effect=fake_run):
            self.aa.run_agy_print("hi", 30, memory_key="conv-1", agent="antigravity")
            self.aa.run_agy_print("again", 30, memory_key="conv-1", agent="antigravity")

        self.assertNotIn("--conversation", captured[0])  # first creates
        self.assertIn("--conversation", captured[1])      # second resumes the captured conv
        self.assertIn("cfae915e-f215-4f77-9fb6-5078e25dfec5", captured[1])

    def test_no_memory_key_no_conversation_flag(self) -> None:
        captured = []
        with mock.patch.object(self.aa.shutil, "which", return_value="/usr/bin/agy"), \
             mock.patch.object(self.aa.subprocess, "run",
                               side_effect=lambda cmd, **kw: captured.append(cmd) or mock.Mock(returncode=0, stdout="ok", stderr="")):
            self.aa.run_agy_print("hi", 30)
        self.assertNotIn("--conversation", captured[0])


if __name__ == "__main__":
    unittest.main()
