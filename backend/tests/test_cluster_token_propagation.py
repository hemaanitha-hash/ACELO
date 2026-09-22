"""
Regression tests for "CLUSTER — Failed: ACELO could not authenticate".

Root cause: the delegated Fabric token reached /api/environments/* but never
/api/jobs, so FabricAdapter.start_analysis() ran with no credential at all and
failed with AUTHENTICATION_FAILED — even though connect/discover/provision had
all succeeded moments earlier.

These prove the token now reaches the adapter on every job route, that its
absence is still a typed error (never a fake success), and that Query/Storage
behaviour is unchanged.
"""

import json
import uuid

import httpx
import pytest
import respx

from models import AnalysisJob, Connection, Customer, Environment, JobRun, Resource
from platforms.errors import ErrorCode
from platforms.fabric import FabricAdapter
from services import job_service, provisioning_service

FABRIC_BASE = "https://api.fabric.microsoft.com/v1"
WORKSPACE_ID = "03e0392d-ed86-41e4-943c-146f8d845a1d"
CLUSTER_NOTEBOOK_ID = "7243040a-235b-4221-ada3-708c97c086a2"
USER_TOKEN = "eyJ0eXAiOiJKV1QiLCJDELEGATED-USER-TOKEN"

SOURCE_TABLE = "Data.dbo.realistic_cluster_dataset"
RESULT_TABLE = "Data.dbo.acelo_cluster_ai_recommendations"


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from main import app

    return TestClient(app)


@pytest.fixture
def delegated_setup(db_session, customer):
    """
    A delegated ("Microsoft Account") Fabric environment with the real Cluster
    notebook registered — i.e. the state the product is actually in.
    """
    connection = Connection(
        id="conn-delegated",
        customer_id=customer.id,
        platform="fabric",
        workspace="acelo demo",
        endpoint="https://api.fabric.microsoft.com/v1",
        auth_method="delegated",
        auth_metadata=json.dumps(
            {
                "workspace_id": WORKSPACE_ID,
                "cluster_source_table": SOURCE_TABLE,
                "cluster_result_table": RESULT_TABLE,
            }
        ),
        secret_encrypted=None,  # delegated mode stores no secret
        status="connected",
    )
    db_session.add(connection)

    environment = Environment(
        id="env-delegated",
        customer_id=customer.id,
        connection_id=connection.id,
        name="acelo demo",
        platform="fabric",
        auth_mode="user",
        workspace_id=WORKSPACE_ID,
        workspace_name="acelo demo",
        status="environment_ready",
        provisioning_status=provisioning_service.INSTALLED,
    )
    db_session.add(environment)
    db_session.add(
        Resource(
            environment_id=environment.id,
            platform="fabric",
            resource_type="Notebook",
            display_name="ACELO Cluster Optimization",
            platform_resource_id=CLUSTER_NOTEBOOK_ID,
            status=provisioning_service.ACELO_OWNED,
            detail_json=json.dumps({"domain": "cluster", "managed_by": "acelo"}),
        )
    )
    db_session.commit()
    return connection, environment


def _start_route():
    return (
        f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items/"
        f"{CLUSTER_NOTEBOOK_ID}/jobs/instances"
    )


# --------------------------------------------------------------------------
# The token reaches the adapter
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_delegated_token_reaches_the_adapter_on_cluster_start(
    db_session, customer, delegated_setup
):
    connection, _ = delegated_setup

    route = respx.post(_start_route()).mock(
        return_value=httpx.Response(
            202,
            headers={
                "Location": f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items/"
                f"{CLUSTER_NOTEBOOK_ID}/jobs/instances/real-fabric-job-9"
            },
        )
    )

    analysis_job = AnalysisJob(
        id="job-1",
        customer_id=customer.id,
        connection_id=connection.id,
        request="Analyze my clusters",
        intent="cluster",
        platform="fabric",
        status="QUEUED",
    )
    db_session.add(analysis_job)
    db_session.commit()
    job_run = job_service.create_job_run(db_session, analysis_job, "cluster")

    await job_service.start_job_run(db_session, connection, job_run, USER_TOKEN)

    assert route.called
    # The user's token is what authenticated the call.
    assert route.calls[0].request.headers["Authorization"] == f"Bearer {USER_TOKEN}"
    # ...and a REAL Fabric job id came back.
    assert job_run.platform_run_id == "real-fabric-job-9"
    assert job_run.status == "STARTING"
    assert job_run.error_code is None


