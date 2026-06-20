# conn 解耦契约(草案 v1,claude-cli 起草,待 codex/gemini 审)

路线会 conv-20260620-171433-801d08 定的硬条款,落成可实现接口。**本草案因 codex worker 被占(-9)由 claude-cli 代起草,须经 codex 终审 + gemini 泄漏/回滚验收。**

## 1. 现状(为什么要解耦)
- `companyctl.connect()` 每次开新连接 + 跑 `executescript(SCHEMA)` + 迁移 + sync_backlog,**重**;连接记进全局 `_OPEN_CONNECTIONS`,靠调用方或 `close_open_connections()` 关。
- `DB_PATH` 是模块全局,被测试大量 `mock.patch.object(companyctl, "DB_PATH")`(锚,不能破)。
- 1.4 万行编排函数把 `conn` 当参数到处传,耦合重。

## 2. 新接口(core/connection.py,叶子,不反向 import companyctl)
```python
def resolve_db_path() -> str           # 实时读"当前 DB 路径",经 path provider 注入(兼容 companyctl.DB_PATH mock)
def get_connection(*, readonly=False) -> sqlite3.Connection   # 短连接,即用即关由上下文管理器负责
@contextmanager
def read_connection() -> Iterator[Connection]   # 只读短连接,退出必关
@contextmanager
def transaction() -> Iterator[Connection]       # 写:BEGIN→yield→commit;异常 rollback;退出必关
```

## 3. 硬条款(逐条可验)
1. **path provider 注入**:connection.py 不直接读 companyctl.DB_PATH(否则反向依赖)。companyctl 在 import 后调 `connection.set_path_provider(lambda: DB_PATH)` 注入;provider 每次实时取值 → `mock.patch.object(companyctl,"DB_PATH")` 仍命中。
2. **短连接即用即关**:`read_connection`/`transaction` 用 `with`,退出无论正常/异常都 `conn.close()`。不缓存、不长持。
3. **事务显式**:写路径必须 `with transaction() as conn:`,自动 commit/rollback;禁裸 `conn.execute` 后忘 commit。
4. **崩溃回滚**:`transaction()` 块内抛异常 → rollback,不留半写(gemini 崩溃注入验)。
5. **连接归零**:命令结束 `connection_leak_guard()` 计数必须回 0(e2e 基线工具验)。
6. **不碰 DB_PATH 全局本身**:本阶段 DB_PATH 仍留 companyctl 当锚,只是 connection.py 通过 provider 读它,不搬不删。

## 4. 第一小块(worker 状态查询,读多写少,爆炸半径最小)
选一条现在 `conn=connect();...;conn.close()` 的【只读 worker 状态】路径,改成 `with read_connection() as conn:`。证明:① provider 注入后 DB_PATH mock 兼容 ② 短连接归零 ③ 行为不变(过 e2e 基线 + 642 单测零改动)。**不动写路径、不动别的命令。**

## 5. 准入四闸(每小块)
DB_PATH mock 兼容 / 事务上下文(写路径)/ 崩溃回滚 / 连接归零。缺一不进。
