#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATUSES = ("online", "session_missing", "worker_stalled", "heartbeat_disabled", "no_reply")


def openclaw_root() -> Path:
    env = os.environ.get("OPENCLAW_ROOT")
    if env:
        return Path(env).expanduser()
    if Path("/Users/owner/openclaw").exists():
        return Path("/Users/owner/openclaw")
    return Path.home() / "openclaw"


ROOT = openclaw_root()
SCRIPT_WORKSPACE = Path(__file__).resolve().parents[1]
WORKSPACE = Path(os.environ.get("OPENCLAW_WORKSPACE", ROOT / "workspace-main")).expanduser()
if "OPENCLAW_WORKSPACE" not in os.environ and Path("/Users/owner/openclaw/workspace-xmanx").exists():
    WORKSPACE = Path("/Users/owner/openclaw/workspace-xmanx")
REGISTRY = Path(os.environ.get("OPENCLAW_AGENT_REGISTRY", WORKSPACE / "config" / "agent_registry.json")).expanduser()


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def safe_read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def load_registry_agents() -> dict[str, dict[str, Any]]:
    data = safe_read_json(REGISTRY, {"agents": {}})
    agents = data.get("agents")
    return agents if isinstance(agents, dict) else {}


def discover_openclaw_agents(timeout: int) -> dict[str, dict[str, Any]]:
    try:
        cp = subprocess.run(
            ["openclaw", "agents", "list", "--json"],
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except Exception:
        return {}
    if cp.returncode != 0:
        return {}
    try:
        rows = json.loads(cp.stdout or "[]")
    except json.JSONDecodeError:
        return {}
    discovered: dict[str, dict[str, Any]] = {}
    for row in rows if isinstance(rows, list) else []:
        agent_id = str(row.get("id") or "").strip()
        if not agent_id:
            continue
        discovered[agent_id] = {
            "workspace": row.get("workspace"),
            "role": row.get("name") or row.get("identityName") or "openclaw agent",
            "source": "openclaw_agents_list",
        }
    return discovered


def load_agents(include_discovered: bool, timeout: int) -> dict[str, dict[str, Any]]:
    agents = {aid: dict(info, source="agent_registry") for aid, info in load_registry_agents().items()}
    if include_discovered:
        for aid, info in discover_openclaw_agents(timeout).items():
            agents.setdefault(aid, info)
    return agents


def parse_agents_filter(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def session_candidates(agent_id: str) -> list[Path]:
    names = [agent_id, agent_id.replace("_", "-"), agent_id.replace("-", "_")]
    if agent_id == "nestcar":
        names.append("car-rental")
    if agent_id in {"hermes", "default"}:
        names.extend(["default", "hermes"])
    seen: set[Path] = set()
    out: list[Path] = []
    for name in names:
        p = ROOT / "agents" / name / "sessions" / "sessions.json"
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def session_probe(agent_id: str) -> dict[str, Any]:
    for path in session_candidates(agent_id):
        if not path.exists():
            continue
        payload = safe_read_json(path, None)
        count = 0
        if isinstance(payload, dict):
            count = len(payload)
        elif isinstance(payload, list):
            count = len(payload)
        return {
            "path": str(path),
            "exists": True,
            "bytes": path.stat().st_size,
            "session_count": count,
        }
    return {"path": str(session_candidates(agent_id)[0]), "exists": False, "bytes": 0, "session_count": 0}


def spool_candidates(agent_id: str) -> list[Path]:
    names = [agent_id, agent_id.replace("-", "_"), agent_id.replace("_", "-")]
    if agent_id == "main":
        names.append("default")
    if agent_id in {"hermes", "default"}:
        names.append("default")
    seen: set[Path] = set()
    out: list[Path] = []
    for name in names:
        p = ROOT / "telegram" / f"ingress-spool-{name}"
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def spool_probe(agent_id: str, stale_seconds: int) -> dict[str, Any]:
    now_ts = datetime.now(timezone.utc).timestamp()
    combined = {
        "paths": [],
        "pending": 0,
        "processing": 0,
        "stale_processing": 0,
        "files": [],
    }
    for spool in spool_candidates(agent_id):
        if not spool.exists():
            continue
        combined["paths"].append(str(spool))
        for p in sorted(spool.iterdir()):
            if not p.is_file():
                continue
            name = p.name
            if name.endswith(".json"):
                combined["pending"] += 1
                combined["files"].append(name)
            elif name.endswith(".json.processing"):
                combined["processing"] += 1
                age = max(0, int(now_ts - p.stat().st_mtime))
                if age >= stale_seconds:
                    combined["stale_processing"] += 1
                combined["files"].append(f"{name}:age_seconds={age}")
    return combined


def classify(agent_id: str, *, stale_seconds: int) -> dict[str, Any]:
    session = session_probe(agent_id)
    spool = spool_probe(agent_id, stale_seconds)
    reply = ""
    reason = ""
    if spool["pending"] or spool["processing"]:
        status = "worker_stalled"
        reason = "telegram_ingress_spool_not_drained"
    elif not session["exists"]:
        status = "heartbeat_disabled"
        reason = "no_session_store"
    elif session["session_count"] <= 0:
        status = "session_missing"
        reason = "session_store_empty"
    else:
        status = "online"
        reason = "session_store_has_active_entries_and_spool_clear"
        reply = f"{agent_id} 报到"
    return {
        "agent": agent_id,
        "status": status,
        "reply": reply,
        "reason": reason,
        "session": session,
        "spool": spool,
    }


def write_report(report: dict[str, Any]) -> dict[str, str]:
    out_dir = Path(os.environ.get("OPENCLAW_ATTENDANCE_DIR", SCRIPT_WORKSPACE / "reports" / "attendance")).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    sweep_id = report["sweep_id"]
    json_path = out_dir / f"{sweep_id}.json"
    md_path = out_dir / f"{sweep_id}.md"
    evidence = {"json": str(json_path), "markdown": str(md_path)}
    report["evidence"] = evidence
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    lines = [
        f"# Attendance Sweep {sweep_id}",
        "",
        f"- generated_at: {report['generated_at']}",
        f"- source_agent: {report['source_agent']}",
        f"- ok: {report['ok']}",
        "",
        "| agent | status | reason | reply |",
        "| --- | --- | --- | --- |",
    ]
    for row in report["employees"]:
        lines.append(f"| {row['agent']} | {row['status']} | {row['reason']} | {row.get('reply') or ''} |")
    md_path.write_text("\n".join(lines) + "\n")
    return evidence


def sweep(args: argparse.Namespace) -> int:
    agents = load_agents(args.include_discovered, args.timeout)
    requested = parse_agents_filter(args.agents)
    if requested:
        selected = {aid: agents.get(aid, {"role": "requested"}) for aid in requested}
    else:
        selected = agents
    sweep_id = args.sweep_id or datetime.now().strftime("attendance-%Y%m%d-%H%M%S")
    rows = []
    for aid in sorted(selected):
        row = classify(aid, stale_seconds=args.stale_minutes * 60)
        info = selected[aid] or {}
        row["role"] = info.get("role") or ""
        row["workspace"] = info.get("workspace") or ""
        row["source"] = info.get("source") or ""
        rows.append(row)
    counts = {status: 0 for status in STATUSES}
    for row in rows:
        counts[row["status"]] += 1
    report = {
        "ok": all(row["status"] == "online" for row in rows),
        "sweep_id": sweep_id,
        "generated_at": now(),
        "source_agent": args.source_agent,
        "counts": counts,
        "employees": rows,
        "evidence_rule": "online requires non-empty session store and clear ingress spool; directory status is not used",
    }
    report["evidence"] = write_report(report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Run an OpenClaw employee attendance sweep.")
    sub = ap.add_subparsers(dest="command", required=True)
    sw = sub.add_parser("sweep", help="Ping every known employee by session and worker-spool evidence.")
    sw.add_argument("--source-agent", default="main")
    sw.add_argument("--agents", default="", help="Comma-separated agent ids. Default: registry + discovered agents.")
    sw.add_argument("--sweep-id", default="")
    sw.add_argument("--include-discovered", action=argparse.BooleanOptionalAction, default=True)
    sw.add_argument("--stale-minutes", type=int, default=15)
    sw.add_argument("--timeout", type=int, default=10)
    sw.set_defaults(func=sweep)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