@pytest.mark.asyncio
@respx.mock
async def test_cluster_uses_the_registered_notebook_and_workspace(
    db_session, customer, delegated_setup
):
    """Execution must resolve the provisioned resource, not a hardcoded id."""
    connection, _ = delegated_setup
    route = respx.post(_start_route()).mock(
        return_value=httpx.Response(
            202,
            headers={
                "Location": f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items/"
                f"{CLUSTER_NOTEBOOK_ID}/jobs/instances/real-fabric-job-9"
            },
        )
    )

    analysis_job = AnalysisJob(
        id="job-2", customer_id=customer.id, connection_id=connection.id,
        request="Analyze my clusters", intent="cluster", platform="fabric", status="QUEUED",
    )
    db_session.add(analysis_job)
    db_session.commit()
    job_run = job_service.create_job_run(db_session, analysis_job, "cluster")

    await job_service.start_job_run(db_session, connection, job_run, USER_TOKEN)

    # The URL itself carries the registered notebook and the customer workspace.
    assert CLUSTER_NOTEBOOK_ID in str(route.calls[0].request.url)
    assert WORKSPACE_ID in str(route.calls[0].request.url)
    assert job_run.platform_resource_id == CLUSTER_NOTEBOOK_ID

    # And the demo tables travel as runtime parameters.
    sent = json.loads(route.calls[0].request.content)["executionData"]["parameters"]
    assert sent["source_table"]["value"] == SOURCE_TABLE
    assert sent["result_table"]["value"] == RESULT_TABLE
    assert sent["acelo_run_id"]["value"] == job_run.id


@respx.mock
def test_post_jobs_forwards_the_header_to_fabric(client, db_session, customer, delegated_setup):
    """End to end through the HTTP API — the header must survive the whole path."""
    connection, _ = delegated_setup
    route = respx.post(_start_route()).mock(
        return_value=httpx.Response(
            202,
            headers={
                "Location": f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items/"
                f"{CLUSTER_NOTEBOOK_ID}/jobs/instances/real-fabric-job-9"
            },
        )
    )

    response = client.post(
        "/api/jobs",
        json={"connection_id": connection.id, "prompt": "Analyze my clusters"},
        headers={"X-Customer-Id": customer.id, "X-Fabric-Access-Token": USER_TOKEN},
    )

    assert response.status_code == 200
    assert route.called
    assert route.calls[0].request.headers["Authorization"] == f"Bearer {USER_TOKEN}"

    cluster = next(r for r in response.json()["job_runs"] if r["domain"] == "cluster")
    assert cluster["platform_run_id"] == "real-fabric-job-9"
    assert cluster["status"] == "STARTING"


@respx.mock
def test_token_reaches_polling_and_results(client, db_session, customer, delegated_setup):
    """Poll-on-read is the only thing that advances a delegated run."""
    connection, _ = delegated_setup
    analysis_job = AnalysisJob(
        id="job-3", customer_id=customer.id, connection_id=connection.id,
        request="Analyze my clusters", intent="cluster", platform="fabric", status="RUNNING",
    )
    db_session.add(analysis_job)
    db_session.add(
        JobRun(
            id="run-3", analysis_job_id="job-3", domain="cluster", platform="fabric",
            platform_run_id="real-fabric-job-9", status="RUNNING",
            platform_resource_id=CLUSTER_NOTEBOOK_ID,
        )
    )
    db_session.commit()

    status_route = respx.get(
        f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items/"
        f"{CLUSTER_NOTEBOOK_ID}/jobs/instances/real-fabric-job-9"
    ).mock(return_value=httpx.Response(200, json={"id": "real-fabric-job-9", "status": "Completed"}))

    response = client.get(
        "/api/jobs/job-3",
        headers={"X-Customer-Id": customer.id, "X-Fabric-Access-Token": USER_TOKEN},
    )

    assert status_route.called
    assert status_route.calls[0].request.headers["Authorization"] == f"Bearer {USER_TOKEN}"
    assert response.json()["job_runs"][0]["status"] == "COMPLETED"


# --------------------------------------------------------------------------
# Missing token -> typed error, never fake success
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_missing_token_fails_with_a_typed_authentication_error(
    db_session, customer, delegated_setup
):
    connection, _ = delegated_setup
    route = respx.post(_start_route())

    analysis_job = AnalysisJob(
        id="job-4", customer_id=customer.id, connection_id=connection.id,
        request="Analyze my clusters", intent="cluster", platform="fabric", status="QUEUED",
    )
    db_session.add(analysis_job)
    db_session.commit()
    job_run = job_service.create_job_run(db_session, analysis_job, "cluster")

    # No token supplied, and delegated connections hold no secret.
    await job_service.start_job_run(db_session, connection, job_run)

    assert job_run.status == "FAILED"
    assert job_run.error_code in (
        ErrorCode.AUTHENTICATION_FAILED,
        ErrorCode.INVALID_CONFIGURATION,
    )
    # No fake run id, and Fabric was never called without a credential.
    assert job_run.platform_run_id is None
    assert not route.called


