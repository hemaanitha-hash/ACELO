"""
Fabric Data Pipeline orchestration of the SAME Cluster notebook, and the Fabric
Environment that supplies its Spark libraries (xgboost).

The pipeline is an additional execution path: direct RunNotebook stays the
default and is unchanged. Every id comes from the Resource table or stored
configuration — none is hardcoded.
"""

import base64
import json
import logging
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from database import get_db
from main import app
from models import Connection, Environment, Resource
from platforms.fabric import FabricAdapter
from services import package_registry, provisioning_service

FABRIC_BASE = "https://api.fabric.microsoft.com/v1"
WORKSPACE_ID = "0a0a0a0a-1111-2222-3333-444444444444"
NOTEBOOK_ID = "5b5b5b5b-6666-7777-8888-999999999999"
PIPELINE_ID = "7c7c7c7c-1111-2222-3333-444444444444"
FABRIC_RUN_ID = "f2d65699-dd22-4889-980c-15226deb0e1b"
USER_TOKEN = "eyJ-DELEGATED-FABRIC-TOKEN"


@pytest.fixture
def client(db_session):
    def _override():
        yield db_session

    app.dependency_overrides[get_db] = _override
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def fabric_env(db_session, customer):
    connection = Connection(
        id="conn-pipe", customer_id=customer.id, platform="fabric", workspace="acelo demo",
        endpoint=FABRIC_BASE, auth_method="delegated",
        auth_metadata=json.dumps({
            "workspace_id": WORKSPACE_ID,
            "cluster_source_table": "realistic_cluster_dataset",
            "cluster_result_table": "acelo_cluster_recommendations",
            "lakehouse_database": "Data",
        }),
        secret_encrypted=None, status="connected",
    )
    environment = Environment(
        id="env-pipe", customer_id=customer.id, connection_id=connection.id, name="acelo demo",
        platform="fabric", auth_mode="user", workspace_id=WORKSPACE_ID, workspace_name="acelo demo",
        status="environment_ready", provisioning_status=provisioning_service.INSTALLED,
        last_verified_at=datetime.utcnow(), last_discovered_at=datetime.utcnow(),
    )
    db_session.add_all([connection, environment])
    db_session.add(Resource(
        environment_id=environment.id, platform="fabric", resource_type="Notebook",
        display_name="ACELO Cluster Optimization", platform_resource_id=NOTEBOOK_ID,
        status=provisioning_service.ACELO_OWNED,
        detail_json=json.dumps({"domain": "cluster", "managed_by": "acelo"}),
    ))
    db_session.commit()
    return connection, environment


def _use_pipeline(db_session, connection, environment):
    metadata = json.loads(connection.auth_metadata)
    metadata["cluster_execution_type"] = "pipeline"
    connection.auth_metadata = json.dumps(metadata)
    db_session.add(Resource(
        environment_id=environment.id, platform="fabric", resource_type="DataPipeline",
        display_name="ACELO_Cluster_Optimization_Pipeline", platform_resource_id=PIPELINE_ID,
        status=provisioning_service.ACELO_OWNED,
        detail_json=json.dumps({"pipeline_for": "cluster", "managed_by": "acelo"}),
    ))
    db_session.commit()


def _run_agent(client, customer, connection):
    return client.post(
        "/api/jobs",
        json={"connection_id": connection.id, "prompt": "Check my cluster utilization"},
        headers={"X-Customer-Id": customer.id, "X-Fabric-Access-Token": USER_TOKEN},
    )


def _pipeline_url():
    return f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items/{PIPELINE_ID}/jobs/instances"


# --- pipeline definition -------------------------------------------------------------

def test_pipeline_definition_runs_the_existing_notebook_with_passthrough_parameters():
    definition = FabricAdapter.pipeline_definition(NOTEBOOK_ID, WORKSPACE_ID)
    [part] = definition["parts"]
    assert part["path"] == "pipeline-content.json"
    content = json.loads(base64.b64decode(part["payload"]))
    [activity] = content["properties"]["activities"]
    assert activity["type"] == "TridentNotebook"
    assert activity["typeProperties"]["notebookId"] == NOTEBOOK_ID
    assert activity["typeProperties"]["workspaceId"] == WORKSPACE_ID
    for name in ("acelo_run_id", "environment_id", "source_lakehouse", "source_table",
                 "result_lakehouse", "result_table"):
        assert name in content["properties"]["parameters"]
        forwarded = activity["typeProperties"]["parameters"][name]["value"]
        assert forwarded == {"value": f"@pipeline().parameters.{name}", "type": "Expression"}


# --- execution ---------------------------------------------------------------------

