"""
Regression tests for the Cluster-only execution path.

Covers the two defects that made a correctly-provisioned Cluster environment
unrunnable: readiness that depended on the unrelated Query/Storage notebooks,
and a start request Fabric rejected with an opaque HTTP 400.
"""

import json

import httpx
import pytest
import respx

from platforms.fabric import FabricAdapter, FABRIC_API_BASE
from platforms.errors import ErrorCode, PlatformError
from services.job_service import missing_required_parameters

WORKSPACE_ID = "03e0392d-ed86-41e4-943c-146f8d845a1d"
ITEM_ID = "7243040a-235b-4221-ada3-708c97c086a2"


def _adapter() -> FabricAdapter:
    adapter = FabricAdapter(
        endpoint="https://api.fabric.microsoft.com",
        auth_metadata={"workspace_id": WORKSPACE_ID, "cluster_notebook_id": ITEM_ID},
        secret=None,
    )
    adapter.delegated_token = "test-token"
    return adapter


# --- the request body Fabric rejected ---------------------------------------


def test_empty_parameter_values_are_never_sent():
    """A blank value is what Fabric reports as 'missing or invalid information'."""
    body = FabricAdapter._execution_body(
        {"acelo_run_id": "run-1", "environment_id": "", "model_dir": None}
    )
    names = body["executionData"]["parameters"].keys()
    assert "environment_id" not in names
    assert "model_dir" not in names
    assert list(names) == ["acelo_run_id"]


def test_no_parameters_produces_no_execution_data():
    assert FabricAdapter._execution_body({"environment_id": ""}) == {}


# --- the execution endpoint --------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_start_uses_documented_job_scheduler_endpoint():
    route = respx.post(
        f"{FABRIC_API_BASE}/workspaces/{WORKSPACE_ID}/items/{ITEM_ID}/jobs/instances"
    ).mock(
        return_value=httpx.Response(
            202, headers={"Location": f"https://x/instances/job-123"}
        )
    )

    result = await _adapter().start_analysis("cluster", {"acelo_run_id": "run-1"})

    assert route.called
    assert route.calls[0].request.url.params["jobType"] == "RunNotebook"
    assert result.platform_run_id == "job-123"


@pytest.mark.asyncio
@respx.mock
async def test_start_falls_back_to_legacy_path_on_404():
    respx.post(
        f"{FABRIC_API_BASE}/workspaces/{WORKSPACE_ID}/items/{ITEM_ID}/jobs/instances"
    ).mock(return_value=httpx.Response(404))
    legacy = respx.post(
        f"{FABRIC_API_BASE}/workspaces/{WORKSPACE_ID}/items/{ITEM_ID}"
        f"/jobs/RunNotebook/instances"
    ).mock(
        return_value=httpx.Response(202, headers={"Location": "https://x/instances/job-9"})
    )

    result = await _adapter().start_analysis("cluster", {"acelo_run_id": "run-1"})

    assert legacy.called
    assert result.platform_run_id == "job-9"


# --- error surfacing ---------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_fabric_error_code_and_message_reach_the_caller():
    """The real cause must not be flattened into a generic failure sentence."""
    respx.post(url__regex=r".*/jobs/.*").mock(
        return_value=httpx.Response(
            400,
            json={
                "requestId": "47d890af",
                "errorCode": "BadRequest",
                "message": "The request could not be processed due to missing or invalid information",
            },
        )
    )

    with pytest.raises(PlatformError) as excinfo:
        await _adapter().start_analysis("cluster", {"acelo_run_id": "run-1"})

    message = excinfo.value.message
    assert "BadRequest" in message
    assert "missing or invalid information" in message
    assert "400" in message
    # The token must never travel with the error.
    assert "test-token" not in message
    assert "test-token" not in (excinfo.value.log_detail or "")


# --- required configuration --------------------------------------------------


def test_cluster_requires_source_and_result_table():
    assert missing_required_parameters(
        "cluster", {"acelo_run_id": "run-1"}
    ) == ["source_table", "result_table"]


