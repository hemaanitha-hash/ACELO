import pytest

from platforms.base import PlatformAdapter, PlatformCapabilityNotImplemented
from platforms.errors import ErrorCode, PlatformError
from platforms.databricks import DatabricksAdapter
from platforms.fabric import FabricAdapter


def test_databricks_adapter_implements_full_interface():
    adapter = DatabricksAdapter(endpoint="https://x", auth_metadata={}, secret="tok")
    assert isinstance(adapter, PlatformAdapter)
    for method in ["connect", "test_connection", "get_environment", "start_analysis", "get_run_status",
                    "get_run_logs", "get_run_result", "cancel_run", "retry_run"]:
        assert hasattr(adapter, method)


def test_fabric_adapter_implements_full_interface():
    adapter = FabricAdapter(endpoint="https://x", auth_metadata={"tenant_id": "t", "client_id": "c"}, secret="s")
    assert isinstance(adapter, PlatformAdapter)


@pytest.mark.asyncio
async def test_fabric_start_analysis_query_domain_raises_controlled_error_not_fake_success():
    # Phase 2 only implements Cluster; Query/Storage stay controlled-not-implemented.
    adapter = FabricAdapter(endpoint="https://x", auth_metadata={"tenant_id": "t", "client_id": "c"}, secret="s")
    with pytest.raises(PlatformError) as exc_info:
        await adapter.start_analysis("query")
    assert exc_info.value.code == ErrorCode.NOTEBOOK_NOT_CONFIGURED
    assert "query" in exc_info.value.message


@pytest.mark.asyncio
async def test_fabric_start_analysis_storage_domain_raises_controlled_error():
    adapter = FabricAdapter(endpoint="https://x", auth_metadata={"tenant_id": "t", "client_id": "c"}, secret="s")
    with pytest.raises(PlatformError) as exc_info:
        await adapter.start_analysis("storage")
    assert exc_info.value.code == ErrorCode.NOTEBOOK_NOT_CONFIGURED


@pytest.mark.asyncio
async def test_acquire_token_reports_bad_tenant_cleanly_instead_of_crashing():
    """
    Regression test: MSAL's ConfidentialClientApplication raises a bare
    ValueError when it can't resolve the authority (bad tenant_id, or the
    authority host is unreachable) instead of returning an error dict like it
    does for auth failures. Before the fix, this crashed every caller
    (test_connection, start_analysis, ...) with an unhandled exception instead
    of a clean ConnectionResult(ok=False, ...). Found during a real dry run
    against this exact failure mode.
    """
    adapter = FabricAdapter(
        endpoint="https://x",
        auth_metadata={"tenant_id": "not-a-real-tenant", "client_id": "c"},
        secret="s",
    )
    # No network mocking needed/possible here — MSAL raises before any HTTP call
    # is made, purely from validating the authority URL shape/resolution.
    token, error = adapter._acquire_token()
    assert token is None
    assert error  # some human-readable message, not a raised exception

    result = await adapter.test_connection()
    assert result.ok is False
    assert result.message.startswith("Azure AD sign-in failed:")


@pytest.mark.asyncio
async def test_fabric_execution_methods_raise_controlled_error():
    adapter = FabricAdapter(endpoint="https://x", auth_metadata={"tenant_id": "t", "client_id": "c"}, secret="s")
    with pytest.raises(PlatformCapabilityNotImplemented):
        await adapter.start_execution({"type": "resize_cluster"})
    with pytest.raises(PlatformCapabilityNotImplemented):
        await adapter.rollback("run-id")
