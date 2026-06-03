#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path


CONTROL_FILES = [
    "AGENTS.md",
    "SOUL.md",
    "CORE.md",
    "USER.md",
    "SESSION-STATE.md",
    "MEMORY.md",
    "README.md",
    "PROJECT_STATE.md",
    "docs/RUNTIME_ADAPTERS.md",
]


def exists(path: Path) -> str:
    return str(path) if path.exists() else ""


def find_openclaw_root(value: str) -> Path | None:
    candidates = [
        Path(value).expanduser() if value else None,
        Path(os.environ.get("OPENCLAW_ROOT", "")).expanduser() if os.environ.get("OPENCLAW_ROOT") else None,
        Path.cwd(),
        Path.home() / "openclaw",
    ]
    for candidate in candidates:
        if candidate and (candidate / "scripts" / "oc").exists():
            return candidate.resolve()
    return None


def find_kernel_root(value: str) -> Path | None:
    candidates = [
        Path(value).expanduser() if value else None,
        Path(os.environ.get("OPENCLAW_COMPANY_KERNEL_ROOT", "")).expanduser() if os.environ.get("OPENCLAW_COMPANY_KERNEL_ROOT") else None,
        Path.cwd(),
    ]
    for candidate in candidates:
        if candidate and (candidate / "bin" / "companyctl").exists():
            return candidate.resolve()
    return None


def read_employees(kernel_root: Path) -> list[dict]:
    db = kernel_root / "company.sqlite"
    if not db.exists():
        return []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT e.id, e.name, e.role, e.runtime, e.status, e.workspace,
                   hb.status AS heartbeat_status, hb.last_seen_at
            FROM employees e
            LEFT JOIN heartbeats hb ON hb.agent_id = e.id
            ORDER BY e.status, e.id
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def classify_employee(employee: dict, kernel_root: Path) -> dict:
    evidence = []
    missing = []
    workspace = Path(employee.get("workspace") or "")
    is_human = employee.get("runtime") == "human" or employee.get("id") == "owner"
    if workspace.exists():
        evidence.append(str(workspace))
    else:
        missing.append("workspace")
    profile = kernel_root / "employees" / employee["id"] / "profile.json"
    if profile.exists():
        evidence.append(str(profile))
    else:
        missing.append("profile")
    if is_human:
        status = "human-owner"
    elif employee.get("status") != "active":
        status = "candidate"
    elif missing:
        status = "blocked"
    else:
        status = "active"
    runtime_agent_id = employee["id"]
    if employee.get("runtime") == "hermes" and employee.get("id") == "hermes":
        runtime_agent_id = "default"
    inbox = kernel_root / "employees" / employee["id"] / "inbox"
    pending_inbox_messages = len(list(inbox.glob("*.message.json"))) if inbox.exists() else 0
    return {
        "agent_id": employee["id"],
        "status": status,
        "runtime": {"type": employee.get("runtime", ""), "workspace": employee.get("workspace", ""), "runtime_agent_id": runtime_agent_id},
        "communication": {
            "default_reply_channel": "current-conversation",
            "default_reply_account": "",
            "default_reply_target": "",
            "session_key": "" if is_human else f"agent:{runtime_agent_id}:<source>",
            "direct_status": "not-worker" if is_human else ("candidate" if status != "blocked" else "blocked"),
            "pending_inbox_messages": pending_inbox_messages,
        },
        "routing": {"active": [], "candidate": [], "blocked": missing},
        "evidence": evidence,
        "next_action": "human owner; do not schedule" if is_human else ("run direct smoke" if status != "blocked" else "fix missing " + ",".join(missing)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan OpenClaw and Company Kernel local employee install")
    parser.add_argument("--openclaw-root", default="")
    parser.add_argument("--kernel-root", default="")
    args = parser.parse_args()

    openclaw_root = find_openclaw_root(args.openclaw_root)
    kernel_root = find_kernel_root(args.kernel_root)
    report: dict = {
        "ok": bool(openclaw_root or kernel_root),
        "openclaw_root": str(openclaw_root) if openclaw_root else "",
        "company_kernel_root": str(kernel_root) if kernel_root else "",
        "control_files": [],
        "employees": [],
        "blocked": [],
    }
    roots = [root for root in [openclaw_root, kernel_root] if root]
    seen = set()
    for root in roots:
        for rel in CONTROL_FILES:
            path = root / rel
            if path.exists() and str(path) not in seen:
                seen.add(str(path))
                report["control_files"].append(str(path))
    if kernel_root:
        employees = [classify_employee(emp, kernel_root) for emp in read_employees(kernel_root)]
        report["employees"] = employees
        report["blocked"] = [emp for emp in employees if emp["status"] == "blocked"]
        report["human_owners"] = [emp for emp in employees if emp["status"] == "human-owner"]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
