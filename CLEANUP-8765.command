#!/bin/zsh
# 收编 8765：把 ai.openclaw.company-kernel.api 从旧 xmanx 克隆改指到 company-kernel
LOG=/Users/shift/openclaw/company-kernel/state/cleanup-8765.log
exec > >(tee "$LOG") 2>&1
set -x
LABEL="ai.openclaw.company-kernel.api"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

# 备份旧 plist
cp "$PLIST" "$PLIST.bak-$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
sleep 1

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/shift/openclaw/company-kernel/bin/company-api-gateway</string>
    <string>--port</string><string>8765</string>
    <string>--quiet</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>OPENCLAW_COMPANY_KERNEL_ROOT</key><string>/Users/shift/openclaw/company-kernel</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/shift/openclaw/company-kernel/logs/company-api.launchd.out.log</string>
  <key>StandardErrorPath</key><string>/Users/shift/openclaw/company-kernel/logs/company-api.launchd.err.log</string>
</dict>
</plist>
EOF

# 杀掉残余进程并重启
lsof -ti tcp:8765 | xargs kill 2>/dev/null || true
sleep 1
launchctl bootstrap "gui/$(id -u)" "$PLIST"
sleep 2

echo "--- 验收（应输出 <!DOCTYPE html）:"
curl -s http://127.0.0.1:8765/ | head -c 60; echo
set +x
echo ""
echo "=== 完成。8765 已收编为新控制台，旧 plist 已备份。 ==="
