from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "company.sqlite"
SCHEMA = ROOT / "company_kernel" / "schema.sql"


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def emit(obj: dict[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def connect(db_path: Path | None = None, schema_path: Path | None = None) -> sqlite3.Connection:
    db_path = (db_path or DB_PATH).expanduser().resolve()
    schema_path = (schema_path or SCHEMA).expanduser().resolve()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(schema_path.read_text(encoding="utf-8"))
    conn.commit()
    return conn


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_json(path: Path) -> dict[str, Any]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def employee(conn: sqlite3.Connection, agent: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM employees WHERE id = ?", (agent,)).fetchone()


def active_task(conn: sqlite3.Connection, agent: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM tasks
        WHERE target_agent = ?
          AND status IN ('submitted', 'claimed')
        ORDER BY updated_at DESC, created_at DESC
        LIMIT 1
        """,
        (agent,),
    ).fetchone()


def progress_files(workspace: Path) -> list[Path]:
    reports = workspace / "reports"
    if not reports.exists():
        return []
    return sorted(reports.glob("progress_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)


def progress_task_id(data: dict[str, Any]) -> str:
    report = data.get("report") if isinstance(data.get("report"), dict) else {}
    return str(data.get("task_id") or report.get("task_id") or "").strip()


def latest_progress(workspace: Path, task_id: str = "") -> tuple[Path | None, dict[str, Any]]:
    for path in progress_files(workspace):
        data = load_json(path)
        report = data.get("report")
        if isinstance(report, dict):
            if task_id and progress_task_id(data) != task_id:
                continue
            return path, data
    return None, {}


def progress_history(workspace: Path, task_id: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not task_id:
        return items
    for path in sorted(progress_files(workspace), key=lambda p: p.stat().st_mtime):
        data = load_json(path)
        report = data.get("report")
        if not isinstance(report, dict):
            continue
        if progress_task_id(data) != task_id:
            continue
        items.append(
            {
                "task_id": task_id,
                "state": progress_state(data),
                "action": progress_action(data, ""),
                "created_at": progress_created_at(data, ""),
                "path": str(path.resolve()),
            }
        )
    return items


def progress_state(data: dict[str, Any]) -> str:
    report = data.get("report") if isinstance(data.get("report"), dict) else {}
    return str(report.get("state") or data.get("state") or "").strip()


def progress_layer(data: dict[str, Any]) -> str:
    state = progress_state(data).lower().replace("-", "_").replace(" ", "_")
    if state in {"received", "acknowledged", "ack", "claimed"}:
        return "received"
    if state in {"working", "in_progress", "actively_progressing", "active", "running"}:
        return "working"
    if state in {"waiting", "blocked_on_input_or_dependency", "awaiting_input", "awaiting_dependency", "pending_input"}:
        return "waiting"
    if state in {"blocked", "failed_to_progress", "error", "stalled", "failed"}:
        return "blocked"
    if state in {"done", "verified_complete", "completed", "complete", "success"}:
        return "done"
    return ""


def progress_created_at(data: dict[str, Any], fallback: str) -> str:
    report = data.get("report") if isinstance(data.get("report"), dict) else {}
    return str(report.get("created_at") or data.get("created_at") or fallback)


def progress_action(data: dict[str, Any], fallback: str) -> str:
    report = data.get("report") if isinstance(data.get("report"), dict) else {}
    action = str(report.get("action") or "").strip()
    return action or fallback


def short_text(value: Any, limit: int = 42) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if not text:
        return "未命名任务"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def write_pm_report(agent: str, result: dict[str, Any], report_root: Path | None = None) -> Path:
    report_root = (report_root or ROOT).expanduser().resolve()
    out_dir = report_root / "employees" / "hermes" / "reports" / "codex-pm"
    out_dir.mkdir(parents=True, exist_ok=True)
    task_id = str(result.get("task_id") or "no-task")
    out = out_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{task_id}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out


def close_task(conn: sqlite3.Connection, task: sqlite3.Row, status: str, summary: str, evidence: str, ts: str) -> None:
    if status == "completed":
        conn.execute(
            """
            UPDATE tasks
            SET status = 'completed', summary = ?, evidence_path = ?, blocker = '', updated_at = ?
            WHERE id = ?
            """,
            (summary, evidence, ts, task["id"]),
        )
    elif status in {"blocked", "stalled"}:
        conn.execute(
            """
            UPDATE tasks
            SET status = 'claimed', blocker = ?, evidence_path = ?, updated_at = ?
            WHERE id = ?
            """,
            (summary, evidence, ts, task["id"]),
        )
    conn.commit()


def supervise_once(
    agent: str = "codex",
    now_ts: str | None = None,
    stale_minutes: int = 15,
    close_completed: bool = True,
    *,
    db_path: Path | None = None,
    schema_path: Path | None = None,
    workspace: Path | None = None,
    report_root: Path | None = None,
) -> dict[str, Any]:
    ts = now_ts or now()
    db_path = (db_path or DB_PATH).expanduser().resolve()
    schema_path = (schema_path or SCHEMA).expanduser().resolve()
    report_root = (report_root or ROOT).expanduser().resolve()
    conn = connect(db_path=db_path, schema_path=schema_path)
    try:
        emp = employee(conn, agent)
        if not emp:
            result = {
                "ok": False,
                "status": "blocked",
                "agent": agent,
                "blocker": "unknown_codex_employee",
                "timestamp": ts,
                "db_path": str(db_path),
                "workspace": str(workspace.expanduser().resolve()) if workspace else "",
            }
            result["report_path"] = str(write_pm_report(agent, result, report_root=report_root))
            return result
        task = active_task(conn, agent)
        effective_workspace = workspace.expanduser().resolve() if workspace else Path(emp["workspace"]).expanduser().resolve()
        progress_path, progress = latest_progress(effective_workspace, str(task["id"]) if task else "")
        if not task:
            result = {
                "ok": True,
                "status": "idle",
                "agent": agent,
                "human_message": "Codex 当前没有待监督任务。",
                "timestamp": ts,
                "db_path": str(db_path),
                "workspace": str(effective_workspace),
            }
            result["report_path"] = str(write_pm_report(agent, result, report_root=report_root))
            return result
        task_title = short_text(task["title"])
        if not progress_path:
            result = {
                "ok": True,
                "status": "stalled",
                "agent": agent,
                "task_id": task["id"],
                "human_message": f"Codex 卡住：{task_title} 没有进度证据，owner=hermes",
                "evidence_path": str(effective_workspace / "reports"),
                "timestamp": ts,
                "db_path": str(db_path),
                "workspace": str(effective_workspace),
                "progress_history": [],
            }
            result["report_path"] = str(write_pm_report(agent, result, report_root=report_root))
            return result
        history = progress_history(effective_workspace, str(task["id"]))
        state = progress_state(progress)
        layer = progress_layer(progress)
        action = short_text(progress_action(progress, task_title))
        evidence = str(progress_path)
        if state == "completed":
            result = {
                "ok": True,
                "status": "completed",
                "agent": agent,
                "task_id": task["id"],
                "human_message": f"完成了 Codex 的 {task_title} 任务",
                "action": action,
                "evidence_path": evidence,
                "latest_progress_path": evidence,
                "progress_layer": layer,
                "progress_state": state,
                "timestamp": ts,
                "db_path": str(db_path),
                "workspace": str(effective_workspace),
                "progress_history": history,
            }
            if close_completed:
                close_task(conn, task, "completed", result["human_message"], evidence, ts)
            result["report_path"] = str(write_pm_report(agent, result, report_root=report_root))
            return result
        if state == "blocked":
            result = {
                "ok": True,
                "status": "blocked",
                "agent": agent,
                "task_id": task["id"],
                "human_message": f"Codex 阻塞：{task_title}，owner=hermes",
                "action": action,
                "evidence_path": evidence,
                "latest_progress_path": evidence,
                "progress_layer": layer,
                "progress_state": state,
                "timestamp": ts,
                "db_path": str(db_path),
                "workspace": str(effective_workspace),
                "progress_history": history,
            }
            close_task(conn, task, "blocked", result["human_message"], evidence, ts)
            result["report_path"] = str(write_pm_report(agent, result, report_root=report_root))
            return result
        created = parse_time(progress_created_at(progress, ts))
        age_minutes = (parse_time(ts) - created).total_seconds() / 60
        if state in {"acknowledged", "in_progress"} and age_minutes > stale_minutes:
            result = {
                "ok": True,
                "status": "stalled",
                "agent": agent,
                "task_id": task["id"],
                "human_message": f"Codex 卡住：{task_title} 超过 {stale_minutes} 分钟无完成，owner=hermes",
                "action": action,
                "evidence_path": evidence,
                "latest_progress_path": evidence,
                "progress_layer": layer,
                "progress_state": state,
                "timestamp": ts,
                "db_path": str(db_path),
                "workspace": str(effective_workspace),
                "progress_history": history,
            }
            close_task(conn, task, "stalled", result["human_message"], evidence, ts)
            result["report_path"] = str(write_pm_report(agent, result, report_root=report_root))
            return result
        result = {
            "ok": True,
            "status": state or "in_progress",
            "agent": agent,
            "task_id": task["id"],
            "human_message": f"Codex 正在处理 {task_title}",
            "action": action,
            "evidence_path": evidence,
            "latest_progress_path": evidence,
            "progress_layer": layer,
            "progress_state": state,
            "timestamp": ts,
            "db_path": str(db_path),
            "workspace": str(effective_workspace),
            "progress_history": history,
        }
        result["report_path"] = str(write_pm_report(agent, result, report_root=report_root))
        return result
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hermes PM supervisor for Codex execution progress.")
    parser.add_argument("--agent", default="codex")
    parser.add_argument("--stale-minutes", type=int, default=15)
    parser.add_argument("--no-close-completed", action="store_true")
    parser.add_argument("--db-path", default=str(DB_PATH))
    parser.add_argument("--schema-path", default=str(SCHEMA))
    parser.add_argument("--workspace", default="")
    parser.add_argument("--report-root", default=str(ROOT))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workspace = Path(args.workspace).expanduser() if args.workspace else None
    result = supervise_once(
        agent=args.agent,
        stale_minutes=args.stale_minutes,
        close_completed=not args.no_close_completed,
        db_path=Path(args.db_path),
        schema_path=Path(args.schema_path),
        workspace=workspace,
        report_root=Path(args.report_root),
    )
    emit(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
