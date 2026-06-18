"""Hermes orchestration loop: a finished step pushes a `result-*.json` completion notice into the
dispatcher's inbox; the daemon-run Hermes adapter must consume those and run the Hermes brain to
ADVANCE the plan (dispatch next step / summarize) — taskless, so no self-task clutters the board —
instead of stalling after one round.
"""
from __future__ import annotations

import argparse
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from company_kernel import hermes_adapter as ha


class ActionableCompletionTest(unittest.TestCase):
    def test_terminal_task_is_actionable(self) -> None:
        self.assertTrue(ha.is_actionable_completion({"task_id": "t1", "status": "completed"}))
        self.assertTrue(ha.is_actionable_completion({"task_id": "t2", "status": "blocked"}))
        # a cancelled task must reach Hermes too — so it stops waiting and re-dispatches/moves on
        self.assertTrue(ha.is_actionable_completion({"task_id": "t4", "status": "cancelled"}))

    def test_no_task_id_or_non_terminal_is_not(self) -> None:
        self.assertFalse(ha.is_actionable_completion({"status": "completed"}))      # loop-guard: no id
        self.assertFalse(ha.is_actionable_completion({"task_id": "t3", "status": "running"}))


class BuildAdvancePromptTest(unittest.TestCase):
    def test_prompt_carries_results_and_orchestrator_rules(self) -> None:
        prompt = ha.build_advance_prompt([
            {"task_id": "task-1", "title": "图片上传 API 骨架", "done_by": "codex",
             "status": "completed", "summary": "完成最小骨架"},
            {"task_id": "task-2", "title": "前端预审", "done_by": "claude",
             "status": "blocked", "blocker": "缺设计稿"},
        ])
        self.assertIn("完成最小骨架", prompt)        # codex result handed to the brain
        self.assertIn("缺设计稿", prompt)            # blocked reason handed too
        self.assertIn("受阻", prompt)
        self.assertIn("codex-cli", prompt)           # told where dev goes
        self.assertIn("claude-cli", prompt)          # ...and review
        self.assertIn("owner", prompt)         # ...and the round summary
        self.assertIn("绝不自己写代码", prompt)       # stays an orchestrator

    def test_cancelled_notice_tells_brain_to_stop_waiting(self) -> None:
        prompt = ha.build_advance_prompt([
            {"task_id": "task-9", "title": "v4 截图", "done_by": "codex",
             "status": "cancelled", "blocker": "已取消: 清板"},
        ])
        self.assertIn("已取消", prompt)
        self.assertIn("别再等", prompt)              # explicit: don't keep waiting


def _args(agent="hermes", execute=True):
    return argparse.Namespace(agent=agent, execute=execute, model="", provider="",
                              isolation="none", sandbox_profile="default", workspace="")


