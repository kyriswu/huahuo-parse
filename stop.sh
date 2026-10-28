#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -f logs/service.pid ]]; then
  echo "service is not running"
  exit 0
fi
pid="$(cat logs/service.pid)"
if kill -0 "$pid" 2>/dev/null; then
  kill "$pid"
  for _ in {1..50}; do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.1
  done
fi
rm -f logs/service.pid
echo "stopped"
