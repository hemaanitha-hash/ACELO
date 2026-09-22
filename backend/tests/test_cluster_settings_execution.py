"""
Cluster Settings: persistence, execution-type selection, and configuration
honesty (no placeholders, missing is null — never a sample value or 0).

The UI, the persisted settings and the ACTUAL execution path must agree: these
tests save through the API, re-read through a fresh request, then run the
agent and assert which Fabric item was really called.
"""

import base64
import contextlib
import io
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from database import SessionLocal, get_db
from main import app
from models import Connection, Environment, Resource
from services import package_registry, provisioning_service
from services.file_analysis_service import summarise

FABRIC_BASE = "https://api.fabric.microsoft.com/v1"
WORKSPACE_ID = "0a0a0a0a-1111-2222-3333-444444444444"
NOTEBOOK_ID = "5b5b5b5b-6666-7777-8888-999999999999"
ACELO_PIPELINE_ID = "7c7c7c7c-1111-2222-3333-444444444444"
OTHER_PIPELINE_ID = "8d8d8d8d-1111-2222-3333-444444444444"
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
def env(db_session, customer):
    connection = Connection(
        id="conn-cs", customer_id=customer.id, platform="fabric", workspace="acelo demo",
        endpoint=FABRIC_BASE, auth_method="delegated",
        auth_metadata=json.dumps({"workspace_id": WORKSPACE_ID}),
        secret_encrypted=None, status="connected",
    )
    environment = Environment(
        id="env-cs", customer_id=customer.id, connection_id=connection.id, name="acelo demo",
        platform="fabric", auth_mode="user", workspace_id=WORKSPACE_ID, workspace_name="acelo demo",
        status="environment_ready", provisioning_status=provisioning_service.INSTALLED,
        last_verified_at=datetime.utcnow(), last_discovered_at=datetime.utcnow(),
    )
    db_session.add_all([connection, environment])
    db_session.add_all([
        Resource(environment_id=environment.id, platform="fabric", resource_type="Notebook",
                 display_name="ACELO Cluster Optimization", platform_resource_id=NOTEBOOK_ID,
                 status=provisioning_service.ACELO_OWNED,
                 detail_json=json.dumps({"domain": "cluster", "managed_by": "acelo"})),
        Resource(environment_id=environment.id, platform="fabric", resource_type="DataPipeline",
                 display_name="ACELO_Cluster_Optimization_Pipeline", platform_resource_id=ACELO_PIPELINE_ID,
                 status=provisioning_service.ACELO_OWNED,
                 detail_json=json.dumps({"pipeline_for": "cluster", "managed_by": "acelo"})),
        Resource(environment_id=environment.id, platform="fabric", resource_type="DataPipeline",
                 display_name="Customer_Own_Pipeline", platform_resource_id=OTHER_PIPELINE_ID,
                 status="discovered"),
    ])
    db_session.commit()
    return connection, environment


# The intended configuration for this environment (supplied per environment,
# never hardcoded in ACELO).
INTENDED = {
    "source_table": "realistic_cluster_dataset",
    "result_table": "acelo_cluster_recommendations",
    "lakehouse_database": "Data",
    "source_schema": "dbo",
    "result_schema": "dbo",
    "execution_type": "pipeline",
}


def _url(environment):
    return f"/api/environments/{environment.id}/cluster-settings"


def _headers(customer):
    return {"X-Customer-Id": customer.id}


def _fresh_get(customer, environment):
    """A brand-new session and client: nothing can come from in-memory state."""
    session = SessionLocal()
    previous = app.dependency_overrides.get(get_db)
    try:
        def _override():
            yield session

        app.dependency_overrides[get_db] = _override
        return TestClient(app).get(_url(environment), headers=_headers(customer)).json()
    finally:
        session.close()
        if previous is not None:
            app.dependency_overrides[get_db] = previous


# --- 1. save really persists -------------------------------------------------------

def test_save_then_reload_returns_every_persisted_value(client, customer, env):
    _, environment = env
    loaded = client.get(_url(environment), headers=_headers(customer)).json()
    assert loaded["settings"]["execution_type"] is None  # never saved yet

    saved = client.put(_url(environment), json=INTENDED, headers=_headers(customer))
    assert saved.status_code == 200

    reloaded = _fresh_get(customer, environment)
    for field, value in INTENDED.items():
        assert reloaded["settings"][field] == value, field
    assert reloaded["missing"] == []


def test_execution_type_change_survives_reload_and_does_not_revert(client, customer, env):
    _, environment = env
    client.put(_url(environment), json=INTENDED, headers=_headers(customer))
    assert _fresh_get(customer, environment)["settings"]["execution_type"] == "pipeline"

    client.put(_url(environment), json={"execution_type": "notebook"}, headers=_headers(customer))
    assert _fresh_get(customer, environment)["settings"]["execution_type"] == "notebook"


