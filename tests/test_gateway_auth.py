from __future__ import annotations

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http import HTTPStatus
from unittest import mock

from company_kernel import api_gateway


class GatewayAuthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = api_gateway.ThreadingHTTPServer(("127.0.0.1", 0), api_gateway.ApiHandler)
        cls.server.quiet = True
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close()

    def _get(self, path, token=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        if token is not None:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                return res.status, res.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_auth_disabled_allows_open_access(self):
        with mock.patch.dict(os.environ, {"COMPANY_KERNEL_API_TOKEN": ""}, clear=False):
            status, _ = self._get("/v1/health")
            self.assertEqual(200, status)

    def test_console_shell_never_requires_token(self):
        with mock.patch.dict(os.environ, {"COMPANY_KERNEL_API_TOKEN": "secret123"}, clear=False):
            status, body = self._get("/")
            self.assertEqual(200, status)
            self.assertIn(b"Super AI Company", body)

    def test_data_endpoint_rejects_without_token(self):
        with mock.patch.dict(os.environ, {"COMPANY_KERNEL_API_TOKEN": "secret123"}, clear=False):
            status, _ = self._get("/v1/health")
            self.assertEqual(401, status)

    def test_data_endpoint_rejects_wrong_token(self):
        with mock.patch.dict(os.environ, {"COMPANY_KERNEL_API_TOKEN": "secret123"}, clear=False):
            status, _ = self._get("/v1/health", token="wrong")
            self.assertEqual(401, status)

    def test_data_endpoint_accepts_correct_token(self):
        with mock.patch.dict(os.environ, {"COMPANY_KERNEL_API_TOKEN": "secret123"}, clear=False):
            status, _ = self._get("/v1/health", token="secret123")
            self.assertEqual(200, status)

    def test_request_authorized_helper(self):
        with mock.patch.dict(os.environ, {"COMPANY_KERNEL_API_TOKEN": "abc"}, clear=False):
            self.assertTrue(api_gateway.request_authorized({"Authorization": "Bearer abc"}))
            self.assertFalse(api_gateway.request_authorized({"Authorization": "Bearer x"}))
            self.assertFalse(api_gateway.request_authorized({}))
        with mock.patch.dict(os.environ, {"COMPANY_KERNEL_API_TOKEN": ""}, clear=False):
            self.assertTrue(api_gateway.request_authorized({}))


if __name__ == "__main__":
    unittest.main()
