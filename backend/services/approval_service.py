"""
In-app approval workflow for optimization recommendations.

Replaces the legacy "cluster email fabric" notebook (SMTP/Gmail + a PENDING/SENT
tracking table). There is no email anywhere in this path: approvals live in
ACELO, are created ONLY from real optimizer result rows, and every state change
is made by an identified user and recorded in approval_audit.

State machine (nothing moves automatically):

    PENDING --approve--> APPROVED --execute--> EXECUTING --> COMPLETED | FAILED
    PENDING --reject (reason required)--> REJECTED
    PENDING | APPROVED --cancel--> CANCELLED

Approval is NOT execution: APPROVED only becomes EXECUTING through an explicit
execute action, and only if the platform adapter really supports execution.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import AnalysisJob, ApprovalAudit, Environment, JobRun, OptimizationApproval
from platforms.base import PlatformCapabilityNotImplemented
from platforms.errors import PlatformError

logger = logging.getLogger("acelo.approvals")

PENDING = "PENDING"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
EXECUTING = "EXECUTING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
STATUSES = (PENDING, APPROVED, REJECTED, EXECUTING, COMPLETED, FAILED, CANCELLED)

# Same selection rule the legacy tracking notebook used for its approval queue:
# clusters the optimizer labelled Risky or Moderately Optimized, with a name.
APPROVAL_LABELS = frozenset({"Risky", "Moderately Optimized"})

# Optimizer output fields shown as evidence, when present on the result row.
EVIDENCE_FIELDS = (
    "efficiency_score",
    "underutilized_label",
    "oversized_label",
    "idle_score",
    "idle_impact_score",
    "oversized_score",
    "avg_cpu_util",
    "avg_memory_util",
    "idle_time_min",
    "cluster_uptime_hours",
    "total_jobs_run",
    "min_workers",
    "max_workers",
    "predicted_cost_usd",
    "predicted_savings_pct",
    "node_type",
)

MIN_REASON_LENGTH = 3


class ApprovalError(Exception):
    """A refused action. `status_code` maps to HTTP; the message is user-safe."""

    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class Actor:
    """The identified person acting. Never a placeholder user."""

    user_id: str
    user_name: str


# --- result rows -> approval candidates ------------------------------------------

def _number(value: Any) -> float | None:
    """A real number, or None when absent/unparseable. A real 0 stays 0."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def result_rows(payload: dict | None) -> list[dict]:
    """The optimizer's rows from a normalized run result (Fabric or file)."""
    if not payload:
        return []
    source = payload.get("source_payload") or {}
    rows = source.get("rows")
    return rows if isinstance(rows, list) else []


def is_candidate(row: dict) -> bool:
    return (
        _text(row.get("optimization_label")) in APPROVAL_LABELS
        and _text(row.get("cluster_name")) is not None
    )


def resource_identity(row: dict) -> str:
    return _text(row.get("cluster_id")) or _text(row.get("cluster_name")) or ""


def _evidence(row: dict) -> dict:
    return {field: row[field] for field in EVIDENCE_FIELDS if row.get(field) is not None}


def _run_context(db: Session, job_run: JobRun) -> tuple[str, str | None]:
    """(customer_id, environment_id) for a run — the tenant boundary."""
    job = job_run.analysis_job or db.query(AnalysisJob).filter(AnalysisJob.id == job_run.analysis_job_id).one()
    environment = db.query(Environment).filter(Environment.connection_id == job.connection_id).first()
    return job.customer_id, environment.id if environment else None


def _audit(
    db: Session,
    approval: OptimizationApproval,
    action: str,
    previous: str | None,
    new: str,
    actor: Actor | None = None,
    reason: str | None = None,
) -> None:
    db.add(
        ApprovalAudit(
            approval_id=approval.id,
            acelo_run_id=approval.acelo_run_id,
            customer_id=approval.customer_id,
            environment_id=approval.environment_id,
            user_id=actor.user_id if actor else None,
            user_name=actor.user_name if actor else None,
            action=action,
            previous_status=previous,
            new_status=new,
            reason=reason,
        )
    )


