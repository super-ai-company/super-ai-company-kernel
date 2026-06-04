# Codex 任务协作与沟通队列

这是 OpenClaw 主理人 (`main`) 与外部研发员工 (`Codex`) 之间的标准化派单和进度反馈信箱。

## 工作流 (Workflow)

1. **派单 (Dispatch)**: `main` 会在此目录下创建 `task-<id>.md`，并在里面写明明确的 Objective（目标）、Scope（作用域）与 Verification（验收标准）。
2. **执行 (Execution)**: `main` 通过 `codex-controller` 技能，使用命令行将该任务派发给 `Codex` 执行。
3. **交付与反馈 (Feedback)**: `Codex` 完成后，除了交付代码/配置的修改，还需在该任务卡底部写入 `Verdict`（结论：done / partial / blocked）以及具体的执行证据。
4. **验收 (Review)**: `main` 定期心跳检查该目录下状态为 `done` 的任务卡，进行复核验证并更新项目整体 `PROJECT_STATE.md`。
