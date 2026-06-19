"""Make an agent TRULY on-duty, not just listed in the kernel.

Registering an employee (`company-add-employee`) only tells the *kernel* about it. The agent itself —
when you open it and chat — still doesn't know it's an employee or that it can use the kernel. This
installs the two things that make it aware, into the agent runtime's OWN config:

  1. the `company-kernel` MCP server (so the kernel tools appear natively), and
  2. a markered instruction block in the agent's global rules file (so every conversation it runs the
     check-in loop: list_my_tasks → claim → do → report; dispatch / meet colleagues).

Safe: backs up each file before touching it, idempotent (re-runnable, never duplicates), and supports
dry-run. These are LOCAL user files, never committed. Pure stdlib (tomllib read-only + manual TOML
append for Codex; JSON merge for Claude/Gemini).
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

ROOT = Path(os.environ.get("OPENCLAW_COMPANY_KERNEL_ROOT", Path(__file__).resolve().parents[1])).resolve()
MCP_BIN = str(ROOT / "bin" / "company-kernel-mcp")
CTL = str(ROOT / "bin" / "companyctl")

INSTR_START = "<!-- COMPANY_KERNEL_INTEGRATION:START -->"
INSTR_END = "<!-- COMPANY_KERNEL_INTEGRATION:END -->"

# runtime -> where that agent reads its MCP config + global instructions.
APP_CONFIG: dict[str, dict] = {
    "codex":       {"mcp": "~/.codex/config.toml",            "fmt": "toml", "instr": "~/.codex/AGENTS.md",  "key": "company_kernel"},
    "claude":      {"mcp": "~/.claude.json",                  "fmt": "json", "instr": "~/.claude/CLAUDE.md", "key": "company-kernel"},
    "gemini":      {"mcp": "~/.gemini/config/mcp_config.json", "fmt": "json", "instr": "~/.gemini/GEMINI.md", "key": "company-kernel"},
    "antigravity": {"mcp": "~/.gemini/config/mcp_config.json", "fmt": "json", "instr": "~/.gemini/GEMINI.md", "key": "company-kernel"},
}


def _instruction_block(agent_id: str) -> str:
    # Capability-awareness, NOT a forced per-turn check-in (the owner explicitly disliked token-wasting
    # auto-check-ins). This just tells the agent the kernel exists and HOW to use it when relevant.
    return "\n".join([
        INSTR_START,
        "## Company Kernel —— 你是公司员工(能力说明)",
        f"你是 Company Kernel 的员工,员工 id 是 `{agent_id}`,可通过 `company-kernel` MCP 使用内核能力:",
        f"- 看/认领/回报任务:`list_my_tasks` / `claim_task` / `report_done` / `report_blocked`(都带 agent=\"{agent_id}\");绝不假装完成。",
        f"- 派活给同事:`dispatch_task`;卡在难决策叫会:`start_meeting`;看你派的活回没回:`check_completions`。",
        f"- MCP 不可用就用绝对 CLI:`{CTL} task list --agent {agent_id}` 等。",
        "需要时主动用这些能力即可。是否每轮主动签到由 owner 的全局规则决定——本块只让你知道\"能用\"。",
        INSTR_END,
    ])


def _backup(path: Path) -> None:
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".ck-bak"))


def _install_mcp_toml(path: Path, key: str, dry_run: bool) -> str:
    """Append the [mcp_servers.<key>] table if absent (idempotent). Codex config.toml."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if f"[mcp_servers.{key}]" in text:
        return "already-present"
    block = (f"\n[mcp_servers.{key}]\ncommand = \"{MCP_BIN}\"\nargs = []\nstartup_timeout_sec = 60\n")
    if dry_run:
        return "would-add"
    path.parent.mkdir(parents=True, exist_ok=True)
    _backup(path)
    path.write_text(text + block, encoding="utf-8")
    return "added"


def _install_mcp_json(path: Path, key: str, dry_run: bool) -> str:
    """Merge mcpServers.<key> into a JSON config (idempotent). Claude/Gemini."""
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            return "skipped-malformed"
    servers = data.setdefault("mcpServers", {})
    desired = {"command": MCP_BIN, "args": []}
    if servers.get(key) == desired:
        return "already-present"
    if dry_run:
        return "would-add"
    servers[key] = desired
    path.parent.mkdir(parents=True, exist_ok=True)
    _backup(path)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return "added"


def _install_instructions(path: Path, agent_id: str, dry_run: bool) -> str:
    block = _instruction_block(agent_id)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    has_markers = INSTR_START in text and INSTR_END in text
    if has_markers:
        pre = text.split(INSTR_START)[0]
        post = text.split(INSTR_END, 1)[1]
        new = pre.rstrip() + "\n\n" + block + post
        verb = "update"
    else:
        new = (text.rstrip() + "\n\n" + block + "\n") if text else block + "\n"
        verb = "add"
    if new == text:
        return "already-present"
    if dry_run:
        return f"would-{verb}"
    path.parent.mkdir(parents=True, exist_ok=True)
    _backup(path)
    path.write_text(new, encoding="utf-8")
    return verb + "ed" if verb == "add" else "updated"


def install_for_runtime(runtime: str, *, agent_id: str | None = None, dry_run: bool = False) -> dict:
    """Install MCP + instructions for one runtime. Returns a per-file result dict."""
    cfg = APP_CONFIG.get(runtime)
    if not cfg:
        return {"ok": False, "runtime": runtime, "error": f"no integration profile for runtime '{runtime}' "
                f"(supported: {', '.join(sorted(APP_CONFIG))})"}
    agent_id = agent_id or runtime
    mcp_path = Path(cfg["mcp"]).expanduser()
    instr_path = Path(cfg["instr"]).expanduser()
    if cfg["fmt"] == "toml":
        mcp_result = _install_mcp_toml(mcp_path, cfg["key"], dry_run)
    else:
        mcp_result = _install_mcp_json(mcp_path, cfg["key"], dry_run)
    instr_result = _install_instructions(instr_path, agent_id, dry_run)
    ok = mcp_result not in ("skipped-malformed",)
    return {"ok": ok, "runtime": runtime, "agent_id": agent_id, "dry_run": dry_run,
            "mcp": {"file": str(mcp_path), "result": mcp_result},
            "instructions": {"file": str(instr_path), "result": instr_result}}
