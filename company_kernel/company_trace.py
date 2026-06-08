from __future__ import annotations

import argparse
import html
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import companyctl
from .schema_migrations import ensure_schema_migrations


ROOT = companyctl.ROOT
DB_PATH = companyctl.DB_PATH
SCHEMA = companyctl.SCHEMA
DEFAULT_OUTPUT_DIR = ROOT / "state" / "traces"


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
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def e(value: object) -> str:
    return html.escape("" if value is None else str(value))


def resolve_trace_id(conn: sqlite3.Connection, trace_id: str = "", task_id: str = "") -> str:
    if trace_id:
        return trace_id
    if not task_id:
        raise SystemExit("pass --trace-id or --task-id")
    metadata = companyctl.task_metadata(conn, task_id)
    trace = str(metadata.get("trace_id", "") or "")
    if not trace:
        raise SystemExit(f"trace_id not found for task: {task_id}")
    return trace


def load_trace(conn: sqlite3.Connection, trace_id: str) -> dict:
    task_ids = set()
    for row in rows(conn, "SELECT task_id, metadata_json FROM task_metadata"):
        try:
            metadata = json.loads(row.get("metadata_json", "{}") or "{}")
        except json.JSONDecodeError:
            metadata = {}
        if metadata.get("trace_id") == trace_id and row.get("task_id"):
            task_ids.add(row["task_id"])
    task_ids.update(row["task_id"] for row in rows(conn, "SELECT DISTINCT task_id FROM company_events WHERE trace_id = ? AND task_id != ''", (trace_id,)))
    task_ids.update(row["task_id"] for row in rows(conn, "SELECT DISTINCT task_id FROM adapter_runs WHERE trace_id = ? AND task_id != ''", (trace_id,)))
    task_ids.update(row["task_id"] for row in rows(conn, "SELECT DISTINCT task_id FROM artifacts WHERE trace_id = ? AND task_id != ''", (trace_id,)))
    task_ids.update(row["task_id"] for row in rows(conn, "SELECT DISTINCT task_id FROM evidence WHERE trace_id = ? AND task_id != ''", (trace_id,)))
    task_ids.update(row["from_task_id"] for row in rows(conn, "SELECT DISTINCT from_task_id FROM handoffs WHERE trace_id = ? AND from_task_id != ''", (trace_id,)))
    task_ids.update(row["to_task_id"] for row in rows(conn, "SELECT DISTINCT to_task_id FROM handoffs WHERE trace_id = ? AND to_task_id != ''", (trace_id,)))
    task_ids.update(row["task_id"] for row in rows(conn, "SELECT DISTINCT task_id FROM execution_attempts WHERE trace_id = ? AND task_id != ''", (trace_id,)))
    tasks = []
    if task_ids:
        placeholders = ",".join("?" for _ in task_ids)
        tasks = rows(conn, f"SELECT * FROM tasks WHERE id IN ({placeholders}) ORDER BY created_at ASC", tuple(sorted(task_ids)))
    events = rows(conn, "SELECT * FROM company_events WHERE trace_id = ? ORDER BY created_at ASC", (trace_id,))
    adapter_runs = rows(conn, "SELECT * FROM adapter_runs WHERE trace_id = ? ORDER BY created_at ASC", (trace_id,))
    artifacts = rows(conn, "SELECT * FROM artifacts WHERE trace_id = ? ORDER BY created_at ASC", (trace_id,))
    evidence = rows(conn, "SELECT * FROM evidence WHERE trace_id = ? ORDER BY created_at ASC", (trace_id,))
    handoffs = rows(conn, "SELECT * FROM handoffs WHERE trace_id = ? ORDER BY created_at ASC", (trace_id,))
    execution_attempts = rows(conn, "SELECT * FROM execution_attempts WHERE trace_id = ? ORDER BY started_at ASC", (trace_id,))
    timeline = []
    for task in tasks:
        timeline.append({"kind": "task", "at": task["created_at"], "label": f"task {task['id']} submitted to {task['target_agent']}", "status": task["status"], "task_id": task["id"]})
        if task.get("updated_at") and task["updated_at"] != task["created_at"]:
            timeline.append({"kind": "task", "at": task["updated_at"], "label": f"task {task['id']} {task['status']}", "status": task["status"], "task_id": task["id"]})
    for event in events:
        timeline_item = {"kind": "event", "at": event["created_at"], "label": event["event_type"], "status": "processed" if event.get("processed_at") else "pending", "event_id": event["id"], "task_id": event.get("task_id", ""), "actor": event.get("source_agent", "")}
        if event["event_type"] in {"supervisor.correction_requested", "supervisor.correction_acknowledged"}:
            try:
                payload = json.loads(event.get("payload_json", "{}") or "{}")
            except json.JSONDecodeError:
                payload = {}
            action = "correction_acknowledged" if event["event_type"] == "supervisor.correction_acknowledged" else "correction_requested"
            timeline_item.update(
                {
                    "action": action,
                    "attempt_id": str(payload.get("attempt_id", "") or ""),
                    "target": "hermes" if action == "correction_acknowledged" else "",
                    "message": payload.get("message", ""),
                }
            )
        timeline.append(timeline_item)
    for run in adapter_runs:
        timeline.append({"kind": "adapter", "at": run["created_at"], "label": f"{run['agent_id']} {run['command']}", "status": "ok" if run.get("ok") else "failed", "run_id": run["id"], "task_id": run.get("task_id", ""), "attempt": run.get("attempt", 1)})
    for artifact in artifacts:
        timeline.append({"kind": "artifact", "at": artifact["created_at"], "label": f"{artifact['name']} v{artifact['version']} {artifact['stage']}", "status": artifact["status"], "artifact_id": artifact["artifact_id"], "task_id": artifact["task_id"]})
    for item in evidence:
        timeline.append({"kind": "evidence", "at": item["created_at"], "label": item["summary"] or item["path_or_url"], "status": "final" if item.get("is_final") else "created", "evidence_id": item["evidence_id"], "task_id": item["task_id"]})
    for handoff in handoffs:
        timeline.append({"kind": "handoff", "at": handoff["created_at"], "label": f"{handoff['from_task_id']} -> {handoff['to_task_id']}: {handoff['summary']}", "status": handoff["status"], "handoff_id": handoff["handoff_id"], "task_id": handoff["from_task_id"]})
    for attempt in execution_attempts:
        timeline.append({"kind": "attempt", "at": attempt["started_at"], "label": f"{attempt['employee_id']} via {attempt['adapter_type']}", "status": attempt["status"], "attempt_id": attempt["attempt_id"], "task_id": attempt["task_id"]})
        if attempt.get("finished_at"):
            timeline.append({"kind": "attempt", "at": attempt["finished_at"], "label": f"{attempt['attempt_id']} finished", "status": attempt["status"], "attempt_id": attempt["attempt_id"], "task_id": attempt["task_id"]})
    timeline.sort(key=lambda item: item.get("at", ""))
    return {
        "trace_id": trace_id,
        "generated_at": now(),
        "tasks": tasks,
        "events": events,
        "adapter_runs": adapter_runs,
        "artifacts": artifacts,
        "evidence": evidence,
        "handoffs": handoffs,
        "execution_attempts": execution_attempts,
        "timeline": timeline,
    }


