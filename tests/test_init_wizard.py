"""The guided `companyctl init` wizard: runtime detection must be driven by what's actually on PATH,
and dry-run / detection must never mutate anything (no employees spawned, no services touched)."""
from __future__ import annotations

import argparse
import unittest
from unittest import mock

from company_kernel import init_wizard


class InitWizardTest(unittest.TestCase):
    def test_detect_runtimes_only_reports_installed_clis(self):
        def fake_which(binary):
            return f"/usr/local/bin/{binary}" if binary in ("codex", "claude") else None
        with mock.patch.object(init_wizard.shutil, "which", side_effect=fake_which):
            found = init_wizard._detect_runtimes()
        self.assertEqual({"codex", "claude"}, set(found))           # only the installed ones
        self.assertEqual("/usr/local/bin/codex", found["codex"])    # with their resolved path
        self.assertNotIn("gemini", found)                            # absent CLI → not offered

    def test_dry_run_changes_nothing(self):
        args = argparse.Namespace(yes=True, execute=False, dry_run=True)
        with mock.patch.object(init_wizard.shutil, "which", side_effect=lambda b: f"/usr/local/bin/{b}"), \
             mock.patch.object(init_wizard.subprocess, "run") as run_mock, \
             mock.patch.object(init_wizard, "_run_ctl", return_value=(0, "{}")) as ctl_mock:
            code = init_wizard.run_init(args)
        self.assertEqual(0, code)
        run_mock.assert_not_called()   # dry-run must not spawn company-add-employee
        ctl_mock.assert_not_called()   # dry-run must not call companyctl (no owner/doctor side effects)

    def test_yes_mode_adds_detected_runtimes_without_execute_by_default(self):
        args = argparse.Namespace(yes=True, execute=False, dry_run=False)
        calls = []
        def fake_run(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(returncode=0, stdout="ok", stderr="")
        with mock.patch.object(init_wizard.shutil, "which", side_effect=lambda b: f"/usr/local/bin/{b}" if b == "codex" else None), \
             mock.patch.object(init_wizard.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(init_wizard, "_run_ctl", return_value=(0, '{"ok": true, "counts": {"employees": 1}}')):
            code = init_wizard.run_init(args)
        self.assertEqual(0, code)
        add_calls = [c for c in calls if any("company-add-employee" in str(x) for x in c)]
        self.assertEqual(1, len(add_calls), calls)               # exactly the one detected runtime
        self.assertIn("codex", [str(x) for x in add_calls[0]])
        self.assertNotIn("--execute", add_calls[0])               # safe default: no autonomous execution


if __name__ == "__main__":
    unittest.main()
