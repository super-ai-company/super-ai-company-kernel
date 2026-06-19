"""Reconcile every employee's status against REALITY: only agents that can actually do
work stay 'active'; everything else is demoted to 'candidate' with a concrete reason.

Probe strategy per runtime:
  - human            : always active (the owner)
  - cli runtimes     : run a real verify-direct round-trip (codex/hermes/claude/trae);
                       pass -> active, fail -> candidate + reason (CLI missing / not
                       logged in / no reply)
  - openclaw agents  : active only if registered in the OpenClaw runtime inventory
  - antigravity      : GUI-only -> candidate (cannot work autonomously)

Run on the Mac:  python3 -m company_kernel.reconcile_status
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(os.environ.get("OPENCLAW_COMPANY_KERNEL_ROOT", Path(__file__).resolve().parents[1])).resolve()
CTL = ROOT / "bin" / "companyctl"
REPORT = ROOT / "state" / "status-reconcile-report.txt"

CLI_RUNTIMES = {"codex": "codex", "hermes": "hermes", "claude": "claude", "trae": "trae"}
GUI_ONLY_RUNTIMES = {"antigravity"}  # the GUI app can't work autonomously -> archived, never online
# …but a GUI runtime can have a HEADLESS CLI twin that CAN (e.g. antigravity's `agy --print`). Map the
# runtime to (twin employee id, its CLI binary); that twin is verified+activated like a CLI runtime
# instead of being archived. Without this, the working headless twin gets wrongly demoted/offline.
GUI_CLI_TWIN = {"antigravity": ("agy", "agy")}

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


def list_employees() -> list[dict]:
    _, d = ctl(["employee", "list"])
    return d.get("employees", [])


def openclaw_registered() -> set:
    _, d = ctl(["doctor"])
    inv = (((d.get("health") or d).get("openclaw_guard") or {}).get("runtime_inventory") or {})
    return {a for a in inv.get("registered_employee_ids", [])}


def verify_reason(results: list[dict]) -> str:
    for r in results:
        resp = r.get("response", {}) if isinstance(r, dict) else {}
        reply = str(resp.get("reply") or "")
        if "not logged in" in reply.lower() or "/login" in reply.lower():
            return "CLI 未登录（需要登录账号）"
        if resp.get("error"):
            return f"探测错误: {str(resp['error'])[:60]}"
        if not reply.strip():
            return "CLI 无响应（未配置/未登录）"
    return "真实通信验证未通过"


def demote(agent: str, reason: str) -> None:
    ctl(["employee", "set-unavailable", "--id", agent, "--reason", reason])


def reconcile_one(emp: dict, oc_registered: set) -> tuple[str, str]:
    agent, runtime = emp["id"], emp.get("runtime", "")
    if runtime == "human":
        return "active", "human owner"

    if runtime in CLI_RUNTIMES:
        cli = CLI_RUNTIMES[runtime]
        if not shutil.which(cli):
            demote(agent, f"{cli} CLI 未安装")
            return "candidate", f"{cli} CLI 未安装"
        code, d = ctl(["employee", "verify-direct", "--id", agent, "--from", "main",
                       "--rounds", "2", "--activate", "--continue-on-failure"])
        if d.get("activation_allowed") or d.get("activated"):
            return "active", "verify-direct 真实通过"
        reason = verify_reason(d.get("results", []))
        demote(agent, reason)
        return "candidate", reason

    if runtime in GUI_ONLY_RUNTIMES:
        # The headless CLI twin (e.g. agy for antigravity) CAN work autonomously — if its CLI is
        # installed, verify+activate it like a CLI runtime instead of archiving it.
        twin = GUI_CLI_TWIN.get(runtime)
        if twin and agent == twin[0] and shutil.which(twin[1]):
            code, d = ctl(["employee", "verify-direct", "--id", agent, "--from", "main",
                           "--rounds", "2", "--activate", "--continue-on-failure"])
            if d.get("activation_allowed") or d.get("activated"):
                return "active", "headless CLI 双胞胎 verify-direct 真实通过"
            reason = verify_reason(d.get("results", []))
            demote(agent, reason)
            return "candidate", reason
        # the GUI app itself can't work autonomously -> archive so it never shows as online.
        ctl(["employee", "update", "--id", agent, "--status", "archived"])
        return "archived", "GUI-only：已下线（不作为在线员工）"

    if runtime == "openclaw":
        # business agents run inside OpenClaw; real = registered in OpenClaw inventory
        rid = agent.replace("-", "_")
        if agent in oc_registered or rid in oc_registered:
            return "active", "OpenClaw 已注册"
        demote(agent, "OpenClaw 运行时未注册")
        return "candidate", "OpenClaw 未注册"

    # unknown runtime
    demote(agent, f"未知运行时 {runtime}")
    return "candidate", f"未知运行时 {runtime}"


def main() -> int:
    log("============ 员工真实状态校正 ============")
    log(f"时间: {datetime.now().isoformat(timespec='seconds')}")
    log("规则: 只有真实探测通过的才保留 active；其余降级 candidate 并写明原因。\n")
    oc = openclaw_registered()
    active, candidate, archived = [], [], []
    for emp in list_employees():
        status, reason = reconcile_one(emp, oc)
        icon = {"active": "✅", "archived": "🗑"}.get(status, "⏸")
        log(f"  {icon} {emp['id']:<16} [{emp.get('runtime',''):<11}] → {status:<10} {reason}")
        {"active": active, "archived": archived}.get(status, candidate).append(emp["id"])
    log("\n============ 结果 ============")
    log(f"✅ 真能干活（active, {len(active)}）: {', '.join(active)}")
    log(f"⏸  暂不可用（candidate, {len(candidate)}）: {', '.join(candidate)}")
    log(f"🗑 已下线（archived, {len(archived)}）: {', '.join(archived)}")
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"\n报告: {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
