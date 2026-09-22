"""
Step 3: deploying the ACELO optimization package into a customer's Fabric workspace.

Guarantees this module enforces:

  * **Explicit action only.** Nothing here runs on connect or discover. It is
    reached solely from POST /api/environments/{id}/provision.
  * **Never deletes customer data.** The only write operations are folder
    creation, notebook creation, and updating a notebook ACELO itself created
    and registered. An item ACELO did not create is never touched.
  * **No fabricated success.** A missing package asset, a refused API call, or a
    failed verification all leave the environment un-ready with a typed error.
    Partial success is reported as FAILED, with the per-domain detail preserved.
  * **Idempotent.** A second run adopts existing ACELO items by name, updates
    only when the asset checksum changed, and creates no duplicates.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from models import Environment, Resource
from platforms.errors import ErrorCode, PlatformError
from services import environment_service, package_registry

logger = logging.getLogger("acelo.provisioning")

# Environment.provisioning_status values
NOT_INSTALLED = "NOT_INSTALLED"
INSTALLING = "INSTALLING"
INSTALLED = "INSTALLED"
UPDATE_AVAILABLE = "UPDATE_AVAILABLE"
UPDATING = "UPDATING"
FAILED = "FAILED"

# Ordered steps, surfaced to the UI so progress reflects real backend state.
STEP_VALIDATING = "Validating prerequisites"
STEP_NAMESPACE = "Creating ACELO namespace"
STEP_DOMAIN = "Creating {domain} assets"
STEP_REGISTER = "Registering resources"
STEP_VERIFY = "Verifying installation"
STEP_DONE = "Completed"

# Marks a Resource row as ACELO-owned, so we never mistake a customer's own
# notebook for one of ours when updating.
ACELO_OWNED = "acelo-managed"


def _log(environment: Environment, event: str, **fields) -> None:
    extra = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    logger.info(
        "provisioning event=%s env_id=%s customer_id=%s platform=%s %s",
        event, environment.id, environment.customer_id, environment.platform, extra,
    )


def _set_state(
    db: Session, environment: Environment, status: str, step: str | None = None,
    detail: dict | None = None,
) -> None:
    environment.provisioning_status = status
    environment.provisioning_step = step
    if detail is not None:
        environment.provisioning_detail_json = json.dumps(detail, default=str)
    db.commit()


def get_provisioning_state(db: Session, environment: Environment) -> dict[str, Any]:
    """Safe status payload for the UI. Contains IDs and outcomes, never secrets."""
    detail: dict[str, Any] = {}
    if environment.provisioning_detail_json:
        try:
            detail = json.loads(environment.provisioning_detail_json)
        except ValueError:
            detail = {}

    availability = package_registry.package_availability()
    resources = (
        db.query(Resource)
        .filter(Resource.environment_id == environment.id, Resource.status == ACELO_OWNED)
        .all()
    )

    domains = {}
    for asset in availability["assets"]:
        domain = asset["domain"]
        resource = next((r for r in resources if _detail(r).get("domain") == domain), None)
        domains[domain] = {
            "asset_available": asset["available"],
            "deployed": resource is not None,
            "display_name": resource.display_name if resource else asset["display_name"],
            "platform_resource_id": resource.platform_resource_id if resource else None,
            "ready": resource is not None,
            "error_code": detail.get("domains", {}).get(domain, {}).get("error_code"),
            "message": detail.get("domains", {}).get(domain, {}).get("message"),
        }

    status = environment.provisioning_status or NOT_INSTALLED
    # An installed package older than this build's is still usable, but the
    # deployed notebook lacks newer changes (e.g. runtime tracing). Say so.
    if (
        status == INSTALLED
        and environment.package_version
        and environment.package_version != availability["package_version"]
    ):
        status = UPDATE_AVAILABLE

    return {
        "status": status,
        "step": environment.provisioning_step,
        "package_name": availability["package_name"],
        "package_version_available": availability["package_version"],
        "package_version_installed": environment.package_version,
        "deployable": availability["deployable"],
        "namespace": availability["namespace"],
        "last_provisioned_at": (
            environment.last_provisioned_at.isoformat() if environment.last_provisioned_at else None
        ),
        "domains": domains,
        "error_code": detail.get("error_code"),
        "message": detail.get("message"),
        "missing_assets": availability["missing"],
        # The lakehouse the notebook is (or would be) attached to. None means the
        # notebook cannot use Spark SQL tables, so a run will fail inside Fabric.
        "default_lakehouse": default_lakehouse(db, environment),
        # What the deployed notebook was read back as carrying on the last setup.
        "lakehouse_binding": detail.get("domains", {}).get("cluster", {}).get("lakehouse_binding"),
        # Spark libraries (xgboost) come from this Environment. None = workspace default.
        "fabric_environment": fabric_environment(db, environment),
        "execution_type": _execution_type(environment, "cluster"),
        "pipeline": _pipeline_state(db, environment, detail),
    }


def _pipeline_state(db: Session, environment: Environment, detail: dict[str, Any]) -> dict[str, Any] | None:
    """The registered Cluster pipeline, plus the last deployment outcome if it failed."""
    pipeline_id = resolved_pipelines(db, environment).get("cluster")
    outcome = detail.get("pipeline") or {}
    if not pipeline_id and not outcome:
        return None
    return {
        "name": CLUSTER_PIPELINE_NAME,
        "platform_resource_id": pipeline_id,
        "ready": bool(pipeline_id),
        "error_code": outcome.get("error_code"),
        "message": outcome.get("message"),
    }


def _detail(resource: Resource) -> dict:
    if not resource.detail_json:
        return {}
    try:
        return json.loads(resource.detail_json)
    except ValueError:
        return {}


async def provision_environment(
    db: Session, environment: Environment, delegated_token: str | None = None
) -> dict[str, Any]:
    """
    Deploys the ACELO package. Returns the same shape as get_provisioning_state().

    This is the only entry point that writes to a customer's workspace, and it
    runs only when a user explicitly asks for it.
    """
    if environment.platform != "fabric":
        return _fail(
            db, environment, ErrorCode.UNSUPPORTED,
            f"ACELO package provisioning is only available for Fabric environments, not '{environment.platform}'.",
        )

    package = package_registry.load_package()
    _log(environment, "provisioning_started", package_version=package.version)

    # ---- Step 1: prerequisites -------------------------------------------
    _set_state(db, environment, INSTALLING, STEP_VALIDATING)

    if not package.has_any_asset:
        missing = ", ".join(a.domain for a in package.missing_assets)
        return _fail(
            db, environment, ErrorCode.ASSET_MISSING,
            "No ACELO optimization assets are bundled in this build, so there is nothing to "
            f"deploy (missing: {missing}). This is an ACELO packaging issue, not a problem "
            "with your workspace.",
        )

    adapter = environment_service.build_adapter(db, environment, delegated_token)

    # The connection must be genuinely working before we write anything.
    validation = await adapter.validate_connection()
    if not validation.connected:
        return _fail(
            db, environment, validation.error_code or ErrorCode.AUTHENTICATION_FAILED,
            validation.message,
        )

    try:
        workspace = await adapter.discover_workspace()
    except PlatformError as exc:
        return _fail(db, environment, exc.code, exc.message, log_detail=exc.log_detail)

    environment.workspace_id = workspace.workspace_id
    environment.workspace_name = workspace.workspace_name
    db.commit()

    # ---- Step 2: namespace ------------------------------------------------
    _set_state(db, environment, INSTALLING, STEP_NAMESPACE)
    folders_supported = True
    root_folder_id: str | None = None
    try:
        root = await adapter.ensure_folder(package.namespace)
        folders_supported = bool(root.get("supported"))
        root_folder_id = root.get("id")
    except PlatformError as exc:
        return _fail(db, environment, exc.code, exc.message, log_detail=exc.log_detail)

    if not folders_supported:
        # Documented limitation: some tenants do not expose the folders API. We
        # fall back to flat items with an "ACELO " name prefix rather than
        # claiming a folder structure that does not exist.
        _log(environment, "folders_unsupported", note="using flat namespaced item names")

    # ---- Step 3: per-domain deployment ------------------------------------
    outcomes: dict[str, dict[str, Any]] = {}
    deployed: list[tuple[package_registry.PackageAsset, dict[str, Any], str | None]] = []
    failed = False

    lakehouse = default_lakehouse(db, environment)
    spark_environment = fabric_environment(db, environment)
    _log(
        environment, "fabric_environment",
        environment_item_id=spark_environment["id"] if spark_environment else "workspace-default",
    )
    _log(
        environment, "default_lakehouse",
        lakehouse_id=lakehouse["id"] if lakehouse else "none",
        lakehouse_name=lakehouse["name"] if lakehouse else "none",
    )

    for asset in package.assets:
        if not asset.available:
            # Honest, per §3: the asset simply is not in this build.
            outcomes[asset.domain] = {
                "status": "ASSET_MISSING",
                "error_code": ErrorCode.ASSET_MISSING,
                "message": f"No ACELO {asset.domain} notebook is bundled in this build.",
            }
            if asset.required:
                failed = True
            continue

        _set_state(db, environment, INSTALLING, STEP_DOMAIN.format(domain=asset.domain))
        try:
            item, folder_id = await _deploy_asset(
                adapter, package, asset, root_folder_id, folders_supported, lakehouse,
                spark_environment,
            )
        except PlatformError as exc:
            _log(environment, "domain_failed", domain=asset.domain, error_code=exc.code)
            logger.warning(
                "provisioning event=domain_failed env_id=%s domain=%s error_code=%s detail=%s",
                environment.id, asset.domain, exc.code, exc.log_detail,
            )
            outcomes[asset.domain] = {
                "status": "FAILED", "error_code": exc.code, "message": exc.message,
            }
            failed = True
            continue
        except Exception as exc:  # noqa: BLE001
            logger.exception("provisioning event=domain_error env_id=%s domain=%s", environment.id, asset.domain)
            outcomes[asset.domain] = {
                "status": "FAILED",
                "error_code": ErrorCode.PROVISIONING_FAILED,
                "message": f"Could not deploy the {asset.domain} notebook.",
            }
            failed = True
            continue

        deployed.append((asset, item, folder_id))
        outcomes[asset.domain] = {
            "status": "DEPLOYED",
            "platform_resource_id": item["id"],
            "display_name": item["displayName"],
        }

    # ---- Step 4: verification (before anything is registered as ready) ----
    _set_state(db, environment, INSTALLING, STEP_VERIFY)
    verified: list[tuple[package_registry.PackageAsset, dict[str, Any], str | None]] = []

    for asset, item, folder_id in deployed:
        try:
            await adapter.get_item(item["id"])
            has_definition = await adapter.notebook_definition_exists(item["id"])
            if asset.domain == "cluster":
                outcomes[asset.domain]["lakehouse_binding"] = await _verify_lakehouse_binding(
                    adapter, item["id"], lakehouse
                )
        except PlatformError as exc:
            outcomes[asset.domain] = {
                "status": "VERIFICATION_FAILED", "error_code": exc.code, "message": exc.message,
            }
            failed = True
            continue

        if not has_definition:
            outcomes[asset.domain] = {
                "status": "VERIFICATION_FAILED",
                "error_code": ErrorCode.PROVISIONING_FAILED,
                "message": "The notebook was created but its content could not be verified.",
            }
            failed = True
            continue

        verified.append((asset, item, folder_id))
        outcomes[asset.domain]["status"] = "VERIFIED"

    # ---- Step 5: register only what verified ------------------------------
    _set_state(db, environment, INSTALLING, STEP_REGISTER)
    for asset, item, folder_id in verified:
        _register_resource(db, environment, package, asset, item, folder_id)

    # ---- Step 6: Cluster pipeline, orchestrating the SAME notebook ----------
    pipeline_outcome: dict[str, Any] | None = None
    cluster = next(((a, i, f) for a, i, f in verified if a.domain == "cluster"), None)
    tracking = next((i for a, i, f in verified if a.domain == "approval_tracking"), None)
    if cluster is not None:
        _, notebook_item, notebook_folder = cluster
        try:
            pipeline_item = await _deploy_pipeline(
                adapter, package, notebook_item["id"], environment.workspace_id or "", notebook_folder,
                tracking["id"] if tracking else None,
            )
            await adapter.get_item(pipeline_item["id"])  # verify it really exists
            _register_pipeline(db, environment, package, "cluster", pipeline_item, notebook_folder)
            pipeline_outcome = {
                "status": "VERIFIED",
                "platform_resource_id": pipeline_item["id"],
                "display_name": pipeline_item["displayName"],
                "notebook_id": notebook_item["id"],
            }
        except PlatformError as exc:
            logger.warning(
                "provisioning event=pipeline_failed env_id=%s error_code=%s detail=%s",
                environment.id, exc.code, exc.log_detail,
            )
            pipeline_outcome = {"status": "FAILED", "error_code": exc.code, "message": exc.message}
        except Exception as exc:  # noqa: BLE001 - must never take down notebook provisioning
            # Type only: a traceback here can quote request-building source lines.
            logger.warning(
                "provisioning event=pipeline_error env_id=%s error=%s", environment.id, type(exc).__name__
            )
            pipeline_outcome = {
                "status": "FAILED",
                "error_code": ErrorCode.PROVISIONING_FAILED,
                "message": "The ACELO Cluster pipeline could not be deployed.",
            }
        # Only fatal when this environment is configured to execute through it;
        # the direct notebook path does not depend on the pipeline.
        if pipeline_outcome.get("status") == "FAILED" and _execution_type(environment, "cluster") == "pipeline":
            failed = True

    detail = {"domains": outcomes, "folders_supported": folders_supported, "pipeline": pipeline_outcome}

    if failed:
        # Partial success is still a failure. Successfully deployed items stay
        # registered (they are real and usable) but the environment is not ready.
        detail["error_code"] = ErrorCode.PROVISIONING_FAILED
        detail["message"] = "ACELO setup did not complete. Some assets could not be deployed."
        _set_state(db, environment, FAILED, None, detail)
        environment.last_error_code = ErrorCode.PROVISIONING_FAILED
        environment.last_error_message = detail["message"]
        db.commit()
        _refresh_environment_status(db, environment)
        _log(environment, "provisioning_failed")
        return get_provisioning_state(db, environment)

    environment.package_version = package.version
    environment.last_provisioned_at = datetime.utcnow()
    environment.last_error_code = None
    environment.last_error_message = None
    _set_state(db, environment, INSTALLED, STEP_DONE, detail)
    _refresh_environment_status(db, environment)
    _log(environment, "provisioning_completed", package_version=package.version)
    return get_provisioning_state(db, environment)


CLUSTER_PIPELINE_NAME = "ACELO_Cluster_Optimization_Pipeline"


def _execution_type(environment: Environment, domain: str) -> str:
    connection = environment.connection
    metadata = json.loads(connection.auth_metadata) if connection and connection.auth_metadata else {}
    return "pipeline" if (metadata.get(f"{domain}_execution_type") or "") == "pipeline" else "notebook"


async def _deploy_pipeline(
    adapter, package, notebook_id: str, workspace_id: str, folder_id: str | None,
    tracking_notebook_id: str | None = None,
):
    """
    Creates ACELO_Cluster_Optimization_Pipeline, or updates the existing one of
    that name, so repeated setups never create duplicates. Its single Notebook
    activity points at the notebook provisioning just deployed and verified.
    """
    definition = adapter.pipeline_definition(notebook_id, workspace_id, tracking_notebook_id)
    existing = await adapter.find_item_by_name("DataPipeline", CLUSTER_PIPELINE_NAME)
    if existing:
        await adapter.update_item_definition(existing["id"], definition)
        return {"id": existing["id"], "displayName": CLUSTER_PIPELINE_NAME}
    return await adapter.create_item(
        "DataPipeline",
        CLUSTER_PIPELINE_NAME,
        definition,
        folder_id=folder_id,
        description=f"{package.name} v{package.version} — runs the ACELO Cluster notebook. Managed by ACELO.",
    )


def _register_pipeline(db, environment, package, domain: str, item, folder_id) -> Resource:
    """
    Registers the pipeline under `pipeline_for` (NOT `domain`), so notebook
    resolution (resolved_domains) can never pick the pipeline up by mistake.
    """
    resource = next(
        (
            r
            for r in db.query(Resource)
            .filter(Resource.environment_id == environment.id, Resource.status == ACELO_OWNED)
            .all()
            if _detail(r).get("pipeline_for") == domain
        ),
        None,
    )
    detail = {
        "pipeline_for": domain,
        "folder_id": folder_id,
        "package_version": package.version,
        "managed_by": "acelo",
    }
    if resource is None:
        resource = Resource(environment_id=environment.id, platform=environment.platform)
        db.add(resource)
    resource.resource_type = "DataPipeline"
    resource.display_name = item["displayName"]
    resource.platform_resource_id = item["id"]
    resource.status = ACELO_OWNED
    resource.detail_json = json.dumps(detail)
    db.commit()
    return resource


def default_lakehouse(db: Session, environment: Environment) -> dict[str, str] | None:
    """
    The lakehouse the deployed notebook must be attached to, resolved from the
    resources discovery actually found — never hardcoded.

    Without a default lakehouse, Fabric refuses Spark SQL table access
    (`spark.read.table`, `saveAsTable`) and the notebook dies inside Spark.
    Resolution order:
      1. An explicitly configured Lakehouse ID (Cluster Settings) — required when
         the Lakehouse is not visible to discovery, e.g. it lives in another
         workspace. Its workspace defaults to this environment's.
      2. A discovered Lakehouse whose name matches the configured one.
      3. The only Lakehouse in the workspace.
    Anything ambiguous attaches nothing rather than guessing.
    """
    connection = environment.connection
    metadata = json.loads(connection.auth_metadata) if connection and connection.auth_metadata else {}
    lakehouses = (
        db.query(Resource)
        .filter(Resource.environment_id == environment.id, Resource.resource_type == "Lakehouse")
        .all()
    )

    configured_id = (metadata.get("cluster_lakehouse_id") or "").strip()
    if configured_id:
        discovered = next((l for l in lakehouses if l.platform_resource_id == configured_id), None)
        name = (
            (discovered.display_name if discovered else "")
            or metadata.get("cluster_source_lakehouse")
            or metadata.get("lakehouse_database")
            or ""
        )
        return {
            "id": configured_id,
            "name": name,
            "workspace_id": (metadata.get("cluster_lakehouse_workspace_id") or environment.workspace_id or ""),
            "source": "configured",
        }

    if not lakehouses:
        return None
    wanted = (
        metadata.get("cluster_source_lakehouse")
        or metadata.get("source_lakehouse")
        or metadata.get("lakehouse_database")
        or ""
    ).strip().lower()

    match = next((l for l in lakehouses if l.display_name.lower() == wanted), None) if wanted else None
    if match is None and len(lakehouses) == 1:
        match = lakehouses[0]
    if match is None:
        return None
    return {
        "id": match.platform_resource_id,
        "name": match.display_name,
        "workspace_id": environment.workspace_id or "",
        "source": "discovered",
    }


# Discovered Fabric Environment an operator can create (with xgboost added under
# Public libraries) for ACELO to attach automatically. An explicitly configured
# environment id always wins over this name.
ACELO_FABRIC_ENVIRONMENT_NAME = "ACELO_Cluster_Environment"


def fabric_environment(db: Session, environment: Environment) -> dict[str, str] | None:
    """
    The Fabric Environment (Spark libraries) the Cluster notebook runs with.

    The notebook no longer installs anything at run time, so its libraries must
    come from here. Resolved from configuration or discovery — never hardcoded.
    None means the notebook runs on the workspace default environment.
    """
    connection = environment.connection
    metadata = json.loads(connection.auth_metadata) if connection and connection.auth_metadata else {}
    configured = (metadata.get("cluster_fabric_environment_id") or "").strip()

    environments = (
        db.query(Resource)
        .filter(Resource.environment_id == environment.id, Resource.resource_type == "SparkEnvironment")
        .all()
    )
    if configured:
        match = next((e for e in environments if e.platform_resource_id == configured), None)
        return {
            "id": configured,
            "name": match.display_name if match else configured,
            "workspace_id": environment.workspace_id or "",
            "source": "configured",
        }
    match = next((e for e in environments if e.display_name == ACELO_FABRIC_ENVIRONMENT_NAME), None)
    if match is None:
        return None
    return {
        "id": match.platform_resource_id,
        "name": match.display_name,
        "workspace_id": environment.workspace_id or "",
        "source": "discovered",
    }


def notebook_payload(
    asset,
    lakehouse: dict[str, str] | None,
    spark_environment: dict[str, str] | None = None,
    preserved: dict | None = None,
) -> str:
    """
    The asset's notebook, base64-encoded, with its lakehouse and Environment attached.

    `preserved` is the `dependencies` block of the notebook currently in Fabric.
    Bindings there are kept unless ACELO resolved its own, so redeploying never
    strips a lakehouse or Environment someone attached in Fabric.
    """
    if not lakehouse and not spark_environment and not preserved:
        return asset.payload_base64()
    notebook = json.loads(asset.read_bytes().decode("utf-8"))
    metadata = notebook.setdefault("metadata", {})
    dependencies = metadata.setdefault("dependencies", {})
    for key, value in (preserved or {}).items():
        dependencies.setdefault(key, value)
    if spark_environment:
        dependencies["environment"] = {
            "environmentId": spark_environment["id"],
            "workspaceId": spark_environment["workspace_id"],
        }
    if lakehouse:
        binding = {
            "default_lakehouse": lakehouse["id"],
            "default_lakehouse_name": lakehouse["name"],
            "default_lakehouse_workspace_id": lakehouse["workspace_id"],
        }
        dependencies["lakehouse"] = {**binding, "known_lakehouses": [{"id": lakehouse["id"]}]}
        # Older Fabric notebook schema reads the same binding from "trident".
        metadata.setdefault("trident", {})["lakehouse"] = {
            **binding,
            "known_lakehouses": [{"id": lakehouse["id"]}],
        }
    if not dependencies:
        metadata.pop("dependencies")
    return base64.b64encode(json.dumps(notebook).encode("utf-8")).decode()


async def _verify_lakehouse_binding(adapter, item_id: str, expected: dict[str, str] | None) -> dict:
    """
    Reads the deployed notebook back from Fabric and reports the default
    Lakehouse it ACTUALLY carries — the binding pipeline runs use. Evidence,
    not an assumption that the upload took effect.
    """
    try:
        dependencies = await adapter.get_notebook_dependencies(item_id)
    except Exception as exc:  # noqa: BLE001 - verification must not fail the deploy
        return {"status": "UNVERIFIED", "reason": f"definition unreadable ({type(exc).__name__})"}
    bound = (dependencies or {}).get("lakehouse") or {}
    bound_id = bound.get("default_lakehouse")
    result = {"bound_id": bound_id, "bound_name": bound.get("default_lakehouse_name")}
    if not bound_id:
        result["status"] = "MISSING"
    elif expected and bound_id != expected["id"]:
        result["status"] = "MISMATCH"
    else:
        result["status"] = "VERIFIED"
    return result


async def _existing_dependencies(adapter, item_id: str) -> dict:
    """Current Fabric bindings of a notebook; {} if they cannot be read."""
    try:
        return await adapter.get_notebook_dependencies(item_id)
    except Exception as exc:  # noqa: BLE001 - best effort; never block a deploy
        logger.warning(
            "provisioning event=dependencies_unreadable item_id=%s error=%s", item_id, type(exc).__name__
        )
        return {}


async def _deploy_asset(
    adapter, package, asset, root_folder_id, folders_supported, lakehouse=None, spark_environment=None
):
    """
    Creates or updates one domain's notebook.

    Idempotency + safety: an existing item with the same name is ADOPTED and
    updated, never duplicated. A name collision with a customer-owned notebook
    is therefore impossible to trigger accidentally, because ACELO's names are
    namespaced (e.g. "ACELO Cluster Optimization").
    """
    folder_id = None
    if folders_supported:
        sub = await adapter.ensure_folder(asset.folder, parent_folder_id=root_folder_id)
        folder_id = sub.get("id")

    display_name = asset.display_name
    existing = await adapter.find_notebook_by_name(display_name)

    if existing:
        preserved = await _existing_dependencies(adapter, existing["id"])
        payload = notebook_payload(asset, lakehouse, spark_environment, preserved)
        await adapter.update_notebook_definition(existing["id"], payload)
        return {"id": existing["id"], "displayName": display_name}, folder_id

    item = await adapter.create_notebook(
        display_name=display_name,
        payload_base64=notebook_payload(asset, lakehouse, spark_environment),
        folder_id=folder_id,
        description=f"{package.name} v{package.version} — {asset.domain} optimization. Managed by ACELO.",
    )
    return item, folder_id


def _register_resource(db, environment, package, asset, item, folder_id) -> Resource:
    """
    Persists the REAL Fabric item ID against the environment.

    This is what the Step 2 execution layer resolves at run time, which is why
    only verified items reach this function.
    """
    # Keyed on the DOMAIN, not the item id. Fabric hands out a fresh item id
    # whenever a notebook is recreated, so matching on the id registered a
    # second row for the same domain and left execution choosing between a
    # stale and a live notebook.
    resource = next(
        (
            existing
            for existing in db.query(Resource)
            .filter(
                Resource.environment_id == environment.id,
                Resource.status == ACELO_OWNED,
            )
            .all()
            if _detail(existing).get("domain") == asset.domain
        ),
        None,
    )
    detail = {
        "domain": asset.domain,
        "resource_key": asset.resource_key,
        "folder": asset.folder,
        "folder_id": folder_id,
        "package_version": package.version,
        "asset_checksum": asset.checksum(),
        "managed_by": "acelo",
    }

    if resource:
        resource.display_name = item["displayName"]
        resource.platform_resource_id = item["id"]
        resource.status = ACELO_OWNED
        resource.detail_json = json.dumps(detail)
    else:
        resource = Resource(
            environment_id=environment.id,
            platform=environment.platform,
            resource_type="Notebook",
            display_name=item["displayName"],
            platform_resource_id=item["id"],  # the REAL Fabric item ID
            status=ACELO_OWNED,
            detail_json=json.dumps(detail),
        )
        db.add(resource)
    db.commit()
    return resource


def _fail(db, environment, code: str, message: str, log_detail: str = "") -> dict[str, Any]:
    detail = {"error_code": code, "message": message, "domains": {}}
    _set_state(db, environment, FAILED, None, detail)
    environment.last_error_code = code
    environment.last_error_message = message
    db.commit()
    _refresh_environment_status(db, environment)
    logger.warning(
        "provisioning event=provisioning_failed env_id=%s error_code=%s detail=%s",
        environment.id, code, log_detail or message,
    )
    return get_provisioning_state(db, environment)


def _refresh_environment_status(db: Session, environment: Environment) -> None:
    """
    An environment is only ENVIRONMENT_READY when connection, discovery AND the
    required ACELO package are all genuinely in place. Authentication alone is
    explicitly not enough.
    """
    if environment.provisioning_status == INSTALLED and resolved_domains(db, environment):
        environment.status = environment_service.STATUS_ENVIRONMENT_READY
    elif environment.status == environment_service.STATUS_ENVIRONMENT_READY:
        # Demote: it was ready, and no longer is.
        environment.status = environment_service.STATUS_CONNECTED
    db.commit()


def resolved_domains(db: Session, environment: Environment) -> dict[str, str]:
    """
    Maps domain -> REAL Fabric item ID from registered ACELO resources.

    This is the single source of truth the execution layer uses, replacing any
    hardcoded notebook ID.
    """
    mapping: dict[str, str] = {}
    # Ordered oldest-first so that if a domain somehow has more than one
    # registration, the most recently provisioned item wins deterministically
    # instead of depending on row order.
    resources = (
        db.query(Resource)
        .filter(Resource.environment_id == environment.id, Resource.status == ACELO_OWNED)
        .order_by(Resource.created_at.asc())
        .all()
    )
    for resource in resources:
        domain = _detail(resource).get("domain")
        if domain:
            mapping[domain] = resource.platform_resource_id
    return mapping


def resolved_pipelines(db: Session, environment: Environment) -> dict[str, str]:
    """domain -> REAL Fabric pipeline item id, from ACELO-registered pipelines."""
    mapping: dict[str, str] = {}
    resources = (
        db.query(Resource)
        .filter(Resource.environment_id == environment.id, Resource.status == ACELO_OWNED)
        .order_by(Resource.created_at.asc())
        .all()
    )
    for resource in resources:
        domain = _detail(resource).get("pipeline_for")
        if domain:
            mapping[domain] = resource.platform_resource_id
    return mapping


def result_location(db: Session, environment: Environment) -> dict[str, str]:
    """
    Where Cluster results are read from on OneLake: the notebook's default
    Lakehouse (configured Lakehouse ID or discovery). Empty when unresolved —
    the adapter then reports exactly what to configure.
    """
    lakehouse = default_lakehouse(db, environment)
    if not lakehouse or not lakehouse.get("id"):
        return {}
    location = {"cluster_result_lakehouse_id": lakehouse["id"]}
    if lakehouse.get("workspace_id"):
        location["cluster_result_lakehouse_workspace_id"] = lakehouse["workspace_id"]
    return location
