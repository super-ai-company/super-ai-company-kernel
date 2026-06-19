"""Guided first-run setup: `companyctl init`.

Turns "clone → working company" into one guided flow for a cold downloader:
  1. environment check (Python, OS, ROOT)
  2. ensure the human owner exists
  3. detect which agent CLIs are actually installed on this machine (codex / claude / gemini / trae)
  4. offer to add each detected runtime as an employee (its `<runtime>-cli` worker twin)
  5. print the OS-specific daemon install command + how to open the console
  6. a final doctor smoke

Safe by default: detection + proposals only. It never enables autonomous execution (`--execute`)
or installs system services unless the operator explicitly opts in. Idempotent — re-runnable.
Pure stdlib, cross-platform (macOS / Linux / Windows).
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("OPENCLAW_COMPANY_KERNEL_ROOT", Path(__file__).resolve().parents[1])).resolve()
CTL = ROOT / "bin" / "companyctl"
ADD_EMPLOYEE = ROOT / "bin" / "company-add-employee"

# runtime -> the CLI binary that proves it's installed on this machine. GUI-only runtimes
# (antigravity) and internal ones (hermes/openclaw/local/skill) are not auto-detected here.
RUNTIME_CLI = {
    "codex": "codex",     # Codex CLI
    "claude": "claude",   # Claude Code CLI
    "gemini": "gemini",   # Gemini CLI
    "trae": "trae",       # Trae agent CLI
}

MIN_PY = (3, 9)


def _say(msg: str = "") -> None:
    print(msg, flush=True)


def _ask_yes(prompt: str, *, assume_yes: bool, default: bool = True) -> bool:
    """y/n prompt. In --yes or non-interactive (no TTY) mode, returns the default."""
    if assume_yes or not sys.stdin.isatty():
        return default
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        ans = input(prompt + suffix).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return default
    if not ans:
        return default
    return ans in ("y", "yes")


def _ask_text(prompt: str, default: str, *, assume_yes: bool) -> str:
    if assume_yes or not sys.stdin.isatty():
        return default
    try:
        ans = input(f"{prompt} [{default}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return default
    return ans or default


def _run_ctl(args: list[str]) -> tuple[int, str]:
    try:
        cp = subprocess.run([str(CTL), *args], cwd=str(ROOT), capture_output=True, text=True, timeout=120)
        return cp.returncode, (cp.stdout or cp.stderr or "").strip()
    except Exception as exc:  # noqa: BLE001
        return 1, str(exc)


def _detect_runtimes() -> dict[str, str]:
    """{runtime: absolute path to its CLI} for every agent CLI found on PATH."""
    found = {}
    for runtime, binary in RUNTIME_CLI.items():
        path = shutil.which(binary)
        if path:
            found[runtime] = path
    return found


def _daemon_install_hint() -> str:
    system = platform.system()
    if system == "Darwin":
        return f"{ROOT}/bin/company-daemon-install-launchd   # macOS (launchd)"
    if system == "Linux":
        return f"{ROOT}/bin/company-daemon --once --summary   # run a tick; for systemd see docs/AGENT_ONBOARDING.md"
    if system == "Windows":
        return "python -m company_kernel.company_daemon --once --summary   # Task Scheduler: see docs/AGENT_ONBOARDING.md"
    return f"{ROOT}/bin/company-daemon --once --summary"


def run_init(args: argparse.Namespace) -> int:
    assume_yes = bool(getattr(args, "yes", False))
    enable_execute = bool(getattr(args, "execute", False))
    dry_run = bool(getattr(args, "dry_run", False))

    _say("\n=== Company Kernel · 引导式初始化 / guided init ===\n")

    # 1) environment
    pyok = sys.version_info >= MIN_PY
    _say(f"  Python      : {platform.python_version()}  {'✓' if pyok else '✗ 需要 ≥ %d.%d' % MIN_PY}")
    _say(f"  OS          : {platform.system()} {platform.machine()}")
    _say(f"  ROOT        : {ROOT}")
    if "OPENCLAW_COMPANY_KERNEL_ROOT" not in os.environ:
        _say(f"  提示        : 建议 `export OPENCLAW_COMPANY_KERNEL_ROOT=\"{ROOT}\"`(否则需在仓库根目录运行)")
    if not pyok:
        _say("\n✗ Python 版本过低,先升级再继续。")
        return 2

    # 2) owner
    _say("\n[1/4] 确保人类 owner 存在 …")
    if not dry_run:
        code, _ = _run_ctl(["employee", "ensure-owner"])
        if code != 0:
            # fall back: ensure-owner may not exist on older builds; owner is auto-created lazily.
            _say("  (owner 将在首次操作时自动创建)")
    _say("  owner ✓")

    # 3) detect + add runtimes
    _say("\n[2/4] 检测本机已安装的 agent CLI …")
    found = _detect_runtimes()
    if not found:
        _say("  未检测到 codex / claude / gemini / trae 任一 CLI。")
        _say("  装好其中之一后重跑 `companyctl init`,或手动 `bin/company-add-employee …`。")
    else:
        for rt, path in found.items():
            _say(f"  ✓ {rt:8s} → {path}")
        _say("")
        default_ws = _ask_text("  这些员工执行任务的默认工作区(你的代码仓库路径)",
                               str(Path.home()), assume_yes=assume_yes)
        for rt in found:
            emp_id = f"{rt}-cli"
            if not _ask_yes(f"  添加员工 {emp_id}(runtime={rt})?", assume_yes=assume_yes):
                continue
            cmd = [str(ADD_EMPLOYEE), "--id", emp_id, "--name", f"{rt.capitalize()} CLI",
                   "--role", "developer", "--runtime", rt, "--workspace", default_ws, "--enable-worker"]
            if enable_execute:
                cmd.append("--execute")
            if dry_run:
                _say(f"    [dry-run] {' '.join(cmd)}")
                continue
            try:
                cp = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=180)
                ok = cp.returncode == 0
            except Exception as exc:  # noqa: BLE001
                ok = False
                cp = None
            _say(f"    {'✓ 已添加' if ok else '✗ 失败'} {emp_id}"
                 + ("" if ok else f" — {(cp.stderr if cp else '').strip()[:160]}"))
        if not enable_execute:
            _say("\n  注:员工已注册但**未开启自动执行**(安全默认)。验证无误后给 worker 加 --execute,"
                 "\n      或重跑 `companyctl init --execute`。")

    # 4) services + console + smoke
    _say("\n[3/4] 启动与服务")
    _say(f"  开控制台 : {ROOT}/bin/company-api-gateway --port 8765   → http://127.0.0.1:8765/")
    _say(f"  装守护   : {_daemon_install_hint()}")

    _say("\n[4/4] 自检 doctor …")
    if not dry_run:
        code, out = _run_ctl(["doctor", "--summary"])
        try:
            d = json.loads(out)
            _say(f"  doctor ok={d.get('ok')}  员工={d.get('counts', {}).get('employees', '?')}"
                 + (f"  问题={d.get('issues')}" if d.get("issues") else ""))
        except Exception:  # noqa: BLE001
            _say(f"  {out[:200]}")
    else:
        _say("  [dry-run] 跳过")

    _say("\n✅ 初始化引导完成。下一步:打开控制台、发个 smoke 任务、或发起一场会议。")
    _say("   文档:docs/AGENT_ONBOARDING.md\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="companyctl init", description="Guided first-run setup")
    p.add_argument("--yes", action="store_true", help="non-interactive: auto-accept detected runtimes")
    p.add_argument("--execute", action="store_true", help="also enable autonomous execution on added workers (off by default)")
    p.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    return p


def main(argv: list[str] | None = None) -> int:
    return run_init(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
