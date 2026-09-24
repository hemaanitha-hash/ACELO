"""
ACTIVE PLATFORM CONTEXT — backend isolation.

The defect this closes: with both a Fabric and a Databricks connection
configured, the backend resolved "the" connection by picking the first
connected one. That is an arbitrary choice, so one platform's data could be
served while the user had the other selected.

Rules pinned down here:
  * the active platform + connection come from the request, not a guess
  * a connection's own `platform` column is the discriminator — a Fabric
    connection can never be handed to a Databricks service, or the reverse
  * Databricks-only routes refuse while Fabric is active (and vice versa)
  * switching platform changes what the shared endpoints return
  * with no context sent, behaviour is unchanged (existing callers keep working)
"""

import uuid

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from main import app
from models import Connection, Environment
from services.crypto import get_cipher

DATABRICKS_ENDPOINT = "https://adb-test.azuredatabricks.net"
TOKEN = "dapi-super-secret-token"

CLUSTERS_URL = f"{DATABRICKS_ENDPOINT}/api/2.0/clusters/list"
WAREHOUSES_URL = f"{DATABRICKS_ENDPOINT}/api/2.0/sql/warehouses"

RAW_WAREHOUSES = [
    {"id": "b1c2", "name": "Serverless Starter Warehouse", "state": "RUNNING", "warehouse_type": "PRO"},
    {"id": "a9b8", "name": "warehouse_db", "state": "STOPPED", "warehouse_type": "CLASSIC"},
]


def _mock_databricks() -> None:
    respx.get(CLUSTERS_URL).mock(return_value=httpx.Response(200, json={"clusters": []}))
    respx.get(WAREHOUSES_URL).mock(return_value=httpx.Response(200, json={"warehouses": RAW_WAREHOUSES}))


@pytest.fixture
def both_platforms(db_session, customer, databricks_connection, fabric_connection):
    """A customer with BOTH platforms connected — the situation that leaked."""
    databricks_connection.secret_encrypted = get_cipher().encrypt(TOKEN)
    databricks_connection.status = "connected"
    fabric_connection.status = "connected"

    databricks_env = Environment(
        id=str(uuid.uuid4()),
        customer_id=customer.id,
        connection_id=databricks_connection.id,
        name="Databricks PoC",
        platform="databricks",
        auth_mode="pat",
        workspace_name="adb-test.azuredatabricks.net",
        status="connected",
    )
    fabric_env = Environment(
        id=str(uuid.uuid4()),
        customer_id=customer.id,
        connection_id=fabric_connection.id,
        name="Fabric Workspace",
        platform="fabric",
        auth_mode="service_principal",
        workspace_name="Fabric Workspace",
        status="connected",
    )
    db_session.add_all([databricks_env, fabric_env])
    db_session.commit()
    return {
        "databricks": {"connection": databricks_connection, "environment": databricks_env},
        "fabric": {"connection": fabric_connection, "environment": fabric_env},
    }


def _headers(customer, platform: str | None = None, connection_id: str | None = None) -> dict:
    headers = {"X-Customer-Id": customer.id}
    if platform:
        headers["X-Acelo-Platform"] = platform
    if connection_id:
        headers["X-Acelo-Connection-Id"] = connection_id
    return headers


# --- A. Databricks active ---------------------------------------------------


