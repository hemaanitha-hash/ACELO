"""
Phase 1 API: customer environment connection, validation and discovery.

Every route resolves the environment through
`environment_service.get_environment_for_customer(db, environment_id, customer.id)`,
so an environment belonging to another customer is a 404 — never readable,
never testable, never discoverable.

No route here executes an optimization notebook. Discovery is read-only.
"""

import json
import re

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from api.deps import get_current_customer, require_resource_admin, resource_access
from database import get_db
from models import Connection, Customer, Environment, Resource
from schemas.environment import (
    ConnectionTestOut,
    DiscoveryOut,
    EnvironmentCreate,
    EnvironmentOut,
    EnvironmentUpdate,
    ProvisioningOut,
    ReadinessOut,
)
from services import resource_registry
from services import environment_service, job_service, package_registry, provisioning_service

router = APIRouter(prefix="/api/environments", tags=["environments"])


def fabric_access_token(
    x_fabric_access_token: str | None = Header(default=None),
) -> str | None:
    """
    Delegated Fabric token for "personal" authentication mode.

    The browser acquires this through MSAL and sends it per request. It is used
    for the duration of the call and then discarded: it is never written to the
    database, never logged, and no response model can carry it back out.

    Service-principal environments ignore this header entirely and keep using
    the backend's own client-credentials flow.
    """
    return x_fabric_access_token


def fabric_sql_token(
    x_fabric_sql_token: str | None = Header(default=None),
) -> str | None:
    """
    Delegated Entra token for the Lakehouse SQL analytics endpoint (audience
    https://database.windows.net/), used ONLY to read a run's result table in
    Microsoft Account environments. Same handling as the Fabric token: never
    stored, never logged, never echoed back.
    """
    return x_fabric_sql_token


def _get_or_404(db: Session, environment_id: str, customer: Customer) -> Environment:
    environment = environment_service.get_environment_for_customer(db, environment_id, customer.id)
    if not environment:
        # Deliberately 404 (not 403) so this cannot be used to probe for the
        # existence of another customer's environment IDs.
        raise HTTPException(status_code=404, detail="Environment not found")
    return environment


@router.post("", response_model=EnvironmentOut)
def create_environment(
    payload: EnvironmentCreate,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
):
    try:
        return environment_service.create_environment(
            db,
            customer.id,
            name=payload.name,
            platform=payload.platform,
            auth_mode=payload.auth_mode,
            tenant_id=payload.tenant_id,
            workspace_id=payload.workspace_id,
            client_id=payload.client_id,
            client_secret=payload.client_secret,
            endpoint=payload.endpoint,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("", response_model=list[EnvironmentOut])
def list_environments(
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
):
    return environment_service.list_environments(db, customer.id)


@router.get("/{environment_id}", response_model=EnvironmentOut)
def get_environment(
    environment_id: str,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
):
    return _get_or_404(db, environment_id, customer)


@router.patch("/{environment_id}", response_model=EnvironmentOut)
def update_environment(
    environment_id: str,
    payload: EnvironmentUpdate,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
):
    environment = _get_or_404(db, environment_id, customer)
    return environment_service.update_environment_credentials(
        db,
        environment,
        name=payload.name,
        tenant_id=payload.tenant_id,
        workspace_id=payload.workspace_id,
        client_id=payload.client_id,
        client_secret=payload.client_secret,
        endpoint=payload.endpoint,
    )


# Non-secret Cluster notebook settings a user may edit from the UI, and where
# execution (job_service.build_run_parameters) and result retrieval
# (FabricAdapter.get_run_result) already read them. Anything else is rejected.
_TABLE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-\[\] ]{0,255}$")
_SCHEMA_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_HOST_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,252}$")
_ITEM_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_EXECUTION_TYPE = re.compile(r"^(notebook|pipeline)$")
_CLUSTER_SETTINGS = {
    # field: (metadata key, validator)
    "source_table": ("cluster_source_table", _TABLE_NAME),
    "result_table": ("cluster_result_table", _TABLE_NAME),
    "source_lakehouse": ("cluster_source_lakehouse", _TABLE_NAME),
    "result_lakehouse": ("cluster_result_lakehouse", _TABLE_NAME),
    # Schema of a schema-enabled Lakehouse, e.g. "dbo": the notebook then reads
    # "dbo.<table>" from its attached lakehouse instead of "<lakehouse>.<table>".
    "source_schema": ("cluster_source_schema", _SCHEMA_NAME),
    "result_schema": ("cluster_result_schema", _SCHEMA_NAME),
    "lakehouse_database": ("lakehouse_database", _TABLE_NAME),
    # The Lakehouse item bound as the notebook's DEFAULT Lakehouse at setup. Needed
    # when discovery cannot see it (e.g. it lives in another workspace).
    "lakehouse_id": ("cluster_lakehouse_id", _ITEM_ID),
    "lakehouse_workspace_id": ("cluster_lakehouse_workspace_id", _ITEM_ID),
    "sql_endpoint": ("sql_endpoint", _HOST_NAME),
    "column_mapping": ("cluster_column_mapping", None),
    # Fabric Environment providing the notebook's Spark libraries (xgboost).
    "fabric_environment_id": ("cluster_fabric_environment_id", _ITEM_ID),
    # How ACELO starts the Cluster notebook: directly, or via a Fabric pipeline.
    "execution_type": ("cluster_execution_type", _EXECUTION_TYPE),
    # Delta table the approval-tracking step maintains (source of approval candidates).
    "approval_tracking_table": ("cluster_approval_tracking_table", _TABLE_NAME),
    # The pipeline to run when execution_type is "pipeline". Must be a real
    # pipeline in this workspace; blank = the one ACELO setup deployed.
    "pipeline_id": ("cluster_pipeline_id", _ITEM_ID),
}

