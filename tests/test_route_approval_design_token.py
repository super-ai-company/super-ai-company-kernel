"""Route-approval classifier: a design token (配色/color token) must NOT trip the secret_change
gate — that false positive was burying frontend tasks in the approval queue. Real secret/auth
token language must still gate.
"""
from __future__ import annotations

import unittest
from unittest import mock

from company_kernel import companyctl


class RouteApprovalDesignTokenTest(unittest.TestCase):
    def setUp(self) -> None:
        # force the default keyword set (the live/test policy.json may blank actions out)
        self._p = mock.patch.object(
            companyctl, "load_policy_config",
            return_value={"route_approval": {"actions": companyctl.DEFAULT_ROUTE_APPROVAL_ACTIONS}},
        )
        self._p.start()

    def tearDown(self) -> None:
        self._p.stop()

    def detect(self, title, desc=""):
        return companyctl.detect_route_approval_action(title, desc)

    def test_design_token_does_not_gate_as_secret(self) -> None:
        self.assertEqual("", self.detect("Android 红金主题复核", "本任务只确保配色 token 与设计源一致。"))
        self.assertEqual("", self.detect("KDS 黑金暗色", "按组件库基准统一 color token 与 design token。"))

    def test_real_secret_token_still_gates(self) -> None:
        self.assertEqual("secret_change", self.detect("轮换 API token", "更新后端 auth token 与凭据"))
        self.assertEqual("secret_change", self.detect("更新密钥", "rotate the secret"))
        self.assertEqual("secret_change", self.detect("改密码", "重置 admin password"))

    def test_other_gates_unaffected(self) -> None:
        self.assertEqual("payment", self.detect("支付回调对账", "处理支付流程"))
        self.assertEqual("external_send", self.detect("发布客户通知", "外发给客户"))

    def test_mixed_design_and_real_token_still_gates(self) -> None:
        # if a task touches BOTH a design token and a real auth token, gate it (safe side)
        self.assertEqual("secret_change", self.detect("主题改造", "更新配色 token,并轮换 auth token"))


if __name__ == "__main__":
    unittest.main()