def test_cluster_configured_has_no_missing_parameters():
    assert (
        missing_required_parameters(
            "cluster", {"source_table": "t", "result_table": "r", "acelo_run_id": "run-1"}
        )
        == []
    )


def test_blank_parameter_counts_as_missing():
    assert "result_table" in missing_required_parameters(
        "cluster", {"source_table": "t", "result_table": "   "}
    )


# ---------------------------------------------------------------------------
# Connection selection
#
# The live failure was not a bad Fabric call — it was a run dispatched against
# a connection that has no Environment behind it, so nothing was provisioned
# and nothing was configured.
# ---------------------------------------------------------------------------

import uuid

from agent.router import build_adapter
from models import Connection, Environment, JobRun, Resource
from services.job_service import build_run_parameters
from services import provisioning_service


def _connection(db_session, customer, metadata: dict) -> Connection:
    conn = Connection(
        id=str(uuid.uuid4()),
        customer_id=customer.id,
        platform="fabric",
        workspace="WS",
        endpoint="https://api.fabric.microsoft.com",
        auth_method="user",
        auth_metadata=json.dumps(metadata),
        secret_encrypted=None,
        status="connected",
    )
    db_session.add(conn)
    db_session.commit()
    return conn


def _environment_with_cluster(db_session, customer, conn, item_id: str) -> Environment:
    env = Environment(
        id=str(uuid.uuid4()),
        customer_id=customer.id,
        connection_id=conn.id,
        name="acelo demo",
        platform="fabric",
        workspace_id=WORKSPACE_ID,
        status="environment_ready",
    )
    db_session.add(env)
    db_session.commit()
    db_session.add(
        Resource(
            id=str(uuid.uuid4()),
            environment_id=env.id,
            platform="fabric",
            resource_type="Notebook",
            display_name="ACELO Cluster Optimization",
            platform_resource_id=item_id,
            status=provisioning_service.ACELO_OWNED,
            detail_json=json.dumps({"domain": "cluster", "resource_key": "cluster_notebook_id"}),
        )
    )
    db_session.commit()
    return env


def _cluster_run() -> JobRun:
    return JobRun(
        id="run-probe", analysis_job_id="job-1", domain="cluster",
        platform="fabric", status="QUEUED",
    )


def test_connection_without_environment_reproduces_invalid_configuration(db_session, customer):
    """
    Reproduces the live failure exactly: a 'connected' connection that has no
    Environment resolves no notebook and no table settings.
    """
    conn = _connection(
        db_session,
        customer,
        {"workspace_id": "ws-acelo-fabric-workspace-01", "cluster_notebook_id": "nb-cluster-opt-v2"},
    )

    parameters = build_run_parameters(conn, _cluster_run(), db_session)

    assert missing_required_parameters("cluster", parameters) == ["source_table", "result_table"]
    assert parameters["environment_id"] == ""


def test_configured_environment_backed_connection_resolves_everything(db_session, customer):
    """A properly configured Cluster resolves every value the Fabric call needs."""
    conn = _connection(
        db_session,
        customer,
        {
            "workspace_id": WORKSPACE_ID,
            "cluster_source_table": "realistic_cluster_dataset",
            "cluster_result_table": "acelo_cluster_recommendations",
            "lakehouse_database": "Data",
        },
    )
    env = _environment_with_cluster(db_session, customer, conn, ITEM_ID)

    adapter = build_adapter(conn, db_session, delegated_token="tok")
    parameters = build_run_parameters(conn, _cluster_run(), db_session)

    assert adapter.auth_metadata["workspace_id"] == WORKSPACE_ID
    assert adapter.auth_metadata["cluster_notebook_id"] == ITEM_ID
    assert parameters["environment_id"] == env.id
    assert parameters["source_table"] == "realistic_cluster_dataset"
    assert missing_required_parameters("cluster", parameters) == []


