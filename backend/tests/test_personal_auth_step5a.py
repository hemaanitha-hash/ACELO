"""
Step 5A — delegated ("personal Microsoft account") Fabric authentication.

Covers the backend half: a per-request user token replaces the service-principal
client-credentials flow, without persisting the token, leaking it, or breaking
the existing service-principal path.
"""

import json
import uuid

import httpx
import pytest
import respx

from models import Connection, Customer, Environment
from platforms.errors import ErrorCode
from platforms.fabric import FabricAdapter
from services import environment_service, provisioning_service

FABRIC_BASE = "https://api.fabric.microsoft.com/v1"
WORKSPACE_ID = "ws-personal-01"
USER_TOKEN = "eyJ0eXAiOiJKV1QiLCJUSER-DELEGATED-TOKEN-VALUE"


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from main import app

    return TestClient(app)


def _personal_env(db_session, customer) -> Environment:
    return environment_service.create_environment(
        db_session,
        customer.id,
        name="Fabric (personal)",
        platform="fabric",
        auth_mode="user",
        workspace_id=WORKSPACE_ID,
    )


def _sp_env(db_session, customer) -> Environment:
    return environment_service.create_environment(
        db_session,
        customer.id,
        name="Fabric (SP)",
        platform="fabric",
        auth_mode="service_principal",
        tenant_id="t",
        workspace_id=WORKSPACE_ID,
        client_id="c",
        client_secret="sp-secret",
    )


# --------------------------------------------------------------------------
# Personal mode needs no client secret
# --------------------------------------------------------------------------

def test_user_mode_environment_stores_no_secret(db_session, customer):
    env = _personal_env(db_session, customer)
    connection = db_session.query(Connection).filter(Connection.id == env.connection_id).first()

    assert env.auth_mode == "user"
    assert connection.auth_method == "delegated"
    assert connection.secret_encrypted is None

    metadata = json.loads(connection.auth_metadata)
    assert "client_id" not in metadata
    assert "tenant_id" not in metadata


def test_service_principal_environment_is_unchanged(db_session, customer):
    env = _sp_env(db_session, customer)
    connection = db_session.query(Connection).filter(Connection.id == env.connection_id).first()

    assert env.auth_mode == "service_principal"
    assert connection.auth_method == "service_principal"
    assert connection.secret_encrypted is not None
    metadata = json.loads(connection.auth_metadata)
    assert metadata["client_id"] == "c"
    assert metadata["tenant_id"] == "t"


# --------------------------------------------------------------------------
# Token short-circuits the confidential-client flow
# --------------------------------------------------------------------------

def test_delegated_token_bypasses_msal(db_session, customer):
    env = _personal_env(db_session, customer)
    adapter = environment_service.build_adapter(db_session, env, USER_TOKEN)

    token, error = adapter._acquire_token()
    assert token == USER_TOKEN
    assert error is None


def test_delegated_mode_requires_only_a_workspace(db_session, customer):
    env = _personal_env(db_session, customer)
    adapter = environment_service.build_adapter(db_session, env, USER_TOKEN)
    # No tenant_id / client_id / secret, yet configuration validates.
    _, _, workspace_id, _ = adapter._require_config()
    assert workspace_id == WORKSPACE_ID


@pytest.mark.asyncio
async def test_personal_mode_without_a_token_does_not_silently_succeed(db_session, customer):
    """No token and no secret must fail, not fall through to an anonymous call."""
    env = _personal_env(db_session, customer)
    result = await environment_service.test_environment_connection(db_session, env)

    assert result["connected"] is False
    assert result["error_code"] in (
        ErrorCode.INVALID_CONFIGURATION,
        ErrorCode.AUTHENTICATION_FAILED,
    )


# --------------------------------------------------------------------------
# The token is actually used on the wire
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_delegated_token_is_sent_as_the_bearer(db_session, customer):
    env = _personal_env(db_session, customer)
    route = respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}").mock(
        return_value=httpx.Response(200, json={"id": WORKSPACE_ID, "displayName": "Prod WS"})
    )

    result = await environment_service.test_environment_connection(db_session, env, USER_TOKEN)

    assert result["connected"] is True
    assert result["workspace_name"] == "Prod WS"
    assert route.calls[0].request.headers["Authorization"] == f"Bearer {USER_TOKEN}"


