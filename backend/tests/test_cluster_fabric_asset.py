"""
Step 14 coverage for the product team's Clusterfabric.ipynb as the ACELO
Cluster asset: parameterisation, schema mapping, and the fact that nothing
customer-specific or secret is baked into it.

These are static and mocked-HTTP checks. They do NOT execute the notebook in
Fabric — that requires live credentials and is reported separately.
"""

import ast
import json
from pathlib import Path

import httpx
import pytest
import respx

from platforms.fabric import FabricAdapter
from services import package_registry

PACKAGE = Path(__file__).resolve().parent.parent / "optimization_package"
NOTEBOOK_PATH = PACKAGE / "cluster" / "Clusterfabric.ipynb"

DEMO_SOURCE = "Data.dbo.realistic_cluster_dataset"
DEMO_RESULT = "Data.dbo.acelo_cluster_ai_recommendations"
DEMO_WORKSPACE = "03e0392d-ed86-41e4-943c-146f8d845a1d"


@pytest.fixture(scope="module")
def notebook() -> dict:
    return json.loads(NOTEBOOK_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def code(notebook) -> str:
    return "\n".join(
        "".join(c["source"]) for c in notebook["cells"] if c["cell_type"] == "code"
    )


@pytest.fixture(scope="module")
def params_cell(notebook) -> dict:
    return next(
        c for c in notebook["cells"] if "parameters" in (c.get("metadata", {}).get("tags") or [])
    )


# --------------------------------------------------------------------------
# Asset replacement
# --------------------------------------------------------------------------

def test_clusterfabric_is_the_registered_cluster_asset():
    package = package_registry.load_package()
    asset = package.asset_for("cluster")
    assert asset.source == "cluster/Clusterfabric.ipynb"
    assert asset.available is True


def test_there_is_exactly_one_cluster_notebook():
    notebooks = sorted(p.name for p in (PACKAGE / "cluster").glob("*.ipynb"))
    assert notebooks == ["Clusterfabric.ipynb"], f"expected one notebook, found {notebooks}"


def test_notebook_carries_no_execution_outputs(notebook):
    # Outputs can embed customer data; the packaged copy must ship clean.
    for cell in notebook["cells"]:
        assert not cell.get("outputs")


def test_every_code_cell_is_valid_python(notebook):
    for index, cell in enumerate(notebook["cells"]):
        if cell["cell_type"] != "code":
            continue
        body = "".join(cell["source"])
        clean = "\n".join(
            line for line in body.splitlines() if not line.strip().startswith(("%", "!"))
        )
        try:
            ast.parse(clean)
        except SyntaxError as exc:  # pragma: no cover
            pytest.fail(f"cell {index} is not valid Python: {exc}")


# --------------------------------------------------------------------------
# Parameters
# --------------------------------------------------------------------------

def test_required_parameters_are_declared(params_cell):
    declared = {
        t.id
        for node in ast.parse("".join(params_cell["source"])).body
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name)
    }
    for required in (
        "acelo_run_id",
        "source_table",
        "result_table",
        "environment_id",
        "column_mapping",
        "model_dir",
    ):
        assert required in declared, f"parameters cell is missing {required}"


def test_required_parameters_default_empty(params_cell):
    for node in ast.parse("".join(params_cell["source"])).body:
        if not isinstance(node, ast.Assign):
            continue
        name = node.targets[0].id
        if name in ("acelo_run_id", "source_table", "result_table", "environment_id"):
            assert node.value.value == "", f"{name} must not have a baked-in default"


def test_missing_required_parameters_fail_loudly(code):
    assert "missing required parameters" in code
    assert "raise ValueError" in code


# --------------------------------------------------------------------------
# Source table + schema mapping
# --------------------------------------------------------------------------

def test_source_table_is_a_runtime_parameter(code):
    assert "spark.read.table(SOURCE_TABLE)" in code
    # The original's hardcoded table must be gone.
    assert 'spark.read.table("realistic_cluster_dataset")' not in code


def test_acme_cluster_id_maps_to_cluster_id(code):
    assert '"acme_cluster_id": "cluster_id"' in code
    assert "withColumnRenamed" in code


def test_mapping_is_overridable_per_environment(code):
    assert "column_mapping" in code
    assert "COLUMN_MAPPING.update" in code


def test_mapping_does_not_clobber_an_existing_expected_column(code):
    # Renaming only when the target is absent keeps a conforming table intact.
    assert "_source_column in df.columns and _expected_column not in df.columns" in code


def test_missing_columns_raise_instead_of_being_invented(code):
    assert "is missing required columns" in code
    assert "does not substitute values for missing telemetry" in code
    # The real column list the optimizer needs.
    for column in (
        "cluster_id",
        "avg_cpu_util",
        "avg_memory_util",
        "idle_time_min",
        "cluster_uptime_hours",
        "total_jobs_run",
        "total_dbus_cost_usd",
    ):
        assert f'"{column}"' in code


def test_empty_source_table_is_an_error(code):
    assert "contains no rows" in code


# --------------------------------------------------------------------------
# Result table + traceability
# --------------------------------------------------------------------------

def test_result_table_is_a_runtime_parameter(code):
    assert "saveAsTable(RESULT_TABLE)" in code
    assert 'saveAsTable("cluster_ai_recommendations")' not in code


def test_results_are_run_scoped_and_appended(code):
    assert 'pdf_inference["acelo_run_id"] = ACELO_RUN_ID' in code
    assert 'mode("append")' in code
    # Replacing the table would destroy previous runs.
    assert 'mode("overwrite")' not in code


def test_traceability_columns_present(code):
    for column in ("acelo_run_id", "acelo_environment_id", "acelo_analyzed_at"):
        assert f'pdf_inference["{column}"]' in code


# --------------------------------------------------------------------------
# Algorithm preserved
# --------------------------------------------------------------------------

def test_xgboost_logic_is_preserved(code):
    assert "xgb.XGBRegressor" in code
    assert "n_estimators=100" in code
    assert "max_depth=4" in code
    assert "learning_rate=0.1" in code
    assert "random_state=42" in code
    for field in (
        "predicted_cost_usd",
        "predicted_savings_pct",
        "ml_cost_variance",
        "potential_monthly_savings",
    ):
        assert field in code


def test_deterministic_scoring_is_preserved(code):
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
        assert fragment in code, f"scoring element lost: {fragment}"


def test_no_fabricated_metrics(code):
    assert "np.random" not in code
    assert "random.uniform" not in code


# --------------------------------------------------------------------------
# Secrets and customer-specific values
# --------------------------------------------------------------------------

def test_no_api_key_in_the_notebook(code):
    import re

    assert not re.search(r"gsk_[A-Za-z0-9]{10,}", code)
    assert not re.search(r"api_key\s*=\s*[\"'][A-Za-z0-9_\-]{20,}[\"']", code)
    # The key is resolved at run time, never embedded.
    assert "getSecret" in code
    assert "llm_key_vault_uri" in code


def test_llm_unavailable_is_explicit_not_fabricated(code):
    assert 'LLM_UNAVAILABLE = "LLM_UNAVAILABLE"' in code
    assert "return LLM_UNAVAILABLE" in code


def test_no_hardcoded_customer_identifiers(code, notebook):
    import re

    # No workspace/lakehouse GUIDs anywhere in the notebook source...
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", code)
    # ...and the stale pinned lakehouse metadata is gone. It named a different
    # workspace from the one the notebook is deployed into.
    assert "dependencies" not in notebook["metadata"]


def test_demo_tables_are_not_baked_into_the_notebook(code):
    # The demo values are passed at run time, never hardcoded.
    assert DEMO_SOURCE not in code
    assert DEMO_RESULT not in code
    assert DEMO_WORKSPACE not in code


# --------------------------------------------------------------------------
# Runtime parameter assembly (ACELO -> Fabric)
# --------------------------------------------------------------------------

def test_acelo_sends_the_demo_parameters(params_cell):
    """The demo configuration flows through as parameters, not as notebook edits."""
    from models import Connection, JobRun
    from services.job_service import build_run_parameters

    connection = Connection(
        id="c1",
        customer_id="cust",
        platform="fabric",
        workspace="acelo demo",
        endpoint="https://api.fabric.microsoft.com/v1",
        auth_method="delegated",
        auth_metadata=json.dumps(
            {
                "workspace_id": DEMO_WORKSPACE,
                "cluster_source_table": DEMO_SOURCE,
                "cluster_result_table": DEMO_RESULT,
                "column_mapping": '{"acme_cluster_id": "cluster_id"}',
            }
        ),
    )
    job_run = JobRun(id="run-demo-1", analysis_job_id="j1", domain="cluster", platform="fabric")

    params = build_run_parameters(connection, job_run)

    assert params["acelo_run_id"] == "run-demo-1"
    assert params["source_table"] == DEMO_SOURCE
    assert params["result_table"] == DEMO_RESULT
    assert params["column_mapping"] == '{"acme_cluster_id": "cluster_id"}'

    # Everything ACELO sends must be a parameter the notebook declares.
    declared = {
        t.id
        for node in ast.parse("".join(params_cell["source"])).body
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name)
    }
    assert set(params) <= declared, f"undeclared parameters: {set(params) - declared}"


