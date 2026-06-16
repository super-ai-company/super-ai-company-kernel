"""Antigravity managed-review timeout: the old 120s default cut off multi-screen reviews. Managed
attempts now floor at 30 min and honor a `超时:`/`timeout:` directive (capped at 1 h), while the
quick attendance probe is untouched."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from company_kernel import antigravity_adapter as agy


def _task(desc: str) -> dict:
    return {"description": desc}


class AntigravityTimeoutTest(unittest.TestCase):
    def test_no_directive_uses_floor_not_cli_default(self):
        # daemon worker passes the 120s CLI default → managed review must still get the 30-min floor
        self.assertEqual(agy.MANAGED_ATTEMPT_MIN_TIMEOUT_SECONDS,
                         agy.resolve_managed_timeout(_task("审核 S01-S15"), 120))

    def test_directive_seconds(self):
        self.assertEqual(3000, agy.resolve_managed_timeout(_task("审核\n超时: 3000\n详细"), 120))

    def test_directive_minutes(self):
        self.assertEqual(1500, agy.resolve_managed_timeout(_task("timeout: 25 min"), 120))

    def test_directive_capped_at_max(self):
        self.assertEqual(agy.MAX_TASK_TIMEOUT_SECONDS,
                         agy.resolve_managed_timeout(_task("超时:90min"), 120))

    def test_explicit_directive_below_floor_is_honored(self):
        # an explicit override is the operator's choice, even if shorter than the floor
        self.assertEqual(600, agy.resolve_managed_timeout(_task("timeout: 600s"), 120))

    def test_base_default_above_floor_wins_without_directive(self):
        self.assertEqual(2400, agy.resolve_managed_timeout(_task("no directive"), 2400))

    def test_garbage_directive_falls_back_to_floor(self):
        self.assertEqual(agy.MANAGED_ATTEMPT_MIN_TIMEOUT_SECONDS,
                         agy.resolve_managed_timeout(_task("超时: abc"), 120))


class AntigravityWorkspaceTest(unittest.TestCase):
    """agy must review in the employee's configured ABSOLUTE workspace automatically — the dispatcher
    should never have to paste absolute paths. A per-task `工作区:` directive can override it."""

    def setUp(self):
        self.a = tempfile.TemporaryDirectory(); self.addCleanup(self.a.cleanup)
        self.b = tempfile.TemporaryDirectory(); self.addCleanup(self.b.cleanup)
        self.dir_a, self.dir_b = Path(self.a.name).resolve(), Path(self.b.name).resolve()

    def test_uses_employee_workspace_without_any_directive(self):
        ws = agy.resolve_managed_workspace(_task("审核 S01-S15"), {"workspace": str(self.dir_b)})
        self.assertEqual(self.dir_b, ws)

    def test_task_directive_overrides_employee_workspace(self):
        ws = agy.resolve_managed_workspace(_task(f"工作区: {self.dir_a}\n审核"), {"workspace": str(self.dir_b)})
        self.assertEqual(self.dir_a, ws)

    def test_relative_or_missing_directive_ignored_falls_to_employee(self):
        ws = agy.resolve_managed_workspace(_task("工作区: ./rel/path"), {"workspace": str(self.dir_b)})
        self.assertEqual(self.dir_b, ws)

    def test_nothing_valid_falls_back_to_root(self):
        self.assertEqual(agy.ROOT, agy.resolve_managed_workspace(_task("审核"), {"workspace": ""}))
        self.assertEqual(agy.ROOT, agy.resolve_managed_workspace(_task("审核"), {"workspace": "/no/such/dir/xyz"}))


if __name__ == "__main__":
    unittest.main()