@respx.mock
def test_databricks_active_resolves_the_databricks_connection(customer, both_platforms):
    _mock_databricks()
    databricks = both_platforms["databricks"]

    with TestClient(app) as client:
        resp = client.get(
            "/api/databricks/resources",
            headers=_headers(customer, "databricks", databricks["connection"].id),
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["environment_id"] == databricks["environment"].id
    assert [r["name"] for r in body["compute"]["sql_warehouses"]["resources"]] == [
        "Serverless Starter Warehouse",
        "warehouse_db",
    ]
    # Only Databricks was contacted — no Fabric call was made.
    for call in respx.calls:
        assert "azuredatabricks.net" in str(call.request.url)
        assert "fabric.microsoft.com" not in str(call.request.url)


# --- B. Fabric active -------------------------------------------------------


def test_databricks_routes_are_refused_while_fabric_is_active(customer, both_platforms):
    """A Databricks route must not serve data when the user is in Fabric."""
    fabric = both_platforms["fabric"]

    with TestClient(app) as client:
        resources = client.get(
            "/api/databricks/resources", headers=_headers(customer, "fabric", fabric["connection"].id)
        )
        agent = client.post(
            "/api/databricks/agent/analyze",
            headers=_headers(customer, "fabric", fabric["connection"].id),
            json={"prompt": "show me all clusters"},
        )

    assert resources.status_code == 409
    assert "fabric" in resources.json()["detail"].lower()
    assert agent.status_code == 409


def test_fabric_active_never_reaches_databricks(customer, both_platforms):
    """No Databricks HTTP call may happen while Fabric is the active platform."""
    fabric = both_platforms["fabric"]

    with respx.mock:
        route = respx.get(CLUSTERS_URL).mock(return_value=httpx.Response(200, json={"clusters": []}))
        with TestClient(app) as client:
            client.get(
                "/api/databricks/resources",
                headers=_headers(customer, "fabric", fabric["connection"].id),
            )
        assert route.call_count == 0


# --- C. Switching -----------------------------------------------------------


@respx.mock
def test_switching_platform_changes_what_the_overview_returns(customer, both_platforms):
    _mock_databricks()

    with TestClient(app) as client:
        as_databricks = client.get(
            "/api/overview",
            headers=_headers(customer, "databricks", both_platforms["databricks"]["connection"].id),
        )
        as_fabric = client.get(
            "/api/overview",
            headers=_headers(customer, "fabric", both_platforms["fabric"]["connection"].id),
        )

    assert as_databricks.status_code == 200
    assert as_fabric.status_code == 200
    assert as_databricks.json()["platformConnected"] == "databricks"
    assert as_fabric.json()["platformConnected"] == "fabric"


@respx.mock
def test_run_history_is_scoped_to_the_active_platform(customer, both_platforms):
    _mock_databricks()

    with TestClient(app) as client:
        databricks_runs = client.get(
            "/api/runs",
            headers=_headers(customer, "databricks", both_platforms["databricks"]["connection"].id),
        )
        fabric_runs = client.get(
            "/api/runs",
            headers=_headers(customer, "fabric", both_platforms["fabric"]["connection"].id),
        )

    assert databricks_runs.status_code == 200
    assert fabric_runs.status_code == 200
    # No stale rows from the other platform in either view.
    assert all(r["platform"] == "databricks" for r in databricks_runs.json()["runs"])
    assert all(r["platform"] == "fabric" for r in fabric_runs.json()["runs"])


# --- E. Connection isolation ------------------------------------------------


def test_a_fabric_connection_cannot_be_used_as_databricks(customer, both_platforms):
    """The connection's platform column is the discriminator, not the header."""
    fabric_connection = both_platforms["fabric"]["connection"]

    with TestClient(app) as client:
        resp = client.get(
            "/api/databricks/resources",
            headers=_headers(customer, "databricks", fabric_connection.id),
        )

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "fabric connection" in detail.lower()


def test_a_databricks_connection_cannot_be_used_as_fabric(customer, both_platforms):
    databricks_connection = both_platforms["databricks"]["connection"]

    with TestClient(app) as client:
        resp = client.get(
            "/api/overview", headers=_headers(customer, "fabric", databricks_connection.id)
        )

    assert resp.status_code == 409


def test_another_customers_connection_is_not_resolvable(db_session, customer, both_platforms):
    """Cross-customer isolation survives the new header."""
    intruder = Connection(
        id=str(uuid.uuid4()),
        customer_id=str(uuid.uuid4()),  # a different customer
        platform="databricks",
        workspace="Someone else's workspace",
        endpoint=DATABRICKS_ENDPOINT,
        auth_method="pat",
        status="connected",
    )
    db_session.add(intruder)
    db_session.commit()

    with TestClient(app) as client:
        resp = client.get(
            "/api/databricks/resources", headers=_headers(customer, "databricks", intruder.id)
        )

    # 404, not 403 — it must not confirm the id exists.
    assert resp.status_code == 404


def test_an_unknown_platform_is_rejected(customer, both_platforms):
    with TestClient(app) as client:
        resp = client.get("/api/overview", headers=_headers(customer, "snowflake"))

    assert resp.status_code == 400


# --- backwards compatibility ------------------------------------------------


@respx.mock
def test_without_context_headers_behaviour_is_unchanged(customer, both_platforms):
    """Existing callers that send no context still work."""
    _mock_databricks()

    with TestClient(app) as client:
        resp = client.get("/api/overview", headers={"X-Customer-Id": customer.id})

    assert resp.status_code == 200
    assert resp.json()["platformConnected"] in ("databricks", "fabric")
