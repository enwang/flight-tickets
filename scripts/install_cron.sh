#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="$(command -v python3)"
SCRIPT="$ROOT_DIR/scripts/daily_flight_report.py"
LOG="$ROOT_DIR/reports/cron.log"

mkdir -p "$ROOT_DIR/reports"

CRON_LINE="15 9 * * * cd $ROOT_DIR && $PY $SCRIPT >> $LOG 2>&1"

EXISTING="$(crontab -l 2>/dev/null || true)"
if echo "$EXISTING" | grep -F "$SCRIPT" >/dev/null 2>&1; then
  echo "Cron entry already exists for $SCRIPT"
  exit 0
fi

{
  echo "$EXISTING"
  echo "$CRON_LINE"
} | crontab -

echo "Installed cron job:"
echo "$CRON_LINE"
