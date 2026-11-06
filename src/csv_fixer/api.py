"""FastAPI control plane — upload CSV + contract, run agent, return audit."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from csv_fixer import __version__
from csv_fixer.agent import CsvFixAgent, record_sandbox_events
from csv_fixer.audit import GLOBAL_AUDIT, containment_summary
from csv_fixer.config import Settings, load_settings
from csv_fixer.models import FixAttempt, new_job_id
from csv_fixer.probes import HOSTILE_PROBE_SOURCE
from csv_fixer.sandbox import SandboxRunner, docker_available, prepare_work_dir
from csv_fixer.validators import contractgate_available

logger = logging.getLogger("csv_fixer")

_JOB_ID = re.compile(r"^[a-f0-9]{12}$")
_STATIC = Path(__file__).resolve().parent / "static" / "index.html"

DEMO_FIXTURES = {
    "messy_orders.csv": "text/csv",
    "poisoned_orders.csv": "text/csv",
    "clean_orders.csv": "text/csv",
    "orders_contract.yaml": "application/yaml",
}

# Completed job payloads, also written under ARTIFACTS_DIR so a restart can reload them.
JOBS: dict[str, dict] = {}


def _ensure_shared_dir(path: Path) -> None:
    if not str(path):
        return
    prepare_work_dir(path)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    _ensure_shared_dir(Path(settings.jobs_dir))
    _ensure_shared_dir(Path(settings.artifacts_dir))
    tmp = os.environ.get("TMPDIR", "")
    if tmp:
        _ensure_shared_dir(Path(tmp))
    yield


app = FastAPI(
    title="vultr-csv-fixer",
    description="Sandboxed CSV fixer (Vultr Agent Rush — Blast Radius Zero)",
    version=__version__,
    lifespan=lifespan,
)


def _require_job_id(job_id: str) -> str:
    if not _JOB_ID.fullmatch(job_id):
        raise HTTPException(status_code=404, detail="unknown job_id")
    return job_id


def _fixtures_dir() -> Path:
    return _default_contract_path().parent


def _default_contract_path() -> Path:
    settings = load_settings()
    candidates: list[Path] = []
    if settings.default_contract:
        candidates.append(Path(settings.default_contract))
    here = Path(__file__).resolve()
    candidates.extend(
        [
            here.parents[2] / "fixtures" / "orders_contract.yaml",
            Path("/app/fixtures/orders_contract.yaml"),
            Path.cwd() / "fixtures" / "orders_contract.yaml",
        ]
    )
    for path in candidates:
        if path.is_file():
            return path
    raise HTTPException(status_code=500, detail="default orders contract is not installed")


def _job_workdir(settings: Settings, job_id: str) -> Path:
    root = Path(settings.jobs_dir)
    prepare_work_dir(root)
    path = root / job_id
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    prepare_work_dir(path, ancestor=root)
    return path


def _store_job(settings: Settings, body: dict) -> None:
    job_id = body["job_id"]
    JOBS[job_id] = body
    art = Path(settings.artifacts_dir)
    art.mkdir(parents=True, exist_ok=True)
    (art / f"{job_id}.json").write_text(json.dumps(body), encoding="utf-8")


def _load_job(job_id: str) -> dict | None:
    cached = JOBS.get(job_id)
    if cached is not None:
        return cached
    path = Path(load_settings().artifacts_dir) / f"{job_id}.json"
    if not path.is_file():
        return None
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(body, dict):
        JOBS[job_id] = body
        return body
    return None


def _public_body(result_body: dict, *, download: str | None, extra: dict | None = None) -> dict:
    body = dict(result_body)
    # The work directory is removed after the response is built.
    body["fixed_csv_path"] = None
    body["download"] = download
    body["containment"] = containment_summary_from_dump(body.get("audit") or [])
    if extra:
        body.update(extra)
    return body


def containment_summary_from_dump(events: list[dict]) -> dict[str, object]:
    blocked_actions = [e.get("action") for e in events if e.get("blocked")]
    return {
        "network_blocked": any(e.get("action") == "block_network" for e in events),
        "env_blocked": any(e.get("action") == "block_env" for e in events),
        "timed_out": any(e.get("action") == "timeout" for e in events),
        "oom_blocked": any(e.get("action") == "block_oom" for e in events),
        "blocked_actions": blocked_actions,
    }


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    if not _STATIC.is_file():
        raise HTTPException(status_code=500, detail="UI is not installed")
    return HTMLResponse(
        _STATIC.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/health")
def health() -> dict:
    settings = load_settings()
    if settings.mock_llm:
        inference: dict[str, str] = {"mode": "mock", "reason": "MOCK_LLM=1"}
    elif not settings.vultr_inference_api_key:
        inference = {"mode": "mock", "reason": "VULTR_INFERENCE_API_KEY is empty"}
    else:
        inference = {
            "mode": "vultr",
            "model": settings.vultr_inference_model,
            "base_url": settings.vultr_inference_base_url,
        }
    if settings.force_mock_sandbox:
        sandbox = {"mode": "mock", "reason": "FORCE_MOCK_SANDBOX=1"}
    elif docker_available():
        sandbox = {"mode": "docker", "image": settings.sandbox_image}
    else:
        sandbox = {"mode": "unavailable", "reason": "docker daemon not reachable"}
    return {
        "ok": True,
        "version": __version__,
        "mock_llm": settings.mock_llm,
        "contractgate": contractgate_available(),
        "force_mock_sandbox": settings.force_mock_sandbox,
        "inference": inference,
        "sandbox": sandbox,
        "max_attempts": settings.max_agent_retries,
        "sandbox_timeout_sec": settings.sandbox_timeout_sec,
        "sandbox_memory": settings.sandbox_memory,
    }


@app.get("/fixtures")
def list_fixtures() -> dict:
    return {"fixtures": list(DEMO_FIXTURES)}


@app.get("/fixtures/{name}")
def get_fixture(name: str) -> FileResponse:
    media = DEMO_FIXTURES.get(name)
    if media is None or "/" in name or ".." in name:
        raise HTTPException(status_code=404, detail="unknown fixture")
    path = _fixtures_dir() / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="unknown fixture")
    return FileResponse(path, filename=name, media_type=media)


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> JSONResponse:
    _require_job_id(job_id)
    body = _load_job(job_id)
    if body is None:
        raise HTTPException(status_code=404, detail="unknown job_id")
    return JSONResponse(body)


@app.get("/audit/{job_id}")
def get_audit(job_id: str) -> JSONResponse:
    _require_job_id(job_id)
    events = GLOBAL_AUDIT.for_job(job_id)
    if events:
        return JSONResponse([e.model_dump(mode="json") for e in events])
    stored = _load_job(job_id)
    if stored and stored.get("audit"):
        return JSONResponse(stored["audit"])
    raise HTTPException(status_code=404, detail="unknown job_id")


@app.post("/fix")
async def fix_csv(
    csv_file: UploadFile = File(...),
    contract_file: UploadFile | None = File(default=None),
) -> JSONResponse:
    settings = load_settings()
    raw_csv = await csv_file.read()
    if not raw_csv:
        raise HTTPException(status_code=400, detail="csv_file is empty")
    if len(raw_csv) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="csv_file exceeds MAX_UPLOAD_BYTES")

    contract_source = "default"
    contract_name = "orders_contract.yaml"
    raw_contract: bytes | None = None
    if contract_file is not None:
        raw_contract = await contract_file.read()
        if raw_contract:
            if len(raw_contract) > settings.max_upload_bytes:
                raise HTTPException(
                    status_code=413, detail="contract_file exceeds MAX_UPLOAD_BYTES"
                )
            contract_source = "upload"
            contract_name = _safe_name(contract_file.filename, "contract.yaml")
        else:
            raw_contract = None

    job_id = new_job_id()
    work = _job_workdir(settings, job_id)
    try:
        csv_path = work / "input.csv"
        contract_path = work / "contract.yaml"
        csv_path.write_bytes(raw_csv)
        if raw_contract is None:
            contract_path.write_bytes(_default_contract_path().read_bytes())
        else:
            contract_path.write_bytes(raw_contract)

        agent = CsvFixAgent(settings=settings, audit=GLOBAL_AUDIT)
        try:
            result = agent.run(
                csv_path=csv_path,
                contract_path=contract_path,
                job_id=job_id,
                work_root=work / "runs",
            )
        except Exception:
            logger.exception("fix failed job_id=%s", job_id)
            raise HTTPException(status_code=500, detail="fixer failed") from None

        download = None
        if result.success and result.fixed_csv_path:
            art = Path(settings.artifacts_dir)
            art.mkdir(parents=True, exist_ok=True)
            dest = art / f"{job_id}-fixed.csv"
            shutil.copyfile(result.fixed_csv_path, dest)
            download = f"/download/{job_id}"

        body = _public_body(
            result.model_dump(mode="json"),
            download=download,
            extra={
                "input_filename": _safe_name(csv_file.filename, "upload.csv"),
                "contract_filename": contract_name,
                "contract_source": contract_source,
                "drill": False,
            },
        )
        _store_job(settings, body)
        return JSONResponse(body)
    finally:
        shutil.rmtree(work, ignore_errors=True)


@app.post("/drill")
def containment_drill() -> JSONResponse:
    """Run the hostile probe in the sandbox. This is not a CSV repair."""
    settings = load_settings()
    job_id = new_job_id()
    work = _job_workdir(settings, job_id)
    try:
        csv_path = work / "input.csv"
        csv_path.write_text(
            "order_id,sku,qty,amount,status\nORD-1,SKU-A,1,1.00,pending\n",
            encoding="utf-8",
        )
        GLOBAL_AUDIT.record(
            job_id=job_id,
            stage="drill",
            action="start",
            detail="hostile probe; not a repair",
        )
        GLOBAL_AUDIT.record(
            job_id=job_id,
            stage="sandbox",
            action="containment_policy",
            detail=(
                "attempt=1; network=none env=empty "
                f"memory={settings.sandbox_memory} cpus={settings.sandbox_cpus} "
                f"user=65534 cap-drop=ALL timeout={settings.sandbox_timeout_sec}s rm=true"
            ),
        )
        sand = SandboxRunner(settings).run_script(
            code=HOSTILE_PROBE_SOURCE,
            input_csv=csv_path,
            job_id=f"{job_id}-1",
            work_root=work / "probe",
        )
        record_sandbox_events(GLOBAL_AUDIT, job_id, sand, attempt=1, settings=settings)
        contained = sand.network_attempt_blocked or sand.env_secret_attempt_blocked or sand.timed_out
        GLOBAL_AUDIT.record(
            job_id=job_id,
            stage="complete",
            action="contained" if contained else "failed",
            detail="hostile probe finished",
        )
        attempt = FixAttempt(attempt=1, code=HOSTILE_PROBE_SOURCE, sandbox=sand, validation=None)
        audit = GLOBAL_AUDIT.for_job(job_id)
        payload = {
            "job_id": job_id,
            "success": False,
            "attempts": [attempt.model_dump(mode="json")],
            "audit": [e.model_dump(mode="json") for e in audit],
            "fixed_csv_path": None,
            "message": (
                "hostile probe was contained"
                if contained
                else "hostile probe was not contained"
            ),
        }
        body = _public_body(
            payload,
            download=None,
            extra={
                "input_filename": "containment-drill",
                "contract_filename": None,
                "contract_source": None,
                "drill": True,
            },
        )
        # containment_summary() agrees with the dump helper for live events.
        body["containment"] = containment_summary(audit)
        _store_job(settings, body)
        return JSONResponse(body)
    finally:
        shutil.rmtree(work, ignore_errors=True)


@app.get("/download/{job_id}")
def download(job_id: str) -> FileResponse:
    _require_job_id(job_id)
    settings = load_settings()
    path = Path(settings.artifacts_dir) / f"{job_id}-fixed.csv"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(path, filename=f"{job_id}-fixed.csv", media_type="text/csv")


def _safe_name(name: str | None, fallback: str) -> str:
    raw = (name or "").replace("\\", "/").split("/")[-1].strip()
    if not raw or raw in {".", ".."}:
        return fallback
    return raw[:180]


def create_app() -> FastAPI:
    return app
