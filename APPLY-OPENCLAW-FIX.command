#!/bin/zsh
# 验证 next_command 修复:重派"发日报"给 chindahotpot,看 OpenClaw 是否接受执行(不再 missing_next_command),然后推送 main。
set -e
ROOT=/Users/shift/openclaw/company-kernel
cd "$ROOT"

echo "=== 1/4 重新派发 发日报 给 chindahotpot(owner 免审批,走新桥接带 next_command)==="
bin/companyctl task submit --from owner-shift --to chindahotpot \
  --title "发一下今天日报" \
  --description "汇总今天公司动态发出日报(按你作为该员工的既有技能/渠道执行),完成后回填证据。" \
  --priority P1 2>&1 | python3 -c "import sys,json
try: d=json.load(sys.stdin); print('  新任务:',d.get('task',{}).get('id'))
except: print('  (已提交)')" || true

echo "=== 2/4 等守护桥接并执行(~45s)==="
sleep 45

echo "=== 3/4 结果检查 ==="
echo "  -- chindahotpot 最近 adapter 运行 --"
python3 -c "import sqlite3;c=sqlite3.connect('$ROOT/company.sqlite');c.row_factory=__import__('sqlite3').Row;[print('   ',r['created_at'],'ok=',r['ok']) for r in c.execute(\"SELECT created_at,ok FROM adapter_runs WHERE agent_id='chindahotpot' ORDER BY created_at DESC LIMIT 3\").fetchall()]"
echo "  -- 发日报 任务状态 --"
python3 -c "import sqlite3;c=sqlite3.connect('$ROOT/company.sqlite');c.row_factory=__import__('sqlite3').Row;[print('   ',r['id'],r['status'],'| blocker:',(r['blocker'] or '')[:80]) for r in c.execute(\"SELECT id,status,blocker FROM tasks WHERE title='发一下今天日报' ORDER BY created_at DESC LIMIT 1\").fetchall()]"
echo "  -- OpenClaw 是否又产生失败记录? --"
FN=$(ls /Users/shift/openclaw/ops/agent_bus/failed/chindahotpot/ 2>/dev/null | wc -l | tr -d ' ')
echo "   failed/chindahotpot 文件数: $FN (0 = 没有新失败)"

echo "=== 4/4 推送 main 到两个远程 ==="
git checkout main
git fetch origin 2>/dev/null||true; git fetch public 2>/dev/null||true
git merge --no-edit origin/main 2>/dev/null||true; git merge --no-edit public/main 2>/dev/null||true
git push origin main && echo "  pushed origin"
git push public main && echo "  pushed public"

echo ""
echo "=== 完成。若 adapter ok=1 且 failed 数=0,说明 OpenClaw 员工已能凭技能执行任务。 ==="
