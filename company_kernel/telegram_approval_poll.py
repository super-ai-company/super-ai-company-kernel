"""Poll the operator Telegram bot for inline approve/deny taps and resolve approvals.

The approval-required notification (see request_route_approval) ships inline buttons
with callback_data `ck_approve:<id>` / `ck_deny:<id>`. This poller reads callback_query
updates via getUpdates, runs `companyctl approval approve|deny`, then acks the tap and
rewrites the message so the owner sees the outcome. Runs on a short launchd interval.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get("OPENCLAW_COMPANY_KERNEL_ROOT", Path(__file__).resolve().parents[1])).resolve()
COMMS_PATH = ROOT / "config" / "company_communications.json"
STATE_PATH = ROOT / "state" / "telegram_approval_poll.json"
OWNER = os.environ.get("COMPANY_KERNEL_OWNER", "owner")


def emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def resolve_bot_token() -> tuple[str, str]:
    """Return (token, account_id) for the operator-notify telegram account."""
    try:
        comms = json.loads(COMMS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", ""
    notif = comms.get("notification", {}) if isinstance(comms.get("notification"), dict) else {}
    accounts = notif.get("telegram_accounts", {}) if isinstance(notif.get("telegram_accounts"), dict) else {}
    route = (notif.get("routes", {}) or {}).get("approval", {}) if isinstance(notif.get("routes"), dict) else {}
    account_id = route.get("account") or (notif.get("employee_notifications", {}) or {}).get("account") or "employee-notify"
    account = accounts.get(account_id) or {}
    token_env = str(account.get("bot_token_env", "") or "")
    return os.environ.get(token_env, ""), account_id


def tg_api(token: str, method: str, params: dict, timeout: int = 25) -> dict:
    data = urllib.parse.urlencode({k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v) for k, v in params.items()}).encode("utf-8")
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"ok": False, "description": str(exc)}


def load_offset() -> int:
    try:
        return int(json.loads(STATE_PATH.read_text(encoding="utf-8")).get("offset", 0))
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return 0


def save_offset(offset: int) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({"offset": offset}, ensure_ascii=False), encoding="utf-8")


def run_companyctl(args: list[str]) -> tuple[int, str]:
    env = {**os.environ, "OPENCLAW_COMPANY_KERNEL_ROOT": str(ROOT)}
    cp = subprocess.run([str(ROOT / "bin" / "companyctl"), *args], cwd=str(ROOT), text=True, capture_output=True, env=env)
    return cp.returncode, (cp.stdout or cp.stderr)


def resolve_approval(action: str, approval_id: str) -> tuple[bool, str]:
    verb = "approve" if action == "approve" else "deny"
    code, out = run_companyctl(["approval", verb, "--approval-id", approval_id, "--by", OWNER, "--reason", "Telegram 一键" + ("批准" if verb == "approve" else "拒绝")])
    if code == 0:
        return True, ("✅ 已批准" if verb == "approve" else "❌ 已拒绝")
    try:
        err = json.loads(out).get("error", out)
    except json.JSONDecodeError:
        err = out.strip()[:120]
    return False, f"⚠️ 处理失败：{err}"


def process(args: argparse.Namespace) -> int:
    token, account_id = resolve_bot_token()
    if not token:
        emit({"ok": True, "skipped": True, "reason": "operator telegram token not configured", "account": account_id})
        return 0
    offset = load_offset()
    updates = tg_api(token, "getUpdates", {"offset": offset, "timeout": 0, "allowed_updates": ["callback_query"]}, timeout=20)
    if not updates.get("ok"):
        emit({"ok": False, "error": "getUpdates failed", "description": updates.get("description", "")})
        return 1
    results = []
    max_uid = offset - 1
    for upd in updates.get("result", []):
        uid = int(upd.get("update_id", 0))
        max_uid = max(max_uid, uid)
        cb = upd.get("callback_query")
        if not isinstance(cb, dict):
            continue
        data = str(cb.get("data") or "")
        if not (data.startswith("ck_approve:") or data.startswith("ck_deny:")):
            continue
        action = "approve" if data.startswith("ck_approve:") else "deny"
        approval_id = data.split(":", 1)[1]
        ok, label = resolve_approval(action, approval_id)
        msg = cb.get("message", {}) if isinstance(cb.get("message"), dict) else {}
        chat_id = str((msg.get("chat", {}) or {}).get("id", ""))
        message_id = msg.get("message_id")
        # ack the tap (toast on the user's phone)
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cb.get("id", ""), "text": label})
        # rewrite the message: keep the original text, append outcome, drop the buttons
        if chat_id and message_id:
            base = str(msg.get("text") or "")
            who = (cb.get("from", {}) or {}).get("username") or (cb.get("from", {}) or {}).get("first_name") or "owner"
            tg_api(token, "editMessageText", {"chat_id": chat_id, "message_id": message_id,
                                              "text": f"{base}\n\n— {label}（by @{who}）", "reply_markup": {"inline_keyboard": []}})
        results.append({"approval_id": approval_id, "action": action, "ok": ok, "label": label})
    if max_uid >= offset:
        save_offset(max_uid + 1)
    emit({"ok": True, "processed": len(results), "results": results})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Poll operator Telegram bot for approval taps")
    parser.set_defaults(func=process)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
