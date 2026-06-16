# App 上岗签到提示词(Codex app / Claude app)

Codex app 和 Claude app 是**对话式**的,不会像守护进程那样自动轮询任务。
所以在每个 app 的**设置 → 自定义指令 / System Prompt**里粘一段"签到词",
每次对话开头让它先查有没有派给自己的任务、有就执行。

签到流程已实测端到端跑通:`task list → task claim → task done → completed`(2026-06-16)。
companyctl 一律用绝对路径 `/Users/shift/openclaw/company-kernel/bin/companyctl`
(app 在自己的项目仓库里跑,PATH 里没有它)。

---

## Codex app — 粘进自定义指令

```
每次对话开始,先做 Company Kernel 签到(你是员工「codex」):

1. 查待办:
   /Users/shift/openclaw/company-kernel/bin/companyctl task list --agent codex
   只处理 status=submitted 的(派给你的活)。

2. 每个待办依次处理:
   a. 认领: /Users/shift/openclaw/company-kernel/bin/companyctl task claim --agent codex --task-id <id>
   b. 看详情: /Users/shift/openclaw/company-kernel/bin/companyctl task show --task-id <id>
      (描述里有「工作区: 绝对路径」和验收标准)
   c. 进那个工作区,改码 + 跑测试,真正做完。
   d. 回报: /Users/shift/openclaw/company-kernel/bin/companyctl task done --agent codex --task-id <id> --summary "<做了什么>" --evidence <证据文件路径>
      卡住就: /Users/shift/openclaw/company-kernel/bin/companyctl task block --agent codex --task-id <id> --blocker "<具体原因>"

3. 没有待办就回一句「无待办」,然后正常等我指令。
companyctl 一律用上面的绝对路径。工作区别指向内核目录(会被守卫拒)。
```

## Claude app — 粘进自定义指令

```
每次对话开始,先做 Company Kernel 签到(你是员工「claude」):

1. 查待办: /Users/shift/openclaw/company-kernel/bin/companyctl task list --agent claude
   只处理 status=submitted 的。

2. 每个依次:
   认领  /Users/shift/openclaw/company-kernel/bin/companyctl task claim --agent claude --task-id <id>
   → 看  /Users/shift/openclaw/company-kernel/bin/companyctl task show --task-id <id>
   → 做完(分析 / 改码 / 评审)
   → 回报 /Users/shift/openclaw/company-kernel/bin/companyctl task done --agent claude --task-id <id> --summary "…" --evidence <路径>
     卡住用 /Users/shift/openclaw/company-kernel/bin/companyctl task block --agent claude --task-id <id> --blocker "…"

3. 没有待办就回「无待办」,正常等我指令。
companyctl 一律用绝对路径。
```

---

## 命令速查

| 动作 | 命令 |
|---|---|
| 查我的待办 | `companyctl task list --agent <id>` → 看 status=submitted |
| 认领 | `companyctl task claim --agent <id> --task-id <id>` |
| 看详情 | `companyctl task show --task-id <id>` |
| 完成回报 | `companyctl task done --agent <id> --task-id <id> --summary "…" --evidence <路径>` |
| 卡住 | `companyctl task block --agent <id> --task-id <id> --blocker "…"` |

## ⚠️ 避坑:调运行时 CLI 用绝对二进制路径

app 跑在你的交互式 shell 里,会加载 shell 函数/别名。比如 Claude Code 装了
`claude () { command claude --bare … }`,`--bare` 不读 OAuth 登录态 → 调裸 `claude` 会
误判"未登录"。**要直接调 claude / codex / agy 等 CLI 时,用绝对二进制路径**
(`/Users/shift/.local/bin/claude`、`/Users/shift/.local/bin/agy` …,`which -a <cmd>` 查真路径)
**或 `command claude`**,别用裸名。companyctl 本来就用绝对路径,不受影响。
(内核自己跑运行时不受影响——它走 subprocess + 绝对二进制,不读 shell 函数。)
