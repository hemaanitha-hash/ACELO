"""
Step 4 validation for the Fabric-converted ACELO Cluster optimizer.

These are static checks on the packaged notebook plus integration checks that
Step 3 provisioning and Step 2 execution can actually pick it up. They do NOT
execute the notebook — that requires a live Fabric workspace.
"""

import ast
import json
import re
from pathlib import Path

import httpx
import pytest
import respx

from platforms.fabric import FabricAdapter
from services import package_registry

NOTEBOOK_PATH = (
    Path(__file__).resolve().parent.parent
    / "optimization_package"
    / "cluster"
    / "Clusterfabric.ipynb"
)

REQUIRED_PARAMETERS = [
    "source_table",
    "result_table",
    "source_lakehouse",
    "result_lakehouse",
    "acelo_run_id",
    "environment_id",
    "model_dir",
    "llm_key_vault_uri",
    "llm_secret_name",
]


@pytest.fixture(scope="module")
def notebook() -> dict:
    return json.loads(NOTEBOOK_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def code_source(notebook) -> str:
    return "\n".join(
        "".join(c["source"]) for c in notebook["cells"] if c["cell_type"] == "code"
    )


# --------------------------------------------------------------------------
# 1/2. Package presence and notebook validity
# --------------------------------------------------------------------------

def test_cluster_notebook_exists_in_package():
    assert NOTEBOOK_PATH.is_file(), f"Cluster notebook missing at {NOTEBOOK_PATH}"


def test_notebook_is_valid_ipynb(notebook):
    assert notebook["nbformat"] == 4
    assert isinstance(notebook["cells"], list) and notebook["cells"]
    for cell in notebook["cells"]:
        assert cell["cell_type"] in ("code", "markdown")
        assert isinstance(cell["source"], list)


def test_every_code_cell_is_valid_python(notebook):
    for index, cell in enumerate(notebook["cells"]):
        if cell["cell_type"] != "code":
            continue
        body = "".join(cell["source"])
        # Strip Jupyter magics, which are not valid Python on their own.
        clean = "\n".join(
            line for line in body.splitlines() if not line.strip().startswith(("%", "!"))
        )
        try:
            ast.parse(clean)
        except SyntaxError as exc:  # pragma: no cover - failure path
            pytest.fail(f"cell {index} is not valid Python: {exc}")


def test_notebook_is_not_a_placeholder(code_source):
    """Guards against a stub being slipped in to make provisioning look green."""
    assert len(code_source) > 8000
    for marker in ("hello world", "SELECT 1", "TODO: implement"):
        assert marker.lower() not in code_source.lower()


# --------------------------------------------------------------------------
# 3/4. No credentials of any kind
# --------------------------------------------------------------------------

def test_no_groq_api_key_in_notebook(code_source):
    """The original notebook embedded a live gsk_ key. It must never be here."""
    assert not re.search(r"gsk_[A-Za-z0-9]{20,}", code_source)


def test_no_hardcoded_credentials(code_source):
    forbidden = [
        r"GROQ_API_KEY\"\]\s*=\s*\"",
        r"GROQ_API_KEY'\]\s*=\s*'",
        r"api_key\s*=\s*[\"'][A-Za-z0-9_\-]{20,}[\"']",
        r"sk-[A-Za-z0-9]{20,}",
        r"password\s*=\s*[\"'][^\"']+[\"']",
        r"client_secret\s*=\s*[\"'][^\"']+[\"']",
    ]
    for pattern in forbidden:
        assert not re.search(pattern, code_source), f"credential-like literal matched {pattern}"


def test_llm_credential_is_resolved_from_key_vault(code_source):
    assert "getSecret" in code_source
    assert "llm_key_vault_uri" in code_source
    assert "llm_secret_name" in code_source


def test_llm_unavailable_does_not_fabricate_a_recommendation(code_source):
    """No credential must yield an explicit status, not invented advice."""
    assert 'LLM_UNAVAILABLE = "LLM_UNAVAILABLE"' in code_source
    # Both the no-credential and call-failed paths return the marker.
    assert "return LLM_UNAVAILABLE" in code_source
    assert 'f"{LLM_UNAVAILABLE}: {type(e).__name__}"' in code_source


def test_whole_notebook_file_has_no_secret_bearing_outputs(notebook):
    """Execution outputs can leak data and keys; the packaged copy carries none."""
    for cell in notebook["cells"]:
        assert not cell.get("outputs"), "packaged notebook must not contain outputs"


# --------------------------------------------------------------------------
# 5/6. No Databricks-only constructs, no hardcoded customer tables
# --------------------------------------------------------------------------

def test_no_dbutils(code_source):
    assert "dbutils" not in code_source


def test_no_databricks_only_apis(code_source):
    for token in ("dbutils", "/Workspace/Shared", "dbfs:/", "/dbfs/", "databricks.sdk"):
        assert token not in code_source, f"Databricks-only construct present: {token}"
    # display() is a notebook-host builtin; the packaged notebook must not rely on it.
    assert not re.search(r"(?<![\w.])display\s*\(", code_source)


def test_no_hardcoded_databricks_source_table(code_source):
    for token in ("databricks_ws", "realistic_cluster_dataset", "cluster_ai_recommendations"):
        assert token not in code_source, f"hardcoded Databricks table reference: {token}"


def test_no_hardcoded_customer_identifiers(code_source):
    assert not re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", code_source
    )
    assert "data_Demo" not in code_source


