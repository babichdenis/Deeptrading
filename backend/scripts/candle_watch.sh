#!/bin/sh
# Watchdog целостности свечей (P2): rolling repair за последние 3 дня.
# Запускается launchd com.denis.candle-watch каждые 15 минут (см. DECISIONS 2026-10-01 20:30).
cd "$(dirname "$0")/.." || exit 1
VENV=.venv/bin/python
FROM=$(date -v-3d +%F)
TO=$(date -v-1d +%F)
mkdir -p reports/candle_integrity
"$VENV" scripts/candle_integrity_check.py repair --eligible --from "$FROM" --to "$TO" \
    >> "reports/candle_integrity/watch_$(date +%F).log" 2>&1
