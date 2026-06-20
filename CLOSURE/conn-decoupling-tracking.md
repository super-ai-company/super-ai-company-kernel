# CLOSURE/conn-decoupling-tracking — 残留 conn 耦合演进(独立未决工程)

## 范围
companyctl.py 收尾后仍 ~14798 行,主体为吃 `conn: sqlite3.Connection` 的 CLI 命令 + 编排函数(task/meeting/conversation/route/approval-gate/adapter 编排等)。这些**不在本次 core-layer 拆分范围**,降级为独立 tracking。

## 为何不在本轮切
- 继续按"纯叶子"切 ROI 递减(剩余纯叶子稀疏跨主题),且会增加 forward 门面债。
- conn 耦合函数需要先设计 **connection/DB 抽象**(参 gemini set_path/get_connection 接口规范草案)才能干净拆成纯核+壳,属于独立工程。

## 下一步(未承诺时点)
1. 设计 connection 抽象层(contextvars get_connection + 短连接路径解析,兼容现有 `mock.patch.object(companyctl, "DB_PATH")` 锚)。
2. 以 dashboard/economics 已验证的「纯核(build_*)+ 壳(fetch loader)」范式,逐域拆 conn 耦合聚合函数。
3. 启动时同步执行 forward deprecation sweep(见 DEPRECATION.md)。

## 现状基线
- 分支 refactor/core-layer,companyctl 14798 行,637 测试绿,doctor True,main 5a241b7 未动。
