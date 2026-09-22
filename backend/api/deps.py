from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from models import Customer


def get_current_customer(
    x_customer_id: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> Customer:
    """
    No login in this version. Every request still resolves to a Customer row so
    the whole scope (connections, environments, runs, approvals, history) is
    customer-scoped from day one. When auth is added later, this function is the
    only place that changes: swap the header lookup for a validated session/JWT
    lookup.

    Isolation rule: if X-Customer-Id is supplied but does not resolve to a real
    customer, the request is REJECTED rather than silently falling back to the
    default customer. The previous fallback meant any unrecognised customer ID
    resolved to the first customer row, which let a caller read another
    customer's data just by sending an unknown ID. Only a request that omits the
    header entirely gets the development default.
    """
    if x_customer_id:
        customer = db.query(Customer).filter(Customer.id == x_customer_id).first()
        if customer:
            return customer
        # Deliberately 404, not 403 — this must not confirm whether an ID exists.
        raise HTTPException(status_code=404, detail="Customer not found")

    customer = db.query(Customer).order_by(Customer.created_at).first()
    if customer:
        return customer

    customer = Customer(name="Default Customer")
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer
