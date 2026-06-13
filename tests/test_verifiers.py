from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from company_kernel.verifiers import parse_verifier, verify_result


class ParseVerifierTest(unittest.TestCase):
    def test_default_is_status(self):
        self.assertEqual(("status", ""), parse_verifier("普通任务，无验收声明"))

    def test_parse_kinds(self):
        self.assertEqual(("test", "pytest -q"), parse_verifier("做事\n验收: test: pytest -q"))
        self.assertEqual(("numeric", "101062.00"), parse_verifier("验收：numeric：101062.00"))
        self.assertEqual(("artifact", "dist/r.pdf"), parse_verifier("verify: artifact: dist/r.pdf"))
        self.assertEqual(("human", ""), parse_verifier("验收: human"))


class VerifyResultTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)

    def test_status_trusts_verdict(self):
        self.assertEqual("pass", verify_result("status", "", workspace=self.ws, output_text="", agent_verdict="completed")[0])
        self.assertEqual("fail", verify_result("status", "", workspace=self.ws, output_text="", agent_verdict="blocked")[0])

    def test_human_never_autopasses(self):
        self.assertEqual("needs_human", verify_result("human", "", workspace=self.ws, output_text="done", agent_verdict="completed")[0])

    def test_numeric(self):
        self.assertEqual("pass", verify_result("numeric", "101062.00", workspace=self.ws, output_text="total=101062.00 ok", agent_verdict="completed")[0])
        self.assertEqual("fail", verify_result("numeric", "101062.00", workspace=self.ws, output_text="total=999", agent_verdict="completed")[0])

    def test_artifact(self):
        (self.ws / "report.txt").write_text("hi", encoding="utf-8")
        self.assertEqual("pass", verify_result("artifact", "report.txt", workspace=self.ws, output_text="", agent_verdict="completed")[0])
        self.assertEqual("fail", verify_result("artifact", "missing.txt", workspace=self.ws, output_text="", agent_verdict="completed")[0])
        (self.ws / "empty.txt").write_text("", encoding="utf-8")
        self.assertEqual("fail", verify_result("artifact", "empty.txt", workspace=self.ws, output_text="", agent_verdict="completed")[0])

    def test_test_command_pass_and_fail(self):
        self.assertEqual("pass", verify_result("test", "true", workspace=self.ws, output_text="", agent_verdict="completed")[0])
        self.assertEqual("fail", verify_result("test", "false", workspace=self.ws, output_text="", agent_verdict="completed")[0])

    def test_unknown_kind_errors(self):
        self.assertEqual("error", verify_result("bogus", "", workspace=self.ws, output_text="", agent_verdict="completed")[0])


if __name__ == "__main__":
    unittest.main()
