#!/bin/zsh
# Company Kernel 全量回归（真实环境）
cd /Users/shift/openclaw/company-kernel || exit 1
REPORT=state/test-report.txt
echo "=== test run $(date) ===" > "$REPORT"
python3 -B -m unittest discover -s tests 2>&1 | tail -15 >> "$REPORT"
echo "" >> "$REPORT"
echo "--- doctor:" >> "$REPORT"
bin/companyctl doctor --summary >> "$REPORT" 2>&1
cat "$REPORT"
echo "=== 完成，报告在 state/test-report.txt ==="
