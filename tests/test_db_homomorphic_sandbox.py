"""
同态回放靶场 (Homomorphic Replay Sandbox)
用途：在 core/db.py 下沉重构过程中提供基线断言。
要求 Codex：重构完毕后，将本文件的 `companyctl` 导入切换为 `core.db`，
确保连过本沙盒（零噪点、I/O特征一致、读写回滚全命中）。
"""

import sqlite3
import re
import tempfile
import pathlib
import contextlib
import sys
import io
import unittest
from unittest.mock import patch

# 此时导的是旧版的实现（companyctl），重构后 Codex 可将其切换为 core.db
from company_kernel.companyctl import (
    connect,
    connect_readonly,
    close_open_connections,
    rows,
    _OPEN_CONNECTIONS,
    SCHEMA
)
import company_kernel.companyctl as companyctl_module

def desensitize_io(text: str) -> str:
    """I/O 脱敏正则：抹平时间戳、内存地址、进程号、临时路径等"""
    if not isinstance(text, str):
        return str(text)
    # 临时路径 / 内存地址 / trace_id
    text = re.sub(r'/tmp/[a-zA-Z0-9_-]+', '/tmp/XXX', text)
    text = re.sub(r'/var/folders/[a-zA-Z0-9_/-]+', '/tmp/XXX', text)
    text = re.sub(r'0x[0-9a-fA-F]+', '0xXXXX', text)
    text = re.sub(r'\b(pid|trace_id)[=:]\s*\d+\b', r'\1=XXXX', text)
    text = re.sub(r'trace-[0-9a-fA-F-]+', 'trace-XXXX', text)
    # 时间戳 (ISO / epoch)
    text = re.sub(r'\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?', 'YYYY-MM-DDTHH:MM:SSZ', text)
    text = re.sub(r'\b1[678]\d{8}(?:\.\d+)?\b', 'EPOCH_TIME', text)
    return text

class TestDBHomomorphicSandbox(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.fake_db = pathlib.Path(self.td.name) / "sandbox.db"
        
        self.patcher_db_path = patch.object(companyctl_module, "DB_PATH", self.fake_db)
        self.patcher_db_path.start()
        
        # 清空已有连接
        self.patcher_open_conns = patch.object(companyctl_module, "_OPEN_CONNECTIONS", [])
        self.patcher_open_conns.start()
        
        self.captured_out = io.StringIO()
        self.captured_err = io.StringIO()
        
        self.sync_called = []
        def mock_sync_backlog(conn):
            self.sync_called.append(True)
        self.patcher_sync = patch.object(companyctl_module, "sync_backlog_from_queue_file", mock_sync_backlog)
        self.patcher_sync.start()

        self.redirect_stdout = contextlib.redirect_stdout(self.captured_out)
        self.redirect_stderr = contextlib.redirect_stderr(self.captured_err)
        self.redirect_stdout.__enter__()
        self.redirect_stderr.__enter__()

    def tearDown(self):
        companyctl_module.close_open_connections()
        self.redirect_stdout.__exit__(None, None, None)
        self.redirect_stderr.__exit__(None, None, None)
        self.patcher_db_path.stop()
        self.patcher_open_conns.stop()
        self.patcher_sync.stop()
        self.td.cleanup()

    def test_db_sandbox_seed_injection(self):
        """Seed 注入(读/写/事务/空结果/异常路径) 及同态基线"""
        # 1. 初始化
        conn1 = companyctl_module.connect()
        self.assertTrue(self.fake_db.exists(), "connect() 应该创建了数据库")
        self.assertTrue(self.sync_called, "业务函数 sync_backlog 被正常触发")
        
        # 2. 注入测试 Seed，正常读写事务
        conn1.execute("CREATE TABLE IF NOT EXISTS sandbox_seed (id INTEGER PRIMARY KEY, val TEXT)")
        conn1.execute("INSERT INTO sandbox_seed (val) VALUES (?)", ("test_val_1",))
        conn1.commit()
        
        # 3. 正常读 (通过 rows)
        res = companyctl_module.rows(conn1, "SELECT * FROM sandbox_seed WHERE val=?", ("test_val_1",))
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["val"], "test_val_1")
        
        # 4. 空结果路径
        res_empty = companyctl_module.rows(conn1, "SELECT * FROM sandbox_seed WHERE val=?", ("not_exist",))
        self.assertEqual(res_empty, [])
        
        # 5. 只读连接测试 (connect_readonly)
        conn_ro = companyctl_module.connect_readonly()
        res_ro = companyctl_module.rows(conn_ro, "SELECT * FROM sandbox_seed")
        self.assertEqual(len(res_ro), 1)
        with self.assertRaises(sqlite3.Error):
            # 只读连接不允许写
            conn_ro.execute("INSERT INTO sandbox_seed (val) VALUES ('test_ro')")
        
        # 6. 事务回滚 Seed 模板
        try:
            conn1.execute("BEGIN TRANSACTION")
            conn1.execute("INSERT INTO sandbox_seed (val) VALUES ('will_rollback')")
            # 抛出通用异常模拟出错
            raise ValueError("Simulated Exception")
        except ValueError:
            conn1.rollback()
            
        res_after_rollback = companyctl_module.rows(conn1, "SELECT * FROM sandbox_seed WHERE val=?", ("will_rollback",))
        self.assertEqual(len(res_after_rollback), 0, "回滚后不应该读到数据")

        # 7. 全量异常模拟及输出指纹比对
        with self.assertRaises(sqlite3.OperationalError) as cm:
            companyctl_module.rows(conn1, "SELECT * FROM non_existent_table")
        
        err_msg = desensitize_io(str(cm.exception))
        self.assertIn("no such table", err_msg.lower())
        
        # 收集整体 I/O 指纹
        out_str = desensitize_io(self.captured_out.getvalue())
        err_str = desensitize_io(self.captured_err.getvalue())
        
        # 确保没有意外的日志噪点
        self.assertNotIn("error", out_str.lower())
        
        # 8. 连接池生命周期验证
        open_conns = getattr(companyctl_module, "_OPEN_CONNECTIONS")
        self.assertEqual(len(open_conns), 2, "此时应有 conn1 和 conn_ro 在池中")
        
        companyctl_module.close_open_connections()
        self.assertEqual(len(open_conns), 0, "连接应全部清理干净")

if __name__ == '__main__':
    unittest.main()
