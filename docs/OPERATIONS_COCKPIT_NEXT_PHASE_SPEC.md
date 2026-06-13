# Super AI Company Kernel 目标模式：AI 员工驾驶舱与长任务闭环

目标：把当前 Dashboard 升级为本机 AI 员工驾驶舱，统一管理 Hermes、Codex、Antigravity、OpenClaw 等员工的任务、状态、审批、纠偏、日志和 evidence。第一阶段只做本机稳定协作，不做 marketplace、支付、云出租、复杂 Swarm 画布。

---

## 核心原则

1. **统一账本**：Company Kernel 是唯一任务账本和状态机。
2. **越权阻断**：Dashboard/API/CLI 都必须通过 Kernel 操作员工，不能绕过账本私发关键任务。
3. **超时定义**：`--timeout` 只代表同步等待窗口结束，不代表 task failed。
4. **长任务生命周期**：长任务必须用 `task_id` + `attempt_id` + `heartbeat` + `progress` + `stagnant` + `correction` + `cancel` + `evidence` 管理。
5. **交付绑定**：done 必须绑定 `task_id`/`attempt_id` 对应 evidence，不能只看 ACK、stdout、聊天或 inbox 文件。
6. **异步保活**：Hermes/Codex/Agy/OpenClaw 可长时间运行，前端靠心跳、进度和日志感知，不用短超时误杀。

---

## 前端定位

第一屏是 **AI Employee Cockpit**，不是传统后台。老板进入页面后必须一眼看到：
- 哪些员工 online/busy/candidate/active-limited/abnormal。
- 哪些任务 running/stagnant/blocked/awaiting approval/done。
- Hermes 监督了什么，给谁发过 correction。
- Codex/Agy/OpenClaw 当前执行到哪一步。
- 哪些动作需要 owner approval。
- evidence 在哪里，是否可验收。

---

## 主导航规划 (5 个栏目)

1. **Cockpit Console**：全局运行态、在线员工、活跃任务、停滞任务、待审批、最近 evidence。
2. **Tasks & Workflows**：task、attempt、trace、状态机、attempt history。
3. **AI Fleet & Skills**：员工状态、能力、候选/激活等级、skill 列表。
4. **Chat Hub**：任务绑定对话流，默认折叠 greeting/handshake/idle chatter。
5. **Audit, Approvals & Evidence**：审批、事件账本、artifact、handoff、evidence、失败记录。

---

## 长任务状态流

- **heartbeat fresh**：员工在线。
- **heartbeat stale**：员工可能掉线。
- **progress fresh**：任务仍在推进。
- **progress stagnant**：心跳正常但 10-15 分钟无进度，不叫 timeout。
- **correcting**：Hermes 或老板已发纠偏。
- **blocked**：需要人类输入、权限、验证码、决策或外部资源。
- **cancelled**：任务已取消，旧 attempt 禁止再提交完成。
- **failed**：必须有明确失败证据。
- **done**：必须绑定 final evidence。

*UI 文案提示*：“员工仍在线，但 15 分钟没有新进度。可继续等待、发送探针、查看日志或请求 Hermes 纠偏。”

---

## 优先级划分 (P0 / P1 / P2)

### P0 (最高优先级，核心骨架与安全)
1. **重构 Cockpit 第一屏**：整合员工状态、运行任务、停滞任务、待审批、最近 evidence。
2. **稳定轮询**：REST 轮询 5-10 秒刷新；不做 WebSocket，SSE 只预留接口。
3. **增强任务详情抽屉**：显示 task、trace、attempt、heartbeat、progress、correction、evidence。
4. **实现长任务状态条**：区分 timeout、stale、stagnant、blocked、failed、done。
5. **增加 correction 输入**：老板或 Hermes 可向 running attempt 发纠偏，写入 event ledger。
6. **增强 Approvals**：approve/deny/mock resolve；真实 Telegram/OpenClaw/external_send 默认 dry-run，真实执行必须 owner approval。
7. **Evidence 安全展示（严格白名单）**：只允许读取 `workspace/evidence/reports/artifacts` 白名单目录；禁止绝对路径、`../`、`~/.ssh`、`.env`、`config`、`profile`、`api key` 文件。
8. **Chat Hub 收口**：改为任务绑定对话流，默认隐藏握手类消息。
9. **数据一致性**：API/CLI/Dashboard 三方必须看到同一账本状态。

### P1 (可观测性与尝试历史)
1. **SSE 事件流**：2 秒内感知关键事件。
2. **Trace Timeline**：展示 Hermes -> Codex/Agy/OpenClaw 的监督与执行链路。
3. **Employee Badge**：区分 active、active-limited、candidate、online-only、unsafe。
4. **Attempt History**：retry/reassign 新建 attempt，保留并展示旧 attempt。
5. **Sanitized Logs**：只展示清洗后的 attempt log，不直接暴露 raw stdout/stderr。

### P2 (未来功能与清理预留)
1. **Workspace retention/prune dry-run**。
2. **Sandbox profile 可视化**。
3. **Skill Registry 页面**。
4. **更复杂 handoff/artifact 图谱**。
5. **后续 marketplace/出租能力预留**，但不进入本阶段。

---

## 角色分工 (Fleet Roster)

* **Codex**：负责 Kernel/API/测试/状态机/安全边界/GitHub 提交；把 Agy 评审意见转成代码；每轮运行测试、doctor、dashboard 验证。
* **Antigravity**：负责前端 UX 评测、布局建议、交互问题发现；当前作为 design-review 员工参与。**只有代码修改、测试通过、文件落地才算 execution evidence，规划回复不算**。
* **Hermes**：作为 supervisor，负责长任务监督、stale 判断、纠偏建议和最终人类可读总结。

---

## Git 提交与发布规则

1. **主线对齐**：GitHub main 已提交：`https://github.com/super-ai-company/super-ai-company-kernel.git`
2. **分支管理**：开工前从 `main` 分支新建开发分支。
3. **脏文件规避（禁止提交）**：
   - `config/company_communications.json`
   - `employees/*/profile.json`
   - 新扫描出来的 runtime-only employees
4. **交付闭环网关**：每阶段完成后必须运行单元测试、运行 `companyctl doctor --summary` 并打开/生成 dashboard 验证，最后 commit/push。
