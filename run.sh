#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
exec docker compose up --detach --build --wait --wait-timeout 600
