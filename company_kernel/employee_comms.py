"""Teach every employee HOW to communicate by surfacing its own rules into the runtime prompt.

Employees CAN already talk to anyone (policy.mode=open), but the kernel wrote `employees/<id>/rules.md`
and never read it back — so agents ran without knowing the comms protocol (report results, escalate
blockers, hand off, reach the owner). This helper injects that briefing into each adapter's prompt.
A per-deployment, gitignored file; if absent, a sensible default is used.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# ABSOLUTE path to companyctl — employees run in their OWN repo (e.g. damov4), where bare
# `companyctl` is NOT on PATH. Always give them the full path so dispatch works from anywhere.
CTL = str(ROOT / "bin" / "companyctl")

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
        f"你是员工「{agent_id}」。**所有 companyctl 命令都用绝对路径 `{CTL}`**(你的工作目录里没有它,别用裸 `companyctl`,也别往本地 state 目录丢文件)。",
        "- **完成/卡住由内核自动记录** —— 你**不要**自己调 `task done`/`task block`(那是适配器的活)。你只管把活做完、把结果(评审/代码改动/证据)输出出来,内核会替你回报。绝不假装完成。",
        f'- 需要别的员工配合 → `{CTL} message send --from {agent_id} --to <对方> --body "…"`;要一起讨论就发起/加入会议。',
        f'- **拿不准的决策叉路**(架构选型、接口取舍等少数真难定的事)别瞎猜也别干等 → 自己叫一场会:`{CTL} meeting request --from {agent_id} --topic "…" --participants <同事,逗号分隔> --question "<要定的具体问题>"`,后台讨论几分钟,再用 `{CTL} meeting result --conversation-id <cid>` 取结论。会议只用于个别拿不准的决策,别拿日常小事开会。',
        f"- 派活给同事 → `{CTL} task submit --from {agent_id} --to <谁> --title … --description …`(`--from` 必须是注册员工,就用你自己的 id「{agent_id}」,别用 app 名)。",
        "  同事:codex 后端/写码 · claude 分析/评审 · antigravity(agy)前端 UI 审核 · hermes 协调 · openclaw 系 客户 LINE/Telegram。派 codex 必写 `工作区: /abs/repo`;派 agy 大审核写 `超时: 3600`。",
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


def default_onboarding_rules(agent_id: str, role: str = "", runtime: str = "") -> str:
    """The rules.md written when an employee is onboarded — its most important instruction file.

    It tells the new hire, up front, that it lives inside Company Kernel with colleagues and tools,
    so it can self-serve (dispatch to codex/agy, message, meet) instead of the owner fetching a
    prompt every time. Edit `employees/<id>/rules.md` to tailor any employee further."""
    note = _RUNTIME_NOTE.get(runtime, "")
    return "\n".join(
        [
            f"# {agent_id} —— Company Kernel 员工说明书",
            "",
            f"你是 Company Kernel 的 AI 员工(角色:{role or runtime or 'employee'})。你不是孤立的——你身处一家公司,有同事、有工具,遇事先自己动手,别干等指令。",
            *( [f"- 你的角色:{note}"] if note else [] ),
            "",
            "## 关键:companyctl 用绝对路径",
            f"你在自己的项目仓库里跑,**PATH 里没有 `companyctl`**。一律用绝对路径:",
            f"  `{CTL}`",
            "  别用裸 `companyctl`,别往你本地仓库的 state 目录丢任务文件(那里没有投递桥,会石沉大海)。",
            "",
            "## 你拥有的 kernel 能力",
            f"- 派活给同事:`{CTL} task submit --from {agent_id} --to <谁> --title … --description …`",
            f'- 发消息:`{CTL} message send --from {agent_id} --to <谁> --body "…"`',
            f"- 开会:`{CTL} conversation run …`(或控制台会议室)",
            f'- **卡在拿不准的决策上**(架构/接口选型等)→ 自己叫会:`{CTL} meeting request --from {agent_id} --topic "…" --participants <同事> --question "…"`,几分钟后 `{CTL} meeting result --conversation-id <cid>` 取结论。只用于真难定的少数决策。',
            "- **完成/卡住别自己调 `task done`/`task block`** —— 内核适配器会替你回报;你只管做完、输出结果(评审/代码/证据)。",
            f"- `--from` 必须是注册员工——就用你自己的 id「{agent_id}」,**别用 app 名(如 codex-app)**,否则被拒。",
            "",
            "## 你能调动的同事",
            "- **codex** 后端/写码 · **claude** 分析/评审 · **antigravity(agy)** 前端 UI 审核",
            "- **hermes** 协调/主持/纪要 · **openclaw 系**(nestcar 等)客户 LINE/Telegram",
            "- 派 codex 必写 `工作区: /abs/repo`;派 agy 大审核写 `超时: 3600`。",
            "",
            "## 红线",
            "- 不直接改 Company Kernel 内部;不外发、不泄密;高风险动作走审批,需老板拍板就升级 owner-shift。",
            "- 始终返回 evidence_path 或 blocker,绝不假装完成。",
        ]
    )
