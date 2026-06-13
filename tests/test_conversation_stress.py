"""Stress the dialogue layer: a long multi-round conversation must persist every message
in order without loss or error. This is the part of "持续对话不出问题" the kernel guarantees
regardless of which agent CLIs are installed.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class ConversationStressTest(unittest.TestCase):
    def setUp(self):
        # Registered FIRST so it runs LAST (cleanups are LIFO): after the env patch is
        # removed, reload the kernel modules with the real env so module-level DB_PATH/ROOT
        # globals revert — otherwise this test's temp paths leak into other test files.
        self.addCleanup(self._restore_modules)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {
            "OPENCLAW_COMPANY_KERNEL_ROOT": str(self.root),
            "COMPANY_KERNEL_DB_PATH": str(self.root / "company.sqlite"),
        }, clear=False)
        patcher.start(); self.addCleanup(patcher.stop)
        # copy schema next to the temp db root so connect() can bootstrap
        src = Path(__file__).resolve().parents[1] / "company_kernel" / "schema.sql"
        (self.root / "company_kernel").mkdir(parents=True, exist_ok=True)
        (self.root / "company_kernel" / "schema.sql").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        import importlib
        from company_kernel import companyctl, api_gateway
        importlib.reload(companyctl); importlib.reload(api_gateway)
        self.ctl = companyctl
        self.gw = api_gateway

    def _restore_modules(self):
        import importlib
        from company_kernel import companyctl, api_gateway
        importlib.reload(companyctl); importlib.reload(api_gateway)

    def _run(self, argv):
        import contextlib, io, json
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.ctl.main(argv)
        raw = buf.getvalue().strip()
        return json.loads(raw) if raw else {}

    def test_30_round_conversation_persists_in_order(self):
        for a in ("codex", "claude", "hermes"):
            self._run(["employee", "create", "--id", a, "--name", a, "--role", "developer",
                       "--runtime", a if a != "claude" else "claude", "--workspace", str(self.root / a)])
        status, started = self.gw.route_post("/v1/conversations", {
            "from": "owner-shift", "participants": "owner-shift,codex,claude,hermes",
            "conversation_id": "conv-stress", "title": "长对话压测", "body": "round-0",
        })
        self.assertEqual(201, status, started)

        speakers = ["codex", "claude", "hermes", "owner-shift"]
        expected = ["round-0"]
        for i in range(1, 31):
            who = speakers[i % len(speakers)]
            body = f"round-{i}"
            status, _ = self.gw.route_post("/v1/conversations/conv-stress/reply",
                                           {"from": who, "body": body, "message_id": f"m{i}"})
            self.assertEqual(201, status, f"round {i} failed")
            expected.append(body)

        status, shown = self.gw.route_get("/v1/conversations/conv-stress", {})
        self.assertEqual(200, status)
        bodies = [m["body"] for m in shown["messages"]]
        self.assertEqual(expected, bodies, "every round must persist in order, none lost")
        self.assertEqual(31, len(bodies))


if __name__ == "__main__":
    unittest.main()
