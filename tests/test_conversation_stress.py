"""Stress the dialogue layer: a long multi-round conversation must persist every message
in order without loss or error. This is the part of "持续对话不出问题" the kernel guarantees
regardless of which agent CLIs are installed.
"""
from __future__ import annotations

import json
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
        # close any connections the tests open via ctl.connect() before the module reload (LIFO: this
        # runs before _restore_modules) so they don't leak as ResourceWarnings at GC.
        self.addCleanup(self._close_open_connections)

    def _close_open_connections(self):
        for conn in list(getattr(self.ctl, "_OPEN_CONNECTIONS", [])):
            try:
                conn.close()
            except Exception:
                pass

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


    def test_meeting_gate_admits_only_capable_employees(self):
        """Only employees that genuinely reply may join a meeting; the rest are excluded
        with a reason and never pollute the thread."""
        for a, rt in (("codex", "codex"), ("nestcar", "openclaw"), ("hermes", "hermes")):
            self._run(["employee", "create", "--id", a, "--name", a, "--role", "developer",
                       "--runtime", rt, "--workspace", str(self.root / a)])
        conn0 = self.ctl.connect()
        conn0.execute("UPDATE employees SET status = 'active' WHERE id IN ('codex','nestcar','hermes')")
        conn0.commit()
        self.gw.route_post("/v1/conversations", {
            "from": "owner-shift", "participants": "owner-shift,codex,nestcar,hermes",
            "conversation_id": "conv-gate", "title": "规范同步", "body": "议程：新规范",
        })

        def fake_invoke(conn, agent, prompt, timeout, memory_key=""):
            if agent == "nestcar":  # this one can't actually participate
                return {"ok": False, "reply": "", "error": "runtime down", "exit_code": 1}
            return {"ok": True, "reply": f"{agent} 的发言", "runtime": "x", "exit_code": 0}

        with mock.patch.object(self.ctl, "conversation_invoke_runtime", side_effect=fake_invoke):
            conn = self.ctl.connect()
            result = self.ctl.conversation_run_internal(
                conn, conversation_id="conv-gate", mode="meeting", rounds=1, synthesizer="hermes")

        self.assertIn("codex", result["speakers"])
        self.assertIn("hermes", result["speakers"])
        self.assertNotIn("nestcar", result["speakers"])
        self.assertTrue(any(s["agent"] == "nestcar" and "未通过参会探测" in s["reason"]
                            for s in result["skipped"]), result["skipped"])
        self.assertTrue(result["final_plan"], "capable chair must still produce minutes")
        # the excluded employee never spoke in the thread
        _, shown = self.gw.route_get("/v1/conversations/conv-gate", {})
        self.assertFalse(any(m["source_agent"] == "nestcar" for m in shown["messages"]))


    def test_meeting_reads_project_memory_and_stores_its_conclusion(self):
        """Memory ↔ meeting closed loop: a project-tied meeting injects the shared digest into each
        speaker's prompt (memory → meeting) and writes its conclusion back into the bank (meeting →
        memory). A meeting that doesn't feed memory is pointless."""
        from company_kernel import project_memory as pm
        for a, rt in (("codex", "codex"), ("hermes", "hermes")):
            self._run(["employee", "create", "--id", a, "--name", a, "--role", "developer",
                       "--runtime", rt, "--workspace", str(self.root / a)])
        conn = self.ctl.connect()
        conn.execute("UPDATE employees SET status='active' WHERE id IN ('codex','hermes')")
        conn.commit()
        pm.create_project(conn, project_id="proj", name="Proj", workspace=str(self.root / "repo"), lead_agent="codex")
        pm.remember(conn, project_id="proj", title="已定:支付走 PromptPay EMV", entry_type="decision")
        pm.curate(conn, project_id="proj")
        self.gw.route_post("/v1/conversations", {
            "from": "owner-shift", "participants": "owner-shift,codex,hermes",
            "conversation_id": "conv-mem", "title": "支付评审", "body": "议程",
        })
        conn2 = self.ctl.connect()
        conn2.execute("UPDATE conversations SET project_id='proj' WHERE id='conv-mem'"); conn2.commit()

        seen_prompts = []

        def fake_invoke(conn, agent, prompt, timeout, memory_key=""):
            seen_prompts.append(prompt)
            return {"ok": True, "reply": "结论:沿用 PromptPay EMV,storeId 注入测试站。", "runtime": "x", "exit_code": 0}

        with mock.patch.object(self.ctl, "conversation_invoke_runtime", side_effect=fake_invoke):
            result = self.ctl.conversation_run_internal(self.ctl.connect(), conversation_id="conv-mem",
                                                        mode="discuss", rounds=1, synthesizer="hermes")
        # memory → meeting: the shared digest reached the speakers' prompts
        self.assertTrue(any("已定:支付走 PromptPay EMV" in p for p in seen_prompts), "digest must be injected into prompts")
        # meeting → memory: the conclusion is now a decision entry in the bank
        self.assertTrue(result.get("captured_memory"), result)
        entries = pm.recall(self.ctl.connect(), project_id="proj")
        self.assertTrue(any("沿用 PromptPay EMV" in e["body"] for e in entries), "conclusion must be stored")

    def test_human_rbac_roles_and_action_gating(self) -> None:
        gw = self.gw
        # no users.json + no env token → open self-host (owner)
        self.assertEqual(("anonymous", "owner"), gw.resolve_actor({}))

        # enable RBAC by writing config/users.json under the (temp) root
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        (self.root / "config" / "users.json").write_text(json.dumps({"tokens": {
            "tok-view": {"user": "vic", "role": "viewer"},
            "tok-op": {"user": "olive", "role": "operator"},
            "tok-admin": {"user": "ada", "role": "admin"},
        }}), encoding="utf-8")

        def actor(tok):
            return gw.resolve_actor({"Authorization": f"Bearer {tok}"} if tok else {})
        self.assertEqual(("vic", "viewer"), actor("tok-view"))
        self.assertEqual(("olive", "operator"), actor("tok-op"))
        self.assertEqual((None, ""), actor("nope"))      # unknown token → unauthorized
        self.assertEqual((None, ""), actor(""))           # missing token (RBAC on) → unauthorized

        # required role per action
        self.assertEqual("viewer", gw.required_role("GET", "/v1/tasks"))
        self.assertEqual("operator", gw.required_role("POST", "/v1/tasks"))          # dispatch
        self.assertEqual("operator", gw.required_role("POST", "/v1/approvals/a1/approve"))  # approve
        self.assertEqual("operator", gw.required_role("POST", "/v1/employees/x/communication"))  # pause
        self.assertEqual("admin", gw.required_role("POST", "/v1/employees"))         # config
        self.assertEqual("owner", gw.required_role("POST", "/v1/users"))             # user mgmt

        # rank: viewer < operator < admin < owner
        rank = gw.ROLE_RANK
        self.assertLess(rank["viewer"], rank["operator"])
        self.assertLess(rank["operator"], rank["admin"])
        self.assertLess(rank["admin"], rank["owner"])
        # operator may dispatch (operator>=operator) but not config (operator<admin)
        self.assertGreaterEqual(rank["operator"], rank[gw.required_role("POST", "/v1/tasks")])
        self.assertLess(rank["operator"], rank[gw.required_role("POST", "/v1/employees")])


if __name__ == "__main__":
    unittest.main()
