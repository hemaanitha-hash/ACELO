"""
Intent-driven execution with the Environment Resource Registry.

The AI Agent only decides the domain; the backend loads that domain's own
resource configuration. No domain ever receives another domain's settings, and
the user never enters table/lakehouse/pipeline settings to start a run.
"""

import json
import re
import uuid
from pathlib import Path

import pytest

from models import Connection, Customer, DomainResource, Environment, JobRun, Resource
from platforms.base import StartAnalysisResult
from services import job_service, provisioning_service, resource_registry

NB_CLUSTER = "11111111-1111-1111-1111-111111111111"
NB_QUERY = "22222222-2222-2222-2222-222222222222"
NB_STORAGE = "33333333-3333-3333-3333-333333333333"
LH_CLUSTER = "44444444-4444-4444-4444-444444444444"
LH_QUERY = "55555555-5555-5555-5555-555555555555"

CLUSTER = {"source_table": "realistic_cluster_dataset", "result_table": "acelo_cluster_recommendations",
           "source_schema": "dbo", "result_schema": "dbo", "lakehouse_id": LH_CLUSTER,
           "approval_tracking_table": "acelo_cluster_approval"}
QUERY = {"source_table": "query_execution_meta_data", "result_table": "query_email_input",
         "result_schema": "analytics", "lakehouse_id": LH_QUERY,
         "approval_tracking_table": "query_tracking_full_v1",
         "llm_key_vault_uri": "https://acelo-kv.vault.azure.net/", "llm_secret_name": "groq-api-key"}
STORAGE = {"source_table": "storage_inventory", "result_table": "storage_recommendations"}


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from main import app

    return TestClient(app)


def _environment(db, customer, env_id="env-reg", conn_id="conn-reg", metadata=None, platform="fabric"):
    connection = Connection(
        id=conn_id, customer_id=customer.id, platform=platform, workspace="ws",
        endpoint="https://api.fabric.microsoft.com/v1", auth_method="delegated",
        auth_metadata=json.dumps(metadata or {"workspace_id": "ws-1"}), status="connected",
    )
    environment = Environment(
        id=env_id, customer_id=customer.id, connection_id=conn_id, name=env_id, platform=platform,
        auth_mode="user", workspace_id="ws-1", status="environment_ready",
    )
    db.add_all([connection, environment])
    for domain, nb in (("cluster", NB_CLUSTER), ("query", NB_QUERY), ("storage", NB_STORAGE)):
        db.add(Resource(environment_id=env_id, platform=platform, resource_type="Notebook",
                        display_name=f"ACELO {domain}", platform_resource_id=nb,
                        status=provisioning_service.ACELO_OWNED,
                        detail_json=json.dumps({"domain": domain, "managed_by": "acelo"})))
    db.commit()
    return connection, environment


@pytest.fixture
def configured(db_session, customer):
    """An administrator mapped all three domains in Environment Setup (never the end user)."""
    connection, environment = _environment(db_session, customer)
    resource_registry.update(db_session, environment, "cluster", CLUSTER)
    resource_registry.update(db_session, environment, "query", QUERY)
    resource_registry.update(db_session, environment, "storage", STORAGE)
    return connection, environment


class CapturingAdapter:
    """Records exactly what the selected domain's notebook would receive."""

    def __init__(self):
        self.calls = []
        self.delegated_token = None

    async def start_analysis(self, domain, parameters):
        self.calls.append((domain, dict(parameters)))
        return StartAnalysisResult(platform_run_id=f"fabric-{uuid.uuid4()}", status="STARTING")


@pytest.fixture
def capture(monkeypatch):
    adapter = CapturingAdapter()
    monkeypatch.setattr(job_service, "build_adapter", lambda connection, db=None, token=None: adapter)
    from services import execution_worker

    monkeypatch.setattr(execution_worker, "watch_job_run", lambda _id: None)
    return adapter


def _ask(client, customer, connection, prompt):
    return client.post("/api/jobs", json={"connection_id": connection.id, "prompt": prompt},
                       headers={"X-Customer-Id": customer.id, "X-Fabric-Access-Token": "tok"})


# 1-3, 6: intent selects the domain; parameters come from THAT domain's registry row ---------------

@pytest.mark.parametrize("prompt,domain,expected", [
    ("Check my cluster utilization", "cluster", CLUSTER),
    ("Find unhealthy queries", "query", QUERY),
    ("Check storage optimization", "storage", STORAGE),
])
def test_intent_selects_the_domain_configuration(client, customer, configured, capture, prompt, domain, expected):
    connection, environment = configured
    resp = _ask(client, customer, connection, prompt)
    assert resp.status_code == 200, resp.text
    assert [d for d, _ in capture.calls] == [domain]
    params = capture.calls[0][1]
    for key in resource_registry.RUNTIME_PARAMETER_KEYS:
        if key in expected:
            assert params[key] == expected[key], key
        else:
            assert key not in params, key
    assert params["environment_id"] == environment.id
    assert params["acelo_run_id"] == resp.json()["job_runs"][0]["id"]


