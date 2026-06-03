# Company Kernel

通用 AI 公司内核。用于把 OpenClaw、Hermes、Codex、Claude、Trae、Antigravity 等工具注册成公司员工，并用统一任务协议互相通信。

## Goal

项目总目标见：[docs/SUPER_AI_COMPANY_GOAL.md](docs/SUPER_AI_COMPANY_GOAL.md)

## Quick Start

```bash
cd /Users/shift/openclaw/company-kernel
python3 -m company_kernel.companyctl doctor
python3 -m company_kernel.companyctl employee create --id hermes --name Hermes --role supervisor --runtime hermes --workspace /Users/shift/.hermes
python3 -m company_kernel.companyctl employee create --id codex --name Codex --role developer --runtime codex --workspace /Users/shift/openclaw/workspace-xmanx/projects/openclaw-codex-controller
python3 -m company_kernel.companyctl conversation start --from hermes --participants hermes,codex,claude --title "方案讨论" --body "请讨论第一版实现方案"
python3 -m company_kernel.companyctl task submit --from hermes --to codex --title "测试互通" --description "Codex 领取任务并回传 evidence"
python3 -m company_kernel.companyctl runtime verify-adapters --agents hermes,codex,claude,trae,antigravity,nestcar
```

## Test

```bash
python3 -m unittest discover -s tests -v
```

## Commands

```bash
python3 -m company_kernel.companyctl employee list
python3 -m company_kernel.companyctl conversation list --agent codex
python3 -m company_kernel.companyctl conversation reply --from codex --conversation-id <id> --body "收到"
python3 -m company_kernel.companyctl workflow validate --workflow video_pipeline_5round
python3 -m company_kernel.companyctl workflow run --workflow video_pipeline_5round --run-id wf-video-ops-maker-publisher-003
python3 -m company_kernel.companyctl scheduler run
python3 -m company_kernel.companyctl approval request --from hermes --action external_send --reason "需要外发客户消息" --target nestcar --risk P1
python3 -m company_kernel.companyctl lock acquire --agent codex --resource task:<id> --lease-seconds 1800
python3 -m company_kernel.companyctl repair reset-stale-claims
python3 -m company_kernel.company_daemon --once
python3 -m company_kernel.company_dashboard
python3 -m company_kernel.companyctl project create --project-id super-ai-company-kernel --title "Super AI Company Kernel" --owner openclaw-main
python3 -m company_kernel.companyctl task list
python3 -m company_kernel.companyctl task claim --agent codex
python3 -m company_kernel.companyctl task done --agent codex --task-id <id> --summary "完成" --evidence /path/to/report.md
python3 -m company_kernel.companyctl task reopen --task-id <id> --by openclaw-main --reason "blocker removed"
python3 -m company_kernel.companyctl task reassign --task-id <id> --by openclaw-main --to hermes --reason "better runtime"
python3 -m company_kernel.companyctl heartbeat --agent codex
python3 -m company_kernel.companyctl runtime test --runtime hermes
bin/company-adapter-worker --agent codex --dry-run
bin/company-codex-adapter
bin/company-codex-adapter --execute --sandbox read-only
bin/company-openclaw-adapter --agent nestcar
bin/company-openclaw-adapter --agent nestcar --execute
bin/company-hermes-adapter
bin/company-hermes-adapter --execute
bin/company-claude-adapter
bin/company-claude-adapter --execute
bin/company-trae-adapter
bin/company-trae-adapter --execute
bin/company-antigravity-adapter
bin/company-antigravity-adapter --execute
bin/company-api-gateway --host 127.0.0.1 --port 8765
bin/company-api-rpc --host 127.0.0.1 --port 8766
bin/company-api-grpc --host 127.0.0.1 --port 8767
```

## Boundary

Company Kernel 管制度和通信。OpenClaw、Hermes、Codex、Claude、Trae、Antigravity 都只是 runtime adapter。

## Employee Communication Config