# --------------------------------------------------------------------------
# 7/8. Parameterization
# --------------------------------------------------------------------------

def test_parameters_cell_is_tagged_for_fabric(notebook):
    tagged = [
        c for c in notebook["cells"] if "parameters" in (c.get("metadata", {}).get("tags") or [])
    ]
    assert len(tagged) == 1, "exactly one cell must carry the Fabric 'parameters' tag"


def test_all_required_parameters_are_declared(notebook):
    params_cell = next(
        c for c in notebook["cells"] if "parameters" in (c.get("metadata", {}).get("tags") or [])
    )
    body = "".join(params_cell["source"])
    tree = ast.parse(body)
    assigned = {
        t.id for node in tree.body if isinstance(node, ast.Assign)
        for t in node.targets if isinstance(t, ast.Name)
    }
    missing = [p for p in REQUIRED_PARAMETERS if p not in assigned]
    assert not missing, f"parameters cell is missing: {missing}"


def test_parameter_defaults_are_empty_so_misconfiguration_fails_loudly(notebook):
    params_cell = next(
        c for c in notebook["cells"] if "parameters" in (c.get("metadata", {}).get("tags") or [])
    )
    tree = ast.parse("".join(params_cell["source"]))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        name = node.targets[0].id
        if name in ("source_table", "result_table", "acelo_run_id", "environment_id"):
            assert node.value.value == "", f"{name} must default to empty, not a baked-in value"


def test_notebook_validates_required_parameters_before_running(code_source):
    assert "missing required parameters" in code_source
    assert "raise ValueError" in code_source


def test_result_destination_is_parameterized(code_source):
    assert "saveAsTable(RESULT_TABLE)" in code_source
    assert "RESULT_TABLE = _qualify(result_table" in code_source


def test_source_is_parameterized_and_read_only(code_source):
    assert "spark.read.table(SOURCE_TABLE)" in code_source
    # The only write target is the ACELO result table.
    writes = re.findall(r"saveAsTable\(([^)]*)\)", code_source)
    assert writes == ["RESULT_TABLE"], f"unexpected write targets: {writes}"
    for destructive in ("DROP TABLE", "DELETE FROM", "TRUNCATE", "spark.sql(\"DROP"):
        assert destructive not in code_source


# --------------------------------------------------------------------------
# 9. Result contract
# --------------------------------------------------------------------------

def test_results_are_stamped_with_the_acelo_run_id(code_source):
    assert 'pdf_inference["acelo_run_id"] = ACELO_RUN_ID' in code_source
    assert 'pdf_inference["acelo_environment_id"] = ACELO_ENVIRONMENT_ID' in code_source
    assert 'pdf_inference["acelo_analyzed_at"] = ACELO_ANALYZED_AT' in code_source


def test_results_append_rather_than_destroy_run_history(code_source):
    assert 'mode("append")' in code_source
    assert 'mode("overwrite")' not in code_source


def test_result_schema_is_documented():
    doc = NOTEBOOK_PATH.parent / "RESULT_SCHEMA.md"
    assert doc.is_file()
    text = doc.read_text(encoding="utf-8")
    for field in (
        "acelo_run_id",
        "optimization_label",
        "recommended_max_workers",
        "predicted_savings_pct",
        "potential_monthly_savings",
        "llm_optimization",
        "efficiency_score",
    ):
        assert field in text, f"{field} is not documented in RESULT_SCHEMA.md"


def test_input_schema_is_documented():
    text = (NOTEBOOK_PATH.parent / "RESULT_SCHEMA.md").read_text(encoding="utf-8")
    for column in (
        "cluster_id",
        "avg_cpu_util",
        "avg_memory_util",
        "idle_time_min",
        "cluster_uptime_hours",
        "total_jobs_run",
        "total_dbus_cost_usd",
    ):
        assert column in text


# --------------------------------------------------------------------------
# Algorithm preservation — the converted notebook must still BE the optimizer
# --------------------------------------------------------------------------

def test_deterministic_scoring_is_preserved(code_source):
    for fragment in (
        "idle_impact_score",
        "oversized_score",
        "efficiency_score",
        "underutilized_label",
        "oversized_label",
        "optimization_label",
        "recommended_max_workers",
        "job_density_norm",
        "approxQuantile",
        "stream|etl|burst|continuous",
    ):
        assert fragment in code_source, f"scoring element lost in conversion: {fragment}"


def test_xgboost_models_are_preserved(code_source):
    assert "xgb.XGBRegressor" in code_source
    assert "n_estimators=100" in code_source
    assert "max_depth=4" in code_source
    assert "learning_rate=0.1" in code_source
    assert "random_state=42" in code_source
    for field in ("predicted_cost_usd", "predicted_savings_pct", "ml_cost_variance",
                  "potential_monthly_savings"):
        assert field in code_source


def test_no_fabricated_metrics(code_source):
    """Scores must be computed, never randomised or hardcoded."""
    assert "random.uniform" not in code_source
    assert "np.random" not in code_source
    assert not re.search(r"potential_monthly_savings\"?\]\s*=\s*\d+", code_source)
    assert not re.search(r"efficiency_score\"?\]\s*=\s*0\.\d+", code_source)


# --------------------------------------------------------------------------
# 10/11. Registry + provisioning pick it up
# --------------------------------------------------------------------------

def test_package_registry_detects_the_cluster_asset():
    availability = package_registry.package_availability()
    cluster = next(a for a in availability["assets"] if a["domain"] == "cluster")
    assert cluster["available"] is True
    assert cluster["checksum"]
    assert availability["deployable"] is True
    assert "cluster" not in availability["missing"]


def test_package_asset_payload_is_the_real_notebook():
    package = package_registry.load_package()
    asset = package.asset_for("cluster")
    import base64

    decoded = base64.b64decode(asset.payload_base64())
    assert json.loads(decoded)["nbformat"] == 4
    assert b"efficiency_score" in decoded


@pytest.mark.asyncio
@respx.mock
async def test_provisioning_deploys_the_cluster_notebook(db_session, customer, monkeypatch):
    """Step 3 provisioning must deploy the real asset with no backend change."""
    from services import environment_service, provisioning_service

    ws = "ws-step4"
    env = environment_service.create_environment(
        db_session, customer.id, name="F", platform="fabric",
        tenant_id="t", workspace_id=ws, client_id="c", client_secret="s", endpoint=None,
    )
    monkeypatch.setattr(FabricAdapter, "_acquire_token", lambda self: ("tok", None))

    base = "https://api.fabric.microsoft.com/v1"
    respx.get(f"{base}/workspaces/{ws}").mock(
        return_value=httpx.Response(200, json={"id": ws, "displayName": "WS"})
    )
    respx.get(f"{base}/workspaces/{ws}/items").mock(return_value=httpx.Response(200, json={"value": []}))
    respx.get(f"{base}/workspaces/{ws}/folders").mock(return_value=httpx.Response(200, json={"value": []}))
    respx.post(f"{base}/workspaces/{ws}/folders").mock(
        return_value=httpx.Response(201, json={"id": "f1", "displayName": "f"})
    )
    captured = {}

    def create(request):
        body = json.loads(request.content)
        captured["payload"] = body["definition"]["parts"][0]["payload"]
        captured["name"] = body["displayName"]
        return httpx.Response(201, json={"id": "real-nb-1", "displayName": body["displayName"]})

    respx.post(f"{base}/workspaces/{ws}/notebooks").mock(side_effect=create)
    respx.get(url__regex=rf"{base}/workspaces/{ws}/items/.+").mock(
        return_value=httpx.Response(200, json={"id": "real-nb-1", "type": "Notebook"})
    )
    respx.post(url__regex=rf"{base}/workspaces/{ws}/notebooks/.+/getDefinition").mock(
        return_value=httpx.Response(200, json={"definition": {"parts": [{"path": "n.ipynb"}]}})
    )

    state = await provisioning_service.provision_environment(db_session, env)

    assert state["domains"]["cluster"]["deployed"] is True
    assert state["domains"]["cluster"]["platform_resource_id"] == "real-nb-1"
    assert captured["name"] == "ACELO Cluster Optimization"

    import base64

    assert b"efficiency_score" in base64.b64decode(captured["payload"])
    assert provisioning_service.resolved_domains(db_session, env)["cluster"] == "real-nb-1"


