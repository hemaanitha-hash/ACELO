"""
In-app approval workflow: real results -> PENDING approvals -> human decision
-> explicit execution. No email, no seeded data, no implicit transitions.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from database import get_db
from main import app
from models import (
    AnalysisJob, ApprovalAudit, Connection, Customer, Environment, JobRun, OptimizationApproval,
)
from platforms.base import RunStatusResult, StartAnalysisResult
from platforms.errors import ErrorCode, PlatformError
from services import approval_service

ACTOR = {"X-Acelo-User-Id": "user-oid-1", "X-Acelo-User-Name": "Priya Reviewer"}


def _row(name, label, **extra):
    row = {
        "cluster_id": f"id-{name}", "cluster_name": name, "optimization_label": label,
        "current_workers": 8, "recommended_max_workers": 5, "total_dbus_cost_usd": 1200.5,
        "potential_monthly_savings": 340.25, "llm_optimization": f"Plan for {name}",
        "efficiency_score": 0.31, "avg_cpu_util": 12.5, "underutilized_label": "Highly Underutilized",
    }
    row.update(extra)
    return row


ROWS = [
    _row("etl-heavy", "Risky"),
    _row("bi-adhoc", "Moderately Optimized"),
    _row("ml-train", "Optimized"),                 # healthy -> no approval
    _row("", "Risky", cluster_id="id-nameless"),    # no name -> no approval (legacy rule)
]


def _payload(rows):
    return {"optimization_type": "cluster", "source_payload": {"rows": rows, "row_count": len(rows)}}


@pytest.fixture
def client(db_session):
    def _override():
        yield db_session

    app.dependency_overrides[get_db] = _override
    yield TestClient(app)
    app.dependency_overrides.clear()


def _make_run(db, customer, *, platform="fabric", run_id="run-1", rows=ROWS, status="COMPLETED", env_id="env-1"):
    connection = Connection(
        id=f"conn-{run_id}", customer_id=customer.id, platform=platform, workspace="ws",
        endpoint="https://api.fabric.microsoft.com/v1", auth_method="delegated",
        auth_metadata=json.dumps({"workspace_id": "ws-1"}), status="connected",
    )
    db.add(connection)
    if env_id:
        db.add(Environment(id=f"{env_id}-{run_id}", customer_id=customer.id, connection_id=connection.id,
                           name="env", platform=platform, auth_mode="user"))
    job = AnalysisJob(id=f"job-{run_id}", customer_id=customer.id, connection_id=connection.id,
                      request="Check my cluster utilization", intent="cluster", platform=platform,
                      status=status)
    run = JobRun(id=run_id, analysis_job_id=job.id, domain="cluster", platform=platform,
                 platform_run_id="fabric-run-1", status=status,
                 result_json=json.dumps(_payload(rows)) if rows is not None else None)
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


# 1-3. real results -> approvals, idempotently -----------------------------------------

def test_real_result_creates_pending_approvals_for_flagged_clusters_only(client, customer, db_session):
    _make_run(db_session, customer)
    _results(client, customer)

    items = _approvals(client, customer)
    assert {a["resource_name"] for a in items} == {"etl-heavy", "bi-adhoc"}
    assert all(a["status"] == "PENDING" for a in items)
    etl = next(a for a in items if a["resource_name"] == "etl-heavy")
    # Straight from the result row — nothing invented.
    assert etl["acelo_run_id"] == "run-1"
    assert etl["customer_id"] == customer.id
    assert etl["environment_id"] == "env-1-run-1"
    assert etl["platform"] == "fabric"
    assert etl["current_workers"] == 8 and etl["recommended_max_workers"] == 5
    assert etl["total_dbus_cost_usd"] == 1200.5 and etl["potential_monthly_savings"] == 340.25
    assert etl["llm_optimization"] == "Plan for etl-heavy"
    assert etl["evidence"] == {"efficiency_score": 0.31, "avg_cpu_util": 12.5,
                               "underutilized_label": "Highly Underutilized"}


@pytest.mark.parametrize("rows,status", [(None, "COMPLETED"), ([], "COMPLETED"), (ROWS, "FAILED")])
def test_no_result_creates_no_approval(client, customer, db_session, rows, status):
    _make_run(db_session, customer, rows=rows, status=status)
    _results(client, customer)
    assert _approvals(client, customer) == []


def test_repeated_retrieval_never_duplicates_or_resets(client, customer, db_session):
    _make_run(db_session, customer)
    _results(client, customer)
    first = {a["resource_name"]: a for a in _approvals(client, customer)}
    _act(client, customer, first["etl-heavy"]["approval_id"], "approve")

    for _ in range(3):
        _results(client, customer)

    after = _approvals(client, customer)
    assert len(after) == 2
    assert next(a for a in after if a["resource_name"] == "etl-heavy")["status"] == "APPROVED"
    assert db_session.query(OptimizationApproval).count() == 2


# 4-6, 14. decisions -------------------------------------------------------------------

def _pending(client, customer, db_session, name="etl-heavy"):
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
                {"reason": "Month-end close depends on this cluster"}).json()
    assert body["status"] == "REJECTED"
    assert body["rejected_by"] == "Priya Reviewer"
    assert body["rejection_reason"] == "Month-end close depends on this cluster"


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


def _approved(client, customer, db_session):
    approval = _pending(client, customer, db_session)
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
    assert adapter.actions[0]["recommended_max_workers"] == 5

    detail = client.get(f"/api/approvals/{approval_id}", headers={"X-Customer-Id": customer.id}).json()
    assert detail["status"] == "COMPLETED"
    assert detail["validation_status"] == "passed"


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
    adapter = _Adapter(start_error=PlatformError(ErrorCode.PERMISSION_DENIED, "No permission to resize."))
    monkeypatch.setattr("api.approvals._adapter_for", lambda *a: adapter)
    approval_id = _approved(client, customer, db_session)
    body = _act(client, customer, approval_id, "execute").json()
    assert body["status"] == "FAILED"
    assert body["execution_error"] == "No permission to resize."


def test_unsupported_platform_execution_keeps_approved_and_changes_nothing(client, customer, db_session):
    """Real adapters: Fabric does not implement execution, so nothing may pretend it ran."""
    approval_id = _approved(client, customer, db_session)
    resp = _act(client, customer, approval_id, "execute")
    assert resp.status_code == 409
    assert "not available for fabric" in resp.json()["detail"]
    detail = client.get(f"/api/approvals/{approval_id}", headers={"X-Customer-Id": customer.id}).json()
    assert detail["status"] == "APPROVED"
    assert detail["execution_id"] is None


def test_file_run_approvals_cannot_execute(client, customer, db_session):
    _make_run(db_session, customer, platform="file", env_id=None)
    _results(client, customer)
    approval = _approvals(client, customer)[0]
    assert approval["environment_id"] is None and approval["platform"] == "file"
    _act(client, customer, approval["approval_id"], "approve")
    resp = _act(client, customer, approval["approval_id"], "execute")
    assert resp.status_code == 409
    assert "uploaded file" in resp.json()["detail"]


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
    for rel in ("services/approval_service.py", "api/approvals.py"):
        source = (backend / rel).read_text(encoding="utf-8").lower()
        for token in ("smtplib", "mimetext", "mimemultipart", "smtp.", "app_password", "sender_email",
                      "approver_email", "gmail"):
            assert token not in source.replace("smtp/gmail", ""), (rel, token)
    manifest = (backend / "optimization_package" / "manifest.json").read_text(encoding="utf-8").lower()
    assert "email" not in manifest


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
        _row("zero", "Risky", potential_monthly_savings=0, total_dbus_cost_usd=0.0),
        {"cluster_id": "id-sparse", "cluster_name": "sparse", "optimization_label": "Risky"},
    ]
    _make_run(db_session, customer, rows=rows)
    _results(client, customer)
    by_name = {a["resource_name"]: a for a in _approvals(client, customer)}
    assert by_name["zero"]["potential_monthly_savings"] == 0
    assert by_name["zero"]["total_dbus_cost_usd"] == 0
    for field in ("current_workers", "recommended_max_workers", "total_dbus_cost_usd",
                  "potential_monthly_savings", "llm_optimization"):
        assert by_name["sparse"][field] is None
    assert by_name["sparse"]["evidence"] == {}


# Send to Approval from Results ------------------------------------------------------------------

def test_send_to_approval_is_idempotent_and_only_for_flagged_rows(client, customer, db_session):
    _make_run(db_session, customer)
    url, headers = "/api/approvals/from-run", {"X-Customer-Id": customer.id}
    first = client.post(url, json={"job_run_id": "run-1", "resource_id": "id-etl-heavy"}, headers=headers).json()
    again = client.post(url, json={"job_run_id": "run-1", "resource_id": "id-etl-heavy"}, headers=headers).json()
    assert first[0]["approval_id"] == again[0]["approval_id"]
    healthy = client.post(url, json={"job_run_id": "run-1", "resource_id": "id-ml-train"}, headers=headers)
    assert healthy.status_code == 422
    assert db_session.query(OptimizationApproval).count() == 1
