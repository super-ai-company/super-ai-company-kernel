"""Subprocess helpers that kill the WHOLE process tree on timeout.

`subprocess.run(cmd, timeout=...)` only SIGKILLs the direct child. The kernel's worker CLIs
(`codex exec`, `claude -p`, `agy --print`) are node shells that spawn engine subprocesses; when
the timeout only kills the shell, the engine grandchildren are orphaned and keep running. Because
the daemon runs each worker synchronously inside one `--once` tick, a single orphaned run blocks
the tick forever — which freezes every employee's heartbeat (the whole company looks "off duty").

Running the child in its own process group (start_new_session) and killing the GROUP on timeout
fixes this: the entire codex/claude/agy tree dies, the tick returns, heartbeats resume.
"""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess


def kill_process_group(proc: subprocess.Popen) -> None:
    """SIGTERM then SIGKILL the child's whole process group, then reap it. Safe if already gone."""
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        with contextlib.suppress(Exception):
            proc.kill()
            proc.wait(timeout=5)
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=5)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=5)


def run_with_group_timeout(cmd, *, timeout, **kwargs) -> subprocess.CompletedProcess:
    """Drop-in for subprocess.run(cmd, timeout=...) that runs the child in its OWN process group and,
    on timeout, kills the whole group before re-raising subprocess.TimeoutExpired. Works for both
    captured output (stdout=PIPE) and file-redirected output. A timeout of 0/None means no limit."""
    kwargs.setdefault("start_new_session", True)
    # translate subprocess.run's convenience kwarg, which Popen doesn't accept
    if kwargs.pop("capture_output", False):
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)
    proc = subprocess.Popen(cmd, **kwargs)
    try:
        stdout, stderr = proc.communicate(timeout=timeout or None)
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        kill_process_group(proc)
        with contextlib.suppress(Exception):
            proc.communicate(timeout=5)  # drain pipes so the reaped child leaves nothing behind
        raise
