"""
ACTIVE PLATFORM CONTEXT — the single source of truth for which platform a
request belongs to.

Before this module, platform selection did not exist as a concept. The Sidebar's
platform dropdown was a hardcoded array in local component state that nothing
read, and the frontend resolved a connection by picking
`connections.find(status == "connected") ?? connections[0]` — an arbitrary
choice. With both a Fabric and a Databricks connection configured, that is a
coin flip, which is exactly how one platform's data leaked into the other's view.

The browser now sends the user's real selection on every request:

    X-Acelo-Platform:      "databricks" | "fabric"
    X-Acelo-Connection-Id: <connection id>

`active_context()` resolves those into a validated ActivePlatform, customer
scoped. Two rules it enforces:

  * the connection must belong to THIS customer (otherwise 404 — never
    confirming another customer's ids exist), and
  * the connection's own `platform` column must match the requested platform.
    That column is the discriminator: a Fabric connection can never be handed to
    a Databricks service, or the reverse, because the mismatch is a 409 rather
    than a silently-wrong credential.

Backwards compatibility: the headers are OPTIONAL. Without them the context
resolves the customer's connection as before, so existing callers and tests
keep working. When they ARE sent they are authoritative.
"""

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from api.deps import get_current_customer
from database import get_db
from models import Connection, Customer, Environment

# The two real platforms a user can be "in". "file" is an internal connection
# backing uploaded-file runs, never a platform the user selects.
PLATFORMS = ("databricks", "fabric")


@dataclass
class ActivePlatform:
    """
    Which platform this request operates in, and the connection backing it.

    `platform` is None only when the customer has configured nothing at all;
    callers that require a platform should say so rather than guess one.
    """

    platform: str | None
    connection: Connection | None
    environment: Environment | None
    # True when the caller actually stated the platform (or named a
    # connection). False when it was merely inferred from the customer's only
    # connection, which is a fallback — not a statement about where the user is.
    explicit: bool = False

    @property
    def connection_id(self) -> str | None:
        return self.connection.id if self.connection else None

    @property
    def environment_id(self) -> str | None:
        return self.environment.id if self.environment else None

    def requires(self, platform: str) -> None:
        """
        Guards a platform-specific endpoint. Calling a Databricks route while
        Fabric is active is a client bug, not something to satisfy with the
        wrong connection — so it is refused rather than served.
        """
        # Only an explicit selection may refuse a route. An inferred platform
        # must not turn "you have no Databricks environment" into a confusing
        # "wrong platform" error for a caller that never chose one.
        if self.explicit and self.platform and self.platform != platform:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"This operation is {platform}-only, but the active platform is "
                    f"{self.platform}. Switch platform in ACELO to continue."
                ),
            )

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "connection_id": self.connection_id,
            "connection_name": self.connection.workspace if self.connection else None,
            "environment_id": self.environment_id,
        }


def _environment_for(db: Session, connection: Connection | None) -> Environment | None:
    if not connection:
        return None
    return db.query(Environment).filter(Environment.connection_id == connection.id).first()


def active_context(
    x_acelo_platform: str | None = Header(default=None),
    x_acelo_connection_id: str | None = Header(default=None),
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
) -> ActivePlatform:
    """Resolves the active platform + connection for this request."""
    platform = (x_acelo_platform or "").strip().lower() or None
    if platform and platform not in PLATFORMS:
        raise HTTPException(status_code=400, detail=f"Unknown platform '{platform}'.")

    # An explicit connection id wins — it is what the user selected.
    if x_acelo_connection_id:
        connection = (
            db.query(Connection)
            .filter(
                Connection.id == x_acelo_connection_id,
                Connection.customer_id == customer.id,
            )
            .first()
        )
        if not connection:
            # 404 rather than 403: this must not confirm another customer's ids.
            raise HTTPException(status_code=404, detail="Connection not found")
        if platform and connection.platform != platform:
            # The isolation guarantee: the connection's own platform column is
            # the discriminator, so a mismatch is never resolved by guessing.
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Connection '{connection.workspace or connection.id}' is a "
                    f"{connection.platform} connection, but the active platform is {platform}."
                ),
            )
        return ActivePlatform(
            platform=connection.platform,
            connection=connection,
            environment=_environment_for(db, connection),
            explicit=True,
        )

    # Platform named without a specific connection: use that platform's own
    # connection. Never another platform's.
    candidates = (
        db.query(Connection)
        .filter(Connection.customer_id == customer.id, Connection.platform != "file")
        .order_by(Connection.created_at)
        .all()
    )
    if platform:
        matching = [c for c in candidates if c.platform == platform]
        connection = matching[0] if matching else None
        return ActivePlatform(
            platform=platform,
            connection=connection,
            environment=_environment_for(db, connection),
            explicit=True,
        )

    # No context supplied at all (a pre-context caller). Fall back to the single
    # connected connection, which is what the app did before this existed.
    connection = next((c for c in candidates if c.status == "connected"), None) or (
        candidates[0] if candidates else None
    )
    return ActivePlatform(
        platform=connection.platform if connection else None,
        connection=connection,
        environment=_environment_for(db, connection),
    )


def require_databricks(context: ActivePlatform = Depends(active_context)) -> ActivePlatform:
    """Dependency for Databricks-only routes."""
    context.requires("databricks")
    return context


def require_fabric(context: ActivePlatform = Depends(active_context)) -> ActivePlatform:
    """Dependency for Fabric-only routes."""
    context.requires("fabric")
    return context
