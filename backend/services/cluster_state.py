"""
Current cluster optimization state (reporting only — clusters need no approval).

Every completed cluster run UPSERTS its rows by business key
(customer, environment, cluster_id): same cluster -> update, new cluster ->
insert. The run itself, and its full result, stay in job_runs untouched, so
Run History / Run Details always show exactly what each execution produced.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import ClusterRecommendation, JobRun
from services.approval_service import _number, _run_context, _text, dedupe_rows, result_rows

logger = logging.getLogger("acelo.cluster_state")

EVIDENCE_FIELDS = (
    "efficiency_score", "underutilized_label", "oversized_label", "idle_score", "idle_impact_score",
    "oversized_score", "idle_time_min", "cluster_uptime_hours", "total_jobs_run", "min_workers",
    "max_workers", "node_type", "ml_cost_variance",
)

LABELS = ("Risky", "Moderately Optimized", "Optimized")


def _values(row: dict) -> dict[str, Any]:
    return {
        "cluster_name": _text(row.get("cluster_name")),
        "optimization_label": _text(row.get("optimization_label")),
        "current_workers": _number(row.get("current_workers")),
        "recommended_max_workers": _number(row.get("recommended_max_workers")),
        "avg_cpu_util": _number(row.get("avg_cpu_util")),
        "avg_memory_util": _number(row.get("avg_memory_util")),
        "total_dbus_cost_usd": _number(row.get("total_dbus_cost_usd")),
        "predicted_cost_usd": _number(row.get("predicted_cost_usd")),
        "predicted_savings_pct": _number(row.get("predicted_savings_pct")),
        "potential_monthly_savings": _number(row.get("potential_monthly_savings")),
        "llm_optimization": _text(row.get("llm_optimization")),
        "analyzed_at": _text(row.get("acelo_analyzed_at")),
        "evidence_json": json.dumps({f: row[f] for f in EVIDENCE_FIELDS if row.get(f) is not None}, default=str),
    }


def upsert_from_run(db: Session, job_run: JobRun, payload: dict | None) -> dict[str, int]:
    """Idempotent: replaying the same run (worker retry, page refresh) changes nothing."""
    if job_run.domain != "cluster" or job_run.status != "COMPLETED":
        return {"inserted": 0, "updated": 0}
    rows = result_rows(payload)
    latest, removed = dedupe_rows(rows)
    customer_id, environment_id = _run_context(db, job_run)
    scope = environment_id or f"file-run:{job_run.id}"
    existing = {
        r.cluster_id: r
        for r in db.query(ClusterRecommendation).filter(
            ClusterRecommendation.customer_id == customer_id,
            ClusterRecommendation.environment_key == scope,
        )
    }
    inserted = updated = 0
    for cluster_id, row in latest.items():
        values = _values(row)
        record = existing.get(cluster_id)
        if record is None:
            record = ClusterRecommendation(
                customer_id=customer_id, environment_id=environment_id, environment_key=scope,
                platform=job_run.platform, cluster_id=cluster_id, first_run_id=job_run.id,
            )
            db.add(record)
            existing[cluster_id] = record
            inserted += 1
        elif record.last_run_id != job_run.id:
            updated += 1
        for field, value in values.items():
            setattr(record, field, value)
        record.last_run_id = job_run.id
        record.last_platform_run_id = job_run.platform_run_id
    try:
        db.commit()
    except IntegrityError:
        db.rollback()  # a concurrent pass stored the same run first; replaying is a no-op
        return upsert_from_run(db, job_run, payload)
    logger.info(
        "[CLUSTER_MERGE] run_id=%s source_rows=%s unique_business_keys=%s inserted=%s updated=%s "
        "duplicates_removed=%s",
        job_run.id, len(rows), len(latest), inserted, updated, removed,
    )
    return {"inserted": inserted, "updated": updated, "unique_business_keys": len(latest),
            "duplicates_removed": removed}


def safe_upsert(db: Session, job_run: JobRun, payload: dict | None) -> None:
    """Result retrieval must never fail because the current-state update did."""
    try:
        upsert_from_run(db, job_run, payload)
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("cluster_state event=upsert_failed run_id=%s", job_run.id)


def summary(db: Session, customer_id: str) -> dict[str, Any]:
    records = db.query(ClusterRecommendation).filter(ClusterRecommendation.customer_id == customer_id).all()
    by_label = {label: 0 for label in LABELS}
    for r in records:
        if r.optimization_label:
            by_label[r.optimization_label] = by_label.get(r.optimization_label, 0) + 1
    savings = [r.potential_monthly_savings for r in records if r.potential_monthly_savings is not None]
    cost = [r.total_dbus_cost_usd for r in records if r.total_dbus_cost_usd is not None]
    return {
        "clusters_analyzed": len(records),
        "by_label": by_label,
        # None, not 0, when no record carries the value.
        "potential_savings": round(sum(savings), 2) if savings else None,
        "monthly_cost": round(sum(cost), 2) if cost else None,
    }


def serialize(r: ClusterRecommendation) -> dict[str, Any]:
    optimized = (
        round(r.total_dbus_cost_usd - r.potential_monthly_savings, 2)
        if r.total_dbus_cost_usd is not None and r.potential_monthly_savings is not None
        else None
    )
    return {
        "id": r.id,
        "environment_id": r.environment_id,
        "platform": r.platform,
        "cluster_id": r.cluster_id,
        "cluster_name": r.cluster_name,
        "optimization_label": r.optimization_label,
        "current_workers": r.current_workers,
        "recommended_max_workers": r.recommended_max_workers,
        "avg_cpu_util": r.avg_cpu_util,
        "avg_memory_util": r.avg_memory_util,
        "current_cost_usd": r.total_dbus_cost_usd,
        # Derived for display: current cost minus the notebook's potential savings.
        "optimized_cost_usd": optimized,
        "predicted_cost_usd": r.predicted_cost_usd,
        "predicted_savings_pct": r.predicted_savings_pct,
        "potential_monthly_savings": r.potential_monthly_savings,
        "recommendation": r.llm_optimization,
        "evidence": json.loads(r.evidence_json) if r.evidence_json else {},
        "first_run_id": r.first_run_id,
        "last_run_id": r.last_run_id,
        "last_platform_run_id": r.last_platform_run_id,
        "analyzed_at": r.analyzed_at,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }
