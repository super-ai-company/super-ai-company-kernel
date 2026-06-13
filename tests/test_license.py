from __future__ import annotations

import unittest
from datetime import date

from company_kernel import license as lic


SECRET = "vendor-secret-123"


class LicenseTest(unittest.TestCase):
    def test_issue_and_verify_roundtrip(self):
        key = lic.issue_license({"org": "Acme", "exp": "2099-12-31"}, SECRET)
        ok, info = lic.verify_license(key, SECRET)
        self.assertTrue(ok)
        self.assertEqual("Acme", info["payload"]["org"])

    def test_tampered_signature_rejected(self):
        key = lic.issue_license({"org": "Acme"}, SECRET)
        ok, info = lic.verify_license(key[:-1] + ("0" if key[-1] != "0" else "1"), SECRET)
        self.assertFalse(ok)
        self.assertIn("signature", info["reason"])

    def test_wrong_secret_rejected(self):
        key = lic.issue_license({"org": "Acme"}, SECRET)
        ok, _ = lic.verify_license(key, "other-secret")
        self.assertFalse(ok)

    def test_expired_rejected(self):
        key = lic.issue_license({"org": "Acme", "exp": "2020-01-01"}, SECRET)
        ok, info = lic.verify_license(key, SECRET, today=date(2026, 6, 13))
        self.assertFalse(ok)
        self.assertIn("expired", info["reason"])

    def test_not_yet_expired_ok(self):
        key = lic.issue_license({"org": "Acme", "exp": "2026-12-31"}, SECRET)
        ok, _ = lic.verify_license(key, SECRET, today=date(2026, 6, 13))
        self.assertTrue(ok)

    def test_malformed_key(self):
        ok, info = lic.verify_license("not-a-license", SECRET)
        self.assertFalse(ok)
        self.assertIn("malformed", info["reason"])

    def test_default_allow_when_not_enforced(self):
        st = lic.license_status(env={})
        self.assertFalse(st["enforced"])
        self.assertTrue(st["ok"])

    def test_enforced_without_key_fails(self):
        st = lic.license_status(env={"COMPANY_KERNEL_LICENSE_ENFORCE": "1"})
        self.assertTrue(st["enforced"])
        self.assertFalse(st["ok"])

    def test_enforced_with_valid_key_ok(self):
        key = lic.issue_license({"org": "Acme", "exp": "2099-01-01"}, SECRET)
        st = lic.license_status(env={
            "COMPANY_KERNEL_LICENSE_ENFORCE": "1",
            "COMPANY_KERNEL_LICENSE_KEY": key,
            "COMPANY_KERNEL_LICENSE_SECRET": SECRET,
        })
        self.assertTrue(st["ok"])
        self.assertEqual("Acme", st["org"])


if __name__ == "__main__":
    unittest.main()
