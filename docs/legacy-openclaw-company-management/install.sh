#!/usr/bin/env bash
set -euo pipefail

echo ">>> 开始部署 OpenClaw 公司管理技能包 (Company Management Skill)..."

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
OPENCLAW_ROOT="${OPENCLAW_ROOT:-${HOME}/openclaw}"
if [[ "${HOME}" == "/Users/owner" && -d "/Users/owner/openclaw" ]]; then
  OPENCLAW_ROOT="${OPENCLAW_ROOT:-/Users/owner/openclaw}"
fi
DEFAULT_WORKSPACE="${OPENCLAW_ROOT}/workspace-main"
if [[ "${HOME}" == "/Users/owner" && -d "/Users/owner/openclaw/workspace-xmanx" ]]; then
  DEFAULT_WORKSPACE="/Users/owner/openclaw/workspace-xmanx"
fi
WORKSPACE="${OPENCLAW_WORKSPACE:-$DEFAULT_WORKSPACE}"
SCRIPTS_DIR="$WORKSPACE/scripts"

# 1. 初始化数据库结构；只建表/索引，不删除现有数据
DB_DIR="$WORKSPACE/config"
DB_PATH="$DB_DIR/skill_accounts.db"
mkdir -p "$DB_DIR"
mkdir -p "$SCRIPTS_DIR"
echo "-> 校验并加固底层数据库约束 ($DB_PATH)..."
if command -v sqlite3 >/dev/null 2>&1; then
  sqlite3 "$DB_PATH" < "$BASE_DIR/templates/skill_accounts.sql"
else
  python3 - "$DB_PATH" "$BASE_DIR/templates/skill_accounts.sql" <<'PY'
import sqlite3
import sys
from pathlib import Path

db_path = Path(sys.argv[1])
schema_path = Path(sys.argv[2])
conn = sqlite3.connect(db_path)
try:
    conn.executescript(schema_path.read_text())
    conn.commit()
finally:
    conn.close()
PY
fi

# 2. 分发统一执行器
echo "-> 分发统一执行器..."
for script in \
  unified_time.py \
  unified_browser.py \
  unified_outbound.py \
  skill_accounts_db.py \
  agent_bus_worker.py \
  agent_registry.py \
  request_main.py \
  agent_comm_contract.py \
  company_kernel_bridge.py \
  attendance_sweep.py \
  agent_comm_smoke.py \
  approval_to_codex_queue.py \
  cleanup_trash.sh \
  progress_report.py
do
  install -m 0755 "$BASE_DIR/scripts/$script" "$SCRIPTS_DIR/$script"
done

# 3. 输出部署结果
echo "-> 员工入职规程已落盘至: $BASE_DIR/SKILL.md"
echo "-> 目标工作区: $WORKSPACE"
echo "-> 部署完成！整个机器已被收编为统一标准架构。"
