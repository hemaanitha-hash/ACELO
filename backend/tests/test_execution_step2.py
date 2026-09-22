"""
Step 2 execution tests.

HTTP is mocked at the boundary with respx so the adapter's real URL building,
status handling, retry classification and error mapping are exercised. These are
deterministic unit tests — they are NOT live Fabric executions and must never be
reported as such.
"""

import json
import sys
import types
import uuid
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from models import AnalysisJob, Connection, Customer, JobRun, JobStatus
from platforms.base import PlatformCapabilityNotImplemented
from platforms.errors import ErrorCode, PlatformError, PlatformError, code_for_status, is_transient
from platforms.fabric import FabricAdapter, extract_job_instance_id
from services import job_service

FABRIC_BASE = "https://api.fabric.microsoft.com/v1"
WORKSPACE_ID = "ws-1111-2222"
CLUSTER_ITEM = "nb-cluster-0001"


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from main import app

    return TestClient(app)


def _adapter(monkeypatch, extra=None, secret="test-secret"):
    metadata = {
        "tenant_id": "t",
        "client_id": "test-client",
        "workspace_id": WORKSPACE_ID,
        "notebook_item_id": CLUSTER_ITEM,
        "sql_endpoint": "test.datawarehouse.fabric.microsoft.com",
        "lakehouse_database": "data_Demo",
    }
    if extra:
        metadata.update(extra)
    adapter = FabricAdapter(endpoint="https://x", auth_metadata=metadata, secret=secret)
    monkeypatch.setattr(adapter, "_acquire_token", lambda: ("fake-token", None))
    return adapter


def _run_url(item=CLUSTER_ITEM):
    return f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items/{item}/jobs/instances"


def _instance_url(run_id, item=CLUSTER_ITEM):
    return f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items/{item}/jobs/instances/{run_id}"


# --------------------------------------------------------------------------
# 1. Real job ID extraction
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "location,expected",
    [
        ("https://api.fabric.microsoft.com/v1/workspaces/w/items/i/jobs/instances/abc-123", "abc-123"),
        ("https://x/jobs/execute/instances/def-456", "def-456"),
        # A query string must not be folded into the ID — the old split("/")[-1]
        # implementation produced "ghi-789?api-version=1" and broke polling.
        ("https://x/jobs/instances/ghi-789?api-version=1.0", "ghi-789"),
        ("https://x/jobs/instances/jkl-000/", "jkl-000"),
        (None, None),
        ("", None),
        ("https://x/no-instance-segment", None),
    ],
)
def test_job_instance_id_extraction(location, expected):
    assert extract_job_instance_id(location) == expected


@pytest.mark.asyncio
@respx.mock
async def test_start_analysis_captures_real_platform_run_id_with_query_string(monkeypatch):
    adapter = _adapter(monkeypatch)
    respx.post(_run_url()).mock(
        return_value=httpx.Response(
            202, headers={"Location": _instance_url("real-guid-42") + "?api-version=1.0"}
        )
    )
    result = await adapter.start_analysis("cluster")
    assert result.platform_run_id == "real-guid-42"
    assert result.status == "STARTING"


@pytest.mark.asyncio
@respx.mock
async def test_start_analysis_without_location_header_raises_not_fake_id(monkeypatch):
    """Fabric accepting the job but returning no trackable ID must fail loudly."""
    adapter = _adapter(monkeypatch)
    respx.post(_run_url()).mock(return_value=httpx.Response(202))
    with pytest.raises(RuntimeError, match="job instance ID"):
        await adapter.start_analysis("cluster")


# --------------------------------------------------------------------------
# 2/3. Failed and successful execution
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_execution_failure_surfaces_real_failure_reason(monkeypatch):
    adapter = _adapter(monkeypatch)
    respx.get(_instance_url("r1")).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "r1",
                "status": "Failed",
                "failureReason": {"errorCode": "NotebookExecutionFailed", "message": "cell 4 raised"},
            },
        )
    )
    status = await adapter.get_run_status("r1")
    assert status.status == "FAILED"
    assert "NotebookExecutionFailed" in status.error


@pytest.mark.asyncio
@respx.mock
async def test_successful_execution_reports_completed(monkeypatch):
    adapter = _adapter(monkeypatch)
    respx.get(_instance_url("r1")).mock(
        return_value=httpx.Response(200, json={"id": "r1", "status": "Completed", "exitValue": "success"})
    )
    status = await adapter.get_run_status("r1")
    assert status.status == "COMPLETED"


# --------------------------------------------------------------------------
# 4. Polling through the job service
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_polling_persists_real_status_transitions(db_session, customer, monkeypatch):
    connection, job_run = _persisted_run(db_session, customer)
    adapter = _adapter(monkeypatch)
    monkeypatch.setattr("services.job_service.build_adapter", lambda c, db=None, delegated_token=None: adapter)

    route = respx.get(_instance_url("real-run-1"))
    route.mock(return_value=httpx.Response(200, json={"id": "real-run-1", "status": "InProgress"}))
    await job_service.sync_run_status(db_session, connection, job_run)
    assert job_run.status == "RUNNING"

    route.mock(return_value=httpx.Response(200, json={"id": "real-run-1", "status": "Completed"}))
    await job_service.sync_run_status(db_session, connection, job_run)
    assert job_run.status == "COMPLETED"
    assert job_run.completed_at is not None


# --------------------------------------------------------------------------
# 5. Timeout
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_timeout_is_typed_and_transient(monkeypatch):
    adapter = _adapter(monkeypatch)
    respx.get(_instance_url("r1")).mock(side_effect=httpx.TimeoutException("too slow"))
    with pytest.raises(PlatformError) as exc:
        await adapter.get_run_status("r1")
    assert exc.value.code == ErrorCode.TIMEOUT
    assert is_transient(exc.value.code)


@pytest.mark.asyncio
@respx.mock
async def test_transient_poll_failure_leaves_run_running_for_retry(db_session, customer, monkeypatch):
    """A transient error must NOT fail a live run — the next poll retries it."""
    connection, job_run = _persisted_run(db_session, customer, status="RUNNING")
    adapter = _adapter(monkeypatch)
    monkeypatch.setattr("services.job_service.build_adapter", lambda c, db=None, delegated_token=None: adapter)

    respx.get(_instance_url("real-run-1")).mock(return_value=httpx.Response(503))
    await job_service.sync_run_status(db_session, connection, job_run)

    assert job_run.status == "RUNNING"  # untouched
    assert job_run.completed_at is None


# --------------------------------------------------------------------------
# 6. Cancellation
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_cancel_marks_requested_not_cancelled(db_session, customer, monkeypatch):
    connection, job_run = _persisted_run(db_session, customer, status="RUNNING")
    adapter = _adapter(monkeypatch)
    monkeypatch.setattr("services.job_service.build_adapter", lambda c, db=None, delegated_token=None: adapter)

    respx.post(_instance_url("real-run-1") + "/cancel").mock(return_value=httpx.Response(202))
    await job_service.cancel_job_run(db_session, connection, job_run)

    assert job_run.status == JobStatus.CANCEL_REQUESTED.value
    assert job_run.status != JobStatus.CANCELLED.value


@pytest.mark.asyncio
@respx.mock
async def test_cancel_refused_by_platform_does_not_change_status(db_session, customer, monkeypatch):
    connection, job_run = _persisted_run(db_session, customer, status="RUNNING")
    adapter = _adapter(monkeypatch)
    monkeypatch.setattr("services.job_service.build_adapter", lambda c, db=None, delegated_token=None: adapter)

    respx.post(_instance_url("real-run-1") + "/cancel").mock(
        return_value=httpx.Response(400, json={"errorCode": "JobAlreadyCompleted"})
    )
    await job_service.cancel_job_run(db_session, connection, job_run)

    assert job_run.status == "RUNNING"
    assert job_run.error_code == ErrorCode.UNSUPPORTED


@pytest.mark.asyncio
@respx.mock
async def test_cancel_requested_promoted_only_by_platform_confirmation(db_session, customer, monkeypatch):
    connection, job_run = _persisted_run(db_session, customer, status=JobStatus.CANCEL_REQUESTED.value)
    adapter = _adapter(monkeypatch)
    monkeypatch.setattr("services.job_service.build_adapter", lambda c, db=None, delegated_token=None: adapter)

    route = respx.get(_instance_url("real-run-1"))
    route.mock(return_value=httpx.Response(200, json={"id": "real-run-1", "status": "InProgress"}))
    await job_service.sync_run_status(db_session, connection, job_run)
    assert job_run.status == JobStatus.CANCEL_REQUESTED.value  # still not cancelled

    route.mock(return_value=httpx.Response(200, json={"id": "real-run-1", "status": "Cancelled"}))
    await job_service.sync_run_status(db_session, connection, job_run)
    assert job_run.status == JobStatus.CANCELLED.value


# --------------------------------------------------------------------------
# 7. Result retrieval failure
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_result_retrieval_failure_yields_no_result_and_typed_code(db_session, customer, monkeypatch):
    connection, job_run = _persisted_run(db_session, customer, status="COMPLETED")
    adapter = _adapter(monkeypatch, extra={"sql_endpoint": None, "lakehouse_database": None})
    monkeypatch.setattr("services.job_service.build_adapter", lambda c, db=None, delegated_token=None: adapter)

    result = await job_service.fetch_and_store_result(db_session, connection, job_run)

    assert result is None
    assert job_run.result_json is None
    assert job_run.error_code == ErrorCode.RESULT_RETRIEVAL_FAILED


@pytest.mark.asyncio
async def test_sql_connect_failure_does_not_leak_secret(monkeypatch):
    """The pyodbc connection string embeds the client secret — a driver failure
    must not surface it."""
    adapter = _adapter(monkeypatch, secret="SUPER-SECRET-XYZ")
    fake_pyodbc = types.ModuleType("pyodbc")
    fake_pyodbc.connect = MagicMock(side_effect=Exception("login failed for SUPER-SECRET-XYZ"))
    monkeypatch.setitem(sys.modules, "pyodbc", fake_pyodbc)

    with pytest.raises(PlatformError) as exc:
        await adapter.get_run_result("r1")

    assert exc.value.code == ErrorCode.RESULT_RETRIEVAL_FAILED
    assert "SUPER-SECRET-XYZ" not in exc.value.message


@pytest.mark.asyncio
async def test_successful_result_is_normalized_and_persisted(db_session, customer, monkeypatch):
    connection, job_run = _persisted_run(db_session, customer, status="COMPLETED")
    adapter = _adapter(monkeypatch)
    monkeypatch.setattr("services.job_service.build_adapter", lambda c, db=None, delegated_token=None: adapter)

    cursor = MagicMock()
    cursor.description = [("cluster_id",), ("recommendation",)]
    cursor.fetchall.return_value = [("c-1", "Enable autotermination")]
    conn = MagicMock()
    conn.cursor.return_value = cursor
    fake_pyodbc = types.ModuleType("pyodbc")
    fake_pyodbc.connect = MagicMock(return_value=conn)
    monkeypatch.setitem(sys.modules, "pyodbc", fake_pyodbc)

    normalized = await job_service.fetch_and_store_result(db_session, connection, job_run)

    assert normalized["optimization_type"] == "cluster"
    assert normalized["platform"] == "fabric"
    assert normalized["platform_run_id"] == "real-run-1"
    assert normalized["row_count"] == 1
    # The notebook's real output is preserved verbatim — nothing synthesized.
    assert normalized["source_payload"]["rows"] == [
        {"cluster_id": "c-1", "recommendation": "Enable autotermination"}
    ]
    assert json.loads(job_run.result_json)["row_count"] == 1
    # No invented business metrics.
    for invented in ("savings", "cost", "health_score", "optimized_count"):
        assert invented not in normalized


# --------------------------------------------------------------------------
# 8/9/10. Auth, permission, missing resource
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_authentication_failure_during_start_is_typed(monkeypatch):
    adapter = _adapter(monkeypatch)
    monkeypatch.setattr(adapter, "_acquire_token", lambda: (None, "AADSTS7000215 bad secret"))
    with pytest.raises(PlatformError) as exc:
        await adapter.start_analysis("cluster")
    assert exc.value.code == ErrorCode.AUTHENTICATION_FAILED
    assert "AADSTS7000215" not in exc.value.message


@pytest.mark.asyncio
@respx.mock
async def test_permission_denied_during_poll_is_typed_and_terminal(monkeypatch):
    adapter = _adapter(monkeypatch)
    respx.get(_instance_url("r1")).mock(return_value=httpx.Response(403))
    with pytest.raises(PlatformError) as exc:
        await adapter.get_run_status("r1")
    assert exc.value.code == ErrorCode.PERMISSION_DENIED
    assert not is_transient(exc.value.code)