@pytest.mark.asyncio
@respx.mock
async def test_execution_sends_parameters_to_the_real_fabric_api(monkeypatch):
    """start_analysis must POST the parameters in Fabric's RunNotebook format."""
    adapter = FabricAdapter(
        endpoint="https://x",
        auth_metadata={
            "tenant_id": "t",
            "client_id": "c",
            "workspace_id": DEMO_WORKSPACE,
            "cluster_notebook_id": "provisioned-nb-1",
        },
        secret="s",
    )
    monkeypatch.setattr(adapter, "_acquire_token", lambda: ("tok", None))

    base = "https://api.fabric.microsoft.com/v1"
    url = f"{base}/workspaces/{DEMO_WORKSPACE}/items/provisioned-nb-1/jobs/instances"
    captured: dict = {}

    def start(request):
        captured["body"] = json.loads(request.content) if request.content else {}
        return httpx.Response(
            202,
            headers={
                "Location": f"{base}/workspaces/{DEMO_WORKSPACE}"
                f"/items/provisioned-nb-1/jobs/instances/real-fabric-job-1"
            },
        )

    respx.post(url).mock(side_effect=start)

    result = await adapter.start_analysis(
        "cluster",
        {
            "acelo_run_id": "run-demo-1",
            "source_table": DEMO_SOURCE,
            "result_table": DEMO_RESULT,
            "environment_id": "env-1",
        },
    )

    # A REAL Fabric job instance id, taken from the Location header.
    assert result.platform_run_id == "real-fabric-job-1"
    sent = captured["body"]["executionData"]["parameters"]
    assert sent["source_table"] == {"value": DEMO_SOURCE, "type": "string"}
    assert sent["result_table"] == {"value": DEMO_RESULT, "type": "string"}
    assert sent["acelo_run_id"] == {"value": "run-demo-1", "type": "string"}


@pytest.mark.asyncio
@respx.mock
async def test_provisioning_deploys_clusterfabric_and_registers_the_real_id(
    db_session, customer, monkeypatch
):
    from services import environment_service, provisioning_service

    env = environment_service.create_environment(
        db_session,
        customer.id,
        name="acelo demo",
        platform="fabric",
        auth_mode="service_principal",
        tenant_id="t",
        workspace_id=DEMO_WORKSPACE,
        client_id="c",
        client_secret="s",
    )
    monkeypatch.setattr(FabricAdapter, "_acquire_token", lambda self: ("tok", None))

    base = f"https://api.fabric.microsoft.com/v1/workspaces/{DEMO_WORKSPACE}"
    respx.get(base).mock(
        return_value=httpx.Response(200, json={"id": DEMO_WORKSPACE, "displayName": "acelo demo"})
    )
    respx.get(f"{base}/items").mock(return_value=httpx.Response(200, json={"value": []}))
    respx.get(f"{base}/folders").mock(return_value=httpx.Response(200, json={"value": []}))
    respx.post(f"{base}/folders").mock(
        return_value=httpx.Response(201, json={"id": "f1", "displayName": "ACELO"})
    )
    captured: dict = {}

    def create(request):
        body = json.loads(request.content)
        captured["payload"] = body["definition"]["parts"][0]["payload"]
        return httpx.Response(
            201, json={"id": "real-nb-id-42", "displayName": body["displayName"]}
        )

    respx.post(f"{base}/notebooks").mock(side_effect=create)
    respx.get(url__regex=rf"{base}/items/.+").mock(
        return_value=httpx.Response(200, json={"id": "real-nb-id-42", "type": "Notebook"})
    )
    respx.post(url__regex=rf"{base}/notebooks/.+/getDefinition").mock(
        return_value=httpx.Response(200, json={"definition": {"parts": [{"path": "n.ipynb"}]}})
    )

    state = await provisioning_service.provision_environment(db_session, env)

    assert state["domains"]["cluster"]["deployed"] is True
    # The REAL Fabric item id is what gets registered — never a generated one.
    assert state["domains"]["cluster"]["platform_resource_id"] == "real-nb-id-42"
    assert provisioning_service.resolved_domains(db_session, env)["cluster"] == "real-nb-id-42"

    # The bytes deployed are the real optimizer, not a stub.
    import base64

    deployed = base64.b64decode(captured["payload"])
    assert b"efficiency_score" in deployed
    assert b"xgb.XGBRegressor" in deployed
    assert b"SOURCE_TABLE" in deployed