员工别名、互通关系、接力策略放在 `config/company_communications.json`。
任务完成后的自动接力规则放在 `config/hooks.json`。
Hook action 可用 `requires_approval` 标记高风险动作；未批准时 `scheduler run` 会阻断该动作并自动生成 pending approval。已完成的 hook action 会记录在 `hook_action_runs`，事件重跑不会重复执行。

通信策略默认是 `policy.mode=open`：任意已注册员工都能互发消息和派任务，除非配置了 `blocked_talk_to` / `blocked_assign_to`。
如果切到 `policy.mode=strict`，则必须显式配置 `can_talk_to` / `can_assign_to`，否则会被 `companyctl` 拦截。

添加员工只需要命令创建，再按需配置通信：

```bash
bin/companyctl runtime register --runtime cursor --notes "Cursor IDE adapter placeholder"
bin/companyctl employee onboard \
  --id cursor \
  --name Cursor \
  --role ide-developer \
  --runtime cursor \
  --workspace /Users/shift/.cursor \
  --alias cursor \
  --skills code-editing,review \
  --tools companyctl \
  --task-types engineering \
  --can-talk-to codex,hermes \
  --can-assign-to codex \
  --channel engineering \
  --create-test-task
bin/companyctl employee create --id video-reviewer --name "Video Reviewer" --role reviewer --runtime openclaw --workspace /Users/shift/openclaw/workspace-video-reviewer
bin/companyctl employee show --id video-reviewer
bin/companyctl employee capabilities --id video-reviewer --add-skill review --add-tool "oc bus"
bin/companyctl employee permissions --id video-reviewer --can-modify-kernel false --requires-approval-for "external_send,payment,compensation"
bin/companyctl communication show --agent video-ops
bin/companyctl communication check --from video-ops --to video-creator --action assign
bin/companyctl communication check --from maker --to ops --action talk
```

每个员工目录会包含 `profile.json`、`capabilities.json`、`permissions.json`、`rules.md`、`heartbeat.json` 和 inbox/outbox/reports。

按能力自动选人和派工：

```bash
bin/companyctl employee match --skills testing,review --task-type engineering
bin/companyctl task route --from openclaw-main --title "修复测试失败" --description "选择最合适员工处理" --skills testing --task-type engineering
```

统一 adapter dry-run 验收：

```bash
bin/companyctl runtime verify-adapters --agents hermes,codex,claude,trae,antigravity,nestcar
```

该命令会给目标员工创建验收任务，运行对应 adapter dry-run，检查任务完成、evidence 文件和 heartbeat。默认不启动真实外部工具；加 `--execute` 才会走真实 runtime。

高风险 route 或直接 submit 都会先生成审批，不会直接创建任务；批准后带 approval id 重跑：

```bash
bin/companyctl policy show
bin/companyctl task submit --from openclaw-main --to chindahotpot --title "发布客户通知" --description "需要外发给客户"
bin/companyctl approval approve --approval-id <id> --by openclaw-main --reason "证据充分"
bin/companyctl task submit --from openclaw-main --to chindahotpot --title "发布客户通知" --description "需要外发给客户" --approval-id <id>
bin/companyctl task route --from openclaw-main --title "发布客户通知" --description "需要外发给客户" --skills business-ops --requires-approval external_send
bin/companyctl approval approve --approval-id <id> --by openclaw-main --reason "证据充分"
bin/companyctl task route --from openclaw-main --title "发布客户通知" --description "需要外发给客户" --skills business-ops --requires-approval external_send --approval-id <id>
```

高风险动作和关键词配置在 `config/policy.json`，普通员工应读取或提交 RFC，不应直接修改内核代码。

## Kernel Guard

保护区配置在 `config/protected_paths.json`，普通员工变更保护区必须先提交 RFC。

```bash
bin/companyctl guard check --path config/policy.json
bin/companyctl guard check --changed-file rfcs/20260603-change-policy.md
```

任务声明会修改保护区时，领取任务前会自动 guard；没有 RFC 会阻止 claim：

```bash
bin/companyctl rfc create --rfc-id rfc-change-policy --title "修改策略" --by codex --paths config/policy.json --reason "需要调整高风险关键词"
bin/companyctl rfc list --status pending
bin/companyctl rfc show --rfc rfc-change-policy
bin/companyctl rfc approve --rfc rfc-change-policy --by openclaw-main --reason "批准"
bin/companyctl task submit --from openclaw-main --to codex --title "修改策略" --changed-files config/policy.json --rfc rfc-change-policy
bin/companyctl task claim --agent codex
```

当前内置别名：

- `ops` -> `video-ops`
- `maker` -> `video-creator`
- `publisher` -> `video-publisher`

视频生产 5 轮协作测试：

```bash
bin/companyctl workflow validate --workflow video_pipeline_5round
bin/companyctl workflow run --workflow video_pipeline_5round --run-id wf-video-ops-maker-publisher-003
bin/companyctl conversation show --conversation-id conv-video-pipeline-wf-video-ops-maker-publisher-003
```

事件驱动接力测试：

```bash
bin/companyctl message send --from maker --to ops --body "我应该创作什么视频？视频主题是什么？"
bin/companyctl scheduler run
bin/companyctl message list --agent maker
bin/companyctl task submit --from video-ops --to video-creator --title "hook测试：创作搞笑中文竖版视频" --description "maker 完成后自动派 publisher 发布" --priority P1
bin/companyctl task done --agent video-creator --task-id <maker-task-id> --summary "maker 已完成" --evidence /path/to/maker-result.md
bin/companyctl scheduler run
bin/companyctl scheduler events --pending
bin/companyctl scheduler skip-event --event-id <event_id> --by openclaw-main --reason "验证事件，无需自动动作"
```

`message send` 会写入 `company_events`。因此员工发问、员工回复、完成任务后的接力都可以用 `config/hooks.json` 配置，不需要改内核代码。当前示例是：maker 问 ops 视频主题，scheduler 自动让 ops 回复创作要求并写 maker 心跳。

带审批 gate 的 hook 会停在 pending event，批准后重跑 scheduler：

```bash
bin/companyctl approval approve --approval-id approval-publish-<task-id> --by openclaw-main --reason "允许发布"
bin/companyctl scheduler run
```

## Approval Center

高风险动作必须先进入审批中心：付款、赔偿、工资、处罚、外发消息、生产部署、密钥变更、内核变更。

```bash
bin/companyctl approval request --from hermes --action external_send --reason "需要外发客户消息" --target nestcar --risk P1 --task-id <task-id>
bin/companyctl approval list --status pending
bin/companyctl approval approve --approval-id <id> --by openclaw-main --reason "证据充分"
bin/companyctl approval deny --approval-id <id> --by openclaw-main --reason "证据不足"
bin/companyctl approval show --approval-id <id>
```

## Recovery

任务领取会写 `task:<id>` 租约锁。员工中断后可释放过期锁并把过期 claimed 任务恢复为 submitted：

```bash
bin/companyctl lock list
bin/companyctl lock unlock-stale
bin/companyctl repair reset-stale-claims
```

## Long Task Delegation

长任务可以拆成多个子任务，分给不同员工执行，全部完成并有 evidence 后再回收父任务：

```bash
bin/companyctl task submit --from openclaw-main --to video-ops --title "制作并发布短视频项目" --task-id task-video-project-001
bin/companyctl task show --task-id task-video-project-001
bin/companyctl task split --task-id task-video-project-001 --by video-ops \
  --item "maker|创作搞笑中文竖版视频|完成成片并提交 evidence|P1" \
  --item "publisher|发布短视频|根据 maker evidence 发布并提交报告|P1" \
  --child-id-prefix task-video-project-001-child
bin/companyctl task children --task-id task-video-project-001
bin/companyctl task collect --task-id task-video-project-001 --agent video-ops --summary "子任务已全部完成"
```

模型或员工也可以先生成 JSON 分解计划，再一次性提交：

```bash
bin/companyctl task split --task-id task-video-project-001 --by video-ops --plan /path/split-plan.json --child-id-prefix task-video-project-001-child
```

`split-plan.json` 支持 JSON list，或 `{ "items": [...] }`，每项包含 `target`、`title`、`description`、`priority`。`task split` 会写入子任务关系、子任务 metadata、`task.split` 事件和审计记录。

## Daemon

`bin/company-daemon` 是本机巡检循环，默认只运行 repair、scheduler 和 heartbeat，不启动真实外部工具。
`config/daemon.json` 里的 `heartbeat_agents` 是固定员工心跳，`heartbeat_runtimes` 会动态覆盖指定 runtime 下所有 active 员工；设为 `["*"]` 时覆盖所有 active 员工，新增员工后不用手改心跳列表。
`config/daemon.json` 里的 `adapter_workers` 可以启用员工 worker，让 daemon 每轮自动领取任务、执行 adapter、写 evidence、回传状态。
每个 worker 支持 `max_tasks_per_tick`，独立状态写到 `state/daemon/workers/<agent>.json`，汇总运行历史写入 SQLite `adapter_runs` 并显示在 dashboard；未确认的失败 adapter run 会让 `companyctl doctor` 报 `adapter_failures`。
任务提交会生成 `trace_id`，并贯穿 task metadata、`company_events`、`adapter_runs` 和 dashboard，用来追踪派发、hook、adapter 执行链路。
`bin/company-trace --task-id <task-id>` 会导出同一条链路的 JSON 和 HTML 时间线到 `state/traces/<trace_id>.html`，用于查看派发、hook、adapter run 的火焰图式耗时视图。
daemon 会按 `run_retries` 和 worker `retry_policy` 对到期失败 adapter run 自动调用 `runtime retry-adapter-run`，默认指数退避写入 `adapter_runs.next_retry_at`，人工确认后不再自动重试。
`companyctl doctor` 会读取 `state/daemon/last-run.json` 检查 daemon 是否在 10 分钟内运行；如果超过阈值会报 `daemon_stale`，因此定时器应按 10 分钟以内周期执行 daemon。
`companyctl doctor --summary` 还会显示 launchd 模板和 `~/Library/LaunchAgents` 安装状态，并给出 install/verify 命令；未安装只作为可观测字段，不直接让健康检查失败。

```bash
bin/company-daemon --once --summary
bin/company-daemon --once --enable-worker codex --summary
bin/company-daemon --iterations 10 --interval 30
bash bin/company-daemon-install-launchd
bash bin/company-daemon-uninstall-launchd
bin/companyctl doctor --summary
bin/companyctl doctor --summary --strict-launchd
bin/companyctl runtime adapter-runs --status failed --unacknowledged-only
bin/companyctl runtime adapter-run show --run-id <adapter-run-id> --summary
bin/companyctl runtime ack-adapter-run --run-id <adapter-run-id> --by openclaw-main --reason "已复核，可消警"
bin/companyctl runtime retry-adapter-run --run-id <adapter-run-id> --by openclaw-main --reason "修复后重试"
bin/company-trace --task-id <task-id>
```

`adapter_runs.task_id` 会记录本次 adapter 处理的任务，旧记录会从 `result_json` 自动回填；`retry-adapter-run` 默认用该字段恢复任务，仍缺失时可补 `--task-id`。
Codex/Hermes/Claude/Trae 真实执行会把完整 stdout/stderr 写入员工 report 目录，并只把短输出摘要写进任务 summary/blocker。告警侧建议优先读取 `doctor --summary` 和 `adapter-run show --summary`，避免把完整 stdout/result_json 发给模型。

最小自动执行闭环：

