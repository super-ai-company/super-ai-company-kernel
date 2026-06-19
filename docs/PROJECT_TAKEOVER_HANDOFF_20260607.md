# Super AI Company Kernel 接管交接

更新时间：2026-06-07

## 1. 当前项目根目录

正式继续开发目录：

```text
~/openclaw/workspace-xmanx/projects/super-ai-company-kernel
```

不要把下面这些目录当成当前同步根：

```text
$OPENCLAW_COMPANY_KERNEL_ROOT
~/openclaw/workspace-xmanx/projects/openclaw-company-management
```

它们和当前 GitHub 同步项目不是同一个开发根。

## 2. GitHub 和分支状态

当前实测 Git 远程：

```text
origin https://github.com/super-ai-company/super-ai-company-kernel.git
```

当前分支：

```text
feat/internal-communication-closure
```

当前 HEAD：

```text
ee48fb1 feat: add autonomous supervisor loop for progress delivery
```

最近提交链：

```text
ee48fb1 feat: add autonomous supervisor loop for progress delivery
ec3d0d3 feat: add progress notification delivery pipeline
3e43efc chore: update codex roster and communication aliases
215ec90 feat: add external mirror intake and communication observability
d244817 fix: tighten watchdog receipt matching
34aeb33 feat: close internal communication loop with watchdog remediation
dddaad3 prepare portable public company kernel
```

注意：`docs/HANDOFF.md` 里仍写有旧 GitHub 地址和 `main` 分支策略，当前接管时以本文件和 `git remote -v` / `git status --short --branch` 的实测结果为准。

## 3. 当前定位

这个项目是一个可移植的公司员工协调内核，不是 OpenClaw 的替代品。

核心职责：

- 管理员工 registry、任务生命周期、审批、证据、会话和 dashboard。
- 把 Codex、Hermes、OpenClaw、Antigravity 等作为 runtime adapter / employee 接入。
- 让员工任务从 `submitted / in_progress / blocked / completed` 走证据闭环，避免停在 ACK、receipt、submitted。
- 给人类只发短报告，不发原始 inbox/done/failed 计数噪音。

OpenClaw 仍然是业务 agent runtime；Company Kernel 负责更上层的任务、监督、证据和交接。

## 4. 和 OpenClaw 的关系

OpenClaw 根目录：

```text
~/openclaw
```

main workspace：

```text
~/openclaw/workspace-xmanx
```

Company Kernel 当前通过文档化 bridge 连接 OpenClaw：

```text
docs/OPENCLAW_COMPANY_BRIDGE.md
```

关键边界：

- 不直接修改 OpenClaw 私有 session、bot、memory、hook、bus 运行态。
- 通过 `company-openclaw-adapter` 进入 OpenClaw，执行模式需要 approval。
- OpenClaw 员工包括 `main`、`nestcar`、`chindahotpot`、`invest`、`krothong`、`video-creator`、`video-ops`、`video-publisher`。
- OpenClaw 员工的完成标准必须是 `completed + evidence path` 或 `blocked + single owner/blocker`，不能把 ACK 当完成。

## 5. 当前员工状态

实测命令：

```bash
bin/companyctl employee list
```

当前员工：

```text
antigravity  runtime=antigravity  role=frontend-developer  status=candidate
codex        runtime=codex        role=developer           status=active
hermes       runtime=hermes       role=supervisor          status=active
main         runtime=openclaw     role=operator            status=active
```

注意：

- `antigravity` 只通过了轻量通信，不等于有真实执行证据，所以保持 `candidate`。
- `codex` 是开发员工。
- `hermes` 是 PM / supervisor。
- `main` 是 OpenClaw operator，不应该直接承担所有业务执行。

员工激活规则见：

```text
docs/EMPLOYEE_INTEGRATION_MATRIX.md
```

## 6. 最近新增/更新的主要能力

### 6.1 内部通信闭环

相关提交：

```text
34aeb33 feat: close internal communication loop with watchdog remediation
d244817 fix: tighten watchdog receipt matching
215ec90 feat: add external mirror intake and communication observability
```

能力：

- direct message / recent feed。
- internal watchdog。
- remediation / reroute。
- external mirror intake。
- adapter run summary。
- communication observability panels。

参考：

```text
reports/internal-communication-closure-20260605.md
reports/internal-communication-employee-verification-20260606.md
```

### 6.2 Codex + Hermes 项目经理监督链

新增/相关文件：

```text
company_kernel/codex_pm_supervisor.py
bin/company-codex-pm-supervisor
tests/test_codex_pm_supervisor.py
docs/CODEX_HERMES_PM_SUPERVISION.md
```

目标链路：

```text
Company Kernel task
-> Codex runtime adapter
-> Codex workspace progress JSON
-> Hermes PM supervisor poll
-> Company Kernel task status/report
-> short human-facing event
```

完成标准：

- 不认 ACK、in_progress、heartbeat、adapter dry-run。
- 只认匹配当前 task_id 的 `progress_completed_*.json`。
- 过期 in-progress 要变成 stalled/blocked，不要无限说“在做”。

手动跑一轮：

```bash
bin/company-codex-pm-supervisor --agent codex --stale-minutes 15
```

### 6.3 进度通知和 supervisor loop

相关提交：

```text
ec3d0d3 feat: add progress notification delivery pipeline
ee48fb1 feat: add autonomous supervisor loop for progress delivery
```

当前 dashboard 已有 supervisor loop panel。

当前 `state/supervisor/latest_delivery_loop.json` 显示 supervisor 能扫描，但通知发送仍可能失败，原因是 human notification account/route 没完全配置。

