# Company Kernel — 安装避坑清单 (Setup Lessons)

> 本文汇总实战中反复踩到的坑与已落地的根治措施,**新机器部署/接手前过一遍**,可提前规避。
> 每条都对应代码里已实现的防护或一次性配置动作。

## 1. 任务反复 blocked → 描述必须带「绝对仓库路径」(最高频问题)

Codex/agent 在临时沙箱跑;描述里不写**绝对**仓库路径,就会落到 `/tmp` 找不到项目而 block
(`codex verdict: blocked — /tmp 内没有 <repo> 仓库入口`)。

- **已防护**:控制台派活表单聚焦自动带出上下文模板(仓库路径/验收/步骤),描述过短直接拦截。
- **派活务必**:`【工作区/仓库绝对路径】: /abs/path` + 验收 + 步骤。详见 skill `dispatch-task-to-codex`。
- **已 blocked 的**:控制台开任务 → 🔧 修复并重开(补路径),别用「仅重开」(会再 block);或 🗑 丢弃。

## 2. 通知刷屏 → 升级/审批已去重+熔断

- supervisor 升级通知按 `task_id+status` 去重,冷却 6h;同一任务最多自动升级 **3 次**后停止(转人工)。
- 离线提醒 `--dedup`:只在离线集合变化或每小时一次时发。
- **注意**:Telegram 投递配通后,存量通知才会真正到达;别误以为是"突然变多"。

## 3. 幽灵员工 → 名册只认 openclaw 配置

`openclaw/agents/` 下任意目录原会被同步成候选员工(gpt5/claude-code/car-rental 等垃圾)。
- **已根治**:`sync-openclaw-runtime` 只注册 `openclaw.json` 的 `agents.list` 里真实存在的 agent。
- **清幽灵**:`employee offboard --id X --hard-delete`(无别名的);有别名碰撞的(如 car-rental→nestcar)
  用直接 SQL 删,别走 offboard(会误指向别名目标)。删后 `sync-openclaw-runtime` 重跑确认不复活。
- **硬删会连带取消该员工的任务**(避免孤儿证据触发 `内核异常`/`evidence_missing_on_disk`)。

## 4. Telegram / LINE 凭据 → secrets.env(永不入 git)

- token 全放 **gitignored** `config/secrets.env`;入口 bin(`companyctl`/`company-daemon`/
  `company-api-gateway`/`company-telegram-approval-poll`)启动时 source 它。
- `company_communications.json` 只存 `bot_token_env` 名称引用 + chat id(非密)。
- **owner chat id** = Telegram 用户 id,对所有 bot 相同;取自 `openclaw.json` 的
  `OPS_APPROVAL_NOTIFY_TELEGRAM_TARGETS`,或让 owner 给 bot 发条消息后 `getUpdates` 读。
- Bot API `getUpdates` 与 webhook 互斥;审批轮询用 getUpdates,故主通知 bot 不要设 webhook。
- **bot 之间不能互发**:Messages 收件人只列有通信渠道的员工(openclaw runtime),纯计算 runtime 不列。

## 5. 一键审批/升级 → 需安装轮询服务

审批/升级通知带 Telegram 内联按钮(✅批准/❌拒绝、🔧让agent修/👤我来/⏭跳过)。点击回收靠
`telegram_approval_poll`,**用户自行运行 `bin/company-telegram-approval-install-launchd`** 装常驻(每 10s)。
控制台 Approvals 标签 + 总览「⚠️ 卡住待处理」面板有对等一键按钮(电脑端也能处理,且有记录)。

## 6. owner 权限 → 可恢复任意任务

`can_manage_task_recovery` 已放行 human owner(owner),可对**任意**任务 reopen/retry/reassign/discard
(此前只限参与者/supervisor,owner 反而被 `actor cannot reopen task` 挡住)。

## 7. 概念边界 → Messages ≠ Tasks

- **Messages** = 直接发信息到通信渠道(员工 / LINE 客户群 / Telegram),发客户群前有确认弹窗。
- **Tasks** = 派需大模型执行的任务。两者收件人/行为分开,别混。

## 8. 重启/崩溃恢复 → 已具备,别手痒

- 已安装 plist 全 `RunAtLoad`(开机自启);api/console/dashboard `KeepAlive`(崩溃自拉起);
  daemon 180s / task-intake 15s / approval-poll 10s 周期。状态全在 `company.sqlite` 持久。
- daemon 每轮 `repair reset-stale-claims`(任务跑一半被杀自动重领)。
- **重启网关**(改 api_gateway.py/通知配置后):`launchctl kickstart -k gui/$(id -u)/ai.openclaw.company-kernel.{api,console,dashboard}`——**避开 codex 在跑任务的时刻**,否则打断它(会自动重试但浪费)。

## 9. 自检命令

```bash
bin/companyctl doctor --summary                  # ok:true / issues:[] = 正常
python3 -m unittest tests.test_company_kernel_core   # 全套回归(改内核后必跑)
launchctl list | grep company-kernel             # 服务(StartInterval 单发任务两次运行间显示 '-' 是正常)
```
