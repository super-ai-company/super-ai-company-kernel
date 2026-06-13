"""Live smoke test: prove each runtime can really do work end-to-end through the kernel.

For every runtime it: checks the CLI/app is available, submits one tiny low-risk task,
runs the adapter with --execute, then reads back the task status + verdict. Writes a
single human-readable report. Honest by design: "CLI not installed" is a real result,
not a failure to hide.

Run on the Mac:  python3 -m company_kernel.live_smoke
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(os.environ.get("OPENCLAW_COMPANY_KERNEL_ROOT", Path(__file__).resolve().parents[1])).resolve()
CTL = ROOT / "bin" / "companyctl"
REPORT = ROOT / "state" / "live-smoke-report.txt"

# runtime -> (agent_id, adapter command, CLI binary to probe, extra adapter args)
RUNTIMES = [
    ("codex",       "codex",       "company-codex-adapter",       "codex",  ["--sandbox", "workspace-write", "--model", "gpt-5.5", "--timeout-seconds", "600"]),
    ("hermes",      "hermes",      "company-hermes-adapter",      "hermes", []),
    ("claude",      "claude",      "company-claude-adapter",      "claude", []),
    ("trae",        "trae",        "company-trae-adapter",        "trae",   []),
    ("openclaw",    "nestcar",     "company-openclaw-adapter",    "openclaw", []),
    ("antigravity", "antigravity", "company-antigravity-adapter", None,     []),  # GUI app, special-cased
]

lines: list[str] = []
def log(s: str = "") -> None:
    print(s)
    lines.append(s)


def ctl(args: list[str]) -> tuple[int, dict]:
    cp = subprocess.run([str(CTL), *args], cwd=str(ROOT), text=True, capture_output=True)
    try:
        return cp.returncode, json.loads(cp.stdout or "{}")
    except json.JSONDecodeError:
        return cp.returncode, {"raw": (cp.stdout or cp.stderr)[-300:]}


def ensure_active(agent: str) -> None:
    ctl(["employee", "update", "--id", agent, "--status", "active"])


def auto_approve(task_id: str) -> None:
    _, d = ctl(["approval", "list", "--status", "pending"])
    for a in d.get("approvals", []):
        if task_id in a.get("id", ""):
            ctl(["approval", "approve", "--approval-id", a["id"], "--by", "owner", "--reason", "live smoke"])


def submit(agent: str, task_id: str, title: str, desc: str) -> bool:
    code, d = ctl(["task", "submit", "--from", "owner", "--to", agent,
                   "--task-id", task_id, "--title", title, "--description", desc, "--priority", "P3"])
    if code != 0 and d.get("approval", {}).get("id"):
        ctl(["approval", "approve", "--approval-id", d["approval"]["id"], "--by", "owner", "--reason", "live smoke"])
        code, d = ctl(["task", "submit", "--from", "owner", "--to", agent,
                       "--task-id", task_id, "--title", title, "--description", desc, "--priority", "P3",
                       "--approval-id", d["approval"]["id"]])
    return code == 0


def smoke(runtime: str, agent: str, adapter: str, cli: str | None, extra: list[str]) -> str:
    log(f"\n──────── {runtime}  (员工: {agent}) ────────")
    # 1. availability
    if runtime == "antigravity":
        avail = Path("/Applications/Antigravity.app").exists()
        where = "/Applications/Antigravity.app" if avail else "(未安装)"
    else:
        path = shutil.which(cli) if cli else None
        avail = bool(path)
        where = path or "(CLI 未安装)"
    log(f"  CLI/App: {where}")
    if not avail:
        log(f"  结果: ⏭  跳过（{cli or 'app'} 不在本机，链路无法实测）")
        return "skipped (no CLI)"

    ensure_active(agent)
    task_id = f"smoke-{runtime}-{datetime.now().strftime('%H%M%S')}"
    title = f"smoke: {runtime} 真实执行确认"
    desc = ("这是一次链路冒烟。请做一件最小的真实动作并确认：在你的工作目录创建文件 "
            f"smoke_{runtime}.txt 写入当前时间戳；然后最后一行输出 STATUS: completed（失败则 STATUS: blocked - 原因）。")
    if not submit(agent, task_id, title, desc):
        log("  结果: ❌ 任务提交失败（可能权限/审批）")
        return "submit failed"

    log(f"  已派任务 {task_id}，运行 {adapter} --execute …")
    exe = ROOT / "bin" / adapter
    args = [str(exe), "--agent", agent, "--execute", *extra]
    cp = subprocess.run(args, cwd=str(ROOT), text=True, capture_output=True, timeout=900)
    auto_approve(task_id)

    _, t = ctl(["task", "show", "--task-id", task_id])
    task = t.get("task", t)
    status = task.get("status", "?")
    summary = (task.get("summary") or task.get("blocker") or "").replace("\n", " ")[:160]
    verdict = "completed" if status == "completed" else status
    icon = "✅" if status == "completed" else ("⛔" if status == "blocked" else "❓")
    log(f"  结果: {icon} 任务状态={status} | {summary}")
    return f"{status}"


def main() -> int:
    log("================ Company Kernel 全员活体冒烟 ================")
    log(f"时间: {datetime.now().isoformat(timespec='seconds')}")
    log("说明: 每条链路检测CLI→派真任务→--execute真跑→读裁决。'skipped'=本机没装该工具。\n")
    results = {}
    for runtime, agent, adapter, cli, extra in RUNTIMES:
        try:
            results[runtime] = smoke(runtime, agent, adapter, cli, extra)
        except subprocess.TimeoutExpired:
            log("  结果: ⛔ 超时（>900s）")
            results[runtime] = "timeout"
        except Exception as exc:  # noqa
            log(f"  结果: ❌ 异常 {exc}")
            results[runtime] = f"error: {exc}"
    log("\n================ 汇总 ================")
    for rt, r in results.items():
        log(f"  {rt:<12} → {r}")
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"\n报告已写入 {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
