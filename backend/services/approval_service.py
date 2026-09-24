"""
In-app approval workflow for optimization recommendations.

Replaces the legacy "cluster email fabric" notebook (mail-based approvals + a PENDING/SENT
tracking table). There is no email anywhere in this path: approvals live in
ACELO, are created ONLY from real optimizer result rows, and every state change
is made by an identified user and recorded in approval_audit.

State machine (nothing moves automatically):

    PENDING --approve--> APPROVED --execute--> EXECUTING --> COMPLETED | FAILED
    PENDING --reject (reason required)--> REJECTED
    PENDING | APPROVED --cancel--> CANCELLED

Approval is NOT execution: APPROVED only becomes EXECUTING through an explicit
execute action, and only if the platform adapter really supports execution.

Current state vs history
------------------------
optimization_approvals holds the CURRENT recommendation per business key
(environment, domain, cluster) — exactly one record, however many runs saw the
cluster. Runs are history (job_runs/job_logs, append-only); decisions are
history (approval_audit). Every write goes through upsert_current():

  * new entity                          -> INSERT, PENDING
  * existing PENDING                    -> UPDATE with the latest values
  * existing decided, same values       -> unchanged (last_seen_* updated)
  * existing decided, new values        -> decision KEPT, new values stored in
                                           latest_recommendation_json and
                                           requires_new_approval = True
  * reviewer explicitly reopens         -> latest values applied, PENDING again
                                           (REOPENED audit keeps the old decision)
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
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
    Upserts a completed run's candidate rows into the CURRENT approval state.

    Identity is the business key (environment, domain, cluster) — not the run.
    The same cluster from a later run UPDATES its record; a new cluster
    INSERTS one. Refreshing results, a replayed completion or a second worker
    pass are all no-ops. `resource_id` limits the sync to one cluster
    ("Send to Approval"). Returns the current records for the synced candidates.
    """
    if job_run.domain != "cluster" or job_run.status != "COMPLETED":
        return []
    rows = [r for r in result_rows(payload) if is_candidate(r)]
    if resource_id is not None:
        rows = [r for r in rows if resource_identity(r) == resource_id]
    if not rows:
        return []

    customer_id, environment_id = _run_context(db, job_run)
    outcome = upsert_current(
        db,
        customer_id=customer_id,
        environment_id=environment_id,
        platform=job_run.platform,
        domain=job_run.domain,
        rows=rows,
        run_id=job_run.id,
        source="run",
        origin=f"ACELO run {job_run.id}",
    )
    return outcome.records


def tracking_source_ref(environment_id: str, table: str, cluster_name: str) -> str:
    """Legacy identity of tracking-table imports (kept to recognise old records)."""
    return f"tracking:{environment_id}:{table}:{cluster_name}"


def sync_from_tracking(
    db: Session, environment: Environment, rows: list[dict], table: str
) -> dict[str, int]:
    """
    Upserts candidates from the Fabric approval tracking Delta table into the
    same CURRENT approval state the run sync maintains — one record per
    (environment, domain, cluster), whichever source saw it first.

    * Only rows the tracking logic selected (Risky / Moderately Optimized, with a
      cluster name) are candidates — re-checked here, never assumed.
    * ACELO owns approval state: the Delta row's own status (legacy "SENT" etc.)
      is informational only and never changes an ACELO decision.
    * Values come from the Delta row; anything absent stays null.
    """
    candidates = [r for r in rows if is_candidate(r)]
    outcome = upsert_current(
        db,
        customer_id=environment.customer_id,
        environment_id=environment.id,
        platform=environment.platform,
        domain="cluster",
        rows=candidates,
        run_id=None,
        source="tracking_table",
        origin=f"Delta table {table}",
    )
    return {
        "rows_read": len(rows),
        "candidates": len(candidates),
        "unique_business_keys": outcome.unique_keys,
        "duplicates_removed": outcome.duplicates_removed,
        "created": outcome.inserted,
        "updated": outcome.updated,
        "unchanged": outcome.unchanged,
        "requires_new_approval": outcome.flagged,
        "merged_existing_duplicates": outcome.merged_existing,
        "skipped": len(rows) - len(candidates),
    }


# --- current-state upsert (the single write path for approvals) --------------------

# Fields that make up "the recommendation". A change in any of them on a decided
# record means the reviewer decided on something else than what is current.
RECOMMENDATION_FIELDS = (
    "optimization_label",
    "current_workers",
    "recommended_max_workers",
    "total_dbus_cost_usd",
    "potential_monthly_savings",
    "llm_optimization",
)
# Statuses whose decision is kept when a new recommendation arrives. PENDING is
# simply updated in place: nobody has decided on the old values yet.
DECIDED = (APPROVED, REJECTED, EXECUTING, COMPLETED, FAILED, CANCELLED)
# Status rank used when legacy duplicates must be merged: the record carrying
# the most advanced human decision survives.
_STATUS_RANK = {PENDING: 0, CANCELLED: 1, REJECTED: 2, APPROVED: 3, FAILED: 4, EXECUTING: 5, COMPLETED: 6}


def business_key(environment_id: str | None, domain: str, resource_id: str, run_id: str | None = None) -> str:
    """
    Current-state identity: (environment, optimization, entity). The ACELO run id
    is deliberately NOT part of it — a run is an execution, not an entity.
    Uploaded-file runs have no environment; each upload is its own scope.
    """
    scope = environment_id or f"file-run:{run_id}"
    return f"current:{scope}|{domain}|{resource_id}"


@dataclass
class UpsertOutcome:
    records: list[OptimizationApproval]
    source_rows: int = 0
    unique_keys: int = 0
    duplicates_removed: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    flagged: int = 0
    matched: int = 0
    merged_existing: int = 0


def _recommendation(row: dict) -> dict[str, Any]:
    return {
        "optimization_label": _text(row.get("optimization_label")),
        "current_workers": _number(row.get("current_workers")),
        "recommended_max_workers": _number(row.get("recommended_max_workers")),
        "total_dbus_cost_usd": _number(row.get("total_dbus_cost_usd")),
        "potential_monthly_savings": _number(row.get("potential_monthly_savings")),
        "llm_optimization": _text(row.get("llm_optimization")),
    }


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        if a is None or b is None:
            return a is b
        return abs(float(a) - float(b)) < 1e-6
    return a == b


def _differs(approval: OptimizationApproval, values: dict[str, Any], fields=RECOMMENDATION_FIELDS) -> bool:
    return any(not _same(getattr(approval, f), values.get(f)) for f in fields)


def _row_time(row: dict) -> str:
    for field in ("acelo_analyzed_at", "updated_at", "analyzed_at"):
        if row.get(field) is not None:
            return str(row[field])
    return ""


def dedupe_rows(rows: list[dict], identity_of=None) -> tuple[dict[str, dict], int]:
    """
    One row per entity BEFORE any write: when the source repeats an entity, the
    latest row (by analysis/update time, then position) is the current one.
    """
    identity_of = identity_of or resource_identity
    latest: dict[str, dict] = {}
    for row in rows:
        identity = identity_of(row)
        if not identity:
            continue
        current = latest.get(identity)
        if current is None or _row_time(row) >= _row_time(current):
            latest[identity] = row
    removed = sum(1 for r in rows if identity_of(r)) - len(latest)
    return latest, removed


def _merge_duplicates(db: Session, records: list[OptimizationApproval]) -> OptimizationApproval:
    """
    Collapses legacy duplicate records of ONE entity into one. The survivor is
    the record with the most advanced human decision (then the most recent);
    the others' audit history is re-pointed to it, so nothing is lost.
    """
    records = sorted(
        records,
        key=lambda a: (_STATUS_RANK.get(a.status, 0), a.updated_at or a.created_at or datetime.min),
        reverse=True,
    )
    keep, extras = records[0], records[1:]
    for extra in extras:
        db.query(ApprovalAudit).filter(ApprovalAudit.approval_id == extra.id).update(
            {"approval_id": keep.id}, synchronize_session=False
        )
        db.delete(extra)
    db.flush()
    _audit(db, keep, "DUPLICATES_MERGED", keep.status, keep.status,
           reason=f"Merged {len(extras)} duplicate record(s) of the same cluster into this one.")
    return keep


