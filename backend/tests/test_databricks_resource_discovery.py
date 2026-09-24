"""
Databricks compute capability discovery (read-only).

These tests pin the behaviour the phase depends on:
  * classic clusters, serverless compute and SQL warehouses each normalise into
    the common ACELO resource model
  * serverless compute is NEVER reported as CLASSIC_CLUSTER
  * one unreadable resource type does not fail the whole discovery
  * an empty classic-cluster list is reported as OK-with-zero, not as a failure
  * no response carries the access token

Every Databricks call is mocked; nothing here touches a real workspace and
nothing here creates a resource.
"""

import json
import uuid

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from main import app
from models import Environment
from platforms.databricks import DatabricksAdapter
from platforms.databricks_resources import (
    ResourceStatus,
    ResourceType,
    UnavailableReason,
    normalize_classic_cluster,
    normalize_serverless_compute,
    normalize_sql_warehouse,
)
from services.crypto import get_cipher

ENDPOINT = "https://adb-test.azuredatabricks.net"
TOKEN = "dapi-super-secret-token"

CLUSTERS_URL = f"{ENDPOINT}/api/2.0/clusters/list"
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


# --- fixtures ---------------------------------------------------------------


def _adapter() -> DatabricksAdapter:
    return DatabricksAdapter(endpoint=ENDPOINT, auth_metadata={}, secret=TOKEN)


RAW_CLASSIC_CLUSTER = {
    "cluster_id": "0421-193742-abcd1234",
    "cluster_name": "analytics-all-purpose",
    "state": "TERMINATED",
    "cluster_source": "UI",
    "driver_node_type_id": "Standard_DS3_v2",
    "node_type_id": "Standard_DS3_v2",
    "autoscale": {"min_workers": 2, "max_workers": 8},
    "autotermination_minutes": 30,
    "spark_version": "14.3.x-scala2.12",
}

# What the workspace actually exposes for serverless compute. Shape is read
# generically because the serverless surface is not one fixed schema.
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
    {
        "id": "b1c2d3e4f5a60718",
        "name": "Serverless Starter Warehouse",
        "state": "RUNNING",
        "warehouse_type": "PRO",
        "cluster_size": "2X-Small",
        "auto_stop_mins": 10,
        "min_num_clusters": 1,
        "max_num_clusters": 1,
        "enable_serverless_compute": True,
        "creator_name": "hema@example.com",
    },
    {
        "id": "a9b8c7d6e5f40312",
        "name": "warehouse_db",
        "state": "STOPPED",
        "warehouse_type": "CLASSIC",
        "cluster_size": "Small",
        "auto_stop_mins": 45,
        "min_num_clusters": 1,
        "max_num_clusters": 3,
    },
]


def _mock_serverless_ok() -> None:
    """Both operator-supplied serverless ids confirm through the Permissions API."""
    for object_id, url in zip(SERVERLESS_IDS, SERVERLESS_URLS):
        respx.get(url).mock(return_value=httpx.Response(200, json=_serverless_permissions(object_id)))


def _mock_all_ok() -> None:
    respx.get(CLUSTERS_URL).mock(return_value=httpx.Response(200, json={"clusters": [RAW_CLASSIC_CLUSTER]}))
    _mock_serverless_ok()
    respx.get(WAREHOUSES_URL).mock(return_value=httpx.Response(200, json={"warehouses": RAW_WAREHOUSES}))


# --- normalisation ----------------------------------------------------------


def test_classic_cluster_normalisation_carries_the_documented_fields():
    resource = normalize_classic_cluster(RAW_CLASSIC_CLUSTER)

    assert resource.platform == "databricks"
    assert resource.resource_type == ResourceType.CLASSIC_CLUSTER
    assert resource.resource_id == "0421-193742-abcd1234"
    assert resource.name == "analytics-all-purpose"
    assert resource.state == "TERMINATED"

    md = resource.metadata
    assert md["cluster_id"] == "0421-193742-abcd1234"
    assert md["cluster_type"] == "UI"
    assert md["driver_node_type"] == "Standard_DS3_v2"
    assert md["worker_node_type"] == "Standard_DS3_v2"
    assert md["autoscaling"] == {"min_workers": 2, "max_workers": 8}
    assert md["auto_termination_minutes"] == 30


def test_classic_cluster_without_autoscale_reports_none_rather_than_inventing_one():
    fixed = {"cluster_id": "c1", "cluster_name": "fixed", "state": "RUNNING", "num_workers": 4}
    md = normalize_classic_cluster(fixed).metadata

    assert md["autoscaling"] is None
    assert md["num_workers"] == 4
    # A field Databricks did not send must not be fabricated.
    assert md["driver_node_type"] is None
    assert "spark_version" not in md


