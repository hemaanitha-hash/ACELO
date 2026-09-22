import json

from sqlalchemy.orm import Session

from models import Connection, Environment
from platforms.base import PlatformAdapter
from platforms.databricks import DatabricksAdapter
from platforms.fabric import FabricAdapter
from services.crypto import get_cipher

_ADAPTERS: dict[str, type[PlatformAdapter]] = {
    "databricks": DatabricksAdapter,
    "fabric": FabricAdapter,
}


def build_adapter(
    connection: Connection,
    db: Session | None = None,
    delegated_token: str | None = None,
) -> PlatformAdapter:
    """
    The one place in the codebase that turns a stored Connection into a live adapter.

    When a `db` session is supplied, resources registered by Step 3 provisioning
    are merged in as `{domain}_notebook_id`. This is what makes execution use the
    notebook ACELO actually deployed, rather than whatever legacy
    `notebook_item_id` happens to sit in the connection's auth_metadata.

    Without this, provisioning would register a real Fabric item ID that the
    execution path never reads — the deployed notebook would simply never run.
    """
    adapter_cls = _ADAPTERS.get(connection.platform)
    if not adapter_cls:
        raise ValueError(f"Unsupported platform: {connection.platform}")

    secret = None
    if connection.secret_encrypted:
        secret = get_cipher().decrypt(connection.secret_encrypted)

    auth_metadata = json.loads(connection.auth_metadata) if connection.auth_metadata else {}

    if db is not None:
        # A pipeline the user explicitly selected (Cluster Settings) wins over
        # the one ACELO setup registered; notebooks always come from provisioning.
        configured_pipelines = {k: v for k, v in auth_metadata.items() if k.endswith("_pipeline_id") and v}
        auth_metadata.update(_provisioned_resources(db, connection))
        auth_metadata.update(configured_pipelines)

    adapter = adapter_cls(
        endpoint=connection.endpoint, auth_metadata=auth_metadata, secret=secret
    )

    # A delegated environment authenticates ONLY with the user's token. Marking
    # it here stops a request without that token from falling back to the
    # service-principal flow (which then fails looking for client_id).
    if connection.auth_method == "delegated":
        adapter.delegated_mode = True

    # Delegated ("Microsoft Account") mode: the signed-in browser supplies the
    # user's Fabric token per request. It lives on the adapter for the duration
    # of this call only and is never written to the database or logged.
    if delegated_token:
        adapter.delegated_token = delegated_token
    return adapter


def _provisioned_resources(db: Session, connection: Connection) -> dict[str, str]:
    """Registered ACELO resources for the environment backed by this connection.
    Imported lazily to avoid a circular import at module load."""
    from services.provisioning_service import resolved_domains, resolved_pipelines, result_location

    environment = (
        db.query(Environment).filter(Environment.connection_id == connection.id).first()
    )
    if not environment:
        return {}

    resolved = {f"{domain}_notebook_id": item_id for domain, item_id in resolved_domains(db, environment).items()}
    resolved.update(
        {f"{domain}_pipeline_id": item_id for domain, item_id in resolved_pipelines(db, environment).items()}
    )
    resolved.update(result_location(db, environment))  # OneLake result reads
    if environment.id:
        resolved["environment_id"] = environment.id
    return resolved
