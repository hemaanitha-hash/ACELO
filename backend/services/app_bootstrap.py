"""
Databricks App bootstrap.

When ACELO starts inside a Databricks App, the workspace is already decided by
the runtime. This module records that as an ordinary ACELO Environment so the
rest of the application keeps working unchanged: platform context, discovery,
the agent capability, approvals and run history all resolve through a
Connection row, and none of them need to know how ACELO authenticated.

What it deliberately does NOT do:

  * store a credential — `secret_encrypted` stays NULL. The adapter asks the
    App runtime for a header per call, so there is nothing to encrypt, nothing
    to leak to the browser, and nothing to rotate.
  * create a second authentication system — this reuses the existing
    Connection/Environment abstraction with the secret omitted.
  * touch Fabric — a Fabric connection in the same database is left exactly
    as it is.

It is idempotent: repeated starts update the host if the workspace URL changed
and otherwise do nothing.
"""

import logging
import uuid

from sqlalchemy.orm import Session

from models import Connection, Customer, Environment
from platforms import databricks_auth

logger = logging.getLogger(__name__)

# Marks the connection ACELO provisioned for itself, so it can be recognised on
# a later start and never confused with one a user configured by hand.
APP_AUTH_METHOD = "databricks_app"
APP_CONNECTION_NAME = "Databricks workspace"


def _default_customer(db: Session) -> Customer:
    customer = db.query(Customer).order_by(Customer.created_at).first()
    if customer:
        return customer
    customer = Customer(id=str(uuid.uuid4()), name="Databricks App")
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


def ensure_app_environment(db: Session) -> Environment | None:
    """
    Makes the App's own workspace the active Databricks environment.

    Returns the Environment, or None when ACELO is not running as a Databricks
    App — in which case nothing is created and the existing user-configured
    flow is untouched.
    """
    if not databricks_auth.is_app_runtime():
        return None

    host = databricks_auth.app_host()
    if not host:
        return None

    customer = _default_customer(db)

    connection = (
        db.query(Connection)
        .filter(
            Connection.customer_id == customer.id,
            Connection.platform == "databricks",
            Connection.auth_method == APP_AUTH_METHOD,
        )
        .first()
    )

    if connection is None:
        connection = Connection(
            id=str(uuid.uuid4()),
            customer_id=customer.id,
            platform="databricks",
            workspace=APP_CONNECTION_NAME,
            endpoint=host,
            auth_method=APP_AUTH_METHOD,
            auth_metadata="{}",
            # No credential is stored. The App identity supplies one per call.
            secret_encrypted=None,
            status="connected",
        )
        db.add(connection)
    else:
        # The App may have been moved to another workspace.
        connection.endpoint = host
        connection.status = "connected"

    db.commit()
    db.refresh(connection)

    environment = (
        db.query(Environment).filter(Environment.connection_id == connection.id).first()
    )
    if environment is None:
        environment = Environment(
            id=str(uuid.uuid4()),
            customer_id=customer.id,
            connection_id=connection.id,
            name=APP_CONNECTION_NAME,
            platform="databricks",
            auth_mode=APP_AUTH_METHOD,
            workspace_name=host.replace("https://", ""),
            status="connected",
        )
        db.add(environment)
    else:
        environment.workspace_name = host.replace("https://", "")
        environment.status = "connected"

    db.commit()
    db.refresh(environment)

    logger.info(
        "databricks_app_environment_ready env_id=%s workspace=%s auth=%s",
        environment.id,
        environment.workspace_name,
        APP_AUTH_METHOD,
    )
    return environment