def safe_trace_payload(trace: dict) -> dict:
    evidence_by_id = {item.get("evidence_id", ""): item for item in trace.get("evidence", [])}
    artifact_by_id = {item.get("artifact_id", ""): item for item in trace.get("artifacts", [])}
    adapter_by_id = {item.get("id", ""): item for item in trace.get("adapter_runs", [])}
    task_target_by_id = {item.get("id", ""): item.get("target_agent", "") for item in trace.get("tasks", [])}
    timeline = []
    supervision_chain = []
    for raw_item in trace.get("timeline", []):
        item = {
            "kind": raw_item.get("kind", ""),
            "at": raw_item.get("at", ""),
            "status": raw_item.get("status", ""),
            "label": companyctl.sanitize_log_text(raw_item.get("label", "")),
            "task_id": raw_item.get("task_id", ""),
        }
        for key in ("event_id", "run_id", "artifact_id", "evidence_id", "handoff_id", "attempt_id", "attempt", "actor", "target", "action"):
            if raw_item.get(key) not in {None, ""}:
                item[key] = raw_item[key]
        if item.get("action") in {"correction_requested", "correction_acknowledged"}:
            if not item.get("target"):
                item["target"] = task_target_by_id.get(item.get("task_id", ""), "")
            item["message"] = companyctl.sanitize_log_text(raw_item.get("message", ""))
            item["summary"] = f"{item.get('actor', '-') or '-'} -> {item.get('target', '-') or '-'} · {item['action']} · {item.get('task_id', '-') or '-'}"
            supervision_chain.append(
                {
                    "at": item.get("at", ""),
                    "actor": item.get("actor", ""),
                    "target": item.get("target", ""),
                    "action": item.get("action", ""),
                    "task_id": item.get("task_id", ""),
                    "attempt_id": item.get("attempt_id", ""),
                    "summary": item["summary"],
                    "message": item["message"],
                }
            )
        if item.get("evidence_id"):
            evidence = evidence_by_id.get(item["evidence_id"], {})
            display = companyctl.sanitize_evidence_path_for_display(str(evidence.get("path_or_url", "")))
            item["display"] = display
            item["label"] = companyctl.sanitize_log_text(evidence.get("summary") or display.get("relative_path") or display.get("basename") or item["label"])
        if item.get("artifact_id"):
            artifact = artifact_by_id.get(item["artifact_id"], {})
            item["display"] = companyctl.sanitize_evidence_path_for_display(str(artifact.get("path", "")))
        if item.get("run_id"):
            adapter_run = adapter_by_id.get(item["run_id"], {})
            try:
                result = json.loads(adapter_run.get("result_json", "{}") or "{}")
            except json.JSONDecodeError:
                result = {"raw": adapter_run.get("result_json", "")}
            summary = companyctl.summarize_adapter_result(result)
            if summary.get("sanitized_log"):
                item["sanitized_log"] = summary["sanitized_log"]
        timeline.append(item)
    return {
        "ok": True,
        "source": "trace.timeline",
        "trace_id": trace.get("trace_id", ""),
        "generated_at": trace.get("generated_at", ""),
        "counts": {
            "tasks": len(trace.get("tasks", [])),
            "events": len(trace.get("events", [])),
            "adapter_runs": len(trace.get("adapter_runs", [])),
            "artifacts": len(trace.get("artifacts", [])),
            "handoffs": len(trace.get("handoffs", [])),
            "evidence": len(trace.get("evidence", [])),
            "execution_attempts": len(trace.get("execution_attempts", [])),
            "timeline": len(timeline),
        },
        "tasks": [
            {
                "id": item.get("id", ""),
                "source_agent": item.get("source_agent", ""),
                "target_agent": item.get("target_agent", ""),
                "status": item.get("status", ""),
                "title": item.get("title", ""),
                "created_at": item.get("created_at", ""),
                "updated_at": item.get("updated_at", ""),
            }
            for item in trace.get("tasks", [])
        ],
        "supervision_chain": supervision_chain,
        "timeline": timeline,
    }