def upsert_current(
    db: Session,
    *,
    customer_id: str,
    environment_id: str | None,
    platform: str,
    domain: str,
    rows: list[dict],
    run_id: str | None,
    source: str,
    origin: str,
    profile: "Profile | None" = None,
) -> UpsertOutcome:
    """
    MERGE of candidate rows into current approval state, keyed by
    business_key(). Conceptually:

        MERGE INTO optimization_approvals t
        USING (latest row per entity) s
        ON t.customer = s.customer AND t.environment = s.environment
           AND t.domain = s.domain AND t.resource_id = s.resource_id
        WHEN MATCHED     -> update (PENDING) / flag new recommendation (decided)
        WHEN NOT MATCHED -> insert PENDING

    Approval-cycle rule (documented in the module docstring): nothing moves
    automatically. A PENDING record takes the latest values. A decided record
    keeps its status and the values that were decided on; a changed
    recommendation is stored in latest_recommendation_json and the record is
    flagged requires_new_approval until a reviewer explicitly reopens it.
    """
    profile = profile or CLUSTER_PROFILE
    latest, removed = dedupe_rows(rows, profile.identity)
    outcome = UpsertOutcome(records=[], source_rows=len(rows), unique_keys=len(latest), duplicates_removed=removed)
    if not latest:
        _log_merge(outcome, run_id, source, environment_id)
        return outcome

    scope = db.query(OptimizationApproval).filter(
        OptimizationApproval.customer_id == customer_id,
        OptimizationApproval.domain == domain,
    )
    if environment_id:
        scope = scope.filter(OptimizationApproval.environment_id == environment_id)
    else:
        scope = scope.filter(OptimizationApproval.environment_id.is_(None),
                             OptimizationApproval.acelo_run_id == run_id)
    by_resource: dict[str, list[OptimizationApproval]] = {}
    by_name: dict[str, list[OptimizationApproval]] = {}
    for approval in scope.all():
        by_resource.setdefault(approval.resource_id, []).append(approval)
        by_name.setdefault(approval.resource_name, []).append(approval)

    now = datetime.utcnow()
    for identity, row in latest.items():
        name = profile.name(row) or identity
        values = profile.values(row)
        matches = list(by_resource.get(identity, []))
        if not profile.match_by_name:
            pass
        elif not matches and not _text(row.get("cluster_id")):
            # A source without cluster_id (legacy tracking table) is matched by
            # name, so it lands on the record a run created for the same cluster.
            matches = list(by_name.get(name, []))
        seen = {id(m) for m in matches}
        for extra in by_name.get(name, []) if profile.match_by_name and _text(row.get("cluster_id")) else []:
            # The same cluster previously recorded under its name only.
            if id(extra) not in seen and extra.resource_id == name:
                matches.append(extra)
                seen.add(id(extra))

        key = business_key(environment_id, domain, identity, run_id)
        evidence = profile.evidence(row)
        if row.get("updated_at") is not None:
            evidence["tracking_updated_at"] = str(row["updated_at"])
        if _text(row.get("acelo_run_id")):
            evidence["source_acelo_run_id"] = _text(row.get("acelo_run_id"))

        if not matches:
            approval = OptimizationApproval(
                customer_id=customer_id,
                environment_id=environment_id,
                acelo_run_id=run_id,
                source=source,
                source_ref=key,
                tracking_status=profile.tracking_status(row) if source == "tracking_table" else None,
                platform=platform,
                domain=domain,
                resource_id=identity,
                resource_name=name,
                status=PENDING,
                evidence_json=json.dumps(evidence, default=str),
                last_seen_run_id=run_id,
                last_seen_at=now,
                requires_new_approval=False,
                **values,
            )
            db.add(approval)
            db.flush()
            _audit(db, approval, "CREATED", None, PENDING, reason=f"From {origin}")
            by_resource.setdefault(identity, []).append(approval)
            by_name.setdefault(name, []).append(approval)
            outcome.inserted += 1
            outcome.records.append(approval)
            continue

        outcome.matched += 1
        if len(matches) > 1:
            outcome.merged_existing += len(matches) - 1
            approval = _merge_duplicates(db, matches)
            for extra in matches:
                if extra is not approval:
                    for index in (by_resource, by_name):
                        for bucket in index.values():
                            if extra in bucket:
                                bucket.remove(extra)
        else:
            approval = matches[0]

        # Canonical identity from now on (also upgrades legacy "run:"/"tracking:" refs).
        approval.source_ref = key
        approval.resource_id = identity
        approval.resource_name = name
        if source == "tracking_table":
            approval.tracking_status = profile.tracking_status(row)
        if run_id:
            approval.last_seen_run_id = run_id
        approval.last_seen_at = now

        if not _differs(approval, values, profile.fields):
            outcome.unchanged += 1
        elif approval.status == PENDING:
            for field, value in values.items():
                setattr(approval, field, value)
            approval.evidence_json = json.dumps(evidence, default=str)
            if run_id:
                approval.acelo_run_id = run_id
            _audit(db, approval, "RECOMMENDATION_UPDATED", PENDING, PENDING, reason=f"Latest values from {origin}")
            outcome.updated += 1
        else:
            pending = {**values, "evidence": evidence, "source": origin, "run_id": run_id}
            previous = json.loads(approval.latest_recommendation_json) if approval.latest_recommendation_json else None
            if not (previous and all(_same(previous.get(f), values.get(f)) for f in profile.fields)):
                approval.latest_recommendation_json = json.dumps(pending, default=str)
                if not approval.requires_new_approval:
                    _audit(db, approval, "NEW_RECOMMENDATION", approval.status, approval.status,
                           reason=f"{origin} produced a different recommendation; the {approval.status} "
                                  "decision is kept until a reviewer reopens it.")
                approval.requires_new_approval = True
                outcome.flagged += 1
            else:
                outcome.unchanged += 1
        outcome.records.append(approval)

    try:
        db.commit()
    except IntegrityError:
        # A concurrent pass (worker + page refresh) wrote the same keys first.
        # Theirs stand; retrying against the committed state is a no-op merge.
        db.rollback()
        return upsert_current(
            db, customer_id=customer_id, environment_id=environment_id, platform=platform,
            domain=domain, rows=rows, run_id=run_id, source=source, origin=origin,
        )
    _log_merge(outcome, run_id, source, environment_id)
    return outcome