```bash
bin/companyctl task submit --from openclaw-main --to codex --task-id task-daemon-worker-smoke --title "daemon worker smoke"
bin/company-daemon --once --enable-worker codex --summary
bin/companyctl task show --task-id task-daemon-worker-smoke
bin/companyctl runtime adapter-runs --agent codex --status ok --limit 3
```

这条 smoke 会证明 daemon 能临时启用 `codex` worker，自动领取任务、写 evidence、完成任务、写 heartbeat，并把本次执行写入 `adapter_runs` 供 dashboard/doctor/告警读取。
上线验收可使用 `doctor --summary --strict-launchd`，把 launchd 未安装或安装文件与模板不一致作为失败 gate。

配置文件：`config/daemon.json`  
状态文件：`state/daemon/last-run.json`  
worker 状态：`state/daemon/workers/<agent>.json`  
日志文件：`logs/daemon.log`
launchd 模板：`config/launchd/ai.openclaw.company-kernel.daemon.plist`，默认 300 秒运行一次 daemon。
dashboard 会显示 Runtime Health，包括 daemon last-run、launchd 安装状态和修复命令。

## API Gateway

`bin/company-api-gateway` 提供轻量 REST 服务层，当前复用 `companyctl` 的制度和状态写入逻辑，作为未来多机/分布式部署前的 API 边界。

```bash
bin/company-api-gateway --host 127.0.0.1 --port 8765
curl http://127.0.0.1:8765/v1
curl http://127.0.0.1:8765/v1/openapi.json
curl http://127.0.0.1:8765/v1/health
curl -X POST http://127.0.0.1:8765/v1/tasks \
  -H 'Content-Type: application/json' \
  --data '{"from":"openclaw-main","to":"codex","title":"REST task","description":"created through API Gateway"}'
curl http://127.0.0.1:8765/v1/tasks/<task-id>
curl -X POST http://127.0.0.1:8765/v1/tasks/<task-id>/claim \
  -H 'Content-Type: application/json' \
  --data '{"agent":"codex"}'
curl -X POST http://127.0.0.1:8765/v1/tasks/<task-id>/done \
  -H 'Content-Type: application/json' \
  --data '{"agent":"codex","summary":"已完成","evidence":"/path/report.md"}'
curl -X POST http://127.0.0.1:8765/v1/tasks/<task-id>/reopen \
  -H 'Content-Type: application/json' \
  --data '{"by":"openclaw-main","reason":"blocker removed"}'
curl -X POST http://127.0.0.1:8765/v1/tasks/<task-id>/reassign \
  -H 'Content-Type: application/json' \
  --data '{"by":"openclaw-main","to":"hermes","reason":"better owner"}'
curl -X POST http://127.0.0.1:8765/v1/conversations \
  -H 'Content-Type: application/json' \
  --data '{"from":"hermes","participants":"hermes,codex,claude","title":"方案讨论","body":"请讨论下一步"}'
curl -X POST http://127.0.0.1:8765/v1/approvals \
  -H 'Content-Type: application/json' \
  --data '{"from":"hermes","action":"external_send","reason":"需要外发审批","target":"nestcar","risk":"P1"}'
curl -X POST http://127.0.0.1:8765/v1/projects \
  -H 'Content-Type: application/json' \
  --data '{"project_id":"project-rest","title":"REST Project","owner":"openclaw-main","goal":"跨机项目治理"}'
curl -X POST http://127.0.0.1:8765/v1/projects/project-rest/plan-items \
  -H 'Content-Type: application/json' \
  --data '{"plan_id":"plan-rest-001","title":"接入远程员工","owner":"codex"}'
curl http://127.0.0.1:8765/v1/projects/project-rest/review
curl -X POST http://127.0.0.1:8765/v1/projects/project-rest/accept \
  -H 'Content-Type: application/json' \
  --data '{"by":"openclaw-main","summary":"验收通过"}'
curl -X POST http://127.0.0.1:8765/v1/locks/acquire \
  -H 'Content-Type: application/json' \
  --data '{"agent":"codex","resource":"task:<task-id>","lease_seconds":1800}'
curl http://127.0.0.1:8765/v1/locks?agent=codex
curl -X POST http://127.0.0.1:8765/v1/runtimes \
  -H 'Content-Type: application/json' \
  --data '{"runtime":"cursor","command":"cursor-agent","notes":"Cursor adapter placeholder"}'
curl -X POST http://127.0.0.1:8765/v1/employees \
  -H 'Content-Type: application/json' \
  --data '{"id":"cursor-dev","name":"Cursor Dev","role":"developer","runtime":"cursor","workspace":"/Users/shift/openclaw/workspace-cursor"}'
curl -X POST http://127.0.0.1:8765/v1/employees/cursor-dev/capabilities \
  -H 'Content-Type: application/json' \
  --data '{"set_skills":"engineering,review","add_tool":["cursor","git"],"set_task_types":"code,review"}'
curl -X POST http://127.0.0.1:8765/v1/employees/cursor-dev/permissions \
  -H 'Content-Type: application/json' \
  --data '{"can_submit_tasks":"false","requires_approval_for":"external_send,payment"}'
```

