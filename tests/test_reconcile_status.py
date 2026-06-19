"""A GUI-only runtime's headless CLI twin (agy for antigravity) must be verified+activated, not
archived as 'GUI-only'. The GUI app itself still archives. (Fixes 'agy always offline'.)"""
from __future__ import annotations

import unittest
from unittest import mock

from company_kernel import reconcile_status as rs


class ReconcileStatusTest(unittest.TestCase):
    def test_headless_cli_twin_of_gui_runtime_is_activated_not_archived(self):
        calls = []
        def fake_ctl(args):
            calls.append(args)
            return 0, ({"activated": True} if "verify-direct" in args else {})
        with mock.patch.object(rs, "ctl", side_effect=fake_ctl), \
             mock.patch.object(rs.shutil, "which", side_effect=lambda b: f"/bin/{b}" if b == "agy" else None):
            status, reason = rs.reconcile_one({"id": "agy", "runtime": "antigravity"}, set())
        self.assertEqual("active", status)                                  # twin verified → active
        self.assertFalse(any("archived" in str(c) for c in calls), calls)   # never archived
        self.assertTrue(any("verify-direct" in c for c in calls))           # went through CLI verify

    def test_gui_app_itself_is_still_archived(self):
        calls = []
        with mock.patch.object(rs, "ctl", side_effect=lambda a: calls.append(a) or (0, {})), \
             mock.patch.object(rs.shutil, "which", side_effect=lambda b: f"/bin/{b}" if b == "agy" else None):
            status, _ = rs.reconcile_one({"id": "antigravity", "runtime": "antigravity"}, set())
        self.assertEqual("archived", status)                                # the GUI app stays archived
        self.assertTrue(any("archived" in str(c) for c in calls))

    def test_twin_without_installed_cli_is_archived(self):
        """If the twin's CLI isn't installed, it can't work either → archive (don't fake online)."""
        with mock.patch.object(rs, "ctl", return_value=(0, {})), \
             mock.patch.object(rs.shutil, "which", return_value=None):  # agy not installed
            status, _ = rs.reconcile_one({"id": "agy", "runtime": "antigravity"}, set())
        self.assertEqual("archived", status)


if __name__ == "__main__":
    unittest.main()
