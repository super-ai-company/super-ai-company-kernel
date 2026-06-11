from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from company_kernel.openclaw_external_agent_bridge import BridgeConfig, run_bridge


class FakeRunner:
    def __init__(self, adapter_ok: bool = True) -> None:
        self.commands: list[list[str]] = []
        self.adapter_ok = adapter_ok

    def __call__(self, command: list[str]) -> tuple[int, str, str]:
        self.commands.append(command)
        text = " ".join(command)
        if " task show " in text:
            return 1, json.dumps({"ok": False, "error": "task not found"}), ""
        if " task submit " in text:
            return 0, json.dumps({"ok": True, "task": {"id": command[-1]}}), ""
        if "company-codex-adapter" in text or "company-antigravity-adapter" in text:
            if self.adapter_ok:
                return 0, json.dumps({"ok": True, "reply": "adapter completed", "progress_report": "/tmp/adapter-evidence.json"}), ""
            return 1, json.dumps({"ok": False, "blocker": "adapter blocked", "progress_report": "/tmp/adapter-blocked.json"}), "blocked"
        if " task done " in text:
            return 0, json.dumps({"ok": True, "status": "completed"}), ""
        if " task block " in text:
            return 0, json.dumps({"ok": True, "status": "blocked"}), ""
        return 0, json.dumps({"ok": True}), ""


def write_openclaw_task(root: Path, agent: str, task_id: str, payload: dict | None = None) -> Path:
    path = root / "ops" / "agent_bus" / "inbox" / agent / f"{task_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "task_id": task_id,
                "created_at": "2026-06-11T00:00:00",
                "source_agent": "main",
                "target_agent": agent,
                "type": "external_agent_task",
                "priority": "P2",
                "payload": payload or {"instruction": "read README", "skill_id": "repo-inspection"},
                "status": "submitted",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


class OpenClawExternalAgentBridgeTests(unittest.TestCase):
    def test_codex_task_is_translated_to_kernel_and_closed_without_adapter_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_path = write_openclaw_task(root, "codex", "oc-codex-1")
            runner = FakeRunner()
            result = run_bridge(BridgeConfig(openclaw_root=root, agents=["codex"], execute=True, execute_adapter=False), runner)

            self.assertTrue(result["ok"])
            self.assertFalse(task_path.exists())
            done = root / "ops" / "agent_bus" / "done" / "codex" / "oc-codex-1.json"
            self.assertTrue(done.exists())
            done_payload = json.loads(done.read_text(encoding="utf-8"))
            self.assertEqual("completed", done_payload["status"])
            joined = [" ".join(cmd) for cmd in runner.commands]
            self.assertTrue(any(" task submit " in cmd and "--to codex" in cmd for cmd in joined))
            self.assertTrue(any(" task done " in cmd and "--agent codex" in cmd for cmd in joined))
            self.assertFalse(any("company-codex-adapter" in cmd for cmd in joined))

    def test_agy_alias_uses_antigravity_adapter_and_failed_adapter_moves_to_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_path = write_openclaw_task(root, "agy", "oc-agy-1", {"instruction": "review dashboard"})
            runner = FakeRunner(adapter_ok=False)
            result = run_bridge(BridgeConfig(openclaw_root=root, agents=["agy"], execute=True, execute_adapter=True), runner)

            self.assertFalse(result["ok"])
            self.assertFalse(task_path.exists())
            failed = root / "ops" / "agent_bus" / "failed" / "agy" / "oc-agy-1.json"
            self.assertTrue(failed.exists())
            failed_payload = json.loads(failed.read_text(encoding="utf-8"))
            self.assertEqual("blocked", failed_payload["status"])
            joined = [" ".join(cmd) for cmd in runner.commands]
            self.assertTrue(any(" task submit " in cmd and "--to antigravity" in cmd for cmd in joined))
            self.assertTrue(any("company-antigravity-adapter" in cmd for cmd in joined))
            self.assertTrue(any(" task block " in cmd and "--agent antigravity" in cmd for cmd in joined))


if __name__ == "__main__":
    unittest.main()
