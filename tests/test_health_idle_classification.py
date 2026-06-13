from __future__ import annotations

import unittest
from unittest import mock

from company_kernel import companyctl


class EnabledWorkerAgentsTest(unittest.TestCase):
    def test_reads_enabled_workers_and_heartbeat_agents(self):
        cfg = {
            "heartbeat_agents": ["openclaw-main"],
            "adapter_workers": [
                {"agent": "codex", "enabled": True},
                {"agent": "hermes", "enabled": False},
                {"agent": "nestcar", "enabled": True},
            ],
        }
        import json
        m = mock.mock_open(read_data=json.dumps(cfg))
        with mock.patch("pathlib.Path.exists", lambda self: True), \
                mock.patch("pathlib.Path.read_text", lambda self, encoding=None: json.dumps(cfg)):
            agents = companyctl.enabled_worker_agents()
        self.assertIn("codex", agents)
        self.assertIn("nestcar", agents)
        self.assertIn("openclaw-main", agents)
        self.assertNotIn("hermes", agents, "disabled worker must not be expected-alive")

    def test_missing_config_returns_empty(self):
        with mock.patch("pathlib.Path.exists", lambda self: False):
            self.assertEqual(set(), companyctl.enabled_worker_agents())


if __name__ == "__main__":
    unittest.main()


class SetUnavailableCommandTest(unittest.TestCase):
    """employee set-unavailable demotes active->candidate with a stored reason."""

    def test_reconcile_reason_classification(self):
        from company_kernel import reconcile_status as rs
        # not-logged-in reply
        r1 = [{"response": {"reply": "Not logged in · Please run /login"}}]
        self.assertIn("登录", rs.verify_reason(r1))
        # empty reply
        r2 = [{"response": {"reply": ""}}]
        self.assertIn("无响应", rs.verify_reason(r2))
        # error
        r3 = [{"response": {"error": "boom"}}]
        self.assertIn("boom", rs.verify_reason(r3))
