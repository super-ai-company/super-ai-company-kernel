from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from company_kernel import companyctl


class DatabaseIntegrityTest(unittest.TestCase):
    def test_fresh_db_reports_ok(self):
        with tempfile.TemporaryDirectory() as d:
            live = Path(d) / "company.sqlite"
            with mock.patch.object(companyctl, "DB_PATH", live):
                conn = companyctl.connect()
                try:
                    integ = companyctl.database_integrity(conn)
                finally:
                    conn.close()
        self.assertTrue(integ["ok"], integ)
        self.assertEqual(["ok"], integ["integrity"])
        self.assertEqual(0, integ["foreign_key_violations"])

    def test_corrupt_db_reports_not_ok(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "bad.sqlite"
            bad.write_bytes(b"definitely not sqlite")
            conn = sqlite3.connect(str(bad))
            try:
                integ = companyctl.database_integrity(conn)
            finally:
                conn.close()
        self.assertFalse(integ["ok"], integ)


if __name__ == "__main__":
    unittest.main()
