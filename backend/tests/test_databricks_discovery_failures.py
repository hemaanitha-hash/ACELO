"""
Per-resource-type failure handling for Databricks discovery.

The contract pinned down here: ONE refused resource type never collapses the
request into a single top-level authorization error. Each type reports its own
status, the types that worked still return their resources, and every call is
recorded with the facts needed to diagnose it (method, path, HTTP status,
Databricks error_code/message) — never a credential.

Classification:
    403  -> UNAVAILABLE / INSUFFICIENT_PERMISSIONS
    404  -> UNAVAILABLE / NOT_EXPOSED (serverless: NOT_EXPOSED_BY_WORKSPACE_API)
    else -> ERROR

Companion to test_databricks_resource_discovery.py, which covers normalisation
and the success path. Every Databricks call is mocked.
"""

import uuid

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from main import app
from models import Environment
from platforms.databricks import DatabricksAdapter
from platforms.databricks_resources import ResourceStatus, ResourceType, UnavailableReason
from platforms.errors import ErrorCode, PlatformError
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

DENIED = {"error_code": "PERMISSION_DENIED", "message": "User does not have permission to view this resource."}
NOT_FOUND = {"error_code": "ENDPOINT_NOT_FOUND", "message": "The endpoint was not found."}

RAW_CLUSTER = {
    "cluster_id": "0421-193742-abcd1234",
    "cluster_name": "analytics-all-purpose",
    "state": "TERMINATED",
    "cluster_source": "UI",
    "node_type_id": "Standard_DS3_v2",
}
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
RAW_WAREHOUSES = [
    {"id": "b1c2", "name": "Serverless Starter Warehouse", "state": "RUNNING", "warehouse_type": "PRO"},
    {"id": "a9b8", "name": "warehouse_db", "state": "STOPPED", "warehouse_type": "CLASSIC"},
]


def _adapter() -> DatabricksAdapter:
    return DatabricksAdapter(endpoint=ENDPOINT, auth_metadata={}, secret=TOKEN)


def _mock(clusters=None, serverless=None, warehouses=None) -> None:
    """Each argument is an httpx.Response; omitted ones default to success."""
    respx.get(CLUSTERS_URL).mock(
        return_value=clusters or httpx.Response(200, json={"clusters": [RAW_CLUSTER]})
    )
    for object_id, url in zip(SERVERLESS_IDS, SERVERLESS_URLS):
        respx.get(url).mock(
            return_value=serverless or httpx.Response(200, json=_serverless_permissions(object_id))
        )
    respx.get(WAREHOUSES_URL).mock(
        return_value=warehouses or httpx.Response(200, json={"warehouses": RAW_WAREHOUSES})
    )


def _last_probe(discovery, resource_type: str):
    probes = discovery.probes_of(resource_type)
    assert probes, f"no probe recorded for {resource_type}"
    return probes[-1]


# --- 403: not permitted -----------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_classic_cluster_403_is_unavailable_and_other_types_still_return():
    _mock(clusters=httpx.Response(403, json=DENIED))

    discovery = await _adapter().discover_compute_resources()
    status = discovery.status_of(ResourceType.CLASSIC_CLUSTER)

    assert status.status == ResourceStatus.UNAVAILABLE
    assert status.reason == UnavailableReason.INSUFFICIENT_PERMISSIONS

    probe = _last_probe(discovery, ResourceType.CLASSIC_CLUSTER)
    assert (probe.method, probe.path, probe.status_code) == ("GET", "/api/2.0/clusters/list", 403)
    assert probe.failure_kind == "AUTHORIZATION"
    assert probe.platform_error_code == "PERMISSION_DENIED"

    # The types that worked are untouched.
    assert discovery.status_of(ResourceType.SQL_WAREHOUSE).status == ResourceStatus.OK
    assert len(discovery.of_type(ResourceType.SQL_WAREHOUSE)) == 2
    assert len(discovery.of_type(ResourceType.SERVERLESS_COMPUTE)) == 2


@pytest.mark.asyncio
@respx.mock
async def test_sql_warehouse_403_is_unavailable_while_clusters_still_return():
    _mock(warehouses=httpx.Response(403, json=DENIED))

    discovery = await _adapter().discover_compute_resources()
    status = discovery.status_of(ResourceType.SQL_WAREHOUSE)

    assert status.status == ResourceStatus.UNAVAILABLE
    assert status.reason == UnavailableReason.INSUFFICIENT_PERMISSIONS
    assert _last_probe(discovery, ResourceType.SQL_WAREHOUSE).status_code == 403
    assert [r.name for r in discovery.of_type(ResourceType.CLASSIC_CLUSTER)] == ["analytics-all-purpose"]