### 6.4 OpenClaw bridge 规则

新增文档：

```text
docs/OPENCLAW_COMPANY_BRIDGE.md
```

人类报告格式要求：

```text
完成了 chindahotpot 的 04店日报检查任务。
nestcar 卡住：SMB share 根目录无权限，owner=main，下一步=修复服务端 ACL。
```

禁止把下面这种 raw counter 发给人类：

```text
inbox: 42 -> 0
done: 71 -> 218
failed: 0 -> 0
```

## 7. 当前工作区未提交状态

当前分支有大量未提交修改和新增文件。接管者第一步必须先审查，不要直接 `git add .`。

主要已修改文件：

```text
.gitignore
README.md
company_kernel/antigravity_adapter.py
company_kernel/codex_adapter.py
company_kernel/companyctl.py
company_kernel/hermes_adapter.py
docs/LOCAL_ENVIRONMENT_AND_SKILLS.md
docs/RUNTIME_ADAPTERS.md
skills/company-employee-antigravity/SKILL.md
skills/company-employee-codex/SKILL.md
skills/company-employee-hermes/SKILL.md
skills/company-employee-openclaw-workspace/SKILL.md
skills/company-employee-openclaw/SKILL.md
skills/openclaw-local-agent-bootstrap/scripts/scan_install.py
tests/test_company_kernel_core.py
```

主要新增文件/目录：

```text
bin/company-codex-pm-supervisor
codex-task-card.md
company_kernel/codex_pm_supervisor.py
design-system/
docs/CODEX_HERMES_PM_SUPERVISION.md
docs/EMPLOYEE_INTEGRATION_MATRIX.md
docs/OPENCLAW_COMPANY_BRIDGE.md
rfcs/20260605-direct-telegram-backend-closure.md
scripts/
tests/test_codex_pm_supervisor.py
```

建议接管后按能力分组提交：

1. Codex/Hermes PM supervisor。
2. OpenClaw bridge 和员工激活规则。
3. Dashboard / communication observability。
4. skill hardening 和 runtime adapter 更新。

## 8. 当前验证结果

实测通过：

```bash
cd ~/openclaw/workspace-xmanx/projects/super-ai-company-kernel
python3 -m unittest discover -s tests -v
```

结果：

```text
Ran 121 tests
OK
```

实测 dashboard 生成通过：

```bash
bin/company-dashboard --variant advanced
```

结果：

```text
output: ~/openclaw/workspace-xmanx/projects/super-ai-company-kernel/state/dashboard.html
employees: 4
active_employees: 3
candidate_employees: 1
tasks: 7
conversations: 1
pending_approvals: 2
pending_events: 62
```

实测不通过：

```bash
bin/companyctl doctor --summary
```

当前问题：

```text
ok=false
issues:
- missing_daemon_state
- missing_heartbeats
- stale_heartbeats
- pending_events
- pending_approvals
```

细节：

```text
missing heartbeat: main
stale heartbeat: codex, hermes
daemon state missing: state/daemon/last-run.json
launchd installed: true
launchd matches_template: false
pending_events: 62
pending_approvals: 2
```

所以当前只能说：代码测试通过，dashboard 可生成；运行态还不能宣称完全健康。

## 9. 接管后的第一批处理事项

1. 修正或替换旧 `docs/HANDOFF.md` 里的过期 GitHub 地址和分支说明。
2. 检查未提交改动，按能力拆分 commit，避免把 state/logs/secrets 提交。
3. 修复 daemon/launchd：

```bash
bash bin/company-daemon-install-launchd
bin/companyctl doctor --summary
```

4. 处理 pending approvals/events，确认哪些是历史测试噪音，哪些是真待办。
5. 恢复 heartbeat：

```bash
bin/companyctl heartbeat --agent main
bin/companyctl heartbeat --agent codex
bin/companyctl heartbeat --agent hermes
bin/companyctl doctor --summary
```

6. 检查 progress delivery route，当前 repo-only / empty account 会导致通知发送失败。
7. 保持 Antigravity 为 candidate，直到拿到真实执行证据后再激活。
8. 如果继续 Codex/Hermes 项目经理链路，必须保证 Codex 写出的 progress file 带当前 Company Kernel task_id。

## 10. 常用命令

```bash
cd ~/openclaw/workspace-xmanx/projects/super-ai-company-kernel

git status --short --branch
git remote -v
git log --oneline -8 --decorate

python3 -m unittest discover -s tests -v
bin/company-dashboard --variant advanced
bin/companyctl doctor --summary
bin/companyctl employee list

bin/company-codex-pm-supervisor --agent codex --stale-minutes 15
```

## 11. 关键文档入口

```text
README.md
docs/CODEX_HERMES_PM_SUPERVISION.md
docs/EMPLOYEE_INTEGRATION_MATRIX.md
docs/OPENCLAW_COMPANY_BRIDGE.md
docs/LOCAL_ENVIRONMENT_AND_SKILLS.md
docs/RUNTIME_ADAPTERS.md
```

## 12. 接管口径

对下一个开发窗口的最短说明：

```text
请接管 ~/openclaw/workspace-xmanx/projects/super-ai-company-kernel。
当前 GitHub remote 是 https://github.com/super-ai-company/super-ai-company-kernel.git，
分支是 feat/internal-communication-closure。
代码测试 121/121 通过，dashboard 可生成，但 doctor 仍非 green：
missing_daemon_state、heartbeat stale/missing、pending_events、pending_approvals、launchd template mismatch。
先读 docs/PROJECT_TAKEOVER_HANDOFF_20260607.md，再处理运行态收口和分组提交。
```