@pytest.mark.asyncio
@respx.mock
async def test_delegated_401_and_403_map_to_real_errors(db_session, customer):
    env = _personal_env(db_session, customer)

    respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}").mock(return_value=httpx.Response(401))
    unauthorized = await environment_service.test_environment_connection(db_session, env, USER_TOKEN)
    assert unauthorized["connected"] is False
    assert unauthorized["error_code"] == ErrorCode.AUTHENTICATION_FAILED

    respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}").mock(return_value=httpx.Response(403))
    forbidden = await environment_service.test_environment_connection(db_session, env, USER_TOKEN)
    assert forbidden["connected"] is False
    assert forbidden["error_code"] == ErrorCode.PERMISSION_DENIED
    assert USER_TOKEN not in forbidden["message"]


@pytest.mark.asyncio
@respx.mock
async def test_discovery_and_provisioning_accept_the_delegated_token(db_session, customer):
    env = _personal_env(db_session, customer)

    respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}").mock(
        return_value=httpx.Response(200, json={"id": WORKSPACE_ID, "displayName": "Prod WS"})
    )
    respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items").mock(
        return_value=httpx.Response(
            200, json={"value": [{"id": "nb-1", "displayName": "Existing", "type": "Notebook"}]}
        )
    )
    respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/folders").mock(
        return_value=httpx.Response(200, json={"value": []})
    )

    discovery = await environment_service.discover_environment(db_session, env, USER_TOKEN)
    assert discovery["counts"] == {"Notebook": 1}

    respx.post(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/folders").mock(
        return_value=httpx.Response(201, json={"id": "f1", "displayName": "ACELO"})
    )
    create = respx.post(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/notebooks").mock(
        return_value=httpx.Response(201, json={"id": "nb-acelo", "displayName": "ACELO Cluster Optimization"})
    )
    respx.get(url__regex=rf"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items/.+").mock(
        return_value=httpx.Response(200, json={"id": "nb-acelo", "type": "Notebook"})
    )
    respx.post(url__regex=rf"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/notebooks/.+/getDefinition").mock(
        return_value=httpx.Response(200, json={"definition": {"parts": [{"path": "n.ipynb"}]}})
    )

    state = await provisioning_service.provision_environment(db_session, env, USER_TOKEN)

    assert state["status"] == provisioning_service.INSTALLED
    assert create.calls[0].request.headers["Authorization"] == f"Bearer {USER_TOKEN}"


# --------------------------------------------------------------------------
# The token is never persisted, logged or returned
# --------------------------------------------------------------------------

@respx.mock
def test_token_header_is_never_persisted_or_echoed(client, db_session, customer):
    respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}").mock(
        return_value=httpx.Response(200, json={"id": WORKSPACE_ID, "displayName": "Prod WS"})
    )

    created = client.post(
        "/api/environments",
        json={
            "name": "Fabric personal",
            "platform": "fabric",
            "auth_mode": "user",
            "workspace_id": WORKSPACE_ID,
        },
        headers={"X-Customer-Id": customer.id},
    )
    env_id = created.json()["id"]

    bodies = [
        created.text,
        client.post(
            f"/api/environments/{env_id}/test",
            headers={"X-Customer-Id": customer.id, "X-Fabric-Access-Token": USER_TOKEN},
        ).text,
        client.get(f"/api/environments/{env_id}", headers={"X-Customer-Id": customer.id}).text,
        client.get("/api/environments", headers={"X-Customer-Id": customer.id}).text,
    ]
    for body in bodies:
        assert USER_TOKEN not in body
        assert "X-Fabric-Access-Token" not in body
        assert "access_token" not in body

    # ...and nothing about it reached the database.
    environment = db_session.query(Environment).filter(Environment.id == env_id).first()
    connection = db_session.query(Connection).filter(Connection.id == environment.connection_id).first()
    assert connection.secret_encrypted is None
    assert USER_TOKEN not in (connection.auth_metadata or "")
    assert USER_TOKEN not in (environment.provisioning_detail_json or "")


