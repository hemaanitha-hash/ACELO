"""
Spark default-Lakehouse context for pipeline-triggered Cluster runs.

"dbo.<table>" only resolves when the notebook has a DEFAULT Lakehouse bound;
source_lakehouse is just a parameter. These tests run the notebook's own
configuration cell against a simulated `notebookutils.runtime.context` and
prove ACELO binds, and verifies, the default Lakehouse at deploy time.
"""

import base64
import contextlib
import io
import json
import sys
import types
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from database import get_db
from main import app
from models import Connection, Environment, Resource
from services import package_registry, provisioning_service

NOTEBOOK = Path(__file__).resolve().parents[1] / "optimization_package" / "cluster" / "Clusterfabric.ipynb"
WORKSPACE_ID = "0a0a0a0a-1111-2222-3333-444444444444"
DATA_LAKEHOUSE_ID = "1b1b1b1b-2222-3333-4444-555555555555"
OTHER_WORKSPACE_ID = "2c2c2c2c-3333-4444-5555-666666666666"

PARAMS = dict(
    acelo_run_id="run-1", environment_id="env-1",
    source_table="realistic_cluster_dataset", result_table="acelo_cluster_recommendations",
    source_lakehouse="Data", result_lakehouse="Data", source_schema="dbo", result_schema="dbo",
)


def _cells():
    return ["".join(c["source"]) for c in json.loads(NOTEBOOK.read_text(encoding="utf-8"))["cells"]
            if c["cell_type"] == "code"]


def _run_config(monkeypatch, context, **overrides):
    """Runs the parameters + configuration cells with a simulated runtime context."""
    if context is None:
        monkeypatch.setitem(sys.modules, "notebookutils", None)  # import fails, as off-Fabric
    else:
        module = types.ModuleType("notebookutils")
        module.runtime = types.SimpleNamespace(context=context)
        monkeypatch.setitem(sys.modules, "notebookutils", module)
    cells = _cells()
    scope: dict = {}
    out = io.StringIO()
    error = None
    with contextlib.redirect_stdout(out):
        exec(next(c for c in cells if 'acelo_run_id = ""' in c), scope)  # noqa: S102
        scope.update(PARAMS, **overrides)
        try:
            exec(next(c for c in cells if "_missing_params = [" in c), scope)  # noqa: S102
        except RuntimeError as exc:
            error = exc
    return out.getvalue(), scope, error


def _pipeline_context(**overrides):
    # Fabric's runtime.context is a dict.
    context = {
        "currentNotebookName": "ACELO Cluster Optimization",
        "currentNotebookId": "nb-1",
        "currentWorkspaceId": WORKSPACE_ID,
        "defaultLakehouseName": "Data",
        "defaultLakehouseId": DATA_LAKEHOUSE_ID,
        "defaultLakehouseWorkspaceId": WORKSPACE_ID,
        "isForPipeline": True,
        "isForInteractive": False,
    }
    context.update(overrides)
    return context


# --- notebook: runtime context diagnostic + early failure ------------------------

def test_pipeline_run_with_default_lakehouse_proceeds(monkeypatch):
    out, scope, error = _run_config(monkeypatch, _pipeline_context())
    assert error is None
    assert "[NOTEBOOK_RUNTIME_CONTEXT]" in out
    assert "'defaultLakehouseName': 'Data'" in out
    assert f"'defaultLakehouseId': '{DATA_LAKEHOUSE_ID}'" in out
    assert "'isForPipeline': True" in out
    assert "lakehouse_context=ok default=Data" in out
    # Schema-aware names against the default context — never Data.dbo.<table>.
    assert scope["SOURCE_TABLE"] == "dbo.realistic_cluster_dataset"
    assert scope["RESULT_TABLE"] == "dbo.acelo_cluster_recommendations"


def test_attribute_style_context_is_also_read(monkeypatch):
    context = types.SimpleNamespace(**_pipeline_context())
    out, _, error = _run_config(monkeypatch, context)
    assert error is None
    assert "'defaultLakehouseName': 'Data'" in out


@pytest.mark.parametrize("empty", [None, ""])
def test_no_default_lakehouse_fails_early_and_clearly(monkeypatch, empty):
    out, _, error = _run_config(monkeypatch, _pipeline_context(defaultLakehouseName=empty))
    assert error is not None
    assert "ACELO Cluster notebook has no default Lakehouse" in str(error)
    assert "Attach and set the Data Lakehouse as the notebook default" in str(error)
    assert "reason=no_default_lakehouse" in out


def test_a_different_default_lakehouse_is_never_used_silently(monkeypatch):
    out, _, error = _run_config(monkeypatch, _pipeline_context(defaultLakehouseName="Staging"))
    assert error is not None
    assert "'Staging'" in str(error) and "'Data'" in str(error)
    assert "reason=default_lakehouse_mismatch" in out


def test_fully_qualified_tables_do_not_need_a_default_lakehouse(monkeypatch):
    _, _, error = _run_config(
        monkeypatch, _pipeline_context(defaultLakehouseName=None),
        source_table="Data.dbo.realistic_cluster_dataset", result_table="Data.dbo.acelo_cluster_recommendations",
    )
    assert error is None


def test_off_fabric_the_context_is_reported_unreadable_not_failed(monkeypatch):
    out, _, error = _run_config(monkeypatch, None)
    assert error is None
    assert "[NOTEBOOK_RUNTIME_CONTEXT] unable_to_read" in out


def test_context_check_runs_before_the_source_read():
    source = "\n".join(_cells())
    assert source.index("[NOTEBOOK_RUNTIME_CONTEXT]") < source.index("spark.read.table(SOURCE_TABLE)")


