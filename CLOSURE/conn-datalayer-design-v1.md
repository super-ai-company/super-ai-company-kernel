# 内核数据层一次定型设计 v1(为几千-几万员工在岗)

**状态:已定稿(唯一)。** 由三轮设计会 `conv-20260620-200005-541bdd`(discuss/3 轮,hermes 主席)裁定,owner 命题"一次做对、不反复返工"。本文件取代 `conn-contract.md` 的草案地位,后者降为本设计读路径阶段的实现注脚。**此后不再开抽象讨论会;新增数据访问凡不走本契约,直接拒审。**

## 0. 一句话
业务调用点永不接触驱动细节(cursor / row_factory / sqlite 异常 / rowid)。换后端(SQLite→Postgres/连接池/分布式)时,**只替换 Provider/Repository 实现,132 处调用点不改**。

## 1. 架构定型
- **通用连接层**:`ConnectionProvider + UnitOfWork + TenantContext` 三件套。
- **薄 Repository**:只覆盖高并发写热点 `claim / task / backlog / ingest`。**不上通用 ORM,不把 132 处一次性改成 Repository**。只读路径先走 Provider 平滑替换。
- **抽象边界画在"业务如何看见数据层"这一层**,不是 SQL 层。只要业务还碰方言,后端可换就是假命题。

## 2. 逐维度决策
| 维度 | 决策 |
|---|---|
| 后端可换 | Provider 注入;业务调用点零驱动细节;换后端只换实现 |
| 写争用天花板 | 触发切服务端库/池阈值:写事务等待 p99 > 50ms 或 SQLITE_BUSY/等价重试率 > 1%(先验值,后续按真实流量校准);**接口一期就池可插**,是否真切看观测 |
| 队列摄取 sync_backlog | 从 connect() **彻底剥离**,定为显式 `ingest` 契约。一期定幂等键/重放语义/lag 指标;二期落独立 worker/service |
| 唯一认领 | 领域契约 `claim_one(...) -> claimed_task\|null`,只承诺"恰好一次 + 调用方不感知锁"。SQLite 维持原子 `UPDATE...WHERE status='submitted'`;Postgres 在 Repository 内部用 `SKIP LOCKED / FOR UPDATE` |
| 多租户 | **现在就纳入定型**:契约/上下文/观测/错误归因全带 tenant。单租户=默认映射,不再是架构前提 |
| 迁移路径 | 增量:①契约定型+只读 Provider 化 → ②写热点走 Repository + ingest 解耦 → ③按观测接池/切 Postgres。每步双实现可回滚 |

## 3. 一期完成 Gate(八项,全满足才算一期完成,缺一不进二期)
1. 连接契约(Provider/UnitOfWork/TenantContext)
2. 写热点 Repository 契约(claim/task/backlog/ingest)
3. 观测事件 schema(必带 `tenant / operation / backend / result_code / latency / retry_count`)
4. 禁直连迁移清单
5. 统一错误模型(封闭枚举,含 `BackendUnavailable / CapacityExceeded`)
6. FakeProvider 隔离测试通过(机器可验:业务层与驱动物理隔离)
7. SQLiteProvider 现有回归不退化
8. 禁直连扫描清零(132 处零 `sqlite3` 导入、零方言异常捕获、零驱动对象访问)

## 4. 分工(下一步谁做什么)
- **claude-cli**:并行启动现状 e2e 基线,只钉现状可观测行为(claim 恰好一次 / ingest 结果 / 连接不泄漏 / SQLite 回归),只补验收不扩业务重构。
- **codex-cli**:起最小契约 PR(不等 e2e):`Protocol/ABC + FakeProvider + SQLiteProvider` 骨架、写热点 Repository 契约、观测 schema、统一错误模型、禁直连迁移清单。
- **gemini**:codex 骨架 PR 建立后产出打分表 + CI 拦截(静态:132 处零 sqlite 导入/零方言捕获/零驱动访问;动态:运行时错误只落封闭枚举,码外异常逃逸即失败)。
- **主席/owner**:本决议为唯一定稿;一期工作只围绕 gate;超边界需求一律延到二期评审。

## 5. 待确认风险(二期校准,不阻塞一期锁契约)
- `claim_one` 在 Postgres/池化下具体 SQL 待二期验证(不影响一期锁语义)。
- FakeProvider 不证明真实并发锁/SQL 性能 → 必须与 SQLiteProvider **双层测试并行保真**,不能拿 Fake 代替回归。
- 50ms/1% 是先验阈值,埋点口径一期先定、数值后校准。
- ingest 二期 worker 化时幂等键过弱会放大重复摄取 → 一期把幂等/重放契约写严。
- 迁移期最大风险=新代码继续绕契约直连 → **禁直连扫描 + 拒审纪律立即生效**,否则边收口边漏水。
