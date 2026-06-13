#!/bin/zsh
# 把当前修复/文档分支推送到两个 GitHub 远程(不动 main)。
# 双击运行。需要你 Mac 上已配置好 GitHub 凭据(平时能 push 即可)。
set -e
cd /Users/shift/openclaw/company-kernel
BRANCH=claude/kernel-heal-and-docs

echo "=== 当前分支:$(git branch --show-current) ==="
git checkout "$BRANCH"

echo "=== 推送到 origin (shiftshen) ==="
git push -u origin "$BRANCH"

echo "=== 推送到 public (super-ai-company) ==="
git push -u public "$BRANCH"

echo ""
echo "=== 完成。两个远程都已收到分支 $BRANCH(未合并进 main)。 ==="
echo "如需回到 main:  git checkout main"