@pytest.mark.asyncio
@respx.mock
async def test_serverless_403_is_insufficient_permissions_not_a_top_level_failure():
    _mock(serverless=httpx.Response(403, json=DENIED))

    discovery = await _adapter().discover_compute_resources()
    status = discovery.status_of(ResourceType.SERVERLESS_COMPUTE)

    assert status.status == ResourceStatus.UNAVAILABLE
    assert status.reason == UnavailableReason.INSUFFICIENT_PERMISSIONS
    # Refused, so nothing was invented.
    assert discovery.of_type(ResourceType.SERVERLESS_COMPUTE) == []
    # Every candidate path was tried and recorded.
    assert len(discovery.probes_of(ResourceType.SERVERLESS_COMPUTE)) == len(SERVERLESS_URLS)
    # Neighbours unaffected.
    assert len(discovery.of_type(ResourceType.CLASSIC_CLUSTER)) == 1
    assert len(discovery.of_type(ResourceType.SQL_WAREHOUSE)) == 2


# --- 404: not exposed -------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_serverless_404_is_not_exposed_by_workspace_api():
    _mock(serverless=httpx.Response(404, json=NOT_FOUND))

    discovery = await _adapter().discover_compute_resources()
    status = discovery.status_of(ResourceType.SERVERLESS_COMPUTE)

    assert status.status == ResourceStatus.UNAVAILABLE
    assert status.reason == UnavailableReason.NOT_EXPOSED_BY_WORKSPACE_API
    assert _last_probe(discovery, ResourceType.SERVERLESS_COMPUTE).failure_kind == "NOT_FOUND"


@pytest.mark.asyncio
@respx.mock
async def test_classic_cluster_404_is_not_exposed():
    _mock(clusters=httpx.Response(404, json=NOT_FOUND))

    status = (await _adapter().discover_compute_resources()).status_of(ResourceType.CLASSIC_CLUSTER)

    assert status.status == ResourceStatus.UNAVAILABLE
    assert status.reason == UnavailableReason.NOT_EXPOSED


@pytest.mark.asyncio
@respx.mock
async def test_serverless_403_outranks_a_404_from_another_candidate_path():
    """A refusal is actionable; a missing path is not. The 403 must be reported."""
    respx.get(CLUSTERS_URL).mock(return_value=httpx.Response(200, json={"clusters": []}))
    respx.get(SERVERLESS_URLS[0]).mock(return_value=httpx.Response(404, json=NOT_FOUND))
    respx.get(SERVERLESS_URLS[1]).mock(return_value=httpx.Response(403, json=DENIED))
    respx.get(WAREHOUSES_URL).mock(return_value=httpx.Response(200, json={"warehouses": RAW_WAREHOUSES}))

    status = (await _adapter().discover_compute_resources()).status_of(ResourceType.SERVERLESS_COMPUTE)

    assert status.reason == UnavailableReason.INSUFFICIENT_PERMISSIONS


# --- everything else is ERROR, not UNAVAILABLE ------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_401_and_500_are_errors_rather_than_unavailable():
    """Not permitted is UNAVAILABLE; broken is ERROR. Different answers."""
    _mock(
        clusters=httpx.Response(401, json={"error_code": "UNAUTHORIZED"}),
        warehouses=httpx.Response(500, text="internal error"),
    )

    discovery = await _adapter().discover_compute_resources()

    clusters = discovery.status_of(ResourceType.CLASSIC_CLUSTER)
    assert clusters.status == ResourceStatus.ERROR
    assert clusters.reason == UnavailableReason.AUTHENTICATION_FAILED
    assert _last_probe(discovery, ResourceType.CLASSIC_CLUSTER).failure_kind == "AUTHENTICATION"

    warehouses = discovery.status_of(ResourceType.SQL_WAREHOUSE)
    assert warehouses.status == ResourceStatus.ERROR
    assert warehouses.reason == UnavailableReason.PLATFORM_API_UNAVAILABLE

    # Serverless still succeeded despite both neighbours failing.
    assert discovery.status_of(ResourceType.SERVERLESS_COMPUTE).status == ResourceStatus.OK


# --- partial discovery ------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_sql_warehouses_survive_when_all_compute_discovery_is_unavailable():
    """The clusters+sql scoped PAT case: warehouses must come back on their own."""
    _mock(clusters=httpx.Response(403, json=DENIED), serverless=httpx.Response(403, json=DENIED))

    discovery = await _adapter().discover_compute_resources()

    assert [r.name for r in discovery.of_type(ResourceType.SQL_WAREHOUSE)] == [
        "Serverless Starter Warehouse",
        "warehouse_db",
    ]
    assert discovery.status_of(ResourceType.SQL_WAREHOUSE).status == ResourceStatus.OK
    assert discovery.status_of(ResourceType.CLASSIC_CLUSTER).status == ResourceStatus.UNAVAILABLE
    assert discovery.status_of(ResourceType.SERVERLESS_COMPUTE).status == ResourceStatus.UNAVAILABLE


