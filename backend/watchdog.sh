#!/bin/bash
cd /Users/Denis/Dev/Deeptrading/backend
while true; do
  if ! lsof -ti :8000 >/dev/null 2>&1; then
    echo "$(date) Backend down, restarting" >> /tmp/backend_watchdog.log
    nohup .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 >> /tmp/backend.log 2>&1 &
  fi
  sleep 20
done