@respx.mock
def test_pipeline_execution_runs_the_pipeline_with_plain_parameters(client, customer, fabric_env, db_session, caplog):
    connection, environment = fabric_env
    _use_pipeline(db_session, connection, environment)
    route = respx.post(_pipeline_url()).mock(
        return_value=httpx.Response(202, headers={"Location": f"{_pipeline_url()}/{FABRIC_RUN_ID}"})
    )
    caplog.set_level(logging.INFO)

    run = _run_agent(client, customer, connection).json()["job_runs"][0]

    assert route.called
    assert route.calls[0].request.url.params["jobType"] == "Pipeline"
    sent = json.loads(route.calls[0].request.content)["executionData"]["parameters"]
    assert sent["acelo_run_id"] == run["id"]
    assert sent["environment_id"] == environment.id
    assert sent["source_table"] == "realistic_cluster_dataset"
    assert sent["result_table"] == "acelo_cluster_recommendations"
    assert sent["source_lakehouse"] == sent["result_lakehouse"] == "Data"
    assert run["execution_type"] == "pipeline"
    assert run["platform_resource_id"] == PIPELINE_ID
    assert run["platform_run_id"] == FABRIC_RUN_ID  # Fabric's own id, from Location
    request = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("[FABRIC_EXECUTION_REQUEST]"))
    assert "execution_type=pipeline" in request
    assert f"pipeline_id={PIPELINE_ID}" in request and f"notebook_id={NOTEBOOK_ID}" in request


@respx.mock
def test_pipeline_runs_are_polled_on_the_pipeline_item(client, customer, fabric_env, db_session):
    connection, environment = fabric_env
    _use_pipeline(db_session, connection, environment)
    respx.post(_pipeline_url()).mock(
        return_value=httpx.Response(202, headers={"Location": f"{_pipeline_url()}/{FABRIC_RUN_ID}"})
    )
    status = respx.get(f"{_pipeline_url()}/{FABRIC_RUN_ID}").mock(
        return_value=httpx.Response(200, json={"id": FABRIC_RUN_ID, "status": "InProgress"})
    )
    job = _run_agent(client, customer, connection).json()
    polled = client.get(
        f"/api/jobs/{job['id']}", headers={"X-Customer-Id": customer.id, "X-Fabric-Access-Token": USER_TOKEN}
    ).json()
    assert status.called
    assert polled["job_runs"][0]["status"] == "RUNNING"


@respx.mock
def test_notebook_execution_is_the_default_and_unchanged(client, customer, fabric_env):
    connection, _ = fabric_env
    notebook_url = f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items/{NOTEBOOK_ID}/jobs/instances"
    route = respx.post(notebook_url).mock(
        return_value=httpx.Response(202, headers={"Location": f"{notebook_url}/{FABRIC_RUN_ID}"})
    )
    run = _run_agent(client, customer, connection).json()["job_runs"][0]
    assert route.calls[0].request.url.params["jobType"] == "RunNotebook"
    body = json.loads(route.calls[0].request.content)
    assert body["executionData"]["parameters"]["source_table"] == {
        "value": "realistic_cluster_dataset", "type": "string",
    }
    assert run["execution_type"] == "notebook"
    assert run["platform_resource_id"] == NOTEBOOK_ID


def test_pipeline_selected_but_not_deployed_is_a_clear_error(client, customer, fabric_env, db_session):
    connection, _ = fabric_env
    metadata = json.loads(connection.auth_metadata)
    metadata["cluster_execution_type"] = "pipeline"
    connection.auth_metadata = json.dumps(metadata)
    db_session.commit()
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url__regex=r".*/jobs/instances.*")
        run = _run_agent(client, customer, connection).json()["job_runs"][0]
    assert not route.called
    assert run["status"] == "FAILED"
    assert "no pipeline is registered" in run["error"]


# --- provisioning ----------------------------------------------------------------------

def test_pipeline_registration_never_shadows_the_notebook(db_session, fabric_env):
    connection, environment = fabric_env
    _use_pipeline(db_session, connection, environment)
    assert provisioning_service.resolved_domains(db_session, environment) == {"cluster": NOTEBOOK_ID}
    assert provisioning_service.resolved_pipelines(db_session, environment) == {"cluster": PIPELINE_ID}


def _fake_adapter(existing):
    adapter = MagicMock()
    adapter.pipeline_definition = FabricAdapter.pipeline_definition
    adapter.find_item_by_name = AsyncMock(return_value=existing)
    adapter.update_item_definition = AsyncMock()
    adapter.create_item = AsyncMock(return_value={"id": "new-pipeline", "displayName": "ACELO_Cluster_Optimization_Pipeline"})
    return adapter


@pytest.mark.asyncio
async def test_deploy_pipeline_reuses_an_existing_pipeline_by_name():
    adapter = _fake_adapter({"id": PIPELINE_ID, "displayName": "ACELO_Cluster_Optimization_Pipeline"})
    item = await provisioning_service._deploy_pipeline(adapter, MagicMock(version="1.3.0"), NOTEBOOK_ID, WORKSPACE_ID, None)
    assert item == {"id": PIPELINE_ID, "displayName": "ACELO_Cluster_Optimization_Pipeline"}
    adapter.find_item_by_name.assert_awaited_with("DataPipeline", "ACELO_Cluster_Optimization_Pipeline")
    adapter.update_item_definition.assert_awaited_once()
    adapter.create_item.assert_not_awaited()  # no duplicates


