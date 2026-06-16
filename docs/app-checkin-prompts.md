# App 接入 Company Kernel(Codex app / Claude app / Antigravity app)

三个常驻**对话式** app 都是 kernel 员工:Codex=`codex`、Claude=`claude`、Antigravity=`antigravity`(agy)。
它们不像守护进程那样自动轮询,所以要让它们**每次对话主动签到、并把每一步播报到对话里**,
这样我在聊天记录里就能看见:谁接了什么活、做到哪、结果如何 —— 即「可视化沟通」。

接入分两层,**优先第 1 层**:

## 1) 原生 MCP 工具(首选,已注册)

MCP server 名 `company-kernel`(`company_kernel/mcp_server.py`,纯标准库 stdio JSON-RPC,
内部调绝对 `companyctl`)。已在三端注册,**改完要重启 app 才生效**:

| App | 注册文件 | 键 |
|---|---|---|
| Codex | `~/.codex/config.toml` | `[mcp_servers.company_kernel]` → command=`bin/company-kernel-mcp` |
| Claude | `~/.claude.json` | `mcpServers.company-kernel` |
| Antigravity | `~/.gemini/config/mcp_config.json` | `mcpServers.company-kernel` |

**七个工具**(`agent` 用本端 id:codex/claude/antigravity):

| 工具 | 作用 |
|---|---|
| `list_my_tasks(agent)` | 列出派给我、待处理的(submitted/claimed) |
| `show_task(task_id)` | 看详情(工作区/超时/验收标准) |
| `claim_task(agent, task_id)` | 认领(加锁,不和守护重复处理) |
| `report_done(agent, task_id, summary, evidence)` | 真做完才报,evidence 是证据绝对路径 |
| `report_blocked(agent, task_id, blocker)` | 卡住如实报,别假装完成 |
| `dispatch_task(from_agent, to_agent, title, description)` | 派活(派 codex 必写「工作区: /abs/repo」;派 agy 大审核写「超时: 3600」) |
| `check_completions(agent)` | 看我派出去的任务完成了没(内核完成即推 inbox,毫秒级,别轮询) |

## 2) 自动读的指令文件(已写,告诉 app 怎么用上面这些工具)

| App | 文件 | 内容 |
|---|---|---|
| Codex | `~/.codex/AGENTS.md` | 「Company Kernel 员工(codex)」段 |
| Claude | `~/.claude/CLAUDE.md` | 同上(claude) |
| Antigravity | `~/.gemini/GEMINI.md` | 同上(antigravity)。若没自动读,把内容粘进 Antigravity 的 Rules/全局规则面板 |

核心要求(三端一致):**每次对话固定动作 + 全程中文播报**:
1. 开场 `list_my_tasks` → 播报「📥 有 N 个待办」或「无待办」。
2. 每个待办:`claim_task` →(`show_task`)→ 进工作区真做完 → `report_done`/`report_blocked`,逐步播报「✅ 已认领 #id / 🔧 执行中… / ✅ 完成 #id:摘要 / ⛔ 受阻 #id:原因」。
3. `check_completions` → 有结果就播报「📨 你派的 #id 回来了:<status> <summary/blocker>」。
4. 都没有就回「无待办、无新完成」,正常等我指令。

---

## 3) 降级:MCP 没加载时用 CLI(全绝对路径)

companyctl 一律用绝对路径 `/Users/owner/openclaw/company-kernel/bin/companyctl`
(app 在自己的项目仓库里跑,PATH 里没有它)。签到流程已实测端到端跑通
(`task list → claim → done → completed`,2026-06-16)。

| 动作 | 命令 |
|---|---|
| 查我的待办 | `companyctl task list --agent <id>` → 看 status=submitted |
| 认领 | `companyctl task claim --agent <id> --task-id <id>` |
| 看详情 | `companyctl task show --task-id <id>` |
| 完成回报 | `companyctl task done --agent <id> --task-id <id> --summary "…" --evidence <路径>` |
| 卡住 | `companyctl task block --agent <id> --task-id <id> --blocker "…"` |
| 派活 | `companyctl task submit --from <id> --to <id> --title … --description …` |

事件驱动(替代轮询):你派出去的任务**一完成,内核就往你 inbox 写 `result-<task>.json`**
(`employees/<你的id>/inbox/`)。CLI 模式可 `fswatch` 这个目录:

```bash
fswatch -0 /Users/owner/openclaw/company-kernel/employees/<id>/inbox/ \
| while read -d "" path; do
    case "$path" in *result-*.json) cat "$path" ;; esac   # note/status/done_by/summary/evidence_path/blocker
  done
```
(没装就 `brew install fswatch`。)用 MCP 的 `check_completions` 则不必自己 watch。

## ⚠️ 避坑:调运行时 CLI 用绝对二进制路径

app 跑在交互式 shell 里会加载 shell 函数/别名。比如 Claude Code 装了
`claude () { command claude --bare … }`,`--bare` 不读 OAuth 登录态 → 调裸 `claude` 会
误判「未登录」。**要直接调 claude / codex / agy 等 CLI 时,用绝对二进制路径**
(`which -a <cmd>` 查真路径)**或 `command claude`**,别用裸名。companyctl 本身就是绝对路径,
不受影响;内核走 subprocess + 绝对二进制,也不受影响。
