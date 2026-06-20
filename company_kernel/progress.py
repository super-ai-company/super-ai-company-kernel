"""company_kernel.progress — pure progress-transition notification helpers, with NO dependency on
companyctl, any domain module, the DB (conn), or config. The closed cluster the meeting admitted
(conv-20260620-134026-293b2d): one data constant + three pure functions that turn a progress
transition into a human message / decision dict / dedup fingerprint.

companyctl forwards these names with a plain `from .progress import ...` (no wrapper) — its own
deliver_pending_progress_notifications still calls them through that forward. Nothing in the repo or
the suite patches `companyctl.progress_notification_*` or `companyctl.PROGRESS_TRANSITION_MESSAGES`,
so no mock-anchor needs to stay behind (unlike NotificationDispatcher).

CRITICAL invariant: progress_notification_fingerprint must stay BYTE-IDENTICAL — dedup of repeat
notifications keys on it, so changing the join separator or field order would resurface duplicates.
The guard test pins its literal output for a known transition.
"""
from __future__ import annotations

PROGRESS_TRANSITION_MESSAGES = {
    ("received", "working"): "已开始处理",
    ("working", "waiting"): "需要等待",
    ("waiting", "blocked"): "已卡住",
    ("working", "done"): "已完成",
}


def progress_notification_message(agent: str, from_progress: dict[str, str], to_progress: dict[str, str]) -> str:
    action = PROGRESS_TRANSITION_MESSAGES.get((from_progress.get("layer", ""), to_progress.get("layer", "")))
    summary = str(to_progress.get("summary", "") or "").strip()
    if action and summary:
        return f"{agent} {action}：{summary}"
    if action:
        return f"{agent} {action}"
    if summary:
        return f"{agent} 进度变更：{summary}"
    return f"{agent} 进度从 {from_progress.get('layer', 'unknown')} 变为 {to_progress.get('layer', 'unknown')}"


def progress_notification_decision(agent: str, from_progress: dict[str, str], to_progress: dict[str, str], *, source: str = "heartbeat") -> dict:
    return {
        "kind": "progress_transition",
        "trigger": source,
        "triggered_by": agent,
        "from_layer": from_progress.get("layer", ""),
        "from_state": from_progress.get("state", ""),
        "to_layer": to_progress.get("layer", ""),
        "to_state": to_progress.get("state", ""),
        "should_notify_user": True,
        "reason": f"progress layer changed from {from_progress.get('layer', '')} to {to_progress.get('layer', '')}",
        "message": progress_notification_message(agent, from_progress, to_progress),
        "summary": to_progress.get("summary", ""),
    }


def progress_notification_fingerprint(agent: str, from_progress: dict[str, str], to_progress: dict[str, str], *, task_id: str = "") -> str:
    parts = [
        str(agent or "").strip(),
        str(task_id or "").strip(),
        str(from_progress.get("layer", "") or "").strip(),
        str(to_progress.get("layer", "") or "").strip(),
    ]
    return "|".join(parts)
