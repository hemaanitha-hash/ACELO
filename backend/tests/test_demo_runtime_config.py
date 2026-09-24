"""
Demo runtime configuration: agent-driven execution with no technical input
from the user, runtime values from backend configuration, no Key Vault, and a
resource mapping that only administrators can change.
"""

import base64
import json
import uuid

import pytest

from models import Connection, Environment, JobLog, Resource
from platforms.base import StartAnalysisResult
from platforms.fabric import FabricAdapter
from services import job_service, provisioning_service, resource_registry

NB_CLUSTER = "11111111-1111-1111-1111-111111111111"
NB_QUERY = "22222222-2222-2222-2222-222222222222"
PL_CLUSTER = "66666666-6666-6666-6666-666666666666"
PL_QUERY = "77777777-7777-7777-7777-777777777777"
LH = "44444444-4444-4444-4444-444444444444"
LH_WS = "88888888-8888-8888-8888-888888888888"
SECRET = "gsk_TEST_ONLY_not_a_real_key_0123456789"


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from main import app

    return TestClient(app)


@pytest.fixture
def demo_env(monkeypatch):
    """The demo's backend configuration (what backend/.env would hold)."""
    for name, value in {
        "LAKEHOUSE": "Data", "LAKEHOUSE_ID": LH, "LAKEHOUSE_WORKSPACE_ID": LH_WS, "TABLE_SCHEMA": "dbo",
        "SOURCE_TABLE": "realistic_cluster_dataset", "RESULT_TABLE": "acelo_cluster_recommendations",
        "ACELO_QUERY_LLM_API_KEY": SECRET,
    }.items():
        monkeypatch.setenv(name, value)


def _environment(db, customer):
    connection = Connection(id="conn-demo", customer_id=customer.id, platform="fabric", workspace="ws",
                            endpoint="https://api.fabric.microsoft.com/v1", auth_method="delegated",
                            auth_metadata=json.dumps({"workspace_id": "ws-1"}), status="connected")
    environment = Environment(id="env-demo", customer_id=customer.id, connection_id="conn-demo", name="demo",
                              platform="fabric", auth_mode="user", workspace_id="ws-1", status="environment_ready")
    db.add_all([connection, environment])
    for domain, kind, item, name, pipeline_for in (
        ("cluster", "Notebook", NB_CLUSTER, "ACELO Cluster Optimization", None),
        ("query", "Notebook", NB_QUERY, "ACELO Query Optimization", None),
        (None, "DataPipeline", PL_CLUSTER, "ACELO_Cluster_Optimization_Pipeline", "cluster"),
        (None, "DataPipeline", PL_QUERY, "ACELO_Query_Optimization_Pipeline", "query"),
    ):
        detail = {"domain": domain} if domain else {"pipeline_for": pipeline_for}
        db.add(Resource(environment_id="env-demo", platform="fabric", resource_type=kind, display_name=name,
                        platform_resource_id=item, status=provisioning_service.ACELO_OWNED,
                        detail_json=json.dumps({**detail, "managed_by": "acelo"})))
    db.commit()
    return connection, environment


class Capture:
    def __init__(self):
        self.calls = []
        self.delegated_token = None

    async def start_analysis(self, domain, parameters):
        self.calls.append((domain, dict(parameters)))
        return StartAnalysisResult(platform_run_id=str(uuid.uuid4()), status="STARTING",
                                   detail={"execution_type": "pipeline"})


@pytest.fixture
def capture(monkeypatch):
    adapter = Capture()
    monkeypatch.setattr(job_service, "build_adapter", lambda connection, db=None, token=None: adapter)
    from services import execution_worker

    monkeypatch.setattr(execution_worker, "watch_job_run", lambda _id: None)
    return adapter


def _ask(client, customer, prompt):
    return client.post("/api/jobs", json={"connection_id": "conn-demo", "prompt": prompt},
                       headers={"X-Customer-Id": customer.id, "X-Fabric-Access-Token": "tok"})


# Runtime values come from configuration, not the user --------------------------------------

