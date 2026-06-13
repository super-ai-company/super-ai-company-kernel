# Agent 接入指南（跨平台 · 全智能体）

让任意 AI 智能体（codex / claude / hermes / openclaw / trae / antigravity，或任何自定义 agent）
在 **Windows / Linux / macOS** 上接入 Company Kernel 员工体系。本指南不依赖任何特定机器路径，
全部用环境变量表达。

---

## 0. 通用前置（任何系统）

```bash
git clone https://github.com/super-ai-company/super-ai-company-kernel.git
cd super-ai-company-kernel
python3 --version   # 需要 Python 3.10+
```

设置内核根目录与数据库（所有命令都读这两个环境变量；不设则默认仓库目录）：

| 系统 | 设置方式 |
|---|---|
| macOS / Linux (bash/zsh) | `export OPENCLAW_COMPANY_KERNEL_ROOT="$PWD"` |
| Windows (PowerShell) | `$env:OPENCLAW_COMPANY_KERNEL_ROOT = (Get-Location).Path` |
| Windows (cmd) | `set OPENCLAW_COMPANY_KERNEL_ROOT=%CD%` |

> Windows 上把下面示例里的 `bin/companyctl` 换成 `python -m company_kernel.companyctl`，
> `bin/company-daemon` 换成 `python -m company_kernel.company_daemon`，其余参数不变。

自检：

```bash
bin/companyctl doctor --summary          # macOS/Linux
python -m company_kernel.companyctl doctor --summary   # Windows
```

---

## 1. 一条命令新增任意员工

```bash
bin/company-add-employee \
  --id <员工ID> --name "<显示名>" --role <角色> \
  --runtime <运行时> --workspace <该员工的项目绝对路径> \
  [--skills "技能1,技能2"] [--enable-worker] [--execute]
```

- 不加 `--enable-worker`：只注册员工（安全，不会自动执行）。
- 加 `--enable-worker`：同时在 daemon 里启用该员工的 adapter worker。
- 加 `--execute`：worker 真实调用对应运行时（否则 dry-run 只生成任务卡）。

`--runtime` 取值与对应 adapter：

| runtime | 适配器 | 真实执行命令 | 说明 |
|---|---|---|---|
| `codex` | company-codex-adapter | `codex exec` | 工程开发，支持 `--model` |
| `claude` | company-claude-adapter | `claude -p` | 分析 / 文档 / 评审 |
| `hermes` | company-hermes-adapter | `hermes -z` | 本机工具 / 自动化主管 |
| `openclaw` | company-openclaw-adapter | 写 OpenClaw `ops/agent_bus` | 业务运营员工 |
| `trae` | company-trae-adapter | `trae chat` | IDE 型开发 |
| `antigravity` | company-antigravity-adapter | 打开 App + GUI worker 回证据 | 多 Agent / 浏览器 |
| `local` | company-adapter-worker | 无（dry-run / 人工执行器） | 通用占位，任意脚本 |

---

## 2. 各运行时接入要点（环境无关）

### codex（工程）
1. 安装 codex CLI，确认 `codex --version` 可用。
2. `company-add-employee --id codex --name Codex --role developer --runtime codex --workspace <你的代码仓库> --enable-worker --execute`
3. 可选模型：编辑 `config/daemon.json` 的 codex worker，args 里加 `--model <模型名>`。
4. 裁决门要求：codex 的最终输出必须以 `STATUS: completed` 或 `STATUS: blocked - <原因>` 结尾。

### claude（分析/文档）
1. 安装 Claude CLI，确认 `claude --version`。
2. 先验证适配器：`companyctl runtime verify-adapters --agents claude --allow-candidate`。
3. 注册并激活：`company-add-employee --id claude --runtime claude ...`，确认状态从 candidate 转 active。

### hermes（本机工具主管）
1. 确认 `hermes --version`（实测 v0.14.0 路径示例 `~/.local/bin/hermes`）。
2. `company-add-employee --id hermes --runtime hermes --workspace <hermes工作目录> --enable-worker`（先 dry-run）。
3. 验证无误后再在 daemon worker 上加 `--execute`，真实调用 `hermes -z`。

### openclaw（业务运营员工）
1. openclaw adapter 把内核任务桥接到 OpenClaw 旧 `ops/agent_bus`。
2. 默认 dry-run 生成 payload；加 `--execute` 才写真实 bus，且受审批门控制（高风险动作需 owner 批准）。
3. 适合把现有 OpenClaw 业务 Agent（如 nestcar/krothong）登记为员工。

### trae（IDE 开发）
1. 确认 `trae` CLI 可用。
2. `company-add-employee --id trae --runtime trae ...`；真实执行调用 `trae chat`。

### antigravity（GUI / 多 Agent）
1. 注册为员工后默认 dry-run 生成 GUI brief。
2. `--execute` 仅打开 App，由 GUI worker 用 `companyctl task done/block` 回传证据，不伪造完成。

### 任意自定义 agent（Cursor / Devin / 自建模型 / 人类）
1. 先注册运行时：`companyctl runtime register --runtime <名字> --command "<启动命令>"`。
2. 用 `--runtime local`（或你注册的名字）接入；执行交给通用 worker 或你自己的脚本。
3. **新增 agent 是命令，不是改代码** —— 这是内核的核心设计。

---

## 3. 启动后台（让员工自动接任务）

```bash
# 持续巡检：自动派活、心跳、看门狗、备份
bin/company-daemon --once --summary           # 跑一轮
# 常驻：macOS 用 launchd（仓库含模板），Linux 用 systemd/cron，Windows 用任务计划程序

# 控制台（浏览器打开）
bin/company-api-gateway --port 8765           # 然后访问 http://127.0.0.1:8765/
```

跨平台常驻建议：
- **macOS**：`bash bin/company-daemon-install-launchd`
- **Linux**：把 `bin/company-daemon --once --summary` 写进 systemd timer 或 crontab（每 1-5 分钟）
- **Windows**：用「任务计划程序」定时运行 `python -m company_kernel.company_daemon --once --summary`

---

## 4. 上线安全（对外暴露必做）

```bash
# 开启网关鉴权：设置 token 后所有 /v1 写操作需 Bearer token
export COMPANY_KERNEL_API_TOKEN="<你的强随机串>"   # macOS/Linux
$env:COMPANY_KERNEL_API_TOKEN = "<token>"          # Windows PowerShell
```

- 控制台首次访问会提示输入该 token（存浏览器本地）。
- 数据库自动备份已默认开启（daemon 每 24h，`bin/company-backup` 可手动快照/恢复）。
- 网关默认只绑 `127.0.0.1`；要对外需显式 `--host 0.0.0.0` 并务必先设 token。

---

## 5. 验证接入成功

```bash
bin/companyctl employee list                  # 看到新员工
bin/companyctl task submit --from owner --to <员工ID> --title "smoke 测试"
bin/company-daemon --once --summary           # 自动执行
bin/companyctl task list --status completed    # 看到完成 + 证据
```

完整全量回归：`python3 -B -m unittest discover -s tests`（基线 206 通过）。