def sync_from_run(
    db: Session, job_run: JobRun, payload: dict | None, resource_id: str | None = None
) -> list[OptimizationApproval]:
    """
    Creates PENDING approvals for a completed run's candidate rows. Idempotent:
    an existing record for (customer, run, resource) is left exactly as it is,
    so refreshing results never duplicates or resets an approval.

    `resource_id` limits the sync to one cluster ("Send to Approval").
    Returns the approvals that exist for the synced candidates.
    """
    if job_run.domain != "cluster" or job_run.status != "COMPLETED":
        return []
    rows = [r for r in result_rows(payload) if is_candidate(r)]
    if resource_id is not None:
        rows = [r for r in rows if resource_identity(r) == resource_id]
    if not rows:
        return []

    customer_id, environment_id = _run_context(db, job_run)
    existing = {
        a.resource_id: a
        for a in db.query(OptimizationApproval).filter(
            OptimizationApproval.customer_id == customer_id,
            OptimizationApproval.acelo_run_id == job_run.id,
        )
    }

    created: list[OptimizationApproval] = []
    result: list[OptimizationApproval] = []
    for row in rows:
        identity = resource_identity(row)
        if not identity:
            continue
        if identity in existing:
            result.append(existing[identity])
            continue
        approval = OptimizationApproval(
            customer_id=customer_id,
            environment_id=environment_id,
            acelo_run_id=job_run.id,
            source="run",
            source_ref=f"run:{job_run.id}:{identity}",
            platform=job_run.platform,
            domain=job_run.domain,
            resource_id=identity,
            resource_name=_text(row.get("cluster_name")) or identity,
            optimization_label=_text(row.get("optimization_label")),
            status=PENDING,
            current_workers=_number(row.get("current_workers")),
            recommended_max_workers=_number(row.get("recommended_max_workers")),
            total_dbus_cost_usd=_number(row.get("total_dbus_cost_usd")),
            potential_monthly_savings=_number(row.get("potential_monthly_savings")),
            llm_optimization=_text(row.get("llm_optimization")),
            evidence_json=json.dumps(_evidence(row), default=str),
        )
        db.add(approval)
        existing[identity] = approval
        created.append(approval)
        result.append(approval)

    if created:
        try:
            db.flush()
            for approval in created:
                _audit(db, approval, "CREATED", None, PENDING)
            db.commit()
        except IntegrityError:
            # A concurrent read created the same records first; theirs stand.
            db.rollback()
            return (
                db.query(OptimizationApproval)
                .filter(OptimizationApproval.customer_id == customer_id,
                        OptimizationApproval.acelo_run_id == job_run.id)
                .all()
            )
        logger.info("approvals event=created run_id=%s count=%s", job_run.id, len(created))
    return result


def tracking_source_ref(environment_id: str, table: str, cluster_name: str) -> str:
    return f"tracking:{environment_id}:{table}:{cluster_name}"


def sync_from_tracking(
    db: Session, environment: Environment, rows: list[dict], table: str
) -> dict[str, int]:
    """
    Imports approval candidates from the Fabric `cluster_optimization_tracking`
    Delta table (the tracking logic of the retired email notebook).

    * Only rows the tracking logic selected (Risky / Moderately Optimized, with a
      cluster name) are candidates — re-checked here, never assumed.
    * ACELO owns approval state: a new candidate starts PENDING regardless of the
      row's legacy email status (SENT meant "emailed", not "decided"); an
      existing ACELO approval is never reset or duplicated by a re-read.
    * Values come from the Delta row; anything absent stays null.
    """
    existing = {
        a.source_ref: a
        for a in db.query(OptimizationApproval).filter(
            OptimizationApproval.customer_id == environment.customer_id,
            OptimizationApproval.environment_id == environment.id,
            OptimizationApproval.source == "tracking_table",
        )
    }
    created: list[OptimizationApproval] = []
    seen = skipped = 0
    for row in rows:
        name = _text(row.get("cluster_name"))
        if not is_candidate(row) or not name:
            skipped += 1
            continue
        seen += 1
        ref = tracking_source_ref(environment.id, table, name)
        if ref in existing:
            existing[ref].tracking_status = _text(row.get("status"))
            continue
        evidence = _evidence(row)
        if row.get("updated_at") is not None:
            evidence["tracking_updated_at"] = str(row["updated_at"])
        if _text(row.get("acelo_run_id")):
            evidence["source_acelo_run_id"] = _text(row.get("acelo_run_id"))
        approval = OptimizationApproval(
            customer_id=environment.customer_id,
            environment_id=environment.id,
            acelo_run_id=None,  # tracking rows are not an ACELO JobRun
            source="tracking_table",
            source_ref=ref,
            tracking_status=_text(row.get("status")),
            platform=environment.platform,
            domain="cluster",
            resource_id=_text(row.get("cluster_id")) or name,
            resource_name=name,
            optimization_label=_text(row.get("optimization_label")),
            status=PENDING,
            current_workers=_number(row.get("current_workers")),
            recommended_max_workers=_number(row.get("recommended_max_workers")),
            total_dbus_cost_usd=_number(row.get("total_dbus_cost_usd")),
            potential_monthly_savings=_number(row.get("potential_monthly_savings")),
            llm_optimization=_text(row.get("llm_optimization")),
            evidence_json=json.dumps(evidence, default=str),
        )
        db.add(approval)
        existing[ref] = approval
        created.append(approval)

    db.flush()
    for approval in created:
        _audit(db, approval, "CREATED", None, PENDING, reason=f"Imported from Delta table {table}")
    db.commit()
    logger.info(
        "approvals event=tracking_sync env_id=%s table=%s rows=%s candidates=%s created=%s",
        environment.id, table, len(rows), seen, len(created),
    )
    return {"rows_read": len(rows), "candidates": seen, "created": len(created), "skipped": skipped}


def safe_sync(db: Session, job_run: JobRun, payload: dict | None) -> None:
    """Result retrieval must never fail because approval creation did."""
    try:
        sync_from_run(db, job_run, payload)
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("approvals event=sync_failed run_id=%s", job_run.id)


# --- actions -------------------------------------------------------------------------

def _require(approval: OptimizationApproval, allowed: tuple[str, ...], action: str) -> None:
    if approval.status not in allowed:
        raise ApprovalError(
            f"This recommendation is {approval.status}; it cannot be {action}.", status_code=409
        )


def approve(db: Session, approval: OptimizationApproval, actor: Actor) -> OptimizationApproval:
    _require(approval, (PENDING,), "approved")
    previous = approval.status
    approval.status = APPROVED
    approval.approved_by = actor.user_name
    approval.approved_at = datetime.utcnow()
    _audit(db, approval, "APPROVED", previous, APPROVED, actor)
    db.commit()
    return approval


def reject(db: Session, approval: OptimizationApproval, actor: Actor, reason: str | None) -> OptimizationApproval:
    reason = (reason or "").strip()
    if len(reason) < MIN_REASON_LENGTH:
        raise ApprovalError("A rejection reason is required.", status_code=422)
    _require(approval, (PENDING,), "rejected")
    previous = approval.status
    approval.status = REJECTED
    approval.rejected_by = actor.user_name
    approval.rejected_at = datetime.utcnow()
    approval.rejection_reason = reason
    _audit(db, approval, "REJECTED", previous, REJECTED, actor, reason)
    db.commit()
    return approval


def cancel(db: Session, approval: OptimizationApproval, actor: Actor, reason: str | None) -> OptimizationApproval:
    _require(approval, (PENDING, APPROVED), "cancelled")
    previous = approval.status
    approval.status = CANCELLED
    _audit(db, approval, "CANCELLED", previous, CANCELLED, actor, (reason or "").strip() or None)
    db.commit()
    return approval


def execution_action(approval: OptimizationApproval) -> dict:
    """What the platform is asked to apply — exactly the approved recommendation."""
    return {
        "type": "cluster_rightsizing",
        "acelo_run_id": approval.acelo_run_id,
        "approval_id": approval.id,
        "resource_id": approval.resource_id,
        "resource_name": approval.resource_name,
        "current_workers": approval.current_workers,
        "recommended_max_workers": approval.recommended_max_workers,
    }


async def execute(db: Session, approval: OptimizationApproval, actor: Actor, adapter) -> OptimizationApproval:
    """
    Starts the approved change on the platform. Only reachable from APPROVED,
    and only by explicit request. If the platform cannot execute, the record
    stays APPROVED and the caller is told so — it is never marked as run.
    """
    _require(approval, (APPROVED,), "executed")
    if adapter is None:
        raise ApprovalError(
            "Optimizations from an uploaded file cannot be executed: there is no platform "
            "connected to apply them to.",
            status_code=409,
        )
    try:
        started = await adapter.start_execution(execution_action(approval))
    except PlatformCapabilityNotImplemented:
        raise ApprovalError(
            f"Automated execution is not available for {approval.platform} yet. The "
            "recommendation stays APPROVED and nothing was changed in your environment.",
            status_code=409,
        ) from None
    except PlatformError as exc:
        previous = approval.status
        approval.status = FAILED
        approval.execution_error = exc.message
        approval.validation_status = "failed"
        _audit(db, approval, "EXECUTION_FAILED", previous, FAILED, actor, exc.message)
        db.commit()
        return approval

    previous = approval.status
    approval.status = EXECUTING
    approval.execution_id = started.platform_run_id
    approval.validation_status = "pending"
    approval.execution_error = None
    _audit(db, approval, "EXECUTION_STARTED", previous, EXECUTING, actor)
    db.commit()
    return approval


