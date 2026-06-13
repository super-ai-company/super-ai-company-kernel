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
