from __future__ import annotations

import argparse
import filecmp
import fnmatch
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .schema_migrations import ensure_schema_migrations

ROOT = Path(os.environ.get("OPENCLAW_COMPANY_KERNEL_ROOT", Path(__file__).resolve().parents[1])).resolve()
DB_PATH = ROOT / "company.sqlite"
EMPLOYEES_DIR = ROOT / "employees"
STATE_DIR = ROOT / "state"
RFC_DIR = ROOT / "rfcs"
CONFIG_DIR = ROOT / "config"
WORKFLOW_DIR = CONFIG_DIR / "workflows"
LAUNCHD_LABEL = "ai.openclaw.company-kernel.daemon"
LAUNCHD_TEMPLATE = CONFIG_DIR / "launchd" / f"{LAUNCHD_LABEL}.plist"
HOOKS_PATH = CONFIG_DIR / "hooks.json"
COMMUNICATIONS_PATH = CONFIG_DIR / "company_communications.json"
POLICY_PATH = CONFIG_DIR / "policy.json"
PROTECTED_PATHS_CONFIG = CONFIG_DIR / "protected_paths.json"
APPROVAL_STATE_DIR = STATE_DIR / "approvals"
SCHEMA = ROOT / "company_kernel" / "schema.sql"

KNOWN_RUNTIMES = {
    "openclaw": "OpenClaw runtime adapter",
    "hermes": "Hermes local runtime adapter",
    "codex": "Codex CLI / openclaw-codex-controller adapter",
    "claude": "Claude Code / Claude CLI adapter",
    "trae": "Trae IDE/Agent adapter",
    "antigravity": "Google Antigravity adapter",
    "local": "Local script/manual adapter",
}

APPROVAL_STATUSES = {"pending", "approved", "denied"}

DEFAULT_ROUTE_APPROVAL_ACTIONS = {
    "payment": ["payment", "pay ", "付款", "支付", "打款"],
    "compensation": ["compensation", "赔偿", "赔付", "押金", "保险", "事故"],
    "salary": ["salary", "工资", "薪资"],
    "penalty": ["penalty", "处罚", "罚款"],
    "external_send": ["external send", "外发", "发送给客户", "发给客户", "发布", "publish"],
    "production_deploy": ["production deploy", "deploy", "上线", "生产部署"],
    "secret_change": ["secret", "token", "password", "密钥", "密码"],
    "kernel_change": ["kernel", "schema", "approval rule", "内核", "审批规则", "通信协议"],
}


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_time(value: str) -> datetime:
    raw = value.replace("Z", "+00:00")
    return datetime.fromisoformat(raw)


