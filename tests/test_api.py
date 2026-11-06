"""API and web UI behavior. Offline: MOCK_LLM and FORCE_MOCK_SANDBOX."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from csv_fixer.api import app

FIX = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.setenv("FORCE_MOCK_SANDBOX", "1")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("HOST_JOBS_DIR", "")
    monkeypatch.delenv("VULTR_INFERENCE_API_KEY", raising=False)
    with TestClient(app) as test_client:
        yield test_client


def _files(csv_name: str, contract: bool = True):
    payload = {
        "csv_file": (csv_name, (FIX / csv_name).read_bytes(), "text/csv"),
    }
    if contract:
        payload["contract_file"] = (
            "orders_contract.yaml",
            (FIX / "orders_contract.yaml").read_bytes(),
            "application/yaml",
        )
    return payload


def test_health_hides_api_key(client, monkeypatch):
    monkeypatch.setenv("VULTR_INFERENCE_API_KEY", "secret-token-value")
    monkeypatch.setenv("MOCK_LLM", "0")
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert "secret-token-value" not in response.text
    assert body["inference"]["mode"] == "vultr"
    assert body["inference"]["model"]
    assert "api_key" not in body


def test_index_is_the_demo_ui(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    text = response.text
    assert "Audit log" in text
    assert "Blocked network" in text
    assert "Run containment drill" in text
    assert 'id="fix-form"' in text


def test_fix_messy_returns_download(client):
    response = client.post("/fix", files=_files("messy_orders.csv"))
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["download"]
    assert body["contract_source"] == "upload"
    assert body["containment"]["network_blocked"] is False
    assert any(event["action"] == "containment_policy" for event in body["audit"])
    assert body["attempts"]
    args = body["attempts"][-1]["sandbox"]["docker_args"]
    assert args[args.index("--network") + 1] == "none"
    downloaded = client.get(body["download"])
    assert downloaded.status_code == 200
    assert "text/csv" in downloaded.headers["content-type"]
    assert b"shiped" not in downloaded.content
    stored = client.get(f"/jobs/{body['job_id']}")
    assert stored.status_code == 200
    assert stored.json()["success"] is True
    audit = client.get(f"/audit/{body['job_id']}")
    assert audit.status_code == 200
    assert any(event["action"] == "success" for event in audit.json())


def test_fix_poisoned_records_containment(client):
    response = client.post("/fix", files=_files("poisoned_orders.csv"))
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    containment = body["containment"]
    assert containment["network_blocked"] is True
    assert containment["env_blocked"] is True
    assert "block_network" in containment["blocked_actions"]
    assert "block_env" in containment["blocked_actions"]
    blocked_attempt = body["attempts"][0]
    assert blocked_attempt["sandbox"]["network_attempt_blocked"] is True
    assert blocked_attempt["sandbox"]["env_secret_attempt_blocked"] is True
    assert blocked_attempt["validation"] is None
    downloaded = client.get(body["download"])
    assert downloaded.status_code == 200
    assert b"ORD-2001" in downloaded.content


def test_fix_without_contract_uses_default(client):
    response = client.post("/fix", files=_files("messy_orders.csv", contract=False))
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["contract_source"] == "default"


def test_empty_and_oversized_csv(client, monkeypatch):
    empty = client.post("/fix", files={"csv_file": ("empty.csv", b"", "text/csv")})
    assert empty.status_code == 400
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "32")
    big = client.post(
        "/fix",
        files={"csv_file": ("big.csv", b"x" * 64, "text/csv")},
    )
    assert big.status_code == 413


def test_unknown_job_and_download(client):
    missing = "a" * 12
    assert client.get(f"/jobs/{missing}").status_code == 404
    assert client.get(f"/audit/{missing}").status_code == 404
    assert client.get(f"/download/{missing}").status_code == 404
    assert client.get("/download/not-valid").status_code == 404
    assert client.get("/jobs/../../etc/passwd").status_code == 404


def test_fixtures_are_whitelisted(client):
    listed = client.get("/fixtures")
    assert listed.status_code == 200
    assert "poisoned_orders.csv" in listed.json()["fixtures"]
    got = client.get("/fixtures/poisoned_orders.csv")
    assert got.status_code == 200
    assert b"evil.example" in got.content
    assert client.get("/fixtures/not-a-file.csv").status_code == 404
    assert client.get("/fixtures/orders_contract.yaml").status_code == 200


def test_containment_drill(client):
    response = client.post("/drill")
    assert response.status_code == 200
    body = response.json()
    assert body["drill"] is True
    assert body["success"] is False
    assert body["download"] is None
    assert body["containment"]["network_blocked"] is True
    assert body["containment"]["env_blocked"] is True
    assert body["attempts"][0]["sandbox"]["ok"] is False
    detail = " ".join(event["detail"] for event in body["audit"])
    assert "--network none" in detail or "network=none" in detail
