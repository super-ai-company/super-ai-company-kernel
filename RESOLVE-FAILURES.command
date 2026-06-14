#!/bin/zsh
# A) 清理 OpenClaw "发日报" 失败噪音(纯目标任务,OpenClaw 缺 next_command 跑不了 -> 取消+归档失败记录)
# B) 修复 codex webclients 返工任务:工作区改成真实绝对路径并重派(codex 全权可做)
set -e
ROOT=/Users/shift/openclaw/company-kernel
cd "$ROOT"

echo "=== B) 修复 codex webclients 任务工作区 ==="
python3 - <<'PY'
import sqlite3, datetime
db="/Users/shift/openclaw/company-kernel/company.sqlite"
c=sqlite3.connect(db); ts=datetime.datetime.now().astimezone().isoformat(timespec="seconds")
tid="task-20260614-230921-23882c"
row=c.execute("SELECT description FROM tasks WHERE id=?",(tid,)).fetchone()
if row:
    desc=row[0] or ""
    desc=desc.replace("工作区: damov4 webclients/", "工作区: /Users/shift/Documents/vdamo/damov4/webclients")
    if "工作区: /Users/shift/Documents/vdamo/damov4/webclients" not in desc:
        desc="工作区: /Users/shift/Documents/vdamo/damov4/webclients\n"+desc
    c.execute("UPDATE tasks SET description=? WHERE id=?",(desc,tid)); c.commit()
    print("  已把工作区改为绝对路径:", tid)
else:
    print("  未找到任务", tid)
c.close()
PY
bin/companyctl task reopen --task-id task-20260614-230921-23882c --by claude \
  --reason "工作区改为绝对路径 /Users/shift/Documents/vdamo/damov4/webclients;codex 全权重跑" >/dev/null 2>&1 \
  && echo "  已重派 codex 任务(全权重跑真实仓库)" \
  || bin/companyctl task reopen --task-id task-20260614-230921-23882c --by owner-shift \
       --reason "工作区改为绝对路径;codex 全权重跑" >/dev/null 2>&1 && echo "  已重派(owner)"

echo "=== A) 取消无法执行的 发日报 任务 + 归档 OpenClaw 失败记录 ==="
python3 - <<'PY'
import sqlite3, datetime
db="/Users/shift/openclaw/company-kernel/company.sqlite"
c=sqlite3.connect(db); ts=datetime.datetime.now().astimezone().isoformat(timespec="seconds")
tid="task-20260614-225858-bae45e"
c.execute("UPDATE tasks SET status='cancelled', blocker='OpenClaw 缺 next_command,纯目标任务无法自动执行;待定义日报命令后再派', updated_at=? WHERE id=?",(ts,tid))
c.execute("DELETE FROM locks WHERE resource_key=?",(f"task:{tid}",))
c.commit(); c.close()
print("  已取消 发日报 任务:", tid)
PY
# 归档 OpenClaw 失败记录(不删,移到 archived 留证)
FAILED=/Users/shift/openclaw/ops/agent_bus/failed/chindahotpot
ARCH=/Users/shift/openclaw/ops/agent_bus/archived/chindahotpot
if [ -d "$FAILED" ] && ls "$FAILED"/*.json >/dev/null 2>&1; then
  mkdir -p "$ARCH" && mv "$FAILED"/*.json "$ARCH"/ 2>/dev/null && echo "  已归档 OpenClaw 失败记录到 $ARCH"
else
  echo "  无 OpenClaw 失败记录待归档"
fi

echo "=== 状态 ==="
python3 -c "import sqlite3;print('  ',dict((r[0],r[1]) for r in sqlite3.connect('$ROOT/company.sqlite').execute(\"SELECT status,COUNT(*) FROM tasks GROUP BY status\").fetchall()))"
echo ""
echo "=== 完成。codex webclients 返工已带真实仓库重派;发日报因 OpenClaw 缺命令暂取消。 ==="
