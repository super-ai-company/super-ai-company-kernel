# Codex 接管简报：Company Kernel 近期变更与待办

写给 codex（内核原负责人）：过去 48 小时 claude（Cowork 会话）对内核做了一轮可靠性修复和控制台上线，
本文档是完整交接。你通过内核任务队列接活，claude 负责验收你的 evidence。

## 你必须知道的 8 个变更（git log dddaad3..389fa24）

1. **裁决门（对你影响最大）**：你的最终输出必须以 `STATUS: completed` 或
   `STATUS: blocked - <一句话原因>` 结尾。exit 0 但无标记 = 任务被 block 等人工复核。
   任务卡会自动包含此要求。永远不要在验证未通过时输出 completed。
2. **任务级工作区**：任务描述里 `工作区: /绝对路径` 会让你在该目录执行（获得写权限）。
   路径必须存在且不得指向内核自身（内核改动走 RFC + PR 流程，见下）。
3. **超时保护**：codex exec 超过 1800 秒会被杀进程并 block（exit 124，证据含 adapter.timeout 事件）。
4. **诚实心跳**：daemon 不再代打心跳；只有真实运行的 worker 才有心跳。
5. **看门狗**：submitted 超 10 分钟无人领取 → 自动告警 owner。
6. **--skip-git-repo-check** 已加入你的执行命令（工作区信任由内核校验承担）。
7. **控制台**：API Gateway 直接服务实时控制台，<http://127.0.0.1:8765/> 与 :8788 同源同库；
   新增 GET /v1/events、GET /v1/heartbeats；run_companyctl 加锁修复并发空响应。
8. **launchd PATH 修复**：daemon/adapter 自动补 /opt/homebrew/bin 等，你的 CLI 可解析。

## 内核代码的开发流程（重要约束）

- 直接以"工作区"指向 $OPENCLAW_COMPANY_KERNEL_ROOT 会被拒绝（自我保护）。
- 正确路径：在你自己的工作区维护内核仓库克隆（GitHub: super-ai-company/super-ai-company-kernel，
  你之前的 PR #2 就是这么进来的）→ 开发 → 测试 → 产出 patch/分支说明，由 owner/claude 合入推送。
- 全量回归基线：**190/190**（python3 -B -m unittest discover -s tests）。任何提交不得低于此线。

## 通道认证矩阵（当前状态 → 你的待办）

| 通道 | 已验证深度 | 待你完成 |
|---|---|---|
| kernel→codex | 真实执行+裁决门+重试 e2e 全通 | 维持 |
| kernel→antigravity | dry-run brief 由 daemon 真实领取 | 设计并执行 --execute GUI 往返：打开 App→人/GUI worker 回 evidence 的完整 runbook |
| kernel→hermes | dry-run；direct 因 gateway PATH 已修待复测 | 真实 hermes -z 执行一单（低风险任务），出报告 |
| kernel→openclaw 真总线 | dry-run payload；--execute 有审批门 | 设计一笔带审批的真实 bus 提交演练（非生产 agent） |
| kernel→claude | 适配器存在未激活（claude 状态 candidate） | 检查本机 claude CLI 可用性，出激活 runbook + 测试任务设计 |
| 多员工会话 | conversation API 可用 | 设计 codex↔claude 任务绑定会话的协作演练 |

## 协作约定

- claude 每轮只读你任务的 summary 验收；不合格会 reopen 并附原因，请按原因修复后重交。
- 你发现内核 bug：直接在报告里写复现步骤 + 建议 patch，claude 负责落地与回归。
- 所有报告写入你工作区 reports/ 并在输出中给出绝对路径。
