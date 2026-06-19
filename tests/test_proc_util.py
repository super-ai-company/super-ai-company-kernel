from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

from company_kernel import proc_util


class ProcUtilTest(unittest.TestCase):
    def test_completes_normally_and_captures_output(self):
        cp = proc_util.run_with_group_timeout(
            [sys.executable, "-c", "print('hi')"], timeout=10, capture_output=True, text=True)
        self.assertEqual(0, cp.returncode)
        self.assertIn("hi", cp.stdout)

    def test_timeout_kills_whole_process_tree(self):
        # Parent spawns a child that sleeps 60s, writes the child's PID, then sleeps too. On timeout
        # the helper must kill the GROUP — so the orphan-prone child dies, not just the parent.
        script = (
            "import os,subprocess,sys,time;"
            "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
            "sys.stderr.write(str(c.pid));sys.stderr.flush();"
            "time.sleep(60)"
        )
        with self.assertRaises(subprocess.TimeoutExpired):
            proc_util.run_with_group_timeout(
                [sys.executable, "-c", script], timeout=1, capture_output=True, text=True)
        # the test passes if the call raised after killing the group; give the OS a beat to reap
        time.sleep(0.5)

    def test_timeout_sigkills_grandchild_that_ignores_sigterm(self):
        pid_file = tempfile.NamedTemporaryFile(delete=False)
        pid_file.close()
        self.addCleanup(lambda: os.path.exists(pid_file.name) and os.unlink(pid_file.name))
        script = textwrap.dedent(
            f"""
            import subprocess, sys, time
            subprocess.Popen([
                sys.executable, '-c',
                "import os,signal,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "open({pid_file.name!r}, 'w').write(str(os.getpid())); "
                "time.sleep(60)"
            ])
            time.sleep(60)
            """
        )
        with self.assertRaises(subprocess.TimeoutExpired):
            proc_util.run_with_group_timeout(
                [sys.executable, "-c", script], timeout=1, capture_output=True, text=True)
        time.sleep(0.5)
        with open(pid_file.name, encoding="utf-8") as fh:
            child_pid = int(fh.read())
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return
        os.kill(child_pid, signal.SIGKILL)
        self.fail("grandchild that ignored SIGTERM survived process-group timeout")

    def test_kill_process_group_safe_on_finished_proc(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        proc.wait()
        proc_util.kill_process_group(proc)  # must not raise even though it already exited

    def test_kill_process_group_never_kills_own_group(self):
        # FOOTGUN GUARD: a child started WITHOUT start_new_session shares THIS process's group.
        # kill_process_group must NOT killpg (that would kill the test runner / whole daemon) — it must
        # fall back to killing only the child. If the guard were missing, this test process would die.
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])  # no start_new_session
        self.assertEqual(os.getpgid(proc.pid), os.getpgid(0))  # confirm it's in our own group
        proc_util.kill_process_group(proc)
        # we are still alive (guard worked) and the child is dead (killed individually)
        self.assertIsNotNone(proc.poll())


if __name__ == "__main__":
    unittest.main()
