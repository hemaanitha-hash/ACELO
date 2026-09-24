"""
Query approvals sourced from the Validation notebook's `query_tracking_full_v1`
Delta table, read directly through OneLake by the Fabric adapter — no SQL
analytics endpoint.

The tables in these tests are REAL Delta tables (written with delta-rs to a temp
folder); only their location is redirected from OneLake to disk. The adapter's
actual Delta reader parses the Delta log and parquet files.
"""

import json
from datetime import datetime

import pyarrow as pa
import pytest
from deltalake import write_deltalake
from fastapi.testclient import TestClient

from database import get_db
from main import app
from models import ApprovalAudit, Connection, Customer, Environment, OptimizationApproval
from platforms.fabric import FabricAdapter
from services import approval_service, provisioning_service

WORKSPACE_ID = "0a0a0a0a-1111-2222-3333-444444444444"
LAKEHOUSE_ID = "1b1b1b1b-2222-3333-4444-555555555555"
TABLE = "query_tracking_full_v1"
ACTOR = {"X-Acelo-User-Id": "oid-1", "X-Acelo-User-Name": "Priya Reviewer"}
ONELAKE = {"X-OneLake-Token": "eyJ-ONELAKE-STORAGE-TOKEN"}


def _q(query_id, status, **extra):
    row = {"query_id": query_id, "query_text": f"SELECT * FROM t WHERE k = '{query_id}'", "query_type": "SELECT",
           "confidence": "HIGH", "bottleneck_type": "DISK_SPILL", "primary_action": f"Reduce spill for {query_id}",
           "root_cause": "Large shuffle join", "actual_cost_usd": 9.5, "potential_savings_usd": 3.25,
           "llm_refactor_suggestion": f"```sql\nSELECT k FROM t WHERE k = '{query_id}'\n```",
           "workflow_status": status, "send_to": None, "optimized_cost_usd": 0.001, "savings_percentage": 38.0,
           "updated_at": datetime(2026, 9, 21, 10, 0)}
    row.update(extra)
    return row


def tracking_rows():
    """Rows as Validation.ipynb leaves them (incl. the retired email flow's send_to column)."""
    return [
        _q("q-100", "verified"),
        _q("q-200", "SENT", potential_savings_usd=0.0, primary_action=None, send_to="someone"),
        _q("q-300", "pending"),  # not validated yet -> not an approval item
    ]


def write_table(path, rows):
    schema = pa.schema([
        ("query_id", pa.string()), ("query_text", pa.string()), ("query_type", pa.string()),
        ("confidence", pa.string()), ("bottleneck_type", pa.string()), ("primary_action", pa.string()),
        ("root_cause", pa.string()), ("actual_cost_usd", pa.float64()), ("potential_savings_usd", pa.float64()),
        ("llm_refactor_suggestion", pa.string()), ("workflow_status", pa.string()), ("send_to", pa.string()),
        ("optimized_cost_usd", pa.float64()), ("savings_percentage", pa.float64()),
        ("updated_at", pa.timestamp("us")),
    ])
    write_deltalake(str(path), pa.Table.from_pylist(rows, schema=schema), mode="overwrite")


@pytest.fixture
def client(db_session):
    def _override():
        yield db_session

    app.dependency_overrides[get_db] = _override
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def env(db_session, customer):
    connection = Connection(
        id="conn-trk", customer_id=customer.id, platform="fabric", workspace="acelo demo",
        endpoint="https://api.fabric.microsoft.com/v1", auth_method="delegated",
        auth_metadata=json.dumps({
            "workspace_id": WORKSPACE_ID,
            "cluster_lakehouse_id": LAKEHOUSE_ID,
            "lakehouse_database": "Data",
            "cluster_result_schema": "dbo",
            # Query has its OWN mapping; it never borrows the Cluster Lakehouse.
            "query_lakehouse_id": LAKEHOUSE_ID,
            "query_result_schema": "dbo",
            # Deliberately NO sql_endpoint: approvals must not need it.
        }),
        status="connected",
    )
    environment = Environment(
        id="env-trk", customer_id=customer.id, connection_id=connection.id, name="acelo demo",
        platform="fabric", auth_mode="user", workspace_id=WORKSPACE_ID, workspace_name="acelo demo",
        status="environment_ready", provisioning_status=provisioning_service.INSTALLED,
        last_verified_at=datetime.utcnow(), last_discovered_at=datetime.utcnow(),
    )
    db_session.add_all([connection, environment])
    db_session.commit()
    return connection, environment


