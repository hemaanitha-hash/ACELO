"""
Execution events, run lifecycle view, and completion notifications.

Builds on the existing models instead of competing ones:
  * JobRun      is the run (ACELO Run ID = JobRun.id)
  * JobLog      is the persisted event log (structured columns added)
  * Notification holds the in-app completion/failure notices

Every event is something that really happened — ACELO's own actions or a
status the platform reported. Nothing is estimated; there is no synthetic
percentage progress. Messages and metadata never contain tokens or secrets.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from models import AnalysisJob, Environment, JobLog, JobRun, JobStatus, Notification

logger = logging.getLogger("acelo.runs")

INFO, SUCCESS, WARNING, ERROR = "INFO", "SUCCESS", "WARNING", "ERROR"

# Lifecycle the UI shows, derived deterministically from persisted fields.
QUEUED, STARTING, SUBMITTED, RUNNING = "QUEUED", "STARTING", "SUBMITTED", "RUNNING"
SUCCEEDED, FAILED, CANCELLED, CANCEL_REQUESTED = "SUCCEEDED", "FAILED", "CANCELLED", "CANCEL_REQUESTED"
ACTIVE_STATES = {QUEUED, STARTING, SUBMITTED, RUNNING, CANCEL_REQUESTED}
TERMINAL_STATES = {SUCCEEDED, FAILED, CANCELLED}

DOMAIN_TITLES = {
    "cluster": "Cluster Optimization",
    "query": "Query Optimization",
    "storage": "Storage Optimization",
}

# Keys that may ever appear in event metadata or be shown as run parameters.
SAFE_PARAMETER_KEYS = (
    "acelo_run_id", "environment_id",
    "source_lakehouse", "source_schema", "source_table",
    "result_lakehouse", "result_schema", "result_table",
    "approval_tracking_table", "column_mapping", "model_dir", "llm_model_name",
)
_FORBIDDEN_META = ("token", "secret", "password", "authorization", "bearer", "key_vault", "credential")


def _safe_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not metadata:
        return {}
    return {
        k: v for k, v in metadata.items()
        if not any(word in k.lower() for word in _FORBIDDEN_META)
    }


def emit(
    db: Session,
    run: JobRun,
    event_type: str,
    message: str,
    *,
    level: str = INFO,
    stage: str | None = None,
    metadata: dict[str, Any] | None = None,
    source: str = "acelo",
    once: bool = False,
) -> JobLog | None:
    """
    Persists one execution event. `once=True` skips it when an identical
    event_type+message already exists (e.g. status polled repeatedly).
    """
    if once and db.query(JobLog).filter(
        JobLog.job_run_id == run.id, JobLog.event_type == event_type, JobLog.message == message
    ).first():
        return None
    event = JobLog(
        job_run_id=run.id,
        level=level,
        message=message,
        source=source,
        event_type=event_type,
        stage=stage,
        platform_run_id=run.platform_run_id,
        metadata_json=json.dumps(_safe_metadata(metadata), default=str) if metadata else None,
    )
    db.add(event)
    db.commit()
    return event


def lifecycle_status(run: JobRun) -> str:
    """ACELO lifecycle from persisted state. Never optimistic."""
    status = run.status
    if status == JobStatus.COMPLETED.value:
        return SUCCEEDED
    if status in (JobStatus.FAILED.value, JobStatus.CANCELLED.value, JobStatus.CANCEL_REQUESTED.value,
                  JobStatus.RUNNING.value):
        return status
    if status in (JobStatus.STARTING.value, JobStatus.QUEUED.value, JobStatus.RETRYING.value):
        # Accepted by the platform (a real run id exists) but not yet running there.
        return SUBMITTED if run.platform_run_id else (STARTING if status != JobStatus.QUEUED.value else QUEUED)
    return status


def notify_terminal(db: Session, run: JobRun) -> None:
    """One in-app notification per run reaching SUCCEEDED / FAILED / CANCELLED."""
    state = lifecycle_status(run)
    if state not in TERMINAL_STATES:
        return
    job = run.analysis_job or db.query(AnalysisJob).filter(AnalysisJob.id == run.analysis_job_id).first()
    if job is None:
        return
    link = f"/runs/{run.id}"
    if db.query(Notification).filter(Notification.customer_id == job.customer_id, Notification.link == link).first():
        return
    title = DOMAIN_TITLES.get(run.domain, f"{run.domain.title()} Optimization")
    body = {
        SUCCEEDED: f"Your {title.lower()} run has completed.",
        FAILED: "Open execution details to view the error.",
        CANCELLED: "The run was cancelled.",
    }[state]
    db.add(
        Notification(
            customer_id=job.customer_id,
            type={SUCCEEDED: "run_succeeded", FAILED: "run_failed", CANCELLED: "run_cancelled"}[state],
            title=f"{title} {({SUCCEEDED: 'completed', FAILED: 'failed', CANCELLED: 'cancelled'})[state]}",
            body=body,
            link=link,
        )
    )
    db.commit()


def on_status_change(db: Session, run: JobRun, previous: str | None) -> None:
    """Events + notification for a real status transition."""
    new = lifecycle_status(run)
    if new == SUCCEEDED:
        emit(db, run, "EXECUTION_COMPLETED", "Run completed.", level=SUCCESS, stage="execution", once=True)
    elif new == FAILED:
        emit(db, run, "EXECUTION_FAILED", f"Run failed: {run.error or 'no platform detail'}", level=ERROR,
             stage="execution", metadata={"error_code": run.error_code}, once=True)
    elif new == CANCELLED:
        emit(db, run, "EXECUTION_CANCELLED", "Run cancelled on the platform.", level=WARNING,
             stage="execution", once=True)
    notify_terminal(db, run)


# --- serializers --------------------------------------------------------------

def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _duration(run: JobRun) -> float | None:
    if run.started_at and run.completed_at:
        return round((run.completed_at - run.started_at).total_seconds(), 1)
    return None


def _environment(db: Session, run: JobRun, job: AnalysisJob | None) -> Environment | None:
    if run.environment_id:
        return db.query(Environment).filter(Environment.id == run.environment_id).first()
    if job:
        return db.query(Environment).filter(Environment.connection_id == job.connection_id).first()
    return None


def run_summary(db: Session, run: JobRun) -> dict[str, Any]:
    job = run.analysis_job
    environment = _environment(db, run, job)
    return {
        "acelo_run_id": run.id,
        "job_id": run.analysis_job_id,
        "optimization": DOMAIN_TITLES.get(run.domain, run.domain),
        "domain": run.domain,
        "platform": run.platform,
        "environment_id": environment.id if environment else None,
        "environment_name": environment.name if environment else None,
        "execution_type": run.execution_type,
        "status": lifecycle_status(run),
        "platform_status": run.status,
        "current_stage": run.current_step,
        "started_at": _iso(run.started_at),
        "completed_at": _iso(run.completed_at),
        "created_at": _iso(run.created_at),
        "duration_seconds": _duration(run),
        "platform_run_id": run.platform_run_id,
        "resource_id": run.platform_resource_id,
        "created_by": run.created_by,
        "retry_of_run_id": run.retry_of_run_id,
        "error_code": run.error_code,
        "error_message": run.error,
        "request": job.request if job else None,
    }


def event_view(event: JobLog) -> dict[str, Any]:
    return {
        "id": event.id,
        "acelo_run_id": event.job_run_id,
        "timestamp": _iso(event.timestamp),
        "level": event.level,
        "stage": event.stage,
        "event_type": event.event_type or "LOG",
        "message": event.message,
        "platform": event.source,
        "platform_run_id": event.platform_run_id,
        "metadata": json.loads(event.metadata_json) if event.metadata_json else {},
    }