def future_seconds(seconds: int) -> str:
    return (datetime.now(timezone.utc).astimezone() + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    ensure_schema_migrations(conn)
    conn.commit()
    return conn


def connect_readonly() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def audit(conn: sqlite3.Connection, actor: str, action: str, target: str = "", detail: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO audit_logs(actor, action, target, detail_json, created_at) VALUES (?, ?, ?, ?, ?)",
        (actor, action, target, json.dumps(detail or {}, ensure_ascii=False), now()),
    )
    conn.commit()


def new_trace_id() -> str:
    return f"trace-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"


def trace_id_for_task(conn: sqlite3.Connection, task_id: str = "", fallback: str = "") -> str:
    if not task_id:
        return fallback or new_trace_id()
    row = conn.execute("SELECT metadata_json FROM task_metadata WHERE task_id = ?", (task_id,)).fetchone()
    if row:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            metadata = {}
        trace_id = str(metadata.get("trace_id", "") or "")
        if trace_id:
            return trace_id
    return fallback or new_trace_id()


def record_event(conn: sqlite3.Connection, event_type: str, source_agent: str, *, task_id: str = "", payload: dict | None = None, trace_id: str = "") -> dict:
    event_id = f"evt-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    ts = now()
    event_trace_id = trace_id or trace_id_for_task(conn, task_id)
    event = {
        "id": event_id,
        "trace_id": event_trace_id,
        "event_type": event_type,
        "source_agent": source_agent,
        "task_id": task_id,
        "payload": payload or {},
        "created_at": ts,
    }
    conn.execute(
        """
        INSERT INTO company_events(id, trace_id, event_type, source_agent, task_id, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (event_id, event_trace_id, event_type, source_agent, task_id, json.dumps(payload or {}, ensure_ascii=False), ts),
    )
    conn.commit()
    return event


def ensure_runtime(conn: sqlite3.Connection, runtime: str) -> None:
    ts = now()
    conn.execute(
        """
        INSERT INTO employee_runtimes(runtime, command, status, notes, created_at, updated_at)
        VALUES (?, '', 'registered', ?, ?, ?)
        ON CONFLICT(runtime) DO UPDATE SET updated_at = excluded.updated_at
        """,
        (runtime, KNOWN_RUNTIMES.get(runtime, "Custom runtime adapter"), ts, ts),
    )
    conn.commit()


def runtime_registered(conn: sqlite3.Connection, runtime: str) -> bool:
    if runtime in KNOWN_RUNTIMES:
        return True
    return bool(conn.execute("SELECT 1 FROM employee_runtimes WHERE runtime = ? AND status != 'disabled'", (runtime,)).fetchone())


def require_runtime(conn: sqlite3.Connection, runtime: str) -> None:
    if not runtime_registered(conn, runtime):
        raise SystemExit(f"unknown runtime: {runtime}; run companyctl runtime register --runtime {runtime}")


def employee_paths(employee_id: str) -> dict[str, Path]:
    base = EMPLOYEES_DIR / employee_id
    return {
        "base": base,
        "profile": base / "profile.json",
        "capabilities": base / "capabilities.json",
        "rules": base / "rules.md",
        "permissions": base / "permissions.json",
        "heartbeat": base / "heartbeat.json",
        "inbox": base / "inbox",
        "outbox": base / "outbox",
        "reports": base / "reports",
    }


def load_communication_config() -> dict:
    if not COMMUNICATIONS_PATH.exists():
        return {"policy": {"mode": "open"}, "aliases": {}, "employees": {}, "channels": {}}
    return json.loads(COMMUNICATIONS_PATH.read_text(encoding="utf-8"))


def write_communication_config(config: dict) -> None:
    COMMUNICATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    COMMUNICATIONS_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def resolve_employee_alias(employee_id: str) -> str:
    config = load_communication_config()
    return str(config.get("aliases", {}).get(employee_id, employee_id))


def communication_list(config: dict, employee_id: str, key: str) -> list[str]:
    employee = config.get("employees", {}).get(employee_id, {})
    return [resolve_employee_alias(str(item)) for item in employee.get(key, [])]


def communication_policy_decision(source: str, target: str, action: str) -> dict:
    config = load_communication_config()
    source = resolve_employee_alias(source)
    target = resolve_employee_alias(target)
    policy = config.get("policy", {})
    mode = policy.get("mode", "open")
    relation_key = "can_assign_to" if action == "task.submit" else "can_talk_to"
    blocked_key = "blocked_assign_to" if action == "task.submit" else "blocked_talk_to"
    blocked = communication_list(config, source, blocked_key)
    allowed = communication_list(config, source, relation_key)
    if target in blocked or "*" in blocked:
        return {"allowed": False, "mode": mode, "source": source, "target": target, "action": action, "reason": f"{blocked_key} blocks target"}
    if mode in {"strict", "allowlist"} and allowed and target not in allowed and "*" not in allowed:
        return {"allowed": False, "mode": mode, "source": source, "target": target, "action": action, "reason": f"{relation_key} does not include target"}
    return {"allowed": True, "mode": mode, "source": source, "target": target, "action": action, "reason": "allowed"}


def require_communication_allowed(source: str, target: str, action: str) -> dict:
    decision = communication_policy_decision(source, target, action)
    if not decision["allowed"]:
        raise SystemExit(f"communication denied: {decision['reason']} ({decision['source']} -> {decision['target']} {action})")
    return decision


def default_capabilities(profile: dict) -> dict:
    runtime = profile.get("runtime", "local")
    base = {
        "agent_id": profile.get("id", ""),
        "runtime": runtime,
        "role": profile.get("role", ""),
        "skills": [],
        "tools": [],
        "preferred_task_types": [],
        "handoff": {
            "can_receive_tasks": True,
            "can_send_messages": True,
            "requires_adapter": runtime not in {"local"},
        },
        "updated_at": now(),
    }
    presets = {
        "codex": {
            "skills": ["code-editing", "testing", "review", "git-workflow", "project-delivery"],
            "tools": ["codex exec", "shell", "apply_patch"],
            "preferred_task_types": ["engineering", "debugging", "test-fix", "repo-maintenance"],
        },
        "hermes": {
            "skills": ["local-automation", "browser-automation", "model-routing", "tool-orchestration"],
            "tools": ["hermes -z", "local tools"],
            "preferred_task_types": ["automation", "research", "ops-support"],
        },
        "openclaw": {
            "skills": ["business-ops", "agent-bus", "workspace-operations"],
            "tools": ["openclaw", "oc bus"],
            "preferred_task_types": ["business-agent-task", "line-ops", "workspace-task"],
        },
        "claude": {
            "skills": ["analysis", "documentation", "code-understanding"],
            "tools": ["claude -p"],
            "preferred_task_types": ["analysis", "documentation", "review"],
        },
        "trae": {
            "skills": ["ide-development", "code-editing"],
            "tools": ["trae chat"],
            "preferred_task_types": ["ide-coding", "implementation"],
        },
        "antigravity": {
            "skills": ["multi-agent-ide", "browser-workflow"],
            "tools": ["Antigravity app"],
            "preferred_task_types": ["gui-assisted-development", "browser-workflow"],
        },
    }
    preset = presets.get(runtime, {})
    base.update({k: preset.get(k, base[k]) for k in ("skills", "tools", "preferred_task_types")})
    return base


def load_json_or_default(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else default
    except json.JSONDecodeError:
        return default


def read_json_file_checked(path: Path) -> tuple[dict, str]:
    if not path.exists():
        return {}, "missing"
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}, "invalid_json"
    if not isinstance(obj, dict):
        return {}, "not_object"
    return obj, ""


def employee_capability_issues(conn: sqlite3.Connection) -> list[dict]:
    issues = []
    for employee in conn.execute("SELECT id, runtime FROM employees WHERE status = 'active' ORDER BY id").fetchall():
        paths = employee_paths(employee["id"])
        capabilities, cap_error = read_json_file_checked(paths["capabilities"])
        permissions, perm_error = read_json_file_checked(paths["permissions"])
        if cap_error:
            issues.append({"agent": employee["id"], "file": str(paths["capabilities"]), "reason": f"capabilities_{cap_error}"})
        else:
            for key in ("skills", "tools", "preferred_task_types"):
                if not isinstance(capabilities.get(key), list):
                    issues.append({"agent": employee["id"], "file": str(paths["capabilities"]), "reason": f"capabilities_{key}_not_list"})
            if not isinstance(capabilities.get("handoff", {}), dict):
                issues.append({"agent": employee["id"], "file": str(paths["capabilities"]), "reason": "capabilities_handoff_not_object"})
        if perm_error:
            issues.append({"agent": employee["id"], "file": str(paths["permissions"]), "reason": f"permissions_{perm_error}"})
        else:
            for key in ("can_submit_tasks", "can_claim_tasks", "can_modify_kernel"):
                if not isinstance(permissions.get(key), bool):
                    issues.append({"agent": employee["id"], "file": str(paths["permissions"]), "reason": f"permissions_{key}_not_bool"})
            if not isinstance(permissions.get("requires_approval_for", []), list):
                issues.append({"agent": employee["id"], "file": str(paths["permissions"]), "reason": "permissions_requires_approval_for_not_list"})
    return issues


def task_evidence_issues(conn: sqlite3.Connection) -> list[dict]:
    issues = []
    completed = rows(
        conn,
        """
        SELECT id, target_agent, evidence_path, updated_at
        FROM tasks
        WHERE status = 'completed'
        ORDER BY updated_at DESC
        LIMIT 100
        """,
    )
    for task in completed:
        evidence_path = str(task.get("evidence_path") or "")
        if not evidence_path:
            issues.append({"task_id": task["id"], "agent": task["target_agent"], "reason": "completed_without_evidence", "evidence_path": ""})
        elif not Path(evidence_path).exists():
            issues.append({"task_id": task["id"], "agent": task["target_agent"], "reason": "evidence_missing_on_disk", "evidence_path": evidence_path})
    blocked_without_blocker = rows(
        conn,
        """
        SELECT id, target_agent, updated_at
        FROM tasks
        WHERE status = 'blocked' AND TRIM(COALESCE(blocker, '')) = ''
        ORDER BY updated_at DESC
        LIMIT 100
        """,
    )
    for task in blocked_without_blocker:
        issues.append({"task_id": task["id"], "agent": task["target_agent"], "reason": "blocked_without_blocker", "evidence_path": ""})
    return issues


def daemon_last_run_path() -> Path:
    return STATE_DIR / "daemon" / "last-run.json"


def daemon_health(max_age_minutes: int = 10) -> dict:
    path = daemon_last_run_path()
    if not path.exists():
        return {
            "ok": False,
            "state_file": str(path),
            "last_run_at": "",
            "age_minutes": None,
            "max_age_minutes": max_age_minutes,
            "reason": "missing_daemon_state",
        }
    state = load_json_or_default(path, {})
    last_run_at = str(state.get("at") or "")
    dt = parse_time(last_run_at) if last_run_at else None
    age_minutes = None if dt is None else int((datetime.now(timezone.utc).astimezone() - dt).total_seconds() // 60)
    stale = age_minutes is None or age_minutes > max_age_minutes
    return {
        "ok": bool(state.get("ok")) and not stale,
        "state_file": str(path),
        "last_run_at": last_run_at,
        "age_minutes": age_minutes,
        "max_age_minutes": max_age_minutes,
        "reason": "daemon_stale" if stale else ("" if state.get("ok") else "daemon_last_run_failed"),
    }


def launchd_health() -> dict:
    installed_path = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    installed = installed_path.exists()
    template_exists = LAUNCHD_TEMPLATE.exists()
    matches_template = bool(installed and template_exists and filecmp.cmp(LAUNCHD_TEMPLATE, installed_path, shallow=False))
    return {
        "label": LAUNCHD_LABEL,
        "template": str(LAUNCHD_TEMPLATE),
        "template_exists": template_exists,
        "installed_path": str(installed_path),
        "installed": installed,
        "matches_template": matches_template,
        "recommended_interval_seconds": 300,
        "install_command": "bash bin/company-daemon-install-launchd",
        "uninstall_command": "bash bin/company-daemon-uninstall-launchd",
        "verify_command": "bin/companyctl doctor --summary",
    }


def openclaw_root() -> Path:
    env = os.environ.get("OPENCLAW_ROOT")
    if env:
        return Path(env).expanduser()
    if Path("/Users/owner/openclaw").exists():
        return Path("/Users/owner/openclaw")
    return Path.home() / "openclaw"


def count_spool_files(spool_dir: Path) -> dict:
    pending = sorted(spool_dir.glob("*.json")) if spool_dir.exists() else []
    processing = sorted(spool_dir.glob("*.processing")) if spool_dir.exists() else []
    stale_processing = []
    cutoff = datetime.now(timezone.utc).astimezone() - timedelta(minutes=15)
    for path in processing:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).astimezone()
        if mtime < cutoff:
            stale_processing.append(str(path))
    return {
        "path": str(spool_dir),
        "exists": spool_dir.exists(),
        "pending": len(pending),
        "processing": len(processing),
        "stale_processing": len(stale_processing),
        "stale_processing_files": stale_processing[:10],
        "pending_files": [str(path) for path in pending[:10]],
    }


def openclaw_guard_health() -> dict:
    root = openclaw_root()
    telegram_dir = root / "telegram"
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    watcher_plist = launch_agents / "ai.openclaw.ops-telegram-approval-watcher.plist"
    watcher_disabled = launch_agents / "ai.openclaw.ops-telegram-approval-watcher.plist.disabled"
    spools = {}
    if telegram_dir.exists():
        for spool_dir in sorted(telegram_dir.glob("ingress-spool-*")):
            account = spool_dir.name.removeprefix("ingress-spool-")
            spools[account] = count_spool_files(spool_dir)
    backlog_accounts = {
        account: spool
        for account, spool in spools.items()
        if int(spool.get("pending", 0)) > 0 or int(spool.get("stale_processing", 0)) > 0
    }
    issues = []
    if watcher_plist.exists():
        issues.append("external_telegram_approval_watcher_enabled")
    if backlog_accounts:
        issues.append("telegram_ingress_spool_backlog")
    return {
        "ok": not issues,
        "issues": issues,
        "openclaw_root": str(root),
        "telegram_dir": str(telegram_dir),
        "external_approval_watcher": {
            "installed_path": str(watcher_plist),
            "installed": watcher_plist.exists(),
            "disabled_path": str(watcher_disabled),
            "disabled_file_exists": watcher_disabled.exists(),
            "risk": "conflicts_with_openclaw_telegram_getupdates" if watcher_plist.exists() else "",
        },
        "telegram_spools": spools,
        "backlog_accounts": backlog_accounts,
        "note": "Read-only guard. It detects conditions that can break OpenClaw native Telegram routing; it does not start, stop, or poll Telegram.",
    }


ATTENDANCE_STATUSES = ("online", "session_missing", "worker_stalled", "heartbeat_disabled", "no_reply")
ATTENDANCE_CLASSIFICATION_GUIDE = {
    "online": "exact reply probe matched, or reply probing disabled with non-empty runtime session and clear ingress spool",
    "session_missing": "runtime session store exists but has no active session entries",
    "worker_stalled": "OpenClaw Telegram ingress spool has pending or processing files, so the worker is not continuously draining",
    "heartbeat_disabled": "no runtime session store and no Company Kernel heartbeat file",
    "no_reply": "employee has heartbeat/session metadata but no supported or successful reply path",
}


def attendance_session_candidates(employee_id: str) -> list[Path]:
    names = [employee_id, employee_id.replace("_", "-"), employee_id.replace("-", "_")]
    if employee_id == "openclaw-main":
        names.append("main")
    if employee_id == "nestcar":
        names.append("car-rental")
    if employee_id in {"hermes", "default"}:
        names.extend(["default", "hermes"])
    result = []
    seen = set()
    for name in names:
        path = openclaw_root() / "agents" / name / "sessions" / "sessions.json"
        if path not in seen:
            seen.add(path)
            result.append(path)
    return result


def attendance_session_probe(employee_id: str) -> dict:
    candidates = attendance_session_candidates(employee_id)
    for path in candidates:
        if not path.exists():
            continue
        payload = load_json_or_default(path, {})
        count = len(payload) if isinstance(payload, (dict, list)) else 0
        return {"path": str(path), "exists": True, "bytes": path.stat().st_size, "session_count": count}
    return {"path": str(candidates[0]), "exists": False, "bytes": 0, "session_count": 0}


def attendance_spool_candidates(employee_id: str) -> list[Path]:
    names = [employee_id, employee_id.replace("-", "_"), employee_id.replace("_", "-")]
    if employee_id in {"main", "openclaw-main"}:
        names.append("default")
    if employee_id in {"hermes", "default"}:
        names.append("default")
    result = []
    seen = set()
    for name in names:
        path = openclaw_root() / "telegram" / f"ingress-spool-{name}"
        if path not in seen:
            seen.add(path)
            result.append(path)
    return result


def attendance_spool_probe(employee_id: str, stale_minutes: int) -> dict:
    cutoff = datetime.now(timezone.utc).astimezone() - timedelta(minutes=stale_minutes)
    probe = {"paths": [], "pending": 0, "processing": 0, "stale_processing": 0, "files": []}
    for spool in attendance_spool_candidates(employee_id):
        if not spool.exists():
            continue
        probe["paths"].append(str(spool))
        for path in sorted(spool.iterdir()):
            if not path.is_file():
                continue
            if path.name.endswith(".json"):
                probe["pending"] += 1
                probe["files"].append(path.name)
            elif path.name.endswith(".json.processing"):
                probe["processing"] += 1
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).astimezone()
                age_seconds = max(0, int((datetime.now(timezone.utc).astimezone() - mtime).total_seconds()))
                if mtime < cutoff:
                    probe["stale_processing"] += 1
                probe["files"].append(f"{path.name}:age_seconds={age_seconds}")
    return probe


def attendance_agent_runtime_id(employee_id: str, runtime: str) -> str:
    if employee_id == "openclaw-main":
        return "main"
    if employee_id == "hermes" and runtime == "hermes":
        return "default"
    return employee_id


def parse_openclaw_agent_reply(stdout: str) -> str:
    payload = parse_json_output(stdout)
    result = payload.get("result") if isinstance(payload, dict) else {}
    payloads = result.get("payloads") if isinstance(result, dict) else []
    if isinstance(payloads, list):
        for item in payloads:
            if isinstance(item, dict) and str(item.get("text") or "").strip():
                return str(item["text"]).strip()
    meta = result.get("meta") if isinstance(result, dict) else {}
    if isinstance(meta, dict):
        for key in ("finalAssistantVisibleText", "finalAssistantRawText"):
            if str(meta.get(key) or "").strip():
                return str(meta[key]).strip()
    return ""


def attendance_reply_probe(employee_id: str, runtime: str, timeout: int) -> dict:
    expected = f"{employee_id} 在岗"
    if runtime in {"openclaw", "hermes"}:
        agent_runtime_id = attendance_agent_runtime_id(employee_id, runtime)
        cmd = ["openclaw", "agent", "--agent", agent_runtime_id, "--message", f"只回复 {expected}", "--timeout", str(timeout), "--json"]
        try:
            cp = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True, timeout=timeout + 10)
        except Exception as exc:
            return {"enabled": True, "ok": False, "agent_runtime_id": agent_runtime_id, "expected": expected, "reply": "", "exit_code": 127, "reason": str(exc)}
        reply = parse_openclaw_agent_reply(cp.stdout)
        return {
            "enabled": True,
            "ok": cp.returncode == 0 and reply == expected,
            "agent_runtime_id": agent_runtime_id,
            "expected": expected,
            "reply": reply,
            "exit_code": cp.returncode,
            "reason": "matched" if reply == expected else "reply_mismatch_or_empty",
            "stderr": cp.stderr[-2000:],
        }
    if runtime == "codex":
        command = ROOT / "bin" / "company-codex-adapter"
        try:
            cp = subprocess.run([str(command), "--agent", employee_id, "--attendance-probe"], cwd=str(ROOT), text=True, capture_output=True, timeout=timeout + 10)
        except Exception as exc:
            return {"enabled": True, "ok": False, "adapter": str(command), "expected": expected, "reply": "", "exit_code": 127, "reason": str(exc)}
        payload = parse_json_output(cp.stdout)
        ok = cp.returncode == 0 and bool(payload.get("ok"))
        return {
            "enabled": True,
            "ok": ok,
            "adapter": str(command),
            "expected": expected,
            "reply": expected if ok else "",
            "exit_code": cp.returncode,
            "reason": "adapter_heartbeat_matched" if ok else str(payload.get("error") or "adapter_failed"),
            "processed": payload.get("processed"),
            "stdout": payload,
            "stderr": cp.stderr[-2000:],
        }
    return {"enabled": False, "ok": False, "reason": f"unsupported_runtime:{runtime}", "reply": "", "expected": expected}


def attendance_classify_employee(employee: dict, stale_minutes: int, *, probe_replies: bool = True, reply_timeout: int = 120) -> dict:
    employee_id = employee["id"]
    runtime = employee.get("runtime", "")
    session = attendance_session_probe(employee_id)
    spool = attendance_spool_probe(employee_id, stale_minutes)
    heartbeat = load_json_or_default(employee_paths(employee_id)["heartbeat"], {})
    reply = ""
    reply_probe = {"enabled": False, "ok": False, "reply": "", "reason": "disabled"}
    if int(spool.get("pending", 0)) > 0 or int(spool.get("processing", 0)) > 0:
        status = "worker_stalled"
        reason = "telegram_ingress_spool_not_drained"
    elif probe_replies:
        reply_probe = attendance_reply_probe(employee_id, runtime, reply_timeout)
        if reply_probe.get("ok"):
            status = "online"
            reason = "agent_reply_matched"
            reply = str(reply_probe.get("reply") or "")
        elif not session["exists"] and not heartbeat:
            status = "heartbeat_disabled"
            reason = "no_session_store_or_employee_heartbeat"
        elif session["exists"] and int(session.get("session_count", 0)) <= 0:
            status = "session_missing"
            reason = "session_store_empty"
        else:
            status = "no_reply"
            reason = str(reply_probe.get("reason") or "reply_probe_failed")
            reply = str(reply_probe.get("reply") or "")
    elif not session["exists"] and not heartbeat:
        status = "heartbeat_disabled"
        reason = "no_session_store_or_employee_heartbeat"
    elif session["exists"] and int(session.get("session_count", 0)) <= 0:
        status = "session_missing"
        reason = "session_store_empty"
    elif not session["exists"]:
        status = "no_reply"
        reason = "no_runtime_session_evidence"
    else:
        status = "online"
        reason = "session_store_has_active_entries_and_spool_clear"
        reply = f"{employee_id} 报到"
    return {
        "agent": employee_id,
        "name": employee.get("name", ""),
        "runtime": runtime,
        "employee_status": employee.get("status", ""),
        "status": status,
        "reply": reply,
        "reason": reason,
        "reply_probe": reply_probe,
        "session": session,
        "spool": spool,
        "heartbeat_file": str(employee_paths(employee_id)["heartbeat"]),
        "heartbeat_file_exists": bool(heartbeat),
    }


def cmd_attendance_sweep(args: argparse.Namespace) -> int:
    conn = connect_readonly()
    requested = set(parse_csv(args.agents))
    if requested:
        placeholders = ",".join("?" for _ in requested)
        query = f"SELECT * FROM employees WHERE id IN ({placeholders}) ORDER BY id"
        employees = [dict(row) for row in conn.execute(query, tuple(sorted(requested))).fetchall()]
        known = {row["id"] for row in employees}
        for missing in sorted(requested - known):
            employees.append({"id": missing, "name": missing, "runtime": "unknown", "status": "missing"})
    else:
        where = "" if args.include_candidates else "WHERE status = 'active'"
        employees = [dict(row) for row in conn.execute(f"SELECT * FROM employees {where} ORDER BY id").fetchall()]
    rows_out = [attendance_classify_employee(emp, args.stale_minutes, probe_replies=args.probe_replies, reply_timeout=args.reply_timeout) for emp in employees]
    counts = {status: 0 for status in ATTENDANCE_STATUSES}
    for row in rows_out:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    report = {
        "ok": bool(rows_out) and all(row["status"] == "online" for row in rows_out),
        "sweep_id": args.sweep_id or f"attendance-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
        "generated_at": now(),
        "source_agent": args.source,
        "counts": counts,
        "employees": rows_out,
        "evidence_rule": "online requires clear ingress spool plus exact agent reply when reply probing is enabled; employee_directory.status is reported but never sufficient",
        "classification_guide": ATTENDANCE_CLASSIFICATION_GUIDE,
    }
    report_dir = STATE_DIR / "attendance"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{report['sweep_id']}.json"
    latest_path = report_dir / "latest.json"
    report["evidence"] = {"json": str(report_path), "latest": str(latest_path)}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    latest_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    emit(report)
    return 0 if report["ok"] else 1


def write_employee_capabilities(employee_id: str, profile: dict, *, dry_run: bool) -> str:
    path = employee_paths(employee_id)["capabilities"]
    if dry_run:
        return str(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(json.dumps(default_capabilities(profile), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


def write_employee_files(employee_id: str, profile: dict, *, dry_run: bool) -> dict:
    paths = employee_paths(employee_id)
    result = {k: str(v) for k, v in paths.items()}
    if dry_run:
        return result
    for key in ("inbox", "outbox", "reports"):
        paths[key].mkdir(parents=True, exist_ok=True)
    paths["profile"].write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_employee_capabilities(employee_id, profile, dry_run=False)
    paths["rules"].write_text(
        "# Employee Rules\n\n"
        "- Use companyctl for task state changes.\n"
        "- Do not edit Company Kernel internals directly.\n"
        "- High-risk business actions require approval.\n"
        "- Always return evidence_path or blocker.\n",
        encoding="utf-8",
    )
    paths["permissions"].write_text(
        json.dumps(
            {
                "can_submit_tasks": True,
                "can_claim_tasks": True,
                "can_modify_kernel": False,
                "requires_approval_for": ["payment", "compensation", "salary", "penalty", "external_send"],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    paths["heartbeat"].write_text(json.dumps({"agent_id": employee_id, "status": "created", "updated_at": now()}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def cmd_employee_create(args: argparse.Namespace) -> int:
    conn = connect()
    try:
        require_runtime(conn, args.runtime)
    except SystemExit:
        conn.close()
        raise
    profile = {
        "id": args.id,
        "name": args.name,
        "role": args.role,
        "runtime": args.runtime,
        "workspace": args.workspace,
        "created_at": now(),
    }
    files = write_employee_files(args.id, profile, dry_run=args.dry_run)
    if args.dry_run:
        conn.close()
        emit({"ok": True, "dry_run": True, "employee": profile, "files": files})
        return 0
    ensure_runtime(conn, args.runtime)
    ts = now()
    conn.execute(
        """
        INSERT INTO employees(id, name, role, runtime, workspace, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          name = excluded.name,
          role = excluded.role,
          runtime = excluded.runtime,
          workspace = excluded.workspace,
          status = 'active',
          updated_at = excluded.updated_at
        """,
        (args.id, args.name, args.role, args.runtime, args.workspace, ts, ts),
    )
    conn.commit()
    audit(conn, "companyctl", "employee.create", args.id, profile)
    emit({"ok": True, "employee": profile, "files": files})
    return 0


def rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def cmd_employee_list(_args: argparse.Namespace) -> int:
    conn = connect()
    emit({"ok": True, "employees": rows(conn, "SELECT * FROM employees ORDER BY id")})
    return 0


def cmd_employee_update(args: argparse.Namespace) -> int:
    conn = connect()
    employee_id = resolve_employee_alias(args.id)
    row = conn.execute("SELECT * FROM employees WHERE id = ?", (employee_id,)).fetchone()
    if not row:
        conn.close()
        emit({"ok": False, "error": "unknown employee", "employee_id": employee_id})
        return 1
    current = dict(row)
    runtime = args.runtime or current["runtime"]
    try:
        require_runtime(conn, runtime)
    except SystemExit:
        conn.close()
        raise
    updated = {
        **current,
        "name": args.name or current["name"],
        "role": args.role or current["role"],
        "runtime": runtime,
        "workspace": args.workspace or current["workspace"],
        "status": args.status or current["status"],
        "updated_at": now(),
    }
    if args.dry_run:
        conn.close()
        emit({"ok": True, "dry_run": True, "changed": updated != current, "employee": updated})
        return 0
    conn.execute(
        """
        UPDATE employees
        SET name = ?, role = ?, runtime = ?, workspace = ?, status = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            updated["name"],
            updated["role"],
            updated["runtime"],
            updated["workspace"],
            updated["status"],
            updated["updated_at"],
            employee_id,
        ),
    )
    profile = {
        "id": employee_id,
        "name": updated["name"],
        "role": updated["role"],
        "runtime": updated["runtime"],
        "workspace": updated["workspace"],
        "status": updated["status"],
        "created_at": current.get("created_at", ""),
        "updated_at": updated["updated_at"],
    }
    files = write_employee_files(employee_id, profile, dry_run=False)
    conn.commit()
    audit(conn, "companyctl", "employee.update", employee_id, {"before": current, "after": updated, "files": files})
    conn.close()
    emit({"ok": True, "changed": updated != current, "employee": updated, "files": files})
    return 0


def employee_file_bundle(conn: sqlite3.Connection, employee_id: str) -> dict:
    employee_id = resolve_employee_alias(employee_id)
    row = conn.execute("SELECT * FROM employees WHERE id = ?", (employee_id,)).fetchone()
    if not row:
        raise SystemExit(f"unknown employee: {employee_id}")
    profile = dict(row)
    paths = employee_paths(employee_id)
    file_profile = load_json_or_default(paths["profile"], profile)
    write_employee_capabilities(employee_id, file_profile, dry_run=False)
    capabilities = load_json_or_default(paths["capabilities"], default_capabilities(file_profile))
    permissions = load_json_or_default(
        paths["permissions"],
        {
            "can_submit_tasks": True,
            "can_claim_tasks": True,
            "can_modify_kernel": False,
            "requires_approval_for": ["payment", "compensation", "salary", "penalty", "external_send"],
        },
    )
    heartbeat = load_json_or_default(paths["heartbeat"], {})
    return {
        "employee": profile,
        "profile": file_profile,
        "capabilities": capabilities,
        "permissions": permissions,
        "heartbeat": heartbeat,
        "files": {key: str(value) for key, value in paths.items()},
    }


def cmd_employee_show(args: argparse.Namespace) -> int:
    conn = connect()
    emit({"ok": True, **employee_file_bundle(conn, args.id)})
    return 0


def parse_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def cmd_employee_capabilities(args: argparse.Namespace) -> int:
    conn = connect()
    bundle = employee_file_bundle(conn, args.id)
    path = Path(bundle["files"]["capabilities"])
    capabilities = bundle["capabilities"]
    changed = False
    if args.set_skills:
        capabilities["skills"] = parse_csv(args.set_skills)
        changed = True
    if args.add_skill:
        skills = list(capabilities.get("skills", []))
        for skill in args.add_skill:
            if skill not in skills:
                skills.append(skill)
        capabilities["skills"] = skills
        changed = True
    if args.set_tools:
        capabilities["tools"] = parse_csv(args.set_tools)
        changed = True
    if args.add_tool:
        tools = list(capabilities.get("tools", []))
        for tool in args.add_tool:
            if tool not in tools:
                tools.append(tool)
        capabilities["tools"] = tools
        changed = True
    if args.set_task_types:
        capabilities["preferred_task_types"] = parse_csv(args.set_task_types)
        changed = True
    if changed:
        capabilities["updated_at"] = now()
        path.write_text(json.dumps(capabilities, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        audit(conn, "companyctl", "employee.capabilities.update", args.id, {"file": str(path), "capabilities": capabilities})
    emit({"ok": True, "changed": changed, "agent": resolve_employee_alias(args.id), "capabilities": capabilities, "file": str(path)})
    return 0


def cmd_employee_permissions(args: argparse.Namespace) -> int:
    conn = connect()
    bundle = employee_file_bundle(conn, args.id)
    path = Path(bundle["files"]["permissions"])
    permissions = bundle["permissions"]
    changed = False
    for key in ("can_submit_tasks", "can_claim_tasks", "can_modify_kernel"):
        value = getattr(args, key)
        if value != "keep":
            permissions[key] = value == "true"
            changed = True
    if args.requires_approval_for:
        permissions["requires_approval_for"] = parse_csv(args.requires_approval_for)
        changed = True
    if changed:
        permissions["updated_at"] = now()
        path.write_text(json.dumps(permissions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        audit(conn, "companyctl", "employee.permissions.update", args.id, {"file": str(path), "permissions": permissions})
    emit({"ok": True, "changed": changed, "agent": resolve_employee_alias(args.id), "permissions": permissions, "file": str(path)})
    return 0


def match_employee_score(bundle: dict, required_skills: list[str], preferred_tools: list[str], task_type: str, runtime: str, role: str) -> dict:
    employee = bundle["employee"]
    capabilities = bundle["capabilities"]
    permissions = bundle["permissions"]
    if employee.get("status") != "active":
        return {"score": -1, "reasons": ["employee inactive"]}
    if not permissions.get("can_claim_tasks", True):
        return {"score": -1, "reasons": ["cannot claim tasks"]}
    score = 0
    reasons = []
    skills = set(str(item).lower() for item in capabilities.get("skills", []))
    tools = set(str(item).lower() for item in capabilities.get("tools", []))
    task_types = set(str(item).lower() for item in capabilities.get("preferred_task_types", []))
    for skill in required_skills:
        key = skill.lower()
        if key in skills:
            score += 5
            reasons.append(f"skill:{skill}")
        else:
            score -= 2
            reasons.append(f"missing_skill:{skill}")
    for tool in preferred_tools:
        key = tool.lower()
        if key in tools:
            score += 2
            reasons.append(f"tool:{tool}")
    if task_type:
        if task_type.lower() in task_types:
            score += 3
            reasons.append(f"task_type:{task_type}")
        else:
            score -= 1
            reasons.append(f"nonpreferred_task_type:{task_type}")
    if runtime:
        if employee.get("runtime") == runtime:
            score += 4
            reasons.append(f"runtime:{runtime}")
        else:
            score -= 3
            reasons.append(f"runtime_mismatch:{employee.get('runtime')}")
    if role:
        if employee.get("role") == role or capabilities.get("role") == role:
            score += 2
            reasons.append(f"role:{role}")
    if capabilities.get("handoff", {}).get("can_receive_tasks", True):
        score += 1
    return {"score": score, "reasons": reasons}


def employee_matches(conn: sqlite3.Connection, args: argparse.Namespace) -> list[dict]:
    required_skills = parse_csv(getattr(args, "skills", ""))
    preferred_tools = parse_csv(getattr(args, "tools", ""))
    task_type = getattr(args, "task_type", "")
    runtime = getattr(args, "runtime", "")
    role = getattr(args, "role", "")
    candidates = []
    for row in conn.execute("SELECT id FROM employees ORDER BY id").fetchall():
        bundle = employee_file_bundle(conn, row["id"])
        decision = match_employee_score(bundle, required_skills, preferred_tools, task_type, runtime, role)
        if decision["score"] < 0 and not getattr(args, "include_unavailable", False):
            continue
        candidates.append(
            {
                "agent": bundle["employee"]["id"],
                "name": bundle["employee"]["name"],
                "role": bundle["employee"]["role"],
                "runtime": bundle["employee"]["runtime"],
                "score": decision["score"],
                "reasons": decision["reasons"],
                "skills": bundle["capabilities"].get("skills", []),
                "tools": bundle["capabilities"].get("tools", []),
                "preferred_task_types": bundle["capabilities"].get("preferred_task_types", []),
            }
        )
    candidates.sort(key=lambda item: (-item["score"], item["agent"]))
    limit = int(getattr(args, "limit", 0) or 0)
    return candidates[:limit] if limit else candidates


def cmd_employee_match(args: argparse.Namespace) -> int:
    conn = connect()
    matches = employee_matches(conn, args)
    emit({"ok": True, "matches": matches})
    return 0


def upsert_employee(conn: sqlite3.Connection, employee_id: str, name: str, role: str, runtime: str, workspace: str, *, dry_run: bool) -> dict:
    profile = {
        "id": employee_id,
        "name": name,
        "role": role,
        "runtime": runtime,
        "workspace": workspace,
        "created_at": now(),
    }
    files = write_employee_files(employee_id, profile, dry_run=dry_run)
    if dry_run:
        return {"employee": profile, "files": files}
    ensure_runtime(conn, runtime)
    ts = now()
    conn.execute(
        """
        INSERT INTO employees(id, name, role, runtime, workspace, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          name = excluded.name,
          role = excluded.role,
          runtime = excluded.runtime,
          workspace = excluded.workspace,
          status = 'active',
          updated_at = excluded.updated_at
        """,
        (employee_id, name, role, runtime, workspace, ts, ts),
    )
    conn.commit()
    audit(conn, "companyctl", "employee.upsert", employee_id, profile)
    return {"employee": profile, "files": files}


def cmd_employee_import_openclaw(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    if not config_path.exists():
        raise SystemExit(f"openclaw config not found: {config_path}")
    obj = json.loads(config_path.read_text(encoding="utf-8"))
    agents = obj.get("agents", {}).get("list", [])
    conn = connect()
    imported = []
    for agent in agents:
        agent_id = str(agent.get("id") or "").strip()
        if not agent_id:
            continue
        name = str(agent.get("identityName") or agent.get("name") or agent_id)
        workspace = str(agent.get("workspace") or "")
        role = "operator" if agent_id == "main" else "business-agent"
        imported.append(upsert_employee(conn, agent_id, name, role, "openclaw", workspace, dry_run=args.dry_run))
    emit({"ok": True, "dry_run": args.dry_run, "count": len(imported), "imported": imported})
    return 0


def update_employee_communication_profile(
    *,
    employee_id: str,
    name: str,
    role: str,
    alias: str,
    can_talk_to: list[str],
    can_assign_to: list[str],
    channel: str,
    handoff_mode: str,
    dry_run: bool,
) -> dict:
    config = load_communication_config()
    config.setdefault("version", 1)
    config.setdefault("policy", {"mode": "open"})
    aliases = config.setdefault("aliases", {})
    if alias:
        aliases[alias] = employee_id
    employees = config.setdefault("employees", {})
    profile = employees.setdefault(employee_id, {})
    profile.update(
        {
            "display_name": name,
            "role": role,
            "can_talk_to": [resolve_employee_alias(item) for item in can_talk_to],
            "can_assign_to": [resolve_employee_alias(item) for item in can_assign_to],
            "handoff_mode": handoff_mode,
        }
    )
    if channel:
        channels = config.setdefault("channels", {})
        channel_obj = channels.setdefault(channel, {"participants": [], "max_rounds_without_task": 20, "on_task_done": "continue_workflow"})
        participants = [resolve_employee_alias(item) for item in channel_obj.get("participants", [])]
        if employee_id not in participants:
            participants.append(employee_id)
        channel_obj["participants"] = participants
    if not dry_run:
        write_communication_config(config)
    return config


def workspace_is_managed(path: Path) -> bool:
    resolved = path.expanduser().resolve()
    root = ROOT.resolve()
    if resolved == root:
        return False
    try:
        resolved.relative_to(root)
        return True
    except ValueError:
        return False


def scaffold_employee_workspace(employee_id: str, name: str, role: str, runtime: str, workspace: str) -> list[str]:
    workspace_path = Path(workspace).expanduser().resolve()
    if not workspace_is_managed(workspace_path):
        return []
    workspace_path.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    if runtime == "hermes":
        files = {
            "SOUL.md": "\n".join(
                [
                    f"# {name} Persona",
                    "",
                    f"You are {name}, acting as `{role}` in the Super AI Company.",
                    "Use Company Kernel for task state, evidence, approvals, and communication.",
                    "",
                ]
            ),
            "AGENTS.md": "\n".join(
                [
                    f"# {name} Collaboration Rules",
                    "",
                    "- Communicate through `companyctl message` and `companyctl conversation`.",
                    "- Complete work with evidence or return a blocker.",
                    "- Request approval before high-risk external, payment, salary, penalty, or compensation actions.",
                    "",
                ]
            ),
        }
    elif runtime in {"codex", "local", "cursor"}:
        files = {
            "AGENTS.md": "\n".join(
                [
                    f"# {name} Execution Rules",
                    "",
                    "- Treat Company Kernel as the source of truth for tasks and evidence.",
                    "- Do not modify protected Company Kernel internals unless the task explicitly includes approval/RFC context.",
                    "- Run focused verification before marking tasks done.",
                    "",
                ]
            )
        }
    elif runtime == "openclaw":
        files = {
            "AGENTS.md": "\n".join(
                [
                    f"# {name} OpenClaw Employee Rules",
                    "",
                    "- Keep business state in the assigned OpenClaw workspace.",
                    "- Bridge work through Company Kernel tasks, messages, approvals, and evidence.",
                    "- Do not bypass high-risk approval gates.",
                    "",
                ]
            )
        }
    else:
        files = {
            "AGENTS.md": "\n".join(
                [
                    f"# {name} Employee Rules",
                    "",
                    "- Use Company Kernel for task state, messages, approvals, and evidence.",
                    "",
                ]
            )
        }
    for filename, content in files.items():
        target = workspace_path / filename
        if not target.exists():
            target.write_text(content, encoding="utf-8")
            written.append(str(target))
    return written


def cmd_employee_onboard(args: argparse.Namespace) -> int:
    conn = connect()
    try:
        require_runtime(conn, args.runtime)
    except SystemExit:
        conn.close()
        raise
    employee_id = resolve_employee_alias(args.id)
    result = upsert_employee(conn, employee_id, args.name, args.role, args.runtime, args.workspace, dry_run=args.dry_run)
    files = result["files"]
    capabilities = default_capabilities(result["employee"])
    if args.skills:
        capabilities["skills"] = parse_csv(args.skills)
    if args.tools:
        capabilities["tools"] = parse_csv(args.tools)
    if args.task_types:
        capabilities["preferred_task_types"] = parse_csv(args.task_types)
    if not args.dry_run:
        Path(files["capabilities"]).write_text(json.dumps(capabilities, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    scaffolded_files = [] if args.dry_run else scaffold_employee_workspace(employee_id, args.name, args.role, args.runtime, args.workspace)
    permissions = {
        "can_submit_tasks": not args.no_submit_tasks,
        "can_claim_tasks": not args.no_claim_tasks,
        "can_modify_kernel": args.can_modify_kernel,
        "requires_approval_for": parse_csv(args.requires_approval_for) or ["payment", "compensation", "salary", "penalty", "external_send"],
        "updated_at": now(),
    }
    if not args.dry_run:
        Path(files["permissions"]).write_text(json.dumps(permissions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    talk_targets = parse_csv(args.can_talk_to)
    assign_targets = parse_csv(args.can_assign_to)
    if args.open_communication:
        existing = [row["id"] for row in conn.execute("SELECT id FROM employees ORDER BY id").fetchall()] if not args.dry_run else []
        talk_targets = sorted(set(talk_targets + [item for item in existing if item != employee_id]))
        assign_targets = sorted(set(assign_targets + [item for item in existing if item != employee_id]))
    config = update_employee_communication_profile(
        employee_id=employee_id,
        name=args.name,
        role=args.role,
        alias=args.alias,
        can_talk_to=talk_targets,
        can_assign_to=assign_targets,
        channel=args.channel,
        handoff_mode=args.handoff_mode,
        dry_run=args.dry_run,
    )
    test_task = {}
    if args.create_test_task and not args.dry_run:
        task = submit_task_internal(
            conn,
            source=args.test_source,
            target=employee_id,
            title=f"Onboarding test: {employee_id}",
            description="请领取此测试任务，写入 heartbeat，并用 evidence 或 blocker 回传结果。",
            priority="P3",
            task_id=args.test_task_id or f"task-onboard-{slug(employee_id)}",
            metadata={"onboarding": True},
        )
        test_task = task["task"]
    if not args.dry_run:
        audit(
            conn,
            "companyctl",
            "employee.onboard",
            employee_id,
            {
                "capabilities": capabilities,
                "permissions": permissions,
                "communication_file": str(COMMUNICATIONS_PATH),
                "test_task": test_task,
                "scaffolded_files": scaffolded_files,
            },
        )
    emit(
        {
            "ok": True,
            "dry_run": args.dry_run,
            "employee": result["employee"],
            "files": files,
            "capabilities": capabilities,
            "permissions": permissions,
            "communication": {
                "file": str(COMMUNICATIONS_PATH),
                "alias": args.alias,
                "can_talk_to": talk_targets,
                "can_assign_to": assign_targets,
                "channel": args.channel,
                "policy": config.get("policy", {}),
            },
            "scaffolded_files": scaffolded_files,
            "test_task": test_task,
        }
    )
    return 0


def cmd_employee_offboard(args: argparse.Namespace) -> int:
    conn = connect()
    employee_id = resolve_employee_alias(args.id)
    row = conn.execute("SELECT * FROM employees WHERE id = ?", (employee_id,)).fetchone()
    if not row:
        conn.close()
        emit({"ok": False, "error": "unknown employee", "employee_id": employee_id})
        return 1
    employee = dict(row)
    managed_paths: list[Path] = []
    workspace = str(employee.get("workspace") or "")
    if workspace:
        workspace_path = Path(workspace).expanduser().resolve()
        if workspace_is_managed(workspace_path) and workspace_path.exists():
            managed_paths.append(workspace_path)
    employee_dir = EMPLOYEES_DIR / employee_id
    if employee_dir.exists():
        managed_paths.append(employee_dir)
    deleted_paths = sorted({str(path) for path in managed_paths})
    if args.dry_run:
        conn.close()
        emit({"ok": True, "dry_run": True, "action": "hard-delete" if args.hard_delete else "soft-delete", "employee": employee, "deleted_paths": deleted_paths})
        return 0
    ts = now()
    if args.hard_delete:
        import shutil

        for path in managed_paths:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        conn.execute("DELETE FROM employees WHERE id = ?", (employee_id,))
        conn.execute("DELETE FROM heartbeats WHERE agent_id = ?", (employee_id,))
        action = "hard-delete"
    else:
        conn.execute("UPDATE employees SET status = 'archived', updated_at = ? WHERE id = ?", (ts, employee_id))
        action = "soft-delete"
    config = load_communication_config()
    config.get("employees", {}).pop(employee_id, None)
    for alias, target in list(config.get("aliases", {}).items()):
        if target == employee_id:
            config["aliases"].pop(alias, None)
    write_communication_config(config)
    conn.commit()
    audit(conn, "companyctl", "employee.offboard", employee_id, {"action": action, "employee": employee, "deleted_paths": deleted_paths})
    conn.close()
    emit({"ok": True, "action": action, "employee": employee, "deleted_paths": deleted_paths})
    return 0


def require_employee(conn: sqlite3.Connection, employee_id: str) -> None:
    if not conn.execute("SELECT 1 FROM employees WHERE id = ?", (employee_id,)).fetchone():
        raise SystemExit(f"unknown employee: {employee_id}")


def cmd_task_submit(args: argparse.Namespace) -> int:
    conn = connect()
    source = resolve_employee_alias(args.source)
    target = resolve_employee_alias(args.target)
    require_employee(conn, source)
    require_employee(conn, target)
    policy = require_communication_allowed(source, target, "task.submit")
    approval_action = detect_route_approval_action(args.title, args.description, args.requires_approval)
    gate = route_approval_gate(conn, args, source, target, [{"agent": target, "reason": "direct_submit"}], approval_action)
    if not gate.get("allowed"):
        emit({"ok": False, "error": "approval required", "target": target, "approval_action": approval_action, "approval": gate["approval_request"], "approval_file": gate["file"]})
        return 2
    task_id = args.task_id or f"task-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    ts = now()
    conn.execute(
        """
        INSERT INTO tasks(id, source_agent, target_agent, title, description, priority, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 'submitted', ?, ?)
        """,
        (task_id, source, target, args.title, args.description, args.priority, ts, ts),
    )
    metadata = {"trace_id": new_trace_id(), "declared_changes": parse_csv(args.changed_files), "rfc": args.rfc, "approval": gate.get("approval")}
    conn.execute(
        "INSERT OR REPLACE INTO task_metadata(task_id, metadata_json, updated_at) VALUES (?, ?, ?)",
        (task_id, json.dumps(metadata, ensure_ascii=False), ts),
    )
    conn.commit()
    inbox = employee_paths(target)["inbox"]
    inbox.mkdir(parents=True, exist_ok=True)
    task_file = inbox / f"{task_id}.json"
    task = {
        "id": task_id,
        "source_agent": source,
        "target_agent": target,
        "title": args.title,
        "description": args.description,
        "priority": args.priority,
        "status": "submitted",
        "metadata": metadata,
        "communication_policy": policy,
        "created_at": ts,
    }
    task_file.write_text(json.dumps(task, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit(conn, source, "task.submit", task_id, task)
    emit({"ok": True, "task": task, "file": str(task_file)})
    return 0


def load_policy_config() -> dict:
    if not POLICY_PATH.exists():
        return {"route_approval": {"default_risk": "P1", "actions": DEFAULT_ROUTE_APPROVAL_ACTIONS}}
    obj = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    route = obj.setdefault("route_approval", {})
    route.setdefault("default_risk", "P1")
    route.setdefault("actions", DEFAULT_ROUTE_APPROVAL_ACTIONS)
    return obj


def detect_route_approval_action(title: str, description: str, explicit_action: str = "") -> str:
    if explicit_action:
        return explicit_action
    text = f"{title}\n{description}".lower()
    actions = load_policy_config().get("route_approval", {}).get("actions", DEFAULT_ROUTE_APPROVAL_ACTIONS)
    for action, keywords in actions.items():
        for keyword in keywords:
            if keyword.lower() in text:
                return action
    return ""


def route_approval_gate(conn: sqlite3.Connection, args: argparse.Namespace, source: str, target: str, matches: list[dict], approval_action: str) -> dict:
    if not approval_action:
        return {"allowed": True}
    task_id = args.task_id or f"route-{slug(args.title)}"
    approval_id = args.approval_id or f"approval-route-{slug(task_id)}-{slug(approval_action)}"
    if args.approval_id:
        gate = approved_gate(conn, args.approval_id, approval_action, source, target)
        if gate["allowed"]:
            return gate
    result = create_approval_internal(
        conn,
        source=source,
        action=approval_action,
        reason=f"task route requires approval before assigning high-risk task `{args.title}` to `{target}`",
        target=target,
        risk=args.risk or load_policy_config().get("route_approval", {}).get("default_risk", "P1"),
        evidence="",
        approval_id=approval_id,
        metadata={
            "route": True,
            "task_id": task_id,
            "title": args.title,
            "description": args.description,
            "target": target,
            "matches": matches[:5],
        },
    )
    return {"allowed": False, "approval_request": result["approval"], "file": result["file"]}


def cmd_task_route(args: argparse.Namespace) -> int:
    conn = connect()
    source = resolve_employee_alias(args.source)
    require_employee(conn, source)
    matches = employee_matches(conn, args)
    if not matches:
        emit({"ok": False, "error": "no matching employee", "criteria": {"skills": args.skills, "tools": args.tools, "task_type": args.task_type, "runtime": args.runtime, "role": args.role}})
        return 1
    target = matches[0]["agent"]
    approval_action = detect_route_approval_action(args.title, args.description, args.requires_approval)
    gate = route_approval_gate(conn, args, source, target, matches, approval_action)
    if not gate.get("allowed"):
        emit({"ok": False, "error": "approval required", "selected": matches[0], "approval_action": approval_action, "approval": gate["approval_request"], "approval_file": gate["file"]})
        return 2
    result = submit_task_internal(
        conn,
        source=source,
        target=target,
        title=args.title,
        description=args.description,
        priority=args.priority,
        task_id=args.task_id,
        metadata={
            "declared_changes": parse_csv(args.changed_files),
            "rfc": args.rfc,
            "route": {"criteria": {"skills": parse_csv(args.skills), "tools": parse_csv(args.tools), "task_type": args.task_type, "runtime": args.runtime, "role": args.role}, "matches": matches[:5], "approval": gate.get("approval")},
        },
    )
    audit(conn, source, "task.route", result["task"]["id"], {"target": target, "matches": matches[:5]})
    emit({"ok": True, "selected": matches[0], "matches": matches[:5], **result})
    return 0


def submit_task_internal(
    conn: sqlite3.Connection,
    *,
    source: str,
    target: str,
    title: str,
    description: str,
    priority: str = "P2",
    task_id: str = "",
    metadata: dict | None = None,
) -> dict:
    source = resolve_employee_alias(source)
    target = resolve_employee_alias(target)
    require_employee(conn, source)
    require_employee(conn, target)
    policy = require_communication_allowed(source, target, "task.submit")
    tid = task_id or f"task-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    ts = now()
    conn.execute(
        """
        INSERT INTO tasks(id, source_agent, target_agent, title, description, priority, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 'submitted', ?, ?)
        """,
        (tid, source, target, title, description, priority, ts, ts),
    )
    task_metadata_obj = {**(metadata or {})}
    task_metadata_obj.setdefault("trace_id", new_trace_id())
    conn.execute(
        "INSERT OR REPLACE INTO task_metadata(task_id, metadata_json, updated_at) VALUES (?, ?, ?)",
        (tid, json.dumps(task_metadata_obj, ensure_ascii=False), ts),
    )
    conn.commit()
    inbox = employee_paths(target)["inbox"]
    inbox.mkdir(parents=True, exist_ok=True)
    task_file = inbox / f"{tid}.json"
    task = {
        "id": tid,
        "source_agent": source,
        "target_agent": target,
        "title": title,
        "description": description,
        "priority": priority,
        "status": "submitted",
        "metadata": task_metadata_obj,
        "communication_policy": policy,
        "created_at": ts,
    }
    task_file.write_text(json.dumps(task, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit(conn, source, "task.submit", tid, task)
    return {"task": task, "file": str(task_file)}


def complete_task_internal(
    conn: sqlite3.Connection,
    *,
    agent: str,
    task_id: str,
    summary: str,
    evidence: str,
) -> dict:
    if not evidence.strip():
        raise ValueError("task evidence is required")
    cur = conn.execute(
        "UPDATE tasks SET status = 'completed', claimed_by = CASE WHEN claimed_by = '' THEN ? ELSE claimed_by END, summary = ?, evidence_path = ?, blocker = '', updated_at = ? WHERE id = ? AND (target_agent = ? OR claimed_by = ?)",
        (agent, summary, evidence, now(), task_id, agent, agent),
    )
    if cur.rowcount == 0:
        raise SystemExit(f"task not found or not owned by agent: {task_id}")
    conn.execute("DELETE FROM locks WHERE resource_key = ?", (f"task:{task_id}",))
    synced_plan_items = sync_project_plan_for_task(conn, task_id=task_id, task_status="completed", actor=agent)
    conn.commit()
    event = record_event(conn, "task.done", agent, task_id=task_id, payload={"summary": summary, "evidence": evidence})
    audit(conn, agent, "task.done", task_id, {"summary": summary, "evidence": evidence, "event_id": event["id"]})
    return {"task_id": task_id, "status": "completed", "evidence": evidence, "event_id": event["id"], "synced_plan_items": synced_plan_items}


def write_task_inbox_file(task: dict) -> str:
    target = task["target_agent"]
    inbox = employee_paths(target)["inbox"]
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / f"{task['id']}.json"
    path.write_text(json.dumps(task, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


def task_with_children(conn: sqlite3.Connection, task_id: str) -> dict:
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        raise SystemExit(f"task not found: {task_id}")
    children = rows(
        conn,
        """
        SELECT t.*, tr.created_by AS relation_created_by, tr.created_at AS relation_created_at
        FROM task_relations tr
        JOIN tasks t ON t.id = tr.child_task_id
        WHERE tr.parent_task_id = ?
        ORDER BY tr.created_at ASC
        """,
        (task_id,),
    )
    return {"task": dict(task), "children": children}


def task_metadata(conn: sqlite3.Connection, task_id: str) -> dict:
    row = conn.execute("SELECT metadata_json FROM task_metadata WHERE task_id = ?", (task_id,)).fetchone()
    if not row:
        return {}
    try:
        parsed = json.loads(row["metadata_json"] or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def update_task_metadata(conn: sqlite3.Connection, task_id: str, patch: dict) -> dict:
    metadata = task_metadata(conn, task_id)
    metadata.update(patch)
    conn.execute(
        "INSERT OR REPLACE INTO task_metadata(task_id, metadata_json, updated_at) VALUES (?, ?, ?)",
        (task_id, json.dumps(metadata, ensure_ascii=False), now()),
    )
    return metadata


def sync_project_plan_for_task(conn: sqlite3.Connection, *, task_id: str, task_status: str, actor: str) -> list[dict]:
    plan_status = {"completed": "done", "blocked": "blocked", "submitted": "in_progress", "claimed": "in_progress"}.get(task_status)
    if not plan_status:
        return []
    ts = now()
    plan_items = rows(conn, "SELECT * FROM project_plan_items WHERE task_id = ? ORDER BY created_at ASC", (task_id,))
    updated = []
    for item in plan_items:
        if item["status"] in {"done", "completed", "cancelled"} and plan_status == "done":
            continue
        if task_status in {"submitted", "claimed"} and item["status"] != "blocked":
            continue
        if item["status"] == plan_status:
            continue
        conn.execute("UPDATE project_plan_items SET status = ?, updated_at = ? WHERE id = ?", (plan_status, ts, item["id"]))
        conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (ts, item["project_id"]))
        updated.append({**item, "status": plan_status, "updated_at": ts})
    if updated:
        audit(conn, actor, "project.plan_sync", task_id, {"task_status": task_status, "plan_status": plan_status, "plan_items": [item["id"] for item in updated]})
    return updated


def sync_project_plan_owner_for_task(conn: sqlite3.Connection, *, task_id: str, owner: str, actor: str) -> list[dict]:
    ts = now()
    plan_items = rows(conn, "SELECT * FROM project_plan_items WHERE task_id = ? ORDER BY created_at ASC", (task_id,))
    updated = []
    for item in plan_items:
        if item["owner_agent"] == owner:
            continue
        conn.execute("UPDATE project_plan_items SET owner_agent = ?, updated_at = ? WHERE id = ?", (owner, ts, item["id"]))
        conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (ts, item["project_id"]))
        updated.append({**item, "owner_agent": owner, "updated_at": ts})
    if updated:
        audit(conn, actor, "project.plan_owner_sync", task_id, {"owner": owner, "plan_items": [item["id"] for item in updated]})
    return updated


def guard_task_claim(conn: sqlite3.Connection, task: sqlite3.Row, agent: str) -> dict:
    metadata = task_metadata(conn, task["id"])
    declared = metadata.get("declared_changes", [])
    if isinstance(declared, str):
        declared = parse_csv(declared)
    rfc = str(metadata.get("rfc", "") or "")
    if rfc:
        declared = [path for path in declared if not normalize_repo_path(path).startswith("rfcs/")]
    if not declared:
        return {"allowed": True, "metadata": metadata, "checks": []}
    config = load_protected_paths_config()
    checks = [protected_path_decision(path, config) for path in declared]
    blocked = [check for check in checks if not check["allowed"]]
    if blocked and rfc:
        rfc_check = protected_path_decision(rfc, config)
        rfc_approval = approved_rfc_covers(conn, rfc, blocked)
        if rfc_approval["allowed"]:
            return {"allowed": True, "metadata": metadata, "checks": checks, "rfc": rfc, "rfc_approval": rfc_approval}
        return {"allowed": False, "metadata": metadata, "checks": checks, "blocked": blocked, "rfc": rfc, "rfc_check": rfc_check, "rfc_approval": rfc_approval, "reason": "protected changes require approved RFC"}
    if blocked:
        return {"allowed": False, "metadata": metadata, "checks": checks, "blocked": blocked, "reason": "protected changes require RFC"}
    return {"allowed": True, "metadata": metadata, "checks": checks}


def write_task_collection_report(parent_task: dict, children: list[dict], collector: str, summary: str) -> Path:
    report_dir = employee_paths(collector)["reports"] / parent_task["id"]
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / "task-collection-report.md"
    lines = [
        "# Task Collection Report",
        "",
        f"- parent_task: `{parent_task['id']}`",
        f"- collector: `{collector}`",
        f"- summary: {summary}",
        "",
        "## Children",
        "",
    ]
    for child in children:
        lines.extend(
            [
                f"### {child['id']}",
                "",
                f"- target: `{child['target_agent']}`",
                f"- status: `{child['status']}`",
                f"- summary: {child.get('summary') or ''}",
                f"- evidence: {child.get('evidence_path') or ''}",
                f"- blocker: {child.get('blocker') or ''}",
                "",
            ]
        )
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


def cmd_message_send(args: argparse.Namespace) -> int:
    conn = connect()
    result = send_message_internal(conn, source=args.source, target=args.target, body=args.body, message_id=args.message_id)
    emit({"ok": True, **result})
    return 0


def parse_openclaw_payload_text(stdout: str) -> str:
    payload = parse_json_output(stdout)
    result = payload.get("result") if isinstance(payload, dict) else {}
    payloads = result.get("payloads") if isinstance(result, dict) else []
    if isinstance(payloads, list):
        texts = [str(item.get("text", "")) for item in payloads if isinstance(item, dict) and item.get("text")]
        if texts:
            return "\n".join(texts)
    return str(payload.get("summary") or "")


def cmd_message_direct(args: argparse.Namespace) -> int:
    conn = connect()
    source = resolve_employee_alias(args.source)
    target = resolve_employee_alias(args.target)
    require_employee(conn, source)
    target_row = conn.execute("SELECT * FROM employees WHERE id = ?", (target,)).fetchone()
    if not target_row:
        raise SystemExit(f"unknown employee: {target}")
    target_employee = dict(target_row)
    require_communication_allowed(source, target, "message.direct")
    runtime = str(target_employee.get("runtime") or "")
    if runtime not in {"openclaw", "hermes"}:
        emit({"ok": False, "error": "direct send unsupported runtime", "target": target, "runtime": runtime})
        return 2
    session_key = args.session_key or f"agent:{target}:{source}"
    agent_runtime_id = attendance_agent_runtime_id(target, runtime)
    cmd = ["openclaw", "agent", "--agent", agent_runtime_id, "--session-key", session_key, "--message", args.body, "--timeout", str(args.timeout), "--json"]
    if args.deliver:
        cmd.append("--deliver")
    if args.reply_channel:
        cmd.extend(["--reply-channel", args.reply_channel])
    if args.reply_to:
        cmd.extend(["--reply-to", args.reply_to])
    if args.reply_account:
        cmd.extend(["--reply-account", args.reply_account])
    try:
        cp = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True, timeout=args.timeout + 10)
    except Exception as exc:
        emit({"ok": False, "error": str(exc), "target": target, "runtime": runtime, "session_key": session_key, "command": cmd})
        return 1
    reply = parse_openclaw_payload_text(cp.stdout)
    message_record = send_message_internal(conn, source=source, target=target, body=args.body, message_id=args.message_id)
    result = {
        "ok": cp.returncode == 0,
        "source": source,
        "target": target,
        "runtime": runtime,
        "agent_runtime_id": agent_runtime_id,
        "session_key": session_key,
        "reply": reply,
        "exit_code": cp.returncode,
        "message": message_record["message"],
        "file": message_record["file"],
        "stderr": cp.stderr[-2000:],
    }
    emit(result)
    return 0 if cp.returncode == 0 else 1


def cmd_message_list(args: argparse.Namespace) -> int:
    conn = connect()
    agent = resolve_employee_alias(args.agent)
    require_employee(conn, agent)
    emit(
        {
            "ok": True,
            "messages": rows(
                conn,
                "SELECT * FROM messages WHERE target_agent = ? OR source_agent = ? ORDER BY created_at DESC",
                (agent, agent),
            ),
        }
    )
    return 0


def cmd_communication_show(args: argparse.Namespace) -> int:
    config = load_communication_config()
    employees = config.get("employees", {})
    source = resolve_employee_alias(args.agent) if args.agent else ""
    if source:
        info = employees.get(source, {})
        emit(
            {
                "ok": True,
                "policy": config.get("policy", {"mode": "open"}),
                "agent": source,
                "profile": info,
                "can_talk_to": communication_list(config, source, "can_talk_to"),
                "can_assign_to": communication_list(config, source, "can_assign_to"),
                "blocked_talk_to": communication_list(config, source, "blocked_talk_to"),
                "blocked_assign_to": communication_list(config, source, "blocked_assign_to"),
            }
        )
        return 0
    emit({"ok": True, "policy": config.get("policy", {"mode": "open"}), "aliases": config.get("aliases", {}), "employees": employees, "channels": config.get("channels", {})})
    return 0


def cmd_communication_check(args: argparse.Namespace) -> int:
    source = resolve_employee_alias(args.source)
    target = resolve_employee_alias(args.target)
    action = "task.submit" if args.action == "assign" else "message.send"
    decision = communication_policy_decision(source, target, action)
    emit({"ok": decision["allowed"], "decision": decision})
    return 0 if decision["allowed"] else 1


def cmd_policy_show(_args: argparse.Namespace) -> int:
    emit({"ok": True, "policy": load_policy_config(), "file": str(POLICY_PATH)})
    return 0


def load_protected_paths_config() -> dict:
    if not PROTECTED_PATHS_CONFIG.exists():
        return {
            "requires_rfc": True,
            "protected": ["company_kernel/**", "config/policy.json", "company.sqlite", "state/**", "employees/*/permissions.json"],
            "rfc_allowed": ["rfcs/**"],
        }
    return json.loads(PROTECTED_PATHS_CONFIG.read_text(encoding="utf-8"))


def normalize_repo_path(path: str) -> str:
    p = Path(path)
    if p.is_absolute():
        try:
            return p.resolve().relative_to(ROOT).as_posix()
        except ValueError:
            return p.as_posix().lstrip("/")
    return p.as_posix().lstrip("./")


def protected_path_decision(path: str, config: dict) -> dict:
    rel = normalize_repo_path(path)
    rfc_allowed = any(fnmatch.fnmatch(rel, pattern) for pattern in config.get("rfc_allowed", []))
    matched = [pattern for pattern in config.get("protected", []) if fnmatch.fnmatch(rel, pattern)]
    protected = bool(matched)
    allowed = not protected or rfc_allowed
    return {"path": rel, "protected": protected, "allowed": allowed, "matched": matched, "rfc_allowed": rfc_allowed}


def cmd_guard_check(args: argparse.Namespace) -> int:
    config = load_protected_paths_config()
    paths = list(args.path or []) + list(args.changed_file or [])
    if not paths:
        emit({"ok": True, "config": config, "checks": []})
        return 0
    checks = [protected_path_decision(path, config) for path in paths]
    blocked = [check for check in checks if not check["allowed"]]
    emit({"ok": not blocked, "requires_rfc": bool(config.get("requires_rfc", True)), "blocked": blocked, "checks": checks, "config_file": str(PROTECTED_PATHS_CONFIG)})
    return 1 if blocked else 0


def parse_participants(raw: str) -> list[str]:
    participants = []
    for item in raw.split(","):
        item = item.strip()
        if item and item not in participants:
            participants.append(item)
    return participants


def notify_conversation_participants(conversation_id: str, message: dict, participants: list[str]) -> dict[str, str]:
    files = {}
    for participant in participants:
        inbox = employee_paths(participant)["inbox"]
        inbox.mkdir(parents=True, exist_ok=True)
        path = inbox / f"{conversation_id}.{message['id']}.conversation.json"
        path.write_text(json.dumps({"type": "conversation_message", "conversation_id": conversation_id, "message": message}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        files[participant] = str(path)
    return files


def conversation_start_internal(
    conn: sqlite3.Connection,
    *,
    source: str,
    participants: list[str],
    title: str,
    body: str,
    evidence: str = "",
    conversation_id: str = "",
) -> dict:
    source = resolve_employee_alias(source)
    participants = [resolve_employee_alias(participant) for participant in participants]
    require_employee(conn, source)
    if source not in participants:
        participants.insert(0, source)
    for participant in participants:
        require_employee(conn, participant)
        if participant != source:
            require_communication_allowed(source, participant, "conversation.start")
    cid = conversation_id or f"conv-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    message_id = f"cmsg-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    ts = now()
    conn.execute(
        """
        INSERT INTO conversations(id, title, created_by, participants_json, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'open', ?, ?)
        """,
        (cid, title, source, json.dumps(participants, ensure_ascii=False), ts, ts),
    )
    conn.execute(
        """
        INSERT INTO conversation_messages(id, conversation_id, source_agent, body, evidence_path, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (message_id, cid, source, body, evidence, ts),
    )
    conn.commit()
    message = {"id": message_id, "source_agent": source, "body": body, "evidence_path": evidence, "created_at": ts}
    files = notify_conversation_participants(cid, message, participants)
    event = record_event(
        conn,
        "conversation.message",
        source,
        payload={"conversation_id": cid, "message_id": message_id, "participants": participants, "body": body, "evidence": evidence, "files": files, "is_start": True},
    )
    audit(conn, source, "conversation.start", cid, {"title": title, "participants": participants, "event_id": event["id"]})
    return {"conversation": {"id": cid, "title": title, "participants": participants, "status": "open"}, "message": message, "files": files, "event_id": event["id"]}


def cmd_conversation_start(args: argparse.Namespace) -> int:
    conn = connect()
    participants = parse_participants(args.participants)
    result = conversation_start_internal(conn, source=args.source, participants=participants, title=args.title, body=args.body, evidence=args.evidence, conversation_id=args.conversation_id)
    emit({"ok": True, **result})
    return 0


def conversation_reply_internal(
    conn: sqlite3.Connection,
    *,
    source: str,
    conversation_id: str,
    body: str,
    evidence: str = "",
    message_id: str = "",
) -> dict:
    source = resolve_employee_alias(source)
    require_employee(conn, source)
    conv = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
    if not conv:
        raise SystemExit(f"conversation not found: {conversation_id}")
    participants = json.loads(conv["participants_json"])
    if source not in participants:
        raise SystemExit(f"source is not a participant: {source}")
    mid = message_id or f"cmsg-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    ts = now()
    conn.execute(
        """
        INSERT INTO conversation_messages(id, conversation_id, source_agent, body, evidence_path, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (mid, conversation_id, source, body, evidence, ts),
    )
    conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (ts, conversation_id))
    conn.commit()
    message = {"id": mid, "source_agent": source, "body": body, "evidence_path": evidence, "created_at": ts}
    files = notify_conversation_participants(conversation_id, message, participants)
    event = record_event(
        conn,
        "conversation.message",
        source,
        payload={"conversation_id": conversation_id, "message_id": mid, "participants": participants, "body": body, "evidence": evidence, "files": files, "is_start": False},
    )
    audit(conn, source, "conversation.reply", conversation_id, {"message_id": mid, "event_id": event["id"]})
    return {"conversation_id": conversation_id, "message": message, "files": files, "event_id": event["id"]}


def cmd_conversation_reply(args: argparse.Namespace) -> int:
    conn = connect()
    result = conversation_reply_internal(conn, source=args.source, conversation_id=args.conversation_id, body=args.body, evidence=args.evidence, message_id=args.message_id)
    emit({"ok": True, **result})
    return 0


def cmd_conversation_list(args: argparse.Namespace) -> int:
    conn = connect()
    require_employee(conn, args.agent)
    all_rows = rows(conn, "SELECT * FROM conversations ORDER BY updated_at DESC")
    conversations = []
    for conv in all_rows:
        participants = json.loads(conv["participants_json"])
        if args.agent in participants:
            conv["participants"] = participants
            del conv["participants_json"]
            conversations.append(conv)
    emit({"ok": True, "conversations": conversations})
    return 0


def cmd_conversation_show(args: argparse.Namespace) -> int:
    conn = connect()
    conv = conn.execute("SELECT * FROM conversations WHERE id = ?", (args.conversation_id,)).fetchone()
    if not conv:
        emit({"ok": False, "error": "conversation not found", "conversation_id": args.conversation_id})
        return 1
    obj = dict(conv)
    obj["participants"] = json.loads(obj.pop("participants_json"))
    messages = rows(conn, "SELECT * FROM conversation_messages WHERE conversation_id = ? ORDER BY created_at ASC", (args.conversation_id,))
    emit({"ok": True, "conversation": obj, "messages": messages})
    return 0


def task_conversation_ids(metadata: dict) -> list[str]:
    values = metadata.get("conversation_ids", [])
    if isinstance(values, str):
        values = [values] if values else []
    return [str(value) for value in values if str(value)]


def cmd_task_discuss(args: argparse.Namespace) -> int:
    conn = connect()
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
    if not task:
        emit({"ok": False, "error": "task not found", "task_id": args.task_id})
        return 1
    source = resolve_employee_alias(args.source or task["source_agent"])
    extra_participants = [resolve_employee_alias(item) for item in parse_participants(args.participants)]
    participants = []
    for participant in [source, task["source_agent"], task["target_agent"], task["claimed_by"], *extra_participants]:
        participant = resolve_employee_alias(str(participant or ""))
        if participant and participant not in participants:
            participants.append(participant)
    title = args.title or f"Task discussion: {task['id']} - {task['title']}"
    body = args.body or f"Discuss task `{task['id']}`: {task['title']}"
    conversation_id = args.conversation_id or f"conv-task-{slug(task['id'])}-{uuid.uuid4().hex[:6]}"
    result = conversation_start_internal(conn, source=source, participants=participants, title=title, body=body, evidence=args.evidence, conversation_id=conversation_id)
    metadata = task_metadata(conn, task["id"])
    conversation_ids = task_conversation_ids(metadata)
    if result["conversation"]["id"] not in conversation_ids:
        conversation_ids.append(result["conversation"]["id"])
    update_task_metadata(conn, task["id"], {"conversation_ids": conversation_ids})
    audit(conn, source, "task.discuss", task["id"], {"conversation_id": result["conversation"]["id"], "participants": participants})
    emit({"ok": True, "task_id": task["id"], **result, "conversation_ids": conversation_ids})
    return 0


def cmd_task_conversations(args: argparse.Namespace) -> int:
    conn = connect()
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
    if not task:
        emit({"ok": False, "error": "task not found", "task_id": args.task_id})
        return 1
    conversation_ids = task_conversation_ids(task_metadata(conn, args.task_id))
    conversations = []
    for conversation_id in conversation_ids:
        conv = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        if not conv:
            continue
        obj = dict(conv)
        obj["participants"] = json.loads(obj.pop("participants_json"))
        obj["messages"] = rows(conn, "SELECT * FROM conversation_messages WHERE conversation_id = ? ORDER BY created_at ASC", (conversation_id,))
        conversations.append(obj)
    emit({"ok": True, "task_id": args.task_id, "conversation_ids": conversation_ids, "conversations": conversations})
    return 0


def load_json_file(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"config not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_workflow_path(name_or_path: str) -> Path:
    direct = Path(name_or_path)
    if direct.exists():
        return direct
    candidate = WORKFLOW_DIR / name_or_path
    if candidate.exists():
        return candidate
    if not name_or_path.endswith(".json"):
        candidate = WORKFLOW_DIR / f"{name_or_path}.json"
        if candidate.exists():
            return candidate
    raise SystemExit(f"workflow not found: {name_or_path}")


def workflow_assert_employee(conn: sqlite3.Connection, employee_id: str, dry_run: bool) -> None:
    if conn.execute("SELECT 1 FROM employees WHERE id = ?", (employee_id,)).fetchone():
        return
    if dry_run:
        return
    raise SystemExit(f"unknown employee in workflow: {employee_id}")


def workflow_render(text: str, context: dict) -> str:
    rendered = text
    for key, value in context.items():
        if isinstance(value, (str, int, float)):
            rendered = rendered.replace("{{" + key + "}}", str(value))
    return rendered


def workflow_report_path(agent: str, run_id: str, name: str) -> Path:
    safe_name = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in name).strip("-") or "report"
    report_dir = employee_paths(agent)["reports"] / run_id
    report_dir.mkdir(parents=True, exist_ok=True)
    return report_dir / f"{safe_name}.md"


def workflow_write_event(run_id: str, event: dict) -> Path:
    run_dir = STATE_DIR / "workflow-runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    event_path = run_dir / "events.jsonl"
    with event_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event_path


def cmd_workflow_validate(args: argparse.Namespace) -> int:
    conn = connect()
    workflow_path = resolve_workflow_path(args.workflow)
    workflow = load_json_file(workflow_path)
    employees = [resolve_employee_alias(employee_id) for employee_id in workflow.get("employees", [])]
    steps = workflow.get("steps", [])
    missing = []
    for employee_id in employees:
        if not conn.execute("SELECT 1 FROM employees WHERE id = ?", (employee_id,)).fetchone():
            missing.append(employee_id)
    emit({"ok": not missing, "workflow": str(workflow_path), "employees": employees, "missing_employees": missing, "steps": len(steps)})
    return 0 if not missing else 1


def cmd_workflow_run(args: argparse.Namespace) -> int:
    conn = connect()
    workflow_path = resolve_workflow_path(args.workflow)
    workflow = load_json_file(workflow_path)
    run_id = args.run_id or f"wf-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    context: dict = {
        "run_id": run_id,
        "workflow_id": workflow.get("id", workflow_path.stem),
        "topic": args.topic or workflow.get("topic", ""),
    }
    aliases = load_communication_config().get("aliases", {})
    employees = [str(aliases.get(employee_id, employee_id)) for employee_id in workflow.get("employees", [])]
    for employee_id in employees:
        workflow_assert_employee(conn, employee_id, args.dry_run)
    max_steps = args.max_steps or len(workflow.get("steps", []))
    events = []
    for index, step in enumerate(workflow.get("steps", [])[:max_steps], start=1):
        event = {"index": index, "type": step.get("type", ""), "at": now()}
        step_type = step.get("type")
        if step_type == "conversation_start":
            source = resolve_employee_alias(step["from"])
            participants = [resolve_employee_alias(p) for p in step["participants"]]
            conversation_id = workflow_render(step.get("conversation_id") or f"{workflow.get('id', 'workflow')}-{run_id}", context)
            body = workflow_render(step["body"], context)
            title = workflow_render(step.get("title", workflow.get("title", "Workflow conversation")), context)
            if not args.dry_run:
                conversation_start_internal(conn, source=source, participants=participants, title=title, body=body, evidence="", conversation_id=conversation_id)
            event.update({"source": source, "participants": participants, "conversation_id": conversation_id, "body": body})
            context["conversation_id"] = conversation_id
        elif step_type == "conversation_reply":
            source = resolve_employee_alias(step["from"])
            conversation_id = workflow_render(step.get("conversation_id", "{{conversation_id}}"), context)
            body = workflow_render(step["body"], context)
            if not args.dry_run:
                conversation_reply_internal(conn, source=source, conversation_id=conversation_id, body=body, evidence="", message_id=step.get("message_id", ""))
            event.update({"source": source, "conversation_id": conversation_id, "body": body})
        elif step_type == "task_submit":
            source_alias = step["from"]
            target_alias = step["to"]
            source = resolve_employee_alias(source_alias)
            target = resolve_employee_alias(target_alias)
            title = workflow_render(step["title"], context)
            description = workflow_render(step.get("description", ""), context)
            task_id = workflow_render(step.get("task_id") or f"{run_id}-{target}-{index}", context)
            if not args.dry_run:
                result = submit_task_internal(conn, source=source, target=target, title=title, description=description, priority=step.get("priority", "P2"), task_id=task_id, metadata={"workflow_run_id": run_id, "step": index})
                event.update(result)
            event.update({"source": source, "target": target, "task_id": task_id, "title": title})
            context[f"{target}_task_id"] = task_id
            context[f"{target_alias}_task_id"] = task_id
            context["last_task_id"] = task_id
        elif step_type == "task_execute":
            agent = resolve_employee_alias(step["agent"])
            task_id = workflow_render(step.get("task_id", "{{last_task_id}}"), context)
            summary = workflow_render(step.get("summary", "完成"), context)
            report = workflow_render(step.get("report", summary), context)
            evidence_path = workflow_report_path(agent, run_id, step.get("evidence_name", task_id))
            if not args.dry_run:
                evidence_path.write_text(report + "\n", encoding="utf-8")
                complete_task_internal(conn, agent=agent, task_id=task_id, summary=summary, evidence=str(evidence_path))
            event.update({"agent": agent, "task_id": task_id, "summary": summary, "evidence": str(evidence_path)})
            context["last_evidence"] = str(evidence_path)
        elif step_type == "heartbeat":
            agent = resolve_employee_alias(step["agent"])
            if not args.dry_run:
                heartbeat_internal(conn, agent, {"source": "workflow", "run_id": run_id, "step": index})
            event.update({"agent": agent})
        else:
            raise SystemExit(f"unknown workflow step type: {step_type}")
        event_path = workflow_write_event(run_id, event)
        events.append(event)
    audit(conn, "companyctl", "workflow.run", run_id, {"workflow": str(workflow_path), "dry_run": args.dry_run, "events": len(events)})
    emit({"ok": True, "dry_run": args.dry_run, "run_id": run_id, "workflow": str(workflow_path), "events": events, "event_log": str(event_path) if events else ""})
    return 0


def load_hooks_config() -> dict:
    if not HOOKS_PATH.exists():
        return {"hooks": []}
    return json.loads(HOOKS_PATH.read_text(encoding="utf-8"))


def event_payload(event: sqlite3.Row | dict) -> dict:
    raw = event["payload_json"] if isinstance(event, sqlite3.Row) else event.get("payload_json", "{}")
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}


def hook_matches(conn: sqlite3.Connection, hook: dict, event: sqlite3.Row) -> bool:
    if not hook.get("enabled", True):
        return False
    payload = event_payload(event)
    match = hook.get("match", {})
    if match.get("event_type") and match["event_type"] != event["event_type"]:
        return False
    if match.get("source_agent") and resolve_employee_alias(match["source_agent"]) != event["source_agent"]:
        return False
    if match.get("task_id") and match["task_id"] != event["task_id"]:
        return False
    if match.get("conversation_id") and match["conversation_id"] != payload.get("conversation_id", ""):
        return False
    if match.get("participant"):
        expected_participant = resolve_employee_alias(match["participant"])
        participants = [resolve_employee_alias(str(item)) for item in payload.get("participants", [])]
        if expected_participant not in participants:
            return False
    if match.get("body_contains"):
        required = match["body_contains"]
        if isinstance(required, str):
            required = [required]
        body = str(payload.get("body", ""))
        if not any(str(item) in body for item in required):
            return False
    if match.get("skip_child_tasks") and event["task_id"]:
        relation = conn.execute("SELECT 1 FROM task_relations WHERE child_task_id = ?", (event["task_id"],)).fetchone()
        if relation:
            return False
    target_agent = match.get("target_agent")
    if target_agent:
        expected = resolve_employee_alias(target_agent)
        actual = ""
        if event["task_id"]:
            task = conn.execute("SELECT target_agent FROM tasks WHERE id = ?", (event["task_id"],)).fetchone()
            actual = task["target_agent"] if task else ""
        else:
            participants = [resolve_employee_alias(str(item)) for item in payload.get("participants", [])]
            actual = resolve_employee_alias(str(payload.get("target_agent", ""))) if payload.get("target_agent") else ""
        if expected != actual and expected not in participants:
            return False
    return True


def render_hook_text(text: str, event: sqlite3.Row, payload: dict, extra: dict | None = None) -> str:
    context = {
        "event_id": event["id"],
        "event_type": event["event_type"],
        "source_agent": event["source_agent"],
        "task_id": event["task_id"],
        "summary": payload.get("summary", ""),
        "evidence": payload.get("evidence", ""),
        "message_id": payload.get("message_id", ""),
        "conversation_id": payload.get("conversation_id", ""),
        "participants": ",".join(str(item) for item in payload.get("participants", [])),
        "target_agent": payload.get("target_agent", ""),
        "body": payload.get("body", ""),
    }
    if extra:
        context.update(extra)
    return workflow_render(text, context)


def send_message_internal(conn: sqlite3.Connection, *, source: str, target: str, body: str, message_id: str = "") -> dict:
    source = resolve_employee_alias(source)
    target = resolve_employee_alias(target)
    require_employee(conn, source)
    require_employee(conn, target)
    policy = require_communication_allowed(source, target, "message.send")
    mid = message_id or f"msg-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    ts = now()
    conn.execute(
        "INSERT INTO messages(id, source_agent, target_agent, body, created_at) VALUES (?, ?, ?, ?, ?)",
        (mid, source, target, body, ts),
    )
    conn.commit()
    inbox = employee_paths(target)["inbox"]
    inbox.mkdir(parents=True, exist_ok=True)
    message = {
        "id": mid,
        "source_agent": source,
        "target_agent": target,
        "body": body,
        "created_at": ts,
        "type": "message",
        "communication_policy": policy,
    }
    message_file = inbox / f"{mid}.message.json"
    message_file.write_text(json.dumps(message, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    event = record_event(
        conn,
        "message.send",
        source,
        payload={"message_id": mid, "target_agent": target, "body": body, "file": str(message_file)},
    )
    audit(conn, source, "message.send", mid, {**message, "event_id": event["id"]})
    return {"message": message, "file": str(message_file), "event_id": event["id"]}


def slug(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in value).strip("-") or "item"


def approved_gate(conn: sqlite3.Connection, approval_id: str, approval_action: str, source: str, target: str) -> dict:
    if not approval_id:
        return {"allowed": False, "reason": "missing approval_id"}
    row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    if not row:
        return {"allowed": False, "reason": "approval not found", "approval_id": approval_id}
    approval = normalize_approval(row)
    detail = approval["detail"]
    if approval["status"] != "approved":
        return {"allowed": False, "reason": f"approval is {approval['status']}", "approval": approval}
    if approval["action"] != approval_action:
        return {"allowed": False, "reason": "approval action mismatch", "approval": approval}
    if detail.get("requested_by") and detail["requested_by"] != source:
        return {"allowed": False, "reason": "approval requester mismatch", "approval": approval}
    if target and detail.get("target") and detail["target"] != target:
        return {"allowed": False, "reason": "approval target mismatch", "approval": approval}
    return {"allowed": True, "approval": approval}


def find_matching_approved_approval(conn: sqlite3.Connection, approval_action: str, source: str, target: str, event: sqlite3.Row, hook_id: str) -> dict | None:
    candidates = conn.execute(
        "SELECT * FROM approvals WHERE status = 'approved' AND source_agent = ? AND action = ? ORDER BY updated_at DESC",
        (source, approval_action),
    ).fetchall()
    for row in candidates:
        approval = normalize_approval(row)
        detail = approval["detail"]
        metadata = detail.get("metadata", {}) if isinstance(detail.get("metadata", {}), dict) else {}
        if target and detail.get("target") and detail["target"] != target:
            continue
        if metadata.get("event_id") and metadata["event_id"] != event["id"]:
            continue
        if metadata.get("hook_id") and metadata["hook_id"] != hook_id:
            continue
        return approval
    return None


def approval_gate_for_hook_action(conn: sqlite3.Connection, hook_id: str, action: dict, event: sqlite3.Row, payload: dict) -> dict:
    approval_action = action.get("requires_approval", "")
    if not approval_action:
        return {"allowed": True}
    source = resolve_employee_alias(action.get("from") or action.get("agent") or event["source_agent"])
    target = resolve_employee_alias(action.get("to") or action.get("target") or "")
    approval_id = render_hook_text(action.get("approval_id", ""), event, payload)
    if not approval_id:
        approved = find_matching_approved_approval(conn, approval_action, source, target, event, hook_id)
        if approved:
            return {"allowed": True, "approval": approved}
    gate = approved_gate(conn, approval_id, approval_action, source, target)
    if gate["allowed"]:
        return gate
    pending_id = action.get("pending_approval_id") or f"approval-hook-{slug(event['id'])}-{slug(hook_id)}-{slug(approval_action)}"
    reason = action.get("approval_reason") or f"Hook {hook_id} requires approval for {approval_action} before processing event {event['id']}"
    result = create_approval_internal(
        conn,
        source=source,
        action=approval_action,
        reason=render_hook_text(reason, event, payload),
        target=target,
        risk=action.get("risk", "P1"),
        evidence=payload.get("evidence", ""),
        approval_id=render_hook_text(pending_id, event, payload),
        metadata={"hook_id": hook_id, "event_id": event["id"], "task_id": event["task_id"]},
    )
    return {"allowed": False, "reason": gate.get("reason", "approval required"), "approval_request": result["approval"], "file": result["file"]}


def run_hook_action(conn: sqlite3.Connection, action: dict, event: sqlite3.Row, payload: dict) -> dict:
    action_type = action.get("type")
    if action_type == "message":
        source = resolve_employee_alias(action["from"])
        target = resolve_employee_alias(action["to"])
        body = render_hook_text(action["body"], event, payload)
        return {"type": "message", **send_message_internal(conn, source=source, target=target, body=body)}
    if action_type == "task_submit":
        source = resolve_employee_alias(action["from"])
        target = resolve_employee_alias(action["to"])
        title = render_hook_text(action["title"], event, payload)
        description = render_hook_text(action.get("description", ""), event, payload)
        task_id = render_hook_text(action.get("task_id", ""), event, payload) or ""
        return {"type": "task_submit", **submit_task_internal(conn, source=source, target=target, title=title, description=description, priority=action.get("priority", "P2"), task_id=task_id, metadata={"hook_event_id": event["id"]})}
    if action_type == "conversation_reply":
        source = resolve_employee_alias(action["from"])
        conversation_id = render_hook_text(action["conversation_id"], event, payload)
        body = render_hook_text(action["body"], event, payload)
        evidence = render_hook_text(action.get("evidence", ""), event, payload)
        message_id = render_hook_text(action.get("message_id", ""), event, payload)
        return {"type": "conversation_reply", **conversation_reply_internal(conn, source=source, conversation_id=conversation_id, body=body, evidence=evidence, message_id=message_id)}
    if action_type == "heartbeat":
        agent = resolve_employee_alias(action["agent"])
        return {"type": "heartbeat", "heartbeat": heartbeat_internal(conn, agent, {"source": "hook", "event_id": event["id"]})}
    raise SystemExit(f"unknown hook action type: {action_type}")


def cmd_scheduler_run(args: argparse.Namespace) -> int:
    conn = connect()
    hooks = load_hooks_config().get("hooks", [])
    pending = conn.execute(
        "SELECT * FROM company_events WHERE processed_at = '' ORDER BY created_at ASC LIMIT ?",
        (args.limit,),
    ).fetchall()
    processed = []
    for event in pending:
        payload = event_payload(event)
        matched_hooks = [hook for hook in hooks if hook_matches(conn, hook, event)]
        actions = []
        blocked = []
        if not args.dry_run:
            for hook in matched_hooks:
                hook_id = hook.get("id", "")
                for action_index, action in enumerate(hook.get("actions", []), start=1):
                    prior = conn.execute(
                        "SELECT * FROM hook_action_runs WHERE event_id = ? AND hook_id = ? AND action_index = ? AND status = 'completed'",
                        (event["id"], hook_id, action_index),
                    ).fetchone()
                    if prior:
                        actions.append({"hook": hook_id, "action_index": action_index, "skipped": "already_completed"})
                        continue
                    gate = approval_gate_for_hook_action(conn, hook_id, action, event, payload)
                    if not gate.get("allowed"):
                        blocked.append({"hook": hook_id, "action_index": action_index, "action": action.get("type", ""), "gate": gate})
                        continue
                    result = run_hook_action(conn, action, event, payload)
                    run_id = f"har-{slug(event['id'])}-{slug(hook_id)}-{action_index}"
                    conn.execute(
                        """
                        INSERT INTO hook_action_runs(id, event_id, hook_id, action_index, status, result_json, created_at)
                        VALUES (?, ?, ?, ?, 'completed', ?, ?)
                        ON CONFLICT(event_id, hook_id, action_index) DO UPDATE SET
                          status = excluded.status,
                          result_json = excluded.result_json
                        """,
                        (run_id, event["id"], hook_id, action_index, json.dumps(result, ensure_ascii=False), now()),
                    )
                    conn.commit()
                    actions.append({"hook": hook_id, "action_index": action_index, "result": result})
            if not blocked:
                conn.execute("UPDATE company_events SET processed_at = ? WHERE id = ?", (now(), event["id"]))
                conn.commit()
        processed.append({"event_id": event["id"], "event_type": event["event_type"], "task_id": event["task_id"], "matched_hooks": [h.get("id", "") for h in matched_hooks], "blocked": blocked, "actions": actions})
    audit(conn, "companyctl", "scheduler.run", "", {"dry_run": args.dry_run, "events": len(processed)})
    emit({"ok": True, "dry_run": args.dry_run, "events": processed})
    return 0


def cmd_scheduler_events(args: argparse.Namespace) -> int:
    conn = connect()
    where = "WHERE processed_at = ''" if args.pending else ""
    emit({"ok": True, "events": rows(conn, f"SELECT * FROM company_events {where} ORDER BY created_at DESC LIMIT ?", (args.limit,))})
    return 0


def cmd_scheduler_skip_event(args: argparse.Namespace) -> int:
    conn = connect()
    actor = resolve_employee_alias(args.by)
    require_employee(conn, actor)
    event = conn.execute("SELECT * FROM company_events WHERE id = ?", (args.event_id,)).fetchone()
    if not event:
        emit({"ok": False, "error": "event not found", "event_id": args.event_id})
        return 2
    if event["processed_at"]:
        emit({"ok": True, "skipped": False, "reason": "event already processed", "event": dict(event)})
        return 0
    ts = now()
    conn.execute("UPDATE company_events SET processed_at = ? WHERE id = ?", (ts, args.event_id))
    conn.commit()
    updated = conn.execute("SELECT * FROM company_events WHERE id = ?", (args.event_id,)).fetchone()
    audit(conn, actor, "scheduler.skip_event", args.event_id, {"reason": args.reason, "event_type": event["event_type"], "source_agent": event["source_agent"]})
    emit({"ok": True, "skipped": True, "event": dict(updated), "reason": args.reason})
    return 0


def approval_detail(raw: str) -> dict:
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {"reason": raw}
    except json.JSONDecodeError:
        return {"reason": raw}


def normalize_approval(row: sqlite3.Row | dict) -> dict:
    obj = dict(row)
    obj["detail"] = approval_detail(obj.pop("reason", ""))
    return obj


def write_approval_state(approval: dict) -> str:
    status = approval["status"]
    for existing_status in APPROVAL_STATUSES:
        old = APPROVAL_STATE_DIR / existing_status / f"{approval['id']}.json"
        if old.exists() and existing_status != status:
            old.unlink()
    target_dir = APPROVAL_STATE_DIR / status
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{approval['id']}.json"
    path.write_text(json.dumps(approval, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


def rfc_state_path(status: str, rfc_id: str) -> Path:
    return STATE_DIR / "rfcs" / status / f"{rfc_id}.json"


def normalize_rfc(row: sqlite3.Row | dict) -> dict:
    obj = dict(row)
    try:
        obj["target_paths"] = json.loads(obj.pop("target_paths_json", "[]") or "[]")
    except json.JSONDecodeError:
        obj["target_paths"] = []
    return obj


def write_rfc_state(rfc: dict) -> str:
    status = rfc["status"]
    for existing in ("pending", "approved", "denied"):
        old = rfc_state_path(existing, rfc["id"])
        if old.exists() and existing != status:
            old.unlink()
    path = rfc_state_path(status, rfc["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rfc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


def rfc_file_path(rfc_id: str) -> Path:
    safe = slug(rfc_id)
    return RFC_DIR / f"{safe}.md"


def rfc_by_ref(conn: sqlite3.Connection, ref: str) -> dict | None:
    rel = normalize_repo_path(ref)
    row = conn.execute("SELECT * FROM rfcs WHERE id = ? OR file_path = ?", (ref, rel)).fetchone()
    return normalize_rfc(row) if row else None


def approved_rfc_covers(conn: sqlite3.Connection, rfc_ref: str, blocked_paths: list[dict]) -> dict:
    rfc = rfc_by_ref(conn, rfc_ref)
    if not rfc:
        return {"allowed": False, "reason": "rfc not found", "rfc": rfc_ref}
    if rfc["status"] != "approved":
        return {"allowed": False, "reason": f"rfc is {rfc['status']}", "rfc": rfc}
    targets = [normalize_repo_path(path) for path in rfc.get("target_paths", [])]
    missing = []
    for blocked in blocked_paths:
        path = blocked["path"]
        if not any(fnmatch.fnmatch(path, target) or fnmatch.fnmatch(path, target.rstrip("/") + "/**") for target in targets):
            missing.append(path)
    if missing:
        return {"allowed": False, "reason": "rfc does not cover protected paths", "missing": missing, "rfc": rfc}
    return {"allowed": True, "rfc": rfc}


def cmd_rfc_create(args: argparse.Namespace) -> int:
    conn = connect()
    author = resolve_employee_alias(args.by)
    require_employee(conn, author)
    targets = [normalize_repo_path(path) for path in parse_csv(args.paths)]
    rfc_id = args.rfc_id or f"rfc-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    path = Path(args.file) if args.file else rfc_file_path(rfc_id)
    if not path.is_absolute():
        path = ROOT / path
    rel_file = normalize_repo_path(str(path))
    ts = now()
    body = "\n".join(
        [
            f"# RFC: {args.title}",
            "",
            f"- id: `{rfc_id}`",
            f"- author: `{author}`",
            "- status: `pending`",
            f"- target_paths: `{', '.join(targets)}`",
            "",
            "## Problem",
            "",
            args.reason,
            "",
            "## Proposed Change",
            "",
            args.proposal or "Pending detailed proposal.",
            "",
            "## Rollback",
            "",
            args.rollback or "Restore previous protected files and rerun companyctl doctor.",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or args.overwrite:
        path.write_text(body, encoding="utf-8")
    conn.execute(
        """
        INSERT INTO rfcs(id, title, author_agent, status, target_paths_json, reason, file_path, created_at, updated_at)
        VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          title = excluded.title,
          target_paths_json = excluded.target_paths_json,
          reason = excluded.reason,
          file_path = excluded.file_path,
          updated_at = excluded.updated_at
        """,
        (rfc_id, args.title, author, json.dumps(targets, ensure_ascii=False), args.reason, rel_file, ts, ts),
    )
    conn.commit()
    rfc = normalize_rfc(conn.execute("SELECT * FROM rfcs WHERE id = ?", (rfc_id,)).fetchone())
    state_file = write_rfc_state(rfc)
    audit(conn, author, "rfc.create", rfc_id, rfc)
    emit({"ok": True, "rfc": rfc, "file": str(path), "state_file": state_file})
    return 0


def cmd_rfc_list(args: argparse.Namespace) -> int:
    conn = connect()
    where = "" if args.status == "all" else "WHERE status = ?"
    params = () if args.status == "all" else (args.status,)
    rfcs = [normalize_rfc(row) for row in conn.execute(f"SELECT * FROM rfcs {where} ORDER BY updated_at DESC", params).fetchall()]
    emit({"ok": True, "rfcs": rfcs})
    return 0


def cmd_rfc_show(args: argparse.Namespace) -> int:
    conn = connect()
    rfc = rfc_by_ref(conn, args.rfc)
    if not rfc:
        emit({"ok": False, "error": "rfc not found", "rfc": args.rfc})
        return 1
    emit({"ok": True, "rfc": rfc})
    return 0


def decide_rfc(args: argparse.Namespace, status: str) -> int:
    conn = connect()
    actor = resolve_employee_alias(args.by)
    require_employee(conn, actor)
    current = rfc_by_ref(conn, args.rfc)
    if not current:
        emit({"ok": False, "error": "rfc not found", "rfc": args.rfc})
        return 1
    ts = now()
    conn.execute(
        "UPDATE rfcs SET status = ?, decision_by = ?, decision_reason = ?, updated_at = ? WHERE id = ?",
        (status, actor, args.reason, ts, current["id"]),
    )
    conn.commit()
    rfc = normalize_rfc(conn.execute("SELECT * FROM rfcs WHERE id = ?", (current["id"],)).fetchone())
    state_file = write_rfc_state(rfc)
    audit(conn, actor, f"rfc.{status}", current["id"], rfc)
    emit({"ok": True, "rfc": rfc, "state_file": state_file})
    return 0


def cmd_rfc_approve(args: argparse.Namespace) -> int:
    return decide_rfc(args, "approved")


def cmd_rfc_deny(args: argparse.Namespace) -> int:
    return decide_rfc(args, "denied")


def cmd_approval_request(args: argparse.Namespace) -> int:
    conn = connect()
    metadata = {"task_id": args.task_id} if args.task_id else None
    result = create_approval_internal(
        conn,
        source=args.source,
        action=args.action,
        reason=args.reason,
        target=args.target,
        risk=args.risk,
        evidence=args.evidence,
        approval_id=args.approval_id,
        metadata=metadata,
    )
    emit({"ok": True, **result})
    return 0


def create_approval_internal(
    conn: sqlite3.Connection,
    *,
    source: str,
    action: str,
    reason: str,
    target: str = "",
    risk: str = "",
    evidence: str = "",
    approval_id: str = "",
    metadata: dict | None = None,
) -> dict:
    source = resolve_employee_alias(source)
    require_employee(conn, source)
    aid = approval_id or f"approval-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    ts = now()
    detail = {
        "request_reason": reason,
        "target": resolve_employee_alias(target) if target else "",
        "risk": risk,
        "evidence": evidence,
        "requested_by": source,
        "metadata": metadata or {},
    }
    conn.execute(
        """
        INSERT INTO approvals(id, source_agent, action, status, reason, created_at, updated_at)
        VALUES (?, ?, ?, 'pending', ?, ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (aid, source, action, json.dumps(detail, ensure_ascii=False), ts, ts),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM approvals WHERE id = ?", (aid,)).fetchone()
    approval = normalize_approval(row)
    path = write_approval_state(approval)
    audit(conn, source, "approval.request", aid, approval)
    return {"approval": approval, "file": path}


def cmd_approval_list(args: argparse.Namespace) -> int:
    conn = connect()
    clauses = []
    params: list[str | int] = []
    if args.status != "all":
        clauses.append("status = ?")
        params.append(args.status)
    if args.agent:
        clauses.append("source_agent = ?")
        params.append(resolve_employee_alias(args.agent))
    if args.action:
        clauses.append("action = ?")
        params.append(args.action)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(args.limit)
    approvals = [normalize_approval(r) for r in conn.execute(f"SELECT * FROM approvals {where} ORDER BY updated_at DESC LIMIT ?", tuple(params)).fetchall()]
    emit({"ok": True, "approvals": approvals})
    return 0


def cmd_approval_show(args: argparse.Namespace) -> int:
    conn = connect()
    row = conn.execute("SELECT * FROM approvals WHERE id = ?", (args.approval_id,)).fetchone()
    if not row:
        emit({"ok": False, "error": "approval not found", "approval_id": args.approval_id})
        return 1
    approval = normalize_approval(row)
    emit({"ok": True, "approval": approval})
    return 0


def decide_approval(args: argparse.Namespace, status: str) -> int:
    conn = connect()
    actor = resolve_employee_alias(args.by)
    require_employee(conn, actor)
    row = conn.execute("SELECT * FROM approvals WHERE id = ?", (args.approval_id,)).fetchone()
    if not row:
        emit({"ok": False, "error": "approval not found", "approval_id": args.approval_id})
        return 1
    current = normalize_approval(row)
    detail = current["detail"]
    detail.update(
        {
            "decided_by": actor,
            "decision": status,
            "decision_reason": args.reason,
            "decided_at": now(),
        }
    )
    conn.execute(
        "UPDATE approvals SET status = ?, reason = ?, updated_at = ? WHERE id = ?",
        (status, json.dumps(detail, ensure_ascii=False), now(), args.approval_id),
    )
    conn.commit()
    approval = normalize_approval(conn.execute("SELECT * FROM approvals WHERE id = ?", (args.approval_id,)).fetchone())
    path = write_approval_state(approval)
    audit(conn, actor, f"approval.{status}", args.approval_id, approval)
    emit({"ok": True, "approval": approval, "file": path})
    return 0


def cmd_approval_approve(args: argparse.Namespace) -> int:
    return decide_approval(args, "approved")


def cmd_approval_deny(args: argparse.Namespace) -> int:
    return decide_approval(args, "denied")


def cmd_task_list(args: argparse.Namespace) -> int:
    conn = connect()
    where = ""
    params: tuple = ()
    if args.agent:
        agent = resolve_employee_alias(args.agent)
        where = "WHERE target_agent = ? OR source_agent = ? OR claimed_by = ?"
        params = (agent, agent, agent)
    emit({"ok": True, "tasks": rows(conn, f"SELECT * FROM tasks {where} ORDER BY created_at DESC", params)})
    return 0


def task_approvals(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    matched = []
    for row in conn.execute("SELECT * FROM approvals ORDER BY updated_at DESC").fetchall():
        approval = normalize_approval(row)
        detail = approval.get("detail", {})
        metadata = detail.get("metadata", {}) if isinstance(detail.get("metadata", {}), dict) else {}
        if metadata.get("task_id") == task_id or task_id in json.dumps(detail, ensure_ascii=False):
            matched.append(approval)
    return matched


def cmd_task_show(args: argparse.Namespace) -> int:
    conn = connect()
    task_id = args.task_id
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        emit({"ok": False, "error": "task not found", "task_id": task_id})
        return 1
    metadata = task_metadata(conn, task_id)
    children = rows(
        conn,
        """
        SELECT tr.parent_task_id, tr.child_task_id, tr.relation_type, tr.created_by, tr.created_at,
               t.status, t.target_agent, t.claimed_by, t.summary, t.evidence_path, t.blocker, t.updated_at
        FROM task_relations tr
        JOIN tasks t ON t.id = tr.child_task_id
        WHERE tr.parent_task_id = ?
        ORDER BY tr.created_at ASC
        """,
        (task_id,),
    )
    parents = rows(
        conn,
        """
        SELECT tr.parent_task_id, tr.child_task_id, tr.relation_type, tr.created_by, tr.created_at,
               t.status, t.target_agent, t.claimed_by, t.summary, t.evidence_path, t.blocker, t.updated_at
        FROM task_relations tr
        JOIN tasks t ON t.id = tr.parent_task_id
        WHERE tr.child_task_id = ?
        ORDER BY tr.created_at ASC
        """,
        (task_id,),
    )
    events = rows(conn, "SELECT * FROM company_events WHERE task_id = ? ORDER BY created_at ASC", (task_id,))
    event_ids = [event["id"] for event in events]
    hook_runs: list[dict] = []
    if event_ids:
        placeholders = ",".join("?" for _ in event_ids)
        hook_runs = rows(conn, f"SELECT * FROM hook_action_runs WHERE event_id IN ({placeholders}) ORDER BY created_at ASC", tuple(event_ids))
        for run in hook_runs:
            try:
                run["result"] = json.loads(run.pop("result_json", "{}") or "{}")
            except json.JSONDecodeError:
                run["result"] = {}
    lock = conn.execute("SELECT * FROM locks WHERE resource_key = ?", (f"task:{task_id}",)).fetchone()
    task_obj = dict(task)
    evidence_path = task_obj.get("evidence_path", "")
    evidence = {
        "path": evidence_path,
        "exists": bool(evidence_path and Path(evidence_path).exists()),
    }
    audit_rows = rows(conn, "SELECT * FROM audit_logs WHERE target = ? ORDER BY created_at ASC", (task_id,))
    emit(
        {
            "ok": True,
            "task": task_obj,
            "metadata": metadata,
            "evidence": evidence,
            "blocker": task_obj.get("blocker", ""),
            "parents": parents,
            "children": children,
            "events": events,
            "hook_runs": hook_runs,
            "approvals": task_approvals(conn, task_id),
            "lock": dict(lock) if lock else {},
            "audit_logs": audit_rows,
        }
    )
    return 0


def cmd_task_children(args: argparse.Namespace) -> int:
    conn = connect()
    result = task_with_children(conn, args.task_id)
    emit({"ok": True, **result})
    return 0


def parse_split_item(raw: str) -> dict:
    parts = raw.split("|", 3)
    if len(parts) < 2:
        raise SystemExit("split item must be target|title or target|title|description|priority")
    return {
        "target": parts[0].strip(),
        "title": parts[1].strip(),
        "description": parts[2].strip() if len(parts) >= 3 else "",
        "priority": parts[3].strip() if len(parts) >= 4 and parts[3].strip() else "P2",
    }


def split_items_from_plan(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_items = payload.get("items", payload if isinstance(payload, list) else [])
    if not isinstance(raw_items, list):
        raise SystemExit("split plan must be a JSON list or an object with items")
    items = []
    for index, item in enumerate(raw_items, start=1):
        if not isinstance(item, dict):
            raise SystemExit(f"split plan item {index} must be an object")
        target = str(item.get("target") or item.get("to") or "").strip()
        title = str(item.get("title") or "").strip()
        if not target or not title:
            raise SystemExit(f"split plan item {index} requires target and title")
        items.append(
            {
                "target": target,
                "title": title,
                "description": str(item.get("description") or "").strip(),
                "priority": str(item.get("priority") or "P2").strip() or "P2",
            }
        )
    return items


def cmd_task_split(args: argparse.Namespace) -> int:
    conn = connect()
    splitter = resolve_employee_alias(args.by)
    require_employee(conn, splitter)
    parent = conn.execute("SELECT * FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
    if not parent:
        emit({"ok": False, "error": "parent task not found", "task_id": args.task_id})
        return 1
    if splitter not in {parent["source_agent"], parent["target_agent"], parent["claimed_by"]}:
        emit({"ok": False, "error": "splitter is not related to parent task", "task_id": args.task_id, "by": splitter})
        return 1
    split_items = [parse_split_item(raw_item) for raw_item in args.item]
    if args.plan:
        split_items.extend(split_items_from_plan(Path(args.plan)))
    if not split_items:
        emit({"ok": False, "error": "no split items", "task_id": args.task_id})
        return 1
    created = []
    for index, item in enumerate(split_items, start=1):
        target = resolve_employee_alias(item["target"])
        child_id = args.child_id_prefix + f"-{index:02d}" if args.child_id_prefix else f"{args.task_id}-sub-{index:02d}"
        result = submit_task_internal(
            conn,
            source=splitter,
            target=target,
            title=item["title"],
            description=item["description"],
            priority=item["priority"],
            task_id=child_id,
            metadata={"parent_task_id": args.task_id, "split_by": splitter, "split_index": index},
        )
        conn.execute(
            "INSERT OR IGNORE INTO task_relations(parent_task_id, child_task_id, relation_type, created_by, created_at) VALUES (?, ?, 'subtask', ?, ?)",
            (args.task_id, result["task"]["id"], splitter, now()),
        )
        conn.commit()
        created.append(result)
    child_ids = [item["task"]["id"] for item in created]
    event = record_event(conn, "task.split", splitter, task_id=args.task_id, payload={"children": child_ids, "plan": args.plan})
    conn.execute("UPDATE company_events SET processed_at = ? WHERE id = ?", (now(), event["id"]))
    conn.commit()
    audit(conn, splitter, "task.split", args.task_id, {"children": child_ids, "plan": args.plan, "event_id": event["id"]})
    emit({"ok": True, "parent_task_id": args.task_id, "children": created, "event_id": event["id"]})
    return 0


def cmd_task_collect(args: argparse.Namespace) -> int:
    conn = connect()
    collector = resolve_employee_alias(args.agent)
    require_employee(conn, collector)
    result = task_with_children(conn, args.task_id)
    parent = result["task"]
    children = result["children"]
    if collector not in {parent["source_agent"], parent["target_agent"], parent["claimed_by"]}:
        emit({"ok": False, "error": "collector is not related to parent task", "task_id": args.task_id, "agent": collector})
        return 1
    if not children:
        emit({"ok": False, "error": "parent task has no children", "task_id": args.task_id})
        return 1
    incomplete = [task for task in children if task["status"] != "completed"]
    missing_evidence = [task for task in children if task["status"] == "completed" and not task.get("evidence_path")]
    missing_files = [task for task in children if task.get("evidence_path") and not Path(task["evidence_path"]).exists()]
    if (incomplete or missing_evidence or missing_files) and not args.force:
        emit(
            {
                "ok": False,
                "error": "children are not ready to collect",
                "incomplete": incomplete,
                "missing_evidence": missing_evidence,
                "missing_files": missing_files,
            }
        )
        return 1
    summary = args.summary or f"Collected {len(children)} child tasks."
    evidence = str(Path(args.evidence)) if args.evidence else str(write_task_collection_report(parent, children, collector, summary))
    completed = complete_task_internal(conn, agent=collector, task_id=args.task_id, summary=summary, evidence=evidence)
    audit(conn, collector, "task.collect", args.task_id, {"children": [task["id"] for task in children], "evidence": evidence})
    emit({"ok": True, "parent_task_id": args.task_id, "children": children, "collection": completed})
    return 0


def cmd_task_claim(args: argparse.Namespace) -> int:
    conn = connect()
    agent = resolve_employee_alias(args.agent)
    require_employee(conn, agent)
    if args.task_id:
        task = conn.execute("SELECT * FROM tasks WHERE id = ? AND target_agent = ?", (args.task_id, agent)).fetchone()
    else:
        task = conn.execute(
            "SELECT * FROM tasks WHERE target_agent = ? AND status = 'submitted' ORDER BY created_at LIMIT 1",
            (agent,),
        ).fetchone()
    if not task:
        emit({"ok": False, "error": "no claimable task", "agent": agent})
        return 1
    guard = guard_task_claim(conn, task, agent)
    if not guard["allowed"]:
        audit(conn, agent, "task.claim.blocked_by_guard", task["id"], guard)
        emit({"ok": False, "error": "guard blocked task claim", "agent": agent, "task_id": task["id"], "guard": guard})
        return 2
    ts = now()
    lease_until = future_seconds(args.lease_seconds)
    conn.execute(
        "UPDATE tasks SET status = 'claimed', claimed_by = ?, updated_at = ? WHERE id = ?",
        (agent, ts, task["id"]),
    )
    conn.execute(
        """
        INSERT INTO locks(resource_key, owner_agent, lease_until, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(resource_key) DO UPDATE SET
          owner_agent = excluded.owner_agent,
          lease_until = excluded.lease_until,
          updated_at = excluded.updated_at
        """,
        (f"task:{task['id']}", agent, lease_until, ts, ts),
    )
    conn.commit()
    audit(conn, agent, "task.claim", task["id"], {"lease_until": lease_until})
    emit({"ok": True, "task": dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task["id"],)).fetchone())})
    return 0


def cmd_task_done(args: argparse.Namespace) -> int:
    conn = connect()
    agent = resolve_employee_alias(args.agent)
    try:
        result = complete_task_internal(conn, agent=agent, task_id=args.task_id, summary=args.summary, evidence=args.evidence)
    except ValueError as exc:
        emit({"ok": False, "error": str(exc), "task_id": args.task_id})
        return 2
    except SystemExit:
        emit({"ok": False, "error": "task not found or not owned by agent", "task_id": args.task_id})
        return 1
    emit({"ok": True, **result})
    return 0


def cmd_task_block(args: argparse.Namespace) -> int:
    conn = connect()
    agent = resolve_employee_alias(args.agent)
    conn.execute(
        "UPDATE tasks SET status = 'blocked', blocker = ?, updated_at = ? WHERE id = ? AND (target_agent = ? OR claimed_by = ?)",
        (args.blocker, now(), args.task_id, agent, agent),
    )
    if conn.total_changes == 0:
        emit({"ok": False, "error": "task not found or not owned by agent", "task_id": args.task_id})
        return 1
    conn.execute("DELETE FROM locks WHERE resource_key = ?", (f"task:{args.task_id}",))
    synced_plan_items = sync_project_plan_for_task(conn, task_id=args.task_id, task_status="blocked", actor=agent)
    conn.commit()
    audit(conn, agent, "task.block", args.task_id, {"blocker": args.blocker})
    emit({"ok": True, "task_id": args.task_id, "status": "blocked", "blocker": args.blocker, "synced_plan_items": synced_plan_items})
    return 0


def cmd_task_reopen(args: argparse.Namespace) -> int:
    conn = connect()
    actor = resolve_employee_alias(args.by)
    require_employee(conn, actor)
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
    if not task:
        emit({"ok": False, "error": "task not found", "task_id": args.task_id})
        return 2
    if actor not in {task["source_agent"], task["target_agent"], task["claimed_by"]}:
        emit({"ok": False, "error": "actor cannot reopen task", "task_id": args.task_id, "by": actor})
        return 2
    claimed_by = "" if args.status == "submitted" else task["claimed_by"]
    conn.execute(
        "UPDATE tasks SET status = ?, claimed_by = ?, blocker = '', updated_at = ? WHERE id = ?",
        (args.status, claimed_by, now(), args.task_id),
    )
    conn.execute("DELETE FROM locks WHERE resource_key = ?", (f"task:{args.task_id}",))
    synced_plan_items = sync_project_plan_for_task(conn, task_id=args.task_id, task_status=args.status, actor=actor)
    conn.commit()
    updated = dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (args.task_id,)).fetchone())
    task_file = write_task_inbox_file(updated)
    audit(conn, actor, "task.reopen", args.task_id, {"reason": args.reason, "status": args.status, "file": task_file})
    emit({"ok": True, "task": updated, "file": task_file, "synced_plan_items": synced_plan_items})
    return 0


def cmd_task_reassign(args: argparse.Namespace) -> int:
    conn = connect()
    actor = resolve_employee_alias(args.by)
    target = resolve_employee_alias(args.to)
    require_employee(conn, actor)
    require_employee(conn, target)
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
    if not task:
        emit({"ok": False, "error": "task not found", "task_id": args.task_id})
        return 2
    if actor not in {task["source_agent"], task["target_agent"], task["claimed_by"]}:
        emit({"ok": False, "error": "actor cannot reassign task", "task_id": args.task_id, "by": actor})
        return 2
    policy = require_communication_allowed(actor, target, "task.submit")
    conn.execute(
        "UPDATE tasks SET target_agent = ?, status = 'submitted', claimed_by = '', blocker = '', updated_at = ? WHERE id = ?",
        (target, now(), args.task_id),
    )
    conn.execute("DELETE FROM locks WHERE resource_key = ?", (f"task:{args.task_id}",))
    synced_plan_items = sync_project_plan_owner_for_task(conn, task_id=args.task_id, owner=target, actor=actor)
    conn.commit()
    updated = dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (args.task_id,)).fetchone())
    task_file = write_task_inbox_file({**updated, "metadata": task_metadata(conn, args.task_id), "communication_policy": policy})
    audit(conn, actor, "task.reassign", args.task_id, {"to": target, "reason": args.reason, "file": task_file})
    emit({"ok": True, "task": updated, "file": task_file, "synced_plan_items": synced_plan_items})
    return 0


def parse_acceptance(raw: str) -> list[str]:
    items = []
    for item in raw.split(";"):
        item = item.strip()
        if item:
            items.append(item)
    return items


def cmd_project_create(args: argparse.Namespace) -> int:
    conn = connect()
    owner = resolve_employee_alias(args.owner)
    require_employee(conn, owner)
    project_id = args.project_id or f"project-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    ts = now()
    project = {
        "id": project_id,
        "title": args.title,
        "goal": args.goal,
        "owner_agent": owner,
        "status": args.status,
        "acceptance": parse_acceptance(args.acceptance),
        "created_at": ts,
        "updated_at": ts,
    }
    conn.execute(
        """
        INSERT INTO projects(id, title, goal, owner_agent, status, acceptance_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (project_id, args.title, args.goal, owner, args.status, json.dumps(project["acceptance"], ensure_ascii=False), ts, ts),
    )
    conn.commit()
    audit(conn, owner, "project.create", project_id, project)
    emit({"ok": True, "project": project})
    return 0


def normalize_project(row: sqlite3.Row | dict) -> dict:
    obj = dict(row)
    try:
        obj["acceptance"] = json.loads(obj.pop("acceptance_json", "[]") or "[]")
    except json.JSONDecodeError:
        obj["acceptance"] = []
    return obj


def cmd_project_list(args: argparse.Namespace) -> int:
    conn = connect()
    where = ""
    params: tuple = ()
    if args.status != "all":
        where = "WHERE status = ?"
        params = (args.status,)
    projects = [normalize_project(row) for row in conn.execute(f"SELECT * FROM projects {where} ORDER BY updated_at DESC", params).fetchall()]
    emit({"ok": True, "projects": projects})
    return 0


def cmd_project_show(args: argparse.Namespace) -> int:
    conn = connect()
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (args.project_id,)).fetchone()
    if not row:
        emit({"ok": False, "error": "project not found", "project_id": args.project_id})
        return 1
    project = normalize_project(row)
    tasks = rows(
        conn,
        """
        SELECT t.*
        FROM project_tasks pt
        JOIN tasks t ON t.id = pt.task_id
        WHERE pt.project_id = ?
        ORDER BY t.updated_at DESC
        """,
        (args.project_id,),
    )
    emit({"ok": True, "project": project, "tasks": tasks, "plan_items": project_plan_items(conn, args.project_id)})
    return 0


def cmd_project_link_task(args: argparse.Namespace) -> int:
    conn = connect()
    if not conn.execute("SELECT 1 FROM projects WHERE id = ?", (args.project_id,)).fetchone():
        emit({"ok": False, "error": "project not found", "project_id": args.project_id})
        return 1
    if not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (args.task_id,)).fetchone():
        emit({"ok": False, "error": "task not found", "task_id": args.task_id})
        return 1
    conn.execute(
        "INSERT OR IGNORE INTO project_tasks(project_id, task_id, created_at) VALUES (?, ?, ?)",
        (args.project_id, args.task_id, now()),
    )
    conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now(), args.project_id))
    conn.commit()
    audit(conn, "companyctl", "project.link_task", args.project_id, {"task_id": args.task_id})
    emit({"ok": True, "project_id": args.project_id, "task_id": args.task_id})
    return 0


def cmd_project_plan_add(args: argparse.Namespace) -> int:
    conn = connect()
    if not project_exists(conn, args.project_id):
        emit({"ok": False, "error": "project not found", "project_id": args.project_id})
        return 1
    if args.task_id and not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (args.task_id,)).fetchone():
        emit({"ok": False, "error": "task not found", "task_id": args.task_id})
        return 1
    owner = resolve_employee_alias(args.owner) if args.owner else ""
    if owner:
        require_employee(conn, owner)
    plan_id = args.plan_id or f"plan-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    ts = now()
    item = {
        "id": plan_id,
        "project_id": args.project_id,
        "title": args.title,
        "task_id": args.task_id,
        "status": args.status,
        "owner_agent": owner,
        "due_at": args.due_at,
        "created_at": ts,
        "updated_at": ts,
    }
    conn.execute(
        """
        INSERT INTO project_plan_items(id, project_id, title, task_id, status, owner_agent, due_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (plan_id, args.project_id, args.title, args.task_id, args.status, owner, args.due_at, ts, ts),
    )
    if args.task_id:
        conn.execute(
            "INSERT OR IGNORE INTO project_tasks(project_id, task_id, created_at) VALUES (?, ?, ?)",
            (args.project_id, args.task_id, ts),
        )
    conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (ts, args.project_id))
    conn.commit()
    audit(conn, owner or "companyctl", "project.plan_add", args.project_id, item)
    emit({"ok": True, "plan_item": item})
    return 0


def cmd_project_plan_list(args: argparse.Namespace) -> int:
    conn = connect()
    if not project_exists(conn, args.project_id):
        emit({"ok": False, "error": "project not found", "project_id": args.project_id})
        return 1
    emit({"ok": True, "project_id": args.project_id, "plan_items": project_plan_items(conn, args.project_id)})
    return 0


def cmd_project_plan_status(args: argparse.Namespace) -> int:
    conn = connect()
    if not project_exists(conn, args.project_id):
        emit({"ok": False, "error": "project not found", "project_id": args.project_id})
        return 1
    ts = now()
    cur = conn.execute(
        """
        UPDATE project_plan_items
        SET status = ?, updated_at = ?
        WHERE project_id = ? AND id = ?
        """,
        (args.status, ts, args.project_id, args.plan_id),
    )
    if cur.rowcount == 0:
        emit({"ok": False, "error": "plan item not found", "project_id": args.project_id, "plan_id": args.plan_id})
        return 1
    conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (ts, args.project_id))
    conn.commit()
    item = conn.execute("SELECT * FROM project_plan_items WHERE project_id = ? AND id = ?", (args.project_id, args.plan_id)).fetchone()
    plan_item = dict(item) if item else {}
    audit(conn, "companyctl", "project.plan_status", args.project_id, {"plan_id": args.plan_id, "status": args.status})
    emit({"ok": True, "plan_item": plan_item})
    return 0


def cmd_project_status(args: argparse.Namespace) -> int:
    conn = connect()
    if args.status not in {"active", "paused", "completed", "blocked"}:
        raise SystemExit(f"unknown project status: {args.status}")
    cur = conn.execute("UPDATE projects SET status = ?, updated_at = ? WHERE id = ?", (args.status, now(), args.project_id))
    if cur.rowcount == 0:
        emit({"ok": False, "error": "project not found", "project_id": args.project_id})
        return 1
    conn.commit()
    audit(conn, "companyctl", "project.status", args.project_id, {"status": args.status})
    emit({"ok": True, "project_id": args.project_id, "status": args.status})
    return 0


def project_tasks(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    return rows(
        conn,
        """
        SELECT t.*
        FROM project_tasks pt
        JOIN tasks t ON t.id = pt.task_id
        WHERE pt.project_id = ?
        ORDER BY t.updated_at DESC
        """,
        (project_id,),
    )


def project_plan_items(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    return rows(
        conn,
        """
        SELECT ppi.*,
               COALESCE(t.status, '') AS task_status,
               COALESCE(t.evidence_path, '') AS task_evidence_path,
               COALESCE(t.blocker, '') AS task_blocker
        FROM project_plan_items ppi
        LEFT JOIN tasks t ON t.id = ppi.task_id
        WHERE ppi.project_id = ?
        ORDER BY ppi.created_at ASC
        """,
        (project_id,),
    )


def project_exists(conn: sqlite3.Connection, project_id: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone())


def project_review_internal(conn: sqlite3.Connection, project_row: sqlite3.Row | dict, project_id: str) -> dict:
    project = normalize_project(project_row)
    tasks = project_tasks(conn, project_id)
    plan_items = project_plan_items(conn, project_id)
    total = len(tasks)
    completed = [task for task in tasks if task["status"] == "completed"]
    blocked = [task for task in tasks if task["status"] == "blocked"]
    open_tasks = [task for task in tasks if task["status"] not in {"completed", "blocked"}]
    open_plan_items = [item for item in plan_items if item["status"] not in {"done", "completed", "cancelled"}]
    completed_without_evidence = [task for task in completed if not task.get("evidence_path")]
    evidence_missing_on_disk = [task for task in completed if task.get("evidence_path") and not Path(task["evidence_path"]).exists()]
    ready = total > 0 and not open_tasks and not blocked and not open_plan_items and not completed_without_evidence and not evidence_missing_on_disk
    review = {
        "project_id": project_id,
        "ready_to_complete": ready,
        "task_counts": {
            "total": total,
            "completed": len(completed),
            "blocked": len(blocked),
            "open": len(open_tasks),
            "completed_without_evidence": len(completed_without_evidence),
            "evidence_missing_on_disk": len(evidence_missing_on_disk),
        },
        "plan_counts": {
            "total": len(plan_items),
            "open": len(open_plan_items),
            "done": len(plan_items) - len(open_plan_items),
        },
        "acceptance_checklist": [{"item": item, "status": "manual_review_required"} for item in project["acceptance"]],
        "open_plan_items": open_plan_items,
        "blocked_tasks": blocked,
        "open_tasks": open_tasks,
        "completed_without_evidence": completed_without_evidence,
        "evidence_missing_on_disk": evidence_missing_on_disk,
    }
    return {"project": project, "tasks": tasks, "plan_items": plan_items, "review": review}


def cmd_project_review(args: argparse.Namespace) -> int:
    conn = connect()
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (args.project_id,)).fetchone()
    if not row:
        emit({"ok": False, "error": "project not found", "project_id": args.project_id})
        return 1
    result = project_review_internal(conn, row, args.project_id)
    review = result["review"]
    audit(conn, "companyctl", "project.review", args.project_id, review)
    emit({"ok": True, "project": result["project"], "review": review})
    return 0


def cmd_project_accept(args: argparse.Namespace) -> int:
    conn = connect()
    actor = resolve_employee_alias(args.by)
    require_employee(conn, actor)
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (args.project_id,)).fetchone()
    if not row:
        emit({"ok": False, "error": "project not found", "project_id": args.project_id})
        return 1
    if row["status"] == "completed" and not args.force:
        emit({"ok": False, "error": "project already completed", "project_id": args.project_id})
        return 1
    result = project_review_internal(conn, row, args.project_id)
    review = result["review"]
    if not review["ready_to_complete"] and not args.force:
        emit({"ok": False, "error": "project is not ready to complete", "review": review})
        return 1
    acceptance_id = f"pacc-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    ts = now()
    acceptance = {
        "id": acceptance_id,
        "project_id": args.project_id,
        "accepted_by": actor,
        "summary": args.summary,
        "review": review,
        "created_at": ts,
        "force": args.force,
    }
    conn.execute(
        "INSERT INTO project_acceptances(id, project_id, accepted_by, summary, review_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (acceptance_id, args.project_id, actor, args.summary, json.dumps(review, ensure_ascii=False), ts),
    )
    conn.execute("UPDATE projects SET status = 'completed', updated_at = ? WHERE id = ?", (ts, args.project_id))
    conn.commit()
    path = STATE_DIR / "project-acceptances" / f"{acceptance_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(acceptance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit(conn, actor, "project.accept", args.project_id, acceptance)
    emit({"ok": True, "acceptance": acceptance, "file": str(path)})
    return 0


def cmd_lock_acquire(args: argparse.Namespace) -> int:
    conn = connect()
    owner = resolve_employee_alias(args.agent)
    require_employee(conn, owner)
    ts = now()
    lease_until = future_seconds(args.lease_seconds)
    existing = conn.execute("SELECT * FROM locks WHERE resource_key = ?", (args.resource,)).fetchone()
    if existing and parse_time(existing["lease_until"]) > datetime.now(timezone.utc).astimezone() and existing["owner_agent"] != owner:
        emit({"ok": False, "error": "lock held", "lock": dict(existing)})
        return 1
    conn.execute(
        """
        INSERT INTO locks(resource_key, owner_agent, lease_until, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(resource_key) DO UPDATE SET
          owner_agent = excluded.owner_agent,
          lease_until = excluded.lease_until,
          updated_at = excluded.updated_at
        """,
        (args.resource, owner, lease_until, ts, ts),
    )
    conn.commit()
    lock = dict(conn.execute("SELECT * FROM locks WHERE resource_key = ?", (args.resource,)).fetchone())
    audit(conn, owner, "lock.acquire", args.resource, lock)
    emit({"ok": True, "lock": lock})
    return 0


def cmd_lock_release(args: argparse.Namespace) -> int:
    conn = connect()
    owner = resolve_employee_alias(args.agent)
    lock = conn.execute("SELECT * FROM locks WHERE resource_key = ?", (args.resource,)).fetchone()
    if not lock:
        emit({"ok": True, "released": False, "resource": args.resource})
        return 0
    if lock["owner_agent"] != owner and not args.force:
        emit({"ok": False, "error": "lock owned by another agent", "lock": dict(lock)})
        return 1
    conn.execute("DELETE FROM locks WHERE resource_key = ?", (args.resource,))
    conn.commit()
    audit(conn, owner, "lock.release", args.resource, {"force": args.force})
    emit({"ok": True, "released": True, "resource": args.resource})
    return 0


def cmd_lock_list(args: argparse.Namespace) -> int:
    conn = connect()
    where = "WHERE owner_agent = ?" if args.agent else ""
    params = (resolve_employee_alias(args.agent),) if args.agent else ()
    emit({"ok": True, "locks": rows(conn, f"SELECT * FROM locks {where} ORDER BY updated_at DESC", params)})
    return 0


def unlock_stale(conn: sqlite3.Connection) -> list[dict]:
    current = datetime.now(timezone.utc).astimezone()
    stale = []
    for lock in conn.execute("SELECT * FROM locks").fetchall():
        if parse_time(lock["lease_until"]) <= current:
            stale.append(dict(lock))
            conn.execute("DELETE FROM locks WHERE id = ?", (lock["id"],))
    conn.commit()
    return stale


def cmd_lock_unlock_stale(_args: argparse.Namespace) -> int:
    conn = connect()
    stale = unlock_stale(conn)
    audit(conn, "companyctl", "lock.unlock_stale", "", {"count": len(stale), "locks": stale})
    emit({"ok": True, "unlocked": stale})
    return 0


def reset_stale_claims(conn: sqlite3.Connection) -> list[dict]:
    current = datetime.now(timezone.utc).astimezone()
    reset = []
    for task in conn.execute("SELECT * FROM tasks WHERE status = 'claimed'").fetchall():
        lock = conn.execute("SELECT * FROM locks WHERE resource_key = ?", (f"task:{task['id']}",)).fetchone()
        stale = not lock or parse_time(lock["lease_until"]) <= current
        if not stale:
            continue
        before = dict(task)
        conn.execute(
            "UPDATE tasks SET status = 'submitted', claimed_by = '', updated_at = ? WHERE id = ?",
            (now(), task["id"]),
        )
        conn.execute("DELETE FROM locks WHERE resource_key = ?", (f"task:{task['id']}",))
        reset.append(before)
    conn.commit()
    return reset


def cmd_repair_reset_stale_claims(_args: argparse.Namespace) -> int:
    conn = connect()
    unlocked = unlock_stale(conn)
    reset = reset_stale_claims(conn)
    audit(conn, "companyctl", "repair.reset_stale_claims", "", {"unlocked": unlocked, "reset": reset})
    emit({"ok": True, "unlocked_locks": unlocked, "reset_tasks": reset})
    return 0


def heartbeat_internal(conn: sqlite3.Connection, agent: str, metadata: dict | None = None) -> dict:
    emp = conn.execute("SELECT * FROM employees WHERE id = ?", (agent,)).fetchone()
    runtime = emp["runtime"] if emp else ""
    workspace = emp["workspace"] if emp else ""
    ts = now()
    conn.execute(
        """
        INSERT INTO heartbeats(agent_id, runtime, workspace, status, last_seen_at, metadata_json)
        VALUES (?, ?, ?, 'alive', ?, ?)
        ON CONFLICT(agent_id) DO UPDATE SET
          runtime = excluded.runtime,
          workspace = excluded.workspace,
          status = 'alive',
          last_seen_at = excluded.last_seen_at,
          metadata_json = excluded.metadata_json
        """,
        (agent, runtime, workspace, ts, json.dumps(metadata or {"source": "companyctl"}, ensure_ascii=False)),
    )
    conn.commit()
    hb = {"agent_id": agent, "runtime": runtime, "workspace": workspace, "status": "alive", "last_seen_at": ts}
    if emp:
        p = employee_paths(agent)["heartbeat"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(hb, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit(conn, agent, "heartbeat", agent, hb)
    return hb


def cmd_heartbeat(args: argparse.Namespace) -> int:
    conn = connect()
    hb = heartbeat_internal(conn, args.agent)
    emit({"ok": True, "heartbeat": hb})
    return 0


def check_command(cmd: str) -> dict:
    path = shutil.which(cmd)
    return {"command": cmd, "available": bool(path), "path": path or ""}


def cmd_runtime_register(args: argparse.Namespace) -> int:
    conn = connect()
    runtime = args.runtime.strip()
    if not runtime:
        raise SystemExit("runtime is required")
    ts = now()
    conn.execute(
        """
        INSERT INTO employee_runtimes(runtime, command, status, notes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(runtime) DO UPDATE SET
          command = excluded.command,
          status = excluded.status,
          notes = excluded.notes,
          updated_at = excluded.updated_at
        """,
        (runtime, args.command, args.status, args.notes or KNOWN_RUNTIMES.get(runtime, "Custom runtime adapter"), ts, ts),
    )
    conn.commit()
    row = dict(conn.execute("SELECT * FROM employee_runtimes WHERE runtime = ?", (runtime,)).fetchone())
    audit(conn, "companyctl", "runtime.register", runtime, row)
    emit({"ok": True, "runtime": row})
    return 0


def cmd_runtime_list(args: argparse.Namespace) -> int:
    try:
        conn = connect_readonly()
        registered = {row["runtime"]: dict(row) for row in conn.execute("SELECT * FROM employee_runtimes ORDER BY runtime").fetchall()}
        conn.close()
    except sqlite3.OperationalError:
        registered = {}
    ts = now()
    for runtime, notes in KNOWN_RUNTIMES.items():
        registered.setdefault(
            runtime,
            {"runtime": runtime, "command": "", "status": "registered", "notes": notes, "created_at": ts, "updated_at": ts},
        )
    emit({"ok": True, "runtimes": [registered[key] for key in sorted(registered)]})
    return 0


def cmd_runtime_test(args: argparse.Namespace) -> int:
    checks: list[dict] = []
    if args.runtime == "openclaw":
        checks.append(check_command("openclaw"))
        oc = Path("/Users/owner/openclaw/scripts/oc")
        checks.append({"command": str(oc), "available": oc.exists(), "path": str(oc) if oc.exists() else ""})
    elif args.runtime == "hermes":
        checks.append(check_command("hermes"))
        checks.append({"path": "/Users/owner/.hermes", "available": Path("/Users/owner/.hermes").exists()})
    elif args.runtime == "codex":
        checks.append(check_command("codex"))
        checks.append({"path": "/Users/owner/openclaw/workspace-xmanx/projects/openclaw-codex-controller", "available": Path("/Users/owner/openclaw/workspace-xmanx/projects/openclaw-codex-controller").exists()})
    elif args.runtime == "claude":
        checks.append(check_command("claude"))
    elif args.runtime == "trae":
        checks.append(check_command("trae"))
    elif args.runtime == "antigravity":
        checks.append(check_command("antigravity"))
    else:
        try:
            conn = connect_readonly()
            row = conn.execute("SELECT * FROM employee_runtimes WHERE runtime = ?", (args.runtime,)).fetchone()
            conn.close()
        except sqlite3.OperationalError:
            row = None
        if not row:
            checks.append({"runtime": args.runtime, "available": False, "reason": "runtime_not_registered"})
        elif row["status"] == "disabled":
            checks.append({"runtime": args.runtime, "available": False, "reason": "runtime_disabled"})
        elif row["command"]:
            checks.append(check_command(row["command"].split()[0]))
        else:
            checks.append({"runtime": args.runtime, "available": True, "reason": "registered_without_probe_command"})
    ok = any(c.get("available") for c in checks)
    emit({"ok": ok, "runtime": args.runtime, "checks": checks})
    return 0 if ok else 1


ADAPTER_COMMANDS = {
    "openclaw": "company-openclaw-adapter",
    "hermes": "company-hermes-adapter",
    "codex": "company-codex-adapter",
    "claude": "company-claude-adapter",
    "trae": "company-trae-adapter",
    "antigravity": "company-antigravity-adapter",
}


def adapter_verify_agents(conn: sqlite3.Connection, requested: list[str]) -> list[dict]:
    clauses = ["runtime IN (%s)" % ",".join("?" for _ in ADAPTER_COMMANDS)]
    params: list[str] = list(ADAPTER_COMMANDS)
    if requested:
        resolved = [resolve_employee_alias(agent) for agent in requested]
        clauses.append("id IN (%s)" % ",".join("?" for _ in resolved))
        params.extend(resolved)
    sql = f"SELECT * FROM employees WHERE {' AND '.join(clauses)} ORDER BY runtime, id"
    return rows(conn, sql, tuple(params))


def parse_json_output(raw: str) -> dict:
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {"raw": raw}
    except json.JSONDecodeError:
        return {"raw": raw}


def run_companyctl_json(args: list[str]) -> tuple[int, dict, str]:
    cp = subprocess.run([str(ROOT / "bin" / "companyctl"), *args], cwd=str(ROOT), text=True, capture_output=True)
    return cp.returncode, parse_json_output(cp.stdout), cp.stderr


def cmd_runtime_ack_adapter_run(args: argparse.Namespace) -> int:
    conn = connect()
    actor = resolve_employee_alias(args.by)
    require_employee(conn, actor)
    run = conn.execute("SELECT * FROM adapter_runs WHERE id = ?", (args.run_id,)).fetchone()
    if not run:
        emit({"ok": False, "error": "adapter run not found", "run_id": args.run_id})
        return 1
    ts = now()
    conn.execute(
        """
        UPDATE adapter_runs
        SET acknowledged_at = ?, acknowledged_by = ?, acknowledgement_reason = ?
        WHERE id = ?
        """,
        (ts, actor, args.reason, args.run_id),
    )
    conn.commit()
    updated = conn.execute("SELECT * FROM adapter_runs WHERE id = ?", (args.run_id,)).fetchone()
    result = dict(updated) if updated else {}
    audit(conn, actor, "runtime.ack_adapter_run", args.run_id, {"reason": args.reason, "adapter_run": result})
    emit({"ok": True, "adapter_run": result})
    return 0


def cmd_runtime_adapter_runs(args: argparse.Namespace) -> int:
    conn = connect()
    where = []
    params: list[object] = []
    if args.agent:
        where.append("agent_id = ?")
        params.append(resolve_employee_alias(args.agent))
    if args.status == "failed":
        where.append("ok = 0")
    elif args.status == "ok":
        where.append("ok = 1")
    if args.unacknowledged_only:
        where.append("acknowledged_at = ''")
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    adapter_runs = rows(
        conn,
        f"""
        SELECT id, trace_id, agent_id, task_id, command, ok, processed, attempt, next_retry_at,
               acknowledged_at, acknowledged_by, acknowledgement_reason, created_at
        FROM adapter_runs
        {where_sql}
        ORDER BY created_at DESC
        LIMIT ?
        """,
        tuple([*params, args.limit]),
    )
    emit({"ok": True, "adapter_runs": adapter_runs})
    return 0


def summarize_adapter_result(result: dict) -> dict:
    runs = []
    for run in result.get("runs", []):
        if not isinstance(run, dict):
            continue
        command_result = run.get("result", {})
        parsed = run.get("parsed_stdout", {})
        runs.append(
            {
                "index": run.get("index", ""),
                "returncode": command_result.get("returncode", "") if isinstance(command_result, dict) else "",
                "task_id": parsed.get("task_id", "") if isinstance(parsed, dict) else "",
                "status": parsed.get("status", "") if isinstance(parsed, dict) else "",
                "processed": parsed.get("processed", "") if isinstance(parsed, dict) else "",
                "report": parsed.get("report", "") if isinstance(parsed, dict) else "",
            }
        )
    return {
        "ok": result.get("ok", False),
        "agent": result.get("agent", ""),
        "command": result.get("command", ""),
        "processed": result.get("processed", 0),
        "at": result.get("at", ""),
        "state_file": result.get("state_file", ""),
        "runs": runs,
    }


def cmd_runtime_adapter_run_show(args: argparse.Namespace) -> int:
    conn = connect()
    run = conn.execute("SELECT * FROM adapter_runs WHERE id = ?", (args.run_id,)).fetchone()
    if not run:
        emit({"ok": False, "error": "adapter run not found", "run_id": args.run_id})
        return 1
    adapter_run = dict(run)
    try:
        result = json.loads(adapter_run.get("result_json", "{}") or "{}")
    except json.JSONDecodeError:
        result = {"raw": adapter_run.get("result_json", "")}
    if args.summary:
        adapter_summary = {k: v for k, v in adapter_run.items() if k != "result_json"}
        emit({"ok": True, "adapter_run": adapter_summary, "result_summary": summarize_adapter_result(result)})
        return 0
    emit({"ok": True, "adapter_run": adapter_run, "result": result})
    return 0


def adapter_run_task_id(adapter_run: sqlite3.Row | dict) -> str:
    structured = dict(adapter_run).get("task_id", "")
    if structured:
        return str(structured)
    try:
        result = json.loads((dict(adapter_run).get("result_json") or "{}"))
    except json.JSONDecodeError:
        return ""
    if isinstance(result, dict):
        for run in result.get("runs", []):
            if isinstance(run, dict):
                parsed = run.get("parsed_stdout", {})
                if isinstance(parsed, dict) and parsed.get("task_id"):
                    return str(parsed["task_id"])
    return ""


def cmd_runtime_retry_adapter_run(args: argparse.Namespace) -> int:
    conn = connect()
    actor = resolve_employee_alias(args.by)
    require_employee(conn, actor)
    run = conn.execute("SELECT * FROM adapter_runs WHERE id = ?", (args.run_id,)).fetchone()
    if not run:
        emit({"ok": False, "error": "adapter run not found", "run_id": args.run_id})
        return 1
    if run["ok"]:
        emit({"ok": False, "error": "adapter run did not fail", "run_id": args.run_id})
        return 1
    task_id = args.task_id or adapter_run_task_id(run)
    if not task_id:
        emit({"ok": False, "error": "task id not found in adapter run; pass --task-id", "run_id": args.run_id})
        return 1
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        emit({"ok": False, "error": "task not found", "task_id": task_id})
        return 1
    ts = now()
    conn.execute(
        """
        UPDATE tasks
        SET status = 'submitted', claimed_by = '', blocker = '', updated_at = ?
        WHERE id = ?
        """,
        (ts, task_id),
    )
    conn.execute("DELETE FROM locks WHERE resource_key = ?", (f"task:{task_id}",))
    conn.execute(
        """
        UPDATE adapter_runs
        SET acknowledged_at = ?, acknowledged_by = ?, acknowledgement_reason = ?
        WHERE id = ?
        """,
        (ts, actor, f"retry requested: {args.reason}", args.run_id),
    )
    metadata = update_task_metadata(
        conn,
        task_id,
        {
            "recovery": {
                "retry_adapter_run": args.run_id,
                "retry_requested_by": actor,
                "retry_reason": args.reason,
                "retried_at": ts,
            }
        },
    )
    conn.commit()
    event = record_event(conn, "task.retried", actor, task_id=task_id, payload={"adapter_run_id": args.run_id, "reason": args.reason})
    conn.execute("UPDATE company_events SET processed_at = ? WHERE id = ?", (now(), event["id"]))
    conn.commit()
    audit(conn, actor, "runtime.retry_adapter_run", task_id, {"reason": args.reason, "adapter_run_id": args.run_id, "event_id": event["id"]})
    emit({"ok": True, "run_id": args.run_id, "task_id": task_id, "status": "submitted", "metadata": metadata, "event_id": event["id"]})
    return 0


def cmd_runtime_verify_adapters(args: argparse.Namespace) -> int:
    conn = connect()
    agents = adapter_verify_agents(conn, parse_csv(args.agents))
    results = []
    for emp in agents:
        runtime = emp["runtime"]
        command = ADAPTER_COMMANDS.get(runtime, "")
        task_id = args.task_id_prefix + f"-{emp['id']}"
        title = f"Runtime adapter dry-run check: {emp['id']}"
        result = {
            "agent": emp["id"],
            "runtime": runtime,
            "command": command,
            "task_id": task_id,
            "ok": False,
        }
        if not command:
            result["error"] = "no adapter command"
            results.append(result)
            continue
        existing = conn.execute("SELECT status, evidence_path FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not existing:
            submit_code, submit_payload, submit_stderr = run_companyctl_json(
                [
                    "task",
                    "submit",
                    "--from",
                    args.source,
                    "--to",
                    emp["id"],
                    "--task-id",
                    task_id,
                    "--title",
                    title,
                    "--description",
                    "Adapter dry-run check task. Adapter must claim, write evidence, complete, and heartbeat.",
                    "--priority",
                    "P3",
                ]
            )
            if submit_code != 0:
                result.update({"error": "task submit failed", "submit_stdout": submit_payload, "submit_stderr": submit_stderr})
                results.append(result)
                continue
            conn.close()
            conn = connect()
        cmd = [str(ROOT / "bin" / command), "--agent", emp["id"]]
        if args.execute:
            cmd.append("--execute")
        cp = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True)
        current = conn.execute("SELECT status, evidence_path, blocker FROM tasks WHERE id = ?", (task_id,)).fetchone()
        hb = conn.execute("SELECT last_seen_at FROM heartbeats WHERE agent_id = ?", (emp["id"],)).fetchone()
        evidence = current["evidence_path"] if current else ""
        result.update(
            {
                "exit_code": cp.returncode,
                "stdout": parse_json_output(cp.stdout),
                "stderr": cp.stderr,
                "task_status": current["status"] if current else "",
                "evidence": evidence,
                "evidence_exists": bool(evidence and Path(evidence).exists()),
                "blocker": current["blocker"] if current else "",
                "heartbeat": hb["last_seen_at"] if hb else "",
            }
        )
        result["ok"] = cp.returncode == 0 and result["task_status"] == "completed" and result["evidence_exists"] and bool(result["heartbeat"])
        results.append(result)
    scheduler_result = {}
    if args.run_scheduler:
        cp = subprocess.run(
            [str(ROOT / "bin" / "companyctl"), "scheduler", "run", "--limit", str(max(20, len(results) * 2))],
            cwd=str(ROOT),
            text=True,
            capture_output=True,
        )
        scheduler_result = {
            "exit_code": cp.returncode,
            "stdout": parse_json_output(cp.stdout),
            "stderr": cp.stderr,
        }
    ok = all(item["ok"] for item in results) if results else False
    if args.run_scheduler and scheduler_result.get("exit_code") != 0:
        ok = False
    audit_error = ""
    try:
        audit(conn, "companyctl", "runtime.verify_adapters", "", {"execute": args.execute, "agents": [r["agent"] for r in results], "ok": ok, "scheduler": scheduler_result})
    except sqlite3.OperationalError as exc:
        audit_error = str(exc)
        if "readonly" not in audit_error.lower():
            raise
    emit({"ok": ok, "execute": args.execute, "count": len(results), "results": results, "scheduler": scheduler_result, "audit_error": audit_error})
    return 0 if ok else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    if args.summary:
        conn = connect_readonly()
    else:
        conn = connect()
        for runtime in KNOWN_RUNTIMES:
            ensure_runtime(conn, runtime)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        RFC_DIR.mkdir(parents=True, exist_ok=True)
        APPROVAL_STATE_DIR.mkdir(parents=True, exist_ok=True)
        EMPLOYEES_DIR.mkdir(parents=True, exist_ok=True)
    try:
        counts = {
            "employees": conn.execute("SELECT COUNT(*) FROM employees").fetchone()[0],
            "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            "task_metadata": conn.execute("SELECT COUNT(*) FROM task_metadata").fetchone()[0],
            "task_relations": conn.execute("SELECT COUNT(*) FROM task_relations").fetchone()[0],
            "projects": conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0],
            "active_projects": conn.execute("SELECT COUNT(*) FROM projects WHERE status = 'active'").fetchone()[0],
            "completed_projects": conn.execute("SELECT COUNT(*) FROM projects WHERE status = 'completed'").fetchone()[0],
            "project_acceptances": conn.execute("SELECT COUNT(*) FROM project_acceptances").fetchone()[0],
            "claimed_tasks": conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'claimed'").fetchone()[0],
            "locks": conn.execute("SELECT COUNT(*) FROM locks").fetchone()[0],
            "stale_locks": sum(1 for row in conn.execute("SELECT lease_until FROM locks").fetchall() if parse_time(row["lease_until"]) <= datetime.now(timezone.utc).astimezone()),
            "conversations": conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
            "conversation_messages": conn.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0],
            "messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            "heartbeats": conn.execute("SELECT COUNT(*) FROM heartbeats").fetchone()[0],
            "runtimes": conn.execute("SELECT COUNT(*) FROM employee_runtimes").fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM company_events").fetchone()[0],
            "pending_events": conn.execute("SELECT COUNT(*) FROM company_events WHERE processed_at = ''").fetchone()[0],
            "approvals": conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0],
            "pending_approvals": conn.execute("SELECT COUNT(*) FROM approvals WHERE status = 'pending'").fetchone()[0],
            "rfcs": conn.execute("SELECT COUNT(*) FROM rfcs").fetchone()[0],
            "pending_rfcs": conn.execute("SELECT COUNT(*) FROM rfcs WHERE status = 'pending'").fetchone()[0],
            "hook_action_runs": conn.execute("SELECT COUNT(*) FROM hook_action_runs").fetchone()[0],
            "adapter_runs": conn.execute("SELECT COUNT(*) FROM adapter_runs").fetchone()[0],
            "failed_adapter_runs": conn.execute("SELECT COUNT(*) FROM adapter_runs WHERE ok = 0 AND acknowledged_at = ''").fetchone()[0],
        }
        heartbeat_cutoff = datetime.now(timezone.utc).astimezone() - timedelta(minutes=15)
        missing_heartbeats = rows(
            conn,
            """
            SELECT e.id, e.runtime
            FROM employees e
            LEFT JOIN heartbeats h ON h.agent_id = e.id
            WHERE e.status = 'active' AND h.agent_id IS NULL
            ORDER BY e.id
            """,
        )
        stale_heartbeats = []
        for row in conn.execute(
            """
            SELECT e.id, e.runtime, h.last_seen_at
            FROM employees e
            JOIN heartbeats h ON h.agent_id = e.id
            WHERE e.status = 'active'
            ORDER BY e.id
            """
        ).fetchall():
            if parse_time(row["last_seen_at"]) < heartbeat_cutoff:
                stale_heartbeats.append(dict(row))
        pending = {
            "events": rows(conn, "SELECT id, event_type, source_agent, task_id, created_at FROM company_events WHERE processed_at = '' ORDER BY created_at ASC LIMIT 20"),
            "approvals": rows(conn, "SELECT id, source_agent, action, status, updated_at FROM approvals WHERE status = 'pending' ORDER BY updated_at ASC LIMIT 20"),
            "rfcs": rows(conn, "SELECT id, author_agent, status, updated_at FROM rfcs WHERE status = 'pending' ORDER BY updated_at ASC LIMIT 20"),
        }
        claimed_tasks = rows(conn, "SELECT id, target_agent, claimed_by, updated_at FROM tasks WHERE status = 'claimed' ORDER BY updated_at ASC LIMIT 20")
        failed_adapter_runs = rows(
            conn,
            """
            SELECT id, agent_id, task_id, command, processed, created_at
            FROM adapter_runs
            WHERE ok = 0 AND acknowledged_at = ''
            ORDER BY created_at DESC
            LIMIT 20
            """,
        )
        capability_issues = employee_capability_issues(conn)
        evidence_issues = task_evidence_issues(conn)
        stale_locks = []
        for lock in conn.execute("SELECT * FROM locks ORDER BY updated_at ASC").fetchall():
            if parse_time(lock["lease_until"]) <= datetime.now(timezone.utc).astimezone():
                stale_locks.append(dict(lock))
        daemon = daemon_health()
        launchd = launchd_health()
        openclaw_guard = openclaw_guard_health()
        issues = []
        if not daemon["ok"]:
            issues.append(daemon["reason"] or "daemon_unhealthy")
        if args.strict_launchd and not launchd["installed"]:
            issues.append("launchd_not_installed")
        if args.strict_launchd and launchd["installed"] and not launchd["matches_template"]:
            issues.append("launchd_template_mismatch")
        if args.strict_openclaw and not openclaw_guard["ok"]:
            issues.extend(openclaw_guard["issues"])
        if missing_heartbeats:
            issues.append("missing_heartbeats")
        if stale_heartbeats:
            issues.append("stale_heartbeats")
        if pending["events"]:
            issues.append("pending_events")
        if pending["approvals"]:
            issues.append("pending_approvals")
        if pending["rfcs"]:
            issues.append("pending_rfcs")
        if failed_adapter_runs:
            issues.append("adapter_failures")
        if capability_issues:
            issues.append("employee_capability_issues")
        if evidence_issues:
            issues.append("task_evidence_issues")
        if stale_locks:
            issues.append("stale_locks")
        health = {
            "ok": not issues,
            "issues": issues,
            "heartbeat_stale_minutes": 15,
            "missing_heartbeats": missing_heartbeats,
            "stale_heartbeats": stale_heartbeats,
            "pending": pending,
            "claimed_tasks": claimed_tasks,
            "failed_adapter_runs": failed_adapter_runs,
            "capability_issues": capability_issues,
            "evidence_issues": evidence_issues,
            "stale_locks": stale_locks,
            "daemon": daemon,
            "launchd": launchd,
            "openclaw_guard": openclaw_guard,
        }
        if args.summary:
            emit(
                {
                    "ok": health["ok"],
                    "issues": issues,
                    "counts": {
                        "employees": counts["employees"],
                        "active_projects": counts["active_projects"],
                        "claimed_tasks": counts["claimed_tasks"],
                        "pending_events": counts["pending_events"],
                        "pending_approvals": counts["pending_approvals"],
                        "pending_rfcs": counts["pending_rfcs"],
                        "heartbeats": counts["heartbeats"],
                        "adapter_runs": counts["adapter_runs"],
                        "failed_adapter_runs": counts["failed_adapter_runs"],
                        "capability_issues": len(capability_issues),
                        "task_evidence_issues": len(evidence_issues),
                    },
                    "heartbeat": {
                        "stale_minutes": health["heartbeat_stale_minutes"],
                        "missing": len(missing_heartbeats),
                        "stale": len(stale_heartbeats),
                        "missing_agents": [row["id"] for row in missing_heartbeats],
                        "stale_agents": [row["id"] for row in stale_heartbeats],
                    },
                    "adapters": {
                        "failed_unacknowledged": len(failed_adapter_runs),
                        "failed_run_ids": [row["id"] for row in failed_adapter_runs],
                    },
                    "capabilities": {
                        "issues": len(capability_issues),
                        "agents": sorted({row["agent"] for row in capability_issues}),
                    },
                    "evidence": {
                        "issues": len(evidence_issues),
                        "tasks": [row["task_id"] for row in evidence_issues[:20]],
                    },
                    "daemon": daemon,
                    "launchd": launchd,
                    "openclaw_guard": openclaw_guard,
                }
            )
            return 0 if health["ok"] else 1
        emit({"ok": health["ok"], "root": str(ROOT), "db": str(DB_PATH), "counts": counts, "health": health})
        return 0 if health["ok"] else 1
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="companyctl", description="Company Kernel command interface")
    sub = parser.add_subparsers(dest="cmd", required=True)

    emp = sub.add_parser("employee")
    emp_sub = emp.add_subparsers(dest="employee_cmd", required=True)
    emp_create = emp_sub.add_parser("create")
    emp_create.add_argument("--id", required=True)
    emp_create.add_argument("--name", required=True)
    emp_create.add_argument("--role", required=True)
    emp_create.add_argument("--runtime", required=True)
    emp_create.add_argument("--workspace", required=True)
    emp_create.add_argument("--dry-run", action="store_true")
    emp_create.set_defaults(func=cmd_employee_create)
    emp_list = emp_sub.add_parser("list")
    emp_list.set_defaults(func=cmd_employee_list)
    emp_show = emp_sub.add_parser("show")
    emp_show.add_argument("--id", required=True)
    emp_show.set_defaults(func=cmd_employee_show)
    emp_update = emp_sub.add_parser("update")
    emp_update.add_argument("--id", required=True)
    emp_update.add_argument("--name", default="")
    emp_update.add_argument("--role", default="")
    emp_update.add_argument("--runtime", default="")
    emp_update.add_argument("--workspace", default="")
    emp_update.add_argument("--status", choices=["active", "candidate", "archived"], default="")
    emp_update.add_argument("--dry-run", action="store_true")
    emp_update.set_defaults(func=cmd_employee_update)
    emp_capabilities = emp_sub.add_parser("capabilities")
    emp_capabilities.add_argument("--id", required=True)
    emp_capabilities.add_argument("--set-skills", default="", help="comma-separated replacement list")
    emp_capabilities.add_argument("--add-skill", action="append", default=[])
    emp_capabilities.add_argument("--set-tools", default="", help="comma-separated replacement list")
    emp_capabilities.add_argument("--add-tool", action="append", default=[])
    emp_capabilities.add_argument("--set-task-types", default="", help="comma-separated replacement list")
    emp_capabilities.set_defaults(func=cmd_employee_capabilities)
    emp_permissions = emp_sub.add_parser("permissions")
    emp_permissions.add_argument("--id", required=True)
    emp_permissions.add_argument("--can-submit-tasks", choices=["keep", "true", "false"], default="keep")
    emp_permissions.add_argument("--can-claim-tasks", choices=["keep", "true", "false"], default="keep")
    emp_permissions.add_argument("--can-modify-kernel", choices=["keep", "true", "false"], default="keep")
    emp_permissions.add_argument("--requires-approval-for", default="", help="comma-separated replacement list")
    emp_permissions.set_defaults(func=cmd_employee_permissions)
    emp_match = emp_sub.add_parser("match")
    emp_match.add_argument("--skills", default="", help="comma-separated required skills")
    emp_match.add_argument("--tools", default="", help="comma-separated preferred tools")
    emp_match.add_argument("--task-type", default="")
    emp_match.add_argument("--runtime", default="")
    emp_match.add_argument("--role", default="")
    emp_match.add_argument("--limit", type=int, default=10)
    emp_match.add_argument("--include-unavailable", action="store_true")
    emp_match.set_defaults(func=cmd_employee_match)
    emp_import_openclaw = emp_sub.add_parser("import-openclaw")
    emp_import_openclaw.add_argument("--config", default="/Users/owner/openclaw/openclaw.json")
    emp_import_openclaw.add_argument("--dry-run", action="store_true")
    emp_import_openclaw.set_defaults(func=cmd_employee_import_openclaw)
    emp_onboard = emp_sub.add_parser("onboard")
    emp_onboard.add_argument("--id", required=True)
    emp_onboard.add_argument("--name", required=True)
    emp_onboard.add_argument("--role", required=True)
    emp_onboard.add_argument("--runtime", required=True)
    emp_onboard.add_argument("--workspace", required=True)
    emp_onboard.add_argument("--alias", default="")
    emp_onboard.add_argument("--skills", default="", help="comma-separated skills")
    emp_onboard.add_argument("--tools", default="", help="comma-separated tools")
    emp_onboard.add_argument("--task-types", default="", help="comma-separated preferred task types")
    emp_onboard.add_argument("--can-talk-to", default="", help="comma-separated employee ids or aliases")
    emp_onboard.add_argument("--can-assign-to", default="", help="comma-separated employee ids or aliases")
    emp_onboard.add_argument("--open-communication", action="store_true", help="allow communication with all currently registered employees")
    emp_onboard.add_argument("--channel", default="")
    emp_onboard.add_argument("--handoff-mode", default="task_or_hook")
    emp_onboard.add_argument("--requires-approval-for", default="payment,compensation,salary,penalty,external_send")
    emp_onboard.add_argument("--no-submit-tasks", action="store_true")
    emp_onboard.add_argument("--no-claim-tasks", action="store_true")
    emp_onboard.add_argument("--can-modify-kernel", action="store_true")
    emp_onboard.add_argument("--create-test-task", action="store_true")
    emp_onboard.add_argument("--test-source", default="openclaw-main")
    emp_onboard.add_argument("--test-task-id", default="")
    emp_onboard.add_argument("--dry-run", action="store_true")
    emp_onboard.set_defaults(func=cmd_employee_onboard)
    emp_offboard = emp_sub.add_parser("offboard")
    emp_offboard.add_argument("--id", required=True)
    emp_offboard.add_argument("--hard-delete", action="store_true", help="delete only Company Kernel-managed employee files/workspace")
    emp_offboard.add_argument("--dry-run", action="store_true")
    emp_offboard.set_defaults(func=cmd_employee_offboard)

    attendance = sub.add_parser("attendance")
    attendance_sub = attendance.add_subparsers(dest="attendance_cmd", required=True)
    attendance_sweep = attendance_sub.add_parser("sweep")
    attendance_sweep.add_argument("--source", default="main")
    attendance_sweep.add_argument("--agents", default="", help="comma-separated employee ids; default active employees")
    attendance_sweep.add_argument("--sweep-id", default="")
    attendance_sweep.add_argument("--include-candidates", action="store_true")
    attendance_sweep.add_argument("--stale-minutes", type=int, default=15)
    attendance_sweep.add_argument("--probe-replies", action=argparse.BooleanOptionalAction, default=True, help="ask each supported runtime to reply <agent_id> 在岗")
    attendance_sweep.add_argument("--reply-timeout", type=int, default=120)
    attendance_sweep.set_defaults(func=cmd_attendance_sweep)

    task = sub.add_parser("task")
    task_sub = task.add_subparsers(dest="task_cmd", required=True)
    task_submit = task_sub.add_parser("submit")
    task_submit.add_argument("--from", dest="source", required=True)
    task_submit.add_argument("--to", dest="target", required=True)
    task_submit.add_argument("--title", required=True)
    task_submit.add_argument("--description", default="")
    task_submit.add_argument("--priority", default="P2")
    task_submit.add_argument("--task-id", default="")
    task_submit.add_argument("--changed-files", default="", help="comma-separated paths the task expects to modify")
    task_submit.add_argument("--rfc", default="", help="RFC path approving protected changes")
    task_submit.add_argument("--requires-approval", default="", help="force approval action before direct submit, e.g. external_send")
    task_submit.add_argument("--approval-id", default="", help="approved approval id for high-risk direct submit")
    task_submit.add_argument("--risk", default="P1")
    task_submit.set_defaults(func=cmd_task_submit)
    task_route = task_sub.add_parser("route")
    task_route.add_argument("--from", dest="source", required=True)
    task_route.add_argument("--title", required=True)
    task_route.add_argument("--description", default="")
    task_route.add_argument("--priority", default="P2")
    task_route.add_argument("--task-id", default="")
    task_route.add_argument("--skills", default="", help="comma-separated required skills")
    task_route.add_argument("--tools", default="", help="comma-separated preferred tools")
    task_route.add_argument("--task-type", default="")
    task_route.add_argument("--runtime", default="")
    task_route.add_argument("--role", default="")
    task_route.add_argument("--limit", type=int, default=10)
    task_route.add_argument("--include-unavailable", action="store_true")
    task_route.add_argument("--requires-approval", default="", help="force approval action before routing, e.g. external_send")
    task_route.add_argument("--approval-id", default="", help="approved approval id for high-risk route")
    task_route.add_argument("--risk", default="P1")
    task_route.add_argument("--changed-files", default="", help="comma-separated paths the task expects to modify")
    task_route.add_argument("--rfc", default="", help="RFC path approving protected changes")
    task_route.set_defaults(func=cmd_task_route)
    task_list = task_sub.add_parser("list")
    task_list.add_argument("--agent", default="")
    task_list.set_defaults(func=cmd_task_list)
    task_show = task_sub.add_parser("show")
    task_show.add_argument("--task-id", required=True)
    task_show.set_defaults(func=cmd_task_show)
    task_children = task_sub.add_parser("children")
    task_children.add_argument("--task-id", required=True)
    task_children.set_defaults(func=cmd_task_children)
    task_split = task_sub.add_parser("split")
    task_split.add_argument("--task-id", required=True)
    task_split.add_argument("--by", required=True)
    task_split.add_argument("--item", action="append", default=[], help="target|title|description|priority; repeat for multiple child tasks")
    task_split.add_argument("--plan", default="", help="JSON list or object with items for long-task decomposition")
    task_split.add_argument("--child-id-prefix", default="")
    task_split.set_defaults(func=cmd_task_split)
    task_collect = task_sub.add_parser("collect")
    task_collect.add_argument("--task-id", required=True)
    task_collect.add_argument("--agent", required=True)
    task_collect.add_argument("--summary", default="")
    task_collect.add_argument("--evidence", default="")
    task_collect.add_argument("--force", action="store_true")
    task_collect.set_defaults(func=cmd_task_collect)
    task_discuss = task_sub.add_parser("discuss")
    task_discuss.add_argument("--task-id", required=True)
    task_discuss.add_argument("--from", dest="source", default="")
    task_discuss.add_argument("--participants", default="", help="comma-separated extra participants")
    task_discuss.add_argument("--title", default="")
    task_discuss.add_argument("--body", default="")
    task_discuss.add_argument("--evidence", default="")
    task_discuss.add_argument("--conversation-id", default="")
    task_discuss.set_defaults(func=cmd_task_discuss)
    task_conversations = task_sub.add_parser("conversations")
    task_conversations.add_argument("--task-id", required=True)
    task_conversations.set_defaults(func=cmd_task_conversations)
    task_claim = task_sub.add_parser("claim")
    task_claim.add_argument("--agent", required=True)
    task_claim.add_argument("--task-id", default="")
    task_claim.add_argument("--lease-seconds", type=int, default=1800)
    task_claim.set_defaults(func=cmd_task_claim)
    task_done = task_sub.add_parser("done")
    task_done.add_argument("--agent", required=True)
    task_done.add_argument("--task-id", required=True)
    task_done.add_argument("--summary", required=True)
    task_done.add_argument("--evidence", required=True)
    task_done.set_defaults(func=cmd_task_done)
    task_block = task_sub.add_parser("block")
    task_block.add_argument("--agent", required=True)
    task_block.add_argument("--task-id", required=True)
    task_block.add_argument("--blocker", required=True)
    task_block.set_defaults(func=cmd_task_block)
    task_reopen = task_sub.add_parser("reopen")
    task_reopen.add_argument("--task-id", required=True)
    task_reopen.add_argument("--by", required=True)
    task_reopen.add_argument("--reason", required=True)
    task_reopen.add_argument("--status", choices=["submitted", "claimed"], default="submitted")
    task_reopen.set_defaults(func=cmd_task_reopen)
    task_reassign = task_sub.add_parser("reassign")
    task_reassign.add_argument("--task-id", required=True)
    task_reassign.add_argument("--by", required=True)
    task_reassign.add_argument("--to", required=True)
    task_reassign.add_argument("--reason", required=True)
    task_reassign.set_defaults(func=cmd_task_reassign)

    project = sub.add_parser("project")
    project_sub = project.add_subparsers(dest="project_cmd", required=True)
    project_create = project_sub.add_parser("create")
    project_create.add_argument("--project-id", default="")
    project_create.add_argument("--title", required=True)
    project_create.add_argument("--goal", default="")
    project_create.add_argument("--owner", required=True)
    project_create.add_argument("--status", default="active")
    project_create.add_argument("--acceptance", default="", help="semicolon-separated acceptance criteria")
    project_create.set_defaults(func=cmd_project_create)
    project_list = project_sub.add_parser("list")
    project_list.add_argument("--status", default="active", choices=["active", "paused", "completed", "blocked", "all"])
    project_list.set_defaults(func=cmd_project_list)
    project_show = project_sub.add_parser("show")
    project_show.add_argument("--project-id", required=True)
    project_show.set_defaults(func=cmd_project_show)
    project_link_task = project_sub.add_parser("link-task")
    project_link_task.add_argument("--project-id", required=True)
    project_link_task.add_argument("--task-id", required=True)
    project_link_task.set_defaults(func=cmd_project_link_task)
    project_plan_add = project_sub.add_parser("plan-add")
    project_plan_add.add_argument("--project-id", required=True)
    project_plan_add.add_argument("--title", required=True)
    project_plan_add.add_argument("--status", default="planned", choices=["planned", "in_progress", "done", "completed", "blocked", "cancelled"])
    project_plan_add.add_argument("--owner", default="")
    project_plan_add.add_argument("--due-at", default="")
    project_plan_add.add_argument("--task-id", default="")
    project_plan_add.add_argument("--plan-id", default="")
    project_plan_add.set_defaults(func=cmd_project_plan_add)
    project_plan_list = project_sub.add_parser("plan-list")
    project_plan_list.add_argument("--project-id", required=True)
    project_plan_list.set_defaults(func=cmd_project_plan_list)
    project_plan_status = project_sub.add_parser("plan-status")
    project_plan_status.add_argument("--project-id", required=True)
    project_plan_status.add_argument("--plan-id", required=True)
    project_plan_status.add_argument("--status", required=True, choices=["planned", "in_progress", "done", "completed", "blocked", "cancelled"])
    project_plan_status.set_defaults(func=cmd_project_plan_status)
    project_status = project_sub.add_parser("status")
    project_status.add_argument("--project-id", required=True)
    project_status.add_argument("--status", required=True)
    project_status.set_defaults(func=cmd_project_status)
    project_review = project_sub.add_parser("review")
    project_review.add_argument("--project-id", required=True)
    project_review.set_defaults(func=cmd_project_review)
    project_accept = project_sub.add_parser("accept")
    project_accept.add_argument("--project-id", required=True)
    project_accept.add_argument("--by", required=True)
    project_accept.add_argument("--summary", required=True)
    project_accept.add_argument("--force", action="store_true")
    project_accept.set_defaults(func=cmd_project_accept)

    message = sub.add_parser("message")
    message_sub = message.add_subparsers(dest="message_cmd", required=True)
    message_send = message_sub.add_parser("send")
    message_send.add_argument("--from", dest="source", required=True)
    message_send.add_argument("--to", dest="target", required=True)
    message_send.add_argument("--body", required=True)
    message_send.add_argument("--message-id", default="")
    message_send.set_defaults(func=cmd_message_send)
    message_direct = message_sub.add_parser("direct")
    message_direct.add_argument("--from", dest="source", required=True)
    message_direct.add_argument("--to", dest="target", required=True)
    message_direct.add_argument("--body", required=True)
    message_direct.add_argument("--message-id", default="")
    message_direct.add_argument("--session-key", default="")
    message_direct.add_argument("--timeout", type=int, default=120)
    message_direct.add_argument("--deliver", action="store_true")
    message_direct.add_argument("--reply-channel", default="")
    message_direct.add_argument("--reply-to", default="")
    message_direct.add_argument("--reply-account", default="")
    message_direct.set_defaults(func=cmd_message_direct)
    message_list = message_sub.add_parser("list")
    message_list.add_argument("--agent", required=True)
    message_list.set_defaults(func=cmd_message_list)

    conversation = sub.add_parser("conversation")
    conversation_sub = conversation.add_subparsers(dest="conversation_cmd", required=True)
    conversation_start = conversation_sub.add_parser("start")
    conversation_start.add_argument("--from", dest="source", required=True)
    conversation_start.add_argument("--participants", required=True, help="comma-separated employee ids")
    conversation_start.add_argument("--title", required=True)
    conversation_start.add_argument("--body", required=True)
    conversation_start.add_argument("--evidence", default="")
    conversation_start.add_argument("--conversation-id", default="")
    conversation_start.set_defaults(func=cmd_conversation_start)
    conversation_reply = conversation_sub.add_parser("reply")
    conversation_reply.add_argument("--from", dest="source", required=True)
    conversation_reply.add_argument("--conversation-id", required=True)
    conversation_reply.add_argument("--body", required=True)
    conversation_reply.add_argument("--evidence", default="")
    conversation_reply.add_argument("--message-id", default="")
    conversation_reply.set_defaults(func=cmd_conversation_reply)
    conversation_list = conversation_sub.add_parser("list")
    conversation_list.add_argument("--agent", required=True)
    conversation_list.set_defaults(func=cmd_conversation_list)
    conversation_show = conversation_sub.add_parser("show")
    conversation_show.add_argument("--conversation-id", required=True)
    conversation_show.set_defaults(func=cmd_conversation_show)

    communication = sub.add_parser("communication")
    communication_sub = communication.add_subparsers(dest="communication_cmd", required=True)
    communication_show = communication_sub.add_parser("show")
    communication_show.add_argument("--agent", default="")
    communication_show.set_defaults(func=cmd_communication_show)
    communication_check = communication_sub.add_parser("check")
    communication_check.add_argument("--from", dest="source", required=True)
    communication_check.add_argument("--to", dest="target", required=True)
    communication_check.add_argument("--action", choices=["talk", "assign"], default="talk")
    communication_check.set_defaults(func=cmd_communication_check)

    policy = sub.add_parser("policy")
    policy_sub = policy.add_subparsers(dest="policy_cmd", required=True)
    policy_show = policy_sub.add_parser("show")
    policy_show.set_defaults(func=cmd_policy_show)

    guard = sub.add_parser("guard")
    guard_sub = guard.add_subparsers(dest="guard_cmd", required=True)
    guard_check = guard_sub.add_parser("check")
    guard_check.add_argument("--path", action="append", default=[])
    guard_check.add_argument("--changed-file", action="append", default=[])
    guard_check.set_defaults(func=cmd_guard_check)

    rfc = sub.add_parser("rfc")
    rfc_sub = rfc.add_subparsers(dest="rfc_cmd", required=True)
    rfc_create = rfc_sub.add_parser("create")
    rfc_create.add_argument("--rfc-id", default="")
    rfc_create.add_argument("--title", required=True)
    rfc_create.add_argument("--by", required=True)
    rfc_create.add_argument("--paths", required=True, help="comma-separated protected paths this RFC covers")
    rfc_create.add_argument("--reason", required=True)
    rfc_create.add_argument("--proposal", default="")
    rfc_create.add_argument("--rollback", default="")
    rfc_create.add_argument("--file", default="")
    rfc_create.add_argument("--overwrite", action="store_true")
    rfc_create.set_defaults(func=cmd_rfc_create)
    rfc_list = rfc_sub.add_parser("list")
    rfc_list.add_argument("--status", choices=["pending", "approved", "denied", "all"], default="pending")
    rfc_list.set_defaults(func=cmd_rfc_list)
    rfc_show = rfc_sub.add_parser("show")
    rfc_show.add_argument("--rfc", required=True)
    rfc_show.set_defaults(func=cmd_rfc_show)
    rfc_approve = rfc_sub.add_parser("approve")
    rfc_approve.add_argument("--rfc", required=True)
    rfc_approve.add_argument("--by", required=True)
    rfc_approve.add_argument("--reason", default="")
    rfc_approve.set_defaults(func=cmd_rfc_approve)
    rfc_deny = rfc_sub.add_parser("deny")
    rfc_deny.add_argument("--rfc", required=True)
    rfc_deny.add_argument("--by", required=True)
    rfc_deny.add_argument("--reason", default="")
    rfc_deny.set_defaults(func=cmd_rfc_deny)

    workflow = sub.add_parser("workflow")
    workflow_sub = workflow.add_subparsers(dest="workflow_cmd", required=True)
    workflow_validate = workflow_sub.add_parser("validate")
    workflow_validate.add_argument("--workflow", required=True)
    workflow_validate.set_defaults(func=cmd_workflow_validate)
    workflow_run = workflow_sub.add_parser("run")
    workflow_run.add_argument("--workflow", required=True)
    workflow_run.add_argument("--topic", default="")
    workflow_run.add_argument("--run-id", default="")
    workflow_run.add_argument("--max-steps", type=int, default=0)
    workflow_run.add_argument("--dry-run", action="store_true")
    workflow_run.set_defaults(func=cmd_workflow_run)

    scheduler = sub.add_parser("scheduler")
    scheduler_sub = scheduler.add_subparsers(dest="scheduler_cmd", required=True)
    scheduler_run = scheduler_sub.add_parser("run")
    scheduler_run.add_argument("--limit", type=int, default=20)
    scheduler_run.add_argument("--dry-run", action="store_true")
    scheduler_run.set_defaults(func=cmd_scheduler_run)
    scheduler_events = scheduler_sub.add_parser("events")
    scheduler_events.add_argument("--limit", type=int, default=20)
    scheduler_events.add_argument("--pending", action="store_true")
    scheduler_events.set_defaults(func=cmd_scheduler_events)
    scheduler_skip_event = scheduler_sub.add_parser("skip-event")
    scheduler_skip_event.add_argument("--event-id", required=True)
    scheduler_skip_event.add_argument("--by", required=True)
    scheduler_skip_event.add_argument("--reason", required=True)
    scheduler_skip_event.set_defaults(func=cmd_scheduler_skip_event)

    approval = sub.add_parser("approval")
    approval_sub = approval.add_subparsers(dest="approval_cmd", required=True)
    approval_request = approval_sub.add_parser("request")
    approval_request.add_argument("--from", dest="source", required=True)
    approval_request.add_argument("--action", required=True)
    approval_request.add_argument("--reason", required=True)
    approval_request.add_argument("--target", default="")
    approval_request.add_argument("--risk", default="")
    approval_request.add_argument("--evidence", default="")
    approval_request.add_argument("--task-id", default="")
    approval_request.add_argument("--approval-id", default="")
    approval_request.set_defaults(func=cmd_approval_request)
    approval_list = approval_sub.add_parser("list")
    approval_list.add_argument("--status", choices=["pending", "approved", "denied", "all"], default="pending")
    approval_list.add_argument("--agent", default="")
    approval_list.add_argument("--action", default="")
    approval_list.add_argument("--limit", type=int, default=50)
    approval_list.set_defaults(func=cmd_approval_list)
    approval_show = approval_sub.add_parser("show")
    approval_show.add_argument("--approval-id", required=True)
    approval_show.set_defaults(func=cmd_approval_show)
    approval_approve = approval_sub.add_parser("approve")
    approval_approve.add_argument("--approval-id", required=True)
    approval_approve.add_argument("--by", required=True)
    approval_approve.add_argument("--reason", default="")
    approval_approve.set_defaults(func=cmd_approval_approve)
    approval_deny = approval_sub.add_parser("deny")
    approval_deny.add_argument("--approval-id", required=True)
    approval_deny.add_argument("--by", required=True)
    approval_deny.add_argument("--reason", default="")
    approval_deny.set_defaults(func=cmd_approval_deny)

    lock = sub.add_parser("lock")
    lock_sub = lock.add_subparsers(dest="lock_cmd", required=True)
    lock_acquire = lock_sub.add_parser("acquire")
    lock_acquire.add_argument("--agent", required=True)
    lock_acquire.add_argument("--resource", required=True)
    lock_acquire.add_argument("--lease-seconds", type=int, default=1800)
    lock_acquire.set_defaults(func=cmd_lock_acquire)
    lock_release = lock_sub.add_parser("release")
    lock_release.add_argument("--agent", required=True)
    lock_release.add_argument("--resource", required=True)
    lock_release.add_argument("--force", action="store_true")
    lock_release.set_defaults(func=cmd_lock_release)
    lock_list = lock_sub.add_parser("list")
    lock_list.add_argument("--agent", default="")
    lock_list.set_defaults(func=cmd_lock_list)
    lock_unlock_stale = lock_sub.add_parser("unlock-stale")
    lock_unlock_stale.set_defaults(func=cmd_lock_unlock_stale)

    repair = sub.add_parser("repair")
    repair_sub = repair.add_subparsers(dest="repair_cmd", required=True)
    repair_reset_stale_claims = repair_sub.add_parser("reset-stale-claims")
    repair_reset_stale_claims.set_defaults(func=cmd_repair_reset_stale_claims)

    hb = sub.add_parser("heartbeat")
    hb.add_argument("--agent", required=True)
    hb.set_defaults(func=cmd_heartbeat)

    runtime = sub.add_parser("runtime")
    runtime_sub = runtime.add_subparsers(dest="runtime_cmd", required=True)
    runtime_register = runtime_sub.add_parser("register")
    runtime_register.add_argument("--runtime", required=True)
    runtime_register.add_argument("--command", default="")
    runtime_register.add_argument("--status", choices=["registered", "disabled"], default="registered")
    runtime_register.add_argument("--notes", default="")
    runtime_register.set_defaults(func=cmd_runtime_register)
    runtime_list = runtime_sub.add_parser("list")
    runtime_list.set_defaults(func=cmd_runtime_list)
    runtime_test = runtime_sub.add_parser("test")
    runtime_test.add_argument("--runtime", required=True)
    runtime_test.set_defaults(func=cmd_runtime_test)
    runtime_verify_adapters = runtime_sub.add_parser("verify-adapters")
    runtime_verify_adapters.add_argument("--agents", default="", help="comma-separated employee ids; defaults to all adapter-backed employees")
    runtime_verify_adapters.add_argument("--source", default="openclaw-main")
    runtime_verify_adapters.add_argument("--task-id-prefix", default="task-runtime-verify")
    runtime_verify_adapters.add_argument("--execute", action="store_true", help="run real adapter execution; default is safe dry-run")
    runtime_verify_adapters.add_argument("--run-scheduler", action=argparse.BooleanOptionalAction, default=True, help="process generated events after adapter verification")
    runtime_verify_adapters.set_defaults(func=cmd_runtime_verify_adapters)
    runtime_adapter_runs = runtime_sub.add_parser("adapter-runs")
    runtime_adapter_runs.add_argument("--agent", default="")
    runtime_adapter_runs.add_argument("--status", choices=["all", "ok", "failed"], default="all")
    runtime_adapter_runs.add_argument("--unacknowledged-only", action="store_true")
    runtime_adapter_runs.add_argument("--limit", type=int, default=20)
    runtime_adapter_runs.set_defaults(func=cmd_runtime_adapter_runs)
    runtime_adapter_run_show = runtime_sub.add_parser("adapter-run")
    runtime_adapter_run_sub = runtime_adapter_run_show.add_subparsers(dest="adapter_run_cmd", required=True)
    runtime_adapter_run_show_cmd = runtime_adapter_run_sub.add_parser("show")
    runtime_adapter_run_show_cmd.add_argument("--run-id", required=True)
    runtime_adapter_run_show_cmd.add_argument("--summary", action="store_true", help="omit raw result_json/stdout and return compact fields for alerts")
    runtime_adapter_run_show_cmd.set_defaults(func=cmd_runtime_adapter_run_show)
    runtime_ack_adapter_run = runtime_sub.add_parser("ack-adapter-run")
    runtime_ack_adapter_run.add_argument("--run-id", required=True)
    runtime_ack_adapter_run.add_argument("--by", required=True)
    runtime_ack_adapter_run.add_argument("--reason", required=True)
    runtime_ack_adapter_run.set_defaults(func=cmd_runtime_ack_adapter_run)
    runtime_retry_adapter_run = runtime_sub.add_parser("retry-adapter-run")
    runtime_retry_adapter_run.add_argument("--run-id", required=True)
    runtime_retry_adapter_run.add_argument("--by", required=True)
    runtime_retry_adapter_run.add_argument("--reason", required=True)
    runtime_retry_adapter_run.add_argument("--task-id", default="")
    runtime_retry_adapter_run.set_defaults(func=cmd_runtime_retry_adapter_run)

    doctor = sub.add_parser("doctor")
    doctor.add_argument("--summary", action="store_true", help="return compact health counts for low-token alert checks")
    doctor.add_argument("--strict-launchd", action="store_true", help="fail health check when the launchd agent is not installed")
    doctor.add_argument("--strict-openclaw", action="store_true", help="fail health check when OpenClaw-native Telegram safety guard detects conflicts or stuck ingress spools")
    doctor.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
