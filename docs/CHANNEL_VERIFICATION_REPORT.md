# 全员链路验证报告（活体冒烟，2026-06-13）

通过 `LIVE-SMOKE.command` 在真实 Mac 环境逐条验证：检测 CLI → 派真任务 → `--execute` 真跑 → 读裁决。
结论先行：**内核侧的通路全部接通；能不能真干活，现在只取决于各 runtime 的 CLI 是否已安装并登录。**

## 逐条结论

| 链路 | CLI 在本机 | 内核通路 | 真实执行 | 结论 |
|---|---|---|---|---|
| **codex** | ✅ /opt/homebrew/bin/codex | ✅ | ✅ 任务 completed | **完全打通，真实干活** |
| **hermes** | ✅ ~/.local/bin/hermes | ✅ | ✅ 任务 completed | **完全打通，真实干活** |
| **claude** | ✅ /opt/homebrew/bin/claude | ✅（本轮已修） | ❌ CLI 返回 "Not logged in" | 通路已通，**差一步：claude 登录账号** |
| **trae** | ✅ /usr/local/bin/trae | ✅（本轮已修） | ❌ CLI 无输出（未配置） | 通路已通，**差一步：trae 登录/配置** |
| **openclaw** | ✅ /opt/homebrew/bin/openclaw | ✅ | ◑ 任务 claimed（已桥接到 ops/agent_bus） | 桥接成功，完成需 OpenClaw 侧处理回写 |
| **antigravity** | ✅ /Applications/Antigravity.app | ✅ | ◑ 打开 App 后 blocked 等 GUI 回证据 | **设计如此**（GUI 类，需人/GUI worker 回证据） |

## 本轮修了什么（真实发现 → 真实修复）

冒烟暴露了一个**真 bug**：claude/trae 之前根本无法被激活，因为
1. 路由 `direct_runtime_command` 没有 claude/trae 分支；
2. 两个适配器没有 `--direct-message` 验证处理。
→ 导致 `verify-direct` 永远过不了 → 给它们派任务被内核拒绝（"target not active"）。

已修复：给 claude/trae 适配器加了直连验证通路 + 路由分支。修复后冒烟**真的调用到了它们的 CLI**——这才暴露出真正的拦路原因是 CLI 没登录（claude 明确报 "Please run /login"）。这是环境配置，不是内核问题。

## 现在能不能"自己用"

**能，而且已经在真实干活的有 codex + hermes 两条**——足够你自营交付跑闭环。其余：
- **claude / trae**：你在 Mac 上各登录一次（`claude` 跑一次走登录；trae 配好账号），再跑一次 `LIVE-SMOKE.command` 即可激活，无需改任何代码。
- **openclaw**：桥接已通，业务员工（nestcar 等）的真实完成依赖 OpenClaw 那边把结果回写内核（你原有体系）。
- **antigravity**：按设计是"打开 App + GUI/人工回证据"，不是全自动。

## 治理验证（顺带确认有效）

冒烟还实证了内核的治理是真的在拦：
- 候选员工（claude/trae）**未经真实验证不能被派任务**——内核正确拒绝，逼着走 verify-direct。
- 这正是你要的"先确认能真实干活，再谈角色权限"的底座：**激活 = 真实通信验证通过**，不是手动一改状态就算。

## 下一步（你确认后）

1. **登录 claude / trae**（owner 操作，各一次），重跑 LIVE-SMOKE → 这两条转为"真实干活"。
2. 全员链路确认后，再做**角色权限分配**（谁能指挥谁）——切到 strict 通信模式 + 配 allowlist。
3. 然后才进入 Phase 2（成本闸 / 验收器 / 自营垂类闭环）。
