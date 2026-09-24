"""
Current optimization opportunities, each from its own domain and never mixed:

* cluster -> the current cluster state (one row per cluster), reporting only
* query   -> query approval items (one per query_id)

Customer-scoped. A specific run's own results live on that run (/api/runs/{id}).
"""

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from api.deps import get_current_customer
from database import get_db
from models import ClusterRecommendation, Customer, OptimizationApproval
from services import approval_service, cluster_state

router = APIRouter(prefix="/api/optimizations", tags=["optimizations"])

_STATUS_VIEW = {"PENDING": "open", "APPROVED": "approved", "REJECTED": "rejected"}


def _cluster_view(r: ClusterRecommendation) -> dict:
    data = cluster_state.serialize(r)
    return {
        "id": f"cluster:{r.id}",
        "kind": "cluster",
        "resource": r.cluster_name or r.cluster_id,
        "resource_id": r.cluster_id,
        "domain": "cluster",
        "environment_id": r.environment_id,
        "title": f"{r.cluster_name or r.cluster_id}: {r.optimization_label}" if r.optimization_label else r.cluster_id,
        "description": r.llm_optimization,
        "estimated_savings_monthly": r.potential_monthly_savings,
        "current_cost_monthly": r.total_dbus_cost_usd,
        "optimization_label": r.optimization_label,
        "status": "reporting",
        "requires_approval": False,
        "acelo_run_id": r.last_run_id,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        "details": {"issue": r.optimization_label, "recommendation": r.llm_optimization, **data},
    }


def _query_view(a: OptimizationApproval) -> dict:
    evidence = json.loads(a.evidence_json) if a.evidence_json else {}
    return {
        "id": a.id,
        "kind": "query",
        "approval_id": a.id,
        "resource": a.resource_name,
        "resource_id": a.resource_id,
        "domain": "query",
        "environment_id": a.environment_id,
        "title": f"Query {a.resource_name}: {a.optimization_label}" if a.optimization_label else f"Query {a.resource_name}",
        "description": a.llm_optimization,
        "estimated_savings_monthly": a.potential_monthly_savings,
        "current_cost_monthly": a.total_dbus_cost_usd,
        "optimization_label": a.optimization_label,
        "status": _STATUS_VIEW.get(a.status, a.status.lower()),
        "approval_status": a.status,
        "requires_approval": True,
        "requires_new_approval": bool(a.requires_new_approval),
        "acelo_run_id": a.acelo_run_id,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
        "details": {"issue": a.optimization_label, "recommendation": a.llm_optimization, "evidence": evidence,
                    "original_sql": a.original_sql, "optimized_sql": a.optimized_sql,
                    "validation_status": a.platform_validation_status},
    }


@router.get("")
def list_optimizations(
    domain: str | None = None,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
):
    items: list[dict] = []
    if domain in (None, "cluster"):
        items += [_cluster_view(r) for r in
                  db.query(ClusterRecommendation).filter(ClusterRecommendation.customer_id == customer.id)
                  if r.optimization_label in ("Risky", "Moderately Optimized")]
    if domain in (None, "query"):
        items += [_query_view(a) for a in db.query(OptimizationApproval).filter(
            OptimizationApproval.customer_id == customer.id, OptimizationApproval.domain == "query")]
    items.sort(key=lambda i: (i["estimated_savings_monthly"] is None, -(i["estimated_savings_monthly"] or 0)))
    return items


@router.get("/recommendations/{recommendation_id}")
def get_recommendation(
    recommendation_id: str,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
):
    if recommendation_id.startswith("cluster:"):
        record = (
            db.query(ClusterRecommendation)
            .filter(ClusterRecommendation.id == recommendation_id.split(":", 1)[1],
                    ClusterRecommendation.customer_id == customer.id)
            .first()
        )
        if not record:
            raise HTTPException(status_code=404, detail="Recommendation not found")
        return _cluster_view(record)
    approval = (
        db.query(OptimizationApproval)
        .filter(OptimizationApproval.id == recommendation_id, OptimizationApproval.customer_id == customer.id)
        .first()
    )
    if not approval:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    return {**_query_view(approval), "approval": approval_service.serialize(approval)}