def test_partial_save_leaves_other_fields_untouched(client, customer, env):
    _, environment = env
    client.put(_url(environment), json=INTENDED, headers=_headers(customer))
    client.put(_url(environment), json={"execution_type": "notebook"}, headers=_headers(customer))
    settings = _fresh_get(customer, environment)["settings"]
    assert settings["source_table"] == "realistic_cluster_dataset"
    assert settings["source_schema"] == "dbo"


# --- 2. pipeline selection is persisted and drives execution ---------------------------

def test_pipeline_defaults_to_the_acelo_pipeline_and_lists_workspace_pipelines(client, customer, env):
    _, environment = env
    body = client.put(_url(environment), json=INTENDED, headers=_headers(customer)).json()
    assert body["execution"] == {
        "execution_type": "pipeline",
        "pipeline": {"id": ACELO_PIPELINE_ID, "name": "ACELO_Cluster_Optimization_Pipeline",
                     "source": "acelo-managed"},
    }
    assert {p["name"] for p in body["pipelines"]} == {
        "ACELO_Cluster_Optimization_Pipeline", "Customer_Own_Pipeline",
    }


def test_selected_pipeline_id_persists(client, customer, env):
    _, environment = env
    client.put(_url(environment), json={**INTENDED, "pipeline_id": OTHER_PIPELINE_ID}, headers=_headers(customer))
    reloaded = _fresh_get(customer, environment)
    assert reloaded["settings"]["pipeline_id"] == OTHER_PIPELINE_ID
    assert reloaded["execution"]["pipeline"] == {
        "id": OTHER_PIPELINE_ID, "name": "Customer_Own_Pipeline", "source": "configured",
    }


def test_unknown_pipeline_id_is_rejected(client, customer, env):
    _, environment = env
    resp = client.put(
        _url(environment), json={"pipeline_id": "12345678-1111-2222-3333-444444444444"},
        headers=_headers(customer),
    )
    assert resp.status_code == 422
    assert "not found in this workspace" in resp.json()["detail"]


def test_pipeline_selected_with_no_pipeline_is_missing_configuration(client, customer, env, db_session):
    _, environment = env
    db_session.query(Resource).filter(Resource.resource_type == "DataPipeline").delete()
    db_session.commit()
    body = client.put(_url(environment), json=INTENDED, headers=_headers(customer)).json()
    assert body["missing"] == ["pipeline"]
    readiness = client.get(f"/api/environments/{environment.id}/readiness", headers=_headers(customer)).json()
    assert readiness["cluster_ready_for_analysis"] is False
    assert readiness["cluster_blocked_reason"] == "Missing Cluster settings: pipeline."


def _run(client, customer, connection):
    return client.post(
        "/api/jobs",
        json={"connection_id": connection.id, "prompt": "Check my cluster utilization"},
        headers={"X-Customer-Id": customer.id, "X-Fabric-Access-Token": USER_TOKEN},
    ).json()["job_runs"][0]


def _jobs_url(item_id):
    return f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items/{item_id}/jobs/instances"


@respx.mock
def test_saved_pipeline_setting_is_what_actually_executes(client, customer, env):
    connection, environment = env
    client.put(_url(environment), json=INTENDED, headers=_headers(customer))
    pipeline = respx.post(_jobs_url(ACELO_PIPELINE_ID)).mock(
        return_value=httpx.Response(202, headers={"Location": f"{_jobs_url(ACELO_PIPELINE_ID)}/{FABRIC_RUN_ID}"})
    )
    notebook = respx.post(_jobs_url(NOTEBOOK_ID))

    run = _run(client, customer, connection)

    assert pipeline.called and not notebook.called
    assert pipeline.calls[0].request.url.params["jobType"] == "Pipeline"
    sent = json.loads(pipeline.calls[0].request.content)["executionData"]["parameters"]
    assert sent == {
        "acelo_run_id": run["id"],
        "environment_id": environment.id,
        "source_table": "realistic_cluster_dataset",
        "result_table": "acelo_cluster_recommendations",
        "source_lakehouse": "Data",
        "result_lakehouse": "Data",
        "source_schema": "dbo",
        "result_schema": "dbo",
    }
    assert run["execution_type"] == "pipeline"
    assert run["platform_run_id"] == FABRIC_RUN_ID


@respx.mock
def test_user_selected_pipeline_wins_over_the_acelo_one(client, customer, env):
    connection, environment = env
    client.put(_url(environment), json={**INTENDED, "pipeline_id": OTHER_PIPELINE_ID}, headers=_headers(customer))
    chosen = respx.post(_jobs_url(OTHER_PIPELINE_ID)).mock(
        return_value=httpx.Response(202, headers={"Location": f"{_jobs_url(OTHER_PIPELINE_ID)}/{FABRIC_RUN_ID}"})
    )
    acelo = respx.post(_jobs_url(ACELO_PIPELINE_ID))
    run = _run(client, customer, connection)
    assert chosen.called and not acelo.called
    assert run["platform_resource_id"] == OTHER_PIPELINE_ID


