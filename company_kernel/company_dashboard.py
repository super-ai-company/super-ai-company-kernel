from __future__ import annotations

import argparse
import base64
import html
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import companyctl
from .schema_migrations import ensure_schema_migrations


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "company.sqlite"
SCHEMA = ROOT / "company_kernel" / "schema.sql"
DEFAULT_OUTPUT = ROOT / "state" / "dashboard.html"
ADVANCED_TEMPLATE_CANDIDATES = [
    ROOT / "dashboard_templates" / "gemini_dashboard.html",
    Path("/Users/shift/Documents/anti/state/dashboard.html"),
]
REAL_PROJECT_ROOT = Path("/Users/shift/openclaw/company-kernel")


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    ensure_schema_migrations(conn)
    conn.commit()
    return conn


def rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def e(value: object) -> str:
    return html.escape("" if value is None else str(value))


def status_counts(conn: sqlite3.Connection, table: str) -> dict[str, int]:
    return {row["status"]: int(row["count"]) for row in rows(conn, f"SELECT status, COUNT(*) AS count FROM {table} GROUP BY status")}


def parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def minutes_since(value: str, generated_at: str) -> int | None:
    timestamp = parse_time(value)
    generated = parse_time(generated_at)
    if not timestamp or not generated:
        return None
    return max(0, int((generated - timestamp).total_seconds() // 60))


def milliseconds_between(start: str, end: str) -> int:
    start_dt = parse_time(start)
    end_dt = parse_time(end)
    if not start_dt or not end_dt:
        return 1
    return max(1, int((end_dt - start_dt).total_seconds() * 1000))


def build_traces(conn: sqlite3.Connection, *, limit: int = 20) -> list[dict]:
    trace_ids = [
        row["trace_id"]
        for row in rows(
            conn,
            """
            SELECT trace_id, MAX(created_at) AS last_seen
            FROM (
              SELECT trace_id, created_at FROM company_events WHERE trace_id != ''
              UNION ALL
              SELECT trace_id, created_at FROM adapter_runs WHERE trace_id != ''
            )
            GROUP BY trace_id
            ORDER BY last_seen DESC
            LIMIT ?
            """,
            (limit,),
        )
    ]
    traces = []
    for trace_id in trace_ids:
        event_rows = rows(
            conn,
            """
            SELECT id, event_type, source_agent, task_id, created_at, processed_at
            FROM company_events
            WHERE trace_id = ?
            ORDER BY created_at ASC
            """,
            (trace_id,),
        )
        run_rows = rows(
            conn,
            """
            SELECT id, agent_id, command, task_id, ok, processed, created_at
            FROM adapter_runs
            WHERE trace_id = ?
            ORDER BY created_at ASC
            """,
            (trace_id,),
        )
        timestamps = [item["created_at"] for item in [*event_rows, *run_rows] if item.get("created_at")]
        if not timestamps:
            continue
        start = min(timestamps)
        end = max([*(item.get("processed_at") for item in event_rows if item.get("processed_at")), *timestamps])
        duration = max(1, milliseconds_between(start, end))
        spans = []
        for event in event_rows:
            span_start = milliseconds_between(start, event["created_at"])
            span_duration = milliseconds_between(event["created_at"], event.get("processed_at") or event["created_at"])
            spans.append(
                {
                    "name": event["event_type"],
                    "service": event["source_agent"] or "event",
                    "duration_ms": max(12, span_duration),
                    "start_ms": span_start,
                    "event_id": event["id"],
                    "task_id": event["task_id"],
                    "created_at": event["created_at"],
                    "processed_at": event.get("processed_at", ""),
                }
            )
        for run in run_rows:
            span_start = milliseconds_between(start, run["created_at"])
            spans.append(
                {
                    "name": run["command"] or "adapter.run",
                    "service": run["agent_id"] or "adapter",
                    "duration_ms": 80 if run.get("processed") else 24,
                    "start_ms": span_start,
                    "adapter_run_id": run["id"],
                    "task_id": run["task_id"],
                    "ok": bool(run["ok"]),
                    "processed": bool(run["processed"]),
                    "created_at": run["created_at"],
                }
            )
        spans.sort(key=lambda item: (item.get("start_ms", 0), item.get("name", "")))
        first = spans[0] if spans else {}
        traces.append(
            {
                "trace_id": trace_id,
                "title": first.get("task_id") or first.get("name") or trace_id,
                "duration_ms": max(duration, max((span["start_ms"] + span["duration_ms"] for span in spans), default=1)),
                "spans": spans,
                "started_at": start,
                "updated_at": end,
            }
        )
    return traces


def load_summary(conn: sqlite3.Connection) -> dict:
    conversation_rows = rows(
        conn,
        """
        SELECT c.id, c.title, c.created_by, c.status, c.updated_at,
               c.participants_json,
               COUNT(cm.id) AS message_count,
               COALESCE(MAX(cm.created_at), c.created_at) AS last_message_at
        FROM conversations c
        LEFT JOIN conversation_messages cm ON cm.conversation_id = c.id
        GROUP BY c.id
        ORDER BY c.updated_at DESC
        LIMIT 20
        """,
    )
    for conversation in conversation_rows:
        conversation["messages"] = rows(
            conn,
            """
            SELECT id, source_agent, body, evidence_path, created_at
            FROM conversation_messages
            WHERE conversation_id = ?
            ORDER BY created_at ASC
            LIMIT 80
            """,
            (conversation["id"],),
        )
    return {
        "generated_at": now(),
        "runtime_health": {
            "daemon": companyctl.daemon_health(),
            "launchd": companyctl.launchd_health(),
        },
        "evidence_health": {
            "issues": companyctl.task_evidence_issues(conn),
        },
        "counts": {
            "employees": scalar(conn, "SELECT COUNT(*) FROM employees"),
            "active_employees": scalar(conn, "SELECT COUNT(*) FROM employees WHERE status = 'active'"),
            "candidate_employees": scalar(conn, "SELECT COUNT(*) FROM employees WHERE status = 'candidate'"),
            "archived_employees": scalar(conn, "SELECT COUNT(*) FROM employees WHERE status = 'archived'"),
            "projects": scalar(conn, "SELECT COUNT(*) FROM projects"),
            "active_projects": scalar(conn, "SELECT COUNT(*) FROM projects WHERE status = 'active'"),
            "completed_projects": scalar(conn, "SELECT COUNT(*) FROM projects WHERE status = 'completed'"),
            "tasks": scalar(conn, "SELECT COUNT(*) FROM tasks"),
            "conversations": scalar(conn, "SELECT COUNT(*) FROM conversations"),
            "open_conversations": scalar(conn, "SELECT COUNT(*) FROM conversations WHERE status = 'open'"),
            "submitted_tasks": scalar(conn, "SELECT COUNT(*) FROM tasks WHERE status = 'submitted'"),
            "claimed_tasks": scalar(conn, "SELECT COUNT(*) FROM tasks WHERE status = 'claimed'"),
            "blocked_tasks": scalar(conn, "SELECT COUNT(*) FROM tasks WHERE status = 'blocked'"),
            "pending_approvals": scalar(conn, "SELECT COUNT(*) FROM approvals WHERE status = 'pending'"),
            "rfcs": scalar(conn, "SELECT COUNT(*) FROM rfcs"),
            "pending_rfcs": scalar(conn, "SELECT COUNT(*) FROM rfcs WHERE status = 'pending'"),
            "pending_events": scalar(conn, "SELECT COUNT(*) FROM company_events WHERE processed_at = ''"),
            "locks": scalar(conn, "SELECT COUNT(*) FROM locks"),
            "adapter_runs": scalar(conn, "SELECT COUNT(*) FROM adapter_runs"),
        },
        "task_status": status_counts(conn, "tasks"),
        "project_status": status_counts(conn, "projects"),
        "approval_status": status_counts(conn, "approvals"),
        "rfc_status": status_counts(conn, "rfcs"),
        "employees": rows(
            conn,
            """
            SELECT e.id, e.name, e.role, e.runtime, e.status AS employee_status, e.workspace,
                   COALESCE(h.status, 'missing') AS heartbeat_status,
                   COALESCE(h.last_seen_at, '') AS last_seen_at,
                   COALESCE(h.metadata_json, '{}') AS heartbeat_metadata_json,
                   COALESCE(
                     (SELECT COUNT(*) FROM tasks t WHERE t.target_agent = e.id AND t.status = 'submitted'),
                     0
                   ) AS submitted_tasks,
                   COALESCE(
                     (SELECT COUNT(*) FROM tasks t WHERE t.target_agent = e.id AND t.status = 'claimed'),
                     0
                   ) AS claimed_tasks
            FROM employees e
            LEFT JOIN heartbeats h ON h.agent_id = e.id
            ORDER BY
              CASE e.status WHEN 'active' THEN 0 WHEN 'candidate' THEN 1 ELSE 2 END,
              e.id
            """,
        ),
        "projects": rows(
            conn,
            """
            SELECT p.*,
                   COUNT(pt.task_id) AS task_count,
                   COALESCE(SUM(CASE WHEN t.status = 'completed' THEN 1 ELSE 0 END), 0) AS completed_tasks,
                   COALESCE(SUM(CASE WHEN t.status = 'blocked' THEN 1 ELSE 0 END), 0) AS blocked_tasks,
                   COALESCE(SUM(CASE WHEN t.status NOT IN ('completed', 'blocked') THEN 1 ELSE 0 END), 0) AS open_tasks,
                   (SELECT COUNT(*) FROM project_acceptances pa WHERE pa.project_id = p.id) AS acceptance_count,
                   (SELECT COUNT(*) FROM project_plan_items ppi WHERE ppi.project_id = p.id) AS plan_item_count,
                   (SELECT COUNT(*) FROM project_plan_items ppi WHERE ppi.project_id = p.id AND ppi.status NOT IN ('done', 'completed', 'cancelled')) AS open_plan_items,
                   COALESCE(
                       (
                         SELECT GROUP_CONCAT(
                           ppi.status || ':' || ppi.title ||
                           CASE WHEN ppi.task_id != '' THEN ' [' || ppi.task_id || '/' || COALESCE(t2.status, 'missing') || ']' ELSE '' END,
                           '; '
                         )
                         FROM project_plan_items ppi
                         LEFT JOIN tasks t2 ON t2.id = ppi.task_id
                         WHERE ppi.project_id = p.id
                         ORDER BY ppi.created_at ASC
                       ),
                       ''
                   ) AS plan_items,
                   COALESCE((SELECT pa.summary FROM project_acceptances pa WHERE pa.project_id = p.id ORDER BY pa.created_at DESC LIMIT 1), '') AS latest_acceptance_summary
            FROM projects p
            LEFT JOIN project_tasks pt ON pt.project_id = p.id
            LEFT JOIN tasks t ON t.id = pt.task_id
            GROUP BY p.id
            ORDER BY p.updated_at DESC
            LIMIT 20
            """,
        ),
        "tasks": rows(
            conn,
            """
            SELECT t.*
            FROM tasks t
            ORDER BY t.updated_at DESC, t.created_at DESC
            LIMIT 30
            """,
        ),
        "task_delegations": rows(
            conn,
            """
            SELECT parent.id AS parent_id,
                   parent.title AS parent_title,
                   parent.status AS parent_status,
                   parent.target_agent AS parent_owner,
                   COUNT(child.id) AS child_count,
                   COALESCE(SUM(CASE WHEN child.status = 'completed' THEN 1 ELSE 0 END), 0) AS completed_children,
                   COALESCE(SUM(CASE WHEN child.status = 'blocked' THEN 1 ELSE 0 END), 0) AS blocked_children,
                   COALESCE(SUM(CASE WHEN child.status NOT IN ('completed', 'blocked') THEN 1 ELSE 0 END), 0) AS open_children,
                   COALESCE(
                       GROUP_CONCAT(child.id || '/' || child.target_agent || '/' || child.status, '; '),
                       ''
                   ) AS child_summary,
                   MAX(child.updated_at) AS latest_child_update
            FROM task_relations tr
            JOIN tasks parent ON parent.id = tr.parent_task_id
            JOIN tasks child ON child.id = tr.child_task_id
            GROUP BY parent.id
            ORDER BY latest_child_update DESC, parent.updated_at DESC
            LIMIT 20
            """,
        ),
        "conversations": conversation_rows,
        "approvals": rows(conn, "SELECT * FROM approvals ORDER BY updated_at DESC LIMIT 20"),
        "rfcs": rows(conn, "SELECT * FROM rfcs ORDER BY updated_at DESC LIMIT 20"),
        "followups": companyctl.list_followups("all")[:20],
        "pending_events": rows(conn, "SELECT * FROM company_events WHERE processed_at = '' ORDER BY created_at ASC LIMIT 20"),
        "events": rows(conn, "SELECT * FROM company_events ORDER BY created_at DESC LIMIT 20"),
        "adapter_runs": rows(conn, "SELECT * FROM adapter_runs ORDER BY created_at DESC LIMIT 20"),
        "locks": rows(conn, "SELECT * FROM locks ORDER BY updated_at DESC"),
        "traces": build_traces(conn),
    }


def pills(counts: dict[str, int]) -> str:
    return "".join(f"<span class='pill'>{e(k)}: <strong>{v}</strong></span>" for k, v in counts.items())


def render_table(headers: list[str], items: list[dict], fields: list[str]) -> str:
    head = "".join(f"<th>{e(h)}</th>" for h in headers)
    body = []
    for item in items:
        body.append("<tr>" + "".join(f"<td>{e(item.get(field, ''))}</td>" for field in fields) + "</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def render_employee_table(items: list[dict]) -> str:
    headers = ["id", "status", "kernel_state", "schedulable", "role", "runtime", "heartbeat", "age_min", "backlog", "skills", "tools", "task_types", "last_seen", "actions"]
    fields = ["id", "employee_status", "kernel_state", "schedulable", "role", "runtime", "heartbeat_status", "heartbeat_age_minutes", "backlog", "skills", "tools", "task_types", "last_seen_at"]
    head = "".join(f"<th>{e(header)}</th>" for header in headers)
    body = []
    for item in items:
        employee_id = e(item.get("id", ""))
        cells = "".join(f"<td>{e(item.get(field, ''))}</td>" for field in fields)
        actions = (
            "<td>"
            f"<button type='button' onclick=\"directMessageEmployee('{employee_id}')\">Direct</button> "
            f"<button type='button' onclick=\"editEmployee('{employee_id}')\">Edit</button> "
            f"<button class='danger-button' type='button' onclick=\"offboardEmployee('{employee_id}', false)\">Archive</button>"
            "</td>"
        )
        body.append(f"<tr>{cells}{actions}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def employee_view_models(summary: dict) -> list[dict]:
    employees = []
    communication_config = companyctl.load_communication_config()
    communication_profiles = communication_config.get("employees", {})
    for employee in summary["employees"]:
        if employee.get("id") == "owner-shift" or employee.get("role") == "human-owner" or employee.get("runtime") == "human":
            continue
        capabilities = companyctl.load_json_or_default(companyctl.employee_paths(employee["id"])["capabilities"], {})
        skills = capabilities.get("skills", [])
        tools = capabilities.get("tools", [])
        task_types = capabilities.get("preferred_task_types", [])
        communication_profile = communication_profiles.get(employee["id"], {})
        age = minutes_since(employee.get("last_seen_at", ""), summary["generated_at"])
        employee_status = employee.get("employee_status") or employee.get("status", "")
        heartbeat_status = employee.get("heartbeat_status", "missing")
        if employee_status != "active":
            kernel_state = employee_status
            schedulable = "no"
        elif heartbeat_status == "missing":
            kernel_state = "missing_heartbeat"
            schedulable = "no"
        elif age is not None and age > 15:
            kernel_state = "stale_heartbeat"
            schedulable = "no"
        else:
            kernel_state = "online"
            schedulable = "yes"
        employees.append(
            {
                **employee,
                "status": employee_status,
                "employee_status": employee_status,
                "kernel_state": kernel_state,
                "schedulable": schedulable,
                "heartbeat_age_minutes": "" if age is None else age,
                "communication_paused": bool(communication_profile.get("communication_paused")),
                "communication_status": "paused" if communication_profile.get("communication_paused") else "enabled",
                "backlog": f"{employee.get('submitted_tasks', 0)} submitted, {employee.get('claimed_tasks', 0)} claimed",
                "skills": ", ".join(str(item) for item in skills[:4]) if isinstance(skills, list) else "invalid",
                "tools": ", ".join(str(item) for item in tools[:4]) if isinstance(tools, list) else "invalid",
                "task_types": ", ".join(str(item) for item in task_types[:4]) if isinstance(task_types, list) else "invalid",
            }
        )
    return employees


def approval_task_id(approval: dict) -> str:
    raw = approval.get("reason", "")
    try:
        detail = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return ""
    if not isinstance(detail, dict):
        return ""
    metadata = detail.get("metadata", {})
    if isinstance(metadata, dict) and metadata.get("task_id"):
        return str(metadata["task_id"])
    return ""


def task_approval_counts(tasks: list[dict], approvals: list[dict]) -> dict[str, int]:
    task_ids = {str(task["id"]) for task in tasks}
    counts = {task_id: 0 for task_id in task_ids}
    for approval in approvals:
        structured_task_id = approval_task_id(approval)
        if structured_task_id in counts:
            counts[structured_task_id] += 1
            continue
        raw = approval.get("reason", "")
        for task_id in task_ids:
            if task_id in raw:
                counts[task_id] += 1
                break
    return counts


def render(summary: dict) -> str:
    counts = summary["counts"]
    danger = (
        counts["pending_approvals"]
        or counts["pending_rfcs"]
        or counts["pending_events"]
        or counts["claimed_tasks"]
        or counts["locks"]
        or summary["evidence_health"]["issues"]
        or not summary["runtime_health"]["daemon"].get("ok")
    )
    state = "attention" if danger else "normal"
    approvals = []
    for approval in summary["approvals"]:
        detail = approval.get("reason", "")
        try:
            detail_obj = json.loads(detail or "{}")
            detail = detail_obj.get("request_reason", detail)
        except json.JSONDecodeError:
            pass
        approvals.append({**approval, "reason": detail})
    approval_counts = task_approval_counts(summary["tasks"], summary["approvals"])
    projects = []
    for project in summary["projects"]:
        try:
            acceptance = json.loads(project.get("acceptance_json", "[]") or "[]")
        except json.JSONDecodeError:
            acceptance = []
        open_tasks = int(project.get("open_tasks", 0) or 0)
        blocked_tasks = int(project.get("blocked_tasks", 0) or 0)
        task_count = int(project.get("task_count", 0) or 0)
        open_plan_items = int(project.get("open_plan_items", 0) or 0)
        ready = bool(task_count and not open_tasks and not blocked_tasks and not open_plan_items)
        projects.append(
            {
                **project,
                "acceptance": "; ".join(str(item) for item in acceptance),
                "review_state": "ready" if ready else "blocked" if blocked_tasks else "in_progress",
                "plan": project.get("plan_items") or f"{project.get('completed_tasks', 0)}/{task_count} done, {open_tasks} open, {blocked_tasks} blocked",
            }
        )
    tasks = []
    for task in summary["tasks"]:
        tasks.append(
            {
                **task,
                "evidence": "yes" if task.get("evidence_path") else "",
                "blocker_detail": task.get("blocker", ""),
                "approval_count": approval_counts.get(str(task["id"]), 0),
            }
        )
    task_delegations = []
    for item in summary["task_delegations"]:
        child_count = int(item.get("child_count", 0) or 0)
        completed = int(item.get("completed_children", 0) or 0)
        blocked = int(item.get("blocked_children", 0) or 0)
        open_children = int(item.get("open_children", 0) or 0)
        task_delegations.append(
            {
                **item,
                "progress": f"{completed}/{child_count}",
                "review_state": "ready" if child_count and completed == child_count else "blocked" if blocked else "in_progress",
                "open_blocked": f"{open_children} open, {blocked} blocked",
            }
        )
    rfcs = []
    for rfc in summary["rfcs"]:
        try:
            target_paths = json.loads(rfc.get("target_paths_json", "[]") or "[]")
        except json.JSONDecodeError:
            target_paths = []
        rfcs.append({**rfc, "target_paths": ", ".join(str(path) for path in target_paths)})
    conversations = []
    for conversation in summary["conversations"]:
        try:
            participants = json.loads(conversation.get("participants_json", "[]") or "[]")
        except json.JSONDecodeError:
            participants = []
        conversations.append({**conversation, "participants": ", ".join(str(participant) for participant in participants)})
    adapter_runs = []
    for run in summary["adapter_runs"]:
        try:
            result = json.loads(run.get("result_json", "{}") or "{}")
        except json.JSONDecodeError:
            result = {}
        adapter_runs.append({**run, "ok_text": "yes" if run.get("ok") else "no", "state_file": result.get("state_file", "")})
    runtime_health = [
        {"name": "daemon", "path": summary["runtime_health"]["daemon"].get("state_file", ""), **summary["runtime_health"]["daemon"]},
        {"name": "launchd", "path": summary["runtime_health"]["launchd"].get("template", ""), **summary["runtime_health"]["launchd"]},
    ]
    evidence_health = []
    for issue in summary["evidence_health"]["issues"]:
        evidence_health.append(
            {
                **issue,
                "path": issue.get("evidence_path", ""),
            }
        )
    employees = employee_view_models(summary)
    return f"""<!doctype html>
<html lang="zh">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Company Kernel Dashboard</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f7f7f4; color: #202124; }}
    header {{ padding: 24px 28px 14px; border-bottom: 1px solid #ddd9cf; background: #fff; }}
    h1 {{ margin: 0 0 6px; font-size: 24px; }}
    h2 {{ margin: 24px 0 10px; font-size: 17px; }}
    main {{ padding: 18px 28px 36px; }}
    .meta {{ color: #68665f; font-size: 13px; }}
    .status {{ display: inline-block; padding: 4px 10px; border-radius: 999px; font-size: 13px; background: {"#fff0d6" if state == "attention" else "#e6f4ea"}; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-top: 16px; }}
    .metric {{ background: #fff; border: 1px solid #dedbd2; border-radius: 8px; padding: 12px; }}
    .metric strong {{ display: block; font-size: 24px; margin-top: 4px; }}
    .pill {{ display: inline-block; margin: 0 8px 8px 0; padding: 6px 10px; background: #fff; border: 1px solid #dedbd2; border-radius: 999px; font-size: 13px; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #dedbd2; border-radius: 8px; overflow: hidden; }}
    th, td {{ padding: 9px 10px; border-bottom: 1px solid #ece8df; text-align: left; vertical-align: top; font-size: 13px; }}
    th {{ background: #efede7; color: #4d4a43; font-weight: 600; }}
    tr:last-child td {{ border-bottom: 0; }}
    td {{ max-width: 360px; overflow-wrap: anywhere; }}
    .toolbar {{ display: flex; align-items: flex-end; gap: 10px; flex-wrap: wrap; margin: 10px 0 14px; padding: 12px; background: #fff; border: 1px solid #dedbd2; border-radius: 8px; }}
    .toolbar label {{ display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: #68665f; }}
    .toolbar input, .toolbar select {{ min-width: 130px; padding: 7px 8px; border: 1px solid #cfcabe; border-radius: 6px; background: #fff; color: #202124; }}
    .toolbar button, .danger-button {{ padding: 8px 10px; border: 1px solid #bdb7aa; border-radius: 6px; background: #202124; color: #fff; cursor: pointer; }}
    .danger-button {{ background: #9f1d1d; border-color: #9f1d1d; }}
    .api-note {{ margin: 8px 0 0; color: #68665f; font-size: 12px; }}
    .api-status {{ margin-top: 8px; font-size: 13px; color: #3f6f32; }}
  </style>
</head>
<body>
  <header>
    <h1>Company Kernel Dashboard</h1>
    <div class="meta">Generated at {e(summary["generated_at"])} from {e(DB_PATH)}</div>
    <div style="margin-top:10px"><span class="status">{'Needs Attention' if state == 'attention' else 'Normal'}</span></div>
  </header>
  <main>
    <section class="grid">
      {''.join(f"<div class='metric'>{e(k)}<strong>{v}</strong></div>" for k, v in counts.items())}
    </section>
    <h2>Task Status</h2>
    <div>{pills(summary["task_status"])}</div>
    <h2>Project Status</h2>
    <div>{pills(summary["project_status"])}</div>
    <h2>Approval Status</h2>
    <div>{pills(summary["approval_status"])}</div>
    <h2>RFC Status</h2>
    <div>{pills(summary["rfc_status"])}</div>
    <h2>Runtime Health</h2>
    {render_table(["name", "ok", "installed", "age", "reason", "state/template", "install", "verify"], runtime_health, ["name", "ok", "installed", "age_minutes", "reason", "path", "install_command", "verify_command"])}
    <h2>Evidence Health</h2>
    {render_table(["task", "agent", "reason", "path"], evidence_health, ["task_id", "agent", "reason", "path"])}
    <h2>Employees</h2>
    <div class="toolbar" id="employee-manager">
      <label>API Gateway
        <input id="api-base" value="http://127.0.0.1:8765">
      </label>
      <label>ID
        <input id="employee-id" placeholder="e.g. nestcar-helper">
      </label>
      <label>Name
        <input id="employee-name" placeholder="Display name">
      </label>
      <label>Role
        <input id="employee-role" value="business-agent">
      </label>
      <label>Runtime
        <select id="employee-runtime">
          <option value="openclaw">openclaw</option>
          <option value="hermes">hermes</option>
          <option value="codex">codex</option>
          <option value="claude">claude</option>
          <option value="trae">trae</option>
          <option value="antigravity">antigravity</option>
          <option value="local">local</option>
        </select>
      </label>
      <label>Workspace
        <input id="employee-workspace" placeholder="/Users/shift/openclaw/...">
      </label>
      <label>Skills
        <input id="employee-skills" placeholder="ops,review">
      </label>
      <button type="button" onclick="checkCompanyApi()">Check API</button>
      <button type="button" onclick="onboardEmployee()">Onboard</button>
    </div>
    <div class="api-note">Employee actions call Company Kernel REST API and then reload this static dashboard. Start with: <code>bin/company-api-gateway --quiet</code></div>
    <div class="api-status" id="employee-api-status"></div>
    {render_employee_table(employees)}
    <h2>Projects</h2>
    {render_table(["id", "owner", "status", "review", "plan", "open_plan", "accepted", "goal", "acceptance", "retro", "title", "updated"], projects, ["id", "owner_agent", "status", "review_state", "plan", "open_plan_items", "acceptance_count", "goal", "acceptance", "latest_acceptance_summary", "title", "updated_at"])}
    <h2>Recent Tasks</h2>
    {render_table(["id", "source", "target", "priority", "status", "claimed_by", "evidence", "blocker", "approvals", "title", "updated"], tasks, ["id", "source_agent", "target_agent", "priority", "status", "claimed_by", "evidence", "blocker_detail", "approval_count", "title", "updated_at"])}
    <h2>Long Task Delegation</h2>
    {render_table(["parent", "owner", "status", "review", "progress", "open/blocked", "children", "latest_child_update"], task_delegations, ["parent_id", "parent_owner", "parent_status", "review_state", "progress", "open_blocked", "child_summary", "latest_child_update"])}
    <h2>Conversations</h2>
    {render_table(["id", "status", "created_by", "participants", "messages", "last_message", "title"], conversations, ["id", "status", "created_by", "participants", "message_count", "last_message_at", "title"])}
    <h2>Approvals</h2>
    {render_table(["id", "source", "action", "status", "reason", "updated"], approvals, ["id", "source_agent", "action", "status", "reason", "updated_at"])}
    <h2>RFCs</h2>
    {render_table(["id", "author", "status", "paths", "reason", "decision_by", "updated"], rfcs, ["id", "author_agent", "status", "target_paths", "reason", "decision_by", "updated_at"])}
    <h2>Followups</h2>
    {render_table(["id", "status", "source", "target", "question", "answer", "answered_at"], summary["followups"], ["id", "status", "source_agent", "target_agent", "question", "answer", "answered_at"])}
    <h2>Events</h2>
    <h2>Pending Events</h2>
    {render_table(["id", "trace", "type", "source", "task", "created"], summary["pending_events"], ["id", "trace_id", "event_type", "source_agent", "task_id", "created_at"])}
    <h2>Recent Events</h2>
    {render_table(["id", "trace", "type", "source", "task", "processed_at", "created"], summary["events"], ["id", "trace_id", "event_type", "source_agent", "task_id", "processed_at", "created_at"])}
    <h2>Adapter Runs</h2>
    {render_table(["id", "trace", "agent", "task", "command", "ok", "processed", "attempt", "next_retry", "ack_by", "ack_reason", "state_file", "created"], adapter_runs, ["id", "trace_id", "agent_id", "task_id", "command", "ok_text", "processed", "attempt", "next_retry_at", "acknowledged_by", "acknowledgement_reason", "state_file", "created_at"])}
    <h2>Locks</h2>
    {render_table(["resource", "owner", "lease_until", "updated"], summary["locks"], ["resource_key", "owner_agent", "lease_until", "updated_at"])}
  </main>
  <script>
    function apiBase() {{
      return (document.getElementById('api-base').value || 'http://127.0.0.1:8765').replace(/\\/$/, '');
    }}
    function setEmployeeApiStatus(text, isError) {{
      const el = document.getElementById('employee-api-status');
      el.textContent = text;
      el.style.color = isError ? '#9f1d1d' : '#3f6f32';
    }}
    async function getCompanyApi(path) {{
      const res = await fetch(apiBase() + path, {{method: 'GET'}});
      const data = await res.json();
      if (!res.ok || data.ok === false) {{
        throw new Error(data.error || data.message || JSON.stringify(data));
      }}
      return data;
    }}
    async function checkCompanyApi() {{
      setEmployeeApiStatus(`Checking ${{apiBase()}}/v1/health...`, false);
      try {{
        const data = await getCompanyApi('/v1/health');
        const employees = data.counts ? data.counts.employees : 'unknown';
        setEmployeeApiStatus(`API online. employees=${{employees}}`, false);
        return true;
      }} catch (err) {{
        setEmployeeApiStatus(`API offline: ${{err.message}}. Start: bin/company-api-gateway --quiet`, true);
        return false;
      }}
    }}
    async function callCompanyApi(path, payload, method) {{
      const res = await fetch(apiBase() + path, {{
        method: method || 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(payload || {{}})
      }});
      const data = await res.json();
      if (!res.ok || data.ok === false) {{
        throw new Error(data.error || data.message || JSON.stringify(data));
      }}
      return data;
    }}
    async function onboardEmployee() {{
      const id = document.getElementById('employee-id').value.trim();
      const name = document.getElementById('employee-name').value.trim() || id;
      const role = document.getElementById('employee-role').value.trim() || 'business-agent';
      const runtime = document.getElementById('employee-runtime').value;
      const workspace = document.getElementById('employee-workspace').value.trim() || `/Users/shift/openclaw/company-kernel/employees/${{id}}`;
      const skills = document.getElementById('employee-skills').value.trim();
      if (!id) {{
        setEmployeeApiStatus('employee id is required', true);
        return;
      }}
      setEmployeeApiStatus(`Onboarding ${{id}}...`, false);
      try {{
        await callCompanyApi('/v1/employees/onboard', {{
          id, name, role, runtime, workspace, skills,
          open_communication: true,
          create_test_task: false
        }});
        setEmployeeApiStatus(`Onboarded ${{id}}. Reloading dashboard...`, false);
        setTimeout(() => location.reload(), 800);
      }} catch (err) {{
        setEmployeeApiStatus(`Onboard failed: ${{err.message}}`, true);
      }}
    }}
    async function directMessageEmployee(id) {{
      if (!id) return;
      const source = prompt(`Source employee for direct message to ${{id}}`, 'main');
      if (source === null) return;
      const body = prompt(`Message to ${{id}}`, `只回复：${{id}}_DIRECT_OK`);
      if (body === null) return;
      setEmployeeApiStatus(`Direct messaging ${{id}}...`, false);
      try {{
        const result = await callCompanyApi('/v1/messages/direct', {{from: source || 'main', to: id, body}}, 'POST');
        setEmployeeApiStatus(`Direct reply from ${{id}}: ${{result.reply || '(empty)'}}; evidence=${{result.file || 'n/a'}}`, false);
      }} catch (err) {{
        setEmployeeApiStatus(`Direct failed: ${{err.message}}`, true);
      }}
    }}
    async function editEmployee(id) {{
      if (!id) return;
      const row = Array.from(document.querySelectorAll('tbody tr')).find((candidate) => candidate.firstElementChild && candidate.firstElementChild.textContent === id);
      const currentStatus = row && row.children[1] ? row.children[1].textContent : '';
      const currentRole = row && row.children[4] ? row.children[4].textContent : '';
      const currentRuntime = row && row.children[5] ? row.children[5].textContent : '';
      const name = prompt(`Name for ${{id}} (blank keeps current)`, '');
      if (name === null) return;
      const role = prompt(`Role for ${{id}}`, currentRole || 'business-agent');
      if (role === null) return;
      const runtime = prompt(`Runtime for ${{id}}`, currentRuntime || 'local');
      if (runtime === null) return;
      const status = prompt(`Status for ${{id}}: active, candidate, archived`, currentStatus || 'active');
      if (status === null) return;
      if (!['active', 'candidate', 'archived'].includes(status)) {{
        setEmployeeApiStatus(`Invalid status: ${{status}}`, true);
        return;
      }}
      setEmployeeApiStatus(`Updating ${{id}}...`, false);
      try {{
        await callCompanyApi(`/v1/employees/${{encodeURIComponent(id)}}`, {{name, role, runtime, status}}, 'PATCH');
        setEmployeeApiStatus(`Updated ${{id}}. Reloading dashboard...`, false);
        setTimeout(() => location.reload(), 800);
      }} catch (err) {{
        setEmployeeApiStatus(`Update failed: ${{err.message}}`, true);
      }}
    }}
    async function offboardEmployee(id, hardDelete) {{
      if (!id) return;
      const action = hardDelete ? 'hard delete' : 'archive';
      if (!confirm(`${{action}} employee "${{id}}"?`)) return;
      setEmployeeApiStatus(`Offboarding ${{id}}...`, false);
      try {{
        await callCompanyApi(`/v1/employees/${{encodeURIComponent(id)}}`, {{hard_delete: !!hardDelete}}, 'DELETE');
        setEmployeeApiStatus(`Offboarded ${{id}}. Reloading dashboard...`, false);
        setTimeout(() => location.reload(), 800);
      }} catch (err) {{
        setEmployeeApiStatus(`Offboard failed: ${{err.message}}`, true);
      }}
    }}
    window.addEventListener('DOMContentLoaded', checkCompanyApi);
  </script>
</body>
</html>
"""


def advanced_summary(summary: dict) -> dict:
    prepared = dict(summary)
    prepared["employees"] = employee_view_models(summary)
    return prepared


def load_advanced_template(path: str = "", *, include_external: bool = False) -> tuple[Path | None, str]:
    if path:
        candidates = [Path(path)]
    else:
        candidates = [ROOT / "dashboard_templates" / "gemini_dashboard.html"]
        if include_external or ROOT == REAL_PROJECT_ROOT:
            candidates.append(Path("/Users/shift/Documents/anti/state/dashboard.html"))
    for candidate in candidates:
        if candidate.exists():
            return candidate, candidate.read_text(encoding="utf-8")
    return None, ""


def inject_advanced_dashboard(template: str, summary: dict, *, db_path: Path, api_base: str) -> str:
    payload = json.dumps(advanced_summary(summary), ensure_ascii=False)
    payload_b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    html_text = template.replace("/Users/shift/Documents/anti", str(ROOT))

    def append_before_body(text: str, insertion: str) -> str:
        idx = text.lower().rfind("</body>")
        if idx == -1:
            return text + insertion
        return text[:idx] + insertion + text[idx:]

    html_text = re.sub(
        r"window\.kernelSummary\s*=\s*.*?;\n\s*window\.dbPath\s*=\s*.*?;",
        f"window.kernelSummary = JSON.parse(decodeURIComponent(escape(atob({json.dumps(payload_b64)}))));\n  window.dbPath = {json.dumps(str(db_path), ensure_ascii=False)};\n  window.companyApiBase = {json.dumps(api_base, ensure_ascii=False)};",
        html_text,
        count=1,
        flags=re.DOTALL,
    )
    if "window.companyApiBase" not in html_text:
        html_text = append_before_body(html_text, f"<script>window.kernelSummary = JSON.parse(decodeURIComponent(escape(atob({json.dumps(payload_b64)})))); window.dbPath = {json.dumps(str(db_path), ensure_ascii=False)}; window.companyApiBase = {json.dumps(api_base, ensure_ascii=False)};</script>")
    if "kernel-summary-debug" not in html_text:
        html_text = html_text.replace("</script>", f'  <!-- kernel-summary-debug {payload} -->\n</script>', 1)
    html_text = re.sub(
        r"<!-- MOCK SIMULATION DATA SEEDER -->\s*<script>\s*const MOCK_SUMMARY\s*=\s*.*?\n\s*</script>",
        "<!-- LIVE SNAPSHOT FALLBACK DATA -->\n<script>\n  const MOCK_SUMMARY = window.kernelSummary;\n</script>",
        html_text,
        count=1,
        flags=re.DOTALL,
    )
    html_text = html_text.replace(
        "document.getElementById('db-path-label').innerText = isSimulationMode ? 'simulation://gateway.company.internal' : 'https://gateway.company.internal';",
        "document.getElementById('db-path-label').innerText = isSimulationMode ? 'simulation://gateway.company.internal' : (window.companyApiBase || 'http://127.0.0.1:8765');",
    )
    verified_employee_script = """function isVerifiedEmployee(emp) {
    return (emp.employee_status || emp.status) === 'active';
  }

  """
    if "function isVerifiedEmployee(emp)" not in html_text:
        html_text = html_text.replace("  let isSimulationMode = false;", "  " + verified_employee_script + "let isSimulationMode = false;", 1)
    html_text = html_text.replace(
        "let summaryData = window.kernelSummary;",
        "let summaryData = window.kernelSummary;\n  window.summaryData = summaryData;",
        1,
    )
    html_text = html_text.replace(
        "summaryData = window.kernelSummary;",
        "summaryData = window.kernelSummary;\n      window.summaryData = summaryData;",
    )
    html_text = html_text.replace(
        "summaryData = JSON.parse(JSON.stringify(MOCK_SUMMARY));",
        "summaryData = JSON.parse(JSON.stringify(MOCK_SUMMARY));\n        window.summaryData = summaryData;",
    )
    html_text = html_text.replace(
        "let activeThreadId = '';",
        "let activeThreadId = '';\n  window.activeThreadId = activeThreadId;",
        1,
    )
    html_text = html_text.replace(
        "activeThreadId = threadId;",
        "activeThreadId = threadId;\n    window.activeThreadId = activeThreadId;",
    )
    html_text = html_text.replace(
        "  function renderChatMessages() {",
        "  window.populateChatHub = populateChatHub;\n  window.selectChatThread = selectChatThread;\n\n  function renderChatMessages() {",
        1,
    )
    html_text = html_text.replace(
        "  // Auto scroll chat to bottom\n    msgContainer.scrollTop = msgContainer.scrollHeight;\n  }",
        "  // Auto scroll chat to bottom\n    msgContainer.scrollTop = msgContainer.scrollHeight;\n  }\n  window.renderChatMessages = renderChatMessages;",
        1,
    )
    html_text = html_text.replace(
        "summary.employees.filter(emp => emp.status !== 'archived').map(emp => {",
        "summary.employees.filter(isVerifiedEmployee).map(emp => {",
    )
    html_text = html_text.replace(
        "const emps = summary.employees;",
        "const emps = summary.employees.filter(isVerifiedEmployee);",
    )
    html_text = html_text.replace(
        "const empIds = new Set(summary.employees.map(e => e.id));",
        "const empIds = new Set(summary.employees.filter(isVerifiedEmployee).map(e => e.id));",
    )
    html_text = html_text.replace(
        "function isVerifiedEmployee(emp) {\n    return (emp.employee_status || emp.status) === 'active';\n  }",
        "function isVerifiedEmployee(emp) {\n    return (emp.employee_status || emp.status) === 'active' && emp.id !== 'owner-shift' && emp.role !== 'human-owner' && emp.runtime !== 'human';\n  }",
    )
    if 'id="candidate-employees-container"' not in html_text:
        html_text = html_text.replace(
            """        <div class="employees-grid" id="employees-cards-container">
          <!-- Populated by JS -->
        </div>""",
            """        <div class="employees-grid" id="employees-cards-container">
          <!-- Populated by JS -->
        </div>

        <div class="section-card" style="margin-top: 20px;">
          <h2><i class="fa-solid fa-user-clock"></i> Candidate Employees</h2>
          <div class="employees-grid" id="candidate-employees-container">
            <!-- Populated by JS -->
          </div>
        </div>""",
            1,
        )
    if 'id="chat-create-btn"' not in html_text:
        html_text = html_text.replace(
            """              <div class="chat-threads-title" data-i18n="chat_threads_title">Conversations</div>
              <div id="chat-threads-list">""",
            """              <div class="chat-threads-title" data-i18n="chat_threads_title">Conversations</div>
              <div class="chat-toolbar">
                <button id="chat-create-btn" class="chat-tool-btn" onclick="window.openNewConversationComposer()"><i class="fa-solid fa-plus"></i> New</button>
                <button id="chat-refresh-btn" class="chat-tool-btn" onclick="window.refreshChatHub()"><i class="fa-solid fa-rotate"></i> Refresh</button>
              </div>
              <div id="chat-threads-list">""",
            1,
        )
        html_text = html_text.replace(
            """                <h3 id="chat-header-title">No Conversation Selected</h3>
                <span class="chat-header-participants" id="chat-header-members">Participants: -</span>""",
            """                <div class="chat-header-main">
                  <div>
                    <h3 id="chat-header-title">No Conversation Selected</h3>
                    <span class="chat-header-participants" id="chat-header-members">Participants: -</span>
                  </div>
                  <button id="chat-join-btn" class="chat-tool-btn" onclick="window.joinActiveConversation()"><i class="fa-solid fa-user-plus"></i> Insert Owner</button>
                </div>""",
            1,
        )
    if ".chat-toolbar" not in html_text:
        html_text = html_text.replace(
            "    .chat-view {\n      display: flex;\n      flex-direction: column;\n      height: 100%;\n    }",
            """    .chat-toolbar {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      padding: 10px 12px 12px;
      border-bottom: 1px solid var(--border-color);
    }

    .chat-tool-btn {
      min-height: 30px;
      border: 1px solid rgba(129, 140, 248, 0.32);
      border-radius: 6px;
      background: rgba(99, 102, 241, 0.12);
      color: #c7d2fe;
      font-size: 11px;
      font-weight: 700;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
    }

    .chat-tool-btn:hover {
      background: rgba(99, 102, 241, 0.22);
      border-color: rgba(129, 140, 248, 0.5);
    }

    .chat-header-main {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }

    .chat-view {
      display: flex;
      flex-direction: column;
      height: 100%;
    }""",
            1,
        )
    html_text = html_text.replace(
        '"soft_archive_desc": "Locks the agent, updates status to "archived", and hides it from the active roster. All files, reports, and evidence remain untouched."',
        '"soft_archive_desc": "Locks the agent, updates status to \\"archived\\", and hides it from the active roster. All files, reports, and evidence remain untouched."',
    )
    html_text = html_text.replace(
        "summaryData.employees.push(generatedRecruitData);\n    summaryData.counts.employees = summaryData.employees.length;",
        "return realOnboardGeneratedEmployee();",
    )
    if 'id="followups-tbody"' not in html_text:
        html_text = html_text.replace(
            "      <!-- Events Tab -->",
            """      <div class=\"section-card\">\n        <h2><i class=\"fa-solid fa-circle-question\"></i> Followups</h2>\n        <div class=\"table-container\">\n          <table>\n            <thead>\n              <tr>\n                <th>Followup ID</th>\n                <th>Status</th>\n                <th>Source</th>\n                <th>Target</th>\n                <th>Question</th>\n                <th>Answer</th>\n                <th>Answered</th>\n              </tr>\n            </thead>\n            <tbody id=\"followups-tbody\">\n              <!-- Populated by JS -->\n            </tbody>\n          </table>\n        </div>\n      </div>\n\n      <!-- Events Tab -->""",
            1,
        )
    html_text = html_text.replace(
        "if (isSimulationMode) {\n      if (mode === 'hard') {",
        "if (!isSimulationMode) {\n      return realOffboardEmployee(employeeToFire, mode === 'hard');\n    }\n\n    if (isSimulationMode) {\n      if (mode === 'hard') {",
    )
    html_text = html_text.replace(
        "<span class=\"badge ${hbStatus}\">${hbStatus}</span>",
        """<span class="badge ${hbStatus}">${hbStatus}</span>
              <span class="badge ${commPaused ? 'blocked' : 'completed'}" style="font-size: 9px;">${commPaused ? 'Comm Paused' : 'Comm On'}</span>
              <button class="chat-send-btn" style="padding: 2px 6px; font-size: 10px; background: rgba(16, 185, 129, 0.15); border: 1px solid rgba(16, 185, 129, 0.35); color: #86efac;" onclick="event.stopPropagation(); openDirectEmployeeMessage('${escapeHtml(emp.id)}')">
                <i class="fa-solid fa-paper-plane"></i> Direct
              </button>
              <button class="chat-send-btn" style="padding: 2px 6px; font-size: 10px; background: ${commPaused ? 'rgba(16, 185, 129, 0.15)' : 'rgba(245, 158, 11, 0.14)'}; border: 1px solid ${commPaused ? 'rgba(16, 185, 129, 0.35)' : 'rgba(245, 158, 11, 0.3)'}; color: ${commPaused ? '#86efac' : '#fbbf24'};" onclick="event.stopPropagation(); toggleEmployeeCommunication('${escapeHtml(emp.id)}', ${commPaused ? 'true' : 'false'})">
                <i class="fa-solid ${commPaused ? 'fa-play' : 'fa-pause'}"></i> ${commPaused ? 'Resume' : 'Pause'}
              </button>
              <button class="chat-send-btn" style="padding: 2px 6px; font-size: 10px; background: rgba(59, 130, 246, 0.15); border: 1px solid rgba(59, 130, 246, 0.3); color: #93c5fd;" onclick="event.stopPropagation(); openEditEmployeeProfile('${escapeHtml(emp.id)}', '${escapeHtml(emp.name)}', '${escapeHtml(emp.role)}', '${escapeHtml(emp.runtime)}', '${escapeHtml(emp.status || 'active')}')">
                <i class="fa-solid fa-pen-to-square"></i> Edit
              </button>""",
    )
    html_text = html_text.replace(
        "      const skills = emp.skills || '';\n      const skillsPills = skills ? skills.split(',').map(s => `<span class=\"capability-tag\" style=\"font-size: 9px; padding: 2px 5px; margin-top: 4px;\">${escapeHtml(s.trim())}</span>`).join(' ') : '';",
        "      const skills = emp.skills || '';\n      const skillsPills = skills ? skills.split(',').map(s => `<span class=\"capability-tag\" style=\"font-size: 9px; padding: 2px 5px; margin-top: 4px;\">${escapeHtml(s.trim())}</span>`).join(' ') : '';\n      const commPaused = !!emp.communication_paused;",
    )
    html_text = html_text.replace(
        "    }).join('');\n\n    // Approvals Table",
        """    }).join('');

    const candidateContainer = document.getElementById('candidate-employees-container');
    if (candidateContainer) {
      const candidates = summary.employees.filter(emp => (emp.employee_status || emp.status) === 'candidate');
      candidateContainer.innerHTML = candidates.length ? candidates.map(emp => {
        const skills = emp.skills || '';
        const skillsPills = skills ? skills.split(',').map(s => `<span class="capability-tag" style="font-size: 9px; padding: 2px 5px; margin-top: 4px;">${escapeHtml(s.trim())}</span>`).join(' ') : '';
        return `
          <div class="employee-card" onclick="showDetails('Candidate employee: ' + '${escapeHtml(emp.id)}', ${JSON.stringify(emp).replace(/'/g, "\\'")})">
            <div class="employee-card-header">
              <div class="employee-identity">
                <div class="employee-avatar"><i class="fa-solid fa-user-clock"></i></div>
                <div>
                  <div class="employee-name">${escapeHtml(emp.name)}</div>
                  <div class="employee-role">${escapeHtml(emp.role)}</div>
                </div>
              </div>
              <div style="display: flex; align-items: center; gap: 8px;">
                <span class="badge blocked">Candidate</span>
                <button class="chat-send-btn" style="padding: 2px 6px; font-size: 10px; background: rgba(16, 185, 129, 0.15); border: 1px solid rgba(16, 185, 129, 0.35); color: #86efac;" onclick="event.stopPropagation(); joinCandidateEmployee('${escapeHtml(emp.id)}')">
                  <i class="fa-solid fa-user-check"></i> Join Team
                </button>
                <button class="chat-send-btn" style="padding: 2px 6px; font-size: 10px; background: rgba(59, 130, 246, 0.15); border: 1px solid rgba(59, 130, 246, 0.3); color: #93c5fd;" onclick="event.stopPropagation(); openEditEmployeeProfile('${escapeHtml(emp.id)}', '${escapeHtml(emp.name)}', '${escapeHtml(emp.role)}', '${escapeHtml(emp.runtime)}', '${escapeHtml(emp.status || 'candidate')}')">
                  <i class="fa-solid fa-pen-to-square"></i> Edit
                </button>
              </div>
            </div>
            <div class="employee-meta" style="margin-top: 10px; font-size: 11px;">
              <div style="margin-bottom: 4px;"><strong>Runtime:</strong> ${escapeHtml(emp.runtime || '-')}</div>
              <div><strong>Last Active:</strong> ${formatDate(emp.last_seen_at) || 'Never'}</div>
            </div>
            <div style="display: flex; flex-wrap: wrap; gap: 4px; margin-top: 10px; border-top: 1px solid rgba(255,255,255,0.05); padding-top: 8px;">
              ${skillsPills}
            </div>
          </div>
        `;
      }).join('') : '<div style="color: var(--text-secondary); font-size: 12px; padding: 10px 0;">No candidate employees.</div>';
    }

    // Approvals Table""",
        1,
    )
    api_script = f"""
<script>
  const HUMAN_OWNER_ID = 'owner-shift';
  function getCompanyApiBase() {{
    return String(window.companyApiBase || {json.dumps(api_base)}).replace(/\\/$/, '');
  }}
  function companyApiLog(tag, text, type) {{
    if (typeof printTerminalLine === 'function') {{
      printTerminalLine({{tag, text, type}});
    }} else {{
      console.log(`[${{tag}}] ${{text}}`);
    }}
  }}
  async function companyApiGet(path) {{
    const res = await fetch(getCompanyApiBase() + path, {{method: 'GET'}});
    const data = await res.json();
    if (!res.ok || data.ok === false) {{
      throw new Error(data.error || data.message || JSON.stringify(data));
    }}
    return data;
  }}
  async function companyApiGetReachable(path) {{
    const res = await fetch(getCompanyApiBase() + path, {{method: 'GET'}});
    const data = await res.json();
    return {{res, data}};
  }}
  async function checkCompanyApi() {{
    try {{
      const {{res, data}} = await companyApiGetReachable('/v1/health');
      const employees = data.counts ? data.counts.employees : 'unknown';
      companyApiLog('SYSTEM', `Company Kernel API online: ${{getCompanyApiBase()}} employees=${{employees}} health=${{data.ok === false ? 'needs-attention' : 'ok'}}`, data.ok === false ? 'normal' : 'success');
      try {{
        const attendance = await companyApiGet('/v1/attendance/latest');
        if (attendance.counts) {{
          companyApiLog('SYSTEM', `Latest attendance: online=${{attendance.counts.online}} stalled=${{attendance.counts.worker_stalled}} no_reply=${{attendance.counts.no_reply}}`, 'success');
        }}
      }} catch (attendanceErr) {{
        companyApiLog('SYSTEM', `No latest attendance report yet: ${{attendanceErr.message}}`, 'normal');
      }}
      const label = document.getElementById('db-path-label');
      if (label && !isSimulationMode) label.innerText = getCompanyApiBase();
      const badge = document.getElementById('top-status-badge');
      const text = document.getElementById('top-status-text');
      if (badge) badge.className = data.ok === false ? 'system-status attention' : 'system-status ok';
      if (text) text.innerText = data.ok === false ? 'API ONLINE / CHECK HEALTH' : 'API ONLINE';
      return true;
    }} catch (err) {{
      companyApiLog('ERROR', `Company Kernel API offline: ${{err.message}}. Start: bin/company-api-gateway --quiet`, 'error');
      const badge = document.getElementById('top-status-badge');
      const text = document.getElementById('top-status-text');
      if (badge) badge.className = 'system-status attention';
      if (text) text.innerText = 'API OFFLINE';
      return false;
    }}
  }}
  async function companyApiRequest(path, payload, method) {{
    const res = await fetch(getCompanyApiBase() + path, {{
      method: method || 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(payload || {{}})
    }});
    const data = await res.json();
    if (!res.ok || data.ok === false) {{
      throw new Error(data.error || data.message || JSON.stringify(data));
    }}
    return data;
  }}
  async function companyApiPost(path, payload) {{
    return companyApiRequest(path, payload, 'POST');
  }}
  function getDashboardSummary() {{
    window.summaryData = window.summaryData || window.kernelSummary || {{conversations: [], employees: []}};
    window.summaryData.conversations = window.summaryData.conversations || [];
    window.summaryData.employees = window.summaryData.employees || [];
    return window.summaryData;
  }}
  function parseMentionedEmployees(text) {{
    const activeIds = new Set((getDashboardSummary().employees || []).filter(isVerifiedEmployee).map(emp => emp.id));
    document.querySelectorAll("button[onclick*='openDirectEmployeeMessage']").forEach(button => {{
      const match = String(button.getAttribute('onclick') || '').match(/openDirectEmployeeMessage\\('([^']+)'\\)/);
      if (match) activeIds.add(match[1]);
    }});
    const found = [];
    for (const match of String(text || '').matchAll(/@([a-zA-Z0-9_-]+)/g)) {{
      const id = match[1];
      if (activeIds.has(id) && !found.includes(id)) found.push(id);
    }}
    return found;
  }}
  function getActiveConversationId() {{
    if (window.activeThreadId) return window.activeThreadId;
    const active = document.querySelector('.chat-thread-item.active');
    const onclick = active ? String(active.getAttribute('onclick') || '') : '';
    const match = onclick.match(/selectChatThread\\('([^']+)'\\)/);
    return match ? match[1] : '';
  }}
  function stripMentions(text) {{
    return String(text || '').replace(/@([a-zA-Z0-9_-]+)/g, '').trim();
  }}
  function upsertConversationFromApi(result) {{
    const conversation = result.conversation || {{}};
    const message = result.message;
    if (!conversation.id) return null;
    const summary = getDashboardSummary();
    let existing = summary.conversations.find(item => item.id === conversation.id);
    if (!existing) {{
      const participants = conversation.participants || [];
      existing = {{
        id: conversation.id,
        title: conversation.title || conversation.id,
        created_by: HUMAN_OWNER_ID,
        status: conversation.status || 'open',
        participants_json: JSON.stringify(participants),
        message_count: 0,
        messages: []
      }};
      summary.conversations.unshift(existing);
    }}
    if (message && !(existing.messages || []).some(item => item.id === message.id)) {{
      existing.messages = existing.messages || [];
      existing.messages.push(message);
      existing.message_count = existing.messages.length;
      existing.last_message_at = message.created_at || new Date().toISOString();
    }}
    return existing;
  }}
  function conversationParticipants(conversation) {{
    try {{
      const parsed = JSON.parse(conversation.participants_json || '[]');
      return Array.isArray(parsed) ? parsed : [];
    }} catch (err) {{
      return [];
    }}
  }}
  function ownerInConversation(conversation) {{
    return conversationParticipants(conversation).includes(HUMAN_OWNER_ID);
  }}
  function renderRealChatMessages() {{
    const summary = getDashboardSummary();
    const conversation = summary.conversations.find(conv => conv.id === getActiveConversationId());
    const title = document.getElementById('chat-header-title');
    const members = document.getElementById('chat-header-members');
    const joinButton = document.getElementById('chat-join-btn');
    const container = document.getElementById('chat-messages-container');
    if (!conversation || !container) return;
    const participants = conversationParticipants(conversation);
    if (title) title.innerText = conversation.title || conversation.id;
    if (members) members.innerText = `Participants: ${{participants.join(', ') || '-'}}`;
    if (joinButton) {{
      joinButton.disabled = ownerInConversation(conversation);
      joinButton.innerHTML = ownerInConversation(conversation) ? '<i class="fa-solid fa-user-check"></i> Owner Inserted' : '<i class="fa-solid fa-user-plus"></i> Insert Owner';
    }}
    const messages = conversation.messages || [];
    if (!messages.length) {{
      container.innerHTML = '<div style="color: var(--text-muted); font-size: 12px; padding: 28px; text-align: center;">No messages recorded in this conversation.</div>';
      return;
    }}
    container.innerHTML = messages.map((message) => {{
      const isOwner = message.source_agent === HUMAN_OWNER_ID;
      const avatarChar = String(message.source_agent || '--').slice(0, 2).toUpperCase();
      return `
        <div class="chat-bubble-container ${{isOwner ? 'sent' : ''}}">
          <div class="chat-bubble-avatar">${{escapeHtml(avatarChar)}}</div>
          <div class="chat-bubble-wrapper">
            <div class="chat-bubble-sender">${{escapeHtml(message.source_agent || '-')}}</div>
            <div class="chat-bubble-body">${{escapeHtml(message.body || '')}}</div>
            <div class="chat-bubble-time">${{formatDate(message.created_at || message.time || '')}}</div>
          </div>
        </div>
      `;
    }}).join('');
    container.scrollTop = container.scrollHeight;
  }}
  function refreshChatHub() {{
    if (typeof window.populateChatHub === 'function') window.populateChatHub();
    renderRealChatMessages();
    companyApiLog('SYSTEM', 'Chat list refreshed from embedded live summary.', 'normal');
  }}
  async function joinActiveConversation() {{
    const conversationId = getActiveConversationId();
    if (!conversationId) {{
      companyApiLog('ERROR', 'Select a conversation before inserting owner.', 'error');
      return;
    }}
    companyApiLog('SYSTEM', `Inserting ${{HUMAN_OWNER_ID}} into ${{conversationId}}...`, 'normal');
    try {{
      const result = await companyApiPost(`/v1/conversations/${{encodeURIComponent(conversationId)}}/join`, {{agent: HUMAN_OWNER_ID}});
      const conversation = upsertConversationFromApi({{conversation: result.conversation}});
      if (conversation && result.conversation && result.conversation.participants) {{
        conversation.participants_json = JSON.stringify(result.conversation.participants);
      }}
      if (typeof window.populateChatHub === 'function') window.populateChatHub();
      renderRealChatMessages();
      companyApiLog('SYSTEM', `${{HUMAN_OWNER_ID}} inserted into conversation: ${{conversationId}}`, 'success');
    }} catch (err) {{
      companyApiLog('ERROR', `Insert owner failed: ${{err.message}}`, 'error');
    }}
  }}
  function openNewConversationComposer() {{
    const participants = prompt('Participants, comma-separated. You can also type @codex @trae in the message box.', 'codex,trae');
    if (participants === null) return;
    const body = prompt('Opening message', '');
    if (body === null) return;
    const mentions = parseMentionedEmployees(`${{participants.split(',').map(item => '@' + item.trim()).join(' ')}} ${{body}}`);
    const input = document.getElementById('chat-input-field');
    if (input) input.value = `${{mentions.map(item => '@' + item).join(' ')}} ${{body}}`.trim();
    return realSendChatMessage();
  }}
  async function realSendChatMessage() {{
    const input = document.getElementById('chat-input-field');
    const msg = input ? input.value.trim() : '';
    if (!msg) return;
    const mentions = parseMentionedEmployees(msg);
    companyApiLog('SYSTEM', mentions.length ? `Creating group conversation for @${{mentions.join(' @')}}...` : `Sending conversation reply as ${{HUMAN_OWNER_ID}}...`, 'normal');
    try {{
      let conversation = null;
      if (mentions.length > 0) {{
        const body = stripMentions(msg) || msg;
        const title = body.slice(0, 48) || `Conversation with ${{mentions.join(', ')}}`;
        const result = await companyApiPost('/v1/conversations', {{
          from: HUMAN_OWNER_ID,
          participants: [HUMAN_OWNER_ID, ...mentions].join(','),
          title,
          body
        }});
        conversation = upsertConversationFromApi(result);
      }} else {{
      const currentThreadId = getActiveConversationId();
      const current = (getDashboardSummary().conversations || []).find(item => item.id === currentThreadId) || {{id: currentThreadId, messages: []}};
        if (!current) {{
          companyApiLog('ERROR', 'Please select a conversation thread first, or @ one or more active employees.', 'error');
          return;
        }}
        const result = await companyApiPost(`/v1/conversations/${{encodeURIComponent(current.id)}}/reply`, {{
          from: HUMAN_OWNER_ID,
          body: msg
        }});
        current.messages = current.messages || [];
        current.messages.push(result.message);
        current.message_count = current.messages.length;
        current.last_message_at = result.message.created_at || new Date().toISOString();
        conversation = current;
      }}
      input.value = '';
      if (conversation) window.activeThreadId = conversation.id;
      if (typeof window.populateChatHub === 'function') window.populateChatHub();
      renderRealChatMessages();
      companyApiLog('SYSTEM', `Conversation message recorded: ${{conversation ? conversation.id : 'unknown'}}`, 'success');
    }} catch (err) {{
      companyApiLog('ERROR', `Conversation send failed: ${{err.message}}`, 'error');
    }}
  }}
  window.sendChatMessage = realSendChatMessage;
  window.joinActiveConversation = joinActiveConversation;
  window.openNewConversationComposer = openNewConversationComposer;
  window.refreshChatHub = refreshChatHub;
  function bindChatControlButtons() {{
    if (window.chatControlButtonsBound) return;
    window.chatControlButtonsBound = true;
    document.addEventListener('click', (event) => {{
      const target = event.target && event.target.closest ? event.target.closest('#chat-join-btn, #chat-create-btn, #chat-refresh-btn') : null;
      if (!target) return;
      event.preventDefault();
      event.stopPropagation();
      companyApiLog('SYSTEM', `Chat control clicked: ${{target.id}}`, 'normal');
      if (target.id === 'chat-join-btn') joinActiveConversation();
      if (target.id === 'chat-create-btn') openNewConversationComposer();
      if (target.id === 'chat-refresh-btn') refreshChatHub();
    }}, true);
  }}
  bindChatControlButtons();
  async function realOnboardGeneratedEmployee() {{
    if (!generatedRecruitData) return;
    printTerminalLine({{tag: 'SYSTEM', text: `Calling Company Kernel API to onboard '${{generatedRecruitData.id}}'...`, type: 'normal'}});
    try {{
      await companyApiPost('/v1/employees/onboard', {{
        id: generatedRecruitData.id,
        name: generatedRecruitData.name,
        role: generatedRecruitData.role,
        runtime: generatedRecruitData.runtime,
        workspace: generatedRecruitData.workspace,
        skills: generatedRecruitData.skills,
        tools: generatedRecruitData.tools,
        task_types: generatedRecruitData.task_types,
        open_communication: true,
        create_test_task: false
      }});
      printTerminalLine({{tag: 'SYSTEM', text: `Onboarded '${{generatedRecruitData.id}}'. Reloading live dashboard...`, type: 'success'}});
      setTimeout(() => location.reload(), 800);
    }} catch (err) {{
      printTerminalLine({{tag: 'ERROR', text: `Onboard failed: ${{err.message}}`, type: 'error'}});
    }}
  }}
  async function realDirectEmployeeMessage(id, source, body) {{
    companyApiLog('SYSTEM', `Calling Company Kernel API direct message '${{source}}' -> '${{id}}'...`, 'normal');
    try {{
      const result = await companyApiPost('/v1/messages/direct', {{from: source || 'main', to: id, body}});
      companyApiLog('SYSTEM', `Direct reply from '${{id}}': ${{result.reply || '(empty)'}}; evidence=${{result.file || 'n/a'}}`, 'success');
      return result;
    }} catch (err) {{
      companyApiLog('ERROR', `Direct message failed: ${{err.message}}`, 'error');
      return null;
    }}
  }}
  function openDirectEmployeeMessage(id) {{
    const source = prompt(`Source employee for direct message to ${{id}}`, 'main');
    if (source === null) return;
    const body = prompt(`Message to ${{id}}`, `只回复：${{id}}_DIRECT_OK`);
    if (body === null) return;
    return realDirectEmployeeMessage(id, source, body);
  }}
  async function realUpdateEmployeeProfile(id, payload) {{
    companyApiLog('SYSTEM', `Calling Company Kernel API to update '${{id}}'...`, 'normal');
    try {{
      await companyApiRequest(`/v1/employees/${{encodeURIComponent(id)}}`, payload || {{}}, 'PATCH');
      companyApiLog('SYSTEM', `Updated '${{id}}'. Reloading live dashboard...`, 'success');
      setTimeout(() => location.reload(), 800);
    }} catch (err) {{
      companyApiLog('ERROR', `Update failed: ${{err.message}}`, 'error');
    }}
  }}
  async function toggleEmployeeCommunication(id, enabled) {{
    companyApiLog('SYSTEM', `${{enabled ? 'Resuming' : 'Pausing'}} communication for '${{id}}'...`, 'normal');
    try {{
      await companyApiPost(`/v1/employees/${{encodeURIComponent(id)}}/communication`, {{enabled}});
      companyApiLog('SYSTEM', `${{enabled ? 'Resumed' : 'Paused'}} communication for '${{id}}'. Reloading live dashboard...`, 'success');
      setTimeout(() => location.reload(), 800);
    }} catch (err) {{
      companyApiLog('ERROR', `Communication update failed: ${{err.message}}`, 'error');
    }}
  }}
  function joinCandidateEmployee(id) {{
    if (!id) return;
    if (!confirm(`Join candidate "${{id}}" to the active team?`)) return;
    return realUpdateEmployeeProfile(id, {{status: 'active'}});
  }}
  function openEditEmployeeProfile(id, currentName, currentRole, currentRuntime, currentStatus) {{
    const name = prompt(`Name for ${{id}}`, currentName || id);
    if (name === null) return;
    const role = prompt(`Role for ${{id}}`, currentRole || 'business-agent');
    if (role === null) return;
    const runtime = prompt(`Runtime for ${{id}}`, currentRuntime || 'local');
    if (runtime === null) return;
    const status = prompt(`Status for ${{id}}: active, candidate, archived`, currentStatus || 'active');
    if (status === null) return;
    if (!['active', 'candidate', 'archived'].includes(status)) {{
      companyApiLog('ERROR', `Invalid employee status: ${{status}}`, 'error');
      return;
    }}
    return realUpdateEmployeeProfile(id, {{name, role, runtime, status}});
  }}
  async function realOffboardEmployee(id, hardDelete) {{
    printTerminalLine({{tag: 'SYSTEM', text: `Calling Company Kernel API to offboard '${{id}}'...`, type: 'normal'}});
    try {{
      await companyApiRequest(`/v1/employees/${{encodeURIComponent(id)}}`, {{hard_delete: !!hardDelete}}, 'DELETE');
      printTerminalLine({{tag: 'SYSTEM', text: `Offboarded '${{id}}'. Reloading live dashboard...`, type: 'success'}});
      setTimeout(() => location.reload(), 800);
    }} catch (err) {{
      printTerminalLine({{tag: 'ERROR', text: `Offboard failed: ${{err.message}}`, type: 'error'}});
    }}
  }}
  function populateFollowups() {{
    const summary = window.summaryData || window.kernelSummary || {{followups: []}};
    const tbody = document.getElementById('followups-tbody');
    if (!tbody) return;
    const followups = summary.followups || [];
    tbody.innerHTML = followups.map(item => `
      <tr onclick="showDetails('Followup: ' + '${{escapeHtml(item.id)}}', ${{JSON.stringify(item).replace(/'/g, "\\'")}})">
        <td style="font-family: monospace;">${{escapeHtml(item.id)}}</td>
        <td><span class="badge ${{escapeHtml(item.status || 'submitted')}}">${{escapeHtml(item.status || '')}}</span></td>
        <td>${{escapeHtml(item.source_agent || '-')}}</td>
        <td>${{escapeHtml(item.target_agent || '-')}}</td>
        <td>${{escapeHtml(item.question || '-')}}</td>
        <td>${{escapeHtml(item.answer || '-')}}</td>
        <td>${{formatDate(item.answered_at || '')}}</td>
      </tr>
    `).join('');
  }}
  document.addEventListener('DOMContentLoaded', () => {{
    window.sendChatMessage = realSendChatMessage;
    bindChatControlButtons();
    document.getElementById('chat-join-btn')?.addEventListener('click', (event) => {{
      event.preventDefault();
      joinActiveConversation();
    }});
    document.getElementById('chat-create-btn')?.addEventListener('click', (event) => {{
      event.preventDefault();
      openNewConversationComposer();
    }});
    document.getElementById('chat-refresh-btn')?.addEventListener('click', (event) => {{
      event.preventDefault();
      refreshChatHub();
    }});
    if (typeof simulateAgentResponse === 'function') {{
      window.simulateAgentResponse = function() {{}};
    }}
    if (typeof window.renderChatMessages === 'function') {{
      window.renderChatMessages = renderRealChatMessages;
    }}
  }});
  document.addEventListener('DOMContentLoaded', () => setTimeout(populateFollowups, 200));
  window.addEventListener('DOMContentLoaded', checkCompanyApi);
</script>
"""
    return append_before_body(html_text, api_script + "\n")


def run(args: argparse.Namespace) -> int:
    conn = connect()
    try:
        summary = load_summary(conn)
    finally:
        conn.close()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    template_path = None
    variant = args.variant
    if variant in {"advanced", "auto"}:
        template_path, template = load_advanced_template(args.template, include_external=variant == "advanced")
        if template:
            output.write_text(inject_advanced_dashboard(template, summary, db_path=DB_PATH, api_base=args.api_base), encoding="utf-8")
            variant = "advanced"
        elif variant == "advanced":
            raise SystemExit("advanced dashboard template not found")
        else:
            output.write_text(render(summary), encoding="utf-8")
            variant = "basic"
    else:
        output.write_text(render(summary), encoding="utf-8")
    print(json.dumps({"ok": True, "output": str(output), "variant": variant, "template": str(template_path or ""), "counts": summary["counts"]}, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate Company Kernel static dashboard")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--variant", choices=["auto", "basic", "advanced"], default="auto")
    parser.add_argument("--template", default="")
    parser.add_argument("--api-base", default="http://127.0.0.1:8765")
    return parser


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