def render_html(trace: dict) -> str:
    rows_html = []
    for item in trace["timeline"]:
        width = "70%" if item["kind"] == "adapter" else "45%" if item["kind"] == "event" else "35%"
        color = "#2f6fed" if item["status"] in {"ok", "completed", "processed"} else "#c2410c" if item["status"] in {"failed", "blocked", "pending"} else "#64748b"
        rows_html.append(
            "<tr>"
            f"<td>{e(item.get('at', ''))}</td>"
            f"<td>{e(item.get('kind', ''))}</td>"
            f"<td>{e(item.get('status', ''))}</td>"
            f"<td><div class='bar' style='width:{width};background:{color}'>{e(item.get('label', ''))}</div></td>"
            f"<td>{e(item.get('task_id', ''))}</td>"
            f"<td>{e(item.get('event_id', item.get('run_id', '')))}</td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html lang="zh">
<head>
  <meta charset="utf-8">
  <title>Trace {e(trace['trace_id'])}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 24px; color: #202124; background: #f8fafc; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; }}
    th, td {{ padding: 9px 10px; border-bottom: 1px solid #e5e7eb; text-align: left; font-size: 13px; vertical-align: top; }}
    th {{ background: #eef2f7; }}
    .bar {{ color: #fff; padding: 6px 8px; border-radius: 4px; min-width: 140px; overflow-wrap: anywhere; }}
    .meta {{ color: #64748b; margin-bottom: 16px; }}
  </style>
</head>
<body>
  <h1>Trace {e(trace['trace_id'])}</h1>
  <div class="meta">generated_at={e(trace['generated_at'])}; tasks={len(trace['tasks'])}; events={len(trace['events'])}; adapter_runs={len(trace['adapter_runs'])}; artifacts={len(trace.get('artifacts', []))}; handoffs={len(trace.get('handoffs', []))}; evidence={len(trace.get('evidence', []))}; attempts={len(trace.get('execution_attempts', []))}</div>
  <table>
    <thead><tr><th>time</th><th>kind</th><th>status</th><th>timeline</th><th>task</th><th>id</th></tr></thead>
    <tbody>{''.join(rows_html)}</tbody>
  </table>
</body>
</html>
"""


def write_outputs(trace: dict, output: Path | None = None) -> dict:
    out = output or (DEFAULT_OUTPUT_DIR / f"{trace['trace_id']}.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(trace), encoding="utf-8")
    json_path = out.with_suffix(".json")
    json_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"html": str(out), "json": str(json_path)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export Company Kernel trace timeline")
    parser.add_argument("--trace-id", default="")
    parser.add_argument("--task-id", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--json-only", action="store_true")
    args = parser.parse_args(argv)
    conn = connect()
    try:
        trace_id = resolve_trace_id(conn, args.trace_id, args.task_id)
        trace = load_trace(conn, trace_id)
    finally:
        conn.close()
    if args.json_only:
        print(json.dumps({"ok": True, **trace}, ensure_ascii=False, indent=2))
        return 0
    files = write_outputs(trace, Path(args.output) if args.output else None)
    print(
        json.dumps(
            {
                "ok": True,
                "trace_id": trace_id,
                "files": files,
                "counts": {
                    "tasks": len(trace["tasks"]),
                    "events": len(trace["events"]),
                    "adapter_runs": len(trace["adapter_runs"]),
                    "artifacts": len(trace.get("artifacts", [])),
                    "handoffs": len(trace.get("handoffs", [])),
                    "evidence": len(trace.get("evidence", [])),
                    "execution_attempts": len(trace.get("execution_attempts", [])),
                    "timeline": len(trace["timeline"]),
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