@respx.mock
def test_saved_notebook_setting_uses_the_notebook_path(client, customer, env):
    connection, environment = env
    client.put(_url(environment), json={**INTENDED, "execution_type": "notebook"}, headers=_headers(customer))
    notebook = respx.post(_jobs_url(NOTEBOOK_ID)).mock(
        return_value=httpx.Response(202, headers={"Location": f"{_jobs_url(NOTEBOOK_ID)}/{FABRIC_RUN_ID}"})
    )
    pipeline = respx.post(_jobs_url(ACELO_PIPELINE_ID))
    run = _run(client, customer, connection)
    assert notebook.called and not pipeline.called
    assert notebook.calls[0].request.url.params["jobType"] == "RunNotebook"
    assert run["execution_type"] == "notebook"


# --- 3. no dummy values, missing is null --------------------------------------------------

@pytest.mark.parametrize(
    "field,value",
    [
        ("sql_endpoint", "xxxx.datawarehouse.fabric.microsoft.com"),
        ("sql_endpoint", "your-endpoint.datawarehouse.fabric.microsoft.com"),
        ("sql_endpoint", "0"),
        ("fabric_environment_id", "00000000-0000-0000-0000-000000000000"),
        ("pipeline_id", "00000000-0000-0000-0000-000000000000"),
        ("source_table", "placeholder_table"),
        ("lakehouse_database", "null"),
    ],
)
def test_placeholder_values_are_rejected_not_stored(client, customer, env, field, value):
    _, environment = env
    resp = client.put(_url(environment), json={field: value}, headers=_headers(customer))
    assert resp.status_code == 422
    assert _fresh_get(customer, environment)["settings"][field] is None


def test_empty_optional_values_are_stored_as_null(client, customer, env):
    _, environment = env
    client.put(_url(environment), json={"sql_endpoint": "real.datawarehouse.fabric.microsoft.com"}, headers=_headers(customer))
    client.put(
        _url(environment),
        json={"sql_endpoint": "", "fabric_environment_id": "", "pipeline_id": "   "},
        headers=_headers(customer),
    )
    settings = _fresh_get(customer, environment)["settings"]
    assert settings["sql_endpoint"] is None
    assert settings["fabric_environment_id"] is None
    assert settings["pipeline_id"] is None
    stored = json.loads(env[0].auth_metadata)
    assert "sql_endpoint" not in stored


def test_unconfigured_settings_are_null_never_zero_or_empty_string(client, customer, env):
    _, environment = env
    settings = client.get(_url(environment), headers=_headers(customer)).json()["settings"]
    assert all(value is None for value in settings.values()), settings


def test_missing_metrics_are_null_not_zero():
    """File-analysis summary: a value no row carries is None, a real 0 stays 0."""
    no_cost = summarise([{"optimization_label": "Optimized"}])
    assert no_cost["current_cost_usd"] is None
    assert no_cost["estimated_monthly_savings_usd"] is None
    assert no_cost["avg_cpu_util"] is None
    real_zero = summarise([{"total_dbus_cost_usd": 0.0, "potential_monthly_savings": 0.0}])
    assert real_zero["current_cost_usd"] == 0.0
    assert real_zero["estimated_monthly_savings_usd"] == 0.0


# --- 6. notebook resolves dbo.<table>, never Data.<table> ------------------------------------

NOTEBOOK = Path(__file__).resolve().parents[1] / "optimization_package" / "cluster" / "Clusterfabric.ipynb"


def _resolve(**params):
    cells = ["".join(c["source"]) for c in json.loads(NOTEBOOK.read_text(encoding="utf-8"))["cells"]
             if c["cell_type"] == "code"]
    scope: dict = {}
    with contextlib.redirect_stdout(io.StringIO()):
        exec(next(c for c in cells if 'acelo_run_id = ""' in c), scope)  # noqa: S102
        scope.update(params)
        exec(next(c for c in cells if "_missing_params = [" in c), scope)  # noqa: S102
    return scope["SOURCE_TABLE"], scope["RESULT_TABLE"]


def test_schema_enabled_lakehouse_resolves_to_dbo_tables():
    assert _resolve(
        acelo_run_id="r", source_table="realistic_cluster_dataset", result_table="acelo_cluster_recommendations",
        source_lakehouse="Data", result_lakehouse="Data", source_schema="dbo", result_schema="dbo",
    ) == ("dbo.realistic_cluster_dataset", "dbo.acelo_cluster_recommendations")