# --------------------------------------------------------------------------
# Step 2 execution can invoke it with parameters
# --------------------------------------------------------------------------

def test_execution_parameters_cover_every_notebook_parameter(notebook):
    """Every parameter ACELO sends must exist in the notebook, and vice versa for
    the ones the notebook needs to run."""
    from models import Connection, JobRun
    from services.job_service import build_run_parameters

    connection = Connection(
        id="c1", customer_id="cust", platform="fabric", workspace="w",
        endpoint="https://x", auth_method="service_principal",
        auth_metadata=json.dumps({
            "lakehouse_database": "acelo_lh",
            "cluster_source_table": "cluster_telemetry",
            "cluster_result_table": "acelo_cluster_results",
        }),
    )
    job_run = JobRun(id="run-abc", analysis_job_id="j1", domain="cluster", platform="fabric")

    params = build_run_parameters(connection, job_run)

    assert params["acelo_run_id"] == "run-abc"
    assert params["source_table"] == "cluster_telemetry"
    assert params["result_table"] == "acelo_cluster_results"
    assert params["source_lakehouse"] == "acelo_lh"
    assert params["result_lakehouse"] == "acelo_lh"

    params_cell = next(
        c for c in notebook["cells"] if "parameters" in (c.get("metadata", {}).get("tags") or [])
    )
    declared = {
        t.id for node in ast.parse("".join(params_cell["source"])).body
        if isinstance(node, ast.Assign) for t in node.targets if isinstance(t, ast.Name)
    }
    unknown = set(params) - declared
    assert not unknown, f"ACELO sends parameters the notebook does not declare: {unknown}"


@pytest.mark.asyncio
@respx.mock
async def test_start_analysis_sends_parameters_in_fabric_format(monkeypatch):
    adapter = FabricAdapter(
        endpoint="https://x",
        auth_metadata={"tenant_id": "t", "client_id": "c", "workspace_id": "w", "notebook_item_id": "nb"},
        secret="s",
    )
    monkeypatch.setattr(adapter, "_acquire_token", lambda: ("tok", None))

    base = "https://api.fabric.microsoft.com/v1"
    captured = {}

    def start(request):
        captured["body"] = json.loads(request.content) if request.content else {}
        return httpx.Response(
            202, headers={"Location": f"{base}/workspaces/w/items/nb/jobs/instances/real-run"}
        )

    respx.post(f"{base}/workspaces/w/items/nb/jobs/instances").mock(side_effect=start)

    result = await adapter.start_analysis("cluster", {"acelo_run_id": "r1", "source_table": "t"})

    assert result.platform_run_id == "real-run"
    params = captured["body"]["executionData"]["parameters"]
    assert params["acelo_run_id"] == {"value": "r1", "type": "string"}
    assert params["source_table"] == {"value": "t", "type": "string"}


