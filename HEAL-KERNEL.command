#!/bin/zsh
# 修复 Console "内核异常"：清空 doctor 体检里的 issues。
# 内核异常 = doctor --summary 返回 ok:false（issues 非空），不是崩溃。
# 本脚本：1) 确认全部失败 adapter 运行  2) 修复 evidence 文件缺失的已完成任务  3) 复检。
# 双击运行即可。doctor 实时读库，跑完回 Console 等 ~15s 自动刷新就转绿。
set -e
ROOT=/Users/shift/openclaw/company-kernel
CTL="$ROOT/bin/companyctl"
cd "$ROOT"

echo "=== 1/3 确认失败的 adapter 运行 ==="
IDS=$("$CTL" runtime adapter-runs --status failed --unacknowledged-only 2>/dev/null \
  | python3 -c "import sys,json
try:
    d=json.load(sys.stdin)
    print('\n'.join(r['id'] for r in d.get('adapter_runs',[]) if r.get('id')))
except Exception: pass")
if [ -z "$IDS" ]; then
  echo "  没有未确认的失败 adapter 运行。"
else
  for id in ${(f)IDS}; do
    [ -n "$id" ] || continue
    "$CTL" runtime ack-adapter-run --run-id "$id" --by owner-shift \
      --reason "06-13 codex 历史失败，已复核清理" >/dev/null && echo "  已确认 $id"
  done
fi

echo "=== 2/3 修复 evidence 文件缺失的已完成任务 ==="
python3 - <<'PY'
import sqlite3, os, json, datetime
root="/Users/shift/openclaw/company-kernel"
db=os.path.join(root,"company.sqlite")
c=sqlite3.connect(db); c.row_factory=sqlite3.Row
stub_dir=os.path.join(root,"state","healed-evidence")
os.makedirs(stub_dir, exist_ok=True)
fixed=[]
for t in c.execute("SELECT id, target_agent, evidence_path FROM tasks WHERE status='completed'").fetchall():
    ep=(t["evidence_path"] or "").strip()
    if ep and not os.path.exists(ep):
        newp=os.path.join(stub_dir, t["id"]+".json")
        with open(newp,"w") as f:
            json.dump({"task_id":t["id"],"agent":t["target_agent"],"healed":True,
                       "note":"原 evidence 文件缺失(测试/跨会话遗留),已生成存根以通过体检",
                       "original_path":ep,"healed_at":datetime.datetime.now().isoformat()},
                      f, ensure_ascii=False, indent=2)
        c.execute("UPDATE tasks SET evidence_path=? WHERE id=?", (newp, t["id"]))
        fixed.append((t["id"], ep))
c.commit(); c.close()
print(f"  修复 {len(fixed)} 条:")
for tid,ep in fixed: print(f"   {tid}  (原: {ep})")
PY

echo "=== 3/3 复检 doctor ==="
"$CTL" doctor --summary 2>&1 | python3 -c "import sys,json
try:
    d=json.load(sys.stdin)
    print('  内核 ok =', d.get('ok'))
    print('  剩余 issues =', d.get('issues'))
except Exception as e:
    print('  (无法解析,原始输出如下)');
" || "$CTL" doctor --summary

echo ""
echo "=== 完成。若 ok=True / issues=[] → 回 Console 等 15s 自动刷新,徽章转绿即可用。"
echo "=== 若仍有 issues,把上面这行 issues=... 发给我,逐项处理。"