# --- provisioning binds and verifies the default Lakehouse -------------------------

@pytest.fixture
def env(db_session, customer):
    connection = Connection(
        id="conn-lh", customer_id=customer.id, platform="fabric", workspace="acelo demo",
        endpoint="https://api.fabric.microsoft.com/v1", auth_method="delegated",
        auth_metadata=json.dumps({"workspace_id": WORKSPACE_ID, "lakehouse_database": "Data"}),
        secret_encrypted=None, status="connected",
    )
    environment = Environment(
        id="env-lh", customer_id=customer.id, connection_id=connection.id, name="acelo demo",
        platform="fabric", auth_mode="user", workspace_id=WORKSPACE_ID, workspace_name="acelo demo",
        status="environment_ready", provisioning_status=provisioning_service.INSTALLED,
        last_verified_at=datetime.utcnow(), last_discovered_at=datetime.utcnow(),
    )
    db_session.add_all([connection, environment])
    db_session.commit()
    return connection, environment


def _configure(db_session, connection, **values):
    metadata = json.loads(connection.auth_metadata)
    metadata.update(values)
    connection.auth_metadata = json.dumps(metadata)
    db_session.commit()


def test_configured_lakehouse_is_bound_even_when_discovery_cannot_see_it(db_session, env):
    connection, environment = env
    assert provisioning_service.default_lakehouse(db_session, environment) is None  # discovery found none
    _configure(db_session, connection, cluster_lakehouse_id=DATA_LAKEHOUSE_ID)
    assert provisioning_service.default_lakehouse(db_session, environment) == {
        "id": DATA_LAKEHOUSE_ID, "name": "Data", "workspace_id": WORKSPACE_ID, "source": "configured",
    }


def test_lakehouse_in_another_workspace(db_session, env):
    connection, environment = env
    _configure(db_session, connection, cluster_lakehouse_id=DATA_LAKEHOUSE_ID,
               cluster_lakehouse_workspace_id=OTHER_WORKSPACE_ID)
    lakehouse = provisioning_service.default_lakehouse(db_session, environment)
    deps = json.loads(base64.b64decode(provisioning_service.notebook_payload(
        package_registry.load_package().asset_for("cluster"), lakehouse)))["metadata"]["dependencies"]
    assert deps["lakehouse"] == {
        "default_lakehouse": DATA_LAKEHOUSE_ID,
        "default_lakehouse_name": "Data",
        "default_lakehouse_workspace_id": OTHER_WORKSPACE_ID,
        "known_lakehouses": [{"id": DATA_LAKEHOUSE_ID}],
    }


def test_configured_id_wins_over_a_discovered_name(db_session, env):
    connection, environment = env
    db_session.add(Resource(environment_id=environment.id, platform="fabric", resource_type="Lakehouse",
                            display_name="Data", platform_resource_id="discovered-lh", status="discovered"))
    db_session.commit()
    _configure(db_session, connection, cluster_lakehouse_id=DATA_LAKEHOUSE_ID)
    assert provisioning_service.default_lakehouse(db_session, environment)["id"] == DATA_LAKEHOUSE_ID


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dependencies,status",
    [
        ({"lakehouse": {"default_lakehouse": DATA_LAKEHOUSE_ID, "default_lakehouse_name": "Data"}}, "VERIFIED"),
        ({"lakehouse": {"default_lakehouse": "someone-else"}}, "MISMATCH"),
        ({}, "MISSING"),
    ],
)
async def test_binding_is_read_back_from_fabric_after_deploy(dependencies, status):
    adapter = MagicMock()
    adapter.get_notebook_dependencies = AsyncMock(return_value=dependencies)
    expected = {"id": DATA_LAKEHOUSE_ID, "name": "Data", "workspace_id": WORKSPACE_ID}
    result = await provisioning_service._verify_lakehouse_binding(adapter, "nb-1", expected)
    assert result["status"] == status


@pytest.mark.asyncio
async def test_unreadable_binding_is_unverified_not_assumed():
    adapter = MagicMock()
    adapter.get_notebook_dependencies = AsyncMock(side_effect=RuntimeError("x"))
    result = await provisioning_service._verify_lakehouse_binding(adapter, "nb-1", None)
    assert result["status"] == "UNVERIFIED"


# --- settings -----------------------------------------------------------------------

@pytest.fixture
def client(db_session):
    def _override():
        yield db_session

    app.dependency_overrides[get_db] = _override
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_lakehouse_id_settings_round_trip(client, customer, env):
    _, environment = env
    url = f"/api/environments/{environment.id}/cluster-settings"
    body = client.put(url, json={"lakehouse_id": DATA_LAKEHOUSE_ID, "lakehouse_workspace_id": OTHER_WORKSPACE_ID},
                      headers={"X-Customer-Id": customer.id}).json()
    assert body["settings"]["lakehouse_id"] == DATA_LAKEHOUSE_ID
    assert body["settings"]["lakehouse_workspace_id"] == OTHER_WORKSPACE_ID
    state = client.get(f"/api/environments/{environment.id}/provision/status",
                       headers={"X-Customer-Id": customer.id}).json()
    assert state["default_lakehouse"]["id"] == DATA_LAKEHOUSE_ID


@pytest.mark.parametrize("value", ["Data", "Data.dbo", "00000000-0000-0000-0000-000000000000"])
def test_lakehouse_id_must_be_a_real_item_id(client, customer, env, value):
    _, environment = env
    resp = client.put(f"/api/environments/{environment.id}/cluster-settings", json={"lakehouse_id": value},
                      headers={"X-Customer-Id": customer.id})
    assert resp.status_code == 422
