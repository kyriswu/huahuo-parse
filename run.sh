#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p logs
if [[ -f logs/service.pid ]] && kill -0 "$(cat logs/service.pid)" 2>/dev/null; then
  echo "service already running with PID $(cat logs/service.pid)" >&2
  exit 1
fi
setsid nohup .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" >> logs/service.log 2>&1 < /dev/null &
echo $! > logs/service.pid
echo "started PID $(cat logs/service.pid), log: logs/service.log"