def test_cluster_parameters_come_from_backend_configuration(client, db_session, customer, demo_env, capture):
    _environment(db_session, customer)
    assert _ask(client, customer, "Run cluster optimization").status_code == 200
    domain, params = capture.calls[0]
    assert domain == "cluster"
    assert params["source_table"] == "realistic_cluster_dataset"
    assert params["result_table"] == "acelo_cluster_recommendations"
    assert params["source_schema"] == params["result_schema"] == "dbo"
    assert params["source_lakehouse"] == params["result_lakehouse"] == "Data"
    assert "llm_api_key" not in params  # the query key never reaches Cluster


def test_query_gets_shared_values_and_its_key_but_never_cluster_tables(client, db_session, customer, demo_env, capture):
    _environment(db_session, customer)
    assert _ask(client, customer, "Run query optimization").status_code == 200
    domain, params = capture.calls[0]
    assert domain == "query"
    assert params["llm_api_key"] == SECRET
    assert "source_table" not in params and "result_table" not in params  # Cluster-only values


def test_admin_mapping_wins_over_configuration(client, db_session, customer, demo_env, capture):
    _, environment = _environment(db_session, customer)
    resource_registry.update(db_session, environment, "cluster", {"source_table": "admin_chosen_table"})
    _ask(client, customer, "Run cluster optimization")
    assert capture.calls[0][1]["source_table"] == "admin_chosen_table"
    assert resource_registry.sources(db_session, environment, "cluster")["source_table"] == "admin"
    assert resource_registry.sources(db_session, environment, "cluster")["result_table"] == "config"


def test_query_lakehouse_resolves_from_configuration(db_session, customer, demo_env):
    _, environment = _environment(db_session, customer)
    lakehouse = provisioning_service.default_lakehouse(db_session, environment, "query")
    assert lakehouse == {"id": LH, "name": "Data", "workspace_id": LH_WS, "source": "configured"}


# The agent selects the registered resource automatically ------------------------------------------

def test_each_optimization_selects_its_registered_resource(db_session, customer, demo_env):
    _, environment = _environment(db_session, customer)
    resource_registry.update(db_session, environment, "cluster", {"execution_type": "pipeline"})
    cluster = resource_registry.get(db_session, environment, "cluster")
    query = resource_registry.get(db_session, environment, "query")
    assert (cluster["execution_type"], cluster["pipeline_id"]) == ("pipeline", PL_CLUSTER)
    # Query runs through ACELO_Query_Optimization_Pipeline by default once it exists.
    assert (query["execution_type"], query["pipeline_id"]) == ("pipeline", PL_QUERY)
    summary = {s["domain"]: s for s in resource_registry.summary(db_session, environment)}
    assert summary["cluster"]["resource"]["name"] == "ACELO_Cluster_Optimization_Pipeline"
    assert summary["query"]["resource"]["name"] == "ACELO_Query_Optimization_Pipeline"
    assert summary["cluster"]["configured"] and summary["query"]["configured"]


def test_storage_resource_is_picked_up_when_present_in_the_workspace(db_session, customer):
    _, environment = _environment(db_session, customer)
    assert resource_registry.get(db_session, environment, "storage")["pipeline_id"] is None
    db_session.add(Resource(environment_id="env-demo", platform="fabric", resource_type="DataPipeline",
                            display_name="ACELO_Storage_Optimization_Pipeline",
                            platform_resource_id="99999999-9999-9999-9999-999999999999", status="discovered"))
    db_session.commit()
    storage = resource_registry.get(db_session, environment, "storage")
    assert storage["execution_type"] == "pipeline"
    assert storage["pipeline_id"] == "99999999-9999-9999-9999-999999999999"
    assert storage["pipeline_source"] == "discovered"


# No Key Vault; the key is never stored, shown or logged ----------------------------------------------

def test_query_pipeline_passes_the_key_as_a_secure_parameter():
    definition = FabricAdapter.query_pipeline_definition(NB_QUERY, "ws-1")
    content = json.loads(base64.b64decode(definition["parts"][0]["payload"]))["properties"]
    assert content["parameters"]["llm_api_key"]["type"] == "securestring"
    activity = content["activities"][0]
    assert activity["name"] == "Run ACELO Query Notebook"
    assert activity["policy"]["secureInput"] is True
    assert activity["typeProperties"]["notebookId"] == NB_QUERY
    assert set(activity["typeProperties"]["parameters"]) == set(FabricAdapter.QUERY_PIPELINE_PARAMETERS)


