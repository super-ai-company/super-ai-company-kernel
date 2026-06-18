"""Company Kernel MCP server — exposes the kernel to Codex/Claude apps as native, auto-discovered
tools (no pasted prompts). Minimal stdio JSON-RPC 2.0 (the MCP transport) using only the stdlib, so
the kernel stays dependency-free. Each tool shells out to the absolute `companyctl` binary.

Register in an app's MCP config to point a command at `bin/company-kernel-mcp`.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CTL = str(ROOT / "bin" / "companyctl")
PROTOCOL_VERSION = "2024-11-05"


def _ctl(args: list[str]) -> dict:
    """Run companyctl and return parsed JSON (or an error envelope)."""
    try:
        cp = subprocess.run([CTL, *args], cwd=str(ROOT), capture_output=True, text=True, timeout=60)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"companyctl failed: {exc}"}
    out = (cp.stdout or "").strip()
    try:
        return json.loads(out) if out else {"ok": cp.returncode == 0, "stderr": cp.stderr[-400:]}
    except json.JSONDecodeError:
        return {"ok": cp.returncode == 0, "output": out[-2000:], "stderr": cp.stderr[-400:]}


# ---------------------------------------------------------------- tool implementations
def t_list_my_tasks(agent: str, **_) -> dict:
    data = _ctl(["task", "list", "--agent", agent])
    tasks = data.get("tasks", data.get("items", []))
    pend = [{"id": t["id"], "status": t["status"], "title": t.get("title"),
             "from": t.get("source_agent"), "priority": t.get("priority")}
            for t in tasks if t.get("status") in {"submitted", "claimed"}]
    return {"agent": agent, "to_do": pend, "count": len(pend)}


def t_show_task(task_id: str, **_) -> dict:
    return _ctl(["task", "show", "--task-id", task_id])


def t_claim_task(agent: str, task_id: str, **_) -> dict:
    return _ctl(["task", "claim", "--agent", agent, "--task-id", task_id])


def t_report_done(agent: str, task_id: str, summary: str, evidence: str, **_) -> dict:
    return _ctl(["task", "done", "--agent", agent, "--task-id", task_id, "--summary", summary, "--evidence", evidence])


def t_report_blocked(agent: str, task_id: str, blocker: str, **_) -> dict:
    return _ctl(["task", "block", "--agent", agent, "--task-id", task_id, "--blocker", blocker])


def t_dispatch_task(from_agent: str, to_agent: str, title: str, description: str = "", **_) -> dict:
    return _ctl(["task", "submit", "--from", from_agent, "--to", to_agent, "--title", title, "--description", description])


def t_start_meeting(from_agent: str, topic: str, participants: str, question: str, project: str = "", **_) -> dict:
    """Call a quick meeting to settle a hard decision; runs async in the background."""
    args = ["meeting", "request", "--from", from_agent, "--topic", topic,
            "--participants", participants, "--question", question]
    if project:
        args += ["--project", project]
    return _ctl(args)


def t_meeting_result(conversation_id: str, **_) -> dict:
    return _ctl(["meeting", "result", "--conversation-id", conversation_id])


def t_check_completions(agent: str, **_) -> dict:
    """Completions of tasks THIS agent dispatched (the result-*.json notices in its inbox)."""
    inbox = ROOT / "employees" / agent / "inbox"
    notices = []
    if inbox.exists():
        for p in sorted(inbox.glob("result-*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:20]:
            try:
                notices.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                pass
    return {"agent": agent, "completions": notices, "count": len(notices)}


TOOLS = [
    {"name": "list_my_tasks", "fn": t_list_my_tasks,
     "description": "List tasks assigned to you that still need doing (submitted/claimed).",
     "schema": {"type": "object", "properties": {"agent": {"type": "string", "description": "your employee id, e.g. codex"}}, "required": ["agent"]}},
    {"name": "show_task", "fn": t_show_task,
     "description": "Show a task's full detail (description, acceptance, workspace, evidence).",
     "schema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
    {"name": "claim_task", "fn": t_claim_task,
     "description": "Claim a task before working on it (takes a lock so no double-processing).",
     "schema": {"type": "object", "properties": {"agent": {"type": "string"}, "task_id": {"type": "string"}}, "required": ["agent", "task_id"]}},
    {"name": "report_done", "fn": t_report_done,
     "description": "Mark a task you executed as completed, with a summary and an evidence file path.",
     "schema": {"type": "object", "properties": {"agent": {"type": "string"}, "task_id": {"type": "string"}, "summary": {"type": "string"}, "evidence": {"type": "string", "description": "absolute path to an evidence file"}}, "required": ["agent", "task_id", "summary", "evidence"]}},
    {"name": "report_blocked", "fn": t_report_blocked,
     "description": "Mark a task blocked with a concrete reason (don't fake completion).",
     "schema": {"type": "object", "properties": {"agent": {"type": "string"}, "task_id": {"type": "string"}, "blocker": {"type": "string"}}, "required": ["agent", "task_id", "blocker"]}},
    {"name": "dispatch_task", "fn": t_dispatch_task,
     "description": "Dispatch a task to a colleague (codex backend, claude analysis, antigravity/agy frontend review, hermes coordination). For codex include '工作区: /abs/repo' in description; for a big agy review include '超时: 3600'.",
     "schema": {"type": "object", "properties": {"from_agent": {"type": "string", "description": "your id"}, "to_agent": {"type": "string"}, "title": {"type": "string"}, "description": {"type": "string"}}, "required": ["from_agent", "to_agent", "title"]}},
    {"name": "check_completions", "fn": t_check_completions,
     "description": "Check results of tasks YOU dispatched that have finished (the kernel pushes these to your inbox the instant they complete).",
     "schema": {"type": "object", "properties": {"agent": {"type": "string"}}, "required": ["agent"]}},
    {"name": "start_meeting", "fn": t_start_meeting,
     "description": "Call a quick meeting with colleagues to settle a hard decision you can't decide alone (e.g. a design fork). Runs async in the background; poll meeting_result for the conclusion. Use this instead of guessing or blocking on a fork — meetings are for the few genuinely hard calls.",
     "schema": {"type": "object", "properties": {"from_agent": {"type": "string", "description": "your id"}, "topic": {"type": "string", "description": "short meeting title"}, "participants": {"type": "string", "description": "comma-separated colleague ids to invite, e.g. codex-cli,claude-cli"}, "question": {"type": "string", "description": "the exact decision/question to settle"}, "project": {"type": "string", "description": "optional project id to tie the meeting to its memory bank"}}, "required": ["from_agent", "topic", "participants", "question"]}},
    {"name": "meeting_result", "fn": t_meeting_result,
     "description": "Poll a meeting you started for its conclusion (the chair's 方案/决策/纪要). Returns done=false while colleagues are still talking.",
     "schema": {"type": "object", "properties": {"conversation_id": {"type": "string"}}, "required": ["conversation_id"]}},
]
_BY_NAME = {t["name"]: t for t in TOOLS}


# ---------------------------------------------------------------- JSON-RPC / MCP loop
def _result(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle(req: dict):
    method = req.get("method")
    req_id = req.get("id")
    if method == "initialize":
        return _result(req_id, {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                                "serverInfo": {"name": "company-kernel", "version": "1.0.0"}})
    if method in {"notifications/initialized", "notifications/cancelled"}:
        return None  # notification, no response
    if method == "ping":
        return _result(req_id, {})
    if method == "tools/list":
        return _result(req_id, {"tools": [{"name": t["name"], "description": t["description"], "inputSchema": t["schema"]} for t in TOOLS]})
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        tool = _BY_NAME.get(name)
        if not tool:
            return _error(req_id, -32602, f"unknown tool: {name}")
        try:
            out = tool["fn"](**(params.get("arguments") or {}))
        except Exception as exc:  # noqa: BLE001
            return _result(req_id, {"content": [{"type": "text", "text": f"error: {exc}"}], "isError": True})
        return _result(req_id, {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, indent=2)}]})
    if req_id is not None:
        return _error(req_id, -32601, f"method not found: {method}")
    return None


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
