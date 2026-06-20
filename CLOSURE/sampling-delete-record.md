# CLOSURE/sampling-delete-record — 破坏性抽样删验

临时 `git worktree`(/tmp/ck-sample-wt @ 8c7c377,验毕销毁)内做破坏性抽样,证明门面守卫测试能抓到 forward 移除/行为漂移。基准 commit=8c7c377。

| # | 抽样类型 | 注入故障 | 期望 | 实测 | 命中守卫 |
|---|---|---|---|---|---|
| 1 | 删 forward 再导出 | companyctl 移除 `slug` 的 forward import | 守卫 FAIL | FAILED(failures=1,errors=1) | test_textutil_symbols_forwarded_as_same_objects |
| 2 | 改坏纯函数行为 | textutil.parse_csv 分隔符 `,`→`;` | 行为守卫 FAIL | FAILED(failures=1) | test_textutil_behaviour_preserved |
| 3 | 删跨域 forward | companyctl 移除 `build_economics` forward | 守卫 FAIL/Error | FAILED(errors=1) | BuildEconomicsCutTest(3 用例) |

结论:门面守卫(同对象再导出 + 行为/golden 断言)对 forward 移除与行为漂移均有效拦截;不存在"冻结一批死代码无人知"风险(配合 ast-forward-dispatch-map 零引用=0 双重兜底)。验毕环境已销毁,主仓未受影响。
