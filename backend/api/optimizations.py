import json
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from api.deps import get_current_customer
from database import get_db
from models import Customer, Recommendation

router = APIRouter(prefix="/api/optimizations", tags=["optimizations"])


@router.get("")
def list_optimizations(
    domain: str | None = None,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
):
    query = db.query(Recommendation)
    if domain:
        query = query.filter(Recommendation.domain == domain)

    recommendations = query.order_by(Recommendation.estimated_monthly_savings.desc()).all()

    results = []
    for rec in recommendations:
        details = {}
        try:
            details = json.loads(rec.proposed_change)
        except Exception:
            details = {"recommendation": rec.proposed_change}

        results.append({
            "id": rec.id,
            "resource": rec.resource,
            "domain": rec.domain,
            "title": details.get("issue", rec.resource),
            "description": details.get("recommendation", rec.proposed_change),
            "estimated_savings_monthly": rec.estimated_monthly_savings,
            "current_cost_monthly": rec.current_monthly_cost,
            "confidence": rec.confidence,
            "status": rec.status,
            "created_at": rec.created_at.isoformat() if rec.created_at else None,
            "details": details,
        })

    return results


@router.get("/recommendations/{recommendation_id}")
def get_recommendation(
    recommendation_id: str,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
):
    rec = db.query(Recommendation).filter(Recommendation.id == recommendation_id).first()
    if not rec:
        raise HTTPException(status_code=404, detail="Recommendation not found")

    details = {}
    try:
        details = json.loads(rec.proposed_change)
    except Exception:
        details = {"recommendation": rec.proposed_change}

    return {
        "id": rec.id,
        "resource": rec.resource,
        "domain": rec.domain,
        "current_state": rec.current_state,
        "estimated_monthly_savings": rec.estimated_monthly_savings,
        "current_monthly_cost": rec.current_monthly_cost,
        "confidence": rec.confidence,
        "status": rec.status,
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
        "details": details,
    }
