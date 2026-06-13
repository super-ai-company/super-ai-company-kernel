# 用 Company Kernel 驱动 Codex 做开发 —— 路径与命令完整说明

本文是给 Claude(或任何操作者/Agent)看的操作手册:如何用本项目把开发任务派给 `codex` 员工、让它在自己的工作区里跑 `codex exec`、并查看/处理结果。所有命令都假设你已 `cd` 到内核根目录。

---

## 0. 关键路径速查

| 用途 | 路径 |
|---|---|
| 内核根目录 | `/Users/owner/openclaw/company-kernel` |
| 主命令行 CLI | `bin/companyctl`(= `python3 -m company_kernel.companyctl`) |
| Codex 适配器 | `bin/company-codex-adapter`(= `python3 -m company_kernel.codex_adapter`) |
| Codex 员工档案 | `employees/codex/profile.json` |
| **Codex 工作区(它实际改代码的地方)** | `/Users/owner/openclaw/workspace-xmanx/projects/openclaw-codex-controller` |
| 守护进程配置 | `config/daemon.json`(`adapter_workers` → codex) |
| 任务证据/产物 | `employees/codex/reports/<task-id>/` 与 `state/` |
| 数据库 | `company.sqlite` |
| 控制台 | `http://127.0.0.1:8765`(及 `8788`) |
| 守护日志 | `logs/daemon.log`、`logs/company-daemon.launchd.out.log` |

Codex 员工当前配置:`runtime=codex`、`role=developer`、`model=gpt-5.5`、`sandbox=workspace-write`、单次超时 `1800s`。

---

## 1. 运行原理(一句话)

你把任务**提交给 `codex`** → 守护进程每 30 秒扫一次,自动认领该任务 → 在 codex 工作区执行 `codex exec`(模型 gpt-5.5、可写工作区)→ 把输出存为证据 → 把任务标记为 `completed` 或 `blocked`。

守护进程实际执行的等价命令(来自 `config/daemon.json`):

```
company-codex-adapter --agent codex --execute --sandbox workspace-write --model gpt-5.5 --timeout-seconds 1800
```

底层最终调用的是:

```
codex exec --model gpt-5.5 --ignore-rules --ephemeral --skip-git-repo-check \
  -C /Users/owner/openclaw/workspace-xmanx/projects/openclaw-codex-controller \
  -s workspace-write -o <输出文件> -
```

---

## 2. 标准开发流程

### 步骤 1 — 提交开发任务给 codex

```
cd /Users/owner/openclaw/company-kernel

bin/companyctl task submit \
  --from owner \
  --to codex \
  --title "实现 X 功能" \
  --description "详细需求:目标、验收标准、涉及文件、回滚方式。写得越具体 codex 做得越准。" \
  --priority P1
```

要点:
- `--from` 必须是有效员工(你自己用 `owner`)。
- `--to codex` 指定由 codex 执行。
- 若任务会改**受保护路径**,需带 `--changed-files a.py,b.py` 和已批准的 `--rfc <rfc路径>`(见 `PROTECTED_PATHS.md`)。
- 命令会回显新任务的 `task-id`,记下它。

### 步骤 2 — 让 codex 执行

两种方式二选一:

A. **等守护进程自动跑**(默认,推荐):30 秒内自动认领并执行,无需手动操作。确认守护在跑:

```
pgrep -fl company-daemon
tail -f logs/daemon.log    # 看到 company-codex-adapter ... "processed": 1 即已处理
```

B. **手动立即跑一次**(调试/不想等):

```
bin/company-codex-adapter --agent codex --execute \
  --sandbox workspace-write --model gpt-5.5 --timeout-seconds 1800
```

> 不加 `--execute` 只生成任务卡和报告、**不真正运行** codex(适合先 dry-run 检查派发内容)。

### 步骤 3 — 查看进度与结果

```
bin/companyctl task list --agent codex            # codex 的任务队列
bin/companyctl task show --task-id <task-id>       # 单个任务详情 + 证据路径 + 状态
bin/companyctl runtime adapter-runs --agent codex  # codex 每次执行的记录(成功/失败)
```

任务完成后,证据(codex 的实际输出报告)在:
`employees/codex/reports/<task-id>/` 下的 `codex-adapter-report-*.md`。

---

## 3. 失败处理

查看某次失败运行的详情:

```
bin/companyctl runtime adapter-runs --agent codex --status failed --unacknowledged-only
bin/companyctl runtime adapter-run --run-id <run-id>
```

重试(重新排队执行):

```
bin/companyctl runtime retry-adapter-run --run-id <run-id> --by owner --reason "修了输入后重试"
```

确认/清理(承认失败、从健康体检里消除,不再报警):

```
bin/companyctl runtime ack-adapter-run --run-id <run-id> --by owner --reason "历史失败已复核"
```

---

## 4. 健康检查与"内核异常"

控制台徽章 **"内核 正常/异常"** 直接取自:

```
bin/companyctl doctor --summary
```

返回 `ok:true / issues:[]` → 徽章绿;`issues` 非空 → 徽章红。常见 issue 与对应处理:

| issue | 含义 | 处理 |
|---|---|---|
| `adapter_failures` | 有未确认的失败 adapter 运行 | `runtime ack-adapter-run` 或 `retry-adapter-run` |
| `task_evidence_issues` | 某 completed 任务的证据文件在磁盘上找不到 | 重建/修正 `evidence_path`,或重跑任务 |
| `missing_heartbeats`/`stale_heartbeats` | 应在线的 worker 没心跳 | 检查守护进程是否在跑 |
| `pending_events`/`pending_approvals`/`pending_rfcs` | 有待处理事项 | 在控制台或 CLI 处理掉 |
| `stale_locks` | 过期锁 | `bin/companyctl repair reset-stale-claims` |

> 注意:doctor **实时读库**计算,issue 清掉后控制台 ~15 秒自动刷新转绿,**无需重启进程**。

---

## 5. 常用辅助命令

```
bin/companyctl task route --from owner --title "..." --skills "python" --task-type dev   # 让内核按技能/工具自动选最合适的员工
bin/companyctl task split --task-id <id> --by owner --item "codex|子任务标题|描述|P2"      # 拆分长任务
bin/companyctl task show --task-id <id>      # 看状态/证据
bin/companyctl heartbeat --agent codex       # 手动打一次心跳
```

---

## 6. 给 Claude 的最小操作清单(复制即用)

```
cd /Users/owner/openclaw/company-kernel
# 1) 派活
bin/companyctl task submit --from owner --to codex \
  --title "<标题>" --description "<含验收标准的详细需求>" --priority P1
# 2) 等 30s 自动执行,或手动:
bin/company-codex-adapter --agent codex --execute --sandbox workspace-write --model gpt-5.5 --timeout-seconds 1800
# 3) 看结果
bin/companyctl task list --agent codex
bin/companyctl task show --task-id <task-id>
# 4) 失败就重试/确认
bin/companyctl runtime adapter-runs --agent codex --status failed --unacknowledged-only
```