@pytest.mark.asyncio
@respx.mock
async def test_configured_cluster_reaches_the_fabric_execution_call(db_session, customer):
    """End to end: a configured Cluster actually reaches Fabric and gets a real id."""
    conn = _connection(
        db_session,
        customer,
        {
            "workspace_id": WORKSPACE_ID,
            "cluster_source_table": "src",
            "cluster_result_table": "dst",
        },
    )
    _environment_with_cluster(db_session, customer, conn, ITEM_ID)

    route = respx.post(
        f"{FABRIC_API_BASE}/workspaces/{WORKSPACE_ID}/items/{ITEM_ID}/jobs/instances"
    ).mock(
        return_value=httpx.Response(202, headers={"Location": "https://x/instances/real-job-77"})
    )

    adapter = build_adapter(conn, db_session, delegated_token="tok")
    result = await adapter.start_analysis(
        "cluster", build_run_parameters(conn, _cluster_run(), db_session)
    )

    assert route.called
    assert result.platform_run_id == "real-job-77"
    sent = json.loads(route.calls[0].request.content)["executionData"]["parameters"]
    assert sent["source_table"]["value"] == "src"
    assert sent["result_table"]["value"] == "dst"


@pytest.mark.asyncio
async def test_no_platform_run_id_is_invented_when_configuration_is_missing(db_session, customer):
    """A failed start must leave platform_run_id empty — never a synthesised id."""
    from services import job_service

    conn = _connection(db_session, customer, {"workspace_id": WORKSPACE_ID})
    job = job_service.create_analysis_job(db_session, customer.id, conn, "Analyze clusters", "cluster")
    run = job_service.create_job_run(db_session, job, "cluster")

    await job_service.start_job_run(db_session, conn, run, delegated_token="tok")

    assert run.status == "FAILED"
    assert run.error_code == ErrorCode.CLUSTER_SOURCE_TABLE_NOT_CONFIGURED
    assert run.platform_run_id is None
    assert "source_table" in run.error


def test_duplicate_cluster_registrations_resolve_to_the_newest(db_session, customer):
    """Re-provisioning must not leave execution choosing between two notebooks."""
    conn = _connection(db_session, customer, {"workspace_id": WORKSPACE_ID})
    env = _environment_with_cluster(db_session, customer, conn, "older-item")
    db_session.add(
        Resource(
            id=str(uuid.uuid4()),
            environment_id=env.id,
            platform="fabric",
            resource_type="Notebook",
            display_name="ACELO Cluster Optimization",
            platform_resource_id="newer-item",
            status=provisioning_service.ACELO_OWNED,
            detail_json=json.dumps({"domain": "cluster"}),
        )
    )
    db_session.commit()

    assert provisioning_service.resolved_domains(db_session, env)["cluster"] == "newer-item"


def test_environment_response_exposes_connection_id(customer, db_session):
    """
    The UI distinguishes a runnable connection from a seeded one by matching
    environments to connections. Dropping this field made every real
    environment invisible without any error being raised.
    """
    conn = _connection(db_session, customer, {"workspace_id": WORKSPACE_ID})
    _environment_with_cluster(db_session, customer, conn, ITEM_ID)

    from fastapi.testclient import TestClient
    from main import app

    body = TestClient(app).get(
        "/api/environments", headers={"X-Customer-Id": customer.id}
    ).json()

    assert body, "the environment must be listed"
    assert body[0]["connection_id"] == conn.id
    # The projection must still carry no credential material.
    assert not any(
        key in body[0] for key in ("secret", "client_secret", "auth_metadata", "token")
    )


# ---------------------------------------------------------------------------
# Discovery must not unconfigure deployed notebooks
#
# The live failure: running discovery after provisioning deleted every Resource
# row for the environment and re-inserted the notebook as a plain "discovered"
# item with no domain. Execution then resolved no cluster notebook and reported
# an unimplemented capability instead of a configuration problem.
# ---------------------------------------------------------------------------