`/v1` 返回服务发现、能力列表、治理约束和端点清单；`/v1/openapi.json` 返回机器可读 OpenAPI 3.1 契约。已覆盖的端点：`/v1/health`、`/v1/doctor`、`/v1/employees`、`/v1/employees/<id>/capabilities|permissions`、`/v1/runtimes`、`/v1/tasks`、`/v1/tasks/<id>/claim|done|block|reopen|reassign`、`/v1/messages`、`/v1/conversations`、`/v1/approvals`、`/v1/projects`、`/v1/projects/<id>/review|accept`、`/v1/locks`、`/v1/heartbeats`、`/v1/adapter-runs`。

`bin/company-api-rpc` 提供同一套治理路由的 JSON-RPC 2.0 服务层，默认端口 `8766`，用于非 HTTP path 风格的远端员工接入。`bin/company-api-grpc` 提供同一组 `Describe/Get/Post` gRPC 服务逻辑，默认端口 `8767`；运行真实网络 gRPC server 需要安装 `grpcio`。当前 server 使用 generic gRPC handler，payload 为 JSON bytes，语义与 `docs/company_kernel.proto` 的 `path/query/body_json/status/body_json` 字段对齐。

```bash
curl -s http://127.0.0.1:8766/rpc
curl -s -X POST http://127.0.0.1:8766/rpc \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":"health","method":"company.get","params":{"path":"/v1/health","query":{}}}'
curl -s -X POST http://127.0.0.1:8766/rpc \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":"task","method":"company.post","params":{"path":"/v1/tasks","body":{"from":"openclaw-main","to":"codex","title":"RPC task","description":"remote dispatch"}}}'
bin/company-api-grpc --check
bin/company-api-grpc --host 127.0.0.1 --port 8767
```

## Sandbox Isolation

`config/sandbox_profiles.json` 定义执行器沙箱策略。Codex/Hermes adapter 默认 `--isolation none`，显式传 `--isolation docker` 或 `--isolation firejail` 才会包装执行命令。

```bash
bin/company-codex-adapter --execute --sandbox workspace-write --isolation docker --sandbox-profile default
bin/company-hermes-adapter --execute --isolation firejail --sandbox-profile default
```

当前已实现可测试的命令构造和 profile 加载；是否安装 Docker/Firejail、镜像内容和真实容器运行由部署环境决定。

安全 dry-run worker 验证：

```bash
bin/companyctl task submit --from openclaw-main --to codex --title "daemon worker dry-run 验证" --description "自动领取并回传 evidence"
bin/company-daemon --once
bin/companyctl task list --agent codex
```

## Dashboard

生成本地静态操作台：

```bash
bin/company-dashboard
open /Users/shift/openclaw/company-kernel/state/dashboard.html
```