@pytest.mark.asyncio
@respx.mock
async def test_missing_platform_run_is_resource_not_found(monkeypatch):
    adapter = _adapter(monkeypatch)
    respx.get(_instance_url("gone")).mock(return_value=httpx.Response(404))
    with pytest.raises(PlatformError) as exc:
        await adapter.get_run_status("gone")
    assert exc.value.code == ErrorCode.RESOURCE_NOT_FOUND


@pytest.mark.asyncio
async def test_domain_without_configured_resource_is_unsupported(monkeypatch):
    """No notebook configured for a domain -> typed config error, no network call."""
    adapter = _adapter(monkeypatch)
    with pytest.raises(PlatformError) as excinfo:
        await adapter.start_analysis("storage")
    assert excinfo.value.code == ErrorCode.NOTEBOOK_NOT_CONFIGURED


@pytest.mark.asyncio
@respx.mock
async def test_domain_with_configured_resource_uses_that_resource(monkeypatch):
    """Per-domain resource resolution — no hardcoded notebook ID."""
    adapter = _adapter(monkeypatch, extra={"query_notebook_id": "nb-query-777"})
    route = respx.post(_run_url("nb-query-777")).mock(
        return_value=httpx.Response(202, headers={"Location": _instance_url("q-1", "nb-query-777")})
    )
    result = await adapter.start_analysis("query")
    assert route.called
    assert result.detail["item_id"] == "nb-query-777"


# --------------------------------------------------------------------------
# 11. No fake fallback
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_start_failure_never_produces_a_synthetic_run_id(db_session, customer, monkeypatch):
    connection, job_run = _persisted_run(db_session, customer, status="QUEUED", platform_run_id=None)
    adapter = _adapter(monkeypatch)
    monkeypatch.setattr("services.job_service.build_adapter", lambda c, db=None, delegated_token=None: adapter)

    respx.post(_run_url()).mock(return_value=httpx.Response(403, json={"error": "denied"}))
    await job_service.start_job_run(db_session, connection, job_run)

    assert job_run.status == JobStatus.FAILED.value
    assert job_run.platform_run_id is None  # nothing invented
    assert not (job_run.platform_run_id or "").startswith("fab_")


@pytest.mark.asyncio
async def test_no_module_generates_fab_prefixed_ids_anymore():
    """Regression guard against the removed synthetic-run-ID fabrication."""
    import inspect
    import platforms.fabric as fabric_module

    source = inspect.getsource(fabric_module)
    assert 'f"fab_' not in source
    assert "sim_run_id" not in source
    assert "pipeline_engine" not in source


@pytest.mark.asyncio
async def test_fabric_adapter_does_not_import_the_demo_optimization_engine():
    """get_run_result used to fall back to hardcoded optimization constants."""
    import inspect
    import platforms.fabric as fabric_module

    source = inspect.getsource(fabric_module)
    assert "execute_full_domain_analysis" not in source
    assert "optimization_engine" not in source


# --------------------------------------------------------------------------
# 12. Customer isolation
# --------------------------------------------------------------------------

def test_job_results_are_not_readable_by_another_customer(client, db_session, customer):
    other = Customer(id=str(uuid.uuid4()), name="Other Co")
    db_session.add(other)
    db_session.commit()

    connection, job_run = _persisted_run(db_session, other, status="COMPLETED")
    job_id = job_run.analysis_job_id

    for path in (f"/api/jobs/{job_id}", f"/api/jobs/{job_id}/results", f"/api/jobs/{job_id}/logs"):
        resp = client.get(path, headers={"X-Customer-Id": customer.id})
        assert resp.status_code == 404

    for path in (f"/api/jobs/{job_id}/cancel", f"/api/jobs/{job_id}/retry"):
        resp = client.post(path, headers={"X-Customer-Id": customer.id})
        assert resp.status_code == 404


# --------------------------------------------------------------------------
# 13. No secret leakage
# --------------------------------------------------------------------------

def test_job_api_responses_contain_no_credentials(client, db_session, customer):
    connection, job_run = _persisted_run(db_session, customer, status="COMPLETED")
    job_id = job_run.analysis_job_id

    bodies = [
        client.get(f"/api/jobs/{job_id}", headers={"X-Customer-Id": customer.id}).text,
        client.get(f"/api/jobs/{job_id}/results", headers={"X-Customer-Id": customer.id}).text,
        client.get(f"/api/jobs/{job_id}/logs", headers={"X-Customer-Id": customer.id}).text,
    ]
    for body in bodies:
        assert "secret_encrypted" not in body
        assert "client_secret" not in body
        assert "access_token" not in body
        assert "Bearer " not in body
        assert "PLAINTEXT-SECRET" not in body


