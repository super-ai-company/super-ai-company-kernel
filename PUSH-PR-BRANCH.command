#!/bin/zsh
# 把 v1.0 工作推成团队仓库的一个分支并打印 PR 链接（不触碰 main，安全）
cd /Users/shift/openclaw/company-kernel || exit 1
LOG=state/push-pr-branch.log
exec > >(tee "$LOG") 2>&1
set -x
BRANCH=claude/v1-production-hardening

# 确保分支指向当前 main 内容
git branch -f "$BRANCH" main

# 推到团队仓库和个人仓库（只推这个分支，不动任何 main）
git push -f public "$BRANCH"
git push -f origin "$BRANCH"

set +x
echo ""
echo "=== 分支已推送。开 PR 链接（点开即可创建 Pull Request）: ==="
echo "团队仓库 PR: https://github.com/super-ai-company/super-ai-company-kernel/compare/main...$BRANCH?expand=1"
echo "个人仓库 PR: https://github.com/shiftshen/super-ai-company-kernel/compare/main...$BRANCH?expand=1"
echo "=== 窗口可关闭，日志 state/push-pr-branch.log ==="
