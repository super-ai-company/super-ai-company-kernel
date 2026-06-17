"""Project Memory Bank — projects scope a shared, curated memory; capture is automatic on task
outcomes; the lead's curate pass dedups and rebuilds the digest; consumption reads the digest by
workspace.
"""
from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from company_kernel import project_memory as pm

SCHEMA = Path(__file__).resolve().parents[1] / "company_kernel" / "schema.sql"


class ProjectMemoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        pm.create_project(self.conn, project_id="damov4", name="Damov4 POS",
                          workspace="/Users/x/damov4", lead_agent="codex")

    def test_create_and_resolve_by_workspace(self) -> None:
        p = pm.get_project(self.conn, "damov4")
        self.assertEqual("codex", p["lead_agent"])
        # a task in a subdir maps to the project (longest-prefix)
        self.assertEqual("damov4", pm.resolve_project_for_workspace(self.conn, "/Users/x/damov4/android-pos")["id"])
        self.assertIsNone(pm.resolve_project_for_workspace(self.conn, "/Users/x/other"))

    def test_longest_prefix_wins(self) -> None:
        pm.create_project(self.conn, project_id="android", workspace="/Users/x/damov4/android-pos")
        self.assertEqual("android", pm.resolve_project_for_workspace(self.conn, "/Users/x/damov4/android-pos/app")["id"])
        self.assertEqual("damov4", pm.resolve_project_for_workspace(self.conn, "/Users/x/damov4/cloud")["id"])

    def test_remember_and_recall(self) -> None:
        pm.remember(self.conn, project_id="damov4", title="支付走 PromptPay EMV", entry_type="decision", importance=3)
        pm.remember(self.conn, project_id="damov4", title="store-sync 在测试站", entry_type="fact")
        items = pm.recall(self.conn, project_id="damov4")
        self.assertEqual(2, len(items))
        self.assertEqual("支付走 PromptPay EMV", items[0]["title"])  # higher importance first
        self.assertEqual(1, len(pm.recall(self.conn, project_id="damov4", query="promptpay")))

    def test_capture_task_outcome_auto_files_into_project(self) -> None:
        task = {"id": "t1", "title": "S07 PromptPay", "target_agent": "codex",
                "workspace": "/Users/x/damov4/android-pos"}
        entry = pm.capture_task_outcome(self.conn, task, kind="done", summary="EMV 动态二维码完成", evidence="/p/e.png")
        self.assertIsNotNone(entry)
        self.assertEqual("evidence", entry["entry_type"])
        self.assertEqual("codex", entry["author_agent"])
        # a task outside any project workspace is a no-op
        self.assertIsNone(pm.capture_task_outcome(self.conn, {"id": "t2", "title": "x", "workspace": "/tmp"}, kind="done"))

    def test_curate_dedups_and_builds_digest(self) -> None:
        pm.remember(self.conn, project_id="damov4", title="base URL", body="指向 prod(旧)", entry_type="diagnosis")
        pm.remember(self.conn, project_id="damov4", title="base URL", body="已改测试站,sync 7/7", entry_type="diagnosis")
        result = pm.curate(self.conn, project_id="damov4")
        self.assertEqual(1, result["superseded"])  # older "base URL" diagnosis superseded by newer
        self.assertEqual(1, result["active_entries"])
        self.assertIn("项目记忆摘要", result["digest"])
        self.assertIn("已改测试站", result["digest"])      # the surviving (newer) entry
        self.assertNotIn("指向 prod(旧)", result["digest"])  # the superseded one is gone
        # digest persisted on the project + reachable by workspace
        self.assertIn("已改测试站", pm.digest_for_workspace(self.conn, "/Users/x/damov4/android-pos"))

    def test_curate_all_only_touches_changed_projects(self) -> None:
        pm.remember(self.conn, project_id="damov4", title="A")
        first = pm.curate_all(self.conn)
        self.assertEqual(["damov4"], first["projects"])
        second = pm.curate_all(self.conn)  # nothing new since digest → no-op
        self.assertEqual(0, second["curated"])
        pm.remember(self.conn, project_id="damov4", title="B")
        third = pm.curate_all(self.conn)  # new memory → re-curate
        self.assertEqual(["damov4"], third["projects"])

    def test_digest_block_for_task_injects_only_for_project_tasks(self) -> None:
        pm.remember(self.conn, project_id="damov4", title="支付走 PromptPay", entry_type="decision")
        pm.curate(self.conn, project_id="damov4")
        block = pm.digest_block_for_task(self.conn, {"id": "t", "workspace": "/Users/x/damov4/android-pos"})
        self.assertIn("项目记忆", block)
        self.assertIn("支付走 PromptPay", block)
        self.assertEqual("", pm.digest_block_for_task(self.conn, {"id": "t2", "workspace": "/tmp/elsewhere"}))

    def test_capture_meeting_conclusion_stores_decision(self) -> None:
        entry = pm.capture_meeting_conclusion(
            self.conn, project_id="damov4", title="支付方案评审",
            conclusion="结论:走 PromptPay EMV,storeId 注入测试站。", conversation_id="conv-1",
            synthesizer="hermes", mode="discuss")
        self.assertIsNotNone(entry)
        self.assertEqual("decision", entry["entry_type"])
        self.assertEqual("conv-1", entry["source_conversation_id"])
        self.assertEqual(3, entry["importance"])
        self.assertIn("方案/决策", entry["title"])
        # no project or empty conclusion → no-op
        self.assertIsNone(pm.capture_meeting_conclusion(self.conn, project_id="", title="x", conclusion="y"))
        self.assertIsNone(pm.capture_meeting_conclusion(self.conn, project_id="damov4", title="x", conclusion="   "))

    def test_archive_entry_removes_it_from_recall_and_digest(self) -> None:
        keep = pm.remember(self.conn, project_id="damov4", title="保留项", entry_type="convention")
        drop = pm.remember(self.conn, project_id="damov4", title="噪音项", entry_type="fact")
        res = pm.archive_entry(self.conn, entry_id=drop["id"], actor="owner")
        self.assertEqual("damov4", res["project_id"])
        titles = [e["title"] for e in pm.recall(self.conn, project_id="damov4")]
        self.assertIn("保留项", titles)
        self.assertNotIn("噪音项", titles)
        pm.curate(self.conn, project_id="damov4")
        self.assertNotIn("噪音项", pm.digest_for_project(self.conn, "damov4"))
        self.assertIsNone(pm.archive_entry(self.conn, entry_id="nope"))

    def test_capture_approval_decision_only_for_human_decisions_in_a_project(self) -> None:
        meta = {"title": "上线 v4", "description": "工作区: /Users/x/damov4/cloud", "target": "codex"}
        entry = pm.capture_approval_decision(self.conn, metadata=meta, action="production_deploy",
                                             decision="approved", actor="owner", reason="确认上线")
        self.assertIsNotNone(entry)
        self.assertEqual("decision", entry["entry_type"])
        self.assertIn("审批批准", entry["title"])
        # auto-approval (no human) → no-op
        self.assertIsNone(pm.capture_approval_decision(self.conn, metadata=meta, action="x", decision="approved", actor="auto"))
        # no project workspace → no-op
        self.assertIsNone(pm.capture_approval_decision(self.conn, metadata={"description": "工作区: /tmp/x"}, action="x", decision="denied", actor="owner"))

    def test_digest_for_project(self) -> None:
        pm.remember(self.conn, project_id="damov4", title="约定A", entry_type="convention")
        pm.curate(self.conn, project_id="damov4")
        self.assertIn("约定A", pm.digest_for_project(self.conn, "damov4"))
        self.assertEqual("", pm.digest_for_project(self.conn, "nope"))

    def test_curate_unknown_project_errors(self) -> None:
        with self.assertRaises(ValueError):
            pm.curate(self.conn, project_id="nope")


if __name__ == "__main__":
    unittest.main()
