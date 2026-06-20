# CLOSURE-SUMMARY — companyctl.py core-layer 拆分物理冻结

收尾会议 `conv-20260620-153805-bbbbf7` 裁定**可收尾**;五件套证据齐 + 四闸门全绿 → 冻结分支 `refactor/core-layer`。

## 四闸门(全绿)
- ① codex 限定终审无阻断 —— `codex-final-review.md` 总票 PASS(parsing/textutil1/textutil2 各 PASS,四类阻断均未发现)。
- ② 动态引用闭合 —— `ast-forward-dispatch-map.md` 62 forward 符号零引用=0。
- ③ 抽样删 forward 证明护栏有效 —— `sampling-delete-record.md` 3 抽样(删 forward/改坏行为/删跨域 forward)守卫全部拦截。
- ④ 全量测试 + doctor 再绿 —— 637 tests OK,doctor True。

## 成果
- 分支 `refactor/core-layer`,**main 全程未动(5a241b7)**。
- companyctl.py 15446 → 14798 行(**-648**)。
- 抽出 13 模块:core/(时间族·db·events·config 依赖倒置地基)+ 域 notify·progress·economics(含 dashboard/economics 聚合「纯核+壳」拆分)·approval + 纯叶子 parsing·textutil + 早期 watchdog·backup·proc_util。
- 每刀:门面 forward 保兼容 / 新模块无反向 import / 门面守卫测试钉死同对象·golden·字节等价。

## 不在本轮(独立工程)
- 残留 ~14798 行 conn 耦合 CLI/编排 → `conn-decoupling-tracking.md`(需先设计 connection 抽象,再按「纯核+壳」范式逐域拆)。
- forward deprecation sweep → `DEPRECATION.md`(绑定 conn 解耦工程启动,仅留痕不执行)。

**状态:core-layer 拆分收尾完成。** 后续 conn 解耦另起工程。
