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


class CoreLayerBoundaryTest(unittest.TestCase):
    """Phase 0.5 — company_kernel.core is the bottom of the import graph: it must NEVER import
    companyctl or any domain module back (that would re-create the cycle the split exists to kill).
    The time helpers must also stay re-exported from companyctl as the same objects (facade)."""

    def test_core_does_not_import_companyctl(self):
        import ast
        import pathlib
        core_dir = pathlib.Path(__file__).resolve().parents[1] / "company_kernel" / "core"
        offenders = []
        for path in core_dir.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import) and any("companyctl" in a.name for a in node.names):
                    offenders.append((str(path), node.lineno))
                if isinstance(node, ast.ImportFrom) and node.module and "companyctl" in node.module:
                    offenders.append((str(path), node.lineno))
        self.assertEqual([], offenders, "core/ must not import companyctl (dependency inversion)")

    def test_time_helpers_reexported_as_same_objects(self):
        from company_kernel import companyctl, core
        for sym in ("now", "future_seconds", "new_trace_id", "parse_time", "parse_iso_datetime", "seconds_since"):
            self.assertIs(getattr(companyctl, sym), getattr(core, sym),
                          f"companyctl.{sym} must be the SAME object as core.{sym} (facade re-export)")

    def test_db_primitive_rows_reexported_as_same_object(self):
        # First DB cut (gate-shrunk to the one pure leaf): rows() lives in core.db, re-exported by
        # companyctl so all 165 call sites are unchanged. DB_PATH / connect family deliberately stayed.
        from company_kernel import companyctl
        from company_kernel.core import db
        self.assertIs(companyctl.rows, db.rows, "companyctl.rows must be the SAME object as core.db.rows")

    def test_event_group_reexported_as_same_objects(self):
        # Event group cut: record_event/audit/emit/trace_id_for_task live in core.events, re-exported
        # by companyctl (~576 call sites unchanged). trace_id_for_task stays a working mock anchor.
        from company_kernel import companyctl
        from company_kernel.core import events
        for sym in ("record_event", "audit", "emit", "trace_id_for_task"):
            self.assertIs(getattr(companyctl, sym), getattr(events, sym),
                          f"companyctl.{sym} must be the SAME object as core.events.{sym}")

    def test_emit_output_byte_identical(self):
        # emit gained stream=None; production output (→ stdout) must be byte-for-byte the old print.
        import contextlib
        import io
        import json
        from company_kernel import companyctl
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            companyctl.emit({"x": 1, "中": "文"})
        self.assertEqual(json.dumps({"x": 1, "中": "文"}, ensure_ascii=False, indent=2) + "\n", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
