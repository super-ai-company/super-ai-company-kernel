#!/bin/zsh
# 把 main 推送到两个 GitHub 远程(本次已把修复+文档合并进 main)。
# 双击运行。需要你 Mac 上已配置好 GitHub 凭据(平时能 push 即可)。
set -e
cd /Users/shift/openclaw/company-kernel

git checkout main
echo "=== main 最新提交 ==="
git log --oneline -3 main

echo "=== 推送 main 到 origin (shiftshen) ==="
git push origin main

echo "=== 推送 main 到 public (super-ai-company) ==="
git push public main

# 同时把开发分支也推上去留档(可选)
git push -u origin claude/kernel-heal-and-docs 2>/dev/null || true
git push -u public claude/kernel-heal-and-docs 2>/dev/null || true

echo ""
echo "=== 完成。两个远程的 main 都已更新。以后开发请在分支上做。 ==="
