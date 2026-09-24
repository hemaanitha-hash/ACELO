"""
Run history, run details, live events, cancel and re-run.

A "run" is a JobRun (ACELO Run ID = JobRun.id). Everything here reads the
persisted state the backend owns; the browser never keeps a run alive.
Delegated tokens arriving on a request are used for that request (and, on
submission, held in memory by token_vault) — never stored or logged.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from agent.orchestrator import start_agent_job
from api.deps import get_current_customer
from api.platform_context import ActivePlatform, active_context
from api.environments import fabric_access_token
from database import get_db
from models import AnalysisJob, Connection, Customer, Environment, JobLog, JobRun, Notification
from services import execution_worker, job_service, run_events, token_vault

router = APIRouter(prefix="/api/runs", tags=["runs"])
notifications_router = APIRouter(prefix="/api/notifications", tags=["notifications"])

# Lifecycle filter value -> persisted JobRun.status values.
_STATUS_FILTER = {
    "QUEUED": ["QUEUED"],
    "STARTING": ["STARTING", "RETRYING"],
    "SUBMITTED": ["STARTING", "QUEUED", "RETRYING"],
    "RUNNING": ["RUNNING"],
    "SUCCEEDED": ["COMPLETED"],
    "FAILED": ["FAILED"],
    "CANCELLED": ["CANCELLED"],
    "CANCEL_REQUESTED": ["CANCEL_REQUESTED"],
}
_ACTIVE_DB_STATUSES = ["QUEUED", "STARTING", "RETRYING", "RUNNING", "CANCEL_REQUESTED"]


def onelake_token(x_onelake_token: str | None = Header(default=None)) -> str | None:
    """Delegated OneLake (Storage audience) token; per request, never stored."""
    return x_onelake_token


def user_name(x_acelo_user_name: str | None = Header(default=None)) -> str | None:
    return (x_acelo_user_name or "").strip()[:200] or None


# --- shared submission path (used by /api/jobs and re-run) ----------------------

def hand_to_backend(db: Session, job: AnalysisJob, fabric: str | None, onelake: str | None,
                    created_by: str | None, retry_of: str | None = None) -> None:
    """
    After the orchestrator dispatched a job: stamp ownership metadata, give the
    worker the user's tokens for this run (memory only), and start monitoring.
    """
    connection = db.query(Connection).filter(Connection.id == job.connection_id).first()
    environment = (
        db.query(Environment).filter(Environment.connection_id == job.connection_id).first()
        if connection else None
    )
    for run in job.job_runs:
        run.created_by = run.created_by or created_by
        if environment and not run.environment_id:
            run.environment_id = environment.id
        if retry_of:
            run.retry_of_run_id = retry_of
        db.commit()
        if run.platform_run_id and run.status not in job_service.TERMINAL_STATUSES:
            if connection is not None and not connection.secret_encrypted:
                token_vault.store(run.id, fabric, onelake)
            execution_worker.watch_job_run(run.id)


def _run_or_404(db: Session, run_id: str, customer_id: str) -> JobRun:
    run = (
        db.query(JobRun)
        .join(AnalysisJob, AnalysisJob.id == JobRun.analysis_job_id)
        .filter(JobRun.id == run_id, AnalysisJob.customer_id == customer_id)
        .first()
    )
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


def _customer_runs(db: Session, customer_id: str):
    return (
        db.query(JobRun)
        .join(AnalysisJob, AnalysisJob.id == JobRun.analysis_job_id)
        .filter(AnalysisJob.customer_id == customer_id)
    )


def _parse_date(value: str | None, field: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid {field} date: {value}") from None


async def _refresh(db: Session, run: JobRun, fabric: str | None, onelake: str | None) -> None:
    """
    Poll-on-read: the fallback that advances a delegated run when the worker has
    no usable token (expired, or the backend restarted). Also resumes the worker.
    """
    if run.status in job_service.TERMINAL_STATUSES and (run.result_json or run.status != "COMPLETED"):
        return
    job = run.analysis_job
    connection = db.query(Connection).filter(Connection.id == job.connection_id).first() if job else None
    if connection is None or not run.platform_run_id:
        return
    delegated = not connection.secret_encrypted
    if delegated and not fabric:
        return
    if delegated:
        token_vault.store(run.id, fabric, onelake)  # lets the worker continue unattended
    if run.status not in job_service.TERMINAL_STATUSES:
        await job_service.sync_run_status(db, connection, run, fabric)
    if run.status == "COMPLETED" and not run.result_json:
        await job_service.fetch_and_store_result(db, connection, run, fabric, onelake_token=onelake)
    if run.status not in job_service.TERMINAL_STATUSES:
        execution_worker.watch_job_run(run.id)
    else:
        token_vault.discard(run.id)


def _safe_parameters(run: JobRun) -> dict[str, str]:
    sent = job_service.sent_parameters(run) or {}
    return {k: v for k, v in sent.items() if k in run_events.SAFE_PARAMETER_KEYS}


# --- runs ---------------------------------------------------------------------------

@router.get("")
def list_runs(
    status: str | None = None,
    domain: str | None = None,
    platform: str | None = None,
    since: str | None = None,
    until: str | None = None,
    search: str | None = None,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
    context: ActivePlatform = Depends(active_context),
):
    query = _customer_runs(db, customer.id)
    # Run History belongs to the platform the user is in. An explicit ?platform
    # still wins, so a caller can ask for the other one deliberately.
    if not platform and context.platform:
        platform = context.platform
    if status:
        values = _STATUS_FILTER.get(status.upper())
        if values is None:
            raise HTTPException(status_code=422, detail=f"Unknown status filter: {status}")
        query = query.filter(JobRun.status.in_(values))
        if status.upper() == "SUBMITTED":
            query = query.filter(JobRun.platform_run_id.isnot(None))
        elif status.upper() in ("QUEUED", "STARTING"):
            query = query.filter(JobRun.platform_run_id.is_(None))
    if domain:
        query = query.filter(JobRun.domain == domain)
    if platform:
        query = query.filter(JobRun.platform == platform)
    start, end = _parse_date(since, "since"), _parse_date(until, "until")
    if start:
        query = query.filter(JobRun.created_at >= start)
    if end:
        query = query.filter(JobRun.created_at <= end)
    if search and search.strip():
        term = f"%{search.strip()}%"
        query = query.filter(or_(JobRun.id.ilike(term), JobRun.platform_run_id.ilike(term)))
    total = query.count()
    runs = query.order_by(JobRun.created_at.desc()).offset(max(offset, 0)).limit(min(max(limit, 1), 500)).all()
    return {"total": total, "runs": [run_events.run_summary(db, run) for run in runs]}


@router.get("/active")
async def active_runs(
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
    fabric: str | None = Depends(fabric_access_token),
    lake: str | None = Depends(onelake_token),
):
    """Every run still in flight (not one global flag), plus counts for the header."""
    active = (
        _customer_runs(db, customer.id)
        .filter(JobRun.status.in_(_ACTIVE_DB_STATUSES))
        .order_by(JobRun.created_at.desc())
        .all()
    )
    for run in active:
        if fabric and not token_vault.has_usable_token(run.id):
            await _refresh(db, run, fabric, lake)
    still_active = [r for r in active if r.status in _ACTIVE_DB_STATUSES]
    return {
        "count": len(still_active),
        "runs": [run_events.run_summary(db, run) for run in still_active],
    }


@router.get("/{run_id}")
async def run_details(
    run_id: str,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
    fabric: str | None = Depends(fabric_access_token),
    lake: str | None = Depends(onelake_token),
):
    import json

    run = _run_or_404(db, run_id, customer.id)
    await _refresh(db, run, fabric, lake)
    events = (
        db.query(JobLog).filter(JobLog.job_run_id == run.id).order_by(JobLog.timestamp.asc(), JobLog.id.asc()).all()
    )
    result = json.loads(run.result_json) if run.result_json else None
    reruns = db.query(JobRun.id).filter(JobRun.retry_of_run_id == run.id).all()
    return {
        **run_events.run_summary(db, run),
        "parameters": _safe_parameters(run),
        "timeline": [run_events.event_view(e) for e in events if e.event_type],
        "logs": [run_events.event_view(e) for e in events],
        "result": result,
        "reruns": [r.id for r in reruns],
        "can_cancel": run.status in _ACTIVE_DB_STATUSES and run.status != "CANCEL_REQUESTED",
        "can_rerun": run.status in job_service.TERMINAL_STATUSES,
    }


@router.get("/{run_id}/events")
def run_event_stream(
    run_id: str,
    after: str | None = None,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
):
    """Structured polling: events newer than `after` (an ISO timestamp)."""
    run = _run_or_404(db, run_id, customer.id)
    query = db.query(JobLog).filter(JobLog.job_run_id == run.id)
    since = _parse_date(after, "after")
    if since:
        query = query.filter(JobLog.timestamp > since)
    events = query.order_by(JobLog.timestamp.asc(), JobLog.id.asc()).all()
    return {
        "status": run_events.lifecycle_status(run),
        "current_stage": run.current_step,
        "events": [run_events.event_view(e) for e in events],
    }


@router.post("/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
    fabric: str | None = Depends(fabric_access_token),
):
    """
    Asks the platform to cancel. The run becomes CANCEL_REQUESTED and is only
    CANCELLED once the platform reports it — never marked locally while it runs.
    """
    run = _run_or_404(db, run_id, customer.id)
    connection = db.query(Connection).filter(Connection.id == run.analysis_job.connection_id).first()
    if connection is None:
        raise HTTPException(status_code=409, detail="The run's connection no longer exists.")
    await job_service.cancel_job_run(db, connection, run, fabric)
    if run.status == "CANCEL_REQUESTED":
        if not connection.secret_encrypted and fabric:
            token_vault.store(run.id, fabric)
        execution_worker.watch_job_run(run.id)
    return run_events.run_summary(db, run)


@router.post("/{run_id}/rerun")
async def rerun(
    run_id: str,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
    fabric: str | None = Depends(fabric_access_token),
    lake: str | None = Depends(onelake_token),
    created_by: str | None = Depends(user_name),
):
    """Starts the same request again as a NEW run (new ACELO Run ID) linked to the original."""
    original = _run_or_404(db, run_id, customer.id)
    job = original.analysis_job
    if original.platform == "file" or job is None:
        raise HTTPException(status_code=409, detail="Uploaded-file runs cannot be re-run; upload the file again.")
    if original.status not in job_service.TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail="The run is still active; wait for it to finish or cancel it.")
    connection = db.query(Connection).filter(Connection.id == job.connection_id).first()
    if connection is None:
        raise HTTPException(status_code=409, detail="The run's connection no longer exists.")
    new_job = await start_agent_job(db, customer.id, connection, job.request, fabric)
    # One run per domain: link the new run for the same domain back to the original.
    for run in new_job.job_runs:
        if run.domain == original.domain:
            run.retry_of_run_id = original.id
    db.commit()
    hand_to_backend(db, new_job, fabric, lake, created_by or original.created_by)
    new_run = next((r for r in new_job.job_runs if r.domain == original.domain), new_job.job_runs[0])
    return run_events.run_summary(db, new_run)


# --- notifications -------------------------------------------------------------------

def _notification_view(n: Notification) -> dict:
    return {
        "id": n.id,
        "type": n.type,
        "title": n.title,
        "body": n.body,
        "link": n.link,
        "read": bool(n.read),
        "created_at": n.created_at.isoformat() if n.created_at else None,
        "acelo_run_id": n.link.rsplit("/", 1)[-1] if n.link and n.link.startswith("/runs/") else None,
    }


@notifications_router.get("")
def list_notifications(
    unread_only: bool = False,
    limit: int = 50,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
):
    query = db.query(Notification).filter(Notification.customer_id == customer.id)
    unread = query.filter(Notification.read.is_(False)).count()
    if unread_only:
        query = query.filter(Notification.read.is_(False))
    items = query.order_by(Notification.created_at.desc()).limit(min(max(limit, 1), 200)).all()
    return {"unread": unread, "notifications": [_notification_view(n) for n in items]}


@notifications_router.post("/{notification_id}/read")
def mark_read(
    notification_id: str,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
):
    n = (
        db.query(Notification)
        .filter(Notification.id == notification_id, Notification.customer_id == customer.id)
        .first()
    )
    if not n:
        raise HTTPException(status_code=404, detail="Notification not found")
    n.read = True
    db.commit()
    return _notification_view(n)


@notifications_router.post("/read-all")
def mark_all_read(db: Session = Depends(get_db), customer: Customer = Depends(get_current_customer)):
    db.query(Notification).filter(
        Notification.customer_id == customer.id, Notification.read.is_(False)
    ).update({"read": True})
    db.commit()
    return {"ok": True}
