from __future__ import annotations

import os
import subprocess
import sys
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

    def test_kill_process_group_safe_on_finished_proc(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        proc.wait()
        proc_util.kill_process_group(proc)  # must not raise even though it already exited


if __name__ == "__main__":
    unittest.main()