def _log_merge(outcome: UpsertOutcome, run_id: str | None, source: str, environment_id: str | None) -> None:
    logger.info(
        "[APPROVAL_MERGE] run_id=%s source=%s environment_id=%s source_rows=%s unique_business_keys=%s "
        "matched=%s inserted=%s updated=%s unchanged=%s requires_new_approval=%s duplicates_removed=%s "
        "merged_existing_duplicates=%s",
        run_id, source, environment_id, outcome.source_rows, outcome.unique_keys, outcome.matched,
        outcome.inserted, outcome.updated, outcome.unchanged, outcome.flagged, outcome.duplicates_removed,
        outcome.merged_existing,
    )


def reopen(db: Session, approval: OptimizationApproval, actor: Actor) -> OptimizationApproval:
    """
    Explicit start of a new approval cycle for a record flagged
    requires_new_approval: the latest recommendation becomes current and the
    record returns to PENDING. The earlier decision stays in approval_audit.
    """
    if not approval.requires_new_approval or not approval.latest_recommendation_json:
        raise ApprovalError("There is no newer recommendation to review for this item.")
    if approval.status == EXECUTING:
        raise ApprovalError("This recommendation is executing; wait for the execution to finish.")
    latest = json.loads(approval.latest_recommendation_json)
    previous = approval.status
    for field in REOPENABLE_FIELDS:
        if field in latest:
            setattr(approval, field, latest.get(field))
    if latest.get("evidence") is not None:
        approval.evidence_json = json.dumps(latest["evidence"], default=str)
    if latest.get("run_id"):
        approval.acelo_run_id = latest["run_id"]
    approval.status = PENDING
    approval.requires_new_approval = False
    approval.latest_recommendation_json = None
    approval.approved_by = approval.approved_at = None
    approval.rejected_by = approval.rejected_at = approval.rejection_reason = None
    approval.execution_id = approval.validation_status = approval.execution_error = None
    _audit(db, approval, "REOPENED", previous, PENDING, actor,
           reason=f"New approval cycle for the latest recommendation (previous decision: {previous}).")
    db.commit()
    return approval


