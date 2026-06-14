#!/bin/zsh
# 诊断:OpenClaw chindahotpot 失败原因 + codex 受阻任务缺的仓库路径。只读,不改任何东西。
# 输出写入 company-kernel/reports/diagnose-failures.txt 方便读取。
mkdir -p /Users/shift/openclaw/company-kernel/reports
exec > /Users/shift/openclaw/company-kernel/reports/diagnose-failures.txt 2>&1
echo "=== A) OpenClaw chindahotpot 失败记录全文 ==="
F=/Users/shift/openclaw/ops/agent_bus/failed/chindahotpot/20260614-232902-328a27.json
[ -f "$F" ] && cat "$F" || echo "  未找到 $F"
echo
echo "=== B) agent_bus/failed 各 agent 失败数量 ==="
for d in /Users/shift/openclaw/ops/agent_bus/failed/*/; do
  [ -d "$d" ] && echo "  $(basename $d): $(ls "$d" 2>/dev/null | wc -l | tr -d ' ') 条"
done 2>/dev/null
echo
echo "=== C) OpenClaw 业务员工的能力/脚本是否存在(发日报靠什么)==="
ls /Users/shift/openclaw/ops/agent_bus/ 2>/dev/null
echo "  --- chindahotpot 在 openclaw.json 的配置 ---"
python3 -c "import json;d=json.load(open('/Users/shift/openclaw/openclaw.json'));[print('   ',a.get('id'),'workspace=',a.get('workspace'),'identityName=',a.get('identityName')) for a in d.get('agents',{}).get('list',[]) if a.get('id') in ('chindahotpot','main')]" 2>/dev/null || echo "   (读不到 openclaw.json)"
echo
echo "=== D) damov4 下有哪些仓库(codex 受阻任务要的 webclients)==="
ls -la /Users/shift/Documents/vdamo/damov4/ 2>/dev/null || echo "  未找到 damov4 目录"
echo
echo "=== E) codex 受阻任务全文描述 ==="
cd /Users/shift/openclaw/company-kernel
python3 -c "import sqlite3;c=sqlite3.connect('company.sqlite');[print(r[0]) for r in c.execute(\"SELECT description FROM tasks WHERE id='task-20260614-230921-23882c'\").fetchall()]"
