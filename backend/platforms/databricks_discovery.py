"""
Read-only Databricks compute capability discovery.

Scope of this phase: discover what exists. Nothing here creates, starts, stops,
resizes or terminates a Databricks resource, and nothing here reads
`system.billing`. Every call is a GET.

Partial-failure rule (the core contract): each resource type is discovered
independently and its failure is captured as a `ResourceTypeStatus`, never
raised. A token scoped to clusters+sql still gets its clusters and warehouses
even though the serverless probe is refused. One type is never allowed to
collapse the whole request into a single top-level AUTHORIZATION/PERMISSION
error — that is exactly the failure mode this module exists to prevent.

Classification, per resource type:
    200  -> OK
    403  -> UNAVAILABLE / INSUFFICIENT_PERMISSIONS
    404  -> UNAVAILABLE / NOT_EXPOSED
            (serverless: NOT_EXPOSED_BY_WORKSPACE_API — it has no single
             documented list endpoint, so a 404 means this workspace does not
             surface it rather than "the endpoint is missing")
    else -> ERROR (401, 5xx, timeout, unreachable)

Every call is also recorded as a `DiscoveryProbe` carrying the method, path,
HTTP status and the platform's own error_code/message — never a request header,
so a PAT cannot reach the record.

Databricks distinction this module respects:
  * classic clusters come from /api/2.0/clusters/list
  * SQL warehouses come from /api/2.0/sql/warehouses
  * serverless compute is neither of those, and is NOT in system.compute.clusters
    (Databricks documents that compute system tables exclude serverless compute
    and SQL warehouses), so it is probed separately and never synthesised from
    the cluster list.
"""

import logging
import os
from typing import Any

from platforms.databricks_resources import (
    AceloResource,
    DiscoveryProbe,
    FailureKind,
    ResourceDiscovery,
    ResourceStatus,
    ResourceType,
    ResourceTypeStatus,
    UnavailableReason,
    normalize_classic_cluster,
    normalize_serverless_compute,
    normalize_sql_warehouse,
)
from platforms.errors import ErrorCode, PlatformError

logger = logging.getLogger(__name__)

CLUSTERS_PATH = "/api/2.0/clusters/list"
WAREHOUSES_PATH = "/api/2.0/sql/warehouses"

# Serverless compute: how ACELO actually finds it.
#
# "Default Interactive Compute" and "Default Automated Compute" are Serverless
# Compute Access Control objects (GA 2026-08-03). They are workspace permission
# objects that gate who may run serverless notebooks (interactive) and serverless
# jobs/pipelines (automated) — they are NOT clusters and NOT warehouses.
#
# Databricks exposes them through the Permissions API under the object type
# `serverless-compute`:
#
#     GET /api/2.0/permissions/serverless-compute/{id}   -> 200
#
# Critically, there is NO public REST endpoint that LISTS these objects. Their
# ids are per-workspace UUIDs, and the only enumeration Databricks offers is an
# internal GraphQL call requiring browser session auth — unusable from a PAT.
#
# So ACELO cannot discover them unaided, and does not pretend to. An operator
# supplies the ids (read from the object's URL in the Compute UI) via
#
#     ACELO_DATABRICKS_SERVERLESS_IDS="<uuid>=Default Interactive Compute,<uuid>=Default Automated Compute"
#
# and ACELO then CONFIRMS each one against the real API. The name is optional
# and is only a label for an id the operator already has; nothing is invented.
# With nothing configured, the status is NOT_EXPOSED_BY_WORKSPACE_API — the
# honest answer, never an empty list presented as "you have none".
SERVERLESS_PERMISSIONS_PATH = "/api/2.0/permissions/serverless-compute/{id}"

SERVERLESS_NOT_LISTABLE_MESSAGE = (
    "Databricks does not expose an API that lists serverless compute objects "
    "(Default Interactive Compute / Default Automated Compute). Set "
    "ACELO_DATABRICKS_SERVERLESS_IDS to their workspace ids to have ACELO "
    "confirm them through the Permissions API."
)

_FAILURE_KIND_BY_STATUS = {401: FailureKind.AUTHENTICATION, 403: FailureKind.AUTHORIZATION, 404: FailureKind.NOT_FOUND}

# Non-HTTP failures (no status code) still need a reason on the status entry.
_REASON_BY_ERROR_CODE = {
    ErrorCode.AUTHENTICATION_FAILED: UnavailableReason.AUTHENTICATION_FAILED,
    ErrorCode.PLATFORM_API_UNAVAILABLE: UnavailableReason.PLATFORM_API_UNAVAILABLE,
    ErrorCode.TIMEOUT: UnavailableReason.TIMEOUT,
    ErrorCode.NOT_CONFIGURED: UnavailableReason.NOT_CONFIGURED,
}


def serverless_compute_targets() -> list[tuple[str, str | None]]:
    """
    The serverless compute objects this workspace's operator has identified, as
    (id, optional label) pairs from ACELO_DATABRICKS_SERVERLESS_IDS.

    Format: "<uuid>=Default Interactive Compute,<uuid>=Default Automated Compute"
    The label is optional; "<uuid>,<uuid>" is equally valid.
    """
    raw = os.getenv("ACELO_DATABRICKS_SERVERLESS_IDS", "")
    targets: list[tuple[str, str | None]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        identifier, _, label = entry.partition("=")
        identifier = identifier.strip()
        if identifier:
            targets.append((identifier, label.strip() or None))
    return targets


def _failure_kind(exc: PlatformError) -> str:
    return _FAILURE_KIND_BY_STATUS.get(exc.status_code or 0, FailureKind.OTHER)


def _probe_for(resource_type: str, path: str, exc: PlatformError) -> DiscoveryProbe:
    return DiscoveryProbe(
        resource_type=resource_type,
        method="GET",
        path=path,
        status_code=exc.status_code,
        ok=False,
        failure_kind=_failure_kind(exc),
        platform_error_code=exc.platform_error_code,
        platform_message=exc.platform_message,
    )


def _status_for(resource_type: str, exc: PlatformError) -> ResourceTypeStatus:
    """
    Turns one failed call into this resource type's status.

    403 and 404 are UNAVAILABLE — asked and answered. Everything else is ERROR.
    Neither ever propagates: the caller records it and moves to the next type.
    """
    status_code = exc.status_code

    if status_code == 403:
        status, reason = ResourceStatus.UNAVAILABLE, UnavailableReason.INSUFFICIENT_PERMISSIONS
    elif status_code == 404:
        reason = (
            UnavailableReason.NOT_EXPOSED_BY_WORKSPACE_API
            if resource_type == ResourceType.SERVERLESS_COMPUTE
            else UnavailableReason.NOT_EXPOSED
        )
        status = ResourceStatus.UNAVAILABLE
    else:
        status = ResourceStatus.ERROR
        reason = _REASON_BY_ERROR_CODE.get(exc.code, UnavailableReason.DISCOVERY_FAILED)

    # exc.log_detail can contain the raw platform response, so it goes to the
    # server log only. exc.message is the already-sanitised user-facing text.
    logger.info(
        "databricks_discovery_failed resource_type=%s status=%s error_code=%s "
        "platform_error_code=%s outcome=%s reason=%s detail=%s",
        resource_type,
        status_code,
        exc.code,
        exc.platform_error_code,
        status,
        reason,
        exc.log_detail,
    )
    return ResourceTypeStatus(
        resource_type=resource_type,
        status=status,
        reason=reason,
        message=exc.message,
    )


def _extract_list(body: Any) -> list[dict[str, Any]]:
    """Pulls the object list out of a probe response of unknown shape."""
    if isinstance(body, list):
        return [item for item in body if isinstance(item, dict)]
    if not isinstance(body, dict):
        return []
    for key in _SERVERLESS_LIST_KEYS:
        value = body.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


async def _discover_simple(
    adapter: Any,
    resource_type: str,
    path: str,
    list_key: str,
    normalize: Any,
) -> tuple[list[AceloResource], ResourceTypeStatus, list[DiscoveryProbe]]:
    """
    One GET, one resource type. Shared by clusters and warehouses, whose APIs
    are both a single list endpoint.

    An empty list from a 200 is OK-with-zero — genuinely "none exist", which is
    a different answer from UNAVAILABLE.
    """
    try:
        body = await adapter._get_json(path)
    except PlatformError as exc:
        return [], _status_for(resource_type, exc), [_probe_for(resource_type, path, exc)]

    raw_items = body.get(list_key, []) if isinstance(body, dict) else []
    resources = [normalize(item) for item in raw_items if isinstance(item, dict)]
    probe = DiscoveryProbe(resource_type=resource_type, method="GET", path=path, status_code=200, ok=True)
    return resources, ResourceTypeStatus(resource_type, ResourceStatus.OK), [probe]


async def _discover_classic_clusters(adapter: Any):
    """Lists classic all-purpose and job clusters."""
    return await _discover_simple(
        adapter, ResourceType.CLASSIC_CLUSTER, CLUSTERS_PATH, "clusters", normalize_classic_cluster
    )


async def _discover_sql_warehouses(adapter: Any):
    return await _discover_simple(
        adapter, ResourceType.SQL_WAREHOUSE, WAREHOUSES_PATH, "warehouses", normalize_sql_warehouse
    )


async def _discover_serverless_compute(
    adapter: Any,
) -> tuple[list[AceloResource], ResourceTypeStatus, list[DiscoveryProbe]]:
    """
    Confirms the workspace's serverless compute objects through the Permissions
    API. Classified as SERVERLESS_COMPUTE — never folded into CLASSIC_CLUSTER,
    because an access-control object for serverless workloads is a different
    thing from a cluster and has none of a cluster's sizing levers.

    Databricks provides no endpoint that lists these objects, so ACELO confirms
    the ids the operator configured. With none configured it reports
    NOT_EXPOSED_BY_WORKSPACE_API rather than an empty list.
    """
    targets = serverless_compute_targets()
    if not targets:
        return (
            [],
            ResourceTypeStatus(
                resource_type=ResourceType.SERVERLESS_COMPUTE,
                status=ResourceStatus.UNAVAILABLE,
                reason=UnavailableReason.NOT_EXPOSED_BY_WORKSPACE_API,
                message=SERVERLESS_NOT_LISTABLE_MESSAGE,
            ),
            [],
        )

    resources: list[AceloResource] = []
    probes: list[DiscoveryProbe] = []
    failures: list[PlatformError] = []

    for identifier, label in targets:
        path = SERVERLESS_PERMISSIONS_PATH.format(id=identifier)
        try:
            body = await adapter._get_json(path)
        except PlatformError as exc:
            failures.append(exc)
            probes.append(_probe_for(ResourceType.SERVERLESS_COMPUTE, path, exc))
            continue

        probes.append(
            DiscoveryProbe(
                resource_type=ResourceType.SERVERLESS_COMPUTE,
                method="GET",
                path=path,
                status_code=200,
                ok=True,
            )
        )

        # The permissions response carries no display name, so the label is the
        # operator's own and the id is what Databricks confirmed.
        raw: dict[str, Any] = {"id": identifier}
        if label:
            raw["name"] = label
        if isinstance(body, dict):
            for key in ("object_id", "object_type"):
                if body.get(key):
                    raw[key] = body[key]
            acl = body.get("access_control_list")
            if isinstance(acl, list):
                raw["access_control_entries"] = len(acl)
        resources.append(normalize_serverless_compute(raw, source=path))

    if resources:
        return resources, ResourceTypeStatus(ResourceType.SERVERLESS_COMPUTE, ResourceStatus.OK), probes

    # Every configured id failed. Prefer a non-404: a 403 (not permitted) is
    # more actionable than "that id does not exist here".
    informative = next((f for f in failures if f.status_code != 404), failures[0])
    return [], _status_for(ResourceType.SERVERLESS_COMPUTE, informative), probes


async def discover_compute_resources(adapter: Any) -> ResourceDiscovery:
    """
    Discovers every compute resource type this identity can see.

    Discovery of one type never aborts the others: an unreachable type becomes
    an UNAVAILABLE/ERROR status entry alongside the types that succeeded, and
    the request as a whole still succeeds.
    """
    discovery = ResourceDiscovery()

    try:
        adapter._require_config()
    except PlatformError as exc:
        # Nothing can be discovered without an endpoint and a token — report
        # that once per type rather than raising a 500.
        for resource_type in (
            ResourceType.CLASSIC_CLUSTER,
            ResourceType.SERVERLESS_COMPUTE,
            ResourceType.SQL_WAREHOUSE,
        ):
            discovery.statuses.append(
                ResourceTypeStatus(
                    resource_type=resource_type,
                    status=ResourceStatus.ERROR,
                    reason=UnavailableReason.NOT_CONFIGURED,
                    message=exc.message,
                )
            )
        return discovery

    for discover in (_discover_classic_clusters, _discover_serverless_compute, _discover_sql_warehouses):
        # A bug in one pass must not take the others down either, so even an
        # unexpected exception is contained to its own resource type.
        try:
            resources, status, probes = await discover(adapter)
        except Exception as exc:  # noqa: BLE001 - one type's fault stays its own
            resource_type = getattr(discover, "_resource_type", "UNKNOWN")
            logger.exception("databricks_discovery_unexpected_error resource_type=%s", resource_type)
            resources, probes = [], []
            status = ResourceTypeStatus(
                resource_type=resource_type,
                status=ResourceStatus.ERROR,
                reason=UnavailableReason.DISCOVERY_FAILED,
                message="This resource type could not be discovered.",
            )
        discovery.resources.extend(resources)
        discovery.statuses.append(status)
        discovery.probes.extend(probes)

    return discovery


# Used only by the containment handler above to name the type that misbehaved.
_discover_classic_clusters._resource_type = ResourceType.CLASSIC_CLUSTER  # type: ignore[attr-defined]
_discover_serverless_compute._resource_type = ResourceType.SERVERLESS_COMPUTE  # type: ignore[attr-defined]
_discover_sql_warehouses._resource_type = ResourceType.SQL_WAREHOUSE  # type: ignore[attr-defined]


# --- per-resource-type discovery state (for Environment Discovery) -----------
#
# The six states below exist to keep an API failure from being rendered as "this
# workspace is empty". "We asked and got zero" (SUCCESS_EMPTY) and "we could not
# ask" (AUTHORIZATION_FAILED / NOT_SUPPORTED / API_ERROR) are different answers,
# and only the first one means empty.


class DiscoveryState:
    SUCCESS_WITH_RESOURCES = "SUCCESS_WITH_RESOURCES"
    SUCCESS_EMPTY = "SUCCESS_EMPTY"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    AUTHORIZATION_FAILED = "AUTHORIZATION_FAILED"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    API_ERROR = "API_ERROR"


_STATE_BY_HTTP_STATUS = {
    401: DiscoveryState.AUTHENTICATION_FAILED,
    403: DiscoveryState.AUTHORIZATION_FAILED,
    404: DiscoveryState.NOT_SUPPORTED,
}

# When no HTTP call was made, the status reason decides the state.
_STATE_BY_REASON = {
    UnavailableReason.NOT_EXPOSED_BY_WORKSPACE_API: DiscoveryState.NOT_SUPPORTED,
    UnavailableReason.NOT_EXPOSED: DiscoveryState.NOT_SUPPORTED,
    UnavailableReason.INSUFFICIENT_PERMISSIONS: DiscoveryState.AUTHORIZATION_FAILED,
    UnavailableReason.AUTHENTICATION_FAILED: DiscoveryState.AUTHENTICATION_FAILED,
    UnavailableReason.NOT_CONFIGURED: DiscoveryState.NOT_SUPPORTED,
}


def discovery_states(discovery: ResourceDiscovery) -> list[dict[str, Any]]:
    """
    Collapses a ResourceDiscovery into one explicit state per resource type,
    and logs the diagnostic line for each: resource type, method, path, HTTP
    status, resource count, and the platform's error message.

    Reads only what discovery already recorded — it makes no extra API call and
    never touches a request header, so no token can reach the log.
    """
    states: list[dict[str, Any]] = []

    for status in discovery.statuses:
        resource_type = status.resource_type
        count = len(discovery.of_type(resource_type))
        probes = discovery.probes_of(resource_type)
        # The probe that decided the outcome: the successful one if any,
        # otherwise the last failure.
        probe = next((p for p in probes if p.ok), probes[-1] if probes else None)

        if status.status == ResourceStatus.OK:
            state = (
                DiscoveryState.SUCCESS_WITH_RESOURCES if count > 0 else DiscoveryState.SUCCESS_EMPTY
            )
        elif probe is not None and probe.status_code in _STATE_BY_HTTP_STATUS:
            state = _STATE_BY_HTTP_STATUS[probe.status_code]
        elif status.reason in _STATE_BY_REASON:
            # No HTTP call was made at all — a type Databricks publishes no way
            # to list. That is "not supported", not an API error.
            state = _STATE_BY_REASON[status.reason]
        else:
            state = DiscoveryState.API_ERROR

        entry = {
            "resource_type": resource_type,
            "state": state,
            "resource_count": count,
            "method": probe.method if probe else "GET",
            "path": probe.path if probe else None,
            "status_code": probe.status_code if probe else None,
            "platform_error_code": probe.platform_error_code if probe else None,
            "platform_message": probe.platform_message if probe else None,
        }
        states.append(entry)

        logger.info(
            "databricks_discovery_state resource_type=%s state=%s method=%s path=%s "
            "http_status=%s count=%d platform_error_code=%s platform_message=%s",
            resource_type,
            state,
            entry["method"],
            entry["path"],
            entry["status_code"],
            count,
            entry["platform_error_code"],
            entry["platform_message"],
        )

    return states


def is_genuinely_empty(states: list[dict[str, Any]]) -> bool:
    """
    True only when EVERY resource type was successfully queried and returned
    zero. One unreadable type means the workspace cannot be called empty.
    """
    if not states:
        return False
    return all(s["state"] == DiscoveryState.SUCCESS_EMPTY for s in states)
