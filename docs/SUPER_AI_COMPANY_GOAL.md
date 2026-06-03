# Super AI Company 项目目标

## 一句话目标

创建一个“超级 AI 公司”操作系统：让 OpenClaw、Hermes、Codex、Claude、Trae、Antigravity 以及未来任何 AI 工具都能成为公司员工，在统一制度下互相发消息、多轮对话、派任务、交付证据、请求审批、恢复中断，并持续协作完成真实项目。

## 我们要解决的问题

现在的 AI 工具都很强，但它们各自为战：

- OpenClaw 有业务 Agent、LINE、任务总线和业务工作区。
- Hermes 有本机工具链、模型、浏览器和自动化能力。
- Codex 擅长真实代码开发、测试、审查和项目推进。
- Claude、Trae、Antigravity 等工具也各有优势。

问题不是缺少工具，而是缺少“公司级协作机制”。如果让每个工具直接改自己的规则、状态和总线，系统会越来越乱：通信协议漂移、任务状态不一致、队列中断、工具互相不知道对方做了什么。

Super AI Company 的目标是把所有 AI 工具从“单独好用的 App”升级成“可协作的公司员工”。

## 核心愿景

我们要做的不是另一个聊天机器人，也不是单个 Agent。我们要做的是一个公司管理内核：

```text
Super AI Company
├─ Company Kernel       # 公司制度、任务、通信、审批、锁、审计、恢复
├─ AI Employees         # OpenClaw / Hermes / Codex / Claude / Trae / Antigravity
├─ Runtime Adapters     # 每个工具的接入器
├─ Workspaces           # 每个员工自己的工作区
├─ Evidence System      # 每个任务必须有证据或阻塞原因
└─ Governance           # 高风险动作审批、规则变更 RFC、内核保护
```

Company Kernel 是公司的“人事部 + 任务系统 + 通信总线 + 审计部 + 恢复中心”。  
AI 工具是员工，不是制度本身。

## 员工角色

第一批员工：

- `openclaw-main`: 公司运营调度，连接现有 OpenClaw 业务 Agent。
- `main / nestcar / chindahotpot / krothong / invest / video-*`: OpenClaw 业务员工。
- `hermes`: 主管型员工，负责本机工具、模型、浏览器和自动化协作。
- `codex`: 工程员工，负责写代码、测试、评审、生成补丁和推进 GitHub 项目。
- `claude`: 分析/文档/代码理解员工。
- `trae`: IDE 型开发员工。
- `antigravity`: 多 Agent/IDE/浏览器型员工，当前先注册为占位，后续接入真实执行器。

未来可以继续添加：

- Cursor
- Devin
- GitHub Copilot
- 自建本地模型 Agent
- 企业内部业务 Agent
- 人类员工账号

## 员工之间怎么通信

所有员工都通过同一套命令协议通信，支持三种协作形式：

1. 单条消息：适合通知。
2. 多轮对话：适合 AI 之间持续讨论和协商。
3. 任务流转：适合明确交付、证据和验收。

```bash
companyctl message send --from hermes --to nestcar --body "请检查今天车辆任务"
companyctl conversation start --from hermes --participants hermes,codex,claude --title "项目方案讨论" --body "请讨论第一版实现方案"
companyctl conversation reply --from codex --conversation-id conv-xxx --body "我建议先做内核命令和适配器"
companyctl task submit --from openclaw-main --to codex --title "修复项目脚本"
companyctl task claim --agent codex
companyctl task done --agent codex --task-id xxx --summary "已完成" --evidence /path/report.md
```

任何员工都可以：

- 给任意员工发消息。
- 和多个员工进行不限轮次的对话。
- 给任意员工派任务。
- 领取分配给自己的任务。
- 完成任务并提交 evidence。
- 阻塞任务并提交 blocker。
- 写心跳证明自己在线。

任何员工都不可以：

- 直接修改公司内核。
- 直接改通信协议。
- 直接改审批规则。
- 直接伪造任务完成。
- 绕过高风险审批。

## 为什么要独立 Company Kernel

不能让 OpenClaw 自己承载全部公司制度，也不能让 Hermes 或 Codex 承载全部制度。原因是：

- OpenClaw 是一个强业务运行时，但它也可能改自己的规则和队列。
- Hermes 是强工具运行时，但它不应该直接控制 OpenClaw。
- Codex 是强开发运行时，但它不应该直接改公司总线。

所以公司制度必须独立出来：

```text
Company Kernel 管制度
OpenClaw / Hermes / Codex / Claude / Trae / Antigravity 负责执行
```

这样任何工具坏了、离线了、升级了，都不会摧毁公司本身。

## 第一阶段目标

第一阶段做本机可运行版本，不上云、不做复杂 API。

必须完成：

- 独立项目目录：`/Users/owner/openclaw/company-kernel`
- SQLite 公司数据库：员工、任务、消息、锁、心跳、审批、审计
- 统一命令：`companyctl`
- 员工创建：`employee create`
- OpenClaw 批量导入：`employee import-openclaw`
- 员工互发消息：`message send/list`
- 员工多轮对话：`conversation start/reply/list/show`
- 员工互派任务：`task submit/claim/done/block/list`
- 配置化协作流：`workflow validate/run`
- 事件驱动接力：`scheduler run/events`
- Runtime 探测：`runtime test`
- 心跳：`heartbeat`
- 审批中心：`approval request/list/show/approve/deny`
- 锁和恢复：`lock acquire/release/list/unlock-stale`、`repair reset-stale-claims`
- 本机巡检：`company-daemon --once/--iterations`
- 静态操作台：`company-dashboard`
- 项目管理：`project create/list/show/link-task/status/review`
- 自检：`doctor`

