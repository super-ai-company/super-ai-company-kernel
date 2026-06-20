"""Guard for the phased companyctl.py split: every public symbol that moved into a domain module
MUST stay re-exported from companyctl (the facade), so the 28 external importers keep working. A move
that drops a symbol fails HERE loudly instead of as a mystery AttributeError somewhere downstream."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from company_kernel import companyctl, watchdog


class FacadeReexportTest(unittest.TestCase):
    # Phase 1 — watchdog. Add the next domain's symbols here as each phase lands.
    WATCHDOG_SYMBOLS = [
        "WATCHDOG_GLOBAL_CAP_SECONDS", "WATCHDOG_ORPHAN_GRACE_SECONDS", "TERMINAL_TASK_STATUSES",
        "REAP_REASON_LABEL", "process_alive", "reap_stuck_attempts_internal",
        "notify_owner_of_reaps", "cmd_watchdog_reap_stuck",
    ]

    def test_watchdog_symbols_reexported_from_companyctl(self):
        for sym in self.WATCHDOG_SYMBOLS:
            self.assertTrue(hasattr(companyctl, sym), f"companyctl must re-export {sym}")
            self.assertIs(getattr(companyctl, sym), getattr(watchdog, sym),
                          f"{sym} on companyctl must be the SAME object as in watchdog (facade, not a copy)")

    def test_cli_dispatch_still_wired(self):
        # build_parser binds func=cmd_watchdog_reap_stuck from the companyctl namespace; the facade
        # must keep that name resolvable so `companyctl watchdog reap-stuck` still dispatches.
        parser = companyctl.build_parser()
        args = parser.parse_args(["watchdog", "reap-stuck"])
        self.assertIs(args.func, companyctl.cmd_watchdog_reap_stuck)

    def test_dash_m_entry_uses_single_companyctl_module(self):
        """Under `python -m company_kernel.companyctl`, a split-out domain module's lazy
        `from company_kernel import companyctl` must reuse the __main__ module (aliased), NOT load a
        second divergent copy. Regression for the codex-caught flaw: run the real -m entry through the
        watchdog path and require clean JSON — a second module copy would split globals and misbehave."""
        repo = str(Path(__file__).resolve().parents[1])
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "company_kernel").mkdir(parents=True)
            (Path(d) / "company_kernel" / "schema.sql").write_text(
                (Path(repo) / "company_kernel" / "schema.sql").read_text(encoding="utf-8"), encoding="utf-8")
            env = {**os.environ, "OPENCLAW_COMPANY_KERNEL_ROOT": d,
                   "COMPANY_KERNEL_DB_PATH": str(Path(d) / "company.sqlite")}
            r = subprocess.run([sys.executable, "-m", "company_kernel.companyctl", "watchdog", "reap-stuck"],
                               capture_output=True, text=True, env=env, cwd=repo, timeout=60)
        self.assertEqual(0, r.returncode, r.stderr)
        payload = json.loads(r.stdout)  # raises if the -m path printed a traceback instead of JSON
        self.assertTrue(payload["ok"])
        self.assertIn("reaped_count", payload)


if __name__ == "__main__":
    unittest.main()
