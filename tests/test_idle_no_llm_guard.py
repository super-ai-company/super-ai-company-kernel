"""Core product invariant: an employee on duty is FREE. A daemon tick with no submitted task must do
only SQL (heartbeat + poll) and return — it must NEVER spawn the runtime LLM. This guard fails loudly
if a future change accidentally makes idle ticks burn tokens.

See docs/ON_DUTY_COST_MODEL.md."""
from __future__ import annotations

import unittest
from unittest import mock


class IdleTickNoLLMGuardTest(unittest.TestCase):
    def test_idle_codex_tick_never_invokes_the_llm(self):
        from company_kernel import codex_adapter
        args = codex_adapter.build_parser().parse_args(["--agent", "codex-cli", "--execute"])
        emp = {"id": "codex-cli", "runtime": "codex", "workspace": "/tmp"}
        with mock.patch.object(codex_adapter.shutil, "which", return_value="/usr/local/bin/codex"), \
             mock.patch.object(codex_adapter, "employee", return_value=emp), \
             mock.patch.object(codex_adapter, "next_codex_task", return_value=None), \
             mock.patch.object(codex_adapter, "run_codex") as run_llm, \
             mock.patch.object(codex_adapter, "run_companyctl", return_value=(0, "", "")) as ctl:
            code = codex_adapter.process(args)
        self.assertEqual(0, code)
        run_llm.assert_not_called()                                            # ZERO LLM on an idle tick
        self.assertTrue(any("heartbeat" in str(c) for c in ctl.call_args_list))  # only the SQL heartbeat

    def test_idle_openclaw_tick_never_invokes_the_llm(self):
        from company_kernel import openclaw_adapter
        args = openclaw_adapter.build_parser().parse_args(["--agent", "nestcar", "--execute"])
        emp = {"id": "nestcar", "runtime": "openclaw", "workspace": "/tmp"}
        # the openclaw runtime is invoked via subprocess.run inside the adapter; on an idle tick it must
        # not be reached. We allow run_companyctl (heartbeat) but assert the runtime subprocess isn't run.
        with mock.patch.object(openclaw_adapter, "employee", return_value=emp), \
             mock.patch.object(openclaw_adapter, "next_task", return_value=None), \
             mock.patch.object(openclaw_adapter, "run_companyctl", return_value=(0, "", "")) as ctl, \
             mock.patch.object(openclaw_adapter.subprocess, "run") as raw_run:
            code = openclaw_adapter.process(args) if hasattr(openclaw_adapter, "process") else openclaw_adapter.main(["--agent", "nestcar", "--execute"])
        self.assertEqual(0, code)
        raw_run.assert_not_called()   # idle tick → no runtime subprocess (the openclaw LLM)
        self.assertTrue(any("heartbeat" in str(c) for c in ctl.call_args_list))


if __name__ == "__main__":
    unittest.main()