def collapse_duplicates(db: Session, customer_id: str | None = None) -> int:
    """
    One-off repair for databases written before business-key identity: merges
    every set of records for the same (customer, environment, domain, entity).
    Audit history is preserved. Returns the number of records removed.
    """
    query = db.query(OptimizationApproval)
    if customer_id:
        query = query.filter(OptimizationApproval.customer_id == customer_id)
    groups: dict[tuple, list[OptimizationApproval]] = {}
    for approval in query.all():
        scope = approval.environment_id or f"file-run:{approval.acelo_run_id}"
        groups.setdefault((approval.customer_id, scope, approval.domain, approval.resource_name), []).append(approval)
    removed = 0
    for (_, scope, domain, _name), records in groups.items():
        keep = _merge_duplicates(db, records) if len(records) > 1 else records[0]
        removed += len(records) - 1
        keep.source_ref = business_key(
            None if scope.startswith("file-run:") else scope, domain, keep.resource_id, keep.acelo_run_id
        )
    db.commit()
    logger.info("[APPROVAL_MERGE] event=collapse_duplicates removed=%s", removed)
    return removed


# --- per-domain profiles ------------------------------------------------------------

@dataclass(frozen=True)
class Profile:
    """How one optimization domain maps source rows onto current approval state."""

    identity: Any
    name: Any
    values: Any
    evidence: Any
    fields: tuple
    tracking_status: Any
    match_by_name: bool = False


CLUSTER_PROFILE = Profile(
    identity=resource_identity,
    name=lambda row: _text(row.get("cluster_name")),
    values=lambda row: _recommendation(row),
    evidence=lambda row: _evidence(row),
    fields=RECOMMENDATION_FIELDS,
    tracking_status=lambda row: _text(row.get("status")),
    match_by_name=True,
)

# Query tracking rows (query_tracking_full_v1) that are approval items: the
# Validation notebook's verdicts. 'sent' is the retired email flow's "verified
# and emailed, awaiting a reply" - still undecided, so it is an item too.
# 'pending' (not validated yet) and the email era's final states
# (APPLIED / REJECTED / FAILED) are not new approval items.
QUERY_REVIEW_STATES = {"verified": "verified", "review_required": "review_required", "sent": "verified"}

QUERY_EVIDENCE_FIELDS = (
    "query_type", "priority", "recommendation_tier", "bottleneck_type", "root_cause", "confidence",
    "secondary_actions", "optimize_commands", "savings_pct", "savings_percentage", "actual_cost_usd",
    "optimized_cost_usd", "original_cost_usd", "original_duration_seconds", "optimized_duration_seconds",
    "validation_schema_match", "validation_row_count_match", "validation_reason", "execution_time_ms",
    "cpu_time_ms", "bytes_scanned", "bytes_spilled", "shuffle_bytes", "daily_run", "monthly_run",
    "total_run", "warehouse_size", "cluster_segment", "llm_status", "sql_source", "start_time",
)
QUERY_FIELDS = ("optimized_sql", "platform_validation_status", "potential_monthly_savings", "optimization_label")