# Values that are clearly placeholders, never real configuration. Rejected so a
# sample like "xxxx.datawarehouse.fabric.microsoft.com" can never be submitted
# to Fabric as if it were a real endpoint.
_PLACEHOLDER_WORDS = ("xxxx", "example.", "placeholder", "your-", "your_", "changeme", "<", ">")
_PLACEHOLDER_VALUES = {"0", "null", "none", "undefined", "n/a", "na", "-", "tbd", "todo"}
_ZERO_GUID = "00000000-0000-0000-0000-000000000000"


def _is_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    return (
        lowered in _PLACEHOLDER_VALUES
        or lowered == _ZERO_GUID
        or any(word in lowered for word in _PLACEHOLDER_WORDS)
    )


def _connection_for(db: Session, environment: Environment) -> Connection:
    connection = db.query(Connection).filter(Connection.id == environment.connection_id).first()
    if connection is None:
        raise HTTPException(
            status_code=409, detail="Save and test the connection before configuring Cluster settings."
        )
    return connection


def _metadata(connection: Connection | None) -> dict:
    if connection is None or not connection.auth_metadata:
        return {}
    return json.loads(connection.auth_metadata)


# Settings stored in the registry row; the rest stay connection-level (legacy SQL path).
_CONNECTION_LEVEL = {"sql_endpoint"}


def _read_cluster_settings(db: Session, environment: Environment) -> dict[str, str | None]:
    """The administrator's Cluster mapping; unset is None (never "", 0 or a sample).
    Values that come from backend configuration are not copied into the form."""
    config = resource_registry.stored(db, environment, "cluster")
    connection = db.query(Connection).filter(Connection.id == environment.connection_id).first()
    metadata = _metadata(connection)
    out: dict[str, str | None] = {}
    for field, (key, _) in _CLUSTER_SETTINGS.items():
        if field in _CONNECTION_LEVEL:
            out[field] = str(metadata[key]) if metadata.get(key) else None
        else:
            out[field] = config.get(field) or None
    return out


def _workspace_pipelines(db: Session, environment: Environment) -> list[dict]:
    """Fabric pipelines this environment knows about (discovered or ACELO-deployed)."""
    rows = (
        db.query(Resource)
        .filter(Resource.environment_id == environment.id, Resource.resource_type == "DataPipeline")
        .order_by(Resource.display_name)
        .all()
    )
    seen: dict[str, dict] = {}
    for row in rows:
        managed = row.status == provisioning_service.ACELO_OWNED
        entry = seen.setdefault(
            row.platform_resource_id,
            {"id": row.platform_resource_id, "name": row.display_name, "managed": managed},
        )
        entry["managed"] = entry["managed"] or managed
    return list(seen.values())


