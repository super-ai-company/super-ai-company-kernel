"""Proxy model failover: when a pool model returns RESOURCE_EXHAUSTED, run_claude must fail over to
the next model with quota instead of failing the task (the pool's models exhaust one at a time)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from company_kernel import claude_adapter as ca


class FakeCP:
    def __init__(self, rc: int, out: str):
        self.returncode, self.stdout, self.stderr = rc, out, ""


class ProxyFailoverTest(unittest.TestCase):
    def _run(self, fake_run):
        with tempfile.TemporaryDirectory() as tmp:
            prompt = Path(tmp) / "p.md"; prompt.write_text("review", encoding="utf-8")
            out = Path(tmp) / "o.md"
            with mock.patch.object(ca, "resolve_claude_proxy",
                                   return_value=({"ANTHROPIC_BASE_URL": "http://pool"}, "model-a", "proxy")), \
                 mock.patch.object(ca.subprocess, "run", side_effect=fake_run):
                rc, summary = ca.run_claude(prompt, out, Path(tmp), "model-a", "bypassPermissions",
                                            agent="no-such-agent")
            return rc, summary, out.read_text(encoding="utf-8")

    def test_fails_over_past_exhausted_model(self):
        calls = []

        def fake_run(cmd, **kw):
            model = cmd[cmd.index("--model") + 1] if "--model" in cmd else None
            calls.append(model)
            if model == "model-a":  # configured model is exhausted
                return FakeCP(1, "API Error: 400 RESOURCE_EXHAUSTED: exhausted on model-a")
            return FakeCP(0, "review complete OK")

        rc, summary, out = self._run(fake_run)
        self.assertEqual(0, rc)                       # eventually succeeded on a fallback
        self.assertEqual("model-a", calls[0])         # tried the configured model first
        self.assertNotEqual("model-a", calls[1])      # failed over to a different model
        self.assertIn("failover", summary)
        self.assertIn("review complete OK", out)

    def test_no_failover_on_normal_failure(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd[cmd.index("--model") + 1] if "--model" in cmd else None)
            return FakeCP(1, "some other error, not quota")  # non-quota failure

        rc, summary, _ = self._run(fake_run)
        self.assertEqual(1, rc)
        self.assertEqual(1, len(calls))               # did NOT try other models
        self.assertNotIn("failover", summary)


if __name__ == "__main__":
    unittest.main()
