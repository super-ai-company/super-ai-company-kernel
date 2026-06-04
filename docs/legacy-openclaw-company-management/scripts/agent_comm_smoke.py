#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(os.environ.get("OPENCLAW_ROOT", "/Users/shift/openclaw" if Path("/Users/shift/openclaw").exists() else Path.home() / "openclaw")).expanduser()
SCRIPT_WORKSPACE = Path(__file__).resolve().parents[1]


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def run(cmd: list[str], timeout: int) -> dict[str, Any]:
    try:
        cp = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
        return {
            "ok": cp.returncode == 0,
            "exit_code": cp.returncode,
            "stdout_full": cp.stdout,
            "stdout": cp.stdout[-12000:],
            "stderr": cp.stderr[-4000:],
        }
    except Exception as exc:
        return {"ok": False, "exit_code": 127, "stdout_full": "", "stdout": "", "stderr": str(exc)}


def http_get(url: str, timeout: int) -> dict[str, Any]:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(500).decode("utf-8", "replace")
            return {"ok": 200 <= resp.status < 300, "status": resp.status, "body": body}
    except urllib.error.HTTPError as exc:
        body = exc.read(500).decode("utf-8", "replace")
        return {"ok": False, "status": exc.code, "body": body}
    except Exception as exc:
        return {"ok": False, "status": 0, "body": str(exc)}


def parse_agent_text(stdout: str) -> str:
    try:
        parsed = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return ""
    payloads = (((parsed.get("result") or {}).get("payloads")) or [])
    for item in payloads:
        if isinstance(item, dict) and str(item.get("text") or "").strip():
            return str(item["text"]).strip()
    for key in ("finalAssistantVisibleText", "finalAssistantRawText"):
        text = str((((parsed.get("result") or {}).get("meta") or {}).get(key)) or "").strip()
        if text:
            return text
    return ""


def gateway_probe(timeout: int) -> dict[str, Any]:
    res = run(["openclaw", "gateway", "probe"], timeout)
    return {"ok": res["ok"] and "Connect: ok" in res["stdout"], **res}


def attendance_probe(args: argparse.Namespace) -> dict[str, Any]:
    script = SCRIPT_WORKSPACE / "scripts" / "attendance_sweep.py"
    out_dir = Path(os.environ.get("OPENCLAW_ATTENDANCE_DIR", SCRIPT_WORKSPACE / "reports" / "attendance")).expanduser()
    env = os.environ.copy()
    env["OPENCLAW_ATTENDANCE_DIR"] = str(out_dir)
    cmd = [
        sys.executable,
        str(script),
        "sweep",
        "--source-agent",
        args.source_agent,
        "--sweep-id",
        args.sweep_id,
        "--agents",
        args.agents,
    ]
    try:
        cp = subprocess.run(cmd, text=True, capture_output=True, timeout=args.timeout, env=env)
    except Exception as exc:
        return {"ok": False, "exit_code": 127, "error": str(exc)}
    try:
        payload = json.loads(cp.stdout or "{}")
    except json.JSONDecodeError:
        payload = {}
    return {
        "ok": cp.returncode == 0,
        "exit_code": cp.returncode,
        "counts": payload.get("counts") or {},
        "employees": payload.get("employees") or [],
        "evidence": payload.get("evidence") or {},
        "stderr": cp.stderr[-2000:],
    }


def agent_probe(agent: str, timeout: int) -> dict[str, Any]:
    expected = f"OK_COMM_{agent.replace('-', '_').upper()}"
    res = run(["openclaw", "agent", "--agent", agent, "--message", f"只回复 {expected}", "--timeout", str(timeout), "--json"], timeout + 10)
    text = parse_agent_text(res.get("stdout_full") or res["stdout"])
    return {
        "agent": agent,
        "ok": res["ok"] and text == expected,
        "expected": expected,
        "reply": text,
        "exit_code": res["exit_code"],
        "stderr": res["stderr"],
    }


def write_report(report: dict[str, Any]) -> dict[str, str]:
    out_dir = Path(os.environ.get("OPENCLAW_COMM_SMOKE_DIR", SCRIPT_WORKSPACE / "reports" / "comm-smoke")).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"{report['smoke_id']}.json"
    report["evidence"] = {"json": str(report_path)}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report["evidence"]


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify OpenClaw company communication links without restarting services.")
    ap.add_argument("--source-agent", default="main")
    ap.add_argument("--agents", default="main,nestcar", help="Comma-separated agents for attendance and agent replies.")
    ap.add_argument("--line-account", default="nestcar")
    ap.add_argument("--line-base-url", default="http://127.0.0.1:3000")
    ap.add_argument("--sweep-id", default=datetime.now().strftime("comm-smoke-%Y%m%d-%H%M%S"))
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--skip-agent-reply", action="store_true")
    args = ap.parse_args()

    agents = [x.strip() for x in args.agents.split(",") if x.strip()]
    report: dict[str, Any] = {
        "ok": False,
        "smoke_id": args.sweep_id,
        "generated_at": now(),
        "checks": {},
    }
    report["checks"]["gateway"] = gateway_probe(min(args.timeout, 20))
    report["checks"]["line_webhook"] = http_get(f"{args.line_base_url.rstrip('/')}/line/{args.line_account}/webhook", min(args.timeout, 10))
    report["checks"]["attendance"] = attendance_probe(args)
    if args.skip_agent_reply:
        report["checks"]["agent_replies"] = {"skipped": True, "items": []}
    else:
        report["checks"]["agent_replies"] = {"items": [agent_probe(agent, args.timeout) for agent in agents]}

    report["ok"] = bool(
        report["checks"]["gateway"].get("ok")
        and report["checks"]["line_webhook"].get("ok")
        and all(item.get("ok") for item in report["checks"]["agent_replies"].get("items", []))
    )
    write_report(report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
