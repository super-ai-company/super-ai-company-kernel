# Claude 通过终端与 Codex 通信说明书

目标：Claude 不打开浏览器、不操作桌面、不点击 Dashboard，只通过终端命令与 Codex 通信、派任务、查进度、收 evidence。

适用对象：

- Claude Code / Claude CLI 操作者
- Codex CLI 员工
- Company Kernel 维护者
- OpenClaw / Hermes 后续接管者

## 1. 关键原则

Claude 不应该直接抢 Codex 的会话、窗口或本机 UI。

稳定路径是：

```text
Claude 终端命令
  -> companyctl / Company Kernel
  -> task ledger / message ledger / adapter run
  -> Codex adapter
  -> codex exec
  -> progress / evidence / task status
  -> Claude 再用 companyctl 查询结果
```

原因：

- 直接操作电脑窗口不可审计，也容易丢状态。
- 直接启动另一个 Codex 会话无法和任务账本绑定。
- Company Kernel 能记录 task_id、attempt、adapter_run、预算、失败、evidence。
- Codex CLI 是本地终端 coding agent，适合由 adapter 在指定 workspace 执行。
- Claude Code 有终端命令能力，适合通过 `companyctl` 操作账本。

## 2. 当前目录

进入项目：

```bash
cd /Users/owner/openclaw/workspace-xmanx/projects/super-ai-company-kernel
```

确认本地和 GitHub 一致：

```bash
git fetch origin --prune
git status --short --branch
git rev-parse HEAD
git rev-parse origin/main
git rev-list --left-right --count main...origin/main
```

期望：

```text
0 0
```

表示本地 `main` 和 GitHub `origin/main` 一致。

## 3. 前置检查

Claude 开始操作前先跑：

```bash
bin/companyctl doctor --summary
bin/companyctl employee list
bin/companyctl runtime verify-adapters --agents codex,claude --allow-candidate
```

如果只想验证 Codex 是否能被真实直连：

```bash
bin/companyctl employee verify-direct --id codex --from claude-code --rounds 3 --timeout 60
```

如果要验证并激活：

```bash
bin/companyctl employee verify-direct --id codex --from claude-code --rounds 3 --timeout 60 --activate
```

判断：

- direct 通过：可以进行短消息通信。
- adapter verify 通过：可以进入任务执行。
- employee active：可以进入正常调度。
- candidate：只能 smoke，不要当正式员工。

## 4. 三种通信方式

### 4.1 短消息直连

用途：

- ping Codex 是否在线
- 让 Codex 简短回答
- 不适合长任务开发

命令：

```bash
bin/companyctl message direct \
  --from claude-code \
  --to codex \
  --body "只回复：CODEX_TERMINAL_OK" \
  --timeout 60
```

要求：

- Claude 必须看 stdout 里的 `reply`。
- 不能只看目标 inbox 文件。
- 如果没有 sender-visible reply，就不算通信成功。

可选写入可追踪 session：

```bash
bin/companyctl message direct \
  --from claude-code \
  --to codex \
  --body "请确认你能通过 Company Kernel 接收 Claude 的终端消息。" \
  --session-key claude-codex-terminal-smoke-001 \
  --timeout 60
```

### 4.2 异步会话

用途：

- Claude 和 Codex 多轮讨论方案
- 不要求 Codex 立刻执行代码
- 适合评审、拆需求、问答

创建会话：

```bash
bin/companyctl conversation start \
  --from claude-code \
  --participants claude-code,codex \
  --title "Claude 与 Codex 终端协作测试" \
  --body "Codex，请用 3 点说明你将如何接收任务、执行、提交 evidence。"
```

查看会话：

```bash
bin/companyctl conversation list --agent claude-code
bin/companyctl conversation list --agent codex
bin/companyctl conversation show --conversation-id <conversation-id>
```

回复会话：

```bash
bin/companyctl conversation reply \
  --from claude-code \
  --conversation-id <conversation-id> \
  --body "收到。下一步请把方案转成可执行 task。"
```

注意：

- 会话是讨论，不等于任务完成。
- 会话内容不能替代 evidence。
- 长任务必须走 task。

