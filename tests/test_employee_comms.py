"""Every employee must be TAUGHT how to communicate — the comms protocol is injected into each
adapter's runtime prompt (previously employees ran without it; rules.md was written but never read)."""
from __future__ import annotations

import unittest

from company_kernel import employee_comms as ec


class EmployeeCommsTest(unittest.TestCase):
    def test_protocol_carries_core_instructions(self):
        p = ec.communication_protocol("nonexistent-agent-xyz", "codex")
        self.assertIn("通讯协议", p)
        self.assertIn("companyctl message send --from nonexistent-agent-xyz", p)
        self.assertIn("owner", p)        # how to escalate
        self.assertIn("evidence_path", p)      # how to report done

    def test_runtime_note_is_role_specific(self):
        self.assertIn("只审查", ec.communication_protocol("a", "antigravity"))
        self.assertIn("relay", ec.communication_protocol("a", "openclaw"))
        self.assertIn("主持会议", ec.communication_protocol("a", "hermes"))

    def test_injected_into_adapter_prompts(self):
        from company_kernel import antigravity_adapter, claude_adapter, hermes_adapter
        task = {"id": "t", "title": "x", "description": "d", "target_agent": "claude",
                "source_agent": "o", "priority": "P2"}
        self.assertIn("通讯协议", claude_adapter.build_prompt(task))
        self.assertIn("通讯协议", hermes_adapter.build_prompt({**task, "target_agent": "hermes"}))
        self.assertIn("通讯协议", antigravity_adapter.build_managed_task_prompt(
            {"id": "t", "title": "x", "description": "审 S04", "target_agent": "antigravity"}))


if __name__ == "__main__":
    unittest.main()
