from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from agent.orchestrator import UnclearIntent, start_agent_job
from api.deps import get_current_customer
from api.environments import fabric_access_token, fabric_sql_token
from database import get_db
from models import AnalysisJob, Connection, Customer, JobRun
from schemas.job import AnalysisJobOut, JobLogOut, JobResultOut
from services import approval_service, execution_worker, job_service
from api.runs import hand_to_backend, onelake_token, user_name

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


class CreateJobRequest(BaseModel):
    connection_id: str
    prompt: str


def _get_job_or_404(db: Session, job_id: str, customer_id: str) -> AnalysisJob:
    job = db.query(AnalysisJob).filter(AnalysisJob.id == job_id, AnalysisJob.customer_id == customer_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _get_connection_or_404(db: Session, connection_id: str, customer_id: str) -> Connection:
    connection = (
        db.query(Connection).filter(Connection.id == connection_id, Connection.customer_id == customer_id).first()
    )
    if not connection:
        raise HTTPException(status_code=404, detail="Connection not found")
    return connection


@router.post("", response_model=AnalysisJobOut)
async def create_job(
    payload: CreateJobRequest,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
    delegated_token: str | None = Depends(fabric_access_token),
    lake_token: str | None = Depends(onelake_token),
    created_by: str | None = Depends(user_name),
):
    connection = _get_connection_or_404(db, payload.connection_id, customer.id)
    try:
        job = await start_agent_job(db, customer.id, connection, payload.prompt, delegated_token)
    except UnclearIntent as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    # The backend owns the run from here: the worker follows each accepted run
    # to completion (with the user's tokens held in memory for delegated mode),
    # so this request returns immediately and closing the tab changes nothing.
    hand_to_backend(db, job, delegated_token, lake_token, created_by)
    db.refresh(job)
    return job


@router.get("", response_model=list[AnalysisJobOut])
def list_jobs(
    limit: int = 50,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
):
    """
    This customer's analysis jobs, oldest first.

    Read-only and customer-scoped; it does not poll the platform, so listing is
    cheap. The Results page uses it to find the most recent completed run.
    """
    return (
        db.query(AnalysisJob)
        .filter(AnalysisJob.customer_id == customer.id)
        .order_by(AnalysisJob.created_at)
        .limit(limit)
        .all()
    )


@router.get("/{job_id}", response_model=AnalysisJobOut)
async def get_job(
    job_id: str,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
    delegated_token: str | None = Depends(fabric_access_token),
):
    job = _get_job_or_404(db, job_id, customer.id)
    connection = db.query(Connection).filter(Connection.id == job.connection_id).first()

    # Poll-on-read. For delegated ("Microsoft Account") environments this is the
    # ONLY way a run advances, because the background worker has no user token —
    # so the caller's token must reach the adapter here.
    for run in job.job_runs:
        await job_service.sync_run_status(db, connection, run, delegated_token)

    db.refresh(job)
    return job


@router.get("/{job_id}/logs", response_model=list[JobLogOut])
async def get_job_logs(
    job_id: str,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
    delegated_token: str | None = Depends(fabric_access_token),
):
    job = _get_job_or_404(db, job_id, customer.id)
    connection = db.query(Connection).filter(Connection.id == job.connection_id).first()

    all_logs = []
    for run in job.job_runs:
        logs = await job_service.fetch_run_logs(db, connection, run, delegated_token)
        all_logs.extend(logs)
    return sorted(all_logs, key=lambda l: l.timestamp)


@router.get("/{job_id}/results", response_model=list[JobResultOut])
async def get_job_results(
    job_id: str,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
    delegated_token: str | None = Depends(fabric_access_token),
    sql_token: str | None = Depends(fabric_sql_token),
    lake_token: str | None = Depends(onelake_token),
):
    """
    Returns the real, persisted result for each run.

    A run that is still executing reports available=False with its live status —
    it never receives a placeholder result.
    """
    import json

    job = _get_job_or_404(db, job_id, customer.id)
    connection = db.query(Connection).filter(Connection.id == job.connection_id).first()

    results: list[JobResultOut] = []
    for run in job.job_runs:
        # Refresh non-terminal runs so the caller sees real current state.
        if connection and run.status not in job_service.TERMINAL_STATUSES:
            await job_service.sync_run_status(db, connection, run, delegated_token)

        if run.status != "COMPLETED":
            results.append(
                JobResultOut(
                    job_run_id=run.id,
                    domain=run.domain,
                    status=run.status,
                    available=False,
                    payload=None,
                    error=run.error,
                    error_code=run.error_code,
                )
            )
            continue

        payload = json.loads(run.result_json) if run.result_json else None
        if payload is None and connection:
            payload = await job_service.fetch_and_store_result(
                db, connection, run, delegated_token, sql_token, onelake_token=lake_token
            )
        elif payload is not None:
            # Idempotent current-state update (cluster state / query items).
            job_service.route_result(db, run, payload)

        results.append(
            JobResultOut(
                job_run_id=run.id,
                domain=run.domain,
                status=run.status,
                available=payload is not None,
                payload=payload,
                error=run.error,
                error_code=run.error_code,
            )
        )
    return results


@router.post("/{job_id}/cancel", response_model=AnalysisJobOut)
async def cancel_job(
    job_id: str,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
    delegated_token: str | None = Depends(fabric_access_token),
):
    job = _get_job_or_404(db, job_id, customer.id)
    connection = db.query(Connection).filter(Connection.id == job.connection_id).first()

    for run in job.job_runs:
        await job_service.cancel_job_run(db, connection, run, delegated_token)
        # Keep polling: CANCEL_REQUESTED is only promoted to CANCELLED when the
        # platform itself reports the run as cancelled.
        if run.status == "CANCEL_REQUESTED":
            execution_worker.watch_job_run(run.id)

    db.refresh(job)
    return job


@router.post("/{job_id}/retry", response_model=AnalysisJobOut)
async def retry_job(
    job_id: str,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
    delegated_token: str | None = Depends(fabric_access_token),
):
    job = _get_job_or_404(db, job_id, customer.id)
    connection = db.query(Connection).filter(Connection.id == job.connection_id).first()

    if job.platform == "file":
        # The upload is deleted once its run ends, so there is nothing to re-run.
        raise HTTPException(status_code=400, detail="File analyses cannot be retried. Upload the file again.")

    failed_runs = [r for r in job.job_runs if r.status == "FAILED"]
    if not failed_runs:
        raise HTTPException(status_code=400, detail="No failed runs to retry.")

    for run in failed_runs:
        await job_service.retry_job_run(db, connection, run, delegated_token)
        if run.platform_run_id and run.status not in job_service.TERMINAL_STATUSES:
            execution_worker.watch_job_run(run.id)

    db.refresh(job)
    return job