def cluster_execution(db: Session, environment: Environment) -> dict:
    """
    The execution path a Cluster run will ACTUALLY take — the same resolution
    the adapter uses: a configured pipeline wins, else the ACELO-deployed one.
    """
    config = resource_registry.configured(db, environment, "cluster")
    execution_type = "pipeline" if config.get("execution_type") == "pipeline" else "notebook"
    pipelines = {p["id"]: p for p in _workspace_pipelines(db, environment)}

    pipeline = None
    configured = config.get("pipeline_id")
    if configured:
        known = pipelines.get(configured)
        pipeline = {"id": configured, "name": known["name"] if known else None, "source": "configured"}
    else:
        registered = provisioning_service.resolved_pipelines(db, environment).get("cluster")
        if registered:
            known = pipelines.get(registered)
            pipeline = {"id": registered, "name": known["name"] if known else None, "source": "acelo-managed"}
    return {"execution_type": execution_type, "pipeline": pipeline}


def _cluster_missing(db: Session, environment: Environment) -> list[str]:
    missing = job_service.missing_required_configuration(
        "cluster", _domain_parameters(db, environment, "cluster")
    )
    execution = cluster_execution(db, environment)
    if execution["execution_type"] == "pipeline" and not execution["pipeline"]:
        missing.append("pipeline")
    return missing


@router.get("/{environment_id}/cluster-settings")
def get_cluster_settings(
    environment_id: str,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
):
    """The Cluster settings as persisted, plus the execution path they resolve to."""
    environment = _get_or_404(db, environment_id, customer)
    return {
        "settings": _read_cluster_settings(db, environment),
        "missing": _cluster_missing(db, environment),
        "execution": cluster_execution(db, environment),
        "pipelines": _workspace_pipelines(db, environment),
    }


@router.put("/{environment_id}/cluster-settings")
def put_cluster_settings(
    environment_id: str,
    payload: dict[str, str | None],
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
    _admin: dict = Depends(require_resource_admin),
):
    """
    Persists Cluster settings. Fields omitted are unchanged; an empty value
    clears (stored as absent, returned as null). Values are identifiers only —
    never credentials — and placeholders are refused rather than stored.
    """
    environment = _get_or_404(db, environment_id, customer)
    connection = _connection_for(db, environment)

    unknown = sorted(set(payload) - set(_CLUSTER_SETTINGS))
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown Cluster settings: {', '.join(unknown)}.")

    metadata = _metadata(connection)
    changes = _validated_changes(db, environment, payload, _CLUSTER_SETTINGS)
    for field, value in changes.items():
        if field in _CONNECTION_LEVEL:
            key = _CLUSTER_SETTINGS[field][0]
            if value:
                metadata[key] = value
            else:
                metadata.pop(key, None)
    connection.auth_metadata = json.dumps(metadata)
    # Everything else is Cluster configuration in the Environment Resource Registry.
    resource_registry.update(db, environment, "cluster",
                             {k: v for k, v in changes.items() if k not in _CONNECTION_LEVEL})
    db.commit()
    return get_cluster_settings(environment_id, db, customer)


def _validated_changes(db: Session, environment: Environment, payload: dict, fields: dict) -> dict[str, str | None]:
    """Validates identifiers; refuses placeholders. Empty value = clear."""
    changes: dict[str, str | None] = {}
    for field, raw in payload.items():
        key, pattern = fields[field]
        value = (raw or "").strip()
        if not value:
            changes[field] = None
            continue
        if _is_placeholder(value):
            raise HTTPException(
                status_code=422,
                detail=f"{field} '{value}' is a placeholder, not a real value. "
                "Leave it empty if it is not configured.",
            )
        if field == "column_mapping":
            try:
                parsed = json.loads(value)
            except ValueError:
                parsed = None
            if not isinstance(parsed, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
            ):
                raise HTTPException(
                    status_code=422,
                    detail='column_mapping must be a JSON object like {"source_column": "expected_column"}.',
                )
        elif not pattern.match(value):
            raise HTTPException(status_code=422, detail=f"{field} is not a valid name.")
        if field == "pipeline_id" and value not in {p["id"] for p in _workspace_pipelines(db, environment)}:
            raise HTTPException(
                status_code=422,
                detail="That pipeline was not found in this workspace. Run Discover Environment, "
                "then choose a pipeline from the list.",
            )
        changes[field] = value
    return changes


# Per-domain resource mapping (Environment Setup > Optimization Resources > Advanced).
_DOMAIN_SETTINGS = {
    "execution_type": ("execution_type", _EXECUTION_TYPE),
    "pipeline_id": ("pipeline_id", _ITEM_ID),
    "notebook_id": ("notebook_id", _ITEM_ID),
    "lakehouse_id": ("lakehouse_id", _ITEM_ID),
    "lakehouse_workspace_id": ("lakehouse_workspace_id", _ITEM_ID),
    "source_lakehouse": ("source_lakehouse", _TABLE_NAME),
    "result_lakehouse": ("result_lakehouse", _TABLE_NAME),
    "source_schema": ("source_schema", _SCHEMA_NAME),
    "result_schema": ("result_schema", _SCHEMA_NAME),
    "source_table": ("source_table", _TABLE_NAME),
    "result_table": ("result_table", _TABLE_NAME),
    "approval_tracking_table": ("approval_tracking_table", _TABLE_NAME),
    "column_mapping": ("column_mapping", None),
    "fabric_environment_id": ("fabric_environment_id", _ITEM_ID),
    "llm_key_vault_uri": ("llm_key_vault_uri", re.compile(r"^https://[A-Za-z0-9.\-]+/?$")),
    "llm_secret_name": ("llm_secret_name", re.compile(r"^[A-Za-z0-9\-]{1,127}$")),
    "llm_model_name": ("llm_model_name", _TABLE_NAME),
    "validation_batch_size": ("validation_batch_size", re.compile(r"^[1-9][0-9]{0,2}$")),
}


@router.get("/{environment_id}/optimization-resources")
def get_optimization_resources(
    environment_id: str,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
    access: dict = Depends(resource_access),
):
    """Per-domain status and mapping (identifiers only, never credentials)."""
    environment = _get_or_404(db, environment_id, customer)
    return {"domains": resource_registry.summary(db, environment), "pipelines": _workspace_pipelines(db, environment),
            "access": access}


@router.put("/{environment_id}/optimization-resources/{domain}")
def put_optimization_resources(
    environment_id: str,
    domain: str,
    payload: dict[str, str | None],
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
    _admin: dict = Depends(require_resource_admin),
):
    """Administrator resource mapping for ONE domain. Other domains are untouched."""
    environment = _get_or_404(db, environment_id, customer)
    if domain not in resource_registry.DOMAINS:
        raise HTTPException(status_code=404, detail=f"Unknown optimization domain: {domain}")
    unknown = sorted(set(payload) - set(_DOMAIN_SETTINGS))
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown {domain} resource settings: {', '.join(unknown)}.")
    changes = _validated_changes(db, environment, payload, _DOMAIN_SETTINGS)
    resource_registry.update(db, environment, domain, changes)
    return get_optimization_resources(environment_id, db, customer, _admin)


@router.delete("/{environment_id}")
def delete_environment(
    environment_id: str,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
):
    environment = _get_or_404(db, environment_id, customer)
    db.delete(environment)
    db.commit()
    return {"ok": True}


@router.post("/{environment_id}/test", response_model=ConnectionTestOut)
async def test_environment(
    environment_id: str,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
    delegated_token: str | None = Depends(fabric_access_token),
):
    """Performs a real platform connection test. Cannot return connected=True
    unless the platform actually confirmed access to the configured workspace."""
    environment = _get_or_404(db, environment_id, customer)
    return await environment_service.test_environment_connection(db, environment, delegated_token)


@router.post("/{environment_id}/discover", response_model=DiscoveryOut)
async def discover_environment(
    environment_id: str,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
    delegated_token: str | None = Depends(fabric_access_token),
):
    """Read-only discovery of the real workspace contents. Runs no notebooks."""
    environment = _get_or_404(db, environment_id, customer)
    return await environment_service.discover_environment(db, environment, delegated_token)


@router.get("/{environment_id}/resources")
def list_resources(
    environment_id: str,
    resource_type: str | None = None,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
):
    """Returns the resources persisted by the last discovery for this environment."""
    environment = _get_or_404(db, environment_id, customer)

    query = db.query(Resource).filter(Resource.environment_id == environment.id)
    if resource_type:
        query = query.filter(Resource.resource_type == resource_type)
    resources = query.order_by(Resource.resource_type, Resource.display_name).all()

    return {
        "environment_id": environment.id,
        "counts": environment_service.summarize_counts(resources),
        "resources": [
            {
                "id": r.id,
                "platform_resource_id": r.platform_resource_id,
                "display_name": r.display_name,
                "resource_type": r.resource_type,
                "status": r.status,
                "detail": json.loads(r.detail_json) if r.detail_json else None,
            }
            for r in resources
        ],
    }


