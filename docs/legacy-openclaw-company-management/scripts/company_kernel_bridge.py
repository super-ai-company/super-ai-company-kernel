#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def default_openclaw_root() -> Path:
    env = os.environ.get("OPENCLAW_ROOT")
    if env:
        return Path(env).expanduser()
    if Path("/Users/shift/openclaw").exists():
        return Path("/Users/shift/openclaw")
    return Path.home() / "openclaw"


def default_company_kernel() -> Path:
    env = os.environ.get("COMPANY_KERNEL_DIR")
    if env:
        return Path(env).expanduser()
    return default_openclaw_root() / "company-kernel"


def default_runtime_alert() -> Path:
    env = os.environ.get("OPENCLAW_COMPANY_RUNTIME_ALERT")
    if env:
        return Path(env).expanduser()
    return default_openclaw_root() / "workspace-xmanx" / "scripts" / "company_runtime_alert.py"


def run_json(cmd: list[str], *, cwd: Path | None, timeout: int) -> tuple[int, dict[str, Any], str]:
    try:
        cp = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except Exception as exc:
        return 127, {}, str(exc)
    try:
        payload = json.loads(cp.stdout or "{}")
    except json.JSONDecodeError as exc:
        return cp.returncode, {}, f"invalid_json:{exc}:stdout={cp.stdout[-1000:]}"
    return cp.returncode, payload, cp.stderr[-2000:]


def compact_health(payload: dict[str, Any], *, command_ok: bool, stderr: str) -> dict[str, Any]:
    heartbeat = payload.get("heartbeat") or {}
    counts = payload.get("counts") or {}
    daemon = payload.get("daemon") or {}
    return {
        "ok": bool(command_ok and payload.get("ok")),
        "source": "company-kernel",
        "employees": int(counts.get("employees") or 0),
        "heartbeats": int(counts.get("heartbeats") or 0),
        "missing_heartbeats": int(heartbeat.get("missing") or 0),
        "stale_heartbeats": int(heartbeat.get("stale") or 0),
        "daemon_ok": bool(daemon.get("ok")),
        "daemon_age_minutes": daemon.get("age_minutes"),
        "issues": list(payload.get("issues") or []),
        "stderr": stderr,
    }


def health(args: argparse.Namespace) -> int:
    kernel_dir = Path(args.company_kernel).expanduser()
    companyctl = kernel_dir / "bin" / "companyctl"
    cmd = [str(companyctl), "doctor", "--summary"]
    if args.strict_launchd:
        cmd.append("--strict-launchd")
    code, payload, stderr = run_json(cmd, cwd=kernel_dir, timeout=args.timeout)
    out = compact_health(payload, command_ok=(code == 0), stderr=stderr)
    if args.full:
        out["raw"] = payload
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["ok"] else 1


def heartbeat_alert(args: argparse.Namespace) -> int:
    alert_script = Path(args.alert_script).expanduser()
    cmd = [sys.executable, str(alert_script), "--json-only"]
    if args.strict_launchd:
        cmd.append("--strict-launchd")
    code, payload, stderr = run_json(cmd, cwd=alert_script.parent, timeout=args.timeout)
    summary = payload.get("summary") or {}
    out = {
        "ok": bool(code == 0 and payload.get("ok") and payload.get("severity") == "ok"),
        "source": "openclaw-company-runtime-alert",
        "severity": payload.get("severity") or "unknown",
        "reasons": list(payload.get("reasons") or []),
        "employee_count": int(summary.get("employee_count") or 0),
        "healthy_recent_count": int(summary.get("healthy_recent_count") or 0),
        "no_heartbeat_count": int(summary.get("no_heartbeat_count") or 0),
        "company_kernel_ok": bool(summary.get("company_kernel_ok")),
        "company_kernel_heartbeats": int(summary.get("company_kernel_heartbeats") or 0),
        "main_down_suspected": bool(summary.get("main_down_suspected")),
        "company_wide_no_heartbeat": bool(summary.get("company_wide_no_heartbeat")),
        "stderr": stderr,
    }
    if args.full:
        out["raw"] = payload
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["ok"] else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Bridge OpenClaw scripts to Company Kernel health and heartbeat alerts.")
    sub = ap.add_subparsers(dest="command", required=True)

    health_ap = sub.add_parser("health", help="Read Company Kernel doctor summary.")
    health_ap.add_argument("--company-kernel", default=str(default_company_kernel()))
    health_ap.add_argument("--timeout", type=int, default=20)
    health_ap.add_argument("--strict-launchd", action="store_true")
    health_ap.add_argument("--full", action="store_true")
    health_ap.set_defaults(func=health)

    alert_ap = sub.add_parser("heartbeat-alert", help="Read OpenClaw runtime alert after Company Kernel suppression logic.")
    alert_ap.add_argument("--alert-script", default=str(default_runtime_alert()))
    alert_ap.add_argument("--timeout", type=int, default=20)
    alert_ap.add_argument("--strict-launchd", action="store_true")
    alert_ap.add_argument("--full", action="store_true")
    alert_ap.set_defaults(func=heartbeat_alert)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
