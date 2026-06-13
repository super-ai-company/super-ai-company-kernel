from __future__ import annotations

import importlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class BackupRestoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "company.sqlite"
        self.backups = Path(self.tmp.name) / "backups"
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO t(v) VALUES ('original')")
        conn.commit(); conn.close()
        patcher = mock.patch.dict(os.environ, {
            "COMPANY_KERNEL_DB_PATH": str(self.db),
            "COMPANY_KERNEL_BACKUP_DIR": str(self.backups),
        }, clear=False)
        patcher.start(); self.addCleanup(patcher.stop)
        from company_kernel import backup
        importlib.reload(backup)
        self.backup = backup

    def test_snapshot_creates_valid_copy(self):
        res = self.backup.snapshot(keep=5)
        self.assertTrue(res["ok"])
        self.assertEqual("ok", res["integrity"])
        self.assertTrue(Path(res["snapshot"]).exists())

    def test_prune_keeps_only_n(self):
        for i in range(4):
            self.backup.snapshot(keep=2, label=f"s{i}")
        self.assertLessEqual(len(self.backup.list_snapshots()), 2)

    def test_restore_requires_yes(self):
        snap = self.backup.snapshot()["snapshot"]
        res = self.backup.restore(Path(snap), yes=False)
        self.assertFalse(res["ok"])
        self.assertIn("--yes", res["error"])

    def test_restore_roundtrip_and_pre_backup(self):
        snap = self.backup.snapshot()["snapshot"]
        # mutate live DB after snapshot
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE t SET v='changed'"); conn.commit(); conn.close()
        res = self.backup.restore(Path(snap), yes=True)
        self.assertTrue(res["ok"])
        self.assertTrue(Path(res["pre_restore_backup"]).exists(), "live DB must be backed up before overwrite")
        conn = sqlite3.connect(self.db)
        val = conn.execute("SELECT v FROM t").fetchone()[0]; conn.close()
        self.assertEqual("original", val, "restore must bring back snapshot content")

    def test_restore_rejects_missing_file(self):
        res = self.backup.restore(Path(self.tmp.name) / "nope.sqlite", yes=True)
        self.assertFalse(res["ok"])


if __name__ == "__main__":
    unittest.main()
