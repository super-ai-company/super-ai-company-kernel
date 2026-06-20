"""company_kernel.approval — pure approval-classification helpers, with NO dependency on companyctl,
any domain module, the DB (conn/Row), or config. The closed cluster the meeting admitted
(conv-20260620-143738-f678dc): one data constant + three pure functions that classify / parse /
summarize approvals from plain dicts.

companyctl forwards these names with a plain `from .approval import ...` (no wrapper) — that single
line feeds both the external qualified callers (company_dashboard.py uses
companyctl.approval_control_summary) and companyctl's own bare-name calls (normalize_approval calls
approval_detail; the CLI emits approval_control_summary). Nothing in the repo or the suite patches
companyctl.approval_* / HIGH_RISK_APPROVAL_ACTIONS, so no mock-anchor needs to stay behind.

NOTE: policy_guard.py has its OWN independent approval_detail — that is a separate pre-existing
implementation and is deliberately NOT touched by this cut.
"""
from __future__ import annotations

import json

HIGH_RISK_APPROVAL_ACTIONS = {
    "external_send",
    "telegram_send",
    "openclaw_send",
    "rule_change",
    "delete_file",
    "sensitive_file",
    "publish",
    "payment",
    "compensation",
    "salary",
    "penalty",
    "budget_overrun",
    "budget.overrun",
}


def approval_detail(raw: str) -> dict:
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {"reason": raw}
    except json.JSONDecodeError:
        return {"reason": raw}


def approval_is_high_risk(approval: dict) -> bool:
    action = str(approval.get("action") or "")
    detail = approval.get("detail", {}) if isinstance(approval.get("detail", {}), dict) else {}
    risk = str(detail.get("risk") or "").upper()
    return action in HIGH_RISK_APPROVAL_ACTIONS or risk in {"P0", "P1"}


def approval_control_summary(approvals: list[dict]) -> dict:
    by_status: dict[str, int] = {}
    by_action: dict[str, int] = {}
    high_risk_actions: set[str] = set()
    pending_high_risk_actions: set[str] = set()
    real_execution_blockers: dict[str, int] = {}
    dry_run_resolved = 0
    external_send_executed = 0
    for approval in approvals:
        status = str(approval.get("status") or "")
        action = str(approval.get("action") or "")
        safety = approval.get("safety", {}) if isinstance(approval.get("safety", {}), dict) else {}
        by_status[status] = by_status.get(status, 0) + 1
        by_action[action] = by_action.get(action, 0) + 1
        if approval_is_high_risk(approval):
            high_risk_actions.add(action)
            if status == "pending":
                pending_high_risk_actions.add(action)
        if safety.get("dry_run"):
            dry_run_resolved += 1
        if safety.get("external_send_executed"):
            external_send_executed += 1
        if (
            status in {"pending", "requested", "waiting_approval"}
            and action in {"external_send", "telegram_send", "openclaw_send"}
            and not safety.get("external_send_executed")
        ):
            real_execution_blockers["external_send"] = real_execution_blockers.get("external_send", 0) + 1
        if action in {"budget_overrun", "budget.overrun"} and status == "pending":
            real_execution_blockers["budget_overrun"] = real_execution_blockers.get("budget_overrun", 0) + 1
    pending_owner_action_count = sum(
        count for status, count in by_status.items() if status in {"pending", "requested", "waiting_approval"}
    )
    blocked_real_execution_count = sum(real_execution_blockers.values())
    queue_health = "owner_action_required" if pending_owner_action_count or blocked_real_execution_count else "clear"
    if queue_health == "owner_action_required":
        blocker_rows = ", ".join(f"{kind}={count}" for kind, count in sorted(real_execution_blockers.items())) or "no real execution blockers"
        pending_rows = ", ".join(sorted(pending_high_risk_actions)) or "no pending high-risk actions"
        owner_next_action = f"review pending high-risk approvals ({pending_rows}); blocked real execution: {blocker_rows}"
    else:
        owner_next_action = "no pending owner approval actions; monitor queue"
    return {
        "total": len(approvals),
        "by_status": by_status,
        "by_action": by_action,
        "high_risk_actions": sorted(high_risk_actions),
        "pending_high_risk_actions": sorted(pending_high_risk_actions),
        "pending_owner_action_count": pending_owner_action_count,
        "blocked_real_execution_count": blocked_real_execution_count,
        "queue_health": queue_health,
        "owner_next_action": owner_next_action,
        "default_policy": "dry_run_until_owner_approval",
        "dry_run_resolved": dry_run_resolved,
        "external_send_executed": external_send_executed,
        "real_external_send_requires_owner_approval": True,
        "real_execution_blockers": real_execution_blockers,
        "summary": (
            "Pending high-risk approvals block real execution; "
            "historical dry-run/mock-resolved approvals are audit history."
        ),
    }