def _managed_cluster_notebook(db_session, env, item_id: str) -> Resource:
    resource = Resource(
        id=str(uuid.uuid4()),
        environment_id=env.id,
        platform="fabric",
        resource_type="Notebook",
        display_name="ACELO Cluster Optimization",
        platform_resource_id=item_id,
        status=provisioning_service.ACELO_OWNED,
        detail_json=json.dumps({"domain": "cluster", "resource_key": "cluster_notebook_id"}),
    )
    db_session.add(resource)
    db_session.commit()
    return resource


@pytest.mark.asyncio
async def test_discovery_preserves_the_registered_cluster_notebook(
    db_session, customer, monkeypatch
):
    from services import environment_service
    from platforms.base import DiscoveredResource, DiscoveredWorkspace

    conn = _connection(
        db_session,
        customer,
        {
            "workspace_id": WORKSPACE_ID,
            "cluster_source_table": "realistic_cluster_dataset",
            "cluster_result_table": "acelo_cluster_recommendations",
        },
    )
    env = _environment_with_cluster(db_session, customer, conn, ITEM_ID)

    class _Adapter:
        async def discover_workspace(self):
            return DiscoveredWorkspace(workspace_id=WORKSPACE_ID, workspace_name="acelo demo")

        async def discover_resources(self):
            # Fabric reports the notebook with no ACELO metadata of its own.
            return [
                DiscoveredResource(
                    resource_type="Notebook",
                    display_name="ACELO Cluster Optimization",
                    platform_resource_id=ITEM_ID,
                    detail={},
                ),
                DiscoveredResource(
                    resource_type="Folder",
                    display_name="ACELO",
                    platform_resource_id="folder-1",
                    detail={},
                ),
            ]

    monkeypatch.setattr(environment_service, "build_adapter", lambda *a, **k: _Adapter())
    await environment_service.discover_environment(db_session, env)

    # The registration survives, so execution can still resolve the notebook.
    assert provisioning_service.resolved_domains(db_session, env) == {"cluster": ITEM_ID}

    parameters = build_run_parameters(conn, _cluster_run(), db_session)
    adapter = build_adapter(conn, db_session, delegated_token="tok")
    assert adapter.auth_metadata["cluster_notebook_id"] == ITEM_ID
    assert missing_required_parameters("cluster", parameters) == []


@pytest.mark.asyncio
async def test_discovery_drops_a_registration_whose_notebook_was_deleted(
    db_session, customer, monkeypatch
):
    """Honest the other way: a notebook removed in Fabric must stop resolving."""
    from services import environment_service
    from platforms.base import DiscoveredWorkspace

    conn = _connection(db_session, customer, {"workspace_id": WORKSPACE_ID})
    env = _environment_with_cluster(db_session, customer, conn, ITEM_ID)

    class _Adapter:
        async def discover_workspace(self):
            return DiscoveredWorkspace(workspace_id=WORKSPACE_ID, workspace_name="acelo demo")

        async def discover_resources(self):
            return []

    monkeypatch.setattr(environment_service, "build_adapter", lambda *a, **k: _Adapter())
    await environment_service.discover_environment(db_session, env)

    assert provisioning_service.resolved_domains(db_session, env) == {}


# ---------------------------------------------------------------------------
# Typed error for an unregistered notebook
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_cluster_notebook_returns_typed_configuration_error():
    """Not a capability error: start_analysis is implemented, config is absent."""
    adapter = FabricAdapter(
        endpoint="https://api.fabric.microsoft.com",
        auth_metadata={"workspace_id": WORKSPACE_ID},
        secret=None,
    )
    adapter.delegated_token = "tok"

    with pytest.raises(PlatformError) as excinfo:
        await adapter.start_analysis("cluster", {"source_table": "s", "result_table": "r"})

    assert excinfo.value.code == ErrorCode.NOTEBOOK_NOT_CONFIGURED
    assert "does not implement" not in excinfo.value.message