def test_serverless_compute_normalisation_is_not_a_classic_cluster():
    raw_objects = [
        {"id": SERVERLESS_IDS[0], "name": SERVERLESS_NAMES[0], "object_type": "serverless-compute"},
        {"id": SERVERLESS_IDS[1], "name": SERVERLESS_NAMES[1], "object_type": "serverless-compute"},
    ]
    resources = [normalize_serverless_compute(raw, source="/api/probe") for raw in raw_objects]

    assert [r.name for r in resources] == ["Default Interactive Compute", "Default Automated Compute"]
    for resource in resources:
        assert resource.resource_type == ResourceType.SERVERLESS_COMPUTE
        assert resource.resource_type != ResourceType.CLASSIC_CLUSTER
        assert resource.platform == "databricks"
        assert resource.metadata["discovered_from"] == "/api/probe"
        # Serverless compute has no classic-cluster sizing levers, so these
        # keys must be absent rather than null-filled.
        assert "driver_node_type" not in resource.metadata
        assert "worker_node_type" not in resource.metadata
        assert "auto_termination_minutes" not in resource.metadata


def test_sql_warehouse_normalisation_carries_size_autostop_and_scaling():
    serverless_starter, warehouse_db = (normalize_sql_warehouse(w) for w in RAW_WAREHOUSES)

    assert serverless_starter.resource_type == ResourceType.SQL_WAREHOUSE
    assert serverless_starter.resource_id == "b1c2d3e4f5a60718"
    assert serverless_starter.name == "Serverless Starter Warehouse"
    assert serverless_starter.state == "RUNNING"
    assert serverless_starter.metadata["warehouse_type"] == "PRO"
    assert serverless_starter.metadata["size"] == "2X-Small"
    assert serverless_starter.metadata["auto_stop_mins"] == 10
    assert serverless_starter.metadata["owner"] == "hema@example.com"

    assert warehouse_db.name == "warehouse_db"
    assert warehouse_db.state == "STOPPED"
    assert warehouse_db.metadata["scaling"] == {"min_num_clusters": 1, "max_num_clusters": 3}
    # No creator_name in the payload, so no invented owner.
    assert "owner" not in warehouse_db.metadata


# --- adapter-level discovery ------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_successful_discovery_returns_all_three_resource_types():
    _mock_all_ok()

    discovery = await _adapter().discover_compute_resources()

    assert [r.name for r in discovery.of_type(ResourceType.CLASSIC_CLUSTER)] == ["analytics-all-purpose"]
    assert [r.name for r in discovery.of_type(ResourceType.SERVERLESS_COMPUTE)] == [
        "Default Interactive Compute",
        "Default Automated Compute",
    ]
    assert [r.name for r in discovery.of_type(ResourceType.SQL_WAREHOUSE)] == [
        "Serverless Starter Warehouse",
        "warehouse_db",
    ]
    assert {s.resource_type: s.status for s in discovery.statuses} == {
        ResourceType.CLASSIC_CLUSTER: ResourceStatus.OK,
        ResourceType.SERVERLESS_COMPUTE: ResourceStatus.OK,
        ResourceType.SQL_WAREHOUSE: ResourceStatus.OK,
    }


@pytest.mark.asyncio
@respx.mock
async def test_empty_classic_cluster_list_is_ok_not_a_failure():
    respx.get(CLUSTERS_URL).mock(return_value=httpx.Response(200, json={"clusters": []}))
    _mock_serverless_ok()
    respx.get(WAREHOUSES_URL).mock(return_value=httpx.Response(200, json={"warehouses": RAW_WAREHOUSES}))

    discovery = await _adapter().discover_compute_resources()
    classic = [s for s in discovery.statuses if s.resource_type == ResourceType.CLASSIC_CLUSTER][0]

    assert discovery.of_type(ResourceType.CLASSIC_CLUSTER) == []
    assert classic.status == ResourceStatus.OK
    assert classic.reason is None
    # The other types still came back.
    assert len(discovery.of_type(ResourceType.SQL_WAREHOUSE)) == 2


@pytest.mark.asyncio
@respx.mock
async def test_partial_permission_failure_does_not_fail_the_whole_discovery():
    respx.get(CLUSTERS_URL).mock(return_value=httpx.Response(403, json={"message": "denied"}))
    _mock_serverless_ok()
    respx.get(WAREHOUSES_URL).mock(return_value=httpx.Response(200, json={"warehouses": RAW_WAREHOUSES}))

    discovery = await _adapter().discover_compute_resources()
    by_type = {s.resource_type: s for s in discovery.statuses}

    assert by_type[ResourceType.CLASSIC_CLUSTER].status == ResourceStatus.UNAVAILABLE
    assert by_type[ResourceType.CLASSIC_CLUSTER].reason == UnavailableReason.INSUFFICIENT_PERMISSIONS
    assert by_type[ResourceType.SQL_WAREHOUSE].status == ResourceStatus.OK
    assert by_type[ResourceType.SERVERLESS_COMPUTE].status == ResourceStatus.OK
    assert len(discovery.of_type(ResourceType.SQL_WAREHOUSE)) == 2