async def refresh_execution(db: Session, approval: OptimizationApproval, adapter) -> OptimizationApproval:
    """Polls a running execution's real validation; promotes to COMPLETED/FAILED."""
    if approval.status != EXECUTING or not approval.execution_id or adapter is None:
        return approval
    try:
        result = await adapter.validate_execution(approval.execution_id)
    except PlatformCapabilityNotImplemented:
        return approval
    except PlatformError as exc:
        logger.warning("approvals event=validation_poll_failed id=%s code=%s", approval.id, exc.code)
        return approval

    if result.status == "COMPLETED":
        approval.status = COMPLETED
        approval.validation_status = "passed"
        _audit(db, approval, "EXECUTION_COMPLETED", EXECUTING, COMPLETED)
        db.commit()
    elif result.status in ("FAILED", "CANCELLED"):
        approval.status = FAILED
        approval.validation_status = "failed"
        approval.execution_error = result.error or f"Platform reported {result.status}."
        _audit(db, approval, "EXECUTION_FAILED", EXECUTING, FAILED, reason=approval.execution_error)
        db.commit()
    return approval


# --- reads -----------------------------------------------------------------------------

def summary(db: Session, customer_id: str) -> dict[str, int]:
    counts = {status: 0 for status in STATUSES}
    for (status,) in db.query(OptimizationApproval.status).filter(OptimizationApproval.customer_id == customer_id):
        counts[status] = counts.get(status, 0) + 1
    return counts


def serialize(approval: OptimizationApproval, history: list[ApprovalAudit] | None = None) -> dict:
    data = {
        "approval_id": approval.id,
        "acelo_run_id": approval.acelo_run_id,
        "source": approval.source,
        "tracking_status": approval.tracking_status,
        "customer_id": approval.customer_id,
        "environment_id": approval.environment_id,
        "platform": approval.platform,
        "domain": approval.domain,
        "resource_id": approval.resource_id,
        "resource_name": approval.resource_name,
        "optimization_label": approval.optimization_label,
        "status": approval.status,
        "current_workers": approval.current_workers,
        "recommended_max_workers": approval.recommended_max_workers,
        "total_dbus_cost_usd": approval.total_dbus_cost_usd,
        "potential_monthly_savings": approval.potential_monthly_savings,
        "llm_optimization": approval.llm_optimization,
        "evidence": json.loads(approval.evidence_json) if approval.evidence_json else {},
        "created_at": _iso(approval.created_at),
        "updated_at": _iso(approval.updated_at),
        "approved_by": approval.approved_by,
        "approved_at": _iso(approval.approved_at),
        "rejected_by": approval.rejected_by,
        "rejected_at": _iso(approval.rejected_at),
        "rejection_reason": approval.rejection_reason,
        "execution_id": approval.execution_id,
        "validation_status": approval.validation_status,
        "execution_error": approval.execution_error,
    }
    if history is not None:
        data["history"] = [
            {
                "action": h.action,
                "previous_status": h.previous_status,
                "new_status": h.new_status,
                "user_id": h.user_id,
                "user_name": h.user_name,
                "reason": h.reason,
                "timestamp": _iso(h.timestamp),
            }
            for h in history
        ]
    return data


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def tracking_config(db: Session, environment: Environment) -> dict | None:
    """
    Where this environment's approval tracking Delta table lives — entirely from
    Cluster Settings and the resolved default Lakehouse. None when not configured.
    """
    from models import Connection
    from services import provisioning_service

    connection = db.query(Connection).filter(Connection.id == environment.connection_id).first()
    metadata = json.loads(connection.auth_metadata) if connection and connection.auth_metadata else {}
    table = (metadata.get("cluster_approval_tracking_table") or "").strip()
    if not table:
        return None
    return {
        "table": table,
        "schema": (metadata.get("cluster_result_schema") or metadata.get("cluster_source_schema") or "").strip() or None,
        "lakehouse": provisioning_service.default_lakehouse(db, environment),
    }


async def import_tracking(db: Session, environment: Environment, adapter) -> dict:
    """
    Reads the environment's tracking Delta table through the adapter (OneLake)
    and imports candidates. Returns an outcome dict; raises nothing — a failed
    read is reported, never replaced with data.
    """
    config = tracking_config(db, environment)
    outcome: dict = {"environment_id": environment.id}
    if config is None:
        return {**outcome, "status": "not_configured"}
    outcome["table"] = config["table"]
    if not config["lakehouse"]:
        return {**outcome, "status": "failed", "error_code": "NOT_CONFIGURED",
                "message": "No Lakehouse is configured for this environment. Set the Lakehouse ID in Cluster Settings."}
    try:
        rows = await adapter.read_delta_table(
            config["lakehouse"]["id"], config["table"], config["schema"],
            workspace_id=config["lakehouse"]["workspace_id"] or None,
        )
    except PlatformCapabilityNotImplemented as exc:
        return {**outcome, "status": "failed", "error_code": "UNSUPPORTED", "message": str(exc)}
    except PlatformError as exc:
        return {**outcome, "status": "failed", "error_code": exc.code, "message": exc.message}
    return {**outcome, "status": "ok", **sync_from_tracking(db, environment, rows, config["table"])}
