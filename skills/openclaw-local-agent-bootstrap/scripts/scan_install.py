#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import shutil
import subprocess
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


RUNTIME_DETECTORS = [
    {"id": "hermes", "name": "Hermes", "role": "supervisor", "runtime": "hermes", "paths": ["~/.hermes", "~/hermes"], "commands": ["hermes"]},
    {"id": "codex", "name": "Codex", "role": "developer", "runtime": "codex", "paths": ["~/openclaw/workspace-xmanx/projects/openclaw-codex-controller"], "commands": ["codex"]},
    {"id": "claude", "name": "Claude", "role": "analyst", "runtime": "claude", "paths": ["~"], "commands": ["claude"]},
    {"id": "trae", "name": "Trae", "role": "developer", "runtime": "trae", "paths": ["~"], "commands": ["trae"]},
    {"id": "antigravity", "name": "Antigravity", "role": "developer", "runtime": "antigravity", "paths": ["/Applications/Antigravity.app", "~"], "commands": []},
    {"id": "cursor", "name": "Cursor", "role": "developer", "runtime": "local", "paths": ["~/openclaw/company-kernel/employees/cursor"], "commands": ["cursor"]},
    {"id": "devin", "name": "Devin", "role": "developer", "runtime": "local", "paths": ["~/openclaw/company-kernel/employees/devin"], "commands": ["devin"]},
    {"id": "github-copilot", "name": "GitHub Copilot", "role": "developer", "runtime": "local", "paths": ["~/openclaw/company-kernel/employees/github-copilot"], "commands": []},
    {"id": "local-model-agent", "name": "Local Model Agent", "role": "model-agent", "runtime": "local", "paths": ["~/openclaw/company-kernel/employees/local-model-agent"], "commands": ["ollama"]},
]

OPENCLAW_WORKSPACE_ROLES = {
    "workspace-xmanx": ("main", "main", "operator"),
    "workspace-nestcar": ("nestcar", "car-rental", "business-agent"),
    "workspace-chindahotpot": ("chindahotpot", "chindahotpot", "business-agent"),
    "workspace-krothong": ("krothong", "krothong", "business-agent"),
    "workspace-invest": ("invest", "invest", "business-agent"),
    "workspace-video-creator": ("video-creator", "video-creator", "business-agent"),
    "workspace-video-ops": ("video-ops", "video-ops", "business-agent"),
    "workspace-video-publisher": ("video-publisher", "video-publisher", "business-agent"),
}


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


def path_exists(raw: str) -> Path | None:
    path = Path(raw).expanduser()
    return path.resolve() if path.exists() else None


def first_existing_path(paths: list[str]) -> str:
    for raw in paths:
        found = path_exists(raw)
        if found:
            return str(found)
    return ""


def command_exists(commands: list[str]) -> list[str]:
    return [cmd for cmd in commands if shutil.which(cmd)]


def summarize_control_files(root: Path) -> list[dict]:
    found = []
    for rel in CONTROL_FILES:
        path = root / rel
        if not path.exists() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        found.append({"path": str(path), "bytes": path.stat().st_size, "summary": " ".join(text.split())[:240]})
    return found


def discover_openclaw_workspace_candidates(openclaw_root: Path | None) -> list[dict]:
    if not openclaw_root:
        return []
    candidates = []
    for workspace in sorted(openclaw_root.glob("workspace-*")):
        if not workspace.is_dir():
            continue
        agent_id, name, role = OPENCLAW_WORKSPACE_ROLES.get(workspace.name, (workspace.name.replace("workspace-", ""), workspace.name.replace("workspace-", ""), "business-agent"))
        candidates.append(
            {
                "agent_id": agent_id,
                "name": name,
                "role": role,
                "runtime": "openclaw",
                "workspace": str(workspace.resolve()),
                "evidence": [str(workspace.resolve()), *[item["path"] for item in summarize_control_files(workspace)[:3]]],
                "reason": "openclaw workspace discovered",
            }
        )
    return candidates


def discover_runtime_candidates() -> list[dict]:
    candidates = []
    for detector in RUNTIME_DETECTORS:
        workspace = first_existing_path(detector["paths"])
        commands = command_exists(detector["commands"])
        if not workspace and not commands:
            continue
        candidates.append(
            {
                "agent_id": detector["id"],
                "name": detector["name"],
                "role": detector["role"],
                "runtime": detector["runtime"],
                "workspace": workspace or str(Path.home()),
                "evidence": [item for item in [workspace, *commands] if item],
                "reason": "runtime command or known workspace discovered",
            }
        )
    return candidates


