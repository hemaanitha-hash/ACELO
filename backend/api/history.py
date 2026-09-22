import json
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from api.deps import get_current_customer
from database import get_db
from models import AuditHistory, Customer

router = APIRouter(prefix="/api/history", tags=["history"])


@router.get("")
def get_audit_history(
    limit: int = 50,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
):
    entries = (
        db.query(AuditHistory)
        .filter(AuditHistory.customer_id == customer.id)
        .order_by(AuditHistory.created_at.desc())
        .limit(limit)
        .all()
    )

    results = []
    for entry in entries:
        payload = {}
        if entry.payload_json:
            try:
                payload = json.loads(entry.payload_json)
            except Exception:
                payload = {}

        results.append({
            "id": entry.id,
            "event_type": entry.event_type,
            "summary": entry.summary,
            "payload": payload,
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
        })

    return results