@pytest.mark.asyncio
@respx.mock
async def test_serverless_probe_reports_unavailable_rather_than_inventing_resources():
    respx.get(CLUSTERS_URL).mock(return_value=httpx.Response(200, json={"clusters": []}))
    for url in SERVERLESS_URLS:
        respx.get(url).mock(return_value=httpx.Response(404, json={"message": "not found"}))
    respx.get(WAREHOUSES_URL).mock(return_value=httpx.Response(200, json={"warehouses": RAW_WAREHOUSES}))

    discovery = await _adapter().discover_compute_resources()
    serverless = [s for s in discovery.statuses if s.resource_type == ResourceType.SERVERLESS_COMPUTE][0]

    assert discovery.of_type(ResourceType.SERVERLESS_COMPUTE) == []
    assert serverless.status == ResourceStatus.UNAVAILABLE
    assert serverless.reason == UnavailableReason.NOT_EXPOSED_BY_WORKSPACE_API


@pytest.mark.asyncio
@respx.mock
async def test_discovery_never_reads_a_system_table_or_creates_a_resource():
    _mock_all_ok()

    await _adapter().discover_compute_resources()

    for call in respx.calls:
        assert call.request.method == "GET", "discovery must be read-only"
        assert "system.compute" not in str(call.request.url)
        assert "system.billing" not in str(call.request.url)


@pytest.mark.asyncio
async def test_unconfigured_databricks_reports_every_type_unavailable():
    adapter = DatabricksAdapter(endpoint="", auth_metadata={}, secret=None)

    discovery = await adapter.discover_compute_resources()

    assert discovery.resources == []
    assert {s.reason for s in discovery.statuses} == {UnavailableReason.NOT_CONFIGURED}
    assert len(discovery.statuses) == 3


# --- API endpoint -----------------------------------------------------------


@pytest.fixture
def databricks_environment(db_session, customer, databricks_connection):
    """A Databricks environment whose PAT is encrypted at rest, as in production."""
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
def test_resources_endpoint_returns_normalised_resources(customer, databricks_environment):
    _mock_all_ok()

    with TestClient(app) as client:
        resp = client.get("/api/databricks/resources", headers={"X-Customer-Id": customer.id})

    assert resp.status_code == 200
    body = resp.json()
    assert body["platform"] == "databricks"
    assert body["connected"] is True

    by_type: dict[str, list[str]] = {}
    for resource in body["resources"]:
        by_type.setdefault(resource["resource_type"], []).append(resource["name"])

    assert by_type["CLASSIC_CLUSTER"] == ["analytics-all-purpose"]
    assert by_type["SERVERLESS_COMPUTE"] == ["Default Interactive Compute", "Default Automated Compute"]
    assert by_type["SQL_WAREHOUSE"] == ["Serverless Starter Warehouse", "warehouse_db"]


@respx.mock
def test_resources_endpoint_reports_unavailable_type_without_failing(customer, databricks_environment):
    respx.get(CLUSTERS_URL).mock(return_value=httpx.Response(200, json={"clusters": []}))
    for url in SERVERLESS_URLS:
        respx.get(url).mock(return_value=httpx.Response(403, json={"message": "denied"}))
    respx.get(WAREHOUSES_URL).mock(return_value=httpx.Response(200, json={"warehouses": RAW_WAREHOUSES}))

    with TestClient(app) as client:
        resp = client.get("/api/databricks/resources", headers={"X-Customer-Id": customer.id})

    assert resp.status_code == 200
    statuses = {s["resource_type"]: s for s in resp.json()["statuses"]}
    assert statuses["SERVERLESS_COMPUTE"]["status"] == "UNAVAILABLE"
    assert statuses["SERVERLESS_COMPUTE"]["reason"] == "INSUFFICIENT_PERMISSIONS"
    assert statuses["SQL_WAREHOUSE"]["status"] == "OK"


@respx.mock
def test_response_never_leaks_the_access_token(customer, databricks_environment):
    _mock_all_ok()

    with TestClient(app) as client:
        resp = client.get("/api/databricks/resources", headers={"X-Customer-Id": customer.id})

    raw = resp.text
    assert TOKEN not in raw
    assert "Authorization" not in raw
    assert "Bearer" not in raw
    # Nothing token-shaped anywhere in the nested metadata either.
    assert "dapi" not in json.dumps(resp.json())


def test_missing_databricks_environment_is_a_clear_404(customer, fabric_connection):
    with TestClient(app) as client:
        resp = client.get("/api/databricks/resources", headers={"X-Customer-Id": customer.id})

    assert resp.status_code == 404
    assert "Databricks" in resp.json()["detail"]
