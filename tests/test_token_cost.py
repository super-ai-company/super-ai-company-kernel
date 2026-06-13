from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from company_kernel import codex_adapter


class ParseTokenUsageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.events = Path(self.tmp.name) / "codex-events.jsonl"

    def write(self, lines):
        self.events.write_text("\n".join(json.dumps(o) for o in lines) + "\n", encoding="utf-8")

    def test_missing_file_returns_zero(self):
        self.assertEqual((0, 0), codex_adapter.parse_token_usage(self.events))

    def test_flat_token_count_event(self):
        self.write([
            {"type": "message", "text": "working"},
            {"type": "token_count", "input_tokens": 1200, "output_tokens": 340},
        ])
        self.assertEqual((1200, 340), codex_adapter.parse_token_usage(self.events))

    def test_nested_total_token_usage(self):
        self.write([
            {"msg": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 5000, "output_tokens": 800}}}},
        ])
        self.assertEqual((5000, 800), codex_adapter.parse_token_usage(self.events))

    def test_openai_usage_prompt_completion(self):
        self.write([
            {"usage": {"prompt_tokens": 900, "completion_tokens": 150}},
        ])
        self.assertEqual((900, 150), codex_adapter.parse_token_usage(self.events))

    def test_takes_max_of_cumulative_events(self):
        # codex emits cumulative counts; the run total is the maximum, not the sum
        self.write([
            {"type": "token_count", "input_tokens": 1000, "output_tokens": 100},
            {"type": "token_count", "input_tokens": 2500, "output_tokens": 400},
            {"type": "token_count", "input_tokens": 2500, "output_tokens": 600},
        ])
        self.assertEqual((2500, 600), codex_adapter.parse_token_usage(self.events))

    def test_non_json_lines_ignored(self):
        self.events.write_text("not json at all\n{bad}\n" + json.dumps({"input_tokens": 10, "output_tokens": 20}) + "\n", encoding="utf-8")
        self.assertEqual((10, 20), codex_adapter.parse_token_usage(self.events))


class RunCostTest(unittest.TestCase):
    def test_token_cost_preferred_when_present(self):
        # rates from config/pricing.json: 0.003/1k in, 0.015/1k out
        cost = codex_adapter.run_cost(1000, 1000, 600)
        self.assertAlmostEqual(0.018, cost, places=6)

    def test_runtime_fallback_when_no_tokens(self):
        # 120s = 2min; runtime_per_minute default 0.05 -> 0.10
        cost = codex_adapter.run_cost(0, 0, 120)
        self.assertGreater(cost, 0)


if __name__ == "__main__":
    unittest.main()
