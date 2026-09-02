#!/bin/bash
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
cd /Users/Denis/Dev/Deeptrading/frontend
while true; do
  if ! lsof -ti :5173 >/dev/null 2>&1; then
    echo "$(date) Vite down, restarting" >> /tmp/vite_watchdog.log
    nohup npx vite --host 0.0.0.0 >> /tmp/vite.log 2>&1 &
  fi
  sleep 20
done
