import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from database import get_db
from main import app
from platforms.base import ConnectionResult, RunStatusResult, StartAnalysisResult


def _client(db_session):
    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def client(db_session):
    yield from _client(db_session)


def test_create_connection_never_echoes_secret(client, customer):
    resp = client.post(
        "/api/connections",
        json={
            "platform": "databricks",
            "workspace": "Prod",
            "endpoint": "https://adb-x.azuredatabricks.net",
            "auth_method": "pat",
            "auth_metadata": {},
            "secret": "dapi-super-secret-token",
        },
        headers={"X-Customer-Id": customer.id},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "secret" not in body
    assert "secret_encrypted" not in body
    assert "dapi-super-secret-token" not in json.dumps(body)


def test_create_job_and_retrieve_it(client, customer, databricks_connection, monkeypatch):
    monkeypatch.setattr(
        "services.job_service.build_adapter",
        lambda conn, db=None, delegated_token=None: _FakeAdapter(start_status="STARTING", run_id="run-1"),
    )

    resp = client.post(
        "/api/jobs",
        json={"connection_id": databricks_connection.id, "prompt": "Analyze my cluster"},
        headers={"X-Customer-Id": customer.id},
    )
    assert resp.status_code == 200
    job = resp.json()
    assert job["intent"] == "cluster"
    assert len(job["job_runs"]) == 1
    assert job["job_runs"][0]["domain"] == "cluster"
    assert job["job_runs"][0]["platform_run_id"] == "run-1"

    get_resp = client.get(f"/api/jobs/{job['id']}", headers={"X-Customer-Id": customer.id})
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == job["id"]


def test_create_job_all_domains_creates_three_runs(client, customer, databricks_connection, monkeypatch):
    monkeypatch.setattr(
        "services.job_service.build_adapter",
        lambda conn, db=None, delegated_token=None: _FakeAdapter(start_status="STARTING", run_id="run-1"),
    )
    resp = client.post(
        "/api/jobs",
        json={"connection_id": databricks_connection.id, "prompt": "Analyze everything"},
        headers={"X-Customer-Id": customer.id},
    )
    job = resp.json()
    assert job["intent"] == "all"
    assert {r["domain"] for r in job["job_runs"]} == {"cluster", "query", "storage"}


def test_get_job_404_for_unknown_id(client, customer):
    resp = client.get("/api/jobs/does-not-exist", headers={"X-Customer-Id": customer.id})
    assert resp.status_code == 404


def test_job_logs_endpoint_returns_persisted_logs(client, customer, databricks_connection, monkeypatch):
    monkeypatch.setattr(
        "services.job_service.build_adapter",
        lambda conn, db=None, delegated_token=None: _FakeAdapter(start_status="STARTING", run_id="run-1"),
    )
    create_resp = client.post(
        "/api/jobs",
        json={"connection_id": databricks_connection.id, "prompt": "Analyze my cluster"},
        headers={"X-Customer-Id": customer.id},
    )
    job_id = create_resp.json()["id"]

    logs_resp = client.get(f"/api/jobs/{job_id}/logs", headers={"X-Customer-Id": customer.id})
    assert logs_resp.status_code == 200
    messages = [l["message"] for l in logs_resp.json()]
    assert any("Starting" in m for m in messages)


def test_retry_rejected_when_no_failed_runs(client, customer, databricks_connection, monkeypatch):
    monkeypatch.setattr(
        "services.job_service.build_adapter",
        lambda conn, db=None, delegated_token=None: _FakeAdapter(start_status="STARTING", run_id="run-1"),
    )
    create_resp = client.post(
        "/api/jobs",
        json={"connection_id": databricks_connection.id, "prompt": "Analyze my cluster"},
        headers={"X-Customer-Id": customer.id},
    )
    job_id = create_resp.json()["id"]

    retry_resp = client.post(f"/api/jobs/{job_id}/retry", headers={"X-Customer-Id": customer.id})
    assert retry_resp.status_code == 400


def test_cancel_job(client, customer, databricks_connection, monkeypatch):
    monkeypatch.setattr(
        "services.job_service.build_adapter",
        lambda conn, db=None, delegated_token=None: _FakeAdapter(start_status="RUNNING", run_id="run-1"),
    )
    create_resp = client.post(
        "/api/jobs",
        json={"connection_id": databricks_connection.id, "prompt": "Analyze my cluster"},
        headers={"X-Customer-Id": customer.id},
    )
    job_id = create_resp.json()["id"]

    cancel_resp = client.post(f"/api/jobs/{job_id}/cancel", headers={"X-Customer-Id": customer.id})
    assert cancel_resp.status_code == 200
    # Step 2 contract change: accepting a cancel is NOT the same as the platform
    # having cancelled the run. The platform acknowledged the request, so the run
    # is CANCEL_REQUESTED; it is only promoted to CANCELLED once a poll observes
    # the platform reporting it cancelled (see the test below). Previously this
    # asserted CANCELLED immediately, which reported a state never confirmed.
    assert cancel_resp.json()["job_runs"][0]["status"] == "CANCEL_REQUESTED"


def test_cancel_is_only_promoted_to_cancelled_after_platform_confirms(
    client, customer, databricks_connection, monkeypatch, db_session
):
    """A cancel request must not self-promote: only the platform reporting a
    terminal Cancelled state makes the run CANCELLED."""
    from models import JobRun

    adapter = _FakeAdapter(start_status="RUNNING", run_id="run-cancel-1")
    monkeypatch.setattr("services.job_service.build_adapter", lambda conn, db=None, delegated_token=None: adapter)

    create_resp = client.post(
        "/api/jobs",
        json={"connection_id": databricks_connection.id, "prompt": "Analyze my cluster"},
        headers={"X-Customer-Id": customer.id},
    )
    job_id = create_resp.json()["id"]

    client.post(f"/api/jobs/{job_id}/cancel", headers={"X-Customer-Id": customer.id})

    # Platform still reports RUNNING -> stays CANCEL_REQUESTED, not CANCELLED.
    polled = client.get(f"/api/jobs/{job_id}", headers={"X-Customer-Id": customer.id})
    assert polled.json()["job_runs"][0]["status"] == "CANCEL_REQUESTED"

    # Now the platform confirms cancellation.
    adapter._start_status = "CANCELLED"
    confirmed = client.get(f"/api/jobs/{job_id}", headers={"X-Customer-Id": customer.id})
    assert confirmed.json()["job_runs"][0]["status"] == "CANCELLED"


def test_create_job_with_fabric_connection_persists_real_platform_run_id(
    client, customer, fabric_connection, monkeypatch
):
    """
    Proves the Phase 1 job architecture is genuinely platform-agnostic: a Fabric
    connection flows through the exact same /api/jobs endpoints as Databricks,
    with no Fabric-specific branching in job_service or the API layer.
    """
    monkeypatch.setattr(
        "services.job_service.build_adapter",
        lambda conn, db=None, delegated_token=None: _FakeAdapter(start_status="STARTING", run_id="fabric-job-instance-abc123"),
    )

    resp = client.post(
        "/api/jobs",
        json={"connection_id": fabric_connection.id, "prompt": "Analyze my cluster"},
        headers={"X-Customer-Id": customer.id},
    )
    assert resp.status_code == 200
    job = resp.json()
    assert job["platform"] == "fabric"
    assert job["job_runs"][0]["platform_run_id"] == "fabric-job-instance-abc123"
    assert job["job_runs"][0]["status"] == "STARTING"


class _FakeAdapter:
    """Test double standing in for a real platform adapter."""

    def __init__(self, start_status: str, run_id: str):
        self._start_status = start_status
        self._run_id = run_id

    async def start_analysis(self, domain, parameters=None):
        return StartAnalysisResult(platform_run_id=self._run_id, status=self._start_status)

    async def get_run_status(self, platform_run_id, domain="cluster"):
        return RunStatusResult(status=self._start_status)

    async def get_run_logs(self, platform_run_id, domain="cluster"):
        return []

    async def cancel_run(self, platform_run_id, domain="cluster"):
        return ConnectionResult(ok=True, message="cancelled")

    async def retry_run(self, domain, platform_run_id, parameters=None):
        return StartAnalysisResult(platform_run_id=platform_run_id, status="STARTING")
