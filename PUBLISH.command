#!/bin/zsh
# 双击发布 v1.0.0 到个人 + 团队两个 GitHub 远程
cd /Users/shift/openclaw/company-kernel || exit 1
bash bin/company-publish "v$(cat VERSION 2>/dev/null || echo 1.0.0)"
echo ""
echo "=== 窗口可关闭 ==="
