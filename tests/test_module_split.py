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
from unittest import mock

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


class ConfigLayerCutTest(unittest.TestCase):
    """Config-layer first cut: the three pure JSON loaders live in core.config and take an explicit
    path; companyctl keeps each name as a THIN wrapper that resolves the path and delegates. Unlike the
    same-object re-exports above, these are wrappers — so the guard is: (a) core.config exposes the pure
    readers, (b) the wrappers still resolve through the path globals so `mock.patch.object` anchors hit,
    (c) core.config carries no path globals / no reverse import."""

    def test_core_config_exposes_pure_readers(self):
        from company_kernel.core import config as core_config
        for sym in ("load_global_config", "load_communication_config", "load_pricing_config"):
            self.assertTrue(callable(getattr(core_config, sym, None)), f"core.config must expose {sym}")

    def test_pure_reader_missing_path_uses_original_fallbacks(self):
        from company_kernel.core import config as core_config
        missing = Path(tempfile.gettempdir()) / "ck-does-not-exist-zzz.json"
        self.assertEqual({}, core_config.load_global_config(missing))
        self.assertEqual({}, core_config.load_pricing_config(missing))
        # comms keeps its distinct open-policy default (NOT {}), preserved verbatim from the old body
        self.assertEqual({"policy": {"mode": "open"}, "aliases": {}, "employees": {}, "channels": {}},
                         core_config.load_communication_config(missing))

    def test_comms_wrapper_still_honors_path_global_anchor(self):
        # The COMMUNICATIONS_PATH mock anchor must still steer load_communication_config through the
        # wrapper — proving the path global stayed on companyctl and the wrapper reads it live.
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "comms.json"
            fake.write_text(json.dumps({"policy": {"mode": "locked"}, "marker": "anchor-hit"}), encoding="utf-8")
            with mock.patch.object(companyctl, "COMMUNICATIONS_PATH", fake):
                self.assertEqual("anchor-hit", companyctl.load_communication_config().get("marker"))

    def test_config_loaders_not_in_companyctl_body_anymore(self):
        # The JSON-parsing body moved out: companyctl's wrappers must delegate to core.config, so the
        # raw `json.loads(... .read_text(...))` parse no longer lives in the wrapper source.
        import inspect
        src = inspect.getsource(companyctl.load_communication_config)
        self.assertIn("_core_config", src)
        self.assertNotIn("read_text", src)


class NotifyCutTest(unittest.TestCase):
    """Notify-domain cut: the pure send cluster moved to company_kernel.notify and is re-exported from
    companyctl as the SAME objects (165+ call sites unchanged). The config-entangled trio
    (notification_settings / update_notification_settings / notification_send_result) stays on
    companyctl, and notify.py must NOT reverse-import companyctl."""

    NOTIFY_SYMBOLS = [
        "resolve_notification_target", "applescript_quote", "send_macos_notification",
        "send_telegram_notification", "send_slack_webhook",
    ]

    def test_notify_symbols_reexported_as_same_objects(self):
        from company_kernel import companyctl, notify
        for sym in self.NOTIFY_SYMBOLS:
            self.assertTrue(hasattr(companyctl, sym), f"companyctl must re-export {sym}")
            self.assertIs(getattr(companyctl, sym), getattr(notify, sym),
                          f"{sym} on companyctl must be the SAME object as in notify (facade, not a copy)")

    def test_dispatcher_and_config_trio_stay_on_companyctl(self):
        # NotificationDispatcher + the config trio must NOT move: they call senders by bare name and the
        # suite patches companyctl.send_* — a patch that only reaches lookups in companyctl's namespace.
        from company_kernel import companyctl, notify
        for sym in ("NotificationDispatcher", "notification_settings",
                    "update_notification_settings", "notification_send_result"):
            self.assertTrue(hasattr(companyctl, sym), f"companyctl must keep {sym}")
            self.assertFalse(hasattr(notify, sym), f"{sym} stays in companyctl, must NOT be in notify")

    def test_notify_does_not_reverse_import_companyctl(self):
        import ast
        import pathlib
        path = pathlib.Path(__file__).resolve().parents[1] / "company_kernel" / "notify.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any("companyctl" in a.name for a in node.names):
                offenders.append(node.lineno)
            if isinstance(node, ast.ImportFrom) and node.module and "companyctl" in node.module:
                offenders.append(node.lineno)
        self.assertEqual([], offenders, "notify.py must not import companyctl (leaf module)")


