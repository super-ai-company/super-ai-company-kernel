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
                 mock.patch.object(ca, "pool_models_with_quota", return_value=None), \
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


class PoolQuotaTest(unittest.TestCase):
    def test_parses_available_models_from_accounts(self):
        accounts = {"accounts": [
            {"enabled": True, "isInvalid": False, "modelRateLimits": {
                "model-x": {"isRateLimited": False}, "model-y": {"isRateLimited": True}}},
            {"enabled": False, "isInvalid": False, "modelRateLimits": {  # disabled → ignored
                "model-z": {"isRateLimited": False}}},
            {"enabled": True, "isInvalid": True, "modelRateLimits": {    # invalid → ignored
                "model-w": {"isRateLimited": False}}},
        ]}

        class Resp:
            def read(self_inner): return __import__("json").dumps(accounts).encode()
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *a): return False
        with mock.patch.object(ca.urllib.request, "urlopen", return_value=Resp()):
            avail = ca.pool_models_with_quota("http://pool", "test")
        self.assertEqual({"model-x"}, avail)   # only the enabled, non-rate-limited model

    def test_fast_fail_when_pool_all_exhausted(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            prompt = Path(tmp) / "p.md"; prompt.write_text("x", encoding="utf-8")
            out = Path(tmp) / "o.md"
            with mock.patch.object(ca, "resolve_claude_proxy",
                                   return_value=({"ANTHROPIC_BASE_URL": "http://pool", "ANTHROPIC_AUTH_TOKEN": "t"}, "model-a", "proxy")), \
                 mock.patch.object(ca, "pool_models_with_quota", return_value=set()), \
                 mock.patch.object(ca.subprocess, "run", side_effect=lambda *a, **k: calls.append(1)):
                rc, summary = ca.run_claude(prompt, out, Path(tmp), "model-a", "bypassPermissions", agent="no-such")
            written = out.read_text(encoding="utf-8")   # read before the temp dir is cleaned up
        self.assertEqual(ca.POOL_QUOTA_EXHAUSTED_RC, rc)   # fast-failed
        self.assertEqual(0, len(calls))                    # never even ran claude (no waiting)
        self.assertIn("ALL_QUOTA_EXHAUSTED", written)

    def test_picks_available_model_skipping_exhausted(self):
        used = []

        def fake_run(cmd, **kw):
            used.append(cmd[cmd.index("--model") + 1])
            return FakeCP(0, "ok")
        with tempfile.TemporaryDirectory() as tmp:
            prompt = Path(tmp) / "p.md"; prompt.write_text("x", encoding="utf-8")
            out = Path(tmp) / "o.md"
            with mock.patch.object(ca, "resolve_claude_proxy",
                                   return_value=({"ANTHROPIC_BASE_URL": "http://pool", "ANTHROPIC_AUTH_TOKEN": "t"}, "model-a", "proxy")), \
                 mock.patch.object(ca, "pool_models_with_quota", return_value={"gemini-3-flash-agent"}), \
                 mock.patch.object(ca.subprocess, "run", side_effect=fake_run):
                rc, _ = ca.run_claude(prompt, out, Path(tmp), "model-a", "bypassPermissions", agent="no-such")
        self.assertEqual(0, rc)
        self.assertEqual(["gemini-3-flash-agent"], used)   # skipped exhausted model-a, used the available one


if __name__ == "__main__":
    unittest.main()