@pytest.mark.asyncio
async def test_query_and_storage_still_report_missing_notebook_the_same_way():
    """Cluster-focused change must not alter the other domains' behaviour."""
    adapter = FabricAdapter(
        endpoint="https://api.fabric.microsoft.com",
        auth_metadata={"workspace_id": WORKSPACE_ID, "cluster_notebook_id": ITEM_ID},
        secret=None,
    )
    adapter.delegated_token = "tok"

    for domain in ("query", "storage"):
        with pytest.raises(PlatformError) as excinfo:
            await adapter.start_analysis(domain, {})
        assert excinfo.value.code == ErrorCode.NOTEBOOK_NOT_CONFIGURED
        assert domain in excinfo.value.message


@pytest.mark.asyncio
@respx.mock
async def test_missing_delegated_token_never_produces_a_run_id(db_session, customer):
    """No token must fail honestly, not mint a placeholder platform_run_id."""
    from services import job_service

    conn = _connection(
        db_session,
        customer,
        {"workspace_id": WORKSPACE_ID, "cluster_source_table": "s", "cluster_result_table": "r"},
    )
    _environment_with_cluster(db_session, customer, conn, ITEM_ID)
    job = job_service.create_analysis_job(db_session, customer.id, conn, "clusters", "cluster")
    run = job_service.create_job_run(db_session, job, "cluster")

    # No delegated token, and the connection stores no client secret either.
    await job_service.start_job_run(db_session, conn, run, delegated_token=None)

    assert run.status == "FAILED"
    assert run.platform_run_id is None


# ---------------------------------------------------------------------------
# Notebook parameter contract
#
# The deployed notebook validates its own parameters and aborts the Spark
# session if source_table, result_table or acelo_run_id is blank. These tests
# pin the payload ACELO actually sends.
# ---------------------------------------------------------------------------


def _configured_connection(db_session, customer) -> Connection:
    return _connection(
        db_session,
        customer,
        {
            "workspace_id": WORKSPACE_ID,
            "cluster_source_table": "realistic_cluster_dataset",
            "cluster_result_table": "acelo_cluster_recommendations",
            "lakehouse_database": "Data",
        },
    )


@pytest.mark.asyncio
@respx.mock
async def test_fabric_payload_carries_every_required_notebook_parameter(db_session, customer):
    conn = _configured_connection(db_session, customer)
    env = _environment_with_cluster(db_session, customer, conn, ITEM_ID)

    route = respx.post(
        f"{FABRIC_API_BASE}/workspaces/{WORKSPACE_ID}/items/{ITEM_ID}/jobs/instances"
    ).mock(return_value=httpx.Response(202, headers={"Location": "https://x/instances/job-1"}))

    adapter = build_adapter(conn, db_session, delegated_token="tok")
    await adapter.start_analysis(
        "cluster", build_run_parameters(conn, _cluster_run(), db_session)
    )

    body = json.loads(route.calls[0].request.content)
    params = body["executionData"]["parameters"]

    # Fabric's Run On Demand format: name -> {value, type}.
    assert params["source_table"] == {"value": "realistic_cluster_dataset", "type": "string"}
    assert params["result_table"] == {"value": "acelo_cluster_recommendations", "type": "string"}
    assert params["acelo_run_id"] == {"value": "run-probe", "type": "string"}
    assert params["environment_id"] == {"value": env.id, "type": "string"}
    assert params["source_lakehouse"] == {"value": "Data", "type": "string"}
    assert params["result_lakehouse"] == {"value": "Data", "type": "string"}


@pytest.mark.asyncio
async def test_missing_source_table_fails_before_any_fabric_call(db_session, customer):
    from services import job_service

    conn = _connection(
        db_session, customer, {"workspace_id": WORKSPACE_ID, "cluster_result_table": "dst"}
    )
    _environment_with_cluster(db_session, customer, conn, ITEM_ID)
    job = job_service.create_analysis_job(db_session, customer.id, conn, "clusters", "cluster")
    run = job_service.create_job_run(db_session, job, "cluster")

    # respx is not active: any outbound HTTP call would error, proving none is made.
    await job_service.start_job_run(db_session, conn, run, delegated_token="tok")

    assert run.error_code == ErrorCode.CLUSTER_SOURCE_TABLE_NOT_CONFIGURED
    assert run.platform_run_id is None