@pytest.fixture
def delta(tmp_path, monkeypatch):
    """Points the adapter's OneLake location at a real local Delta table."""
    table_path = tmp_path / TABLE
    write_table(table_path, tracking_rows())
    seen = {}

    def _location(self, workspace_id, lakehouse_id, table, schema):
        seen.update(workspace_id=workspace_id, lakehouse_id=lakehouse_id, table=table, schema=schema,
                    token=self._onelake_token())
        return str(tmp_path / table), {}

    monkeypatch.setattr(FabricAdapter, "_delta_location", _location)
    return table_path, seen


def _refresh(client, customer, headers=ONELAKE):
    return client.post("/api/approvals/refresh", headers={"X-Customer-Id": customer.id, **headers})


def _list(client, customer, **params):
    return client.get("/api/approvals", params=params, headers={"X-Customer-Id": customer.id}).json()


def test_real_delta_records_appear_in_approvals(client, customer, env, delta, db_session):
    _, seen = delta
    resp = _refresh(client, customer)
    assert resp.status_code == 200
    source = resp.json()["sources"][0]
    assert source.items() >= {"environment_id": "env-trk", "table": TABLE, "status": "ok",
                              "rows_read": 3, "candidates": 2, "created": 2, "skipped": 1}.items()
    assert source["unique_business_keys"] == 2 and source["updated"] == 0
    # Read from the configured workspace/lakehouse/schema with the user's OneLake token.
    assert seen == {"workspace_id": WORKSPACE_ID, "lakehouse_id": LAKEHOUSE_ID, "table": TABLE,
                    "schema": "dbo", "token": "eyJ-ONELAKE-STORAGE-TOKEN"}

    by_id = {a["resource_name"]: a for a in _list(client, customer)}
    assert set(by_id) == {"q-100", "q-200"}  # 'pending' is not validated yet
    q = by_id["q-100"]
    assert q["source"] == "tracking_table" and q["environment_id"] == "env-trk" and q["domain"] == "query"
    assert q["original_sql"] == "SELECT * FROM t WHERE k = 'q-100'"
    assert q["optimized_sql"] == "SELECT k FROM t WHERE k = 'q-100'"
    assert q["platform_validation_status"] == "verified"
    assert q["total_dbus_cost_usd"] == 9.5 and q["potential_monthly_savings"] == 3.25
    assert q["evidence"]["savings_percentage"] == 38.0
    assert q["evidence"]["tracking_updated_at"].startswith("2026-09-21")


def test_pending_records_are_shown_and_email_state_is_ignored(client, customer, env, delta):
    _refresh(client, customer)
    pending = _list(client, customer, status="PENDING")
    # "SENT" only meant "verified and emailed" in the retired flow — it is not a decision.
    assert {a["resource_name"] for a in pending} == {"q-100", "q-200"}
    assert {a["tracking_status"] for a in pending} == {"verified", "SENT"}
    for approval in pending:
        assert "send_to" not in approval and "send_to" not in approval["evidence"]


def test_real_zero_and_missing_values(client, customer, env, delta):
    _refresh(client, customer)
    q = next(a for a in _list(client, customer) if a["resource_name"] == "q-200")
    assert q["potential_monthly_savings"] == 0
    assert q["llm_optimization"] is None