# --- endpoint behaviour -----------------------------------------------------


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


@respx.mock
def test_endpoint_returns_200_with_partial_results_when_a_type_is_refused(customer, databricks_environment):
    """A refused resource type must never become a failed HTTP request."""
    _mock(clusters=httpx.Response(403, json=DENIED), serverless=httpx.Response(403, json=DENIED))

    with TestClient(app) as client:
        resp = client.get("/api/databricks/resources", headers={"X-Customer-Id": customer.id})

    assert resp.status_code == 200
    body = resp.json()
    assert body["connected"] is True
    assert [r["name"] for r in body["resources"]] == ["Serverless Starter Warehouse", "warehouse_db"]

    statuses = {s["resource_type"]: s for s in body["statuses"]}
    assert statuses["CLASSIC_CLUSTER"]["reason"] == "INSUFFICIENT_PERMISSIONS"
    assert statuses["SERVERLESS_COMPUTE"]["reason"] == "INSUFFICIENT_PERMISSIONS"
    assert statuses["SQL_WAREHOUSE"]["status"] == "OK"

    # The diagnostic record names the exact call that was refused.
    refused = [p for p in body["probes"] if p["resource_type"] == "CLASSIC_CLUSTER"][0]
    assert refused["path"] == "/api/2.0/clusters/list"
    assert refused["status_code"] == 403
    assert refused["platform_error_code"] == "PERMISSION_DENIED"


@respx.mock
def test_probe_diagnostics_never_leak_the_token_or_auth_header(customer, databricks_environment):
    _mock(clusters=httpx.Response(403, json=DENIED), serverless=httpx.Response(403, json=DENIED))

    with TestClient(app) as client:
        resp = client.get("/api/databricks/resources", headers={"X-Customer-Id": customer.id})

    raw = resp.text
    assert TOKEN not in raw
    assert "dapi" not in raw
    assert "Bearer" not in raw
    assert "Authorization" not in raw


# --- environment discovery must not die on one refused listing ---------------


@pytest.mark.asyncio
@respx.mock
async def test_environment_discovery_survives_a_refused_jobs_listing():
    """
    The reported failure: a clusters+sql scoped PAT is refused on /jobs/list,
    which previously raised and failed the whole environment discovery with a
    single "identity does not have access" error, hiding the visible clusters.
    """
    respx.get(CLUSTERS_URL).mock(return_value=httpx.Response(200, json={"clusters": [RAW_CLUSTER]}))
    respx.get(JOBS_URL).mock(return_value=httpx.Response(403, json=DENIED))

    resources = await _adapter().discover_resources()

    assert [r.display_name for r in resources] == ["analytics-all-purpose"]
    assert all(r.resource_type == "Cluster" for r in resources)


@pytest.mark.asyncio
@respx.mock
async def test_environment_discovery_survives_a_refused_cluster_listing():
    respx.get(CLUSTERS_URL).mock(return_value=httpx.Response(403, json=DENIED))
    respx.get(JOBS_URL).mock(
        return_value=httpx.Response(200, json={"jobs": [{"job_id": 7, "settings": {"name": "nightly"}}]})
    )

    resources = await _adapter().discover_resources()

    assert [r.display_name for r in resources] == ["nightly"]


@pytest.mark.asyncio
@respx.mock
async def test_environment_discovery_still_fails_when_nothing_can_be_listed():
    """A total refusal is a real failure, not an empty workspace."""
    respx.get(CLUSTERS_URL).mock(return_value=httpx.Response(403, json=DENIED))
    respx.get(JOBS_URL).mock(return_value=httpx.Response(403, json=DENIED))

    with pytest.raises(PlatformError) as excinfo:
        await _adapter().discover_resources()

    assert excinfo.value.code == ErrorCode.PERMISSION_DENIED
    assert excinfo.value.status_code == 403


# --- serverless compute: the documented Permissions API path ----------------
#
# "Default Interactive Compute" and "Default Automated Compute" are Serverless
# Compute Access Control objects. Databricks exposes them at
# GET /api/2.0/permissions/serverless-compute/{id} but publishes NO endpoint
# that lists them, so ACELO confirms operator-supplied ids and otherwise says
# NOT_EXPOSED_BY_WORKSPACE_API.


