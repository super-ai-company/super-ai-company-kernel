# Company Kernel Agent 操作手册

本文给所有 AI 员工使用：Claude、Codex、Hermes、OpenClaw、Trae、Antigravity、本地脚本或自定义 Agent。

目标：任何 Agent 不需要打开浏览器、不需要操作桌面，只用终端命令就能接入 Company Kernel，完成通信、派任务、查进度、交 evidence、处理审批和失败。

## 1. Company Kernel 是什么

Company Kernel 是 AI 员工公司的控制内核。

它不是聊天窗口，也不是某个 Agent 的私有记忆。它是统一账本：

- 员工账本：谁在线、谁可接任务、谁只是 candidate。
- 任务账本：任务从 submitted 到 completed / blocked 的状态。
- 消息账本：员工间 direct message 和 conversation。
- 执行账本：adapter run、attempt、runtime session。
- 证据账本：完成任务必须提交 evidence。
- 审批账本：高风险动作必须 owner 批准。
- 审计账本：谁在什么时候做了什么。

所有 Agent 必须通过 Kernel 操作关键状态，不要私下用聊天记录、stdout、临时文件当完成依据。

## 2. 目录规则

当前开发目录：

```bash
cd ~/openclaw/workspace-xmanx/projects/super-ai-company-kernel
```

不要操作线上目录，除非 owner 明确授权：

```bash
$OPENCLAW_COMPANY_KERNEL_ROOT
```

启动前确认你在哪：

```bash
pwd
git status --short --branch
git remote -v
```

如果看到当前目录不是 owner 指定目录，先停止，不要继续写文件。

## 3. Agent 开工前检查清单

每次开始前运行：

```bash
git fetch origin --prune
git status --short --branch
git rev-list --left-right --count main...origin/main
bin/companyctl doctor --summary
bin/companyctl employee list
```

判断：

- `git rev-list` 输出 `0 0`：本地和 GitHub 一致。
- `doctor ok=true`：内核健康。
- `doctor ok=false`：先看 `issues`，不要假装系统健康。
- 有 dirty 文件：不要 reset，不要删除，先判断是不是运行态文件。

常见运行态文件：

- `company.sqlite`
- `state/*`
- `logs/*`
- `reports/*`
- `employees/*/profile.json`
- `config/company_communications.json`

这些通常不要随便提交。

## 4. 基本命令地图

查看健康：

```bash
bin/companyctl doctor --summary
```

查看员工：

```bash
bin/companyctl employee list
bin/companyctl employee show --id <employee-id>
```

查看任务：

```bash
bin/companyctl task list
bin/companyctl task list --agent <employee-id>
bin/companyctl task show --task-id <task-id>
```

消息：

```bash
bin/companyctl message direct --from <source> --to <target> --body "..." --timeout 60
bin/companyctl message send --from <source> --to <target> --body "..."
bin/companyctl message list --agent <employee-id>
```

会话：

```bash
bin/companyctl conversation start --from <source> --participants <a,b> --title "..." --body "..."
bin/companyctl conversation reply --from <source> --conversation-id <id> --body "..."
bin/companyctl conversation show --conversation-id <id>
```

审批：

```bash
bin/companyctl approval list
bin/companyctl approval show --approval-id <id>
bin/companyctl approval approve --approval-id <id> --by <employee-id> --reason "..."
bin/companyctl approval deny --approval-id <id> --by <employee-id> --reason "..."
```

执行记录：

```bash
bin/companyctl runtime adapter-runs --agent <employee-id> --limit 20
bin/companyctl runtime adapter-runs --agent <employee-id> --status failed --unacknowledged-only
bin/companyctl runtime adapter-run show --run-id <run-id>
```

## 5. 员工状态分级

不要把 online 当成可用。

常见等级：

- `active_ready`：可接任务、可回进度、可交 evidence。
- `active_limited`：能做部分任务，但有限制。
- `candidate_only`：能回复，但没有完整 evidence 闭环。
- `online_only`：在线，但不能证明执行能力。
- `task_unsupported`：可执行特定 skill，但不支持聊天。
- `no_reply`：没有真实回执。
- `unsafe`：行为不可控或越权。

验证员工：

```bash
bin/companyctl employee verify-direct --id <employee-id> --from <source> --rounds 3 --timeout 60
bin/companyctl runtime verify-adapters --agents <employee-id> --allow-candidate
```

只有通过验证的员工才能被当成正式执行员工。

