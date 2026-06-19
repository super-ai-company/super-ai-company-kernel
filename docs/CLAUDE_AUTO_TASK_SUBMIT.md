# Claude 自动提交任务到待领取队列

目标：Claude 不再每次执行固定的 `DISPATCH-BACKEND-P1.command`，而是用通用脚本或 `companyctl task submit` 把任意新任务提交进 Company Kernel 任务账本。

## 正确机制

```text
Claude 终端
  -> CLAUDE-SUBMIT-TASK.command 或 bin/companyctl task submit
  -> tasks.status=submitted
  -> company-daemon 每 30 秒扫描
  -> codex/hermes/openclaw adapter 自动 claim
  -> running / completed / blocked + evidence
```

`DISPATCH-BACKEND-P1.command` 是一次性脚本，只适用于固定的 `vdamo-cloud 云端 P1 后端`任务。不要把它当通用入口。

## Claude 推荐命令

先写任务描述文件：

```bash
cat > /tmp/claude-task.md <<'EOF'
工作区: /absolute/path/to/project

目标:
写清楚要做什么。

要求:
1. 先读取项目现状。
2. 只改必要文件。
3. 运行最小验证。
4. 成功输出 STATUS: completed。
5. 卡住输出 STATUS: blocked - 具体原因。
6. 提交 evidence。

验收:
写清楚命令和成功标准。
EOF
```

提交给 Codex：

```bash
cd $OPENCLAW_COMPANY_KERNEL_ROOT

./CLAUDE-SUBMIT-TASK.command \
  --from claude \
  --to codex \
  --priority P1 \
  --title "这里写任务标题" \
  --description-file /tmp/claude-task.md
```

提交后不要再手动点固定 dispatch 脚本。等待 daemon 自动认领：

```bash
bin/companyctl task list --agent codex
bin/companyctl runtime adapter-runs --agent codex --limit 10
```

## 直接用 companyctl

不用脚本也可以：

```bash
bin/companyctl task submit \
  --from claude \
  --to codex \
  --title "这里写任务标题" \
  --description "$(cat /tmp/claude-task.md)" \
  --priority P1
```

## 判断是否进入待领取

提交后查看：

```bash
bin/companyctl task list --agent codex | head -40
```

状态含义：

- `submitted`：已进入待领取队列。
- `claimed`：已被 Codex worker 领取。
- `completed`：完成，必须检查 evidence。
- `blocked`：卡住，需要 Claude/owner 补充。

如果 daemon 正常，`submitted` 可能很快变成 `claimed`，这是正常的。

## Claude 禁止事项

- 不要重复执行 `DISPATCH-BACKEND-P1.command`，除非 owner 明确要求重新派那一个固定任务。
- 不要用固定 `.command` 作为所有任务入口。
- 不要把 Claude 自己改成 active 来绕过验证。
- 不要绕过 `companyctl task submit` 直接改数据库。
- 不要把 `claimed` 误判为失败；这说明 worker 已领取。

