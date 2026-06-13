#!/usr/bin/env bash
set -euo pipefail
cd /app

# License gate (no-op unless COMPANY_KERNEL_LICENSE_ENFORCE=1).
LIC=$(python3 -c "import json;from company_kernel import license;print(json.dumps(license.license_status()))")
LIC_OK=$(python3 -c "import json,sys;print(json.loads(sys.argv[1])['ok'])" "$LIC")
echo "[entrypoint] license: $LIC"
if [ "$LIC_OK" != "True" ]; then
  echo "[entrypoint] FATAL: license check failed. Set a valid COMPANY_KERNEL_LICENSE_KEY (and _SECRET), or unset COMPANY_KERNEL_LICENSE_ENFORCE for self-host." >&2
  exit 78
fi

# Initialize the database/schema on first boot.
mkdir -p "$(dirname "${COMPANY_KERNEL_DB_PATH:-/data/company.sqlite}")"
python3 -m company_kernel.companyctl doctor >/dev/null 2>&1 || true

HOST="${COMPANY_KERNEL_API_HOST:-0.0.0.0}"
PORT="${COMPANY_KERNEL_API_PORT:-8765}"

start_gateway() { exec python3 -m company_kernel.api_gateway --host "$HOST" --port "$PORT"; }
start_daemon()  { exec python3 -m company_kernel.company_daemon; }

case "${1:-all}" in
  gateway) start_gateway ;;
  daemon)  start_daemon ;;
  all)
    # daemon in background, gateway in foreground (PID 1).
    python3 -m company_kernel.company_daemon &
    DAEMON_PID=$!
    trap 'kill "$DAEMON_PID" 2>/dev/null || true' TERM INT
    start_gateway
    ;;
  *) exec "$@" ;;
esac
