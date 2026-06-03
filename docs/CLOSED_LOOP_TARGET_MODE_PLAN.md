# Company Kernel 闭环通信目标模式计划

## 强制规则

任何员工通信都必须按“发起入口 -> 协作员工 -> 发起入口 -> 最终操作员”的轮回验收。当前必须支持两条标准流程：

```text
human -> main -> codex -> main -> human/main
human -> hermes -> codex -> hermes -> main
```

只看到 `message direct ok`、adapter stdout、inbox 文件、dashboard 显示 API ONLINE，都不算闭环成功。必须证明发起入口收到协作员工回执，并且最终操作员收到明确结果。

本机验证和审批流程验证默认交给 Hermes 管理。任何“本机是否最新”“审批链路是否有效”“员工是否可上岗”的验收，先走 Hermes 作为 validation admin；如果任务路由被 approval gate 阻断，Hermes 必须用 direct read-only 验证路径回传 blocker 和证据，不能静默失败。

## 最近已实现

- Dashboard:
  - API ONLINE 实测。
  - 员工 active/candidate 分层。
  - Join Team、Pause/Resume。
  - 会话真实 API 收发已替换大部分模拟逻辑。
  - 输入 `@` 会列 active 员工，例如 `@codex`。
  - 发送失败会提示是否需要其他员工协助。
- 员工身份:
  - 员工 `id/name/alias/display_name` 都作为高优先级身份。
  - `employee show Hermes` 可解析到 `hermes`。
  - `message direct --to "Hermes"` 可解析到 `hermes`。
  - scanner 输出 `employee_directory.all` 和改名命令。
- Skill:
  - OpenClaw/Codex/Hermes/Claude/Trae/Antigravity/local runtime 都补了 ACK、失败反馈、人类通知规则。
  - bootstrap skill 增加 2-4 轮员工握手机制。
  - scanner 生成 `handshake.plan`，可用 `--handshake` 执行。
- 服务:
  - launchd 固定 API `8765`、dashboard `8780`。
  - 当前 health 可达，但有 `pending_events` 时 `/v1/health` 会返回非 2xx。
- 验证管理员:
  - 本机 validation admin / approval-flow admin 默认是 Hermes。
  - scanner 默认 `--installer-agent hermes`，用于员工握手、审批流验证和本机最新代码验收。
  - Codex/OpenClaw 只能在用户显式指定时作为 installer agent 覆盖默认值。

## 仍未完整验收的问题

- Codex 双向通信:
  - 以前只返回 adapter stdout，不等于 `Codex -> OpenClaw` 真回执。
  - 已开始补：Codex direct 写 repo-local progress report，Company Kernel 记录 `codex -> source` receipt message。
  - 但还没完整跑真实链路：`human -> main -> codex -> main -> human/main` 和 `human -> hermes -> codex -> hermes -> main`。
- Hermes -> Telegram:
  - `main -> Hermes` direct 能返回 `ok`，并带 `deliver=true telegram/default/current`。
  - 但没有用可观测证据确认 Telegram 上人类真实收到 Hermes 消息。
- OpenClaw 转发:
  - 需要验证 OpenClaw 能代表人类发给 Codex，并接收 Codex receipt，再回 Telegram/人类。
- 失败闭环:
  - 需要验证 target 不存在、runtime 不在线、policy denied、API offline 时，是否自动通知发起人并列出可协助员工。
- pending events:
  - dashboard/CLI 需要一键处理或 ack，否则 health 一直 `pending_events`。

## 目标模式执行稿

```text
目标：逐项验证 Company Kernel 的真实闭环通信，不允许把 stdout、inbox 文件、API ONLINE 当作最终成功。仓库：/Users/shift/openclaw/company-kernel，分支：codex/dashboard-real-conversations。不要覆盖 main，不要重置用户改动。

强制验收链路：
1. human -> main -> codex -> main -> human/main
2. human -> hermes -> codex -> hermes -> main

步骤：
1. 先确认状态：
   - git status --short --branch
   - curl http://127.0.0.1:8765/v1/health（允许 pending_events，但端口必须可达）
   - curl http://127.0.0.1:8780/dashboard.html
   - bin/companyctl employee list

2. 验证员工身份解析：
   - bin/companyctl employee show Hermes
   - bin/companyctl employee show Codex
   - 用 name/id/alias 各发一次 dry direct，确认 target_agent 是 canonical id。

3. 验证 Codex 双向回执：
   - main/openclaw-main 发 direct 给 codex。
   - Codex 必须产生 repo-local progress report。
   - Company Kernel 必须记录 codex -> main/openclaw-main receipt message。
   - message list 中必须能看到双向两条消息。
   - 不能只看 company-codex-adapter stdout。

4. 验证 main 入口闭环：
   - 从 human 当前 Telegram 或等价 human-facing channel 触发 main。
   - main 发给 codex。
   - codex 回执给 main。
   - main 把结果回给 human，或至少写入 main 的 human-facing 操作队列并可见。
   - 最终证据必须包括：human 可见消息/队列、main 入站/出站记录、Company Kernel 双向 message/receipt、Codex progress report。

5. 验证 Hermes 入口闭环：
   - 从 human 触发 hermes，或用等价测试消息模拟 human -> hermes。
   - hermes 发给 codex。
   - codex 回执给 hermes。
   - hermes 把结果转给 main。
   - 最终证据必须包括：hermes 入站/出站记录、Company Kernel 双向 message/receipt、Codex progress report、main 收到 hermes 结果。

6. 验证失败闭环：
   - 对不存在员工发消息。
   - 对 paused 员工发消息。
   - 模拟 API offline 或 runtime denied。
   - 每种失败必须反馈给发送者，并提示可 @ 的协助员工。

7. 验证 2-4 轮员工握手：
   - scanner 输出 handshake.plan。
   - 默认选择 hermes 作为 installer_agent 和 validation admin。
   - 至少对 codex/hermes/openclaw-main 跑 3 轮。
   - 每轮必须有回复或明确 blocked reason。

8. 修复直到全部通过：
   - 如果 Codex 不能回给 OpenClaw，补 adapter/watchdog/import receipt。
   - 如果 Hermes 不能回 Telegram，补 reply bridge 证据。
   - 如果 health 卡 pending_events，补 ack/处理入口。

9. 验证命令：
   - python3 -B -m unittest discover -s tests -v
   - bin/company-dashboard --variant advanced
   - Node inline scripts 语法检查 state/dashboard.html
   - 浏览器打开 http://127.0.0.1:8780/dashboard.html 实测

完成标准：
- human -> main -> codex -> main -> human/main 有真实证据。
- human -> hermes -> codex -> hermes -> main 有真实证据。
- 员工 name/id/alias 路由不串人。
- 失败会回传发起人，不静默。
- GitHub 分支推送成功，只推 codex/dashboard-real-conversations，不合并 main。
```

## 下一步优先级

1. 完成 Codex receipt importer/watchdog，确保 Codex 不只是 stdout，而是可被 OpenClaw 收到。
2. 给 OpenClaw -> Telegram 人类回传建立统一 evidence。
3. 给 pending events 做 dashboard 一键 ack/处理。
4. 把 handshake 结果写入员工 profile，作为是否 active 的准入证据。
5. 做一键安装后自动：scan -> list employees -> handshake -> smoke -> dashboard URL。