@pytest.mark.asyncio
@respx.mock
async def test_serverless_is_confirmed_through_the_permissions_api():
    _mock()

    discovery = await _adapter().discover_compute_resources()
    resources = discovery.of_type(ResourceType.SERVERLESS_COMPUTE)

    assert [r.name for r in resources] == list(SERVERLESS_NAMES)
    assert [r.resource_id for r in resources] == list(SERVERLESS_IDS)
    # Classified as serverless, never as a cluster.
    for resource in resources:
        assert resource.resource_type == ResourceType.SERVERLESS_COMPUTE
        assert resource.metadata["object_type"] == "serverless-compute"

    # The documented endpoint is what was called.
    probe = discovery.probes_of(ResourceType.SERVERLESS_COMPUTE)[0]
    assert probe.path == f"/api/2.0/permissions/serverless-compute/{SERVERLESS_IDS[0]}"
    assert probe.method == "GET"


@pytest.mark.asyncio
@respx.mock
async def test_serverless_without_configured_ids_is_not_exposed_not_empty(monkeypatch):
    """Databricks has no list API, so ACELO must say so rather than return []."""
    monkeypatch.delenv("ACELO_DATABRICKS_SERVERLESS_IDS", raising=False)
    _mock()

    discovery = await _adapter().discover_compute_resources()
    status = discovery.status_of(ResourceType.SERVERLESS_COMPUTE)

    assert status.status == ResourceStatus.UNAVAILABLE
    assert status.reason == UnavailableReason.NOT_EXPOSED_BY_WORKSPACE_API
    assert "ACELO_DATABRICKS_SERVERLESS_IDS" in status.message
    # No call was invented for a type that cannot be listed.
    assert discovery.probes_of(ResourceType.SERVERLESS_COMPUTE) == []
    # The other two categories are unaffected.
    assert len(discovery.of_type(ResourceType.SQL_WAREHOUSE)) == 2
    assert len(discovery.of_type(ResourceType.CLASSIC_CLUSTER)) == 1


@pytest.mark.asyncio
@respx.mock
async def test_ids_may_be_configured_without_labels(monkeypatch):
    monkeypatch.setenv("ACELO_DATABRICKS_SERVERLESS_IDS", SERVERLESS_IDS[0])
    _mock()

    resources = (await _adapter().discover_compute_resources()).of_type(ResourceType.SERVERLESS_COMPUTE)

    # With no label the id is the name — nothing is invented.
    assert len(resources) == 1
    assert resources[0].name == SERVERLESS_IDS[0]


# --- the grouped Compute shape ----------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_compute_groups_all_three_categories():
    _mock()

    compute = (await _adapter().discover_compute_resources()).compute()

    assert [r["name"] for r in compute["classic_clusters"]["resources"]] == ["analytics-all-purpose"]
    assert [r["name"] for r in compute["serverless_compute"]["resources"]] == list(SERVERLESS_NAMES)
    assert [r["name"] for r in compute["sql_warehouses"]["resources"]] == [
        "Serverless Starter Warehouse",
        "warehouse_db",
    ]
    assert all(compute[key]["status"] == ResourceStatus.OK for key in compute)
    # A warehouse is never grouped as a cluster.
    cluster_names = {r["name"] for r in compute["classic_clusters"]["resources"]}
    assert "warehouse_db" not in cluster_names
    assert "Default Interactive Compute" not in cluster_names


@pytest.mark.asyncio
@respx.mock
async def test_each_compute_group_carries_its_own_status():
    """An unavailable category must be distinguishable from an empty one."""
    _mock(clusters=httpx.Response(403, json=DENIED), warehouses=httpx.Response(200, json={"warehouses": []}))

    compute = (await _adapter().discover_compute_resources()).compute()

    # Refused: empty list, but explicitly UNAVAILABLE.
    assert compute["classic_clusters"]["resources"] == []
    assert compute["classic_clusters"]["status"] == ResourceStatus.UNAVAILABLE
    assert compute["classic_clusters"]["reason"] == UnavailableReason.INSUFFICIENT_PERMISSIONS
    # Genuinely empty: empty list, but OK.
    assert compute["sql_warehouses"]["resources"] == []
    assert compute["sql_warehouses"]["status"] == ResourceStatus.OK
    # Still discovered.
    assert len(compute["serverless_compute"]["resources"]) == 2


@respx.mock
def test_endpoint_returns_the_grouped_compute_shape(customer, databricks_environment):
    _mock()

    with TestClient(app) as client:
        resp = client.get("/api/databricks/resources", headers={"X-Customer-Id": customer.id})

    assert resp.status_code == 200
    compute = resp.json()["compute"]
    assert set(compute) == {"classic_clusters", "serverless_compute", "sql_warehouses"}
    assert [r["name"] for r in compute["sql_warehouses"]["resources"]] == [
        "Serverless Starter Warehouse",
        "warehouse_db",
    ]
    assert [r["name"] for r in compute["serverless_compute"]["resources"]] == list(SERVERLESS_NAMES)
    assert len(compute["classic_clusters"]["resources"]) == 1