### 4.3 真实任务派发

用途：

- 让 Codex 真正改代码、跑测试、提交证据
- Claude 作为 PM / Reviewer 监督

提交任务：

```bash
TASK_ID="claude-to-codex-dev-$(date +%Y%m%d-%H%M%S)"

bin/companyctl task submit \
  --from claude-code \
  --to codex \
  --task-id "$TASK_ID" \
  --title "Claude 派发给 Codex 的终端开发任务" \
  --description "工作区: /absolute/path/to/target-project

目标:
请完成一个最小可验证修改。

要求:
1. 先读取项目结构。
2. 修改必要文件。
3. 运行最小测试。
4. 如果成功，最终输出 STATUS: completed。
5. 如果无法完成，输出 STATUS: blocked - 具体原因。
6. 必须提交 evidence 文件路径。

验收:
Claude 能用 companyctl task show 查询状态，能看到 evidence。" \
  --priority P1
```

立刻触发 Codex adapter 执行：

```bash
bin/company-codex-adapter \
  --agent codex \
  --execute \
  --sandbox workspace-write \
  --model gpt-5.5 \
  --timeout-seconds 1800
```

如果不想立刻执行，可以只等 daemon 自动处理：

```bash
bin/company-daemon --once --summary
```

## 5. Claude 如何查进度

查看任务：

```bash
bin/companyctl task show --task-id "$TASK_ID"
```

查看 Codex 队列：

```bash
bin/companyctl task list --agent codex
```

查看 adapter run：

```bash
bin/companyctl runtime adapter-runs --agent codex --limit 20
```

查看失败 run：

```bash
bin/companyctl runtime adapter-runs --agent codex --status failed --unacknowledged-only
```

查看某次 run 详情：

```bash
bin/companyctl runtime adapter-run show --run-id <run-id>
```

判断任务状态：

- `submitted`：任务已创建，等待处理。
- `claimed` / `running`：Codex 正在执行。
- `completed`：完成，但仍要检查 evidence。
- `blocked`：Codex 明确卡住，需要 Claude 或 owner 处理。
- `failed`：adapter 或执行失败，先看 adapter run。

## 6. Claude 如何纠偏

如果 Codex 偏题或需要补充要求：

```bash
bin/companyctl task correct \
  --task-id "$TASK_ID" \
  --by claude-code \
  --message "请不要继续扩展功能。只完成最小修改，并补充测试与 evidence。"
```

如果当前版本没有 `task correct` 参数差异，先查：

```bash
bin/companyctl task correct --help
```

替代方式：发 direct message 绑定 session：

```bash
bin/companyctl message direct \
  --from claude-code \
  --to codex \
  --session-key "$TASK_ID" \
  --body "纠偏：请回到 task_id=$TASK_ID，只做最小实现并提交 evidence。" \
  --timeout 60
```

## 7. Claude 如何重试或处理失败

列出失败：

```bash
bin/companyctl runtime adapter-runs --agent codex --status failed --unacknowledged-only
```

重试失败 run：

```bash
bin/companyctl runtime retry-adapter-run \
  --run-id <run-id> \
  --by claude-code \
  --reason "Claude 已补充需求，允许 Codex 重试"
```

确认历史失败，不再报警：

```bash
bin/companyctl runtime ack-adapter-run \
  --run-id <run-id> \
  --by claude-code \
  --reason "历史失败已复核，不需要重试"
```

如果是任务级重试：

```bash
bin/companyctl task retry \
  --task-id "$TASK_ID" \
  --by claude-code \
  --reason "修复输入后重试"
```

## 8. Claude 如何验收 evidence

查看任务详情：

```bash
bin/companyctl task show --task-id "$TASK_ID"
```

必须确认：

- 任务状态是 `completed`。
- 有 evidence 路径。
- evidence 文件真实存在。
- evidence 内容匹配当前 task_id。
- Codex 输出里有 `STATUS: completed`。

如果 evidence 缺失或不匹配，Claude 不能宣称完成，应改为：

```bash
bin/companyctl task block \
  --agent claude-code \
  --task-id "$TASK_ID" \
  --blocker "Codex 输出完成但 evidence 缺失或不匹配，需要重跑"
```