@pytest.mark.asyncio
@respx.mock
async def test_token_never_appears_in_logs(db_session, customer, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    env = _personal_env(db_session, customer)
    respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}").mock(return_value=httpx.Response(403))

    await environment_service.test_environment_connection(db_session, env, USER_TOKEN)

    assert USER_TOKEN not in caplog.text
    assert "Bearer" not in caplog.text


def test_environment_out_schema_cannot_carry_a_token():
    from schemas.environment import EnvironmentOut

    forbidden = {"access_token", "delegated_token", "token", "client_secret", "secret_encrypted"}
    assert forbidden.isdisjoint(EnvironmentOut.model_fields.keys())


# --------------------------------------------------------------------------
# Service principal compatibility + isolation
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_service_principal_ignores_the_delegated_header(db_session, customer, monkeypatch):
    """An SP environment must keep using its own credentials, not a stray token."""
    env = _sp_env(db_session, customer)
    monkeypatch.setattr(FabricAdapter, "_acquire_token", lambda self: ("sp-token", None))

    route = respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}").mock(
        return_value=httpx.Response(200, json={"id": WORKSPACE_ID, "displayName": "Prod WS"})
    )
    result = await environment_service.test_environment_connection(db_session, env)

    assert result["connected"] is True
    assert route.calls[0].request.headers["Authorization"] == "Bearer sp-token"


def test_delegated_header_does_not_bypass_customer_isolation(client, db_session, customer):
    other = Customer(id=str(uuid.uuid4()), name="Other Co")
    db_session.add(other)
    db_session.commit()
    env = _personal_env(db_session, other)

    resp = client.post(
        f"/api/environments/{env.id}/test",
        headers={"X-Customer-Id": customer.id, "X-Fabric-Access-Token": USER_TOKEN},
    )
    assert resp.status_code == 404


def test_auth_mode_defaults_preserve_existing_records(db_session, customer):
    """Rows created before this change have no auth_mode and must still work."""
    env = _sp_env(db_session, customer)
    env.auth_mode = None
    db_session.commit()

    adapter = environment_service.build_adapter(db_session, env)
    assert adapter.delegated_token is None  # falls back to service principal


# --------------------------------------------------------------------------
# Mode naming: "user" is canonical, "personal" stays accepted
# --------------------------------------------------------------------------

def test_legacy_personal_mode_is_normalised_to_user(db_session, customer):
    """Environments created before the rename must keep working."""
    env = environment_service.create_environment(
        db_session,
        customer.id,
        name="Legacy",
        platform="fabric",
        auth_mode="personal",
        workspace_id=WORKSPACE_ID,
    )
    assert env.auth_mode == "user"
    assert environment_service.is_delegated(env.auth_mode) is True

    connection = db_session.query(Connection).filter(Connection.id == env.connection_id).first()
    assert connection.auth_method == "delegated"
    assert connection.secret_encrypted is None


def test_auth_mode_helpers():
    assert environment_service.normalize_auth_mode(None) == "service_principal"
    assert environment_service.normalize_auth_mode("personal") == "user"
    assert environment_service.normalize_auth_mode("user") == "user"
    assert environment_service.is_delegated("service_principal") is False
    assert environment_service.is_delegated("user") is True


def test_api_accepts_both_user_and_personal(client, customer):
    for mode in ("user", "personal"):
        created = client.post(
            "/api/environments",
            json={
                "name": f"Fabric {mode}",
                "platform": "fabric",
                "auth_mode": mode,
                "workspace_id": WORKSPACE_ID,
            },
            headers={"X-Customer-Id": customer.id},
        )
        assert created.status_code == 200, created.text
        # Both normalise to the canonical value on the way out.
        assert created.json()["auth_mode"] == "user"