@pytest.mark.asyncio
async def test_deploy_pipeline_creates_it_when_absent():
    adapter = _fake_adapter(None)
    item = await provisioning_service._deploy_pipeline(adapter, MagicMock(version="1.3.0"), NOTEBOOK_ID, WORKSPACE_ID, "folder-1")
    assert item["id"] == "new-pipeline"
    args, kwargs = adapter.create_item.await_args
    assert args[0] == "DataPipeline" and args[1] == "ACELO_Cluster_Optimization_Pipeline"
    content = json.loads(base64.b64decode(args[2]["parts"][0]["payload"]))
    assert content["properties"]["activities"][0]["typeProperties"]["notebookId"] == NOTEBOOK_ID
    assert kwargs["folder_id"] == "folder-1"


def test_register_pipeline_is_idempotent(db_session, fabric_env):
    _, environment = fabric_env
    package = MagicMock(version="1.3.0")
    for _ in range(2):
        provisioning_service._register_pipeline(
            db_session, environment, package, "cluster",
            {"id": PIPELINE_ID, "displayName": "ACELO_Cluster_Optimization_Pipeline"}, None,
        )
    pipelines = db_session.query(Resource).filter(Resource.resource_type == "DataPipeline").all()
    assert len(pipelines) == 1


# --- Fabric Environment (Spark libraries) ------------------------------------------------

def test_fabric_environment_attached_from_configuration(db_session, fabric_env):
    connection, environment = fabric_env
    env_id = "9e9e9e9e-1111-2222-3333-444444444444"
    metadata = json.loads(connection.auth_metadata)
    metadata["cluster_fabric_environment_id"] = env_id
    connection.auth_metadata = json.dumps(metadata)
    db_session.commit()

    spark_env = provisioning_service.fabric_environment(db_session, environment)
    assert spark_env["id"] == env_id and spark_env["source"] == "configured"
    asset = package_registry.load_package().asset_for("cluster")
    notebook = json.loads(base64.b64decode(provisioning_service.notebook_payload(asset, None, spark_env)))
    assert notebook["metadata"]["dependencies"]["environment"] == {
        "environmentId": env_id, "workspaceId": WORKSPACE_ID,
    }
    # Metadata only: the notebook code is deployed exactly as packaged.
    assert notebook["cells"] == json.loads(asset.read_bytes())["cells"]


def test_fabric_environment_discovered_by_acelo_name(db_session, fabric_env):
    _, environment = fabric_env
    assert provisioning_service.fabric_environment(db_session, environment) is None
    db_session.add(Resource(
        environment_id=environment.id, platform="fabric", resource_type="SparkEnvironment",
        display_name="ACELO_Cluster_Environment", platform_resource_id="env-item-1", status="discovered",
    ))
    db_session.commit()
    assert provisioning_service.fabric_environment(db_session, environment)["id"] == "env-item-1"


def test_provisioning_state_reports_execution_path(client, customer, fabric_env, db_session):
    connection, environment = fabric_env
    _use_pipeline(db_session, connection, environment)
    state = client.get(
        f"/api/environments/{environment.id}/provision/status", headers={"X-Customer-Id": customer.id}
    ).json()
    assert state["execution_type"] == "pipeline"
    assert state["pipeline"]["platform_resource_id"] == PIPELINE_ID
    assert state["pipeline"]["ready"] is True
    assert state["fabric_environment"] is None


@pytest.mark.parametrize("payload", [{"execution_type": "spark-submit"}, {"fabric_environment_id": "not-a-guid"}])
def test_new_cluster_settings_are_validated(client, customer, fabric_env, payload):
    _, environment = fabric_env
    resp = client.put(
        f"/api/environments/{environment.id}/cluster-settings", json=payload,
        headers={"X-Customer-Id": customer.id},
    )
    assert resp.status_code == 422


def test_new_cluster_settings_round_trip(client, customer, fabric_env):
    _, environment = fabric_env
    saved = client.put(
        f"/api/environments/{environment.id}/cluster-settings",
        json={"execution_type": "pipeline", "fabric_environment_id": "9e9e9e9e-1111-2222-3333-444444444444"},
        headers={"X-Customer-Id": customer.id},
    ).json()
    assert saved["settings"]["execution_type"] == "pipeline"
    assert saved["settings"]["fabric_environment_id"] == "9e9e9e9e-1111-2222-3333-444444444444"


def test_deployment_bindings_reach_the_api_response(client, customer, fabric_env, db_session):
    """REGRESSION: ProvisioningOut used to drop default_lakehouse, so the UI's
    'no lakehouse' warning could never reflect the backend."""
    _, environment = fabric_env
    db_session.add(Resource(
        environment_id=environment.id, platform="fabric", resource_type="Lakehouse",
        display_name="Data", platform_resource_id="lh-data", status="discovered",
    ))
    db_session.commit()
    state = client.get(
        f"/api/environments/{environment.id}/provision/status", headers={"X-Customer-Id": customer.id}
    ).json()
    assert state["default_lakehouse"] == {"id": "lh-data", "name": "Data", "workspace_id": WORKSPACE_ID, "source": "discovered"}
    assert state["execution_type"] == "notebook"
