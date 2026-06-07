# Codex Task Card: Hardening PM Supervisor & Codebase Hygiene

模式：目标模式 / Target Mode
负责人：Codex (Developer)
监督人与验收人：Hermes (PM/Supervisor)
停止条件：完成本卡所列目标，且通过所有验证网关；如需改动本卡规定以外的代码，须停止并向 PM 汇报。

---

## 1. 目标 (Goals)

### 任务一：修复 PM 监督器超时逻辑漏洞 (Fix Timeout Loophole)
* **目标文件**：`company_kernel/codex_pm_supervisor.py`
* **漏洞描述**：在进行任务卡死超时判定时，目前仅对 `acknowledged` 和 `in_progress` 进行字面量匹配。如果员工汇报了其他有效的处理中状态（如 `working`, `active`, `running`, `claimed` 等），这些任务会被永远忽略而不会超时卡死。
* **具体要求**：
  * 修改超时逻辑判断，将 literal 匹配改为基于归一化层 `layer` 判断。如果 `layer in {"received", "working"}` 且 `age_minutes > stale_minutes`，应判定为 `stalled`（卡住）。
  * 在 `tests/test_codex_pm_supervisor.py` 中增加新的测试用例（例如汇报 `working` 状态超时后，被 PM 正确判定为 `stalled`），确保该修复逻辑长期有效。

### 任务二：清理未关闭的 SQLite 连接警告 (Fix ResourceWarnings)
* **目标文件**：`company_kernel/companyctl.py` 及相关测试用例
* **问题描述**：在最新的 Python 3.14 严格垃圾回收检查下，执行单元测试会报出大量的 `ResourceWarning: unclosed database in <sqlite3.Connection object>` 未关闭连接警告。
* **具体要求**：
  * 检查并重构 `companyctl.py` 中被遗漏的 `conn.close()` 或改用 `with connect() as conn` 上下文管理器。
  * 确保运行测试命令时不再产生任何 unclosed database 的资源泄露报警。

### 任务三：优化 Launchd 模板匹配路径差异提示 (Path Mismatch Diagnostics)
* **目标文件**：`company_kernel/companyctl.py` 内的 `launchd_health()` 函数
* **问题描述**：在多个克隆工作区时运行 `doctor` 会因为 `ROOT` 路径不同而报 LaunchAgent 模板不匹配。
* **具体要求**：
  * 如果比对发现仅仅是因为 `__COMPANY_KERNEL_ROOT__` 替换后的绝对路径不一致，在 `issues` 报告中以警告形式输出提示，而非简单报 `matches_template = False` 影响全局 OK 状态判定。

### 任务四：清理工作区未跟踪文件并提交 (Workdir Cleanup)
* **具体要求**：
  * 审查根目录的未跟踪脚本 `scripts/patch_*.py` 和 `scripts/harden_employee_skills.py`。
  * 在确保其功能和代码已被合并至最新提交后，安全地将这些多余的临时脚本移除；或者在 `.gitignore` 中补充忽略规则，使工作区保持干净的 Clean 状态。

---

## 2. 非目标 (Non-goals)

* 绝对**不要**启用或修改 `ops-telegram-approval-watcher.plist` 后台服务（避免 Token 冲突）。
* 绝对**不要**在没有人工授权的情况下向真实 OpenClaw 总线执行 `--execute` 级写入。
* 不要对前端组件进行与本任务无关的重构。

---

## 3. 验收标准与验证网关 (Verification Gate)

1. **测试套件验证**：
   * 运行测试命令且 exit_code 为 0，且不能输出 `ResourceWarning` 警告：
     ```bash
     PYTHONWARNINGS=error::ResourceWarning python3 -B -m unittest discover -s tests -v
     ```
2. **PM 超时测试运行**：
   * 手动运行一次 PM 监督器，输出判定结果符合预期：
     ```bash
     bin/company-codex-pm-supervisor --agent codex --stale-minutes 15
     ```
3. **工作区状态自检**：
   * `git status` 确认工作区内除了必要修改文件外，无其他杂乱未跟踪文件。
