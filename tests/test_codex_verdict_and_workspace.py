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
            return 0, "{}", ""

        def fake_run_codex(task_card, workspace, output, events, sandbox, model, isolation, sandbox_profile, timeout_seconds=1800):
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


if __name__ == "__main__":
    unittest.main()
