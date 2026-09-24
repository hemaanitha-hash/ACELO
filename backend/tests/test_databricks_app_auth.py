"""
Databricks App identity.

Running as a native Databricks App, ACELO authenticates with the App's own
identity against the workspace it is deployed in. The user enters no workspace
URL, no access token and no platform.

What these tests protect:
  * a stored PAT still wins, so every existing configured connection is unchanged
  * with no stored secret, the App identity supplies the Authorization header
  * the workspace host comes from the runtime when none is configured
  * discovery and the agent capability keep working through that identity
  * no credential is stored, returned to the browser, or logged
  * outside an App nothing changes — the missing-credential error still fires
"""

import uuid

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from main import app
from models import Connection, Environment
from platforms import databricks_auth
from platforms.databricks import DatabricksAdapter
from platforms.databricks_resources import ResourceType
from platforms.errors import ErrorCode, PlatformError
from services.app_bootstrap import APP_AUTH_METHOD, ensure_app_environment

HOST = "https://adb-7405617514546966.6.azuredatabricks.net"
APP_TOKEN = "app-identity-oauth-token"
STORED_PAT = "dapi-stored-personal-token"

CLUSTERS_URL = f"{HOST}/api/2.0/clusters/list"
WAREHOUSES_URL = f"{HOST}/api/2.0/sql/warehouses"

RAW_WAREHOUSES = [
    {"id": "b1c2", "name": "Serverless Starter Warehouse", "state": "RUNNING", "warehouse_type": "PRO"},
    {"id": "a9b8", "name": "warehouse_db", "state": "STOPPED", "warehouse_type": "CLASSIC"},
]


@pytest.fixture(autouse=True)
def _clean_auth_cache():
    databricks_auth.reset_cache()
    yield
    databricks_auth.reset_cache()


@pytest.fixture
def app_runtime(monkeypatch):
    """ACELO running inside a Databricks App, with a working App identity."""
    monkeypatch.setenv("DATABRICKS_HOST", HOST)
    monkeypatch.setenv("DATABRICKS_APP_NAME", "acelo")
    monkeypatch.setattr(
        databricks_auth,
        "app_auth_headers",
        lambda: {"Authorization": f"Bearer {APP_TOKEN}"},
    )


def _mock_workspace() -> None:
    respx.get(CLUSTERS_URL).mock(return_value=httpx.Response(200, json={"clusters": []}))
    respx.get(WAREHOUSES_URL).mock(return_value=httpx.Response(200, json={"warehouses": RAW_WAREHOUSES}))


# --- runtime detection ------------------------------------------------------


def test_app_runtime_needs_both_a_host_and_an_app_marker(monkeypatch):
    monkeypatch.delenv("DATABRICKS_APP_NAME", raising=False)
    monkeypatch.delenv("DATABRICKS_APP_PORT", raising=False)
    monkeypatch.delenv("DATABRICKS_WORKSPACE_ID", raising=False)
    monkeypatch.setenv("DATABRICKS_HOST", HOST)

    # A developer with DATABRICKS_HOST exported for the CLI is NOT in an App.
    assert databricks_auth.is_app_runtime() is False

    monkeypatch.setenv("DATABRICKS_APP_NAME", "acelo")
    assert databricks_auth.is_app_runtime() is True


