"""
In-app approvals for optimization recommendations.

Every record comes from a real optimizer result (see services/approval_service).
There is no email path and no seeded data. Every read and write is scoped to the
calling customer; actions require an identified user (X-Acelo-User-Id /
X-Acelo-User-Name, supplied by the signed-in session) and are audited.
"""

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from agent.router import build_adapter
from api.deps import get_current_customer
from api.environments import fabric_access_token
from database import get_db
import json

from models import (
    AnalysisJob, ApprovalAudit, Connection, Customer, Environment, JobRun, OptimizationApproval,
)
from platforms.base import PlatformCapabilityNotImplemented
from platforms.errors import PlatformError
from services import approval_service as approvals
from services import environment_service, provisioning_service

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


class RejectRequest(BaseModel):
    reason: str | None = None


class CancelRequest(BaseModel):
    reason: str | None = None


class FromRunRequest(BaseModel):
    job_run_id: str
    resource_id: str | None = None


def current_actor(
    x_acelo_user_id: str | None = Header(default=None),
    x_acelo_user_name: str | None = Header(default=None),
) -> approvals.Actor:
    """
    The person taking the action. Required: an approval with no identified
    approver is refused rather than attributed to a default or dummy user.
    """
    user_id = (x_acelo_user_id or "").strip()
    user_name = (x_acelo_user_name or "").strip()
    if not user_id or not user_name:
        raise HTTPException(status_code=401, detail="Sign in to approve, reject or execute optimizations.")
    return approvals.Actor(user_id=user_id[:200], user_name=user_name[:200])


def _get_or_404(db: Session, approval_id: str, customer: Customer) -> OptimizationApproval:
    approval = (
        db.query(OptimizationApproval)
        .filter(OptimizationApproval.id == approval_id, OptimizationApproval.customer_id == customer.id)
        .first()
    )
    if not approval:
        raise HTTPException(status_code=404, detail="Approval not found")
    return approval


def _history(db: Session, approval: OptimizationApproval) -> list[ApprovalAudit]:
    return (
        db.query(ApprovalAudit)
        .filter(ApprovalAudit.approval_id == approval.id)
        .order_by(ApprovalAudit.timestamp)
        .all()
    )


def _adapter_for(db: Session, approval: OptimizationApproval, delegated_token: str | None):
    """The platform adapter for the approval's run or environment; None for uploaded files."""
    if approval.platform == "file":
        return None
    connection = None
    if approval.acelo_run_id:
        run = db.query(JobRun).filter(JobRun.id == approval.acelo_run_id).first()
        job = db.query(AnalysisJob).filter(AnalysisJob.id == run.analysis_job_id).first() if run else None
        connection = db.query(Connection).filter(Connection.id == job.connection_id).first() if job else None
    elif approval.environment_id:
        environment = db.query(Environment).filter(Environment.id == approval.environment_id).first()
        if environment and environment.connection_id:
            connection = db.query(Connection).filter(Connection.id == environment.connection_id).first()
    if connection is None:
        return None
    return build_adapter(connection, db, delegated_token)


def onelake_token(x_onelake_token: str | None = Header(default=None)) -> str | None:
    """Delegated OneLake (storage.azure.com) token for direct Delta reads. Never stored or logged."""
    return x_onelake_token


tracking_config = approvals.tracking_config


def _refused(exc: approvals.ApprovalError):
    raise HTTPException(status_code=exc.status_code, detail=exc.message)


@router.get("")
def list_approvals(
    status: str | None = None,
    acelo_run_id: str | None = None,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
):
    query = db.query(OptimizationApproval).filter(OptimizationApproval.customer_id == customer.id)
    if status:
        wanted = [s.strip().upper() for s in status.split(",") if s.strip()]
        unknown = sorted(set(wanted) - set(approvals.STATUSES))
        if unknown:
            raise HTTPException(status_code=422, detail=f"Unknown status: {', '.join(unknown)}.")
        query = query.filter(OptimizationApproval.status.in_(wanted))
    if acelo_run_id:
        query = query.filter(OptimizationApproval.acelo_run_id == acelo_run_id)
    rows = query.order_by(
        OptimizationApproval.created_at.desc(), OptimizationApproval.potential_monthly_savings.desc()
    ).all()
    return [approvals.serialize(a) for a in rows]


@router.get("/summary")
def approvals_summary(
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
):
    """Counts per status from the real approval records."""
    return approvals.summary(db, customer.id)


