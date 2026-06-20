"""company_kernel.notify — the notification SEND primitives, with NO dependency on companyctl, any
domain module, the DB (connect/conn), or the config globals (COMMUNICATIONS_PATH / load_communication_
config). Every function is pure transport: it takes the token / chat_id / webhook / settings it needs
as arguments and only talks to the OS notifier or an HTTP endpoint.

Notify-domain first cut (codex-guided subset, batched): the self-contained send cluster —
resolve_notification_target / applescript_quote / send_macos_notification / send_telegram_notification
/ send_slack_webhook / NotificationDispatcher. They depend only on stdlib (os/json/subprocess/urllib)
and each other, so this is a plain leaf module (no lazy-import workaround). companyctl re-exports them,
so every existing `companyctl.send_telegram_notification(...)` caller is unchanged.

NotificationDispatcher deliberately STAYS in companyctl, not here: its methods call the senders by
bare name, and the suite patches `mock.patch.object(companyctl, "send_telegram_notification", ...)` to
intercept them — that patch only reaches a caller whose name lookup happens in companyctl's namespace.
Likewise the config-entangled trio stays in companyctl: notification_settings /
update_notification_settings / notification_send_result (they read load_communication_config and
assemble routing, and call back into these re-exported senders through the companyctl namespace).
"""
from __future__ import annotations

import json
import subprocess
import urllib.parse
import urllib.request


def resolve_notification_target(target: str) -> tuple[str, str]:
    raw = str(target or "").strip()
    if raw in {"macos", "macos:", "local", "local:"}:
        return "macos", "default"
    if raw.startswith("macos:"):
        return "macos", raw.split(":", 1)[1].strip() or "default"
    if raw.startswith("telegram:"):
        chat_id = raw.split(":", 1)[1].strip()
        if not chat_id:
            raise ValueError("telegram target chat id is required")
        return "telegram", chat_id
    if raw.startswith("slack:"):
        webhook_id = raw.split(":", 1)[1].strip()
        if not webhook_id:
            raise ValueError("slack webhook id is required")
        return "slack", webhook_id
    if raw:
        return "telegram", raw
    raise ValueError("notification target is required")


def applescript_quote(value: str) -> str:
    return '"' + str(value or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def send_macos_notification(*, text: str, title: str = "Company Kernel", subtitle: str = "") -> dict:
    command = ["osascript", "-e"]
    script = f"display notification {applescript_quote(text)} with title {applescript_quote(title)}"
    if subtitle:
        script += f" subtitle {applescript_quote(subtitle)}"
    subprocess.run(command + [script], check=True, capture_output=True, text=True, timeout=10)
    return {"ok": True, "platform": "macos", "message_id": "osascript"}


def send_telegram_notification(*, token: str, chat_id: str, text: str, timeout: int = 20, reply_markup: dict | None = None) -> dict:
    if not token:
        raise ValueError("telegram bot token is not configured")
    if not chat_id:
        raise ValueError("telegram chat id is required")
    fields = {"chat_id": chat_id, "text": text}
    if reply_markup:
        fields["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    payload = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=payload, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not data.get("ok"):
        raise ValueError(str(data.get("description") or "telegram send failed"))
    result = data.get("result", {}) if isinstance(data.get("result"), dict) else {}
    return {
        "ok": True,
        "platform": "telegram",
        "chat_id": str(result.get("chat", {}).get("id", chat_id) if isinstance(result.get("chat"), dict) else chat_id),
        "message_id": result.get("message_id", ""),
    }


def send_slack_webhook(webhook_url: str, payload: dict, timeout: int = 20) -> dict:
    if not webhook_url:
        raise ValueError("slack webhook url is not configured")
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(webhook_url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")
    return {"ok": True, "platform": "slack", "message_id": body or "ok"}
