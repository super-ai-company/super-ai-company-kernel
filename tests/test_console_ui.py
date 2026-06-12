from __future__ import annotations

import json
import threading
import unittest
import urllib.request
from http import HTTPStatus

from company_kernel import api_gateway


class ConsoleUiTest(unittest.TestCase):
    """Serve the live console and its data endpoints from the API gateway."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = api_gateway.ThreadingHTTPServer(("127.0.0.1", 0), api_gateway.ApiHandler)
        cls.server.quiet = True
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def fetch(self, path: str) -> tuple[int, str, str]:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status, res.headers.get("Content-Type", ""), res.read().decode("utf-8")

    def test_console_served_at_root_and_alias(self) -> None:
        for path in ("/", "/console"):
            status, ctype, body = self.fetch(path)
            self.assertEqual(HTTPStatus.OK, status, path)
            self.assertIn("text/html", ctype, path)
            self.assertIn("Super AI Company", body, path)
            self.assertIn("refreshAll", body, path)

    def test_console_template_exists(self) -> None:
        self.assertTrue(api_gateway.CONSOLE_TEMPLATE.exists(), str(api_gateway.CONSOLE_TEMPLATE))

    def test_events_endpoint_returns_json_list(self) -> None:
        status, ctype, body = self.fetch("/v1/events?limit=5")
        self.assertEqual(HTTPStatus.OK, status)
        self.assertIn("application/json", ctype)
        payload = json.loads(body)
        self.assertTrue(payload.get("ok"))
        self.assertIsInstance(payload.get("events"), list)
        self.assertLessEqual(len(payload["events"]), 5)

    def test_heartbeats_endpoint_returns_json_list(self) -> None:
        status, ctype, body = self.fetch("/v1/heartbeats")
        self.assertEqual(HTTPStatus.OK, status)
        payload = json.loads(body)
        self.assertTrue(payload.get("ok"))
        self.assertIsInstance(payload.get("heartbeats"), list)
        for hb in payload["heartbeats"]:
            self.assertIn("agent_id", hb)
            self.assertIn("last_seen_at", hb)

    def test_descriptor_includes_console_and_new_endpoints(self) -> None:
        paths = {(e["method"], e["path"]) for e in api_gateway.API_ENDPOINTS}
        self.assertIn(("GET", "/v1/events"), paths)
        self.assertIn(("GET", "/v1/heartbeats"), paths)
        self.assertIn(("GET", "/"), paths)


if __name__ == "__main__":
    unittest.main()