# 4-5: no domain mixing -------------------------------------------------------------------------------

def test_cluster_never_receives_query_configuration(client, customer, configured, capture):
    connection, _ = configured
    _ask(client, customer, connection, "Analyze my clusters")
    params = capture.calls[0][1]
    for value in QUERY.values():
        assert value not in params.values()


def test_query_never_receives_cluster_configuration(client, customer, db_session, capture):
    connection, environment = _environment(db_session, customer)
    # Only Cluster is configured: Query must NOT fall back to it.
    resource_registry.update(db_session, environment, "cluster", CLUSTER)
    _ask(client, customer, connection, "Optimize expensive queries")
    domain, params = capture.calls[0]
    assert domain == "query"
    assert set(params) == {"acelo_run_id", "environment_id"}
    assert params["environment_id"] == environment.id


def test_adapter_metadata_is_domain_scoped(db_session, customer):
    from agent.router import build_adapter

    connection, environment = _environment(db_session, customer)
    resource_registry.update(db_session, environment, "cluster", CLUSTER)
    adapter = build_adapter(connection, db_session)
    meta = adapter.auth_metadata
    assert meta["cluster_result_table"] == "acelo_cluster_recommendations"
    assert "query_result_table" not in meta and "query_result_schema" not in meta
    # Each domain runs its own notebook.
    assert meta["cluster_notebook_id"] == NB_CLUSTER and meta["query_notebook_id"] == NB_QUERY
    # The adapter itself never falls back to the Cluster table for another domain.
    from platforms.errors import PlatformError

    with pytest.raises(PlatformError):
        adapter._result_table("query")
    # ...nor to the Cluster Lakehouse for query results.
    assert meta["cluster_result_lakehouse_id"] == LH_CLUSTER
    assert meta.get("query_result_lakehouse_id") != LH_CLUSTER


def test_each_domain_is_bound_to_its_own_lakehouse(db_session, customer):
    _, environment = _environment(db_session, customer)
    resource_registry.update(db_session, environment, "cluster", {"lakehouse_id": LH_CLUSTER})
    resource_registry.update(db_session, environment, "query", {"lakehouse_id": LH_QUERY})
    assert provisioning_service.default_lakehouse(db_session, environment, "cluster")["id"] == LH_CLUSTER
    assert provisioning_service.default_lakehouse(db_session, environment, "query")["id"] == LH_QUERY
    # Package assets map to their owning domain.
    assert provisioning_service.default_lakehouse(db_session, environment, "approval_tracking")["id"] == LH_CLUSTER
    assert provisioning_service.default_lakehouse(db_session, environment, "query_apply")["id"] == LH_QUERY
    assert provisioning_service.default_lakehouse(db_session, environment, "storage") is None


# 7: nothing hardcoded ------------------------------------------------------------------------------

def test_no_hardcoded_workspace_notebook_or_pipeline_ids():
    backend = Path(__file__).resolve().parents[1]
    guid = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
    offenders = []
    for folder in ("agent", "services", "platforms", "api"):
        for py in (backend / folder).glob("*.py"):
            for n, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
                if guid.search(line) and "00000000-0000-0000-0000-000000000000" not in line:
                    offenders.append(f"{py.name}:{n}")
    assert not offenders, offenders


# 8-9: existing Cluster configuration keeps working, automatically ----------------------------------

def test_existing_cluster_configuration_is_migrated_not_lost(client, customer, db_session, capture):
    legacy = {
        "workspace_id": "ws-1",
        "cluster_source_table": "realistic_cluster_dataset", "cluster_result_table": "acelo_cluster_recommendations",
        "cluster_result_schema": "dbo", "cluster_lakehouse_id": LH_CLUSTER, "lakehouse_database": "Data",
        "cluster_approval_tracking_table": "acelo_cluster_approval", "cluster_execution_type": "notebook",
    }
    connection, environment = _environment(db_session, customer, metadata=legacy)
    for _ in range(2):  # repeated Cluster runs use the Cluster configuration automatically
        assert _ask(client, customer, connection, "Check my cluster utilization").status_code == 200
    assert [d for d, _ in capture.calls] == ["cluster", "cluster"]
    for _, params in capture.calls:
        assert params["source_table"] == "realistic_cluster_dataset"
        assert params["result_table"] == "acelo_cluster_recommendations"
        assert params["result_schema"] == "dbo" and params["approval_tracking_table"] == "acelo_cluster_approval"
        assert params["source_lakehouse"] == "Data"
    row = db_session.query(DomainResource).filter_by(environment_id=environment.id, domain="cluster").one()
    assert row.migrated_from == "connection.auth_metadata" and row.lakehouse_id == LH_CLUSTER
    # Copied, never deleted.
    db_session.refresh(connection)
    assert json.loads(connection.auth_metadata)["cluster_source_table"] == "realistic_cluster_dataset"
    # Legacy Cluster keys never became Query configuration.
    query_row = db_session.query(DomainResource).filter_by(environment_id=environment.id, domain="query").first()
    assert query_row is None or not any(resource_registry._row_values(query_row).values())


