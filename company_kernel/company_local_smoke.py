from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime
from pathlib import Path

from . import api_gateway
from . import company_service_smoke

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / "state" / "local-smoke"


def persist_report(report: dict) -> dict:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    smoke_id = str(report.get("smoke_id") or datetime.now().strftime("local-smoke-%Y%m%d-%H%M%S"))
    report_path = STATE_DIR / f"{smoke_id}.json"
    latest_path = STATE_DIR / "latest.json"
    report["evidence"] = {"json": str(report_path), "latest": str(latest_path)}
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    report_path.write_text(payload, encoding="utf-8")
    latest_path.write_text(payload, encoding="utf-8")
    return report


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


def cockpit_snapshot() -> dict:
    try:
        status, payload = api_gateway.route_get("/v1/dashboard/cockpit", {})
    except Exception as exc:  # pragma: no cover - defensive smoke reporting
        return {"ok": False, "exit_code": 1, "payload": {}, "stderr": str(exc)}
    return {"ok": 200 <= int(status) < 300 and bool(payload.get("ok", True)), "exit_code": 0 if 200 <= int(status) < 300 else 1, "payload": payload, "stderr": ""}


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
    return persist_report(report)


def run_skill_closed_loop_smoke(source: str, agent: str, package: str, timeout: int) -> dict:
    task_id = f"task-local-smoke-skill-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    run_cmd([str(ROOT / "bin" / "companyctl"), "runtime", "register", "--runtime", "skill", "--command", "company-skill-package-worker", "--notes", "Skill Package runtime"], timeout=60)
    run_cmd(
        [
            str(ROOT / "bin" / "companyctl"),
            "employee",
            "create",
            "--id",
            agent,
            "--name",
            "Ecommerce Copy Skill",
            "--role",
            "skill-worker",
            "--runtime",
            "skill",
            "--workspace",
            str(ROOT / "employees" / agent),
        ],
        timeout=60,
    )
    submitted = run_cmd(
        [
            str(ROOT / "bin" / "companyctl"),
            "task",
            "submit",
            "--from",
            source,
            "--to",
            agent,
            "--task-id",
            task_id,
            "--title",
            "Local smoke ecommerce copy skill closed loop",
            "--description",
            "Verify runtime session, tool call, budget, artifact, evidence, and trace timeline.",
        ],
        timeout=60,
    )
    worker = run_cmd([str(ROOT / "bin" / "company-skill-package-worker"), "--agent", agent, "--package", package, "--by", source, "--timeout", str(timeout)], timeout=timeout + 60)
    task_payload = submitted.get("payload", {}).get("task", {}) if isinstance(submitted.get("payload"), dict) else {}
    trace_id = str((task_payload.get("metadata") or {}).get("trace_id") or "")
    worker_payload = worker.get("payload") if isinstance(worker.get("payload"), dict) else {}
    review_task_id = f"{task_id}-review"
    review_task = run_cmd(
        [
            str(ROOT / "bin" / "companyctl"),
            "task",
            "submit",
            "--from",
            source,
            "--to",
            source,
            "--task-id",
            review_task_id,
            "--title",
            "Review local smoke skill evidence",
            "--description",
            "Downstream review task for local skill closed-loop handoff validation.",
        ],
        timeout=60,
    )
    artifact_id = str((worker_payload.get("artifact") or {}).get("artifact_id") or "")
    handoff = (
        run_cmd(
            [
                str(ROOT / "bin" / "companyctl"),
                "task",
                "handoff",
                "create",
                "--from-task",
                task_id,
                "--to-task",
                review_task_id,
                "--from-employee",
                agent,
                "--to-employee",
                source,
                "--summary",
                "Local smoke skill final artifact handed to owner review",
                "--artifact",
                artifact_id,
                "--next-steps",
                "Review final evidence in CEO cockpit.",
            ],
            timeout=60,
        )
        if artifact_id
        else {"ok": False, "payload": {}, "stderr": "missing artifact_id"}
    )
    shown = run_cmd([str(ROOT / "bin" / "companyctl"), "task", "show", "--task-id", task_id], timeout=60)
    shown_payload = shown.get("payload") if isinstance(shown.get("payload"), dict) else {}
    evidence = shown_payload.get("evidence_records", []) if isinstance(shown_payload.get("evidence_records"), list) else []
    evidence_id = ""
    if evidence and isinstance(evidence[0], dict):
        evidence_id = str(evidence[0].get("evidence_id") or "")
    if not evidence_id:
        evidence_id = str((worker_payload.get("evidence") or {}).get("evidence_id") or "")
    acceptance = (
        run_cmd(
            [
                str(ROOT / "bin" / "companyctl"),
                "task",
                "evidence",
                "accept",
                "--evidence-id",
                evidence_id,
                "--by",
                source,
                "--summary",
                "Local smoke owner accepted final evidence",
            ],
            timeout=60,
        )
        if evidence_id
        else {"ok": False, "payload": {}, "stderr": "missing evidence_id"}
    )
    shown_after_accept = run_cmd([str(ROOT / "bin" / "companyctl"), "task", "show", "--task-id", task_id], timeout=60) if acceptance.get("ok") else shown
    trace = run_cmd([str(ROOT / "bin" / "companyctl"), "trace", "timeline", "--trace-id", trace_id], timeout=60) if trace_id else {"ok": False, "payload": {"timeline": []}, "stderr": "missing trace_id"}
    cockpit = cockpit_snapshot()
    if isinstance(shown_after_accept.get("payload"), dict) and shown_after_accept.get("payload", {}).get("ok", True):
        shown_payload = shown_after_accept.get("payload", {})
    task = shown_payload.get("task", {}) if isinstance(shown_payload.get("task"), dict) else {}
    completion_contract = shown_payload.get("completion_contract", {}) if isinstance(shown_payload.get("completion_contract"), dict) else {}
    attempts = shown_payload.get("attempts", []) if isinstance(shown_payload.get("attempts"), list) else []
    runtime_sessions = shown_payload.get("runtime_sessions", []) if isinstance(shown_payload.get("runtime_sessions"), list) else []
    tool_calls = shown_payload.get("tool_calls", []) if isinstance(shown_payload.get("tool_calls"), list) else []
    evidence = shown_payload.get("evidence_records", []) if isinstance(shown_payload.get("evidence_records"), list) else []
    accepted_evidence = [
        item
        for item in evidence
        if isinstance(item, dict) and isinstance(item.get("acceptance_decision"), dict) and item["acceptance_decision"].get("status") == "accepted"
    ]
    handoffs = shown_payload.get("handoffs", []) if isinstance(shown_payload.get("handoffs"), list) else []
    budget_summary = shown_payload.get("budget_summary", {}) if isinstance(shown_payload.get("budget_summary"), dict) else {}
    timeline = trace.get("payload", {}).get("timeline", []) if isinstance(trace.get("payload"), dict) else []
    trace_counts = trace.get("payload", {}).get("counts", {}) if isinstance(trace.get("payload"), dict) and isinstance(trace.get("payload", {}).get("counts", {}), dict) else {}
    trace_kinds = sorted({str(item.get("kind") or "") for item in timeline if isinstance(item, dict) and item.get("kind")})
    cockpit_payload = cockpit.get("payload") if isinstance(cockpit.get("payload"), dict) else {}
    cockpit_task_cards = cockpit_payload.get("task_cards", []) if isinstance(cockpit_payload.get("task_cards"), list) else []
    cockpit_card = next((item for item in cockpit_task_cards if isinstance(item, dict) and str(item.get("task_id") or "") == task_id), {})
    cockpit_tool_calls = [
        item for item in (cockpit_payload.get("tool_calls", []) if isinstance(cockpit_payload.get("tool_calls"), list) else [])
        if isinstance(item, dict) and str(item.get("task_id") or "") == task_id
    ]
    cockpit_budget_summary = cockpit_payload.get("budget_summary", {}) if isinstance(cockpit_payload.get("budget_summary"), dict) else {}
    cockpit_recent_evidence = [
        item for item in (cockpit_payload.get("recent_evidence", []) if isinstance(cockpit_payload.get("recent_evidence"), list) else [])
        if isinstance(item, dict) and str(item.get("task_id") or "") == task_id
    ]
    cockpit_evidence_queue = [
        item for item in (cockpit_payload.get("evidence_acceptance_queue", []) if isinstance(cockpit_payload.get("evidence_acceptance_queue"), list) else [])
        if isinstance(item, dict) and str(item.get("task_id") or "") == task_id
    ]
    cockpit_legacy_evidence = [
        item for item in (cockpit_payload.get("legacy_task_evidence", []) if isinstance(cockpit_payload.get("legacy_task_evidence"), list) else [])
        if isinstance(item, dict) and str(item.get("task_id") or "") == task_id
    ]
    cockpit_card_tool = cockpit_card.get("tool_summary", {}) if isinstance(cockpit_card.get("tool_summary"), dict) else {}
    cockpit_card_budget = cockpit_card.get("budget_summary", {}) if isinstance(cockpit_card.get("budget_summary"), dict) else {}
    cockpit_card_evidence = cockpit_card.get("evidence_summary", {}) if isinstance(cockpit_card.get("evidence_summary"), dict) else {}
    cockpit_budget_events = int(cockpit_card_budget.get("event_count") or 0)
    if cockpit_budget_events <= 0 and isinstance(cockpit_budget_summary.get("by_task_event_count"), dict):
        cockpit_budget_events = int(cockpit_budget_summary.get("by_task_event_count", {}).get(task_id, 0) or 0)
    cockpit_tool_count = int(cockpit_card_tool.get("tool_call_count") or len(cockpit_tool_calls))
    cockpit_evidence_count = int(cockpit_card_evidence.get("final_evidence_count") or len(cockpit_recent_evidence) or len(cockpit_evidence_queue) or len(cockpit_legacy_evidence))
    cockpit_task_visible = bool(cockpit_card or cockpit_recent_evidence or cockpit_evidence_queue or cockpit_legacy_evidence)
    cockpit_verification = {
        "ok": bool(cockpit.get("ok") and cockpit_task_visible and cockpit_tool_count > 0 and cockpit_budget_events > 0 and cockpit_evidence_count > 0),
        "task_id": task_id,
        "task_visible": cockpit_task_visible,
        "task_card_visible": bool(cockpit_card),
        "recent_evidence_visible": bool(cockpit_recent_evidence),
        "evidence_queue_visible": bool(cockpit_evidence_queue),
        "legacy_evidence_visible": bool(cockpit_legacy_evidence),
        "tool_call_count": cockpit_tool_count,
        "budget_event_count": cockpit_budget_events,
        "evidence_count": cockpit_evidence_count,
        "ledger_gaps": cockpit_card.get("ledger_gaps", []) if isinstance(cockpit_card.get("ledger_gaps", []), list) else [],
        "state": str(cockpit_card.get("state") or ""),
    }
    counts = {
        "attempts": len(attempts),
        "runtime_sessions": len(runtime_sessions),
        "tool_calls": len(tool_calls),
        "budget_events": int(budget_summary.get("event_count") or 0),
        "evidence": len(evidence),
        "accepted_evidence": len(accepted_evidence),
        "handoffs": int(trace_counts.get("handoffs") or len(handoffs)),
    }
    acceptance_payload = acceptance.get("payload") if isinstance(acceptance.get("payload"), dict) else {}
    acceptance_decision = (acceptance_payload.get("evidence") or {}).get("acceptance_decision") if isinstance(acceptance_payload.get("evidence"), dict) else {}
    acceptance_status = str((acceptance_decision or {}).get("status") or "")
    ok = (
        bool(worker.get("ok") and worker_payload.get("ok"))
        and bool(review_task.get("ok") and handoff.get("ok") and acceptance.get("ok"))
        and str(task.get("status") or "") == "completed"
        and bool(completion_contract.get("valid"))
        and str(completion_contract.get("acceptance_status") or "") == "accepted"
        and all(counts[key] > 0 for key in ["attempts", "runtime_sessions", "tool_calls", "budget_events", "evidence", "handoffs"])
        and counts["accepted_evidence"] > 0
        and {"tool_call", "budget_event", "evidence", "handoff"}.issubset(set(trace_kinds))
        and bool(cockpit_verification["ok"])
    )
    return {
        "ok": ok,
        "task_id": task_id,
        "trace_id": trace_id,
        "task_status": str(task.get("status") or ""),
        "completion_contract": completion_contract,
        "counts": counts,
        "trace_kinds": trace_kinds,
        "acceptance": {"ok": bool(acceptance.get("ok")), "status": acceptance_status, "result": acceptance_payload},
        "cockpit_verification": cockpit_verification,
        "worker": worker_payload,
        "review_task": review_task.get("payload", {}),
        "handoff": handoff.get("payload", {}),
        "task_show": shown_payload,
        "trace": trace.get("payload", {}),
        "errors": {
            "submit": submitted.get("stderr", ""),
            "worker": worker.get("stderr", ""),
            "review_task": review_task.get("stderr", ""),
            "handoff": handoff.get("stderr", ""),
            "acceptance": acceptance.get("stderr", ""),
            "show": shown.get("stderr", ""),
            "show_after_accept": shown_after_accept.get("stderr", ""),
            "trace": trace.get("stderr", ""),
            "cockpit": cockpit.get("stderr", ""),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run local Super AI Company usability smoke")
    parser.add_argument("--agents", default="nestcar,chindahotpot,codex", help="comma-separated employees to attendance probe")
    parser.add_argument("--source", default="main")
    parser.add_argument("--direct-targets", default="nestcar,chindahotpot,codex", help="comma-separated employees to direct message")
    parser.add_argument("--reply-timeout", type=int, default=120)
    parser.add_argument("--skill-closed-loop", action="store_true", help="also run a local Skill Package task and verify attempt/session/tool/budget/evidence/trace ledgers")
    parser.add_argument("--skill-agent", default="ecommerce-copy-skill")
    parser.add_argument("--skill-package", default="skill-packages/ecommerce-copy-demo/skill.json")
    parser.add_argument("--skill-timeout", type=int, default=120)
    parser.add_argument("--json-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_local_smoke(args.agents, args.source, args.direct_targets, args.reply_timeout)
    if args.skill_closed_loop:
        report["skill_closed_loop"] = run_skill_closed_loop_smoke(args.source, args.skill_agent, args.skill_package, args.skill_timeout)
        report["ok"] = bool(report["ok"] and report["skill_closed_loop"].get("ok"))
        persist_report(report)
    print(json.dumps(report, ensure_ascii=False, indent=None if args.json_only else 2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
