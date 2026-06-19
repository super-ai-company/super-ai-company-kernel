# Super AI Company Kernel 使用说明

## 1. 当前定位

Company Kernel 是 AI 员工管理内核，用来把 Codex、Hermes、OpenClaw、Claude、Trae、Antigravity、本地脚本等统一成“员工”。

它负责：

- 员工注册、状态、心跳
- 任务派发、认领、完成、阻塞、重试
- 消息、对话、审批、锁、审计
- evidence 证据、成本、运行记录
- API / CLI / Dashboard 统一查看

它不应该替代 OpenClaw 内部业务总线；OpenClaw 是运行时员工或管理层之一，通过 adapter / bridge 接入。

## 2. 本机地址

当前开发克隆目录：

```bash
~/openclaw/workspace-xmanx/projects/super-ai-company-kernel
```

当前本机实际运行目录：

```bash
$OPENCLAW_COMPANY_KERNEL_ROOT
```

GitHub：

```bash
https://github.com/super-ai-company/super-ai-company-kernel.git
```

本机后台：

```bash
http://127.0.0.1:8765/
```

本机 Dashboard：

```bash
http://127.0.0.1:8780/dashboard.html
```

## 3. 每次使用前先检查

```bash
cd ~/openclaw/workspace-xmanx/projects/super-ai-company-kernel
git status --short --branch
git remote -v
bin/companyctl doctor --summary
```

如果本机 API / daemon 实际指向 `$OPENCLAW_COMPANY_KERNEL_ROOT`，则先切到真实运行目录：

```bash
cd $OPENCLAW_COMPANY_KERNEL_ROOT
git status --short --branch
git remote -v
bin/companyctl doctor --summary
```

重点看：

- 当前分支是否是 `main`
- 本地 HEAD 是否和 `origin/main` 一致
- doctor 是否 `ok=true`
- heartbeat stale/missing 是否为 0
- daemon / launchd 是否指向当前项目目录
- `launchd.installed_root` 是否和你正在操作的目录一致

## 4. 启动服务

API Gateway：

```bash
bin/company-api-gateway --host 127.0.0.1 --port 8765
```

Dashboard 静态服务：

```bash
bin/company-dashboard-server
```

Daemon 跑一轮：

```bash
bin/company-daemon --once --summary
```

macOS 常驻安装：

```bash
bash bin/company-daemon-install-launchd
```

验证端口：

```bash
lsof -nP -iTCP:8765 -sTCP:LISTEN
lsof -nP -iTCP:8780 -sTCP:LISTEN
```

## 5. 常用命令

查看员工：

```bash
bin/companyctl employee list
```

查看任务：

```bash
bin/companyctl task list
```

提交任务：

```bash
bin/companyctl task submit \
  --from owner \
  --to codex \
  --title "smoke 测试" \
  --description "请回复 STATUS: completed 并提交 evidence"
```

跑 daemon 执行任务：

```bash
bin/company-daemon --once --summary
```

查看审批：

```bash
bin/companyctl approval list --status pending
```

批准审批：

```bash
bin/companyctl approval approve --approval-id <id> --by owner --reason "确认执行"
```

拒绝审批：

```bash
bin/companyctl approval deny --approval-id <id> --by owner --reason "证据不足"
```

查看系统健康：

```bash
bin/companyctl doctor --summary
```

## 6. 新增员工

推荐使用一条命令：

```bash
bin/company-add-employee \
  --id codex \
  --name Codex \
  --role developer \
  --runtime codex \
  --workspace /absolute/path/to/project \
  --skills "coding,review,testing" \
  --enable-worker
```

说明：

- 不加 `--execute`：只注册或 dry-run，安全。
- 加 `--execute`：worker 会真实调用运行时。
- 真实外发、删除、发布、付款、规则修改等高风险动作必须走审批。

## 7. Codex 员工使用方式

Codex 适合工程任务。

任务描述里可以写：

```text
工作区: /absolute/path/to/project
目标: 修复后端接口并补测试
要求:
1. 先读取项目现状
2. 修改代码
3. 运行测试
4. 最终输出 STATUS: completed 或 STATUS: blocked - 原因
5. 提交 evidence 路径
```

注意：

- `exit code 0` 不等于完成。
- Codex 最终必须输出 `STATUS: completed` 或 `STATUS: blocked - <reason>`。
- 任务必须绑定 evidence，否则不能算完成。

## 8. OpenClaw 接入方式

OpenClaw 不要被改成 Company Kernel 的内部实现。

正确关系：

```text
Company Kernel = 任务账本 / 审批 / 审计 / 状态机
OpenClaw = 业务运行时 / agent bus / Telegram 通道
Bridge Adapter = 两者之间的边界
```

OpenClaw 真实业务通信仍优先走它自己的 `agent_bus` 和 Telegram 通道；Company Kernel 负责记录任务、状态、证据、审批与可视化。

查看 OpenClaw bridge 文档：

```bash
cat docs/OPENCLAW_EXTERNAL_AGENT_BRIDGE.md
```

## 9. Dashboard 使用

打开：

```bash
http://127.0.0.1:8780/dashboard.html
```

重点看：

- Employees：员工是否 online / active / candidate
- Tasks：任务状态和 owner action
- Conversations：对话与多轮消息
- Approvals：待审批动作
- Events / Audit：事件账本
- Evidence：任务证据

Dashboard 只能作为控制台，不应该绕过 Kernel 私发关键任务。

## 10. GitHub 同步规则

查看本地和远端是否一致：

```bash
git fetch origin --prune
git rev-parse HEAD
git rev-parse origin/main
git rev-list --left-right --count main...origin/main
```

解释：

- `0 0`：本地 main 和 GitHub main 完全一致。
- `0 N`：本地落后 GitHub N 个提交。
- `N 0`：本地领先 GitHub N 个提交。
- `A B`：本地和远端分叉，需要人工合并。

同步前必须先看：

```bash
git status --short --branch
```

不要把这些运行态文件误提交：

- `config/company_communications.json`
- `employees/*/profile.json`
- `reports/*`
- `state/*`
- 本机新扫描出来但未确认的 runtime-only employees

## 11. 测试与验收

最小验收：

```bash
python3 -B -m unittest discover -s tests
bin/companyctl doctor --summary
curl -fsS http://127.0.0.1:8765/v1/health
```

Dashboard 验收：

```bash
curl -fsS http://127.0.0.1:8780/dashboard.html -o /tmp/company-dashboard.html
```

重点确认：

- API 正常
- doctor 正常
- Dashboard 能读取真实数据
- 任务可以从 submitted -> running/blocked/completed
- completed 必须有 evidence

## 12. 安全边界

不要自动批准：

- 付款、赔偿、罚款、工资、押金、保险
- 外发客户消息
- 删除文件
- 修改规则 / policy
- 发布到公网
- 使用敏感文件、token、API key

敏感信息必须走环境变量，不要写进代码或 profile。

## 13. 当前本机特别注意

本机可能同时存在：

- GitHub 最新代码
- 本机 OpenClaw 运行态文件
- Dashboard 本地修复
- 新扫描出来的 employee profile

因此更新前必须先确认 `git status`。

如果本地有未提交修改，不要直接 `git pull --rebase` 或 `git reset`。先决定：

1. 哪些是产品代码，应该提交。
2. 哪些是运行态，应该保留但不提交。
3. 哪些是临时文件，确认后再清理。
