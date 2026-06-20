# CLOSURE/DEPRECATION — forward 门面边界与清理触发条件

## 现状
companyctl.py 通过显式 `from .<module> import (...)  # noqa: F401 (facade re-export)` 把已拆分模块的符号再导出,保持 ~28 个 importer + 全部调用点零改动。本次拆分共 62 个 forward 符号(见 ast-forward-dispatch-map.md),零引用 0 个。

## 边界(刻意保留,非债务堆积)
- forward 再导出是**兼容层**:外部模块/脚本/测试仍按 `companyctl.X` 或裸名访问。删除会破坏兼容,故本阶段一律保留。
- 每个 forward 符号都有守卫测试钉死「与源模块同对象」或行为 golden(见 test_module_split.py)。

## 清理(deprecation sweep)触发条件 —— 仅留痕,本轮不执行
触发时点 = **下一个域真正开始解耦 conn(connection 抽象工程启动)时**,届时:
1. 为 forward 符号加 `DeprecationWarning`,公示外部迁移到 `from company_kernel.<module> import X`。
2. 一个 release 周期后移除 companyctl 的 forward,守卫测试同步改为直接 import 源模块。
3. 禁止在 conn 解耦工程之外单独做 sweep(避免无意义的大文件 churn)。

## 关联
- 剩余 14798 行 conn 耦合代码的演进 = 独立 tracking issue(见 conn-decoupling-tracking.md),forward sweep 绑定该工程启动,防止无限堆积被遗忘。