def extract_optimized_sql(suggestion: Any, original_sql: str | None) -> str | None:
    """The same extraction the Validation/Email notebooks apply to llm_refactor_suggestion."""
    import re

    text = _text(suggestion)
    if not text:
        return None
    match = re.search(r"```(?:sql)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    sql = match.group(1).strip() if match else text
    return sql.rstrip(";").strip() or original_sql


def is_query_candidate(row: dict) -> bool:
    return bool(_text(row.get("query_id"))) and (
        (_text(row.get("workflow_status")) or "").lower() in QUERY_REVIEW_STATES
    )


def _query_values(row: dict) -> dict[str, Any]:
    status = (_text(row.get("workflow_status")) or "").lower()
    original = _text(row.get("query_text"))
    return {
        "optimization_label": _text(row.get("bottleneck_type")),
        "llm_optimization": _text(row.get("primary_action")),
        "potential_monthly_savings": _number(row.get("potential_savings_usd")),
        "total_dbus_cost_usd": _number(row.get("actual_cost_usd")),
        "current_workers": None,
        "recommended_max_workers": None,
        "original_sql": original,
        "optimized_sql": _text(row.get("validated_optimized_sql"))
        or extract_optimized_sql(row.get("llm_refactor_suggestion"), original),
        "platform_validation_status": QUERY_REVIEW_STATES.get(status),
    }


QUERY_PROFILE = Profile(
    identity=lambda row: _text(row.get("query_id")) or "",
    name=lambda row: _text(row.get("query_id")),
    values=_query_values,
    evidence=lambda row: {f: row[f] for f in QUERY_EVIDENCE_FIELDS if row.get(f) is not None},
    fields=QUERY_FIELDS,
    tracking_status=lambda row: _text(row.get("workflow_status")),
)

REOPENABLE_FIELDS = tuple(dict.fromkeys(RECOMMENDATION_FIELDS + QUERY_FIELDS + (
    "total_dbus_cost_usd", "original_sql", "llm_optimization")))


def sync_query_tracking(
    db: Session, environment: Environment, rows: list[dict], table: str, run_id: str | None = None
) -> dict[str, int]:
    """
    Upserts query approval items from the Validation notebook's tracking table,
    keyed by (environment, "query", query_id). Idempotent: the same 117 rows
    read twice create nothing the second time; 2 genuinely new queries add 2;
    an existing decision is never reset (see upsert_current).
    """
    candidates = [r for r in rows if is_query_candidate(r)]
    outcome = upsert_current(
        db,
        customer_id=environment.customer_id,
        environment_id=environment.id,
        platform=environment.platform,
        domain="query",
        rows=candidates,
        run_id=run_id,
        source="tracking_table",
        origin=f"Delta table {table}",
        profile=QUERY_PROFILE,
    )
    if outcome.inserted:
        _notify(db, environment.customer_id, "approval_required",
                "Query optimization requires your approval.",
                f"{outcome.inserted} new query optimization(s) are ready for review.",
                "/approvals?status=PENDING")
    return {
        "rows_read": len(rows),
        "candidates": len(candidates),
        "unique_business_keys": outcome.unique_keys,
        "duplicates_removed": outcome.duplicates_removed,
        "created": outcome.inserted,
        "updated": outcome.updated,
        "unchanged": outcome.unchanged,
        "requires_new_approval": outcome.flagged,
        "skipped": len(rows) - len(candidates),
    }


def _notify(db: Session, customer_id: str, kind: str, title: str, body: str, link: str) -> None:
    """In-app notification only. There is no email anywhere in ACELO."""
    from models import Notification

    db.add(Notification(customer_id=customer_id, type=kind, title=title, body=body, link=link))
    db.commit()


def safe_sync_query(db: Session, job_run: JobRun, payload: dict | None) -> dict | None:
    """Query run completed: import its tracking rows as approval items. Never fatal."""
    try:
        if job_run.domain != "query" or job_run.status != "COMPLETED":
            return None
        _, environment_id = _run_context(db, job_run)
        environment = db.query(Environment).filter(Environment.id == environment_id).first()
        if environment is None:
            return None
        source = (payload or {}).get("source_payload") or {}
        rows = source.get("rows") if isinstance(source.get("rows"), list) else []
        return sync_query_tracking(db, environment, rows, str(source.get("table") or "query tracking"), job_run.id)
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("approvals event=query_sync_failed run_id=%s", job_run.id)
        return None


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


def _decision_notice(db: Session, approval: OptimizationApproval, verb: str) -> None:
    kind = "Query" if approval.domain == "query" else approval.domain.title()
    _notify(db, approval.customer_id, f"approval_{verb}", f"{kind} optimization {verb}.",
            f"{approval.resource_name}", f"/approvals/{approval.id}")


def approve(db: Session, approval: OptimizationApproval, actor: Actor) -> OptimizationApproval:
    _require(approval, (PENDING,), "approved")
    previous = approval.status
    approval.status = APPROVED
    approval.approved_by = actor.user_name
    approval.approved_at = datetime.utcnow()
    _audit(db, approval, "APPROVED", previous, APPROVED, actor)
    db.commit()
    _decision_notice(db, approval, "approved")
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
    _decision_notice(db, approval, "rejected")
    return approval


def cancel(db: Session, approval: OptimizationApproval, actor: Actor, reason: str | None) -> OptimizationApproval:
    _require(approval, (PENDING, APPROVED), "cancelled")
    previous = approval.status
    approval.status = CANCELLED
    _audit(db, approval, "CANCELLED", previous, CANCELLED, actor, (reason or "").strip() or None)
    db.commit()
    return approval


def execution_action(approval: OptimizationApproval, actor: "Actor | None" = None) -> dict:
    """What the platform is asked to apply — exactly the approved recommendation."""
    if approval.domain == "query":
        return {
            "type": "query_apply",
            "approval_id": approval.id,
            "query_id": approval.resource_id,
            "approved_by": approval.approved_by or (actor.user_name if actor else None),
        }
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
    if approval.domain == "cluster":
        raise ApprovalError(
            "Cluster recommendations are reporting only; there is nothing to execute.", status_code=409
        )
    if approval.domain == "query" and approval.platform_validation_status != "verified":
        raise ApprovalError(
            "The Validation notebook did not verify this optimization (review required), so it cannot be "
            "applied automatically. It stays APPROVED; nothing was changed.",
            status_code=409,
        )
    if adapter is None:
        raise ApprovalError(
            "Optimizations from an uploaded file cannot be executed: there is no platform "
            "connected to apply them to.",
            status_code=409,
        )
    try:
        started = await adapter.start_execution(execution_action(approval, actor))
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

def summary(db: Session, customer_id: str, domain: str | None = None) -> dict[str, int]:
    counts = {status: 0 for status in STATUSES}
    query = db.query(OptimizationApproval.status).filter(OptimizationApproval.customer_id == customer_id)
    if domain:
        query = query.filter(OptimizationApproval.domain == domain)
    for (status,) in query:
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
        "original_sql": approval.original_sql,
        "optimized_sql": approval.optimized_sql,
        "platform_validation_status": approval.platform_validation_status,
        "business_key": approval.source_ref,
        "last_seen_run_id": approval.last_seen_run_id,
        "last_seen_at": _iso(approval.last_seen_at),
        "requires_new_approval": bool(approval.requires_new_approval),
        "latest_recommendation": (
            json.loads(approval.latest_recommendation_json) if approval.latest_recommendation_json else None
        ),
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
    Where this environment's QUERY approval tracking table lives - from the
    Query registry row only (default: the Validation notebook's table).
    """
    from services import provisioning_service, resource_registry

    config = resource_registry.configured(db, environment, "query")
    return {
        "table": (config.get("approval_tracking_table") or "query_tracking_full_v1").strip(),
        "schema": (config.get("result_schema") or "").strip() or None,
        "lakehouse": provisioning_service.default_lakehouse(db, environment, "query"),
    }


async def import_tracking(db: Session, environment: Environment, adapter) -> dict:
    """
    Reads the environment's query tracking Delta table through the adapter
    (OneLake) and upserts the approval items. Returns an outcome dict; raises
    nothing - a failed read is reported, never replaced with data.
    """
    config = tracking_config(db, environment)
    outcome: dict = {"environment_id": environment.id}
    if config is None:
        return {**outcome, "status": "not_configured"}
    outcome["table"] = config["table"]
    if not config["lakehouse"]:
        return {**outcome, "status": "failed", "error_code": "NOT_CONFIGURED",
                "message": "No Lakehouse is configured for Query optimization. Set it in Environment Setup > Optimization Resources."}
    try:
        rows = await adapter.read_delta_table(
            config["lakehouse"]["id"], config["table"], config["schema"],
            workspace_id=config["lakehouse"]["workspace_id"] or None,
        )
    except PlatformCapabilityNotImplemented as exc:
        return {**outcome, "status": "failed", "error_code": "UNSUPPORTED", "message": str(exc)}
    except PlatformError as exc:
        return {**outcome, "status": "failed", "error_code": exc.code, "message": exc.message}
    return {**outcome, "status": "ok", **sync_query_tracking(db, environment, rows, config["table"])}