def _domain_parameters(db: Session, environment, domain: str) -> dict[str, str]:
    """
    The notebook settings this domain's runs receive - from its own row in the
    Environment Resource Registry, exactly as execution builds them.
    """
    params = resource_registry.build_runtime_parameters(db, environment, domain, "-")
    params.pop("acelo_run_id", None)
    params.pop("environment_id", None)
    return params


@router.get("/{environment_id}/readiness", response_model=ReadinessOut)
def environment_readiness(
    environment_id: str,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
):
    """
    Derived entirely from persisted real outcomes. There is no way to report
    ready without a successful connection test, a successful discovery, AND a
    verified ACELO package installation.
    """
    environment = _get_or_404(db, environment_id, customer)

    verified = environment.last_verified_at is not None
    auth_blocked = environment.last_error_code in environment_service.AUTH_FAILURE_CODES

    connected = verified and not auth_blocked
    workspace_access = connected and bool(environment.workspace_name)
    discovered = bool(environment.last_discovered_at)

    state = provisioning_service.get_provisioning_state(db, environment)
    domains = state["domains"]
    cluster_ready = bool(domains.get("cluster", {}).get("ready"))
    query_ready = bool(domains.get("query", {}).get("ready"))
    storage_ready = bool(domains.get("storage", {}).get("ready"))

    # Cluster is the minimum required capability: an environment with no
    # deployed optimization notebook cannot run any analysis.
    package_installed = state["status"] in (
        provisioning_service.INSTALLED,
        provisioning_service.UPDATE_AVAILABLE,  # installed, just not the newest build
    )

    # Cluster readiness is evaluated on its own. The Query and Storage
    # notebooks are separate deployments that Cluster never calls into, so a
    # failure there must not stop a Cluster run. Overall ready_for_analysis is
    # deliberately left strict.
    cluster_missing = _cluster_missing(db, environment)
    cluster_configured = not cluster_missing

    cluster_blocked_reason = None
    if not connected:
        cluster_blocked_reason = "Not connected to the workspace."
    elif not discovered:
        cluster_blocked_reason = "Workspace contents have not been discovered yet."
    elif not cluster_ready:
        cluster_blocked_reason = "The Cluster optimization notebook is not deployed."
    elif cluster_missing:
        cluster_blocked_reason = (
            "Missing Cluster settings: " + ", ".join(cluster_missing) + "."
        )

    return ReadinessOut(
        cluster_ready_for_analysis=(
            connected and workspace_access and discovered and cluster_ready and cluster_configured
        ),
        cluster_configured=cluster_configured,
        cluster_blocked_reason=cluster_blocked_reason,
        authentication=connected,
        workspace_access=workspace_access,
        environment_discovery=discovered,
        package_status=state["status"],
        cluster_ready=cluster_ready,
        query_ready=query_ready,
        storage_ready=storage_ready,
        ready_for_analysis=(
            connected and workspace_access and discovered and package_installed and cluster_ready
        ),
        status=environment.status,
    )


@router.get("/package/availability")
def package_availability():
    """What this ACELO build can deploy. Reports missing assets explicitly rather
    than quietly offering fewer capabilities."""
    return package_registry.package_availability()


@router.post("/{environment_id}/provision", response_model=ProvisioningOut)
async def provision_environment(
    environment_id: str,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
    delegated_token: str | None = Depends(fabric_access_token),
):
    """
    Deploys the ACELO optimization package into the customer's workspace.

    This is the ONLY route that writes to a customer workspace, and it runs only
    on explicit user action — connecting or discovering never triggers it.
    """
    environment = _get_or_404(db, environment_id, customer)

    if environment.provisioning_status in (
        provisioning_service.INSTALLING,
        provisioning_service.UPDATING,
    ):
        # Already running: report current state rather than starting a second
        # concurrent deployment into the same workspace.
        return provisioning_service.get_provisioning_state(db, environment)

    return await provisioning_service.provision_environment(db, environment, delegated_token)


@router.get("/{environment_id}/provision/status", response_model=ProvisioningOut)
def provisioning_status(
    environment_id: str,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
):
    environment = _get_or_404(db, environment_id, customer)
    return provisioning_service.get_provisioning_state(db, environment)
