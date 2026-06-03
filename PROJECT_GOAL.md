# Company Kernel 项目目标

## 一句话目标

把 OpenClaw、Hermes、Codex、Claude、Trae、Antigravity 等 AI 工具统一接入为“公司员工”，由一个独立 Company Kernel 管理员工、任务、审批、通信、锁、审计和恢复。

## 为什么做

现在每个 Agent 或工具都有自己的规则、状态、任务队列和执行方式。问题是：工具一多，通信协议容易漂移，状态文件容易被误改，OpenClaw 或 Hermes 这样的自治系统还可能修改自己的总线和规则，导致中断。

Company Kernel 的职责是把“公司怎么运转”从具体工具里抽离出来。工具只负责执行任务，不能直接改公司制度、通信协议、审批规则或底层状态。

## 架构定位

```text
Company Kernel
├─ employees       # 员工身份、角色、权限、工作区
├─ tasks           # 任务提交、领取、完成、阻塞
├─ messages        # 员工间消息
├─ approvals       # 高风险动作审批
├─ locks           # 资源锁与 lease
├─ heartbeats      # 员工在线状态
├─ audit_logs      # 所有关键动作审计
└─ runtimes        # OpenClaw / Hermes / Codex / Claude / Trae / Antigravity 适配
```

## 支持的员工运行时

- `openclaw`: 业务员工、LINE/业务流程、现有 OpenClaw 总线适配。
- `hermes`: 本机 Hermes CLI、模型/浏览器/工具栈适配。
- `codex`: Codex CLI 和 `openclaw-codex-controller` 适配，负责开发、评审、测试。
- `claude`: Claude Code 或 Claude CLI 适配，负责开发、分析、文档、评审。
- `trae`: Trae IDE/Agent 适配，负责本地代码工作流。
- `antigravity`: Google Antigravity 适配，负责 IDE/浏览器/多 Agent 工作流。
- `local`: 普通脚本或人工执行器。

## 核心原则

1. 员工可以提交任务，但不能直接改内核。
2. 员工可以读规则，但不能直接改规则。
3. 员工可以请求变更，但只能生成 RFC。
4. 所有状态写入必须通过 `companyctl`。
5. 所有高风险动作必须走审批。
6. 所有任务必须有 evidence 或 blocker。
7. 所有运行时都只是 adapter，不是公司制度本体。

## 最小可用版本

第一阶段只做本机 SQLite + 文件 evidence，不上 API server。

必须支持：

```bash
companyctl employee create
companyctl employee list
companyctl task submit
companyctl task claim
companyctl task done
companyctl task block
companyctl heartbeat
companyctl doctor
companyctl runtime test
```

## 成功标准

- 可以注册 OpenClaw、Hermes、Codex、Claude、Trae、Antigravity 为员工。
- Hermes 可以提交任务给 Codex，Codex 可以回传完成证据。
- OpenClaw 可以提交任务给 Hermes，Hermes 可以领取和回传结果。
- Codex 可以通过 `openclaw-codex-controller` 作为开发员工接任务。
- 所有任务状态保存在 Company Kernel，不依赖某个工具自己的队列。
- 任意工具离线不会破坏公司总线；恢复后可继续领取任务。

## 默认项目名

建议新 GitHub 仓库名：

```text
company-kernel
```

或更品牌化：

```text
super-ai-company-kernel
```