## 第二阶段目标

让工具真的自动执行，而不是只登记和写队列：

- OpenClaw Adapter：把 Company Kernel 任务桥接到 OpenClaw 旧 `ops/agent_bus`，默认 dry-run，显式 `--execute` 才写真实 bus。
- Hermes Adapter：让 Hermes 自动领取任务并回传结果，默认 dry-run，显式 `--execute` 才调用 `hermes -z`。
- Codex Adapter：接入 `openclaw-codex-controller`，自动生成 task card、可选运行 `codex exec`、回传测试结果。
- Claude Adapter：接入 Claude CLI/Claude Code，默认 dry-run，显式 `--execute` 才调用 `claude -p`。
- Trae Adapter：接入 Trae 本地工作流，默认 dry-run，显式 `--execute` 才调用 `trae chat`。
- Antigravity Adapter：接入 Antigravity GUI 入口，默认 dry-run，显式 `--execute` 只打开 App，不伪造完成。

当前进度：

- 已有最小 `company-adapter-worker`，可安全领取任务、写 evidence、回写状态和 heartbeat。
- 当前 worker 支持 dry-run，不会擅自启动真实外部工具。
- 已有 `company-codex-adapter`，可把 Company Kernel 任务转成 Codex task card；加 `--execute` 后可运行 `codex exec`。
- 已有 `company-openclaw-adapter`，可把 Company Kernel 任务转成 OpenClaw legacy bus payload；加 `--execute` 后可提交到 OpenClaw `ops/agent_bus`。
- 已有 `company-hermes-adapter`，可把 Company Kernel 任务转成 Hermes oneshot prompt；加 `--execute` 后可调用 `hermes -z`。
- 已有 `company-claude-adapter`，可把 Company Kernel 任务转成 Claude print prompt；加 `--execute` 后可调用 `claude -p`。
- 已有 `company-trae-adapter`，可把 Company Kernel 任务转成 Trae chat prompt；加 `--execute` 后可调用 `trae chat`。
- 已有 `company-antigravity-adapter`，可把 Company Kernel 任务转成 Antigravity GUI brief；加 `--execute` 后只打开 App，等待未来 GUI worker 回传 evidence。
- 下一步是为这些 adapter 增加真实结果回收、审批联动和操作台。
- 已有 `config/company_communications.json` 管员工别名和互通关系。
- 已有 `config/workflows/video_pipeline_5round.json`，已跑通 `ops/maker/publisher` 五轮视频协作：maker 问需求，ops 下达搞笑中文竖版视频，maker 完成并回传证据，ops 接力 publisher，publisher 完成并回传发布报告。
- 已有 `config/hooks.json` 和 `company_events` 事件表，`task.done` 后可由 scheduler 自动触发消息、派新任务和心跳；已验证 maker 完成后自动派 publisher，publisher 完成后自动通知 ops。
- 已有审批中心，支持高风险动作进入 `pending/approved/denied` 状态，并写入 `state/approvals/<status>/` 作为可恢复审计文件。
- Scheduler 已支持 approval gate：hook action 标记 `requires_approval` 后，未批准会自动生成 pending approval 并阻断高风险动作；安全动作可先执行，已执行动作记录到 `hook_action_runs`，批准后重跑 scheduler 只补执行未完成动作。
- OpenClaw adapter 的 `--execute` 已接入 approval gate：未批准不会写 legacy bus，会生成 pending approval；批准后重跑才提交到 `/Users/owner/openclaw/ops/agent_bus`。
- 已有任务租约恢复：`task claim` 会创建 `task:<id>` 锁；`repair reset-stale-claims` 可释放过期锁并把过期 claimed 任务恢复为 submitted，避免员工中断后队列卡死。
- 已有 `company-daemon` 本机巡检循环，按 `config/daemon.json` 定期执行 repair、scheduler 和 heartbeat；默认不启动真实外部工具。
- 已有静态 Dashboard，生成到 `state/dashboard.html`，集中展示员工在线状态、任务队列、审批、事件和锁。
- 已有项目管理层，支持项目目标、owner、验收标准、任务关联和项目验收 review；`super-ai-company-kernel` 已关联 3 个关键验证任务，review 显示任务证据层面可完成。

## 第三阶段目标

把公司机制升级成真正的协作操作台：

- 可视化员工列表、在线状态、任务队列。
- 每个任务有 owner、状态、证据、阻塞、审批记录。
- 高风险动作自动进入审批中心。
- 长任务支持分解、委派、回收、恢复。
- 每个员工有能力档案和权限边界。
- 每个项目有目标、计划、任务、验收和复盘。

## 最重要的规则

1. 公司内核不能被普通员工直接修改。
2. 所有员工只能通过 `companyctl` 改状态。
3. 所有任务必须有 evidence 或 blocker。
4. 所有高风险业务动作必须审批。
5. 所有工具都是员工，不是公司制度本体。
6. 添加员工应该是命令，不是手改一堆代码。
7. 任意两个员工都应该能互相通信。
8. 多个 AI 员工之间可以进行不限轮次对话，但对话结论必须能转成任务、证据或审批。

## 最终形态

最终我们要的是一个“超级 AI 团队”：

- OpenClaw 管业务运营。
- Hermes 管本机工具和自动化。
- Codex 管工程交付。
- Claude 管分析、审查和文档。
- Trae/Antigravity 管 IDE 型开发协作。
- Company Kernel 管公司制度、任务、通信、审批和恢复。

这套系统的目标不是让一个 AI 更强，而是让一群强 AI 像真正公司一样协作。