@pytest.mark.asyncio
async def test_missing_result_table_fails_before_any_fabric_call(db_session, customer):
    from services import job_service

    conn = _connection(
        db_session, customer, {"workspace_id": WORKSPACE_ID, "cluster_source_table": "src"}
    )
    _environment_with_cluster(db_session, customer, conn, ITEM_ID)
    job = job_service.create_analysis_job(db_session, customer.id, conn, "clusters", "cluster")
    run = job_service.create_job_run(db_session, job, "cluster")

    await job_service.start_job_run(db_session, conn, run, delegated_token="tok")

    assert run.error_code == ErrorCode.CLUSTER_RESULT_TABLE_NOT_CONFIGURED
    assert run.platform_run_id is None


@pytest.mark.asyncio
@respx.mock
async def test_valid_configuration_produces_a_real_fabric_run_id(db_session, customer):
    from services import job_service

    conn = _configured_connection(db_session, customer)
    _environment_with_cluster(db_session, customer, conn, ITEM_ID)
    respx.post(
        f"{FABRIC_API_BASE}/workspaces/{WORKSPACE_ID}/items/{ITEM_ID}/jobs/instances"
    ).mock(
        return_value=httpx.Response(
            202,
            headers={"Location": "https://x/instances/7c28a94b-2fb9-4bf7-aa6e-75a3b127957a"},
        )
    )
    job = job_service.create_analysis_job(db_session, customer.id, conn, "clusters", "cluster")
    run = job_service.create_job_run(db_session, job, "cluster")

    await job_service.start_job_run(db_session, conn, run, delegated_token="tok")

    assert run.platform_run_id == "7c28a94b-2fb9-4bf7-aa6e-75a3b127957a"
    assert run.status != "FAILED"


@pytest.mark.asyncio
@respx.mock
async def test_parameter_log_names_settings_but_never_leaks_credentials(
    db_session, customer, caplog
):
    from services import job_service

    conn = _connection(
        db_session,
        customer,
        {
            "workspace_id": WORKSPACE_ID,
            "cluster_source_table": "realistic_cluster_dataset",
            "cluster_result_table": "acelo_cluster_recommendations",
            "cluster_llm_key_vault_uri": "https://vault.azure.net/secret-path",
            "cluster_llm_secret_name": "groq-api-key",
        },
    )
    _environment_with_cluster(db_session, customer, conn, ITEM_ID)
    respx.post(url__regex=r".*/jobs/instances.*").mock(
        return_value=httpx.Response(202, headers={"Location": "https://x/instances/job-9"})
    )
    job = job_service.create_analysis_job(db_session, customer.id, conn, "clusters", "cluster")
    run = job_service.create_job_run(db_session, job, "cluster")

    with caplog.at_level("INFO", logger="acelo.execution"):
        await job_service.start_job_run(db_session, conn, run, delegated_token="super-secret-token")

    logged = "\n".join(record.getMessage() for record in caplog.records)

    assert "fabric_notebook_parameters" in logged
    assert "source_table=realistic_cluster_dataset" in logged
    assert "result_table=acelo_cluster_recommendations" in logged
    assert "acelo_run_id=" in logged
    assert "environment_id=" in logged

    # Credential-adjacent settings are named, never valued.
    assert "llm_key_vault_uri=<set>" in logged
    assert "llm_secret_name=<set>" in logged
    assert "vault.azure.net/secret-path" not in logged
    assert "groq-api-key" not in logged
    # The delegated token is not a notebook parameter and must never appear.
    assert "super-secret-token" not in logged
