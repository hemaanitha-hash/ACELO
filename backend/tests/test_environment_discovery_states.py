"""
Environment Discovery must never render an API failure as "workspace empty".

The live defect these tests pin down: a workspace whose only compute is
serverless plus SQL warehouses reported "This workspace is empty". The cluster
listing honestly returned zero, and nothing ever asked about warehouses or
serverless compute, so the inventory came back [].

Rules enforced here:
  * discover_resources() enumerates warehouses and serverless compute too
  * every resource type reports one of the six explicit states
  * "empty" is only true when EVERY supported call succeeded with zero
  * a type that succeeds is shown even when another type fails
  * no token reaches the states or the diagnostic output
"""

import uuid

import httpx
import pytest
import respx

from models import Environment
from platforms.databricks import DatabricksAdapter
from platforms.databricks_discovery import DiscoveryState, discovery_states, is_genuinely_empty
from platforms.databricks_resources import ResourceType
from services import environment_service
from services.crypto import get_cipher

ENDPOINT = "https://adb-test.azuredatabricks.net"
TOKEN = "dapi-super-secret-token"

CLUSTERS_URL = f"{ENDPOINT}/api/2.0/clusters/list"
JOBS_URL = f"{ENDPOINT}/api/2.1/jobs/list"
WAREHOUSES_URL = f"{ENDPOINT}/api/2.0/sql/warehouses"
# Serverless compute has no list API: ACELO confirms operator-supplied ids
# through the Permissions API. These ids are set by the autouse fixture below.
SERVERLESS_IDS = (
    "aaaa1111-0000-4000-8000-000000000001",
    "bbbb2222-0000-4000-8000-000000000002",
)
SERVERLESS_NAMES = ("Default Interactive Compute", "Default Automated Compute")
SERVERLESS_URLS = tuple(
    f"{ENDPOINT}/api/2.0/permissions/serverless-compute/{i}" for i in SERVERLESS_IDS
)


@pytest.fixture(autouse=True)
def _configured_serverless_ids(monkeypatch):
    """The two serverless objects this workspace's operator has identified."""
    monkeypatch.setenv(
        "ACELO_DATABRICKS_SERVERLESS_IDS",
        ",".join(f"{i}={n}" for i, n in zip(SERVERLESS_IDS, SERVERLESS_NAMES)),
    )

DENIED = {"error_code": "PERMISSION_DENIED", "message": "User does not have permission."}
NOT_FOUND = {"error_code": "ENDPOINT_NOT_FOUND", "message": "Not found."}

RAW_WAREHOUSES = [
    {"id": "b1c2", "name": "Serverless Starter Warehouse", "state": "RUNNING", "warehouse_type": "PRO"},
    {"id": "a9b8", "name": "warehouse_db", "state": "STOPPED", "warehouse_type": "CLASSIC"},
]
# A real Permissions API response for a serverless-compute object. It carries
# no display name — the name comes from the operator-supplied label.
def _serverless_permissions(object_id: str) -> dict:
    return {
        "object_id": f"/serverless-compute/{object_id}",
        "object_type": "serverless-compute",
        "access_control_list": [
            {"group_name": "users", "all_permissions": [{"permission_level": "CAN_USE"}]}
        ],
    }


def _adapter() -> DatabricksAdapter:
    return DatabricksAdapter(endpoint=ENDPOINT, auth_metadata={}, secret=TOKEN)


def _mock(clusters=None, jobs=None, serverless=None, warehouses=None) -> None:
    respx.get(CLUSTERS_URL).mock(return_value=clusters or httpx.Response(200, json={"clusters": []}))
    respx.get(JOBS_URL).mock(return_value=jobs or httpx.Response(200, json={"jobs": []}))
    for object_id, url in zip(SERVERLESS_IDS, SERVERLESS_URLS):
        respx.get(url).mock(
            return_value=serverless or httpx.Response(200, json=_serverless_permissions(object_id))
        )
    respx.get(WAREHOUSES_URL).mock(
        return_value=warehouses or httpx.Response(200, json={"warehouses": RAW_WAREHOUSES})
    )


def _state_of(states: list[dict], resource_type: str) -> dict:
    return next(s for s in states if s["resource_type"] == resource_type)


