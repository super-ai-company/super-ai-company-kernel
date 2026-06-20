# CLOSURE/e2e-baseline — conn 解耦前的 e2e 集成基线(第一棒,claude-cli)

路线会 conv-20260620-171433-801d08 定:1.4 万行带状态拆分前必须先有客观集成回归网(637 单测多为单元)。本基线 = `tests/test_e2e_baseline.py`。

## 覆盖关键路径(真实临时库 + env 隔离 reload)
- `test_task_lifecycle` — 任务 submit→claim→done 状态机
- `test_concurrent_claim_exactly_one_winner` — 8 线程并发抢单,断言【恰好一个 worker 赢】(原子 UPDATE ... WHERE status='submitted')
- `test_worker_heartbeat_on_duty` — 心跳写入 + heartbeat_age_minutes 在岗判定
- `test_api_three_endpoints` — /v1/health · /v1/cost-dashboard · /v1/economics 路由层返回 200 + 结构键(锁 shape 非系统健康)

## 连接泄漏断言工具(release-gate 基础)
`connection_leak_guard()` 上下文管理器:经 Connection 子类 factory 计数 net 开/关连接,断言命令结束后【空闲连接归零】+ peak≥1。这是 conn 解耦 release gate「空闲连接=0」的度量工具,后续每个 slice 用它证明不泄漏/不长持连接。

## 基线指标(冻结对照)
- 5 e2e 用例全绿;并入后全套 642 测试绿(637 单测零改动 + 5 e2e)。
- 断言只锁结构化状态(退出码/JSON shape/DB 行/计数),文本只锁关键字段。

## 串行流水线下一棒
codex 的 conn 契约 + worker 状态草案 PR 现可开工(本基线为其提供参照与回归网)。每个 slice 准入:DB_PATH mock 兼容 / 事务上下文 / 崩溃回滚 / 连接归零(用本工具验)。
