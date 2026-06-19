# Company Kernel — 交接 / Handoff

> 给下一个会话(或新窗口的 Claude)快速接手用。更新于 2026-06-16。
> 产品目标:**把你的 agent(codex/claude/gemini/antigravity/openclaw)变成受管的 AI 员工。**

## 1. 这是什么
商用 AI 员工治理内核。GitHub: `super-ai-company/super-ai-company-kernel`,根目录 `$OPENCLAW_COMPANY_KERNEL_ROOT`。
Python CLI + launchd 托管服务 + SQLite + 网页控制台(`http://127.0.0.1:8765/`)。
- **employee** = 绑定到 **runtime**(真正干活的 CLI)的 AI 员工。内核派活/开会/留证据/回报结果;runtime 执行。
- 服务:**daemon**(派活循环)、**api**(控制台+REST :8765)、可选 **task-intake**(文件投递桥)。

## 2. 怎么跑 / 测
```
bin/companyctl <cmd>                       # CLI(Win: python -m company_kernel.companyctl)
bash bin/company-services-install-launchd  # mac 自启 daemon+api
launchctl kickstart -k gui/$(id -u)/ai.openclaw.company-kernel.daemon   # 改配置后重启 daemon
python3 -m unittest discover -s tests      # 全量 398 测试(注意:本机是 python3,没有 pytest)
```
- 测试在制环境:`python3` = 3.14,**无 pytest**,用 `unittest`。
- daemon **只在启动时读 config**,改 `config/daemon.json` 必须重启 daemon。

## 3. 关键设计点(踩过坑的)
- **submit 守卫** `validate_task_submission`:codex 无 `工作区:` 路径 → 拒(否则跑 /tmp 卡住);重复活/刚弃单(60min 冷却)→ 拒;`--force` 越过;env `COMPANY_KERNEL_SUBMIT_GUARDS=0` 关(测试 setUp 已设 0)。
- **派单错误自动放弃**:`auto_triage_misdispatched_tasks` daemon 步骤,烂单自动弃 + 反馈派单人。
- **三层健康**:issues(基建→内核异常红)/ attention(任务级待处理)/ warnings(忙)。曾经 internal 误报内核异常,已用三层分级根治。
- **adapter 超时兜底**:`run_cmd` Popen+`os.killpg`,卡死的 `claude -p`/`codex exec` 会被杀,不再冻结 daemon。
- **gemini** = 走 claude adapter 的 Claude 兼容代理 runtime;`presence.recover-unavailable` 步骤在代理恢复后自动重新激活。
- **本地配置不上传**:`config/company_communications.json`、`employees/*/profile|capabilities|permissions|rules`、`config/users.json`、`config/secrets.env`、`state/` 全部 gitignore。secrets.env chmod 600,**永不提交**。
- **RBAC 默认休眠**:本机无 `config/users.json` → 开放(owner)。别误建 users.json,否则控制台开始要 token。

## 4. 最近完成(本轮+上轮路线图)
- ✅ 网页加员工自助闭环(注册→验证→激活,不敲命令)
- ✅ 跨 OS Docker 一键安装(`QUICKSTART.md`)
- ✅ API token 常量时间比较 + **人类 RBAC**(viewer<operator<admin<owner,默认开放)
- ✅ 派单错误自动放弃 + 反馈派单人
- ✅ Stuck 面板三动作逻辑修正(操作后真从面板消失)
- ✅ 会议编排(conversation run,只有真能发言的员工入会)、完成回报闭环、积压可见、重启自动恢复(已验证)
- ✅ **外部 app 文件投递桥**(`state/task-intake/incoming/` 丢 JSON → 自动派单;codex/antigravity APP 对接路径)
- ✅ 根治 daemon retry 测试 flaky
- ✅ **控制台董事会风重构**(frontend-design 方法论):象牙白+靛黑+黄铜金,公司运营账本气质,脱离旧霓虹默认风。改样式守则见记忆 console-design-system。
- ✅ **网页改员工配置/权限 UI**(P1):员工卡片「✎ 编辑」→ 弹窗读 GET 预填,按改动分别提交 profile/capabilities/permissions 三端点。
- ✅ **会议人在环中**(P1):会议室「⛔ 否决/改向」一键下达【主理人指令】并继续;主理人发言渲染成黄铜绑定指令块。

- ✅ **secrets 进密钥管理**(P0):`company-secrets` CLI,macOS keychain 后端 + 文件回退;bin 脚本先 eval keychain 再 source 文件(向后兼容)。`set/get/list/migrate-file/doctor`;scope 维度预留多租户。详见 onboard skill §12。

## 5. 还剩(按优先级)
- **P0 多租户隔离**:定位已定为**先单租户、架构留多租户余地**(2026-06-16 老板拍板)。现按单租户卖,secrets 已带 scope 维度;数据层 tenant_id 改造留待真要转 SaaS 时做。
- **增值**:`license.py` → 真账单就能卖。

## 6. 当前 git 状态注意
- 已推送到 origin/main(最新见 git log)。
- 工作区有**运行时产物**未提交、**不要 commit**:`reports/openclaw-external-agent-bridge/ocbridge-oc-codex-1.json`(M)、`reports/diagnose-failures.txt`(??)。

## 7. 商用完整度自评
自用 ~85%,商用 ~65%。缺口集中在:安全/RBAC(部分已补)、多租户、计费、跨 OS 自启(mac 已 ship,Linux systemd / Win Task Scheduler 是模板)。
