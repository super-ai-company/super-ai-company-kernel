from __future__ import annotations

import os
import unittest
from unittest import mock

from company_kernel import api_gateway as g


class GatewayBindSafetyTest(unittest.TestCase):
    def test_loopback_detection(self):
        for h in ("127.0.0.1", "::1", "localhost", "127.5.5.5", "LOCALHOST"):
            self.assertTrue(g.is_loopback_host(h), h)
        for h in ("0.0.0.0", "::", "192.168.1.10", "10.0.0.2", ""):
            self.assertFalse(g.is_loopback_host(h), h)

    def test_loopback_bind_never_blocked(self):
        with mock.patch.dict(os.environ, {"COMPANY_KERNEL_API_TOKEN": ""}, clear=False):
            g.assert_safe_bind("127.0.0.1")  # must not raise

    def test_public_bind_without_token_refused(self):
        env = {"COMPANY_KERNEL_API_TOKEN": "", "COMPANY_KERNEL_ALLOW_INSECURE_BIND": ""}
        with mock.patch.dict(os.environ, env, clear=False):
            with self.assertRaises(SystemExit):
                g.assert_safe_bind("0.0.0.0")
            with self.assertRaises(SystemExit):
                g.assert_safe_bind("")  # bind-all is also exposed

    def test_public_bind_with_token_allowed(self):
        with mock.patch.dict(os.environ, {"COMPANY_KERNEL_API_TOKEN": "s3cret"}, clear=False):
            g.assert_safe_bind("0.0.0.0")  # authenticated → fine

    def test_explicit_override_allows_insecure_bind(self):
        env = {"COMPANY_KERNEL_API_TOKEN": "", "COMPANY_KERNEL_ALLOW_INSECURE_BIND": "1"}
        with mock.patch.dict(os.environ, env, clear=False):
            g.assert_safe_bind("0.0.0.0", quiet=True)  # conscious opt-out → fine


if __name__ == "__main__":
    unittest.main()