class AdvanceFromCompletionsTest(unittest.TestCase):
    def _notice(self, inbox: Path, tid: str, **extra) -> None:
        inbox.mkdir(parents=True, exist_ok=True)
        payload = {"type": "task.completed", "task_id": tid, "title": "t", "status": "completed", **extra}
        (inbox / f"result-{tid}.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_runs_brain_once_on_all_notices_and_archives(self) -> None:
        with TemporaryDirectory() as d:
            root = Path(d)
            inbox = root / "employees" / "hermes" / "inbox"
            self._notice(inbox, "task-a", done_by="codex", summary="A done")
            self._notice(inbox, "task-b", done_by="claude", summary="B reviewed")
            ran = {}
            calls = []

            def fake_run_hermes(prompt, output, workspace, *a, **k):
                ran["prompt"] = prompt.read_text(encoding="utf-8")
                output.write_text("已派 claude-cli 审核 task-a;task-b 通过→派下一阶段。", encoding="utf-8")
                return (0, "hermes -z <prompt>")

            def fake_ctl(argv):
                calls.append(argv)
                return (0, "{}", "")

            with mock.patch.object(ha, "ROOT", root), mock.patch.object(ha, "run_hermes", fake_run_hermes), \
                 mock.patch.object(ha, "run_companyctl", fake_ctl):
                res = ha.advance_from_completions(_args(), root)

            self.assertIsNotNone(res)
            self.assertTrue(res["executed"])
            self.assertEqual({"task-a", "task-b"}, set(res["advanced"]))
            self.assertIn("A done", ran["prompt"])           # one brain run got BOTH completions
            self.assertIn("B reviewed", ran["prompt"])
            # a progress note was pushed to the owner (→ Telegram via the message mirror)
            self.assertTrue(res["owner_progress_sent"])
            sends = [c for c in calls if c[:2] == ["message", "send"]]
            self.assertEqual(1, len(sends))
            self.assertIn("owner", sends[0])
            # no self-task created on the board; notices consumed/archived
            self.assertFalse(list(inbox.glob("result-*.json")))
            self.assertEqual(2, len(list((inbox / "processed").glob("result-*.json"))))

    def test_dry_run_keeps_notices(self) -> None:
        with TemporaryDirectory() as d:
            root = Path(d)
            inbox = root / "employees" / "hermes" / "inbox"
            self._notice(inbox, "task-c")
            with mock.patch.object(ha, "ROOT", root), \
                 mock.patch.object(ha, "run_hermes", lambda *a, **k: (0, "")):
                res = ha.advance_from_completions(_args(execute=False), root)
            self.assertFalse(res["executed"])
            self.assertTrue(list(inbox.glob("result-*.json")))   # not lost — left for an --execute tick

    def test_failed_brain_run_keeps_notices_for_retry(self) -> None:
        # If the hermes brain run fails (crash/timeout/529), notices must NOT be archived — they stay for
        # the next tick to retry, so a failed advance never silently drops a completion.
        with TemporaryDirectory() as d:
            root = Path(d)
            inbox = root / "employees" / "hermes" / "inbox"
            self._notice(inbox, "task-x", done_by="codex", summary="done")
            with mock.patch.object(ha, "ROOT", root), \
                 mock.patch.object(ha, "run_hermes", lambda *a, **k: (1, "boom")), \
                 mock.patch.object(ha, "run_companyctl", lambda a: (0, "", "")):
                res = ha.advance_from_completions(_args(), root)
            self.assertEqual(1, res["exit_code"])
            self.assertTrue(res["retry_pending"])
            self.assertTrue(list(inbox.glob("result-*.json")))            # kept for retry
            self.assertFalse(list((inbox / "processed").glob("result-*.json")))

    def test_none_when_nothing_waiting(self) -> None:
        with TemporaryDirectory() as d:
            with mock.patch.object(ha, "ROOT", Path(d)):
                self.assertIsNone(ha.advance_from_completions(_args(), Path(d)))

    def test_non_actionable_stragglers_cleared_without_running_brain(self) -> None:
        with TemporaryDirectory() as d:
            root = Path(d)
            inbox = root / "employees" / "hermes" / "inbox"
            self._notice(inbox, "task-x", status="running")   # not terminal → not actionable
            called = {"n": 0}

            def fake_run_hermes(*a, **k):
                called["n"] += 1
                return (0, "")

            with mock.patch.object(ha, "ROOT", root), mock.patch.object(ha, "run_hermes", fake_run_hermes):
                res = ha.advance_from_completions(_args(), root)
            self.assertIsNone(res)
            self.assertEqual(0, called["n"])                  # brain not run for non-actionable
            self.assertFalse(list(inbox.glob("result-*.json")))  # straggler still cleared


class ReportProgressToOwnerTest(unittest.TestCase):
    def _out(self, root, text="hermes 已派下一步"):
        p = root / "out.md"
        p.write_text(text, encoding="utf-8")
        return p

    def test_sends_progress_for_completed_batch(self) -> None:
        with TemporaryDirectory() as d:
            root = Path(d)
            calls = []
            with mock.patch.object(ha, "run_companyctl", lambda a: (calls.append(a), (0, "", ""))[1]):
                sent = ha.report_progress_to_owner(
                    "hermes", [{"task_id": "t1", "title": "阶段1F 批次 manifest", "status": "completed"}], self._out(root))
            self.assertTrue(sent)
            self.assertEqual(1, len([c for c in calls if c[:2] == ["message", "send"]]))
            body = calls[0][calls[0].index("--body") + 1]
            self.assertIn("阶段1F", body)            # carries what advanced
            self.assertIn("已派下一步", body)         # ...and hermes's decision snippet

    def test_skips_pure_blocked_batch(self) -> None:
        # pure block/cancel → no progress note (the watchdog alert already covers it)
        with TemporaryDirectory() as d:
            calls = []
            with mock.patch.object(ha, "run_companyctl", lambda a: (calls.append(a), (0, "", ""))[1]):
                sent = ha.report_progress_to_owner(
                    "hermes", [{"task_id": "t2", "title": "x", "status": "blocked"}], self._out(Path(d)))
            self.assertFalse(sent)
            self.assertEqual([], calls)


if __name__ == "__main__":
    unittest.main()
