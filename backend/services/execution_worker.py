"""
Background execution monitor.

POST /api/jobs returns as soon as Fabric has accepted the notebook run. This
module then watches that run to completion out-of-band, so a notebook that takes
twenty minutes never holds an HTTP request open.

Deliberately built on asyncio tasks and the existing job_service rather than
Celery/Redis: the work is I/O-bound polling of a handful of runs, and the
existing poll-on-read path stays as a correctness backstop, so a worker lost to
a process restart degrades to "status updates on next read" rather than to a
stuck run.

Bounded by design:
  * total wall-clock timeout per run
  * exponential backoff between polls, capped
  * transient platform errors retried a bounded number of consecutive times
  * terminal platform errors stop the watch immediately
"""

from __future__ import annotations

import asyncio
import logging
import os

from database import SessionLocal
from models import Connection, Environment, JobRun, JobStatus
from services import job_service, run_events, token_vault

logger = logging.getLogger("acelo.execution.worker")

# Tunable without code changes; defaults suit a long-running Spark notebook.
POLL_TIMEOUT_SECONDS = int(os.getenv("ACELO_POLL_TIMEOUT_SECONDS", "3600"))  # 1 hour
POLL_INITIAL_INTERVAL = float(os.getenv("ACELO_POLL_INITIAL_INTERVAL", "5"))
POLL_MAX_INTERVAL = float(os.getenv("ACELO_POLL_MAX_INTERVAL", "30"))
POLL_BACKOFF_FACTOR = 1.5
MAX_CONSECUTIVE_TRANSIENT_FAILURES = 5

# Lets a test suite (or a one-shot script) opt out of spawning real pollers.
# Production leaves this on; the poll-on-read path is unaffected either way.
BACKGROUND_POLLING_ENABLED = os.getenv("ACELO_BACKGROUND_POLLING", "1") != "0"

# Keeps a strong reference to in-flight tasks; without this, asyncio may garbage
# collect a running task mid-poll.
_ACTIVE: dict[str, asyncio.Task] = {}


def can_poll_unattended(db, job_run: JobRun) -> bool:
    """
    True when this run's connection can authenticate without a user present.

    Service-principal environments store an encrypted secret, so the backend can
    mint its own token and poll in the background. Delegated ("Microsoft
    Account") environments cannot: the Fabric token lives in the signed-in
    browser session and never reaches this process. Polling them here would
    authenticate as nobody and fail a perfectly healthy run — those are driven
    by poll-on-read from the frontend, which does carry the token.
    """
    connection = _connection_for(db, job_run)
    if connection and connection.secret_encrypted:
        return True
    # Delegated: only while the user's own token (given when they started the
    # run) is held in memory for this run and has not expired.
    return token_vault.has_usable_token(job_run.id)


def watch_job_run(job_run_id: str) -> None:
    """Schedules background monitoring for a run. Safe to call more than once."""
    if not BACKGROUND_POLLING_ENABLED:
        return

    # Skip runs this process cannot authenticate for.
    db = SessionLocal()
    try:
        job_run = db.query(JobRun).filter(JobRun.id == job_run_id).first()
        if job_run and not can_poll_unattended(db, job_run):
            logger.info(
                "execution event=worker_skipped run_id=%s reason=delegated_auth "
                "note=status advances via poll-on-read",
                job_run_id,
            )
            return
    finally:
        db.close()

    if job_run_id in _ACTIVE and not _ACTIVE[job_run_id].done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No event loop (e.g. a synchronous script) — the poll-on-read path in
        # api/jobs.py still keeps the run's status current.
        logger.info("execution event=worker_skipped run_id=%s reason=no_event_loop", job_run_id)
        return

    task = loop.create_task(_watch(job_run_id))
    _ACTIVE[job_run_id] = task
    task.add_done_callback(lambda _t: _ACTIVE.pop(job_run_id, None))