def test_approve_and_reject_persist(client, customer, env, delta, db_session):
    _refresh(client, customer)
    by_id = {a["resource_name"]: a for a in _list(client, customer)}
    approved = client.post(f"/api/approvals/{by_id['q-100']['approval_id']}/approve",
                           headers={"X-Customer-Id": customer.id, **ACTOR}).json()
    assert approved["status"] == "APPROVED" and approved["approved_by"] == "Priya Reviewer"

    no_reason = client.post(f"/api/approvals/{by_id['q-200']['approval_id']}/reject", json={},
                            headers={"X-Customer-Id": customer.id, **ACTOR})
    assert no_reason.status_code == 422
    rejected = client.post(f"/api/approvals/{by_id['q-200']['approval_id']}/reject",
                           json={"reason": "Changes NULL handling"},
                           headers={"X-Customer-Id": customer.id, **ACTOR}).json()
    assert rejected["status"] == "REJECTED"

    db_session.expire_all()
    stored = {a.resource_name: a for a in db_session.query(OptimizationApproval).all()}
    assert stored["q-100"].status == "APPROVED"
    assert stored["q-200"].rejection_reason == "Changes NULL handling"
    assert stored["q-200"].original_sql and stored["q-200"].optimized_sql
    actions = [a.action for a in db_session.query(ApprovalAudit).order_by(ApprovalAudit.timestamp)]
    assert actions.count("CREATED") == 2 and "APPROVED" in actions and "REJECTED" in actions


def test_repeated_refresh_never_duplicates_or_resets(client, customer, env, delta, db_session):
    _refresh(client, customer)
    q = next(a for a in _list(client, customer) if a["resource_name"] == "q-100")
    client.post(f"/api/approvals/{q['approval_id']}/approve", headers={"X-Customer-Id": customer.id, **ACTOR})

    for _ in range(3):
        again = _refresh(client, customer).json()["sources"][0]
        assert again["created"] == 0
    assert db_session.query(OptimizationApproval).count() == 2
    assert next(a for a in _list(client, customer) if a["resource_name"] == "q-100")["status"] == "APPROVED"


def test_117_then_119_then_119(client, customer, env, delta, db_session):
    """The spec's scenario: 117 items, same 117 again (0 new), then 2 genuinely new (119, not re-created)."""
    path, _ = delta
    base = [_q(f"q-{i}", "verified") for i in range(117)]
    write_table(path, base)
    assert _refresh(client, customer).json()["sources"][0]["created"] == 117
    first = next(a for a in _list(client, customer) if a["resource_name"] == "q-5")
    client.post(f"/api/approvals/{first['approval_id']}/approve", headers={"X-Customer-Id": customer.id, **ACTOR})
    again = _refresh(client, customer).json()["sources"][0]
    assert again["created"] == 0 and again["unique_business_keys"] == 117
    write_table(path, base + [_q("q-new-1", "verified"), _q("q-new-2", "review_required")])
    more = _refresh(client, customer).json()["sources"][0]
    assert more["created"] == 2
    assert db_session.query(OptimizationApproval).count() == 119
    assert next(a for a in _list(client, customer) if a["resource_name"] == "q-5")["status"] == "APPROVED"


def test_new_tracking_rows_are_added_on_refresh(client, customer, env, delta):
    path, _ = delta
    _refresh(client, customer)
    write_table(path, tracking_rows() + [_q("q-400", "review_required")])
    assert _refresh(client, customer).json()["sources"][0]["created"] == 1
    assert len(_list(client, customer)) == 3


def test_sql_endpoint_is_not_required(client, customer, env, delta, monkeypatch):
    import sys

    connection, _ = env
    assert "sql_endpoint" not in json.loads(connection.auth_metadata)
    # Any attempt to use SQL/ODBC would fail loudly.
    monkeypatch.setitem(sys.modules, "pyodbc", None)
    assert _refresh(client, customer).status_code == 200
    assert len(_list(client, customer)) == 2


# --- failures never fabricate ------------------------------------------------------------

def test_missing_table_is_a_clear_error_and_creates_nothing(client, customer, env, tmp_path, monkeypatch, db_session):
    monkeypatch.setattr(FabricAdapter, "_delta_location",
                        lambda self, w, l, t, s: (str(tmp_path / "does_not_exist"), {}))
    body = _refresh(client, customer).json()
    source = body["sources"][0]
    assert source["status"] == "failed"
    assert source["error_code"] == "RESOURCE_NOT_FOUND"
    assert TABLE in source["message"]
    assert db_session.query(OptimizationApproval).count() == 0
    assert _list(client, customer) == []


def test_no_onelake_token_is_a_sign_in_error_not_a_fallback(client, customer, env, delta, db_session):
    source = _refresh(client, customer, headers={}).json()["sources"][0]
    assert source["status"] == "failed" and source["error_code"] == "AUTHENTICATION_FAILED"
    assert "Azure Storage / user_impersonation" in source["message"]
    assert db_session.query(OptimizationApproval).count() == 0