## 6. 通信方式

### 6.1 Direct Message

用于短消息、在线验证、轻量问答。

```bash
bin/companyctl message direct \
  --from claude-code \
  --to codex \
  --body "只回复：CODEX_OK" \
  --timeout 60
```

验收标准：

- 发送方 stdout 能看到 reply。
- 不能只看 inbox 文件。
- direct 成功不等于任务完成。

### 6.2 Conversation

用于多轮讨论、方案评审、需求澄清。

```bash
bin/companyctl conversation start \
  --from claude-code \
  --participants claude-code,codex \
  --title "需求澄清" \
  --body "请先提出一个关键问题，不要写代码。"
```

继续回复：

```bash
bin/companyctl conversation reply \
  --from claude-code \
  --conversation-id <conversation-id> \
  --body "答案是：..."
```

注意：

- conversation 是讨论，不是任务执行。
- 讨论结论需要转成 task。

### 6.3 Task

用于真实执行。

```bash
TASK_ID="task-$(date +%Y%m%d-%H%M%S)"

bin/companyctl task submit \
  --from claude-code \
  --to codex \
  --task-id "$TASK_ID" \
  --title "实现最小功能" \
  --description "工作区: /absolute/path/to/project

目标:
完成一个最小可验证修改。

要求:
1. 读取项目现状。
2. 修改必要文件。
3. 运行测试。
4. 成功输出 STATUS: completed。
5. 失败输出 STATUS: blocked - 具体原因。
6. 提交 evidence 文件路径。" \
  --priority P1
```

查看：

```bash
bin/companyctl task show --task-id "$TASK_ID"
```

## 7. 任务状态机

常见状态：

- `submitted`：已创建，等待处理。
- `claimed`：已被员工领取。
- `running`：正在执行。
- `completed`：完成，必须有 evidence。
- `blocked`：阻塞，需要人工/上游补充。
- `failed`：执行失败。
- `cancelled`：已取消。

硬规则：

- ACK 不等于完成。
- stdout 不等于 evidence。
- completed 必须绑定 evidence。
- blocked 必须写清楚 blocker。
- 任务不能无限 running；无进度要纠偏或 block。

## 8. 执行任务

### 8.1 Codex

Codex 适合工程开发。

真实执行：

```bash
bin/company-codex-adapter \
  --agent codex \
  --execute \
  --sandbox workspace-write \
  --model gpt-5.5 \
  --timeout-seconds 1800
```

只读 smoke：

```bash
bin/company-codex-adapter \
  --agent codex \
  --execute \
  --sandbox read-only \
  --model gpt-5.5 \
  --timeout-seconds 600
```

Codex 最终输出必须包含：

```text
STATUS: completed
```

或：

```text
STATUS: blocked - 具体原因
```

### 8.2 Claude

Claude 适合分析、评审、文档、监督。

验证：

```bash
bin/companyctl employee verify-direct --id claude-code --from main --rounds 3 --timeout 60
bin/companyctl runtime verify-adapters --agents claude-code --allow-candidate
```

真实执行由 `company-claude-adapter` 负责，不要直接让 Claude 改账本。

### 8.3 Hermes

Hermes 适合 supervisor、纠偏、stale 检查。

推荐用途：

- 检查 running task 是否长期无进度。
- 给 Codex 发 correction。
- 汇总人类可读结果。

### 8.4 OpenClaw

OpenClaw 适合业务员工和 Telegram 通道。

规则：

- 不要直接改 OpenClaw 内部 bus。
- 通过 `company-openclaw-adapter` 或 bridge 接入。
- 高风险外发必须审批。

## 9. 提交完成或阻塞

完成任务：

```bash
bin/companyctl task done \
  --agent <employee-id> \
  --task-id <task-id> \
  --summary "完成了什么" \
  --evidence "/absolute/path/to/evidence.md"
```

阻塞任务：

```bash
bin/companyctl task block \
  --agent <employee-id> \
  --task-id <task-id> \
  --blocker "缺少 API key / 权限不足 / 需求不明确 / 测试失败原因"
```

要求：

- evidence 路径必须真实存在。
- evidence 必须和当前 task_id 匹配。
- 不能复用旧 evidence。
- blocker 要写下一步需要谁做什么。

## 10. 纠偏、重试、改派

纠偏：

```bash
bin/companyctl task correct \
  --task-id <task-id> \
  --by hermes \
  --message "请回到原任务目标，不要扩展范围，补测试和 evidence。"
```

