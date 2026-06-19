"""Installing the kernel integration into an agent's own config must be idempotent and non-destructive
(backup, marker-bounded instruction block, JSON merge that preserves other keys)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from company_kernel import integration_installer as ii


class IntegrationInstallerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_json_merge_preserves_other_keys_and_is_idempotent(self):
        p = self.root / "claude.json"
        p.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}), encoding="utf-8")
        r1 = ii._install_mcp_json(p, "company-kernel", dry_run=False)
        self.assertEqual("added", r1)
        data = json.loads(p.read_text(encoding="utf-8"))
        self.assertIn("other", data["mcpServers"])              # existing server preserved
        self.assertEqual("dark", data["theme"])                 # unrelated keys preserved
        self.assertEqual(ii.MCP_BIN, data["mcpServers"]["company-kernel"]["command"])
        r2 = ii._install_mcp_json(p, "company-kernel", dry_run=False)
        self.assertEqual("already-present", r2)                 # idempotent

    def test_toml_append_idempotent(self):
        p = self.root / "config.toml"
        p.write_text("[some.other]\nx = 1\n", encoding="utf-8")
        self.assertEqual("added", ii._install_mcp_toml(p, "company_kernel", dry_run=False))
        self.assertIn("[mcp_servers.company_kernel]", p.read_text(encoding="utf-8"))
        self.assertIn("x = 1", p.read_text(encoding="utf-8"))   # original content kept
        self.assertEqual("already-present", ii._install_mcp_toml(p, "company_kernel", dry_run=False))

    def test_instruction_block_is_marker_bounded_and_updatable(self):
        p = self.root / "AGENTS.md"
        p.write_text("# My existing rules\nkeep me\n", encoding="utf-8")
        self.assertEqual("added", ii._install_instructions(p, "codex-cli", dry_run=False))
        body = p.read_text(encoding="utf-8")
        self.assertIn("keep me", body)                          # existing content preserved
        self.assertIn(ii.INSTR_START, body)
        self.assertIn("codex-cli", body)
        self.assertEqual(1, body.count(ii.INSTR_START))         # exactly one block
        # re-run updates in place, never duplicates
        self.assertIn(ii._install_instructions(p, "codex-cli", dry_run=False), ("already-present", "updated"))
        self.assertEqual(1, p.read_text(encoding="utf-8").count(ii.INSTR_START))

    def test_dry_run_writes_nothing(self):
        p = self.root / "claude.json"
        self.assertEqual("would-add", ii._install_mcp_json(p, "company-kernel", dry_run=True))
        self.assertFalse(p.exists())                            # dry-run created nothing

    def test_structurally_bad_json_is_skipped_not_crashed(self):
        """A syntactically-valid but structurally-wrong config (list top-level, or mcpServers not an
        object) must be left untouched, not crash with AttributeError (codex finding)."""
        p = self.root / "claude.json"
        p.write_text("[]", encoding="utf-8")                    # top-level list
        self.assertEqual("skipped-malformed", ii._install_mcp_json(p, "company-kernel", dry_run=False))
        self.assertEqual("[]", p.read_text(encoding="utf-8"))   # untouched
        p.write_text('{"mcpServers": "oops"}', encoding="utf-8")  # wrong type
        self.assertEqual("skipped-malformed", ii._install_mcp_json(p, "company-kernel", dry_run=False))

    def test_single_marker_file_is_not_duplicated(self):
        """A file with only the START marker (corrupted) must be left alone, not get a 2nd block."""
        p = self.root / "AGENTS.md"
        p.write_text(f"top\n{ii.INSTR_START}\nhalf block, no end\n", encoding="utf-8")
        self.assertEqual("skipped-corrupted-markers", ii._install_instructions(p, "codex-cli", dry_run=False))
        self.assertEqual(1, p.read_text(encoding="utf-8").count(ii.INSTR_START))  # no duplicate appended

    def test_unknown_runtime_is_rejected(self):
        out = ii.install_for_runtime("nonsense-runtime")
        self.assertFalse(out["ok"])
        self.assertIn("no integration profile", out["error"])


if __name__ == "__main__":
    unittest.main()