class ProgressCutTest(unittest.TestCase):
    """Progress cut: the pure transition helpers + their data constant moved to company_kernel.progress
    and are forwarded from companyctl as the SAME objects. The fingerprint is the dedup key, so its
    byte output is pinned here — a separator/field-order drift would resurface duplicate notifications."""

    PROGRESS_SYMBOLS = [
        "PROGRESS_TRANSITION_MESSAGES", "progress_notification_message",
        "progress_notification_decision", "progress_notification_fingerprint",
    ]

    def test_progress_symbols_forwarded_as_same_objects(self):
        from company_kernel import companyctl, progress
        for sym in self.PROGRESS_SYMBOLS:
            self.assertTrue(hasattr(companyctl, sym), f"companyctl must forward {sym}")
            self.assertIs(getattr(companyctl, sym), getattr(progress, sym),
                          f"{sym} on companyctl must be the SAME object as in progress (forward, not a copy)")

    def test_fingerprint_byte_identical_golden(self):
        # Golden: dedup keys on this exact string. Pin it so a refactor can't silently change the shape.
        from company_kernel import progress
        fp = progress.progress_notification_fingerprint(
            "codex", {"layer": "working"}, {"layer": "done"}, task_id="t1")
        self.assertEqual("codex|t1|working|done", fp)

    def test_progress_does_not_reverse_import_companyctl(self):
        import ast
        import pathlib
        path = pathlib.Path(__file__).resolve().parents[1] / "company_kernel" / "progress.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any("companyctl" in a.name for a in node.names):
                offenders.append(node.lineno)
            if isinstance(node, ast.ImportFrom) and node.module and "companyctl" in node.module:
                offenders.append(node.lineno)
        self.assertEqual([], offenders, "progress.py must not import companyctl (leaf module)")


class EconomicsCutTest(unittest.TestCase):
    """Economics pure cut: the two pure estimators moved to company_kernel.economics and are forwarded
    from companyctl. Per the meeting, behaviour equivalence is asserted by the existing tests/
    test_economics.py through the companyctl public entry (so a forward breakage fails loudly there);
    here we guard the forward wiring + leaf purity. The compute_* aggregators stay this batch."""

    def test_economics_estimators_forwarded_and_callable_via_companyctl(self):
        from company_kernel import companyctl, economics
        for sym in ("classify_task_type", "estimate_task_cost"):
            self.assertTrue(hasattr(companyctl, sym), f"companyctl must forward {sym}")
            self.assertIs(getattr(companyctl, sym), getattr(economics, sym))
        # call through the companyctl public entry — what real callers and test_economics.py use
        self.assertEqual("default", companyctl.classify_task_type("x", "y", {}))
        self.assertEqual(2.5, companyctl.estimate_task_cost({"amount": 2.5}, {}))

    def test_aggregators_stay_on_companyctl_this_batch(self):
        from company_kernel import companyctl, economics
        for sym in ("compute_economics", "compute_cost_dashboard"):
            self.assertTrue(hasattr(companyctl, sym), f"companyctl must keep {sym} this batch")
            self.assertFalse(hasattr(economics, sym), f"{sym} is conn-coupled; deferred to next batch")

    def test_economics_does_not_reverse_import_companyctl(self):
        import ast
        import pathlib
        path = pathlib.Path(__file__).resolve().parents[1] / "company_kernel" / "economics.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any("companyctl" in a.name for a in node.names):
                offenders.append(node.lineno)
            if isinstance(node, ast.ImportFrom) and node.module and "companyctl" in node.module:
                offenders.append(node.lineno)
        self.assertEqual([], offenders, "economics.py must not import companyctl (leaf module)")


