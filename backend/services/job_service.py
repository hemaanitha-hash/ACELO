"""
Run lifecycle: start -> poll -> terminal -> retrieve result.

Step 2 rules enforced here:
  * A JobRun only ever holds a platform_run_id the platform actually issued.
  * A status is only written when the platform reported it. Nothing is assumed.
  * Transient platform failures leave the run alone to be polled again; terminal
    failures fail the run with a typed, user-safe error code.
  * Cancel marks CANCEL_REQUESTED; only the platform reporting Cancelled
    promotes it to CANCELLED.
"""

import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from agent.router import build_adapter
from models import AnalysisJob, Connection, JobLog, JobRun, JobStatus
from platforms.base import PlatformCapabilityNotImplemented
from platforms.errors import ErrorCode, PlatformError, is_transient
from platforms.fabric import SENSITIVE_PARAMETERS, redact_parameters, trace
from services import run_events
from services.run_events import emit

# JobLog prefix under which the exact submitted notebook parameters are kept.
SENT_PARAMETERS_PREFIX = "[FABRIC_EXECUTION_REQUEST] parameters="

# Notebook parameters ACELO supplies per run. Everything customer-specific
# comes from the stored Connection/Environment config — nothing is hardcoded.
_PARAMETER_KEYS = (
    "source_table",
    "result_table",
    "source_lakehouse",
    "result_lakehouse",
    "source_schema",
    "result_schema",
    "column_mapping",
    "approval_tracking_table",
    "model_dir",
    "llm_key_vault_uri",
    "llm_secret_name",
    "llm_model_name",
    "validation_batch_size",
)

logger = logging.getLogger("acelo.execution")

TERMINAL_STATUSES = {s.value for s in JobStatus.terminal()}


def _log(job_run: JobRun, event: str, **fields) -> None:
    """Structured execution logging. IDs and codes only — never credentials."""
    extra = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    logger.info(
        "execution event=%s run_id=%s domain=%s platform=%s platform_run_id=%s status=%s %s",
        event,
        job_run.id,
        job_run.domain,
        job_run.platform,
        job_run.platform_run_id or "-",
        job_run.status,
        extra,
    )


