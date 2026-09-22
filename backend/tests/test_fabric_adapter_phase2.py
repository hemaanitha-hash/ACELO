import sys
import types
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from platforms.errors import ErrorCode, PlatformError

from platforms.base import PlatformCapabilityNotImplemented
from platforms.fabric import FabricAdapter

WORKSPACE_ID = "4b218778-e7a5-4d73-8187-f10824047715"
ITEM_ID = "431e8d7b-4a95-4c02-8ccd-6faef5ba1bd7"
FABRIC_BASE = "https://api.fabric.microsoft.com/v1"


def _cluster_adapter(monkeypatch, extra_metadata: dict | None = None) -> FabricAdapter:
    metadata = {
        "tenant_id": "test-tenant",
        "client_id": "test-client",
        "workspace_id": WORKSPACE_ID,
        "notebook_item_id": ITEM_ID,
        "sql_endpoint": "test.datawarehouse.fabric.microsoft.com",
        "lakehouse_database": "data_Demo",
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    adapter = FabricAdapter(endpoint="https://x", auth_metadata=metadata, secret="test-secret")
    # Real AAD sign-in is out of scope for these tests — mock at the token boundary,
    # exercise everything past it (the real Fabric Job Scheduler / SQL calls) for real.
    monkeypatch.setattr(adapter, "_acquire_token", lambda: ("fake-token", None))
    return adapter


@pytest.mark.asyncio
@respx.mock
async def test_start_analysis_cluster_triggers_real_job_scheduler_call_and_captures_run_id(monkeypatch):
    adapter = _cluster_adapter(monkeypatch)
    job_instance_id = "f2d65699-dd22-4889-980c-15226deb0e1b"
    location = f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items/{ITEM_ID}/jobs/instances/{job_instance_id}"

    route = respx.post(
        f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items/{ITEM_ID}/jobs/instances"
    ).mock(return_value=httpx.Response(202, headers={"Location": location}))

    result = await adapter.start_analysis("cluster")

    assert route.called
    assert result.platform_run_id == job_instance_id
    assert result.status == "STARTING"
    assert result.detail == {"workspace_id": WORKSPACE_ID, "item_id": ITEM_ID}


@pytest.mark.asyncio
@respx.mock
async def test_start_analysis_query_domain_never_calls_fabric(monkeypatch):
    adapter = _cluster_adapter(monkeypatch)
    route = respx.post(
        f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items/{ITEM_ID}/jobs/instances"
    ).mock(return_value=httpx.Response(202, headers={"Location": "irrelevant"}))

    with pytest.raises(PlatformError):
        await adapter.start_analysis("query")

    assert not route.called  # no fake success, no network call at all for unsupported domains


@pytest.mark.asyncio
async def test_start_analysis_missing_workspace_id_raises_clear_error(monkeypatch):
    adapter = FabricAdapter(
        endpoint="https://x",
        auth_metadata={"tenant_id": "t", "client_id": "c", "notebook_item_id": ITEM_ID},
        secret="s",
    )
    monkeypatch.setattr(adapter, "_acquire_token", lambda: ("fake-token", None))
    with pytest.raises(ValueError, match="workspace_id"):
        await adapter.start_analysis("cluster")


@pytest.mark.asyncio
@respx.mock
async def test_start_analysis_non_202_response_raises_typed_error(monkeypatch):
    """A refusal is a typed PlatformError that carries Fabric's own message."""
    adapter = _cluster_adapter(monkeypatch)
    respx.post(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items/{ITEM_ID}/jobs/instances").mock(
        return_value=httpx.Response(403, json={"message": "insufficient privileges"})
    )
    with pytest.raises(PlatformError, match="403") as excinfo:
        await adapter.start_analysis("cluster")
    assert "insufficient privileges" in excinfo.value.message


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    "fabric_status,expected",
    [
        ("NotStarted", "QUEUED"),
        ("InProgress", "RUNNING"),
        ("Completed", "COMPLETED"),
        ("Failed", "FAILED"),
        ("Cancelled", "CANCELLED"),
        ("Deduped", "CANCELLED"),
    ],
)
async def test_get_run_status_maps_every_fabric_status(monkeypatch, fabric_status, expected):
    adapter = _cluster_adapter(monkeypatch)
    run_id = "run-123"
    respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items/{ITEM_ID}/jobs/instances/{run_id}").mock(
        return_value=httpx.Response(200, json={"id": run_id, "status": fabric_status, "failureReason": None})
    )
    result = await adapter.get_run_status(run_id)
    assert result.status == expected


@pytest.mark.asyncio
@respx.mock
async def test_get_run_status_surfaces_real_failure_reason(monkeypatch):
    adapter = _cluster_adapter(monkeypatch)
    run_id = "run-123"
    respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items/{ITEM_ID}/jobs/instances/{run_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": run_id,
                "status": "Failed",
                "failureReason": {"errorCode": "NotebookExecutionFailed", "message": "cell 3 raised an exception"},
            },
        )
    )
    result = await adapter.get_run_status(run_id)
    assert result.status == "FAILED"
    assert "NotebookExecutionFailed" in result.error or "cell 3" in result.error
    # No invented progress/current_step — Fabric's Job Scheduler API doesn't expose these.
    assert result.progress is None
    assert result.current_step is None


