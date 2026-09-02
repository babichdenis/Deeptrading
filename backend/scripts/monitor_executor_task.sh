#!/bin/bash
TASK=/Users/Denis/Dev/Deeptrading/backend/orchestrator/executor_task.md
STATE=/tmp/last_task_ref.txt
LOG=/tmp/monitor_executor.log
ALERT=/Users/Denis/Dev/Deeptrading/backend/reports/_new_task_alert.txt
cd /Users/Denis/Dev/Deeptrading/backend
while true; do
  if [ -f "$TASK" ]; then
    ref=$(grep -m1 "executor_task_" "$TASK" | tr -d "[:space:]")
    last=$(cat "$STATE" 2>/dev/null)
    if [ "$ref" != "$last" ] && [ -n "$ref" ]; then
      ts=$(date "+%Y-%m-%d %H:%M:%S")
      echo "$ts NEW TASK REF: $ref" >> "$LOG"
      echo "$ts $ref" > "$ALERT"
      echo "$ref" > "$STATE"
      python3 -m orchestrator notify "NOVOE ZADANIE EXECUTOR: $ref" >> "$LOG" 2>&1 || true
    fi
  fi
  sleep 300
done
