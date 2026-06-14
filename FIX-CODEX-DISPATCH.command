#!/bin/zsh
# 修复:claude 发不出任务给 codex。
# 根因:claude 在 config/company_communications.json 里被标了 communication_paused=true,
#        而 task submit 的第一道检查就是"发送方被暂停 -> 拒绝"。
# 本脚本解除 claude 的通信暂停,并验证 claude->codex 派活已放行。双击运行。
set -e
ROOT=/Users/shift/openclaw/company-kernel
cd "$ROOT"

echo "=== 1/3 解除 claude 的通信暂停 ==="
OPENCLAW_COMPANY_KERNEL_ROOT="$ROOT" python3 - <<'PY'
import company_kernel.companyctl as k
r = k.set_employee_communication_enabled("claude", True)
print("  agent =", r["agent"], " communication_paused =", r["communication_paused"])
PY

echo "=== 2/3 验证 claude -> codex 是否允许派活 ==="
bin/companyctl communication check --from claude --to codex --action assign

echo "=== 3/3 确认 codex 仍是 active(目标可被派活)==="
bin/companyctl employee show --id codex 2>/dev/null | python3 -c "import sys,json
try:
    d=json.load(sys.stdin); e=d.get('employee',d)
    print('  codex status =', e.get('status'))
except Exception: print('  (查看 employee show 输出)')" || bin/companyctl employee show --id codex

echo ""
echo "=== 完成。若上面 communication check 显示 allowed=true,claude 现在就能给 codex 派活。 ==="
echo "    试派一条:bin/companyctl task submit --from claude --to codex --title \"...\" --description \"...\" --priority P1"
