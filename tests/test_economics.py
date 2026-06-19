from __future__ import annotations

import unittest

from company_kernel import companyctl


class EconomicsTest(unittest.TestCase):
    def test_classify_task_type(self):
        pricing = {"task_type_keywords": {"code_fix": ["fix", "修复"], "data_report": ["report", "报表"]}}
        self.assertEqual("code_fix", companyctl.classify_task_type("fix the bug", "", pricing))
        self.assertEqual("code_fix", companyctl.classify_task_type("修复登录", "", pricing))
        self.assertEqual("data_report", companyctl.classify_task_type("生成报表", "", pricing))
        self.assertEqual("default", companyctl.classify_task_type("随便", "无关", pricing))

    def test_estimate_cost_prefers_amount_then_tokens_then_runtime(self):
        rates = {"token_input_per_1k": 0.003, "token_output_per_1k": 0.015, "runtime_per_minute": 0.06}
        self.assertEqual(2.5, companyctl.estimate_task_cost({"amount": 2.5}, rates))
        # tokens: 1000 in *0.003 + 1000 out *0.015 = 0.018
        self.assertAlmostEqual(0.018, companyctl.estimate_task_cost({"token_input": 1000, "token_output": 1000}, rates))
        # runtime: 120s = 2min * 0.06 = 0.12
        self.assertAlmostEqual(0.12, companyctl.estimate_task_cost({"runtime_seconds": 120}, rates))

    def test_compute_economics_margin(self):
        import uuid
        tid = f"eco-t-{uuid.uuid4().hex[:8]}"
        beid = f"be-eco-{uuid.uuid4().hex[:8]}"
        conn = companyctl.connect()
        try:
            conn.execute("INSERT OR REPLACE INTO employees(id,name,role,runtime,workspace,status,created_at,updated_at) VALUES('codex','Codex','dev','codex','/tmp','active','t','t')")
            conn.execute("INSERT OR REPLACE INTO tasks(id,source_agent,target_agent,title,description,priority,status,created_at,updated_at) VALUES(?,'owner','codex','fix bug','修复测试','P2','completed','t','t')", (tid,))
            conn.execute("INSERT INTO budget_events(budget_event_id,task_id,employee_id,cost_type,amount,currency,created_at) VALUES(?,?,'codex','codex_runtime',1.0,'USD','t')", (beid, tid))
            conn.commit()
            eco = companyctl.compute_economics(conn)
            # cleanup so re-runs and the live DB stay clean
            conn.execute("DELETE FROM budget_events WHERE budget_event_id = ?", (beid,))
            conn.execute("DELETE FROM tasks WHERE id = ?", (tid,))
            conn.commit()
        finally:
            conn.close()
        cf = next((b for b in eco["by_task_type"] if b["task_type"] == "code_fix"), None)
        self.assertIsNotNone(cf, eco)
        self.assertGreaterEqual(cf["count"], 1)
        self.assertGreater(cf["revenue"], 0)
        self.assertIn("margin", cf)


    def test_compute_cost_dashboard_separates_free_onduty_from_spenders(self):
        import uuid
        from datetime import datetime, timedelta
        sfx = uuid.uuid4().hex[:8]
        spender, idler = f"cost-spend-{sfx}", f"cost-idle-{sfx}"
        beid = f"be-cost-{sfx}"
        fresh = (datetime.fromisoformat(companyctl.now())).isoformat()
        conn = companyctl.connect()
        try:
            for eid in (spender, idler):
                conn.execute("INSERT OR REPLACE INTO employees(id,name,role,runtime,workspace,status,created_at,updated_at) VALUES(?,?,'dev','codex','/tmp','active','t','t')", (eid, eid))
            # both on duty (fresh heartbeat); only `spender` has a billed execution
            for eid in (spender, idler):
                conn.execute("INSERT OR REPLACE INTO heartbeats(agent_id,last_seen_at) VALUES(?,?)", (eid, fresh))
            conn.execute("INSERT INTO budget_events(budget_event_id,employee_id,cost_type,amount,currency,created_at) VALUES(?,?,'codex_runtime',3.0,'USD',?)", (beid, spender, fresh))
            conn.commit()
            d = companyctl.compute_cost_dashboard(conn, days=7)
            conn.execute("DELETE FROM budget_events WHERE budget_event_id = ?", (beid,))
            for eid in (spender, idler):
                conn.execute("DELETE FROM heartbeats WHERE agent_id = ?", (eid,))
                conn.execute("DELETE FROM employees WHERE id = ?", (eid,))
            conn.commit()
        finally:
            conn.close()
        by_emp = {e["employee_id"]: e for e in d["by_employee"]}
        self.assertIn(spender, by_emp)
        self.assertIn(idler, by_emp)
        # spender: on duty, billed
        self.assertTrue(by_emp[spender]["on_duty"])
        self.assertEqual(3.0, by_emp[spender]["cost"])
        self.assertEqual(1, by_emp[spender]["executions"])
        # idler: on duty but free (0 cost) — the core selling point
        self.assertTrue(by_emp[idler]["on_duty"])
        self.assertEqual(0, by_emp[idler]["cost"])
        self.assertGreaterEqual(d["totals"]["on_duty_free"], 1)
        # human owner is never billed/listed as a worker
        self.assertNotIn("owner", by_emp)

    def test_cost_dashboard_excludes_human_owner(self):
        conn = companyctl.connect_readonly()
        try:
            d = companyctl.compute_cost_dashboard(conn, days=1)
        finally:
            conn.close()
        for e in d["by_employee"]:
            self.assertFalse(companyctl.is_human_owner_employee({"id": e["employee_id"]}))


if __name__ == "__main__":
    unittest.main()