def ensure_runtime(kernel_root: Path, runtime: str, *, apply: bool) -> dict:
    if not apply:
        return {"ok": True, "dry_run": True}
    cmd = [str(kernel_root / "bin" / "companyctl"), "runtime", "register", "--runtime", runtime, "--command", "", "--status", "registered"]
    cp = subprocess.run(cmd, cwd=str(kernel_root), text=True, capture_output=True)
    return {"ok": cp.returncode == 0, "exit_code": cp.returncode, "stdout": cp.stdout[-1000:], "stderr": cp.stderr[-1000:]}


def apply_candidate(kernel_root: Path, candidate: dict) -> dict:
    ensure_runtime(kernel_root, candidate["runtime"], apply=True)
    cmd = [
        str(kernel_root / "bin" / "companyctl"),
        "employee",
        "create",
        "--id",
        candidate["agent_id"],
        "--name",
        candidate["name"],
        "--role",
        candidate["role"],
        "--runtime",
        candidate["runtime"],
        "--workspace",
        candidate["workspace"],
    ]
    cp = subprocess.run(cmd, cwd=str(kernel_root), text=True, capture_output=True)
    if cp.returncode == 0:
        update = subprocess.run(
            [str(kernel_root / "bin" / "companyctl"), "employee", "update", "--id", candidate["agent_id"], "--status", "candidate"],
            cwd=str(kernel_root),
            text=True,
            capture_output=True,
        )
        return {"ok": update.returncode == 0, "create_exit_code": cp.returncode, "update_exit_code": update.returncode, "stdout": (cp.stdout + update.stdout)[-1200:], "stderr": (cp.stderr + update.stderr)[-1200:]}
    return {"ok": False, "create_exit_code": cp.returncode, "stdout": cp.stdout[-1200:], "stderr": cp.stderr[-1200:]}


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
    is_human = employee.get("runtime") == "human" or employee.get("id") == "owner-shift"
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
            "ack_required": True,
            "failure_feedback_required": True,
            "pending_inbox_messages": pending_inbox_messages,
        },
        "coordination": {
            "closed_loop_required": True,
            "record_only_is_not_ack": True,
            "human_notification_required": True,
            "collaboration_trigger": "@agent",
        },
        "routing": {"active": [], "candidate": [], "blocked": missing},
        "evidence": evidence,
        "next_action": "human owner; do not schedule" if is_human else ("run direct smoke" if status != "blocked" else "fix missing " + ",".join(missing)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan OpenClaw and Company Kernel local employee install")
    parser.add_argument("--openclaw-root", default="")
    parser.add_argument("--kernel-root", default="")
    parser.add_argument("--apply", action="store_true", help="create discovered employees as candidate entries; default is read-only")
    args = parser.parse_args()

    openclaw_root = find_openclaw_root(args.openclaw_root)
    kernel_root = find_kernel_root(args.kernel_root)
    report: dict = {
        "ok": bool(openclaw_root or kernel_root),
        "openclaw_root": str(openclaw_root) if openclaw_root else "",
        "company_kernel_root": str(kernel_root) if kernel_root else "",
        "control_files": [],
        "employees": [],
        "discovered_candidates": [],
        "apply_results": [],
        "blocked": [],
        "coordination": {
            "closed_loop_required": True,
            "ack_required": True,
            "failure_feedback_required": True,
            "record_only_is_not_ack": True,
            "human_notification_required": True,
            "collaboration_trigger": "@agent",
        },
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
        existing_ids = set()
        employees = [classify_employee(emp, kernel_root) for emp in read_employees(kernel_root)]
        existing_ids = {employee["agent_id"] for employee in employees}
        discovered = [*discover_openclaw_workspace_candidates(openclaw_root), *discover_runtime_candidates()]
        deduped = []
        seen = set()
        for candidate in discovered:
            if candidate["agent_id"] in existing_ids or candidate["agent_id"] in seen:
                continue
            seen.add(candidate["agent_id"])
            candidate["recommended_command"] = " ".join(
                [
                    "bin/companyctl employee create",
                    f"--id {candidate['agent_id']}",
                    f"--name {json.dumps(candidate['name'])}",
                    f"--role {candidate['role']}",
                    f"--runtime {candidate['runtime']}",
                    f"--workspace {json.dumps(candidate['workspace'])}",
                    "&&",
                    f"bin/companyctl employee update --id {candidate['agent_id']} --status candidate",
                ]
            )
            deduped.append(candidate)
        if args.apply:
            report["apply_results"] = [apply_candidate(kernel_root, candidate) for candidate in deduped]
            employees = [classify_employee(emp, kernel_root) for emp in read_employees(kernel_root)]
        report["employees"] = employees
        report["discovered_candidates"] = deduped
        report["blocked"] = [emp for emp in employees if emp["status"] == "blocked"]
        report["human_owners"] = [emp for emp in employees if emp["status"] == "human-owner"]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