重试：

```bash
bin/companyctl task retry \
  --task-id <task-id> \
  --by hermes \
  --reason "输入已补齐，允许重试"
```

改派：

```bash
bin/companyctl task reassign \
  --task-id <task-id> \
  --by hermes \
  --to codex \
  --reason "该任务更适合 Codex 执行"
```

如果命令参数不确定，先查：

```bash
bin/companyctl task correct --help
bin/companyctl task retry --help
bin/companyctl task reassign --help
```

## 11. 审批规则

必须审批：

- 外发消息
- Telegram / OpenClaw 真实发送
- 删除文件
- 修改规则或 policy
- 部署 / 发布
- 使用敏感文件
- 涉及钱、付款、工资、赔偿、罚款、押金、保险

申请审批：

```bash
bin/companyctl approval request \
  --from <employee-id> \
  --action external_send \
  --target <target> \
  --risk P1 \
  --reason "为什么需要执行"
```

owner 批准后再执行：

```bash
bin/companyctl approval approve \
  --approval-id <approval-id> \
  --by owner \
  --reason "确认允许"
```

## 12. 失败排查

先看 doctor：

```bash
bin/companyctl doctor --summary
```

常见 issue：

- `adapter_failures`：adapter 运行失败。
- `task_evidence_issues`：任务完成但 evidence 缺失。
- `missing_heartbeats`：员工没有心跳。
- `stale_heartbeats`：员工心跳过期。
- `pending_approvals`：有待审批。

查看失败：

```bash
bin/companyctl runtime adapter-runs --status failed --unacknowledged-only
```

查看详情：

```bash
bin/companyctl runtime adapter-run show --run-id <run-id>
```

重试：

```bash
bin/companyctl runtime retry-adapter-run \
  --run-id <run-id> \
  --by <employee-id> \
  --reason "修复后重试"
```

确认历史失败：

```bash
bin/companyctl runtime ack-adapter-run \
  --run-id <run-id> \
  --by <employee-id> \
  --reason "历史失败已复核"
```

## 13. 给新 Agent 的最小上手脚本

复制执行：

```bash
set -euo pipefail

cd ~/openclaw/workspace-xmanx/projects/super-ai-company-kernel

echo "== git =="
git status --short --branch
git rev-list --left-right --count main...origin/main

echo "== health =="
bin/companyctl doctor --summary || true

echo "== employees =="
bin/companyctl employee list

echo "== direct codex =="
bin/companyctl message direct \
  --from claude-code \
  --to codex \
  --body "只回复：CODEX_OK" \
  --timeout 60

echo "== tasks =="
bin/companyctl task list --agent codex
```

## 14. 给 PM/Supervisor Agent 的流程

1. 读取任务目标。
2. 判断需要哪个员工。
3. 用 conversation 澄清需求。
4. 用 task submit 创建执行任务。
5. 用 adapter-runs 和 task show 盯状态。
6. 无进度时 correction。
7. 失败时 block/retry/reassign。
8. 完成时检查 evidence。
9. 给 owner 输出一句人类可读总结。

总结格式：

```text
Codex 完成 <任务名>，状态 completed，证据在 <path>。
```

或：

```text
Codex 阻塞在 <原因>，需要 <谁> 提供 <什么>，下一步建议 <动作>。
```

## 15. 禁止事项

所有 Agent 都禁止：

- 不要直接改 `company.sqlite`。
- 不要直接改任务状态绕过 `companyctl`。
- 不要把 ACK / heartbeat / stdout 当完成。
- 不要把 candidate 员工当 active。
- 不要无审批执行外发、删除、付款、发布。
- 不要碰 owner 禁止的线上目录。
- 不要私下改 OpenClaw bus 关键状态。
- 不要在没有 evidence 时宣称任务完成。

## 16. 相关文档

- `docs/COMPANY_KERNEL_USAGE.md`：人类操作者使用说明。
- `docs/AGENT_ONBOARDING.md`：新增员工接入说明。
- `docs/CODEX_DEV_GUIDE.md`：Codex 执行开发任务说明。
- `docs/CLAUDE_CODEX_TERMINAL_COMMUNICATION.md`：Claude 通过终端与 Codex 通信说明。
- `docs/RUNTIME_ADAPTERS.md`：Adapter 设计。
- `docs/OPENCLAW_EXTERNAL_AGENT_BRIDGE.md`：OpenClaw bridge。