## 9. 推荐给 Claude 的标准提示词

Claude 收到“通过终端和 Codex 协作”任务时，直接按这段执行：

```text
你只能使用终端命令，不要打开浏览器，不要操作桌面。

请进入：
/Users/owner/openclaw/workspace-xmanx/projects/super-ai-company-kernel

目标：
通过 Company Kernel 与 codex 员工通信，不直接控制 Codex 窗口。

步骤：
1. 运行 git status、git rev-list --left-right --count main...origin/main，确认当前代码状态。
2. 运行 bin/companyctl doctor --summary。
3. 运行 bin/companyctl employee verify-direct --id codex --from claude-code --rounds 3 --timeout 60。
4. 如果 direct 通过，使用 bin/companyctl task submit --from claude-code --to codex 创建一个明确 task。
5. 用 bin/company-codex-adapter --agent codex --execute --sandbox workspace-write --model gpt-5.5 --timeout-seconds 1800 或 bin/company-daemon --once --summary 触发执行。
6. 用 bin/companyctl task show、runtime adapter-runs 查询状态。
7. 如果 Codex blocked/failed，先记录 blocker，再 correction/retry，不要假装完成。
8. 最终只在看到 completed + evidence 文件存在后，才汇报完成。

硬规则：
- 不把 ACK 当完成。
- 不把 stdout 当 evidence。
- 不绕过 Company Kernel 私下调用 Codex。
- 不修改 OpenClaw 线上目录。
- 不碰 /Users/owner/openclaw/company-kernel，除非 owner 明确授权。
```

## 10. 一条完整 smoke 脚本

Claude 可以直接复制执行：

```bash
set -euo pipefail

cd /Users/owner/openclaw/workspace-xmanx/projects/super-ai-company-kernel

echo "== git =="
git status --short --branch
git rev-list --left-right --count main...origin/main

echo "== doctor =="
bin/companyctl doctor --summary || true

echo "== direct codex =="
bin/companyctl message direct \
  --from claude-code \
  --to codex \
  --body "只回复：CODEX_TERMINAL_OK" \
  --session-key "claude-codex-terminal-smoke" \
  --timeout 60

echo "== submit task =="
TASK_ID="claude-codex-terminal-smoke-$(date +%Y%m%d-%H%M%S)"
bin/companyctl task submit \
  --from claude-code \
  --to codex \
  --task-id "$TASK_ID" \
  --title "Claude to Codex terminal smoke" \
  --description "请只做只读检查：读取当前项目 README.md，总结 3 条能力。最终输出 STATUS: completed，并提交 evidence。" \
  --priority P3

echo "== run codex adapter =="
bin/company-codex-adapter \
  --agent codex \
  --execute \
  --sandbox read-only \
  --model gpt-5.5 \
  --timeout-seconds 600 || true

echo "== task result =="
bin/companyctl task show --task-id "$TASK_ID"

echo "== adapter runs =="
bin/companyctl runtime adapter-runs --agent codex --limit 5
```

## 11. 禁止事项

Claude 不要做：

- 不要打开 Dashboard 点按钮。
- 不要用 AppleScript/鼠标键盘控制 Codex 窗口。
- 不要直接改 `company.sqlite`。
- 不要直接改 `employees/*/profile.json` 把 candidate 改 active。
- 不要把 `message direct ok` 当长任务完成。
- 不要在没有 evidence 的情况下 `task done`。
- 不要操作 `/Users/owner/openclaw/company-kernel` 线上目录，除非 owner 明确授权。

## 12. 参考来源

- 本项目命令：`bin/companyctl --help`、`bin/company-codex-adapter --help`、`bin/company-claude-adapter --help`
- 本项目文档：`docs/CODEX_DEV_GUIDE.md`、`docs/COMPANY_KERNEL_USAGE.md`、`docs/AGENT_ONBOARDING.md`
- OpenAI Codex CLI 官方文档说明 Codex CLI 是本地终端 coding agent，可读写和运行代码。
- Claude Code 官方文档说明 Claude Code 有终端/会话命令体系，适合在终端内驱动工作流。

