"""Secrets management: set/get/list/delete round-trip, masking, shell-safe export, env-file
migration, name validation, scope reservation, and doctor checks — all against the portable
file backend so the suite never touches the real OS keychain."""
from __future__ import annotations

import importlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class SecretsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {
            "OPENCLAW_COMPANY_KERNEL_ROOT": str(self.root),
            "COMPANY_KERNEL_SECRETS_BACKEND": "file",   # never hit the real keychain in tests
        }, clear=False)
        patcher.start(); self.addCleanup(patcher.stop)
        from company_kernel import secrets as secrets_mod
        importlib.reload(secrets_mod)
        self.s = secrets_mod

    def _run(self, argv):
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = self.s.main(argv)
        out = buf.getvalue().strip()
        try:
            return code, json.loads(out)
        except json.JSONDecodeError:
            return code, out  # export-env prints plain shell, not JSON

    def test_set_get_list_delete_roundtrip(self):
        self.s.set_secret("TELEGRAM_BOT_TOKEN", "123:abc")
        self.assertEqual("123:abc", self.s.get_secret("TELEGRAM_BOT_TOKEN"))
        self.assertEqual(["TELEGRAM_BOT_TOKEN"], self.s.index_names("default"))
        self.s.delete_secret("TELEGRAM_BOT_TOKEN")
        self.assertIsNone(self.s.get_secret("TELEGRAM_BOT_TOKEN"))
        self.assertEqual([], self.s.index_names("default"))

    def test_store_file_is_chmod_600(self):
        self.s.set_secret("API_KEY", "shh")
        mode = stat.S_IMODE(self.s.store_path().stat().st_mode)
        self.assertEqual(0o600, mode, oct(mode))

    def test_name_validation_rejects_bad_names(self):
        for bad in ("lower", "1LEADS_DIGIT", "has-dash", "has space", ""):
            with self.assertRaises(ValueError):
                self.s.set_secret(bad, "x")

    def test_scope_isolation_reserved(self):
        self.s.set_secret("K", "default-val")
        self.s.set_secret("K", "tenant-val", scope="acme")
        self.assertEqual("default-val", self.s.get_secret("K"))
        self.assertEqual("tenant-val", self.s.get_secret("K", scope="acme"))
        self.assertEqual(["K"], self.s.index_names("acme"))

    def test_export_env_is_shell_escaped(self):
        self.s.set_secret("WEIRD", "a'b c$d")
        lines = self.s.export_env_lines()
        self.assertEqual(["export WEIRD='a'\\''b c$d'"], lines)

    def test_mask_never_reveals_full_value(self):
        self.assertEqual("(empty)", self.s.mask(""))
        self.assertNotIn("supersecretlongtoken", self.s.mask("supersecretlongtoken"))
        self.assertIn("chars", self.s.mask("supersecretlongtoken"))

    def test_migrate_file_imports_env_pairs(self):
        env = self.root / "config" / "secrets.env"
        env.parent.mkdir(parents=True, exist_ok=True)
        env.write_text('# comment\nexport TG_TOKEN="abc"\nLINE_TOKEN=def\nbad-name=skip\n', encoding="utf-8")
        code, payload = self._run(["migrate-file"])
        self.assertEqual(0, code, payload)
        self.assertEqual(["LINE_TOKEN", "TG_TOKEN"], payload["imported"])
        self.assertEqual("abc", self.s.get_secret("TG_TOKEN"))
        self.assertEqual("def", self.s.get_secret("LINE_TOKEN"))

    def test_cli_get_masks_unless_reveal(self):
        self.s.set_secret("SECRET_X", "longvalue12345")
        _, masked = self._run(["get", "--key", "SECRET_X"])
        self.assertNotIn("longvalue12345", json.dumps(masked))
        code, raw = self._run(["get", "--key", "SECRET_X", "--reveal"])
        self.assertEqual(0, code)
        self.assertEqual("longvalue12345", raw)

    def test_doctor_flags_world_readable_and_counts(self):
        self.s.set_secret("A", "1")
        self.s.set_secret("B", "2", scope="acme")
        # make the store world-readable to trigger the perms issue
        self.s.store_path().chmod(0o644)
        with mock.patch.object(self.s, "_git_tracked", return_value=False):
            code, payload = self._run(["doctor"])
        self.assertEqual(1, code, payload)          # issue present → non-zero
        self.assertFalse(payload["ok"])
        self.assertTrue(any("0600" in i for i in payload["issues"]), payload["issues"])
        self.assertEqual({"default": 1, "acme": 1}, payload["secret_counts_by_scope"])

    def test_doctor_clean_when_perms_ok_and_untracked(self):
        self.s.set_secret("A", "1")
        with mock.patch.object(self.s, "_git_tracked", return_value=False):
            code, payload = self._run(["doctor"])
        self.assertEqual(0, code, payload)
        self.assertTrue(payload["ok"])


if __name__ == "__main__":
    unittest.main()