async def _watch(job_run_id: str) -> None:
    """Polls one run until it reaches a terminal state, times out, or gives up."""
    logger.info("execution event=polling_started run_id=%s", job_run_id)

    elapsed = 0.0
    interval = POLL_INITIAL_INTERVAL
    consecutive_failures = 0

    while elapsed < POLL_TIMEOUT_SECONDS:
        await asyncio.sleep(interval)
        elapsed += interval

        # A short-lived session per poll: a watch can outlive any request scope,
        # and holding one session open for an hour would pin a connection.
        db = SessionLocal()
        try:
            job_run = db.query(JobRun).filter(JobRun.id == job_run_id).first()
            if not job_run:
                logger.warning("execution event=polling_abandoned run_id=%s reason=run_deleted", job_run_id)
                return

            if job_run.status in job_service.TERMINAL_STATUSES:
                await _finalize(db, job_run)
                return

            connection = _connection_for(db, job_run)
            if not connection:
                logger.warning("execution event=polling_abandoned run_id=%s reason=no_connection", job_run_id)
                return

            token = None
            if not connection.secret_encrypted:
                token = token_vault.fabric_token(job_run_id)
                if token is None:
                    # The user's session token expired. The run is NOT failed: the
                    # platform keeps running and ACELO resumes when the app is open.
                    run_events.emit(
                        db, job_run, "BACKGROUND_MONITORING_PAUSED",
                        "Background monitoring paused: the Microsoft session used to start this run "
                        "expired. Status resumes automatically while ACELO is open.",
                        level=run_events.WARNING, stage="execution", once=True,
                    )
                    return

            before = job_run.status
            await job_service.sync_run_status(db, connection, job_run, token)

            if job_run.status in job_service.TERMINAL_STATUSES:
                await _finalize(db, job_run)
                return

            # sync_run_status leaves status untouched on a transient failure.
            # Repeated no-progress polls are how we detect a persistently
            # unreachable platform without retrying forever.
            if job_run.status == before:
                consecutive_failures += 1
            else:
                consecutive_failures = 0
                interval = POLL_INITIAL_INTERVAL  # state moved; poll promptly again

            if consecutive_failures >= MAX_CONSECUTIVE_TRANSIENT_FAILURES and _last_log_was_warning(job_run):
                logger.warning(
                    "execution event=polling_gave_up run_id=%s consecutive_failures=%s",
                    job_run_id, consecutive_failures,
                )
                job_service.append_log(
                    db, job_run, "WARNING",
                    "Stopped background monitoring after repeated platform errors. "
                    "The run may still be executing; reload to poll again.",
                )
                return

            interval = min(interval * POLL_BACKOFF_FACTOR, POLL_MAX_INTERVAL)
        except Exception:  # noqa: BLE001 - a worker crash must not take down the app
            logger.exception("execution event=polling_error run_id=%s", job_run_id)
            return
        finally:
            db.close()

    # Timed out. The platform job may well still be running, so the run is NOT
    # marked failed — that would be asserting a state we did not observe.
    db = SessionLocal()
    try:
        job_run = db.query(JobRun).filter(JobRun.id == job_run_id).first()
        if job_run and job_run.status not in job_service.TERMINAL_STATUSES:
            logger.warning("execution event=polling_timeout run_id=%s elapsed=%s", job_run_id, elapsed)
            job_service.append_log(
                db, job_run, "WARNING",
                f"Stopped monitoring after {POLL_TIMEOUT_SECONDS}s. The platform run may still be "
                "in progress — its status will refresh the next time this job is opened.",
            )
    finally:
        db.close()


def _last_log_was_warning(job_run: JobRun) -> bool:
    """Only give up when the stalls were actually platform errors, not just a
    genuinely long-running notebook sitting in the same status."""
    if not job_run.logs:
        return False
    latest = sorted(job_run.logs, key=lambda l: l.timestamp)[-1]
    return latest.level in ("WARNING", "ERROR")


async def _finalize(db, job_run: JobRun) -> None:
    """On a terminal state, retrieve and store the real result."""
    logger.info(
        "execution event=execution_completed run_id=%s status=%s platform_run_id=%s",
        job_run.id, job_run.status, job_run.platform_run_id,
    )
    try:
        if job_run.status != JobStatus.COMPLETED.value:
            return
        connection = _connection_for(db, job_run)
        if not connection:
            return
        token = None if connection.secret_encrypted else token_vault.fabric_token(job_run.id)
        onelake = token_vault.onelake_token(job_run.id)
        # Results from OneLake (no SQL endpoint), then the pipeline's approval
        # tracking table — both through the same adapter, both best effort.
        await job_service.fetch_and_store_result(db, connection, job_run, token, onelake_token=onelake)
        await _import_tracking(db, connection, job_run, token, onelake)
    finally:
        token_vault.discard(job_run.id)


async def _import_tracking(db, connection, job_run: JobRun, token, onelake) -> None:
    from agent.router import build_adapter
    from services import approval_service

    if job_run.execution_type != "pipeline":
        return
    environment = db.query(Environment).filter(Environment.connection_id == connection.id).first()
    if environment is None or approval_service.tracking_config(db, environment) is None:
        return
    adapter = build_adapter(connection, db, token)
    adapter.onelake_token = onelake
    outcome = await approval_service.import_tracking(db, environment, adapter)
    if outcome["status"] == "ok":
        run_events.emit(
            db, job_run, "APPROVALS_IMPORTED",
            f"Approval tracking read from OneLake: {outcome.get('created', 0)} new approval(s).",
            level=run_events.SUCCESS, stage="approval_tracking",
            metadata={k: outcome.get(k) for k in ("table", "rows_read", "candidates", "created")},
        )
    else:
        run_events.emit(
            db, job_run, "APPROVALS_IMPORT_FAILED",
            f"Approval tracking could not be read: {outcome.get('message')}",
            level=run_events.WARNING, stage="approval_tracking",
            metadata={"error_code": outcome.get("error_code"), "table": outcome.get("table")},
        )


def _connection_for(db, job_run: JobRun) -> Connection | None:
    analysis_job = job_run.analysis_job
    if not analysis_job:
        return None
    return db.query(Connection).filter(Connection.id == analysis_job.connection_id).first()