def create_analysis_job(db: Session, customer_id: str, connection: Connection, request: str, intent: str) -> AnalysisJob:
    job = AnalysisJob(
        customer_id=customer_id,
        connection_id=connection.id,
        request=request,
        intent=intent,
        platform=connection.platform,
        status=JobStatus.QUEUED.value,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def create_job_run(db: Session, analysis_job: AnalysisJob, domain: str) -> JobRun:
    run = JobRun(
        analysis_job_id=analysis_job.id,
        domain=domain,
        platform=analysis_job.platform,
        status=JobStatus.QUEUED.value,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    emit(db, run, "RUN_CREATED", "ACELO run created.", stage="submission",
         metadata={"domain": domain, "platform": run.platform})
    return run


def append_log(db: Session, job_run: JobRun, level: str, message: str, source: str = "acelo") -> JobLog:
    log = JobLog(job_run_id=job_run.id, level=level, message=message, source=source)
    db.add(log)
    db.commit()
    return log


def build_run_parameters(
    connection: Connection, job_run: JobRun, db: Session | None = None
) -> dict[str, str]:
    """
    The parameters the selected notebook/pipeline receives for this run, built
    by the Environment Resource Registry from THIS run's domain only - a Cluster
    setting can never reach the Query notebook, or the reverse.
    """
    from services import resource_registry

    environment = None
    if db is not None:
        from models import Environment

        environment = db.query(Environment).filter(Environment.connection_id == connection.id).first()
    metadata = json.loads(connection.auth_metadata) if connection.auth_metadata else {}
    return resource_registry.build_runtime_parameters(
        db, environment, job_run.domain, job_run.id, legacy_metadata=metadata
    )


# Parameters each domain's notebook cannot run without. Checking these before
# dispatch turns a silent Fabric "missing or invalid information" 400 into an
# actionable configuration error naming the exact missing setting.
# Settings an environment must configure. Readiness checks exactly these.
_REQUIRED_CONFIGURATION = {
    "cluster": ("source_table", "result_table"),
}

# Everything the notebook's own validation requires. acelo_run_id is supplied
# per run rather than configured, so it belongs here but not above.
_REQUIRED_PARAMETERS = {
    "cluster": ("source_table", "result_table", "acelo_run_id"),
}

# The notebook's own validation rejects a blank value for any of these, so a
# specific code lets the UI name the one setting that needs attention.
_PARAMETER_ERROR_CODES = {
    ("cluster", "source_table"): ErrorCode.CLUSTER_SOURCE_TABLE_NOT_CONFIGURED,
    ("cluster", "result_table"): ErrorCode.CLUSTER_RESULT_TABLE_NOT_CONFIGURED,
}


def _missing(keys, parameters: dict[str, str]) -> list[str]:
    return [key for key in keys if not str(parameters.get(key, "")).strip()]


def missing_required_parameters(domain: str, parameters: dict[str, str]) -> list[str]:
    """Everything the notebook needs at run time, including acelo_run_id."""
    return _missing(_REQUIRED_PARAMETERS.get(domain, ()), parameters)


def missing_required_configuration(domain: str, parameters: dict[str, str]) -> list[str]:
    """Only the settings a customer configures — what readiness reports on."""
    return _missing(_REQUIRED_CONFIGURATION.get(domain, ()), parameters)


def parameter_error_code(domain: str, parameter: str) -> str:
    """Typed code for a specific missing parameter, falling back to the generic."""
    return _PARAMETER_ERROR_CODES.get((domain, parameter), ErrorCode.INVALID_CONFIGURATION)


def _fail_run(db: Session, job_run: JobRun, message: str, code: str, source: str) -> JobRun:
    job_run.status = JobStatus.FAILED.value
    job_run.error = message
    job_run.error_code = code
    job_run.completed_at = datetime.utcnow()
    db.commit()
    append_log(db, job_run, "ERROR", message, source=source)
    _log(job_run, "execution_failed", error_code=code)
    _recompute_analysis_job_status(db, job_run.analysis_job_id)
    run_events.on_status_change(db, job_run, None)
    return job_run


# Parameter names that may carry credential material. Their names are logged so
# a missing configuration is still diagnosable; their values never are.
_SENSITIVE_PARAMETERS = frozenset(
    {"llm_key_vault_uri", "llm_secret_name", "llm_api_key"}
)


def _log_notebook_parameters(job_run: JobRun, parameters: dict[str, str]) -> None:
    """
    Records what the notebook will receive, so a parameter-validation failure
    inside Fabric can be diagnosed without a rerun.

    Values are included only for plainly non-secret settings (table and
    lakehouse names, ids). Anything that points at a credential is logged by
    NAME ONLY. Tokens and secrets never pass through this function at all --
    they are not notebook parameters.
    """
    rendered = " ".join(
        f"{name}=<set>" if name in _SENSITIVE_PARAMETERS else f"{name}={value}"
        for name, value in sorted(parameters.items())
    )
    logger.info(
        "fabric_notebook_parameters run_id=%s domain=%s %s",
        job_run.id,
        job_run.domain,
        rendered,
    )


async def start_job_run(
    db: Session,
    connection: Connection,
    job_run: JobRun,
    delegated_token: str | None = None,
) -> JobRun:
    """
    Dispatches to the platform adapter and returns as soon as the platform has
    accepted the job. Returns without a platform_run_id only by failing the run.
    """
    adapter = build_adapter(connection, db, delegated_token)
    job_run.status = JobStatus.STARTING.value
    job_run.started_at = datetime.utcnow()
    db.commit()
    append_log(db, job_run, "INFO", f"Starting {job_run.domain} analysis on {connection.platform}.")
    emit(db, job_run, "EXECUTION_STARTED", f"Starting {job_run.domain} analysis on {connection.platform}.",
         stage="submission")
    _log(job_run, "platform_execution_requested")

    parameters = build_run_parameters(connection, job_run, db)
    missing = missing_required_parameters(job_run.domain, parameters)
    if missing:
        return _fail_run(
            db,
            job_run,
            f"This environment is missing required {job_run.domain} settings: "
            f"{', '.join(missing)}. Set them in Environment Setup before running.",
            parameter_error_code(job_run.domain, missing[0]),
            connection.platform,
        )

    _log_notebook_parameters(job_run, parameters)
    emit(
        db, job_run, "PLATFORM_EXECUTION_REQUESTED",
        f"Submitting to {connection.platform}.", stage="submission",
        metadata={k: v for k, v in parameters.items() if k in run_events.SAFE_PARAMETER_KEYS},
    )

    try:
        result = await adapter.start_analysis(job_run.domain, parameters)
    except PlatformCapabilityNotImplemented as exc:
        return _fail_run(db, job_run, str(exc), ErrorCode.UNSUPPORTED, connection.platform)
    except PlatformError as exc:
        logger.warning(
            "execution event=start_failed run_id=%s error_code=%s detail=%s",
            job_run.id, exc.code, exc.log_detail,
        )
        return _fail_run(db, job_run, exc.message, exc.code, connection.platform)
    except Exception as exc:  # noqa: BLE001 - any adapter/API failure becomes a first-class FAILED state
        return _fail_run(db, job_run, f"Failed to start: {exc}", ErrorCode.EXECUTION_FAILED, connection.platform)

    job_run.platform_run_id = result.platform_run_id
    job_run.status = result.status
    if result.detail:
        job_run.platform_resource_id = result.detail.get("item_id") or result.detail.get("job_id")
    if connection.platform == "fabric":
        job_run.execution_type = (result.detail or {}).get("execution_type", "notebook")
    db.commit()
    # Persist exactly what the platform was sent, so the run's own history can
    # later be compared with what the notebook reports it received.
    # Same rule the adapter applies when building the request: blank values are
    # never sent to the notebook.
    sent = redact_parameters(
        {k: v for k, v in parameters.items() if v is not None and str(v) != ""}
    )
    if sent:
        append_log(
            db, job_run, "INFO",
            f"{SENT_PARAMETERS_PREFIX}{json.dumps(sent, sort_keys=True)}",
            source="acelo",
        )
    append_log(
        db,
        job_run,
        "INFO",
        f"{connection.platform} accepted the job — platform run id {result.platform_run_id}.",
        source=connection.platform,
    )
    _log(job_run, "platform_run_id_received", resource_id=job_run.platform_resource_id)
    kind = "Pipeline" if job_run.execution_type == "pipeline" else "Notebook"
    job_run.current_step = f"{kind} submitted"
    db.commit()
    emit(
        db, job_run, "PLATFORM_RUN_ID_RECEIVED",
        f"{kind} submitted — {connection.platform} run ID {result.platform_run_id}.",
        level=run_events.SUCCESS, stage="submission", source=connection.platform,
        metadata={"execution_type": job_run.execution_type, "resource_id": job_run.platform_resource_id},
    )
    _recompute_analysis_job_status(db, job_run.analysis_job_id)
    return job_run


async def sync_run_status(
    db: Session,
    connection: Connection,
    job_run: JobRun,
    delegated_token: str | None = None,
) -> JobRun:
    """
    Polls the platform for this run's real status.

    Transient failures are logged and the run is left untouched so the next poll
    can retry. Terminal failures fail the run.
    """
    if job_run.status in TERMINAL_STATUSES or not job_run.platform_run_id:
        return job_run

    adapter = build_adapter(connection, db, delegated_token)
    if getattr(adapter, "delegated_mode", False) and not delegated_token:
        # No user session on this request. That says nothing about the Fabric
        # run, so its status is left exactly as last observed - never failed.
        trace("FABRIC_AUTH", auth_mode="delegated", token_present=False, action="status_poll_skipped")
        return job_run
    try:
        status_result = await adapter.get_run_status(job_run.platform_run_id, job_run.domain)
    except PlatformCapabilityNotImplemented as exc:
        return _fail_run(db, job_run, str(exc), ErrorCode.UNSUPPORTED, connection.platform)
    except PlatformError as exc:
        if is_transient(exc.code):
            append_log(db, job_run, "WARNING", f"Status poll failed, will retry: {exc.message}", source=connection.platform)
            logger.warning(
                "execution event=poll_transient_failure run_id=%s error_code=%s detail=%s",
                job_run.id, exc.code, exc.log_detail,
            )
            return job_run
        return _fail_run(db, job_run, exc.message, exc.code, connection.platform)
    except Exception as exc:  # noqa: BLE001 - unknown failure: retry rather than falsely failing a live run
        append_log(db, job_run, "WARNING", f"Status poll failed: {exc}", source=connection.platform)
        return job_run

    previous_status = job_run.status
    # A cancel we already requested stays CANCEL_REQUESTED until the platform
    # actually reports a terminal state — we never self-promote it to CANCELLED.
    if previous_status == JobStatus.CANCEL_REQUESTED.value and status_result.status not in TERMINAL_STATUSES:
        return job_run

    job_run.status = status_result.status
    if status_result.current_step:
        job_run.current_step = status_result.current_step
    job_run.progress = status_result.progress
    job_run.error = status_result.error
    if status_result.status == JobStatus.FAILED.value and status_result.error:
        job_run.error_code = ErrorCode.EXECUTION_FAILED
    if status_result.status in TERMINAL_STATUSES:
        job_run.completed_at = datetime.utcnow()
    db.commit()

    if previous_status != job_run.status:
        trace(
            f"{job_run.platform.upper()}_RUN_STATUS",
            acelo_run_id=job_run.id,
            platform_run_id=job_run.platform_run_id,
            status=(status_result.detail or {}).get("fabric_status") or job_run.status,
            acelo_status=job_run.status,
            error_code=job_run.error_code or "-",
            error_message=job_run.error or "-",
        )
        fabric_status = (status_result.detail or {}).get("fabric_status") or job_run.status
        emit(
            db, job_run, "PLATFORM_STATUS_CHANGED",
            f"{connection.platform.title()} status: {fabric_status}.",
            level=run_events.ERROR if job_run.status == JobStatus.FAILED.value else run_events.INFO,
            stage="execution", source=connection.platform,
            metadata={"previous": previous_status, "status": job_run.status, "platform_status": fabric_status},
        )
        _log(job_run, "polling_status", previous=previous_status)
        _recompute_analysis_job_status(db, job_run.analysis_job_id)

    await _emit_activity_events(db, adapter, job_run)

    if previous_status != job_run.status:
        run_events.on_status_change(db, job_run, previous_status)

    return job_run


# Pipeline activity -> (stage, event on start, event on success, human label).
_ACTIVITY_EVENTS = {
    "Run ACELO Cluster Notebook": ("notebook", "NOTEBOOK_STARTED", "NOTEBOOK_COMPLETED", "Cluster notebook"),
    "Run ACELO Query Notebook": ("notebook", "NOTEBOOK_STARTED", "NOTEBOOK_COMPLETED", "Query notebook"),
    "Update ACELO Approval Tracking": (
        "approval_tracking", "APPROVAL_TRACKING_STARTED", "APPROVAL_TRACKING_COMPLETED", "Approval tracking",
    ),
}


async def _emit_activity_events(db: Session, adapter, job_run: JobRun) -> None:
    """
    Real per-activity progress for pipeline runs, from Fabric's activity-run
    query. Best effort: if the platform does not expose it, nothing is invented.
    """
    if job_run.execution_type != "pipeline" or not hasattr(adapter, "query_activity_runs"):
        return
    try:
        activities = await adapter.query_activity_runs(job_run.platform_run_id)
    except Exception as exc:  # noqa: BLE001 - optional detail; status polling already covers the run
        logger.info("execution event=activity_query_unavailable run_id=%s error=%s", job_run.id, type(exc).__name__)
        return
    for activity in activities:
        name = activity.get("activity_name") or ""
        status = (activity.get("status") or "").lower()
        stage, started, completed, label = _ACTIVITY_EVENTS.get(
            name, ("execution", "ACTIVITY_STARTED", "ACTIVITY_COMPLETED", name or "Activity")
        )
        meta = {"activity": name, "activity_run_id": activity.get("activity_run_id")}
        if status in ("inprogress", "queued", "succeeded", "failed", "cancelled"):
            emit(db, job_run, started, f"{label} started.", stage=stage, source=job_run.platform,
                 metadata=meta, once=True)
        if status == "inprogress":
            job_run.current_step = f"{label} running"
            db.commit()
        elif status == "succeeded":
            emit(db, job_run, completed, f"{label} completed.", level=run_events.SUCCESS, stage=stage,
                 source=job_run.platform, metadata=meta, once=True)
        elif status in ("failed", "cancelled"):
            detail = activity.get("error") or status
            failed_type = started[: -len("_STARTED")] + "_FAILED"
            emit(db, job_run, failed_type, f"{label} {status}: {detail}",
                 level=run_events.ERROR, stage=stage, source=job_run.platform, metadata=meta, once=True)
            job_run.current_step = f"{label} {status}"
            db.commit()


async def fetch_and_store_result(
    db: Session,
    connection: Connection,
    job_run: JobRun,
    delegated_token: str | None = None,
    sql_token: str | None = None,
    onelake_token: str | None = None,
) -> dict | None:
    """
    Retrieves the real result for a COMPLETED run and persists the normalized
    payload. Never invents a result: a retrieval failure is recorded as such and
    the run keeps its COMPLETED platform status with a result error attached.
    """
    if job_run.status != JobStatus.COMPLETED.value or not job_run.platform_run_id:
        return None
    if job_run.result_json:
        return json.loads(job_run.result_json)

    adapter = build_adapter(connection, db, delegated_token)
    if sql_token:
        # Delegated read of the Lakehouse SQL endpoint; used for this call only.
        adapter.sql_token = sql_token
    if onelake_token:
        # Delegated OneLake read of the result Delta table; used for this call only.
        adapter.onelake_token = onelake_token
    _log(job_run, "result_retrieval_started")

    try:
        result = await adapter.get_run_result(
            job_run.platform_run_id, job_run.domain, acelo_run_id=job_run.id
        )
    except PlatformCapabilityNotImplemented as exc:
        job_run.error_code = ErrorCode.UNSUPPORTED
        job_run.error = str(exc)
        db.commit()
        append_log(db, job_run, "WARNING", str(exc), source=connection.platform)
        return None
    except PlatformError as exc:
        job_run.error_code = exc.code
        job_run.error = exc.message
        db.commit()
        append_log(db, job_run, "ERROR", exc.message, source=connection.platform)
        emit(db, job_run, "RESULT_RETRIEVAL_FAILED", exc.message, level=run_events.WARNING,
             stage="results", metadata={"error_code": exc.code}, once=True)
        logger.warning(
            "execution event=result_retrieval_failed run_id=%s error_code=%s detail=%s",
            job_run.id, exc.code, exc.log_detail,
        )
        return None
    except Exception as exc:  # noqa: BLE001
        job_run.error_code = ErrorCode.RESULT_RETRIEVAL_FAILED
        job_run.error = "The run finished, but its results could not be retrieved."
        db.commit()
        append_log(db, job_run, "ERROR", f"Result retrieval failed: {exc}", source=connection.platform)
        return None

    normalized = normalize_result(job_run, connection, result)
    normalized["parameter_verification"] = verify_notebook_parameters(job_run, result.payload or {})
    job_run.result_reference = result.result_reference
    job_run.result_json = json.dumps(normalized, default=str)
    job_run.error_code = None
    db.commit()
    _log(job_run, "result_retrieval_completed", rows=normalized.get("row_count"))
    emit(db, job_run, "RESULTS_RETRIEVED",
         f"Results retrieved: {normalized.get('row_count')} rows from {result.result_reference}.",
         level=run_events.SUCCESS, stage="results",
         metadata={"row_count": normalized.get("row_count"), "result_reference": result.result_reference},
         once=True)

    route_result(db, job_run, normalized)
    return normalized


def route_result(db: Session, job_run: JobRun, normalized: dict | None) -> None:
    """
    Where a completed run's result goes (idempotent, never fatal):
      * cluster -> current cluster optimization state (reporting only, NO approval)
      * query   -> query approval items from the Validation tracking table
    """
    from services import approval_service, cluster_state

    if job_run.domain == "cluster":
        cluster_state.safe_upsert(db, job_run, normalized)
    elif job_run.domain == "query":
        outcome = approval_service.safe_sync_query(db, job_run, normalized)
        already = db.query(JobLog).filter(
            JobLog.job_run_id == job_run.id, JobLog.event_type == "APPROVALS_IMPORTED"
        ).first()
        if outcome is not None and already is None:  # one import event per run, even on replay
            emit(db, job_run, "APPROVALS_IMPORTED",
                 f"Query optimizations for review: {outcome['unique_business_keys']} — "
                 f"{outcome['created']} new, {outcome['updated']} updated, {outcome['unchanged']} unchanged.",
                 level=run_events.SUCCESS, stage="approval_tracking", metadata=outcome, once=True)


def normalize_result(job_run: JobRun, connection: Connection, result) -> dict:
    """
    Minimal normalization so ACELO can consume the existing notebook output
    without reshaping it. The platform's own payload is preserved verbatim under
    `source_payload` — no source field is discarded, and nothing is synthesized.
    """
    payload = result.payload or {}
    rows = payload.get("rows")
    return {
        "platform": job_run.platform,
        "environment_connection_id": connection.id,
        "optimization_type": job_run.domain,
        "run_id": job_run.id,
        "platform_run_id": job_run.platform_run_id,
        "platform_resource_id": job_run.platform_resource_id,
        "result_reference": result.result_reference,
        "retrieved_at": datetime.utcnow().isoformat(),
        "row_count": payload.get("row_count", len(rows) if isinstance(rows, list) else None),
        # The real notebook output, untouched.
        "source_payload": payload,
    }


def sent_parameters(job_run: JobRun) -> dict[str, str] | None:
    """The parameters recorded at submission for this run, if any."""
    for log in sorted(job_run.logs, key=lambda l: l.timestamp):
        if log.message.startswith(SENT_PARAMETERS_PREFIX):
            try:
                return json.loads(log.message[len(SENT_PARAMETERS_PREFIX):])
            except ValueError:
                return None
    return None


def verify_notebook_parameters(job_run: JobRun, payload: dict) -> dict:
    """
    ACELO SENT vs NOTEBOOK RECEIVED, from real evidence only.

    The notebook stamps every result row with `acelo_received_parameters` (the
    non-secret parameters it actually saw at runtime). Rows are fetched filtered
    by acelo_run_id, so their existence already proves the correlation id made
    the round trip. Anything that cannot be verified is reported as such —
    never assumed to match.
    """
    sent = sent_parameters(job_run)
    rows = payload.get("rows") or []
    outcome: dict = {"status": "UNVERIFIED", "sent": sent, "received": None, "mismatches": []}

    if not rows:
        outcome["reason"] = "No result rows were returned for this run."
    elif sent is None:
        outcome["reason"] = "No record of the submitted parameters for this run."
    else:
        raw = rows[0].get("acelo_received_parameters")
        try:
            received = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            received = None
        if not isinstance(received, dict):
            outcome["reason"] = (
                "The result rows do not carry acelo_received_parameters. The deployed "
                "notebook predates runtime-parameter tracing; re-run 'Set up ACELO in Fabric'."
            )
            # The correlation id is still verifiable from the row stamp itself.
            outcome["correlation_id_matched"] = rows[0].get("acelo_run_id") == job_run.id
        else:
            outcome["received"] = received
            for name, value in sent.items():
                if name in SENSITIVE_PARAMETERS:
                    continue
                if str(received.get(name, "")) != str(value):
                    outcome["mismatches"].append(
                        {"parameter": name, "sent": value, "received": received.get(name)}
                    )
            outcome["status"] = "MISMATCH" if outcome["mismatches"] else "MATCHED"
            outcome["correlation_id_matched"] = received.get("acelo_run_id") == job_run.id

    trace(
        "NOTEBOOK_PARAMETER_MATCH",
        acelo_run_id=job_run.id,
        platform_run_id=job_run.platform_run_id,
        status=outcome["status"],
        mismatches=outcome["mismatches"],
        reason=outcome.get("reason", "-"),
    )
    return outcome


async def fetch_run_logs(
    db: Session,
    connection: Connection,
    job_run: JobRun,
    delegated_token: str | None = None,
) -> list[JobLog]:
    """Pulls fresh logs from the platform and persists any not already stored, then returns the full history."""
    if job_run.platform_run_id:
        adapter = build_adapter(connection, db, delegated_token)
        try:
            remote_entries = await adapter.get_run_logs(job_run.platform_run_id, job_run.domain)
            existing_messages = {log.message for log in job_run.logs}
            for entry in remote_entries:
                if entry.message not in existing_messages:
                    append_log(db, job_run, entry.level, entry.message, source=entry.source)
        except PlatformCapabilityNotImplemented as exc:
            append_log(db, job_run, "WARNING", str(exc), source=connection.platform)
        except PlatformError as exc:
            append_log(db, job_run, "WARNING", f"Could not fetch remote logs: {exc.message}", source=connection.platform)
        except Exception as exc:  # noqa: BLE001
            append_log(db, job_run, "WARNING", f"Could not fetch remote logs: {exc}", source=connection.platform)

    db.refresh(job_run)
    return sorted(job_run.logs, key=lambda l: l.timestamp)


async def cancel_job_run(
    db: Session,
    connection: Connection,
    job_run: JobRun,
    delegated_token: str | None = None,
) -> JobRun:
    """
    Asks the platform to cancel. The run becomes CANCEL_REQUESTED, not
    CANCELLED — only a subsequent poll seeing the platform report Cancelled
    promotes it. A refused cancel leaves the run running and says so.
    """
    if job_run.status in TERMINAL_STATUSES:
        return job_run

    if not job_run.platform_run_id:
        # Nothing was ever started on the platform, so there is nothing to
        # confirm — cancelling locally is accurate here.
        job_run.status = JobStatus.CANCELLED.value
        job_run.completed_at = datetime.utcnow()
        db.commit()
        run_events.on_status_change(db, job_run, None)
        _log(job_run, "cancellation_completed", note="no platform run existed")
        return job_run

    adapter = build_adapter(connection, db, delegated_token)
    _log(job_run, "cancellation_requested")

    try:
        result = await adapter.cancel_run(job_run.platform_run_id, job_run.domain)
    except PlatformCapabilityNotImplemented as exc:
        append_log(db, job_run, "WARNING", str(exc), source=connection.platform)
        job_run.error_code = ErrorCode.UNSUPPORTED
        job_run.error = str(exc)
        db.commit()
        return job_run
    except PlatformError as exc:
        append_log(db, job_run, "ERROR", exc.message, source=connection.platform)
        job_run.error_code = exc.code
        job_run.error = exc.message
        db.commit()
        return job_run

    append_log(db, job_run, "INFO" if result.ok else "ERROR", result.message, source=connection.platform)
    if result.ok:
        job_run.status = JobStatus.CANCEL_REQUESTED.value
        db.commit()
        _log(job_run, "cancellation_acknowledged")
        emit(db, job_run, "CANCEL_REQUESTED",
             f"Cancellation requested on {connection.platform}; waiting for the platform to confirm.",
             level=run_events.WARNING, stage="execution", source=connection.platform)
    else:
        job_run.error_code = ErrorCode.UNSUPPORTED
        job_run.error = result.message
        db.commit()

    _recompute_analysis_job_status(db, job_run.analysis_job_id)
    return job_run


async def retry_job_run(
    db: Session,
    connection: Connection,
    job_run: JobRun,
    delegated_token: str | None = None,
) -> JobRun:
    if job_run.status != JobStatus.FAILED.value:
        raise ValueError(f"Only FAILED runs can be retried (current status: {job_run.status}).")

    adapter = build_adapter(connection, db, delegated_token)
    job_run.status = JobStatus.RETRYING.value
    job_run.error = None
    job_run.error_code = None
    db.commit()
    append_log(db, job_run, "INFO", "Retrying job run.", source=connection.platform)

    try:
        result = await adapter.retry_run(job_run.domain, job_run.platform_run_id or "")
    except PlatformCapabilityNotImplemented as exc:
        return _fail_run(db, job_run, str(exc), ErrorCode.UNSUPPORTED, connection.platform)
    except PlatformError as exc:
        return _fail_run(db, job_run, exc.message, exc.code, connection.platform)
    except Exception as exc:  # noqa: BLE001
        return _fail_run(db, job_run, f"Retry failed: {exc}", ErrorCode.EXECUTION_FAILED, connection.platform)

    job_run.platform_run_id = result.platform_run_id
    job_run.status = result.status
    job_run.completed_at = None
    db.commit()
    _recompute_analysis_job_status(db, job_run.analysis_job_id)
    return job_run


def _recompute_analysis_job_status(db: Session, analysis_job_id: str) -> None:
    job = db.query(AnalysisJob).filter(AnalysisJob.id == analysis_job_id).first()
    if not job:
        return
    statuses = [run.status for run in job.job_runs]
    if not statuses:
        return

    if all(s in TERMINAL_STATUSES for s in statuses):
        job.status = (
            JobStatus.FAILED.value
            if any(s == JobStatus.FAILED.value for s in statuses)
            else JobStatus.COMPLETED.value
        )
        job.completed_at = datetime.utcnow()
    elif any(s == JobStatus.RUNNING.value for s in statuses):
        job.status = JobStatus.RUNNING.value
    else:
        job.status = JobStatus.STARTING.value
    db.commit()
