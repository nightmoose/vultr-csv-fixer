#!/usr/bin/env bash
# Start the control plane on a Docker host (Vultr Cloud Compute or a laptop).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Wrote .env from .env.example."
  echo "Set VULTR_INFERENCE_API_KEY and keep MOCK_LLM=0 for live inference."
fi

DATA="${HOST_DATA_DIR:-/var/lib/csv-fixer}"
if [[ ! -d "$DATA/jobs" || ! -d "$DATA/artifacts" ]]; then
  if ! mkdir -p "$DATA/jobs" "$DATA/artifacts" 2>/dev/null; then
    echo "Could not create $DATA. Re-run with sudo, or set HOST_DATA_DIR to a directory you own."
    exit 1
  fi
fi

docker compose up -d --build
docker compose ps
port="${PORT:-8080}"
echo "Open http://127.0.0.1:${port}  (use the VM public IP from another machine)"
