"""
Step 3 provisioning tests.

HTTP is mocked at the boundary with respx, so the adapter's real URL building,
long-running-operation handling and error mapping run for real. These are
deterministic unit tests, NOT live Fabric provisioning.

A temporary package directory is used for the "assets present" cases so the
tests do not depend on whether real ACELO notebooks happen to be bundled.
"""

import base64
import json
import uuid
from pathlib import Path

import httpx
import pytest
import respx

from models import Connection, Customer, Environment, Resource
from platforms.errors import ErrorCode
from platforms.fabric import FabricAdapter
from services import environment_service, package_registry, provisioning_service

FABRIC_BASE = "https://api.fabric.microsoft.com/v1"
WORKSPACE_ID = "ws-step3-0001"

NOTEBOOK_IPYNB = json.dumps(
    {"cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
).encode()


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from main import app

    return TestClient(app)


@pytest.fixture
def env(db_session, customer):
    return environment_service.create_environment(
        db_session,
        customer.id,
        name="Fabric Prod",
        platform="fabric",
        tenant_id="t",
        workspace_id=WORKSPACE_ID,
        client_id="c",
        client_secret="s",
        endpoint=None,
    )


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setattr(FabricAdapter, "_acquire_token", lambda self: ("fake-token", None))


@pytest.fixture
def package_with_assets(tmp_path, monkeypatch):
    """A package dir where all three notebooks exist."""
    return _build_package(tmp_path, monkeypatch, domains=("cluster", "query", "storage"))


@pytest.fixture
def package_cluster_only(tmp_path, monkeypatch):
    """Cluster present; query/storage assets genuinely absent."""
    return _build_package(tmp_path, monkeypatch, domains=("cluster",))


def _build_package(tmp_path: Path, monkeypatch, domains: tuple[str, ...], version="1.0.0"):
    root = tmp_path / "optimization_package"
    manifest = {
        "package_name": "ACELO Optimization Package",
        "package_version": version,
        "namespace": "ACELO",
        "assets": [
            {
                "domain": d,
                "folder": d.capitalize(),
                "display_name": f"ACELO {d.capitalize()} Optimization",
                "source": f"{d}/{d}_optimization.ipynb",
                "required": d == "cluster",
                "resource_key": f"{d}_notebook_id",
            }
            for d in ("cluster", "query", "storage")
        ],
    }
    root.mkdir(parents=True)
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for d in domains:
        (root / d).mkdir(parents=True, exist_ok=True)
        (root / d / f"{d}_optimization.ipynb").write_bytes(NOTEBOOK_IPYNB)

    monkeypatch.setattr(package_registry, "PACKAGE_ROOT", root)
    monkeypatch.setattr(package_registry, "MANIFEST_PATH", root / "manifest.json")
    return root


def _mock_fabric(*, items=None, folders_supported=True, create_status=201, fail_domain=None):
    """Wires the Fabric endpoints provisioning touches."""
    items = items if items is not None else []

    respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}").mock(
        return_value=httpx.Response(200, json={"id": WORKSPACE_ID, "displayName": "Production Workspace"})
    )
    respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items").mock(
        return_value=httpx.Response(200, json={"value": items})
    )
    if folders_supported:
        respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/folders").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        respx.post(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/folders").mock(
            side_effect=lambda req: httpx.Response(
                201, json={"id": "folder-" + json.loads(req.content)["displayName"], "displayName": "f"}
            )
        )
    else:
        # A tenant without the folders API rejects both verbs.
        respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/folders").mock(
            return_value=httpx.Response(404)
        )
        respx.post(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/folders").mock(
            return_value=httpx.Response(404)
        )

    created: dict[str, str] = {}

    def create_notebook(request):
        body = json.loads(request.content)
        name = body["displayName"]
        if fail_domain and fail_domain.lower() in name.lower():
            return httpx.Response(403, json={"errorCode": "InsufficientPrivileges"})
        item_id = f"real-item-{len(created) + 1}"
        created[name] = item_id
        return httpx.Response(create_status, json={"id": item_id, "displayName": name})

    respx.post(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/notebooks").mock(side_effect=create_notebook)

    # verification endpoints
    respx.get(url__regex=rf"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items/.+").mock(
        return_value=httpx.Response(200, json={"id": "real-item-1", "type": "Notebook"})
    )
    respx.post(url__regex=rf"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/notebooks/.+/getDefinition").mock(
        return_value=httpx.Response(
            200,
            json={"definition": {"parts": [{"path": "notebook-content.ipynb", "payload": base64.b64encode(NOTEBOOK_IPYNB).decode()}]}},
        )
    )
    respx.post(url__regex=rf"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/notebooks/.+/updateDefinition").mock(
        return_value=httpx.Response(200, json={})
    )
    return created


# --------------------------------------------------------------------------
# 1. Provisioning requires explicit action
# --------------------------------------------------------------------------

@respx.mock
def test_connecting_and_discovering_never_provisions(client, token, package_with_assets):
    """Connect + discover must not write anything into the customer workspace."""
    _mock_fabric()
    create_route = respx.post(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/notebooks")

    created = client.post(
        "/api/environments",
        json={"name": "E", "platform": "fabric", "tenant_id": "t",
              "workspace_id": WORKSPACE_ID, "client_id": "c", "client_secret": "s"},
    )
    env_id = created.json()["id"]

    client.post(f"/api/environments/{env_id}/test")
    client.post(f"/api/environments/{env_id}/discover")

    assert not create_route.called  # nothing deployed
    status = client.get(f"/api/environments/{env_id}/provision/status").json()
    assert status["status"] == provisioning_service.NOT_INSTALLED


# --------------------------------------------------------------------------
# 2/13. Invalid environment + customer isolation
# --------------------------------------------------------------------------

def test_provision_rejects_unknown_environment(client):
    resp = client.post(f"/api/environments/{uuid.uuid4()}/provision")
    assert resp.status_code == 404


def test_provision_is_blocked_across_customers(client, db_session, customer, env):
    other = Customer(id=str(uuid.uuid4()), name="Other Co")
    db_session.add(other)
    db_session.commit()

    for path, method in [
        (f"/api/environments/{env.id}/provision", client.post),
        (f"/api/environments/{env.id}/provision/status", client.get),
    ]:
        assert method(path, headers={"X-Customer-Id": other.id}).status_code == 404


# --------------------------------------------------------------------------
# 3/4. Connection / workspace failures stop provisioning
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_authentication_failure_stops_provisioning(db_session, env, monkeypatch, package_with_assets):
    monkeypatch.setattr(FabricAdapter, "_acquire_token", lambda self: (None, "AADSTS bad secret"))
    create_route = respx.post(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/notebooks")

    state = await provisioning_service.provision_environment(db_session, env)

    assert state["status"] == provisioning_service.FAILED
    assert state["error_code"] == ErrorCode.AUTHENTICATION_FAILED
    assert not create_route.called


@pytest.mark.asyncio
@respx.mock
async def test_inaccessible_workspace_stops_provisioning(db_session, env, token, package_with_assets):
    respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}").mock(return_value=httpx.Response(403))
    create_route = respx.post(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/notebooks")

    state = await provisioning_service.provision_environment(db_session, env)

    assert state["status"] == provisioning_service.FAILED
    assert state["error_code"] == ErrorCode.PERMISSION_DENIED
    assert not create_route.called


# --------------------------------------------------------------------------
# 5/6/7. Success, registration, REAL resource IDs
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_successful_provisioning_registers_real_fabric_item_ids(db_session, env, token, package_with_assets):
    _mock_fabric()
    state = await provisioning_service.provision_environment(db_session, env)

    assert state["status"] == provisioning_service.INSTALLED
    assert state["package_version_installed"] == "1.0.0"

    resources = (
        db_session.query(Resource)
        .filter(Resource.environment_id == env.id, Resource.status == provisioning_service.ACELO_OWNED)
        .all()
    )
    assert len(resources) == 3
    for r in resources:
        # Real Fabric IDs from the API response — not generated locally.
        assert r.platform_resource_id.startswith("real-item-")
        assert r.resource_type == "Notebook"
        detail = json.loads(r.detail_json)
        assert detail["managed_by"] == "acelo"
        assert detail["asset_checksum"]

    mapping = provisioning_service.resolved_domains(db_session, env)
    assert set(mapping) == {"cluster", "query", "storage"}


@pytest.mark.asyncio
@respx.mock
async def test_provisioned_ids_are_resolved_by_the_execution_layer(db_session, env, token, package_with_assets):
    """Step 2's adapter must pick up the provisioned notebook IDs — no hardcoding."""
    _mock_fabric()
    await provisioning_service.provision_environment(db_session, env)

    adapter = environment_service.build_adapter(db_session, env)
    mapping = provisioning_service.resolved_domains(db_session, env)

    assert adapter.auth_metadata["cluster_notebook_id"] == mapping["cluster"]
    assert adapter.auth_metadata["query_notebook_id"] == mapping["query"]
    assert adapter._resolve_notebook_id("cluster") == mapping["cluster"]


# --------------------------------------------------------------------------
# 8. Idempotency
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_second_provisioning_creates_no_duplicates(db_session, env, token, package_with_assets):
    _mock_fabric()
    await provisioning_service.provision_environment(db_session, env)
    first = provisioning_service.resolved_domains(db_session, env)

    # Second run: the items now already exist in the workspace.
    respx.get(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items").mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    {"id": item_id, "displayName": f"ACELO {d.capitalize()} Optimization", "type": "Notebook"}
                    for d, item_id in first.items()
                ]
            },
        )
    )
    create_route = respx.post(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/notebooks")
    create_route.reset()

    state = await provisioning_service.provision_environment(db_session, env)

    assert state["status"] == provisioning_service.INSTALLED
    assert not create_route.called  # adopted, not recreated
    assert provisioning_service.resolved_domains(db_session, env) == first
    assert (
        db_session.query(Resource)
        .filter(Resource.environment_id == env.id, Resource.status == provisioning_service.ACELO_OWNED)
        .count()
        == 3
    )


# --------------------------------------------------------------------------
# 9. Partial failure is reported honestly
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_partial_failure_reports_failed_not_success(db_session, env, token, package_with_assets):
    _mock_fabric(fail_domain="Query")
    state = await provisioning_service.provision_environment(db_session, env)

    assert state["status"] == provisioning_service.FAILED
    assert state["domains"]["cluster"]["deployed"] is True
    assert state["domains"]["query"]["deployed"] is False
    assert state["domains"]["query"]["error_code"] == ErrorCode.PERMISSION_DENIED
    assert env.status != environment_service.STATUS_ENVIRONMENT_READY


# --------------------------------------------------------------------------
# 10. No customer resource deletion
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_provisioning_never_issues_a_delete(db_session, env, token, package_with_assets):
    _mock_fabric()
    delete_route = respx.delete(url__regex=rf"{FABRIC_BASE}/.*")
    await provisioning_service.provision_environment(db_session, env)
    assert not delete_route.called


@pytest.mark.asyncio
@respx.mock
async def test_existing_customer_notebook_with_other_name_is_untouched(db_session, env, token, package_with_assets):
    """A customer's own notebooks must not be adopted, updated or overwritten."""
    customer_item = {"id": "customer-owned-1", "displayName": "Monthly Finance Report", "type": "Notebook"}
    _mock_fabric(items=[customer_item])
    update_route = respx.post(
        f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/notebooks/customer-owned-1/updateDefinition"
    )

    await provisioning_service.provision_environment(db_session, env)

    assert not update_route.called
    registered = [
        r.platform_resource_id
        for r in db_session.query(Resource).filter(Resource.environment_id == env.id).all()
    ]
    assert "customer-owned-1" not in registered


# --------------------------------------------------------------------------
# 11/12. No secrets in responses or logs
# --------------------------------------------------------------------------

@respx.mock
def test_provisioning_responses_contain_no_credentials(client, token, package_with_assets):
    _mock_fabric()
    secret = "SUPER-SECRET-PROV-123"
    created = client.post(
        "/api/environments",
        json={"name": "E", "platform": "fabric", "tenant_id": "t",
              "workspace_id": WORKSPACE_ID, "client_id": "c", "client_secret": secret},
    )
    env_id = created.json()["id"]

    bodies = [
        client.post(f"/api/environments/{env_id}/provision").text,
        client.get(f"/api/environments/{env_id}/provision/status").text,
        client.get(f"/api/environments/{env_id}/readiness").text,
        client.get("/api/environments/package/availability").text,
    ]
    for body in bodies:
        assert secret not in body
        assert "client_secret" not in body
        assert "secret_encrypted" not in body
        assert "Bearer " not in body


@pytest.mark.asyncio
@respx.mock
async def test_provisioning_logs_contain_no_credentials(db_session, env, token, package_with_assets, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    _mock_fabric(fail_domain="Query")
    await provisioning_service.provision_environment(db_session, env)

    text = caplog.text
    assert "fake-token" not in text
    assert "Bearer" not in text
    assert "client_secret" not in text


# --------------------------------------------------------------------------
# 14. Readiness only after verification
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_environment_not_ready_until_package_verified(db_session, env, token, package_with_assets, client, customer):
    _mock_fabric()

    # Connected + discovered, but not provisioned.
    await environment_service.test_environment_connection(db_session, env)
    await environment_service.discover_environment(db_session, env)
    readiness = client.get(
        f"/api/environments/{env.id}/readiness", headers={"X-Customer-Id": customer.id}
    ).json()
    assert readiness["ready_for_analysis"] is False
    assert readiness["package_status"] == provisioning_service.NOT_INSTALLED

    await provisioning_service.provision_environment(db_session, env)
    readiness = client.get(
        f"/api/environments/{env.id}/readiness", headers={"X-Customer-Id": customer.id}
    ).json()
    assert readiness["package_status"] == provisioning_service.INSTALLED
    assert readiness["cluster_ready"] is True
    assert readiness["ready_for_analysis"] is True


@pytest.mark.asyncio
@respx.mock
async def test_failed_verification_blocks_readiness(db_session, env, token, package_with_assets):
    """An item that exists but has no definition must not count as ready."""
    _mock_fabric()
    respx.post(url__regex=rf"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/notebooks/.+/getDefinition").mock(
        return_value=httpx.Response(200, json={"definition": {"parts": []}})
    )

    state = await provisioning_service.provision_environment(db_session, env)

    assert state["status"] == provisioning_service.FAILED
    assert env.status != environment_service.STATUS_ENVIRONMENT_READY
    assert provisioning_service.resolved_domains(db_session, env) == {}


# --------------------------------------------------------------------------
# 15/16/17. Missing assets — never fabricated
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_missing_query_and_storage_assets_reported_not_invented(
    db_session, env, token, package_cluster_only
):
    _mock_fabric()
    state = await provisioning_service.provision_environment(db_session, env)

    # Cluster (the required asset) deployed, so overall install succeeds.
    assert state["status"] == provisioning_service.INSTALLED
    assert state["domains"]["cluster"]["deployed"] is True

    for domain in ("query", "storage"):
        assert state["domains"][domain]["asset_available"] is False
        assert state["domains"][domain]["deployed"] is False
        assert state["domains"][domain]["error_code"] == ErrorCode.ASSET_MISSING
    assert set(state["missing_assets"]) == {"query", "storage"}
    assert set(provisioning_service.resolved_domains(db_session, env)) == {"cluster"}


@pytest.mark.asyncio
@respx.mock
async def test_missing_required_cluster_asset_fails_provisioning(db_session, env, token, tmp_path, monkeypatch):
    _build_package(tmp_path, monkeypatch, domains=("query",))
    _mock_fabric()

    state = await provisioning_service.provision_environment(db_session, env)

    assert state["status"] == provisioning_service.FAILED
    assert state["domains"]["cluster"]["error_code"] == ErrorCode.ASSET_MISSING


@pytest.mark.asyncio
@respx.mock
async def test_empty_package_deploys_nothing_at_all(db_session, env, token, tmp_path, monkeypatch):
    """The real repository state: no assets bundled -> nothing is deployed."""
    _build_package(tmp_path, monkeypatch, domains=())
    create_route = respx.post(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/notebooks")

    state = await provisioning_service.provision_environment(db_session, env)

    assert state["status"] == provisioning_service.FAILED
    assert state["error_code"] == ErrorCode.ASSET_MISSING
    assert not create_route.called
    assert provisioning_service.resolved_domains(db_session, env) == {}


def test_real_repository_package_reports_its_assets_honestly():
    """Guards against a placeholder notebook being slipped in to fake a green run."""
    availability = package_registry.package_availability()
    for asset in availability["assets"]:
        if asset["available"]:
            path = package_registry.PACKAGE_ROOT / asset["source"]
            content = path.read_bytes()
            # A real optimization notebook is not a 200-byte stub.
            assert len(content) > 2000, f"{asset['source']} looks like a placeholder"


# --------------------------------------------------------------------------
# 18. Unsupported Fabric capability (folders)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_folders_unsupported_falls_back_without_claiming_folders(
    db_session, env, token, package_with_assets
):
    _mock_fabric(folders_supported=False)
    state = await provisioning_service.provision_environment(db_session, env)

    assert state["status"] == provisioning_service.INSTALLED
    detail = json.loads(env.provisioning_detail_json)
    assert detail["folders_supported"] is False


@pytest.mark.asyncio
async def test_non_fabric_platform_is_unsupported(db_session, customer):
    databricks_env = environment_service.create_environment(
        db_session, customer.id, name="DBX", platform="databricks",
        tenant_id=None, workspace_id=None, client_id=None, client_secret=None, endpoint="https://x",
    )
    state = await provisioning_service.provision_environment(db_session, databricks_env)
    assert state["status"] == provisioning_service.FAILED
    assert state["error_code"] == ErrorCode.UNSUPPORTED


# --------------------------------------------------------------------------
# 19. Retry after failure
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_retry_after_failure_succeeds(db_session, env, token, package_with_assets):
    _mock_fabric(fail_domain="Query")
    first = await provisioning_service.provision_environment(db_session, env)
    assert first["status"] == provisioning_service.FAILED

    respx.clear()
    _mock_fabric()  # the permission problem is fixed
    second = await provisioning_service.provision_environment(db_session, env)

    assert second["status"] == provisioning_service.INSTALLED
    assert set(provisioning_service.resolved_domains(db_session, env)) == {"cluster", "query", "storage"}


# --------------------------------------------------------------------------
# 20. Dashboard no longer uses fabricated execution data
# --------------------------------------------------------------------------

def test_overview_shows_honest_empty_state_without_real_runs(client, customer):
    body = client.get("/api/overview", headers={"X-Customer-Id": customer.id}).json()

    assert body["hasData"] is False
    assert body["completedRuns"] == 0
    # Unknown is null ("Not available" in the UI), never a fabricated 0.
    assert body["kpis"]["monthlyCost"] is None
    assert body["kpis"]["potentialSavings"] is None
    assert body["kpis"]["openOpportunities"] == 0
    # No invented health score.
    assert body["kpis"]["optimizationHealth"] is None
    for block in body["health"]:
        assert block["health"] is None
        assert block["potentialSavings"] is None


def test_no_endpoint_seeds_fabricated_data(client, db_session, customer):
    from models import Recommendation

    for path in ("/api/overview", "/api/optimizations", "/api/approvals", "/api/history"):
        client.get(path, headers={"X-Customer-Id": customer.id})

    # The old code seeded 4 hardcoded recommendations on these reads.
    assert db_session.query(Recommendation).count() == 0


def test_seeding_functions_are_never_invoked_by_the_app():
    """The demo seeder was removed; no seeding call may reappear in the app."""
    import pathlib

    backend = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    for py in list(backend.glob("api/*.py")) + list(backend.glob("*.py")) + list(backend.glob("services/*.py")):
        if py.name == "optimization_engine.py":
            continue
        text = py.read_text(encoding="utf-8")
        for call in ("init_default_data(", "seed_initial_recommendations(", "execute_full_domain_analysis("):
            # Ignore the explanatory comment in main.py.
            for line in text.splitlines():
                if call in line and not line.strip().startswith("#"):
                    offenders.append(f"{py.name}: {line.strip()}")
    assert not offenders, offenders


# --------------------------------------------------------------------------
# Readiness must reflect the real provisioning state
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_readiness_not_ready_when_provisioning_failed(
    db_session, env, token, package_with_assets, client, customer
):
    """
    Connection and discovery succeed, then folder creation is refused (this is
    what InsufficientScopes looks like). Readiness must NOT say ready.
    """
    _mock_fabric()
    await environment_service.test_environment_connection(db_session, env)
    await environment_service.discover_environment(db_session, env)

    respx.post(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/folders").mock(
        return_value=httpx.Response(403, json={"errorCode": "InsufficientScopes"})
    )
    state = await provisioning_service.provision_environment(db_session, env)
    assert state["status"] == provisioning_service.FAILED

    readiness = client.get(
        f"/api/environments/{env.id}/readiness", headers={"X-Customer-Id": customer.id}
    ).json()

    # The early checks genuinely passed...
    assert readiness["authentication"] is True
    assert readiness["workspace_access"] is True
    assert readiness["environment_discovery"] is True
    # ...but nothing is deployed, so the environment is not analysis-ready.
    assert readiness["package_status"] == provisioning_service.FAILED
    assert readiness["cluster_ready"] is False
    assert readiness["ready_for_analysis"] is False


@pytest.mark.asyncio
@respx.mock
async def test_readiness_ready_when_installed_and_cluster_deployed(
    db_session, env, token, package_with_assets, client, customer
):
    _mock_fabric()
    await environment_service.test_environment_connection(db_session, env)
    await environment_service.discover_environment(db_session, env)
    state = await provisioning_service.provision_environment(db_session, env)
    assert state["status"] == provisioning_service.INSTALLED

    readiness = client.get(
        f"/api/environments/{env.id}/readiness", headers={"X-Customer-Id": customer.id}
    ).json()

    assert readiness["cluster_ready"] is True
    assert readiness["ready_for_analysis"] is True


@pytest.mark.asyncio
@respx.mock
async def test_readiness_not_ready_when_no_runnable_domain(
    db_session, env, token, client, customer, tmp_path, monkeypatch
):
    """Nothing bundled -> nothing deployable -> no runnable capability."""
    _build_package(tmp_path, monkeypatch, domains=())
    _mock_fabric()
    await environment_service.test_environment_connection(db_session, env)
    await environment_service.discover_environment(db_session, env)
    await provisioning_service.provision_environment(db_session, env)

    readiness = client.get(
        f"/api/environments/{env.id}/readiness", headers={"X-Customer-Id": customer.id}
    ).json()

    assert readiness["cluster_ready"] is False
    assert readiness["query_ready"] is False
    assert readiness["storage_ready"] is False
    assert readiness["ready_for_analysis"] is False


@pytest.mark.asyncio
@respx.mock
async def test_missing_query_and_storage_are_not_reported_ready(
    db_session, env, token, package_cluster_only, client, customer
):
    """Cluster alone is runnable; absent assets must not claim readiness."""
    _mock_fabric()
    await environment_service.test_environment_connection(db_session, env)
    await environment_service.discover_environment(db_session, env)
    state = await provisioning_service.provision_environment(db_session, env)
    assert state["status"] == provisioning_service.INSTALLED

    readiness = client.get(
        f"/api/environments/{env.id}/readiness", headers={"X-Customer-Id": customer.id}
    ).json()

    assert readiness["cluster_ready"] is True
    assert readiness["query_ready"] is False
    assert readiness["storage_ready"] is False
    # Cluster being runnable is enough for the environment overall.
    assert readiness["ready_for_analysis"] is True


# ---------------------------------------------------------------------------
# Cluster-only readiness
#
# Cluster does not call into the Query or Storage notebooks. A customer who has
# only the Cluster notebook deployed must be able to run Cluster analysis, even
# while the environment as a whole reports NOT READY.
# ---------------------------------------------------------------------------


def _configure_cluster_tables(db_session, env):
    """Cluster configuration lives in the Environment Resource Registry (domain=cluster)."""
    from services import resource_registry

    resource_registry.update(db_session, env, "cluster", {
        "source_table": "realistic_cluster_dataset",
        "result_table": "acelo_cluster_recommendations",
        "lakehouse_database": "Data",
    })


@pytest.mark.asyncio
@respx.mock
async def test_cluster_is_runnable_even_when_query_and_storage_are_absent(
    db_session, env, token, package_cluster_only, client, customer
):
    _mock_fabric()
    await environment_service.test_environment_connection(db_session, env)
    await environment_service.discover_environment(db_session, env)
    await provisioning_service.provision_environment(db_session, env)
    _configure_cluster_tables(db_session, env)

    readiness = client.get(
        f"/api/environments/{env.id}/readiness", headers={"X-Customer-Id": customer.id}
    ).json()

    assert readiness["query_ready"] is False
    assert readiness["storage_ready"] is False
    assert readiness["cluster_ready_for_analysis"] is True
    assert readiness["cluster_blocked_reason"] is None


@pytest.mark.asyncio
@respx.mock
async def test_cluster_blocked_reason_names_missing_table_settings(
    db_session, env, token, package_cluster_only, client, customer
):
    """An unconfigured Cluster must say what is missing, not just 'not ready'."""
    _mock_fabric()
    await environment_service.test_environment_connection(db_session, env)
    await environment_service.discover_environment(db_session, env)
    await provisioning_service.provision_environment(db_session, env)

    readiness = client.get(
        f"/api/environments/{env.id}/readiness", headers={"X-Customer-Id": customer.id}
    ).json()

    assert readiness["cluster_ready_for_analysis"] is False
    assert readiness["cluster_configured"] is False
    assert "source_table" in readiness["cluster_blocked_reason"]


@pytest.mark.asyncio
@respx.mock
async def test_failed_provisioning_still_blocks_cluster(
    db_session, env, token, package_with_assets, client, customer
):
    """Cluster independence must not become a way to claim readiness falsely."""
    _mock_fabric()
    await environment_service.test_environment_connection(db_session, env)
    await environment_service.discover_environment(db_session, env)
    respx.post(f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/folders").mock(
        return_value=httpx.Response(403, json={"errorCode": "InsufficientScopes"})
    )
    await provisioning_service.provision_environment(db_session, env)
    _configure_cluster_tables(db_session, env)

    readiness = client.get(
        f"/api/environments/{env.id}/readiness", headers={"X-Customer-Id": customer.id}
    ).json()

    assert readiness["cluster_ready_for_analysis"] is False
    assert readiness["ready_for_analysis"] is False
    assert "notebook is not deployed" in readiness["cluster_blocked_reason"]
