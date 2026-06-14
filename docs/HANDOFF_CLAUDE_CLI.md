# Company Kernel 交接文档(给 Claude CLI 接手)

> 目的:把当前项目状态、本次会话的所有改动、已知问题与待办,完整交接给 Claude Code(CLI),让它直接继续。
> 交接时间:2026-06-15。仓库 main 已同步到两个远程(origin/public),最新提交 `8a5f5f6`。

---

## 0. 一句话现状

Company Kernel(AI 员工治理内核)运行正常:**内核健康=正常**,codex 已完成 82 条任务且在持续干活;本次修复了「claude 派不出活、内核异常、管理员需审批、任务排队不执行、OpenClaw 员工任务失败」等一连串问题。**仍有 2 个收尾项**(见 §6):OpenClaw 员工技能未细化、Telegram 运营通知未配。

---

## 1. 关键路径与地址

| 用途 | 路径 / 地址 |
|---|---|
| 内核根目录(实际运行) | `/Users/owner/openclaw/company-kernel` |
| 主 CLI | `bin/companyctl` |
| 数据库 | `company.sqlite` |
| 守护配置 | `config/daemon.json` |
| 审批策略 | `config/policy.json` |
| 通信策略 | `config/company_communications.json` |
| 员工档案 / 能力 | `employees/<id>/profile.json`、`employees/<id>/capabilities.json` |
| Console(浏览器) | `http://127.0.0.1:8765/` 与 `http://127.0.0.1:8788/` |
| 守护日志 | `logs/daemon.log`、`logs/*.launchd.*.log` |
| OpenClaw 根 | `/Users/owner/openclaw`(总线 `ops/agent_bus/`,失败记录在 `failed/<agent>/`) |
| 业务代码仓库 | `/Users/owner/Documents/vdamo/damov4/`(含 `vdamo-cloud`、`webclients`、`android-pos`) |
| codex 工作区(默认) | `/Users/owner/openclaw/workspace-xmanx/projects/openclaw-codex-controller` |
| GitHub | origin=`super-ai-company/super-ai-company-kernel`,public=`super-ai-company/super-ai-company-kernel` |

---

## 2. 本次会话的改动(均已提交并推送到两个远程 main)

按提交顺序:

1. `2ba4534` **修复 claude→codex 派活**:守护"真实通信验证"探测失败后把 `claude` 自动暂停(`communication_paused`),导致 `task submit` 第一道检查"发送方暂停→拒绝"。解除暂停即恢复。新增 skill `skills/dispatch-task-to-codex/SKILL.md`。
2. `cf38469` **一揽子**:
   - **owner 免审批**:`config/policy.json` 加 `owner_admin_no_approval` 规则(source=owner 的任务自动批准,直接执行)。
   - **codex 全权**:`config/daemon.json` 把 codex 适配器沙箱 `workspace-write` → `danger-full-access`(YOLO,可 SSH/写盘/装包/Gradle)。
   - **OpenClaw 员工开 worker**:给 chindahotpot/krothong/invest/video-* 加 `company-openclaw-adapter` worker。
   - **doctor pending_events 宽限期**:事件滞留 <10 分钟不算异常。
   - 新增使用文档 `docs/COMPANY_KERNEL_USAGE.md`、`docs/CODEX_DEV_GUIDE.md`。
3. `0ae3510` **doctor:待审批/待 RFC 不再判内核异常**(它们是正常待办,Console 顶部已有独立提示);并让审批理由带任务标题。
4. `5e63adb` **daemon:整轮 ok 排除 `adapter.*` 步骤**——单个 adapter 任务失败/受阻不再把整轮守护标失败、进而让内核异常(adapter 失败仍由 `failed_adapter_runs` 跟踪)。
5. `56318f7` **OpenClaw 桥接补 `next_command`**(核心):`company_kernel/openclaw_adapter.py` 的 `build_payload()` 之前不带 `next_command`,导致 OpenClaw 总线 `missing_next_command` 失败。现自动把「目标+说明+用既有技能执行+回填证据」组成 next_command 交给员工。

> 其余 `chore:` 提交是运维/推送脚本。`d89f432`/`147983d` 是 codex 在 public 分支加的两份文档,已并入 main。

---

## 3. 当前运行配置(事实)

- **启用的 adapter workers**(`config/daemon.json`,每 30s 一轮,每 worker 每轮 1 条):
  - codex → `company-codex-adapter --execute --sandbox danger-full-access --model gpt-5.5 --timeout-seconds 1800`
  - hermes → `company-hermes-adapter`
  - nestcar / chindahotpot / krothong / invest / video-creator / video-ops / video-publisher → `company-openclaw-adapter --execute`
- **免审批规则**(`config/policy.json`):`owner_admin_no_approval`(owner 全免)、`nestcar_non_p1_data_fetch_external_send`。
- **任务分布**:completed 82,claimed 1,submitted 1,cancelled 17,blocked 0。
- **内核健康**:正常(绿)。

---

## 4. ⚠️ 给 Claude CLI 的关键提示(必读)

1. **你比上一棒强的地方**:上一会话(Cowork)只能"双击 .command 脚本"在 Mac 上执行(终端被限制为只读、不能直接打字),且看到的 `company.sqlite`/日志有**同步延迟**。**你(Claude CLI)在真实终端,可直接跑 `bin/companyctl ...`,无此限制——优先直接执行,不要再用那堆 .command。**
2. **改动生效**:`companyctl`/adapter 是每次新进程,改 `.py` 后下一次调用即生效;但**长驻网关(console:8788 / api:8765)与守护(daemon)是常驻进程,改内核代码后需重启**才会加载:
   `launchctl kickstart -k gui/$(id -u)/ai.openclaw.company-kernel.{console,api,daemon}`