# --- the live defect --------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_workspace_with_only_warehouses_and_serverless_is_not_empty():
    """
    The exact live case: zero classic clusters, zero jobs, but real SQL
    warehouses and serverless compute. Inventory must NOT come back empty.
    """
    _mock()

    resources = await _adapter().discover_resources()
    names = {r.display_name for r in resources}

    assert names == {
        "Serverless Starter Warehouse",
        "warehouse_db",
        "Default Interactive Compute",
        "Default Automated Compute",
    }
    types = {r.resource_type for r in resources}
    assert types == {"SQLWarehouse", "ServerlessCompute"}


@pytest.mark.asyncio
@respx.mock
async def test_discovery_reports_a_state_for_every_resource_type():
    _mock()
    adapter = _adapter()

    await adapter.discover_resources()
    states = adapter.last_discovery_states

    assert {s["resource_type"] for s in states} == {
        ResourceType.CLASSIC_CLUSTER,
        ResourceType.SERVERLESS_COMPUTE,
        ResourceType.SQL_WAREHOUSE,
    }
    # Clusters genuinely returned zero; the other two returned real resources.
    assert _state_of(states, ResourceType.CLASSIC_CLUSTER)["state"] == DiscoveryState.SUCCESS_EMPTY
    assert _state_of(states, ResourceType.SQL_WAREHOUSE)["state"] == DiscoveryState.SUCCESS_WITH_RESOURCES
    assert _state_of(states, ResourceType.SQL_WAREHOUSE)["resource_count"] == 2


# --- the six states ---------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_403_is_authorization_failed_not_empty():
    _mock(clusters=httpx.Response(403, json=DENIED))
    adapter = _adapter()

    await adapter.discover_resources()
    state = _state_of(adapter.last_discovery_states, ResourceType.CLASSIC_CLUSTER)

    assert state["state"] == DiscoveryState.AUTHORIZATION_FAILED
    assert state["status_code"] == 403
    assert state["method"] == "GET"
    assert state["path"] == "/api/2.0/clusters/list"
    assert state["platform_error_code"] == "PERMISSION_DENIED"
    assert state["resource_count"] == 0


@pytest.mark.asyncio
@respx.mock
async def test_404_is_not_supported_not_empty():
    _mock(serverless=httpx.Response(404, json=NOT_FOUND))
    adapter = _adapter()

    await adapter.discover_resources()
    state = _state_of(adapter.last_discovery_states, ResourceType.SERVERLESS_COMPUTE)

    assert state["state"] == DiscoveryState.NOT_SUPPORTED
    assert state["status_code"] == 404


@pytest.mark.asyncio
@respx.mock
async def test_401_is_authentication_failed():
    _mock(warehouses=httpx.Response(401, json={"error_code": "UNAUTHORIZED"}))
    adapter = _adapter()

    await adapter.discover_resources()

    assert (
        _state_of(adapter.last_discovery_states, ResourceType.SQL_WAREHOUSE)["state"]
        == DiscoveryState.AUTHENTICATION_FAILED
    )


@pytest.mark.asyncio
@respx.mock
async def test_500_is_api_error():
    _mock(warehouses=httpx.Response(500, text="internal error"))
    adapter = _adapter()

    await adapter.discover_resources()

    assert (
        _state_of(adapter.last_discovery_states, ResourceType.SQL_WAREHOUSE)["state"]
        == DiscoveryState.API_ERROR
    )


@pytest.mark.asyncio
@respx.mock
async def test_a_successful_type_is_returned_even_when_others_fail():
    """SQL warehouses must survive cluster and serverless failure."""
    _mock(clusters=httpx.Response(403, json=DENIED), serverless=httpx.Response(404, json=NOT_FOUND))

    adapter = _adapter()
    resources = await adapter.discover_resources()

    assert {r.display_name for r in resources} == {"Serverless Starter Warehouse", "warehouse_db"}
    states = adapter.last_discovery_states
    assert _state_of(states, ResourceType.SQL_WAREHOUSE)["state"] == DiscoveryState.SUCCESS_WITH_RESOURCES
    assert _state_of(states, ResourceType.CLASSIC_CLUSTER)["state"] == DiscoveryState.AUTHORIZATION_FAILED
    assert _state_of(states, ResourceType.SERVERLESS_COMPUTE)["state"] == DiscoveryState.NOT_SUPPORTED


# --- "empty" means empty ----------------------------------------------------