class DashboardCoreCutTest(unittest.TestCase):
    """Dashboard pure-core cut (A): build_cost_dashboard moved to economics.py with every DB/clock/
    config/constant dep hoisted to args. Golden-pinned, canonical-JSON, covering all three preserved
    quirks: by_day counts the full ledger (owner event included) while totals count only the filtered
    employees; an age of inf or None renders null + off-duty; on_duty_free counts on-duty zero-cost."""

    GOLDEN = ('{"by_day":[{"cost":0.12,"day":"2026-06-18","executions":1},{"cost":2.5,"day":'
              '"2026-06-19","executions":1},{"cost":5.018,"day":"2026-06-20","executions":2}],'
              '"by_employee":[{"cost":2.518,"employee_id":"codex","executions":2,'
              '"heartbeat_age_minutes":5.0,"on_duty":true,"status":"active","tokens":2000},'
              '{"cost":0.12,"employee_id":"gemini","executions":1,"heartbeat_age_minutes":60.0,'
              '"on_duty":false,"status":"active","tokens":0},{"cost":0.0,"employee_id":"ghost",'
              '"executions":0,"heartbeat_age_minutes":null,"on_duty":false,"status":"active",'
              '"tokens":0},{"cost":0.0,"employee_id":"idle","executions":0,'
              '"heartbeat_age_minutes":2.0,"on_duty":true,"status":"active","tokens":0},'
              '{"cost":0.0,"employee_id":"never","executions":0,"heartbeat_age_minutes":null,'
              '"on_duty":false,"status":"active","tokens":0}],"currency":"USD","note":'
              '"在岗=心跳15分钟内仍活跃(内部通信/查任务0花费);cost=budget_events 估算'
              '(amount>token>runtime);只有接单执行才计费。","totals":{"cost":2.638,'
              '"employees":5,"executions":3,"on_duty":2,"on_duty_free":1}}')

    def _inputs(self):
        ledger = [
            {"employee_id": "codex", "amount": 2.5, "token_input": 0, "token_output": 0, "runtime_seconds": 0, "day": "2026-06-19"},
            {"employee_id": "codex", "amount": 0, "token_input": 1000, "token_output": 1000, "runtime_seconds": 0, "day": "2026-06-20"},
            {"employee_id": "gemini", "amount": 0, "token_input": 0, "token_output": 0, "runtime_seconds": 120, "day": "2026-06-18"},
            {"employee_id": "owner", "amount": 5.0, "token_input": 0, "token_output": 0, "runtime_seconds": 0, "day": "2026-06-20"},
        ]
        employee_rows = [{"id": "codex", "status": "active"}, {"id": "gemini", "status": "active"},
                         {"id": "idle", "status": "active"}, {"id": "ghost", "status": "active"},
                         {"id": "never", "status": "active"}]
        heartbeat_ages = {"codex": 5.0, "gemini": 60.0, "idle": 2.0, "ghost": float("inf"), "never": None}
        pricing = {"cost_rates": {"token_input_per_1k": 0.003, "token_output_per_1k": 0.015,
                                  "runtime_per_minute": 0.06}, "currency": "USD"}
        return ledger, employee_rows, heartbeat_ages, pricing

    def test_build_cost_dashboard_golden_canonical_json(self):
        from company_kernel import economics
        out = economics.build_cost_dashboard(*self._inputs(), off_duty_threshold=15, days=14)
        text = json.dumps(out, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))
        self.assertEqual(self.GOLDEN, text)

    def test_build_cost_dashboard_forwarded_and_pure(self):
        import ast
        import pathlib
        from company_kernel import companyctl, economics
        self.assertIs(companyctl.build_cost_dashboard, economics.build_cost_dashboard)
        # the shell that used to hold the logic now keeps only the loaders + a delegating call
        self.assertTrue(hasattr(companyctl, "load_heartbeat_ages"))
        path = pathlib.Path(__file__).resolve().parents[1] / "company_kernel" / "economics.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders = [n.lineno for n in ast.walk(tree)
                     if (isinstance(n, ast.Import) and any("companyctl" in a.name for a in n.names))
                     or (isinstance(n, ast.ImportFrom) and n.module and "companyctl" in n.module)]
        self.assertEqual([], offenders, "economics.py must stay a leaf (no companyctl import)")


class BuildEconomicsCutTest(unittest.TestCase):
    """Economics build-core cut B: build_economics moved to economics.py (pure bucket aggregation),
    compute_economics is now the shell. Golden-pinned canonical-JSON covering the two-level price
    fallback, the margin_pct denominator-0 → 0.0 branch (a zero-revenue bucket), and the empty case."""

    GOLDEN_BUCKETS = ('{"by_task_type":[{"cost":2.5,"count":1,"margin":7.5,"margin_pct":75.0,'
                      '"revenue":10.0,"task_type":"code_fix"},{"cost":0.0,"count":1,"margin":0.0,'
                      '"margin_pct":0.0,"revenue":0.0,"task_type":"default"}],"currency":"USD",'
                      '"note":"revenue=按 config/pricing.json 结果价；cost=budget_events 估算'
                      '（amount>token>runtime 兜底）。","totals":{"completed_tasks":2,"cost":2.5,'
                      '"margin":7.5,"margin_pct":75.0,"revenue":10.0}}')
    GOLDEN_EMPTY = ('{"by_task_type":[],"currency":"USD","note":"revenue=按 config/pricing.json 结果价'
                    '；cost=budget_events 估算（amount>token>runtime 兜底）。","totals":'
                    '{"completed_tasks":0,"cost":0,"margin":0,"margin_pct":0.0,"revenue":0}}')

    PRICING = {"result_prices": {"code_fix": 10},
               "cost_rates": {"token_input_per_1k": 0.003, "token_output_per_1k": 0.015, "runtime_per_minute": 0.06},
               "currency": "USD", "task_type_keywords": {"code_fix": ["fix", "修复"]}}

    @staticmethod
    def _canon(obj):
        return json.dumps(obj, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))

    def test_build_economics_golden_with_zero_revenue_bucket(self):
        from company_kernel import economics
        task_rows = [{"id": "t1", "title": "fix", "description": "修复"},
                     {"id": "t2", "title": "draft", "description": "x"}]
        cost_by_task = {"t1": [{"amount": 2.5, "token_input": 0, "token_output": 0, "runtime_seconds": 0}]}
        out = economics.build_economics(task_rows, cost_by_task, self.PRICING)
        self.assertEqual(self.GOLDEN_BUCKETS, self._canon(out))

    def test_build_economics_golden_empty(self):
        from company_kernel import economics
        self.assertEqual(self.GOLDEN_EMPTY, self._canon(economics.build_economics([], {}, self.PRICING)))

    def test_build_economics_forwarded_same_object(self):
        from company_kernel import companyctl, economics
        self.assertIs(companyctl.build_economics, economics.build_economics)


