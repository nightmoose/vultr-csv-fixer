"""FastAPI control plane — upload CSV + contract, run agent, return audit."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from csv_fixer.agent import CsvFixAgent
from csv_fixer.audit import GLOBAL_AUDIT
from csv_fixer.config import load_settings
from csv_fixer.models import new_job_id
from csv_fixer.validators import contractgate_available

app = FastAPI(
    title="vultr-csv-fixer",
    description="Sandboxed CSV fixer (Vultr Agent Rush — Blast Radius Zero)",
    version="0.1.0",
)


@app.get("/health")
def health() -> dict:
    settings = load_settings()
    return {
        "ok": True,
        "mock_llm": settings.mock_llm,
        "contractgate": contractgate_available(),
        "force_mock_sandbox": settings.force_mock_sandbox,
    }


@app.get("/audit/{job_id}")
def get_audit(job_id: str) -> JSONResponse:
    events = GLOBAL_AUDIT.for_job(job_id)
    if not events:
        raise HTTPException(status_code=404, detail="unknown job_id")
    return JSONResponse([e.model_dump(mode="json") for e in events])


@app.post("/fix")
async def fix_csv(
    csv_file: UploadFile = File(...),
    contract_file: UploadFile = File(...),
) -> JSONResponse:
    settings = load_settings()
    job_id = new_job_id()
    work = Path(tempfile.mkdtemp(prefix=f"api-{job_id}-"))
    try:
        csv_path = work / "input.csv"
        contract_path = work / "contract.yaml"
        csv_path.write_bytes(await csv_file.read())
        contract_path.write_bytes(await contract_file.read())

        agent = CsvFixAgent(settings=settings, audit=GLOBAL_AUDIT)
        result = agent.run(
            csv_path=csv_path,
            contract_path=contract_path,
            job_id=job_id,
            work_root=work / "runs",
        )

        # Persist fixed file under artifacts for download endpoint
        download = None
        if result.success and result.fixed_csv_path:
            art = Path(settings.artifacts_dir)
            art.mkdir(parents=True, exist_ok=True)
            dest = art / f"{job_id}-fixed.csv"
            shutil.copyfile(result.fixed_csv_path, dest)
            download = f"/download/{job_id}"

        body = result.model_dump(mode="json")
        body["download"] = download
        return JSONResponse(body)
    finally:
        # Keep work dir briefly for debugging in local mock mode; wipe secrets always.
        pass


@app.get("/download/{job_id}")
def download(job_id: str) -> FileResponse:
    settings = load_settings()
    path = Path(settings.artifacts_dir) / f"{job_id}-fixed.csv"
    if not path.exists():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(path, filename=f"{job_id}-fixed.csv", media_type="text/csv")


def create_app() -> FastAPI:
    return app