def test_no_fabric_environment_is_reported(client, customer, db_session):
    resp = _refresh(client, customer)
    assert resp.status_code == 409
    assert "No Fabric environment" in resp.json()["detail"]


def test_missing_lakehouse_is_reported(client, customer, env, db_session):
    connection, _ = env
    metadata = json.loads(connection.auth_metadata)
    metadata.pop("query_lakehouse_id")
    connection.auth_metadata = json.dumps(metadata)
    db_session.commit()
    source = _refresh(client, customer).json()["sources"][0]
    assert source["error_code"] == "NOT_CONFIGURED"


def test_no_dummy_records_without_a_read(client, customer, env):
    assert _list(client, customer) == []
    assert client.get("/api/approvals/summary", headers={"X-Customer-Id": customer.id}).json()["PENDING"] == 0


def test_other_tenants_are_isolated(client, customer, env, delta, db_session):
    _refresh(client, customer)
    other = Customer(id="other", name="Other")
    db_session.add(other)
    db_session.commit()
    assert client.get("/api/approvals", headers={"X-Customer-Id": "other"}).json() == []


# --- tracking notebook + pipeline -------------------------------------------------------------

def test_tracking_notebook_keeps_the_business_rule_and_has_no_email():
    from pathlib import Path

    nb = json.loads((Path(__file__).resolve().parents[1] / "optimization_package" / "approvals" /
                     "ClusterApprovalTracking.ipynb").read_text(encoding="utf-8"))
    source = "\n".join("".join(c["source"]) for c in nb["cells"]).lower()
    assert "optimization_label in ('risky', 'moderately optimized')" in source
    assert "when not matched then" in source and "'pending'" in source
    for token in ("smtp", "gmail", "mime", "password", "send_to", "email_sent_at"):
        assert token not in source, token


def test_pipeline_runs_tracking_only_after_the_cluster_notebook_succeeds():
    import base64

    definition = FabricAdapter.pipeline_definition("nb-cluster", WORKSPACE_ID, "nb-tracking")
    content = json.loads(base64.b64decode(definition["parts"][0]["payload"]))
    cluster, tracking = content["properties"]["activities"]
    assert cluster["typeProperties"]["notebookId"] == "nb-cluster" and cluster["dependsOn"] == []
    assert tracking["typeProperties"]["notebookId"] == "nb-tracking"
    assert tracking["dependsOn"] == [{"activity": cluster["name"], "dependencyConditions": ["Succeeded"]}]
    assert set(tracking["typeProperties"]["parameters"]) == {
        "acelo_run_id", "result_table", "result_schema", "approval_tracking_table",
    }
    # Without a tracking notebook the pipeline is exactly the single Cluster step.
    single = json.loads(base64.b64decode(
        FabricAdapter.pipeline_definition("nb-cluster", WORKSPACE_ID)["parts"][0]["payload"]))
    assert len(single["properties"]["activities"]) == 1


def test_reads_go_to_fabric_onelake_only_never_a_storage_account():
    """The tracking table stays in the Fabric Lakehouse; ACELO addresses it via OneLake."""
    adapter = FabricAdapter(endpoint="https://api.fabric.microsoft.com/v1",
                            auth_metadata={"workspace_id": WORKSPACE_ID}, secret=None)
    adapter.delegated_mode = True
    adapter.onelake_token = "eyJ-ONELAKE"
    uri, options = adapter._delta_location(WORKSPACE_ID, LAKEHOUSE_ID, "cluster_optimization_tracking", "dbo")
    assert uri == (f"abfss://{WORKSPACE_ID}@onelake.dfs.fabric.microsoft.com/"
                   f"{LAKEHOUSE_ID}/Tables/dbo/cluster_optimization_tracking")
    # Only the Fabric OneLake endpoint + the user's delegated token: no account
    # name, account key, SAS or connection string of any storage resource.
    assert options == {"bearer_token": "eyJ-ONELAKE", "use_fabric_endpoint": "true"}
    assert "blob.core.windows.net" not in uri and "dfs.core.windows.net" not in uri
