#!/usr/bin/env bash
# Offline containment demo (MOCK_LLM=1, FORCE_MOCK_SANDBOX=1).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export MOCK_LLM=1 FORCE_MOCK_SANDBOX=1
PYTHON="${ROOT}/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then PYTHON=python3; fi
export PYTHONPATH="${ROOT}/src:${PYTHONPATH:-}"
echo "== messy CSV repair =="
"$PYTHON" -m csv_fixer.cli fixtures/messy_orders.csv fixtures/orders_contract.yaml
echo
echo "== poisoned CSV containment =="
"$PYTHON" -m csv_fixer.cli fixtures/poisoned_orders.csv fixtures/orders_contract.yaml
echo
echo "Web UI (same flags): uvicorn csv_fixer.api:app --host 127.0.0.1 --port 8080"
