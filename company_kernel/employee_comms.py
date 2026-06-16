"""Teach every employee HOW to communicate by surfacing its own rules into the runtime prompt.

Employees CAN already talk to anyone (policy.mode=open), but the kernel wrote `employees/<id>/rules.md`
and never read it back — so agents ran without knowing the comms protocol (report results, escalate
blockers, hand off, reach the owner). This helper injects that briefing into each adapter's prompt.
A per-deployment, gitignored file; if absent, a sensible default is used.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Per-runtime one-liner appended to the universal protocol.
_RUNTIME_NOTE = {
    "codex": "你可以改代码;完成把改动留在分支并回 evidence_path。",
    "claude": "你可以改代码或做分析;完成回 evidence_path。",
    "gemini": "你做 PM/UX 评审;完成回 evidence_path。",
    "antigravity": "你只审查、给建议,绝不改代码或新建文件;无法验证就 status: blocked。",
    "hermes": "你主持会议、做汇总;把纪要和结论回报给 owner-shift。",
    "openclaw": "你可以 relay 到 LINE/Telegram;发客户群用 channel-send 或任务 --deliver-to。",
}


def employee_rules_text(agent_id: str) -> str:
    p = ROOT / "employees" / str(agent_id) / "rules.md"
    try:
        return p.read_text(encoding="utf-8").strip() if p.exists() else ""
    except OSError:
        return ""


def communication_protocol(agent_id: str, runtime: str) -> str:
    """The short, must-read comms briefing every employee gets each run."""
    lines = [
        "## 通讯协议 / How to communicate (必读)",
        f"你是员工「{agent_id}」。和同事、老板的通讯方式:",
        "- 干完 → 用 companyctl 标记完成并附 evidence_path(真理源是「完成回报」,不是聊天回复)。",
        "- 卡住 → 用 companyctl task block 写明具体 blocker,绝不假装完成。",
        f'- 需要别的员工配合 → `companyctl message send --from {agent_id} --to <对方> --body "…"`;要一起讨论就发起/加入会议。',
        "- 高风险或重大动作 → 走审批;需要老板拍板就升级给 owner-shift。",
    ]
    note = _RUNTIME_NOTE.get(runtime, "")
    if note:
        lines.append(f"- 你的角色:{note}")
    block = "\n".join(lines)
    rules = employee_rules_text(agent_id)
    if rules:
        block += "\n\n## 你的专属规则 / Your rules\n" + rules
    return block
