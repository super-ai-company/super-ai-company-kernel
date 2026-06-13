# Company Kernel 使用说明(当前版 · 实操手册)

把任意 AI 工具(Codex、Hermes、OpenClaw、Claude、Trae…)统一成"员工",用一套协议管理派活、认领、完成、审批、锁、审计与恢复。本文是照着就能用的操作手册。Codex 开发细节见配套文档 `docs/CODEX_DEV_GUIDE.md`。

所有命令默认先:

```
cd /Users/shift/openclaw/company-kernel
```

---

## 1. 路径与地址速查

| 用途 | 路径 / 地址 |
|---|---|
| 内核根目录(实际运行) | `/Users/shift/openclaw/company-kernel` |
| 主 CLI | `bin/companyctl` |
| 控制台(浏览器打开) | `http://127.0.0.1:8765/`(及 `8788`) |
| 数据库 | `company.sqlite` |
| 守护进程配置 | `config/daemon.json` |
| 员工档案 | `employees/<id>/profile.json` |
| 任务证据/产物 | `employees/<id>/reports/<task-id>/` |
| 日志 | `logs/daemon.log`、`logs/*.launchd.*.log` |
| GitHub | `origin` = shiftshen/super-ai-company-kernel · `public` = super-ai-company/super-ai-company-kernel |

---

## 2. 启动 / 停止 / 状态

```
# 控制台(API 网关,端口 8788)
./START-CONSOLE.command            # 双击或终端运行,装好 launchd 自启

# 看后台守护是否在跑
pgrep -fl company-daemon
tail -f logs/daemon.log            # 每 30s 一轮,returncode 0 即健康

# 整体体检(控制台"内核"徽章就读这个)
bin/companyctl doctor --summary    # ok:true / issues:[] => 健康
```

守护进程(`config/daemon.json`)每 30 秒做一轮:同步 OpenClaw 运行时与心跳、跑调度器、重试、监督交付循环、并驱动各 adapter worker(codex / hermes / nestcar)执行任务。

---

## 3. 控制台能看什么

浏览器打开 `http://127.0.0.1:8765/`:

- 顶部徽章:**内核 正常/异常**、**员工 N**、**待审批 N**
- Overview / Employees / Tasks / Messages / Conversations / Approvals 各标签
- 在岗员工、任务看板、消息、对话、审批、活动流

> "内核 异常" 不是崩溃,而是 `doctor` 体检里有未清的 `issues`(见第 7 节)。

---

## 4. 员工管理

```
bin/companyctl employee list                       # 所有员工
bin/companyctl employee show --id codex            # 单个员工
bin/companyctl employee onboard ...                # 注册新员工
bin/companyctl employee offboard --id <id>         # 下线
bin/companyctl employee set-unavailable --id <id>  # 临时不可用
bin/companyctl heartbeat --agent <id>              # 手动打心跳
```

当前关键员工:`owner-shift`(你,human-owner)、`codex`(开发)、`hermes`、`nestcar`/`openclaw-main`(OpenClaw 运行时)。

---

## 5. 任务流转(核心)

```
# 提交任务给指定员工
bin/companyctl task submit --from owner-shift --to codex \
  --title "标题" --description "含验收标准的详细需求" --priority P1

# 或:按技能/工具让内核自动选员工
bin/companyctl task route --from owner-shift --title "..." --skills "python" --task-type dev

# 查看
bin/companyctl task list --agent codex
bin/companyctl task show --task-id <task-id>
bin/companyctl task children --task-id <task-id>

# 拆分长任务
bin/companyctl task split --task-id <id> --by owner-shift --item "codex|子标题|描述|P2"

# 手动认领 / 完成 / 阻塞(一般由 adapter 自动做)
bin/companyctl task claim --agent codex --task-id <id>
bin/companyctl task done  --agent codex --task-id <id> --summary "..." --evidence "<证据文件路径>"
bin/companyctl task block --agent codex --task-id <id> --blocker "原因"
```

**完成必须带证据(evidence)**:worker 退出码 0 不算完成,必须产出 `STATUS: completed` 且有证据文件,否则任务会 block 等人工复核——避免"假完成"。

派给 codex 后由守护进程自动执行(详见 `docs/CODEX_DEV_GUIDE.md`)。

---

## 6. 适配器运行(adapter runs)

每次某员工实际执行任务都会记一条 adapter run:

```
bin/companyctl runtime adapter-runs --agent codex                       # 列出
bin/companyctl runtime adapter-runs --agent codex --status failed --unacknowledged-only
bin/companyctl runtime adapter-run --run-id <run-id>                    # 详情
bin/companyctl runtime retry-adapter-run --run-id <id> --by owner-shift --reason "..."
bin/companyctl runtime ack-adapter-run   --run-id <id> --by owner-shift --reason "..."
```

---

## 7. "内核异常" 排查(最常见)

徽章红 = `bin/companyctl doctor --summary` 返回 `ok:false`,`issues` 列出原因:

| issue | 处理 |
|---|---|
| `adapter_failures` | `runtime ack-adapter-run` 或 `retry-adapter-run` |
| `task_evidence_issues` | 某 completed 任务的证据文件在磁盘缺失 → 修正 `evidence_path` 或重跑 |
| `missing_heartbeats` / `stale_heartbeats` | 检查守护进程是否在跑 |
| `pending_events`/`approvals`/`rfcs` | 在控制台/CLI 处理掉 |
| `stale_locks` | `bin/companyctl repair reset-stale-claims` |

doctor 实时读库,清掉 issue 后控制台 ~15 秒自动刷新转绿,**不用重启进程**。
一键修复脚本:双击 `HEAL-KERNEL.command`(自动确认失败 adapter + 修复缺失证据 + 复检)。

---

## 8. 审批 / RFC / 锁 / 审计

```
bin/companyctl approval list ; bin/companyctl approve --id <id> ; bin/companyctl deny --id <id>
bin/companyctl rfc list                       # 规则变更需走 RFC
bin/companyctl lock list ; bin/companyctl repair reset-stale-claims
bin/companyctl audit ...                       # 审计日志
```

高风险动作(对外发送、部署、改内核)需 owner 审批;改"公司规则"需 RFC。受保护路径见 `PROTECTED_PATHS.md`,改动需 `--changed-files` + 已批准 `--rfc`。

---

## 9. 备份与恢复

```
bin/company-backup                 # SQLite 在线备份(守护进程默认每 24h、留 14 份)
```

---

## 10. 常用排错入口

```
tail -n 100 logs/daemon.log | cut -c1-300        # 守护循环
tail -n 80  logs/console.launchd.err.log         # 控制台错误
tail -n 80  logs/company-api.launchd.err.log     # API 网关错误
bin/companyctl doctor --summary                  # 体检总览
```

---

### 配套文档
- `docs/CODEX_DEV_GUIDE.md` —— 用本内核驱动 codex 做开发(路径+命令完整版)
- `README.md` —— 项目总览 / 架构 / 30 秒上手
- `PROTECTED_PATHS.md` —— 受保护路径与改动规则