# --------------------------------------------------------------------------
# The token is never persisted, logged or returned
# --------------------------------------------------------------------------

@respx.mock
def test_token_never_persisted_logged_or_returned(
    client, db_session, customer, delegated_setup, caplog
):
    import logging

    caplog.set_level(logging.DEBUG)
    connection, _ = delegated_setup
    respx.post(_start_route()).mock(
        return_value=httpx.Response(
            202,
            headers={
                "Location": f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items/"
                f"{CLUSTER_NOTEBOOK_ID}/jobs/instances/real-fabric-job-9"
            },
        )
    )

    response = client.post(
        "/api/jobs",
        json={"connection_id": connection.id, "prompt": "Analyze my clusters"},
        headers={"X-Customer-Id": customer.id, "X-Fabric-Access-Token": USER_TOKEN},
    )

    assert USER_TOKEN not in response.text
    assert USER_TOKEN not in caplog.text
    assert "Bearer" not in caplog.text

    db_session.expire_all()
    for run in db_session.query(JobRun).all():
        assert USER_TOKEN not in (run.result_json or "")
        assert USER_TOKEN not in (run.error or "")
    stored = db_session.query(Connection).filter(Connection.id == connection.id).first()
    assert stored.secret_encrypted is None
    assert USER_TOKEN not in (stored.auth_metadata or "")


def test_token_does_not_bypass_customer_isolation(client, db_session, customer, delegated_setup):
    connection, _ = delegated_setup
    other = Customer(id=str(uuid.uuid4()), name="Other Co")
    db_session.add(other)
    db_session.commit()

    response = client.post(
        "/api/jobs",
        json={"connection_id": connection.id, "prompt": "Analyze my clusters"},
        headers={"X-Customer-Id": other.id, "X-Fabric-Access-Token": USER_TOKEN},
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------
# Background worker must not kill a delegated run
# --------------------------------------------------------------------------

def test_background_worker_skips_delegated_runs(db_session, customer, delegated_setup):
    """
    The worker has no user token. Polling a delegated run there would
    authenticate as nobody and fail a healthy run seconds after it started.
    """
    from services import execution_worker

    connection, _ = delegated_setup
    analysis_job = AnalysisJob(
        id="job-5", customer_id=customer.id, connection_id=connection.id,
        request="Analyze my clusters", intent="cluster", platform="fabric", status="RUNNING",
    )
    db_session.add(analysis_job)
    db_session.commit()
    job_run = job_service.create_job_run(db_session, analysis_job, "cluster")

    assert execution_worker.can_poll_unattended(db_session, job_run) is False


def test_background_worker_still_polls_service_principal_runs(db_session, customer):
    """Service-principal environments keep unattended background polling."""
    from services import execution_worker
    from services.crypto import get_cipher

    connection = Connection(
        id="conn-sp", customer_id=customer.id, platform="fabric", workspace="w",
        endpoint="https://api.fabric.microsoft.com/v1", auth_method="service_principal",
        auth_metadata=json.dumps({"workspace_id": WORKSPACE_ID}),
        secret_encrypted=get_cipher().encrypt("sp-secret"), status="connected",
    )
    db_session.add(connection)
    analysis_job = AnalysisJob(
        id="job-6", customer_id=customer.id, connection_id=connection.id,
        request="Analyze my clusters", intent="cluster", platform="fabric", status="RUNNING",
    )
    db_session.add(analysis_job)
    db_session.commit()
    job_run = job_service.create_job_run(db_session, analysis_job, "cluster")

    assert execution_worker.can_poll_unattended(db_session, job_run) is True


# --------------------------------------------------------------------------
# Query / Storage unaffected
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_query_and_storage_remain_unsupported_and_do_not_block_cluster(
    db_session, customer, delegated_setup
):
    connection, _ = delegated_setup
    route = respx.post(url__regex=rf"{FABRIC_BASE}/.*")

    analysis_job = AnalysisJob(
        id="job-7", customer_id=customer.id, connection_id=connection.id,
        request="Analyze everything", intent="all", platform="fabric", status="QUEUED",
    )
    db_session.add(analysis_job)
    db_session.commit()

    for domain in ("query", "storage"):
        run = job_service.create_job_run(db_session, analysis_job, domain)
        await job_service.start_job_run(db_session, connection, run, USER_TOKEN)
        # No notebook registered for these domains -> controlled, typed refusal.
        assert run.status == "FAILED"
        assert run.error_code == ErrorCode.NOTEBOOK_NOT_CONFIGURED
        assert run.platform_run_id is None

    # And nothing was sent to Fabric for them.
    assert not route.called