@pytest.mark.asyncio
async def test_result_retrieval_is_scoped_to_the_acelo_run(monkeypatch):
    """Without scoping, a run would return rows appended by earlier runs."""
    import sys
    import types
    from unittest.mock import MagicMock

    adapter = FabricAdapter(
        endpoint="https://x",
        auth_metadata={
            "tenant_id": "t", "client_id": "c", "workspace_id": "w", "notebook_item_id": "nb",
            "sql_endpoint": "e.datawarehouse.fabric.microsoft.com", "lakehouse_database": "lh",
        },
        secret="s",
    )
    monkeypatch.setattr(adapter, "_acquire_token", lambda: ("tok", None))

    cursor = MagicMock()
    cursor.description = [("cluster_id",), ("acelo_run_id",)]
    cursor.fetchall.return_value = [("c-1", "run-xyz")]
    conn = MagicMock()
    conn.cursor.return_value = cursor
    fake = types.ModuleType("pyodbc")
    fake.connect = MagicMock(return_value=conn)
    monkeypatch.setitem(sys.modules, "pyodbc", fake)

    result = await adapter.get_run_result("fabric-job-1", "cluster", acelo_run_id="run-xyz")

    sql, *args = cursor.execute.call_args.args
    assert "WHERE acelo_run_id = ?" in sql
    assert args == ["run-xyz"]
    assert result.payload["acelo_run_id"] == "run-xyz"


@pytest.mark.asyncio
@respx.mock
async def test_execution_path_uses_the_provisioned_notebook_id(db_session, customer, monkeypatch):
    """
    Regression for a Step 5 finding: provisioning registered the real Fabric item
    ID, but job_service built its adapter from the Connection alone and never
    consulted the Resource table — so the deployed notebook would never have run.
    """
    from agent.router import build_adapter
    from models import AnalysisJob, Connection
    from services import job_service, provisioning_service

    ws = "ws-step5"
    connection = Connection(
        id="conn-step5", customer_id=customer.id, platform="fabric", workspace="w",
        endpoint="https://api.fabric.microsoft.com/v1", auth_method="service_principal",
        auth_metadata=json.dumps({
            "tenant_id": "t", "client_id": "c", "workspace_id": ws,
            "cluster_source_table": "cluster_telemetry",
            "cluster_result_table": "acelo_cluster_results",
        }),
        secret_encrypted=None, status="connected",
    )
    db_session.add(connection)
    db_session.commit()

    from models import Environment, Resource

    environment = Environment(
        id="env-step5", customer_id=customer.id, connection_id=connection.id,
        name="E", platform="fabric", workspace_id=ws,
        status="connected", provisioning_status=provisioning_service.INSTALLED,
    )
    db_session.add(environment)
    db_session.add(
        Resource(
            environment_id=environment.id, platform="fabric", resource_type="Notebook",
            display_name="ACELO Cluster Optimization",
            platform_resource_id="provisioned-nb-999",
            status=provisioning_service.ACELO_OWNED,
            detail_json=json.dumps({"domain": "cluster", "managed_by": "acelo"}),
        )
    )
    db_session.commit()

    # The adapter the execution path builds must carry the provisioned ID.
    adapter = build_adapter(connection, db_session)
    assert adapter.auth_metadata["cluster_notebook_id"] == "provisioned-nb-999"
    assert adapter._resolve_notebook_id("cluster") == "provisioned-nb-999"

    # ...and the job must actually be started against that notebook.
    monkeypatch.setattr(FabricAdapter, "_acquire_token", lambda self: ("tok", None))
    base = "https://api.fabric.microsoft.com/v1"
    route = respx.post(
        f"{base}/workspaces/{ws}/items/provisioned-nb-999/jobs/instances"
    ).mock(
        return_value=httpx.Response(
            202,
            headers={
                "Location": f"{base}/workspaces/{ws}/items/provisioned-nb-999/jobs/instances/fab-run-1"
            },
        )
    )

    analysis_job = AnalysisJob(
        id="job-step5", customer_id=customer.id, connection_id=connection.id,
        request="Analyze my cluster", intent="cluster", platform="fabric", status="QUEUED",
    )
    db_session.add(analysis_job)
    db_session.commit()
    job_run = job_service.create_job_run(db_session, analysis_job, "cluster")

    await job_service.start_job_run(db_session, connection, job_run)

    assert route.called
    assert job_run.platform_run_id == "fab-run-1"
    assert job_run.platform_resource_id == "provisioned-nb-999"

    sent = json.loads(route.calls[0].request.content)["executionData"]["parameters"]
    assert sent["acelo_run_id"]["value"] == job_run.id
    assert sent["source_table"]["value"] == "cluster_telemetry"
    assert sent["result_table"]["value"] == "acelo_cluster_results"
    assert sent["environment_id"]["value"] == "env-step5"
