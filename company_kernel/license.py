"""Lightweight offline license check for the private-deployment SKU.

A license key is ``CK1.<base64url(payload_json)>.<hmac_sig>`` where the signature
is HMAC-SHA256 over the payload bytes using a vendor secret. Payload carries at
least ``{"org": "...", "exp": "YYYY-MM-DD"}`` (exp optional = perpetual).

Design choices:
- Default-allow: with no license configured the kernel runs unrestricted, so
  self-hosting and development are never blocked.
- Enforce mode (``COMPANY_KERNEL_LICENSE_ENFORCE=1``) requires a present, valid,
  unexpired key — this is what the paid private-deployment image turns on.
- Verification is offline (no phone-home); the vendor issues keys with the same
  secret used here, set via ``COMPANY_KERNEL_LICENSE_SECRET``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import date, datetime


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def issue_license(payload: dict, secret: str) -> str:
    """Vendor-side: mint a license key from a payload dict and the vendor secret."""
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"CK1.{_b64url_encode(body)}.{sig}"


def parse_license(key: str) -> dict | None:
    try:
        prefix, body_b64, sig = key.strip().split(".", 2)
    except ValueError:
        return None
    if prefix != "CK1":
        return None
    try:
        body = _b64url_decode(body_b64)
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return {"payload": payload, "body": body, "sig": sig}


def verify_license(key: str, secret: str, *, today: date | None = None) -> tuple[bool, dict]:
    """Return (ok, info). info carries reason on failure, payload on success."""
    parsed = parse_license(key)
    if not parsed:
        return False, {"reason": "malformed license key"}
    if not secret:
        return False, {"reason": "no license secret configured to verify against"}
    expected = hmac.new(secret.encode("utf-8"), parsed["body"], hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, parsed["sig"]):
        return False, {"reason": "signature mismatch"}
    payload = parsed["payload"]
    exp = str(payload.get("exp", "") or "").strip()
    if exp:
        try:
            exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
        except ValueError:
            return False, {"reason": f"invalid exp date: {exp}"}
        if (today or date.today()) > exp_date:
            return False, {"reason": f"license expired on {exp}", "payload": payload}
    return True, {"payload": payload}


def license_status(env: dict | None = None, *, today: date | None = None) -> dict:
    """Resolve the runtime license posture from the environment.

    Returns a dict with: enforced (bool), ok (bool), reason/org/exp.
    When not enforced, ok is always True (default-allow)."""
    env = env if env is not None else os.environ
    enforced = str(env.get("COMPANY_KERNEL_LICENSE_ENFORCE", "") or "").strip() in {"1", "true", "yes", "on"}
    key = str(env.get("COMPANY_KERNEL_LICENSE_KEY", "") or "").strip()
    secret = str(env.get("COMPANY_KERNEL_LICENSE_SECRET", "") or "").strip()
    if not enforced:
        return {"enforced": False, "ok": True, "reason": "license enforcement disabled (self-host/dev)"}
    if not key:
        return {"enforced": True, "ok": False, "reason": "COMPANY_KERNEL_LICENSE_KEY not set"}
    ok, info = verify_license(key, secret, today=today)
    payload = info.get("payload", {})
    return {
        "enforced": True,
        "ok": ok,
        "reason": info.get("reason", "valid"),
        "org": payload.get("org", ""),
        "exp": payload.get("exp", ""),
    }