def test_the_key_is_redacted_in_run_logs_and_never_exposed(client, db_session, customer, demo_env, capture):
    _environment(db_session, customer)
    run_id = _ask(client, customer, "Run query optimization").json()["job_runs"][0]["id"]
    logs = " ".join(l.message for l in db_session.query(JobLog).filter(JobLog.job_run_id == run_id))
    assert SECRET not in logs and "llm_api_key" in logs  # named, value redacted
    details = client.get(f"/api/runs/{run_id}", headers={"X-Customer-Id": customer.id}).text
    assert SECRET not in details
    resources = client.get("/api/environments/env-demo/optimization-resources",
                           headers={"X-Customer-Id": customer.id}).json()
    assert SECRET not in json.dumps(resources)
    assert {d["domain"]: d for d in resources["domains"]}["query"]["llm_api_key_configured"] is True


def test_no_key_vault_is_required():
    from pathlib import Path

    notebook = Path(__file__).resolve().parents[1] / "optimization_package" / "query" / "deploy" / "QueryOptimization.ipynb"
    source = "\n".join("".join(c["source"]) for c in json.loads(notebook.read_text("utf-8"))["cells"])
    # The key arrives as a parameter; Key Vault is only an optional alternative.
    assert "if (llm_api_key or \"\").strip():" in source
    assert "ACELO_QUERY_LLM_API_KEY" in source


def test_query_pipeline_only_receives_its_declared_parameters(monkeypatch):
    import asyncio
    import re

    import httpx
    import respx

    adapter = FabricAdapter(endpoint="https://api.fabric.microsoft.com/v1", secret=None, auth_metadata={
        "workspace_id": "ws-1", "query_notebook_id": NB_QUERY, "query_pipeline_id": PL_QUERY,
        "query_execution_type": "pipeline"})
    monkeypatch.setattr(adapter, "_acquire_token", lambda: ("tok", None))
    with respx.mock:
        route = respx.post(re.compile(rf".*/items/{PL_QUERY}/jobs/instances.*")).mock(
            return_value=httpx.Response(202, headers={"Location": "https://x/jobs/instances/abc"}))
        asyncio.run(adapter.start_analysis("query", {"acelo_run_id": "r", "environment_id": "e",
                                                     "source_schema": "dbo", "llm_api_key": SECRET}))
    body = json.loads(route.calls[0].request.content)
    assert set(body["executionData"]["parameters"]) == {"acelo_run_id", "environment_id", "llm_api_key"}


# Administrators configure; everyone else is read-only --------------------------------------------------

def test_mapping_is_read_only_for_non_administrators(client, db_session, customer, monkeypatch):
    _environment(db_session, customer)
    monkeypatch.setenv("ACELO_ADMIN_USERS", "admin@contoso.com")
    user = {"X-Customer-Id": customer.id, "X-Acelo-User-Email": "analyst@contoso.com"}
    admin = {"X-Customer-Id": customer.id, "X-Acelo-User-Email": "Admin@Contoso.com"}
    view = client.get("/api/environments/env-demo/optimization-resources", headers=user).json()
    assert view["access"]["can_configure_resources"] is False
    for url in ("/api/environments/env-demo/optimization-resources/query",
                "/api/environments/env-demo/cluster-settings"):
        assert client.put(url, json={"result_schema": "dbo"}, headers=user).status_code == 403
        assert client.put(url, json={"result_schema": "dbo"}, headers=admin).status_code == 200


def test_production_without_an_admin_list_is_read_only(client, db_session, customer, monkeypatch):
    _environment(db_session, customer)
    from config import get_settings

    monkeypatch.setattr(type(get_settings()), "is_production", property(lambda self: True))
    resp = client.put("/api/environments/env-demo/optimization-resources/query", json={"result_schema": "dbo"},
                      headers={"X-Customer-Id": customer.id})
    assert resp.status_code == 403