def test_empty_requires_every_type_to_have_succeeded_with_zero():
    all_empty = [
        {"resource_type": t, "state": DiscoveryState.SUCCESS_EMPTY, "resource_count": 0}
        for t in ("CLASSIC_CLUSTER", "SERVERLESS_COMPUTE", "SQL_WAREHOUSE")
    ]
    assert is_genuinely_empty(all_empty) is True

    # One refused type means the workspace cannot be called empty.
    with_refusal = list(all_empty)
    with_refusal[0] = {
        "resource_type": "CLASSIC_CLUSTER",
        "state": DiscoveryState.AUTHORIZATION_FAILED,
        "resource_count": 0,
    }
    assert is_genuinely_empty(with_refusal) is False
    # No states at all is not evidence of emptiness either.
    assert is_genuinely_empty([]) is False


@pytest.mark.asyncio
@respx.mock
async def test_no_clusters_and_no_warehouses_is_not_empty_while_serverless_is_unconfirmed(monkeypatch):
    """
    With no serverless ids configured, Databricks offers no way to confirm
    whether serverless compute exists. Zero clusters and zero warehouses
    therefore does NOT prove the workspace empty — the unconfirmable type keeps
    it honest.
    """
    monkeypatch.delenv("ACELO_DATABRICKS_SERVERLESS_IDS", raising=False)
    _mock(warehouses=httpx.Response(200, json={"warehouses": []}))
    adapter = _adapter()

    resources = await adapter.discover_resources()

    assert resources == []
    assert is_genuinely_empty(adapter.last_discovery_states) is False
    assert (
        _state_of(adapter.last_discovery_states, ResourceType.SERVERLESS_COMPUTE)["state"]
        == DiscoveryState.NOT_SUPPORTED
    )


@pytest.mark.asyncio
@respx.mock
async def test_a_genuinely_empty_workspace_is_reported_as_empty():
    """Every type asked, every type answered zero — that is genuinely empty."""
    _mock(
        serverless=httpx.Response(404, json=NOT_FOUND),
        warehouses=httpx.Response(200, json={"warehouses": []}),
    )
    adapter = _adapter()
    resources = await adapter.discover_resources()

    assert resources == []
    # Clusters and warehouses succeeded with zero; serverless could not be
    # confirmed, so "empty" is still not claimed.
    states = adapter.last_discovery_states
    assert _state_of(states, ResourceType.CLASSIC_CLUSTER)["state"] == DiscoveryState.SUCCESS_EMPTY
    assert _state_of(states, ResourceType.SQL_WAREHOUSE)["state"] == DiscoveryState.SUCCESS_EMPTY
    assert is_genuinely_empty(states) is False


# --- through the discovery service -----------------------------------------


@pytest.fixture
def databricks_environment(db_session, customer, databricks_connection):
    databricks_connection.secret_encrypted = get_cipher().encrypt(TOKEN)
    environment = Environment(
        id=str(uuid.uuid4()),
        customer_id=customer.id,
        connection_id=databricks_connection.id,
        name="Databricks PoC",
        platform="databricks",
        auth_mode="pat",
        workspace_name="adb-test.azuredatabricks.net",
        status="connected",
    )
    db_session.add(environment)
    db_session.commit()
    db_session.refresh(environment)
    return environment


@pytest.mark.asyncio
@respx.mock
async def test_discover_environment_returns_items_and_states(db_session, databricks_environment):
    _mock()

    result = await environment_service.discover_environment(db_session, databricks_environment)

    assert result.get("discovered") is not False
    assert len(result["items"]) == 4
    assert result["counts"]["SQLWarehouse"] == 2
    assert result["counts"]["ServerlessCompute"] == 2
    assert len(result["resource_states"]) == 3
    # This is what stops the UI printing "workspace empty".
    assert is_genuinely_empty(result["resource_states"]) is False


@pytest.mark.asyncio
@respx.mock
async def test_discovery_states_never_carry_a_credential(db_session, databricks_environment):
    _mock(clusters=httpx.Response(403, json=DENIED))

    result = await environment_service.discover_environment(db_session, databricks_environment)
    rendered = str(result["resource_states"])

    assert TOKEN not in rendered
    assert "dapi" not in rendered
    assert "Bearer" not in rendered
    assert "Authorization" not in rendered


@pytest.mark.asyncio
@respx.mock
async def test_discovery_still_fails_when_no_listing_at_all_succeeds(db_session, databricks_environment):
    """Total refusal is a real failure, not an empty workspace."""
    _mock(
        clusters=httpx.Response(403, json=DENIED),
        jobs=httpx.Response(403, json=DENIED),
        serverless=httpx.Response(403, json=DENIED),
        warehouses=httpx.Response(403, json=DENIED),
    )

    result = await environment_service.discover_environment(db_session, databricks_environment)

    assert result["discovered"] is False
    assert result["error_code"] == "PERMISSION_DENIED"
