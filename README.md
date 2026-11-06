# vultr-csv-fixer

NightMoose — Vultr Agent Rush, Challenge 1: Blast Radius Zero.

Upload a messy CSV. An agent writes a repair script, runs that script in a throwaway Docker sandbox, and returns the file only after [DataContractGate](https://pypi.org/project/contractgate/) validators pass. A poisoned file that tries to make the agent open a network connection or read the environment is blocked, recorded in the audit log, and retried.

MIT License. See [LICENSE](LICENSE).

## What it does

1. Accept a CSV and an optional contract YAML. If you omit the contract, the bundled orders contract is used.
2. Ask the model for a short pandas repair script.
3. Run that script in a new container that has no network, an empty environment, a 512 MB memory cap, dropped capabilities, and a non-root user. The container is removed when the attempt finishes.
4. Validate every output row against the contract. On failure, send the errors back to the model and retry, up to three attempts.
5. Offer the fixed CSV for download, plus the attempt history and the containment audit.

The web UI is the demo surface: each attempt, the validation report, the download, and the audit log. Blocked network, blocked environment, and timeout rows are marked in red.

## Architecture

```
Browser (web UI) or CLI
    │  CSV + optional contract YAML
    ▼
Control plane (FastAPI on a Vultr Cloud Compute VM)
    │  1. Model proposes a repair script
    │     Vultr Serverless Inference, OpenAI-compatible
    │     https://api.vultrinference.com/v1
    │  2. Script runs in a throwaway container
    │  3. DataContractGate validates output rows
    │  4. Failures retry (max 3) with the error text
    ▼
Fixed CSV + validation report + audit log
```

The control plane is the only long-running process. Each repair attempt is a separate `docker run --rm` that the API starts through the host Docker socket. The socket is not mounted into the sandbox. The inference API key is not passed into the sandbox.

## Vultr services

| Service | Role |
|---|---|
| Vultr Cloud Compute (Ubuntu VM with Docker) | Runs the FastAPI control plane and the Docker engine |
| Vultr Serverless Inference | All model calls (`VULTR_INFERENCE_BASE_URL`, default `https://api.vultrinference.com/v1`) |

No GPU is attached to the VM. Job files live on the VM disk under `/var/lib/csv-fixer` and can be deleted with the job.

## Containment

Every sandbox run is:

- `--network none` — no egress
- `--env-file /dev/null` — no host environment and no inference key
- `--memory 512m` and `--memory-swap 512m`, `--cpus 1`
- `--read-only` root filesystem and a small noexec `/tmp`
- `--cap-drop ALL`, `no-new-privileges`, user `65534:65534`
- `--rm` — container destroyed after the run
- bind-mount of that attempt's work directory only, at `/work`
- wall clock from `SANDBOX_TIMEOUT_SEC` (default 30). On expiry the runner kills the container and writes a timeout audit event

If Docker is not reachable, the control plane refuses to run the script on the host. The offline mock (`FORCE_MOCK_SANDBOX=1`) is the exception used by tests: it detects network and environment calls statically, reports them as blocked, and does not open them.

**Containment moment.** `fixtures/poisoned_orders.csv` contains a cell that tells the model to `curl` an outside host and print `os.environ`. With `MOCK_LLM=1`, the first attempt is that hostile script. The sandbox blocks it. The audit log records `block_network` and `block_env`. The next attempt is a normal repair and, when it passes the contract, the fixed CSV is returned. The same block is what the **Run containment drill** button shows: it executes that hostile script in the sandbox and is not a repair. Use the drill when a live model refuses the injected instructions and the poisoned upload therefore has nothing to block.

## Local mock run

No Docker daemon and no inference key.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .

export MOCK_LLM=1 FORCE_MOCK_SANDBOX=1
pytest -q

python -m csv_fixer.cli fixtures/messy_orders.csv fixtures/orders_contract.yaml
python -m csv_fixer.cli fixtures/poisoned_orders.csv fixtures/orders_contract.yaml

uvicorn csv_fixer.api:app --host 127.0.0.1 --port 8080
```

Open `http://127.0.0.1:8080`. The health pills should read inference mock and sandbox mock. **Messy orders** repairs the file. **Poisoned orders** shows the blocked attempt, then a download. **Run containment drill** shows the same block without a download.

`FORCE_MOCK_SANDBOX=1` is not a security boundary. It exists so tests and laptops can run without a daemon.

## Live run on a Vultr Cloud Compute VM

The VM needs Docker Engine and the Docker Compose plugin. From the clone:

```bash
cp .env.example .env
# Set VULTR_INFERENCE_API_KEY.
# Leave MOCK_LLM=0 and FORCE_MOCK_SANDBOX=0.
bash scripts/vm-up.sh
```

Or the same steps by hand:

```bash
sudo mkdir -p /var/lib/csv-fixer/jobs /var/lib/csv-fixer/artifacts
docker compose up -d --build
curl -fsS http://127.0.0.1:8080/health
```

Open `http://<vm-public-ip>:8080`. Allow TCP 8080 in the Vultr firewall group for the instance. The API listens on `0.0.0.0` inside the container and Compose publishes `${PORT:-8080}`.

Health should show inference mode `vultr` and sandbox mode `docker`. Upload `fixtures/messy_orders.csv` (or click **Messy orders**) and download the fixed file. For the blocked-attempt shot, either click **Poisoned orders** while `MOCK_LLM=1`, or with live inference click **Run containment drill** if the model does not emit network or environment access.

To film the deterministic poisoned-CSV block on the VM without calling inference, set `MOCK_LLM=1` in `.env` and `docker compose up -d --build` again. Sandbox flags stay the same. Set `MOCK_LLM=0` to send repair prompts to Vultr Serverless Inference.

The API process runs as root in its container so it can use the Docker socket. That socket is host-root equivalent for the control plane. Sandbox containers do not get the socket, a network, or the environment.

### Environment variables

Do not commit `.env`. `.env.example` lists every variable with empty secrets.

| Variable | Purpose |
|---|---|
| `VULTR_INFERENCE_API_KEY` | Serverless Inference bearer token. Empty keeps the mock model even if `MOCK_LLM=0`. |
| `VULTR_INFERENCE_BASE_URL` | Default `https://api.vultrinference.com/v1` |
| `VULTR_INFERENCE_MODEL` | Default `gpt-oss-120b`. Use a model id enabled on the inference account. |
| `MOCK_LLM` | `1` deterministic offline model. `0` call Vultr when the key is set. |
| `FORCE_MOCK_SANDBOX` | `1` skip Docker (tests, laptops). `0` require Docker and refuse host execution if it is down. |
| `SANDBOX_IMAGE` | Image tag for the throwaway worker. Compose builds `csv-fixer-sandbox:local`. |
| `SANDBOX_TIMEOUT_SEC` | Wall clock before the runner kills the container. Default `30`. |
| `SANDBOX_MEMORY` | Default `512m`. |
| `SANDBOX_CPUS` | Default `1`. |
| `MAX_AGENT_RETRIES` | Default `3`. |
| `MAX_UPLOAD_BYTES` | Default `2000000`. |
| `PORT` | Host port published by Compose. Default `8080`. |
| `HOST_DATA_DIR` | Absolute host path bind-mounted at `/var/lib/csv-fixer`. Default `/var/lib/csv-fixer`. |
| `JOBS_DIR` | In-container job directory. Compose sets `/var/lib/csv-fixer/jobs`. |
| `HOST_JOBS_DIR` | Host path of `JOBS_DIR`, so `docker run -v` mounts the right directory. Compose sets this. |
| `ARTIFACTS_DIR` | Where fixed CSVs and job JSON are stored. |
| `DEFAULT_CONTRACT` | Contract used when the upload omits one. |

`JOBS_DIR` and `HOST_JOBS_DIR` differ when the API runs inside Compose: the process writes `/var/lib/csv-fixer/jobs/...` and the host daemon mounts `${HOST_DATA_DIR}/jobs/...`. Leave `HOST_JOBS_DIR` empty when you run `uvicorn` directly on the host.

## HTTP API

| Method | Path | Purpose |
|---|---|---|
| `GET /` | Web UI |
| `GET /health` | Inference mode, sandbox mode, contract backend. Does not return the API key. |
| `POST /fix` | Multipart `csv_file` (required) and `contract_file` (optional). |
| `POST /drill` | Run the hostile probe in the sandbox. Not a repair. |
| `GET /jobs/{job_id}` | Stored result |
| `GET /audit/{job_id}` | Audit events for one job |
| `GET /download/{job_id}` | Fixed CSV |
| `GET /fixtures` | Demo fixture names |
| `GET /fixtures/{name}` | One of the bundled demo files |

`POST /fix` returns attempts (script, sandbox stdout/stderr, docker argv, validation), `audit`, `containment` (`network_blocked`, `env_blocked`, `timed_out`), and `download` when the contract passed.

## Tests

Offline. No network and no Docker daemon.

```bash
export MOCK_LLM=1 FORCE_MOCK_SANDBOX=1
pytest -q
```

## Repo layout

```
src/csv_fixer/          API, web UI, CLI, agent loop, inference client, sandbox, validators, audit
src/csv_fixer/static/   Single-page UI
sandbox_worker/         Image for the throwaway pandas worker
fixtures/               messy, poisoned, and clean CSVs, plus the orders contract
tests/                  Offline pytest
docker-compose.yml      API + sandbox image build for the VM
```

## License

MIT. Copyright (c) 2026 nightmoose.
