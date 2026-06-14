# Claude 自动派任务入口桥

目的：Claude 不再直接写 `company.sqlite`，也不再反复执行一次性的 `DISPATCH-*.command`。Claude 只需要把任务 JSON 写进 intake 目录，Mac 原生 LaunchAgent 会自动导入 Company Kernel 任务账本，后续由 daemon/adapter 自动领取。

## 固定目录

```bash
/Users/owner/openclaw/company-kernel/state/task-intake/incoming
/Users/owner/openclaw/company-kernel/state/task-intake/processed
/Users/owner/openclaw/company-kernel/state/task-intake/failed
```

## Claude 派任务方式

Claude 只执行这种写文件动作：

```bash
cd /Users/owner/openclaw/company-kernel
mkdir -p state/task-intake/incoming

cat > state/task-intake/incoming/task-claude-to-codex-$(date +%Y%m%d-%H%M%S).json <<'JSON'
{
  "from": "claude",
  "to": "codex",
  "priority": "P1",
  "title": "这里写任务标题",
  "description": "这里写完整任务要求、验收标准、目标路径和禁止事项"
}
JSON
```

允许字段：

- `task_id`：可选；不填由 Kernel 自动生成。
- `from` / `source` / `source_agent`：提交员工，例如 `claude`。
- `to` / `target` / `target_agent`：目标员工，例如 `codex`。
- `title`：必填。
- `description` / `body` / `message`：任务正文。
- `priority`：默认 `P2`，可填 `P0/P1/P2/P3`。
- `metadata`：可选对象，会进入 task metadata。

## Mac 原生导入

手动导入一次：

```bash
cd /Users/owner/openclaw/company-kernel
bin/company-task-intake-importer
```

安装自动导入 LaunchAgent：

```bash
cd /Users/owner/openclaw/company-kernel
bin/company-task-intake-install-launchd
```

安装后每 15 秒扫描一次 `state/task-intake/incoming/*.json`。

## 验证

```bash
cd /Users/owner/openclaw/company-kernel
bin/companyctl task list --agent codex | tail -40
ls -lt state/task-intake/processed | head
ls -lt state/task-intake/failed | head
```

成功时：

- incoming 里的 JSON 会移动到 `processed/`。
- 同目录生成 `.receipt.json`。
- `companyctl task list --agent codex` 能看到任务。
- 如果 codex worker 正常运行，任务会从 `submitted` 变成 `claimed/running/completed/blocked`。

失败时：

- JSON 会移动到 `failed/`。
- `.receipt.json` 里有错误原因。

## 禁止

- 不要让 Claude 直接写 `company.sqlite`。
- 不要把一次性 `DISPATCH-BACKEND-P1.command` 当通用入口。
- 不要在 Claude 沙箱里直接跑长任务 worker。
- 不要把文件写入 `incoming` 后立即宣称完成；必须看 processed receipt 和 task list。
