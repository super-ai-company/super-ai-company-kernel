"""Guided first-run setup: `companyctl init`.

Turns "clone → working company" into one guided flow for a cold downloader:
  1. environment check (Python, OS, ROOT)
  2. ensure the human owner exists
  3. detect which agent CLIs are actually installed on this machine (codex / claude / gemini / trae)
  4. offer to REGISTER each detected runtime as an employee (its `<runtime>-cli` worker twin)
  5. print the OS-specific daemon install command + how to open the console
  6. a final doctor smoke

Safe by default:
  * Registration only — it does NOT enable the daemon worker (no `config/daemon.json` write) and
    does NOT enable autonomous execution. Those happen only with `--execute`.
  * Interactive prompts. With no TTY and without `--yes`, it prints the PLAN and changes nothing.
  * Idempotent — re-runnable. Any failed step makes `init` exit non-zero (no false "done").

Cross-platform: on Windows run `python -m company_kernel.companyctl init` (the bash `bin/` shims are
POSIX-only). Pure stdlib, no third-party dependencies.
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

# runtime -> the CLI binary that proves it's installed. Keys MUST be a subset of companyctl's
# KNOWN_RUNTIMES (asserted below) so this can't silently drift. GUI-only runtimes (antigravity) and
# internal ones (hermes / openclaw / local / skill) are intentionally NOT auto-detected here.
RUNTIME_CLI = {
    "codex": "codex",     # Codex CLI
    "claude": "claude",   # Claude Code CLI
    "gemini": "gemini",   # Gemini CLI
    "trae": "trae",       # Trae agent CLI
}

MIN_PY = (3, 9)


def _known_runtimes() -> set[str]:
    try:
        from company_kernel import companyctl
        return set(companyctl.KNOWN_RUNTIMES)
    except Exception:  # noqa: BLE001
        return set(RUNTIME_CLI)


# fail fast at import if the detection table drifts away from the canonical runtime registry
assert set(RUNTIME_CLI).issubset(_known_runtimes()), "RUNTIME_CLI drifted from KNOWN_RUNTIMES"


def _say(msg: str = "") -> None:
    print(msg, flush=True)


def _interactive() -> bool:
    return sys.stdin.isatty()


def _confirm(prompt: str, *, assume_yes: bool, default: bool = True) -> bool:
    """Ask to perform a MUTATION. --yes → yes. No TTY and no --yes → no (plan-only, never mutate)."""
    if assume_yes:
        return True
    if not _interactive():
        return False
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        ans = input(prompt + suffix).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return default if not ans else ans in ("y", "yes")


def _ask_text(prompt: str, default: str, *, assume_yes: bool) -> str:
    if assume_yes or not _interactive():
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
    return {rt: path for rt, binary in RUNTIME_CLI.items() if (path := shutil.which(binary))}


def _daemon_install_hint() -> str:
    system = platform.system()
    if system == "Darwin":
        return f"{ROOT}/bin/company-daemon-install-launchd   # macOS (launchd)"
    if system == "Linux":
        return f"{ROOT}/bin/company-daemon --once --summary   # one tick; for systemd see docs/AGENT_ONBOARDING.md"
    if system == "Windows":
        return "python -m company_kernel.company_daemon --once --summary   # Task Scheduler: see docs/AGENT_ONBOARDING.md"
    return f"{ROOT}/bin/company-daemon --once --summary"


def _console_hint() -> str:
    if platform.system() == "Windows":
        return "python -m company_kernel.api_gateway --port 8765   → http://127.0.0.1:8765/"
    return f"{ROOT}/bin/company-api-gateway --port 8765   → http://127.0.0.1:8765/"


def run_init(args: argparse.Namespace) -> int:
    assume_yes = bool(getattr(args, "yes", False))
    enable_execute = bool(getattr(args, "execute", False))
    dry_run = bool(getattr(args, "dry_run", False))
    plan_only = dry_run or (not assume_yes and not _interactive())  # no TTY + no --yes → never mutate
    errors: list[str] = []

    _say("\n=== Company Kernel · 引导式初始化 / guided init ===")
    if plan_only and not dry_run:
        _say("(无交互终端且未加 --yes:仅打印计划,不做任何改动。要执行请加 --yes)")
    _say("")

    # 1) environment
    pyok = sys.version_info >= MIN_PY
    _say(f"  Python      : {platform.python_version()}  {'✓' if pyok else '✗ 需要 ≥ %d.%d' % MIN_PY}")
    _say(f"  OS          : {platform.system()} {platform.machine()}")
    _say(f"  ROOT        : {ROOT}")
    if "OPENCLAW_COMPANY_KERNEL_ROOT" not in os.environ:
        _say(f"  提示        : 建议 `export OPENCLAW_COMPANY_KERNEL_ROOT=\"{ROOT}\"`(否则需在仓库根目录运行)")
    if platform.system() == "Windows":
        _say("  Windows     : 入口用 `python -m company_kernel.companyctl init`(bin/ 下是 POSIX 脚本)")
    if not pyok:
        _say("\n✗ Python 版本过低,先升级再继续。")
        return 2

    # 2) owner
    _say("\n[1/4] 确保人类 owner 存在 …")
    if plan_only:
        _say("  [计划] companyctl employee ensure-owner")
    else:
        code, out = _run_ctl(["employee", "ensure-owner"])
        if code == 0:
            _say("  owner ✓")
        else:
            errors.append("ensure-owner")
            _say(f"  ✗ owner 创建失败:{out[:160]}")

    # 3) detect + register runtimes
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
        mode = "注册并启用 worker + 自动执行" if enable_execute else "仅注册(不启用 worker / 不自动执行)"
        _say(f"  添加方式:{mode}" + ("" if enable_execute else " —— 验证后再加 --execute 或手动 --enable-worker"))
        for rt in found:
            emp_id = f"{rt}-cli"
            cmd = [str(ADD_EMPLOYEE), "--id", emp_id, "--name", f"{rt.capitalize()} CLI",
                   "--role", "developer", "--runtime", rt, "--workspace", default_ws]
            if enable_execute:
                cmd += ["--enable-worker", "--execute"]   # only --execute opts into config write + autonomy
            if plan_only:
                _say(f"  [计划] {' '.join(cmd)}")
                continue
            if not _confirm(f"  添加员工 {emp_id}(runtime={rt})?", assume_yes=assume_yes):
                _say(f"  - 跳过 {emp_id}")
                continue
            try:
                cp = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=180)
                ok = cp.returncode == 0
                detail = (cp.stderr or cp.stdout or "").strip()
            except Exception as exc:  # noqa: BLE001
                ok, detail = False, str(exc)
            if ok:
                _say(f"  ✓ 已添加 {emp_id}")
            else:
                errors.append(f"add:{emp_id}")
                _say(f"  ✗ 失败 {emp_id} — {detail[:160]}")

    # 4) services + console + smoke
    _say("\n[3/4] 启动与服务")
    _say(f"  开控制台 : {_console_hint()}")
    _say(f"  装守护   : {_daemon_install_hint()}")

    _say("\n[4/4] 自检 doctor …")
    if plan_only:
        _say("  [计划] companyctl doctor --summary")
    else:
        code, out = _run_ctl(["doctor", "--summary"])
        if code != 0:
            errors.append("doctor")
        try:
            d = json.loads(out)
            _say(f"  doctor ok={d.get('ok')}  员工={d.get('counts', {}).get('employees', '?')}"
                 + (f"  问题={d.get('issues')}" if d.get("issues") else ""))
        except Exception:  # noqa: BLE001
            _say(f"  {out[:200]}")

    if errors:
        _say(f"\n✗ 初始化有 {len(errors)} 步失败:{', '.join(errors)} —— 请修复后重跑(本命令幂等)。")
        return 2
    _say("\n✅ 初始化引导完成。下一步:打开控制台、发个 smoke 任务、或发起一场会议。")
    _say("   文档:docs/AGENT_ONBOARDING.md\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="companyctl init", description="Guided first-run setup")
    p.add_argument("--yes", action="store_true", help="non-interactive: register detected runtimes without prompting")
    p.add_argument("--execute", action="store_true", help="also enable the daemon worker + autonomous execution (writes config; off by default)")
    p.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    return p


def main(argv: list[str] | None = None) -> int:
    return run_init(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
