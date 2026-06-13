"""Pluggable result verifiers — the trust core of outcome-based delivery.

A task card declares HOW its result is verified via a directive in the description:

    验收: test: pytest -q                 # run a command in the workspace; exit 0 = pass
    验收: numeric: 101062.00              # the agent output must contain this exact value
    验收: artifact: dist/report.pdf       # this file must exist in the workspace
    验收: human                           # always route to human review (never auto-pass)
    (verify: ... works too; default = "status" = trust the agent's STATUS line)

The verifier's result — not the agent's self-report — decides whether a task is truly done.
This is what stops a dishonest or optimistic "STATUS: completed" from getting paid.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

VERIFY_DIRECTIVE = re.compile(r"^\s*(?:验收|verify)\s*[:：]\s*(\w+)\s*(?:[:：]\s*(.*))?$", re.IGNORECASE | re.MULTILINE)


def parse_verifier(description: str) -> tuple[str, str]:
    """Return (kind, arg). Defaults to ('status', '') when no directive is present."""
    m = VERIFY_DIRECTIVE.search(description or "")
    if not m:
        return "status", ""
    kind = (m.group(1) or "status").lower()
    arg = (m.group(2) or "").strip()
    return kind, arg


def verify_result(kind: str, arg: str, *, workspace: Path, output_text: str,
                  agent_verdict: str, timeout: int = 120) -> tuple[str, str]:
    """Run the declared verifier. Returns (result, detail) where result is one of:
       pass / fail / needs_human / error.
    output_text = the agent's final message; agent_verdict = completed/blocked/...
    """
    kind = (kind or "status").lower()

    if kind == "status":
        # trust the agent's explicit verdict (the baseline gate)
        if agent_verdict == "completed":
            return "pass", "agent STATUS: completed (no external verifier declared)"
        return "fail", f"agent verdict = {agent_verdict}"

    if kind == "human":
        return "needs_human", "human verification required by task card; queued for review"

    if kind == "numeric":
        if not arg:
            return "error", "numeric verifier needs an expected value (验收: numeric: <value>)"
        return ("pass", f"expected value {arg!r} found in output") if arg in (output_text or "") \
            else ("fail", f"expected value {arg!r} NOT found in agent output")

    if kind == "artifact":
        if not arg:
            return "error", "artifact verifier needs a path (验收: artifact: <relative/path>)"
        target = (workspace / arg) if not Path(arg).is_absolute() else Path(arg)
        if not target.exists():
            return "fail", f"expected artifact missing: {target}"
        try:
            if target.is_file() and target.stat().st_size == 0:
                return "fail", f"artifact is empty: {target}"
        except OSError:
            pass
        return "pass", f"artifact present: {target}"

    if kind == "test":
        if not arg:
            return "error", "test verifier needs a command (验收: test: <command>)"
        try:
            cp = subprocess.run(arg, cwd=str(workspace), shell=True, text=True,
                                capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return "fail", f"verifier command timed out after {timeout}s: {arg}"
        except Exception as exc:  # noqa
            return "error", f"verifier command could not run: {exc}"
        tail = (cp.stdout or "")[-300:] + (("\n" + cp.stderr[-300:]) if cp.stderr else "")
        if cp.returncode == 0:
            return "pass", f"`{arg}` exit 0\n{tail}".strip()
        return "fail", f"`{arg}` exit {cp.returncode}\n{tail}".strip()

    return "error", f"unknown verifier kind: {kind}"