@pytest.mark.asyncio
@respx.mock
async def test_get_run_logs_reflects_real_job_instance_fields_only(monkeypatch):
    adapter = _cluster_adapter(monkeypatch)
    run_id = "run-123"
    respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items/{ITEM_ID}/jobs/instances/{run_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": run_id,
                "status": "Completed",
                "startTimeUtc": "2026-03-01T10:00:00Z",
                "endTimeUtc": "2026-03-01T10:05:00Z",
                "failureReason": None,
                "exitValue": "success",
            },
        )
    )
    logs = await adapter.get_run_logs(run_id)
    messages = [entry.message for entry in logs]
    assert any("Completed" in m for m in messages)
    assert any("exitValue: success" in m for m in messages)
    assert not any("ERROR" == entry.level for entry in logs)  # no failure reason -> no fabricated error log


@pytest.mark.asyncio
@respx.mock
async def test_cancel_run_success(monkeypatch):
    adapter = _cluster_adapter(monkeypatch)
    run_id = "run-123"
    respx.post(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items/{ITEM_ID}/jobs/instances/{run_id}/cancel").mock(
        return_value=httpx.Response(202)
    )
    result = await adapter.cancel_run(run_id)
    assert result.ok is True


@pytest.mark.asyncio
@respx.mock
async def test_cancel_run_already_completed_reports_failure_not_success(monkeypatch):
    adapter = _cluster_adapter(monkeypatch)
    run_id = "run-123"
    respx.post(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items/{ITEM_ID}/jobs/instances/{run_id}/cancel").mock(
        return_value=httpx.Response(400, json={"errorCode": "JobAlreadyCompleted"})
    )
    result = await adapter.cancel_run(run_id)
    assert result.ok is False


@pytest.mark.asyncio
async def test_get_run_result_queries_real_result_table_via_sql_endpoint(monkeypatch):
    """
    pyodbc's native ODBC driver isn't installable in this sandbox (no network
    access to Microsoft's package repo), so the pyodbc module itself is faked
    here to prove the adapter builds the right connection string and issues the
    right SQL — not to fake the actual database round trip.
    """
    adapter = _cluster_adapter(monkeypatch)

    fake_cursor = MagicMock()
    fake_cursor.description = [("cluster_id",), ("recommendation",), ("estimated_savings",)]
    fake_cursor.fetchall.return_value = [("cluster-07", "Enable autotermination", 210.0)]

    fake_conn = MagicMock()
    fake_conn.cursor.return_value = fake_cursor

    fake_pyodbc = types.ModuleType("pyodbc")
    fake_pyodbc.connect = MagicMock(return_value=fake_conn)
    monkeypatch.setitem(sys.modules, "pyodbc", fake_pyodbc)

    result = await adapter.get_run_result("run-123")

    connect_args = fake_pyodbc.connect.call_args
    conn_str = connect_args.args[0]
    assert "Server=test.datawarehouse.fabric.microsoft.com,1433" in conn_str
    assert "Database=data_Demo" in conn_str
    assert "Authentication=ActiveDirectoryServicePrincipal" in conn_str
    assert "UID=test-client" in conn_str
    assert "PWD=test-secret" in conn_str

    executed_sql = fake_cursor.execute.call_args.args[0]
    assert "data_Demo.acelo_cluster_optimization_results" in executed_sql

    assert result.result_reference == "data_Demo.acelo_cluster_optimization_results"
    assert result.payload["row_count"] == 1
    assert result.payload["rows"] == [
        {"cluster_id": "cluster-07", "recommendation": "Enable autotermination", "estimated_savings": 210.0}
    ]
    fake_conn.close.assert_called_once()


@pytest.mark.asyncio
async def test_get_run_result_missing_pyodbc_raises_clear_actionable_error(monkeypatch):
    adapter = _cluster_adapter(monkeypatch)
    monkeypatch.setitem(sys.modules, "pyodbc", None)  # forces the lazy `import pyodbc` to fail

    with pytest.raises(RuntimeError, match="pyodbc"):
        await adapter.get_run_result("run-123")


@pytest.mark.asyncio
async def test_get_run_result_missing_sql_config_raises_clear_error(monkeypatch):
    adapter = _cluster_adapter(monkeypatch, extra_metadata={"sql_endpoint": None, "lakehouse_database": None})
    fake_pyodbc = types.ModuleType("pyodbc")
    fake_pyodbc.connect = MagicMock()
    monkeypatch.setitem(sys.modules, "pyodbc", fake_pyodbc)

    with pytest.raises(ValueError, match="sql_endpoint"):
        await adapter.get_run_result("run-123")


@pytest.mark.asyncio
@respx.mock
async def test_retry_run_re_triggers_a_fresh_job(monkeypatch):
    adapter = _cluster_adapter(monkeypatch)
    new_run_id = "run-456"
    location = f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items/{ITEM_ID}/jobs/instances/{new_run_id}"
    respx.post(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items/{ITEM_ID}/jobs/instances").mock(
        return_value=httpx.Response(202, headers={"Location": location})
    )
    result = await adapter.retry_run("cluster", "old-run-id")
    assert result.platform_run_id == new_run_id