def test_host_is_normalised_to_an_https_origin(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", "adb-123.azuredatabricks.net/")
    assert databricks_auth.app_host() == "https://adb-123.azuredatabricks.net"


def test_describe_reports_the_mode_without_a_credential(monkeypatch, app_runtime):
    described = databricks_auth.describe()

    assert described["databricks_app"] is True
    assert described["auth_mode"] == "databricks_app_identity"
    assert described["workspace_host"] == HOST
    assert APP_TOKEN not in str(described)


# --- the adapter ------------------------------------------------------------


def test_a_stored_pat_still_wins(app_runtime):
    """Existing configured connections must behave exactly as before."""
    adapter = DatabricksAdapter(endpoint=HOST, auth_metadata={}, secret=STORED_PAT)

    assert adapter._headers()["Authorization"] == f"Bearer {STORED_PAT}"


def test_without_a_stored_secret_the_app_identity_authenticates(app_runtime):
    adapter = DatabricksAdapter(endpoint="", auth_metadata={}, secret=None)

    assert adapter._headers()["Authorization"] == f"Bearer {APP_TOKEN}"
    # The workspace comes from the runtime, so no URL was ever entered.
    assert adapter._endpoint == HOST


def test_outside_an_app_a_missing_credential_is_still_an_error(monkeypatch):
    monkeypatch.delenv("DATABRICKS_HOST", raising=False)
    monkeypatch.delenv("DATABRICKS_APP_NAME", raising=False)
    monkeypatch.setattr(databricks_auth, "app_auth_headers", lambda: None)
    adapter = DatabricksAdapter(endpoint="", auth_metadata={}, secret=None)

    with pytest.raises(PlatformError) as excinfo:
        adapter._require_config()

    assert excinfo.value.code == ErrorCode.NOT_CONFIGURED
    assert "workspace_url" in excinfo.value.message
    assert "access_token" in excinfo.value.message


def test_app_identity_satisfies_the_configuration_check(app_runtime):
    DatabricksAdapter(endpoint="", auth_metadata={}, secret=None)._require_config()


# --- discovery keeps working -----------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_discovery_runs_on_the_app_identity(app_runtime):
    """The existing discovery implementation is unchanged — only the header is."""
    _mock_workspace()
    adapter = DatabricksAdapter(endpoint="", auth_metadata={}, secret=None)

    discovery = await adapter.discover_compute_resources()

    assert [r.name for r in discovery.of_type(ResourceType.SQL_WAREHOUSE)] == [
        "Serverless Starter Warehouse",
        "warehouse_db",
    ]
    # Every call carried the App identity and hit the runtime's workspace.
    assert respx.calls.call_count > 0
    for call in respx.calls:
        assert call.request.headers["Authorization"] == f"Bearer {APP_TOKEN}"
        assert str(call.request.url).startswith(HOST)


# --- bootstrap --------------------------------------------------------------


def test_bootstrap_registers_the_workspace_without_storing_a_credential(db_session, app_runtime):
    environment = ensure_app_environment(db_session)

    assert environment is not None
    assert environment.platform == "databricks"
    connection = db_session.query(Connection).filter(Connection.id == environment.connection_id).one()
    assert connection.endpoint == HOST
    assert connection.auth_method == APP_AUTH_METHOD
    # The point of App identity: there is no secret at rest.
    assert connection.secret_encrypted is None


def test_bootstrap_is_idempotent(db_session, app_runtime):
    first = ensure_app_environment(db_session)
    second = ensure_app_environment(db_session)

    assert first.id == second.id
    assert db_session.query(Connection).filter(Connection.auth_method == APP_AUTH_METHOD).count() == 1


def test_bootstrap_does_nothing_outside_an_app(db_session, monkeypatch):
    monkeypatch.delenv("DATABRICKS_HOST", raising=False)
    monkeypatch.delenv("DATABRICKS_APP_NAME", raising=False)

    assert ensure_app_environment(db_session) is None
    assert db_session.query(Connection).count() == 0


def test_bootstrap_leaves_a_fabric_connection_alone(db_session, customer, fabric_connection, app_runtime):
    ensure_app_environment(db_session)

    db_session.refresh(fabric_connection)
    assert fabric_connection.platform == "fabric"
    assert fabric_connection.auth_method == "service_principal"


# --- API surface ------------------------------------------------------------


def test_runtime_endpoint_reports_app_mode_and_leaks_nothing(customer, app_runtime):
    with TestClient(app) as client:
        resp = client.get("/api/runtime", headers={"X-Customer-Id": customer.id})

    assert resp.status_code == 200
    body = resp.json()
    assert body["databricks_app"] is True
    assert body["auth_mode"] == "databricks_app_identity"
    assert APP_TOKEN not in resp.text
    assert "Bearer" not in resp.text


@respx.mock
def test_existing_discovery_endpoint_works_with_no_configured_connection(
    db_session, customer, app_runtime
):
    """GET /api/databricks/resources must keep working, credentials or not."""
    _mock_workspace()
    environment = ensure_app_environment(db_session)

    with TestClient(app) as client:
        resp = client.get(
            "/api/databricks/resources",
            headers={
                "X-Customer-Id": environment.customer_id,
                "X-Acelo-Platform": "databricks",
                "X-Acelo-Connection-Id": environment.connection_id,
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["connected"] is True
    assert [r["name"] for r in body["compute"]["sql_warehouses"]["resources"]] == [
        "Serverless Starter Warehouse",
        "warehouse_db",
    ]
    # No credential reaches the browser.
    assert APP_TOKEN not in resp.text
    assert "Bearer" not in resp.text


@respx.mock
def test_the_agent_analyses_the_app_workspace_without_configuration(db_session, customer, app_runtime):
    _mock_workspace()
    environment = ensure_app_environment(db_session)

    with TestClient(app) as client:
        resp = client.post(
            "/api/databricks/agent/analyze",
            headers={
                "X-Customer-Id": environment.customer_id,
                "X-Acelo-Platform": "databricks",
                "X-Acelo-Connection-Id": environment.connection_id,
            },
            json={"prompt": "show me all clusters"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["environment_id"] == environment.id
    assert APP_TOKEN not in resp.text