def test_legacy_cluster_settings_api_still_works_and_writes_the_cluster_row(client, customer, db_session):
    _, environment = _environment(db_session, customer)
    resp = client.put(f"/api/environments/{environment.id}/cluster-settings",
                      json={"source_table": "realistic_cluster_dataset", "result_schema": "dbo"},
                      headers={"X-Customer-Id": customer.id})
    assert resp.status_code == 200
    assert resp.json()["settings"]["source_table"] == "realistic_cluster_dataset"
    assert resource_registry.configured(db_session, environment, "cluster")["source_table"] == "realistic_cluster_dataset"
    assert resource_registry.configured(db_session, environment, "query")["source_table"] is None


# 10: the user never configures anything to start a run -------------------------------------------------

def test_user_starts_a_run_without_entering_any_settings(client, customer, configured, capture):
    connection, _ = configured
    # The only user action is the natural-language request.
    resp = _ask(client, customer, connection, "Check my cluster utilization")
    assert resp.status_code == 200
    run = resp.json()["job_runs"][0]
    assert run["domain"] == "cluster" and run["status"] != "FAILED"


def test_unclear_request_is_asked_back_instead_of_running_every_domain(client, customer, configured, capture):
    connection, _ = configured
    resp = _ask(client, customer, connection, "make things faster")
    assert resp.status_code == 422 and "clusters" in resp.json()["detail"]
    assert capture.calls == []


# Admin resource mapping + multi-tenant ------------------------------------------------------------------

def test_environment_setup_lists_each_domains_resource(client, customer, configured):
    _, environment = configured
    body = client.get(f"/api/environments/{environment.id}/optimization-resources",
                      headers={"X-Customer-Id": customer.id}).json()
    by_domain = {d["domain"]: d for d in body["domains"]}
    assert set(by_domain) == {"cluster", "query", "storage"}
    assert by_domain["cluster"]["configured"] is True
    assert by_domain["cluster"]["resource"] == {"type": "notebook", "id": NB_CLUSTER, "name": "ACELO cluster",
                                                "source": "acelo-managed"}
    assert by_domain["query"]["resource"]["id"] == NB_QUERY
    text = json.dumps(body).lower()
    for secret in ("secret\":", "password", "token", "client_secret"):
        assert secret not in text


def test_admin_mapping_for_one_domain_leaves_the_others_untouched(client, customer, configured, db_session):
    _, environment = configured
    resp = client.put(f"/api/environments/{environment.id}/optimization-resources/query",
                      json={"result_table": "query_results_v2"}, headers={"X-Customer-Id": customer.id})
    assert resp.status_code == 200
    assert resource_registry.configured(db_session, environment, "query")["result_table"] == "query_results_v2"
    assert resource_registry.configured(db_session, environment, "cluster")["result_table"] == CLUSTER["result_table"]
    bad = client.put(f"/api/environments/{environment.id}/optimization-resources/query",
                     json={"result_table": "your_table_here"}, headers={"X-Customer-Id": customer.id})
    assert bad.status_code == 422
    unknown = client.put(f"/api/environments/{environment.id}/optimization-resources/billing",
                         json={}, headers={"X-Customer-Id": customer.id})
    assert unknown.status_code == 404


def test_two_customers_each_use_their_own_mapping(client, db_session, capture):
    a = Customer(id="cust-a", name="A")
    b = Customer(id="cust-b", name="B")
    db_session.add_all([a, b])
    db_session.commit()
    conn_a, env_a = _environment(db_session, a, env_id="env-a", conn_id="conn-a")
    conn_b, env_b = _environment(db_session, b, env_id="env-b", conn_id="conn-b")
    resource_registry.update(db_session, env_a, "cluster", {"source_table": "a_clusters", "result_table": "a_out"})
    resource_registry.update(db_session, env_b, "cluster", {"source_table": "b_clusters", "result_table": "b_out"})
    _ask(client, a, conn_a, "Check my cluster utilization")
    _ask(client, b, conn_b, "Check my cluster utilization")
    assert [p["source_table"] for _, p in capture.calls] == ["a_clusters", "b_clusters"]