@router.post("/refresh")
async def refresh_from_tracking_table(
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
    delegated_token: str | None = Depends(fabric_access_token),
    lake_token: str | None = Depends(onelake_token),
):
    """
    Reads each Fabric environment's approval tracking Delta table DIRECTLY from
    OneLake (no SQL analytics endpoint) and imports its candidates as PENDING
    approvals. Idempotent. A failed read is reported per environment and creates
    nothing — no fallback data.
    """
    environments = (
        db.query(Environment)
        .filter(Environment.customer_id == customer.id, Environment.platform == "fabric")
        .all()
    )
    outcomes = []
    for environment in environments:
        adapter = environment_service.build_adapter(db, environment, delegated_token)
        adapter.onelake_token = lake_token
        outcome = await approvals.import_tracking(db, environment, adapter)
        if outcome["status"] != "not_configured":
            outcomes.append(outcome)

    if not outcomes:
        raise HTTPException(
            status_code=409,
            detail="No approval tracking table is configured. Set it in Settings → Cluster Settings.",
        )
    return {"sources": outcomes, "summary": approvals.summary(db, customer.id)}


@router.post("/from-run")
def send_to_approval(
    payload: FromRunRequest,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
):
    """
    'Send to Approval' from the Results page. Idempotent: returns the existing
    approval if there is one. Only rows the optimizer flagged are eligible.
    """
    import json

    run = (
        db.query(JobRun)
        .join(AnalysisJob, JobRun.analysis_job_id == AnalysisJob.id)
        .filter(JobRun.id == payload.job_run_id, AnalysisJob.customer_id == customer.id)
        .first()
    )
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status != "COMPLETED" or not run.result_json:
        raise HTTPException(status_code=409, detail="This run has no retrieved results to review.")
    created = approvals.sync_from_run(db, run, json.loads(run.result_json), payload.resource_id)
    if payload.resource_id is not None and not created:
        raise HTTPException(
            status_code=422,
            detail="This cluster does not require approval (the optimizer did not flag it).",
        )
    return [approvals.serialize(a) for a in created]


@router.get("/{approval_id}")
async def get_approval(
    approval_id: str,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
    delegated_token: str | None = Depends(fabric_access_token),
):
    approval = _get_or_404(db, approval_id, customer)
    if approval.status == approvals.EXECUTING:
        # Poll-on-read, like analysis runs: the real validation drives the state.
        await approvals.refresh_execution(db, approval, _adapter_for(db, approval, delegated_token))
    return approvals.serialize(approval, _history(db, approval))


@router.post("/{approval_id}/approve")
def approve(
    approval_id: str,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
    actor: approvals.Actor = Depends(current_actor),
):
    approval = _get_or_404(db, approval_id, customer)
    try:
        approvals.approve(db, approval, actor)
    except approvals.ApprovalError as exc:
        _refused(exc)
    return approvals.serialize(approval, _history(db, approval))


@router.post("/{approval_id}/reject")
def reject(
    approval_id: str,
    payload: RejectRequest,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
    actor: approvals.Actor = Depends(current_actor),
):
    approval = _get_or_404(db, approval_id, customer)
    try:
        approvals.reject(db, approval, actor, payload.reason)
    except approvals.ApprovalError as exc:
        _refused(exc)
    return approvals.serialize(approval, _history(db, approval))


@router.post("/{approval_id}/cancel")
def cancel(
    approval_id: str,
    payload: CancelRequest,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
    actor: approvals.Actor = Depends(current_actor),
):
    approval = _get_or_404(db, approval_id, customer)
    try:
        approvals.cancel(db, approval, actor, payload.reason)
    except approvals.ApprovalError as exc:
        _refused(exc)
    return approvals.serialize(approval, _history(db, approval))


@router.post("/{approval_id}/execute")
async def execute(
    approval_id: str,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
    actor: approvals.Actor = Depends(current_actor),
    delegated_token: str | None = Depends(fabric_access_token),
):
    """Explicit execution of an APPROVED recommendation. Never implied by approval."""
    approval = _get_or_404(db, approval_id, customer)
    try:
        await approvals.execute(db, approval, actor, _adapter_for(db, approval, delegated_token))
    except approvals.ApprovalError as exc:
        _refused(exc)
    return approvals.serialize(approval, _history(db, approval))
