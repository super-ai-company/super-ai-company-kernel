#!/bin/zsh
# Company Kernel 一键修复：git 同步 + 重启 gateway + 恢复 daemon
# 由 Cowork 会话生成；双击运行，完成后窗口保留输出供检查。
LOG=/Users/shift/openclaw/company-kernel/state/fix-and-sync.log
exec > >(tee "$LOG") 2>&1
set -x
cd /Users/shift/openclaw/company-kernel || exit 1

# 1. git 同步（先 pull --rebase 再 push）
bash bin/company-git-sync "sync: cowork session commits" || true

# 2. 停掉占着 8765 的旧 gateway（xmanx 克隆的进程）
lsof -ti tcp:8765 | xargs kill 2>/dev/null || true
sleep 1

# 3. 从本克隆启动新 gateway（带控制台页面）
launchctl kickstart -k "gui/$(id -u)/ai.openclaw.company-kernel.api" 2>/dev/null \
  || launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/ai.openclaw.company-kernel.api.plist" 2>/dev/null \
  || (nohup bin/company-api-gateway --quiet >/dev/null 2>&1 &)
sleep 2

# 4. 重装并拉起 daemon（已停多日）
bash bin/company-daemon-install-launchd || true

# 5. 验收
echo "--- console check (应输出 <!DOCTYPE html):"
curl -s http://127.0.0.1:8765/ | head -c 120; echo
echo "--- doctor:"
bin/companyctl doctor --summary || true

set +x
echo ""
echo "=== 完成。窗口可关闭，日志已存 $LOG ==="
