"""Stage 1 approval workflow for persisted Recommendation records.

This workflow is intentionally separate from the legacy OptimizationApproval
workflow.

Phase 6 stops at APPROVED / REJECTED.

Approval:
- never creates an Execution
- never starts an execution job
- never mutates Databricks
- is customer-scoped
- records an audit event for every approval action
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from models import ApprovalRequest, AuditHistory, Customer, Recommendation


# ---------------------------------------------------------------------------
# Stage 1 recommendation lifecycle values
# ---------------------------------------------------------------------------

PENDING = "PENDING"
APPROVED = "APPROVED"
REJECTED = "REJECTED"

# ApprovalRequest stores lowercase workflow values.
DB_PENDING = "pending"
DB_APPROVED = "approved"
DB_REJECTED = "rejected"

# Explicit Phase 6 boundary values.
EXECUTION_NOT_STARTED = "NOT_STARTED"
VERIFICATION_NOT_STARTED = "NOT_STARTED"
POLICY_NOT_EVALUATED = "NOT_EVALUATED"

STAGE1_DOMAINS = {
    "cluster_sizing",
    "cluster_runtime",
    "autoscaling",
    "cluster",
}


class Stage1ApprovalError(Exception):
    """Expected business-rule failure in the Stage 1 approval workflow."""

    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Recommendation lookup
# ---------------------------------------------------------------------------


def _recommendation(
    db: Session,
    customer_id: str,
    recommendation_id: str,
) -> Recommendation:
    """Return a recommendation belonging to the current customer."""

    row = (
        db.query(Recommendation)
        .filter(
            Recommendation.recommendation_id == recommendation_id,
            Recommendation.customer_id == customer_id,
        )
        .first()
    )

    if not row:
        raise Stage1ApprovalError("Recommendation not found.", 404)

    return row


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------


def _json_value(value: str | None) -> Any:
    """Safely deserialize persisted JSON.

    Existing recommendation records should contain valid JSON. If a legacy
    or malformed row is encountered, do not turn the entire approval API into
    a 500 response.
    """
    if not value:
        return None

    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _serialize(
    rec: Recommendation,
    approval: ApprovalRequest | None,
) -> dict[str, Any]:
    """Return the approval view model consumed by the API/UI."""

    return {
        "approval_id": approval.id if approval else None,
        "recommendation_id": rec.recommendation_id,
        "status": approval.status if approval else None,
        "requested_by": approval.requested_by if approval else None,
        "decided_by": approval.decided_by if approval else None,
        "created_at": (
            approval.created_at.isoformat()
            if approval and approval.created_at
            else None
        ),
        "decided_at": (
            approval.decided_at.isoformat()
            if approval and approval.decided_at
            else None
        ),
        "recommendation": {
            "id": rec.id,
            "domain": rec.domain,
            "resource": rec.resource,
            "resource_id": rec.resource_id,
            "resource_type": rec.resource_type,
            "finding_id": rec.finding_id,
            "rule_id": rec.rule_id,
            "finding_type": rec.finding_type,
            "title": rec.title,
            "summary": rec.summary,
            "description": rec.description,
            "current_state": rec.current_state,
            "proposed_change": rec.proposed_change,
            "proposed_state": rec.proposed_state,
            "evidence": _json_value(rec.evidence_json),
            "evidence_references": _json_value(
                rec.evidence_references_json
            ),
            "evidence_quality": _json_value(
                rec.evidence_quality_json
            ),
            "observation_window": _json_value(
                rec.observation_window_json
            ),
            "expected_impact": _json_value(
                rec.expected_impact_json
            ),
            "estimated_savings": _json_value(
                rec.estimated_savings_json
            ),
            "confidence": rec.confidence,
            "severity": rec.severity,
            "risk": rec.risk,
            "approval_status": rec.approval_status,
            "policy_status": rec.policy_status,
            "execution_status": rec.execution_status,
            "verification_status": rec.verification_status,
            "customer_id": rec.customer_id,
            "environment_id": rec.environment_id,
            "workspace_name": rec.workspace_name,
        },
    }


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def _audit(
    db: Session,
    customer_id: str,
    event_type: str,
    summary: str,
    payload: dict[str, Any],
) -> None:
    """Persist a customer-scoped Stage 1 audit event."""

    db.add(
        AuditHistory(
            customer_id=customer_id,
            event_type=event_type,
            summary=summary,
            payload_json=json.dumps(payload, default=str),
        )
    )


# ---------------------------------------------------------------------------
# Approval lookup
# ---------------------------------------------------------------------------


def _find_active(
    db: Session,
    recommendation_pk: str,
) -> ApprovalRequest | None:
    """Return the newest pending approval request for a recommendation."""

    return (
        db.query(ApprovalRequest)
        .filter(
            ApprovalRequest.recommendation_id == recommendation_pk,
            ApprovalRequest.status == DB_PENDING,
        )
        .order_by(ApprovalRequest.created_at.desc())
        .first()
    )


def _find_latest(
    db: Session,
    recommendation_pk: str,
) -> ApprovalRequest | None:
    """Return the newest approval request, regardless of status."""

    return (
        db.query(ApprovalRequest)
        .filter(
            ApprovalRequest.recommendation_id == recommendation_pk,
        )
        .order_by(ApprovalRequest.created_at.desc())
        .first()
    )


# ---------------------------------------------------------------------------
# Request approval
# ---------------------------------------------------------------------------


def request_approval(
    db: Session,
    customer: Customer,
    recommendation_id: str,
    actor_name: str = "ACELO Agent",
) -> dict[str, Any]:
    """Create an idempotent Stage 1 approval request."""

    rec = _recommendation(
        db,
        customer.id,
        recommendation_id,
    )

    # Only Stage 1 recommendations may enter this workflow.
    if rec.domain not in STAGE1_DOMAINS:
        raise Stage1ApprovalError(
            "Only Stage 1 Databricks recommendations can enter this approval flow.",
            422,
        )

    # Repeated clicks/runs must not create another pending request.
    existing = _find_active(db, rec.id)
    if existing:
        return _serialize(rec, existing)

    # An approved recommendation is terminal for Phase 6.
    if (rec.approval_status or "").upper() == APPROVED:
        raise Stage1ApprovalError(
            "This recommendation is already approved.",
            409,
        )

    approval = ApprovalRequest(
        recommendation_id=rec.id,
        status=DB_PENDING,
        requested_by=(actor_name or "ACELO Agent")[:200],
    )

    db.add(approval)

    # Recommendation-level lifecycle fields.
    rec.approval_status = PENDING

    # Phase 6 explicitly stops before execution.
    rec.policy_status = POLICY_NOT_EVALUATED
    rec.execution_status = EXECUTION_NOT_STARTED
    rec.verification_status = VERIFICATION_NOT_STARTED
    rec.updated_at = datetime.utcnow()

    db.flush()

    _audit(
        db,
        customer.id,
        "STAGE1_APPROVAL_REQUESTED",
        f"Approval requested for recommendation {recommendation_id}.",
        {
            "recommendation_id": recommendation_id,
            "approval_id": approval.id,
            "requested_by": approval.requested_by,
            "approval_status": PENDING,
            "execution_status": EXECUTION_NOT_STARTED,
        },
    )

    db.commit()
    db.refresh(approval)

    return _serialize(rec, approval)


# ---------------------------------------------------------------------------
# Read one approval
# ---------------------------------------------------------------------------


def get_approval(
    db: Session,
    customer: Customer,
    recommendation_id: str,
) -> dict[str, Any]:
    """Return the latest approval request for a recommendation."""

    rec = _recommendation(
        db,
        customer.id,
        recommendation_id,
    )

    approval = _find_latest(db, rec.id)

    return _serialize(rec, approval)


# ---------------------------------------------------------------------------
# List approvals
# ---------------------------------------------------------------------------


def list_approvals(
    db: Session,
    customer: Customer,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """Return customer-scoped Stage 1 approval records."""

    query = (
        db.query(ApprovalRequest, Recommendation)
        .join(
            Recommendation,
            ApprovalRequest.recommendation_id == Recommendation.id,
        )
        .filter(
            Recommendation.customer_id == customer.id,
            Recommendation.domain.in_(STAGE1_DOMAINS),
        )
    )

    if status:
        wanted = [
            value.strip().lower()
            for value in status.split(",")
            if value.strip()
        ]

        allowed = {
            DB_PENDING,
            DB_APPROVED,
            DB_REJECTED,
        }

        unknown = sorted(set(wanted) - allowed)

        if unknown:
            raise Stage1ApprovalError(
                f"Unknown Stage 1 approval status: {', '.join(unknown)}.",
                422,
            )

        query = query.filter(
            ApprovalRequest.status.in_(wanted)
        )

    rows = (
        query.order_by(
            ApprovalRequest.created_at.desc()
        )
        .all()
    )

    return [
        _serialize(rec, approval)
        for approval, rec in rows
    ]


# ---------------------------------------------------------------------------
# Approve
# ---------------------------------------------------------------------------


def approve(
    db: Session,
    customer: Customer,
    recommendation_id: str,
    actor_id: str,
    actor_name: str,
) -> dict[str, Any]:
    """Approve a pending Stage 1 recommendation.

    IMPORTANT:
    This changes approval state only.
    It does NOT create an Execution and does NOT mutate Databricks.
    """

    rec = _recommendation(
        db,
        customer.id,
        recommendation_id,
    )

    approval = _find_active(db, rec.id)

    if not approval:
        raise Stage1ApprovalError(
            "There is no pending approval request for this recommendation.",
            409,
        )

    # Guard against an inconsistent recommendation/request state.
    if (rec.approval_status or "").upper() != PENDING:
        raise Stage1ApprovalError(
            "This recommendation is not in a pending approval state.",
            409,
        )

    approval.status = DB_APPROVED
    approval.decided_by = (actor_name or actor_id)[:200]
    approval.decided_at = datetime.utcnow()

    rec.approval_status = APPROVED

    # Phase 6 boundary remains explicit.
    rec.execution_status = EXECUTION_NOT_STARTED
    rec.verification_status = VERIFICATION_NOT_STARTED
    rec.updated_at = datetime.utcnow()

    _audit(
        db,
        customer.id,
        "STAGE1_APPROVAL_APPROVED",
        f"Recommendation {recommendation_id} approved.",
        {
            "recommendation_id": recommendation_id,
            "approval_id": approval.id,
            "decided_by": approval.decided_by,
            "actor_id": actor_id,
            "approval_status": APPROVED,
            "execution_status": EXECUTION_NOT_STARTED,
            "execution_created": False,
        },
    )

    db.commit()
    db.refresh(approval)

    return _serialize(rec, approval)


# ---------------------------------------------------------------------------
# Reject
# ---------------------------------------------------------------------------


def reject(
    db: Session,
    customer: Customer,
    recommendation_id: str,
    actor_id: str,
    actor_name: str,
    reason: str,
) -> dict[str, Any]:
    """Reject a pending Stage 1 recommendation."""

    reason = (reason or "").strip()

    if len(reason) < 3:
        raise Stage1ApprovalError(
            "A rejection reason is required.",
            422,
        )

    rec = _recommendation(
        db,
        customer.id,
        recommendation_id,
    )

    approval = _find_active(db, rec.id)

    if not approval:
        raise Stage1ApprovalError(
            "There is no pending approval request for this recommendation.",
            409,
        )

    # Guard against an inconsistent recommendation/request state.
    if (rec.approval_status or "").upper() != PENDING:
        raise Stage1ApprovalError(
            "This recommendation is not in a pending approval state.",
            409,
        )

    approval.status = DB_REJECTED
    approval.decided_by = (actor_name or actor_id)[:200]
    approval.decided_at = datetime.utcnow()

    rec.approval_status = REJECTED

    # Phase 6 still stops here.
    rec.execution_status = EXECUTION_NOT_STARTED
    rec.verification_status = VERIFICATION_NOT_STARTED
    rec.updated_at = datetime.utcnow()

    _audit(
        db,
        customer.id,
        "STAGE1_APPROVAL_REJECTED",
        f"Recommendation {recommendation_id} rejected.",
        {
            "recommendation_id": recommendation_id,
            "approval_id": approval.id,
            "decided_by": approval.decided_by,
            "actor_id": actor_id,
            "reason": reason,
            "approval_status": REJECTED,
            "execution_status": EXECUTION_NOT_STARTED,
        },
    )

    db.commit()
    db.refresh(approval)

    return _serialize(rec, approval)