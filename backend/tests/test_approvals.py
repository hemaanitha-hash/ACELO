"""
In-app approval workflow for QUERY optimizations: a query run's validated
tracking rows -> PENDING approvals -> human decision -> explicit execution.
Cluster recommendations are reporting only and never become approvals.
No email, no seeded data, no implicit transitions.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from database import get_db
from main import app
from models import (
    AnalysisJob, ApprovalAudit, ClusterRecommendation, Connection, Customer, Environment, JobRun,
    Notification, OptimizationApproval,
)
from platforms.base import RunStatusResult, StartAnalysisResult
from platforms.errors import ErrorCode, PlatformError
from services import approval_service

ACTOR = {"X-Acelo-User-Id": "user-oid-1", "X-Acelo-User-Name": "Priya Reviewer"}


def _qrow(query_id, status="verified", **extra):
    """A query_tracking_full_v1 row as the Detection + Validation notebooks write it."""
    row = {
        "query_id": query_id, "query_text": f"SELECT * FROM sales WHERE id = '{query_id}'",
        "query_type": "SELECT", "bottleneck_type": "FULL_TABLE_SCAN", "root_cause": "Missing partition filter",
        "primary_action": f"Prune partitions for {query_id}", "confidence": "HIGH", "priority": "HIGH",
        "actual_cost_usd": 12.5, "potential_savings_usd": 4.25, "savings_pct": 0.34,
        "llm_refactor_suggestion": f"```sql\nSELECT id FROM sales WHERE id = '{query_id}';\n```",
        "workflow_status": status, "optimized_cost_usd": 0.0021, "savings_percentage": 41.5,
        "execution_time_ms": 9000, "cpu_time_ms": 7000, "bytes_scanned": 5368709120,
        "bytes_spilled": 0, "shuffle_bytes": 1048576,
    }
    row.update(extra)
    return row


ROWS = [
    _qrow("q-verified"),
    _qrow("q-review", status="review_required"),
    _qrow("q-pending", status="pending"),      # not validated yet -> no approval
    _qrow("q-applied", status="APPLIED"),      # decided in the retired email flow -> no approval
]


def _payload(rows, domain="query"):
    return {"optimization_type": domain, "source_payload": {"rows": rows, "row_count": len(rows)}}


@pytest.fixture
def client(db_session):
    def _override():
        yield db_session

    app.dependency_overrides[get_db] = _override
    yield TestClient(app)
    app.dependency_overrides.clear()


def _make_run(db, customer, *, domain="query", run_id="run-1", rows=ROWS, status="COMPLETED", env_id="env-1"):
    connection = Connection(
        id=f"conn-{run_id}", customer_id=customer.id, platform="fabric", workspace="ws",
        endpoint="https://api.fabric.microsoft.com/v1", auth_method="delegated",
        auth_metadata=json.dumps({"workspace_id": "ws-1"}), status="connected",
    )
    db.add(connection)
    if env_id:
        db.add(Environment(id=f"{env_id}-{run_id}", customer_id=customer.id, connection_id=connection.id,
                           name="env", platform="fabric", auth_mode="user"))
    job = AnalysisJob(id=f"job-{run_id}", customer_id=customer.id, connection_id=connection.id,
                      request="Find unhealthy queries", intent=domain, platform="fabric", status=status)
    run = JobRun(id=run_id, analysis_job_id=job.id, domain=domain, platform="fabric",
                 platform_run_id="fabric-run-1", status=status,
                 result_json=json.dumps(_payload(rows, domain)) if rows is not None else None)
    db.add_all([job, run])
    db.commit()
    return run


def _results(client, customer, job_id="job-run-1"):
    return client.get(f"/api/jobs/{job_id}/results", headers={"X-Customer-Id": customer.id})


def _approvals(client, customer, **params):
    return client.get("/api/approvals", params=params, headers={"X-Customer-Id": customer.id}).json()


def _act(client, customer, approval_id, action, body=None, actor=ACTOR):
    return client.post(f"/api/approvals/{approval_id}/{action}", json=body or {},
                       headers={"X-Customer-Id": customer.id, **actor})


# 1-3. real query results -> approvals, idempotently -----------------------------------------

def test_validated_queries_become_pending_approvals_with_real_evidence(client, customer, db_session):
    _make_run(db_session, customer)
    _results(client, customer)

    items = _approvals(client, customer)
    assert {a["resource_name"] for a in items} == {"q-verified", "q-review"}
    assert all(a["status"] == "PENDING" and a["domain"] == "query" for a in items)
    q = next(a for a in items if a["resource_name"] == "q-verified")
    assert q["acelo_run_id"] == "run-1" and q["environment_id"] == "env-1-run-1"
    assert q["original_sql"] == "SELECT * FROM sales WHERE id = 'q-verified'"
    # The notebooks' own extraction of the LLM suggestion.
    assert q["optimized_sql"] == "SELECT id FROM sales WHERE id = 'q-verified'"
    assert q["platform_validation_status"] == "verified"
    assert q["optimization_label"] == "FULL_TABLE_SCAN"
    assert q["llm_optimization"] == "Prune partitions for q-verified"
    assert q["total_dbus_cost_usd"] == 12.5 and q["potential_monthly_savings"] == 4.25
    for field, value in (("savings_percentage", 41.5), ("cpu_time_ms", 7000), ("bytes_scanned", 5368709120),
                         ("bytes_spilled", 0), ("shuffle_bytes", 1048576), ("root_cause", "Missing partition filter")):
        assert q["evidence"][field] == value
    review = next(a for a in items if a["resource_name"] == "q-review")
    assert review["platform_validation_status"] == "review_required"


@pytest.mark.parametrize("rows,status", [(None, "COMPLETED"), ([], "COMPLETED"), (ROWS, "FAILED")])
def test_no_result_creates_no_approval(client, customer, db_session, rows, status):
    _make_run(db_session, customer, rows=rows, status=status)
    _results(client, customer)
    assert _approvals(client, customer) == []


def test_repeated_retrieval_never_duplicates_or_resets(client, customer, db_session):
    _make_run(db_session, customer)
    _results(client, customer)
    first = {a["resource_name"]: a for a in _approvals(client, customer)}
    _act(client, customer, first["q-verified"]["approval_id"], "approve")

    for _ in range(3):
        _results(client, customer)

    after = _approvals(client, customer)
    assert len(after) == 2
    assert next(a for a in after if a["resource_name"] == "q-verified")["status"] == "APPROVED"
    assert db_session.query(OptimizationApproval).count() == 2


# Cluster = reporting only ----------------------------------------------------------------------

def test_cluster_results_never_become_approvals(client, customer, db_session):
    rows = [{"cluster_id": "c-1", "cluster_name": "etl-heavy", "optimization_label": "Risky",
             "current_workers": 8, "recommended_max_workers": 5, "potential_monthly_savings": 340.25}]
    _make_run(db_session, customer, domain="cluster", rows=rows)
    for _ in range(2):
        _results(client, customer)
    assert db_session.query(OptimizationApproval).count() == 0
    assert _approvals(client, customer, domain="cluster") == []
    # ... it becomes current cluster state instead, once.
    assert db_session.query(ClusterRecommendation).count() == 1
    resp = client.post("/api/approvals/from-run", json={"job_run_id": "run-1", "resource_id": "c-1"},
                       headers={"X-Customer-Id": customer.id})
    assert resp.status_code == 409 and "reporting only" in resp.json()["detail"]


def test_a_cluster_record_cannot_be_executed(client, customer, db_session):
    approval = OptimizationApproval(customer_id=customer.id, platform="fabric", domain="cluster",
                                    resource_id="c", resource_name="c", status="APPROVED", source_ref="x")
    db_session.add(approval)
    db_session.commit()
    resp = _act(client, customer, approval.id, "execute")
    assert resp.status_code == 409 and "reporting only" in resp.json()["detail"]


# 4-6, 14. decisions -------------------------------------------------------------------

def _pending(client, customer, db_session, name="q-verified"):
    if not db_session.query(JobRun).filter(JobRun.id == "run-1").first():
        _make_run(db_session, customer)
    _results(client, customer)
    return next(a for a in _approvals(client, customer) if a["resource_name"] == name)


def test_approve_moves_pending_to_approved_and_is_attributed(client, customer, db_session):
    approval = _pending(client, customer, db_session)
    resp = _act(client, customer, approval["approval_id"], "approve")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "APPROVED"
    assert body["approved_by"] == "Priya Reviewer"
    assert body["approved_at"]
    assert body["execution_id"] is None  # approval is NOT execution


def test_reject_requires_a_reason(client, customer, db_session):
    approval = _pending(client, customer, db_session)
    for body in ({}, {"reason": ""}, {"reason": "  "}, {"reason": "no"}):
        resp = _act(client, customer, approval["approval_id"], "reject", body)
        assert resp.status_code == 422
    assert _approvals(client, customer, status="PENDING")


def test_reject_moves_pending_to_rejected_with_reason(client, customer, db_session):
    approval = _pending(client, customer, db_session)
    body = _act(client, customer, approval["approval_id"], "reject",
                {"reason": "Changes the result ordering the report relies on"}).json()
    assert body["status"] == "REJECTED"
    assert body["rejected_by"] == "Priya Reviewer"
    assert body["rejection_reason"] == "Changes the result ordering the report relies on"
    # Persisted with the SQL that was reviewed.
    assert body["original_sql"] and body["optimized_sql"]


@pytest.mark.parametrize("first,second", [
    ("approve", "approve"), ("approve", "reject"), ("reject", "approve"), ("reject", "reject"),
])
def test_decided_records_cannot_be_decided_again(client, customer, db_session, first, second):
    approval = _pending(client, customer, db_session)
    _act(client, customer, approval["approval_id"], first, {"reason": "valid reason"})
    resp = _act(client, customer, approval["approval_id"], second, {"reason": "valid reason"})
    assert resp.status_code == 409


def test_actions_require_an_identified_user(client, customer, db_session):
    approval = _pending(client, customer, db_session)
    for action in ("approve", "reject", "execute", "cancel"):
        resp = _act(client, customer, approval["approval_id"], action, {"reason": "x" * 5}, actor={})
        assert resp.status_code == 401
    assert _approvals(client, customer, status="PENDING")


def test_decisions_notify_in_app_only(client, customer, db_session):
    approval = _pending(client, customer, db_session)
    other = _pending(client, customer, db_session, name="q-review")
    _act(client, customer, approval["approval_id"], "approve")
    _act(client, customer, other["approval_id"], "reject", {"reason": "Not equivalent"})
    titles = [n.title for n in db_session.query(Notification).all()]
    assert "Query optimization requires your approval." in titles
    assert "Query optimization approved." in titles and "Query optimization rejected." in titles


# 7-9. execution is explicit, real, and honest -------------------------------------------

class _Adapter:
    def __init__(self, start_error=None, validation="COMPLETED"):
        self.start_error = start_error
        self.validation = validation
        self.actions = []

    async def start_execution(self, action):
        self.actions.append(action)
        if self.start_error:
            raise self.start_error
        return StartAnalysisResult(platform_run_id="exec-run-42", status="RUNNING")

    async def validate_execution(self, platform_run_id):
        return RunStatusResult(status=self.validation, error="validation failed" if self.validation == "FAILED" else None)


def _approved(client, customer, db_session, name="q-verified"):
    approval = _pending(client, customer, db_session, name)
    _act(client, customer, approval["approval_id"], "approve")
    return approval["approval_id"]


def test_pending_cannot_be_executed(client, customer, db_session, monkeypatch):
    adapter = _Adapter()
    monkeypatch.setattr("api.approvals._adapter_for", lambda *a: adapter)
    approval = _pending(client, customer, db_session)
    assert _act(client, customer, approval["approval_id"], "execute").status_code == 409
    assert adapter.actions == []


def test_execution_success_goes_executing_then_completed(client, customer, db_session, monkeypatch):
    adapter = _Adapter(validation="COMPLETED")
    monkeypatch.setattr("api.approvals._adapter_for", lambda *a: adapter)
    approval_id = _approved(client, customer, db_session)

    started = _act(client, customer, approval_id, "execute").json()
    assert started["status"] == "EXECUTING"
    assert started["execution_id"] == "exec-run-42"
    # Exactly the approved query is sent to the Apply notebook.
    assert adapter.actions[0] == {"type": "query_apply", "approval_id": approval_id,
                                  "query_id": "q-verified", "approved_by": "Priya Reviewer"}

    detail = client.get(f"/api/approvals/{approval_id}", headers={"X-Customer-Id": customer.id}).json()
    assert detail["status"] == "COMPLETED"
    assert detail["validation_status"] == "passed"


def test_review_required_query_can_be_approved_but_not_applied(client, customer, db_session, monkeypatch):
    adapter = _Adapter()
    monkeypatch.setattr("api.approvals._adapter_for", lambda *a: adapter)
    approval_id = _approved(client, customer, db_session, name="q-review")
    resp = _act(client, customer, approval_id, "execute")
    assert resp.status_code == 409 and "did not verify" in resp.json()["detail"]
    assert adapter.actions == []


def test_execution_failure_is_failed_with_the_real_error(client, customer, db_session, monkeypatch):
    adapter = _Adapter(validation="FAILED")
    monkeypatch.setattr("api.approvals._adapter_for", lambda *a: adapter)
    approval_id = _approved(client, customer, db_session)
    _act(client, customer, approval_id, "execute")
    detail = client.get(f"/api/approvals/{approval_id}", headers={"X-Customer-Id": customer.id}).json()
    assert detail["status"] == "FAILED"
    assert detail["execution_id"] == "exec-run-42"
    assert detail["execution_error"] == "validation failed"


def test_refused_start_is_failed(client, customer, db_session, monkeypatch):
    adapter = _Adapter(start_error=PlatformError(ErrorCode.PERMISSION_DENIED, "No permission to run the notebook."))
    monkeypatch.setattr("api.approvals._adapter_for", lambda *a: adapter)
    approval_id = _approved(client, customer, db_session)
    body = _act(client, customer, approval_id, "execute").json()
    assert body["status"] == "FAILED"
    assert body["execution_error"] == "No permission to run the notebook."


def test_real_fabric_adapter_runs_the_apply_notebook(monkeypatch):
    """No fake execution: the Fabric adapter starts the registered Apply Approved Query notebook."""
    import asyncio

    from platforms.fabric import FabricAdapter

    adapter = FabricAdapter(endpoint="https://api.fabric.microsoft.com/v1",
                            auth_metadata={"workspace_id": "ws"}, secret=None)
    calls = []

    async def start(domain, parameters):
        calls.append((domain, parameters))
        return StartAnalysisResult(platform_run_id="job-1", status="STARTING")

    monkeypatch.setattr(adapter, "start_analysis", start)
    asyncio.run(adapter.start_execution({"type": "query_apply", "query_id": "q-1", "approval_id": "a-1",
                                         "approved_by": "Priya"}))
    assert calls == [("query_apply", {"query_id": "q-1", "approval_id": "a-1", "approved_by": "Priya"})]
    with pytest.raises(Exception):
        asyncio.run(adapter.start_execution({"type": "cluster_rightsizing"}))


# 10. dashboard KPIs from real records ---------------------------------------------------

def test_summary_counts_real_records(client, customer, db_session):
    empty = client.get("/api/approvals/summary", headers={"X-Customer-Id": customer.id}).json()
    assert empty == {s: 0 for s in approval_service.STATUSES}

    approval = _pending(client, customer, db_session)
    _act(client, customer, approval["approval_id"], "reject", {"reason": "Needed for SLA"})
    counts = client.get("/api/approvals/summary", headers={"X-Customer-Id": customer.id}).json()
    assert counts["PENDING"] == 1 and counts["REJECTED"] == 1 and counts["APPROVED"] == 0


# 11. nothing seeded -----------------------------------------------------------------------

def test_no_approval_data_exists_without_real_results(client, customer, db_session):
    for path in ("/api/approvals", "/api/approvals/summary", "/api/overview", "/api/optimizations"):
        client.get(path, headers={"X-Customer-Id": customer.id})
    assert db_session.query(OptimizationApproval).count() == 0
    assert _approvals(client, customer) == []


def test_no_email_dependency_anywhere_in_the_approval_path():
    backend = Path(__file__).resolve().parents[1]
    for rel in ("services/approval_service.py", "api/approvals.py", "services/job_service.py",
                "services/execution_worker.py"):
        source = (backend / rel).read_text(encoding="utf-8").lower()
        for token in ("smtplib", "imaplib", "mimetext", "mimemultipart", "smtp.", "app_password",
                      "sender_email", "approver_email", "gmail"):
            assert token not in source, (rel, token)
    manifest = json.loads((backend / "optimization_package" / "manifest.json").read_text(encoding="utf-8"))
    sources = " ".join(a["source"] for a in manifest["assets"]).lower()
    assert "email" not in sources


# 12. tenant isolation ------------------------------------------------------------------------

def test_other_customers_cannot_see_or_act(client, customer, db_session):
    approval = _pending(client, customer, db_session)
    other = Customer(id="other-cust", name="Other")
    db_session.add(other)
    db_session.commit()
    headers = {"X-Customer-Id": other.id, **ACTOR}
    assert client.get("/api/approvals", headers=headers).json() == []
    assert client.get(f"/api/approvals/{approval['approval_id']}", headers=headers).status_code == 404
    assert client.post(f"/api/approvals/{approval['approval_id']}/approve", headers=headers).status_code == 404
    assert client.get("/api/approvals/summary", headers=headers).json()["PENDING"] == 0


# 13. audit trail ---------------------------------------------------------------------------------

def test_every_action_is_audited_with_the_real_user(client, customer, db_session):
    approval = _pending(client, customer, db_session)
    _act(client, customer, approval["approval_id"], "approve")
    detail = client.get(f"/api/approvals/{approval['approval_id']}", headers={"X-Customer-Id": customer.id}).json()
    history = detail["history"]
    assert [h["action"] for h in history] == ["CREATED", "APPROVED"]
    assert history[0]["user_name"] is None  # created by ACELO from results, not a person
    assert history[1] == {
        **history[1],
        "previous_status": "PENDING", "new_status": "APPROVED",
        "user_id": "user-oid-1", "user_name": "Priya Reviewer",
    }
    rows = db_session.query(ApprovalAudit).filter(ApprovalAudit.approval_id == approval["approval_id"]).all()
    assert all(r.acelo_run_id == "run-1" and r.environment_id == "env-1-run-1" for r in rows)


def test_rejection_reason_is_in_the_audit(client, customer, db_session):
    approval = _pending(client, customer, db_session)
    _act(client, customer, approval["approval_id"], "reject", {"reason": "Owner is on leave"})
    history = client.get(f"/api/approvals/{approval['approval_id']}",
                         headers={"X-Customer-Id": customer.id}).json()["history"]
    assert history[-1]["action"] == "REJECTED" and history[-1]["reason"] == "Owner is on leave"


# 15-16. real zero stays zero, missing stays missing ---------------------------------------------

def test_real_zero_stays_zero_and_missing_stays_null(client, customer, db_session):
    rows = [
        _qrow("q-zero", potential_savings_usd=0, actual_cost_usd=0.0),
        {"query_id": "q-sparse", "workflow_status": "verified"},
    ]
    _make_run(db_session, customer, rows=rows)
    _results(client, customer)
    by_name = {a["resource_name"]: a for a in _approvals(client, customer)}
    assert by_name["q-zero"]["potential_monthly_savings"] == 0
    assert by_name["q-zero"]["total_dbus_cost_usd"] == 0
    for field in ("original_sql", "optimized_sql", "total_dbus_cost_usd", "potential_monthly_savings",
                  "llm_optimization"):
        assert by_name["q-sparse"][field] is None
    assert by_name["q-sparse"]["evidence"] == {}
