#!/bin/zsh
# 推送合并后的 main（已含团队全部历史，fast-forward）到两个远程 + 更新 tag
cd /Users/shift/openclaw/company-kernel || exit 1
LOG=state/push-merged.log
exec > >(tee "$LOG") 2>&1
set -x
git tag -f v1.0.0 -m "Company Kernel v1.0.0 (merged)"
git push public main
git push -f public v1.0.0
git push origin main
git push -f origin v1.0.0
set +x
echo ""
echo "=== 合并后 main 已推送到两个远程 ==="
echo "团队: https://github.com/super-ai-company/super-ai-company-kernel"
echo "个人: https://github.com/shiftshen/super-ai-company-kernel"
echo "=== 窗口可关闭 ==="
