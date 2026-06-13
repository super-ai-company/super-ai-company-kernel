"""Install-time self-check: run once after install to see EXACTLY what works and what the
operator must fix — so a customer never has to blind-debug the environment issues we hit.

Checks are split into:
  - kernel (code/runtime that the package guarantees)
  - environment (things the operator must provide: CLIs, logins, daemon, ports)

Exit code 0 if no blocking issues. Run:  python3 -m company_kernel.preflight
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get("OPENCLAW_COMPANY_KERNEL_ROOT", Path(__file__).resolve().parents[1])).resolve()
CTL = ROOT / "bin" / "companyctl"
CONFIG = ROOT / "config" / "daemon.json"

checks: list[tuple[str, str, str]] = []  # (level PASS/WARN/FAIL, title, detail)
def add(level: str, title: str, detail: str = "") -> None:
    checks.append((level, title, detail))


def sh(args: list[str]) -> tuple[int, str]:
    try:
        cp = subprocess.run(args, cwd=str(ROOT), text=True, capture_output=True, timeout=60)
        return cp.returncode, cp.stdout
    except Exception as exc:  # noqa
        return 1, str(exc)


def check_python() -> None:
    v = sys.version_info
    if (v.major, v.minor) >= (3, 10):
        add("PASS", "Python", f"{v.major}.{v.minor}.{v.micro}")
    else:
        add("FAIL", "Python", f"需要 3.10+，当前 {v.major}.{v.minor}")


def check_db() -> None:
    code, out = sh([str(CTL), "doctor", "--summary"])
    try:
        d = json.loads(out)
        add("PASS" if code == 0 or d.get("counts") else "WARN", "数据库 / doctor",
            f"employees={d.get('counts',{}).get('employees','?')} issues={d.get('issues',[])}")
    except json.JSONDecodeError:
        add("FAIL", "数据库 / doctor", "doctor 无法运行（检查 OPENCLAW_COMPANY_KERNEL_ROOT）")


def check_gateway() -> None:
    # try common ports
    for port in (8765, 8788):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/health", timeout=3) as r:
                if r.status in (200, 401):
                    add("PASS", "API 网关", f"127.0.0.1:{port} 在线")
                    return
        except Exception:
            continue
    add("WARN", "API 网关", "未检测到（运行 bin/company-api-gateway 或装 launchd）")


def check_daemon() -> None:
    state = ROOT / "state" / "daemon" / "last-run.json"
    if not state.exists():
        add("WARN", "Daemon", "从未运行（装 launchd/systemd，或 bin/company-daemon --once）")
        return
    try:
        import datetime as dt
        last = json.loads(state.read_text())["at"]
        age_min = (dt.datetime.now(dt.timezone.utc).astimezone() - dt.datetime.fromisoformat(last)).total_seconds() / 60
        if age_min < 15:
            add("PASS", "Daemon", f"{int(age_min)} 分钟前运行")
        else:
            add("WARN", "Daemon", f"上次运行 {int(age_min)} 分钟前（可能已停）")
    except Exception:
        add("WARN", "Daemon", "状态文件无法解析")


def check_auth() -> None:
    if os.environ.get("COMPANY_KERNEL_API_TOKEN", "").strip():
        add("PASS", "网关鉴权", "已启用 token（适合对外暴露）")
    else:
        add("WARN", "网关鉴权", "未启用（仅 127.0.0.1 安全；对外暴露前请设 COMPANY_KERNEL_API_TOKEN）")


def check_workers() -> None:
    if not CONFIG.exists():
        add("FAIL", "Worker 配置", "缺 config/daemon.json")
        return
    cfg = json.loads(CONFIG.read_text())
    cli_map = {"company-codex-adapter": "codex", "company-hermes-adapter": "hermes",
               "company-claude-adapter": "claude", "company-trae-adapter": "trae"}
    for w in cfg.get("adapter_workers", []):
        if not w.get("enabled"):
            continue
        agent = w.get("agent", "?")
        cli = cli_map.get(w.get("command", ""), None)
        if cli is None:
            add("PASS", f"worker {agent}", "非 CLI 类（openclaw 桥接）")
            continue
        if shutil.which(cli):
            add("PASS", f"worker {agent}", f"{cli} CLI 已安装（登录状态由 reconcile 校正）")
        else:
            add("FAIL", f"worker {agent}", f"{cli} CLI 未安装 —— 该 worker 无法干活")


def main() -> int:
    print("================ Company Kernel 装机自检 (preflight) ================")
    check_python(); check_db(); check_gateway(); check_daemon(); check_auth(); check_workers()
    fails = [c for c in checks if c[0] == "FAIL"]
    warns = [c for c in checks if c[0] == "WARN"]
    icon = {"PASS": "✅", "WARN": "⚠️ ", "FAIL": "❌"}
    for level, title, detail in checks:
        print(f"  {icon[level]} {title:<16} {detail}")
    print("\n================ 结论 ================")
    if fails:
        print(f"❌ {len(fails)} 项阻断必须修复后才能正常运行：")
        for _, t, d in fails:
            print(f"   - {t}: {d}")
    if warns:
        print(f"⚠️  {len(warns)} 项建议处理（不阻断本机使用）")
    if not fails:
        print("✅ 无阻断项：内核可运行。能干活的员工以 reconcile 报告为准。")
    print("\n提示：员工真实可用状态见 state/status-reconcile-report.txt（daemon 自动校正）")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