@pytest.mark.parametrize(
    "params,expected",
    [
        # Already qualified: used exactly as given, never double-prefixed.
        ({"source_table": "dbo.realistic_cluster_dataset", "source_lakehouse": "Data"}, "dbo.realistic_cluster_dataset"),
        ({"source_table": "Data.dbo.realistic_cluster_dataset", "source_lakehouse": "Data"}, "Data.dbo.realistic_cluster_dataset"),
        # No schema configured: previous behaviour for non-schema lakehouses.
        ({"source_table": "realistic_cluster_dataset", "source_lakehouse": "Data"}, "Data.realistic_cluster_dataset"),
        ({"source_table": "realistic_cluster_dataset"}, "realistic_cluster_dataset"),
    ],
)
def test_table_qualification_rules(params, expected):
    source, _ = _resolve(acelo_run_id="r", result_table="out", **params)
    assert source == expected
    assert "Data.Data." not in source


# --- redeploy keeps bindings configured in Fabric ---------------------------------------------

def _asset():
    return package_registry.load_package().asset_for("cluster")


def _deps(payload):
    return json.loads(base64.b64decode(payload))["metadata"].get("dependencies", {})


def test_redeploy_keeps_a_lakehouse_attached_in_fabric():
    attached = {"lakehouse": {"default_lakehouse": "lh-data", "default_lakehouse_name": "Data",
                              "default_lakehouse_workspace_id": WORKSPACE_ID}}
    payload = provisioning_service.notebook_payload(_asset(), None, None, attached)
    assert _deps(payload)["lakehouse"]["default_lakehouse"] == "lh-data"


def test_acelo_resolved_binding_wins_over_the_existing_one():
    attached = {"lakehouse": {"default_lakehouse": "old"}, "environment": {"environmentId": "keep-me"}}
    lakehouse = {"id": "lh-new", "name": "Data", "workspace_id": WORKSPACE_ID}
    deps = _deps(provisioning_service.notebook_payload(_asset(), lakehouse, None, attached))
    assert deps["lakehouse"]["default_lakehouse"] == "lh-new"
    assert deps["environment"] == {"environmentId": "keep-me"}


@pytest.mark.asyncio
async def test_deploy_reads_existing_bindings_before_updating():
    attached = {"lakehouse": {"default_lakehouse": "lh-data"}}
    adapter = MagicMock()
    adapter.find_notebook_by_name = AsyncMock(return_value={"id": NOTEBOOK_ID, "displayName": "x"})
    adapter.get_notebook_dependencies = AsyncMock(return_value=attached)
    adapter.update_notebook_definition = AsyncMock()
    asset = _asset()

    await provisioning_service._deploy_asset(adapter, MagicMock(), asset, None, False)

    adapter.get_notebook_dependencies.assert_awaited_with(NOTEBOOK_ID)
    sent_payload = adapter.update_notebook_definition.await_args.args[1]
    assert _deps(sent_payload)["lakehouse"]["default_lakehouse"] == "lh-data"


@pytest.mark.asyncio
async def test_unreadable_existing_bindings_never_block_a_deploy():
    adapter = MagicMock()
    adapter.find_notebook_by_name = AsyncMock(return_value={"id": NOTEBOOK_ID, "displayName": "x"})
    adapter.get_notebook_dependencies = AsyncMock(side_effect=RuntimeError("boom"))
    adapter.update_notebook_definition = AsyncMock()
    await provisioning_service._deploy_asset(adapter, MagicMock(), _asset(), None, False)
    adapter.update_notebook_definition.assert_awaited_once()


@respx.mock
@pytest.mark.asyncio
async def test_adapter_reads_notebook_dependencies_from_fabric():
    from platforms.fabric import FabricAdapter

    notebook = {"cells": [], "metadata": {"dependencies": {"lakehouse": {"default_lakehouse": "lh-data"}}}}
    payload = base64.b64encode(json.dumps(notebook).encode()).decode()
    route = respx.post(
        f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/notebooks/{NOTEBOOK_ID}/getDefinition"
    ).mock(return_value=httpx.Response(200, json={"definition": {"parts": [
        {"path": "notebook-content.ipynb", "payload": payload, "payloadType": "InlineBase64"}
    ]}}))
    adapter = FabricAdapter(endpoint=FABRIC_BASE, auth_metadata={"workspace_id": WORKSPACE_ID}, secret=None)
    adapter.delegated_mode = True
    adapter.delegated_token = USER_TOKEN

    deps = await adapter.get_notebook_dependencies(NOTEBOOK_ID)

    assert route.calls[0].request.url.params["format"] == "ipynb"
    assert deps == {"lakehouse": {"default_lakehouse": "lh-data"}}
