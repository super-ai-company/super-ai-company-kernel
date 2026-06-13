# Company Kernel 状态与规划（2026-06-13）

## 一、现在它到底是什么状态：可用，且经过实测

内核已从"一直停着的半成品"变成**经真实环境验证的可运行系统**。证据：

- Mac 真实环境全量回归 **190/190 通过**（沙箱卡死是环境问题，与代码无关）
- 真实 codex 端到端闭环跑通：派任务 → codex 真实执行 → 写证据 → 裁决门验收 → 完成
- 员工互通矩阵全部验证：openclaw→codex（真实执行）、codex→krothong、codex→antigravity、hermes↔nestcar、OpenClaw 内部 hook 接力、看门狗实战告警
- 控制台、API 网关、daemon、launchd 常驻全部上线

## 二、今天修掉的 3 个真 bug（都是实测暴露的，不是表面工作）

1. **控制台"已完成"永远显示 0** —— 看板用 `done` 匹配，但内核状态是 `completed`，47 个完成任务全部不可见。已修为状态集合匹配。
2. **控制台批不动审批** —— 审批 ID 含中文（如 `S04-只读态`），浏览器 `encodeURIComponent` 编码后网关未解码就查库 → 查不到 → 批准失败。已修：网关统一 `unquote` 路径。
3. **正常开发任务被误判为高风险** —— `kernel_change` 关键词含 `schema`/`通信协议` 这种通用词，POS 任务一提到数据库 schema 或 HTTP 协议就触发审批，导致 20 张卡卡死。已收窄到只匹配真正的内核治理词。

外加：daemon 不再把"没任务可领"的空跑记成失败（消除健康检查噪音）。

## 三、部署会不会很麻烦？要不要每步都封装成 skill？

**结论：不需要每步都做成 skill。** 内核已有统一 CLI（`companyctl`），过度封装反而增加维护面。正确做法是三层：

1. **一次性环境引导**（每台新机器一次）：`git clone` → 设环境变量 → 装 launchd。已有 `bin/company-daemon-install-launchd`、`START-CONSOLE.command`。
2. **新增员工一条命令**（新建）：`bin/company-add-employee` —— 把"注册 runtime + onboard 员工 + 启用 daemon worker + 验证心跳"四步合一，幂等、默认 dry-run 安全。
   ```bash
   bin/company-add-employee --id qa-bot --name "QA Bot" --role developer \
     --runtime codex --workspace /路径 --skills "python,review" --enable-worker --execute
   ```
3. **一个 Cowork skill（而非十个）**：只在"自然语言入口"这一层做 skill，让任意 Claude/Cowork 会话能对话式调用上面的命令。这是 skill 真正增值的地方，不是把每个 CLI 子命令都包一遍。

即：**部署 = 1 个引导脚本 + 1 条加员工命令 + 1 个对话 skill**，不是几十个 skill。

## 四、进度列表

### 已完成 ✅
- [x] 裁决门（STATUS 标记，done≠合格）
- [x] 任务级工作区（`工作区:` 指令 + 内核自我保护）
- [x] codex exec 超时保护 + --skip-git-repo-check
- [x] 诚实心跳 + 断链看门狗
- [x] 实时控制台（员工/任务/消息/对话/审批/活动流）
- [x] API/RPC/gRPC 网关 + launchd 常驻
- [x] 控制台 3 个 bug 修复（completed 状态 / 中文审批 ID / 审批误判）
- [x] 员工互通矩阵实测
- [x] 一条命令新增员工 `company-add-employee`
- [x] GitHub main 持续同步

### 进行中 🔧（codex 主导，claude 验收）
- [ ] hermes 真实执行认证 runbook
- [ ] claude 反向通道激活方案
- [ ] antigravity GUI 往返认证设计
- [ ] codex 内核开发交接回执 + 设计风险评审

### 待办 📋（owner 决策后）
- [ ] Cowork 对话式 onboarding skill（封装 add-employee）
- [ ] codex 沙箱环境补全（php/docker，需 owner 授权高权限）
- [ ] 一键 bootstrap 脚本（裸机 clone→env→launchd→verify 合一）
- [ ] 控制台增加"新增员工"表单（调 add-employee）

## 五、给 owner 的当前可操作项
1. 控制台批审批现在能用了（中文 ID 已修）——重启网关后即生效。
2. Damov4 那 20 张卡：用收窄后的关键词重新提交就不会再卡审批；已存在的可在控制台逐条批准。
3. 决定是否给 codex 补 docker/全盘权限（建议：默认不给，按任务走审批）。