操作台包含员工、心跳、项目目标/验收/复盘、任务、审批、事件和锁状态。

## Projects

项目用于把目标、计划、验收标准和任务队列绑定起来：

```bash
bin/companyctl project create --project-id super-ai-company-kernel --title "Super AI Company Kernel" --owner openclaw-main --goal "统一 AI 员工协作"
bin/companyctl project plan-add --project-id super-ai-company-kernel --title "接入 Codex Adapter" --owner codex --task-id <task-id>
bin/companyctl project plan-status --project-id super-ai-company-kernel --plan-id <plan-id> --status done
bin/companyctl project plan-list --project-id super-ai-company-kernel
bin/companyctl project link-task --project-id super-ai-company-kernel --task-id <task-id>
bin/companyctl project show --project-id super-ai-company-kernel
bin/companyctl project review --project-id super-ai-company-kernel
bin/companyctl project list --status active
```

## Schema Migrations

`companyctl` 和 `company-dashboard` 会自动执行轻量 SQLite 迁移，并把已应用迁移记录到 `schema_migrations`。

## Adapter Worker

`bin/company-adapter-worker` 是第二阶段的最小 worker。它会领取指定员工的一个待处理任务，写入 evidence report，并通过 `companyctl task done/block` 回写状态。

默认建议先用 `--dry-run`，真实启动 OpenClaw/Hermes/Codex/Claude/Trae/Antigravity 需要后续专用 adapter。

## Codex Adapter

`bin/company-codex-adapter` 会领取 `codex` 员工的一个待处理任务，生成 Codex task card，并写入 evidence。

默认不启动 Codex：

```bash
bin/company-codex-adapter
```

真实执行 Codex：

```bash
bin/company-codex-adapter --execute --sandbox read-only
```

## OpenClaw Adapter

`bin/company-openclaw-adapter` 会领取指定 OpenClaw 员工的 Company Kernel 任务，转换成 OpenClaw 旧 `ops/agent_bus` payload。

默认不写真实 OpenClaw bus：

```bash
bin/company-openclaw-adapter --agent nestcar
```

真实提交到 OpenClaw：

```bash
bin/company-openclaw-adapter --agent nestcar --execute
```

`--execute` 会写 OpenClaw legacy bus，必须先通过 `external_send` 审批。未审批时 adapter 会生成 pending approval，并保留任务为 claimed，批准后重跑 adapter 才会写 bus。

## Hermes Adapter

`bin/company-hermes-adapter` 会领取 `hermes` 员工的 Company Kernel 任务，生成 Hermes oneshot prompt，并写入 evidence。

默认不启动 Hermes：

```bash
bin/company-hermes-adapter
```

真实执行 Hermes：

```bash
bin/company-hermes-adapter --execute
```

## Claude Adapter

`bin/company-claude-adapter` 会领取 `claude` 员工的 Company Kernel 任务，生成 Claude print prompt，并写入 evidence。

默认不启动 Claude：

```bash
bin/company-claude-adapter
```

真实执行 Claude：

```bash
bin/company-claude-adapter --execute
```

## Trae Adapter

`bin/company-trae-adapter` 会领取 `trae` 员工的 Company Kernel 任务，生成 Trae chat prompt，并写入 evidence。

默认不启动 Trae：

```bash
bin/company-trae-adapter
```

真实执行 Trae：

```bash
bin/company-trae-adapter --execute
```

## Antigravity Adapter

`bin/company-antigravity-adapter` 会领取 `antigravity` 员工的 Company Kernel 任务，生成 GUI task brief，并写入 evidence。

默认不打开 Antigravity：

```bash
bin/company-antigravity-adapter
```

真实执行只打开 App，不伪造完成：

```bash
bin/company-antigravity-adapter --execute
bin/company-antigravity-adapter --complete --task-id <task-id> --summary "GUI 已完成" --evidence /path/evidence.md
bin/company-antigravity-adapter --block --task-id <task-id> --blocker "GUI 登录失效"
```
