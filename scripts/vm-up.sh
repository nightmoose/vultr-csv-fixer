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

DATA="${HOST_DATA_DIR:-/var/lib/csvfix-work}"
if ! mkdir -p "$DATA/jobs" "$DATA/artifacts" "$DATA/tmp" 2>/dev/null; then
  echo "Could not create $DATA. Re-run with sudo, or set HOST_DATA_DIR to a directory you own."
  exit 1
fi
# Sandbox user 65534 must be able to create output.csv in these directories.
chmod 777 "$DATA" "$DATA/jobs" "$DATA/artifacts" "$DATA/tmp" 2>/dev/null || true

docker compose up -d --build
docker compose ps
port="${PORT:-8080}"
echo "Open http://127.0.0.1:${port}  (use the VM public IP from another machine)"
