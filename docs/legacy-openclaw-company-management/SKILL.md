---
name: company-management
aliases: ["公司管理skill", "enterprise-governance"]
description: "OpenClaw 核心公司管理技能包：涵盖统一执行器、单源数据库约束、员工（Agent）入职规程及垃圾态清理准则。"
---

# 公司管理 (Company Management Skill)

此 Skill 是整个 OpenClaw 多智能体生态的“企业管理法典”与“基础设施包”。任何跨机器部署、新环境初始化、或对全局架构进行完善时，都必须以此为唯一依据。

如果你接到指令“完善公司管理skill”或“优化员工规则”，请直接修改本文件及配套目录下的模板。

## 一、四大企业级核心法则

### 1. 唯一执行器 (Unified Execution)
- **时间归属**：所有业务必须调用 `scripts/unified_time.py`，以 `Asia/Bangkok` 早上 7:00 作为昨天与今天的分界线。禁止任何业务脚本内联 `date.today()`。
- **动作路由**：所有浏览器操作和发包动作强制走 `unified_browser.py` 或 `unified_outbound.py`。禁止各业务自行拉起不受管的 Chrome 或手写死代码路由。

### 2. 数据单一真理 (Database-Driven Config)
- 所有业务账号、鉴权、路由映射必须存放在受 `UNIQUE` 约束保护的 SQLite 数据库（`skill_accounts.db`）中。
- **绝对纪律**：只允许 `UPSERT`（插入或替换），绝不允许跑一次脚本多出一条重复记录的数据污染。
- 架构参考：`templates/skill_accounts.sql`

### 3. 主从自治法则 (Control Tower & Autonomy)
- **总控 (main)**：确立全局标准（如统一下发 DB 架构、Time 脚本），不越权去写子业务代码。
- **专家 (sub-agents)**：在各自业务域内执行规则，理解自身业务逻辑。
- 跨部门交接必须通过 `agent_bus`（企业内部任务流）进行，附带明确的 `task_id` 和回执。

### 4. 零垃圾态纪律 (Transient Cleanup)
- 产生在 `/tmp/`、`logs/` 下的调试文件 `.bak`、`.patch`、重复生成的废弃 `.json` 等，一旦业务跑通必须顺手物理删除 (`rm -f`)。
- 不得让工作区堆积会造成自我困惑的脏数据。

### 5. Company Kernel 心跳桥接 (Heartbeat Bridge)
- OpenClaw 侧只通过 `company_kernel_bridge.py health` 读取 Company Kernel `doctor --summary`，不要复制内核心跳判断。
- 告警统一通过 `company_kernel_bridge.py heartbeat-alert` 读取既有 `company_runtime_alert.py --json-only` 结果；若 Company Kernel 心跳健康，应抑制 `company_wide_no_heartbeat` 与 `main_no_heartbeat` 误报。
- 默认路径可用 `COMPANY_KERNEL_DIR` 与 `OPENCLAW_COMPANY_RUNTIME_ALERT` 覆盖；不要把本机绝对路径硬编码进业务脚本。

---

## 二、员工（Agent）入职规程 (Employee Onboarding Protocol)

当需要新增一个业务 Agent（即“新员工”）时，严禁随意建个文件夹了事。必须严格走完以下标准化入职流程。

### 1. 员工核心档案参数 (Required Onboarding Params)
在入职新员工前，总控（main）必须集齐以下档案参数：
- `agentId`: 员工全局唯一标识（如 `krothong`, `chindahotpot`）。
- `aliases`: 识别别名（如 `["金达", "chinda"]`）。
- `business_domain`: 负责的具体业务线范围。
- `workspace_path`: 专属办公桌路径（严格隔离，如 `${OPENCLAW_ROOT:-$HOME/openclaw}/workspace-krothong`）。
- `cron_heartbeat`: 心跳/打卡频率（例：每5分钟，或每日定时）。
- `report_to`: 汇报对象（通常为 `main`）。

### 2. 标准入职办理步骤 (Onboarding Pipeline)

#### 步骤一：全局登记 (Global Registration)
- 将新员工参数写入注册表：`${OPENCLAW_WORKSPACE:-$HOME/openclaw/workspace-main}/config/agent_registry.json`；本机 Shift 环境可继续回退到既有 `workspace-xmanx`。
- 如果涉及 UI/前端路由，执行 `agent_registry.py --discover` 刷新全公司通讯录。
- `request_main.py` 必须接受 Company Kernel 员工（`codex`、`hermes`、`claude`、`trae`、`antigravity`、`openclaw-main`）作为合法发起人；这些员工不一定存在于旧 OpenClaw `agent_registry.json`。

#### 步骤二：分配工位与标准六件套 (Workspace Initialization)
为新员工创建专属 `workspace`，并严格初始化以下六个核心文件（缺一不可）：
1. `SOUL.md`: 设定员工的性格、底线和工作态度。
2. `IDENTITY.md`: 设定员工的技能清单和负责的业务边界。
3. `AGENTS.md`: 复制并注入“公司四大企业法则”（指向统一下发的工具链）。
4. `MEMORY.md`: 建立长效业务记忆结构。
5. `SESSION-STATE.md`: 初始化工作台状态。
6. `HEARTBEAT.md`: 约定定时触发的巡检与推进逻辑。

#### 步骤三：配发标准工具 (Toolchain Allocation)
- 从本 Skill 包下拷贝并软链统一基建：
  - 将 `templates/skill_accounts.sql` 结构纳入该业务的可用权限。
  - 注入 `agent_bus_worker.py`（务必在脚本的 `WORKSPACES` 数组里加上该员工的名字，使其能合法监听企业总线）。
- 授予该员工在 `skill_authorization` 表中的操作凭证。

#### 步骤四：入职测试 (Probation Task)
- `main` 通过 `agent_bus` 向新员工的 `inbox` 发送一条带有 `receipt_required: true` 的 `agent_request`（如：“打印你的工作边界并回复 ACK”）。
- 监控新员工是否能通过 `agent_bus_worker.py` 正确收到、处理并生成 `evidence_path`。
- 测试通过后，员工正式上线。

---

## 三、部署与更新说明 (Deployment)
要将这套管理机制迁移到新的 Mac 节点：
1. 检出此目录 `skills/company-management`。
2. 执行 `OPENCLAW_WORKSPACE=$HOME/openclaw/workspace-main ./install.sh` 初始化或加固全局数据库；该动作只建表/索引，不清空已有 `skill_accounts.db` 数据。
3. `install.sh` 会同步标准执行器到目标工作区 `scripts/`，包括 `unified_time.py`、`unified_browser.py`、`unified_outbound.py`、`agent_bus_worker.py`、`agent_registry.py`、`request_main.py`、`company_kernel_bridge.py` 及其通信契约依赖。