# --------------------------------------------------------------------------
# 14. Concurrent runs
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_concurrent_runs_track_independent_platform_run_ids(db_session, customer, monkeypatch):
    """Two domains running at once must not share or overwrite each other's IDs."""
    adapter = _adapter(monkeypatch, extra={"query_notebook_id": "nb-query-777"})
    monkeypatch.setattr("services.job_service.build_adapter", lambda c, db=None, delegated_token=None: adapter)

    connection = _connection(db_session, customer)
    analysis_job = AnalysisJob(
        id=str(uuid.uuid4()), customer_id=customer.id, connection_id=connection.id,
        request="Analyze everything", intent="all", platform="fabric", status="QUEUED",
    )
    db_session.add(analysis_job)
    db_session.commit()

    cluster_run = job_service.create_job_run(db_session, analysis_job, "cluster")
    query_run = job_service.create_job_run(db_session, analysis_job, "query")

    respx.post(_run_url()).mock(
        return_value=httpx.Response(202, headers={"Location": _instance_url("cluster-run-A")})
    )
    respx.post(_run_url("nb-query-777")).mock(
        return_value=httpx.Response(202, headers={"Location": _instance_url("query-run-B", "nb-query-777")})
    )

    await job_service.start_job_run(db_session, connection, cluster_run)
    await job_service.start_job_run(db_session, connection, query_run)

    assert cluster_run.platform_run_id == "cluster-run-A"
    assert query_run.platform_run_id == "query-run-B"
    assert cluster_run.platform_resource_id == CLUSTER_ITEM
    assert query_run.platform_resource_id == "nb-query-777"


# --------------------------------------------------------------------------
# 15. Retry behaviour classification
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status,transient",
    [(400, False), (401, False), (403, False), (404, False), (429, True), (500, True), (503, True)],
)
def test_retry_classification(status, transient):
    assert is_transient(code_for_status(status)) is transient


@pytest.mark.asyncio
@respx.mock
async def test_auth_failure_during_poll_fails_run_instead_of_retrying_forever(db_session, customer, monkeypatch):
    connection, job_run = _persisted_run(db_session, customer, status="RUNNING")
    adapter = _adapter(monkeypatch)
    monkeypatch.setattr("services.job_service.build_adapter", lambda c, db=None, delegated_token=None: adapter)

    respx.get(_instance_url("real-run-1")).mock(return_value=httpx.Response(401))
    await job_service.sync_run_status(db_session, connection, job_run)

    assert job_run.status == JobStatus.FAILED.value
    assert job_run.error_code == ErrorCode.AUTHENTICATION_FAILED


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _connection(db_session, customer) -> Connection:
    conn = Connection(
        id=str(uuid.uuid4()),
        customer_id=customer.id,
        platform="fabric",
        workspace="WS",
        endpoint="https://api.fabric.microsoft.com/v1",
        auth_method="service_principal",
        auth_metadata=json.dumps(
            {"tenant_id": "t", "client_id": "test-client", "workspace_id": WORKSPACE_ID,
             "notebook_item_id": CLUSTER_ITEM,
             "cluster_source_table": "src", "cluster_result_table": "dst"}
        ),
        secret_encrypted=None,
        status="connected",
    )
    db_session.add(conn)
    db_session.commit()
    return conn


def _persisted_run(db_session, customer, status="STARTING", platform_run_id="real-run-1"):
    connection = _connection(db_session, customer)
    analysis_job = AnalysisJob(
        id=str(uuid.uuid4()), customer_id=customer.id, connection_id=connection.id,
        request="Analyze my cluster", intent="cluster", platform="fabric", status=status,
    )
    db_session.add(analysis_job)
    db_session.commit()

    job_run = JobRun(
        id=str(uuid.uuid4()), analysis_job_id=analysis_job.id, domain="cluster",
        platform="fabric", platform_run_id=platform_run_id, status=status,
    )
    db_session.add(job_run)
    db_session.commit()
    return connection, job_run
