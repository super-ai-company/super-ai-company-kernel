"""Company Kernel MCP server — verify the JSON-RPC/MCP handshake, tool listing, and tool dispatch
so the Codex/Claude apps can connect and auto-discover the kernel tools."""
from __future__ import annotations

import unittest
from unittest import mock

from company_kernel import mcp_server as mcp


class McpServerTest(unittest.TestCase):
    def test_initialize_handshake(self):
        r = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual("company-kernel", r["result"]["serverInfo"]["name"])
        self.assertIn("tools", r["result"]["capabilities"])

    def test_notification_gets_no_response(self):
        self.assertIsNone(mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_tools_list_exposes_all_tools(self):
        r = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in r["result"]["tools"]}
        self.assertEqual(
            {"list_my_tasks", "show_task", "claim_task", "report_done", "report_blocked",
             "dispatch_task", "check_completions", "start_meeting", "meeting_result"}, names)
        for t in r["result"]["tools"]:  # every tool must self-describe (so apps know how to call it)
            self.assertTrue(t["description"])
            self.assertEqual("object", t["inputSchema"]["type"])

    def test_tools_call_dispatches_to_companyctl(self):
        with mock.patch.object(mcp, "_ctl", return_value={"ok": True, "task": {"id": "t1"}}) as ctl:
            r = mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                            "params": {"name": "dispatch_task",
                                       "arguments": {"from_agent": "codex", "to_agent": "antigravity", "title": "x"}}})
        ctl.assert_called_once()
        self.assertIn("task submit", " ".join(ctl.call_args.args[0]))
        self.assertIn("t1", r["result"]["content"][0]["text"])

    def test_unknown_tool_errors(self):
        r = mcp.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope"}})
        self.assertIn("error", r)

    def test_unknown_method_errors_for_request(self):
        r = mcp.handle({"jsonrpc": "2.0", "id": 5, "method": "bogus/method"})
        self.assertEqual(-32601, r["error"]["code"])


if __name__ == "__main__":
    unittest.main()