class ApprovalCutTest(unittest.TestCase):
    """Approval pure cut: the classification helpers + their constant moved to company_kernel.approval
    and are forwarded from companyctl as the SAME objects. No test patches these, so a plain forward is
    safe; policy_guard.py keeps its own independent approval_detail (out of scope, must NOT be touched)."""

    APPROVAL_SYMBOLS = [
        "HIGH_RISK_APPROVAL_ACTIONS", "approval_detail", "approval_is_high_risk", "approval_control_summary",
    ]

    def test_approval_symbols_forwarded_as_same_objects(self):
        from company_kernel import approval, companyctl
        for sym in self.APPROVAL_SYMBOLS:
            self.assertTrue(hasattr(companyctl, sym), f"companyctl must forward {sym}")
            self.assertIs(getattr(companyctl, sym), getattr(approval, sym),
                          f"{sym} on companyctl must be the SAME object as in approval (forward, not a copy)")

    def test_approval_classification_behaviour(self):
        from company_kernel import companyctl
        self.assertTrue(companyctl.approval_is_high_risk({"action": "payment"}))
        self.assertTrue(companyctl.approval_is_high_risk({"action": "x", "detail": {"risk": "p0"}}))
        self.assertFalse(companyctl.approval_is_high_risk({"action": "note"}))
        self.assertEqual({"reason": "oops"}, companyctl.approval_detail("oops"))  # non-JSON → reason
        summary = companyctl.approval_control_summary([{"status": "pending", "action": "payment"}])
        self.assertEqual("owner_action_required", summary["queue_health"])
        self.assertEqual(["payment"], summary["pending_high_risk_actions"])

    def test_approval_does_not_reverse_import_companyctl(self):
        import ast
        import pathlib
        path = pathlib.Path(__file__).resolve().parents[1] / "company_kernel" / "approval.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders = [n.lineno for n in ast.walk(tree)
                     if (isinstance(n, ast.Import) and any("companyctl" in a.name for a in n.names))
                     or (isinstance(n, ast.ImportFrom) and n.module and "companyctl" in n.module)]
        self.assertEqual([], offenders, "approval.py must not import companyctl (leaf module)")


class ParsingSweepBatch1Test(unittest.TestCase):
    """Pure-leaf sweep batch 1: the parse_* / OpenClaw native-result extractors moved to
    company_kernel.parsing and are forwarded from companyctl as the SAME objects. parse_json_output
    rode along as parse_openclaw_agent_reply's dependency so parsing.py stays a clean leaf."""

    PARSING_SYMBOLS = [
        "parse_json_arg", "parse_json_output", "parse_openclaw_agent_reply",
        "_openclaw_native_result_task_id", "_openclaw_native_result_agent",
        "_openclaw_native_result_summary", "_openclaw_native_result_evidence",
    ]

    def test_parsing_symbols_forwarded_as_same_objects(self):
        from company_kernel import companyctl, parsing
        for sym in self.PARSING_SYMBOLS:
            self.assertTrue(hasattr(companyctl, sym), f"companyctl must forward {sym}")
            self.assertIs(getattr(companyctl, sym), getattr(parsing, sym),
                          f"{sym} on companyctl must be the SAME object as in parsing")

    def test_parsing_behaviour_preserved(self):
        from company_kernel import companyctl
        self.assertEqual({"a": 1}, companyctl.parse_json_arg('{"a": 1}', None))
        self.assertEqual("def", companyctl.parse_json_arg("", "def"))
        self.assertEqual({"raw": "x"}, companyctl.parse_json_output("x"))  # bad JSON → {"raw": ...}
        self.assertEqual("t1", companyctl._openclaw_native_result_task_id({"task_id": "t1"}))

    def test_parsing_is_leaf(self):
        import ast
        import pathlib
        path = pathlib.Path(__file__).resolve().parents[1] / "company_kernel" / "parsing.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders = [n.lineno for n in ast.walk(tree)
                     if (isinstance(n, ast.Import) and any("companyctl" in a.name for a in n.names))
                     or (isinstance(n, ast.ImportFrom) and n.module and "companyctl" in n.module)]
        self.assertEqual([], offenders, "parsing.py must not import companyctl (leaf module)")


if __name__ == "__main__":
    unittest.main()
