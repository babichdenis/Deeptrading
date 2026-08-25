#!/bin/zsh
set -e
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "Создаю виртуальное окружение…"
  python3.11 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt
fi

echo "Запуск бэкенда: http://0.0.0.0:8000 (Ctrl+C для остановки)"
exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload --loop asyncio
