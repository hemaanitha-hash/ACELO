"""
Phase 1 environment lifecycle: create -> test connection -> discover -> ready.

This is the only module that turns an Environment row into a live platform
adapter and writes back the *real* result. Two rules it enforces everywhere:

  1. Status is never optimistic. "connected" is written only after the platform
     actually confirmed the configured workspace is readable.
  2. Nothing secret leaves this layer. Adapters get the decrypted secret; what
     goes back to the caller is metadata plus a safe error code/message.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from models import Connection, Environment, Resource
from platforms.base import PlatformAdapter, PlatformCapabilityNotImplemented
from platforms.databricks import DatabricksAdapter
from platforms.errors import ErrorCode, PlatformError
from platforms.file import FileAdapter
from platforms.fabric import FabricAdapter
from services.crypto import get_cipher

logger = logging.getLogger("acelo.environment")

_ADAPTERS: dict[str, type[PlatformAdapter]] = {
    "fabric": FabricAdapter,
    "databricks": DatabricksAdapter,
    "file": FileAdapter,
}

# Environment.status values
STATUS_NOT_CONFIGURED = "not_configured"
STATUS_CONNECTED = "connected"
STATUS_CONNECTION_FAILED = "connection_failed"
STATUS_ENVIRONMENT_READY = "environment_ready"
STATUS_DISCOVERY_FAILED = "discovery_failed"

# Authentication modes. "personal" is the pre-rename spelling of "user" and is
# normalised away on write, but still recognised on read for existing rows.
AUTH_MODE_SERVICE_PRINCIPAL = "service_principal"
AUTH_MODE_USER = "user"
_LEGACY_USER_ALIASES = {"personal"}


def normalize_auth_mode(mode: str | None) -> str:
    """Maps any recognised spelling onto the canonical value."""
    if not mode:
        return AUTH_MODE_SERVICE_PRINCIPAL
    if mode in _LEGACY_USER_ALIASES:
        return AUTH_MODE_USER
    return mode


def is_delegated(mode: str | None) -> bool:
    """True when this environment authenticates as the signed-in user."""
    return normalize_auth_mode(mode) == AUTH_MODE_USER

# Failures that mean "we could not authenticate / are not configured", as
# opposed to failures that happen after a successful sign-in. Readiness uses
# this to avoid reporting authentication as passing when it plainly did not.
AUTH_FAILURE_CODES = {
    ErrorCode.AUTHENTICATION_FAILED,
    ErrorCode.INVALID_CONFIGURATION,
    ErrorCode.NOT_CONFIGURED,
}


def _log(environment: Environment, operation: str, ok: bool, error_code: str | None = None) -> None:
    """Operational logging only: IDs, platform, outcome. Never credentials."""
    logger.info(
        "environment_operation env_id=%s customer_id=%s platform=%s operation=%s outcome=%s error_code=%s ts=%s",
        environment.id,
        environment.customer_id,
        environment.platform,
        operation,
        "success" if ok else "failure",
        error_code or "-",
        datetime.utcnow().isoformat(),
    )


def get_environment_for_customer(db: Session, environment_id: str, customer_id: str) -> Environment | None:
    """
    The single lookup used by every environment endpoint. Filtering on
    customer_id here is what prevents cross-customer access — callers must not
    query Environment directly.
    """
    return (
        db.query(Environment)
        .filter(Environment.id == environment_id, Environment.customer_id == customer_id)
        .first()
    )


def list_environments(db: Session, customer_id: str) -> list[Environment]:
    return (
        db.query(Environment)
        .filter(Environment.customer_id == customer_id)
        .order_by(Environment.created_at)
        .all()
    )


def create_environment(
    db: Session,
    customer_id: str,
    *,
    name: str,
    platform: str,
    auth_mode: str = AUTH_MODE_SERVICE_PRINCIPAL,
    tenant_id: str | None = None,
    workspace_id: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    endpoint: str | None = None,
) -> Environment:
    """
    Creates the Environment plus its credential-bearing Connection.

    The secret is encrypted via SecretCipher before it touches the DB and is
    never read back out except by build_adapter() on the server.
    """
    if platform not in _ADAPTERS:
        raise ValueError(f"Unsupported platform: {platform}")

    auth_metadata: dict[str, Any] = {}
    if tenant_id:
        auth_metadata["tenant_id"] = tenant_id
    if client_id:
        auth_metadata["client_id"] = client_id
    if workspace_id:
        auth_metadata["workspace_id"] = workspace_id

    resolved_endpoint = endpoint or ("https://api.fabric.microsoft.com/v1" if platform == "fabric" else "")

    connection = Connection(
        customer_id=customer_id,
        platform=platform,
        workspace=name,
        endpoint=resolved_endpoint,
        auth_method=("delegated" if is_delegated(auth_mode) else "service_principal")
        if platform == "fabric"
        else "pat",
        auth_metadata=json.dumps(auth_metadata),
        secret_encrypted=get_cipher().encrypt(client_secret) if client_secret else None,
        status="pending",
    )
    db.add(connection)
    db.flush()  # assign connection.id without a second round trip

    environment = Environment(
        customer_id=customer_id,
        connection_id=connection.id,
        name=name,
        platform=platform,
        auth_mode=normalize_auth_mode(auth_mode),
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        status=STATUS_NOT_CONFIGURED,
    )
    db.add(environment)
    db.commit()
    db.refresh(environment)
    _log(environment, "create", ok=True)
    return environment


def update_environment_credentials(
    db: Session,
    environment: Environment,
    *,
    name: str | None = None,
    tenant_id: str | None = None,
    workspace_id: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    endpoint: str | None = None,
) -> Environment:
    """
    Updates configuration in place. A blank client_secret means "leave the
    stored secret alone" so the UI never has to round-trip it.
    """
    connection = _connection_for(db, environment)

    if name:
        environment.name = name
        if connection:
            connection.workspace = name
    if tenant_id is not None:
        environment.tenant_id = tenant_id
    if workspace_id is not None:
        environment.workspace_id = workspace_id

    if connection:
        metadata = json.loads(connection.auth_metadata) if connection.auth_metadata else {}
        if tenant_id is not None:
            metadata["tenant_id"] = tenant_id
        if client_id is not None:
            metadata["client_id"] = client_id
        if workspace_id is not None:
            metadata["workspace_id"] = workspace_id
        connection.auth_metadata = json.dumps(metadata)
        if endpoint:
            connection.endpoint = endpoint
        if client_secret:
            connection.secret_encrypted = get_cipher().encrypt(client_secret)

    # Configuration changed, so any previous verdict is stale.
    environment.status = STATUS_NOT_CONFIGURED
    environment.last_error_code = None
    environment.last_error_message = None
    db.commit()
    db.refresh(environment)
    return environment


def _connection_for(db: Session, environment: Environment) -> Connection | None:
    if not environment.connection_id:
        return None
    return db.query(Connection).filter(Connection.id == environment.connection_id).first()


def build_adapter(
    db: Session, environment: Environment, delegated_token: str | None = None
) -> PlatformAdapter:
    """Turns an Environment into a live adapter. The one place secrets are decrypted."""
    adapter_cls = _ADAPTERS.get(environment.platform)
    if not adapter_cls:
        raise ValueError(f"Unsupported platform: {environment.platform}")

    connection = _connection_for(db, environment)
    secret = None
    auth_metadata: dict[str, Any] = {}
    endpoint = ""

    if connection:
        endpoint = connection.endpoint or ""
        auth_metadata = json.loads(connection.auth_metadata) if connection.auth_metadata else {}
        if connection.secret_encrypted:
            secret = get_cipher().decrypt(connection.secret_encrypted)

    # The Environment row is authoritative for identity fields — it is what the
    # user edited most recently.
    if environment.workspace_id:
        auth_metadata["workspace_id"] = environment.workspace_id
    if environment.tenant_id:
        auth_metadata["tenant_id"] = environment.tenant_id

    # Provisioned ACELO resources are the source of truth for which notebook
    # serves which domain. Registered Fabric item IDs override anything left in
    # the connection's legacy auth_metadata, so no notebook ID is ever hardcoded.
    # Imported here to avoid a circular import at module load.
    from services import resource_registry

    resource_registry.apply_to_adapter(db, environment, auth_metadata)

    adapter = adapter_cls(endpoint=endpoint, auth_metadata=auth_metadata, secret=secret)

    # Delegated environments never fall back to service-principal credentials.
    if is_delegated(environment.auth_mode) or (connection and connection.auth_method == "delegated"):
        adapter.delegated_mode = True

    # Delegated (personal) mode: the browser supplies the user's Fabric token per
    # request. It is attached to the adapter for the lifetime of this call only
    # and is never written to the database.
    if delegated_token:
        adapter.delegated_token = delegated_token
    return adapter


async def test_environment_connection(
    db: Session, environment: Environment, delegated_token: str | None = None
) -> dict[str, Any]:
    """
    Performs a REAL platform connection test and persists the real verdict.
    Returns a safe payload — no token, no secret, no stack trace.
    """
    adapter = build_adapter(db, environment, delegated_token)

    try:
        validation = await adapter.validate_connection()
    except PlatformCapabilityNotImplemented as exc:
        validation = None
        error_code = ErrorCode.NOT_CONFIGURED
        message = str(exc)
        log_detail = str(exc)
    except PlatformError as exc:
        validation = None
        error_code = exc.code
        message = exc.message
        log_detail = exc.log_detail
    except Exception as exc:  # noqa: BLE001 - unexpected failures must not leak upward as a 500 body
        validation = None
        error_code = ErrorCode.PLATFORM_API_UNAVAILABLE
        message = "An unexpected error occurred while contacting the platform."
        log_detail = repr(exc)

    if validation is not None and validation.connected:
        environment.status = STATUS_CONNECTED
        environment.workspace_name = validation.workspace_name
        if validation.workspace_id:
            environment.workspace_id = validation.workspace_id
        environment.last_error_code = None
        environment.last_error_message = None
        environment.last_verified_at = datetime.utcnow()
        connection = _connection_for(db, environment)
        if connection:
            connection.status = "connected"
            connection.last_error = None
        db.commit()
        db.refresh(environment)
        _log(environment, "test_connection", ok=True)
        return {
            "platform": environment.platform,
            "connected": True,
            "workspace_id": environment.workspace_id,
            "workspace_name": environment.workspace_name,
            "message": validation.message,
            "last_verified_at": environment.last_verified_at.isoformat(),
        }

    if validation is not None:
        error_code = validation.error_code or ErrorCode.AUTHENTICATION_FAILED
        message = validation.message
        log_detail = validation.message

    environment.status = STATUS_CONNECTION_FAILED
    environment.last_error_code = error_code
    environment.last_error_message = message
    connection = _connection_for(db, environment)
    if connection:
        connection.status = "failed"
        connection.last_error = message
    db.commit()
    db.refresh(environment)

    logger.warning(
        "environment_connection_failed env_id=%s customer_id=%s platform=%s error_code=%s detail=%s",
        environment.id,
        environment.customer_id,
        environment.platform,
        error_code,
        log_detail,
    )
    return {
        "platform": environment.platform,
        "connected": False,
        "error_code": error_code,
        "message": message,
    }


async def discover_environment(
    db: Session, environment: Environment, delegated_token: str | None = None
) -> dict[str, Any]:
    """
    Real workspace + resource discovery. Replaces the environment's cached
    Resource rows with exactly what the platform reported.
    """
    adapter = build_adapter(db, environment, delegated_token)

    try:
        workspace = await adapter.discover_workspace()
        discovered = await adapter.discover_resources()
    except PlatformCapabilityNotImplemented as exc:
        return _discovery_failure(db, environment, ErrorCode.NOT_CONFIGURED, str(exc), str(exc))
    except PlatformError as exc:
        return _discovery_failure(db, environment, exc.code, exc.message, exc.log_detail)
    except Exception as exc:  # noqa: BLE001
        return _discovery_failure(
            db,
            environment,
            ErrorCode.DISCOVERY_FAILED,
            "The workspace was reached, but its contents could not be listed.",
            repr(exc),
        )

    # Discovery output is a snapshot of real state, so plain discovered rows are
    # replaced wholesale. ACELO-managed registrations are NOT: they carry the
    # domain -> notebook mapping that execution resolves, which discovery does
    # not know about. Deleting them silently unconfigured every deployed
    # notebook, so any discovery run after provisioning broke execution.
    from services.provisioning_service import ACELO_OWNED

    managed = {
        resource.platform_resource_id: resource
        for resource in db.query(Resource)
        .filter(
            Resource.environment_id == environment.id,
            Resource.status == ACELO_OWNED,
        )
        .all()
    }

    db.query(Resource).filter(
        Resource.environment_id == environment.id,
        Resource.status != ACELO_OWNED,
    ).delete(synchronize_session=False)

    seen: set[str] = set()
    for item in discovered:
        existing = managed.get(item.platform_resource_id)
        if existing is not None:
            # Still present in the workspace: refresh only what discovery is
            # authoritative for, keeping the ACELO registration intact.
            existing.display_name = item.display_name
            seen.add(item.platform_resource_id)
            continue

        db.add(
            Resource(
                environment_id=environment.id,
                platform=environment.platform,
                resource_type=item.resource_type,
                display_name=item.display_name,
                platform_resource_id=item.platform_resource_id,
                status="discovered",
                detail_json=json.dumps(item.detail) if item.detail else None,
            )
        )

    # A managed item that discovery no longer sees has been deleted in the
    # workspace. Drop the registration so readiness reports it honestly as
    # missing rather than pointing execution at a notebook that is gone.
    for resource_id, resource in managed.items():
        if resource_id not in seen:
            db.delete(resource)

    environment.workspace_id = workspace.workspace_id
    environment.workspace_name = workspace.workspace_name
    environment.status = STATUS_ENVIRONMENT_READY
    environment.last_error_code = None
    environment.last_error_message = None
    environment.last_discovered_at = datetime.utcnow()
    db.commit()
    db.refresh(environment)
    _log(environment, "discover", ok=True)

    return {
        "workspace": {"id": workspace.workspace_id, "name": workspace.workspace_name},
        "items": [
            {
                "id": item.platform_resource_id,
                "display_name": item.display_name,
                "type": item.resource_type,
            }
            for item in discovered
        ],
        "counts": summarize_counts(discovered),
        # Per-resource-type outcome, where the adapter reports one. This is what
        # lets the UI distinguish "this type returned zero" from "this type
        # could not be read" — without it, a refused or unsupported endpoint
        # renders identically to an empty workspace.
        "resource_states": getattr(adapter, "last_discovery_states", []) or [],
        "discovered_at": environment.last_discovered_at.isoformat(),
    }


def _discovery_failure(
    db: Session, environment: Environment, code: str, message: str, log_detail: str
) -> dict[str, Any]:
    environment.status = STATUS_DISCOVERY_FAILED
    environment.last_error_code = code
    environment.last_error_message = message
    db.commit()
    db.refresh(environment)
    logger.warning(
        "environment_discovery_failed env_id=%s customer_id=%s platform=%s error_code=%s detail=%s",
        environment.id,
        environment.customer_id,
        environment.platform,
        code,
        log_detail,
    )
    return {"discovered": False, "error_code": code, "message": message}


def summarize_counts(resources: list) -> dict[str, int]:
    """Counts by normalised resource_type. Works for adapter results and ORM rows."""
    counts: dict[str, int] = {}
    for item in resources:
        key = item.resource_type
        counts[key] = counts.get(key, 0) + 1
    return counts
