from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from company_kernel import codex_adapter


class FakeRow:
    def __init__(self, data: dict):
        self._data = data

    def __getitem__(self, key):
        return self._data.get(key)


def fake_task(**overrides) -> FakeRow:
    base = {
        "id": f"task-verdict-test-{uuid.uuid4().hex[:8]}",
        "title": "verdict test",
        "description": "",
        "source_agent": "owner",
        "target_agent": "codex",
        "priority": "P2",
    }
    base.update(overrides)
    return FakeRow(base)


class ParseVerdictTest(unittest.TestCase):
    def write(self, text: str) -> Path:
        p = Path(self.tmp.name) / "out.md"
        p.write_text(text, encoding="utf-8")
        return p

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_completed(self) -> None:
        self.assertEqual(("completed", ""), codex_adapter.parse_verdict(self.write("did work\nSTATUS: completed\n")))

    def test_done_alias_maps_to_completed(self) -> None:
        verdict, _ = codex_adapter.parse_verdict(self.write("ok\nstatus: done\n"))
        self.assertEqual("completed", verdict)

    def test_blocked_with_dash_reason(self) -> None:
        verdict, reason = codex_adapter.parse_verdict(self.write("tried\nSTATUS: blocked - php not installed\n"))
        self.assertEqual("blocked", verdict)
        self.assertEqual("php not installed", reason)

    def test_blocked_with_fullwidth_colon(self) -> None:
        verdict, reason = codex_adapter.parse_verdict(self.write("尝试过\nSTATUS：blocked —— 沙箱无 docker 权限\n"))
        self.assertEqual("blocked", verdict)
        self.assertIn("docker", reason)

    def test_missing_marker(self) -> None:
        self.assertEqual(("missing", ""), codex_adapter.parse_verdict(self.write("I think everything is fine!\n")))

    def test_multiple_markers_takes_last(self) -> None:
        text = "STATUS: blocked - first try\nretried successfully\nSTATUS: completed\n"
        verdict, _ = codex_adapter.parse_verdict(self.write(text))
        self.assertEqual("completed", verdict)

    def test_missing_file(self) -> None:
        verdict, reason = codex_adapter.parse_verdict(Path(self.tmp.name) / "nope.md")
        self.assertEqual("missing", verdict)
        self.assertTrue(reason)


class ResolveTaskWorkspaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.default = Path(self.tmp.name) / "default-ws"
        self.default.mkdir()

    def test_no_directive_uses_default(self) -> None:
        ws, err = codex_adapter.resolve_task_workspace(fake_task(description="普通任务描述"), self.default)
        self.assertEqual(self.default, ws)
        self.assertEqual("", err)

    def test_valid_absolute_directive(self) -> None:
        target = Path(self.tmp.name) / "proj"
        target.mkdir()
        for directive in (f"工作区: {target}", f"workspace: {target}", f"工作区：{target}"):
            ws, err = codex_adapter.resolve_task_workspace(fake_task(description=f"目标说明\n{directive}\n验收: x"), self.default)
            self.assertEqual(target.resolve(), ws, directive)
            self.assertEqual("", err, directive)

    def test_forgiving_directive_variants(self) -> None:
        # dispatchers write the path many ways — the parser must catch the bracketed/labeled
        # forms too, not just a bare `工作区:` line (this mismatch caused real /tmp blocks).
        target = Path(self.tmp.name) / "repo"
        target.mkdir()
        for directive in (
            f"【工作区/仓库绝对路径】：{target}",
            f"工作目录: {target}",
            f"仓库路径：{target}",
            f"请在工作区: {target} 执行",
        ):
            ws, err = codex_adapter.resolve_task_workspace(fake_task(description=f"目标\n{directive}\n步骤"), self.default)
            self.assertEqual(target.resolve(), ws, directive)
            self.assertEqual("", err, directive)

    def test_task_timeout_directive(self) -> None:
        self.assertEqual(1800, codex_adapter.resolve_task_timeout(fake_task(description="无指令"), 1800))
        self.assertEqual(3600, codex_adapter.resolve_task_timeout(fake_task(description="超时: 3600\n其它"), 1800))
        self.assertEqual(3600, codex_adapter.resolve_task_timeout(fake_task(description="超时：60分钟"), 1800))
        self.assertEqual(2400, codex_adapter.resolve_task_timeout(fake_task(description="timeout: 40min"), 1800))
        # capped at 60 min so one task can't hang the synchronous daemon
        self.assertEqual(3600, codex_adapter.resolve_task_timeout(fake_task(description="超时: 99999"), 1800))
        # garbage / zero falls back to default
        self.assertEqual(1800, codex_adapter.resolve_task_timeout(fake_task(description="超时: 0"), 1800))
        # no directive but a big task (marker or long desc) auto-bumps to the cap; small stays default
        self.assertEqual(3600, codex_adapter.resolve_task_timeout(fake_task(description="全流程 E2E 重测打包 APK"), 1800))
        self.assertEqual(3600, codex_adapter.resolve_task_timeout(fake_task(description="v3→v4 会员订单 ETL 迁移对账"), 1800))
        self.assertEqual(3600, codex_adapter.resolve_task_timeout(fake_task(description="x" * 900), 1800))
        self.assertEqual(1800, codex_adapter.resolve_task_timeout(fake_task(description="改个字段名"), 1800))


    def test_last_message_substantive_distinguishes_output_from_hang(self) -> None:
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            full = Path(d)/"m.md"; full.write_text("STATUS: 已实现登录端点并通过单测。"*12, encoding="utf-8")
            self.assertTrue(codex_adapter.last_message_substantive(full))
            empty = Path(d)/"e.md"; empty.write_text("codex exec killed after exceeding timeout of 1800 seconds", encoding="utf-8")
            self.assertFalse(codex_adapter.last_message_substantive(empty))

    def test_prose_path_without_keyword_ignored(self) -> None:
        # a path mentioned in prose (no workspace keyword+colon) must NOT be grabbed as the workspace
        for desc in ("参考 /tmp/x.md 里的说明", "在 v4 容器内跑:项目 /home/h 里"):
            ws, err = codex_adapter.resolve_task_workspace(fake_task(description=desc), self.default)
            self.assertEqual(self.default, ws, desc)
            self.assertEqual("", err, desc)

    def test_relative_path_rejected(self) -> None:
        ws, err = codex_adapter.resolve_task_workspace(fake_task(description="工作区: ./relative"), self.default)
        self.assertEqual(self.default, ws)
        self.assertIn("absolute", err)

    def test_nonexistent_path_rejected(self) -> None:
        ws, err = codex_adapter.resolve_task_workspace(fake_task(description=f"工作区: {self.tmp.name}/missing-dir"), self.default)
        self.assertIn("does not exist", err)

    def test_kernel_root_rejected(self) -> None:
        for path in (codex_adapter.ROOT, codex_adapter.ROOT / "company_kernel"):
            ws, err = codex_adapter.resolve_task_workspace(fake_task(description=f"工作区: {path}"), self.default)
            self.assertIn("Company Kernel", err, str(path))


class QueueVerdictIntegrationTest(unittest.TestCase):
    """End-to-end through process() with kernel writes mocked: claim/done/block calls captured."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name) / "ws"
        self.ws.mkdir()
        self.calls: list[list[str]] = []

    def run_adapter(self, task: FakeRow, codex_output: str, codex_exit: int = 0, expect_run: bool = True) -> dict:
        ran = {"called": False}

        def fake_run_companyctl(argv):
            self.calls.append(list(argv))
            # Provide payloads the managed-execution flow expects (task run -> attempt, etc.)
            joined = " ".join(argv)
            if argv[:2] == ["task", "run"]:
                return 0, json.dumps({"ok": True, "attempt": {"attempt_id": "att-test", "trace_id": "trace-test"}}), ""
            if "session" in argv and "start" in argv:
                return 0, json.dumps({"ok": True, "session": {"session_id": "sess-test"}}), ""
            if argv[:2] == ["task", "attempt"] or ("attempt" in argv and "finish" in argv):
                return 0, json.dumps({"ok": True, "attempt": {"attempt_id": "att-test", "status": "finished"}}), ""
            return 0, "{}", ""

        def fake_run_codex(task_card, workspace, output, events, sandbox, model, isolation, sandbox_profile, timeout_seconds=1800, **kwargs):
            ran["called"] = True
            ran["workspace"] = workspace
            output.write_text(codex_output, encoding="utf-8")
            events.write_text("{}\n", encoding="utf-8")
            return codex_exit, f"codex exec -C {workspace}"

        emp = FakeRow({"id": "codex", "runtime": "codex", "workspace": str(self.ws), "status": "active"})
        captured = io.StringIO()
        with mock.patch.object(codex_adapter, "employee", lambda agent: emp), \
                mock.patch.object(codex_adapter, "next_codex_task", lambda agent: task), \
                mock.patch.object(codex_adapter, "run_companyctl", fake_run_companyctl), \
                mock.patch.object(codex_adapter, "run_codex", fake_run_codex), \
                mock.patch.object(codex_adapter, "copy_report_to_task_evidence", lambda task_id, report: report), \
                mock.patch.object(codex_adapter.shutil, "which", lambda name: "/usr/local/bin/codex"), \
                contextlib.redirect_stdout(captured):
            codex_adapter.main(["--agent", "codex", "--execute"])
        self.assertEqual(expect_run, ran["called"], "run_codex invocation mismatch")
        result = json.loads(captured.getvalue())
        result["_ran"] = ran
        return result

    def verbs(self) -> list[str]:
        return [argv[1] if argv[0] == "task" else argv[0] for argv in self.calls if argv]

    def test_completed_verdict_marks_done(self) -> None:
        result = self.run_adapter(fake_task(), "work done\nSTATUS: completed\n")
        self.assertEqual("completed", result["verdict"])
        self.assertIn("done", self.verbs())
        self.assertNotIn("block", self.verbs())
        self.assertTrue(result["ok"])

    def test_blocked_verdict_blocks_with_reason(self) -> None:
        result = self.run_adapter(fake_task(), "tried\nSTATUS: blocked - composer missing\n")
        self.assertEqual("blocked", result["verdict"])
        self.assertIn("block", self.verbs())
        self.assertNotIn("done", self.verbs())
        block_argv = next(argv for argv in self.calls if argv[:2] == ["task", "block"])
        blocker = block_argv[block_argv.index("--blocker") + 1]
        self.assertIn("composer missing", blocker)
        # deterministic verdict block must not look like infra failure (no auto-retry)
        self.assertTrue(result["ok"])

    def test_missing_verdict_blocks_for_review(self) -> None:
        result = self.run_adapter(fake_task(), "I am sure everything worked\n")
        self.assertEqual("missing", result["verdict"])
        self.assertIn("block", self.verbs())
        block_argv = next(argv for argv in self.calls if argv[:2] == ["task", "block"])
        blocker = block_argv[block_argv.index("--blocker") + 1]
        self.assertIn("STATUS", blocker)

    def test_crash_blocks_and_flags_infra_failure(self) -> None:
        result = self.run_adapter(fake_task(), "partial\n", codex_exit=9)
        self.assertEqual("crashed", result["verdict"])
        self.assertIn("block", self.verbs())
        self.assertFalse(result["ok"], "infra failure must surface for retry policy")

    def test_workspace_directive_routes_execution(self) -> None:
        target = Path(self.tmp.name) / "damov4"
        target.mkdir()
        task = fake_task(description=f"做点事\n工作区: {target}\nSTATUS 要求照常")
        result = self.run_adapter(task, "ok\nSTATUS: completed\n")
        self.assertEqual(str(target.resolve()), str(result["_ran"]["workspace"]))

    def test_invalid_workspace_blocks_before_execution(self) -> None:
        task = fake_task(description=f"工作区: {self.tmp.name}/not-there")
        result = self.run_adapter(task, "should never run", expect_run=False)
        self.assertEqual("workspace_invalid", result["verdict"])
        self.assertIn("block", self.verbs())
        self.assertNotIn("done", self.verbs())

    def test_kernel_workspace_blocks_before_execution(self) -> None:
        task = fake_task(description=f"工作区: {codex_adapter.ROOT}")
        result = self.run_adapter(task, "should never run", expect_run=False)
        self.assertEqual("workspace_invalid", result["verdict"])
        block_argv = next(argv for argv in self.calls if argv[:2] == ["task", "block"])
        self.assertIn("RFC", block_argv[block_argv.index("--blocker") + 1])


class ReviewTaskVerdictTest(unittest.TestCase):
    """A review/analysis task's deliverable is the verdict — a 'blocked / can't merge' conclusion is a
    finding, not a task failure, so review tasks must be detected and not pile up as 'blocked'."""

    def test_is_review_task_detection(self) -> None:
        self.assertTrue(codex_adapter.is_review_task("只读审核 这个分支"))
        self.assertTrue(codex_adapter.is_review_task("任务类型: 审核\n看 diff"))
        self.assertTrue(codex_adapter.is_review_task("第3轮复审:只读复审 main 改动"))
        self.assertTrue(codex_adapter.is_review_task("只读终轮复审。读 VERSION-SUMMARY.md"))  # words between 只读…复审
        self.assertTrue(codex_adapter.is_review_task("please code review the change"))
        self.assertFalse(codex_adapter.is_review_task("实现登录功能并提交"))
        self.assertFalse(codex_adapter.is_review_task(""))
        # must NOT match a dev task that merely names a "code review" feature (codex round-6 finding)
        self.assertFalse(codex_adapter.is_review_task("实现 code review dashboard 功能并提交"))
        self.assertFalse(codex_adapter.is_review_task("build a review system for orders"))


if __name__ == "__main__":
    unittest.main()


class CostGateTest(unittest.TestCase):
    """codex adapter blocks (needs quote) when a task's cumulative cost hits the cap,
    instead of running codex again and losing money on a hard task."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name) / "ws"; self.ws.mkdir()
        self.calls = []

    def test_cost_cap_blocks_before_running(self):
        ran = {"called": False}

        def fake_run_companyctl(argv):
            self.calls.append(list(argv))
            return 0, "{}", ""

        def fake_run_codex(*a, **k):
            ran["called"] = True
            return 0, "codex exec"

        emp = FakeRow({"id": "codex", "runtime": "codex", "workspace": str(self.ws), "status": "active"})
        task = fake_task(description="普通任务")
        import io, contextlib, json as _json
        captured = io.StringIO()
        with mock.patch.object(codex_adapter, "employee", lambda agent: emp), \
                mock.patch.object(codex_adapter, "next_codex_task", lambda agent: task), \
                mock.patch.object(codex_adapter, "run_companyctl", fake_run_companyctl), \
                mock.patch.object(codex_adapter, "run_codex", fake_run_codex), \
                mock.patch.object(codex_adapter, "task_cost_so_far", lambda tid: 9.99), \
                mock.patch.object(codex_adapter.shutil, "which", lambda name: "/usr/local/bin/codex"), \
                contextlib.redirect_stdout(captured):
            codex_adapter.main(["--agent", "codex", "--execute", "--max-cost", "5"])
        result = _json.loads(captured.getvalue())
        self.assertEqual("cost_capped", result["verdict"])
        self.assertTrue(result["needs_quote"])
        self.assertFalse(ran["called"], "must NOT run codex once cost cap is hit")
        self.assertIn("block", [a[1] if a[0] == "task" else a[0] for a in self.calls])

    def test_under_cap_still_runs(self):
        ran = {"called": False}
        def fake_run_companyctl(argv):
            self.calls.append(list(argv))
            if argv[:2] == ["task", "run"]:
                return 0, '{"ok":true,"attempt":{"attempt_id":"a","trace_id":"t"}}', ""
            if "session" in argv and "start" in argv:
                return 0, '{"ok":true,"session":{"session_id":"s"}}', ""
            if argv[:2] == ["task", "attempt"]:
                return 0, '{"ok":true,"attempt":{"attempt_id":"a"}}', ""
            return 0, "{}", ""
        def fake_run_codex(task_card, workspace, output, events, *a, **k):
            ran["called"] = True
            output.write_text("ok\nSTATUS: completed\n", encoding="utf-8")
            events.write_text("{}\n", encoding="utf-8")
            return 0, "codex exec"
        emp = FakeRow({"id": "codex", "runtime": "codex", "workspace": str(self.ws), "status": "active"})
        import io, contextlib, json as _json
        captured = io.StringIO()
        with mock.patch.object(codex_adapter, "employee", lambda agent: emp), \
                mock.patch.object(codex_adapter, "next_codex_task", lambda agent: fake_task(description="x")), \
                mock.patch.object(codex_adapter, "run_companyctl", fake_run_companyctl), \
                mock.patch.object(codex_adapter, "run_codex", fake_run_codex), \
                mock.patch.object(codex_adapter, "task_cost_so_far", lambda tid: 1.0), \
                mock.patch.object(codex_adapter, "copy_report_to_task_evidence", lambda tid, r: r), \
                mock.patch.object(codex_adapter.shutil, "which", lambda name: "/usr/local/bin/codex"), \
                contextlib.redirect_stdout(captured):
            codex_adapter.main(["--agent", "codex", "--execute", "--max-cost", "5"])
        self.assertTrue(ran["called"], "under the cap codex should run")


class VerifierGateIntegrationTest(QueueVerdictIntegrationTest):
    """Even when the agent says STATUS: completed, a failing verifier blocks the task."""

    def test_numeric_verifier_fail_blocks_despite_completed(self):
        task = fake_task(description="算总额\n验收: numeric: 101062.00")
        # agent claims completed but its output has the wrong number
        result = self.run_adapter(task, "total=999\nSTATUS: completed\n")
        self.assertEqual("verifier_failed", result["verdict"])
        self.assertIn("block", self.verbs())
        self.assertNotIn("done", self.verbs())

    def test_numeric_verifier_pass_completes(self):
        task = fake_task(description="算总额\n验收: numeric: 101062.00")
        result = self.run_adapter(task, "result total=101062.00 done\nSTATUS: completed\n")
        self.assertEqual("completed", result["verdict"])
        self.assertIn("done", self.verbs())

    def test_human_verifier_routes_to_review(self):
        task = fake_task(description="设计稿\n验收: human")
        result = self.run_adapter(task, "做完了\nSTATUS: completed\n")
        self.assertEqual("needs_human", result["verdict"])
        self.assertIn("block", self.verbs())
        self.assertNotIn("done", self.verbs())


class TokenRetryGateTest(CostGateTest):
    """Token and retry caps block (needs quote) before running codex again."""

    def _block_run(self, argv_extra, mocks):
        ran = {"called": False}
        def fake_run_companyctl(argv):
            self.calls.append(list(argv))
            return 0, "{}", ""
        def fake_run_codex(*a, **k):
            ran["called"] = True
            return 0, "codex exec"
        emp = FakeRow({"id": "codex", "runtime": "codex", "workspace": str(self.ws), "status": "active"})
        import io, contextlib, json as _json
        captured = io.StringIO()
        ctx = [
            mock.patch.object(codex_adapter, "employee", lambda agent: emp),
            mock.patch.object(codex_adapter, "next_codex_task", lambda agent: fake_task(description="x")),
            mock.patch.object(codex_adapter, "run_companyctl", fake_run_companyctl),
            mock.patch.object(codex_adapter, "run_codex", fake_run_codex),
            mock.patch.object(codex_adapter.shutil, "which", lambda name: "/usr/local/bin/codex"),
        ]
        for k, v in mocks.items():
            ctx.append(mock.patch.object(codex_adapter, k, v))
        with contextlib.ExitStack() as stack:
            for c in ctx:
                stack.enter_context(c)
            stack.enter_context(contextlib.redirect_stdout(captured))
            codex_adapter.main(["--agent", "codex", "--execute"] + argv_extra)
        return _json.loads(captured.getvalue()), ran

    def test_token_cap_blocks(self):
        result, ran = self._block_run(["--max-tokens", "1000"], {"task_tokens_so_far": lambda tid: 1500})
        self.assertEqual("token_capped", result["verdict"])
        self.assertTrue(result["needs_quote"])
        self.assertFalse(ran["called"])

    def test_retry_cap_blocks(self):
        result, ran = self._block_run(["--max-retries", "3"], {"task_attempts_so_far": lambda tid: 3})
        self.assertEqual("retry_capped", result["verdict"])
        self.assertTrue(result["needs_quote"])
        self.assertFalse(ran["called"])

    def test_under_token_cap_runs(self):
        ran = {"called": False}
        def fake_run_companyctl(argv):
            self.calls.append(list(argv))
            if argv[:2] == ["task", "run"]:
                return 0, '{"ok":true,"attempt":{"attempt_id":"a","trace_id":"t"}}', ""
            if "session" in argv and "start" in argv:
                return 0, '{"ok":true,"session":{"session_id":"s"}}', ""
            return 0, "{}", ""
        def fake_run_codex(task_card, workspace, output, events, *a, **k):
            ran["called"] = True
            output.write_text("ok\nSTATUS: completed\n", encoding="utf-8")
            events.write_text("{}\n", encoding="utf-8")
            return 0, "codex exec"
        emp = FakeRow({"id": "codex", "runtime": "codex", "workspace": str(self.ws), "status": "active"})
        import io, contextlib, json as _json
        captured = io.StringIO()
        with mock.patch.object(codex_adapter, "employee", lambda agent: emp), \
                mock.patch.object(codex_adapter, "next_codex_task", lambda agent: fake_task(description="x")), \
                mock.patch.object(codex_adapter, "run_companyctl", fake_run_companyctl), \
                mock.patch.object(codex_adapter, "run_codex", fake_run_codex), \
                mock.patch.object(codex_adapter, "task_tokens_so_far", lambda tid: 10), \
                mock.patch.object(codex_adapter, "task_attempts_so_far", lambda tid: 0), \
                mock.patch.object(codex_adapter, "copy_report_to_task_evidence", lambda tid, r: r), \
                mock.patch.object(codex_adapter.shutil, "which", lambda name: "/usr/local/bin/codex"), \
                contextlib.redirect_stdout(captured):
            codex_adapter.main(["--agent", "codex", "--execute", "--max-tokens", "100000", "--max-retries", "5"])
        self.assertTrue(ran["called"])
