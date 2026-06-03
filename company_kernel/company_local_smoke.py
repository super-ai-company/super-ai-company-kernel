from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime
from pathlib import Path

from . import company_service_smoke

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / "state" / "local-smoke"


def run_cmd(args: list[str], timeout: int = 180) -> dict:
    cp = subprocess.run(args, cwd=str(ROOT), text=True, capture_output=True, timeout=timeout)
    stdout = cp.stdout.strip()
    payload: dict = {}
    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = {"raw_stdout": stdout}
    return {
        "ok": cp.returncode == 0,
        "exit_code": cp.returncode,
        "command": args,
        "payload": payload,
        "stderr": cp.stderr[-2000:],
    }


def run_local_smoke(agents: str, source: str, direct_targets: str, reply_timeout: int) -> dict:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    smoke_id = f"local-smoke-{timestamp}"
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    service = company_service_smoke.run_smoke()
    dashboard_path = ROOT / "state" / "dashboard.html"
    dashboard = run_cmd([str(ROOT / "bin" / "company-dashboard"), "--variant", "auto", "--output", str(dashboard_path)], timeout=120)
    attendance = run_cmd([
        str(ROOT / "bin" / "companyctl"),
        "attendance",
        "sweep",
        "--source",
        source,
        "--agents",
        agents,
        "--sweep-id",
        smoke_id,
        "--reply-timeout",
        str(reply_timeout),
    ], timeout=max(reply_timeout * max(1, len([a for a in agents.split(',') if a.strip()])), reply_timeout) + 60)

    direct_results = []
    for target in [item.strip() for item in direct_targets.split(",") if item.strip()]:
        message_id = f"msg-{smoke_id}-{target}"
        direct_results.append(run_cmd([
            str(ROOT / "bin" / "companyctl"),
            "message",
            "direct",
            "--from",
            source,
            "--to",
            target,
            "--body",
            f"只回复：{target.upper().replace('-', '_')}_LOCAL_SMOKE_OK",
            "--message-id",
            message_id,
            "--timeout",
            str(reply_timeout),
        ], timeout=reply_timeout + 30))

    attendance_payload = attendance.get("payload") or {}
    direct_matrix = []
    attendance_by_agent = {row.get("agent"): row for row in attendance_payload.get("employees", []) if isinstance(row, dict)}
    for result in direct_results:
        payload = result.get("payload") or {}
        target = payload.get("target") or ""
        attendance_row = attendance_by_agent.get(target, {})
        direct_matrix.append({
            "agent_id": target,
            "attendance_status": attendance_row.get("status", "unknown"),
            "direct_status": "ok" if result.get("ok") and payload.get("ok") else "failed",
            "session_key": payload.get("session_key", ""),
            "reply_text": payload.get("reply", ""),
            "failure_class": "none" if result.get("ok") and payload.get("ok") else payload.get("error") or result.get("stderr") or "direct_failed",
            "evidence": payload.get("file", ""),
        })

    dashboard_ok = bool(dashboard.get("ok")) and dashboard_path.exists()
    attendance_ok = bool(attendance.get("ok")) and not (attendance_payload.get("counts", {}).get("worker_stalled") or attendance_payload.get("counts", {}).get("session_missing"))
    direct_ok = all(item["direct_status"] == "ok" for item in direct_matrix)
    ok = bool(service.get("ok")) and dashboard_ok and attendance_ok and direct_ok

    report = {
        "ok": ok,
        "smoke_id": smoke_id,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "service": service,
        "dashboard": {"ok": dashboard_ok, "path": str(dashboard_path), "result": dashboard},
        "attendance": {"ok": attendance_ok, "result": attendance, "evidence": attendance_payload.get("evidence", {})},
        "direct_matrix": direct_matrix,
    }
    report_path = STATE_DIR / f"{smoke_id}.json"
    latest_path = STATE_DIR / "latest.json"
    report["evidence"] = {"json": str(report_path), "latest": str(latest_path)}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    latest_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run local Super AI Company usability smoke")
    parser.add_argument("--agents", default="nestcar,chindahotpot,codex", help="comma-separated employees to attendance probe")
    parser.add_argument("--source", default="main")
    parser.add_argument("--direct-targets", default="nestcar,chindahotpot,codex", help="comma-separated employees to direct message")
    parser.add_argument("--reply-timeout", type=int, default=120)
    parser.add_argument("--json-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_local_smoke(args.agents, args.source, args.direct_targets, args.reply_timeout)
    print(json.dumps(report, ensure_ascii=False, indent=None if args.json_only else 2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