3. **内核健康判定**(已被本会话调整,见 §2.3/2.4):真故障才红(daemon 挂、心跳缺失、adapter 未确认失败、过期锁、能力/证据问题)。待审批/待 RFC/新鲜事件**不**算异常。
4. **派活两道闸**:`task submit` 需 ① 发送方未 `communication_paused`(`config/company_communications.json`)② 目标 `status='active'`。adapter `--execute` 外发再过 `policy.json` 审批(owner 已免)。
5. **OpenClaw 桥接**:任务经 `company-openclaw-adapter --execute` → `oc bus submit` 到 OpenClaw 总线,**payload 必须含 `next_command`**(已修)。失败记录在 `/Users/owner/openclaw/ops/agent_bus/failed/<agent>/`。
6. **沙箱目录**:`/Users/owner/openclaw/ops` 等不在内核目录下,从内核脚本可直接读;注意 codex 任务若不写绝对 `工作区:` 路径会跑去 `/tmp` 找不到仓库(见下方已修例)。

---

## 5. 一连串问题的根因小结(便于举一反三)

- "内核异常" 多次出现,**从来不是崩溃**,而是 doctor 把各种正常状态(待审批、新鲜事件、单任务失败、daemon 重启瞬间)判成 issue。已逐一收敛为"真故障才红"。
- "任务排队不执行":目标员工**没有 enabled 的 adapter worker**(只有 codex/hermes/nestcar 有过)。已给 OpenClaw 业务员工补 worker。
- "OpenClaw 员工任务失败 missing_next_command":桥接缺 `next_command`。已修。
- "codex 受阻":多为沙箱限制(已给 danger-full-access)或**任务没写绝对工作区路径**(跑 /tmp)。例:webclients 返工任务把 `工作区: damov4 webclients/` 改成 `/Users/owner/Documents/vdamo/damov4/webclients` 后恢复。

---

## 6. 待办(请 Claude CLI 接着做)

1. **验证 OpenClaw next_command 修复是否真让员工完成任务**:最新"发日报"任务 `task-20260615-004038-d264e3`(chindahotpot)是用修好的桥接派的。确认它是 `completed` 还是又 `blocked`;若 blocked,看 blocker 现在是不是具体的 OpenClaw 侧原因(不应再是 missing_next_command)。
   - 查:`bin/companyctl task show --task-id task-20260615-004038-d264e3`;失败看 `/Users/owner/openclaw/ops/agent_bus/failed/chindahotpot/`。
2. **细化员工技能/参数,让其"持证上岗"**(用户明确要求):为 OpenClaw 业务员工逐个写 `employees/<id>/capabilities.json`(skills/tools/preferred_task_types),并视情况让 `build_next_command`/派活路由结合技能。当前 next_command 是通用指令,够用但不精准。
3. **Telegram 运营通知未配**:`config/company_communications.json` 的 `notification` 里 `bot_token_env=COMPANY_EMPLOYEE_TELEGRAM_BOT_TOKEN`、target=`telegram:<operator-chat-id>`(占位符)。需要真实 bot token + chat id 才能把审批/报错/日报类通知真正发出。**涉及密钥,让用户自己填**。
4. **codex 在跑的真实业务任务**(webclients UI 返工、Android、vdamo-cloud 部署等):盯完成证据;部署/SSH 类需确认 192.168.1.83 可达与密钥。
5. **运维脚本清理**:本会话产生大量一次性 `*.command`(根目录),可整理进 `scripts/ops/` 或删掉非复用的,保持仓库整洁。复用价值高的:`GREEN-KERNEL`、`BLOCKED-REPORT`、`HEAL-KERNEL`、`PUSH-MAIN`。

---

## 7. 常用命令速查

```bash
cd /Users/owner/openclaw/company-kernel
bin/companyctl doctor --summary                       # 体检(内核健康徽章的来源)
bin/companyctl task submit --from owner --to <agent> --title "..." --description "..." --priority P1
bin/companyctl task list --agent <agent>
bin/companyctl task show --task-id <id>
bin/companyctl task reopen --task-id <id> --by owner --reason "..."
bin/companyctl runtime adapter-runs --agent <agent> --status failed --unacknowledged-only
bin/companyctl runtime ack-adapter-run --run-id <id> --by owner --reason "..."
bin/companyctl approval list --status pending
bin/companyctl employee list
tail -f logs/daemon.log | cut -c1-200
```

工作流文档见 `docs/COMPANY_KERNEL_USAGE.md`、`docs/CODEX_DEV_GUIDE.md`、skill `skills/dispatch-task-to-codex/SKILL.md`。

---

## 8. 验证清单(接手后先跑一遍)

- [ ] `bin/companyctl doctor --summary` → `ok: true, issues: []`
- [ ] Console 8765/8788 顶部"内核 正常"
- [ ] `task show` 看 `task-20260615-004038-d264e3`(发日报)结果
- [ ] `git status` 干净、`git log origin/main..main` 为空(已全部推送)
- [ ] codex 在跑的任务有 `completed/blocked` 闭环与证据
