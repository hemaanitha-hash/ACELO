"""
Phase 1 tests: configuration validation, authentication, workspace access,
discovery, error mapping, customer isolation, and credential non-exposure.

Network is mocked at the HTTP boundary with respx (so the adapter's real URL
construction, status handling and pagination are exercised for real), and Azure
AD sign-in is mocked at the token boundary only.
"""

import json
import uuid

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from models import Connection, Customer, Environment, Resource
from platforms.errors import ErrorCode
from platforms.fabric import FabricAdapter
from services import environment_service

FABRIC_BASE = "https://api.fabric.microsoft.com/v1"
WORKSPACE_ID = "11111111-2222-3333-4444-555555555555"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _make_environment(db_session, customer, *, workspace_id=WORKSPACE_ID, secret="client-secret"):
    env = environment_service.create_environment(
        db_session,
        customer.id,
        name="Fabric Test",
        platform="fabric",
        tenant_id="tenant-abc",
        workspace_id=workspace_id,
        client_id="client-abc",
        client_secret=secret,
        endpoint=None,
    )
    return env


def _patch_token(monkeypatch, token="fake-token", error=None):
    monkeypatch.setattr(
        FabricAdapter, "_acquire_token", lambda self: (token, error)
    )


@pytest.fixture
def client():
    from main import app

    return TestClient(app)


# --------------------------------------------------------------------------
# 1. Fabric configuration validation
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_missing_workspace_id_reports_invalid_configuration(db_session, customer, monkeypatch):
    env = _make_environment(db_session, customer, workspace_id=None)
    _patch_token(monkeypatch)

    result = await environment_service.test_environment_connection(db_session, env)

    assert result["connected"] is False
    assert result["error_code"] == ErrorCode.INVALID_CONFIGURATION
    assert "workspace_id" in result["message"]


@pytest.mark.asyncio
async def test_missing_client_secret_reports_invalid_configuration(db_session, customer, monkeypatch):
    env = _make_environment(db_session, customer, secret=None)
    _patch_token(monkeypatch)

    result = await environment_service.test_environment_connection(db_session, env)

    assert result["connected"] is False
    assert result["error_code"] == ErrorCode.INVALID_CONFIGURATION
    assert "client_secret" in result["message"]


# --------------------------------------------------------------------------
# 2. Fabric authentication failure
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_authentication_failure_is_mapped_and_persisted(db_session, customer, monkeypatch):
    env = _make_environment(db_session, customer)
    _patch_token(monkeypatch, token=None, error="AADSTS7000215: Invalid client secret provided")

    result = await environment_service.test_environment_connection(db_session, env)

    assert result["connected"] is False
    assert result["error_code"] == ErrorCode.AUTHENTICATION_FAILED
    # The raw AAD diagnostic must not reach the user-facing message.
    assert "AADSTS7000215" not in result["message"]
    assert env.status == environment_service.STATUS_CONNECTION_FAILED
    assert env.last_error_code == ErrorCode.AUTHENTICATION_FAILED


# --------------------------------------------------------------------------
# 3. Fabric authentication + workspace access success
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_successful_connection_records_real_workspace_name(db_session, customer, monkeypatch):
    env = _make_environment(db_session, customer)
    _patch_token(monkeypatch)
    respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}").mock(
        return_value=httpx.Response(
            200, json={"id": WORKSPACE_ID, "displayName": "Production Workspace", "type": "Workspace"}
        )
    )

    result = await environment_service.test_environment_connection(db_session, env)

    assert result["connected"] is True
    assert result["workspace_name"] == "Production Workspace"
    assert result["message"] == "Fabric connection verified"
    assert env.status == environment_service.STATUS_CONNECTED
    assert env.last_verified_at is not None


# --------------------------------------------------------------------------
# 4. Workspace access failure
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_workspace_permission_denied_is_mapped(db_session, customer, monkeypatch):
    env = _make_environment(db_session, customer)
    _patch_token(monkeypatch)
    respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}").mock(
        return_value=httpx.Response(403, json={"errorCode": "InsufficientPrivileges"})
    )

    result = await environment_service.test_environment_connection(db_session, env)

    assert result["connected"] is False
    assert result["error_code"] == ErrorCode.PERMISSION_DENIED
    assert "does not have" in result["message"]


@pytest.mark.asyncio
@respx.mock
async def test_workspace_not_found_is_mapped(db_session, customer, monkeypatch):
    env = _make_environment(db_session, customer)
    _patch_token(monkeypatch)
    respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}").mock(return_value=httpx.Response(404))

    result = await environment_service.test_environment_connection(db_session, env)

    assert result["error_code"] == ErrorCode.WORKSPACE_NOT_FOUND


@pytest.mark.asyncio
@respx.mock
async def test_platform_unreachable_is_mapped(db_session, customer, monkeypatch):
    env = _make_environment(db_session, customer)
    _patch_token(monkeypatch)
    respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    result = await environment_service.test_environment_connection(db_session, env)

    assert result["error_code"] == ErrorCode.PLATFORM_API_UNAVAILABLE
    assert "connection refused" not in result["message"]


# --------------------------------------------------------------------------
# 5/6. Workspace + resource discovery
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_discovery_returns_real_items_and_persists_resources(db_session, customer, monkeypatch):
    env = _make_environment(db_session, customer)
    _patch_token(monkeypatch)

    respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}").mock(
        return_value=httpx.Response(200, json={"id": WORKSPACE_ID, "displayName": "Production Workspace"})
    )
    respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items").mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    {"id": "nb-1", "displayName": "Cluster Analysis", "type": "Notebook"},
                    {"id": "nb-2", "displayName": "Query Analysis", "type": "Notebook"},
                    {"id": "lh-1", "displayName": "Data", "type": "Lakehouse"},
                    {"id": "ex-1", "displayName": "finops_optimizer", "type": "MLExperiment"},
                ]
            },
        )
    )
    respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/folders").mock(
        return_value=httpx.Response(200, json={"value": [{"id": "f-1", "displayName": "ACELO"}]})
    )

    result = await environment_service.discover_environment(db_session, env)

    assert result["workspace"]["name"] == "Production Workspace"
    assert result["counts"] == {"Notebook": 2, "Lakehouse": 1, "Experiment": 1, "Folder": 1}
    names = {i["display_name"] for i in result["items"]}
    assert names == {"Cluster Analysis", "Query Analysis", "Data", "finops_optimizer", "ACELO"}

    persisted = db_session.query(Resource).filter(Resource.environment_id == env.id).all()
    assert len(persisted) == 5
    assert env.status == environment_service.STATUS_ENVIRONMENT_READY


@pytest.mark.asyncio
@respx.mock
async def test_discovery_follows_continuation_token(db_session, customer, monkeypatch):
    """A workspace larger than one page must not be silently truncated."""
    env = _make_environment(db_session, customer)
    _patch_token(monkeypatch)

    respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}").mock(
        return_value=httpx.Response(200, json={"id": WORKSPACE_ID, "displayName": "WS"})
    )
    respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items", params={"continuationToken": "tok1"}).mock(
        return_value=httpx.Response(
            200, json={"value": [{"id": "nb-2", "displayName": "Second", "type": "Notebook"}]}
        )
    )
    respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items").mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [{"id": "nb-1", "displayName": "First", "type": "Notebook"}],
                "continuationToken": "tok1",
            },
        )
    )
    respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/folders").mock(
        return_value=httpx.Response(404)
    )

    result = await environment_service.discover_environment(db_session, env)

    assert result["counts"] == {"Notebook": 2}


@pytest.mark.asyncio
@respx.mock
async def test_discovery_failure_is_mapped_and_does_not_mark_ready(db_session, customer, monkeypatch):
    env = _make_environment(db_session, customer)
    _patch_token(monkeypatch)
    respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}").mock(
        return_value=httpx.Response(200, json={"id": WORKSPACE_ID, "displayName": "WS"})
    )
    respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items").mock(return_value=httpx.Response(403))

    result = await environment_service.discover_environment(db_session, env)

    assert result["discovered"] is False
    assert result["error_code"] == ErrorCode.PERMISSION_DENIED
    assert env.status == environment_service.STATUS_DISCOVERY_FAILED


@pytest.mark.asyncio
@respx.mock
async def test_empty_workspace_discovers_zero_items_without_inventing_any(db_session, customer, monkeypatch):
    env = _make_environment(db_session, customer)
    _patch_token(monkeypatch)
    respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}").mock(
        return_value=httpx.Response(200, json={"id": WORKSPACE_ID, "displayName": "Empty WS"})
    )
    respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items").mock(
        return_value=httpx.Response(200, json={"value": []})
    )
    respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/folders").mock(
        return_value=httpx.Response(200, json={"value": []})
    )

    result = await environment_service.discover_environment(db_session, env)

    assert result["items"] == []
    assert result["counts"] == {}


# --------------------------------------------------------------------------
# 7. API error mapping (HTTP layer)
# --------------------------------------------------------------------------

@respx.mock
def test_api_test_endpoint_returns_safe_error_body(client, monkeypatch):
    monkeypatch.setattr(FabricAdapter, "_acquire_token", lambda self: (None, "AADSTS7000215: bad secret"))

    created = client.post(
        "/api/environments",
        json={
            "name": "Fabric Prod",
            "platform": "fabric",
            "tenant_id": "t",
            "workspace_id": WORKSPACE_ID,
            "client_id": "c",
            "client_secret": "s",
        },
    )
    assert created.status_code == 200
    env_id = created.json()["id"]

    resp = client.post(f"/api/environments/{env_id}/test")
    body = resp.json()

    assert resp.status_code == 200
    assert body["connected"] is False
    assert body["error_code"] == ErrorCode.AUTHENTICATION_FAILED
    assert "AADSTS" not in json.dumps(body)


# --------------------------------------------------------------------------
# 8. Environment isolation
# --------------------------------------------------------------------------

def test_customer_cannot_access_another_customers_environment(db_session, customer, client):
    other = Customer(id=str(uuid.uuid4()), name="Other Customer")
    db_session.add(other)
    db_session.commit()

    env = _make_environment(db_session, other)

    # Requesting as `customer` (a different customer) must not resolve it.
    resp = client.get(f"/api/environments/{env.id}", headers={"X-Customer-Id": customer.id})
    assert resp.status_code == 404

    for path, method in [
        (f"/api/environments/{env.id}/test", client.post),
        (f"/api/environments/{env.id}/discover", client.post),
        (f"/api/environments/{env.id}/resources", client.get),
        (f"/api/environments/{env.id}/readiness", client.get),
    ]:
        assert method(path, headers={"X-Customer-Id": customer.id}).status_code == 404


def test_list_environments_only_returns_own(db_session, customer, client):
    other = Customer(id=str(uuid.uuid4()), name="Other Customer 2")
    db_session.add(other)
    db_session.commit()

    mine = _make_environment(db_session, customer)
    _make_environment(db_session, other)

    resp = client.get("/api/environments", headers={"X-Customer-Id": customer.id})
    ids = [e["id"] for e in resp.json()]

    assert mine.id in ids
    assert len(ids) == 1


def test_service_lookup_enforces_customer_scope(db_session, customer):
    other = Customer(id=str(uuid.uuid4()), name="Other Customer 3")
    db_session.add(other)
    db_session.commit()
    env = _make_environment(db_session, other)

    assert environment_service.get_environment_for_customer(db_session, env.id, customer.id) is None
    assert environment_service.get_environment_for_customer(db_session, env.id, other.id) is not None


# --------------------------------------------------------------------------
# 9. No credentials in API responses
# --------------------------------------------------------------------------

def test_no_credentials_in_any_environment_api_response(client, db_session, customer, monkeypatch):
    secret = "super-secret-value-9182"
    created = client.post(
        "/api/environments",
        json={
            "name": "Fabric Prod",
            "platform": "fabric",
            "tenant_id": "tenant-abc",
            "workspace_id": WORKSPACE_ID,
            "client_id": "client-abc",
            "client_secret": secret,
        },
    )
    env_id = created.json()["id"]

    bodies = [
        created.text,
        client.get("/api/environments").text,
        client.get(f"/api/environments/{env_id}").text,
        client.get(f"/api/environments/{env_id}/resources").text,
        client.get(f"/api/environments/{env_id}/readiness").text,
    ]

    for body in bodies:
        assert secret not in body
        assert "client_secret" not in body
        assert "secret_encrypted" not in body
        assert "access_token" not in body


def test_secret_is_encrypted_at_rest_not_stored_plaintext(db_session, customer):
    secret = "plaintext-should-never-appear"
    env = _make_environment(db_session, customer, secret=secret)

    connection = db_session.query(Connection).filter(Connection.id == env.connection_id).first()
    assert connection.secret_encrypted is not None
    assert secret not in connection.secret_encrypted
    # ...but it must still decrypt correctly for server-side use.
    from services.crypto import get_cipher

    assert get_cipher().decrypt(connection.secret_encrypted) == secret


def test_environment_out_schema_has_no_credential_fields():
    from schemas.environment import EnvironmentOut

    forbidden = {"client_secret", "secret", "secret_encrypted", "access_token", "auth_metadata", "client_id"}
    assert forbidden.isdisjoint(EnvironmentOut.model_fields.keys())


# --------------------------------------------------------------------------
# readiness + no-faking guarantees
# --------------------------------------------------------------------------

def test_new_environment_is_not_ready_and_not_connected(client):
    created = client.post(
        "/api/environments",
        json={"name": "Fresh", "platform": "fabric", "workspace_id": WORKSPACE_ID},
    )
    env_id = created.json()["id"]
    assert created.json()["status"] == environment_service.STATUS_NOT_CONFIGURED

    readiness = client.get(f"/api/environments/{env_id}/readiness").json()
    assert readiness["ready_for_analysis"] is False
    assert readiness["authentication"] is False
    assert readiness["environment_discovery"] is False


def test_discovery_without_successful_connection_does_not_report_ready(client, monkeypatch):
    monkeypatch.setattr(FabricAdapter, "_acquire_token", lambda self: (None, "no token"))
    created = client.post(
        "/api/environments",
        json={
            "name": "Unverified",
            "platform": "fabric",
            "tenant_id": "t",
            "workspace_id": WORKSPACE_ID,
            "client_id": "c",
            "client_secret": "s",
        },
    )
    env_id = created.json()["id"]

    disc = client.post(f"/api/environments/{env_id}/discover").json()
    assert disc["discovered"] is False

    readiness = client.get(f"/api/environments/{env_id}/readiness").json()
    assert readiness["ready_for_analysis"] is False


@pytest.mark.asyncio
async def test_databricks_without_credentials_reports_not_configured_not_connected(db_session, customer):
    """Phase 1 must never fake a Databricks connection when none is configured."""
    env = environment_service.create_environment(
        db_session,
        customer.id,
        name="Databricks Dev",
        platform="databricks",
        tenant_id=None,
        workspace_id=None,
        client_id=None,
        client_secret=None,
        endpoint="",
    )
    result = await environment_service.test_environment_connection(db_session, env)

    assert result["connected"] is False
    assert result["error_code"] == ErrorCode.NOT_CONFIGURED


@pytest.mark.asyncio
async def test_file_platform_reports_not_configured(db_session, customer):
    env = environment_service.create_environment(
        db_session,
        customer.id,
        name="File source",
        platform="file",
        tenant_id=None,
        workspace_id=None,
        client_id=None,
        client_secret=None,
        endpoint=None,
    )
    result = await environment_service.test_environment_connection(db_session, env)

    assert result["connected"] is False
    assert result["error_code"] == ErrorCode.NOT_CONFIGURED


def test_readiness_does_not_report_authentication_after_auth_failure(client, monkeypatch):
    """
    Regression: discovery failing with AUTHENTICATION_FAILED sets status to
    discovery_failed. Readiness must not read that as 'authentication passed'.
    """
    monkeypatch.setattr(FabricAdapter, "_acquire_token", lambda self: (None, "bad secret"))
    created = client.post(
        "/api/environments",
        json={
            "name": "Auth fail",
            "platform": "fabric",
            "tenant_id": "t",
            "workspace_id": WORKSPACE_ID,
            "client_id": "c",
            "client_secret": "wrong",
        },
    )
    env_id = created.json()["id"]

    assert client.post(f"/api/environments/{env_id}/test").json()["connected"] is False
    assert client.post(f"/api/environments/{env_id}/discover").json()["discovered"] is False

    readiness = client.get(f"/api/environments/{env_id}/readiness").json()
    assert readiness["authentication"] is False
    assert readiness["workspace_access"] is False
    assert readiness["ready_for_analysis"] is False


@pytest.mark.asyncio
@respx.mock
async def test_readiness_true_only_after_real_connection_and_discovery(db_session, customer, monkeypatch, client):
    env = _make_environment(db_session, customer)
    _patch_token(monkeypatch)
    respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}").mock(
        return_value=httpx.Response(200, json={"id": WORKSPACE_ID, "displayName": "Production Workspace"})
    )
    respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items").mock(
        return_value=httpx.Response(
            200, json={"value": [{"id": "nb-1", "displayName": "Cluster Analysis", "type": "Notebook"}]}
        )
    )
    respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/folders").mock(
        return_value=httpx.Response(200, json={"value": []})
    )

    await environment_service.test_environment_connection(db_session, env)
    await environment_service.discover_environment(db_session, env)

    readiness = client.get(
        f"/api/environments/{env.id}/readiness", headers={"X-Customer-Id": customer.id}
    ).json()
    assert readiness["authentication"] is True
    assert readiness["workspace_access"] is True
    assert readiness["environment_discovery"] is True
    # Step 3 contract change: connection + discovery alone no longer make an
    # environment ready for analysis. Without a verified ACELO package there is
    # no notebook to run, so ready_for_analysis stays False. Previously this
    # asserted True, which reported an environment as analysis-ready when it had
    # no optimization assets deployed at all.
    assert readiness["ready_for_analysis"] is False
    assert readiness["package_status"] == "NOT_INSTALLED"
    assert readiness["cluster_ready"] is False


def test_unknown_customer_id_is_rejected_not_silently_defaulted(client, db_session, customer):
    """
    Regression: an unrecognised X-Customer-Id used to fall back to the first
    customer row, which let any caller read another customer's environments
    simply by sending a bogus ID.
    """
    env = _make_environment(db_session, customer)

    resp = client.get(f"/api/environments/{env.id}", headers={"X-Customer-Id": "cust_does_not_exist"})
    assert resp.status_code == 404

    listing = client.get("/api/environments", headers={"X-Customer-Id": "cust_does_not_exist"})
    assert listing.status_code == 404
