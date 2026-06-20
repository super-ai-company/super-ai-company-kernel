# Codex Final Review - closure gate 1

## 范围

- 会议/闸门: `conv-20260620-153805-bbbbf7` 闸门 1 收尾限定终审。
- 审核批次:
  - `a591fd4` parsing: `company_kernel/parsing.py` 纯叶子下沉 7 个 parse/OpenClaw 解析符号。
  - `abc922f` textutil1: `company_kernel/textutil.py` 下沉 8 个 slug/normalize/parse 符号。
  - `8c7c377` textutil2: `company_kernel/textutil.py` 再下沉 8 个 parse/normalize/safe_path 符号。
- 审核边界: 只读 `/tmp/ck-review/closure-*.diff`、`/tmp/ck-review/ast-forward-dispatch-map.md`，并用 `git -C /Users/shift/openclaw/company-kernel show <sha>` 精确核对提交内容；未 checkout、未重跑测试套件、未修改内核代码。
- 阻断口径仅四类: 兼容性、反向 import、行为不等价、测试护栏缺失。

## 发现

### parsing `a591fd4`

- 搬迁符号: `parse_json_arg`, `parse_json_output`, `parse_openclaw_agent_reply`, `_openclaw_native_result_task_id`, `_openclaw_native_result_agent`, `_openclaw_native_result_summary`, `_openclaw_native_result_evidence`。
- AST 等价: 7/7 函数从父提交 `companyctl.py` 到 `parsing.py` 的 AST 完全一致。
- 反向 import: `company_kernel/parsing.py` 只 import `json` 和 `Path`，未 import `companyctl`。
- forward 完整性: `companyctl.py` 通过 `from .parsing import (...)` 转发 7/7 符号。
- 动态/字符串派发: `ast-forward-dispatch-map.md` 中 parsing 批 7 个符号均 `string_hits=1`，对应符号全部保留在 `companyctl` 命名空间；前次 `_blocker` 前缀误匹配不在本批搬迁符号内，本次按精确函数名复核未构成缺口。
- 测试护栏: `tests/test_module_split.py` 新增 `ParsingSweepBatch1Test`，覆盖 same-object forward、基础行为、leaf no reverse import。
- 票: PASS。

### textutil1 `abc922f`

- 搬迁符号: `slug`, `mermaid_node_id`, `clamp_audit_limit`, `normalize_task_title`, `normalize_rfc`, `normalize_project`, `parse_split_item`, `parse_csv`。
- AST 等价: 6/8 函数 AST 完全一致；`normalize_rfc`、`normalize_project` 函数体与运行时签名一致，仅移除 `sqlite3.Row | dict` 类型注解，不改变运行时行为。
- 反向 import: `company_kernel/textutil.py` 只 import `json` 和 `re`，未 import `companyctl`。
- forward 完整性: `companyctl.py` 通过 `from .textutil import (...)` 转发 8/8 符号。
- 动态/字符串派发: 对照表中 textutil1 符号均有 `string_hits=1`，且全部保留在 `companyctl` facade 命名空间；未发现字符串派发缺失 forward。
- 测试护栏: `TextutilSweepTest` 覆盖 same-object forward、代表性行为、leaf no reverse import。
- 票: PASS。

### textutil2 `8c7c377`

- 搬迁符号: `parse_participants`, `parse_acceptance`, `normalize_employee_lookup`, `safe_path_token`, `communication_name_aliases`, `report_progress_task_id`, `owner_action_next_step`, `direct_probe_body`。
- AST 等价: 8/8 函数从父提交 `companyctl.py` 到 `textutil.py` 的 AST 完全一致。
- 反向 import: `company_kernel/textutil.py` 仍未 import `companyctl`。
- forward 完整性: `companyctl.py` 追加转发 8/8 符号；连同 textutil1，`textutil` 对照表里全部 16 个符号均在 facade 中可见。
- 动态/字符串派发: 对照表中 textutil2 符号均 `string_hits=1`，全部被 forward 覆盖；未发现 `getattr`/字符串名派发会因搬迁丢失 `companyctl` 命名空间符号。
- 测试护栏: 既有 `TextutilSweepTest.TEXTUTIL_SYMBOLS` 扩展到 16 个符号，覆盖 same-object forward 和 leaf no reverse import；行为样例主要在 textutil1 已有，textutil2 为纯搬迁且 AST 等价。
- 票: PASS。

## 阻断分类

- 兼容性(forward 缺失/调用点断): 未发现。
- 反向 import(新模块 import companyctl): 未发现。
- 行为不等价(搬迁前后语义变): 未发现；`normalize_rfc`/`normalize_project` 仅类型注解差异，运行时签名与函数体一致。
- 测试护栏缺失: 未发现阻断；三批均有 same-object forward 与 leaf 约束测试，parsing/textutil1 有代表性行为样例，textutil2 依赖 AST 等价和扩展 same-object 守卫。

## 处理状态

- 未改代码。
- 未 checkout。
- 未重跑测试套件，遵守任务限制；测试状态引用提交说明中的既有 `637 OK` 背景，不作为本次新跑结果。
- 本次实际核验命令:
  - `git -C /Users/shift/openclaw/company-kernel show --stat --oneline --decorate --no-renames a591fd4 abc922f 8c7c377`
  - `git -C /Users/shift/openclaw/company-kernel show --no-ext-diff --unified=80 --no-renames <sha> -- company_kernel/companyctl.py company_kernel/parsing.py company_kernel/textutil.py tests/test_module_split.py`
  - 本地 AST 只读脚本对比父提交 `companyctl.py` 与目标提交新模块函数体、反向 import、forward import 列表。

## 最终票

- parsing `a591fd4`: PASS。
- textutil1 `abc922f`: PASS。
- textutil2 `8c7c377`: PASS。
- 总票: PASS。
