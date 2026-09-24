"""
Normalised ACELO compute-resource model for Databricks discovery (read-only).

Why this module exists separately from `platforms/databricks.py`:
`DiscoveredResource` in platforms/base.py is the *workspace inventory* shape
used by environment discovery (notebooks, jobs, lakehouses). Compute capability
discovery needs a different, stricter shape — a platform-tagged resource with a
normalised `resource_type` and a `metadata` bag of values copied verbatim from
the platform — plus a way to report that one resource *type* was unreachable
without failing the other types. Keeping it here leaves the existing adapter
contract and Fabric untouched.

Databricks distinction this module encodes deliberately:
serverless compute is NOT a classic all-purpose/jobs cluster, and neither
serverless compute nor SQL warehouses appear in `system.compute.clusters`.
So SERVERLESS_COMPUTE is never produced by the cluster normaliser, and nothing
here reads a system table.

Nothing in this module invents a field: every `metadata` entry is copied from
the platform payload, and a key the platform did not send is simply absent.
"""

from dataclasses import dataclass, field
from typing import Any

PLATFORM = "databricks"


class ResourceType:
    """Normalised ACELO compute resource types. Stable across platforms."""

    CLASSIC_CLUSTER = "CLASSIC_CLUSTER"
    SERVERLESS_COMPUTE = "SERVERLESS_COMPUTE"
    SQL_WAREHOUSE = "SQL_WAREHOUSE"


class ResourceStatus:
    """
    Per-resource-type outcome of a discovery pass.

    UNAVAILABLE means "we asked and were told no" — a 403 (not permitted) or a
    404 (endpoint not exposed). Those are expected, explainable states.
    ERROR means the pass failed for any other reason (401, 5xx, timeout,
    unreachable): something is wrong rather than merely not permitted.
    """

    OK = "OK"
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"


class UnavailableReason:
    """
    Why one resource type could not be discovered. These are safe to show a
    user and stable enough for the UI to branch on.
    """

    INSUFFICIENT_PERMISSIONS = "INSUFFICIENT_PERMISSIONS"
    # 404 on a resource type Databricks normally exposes (clusters, warehouses).
    NOT_EXPOSED = "NOT_EXPOSED"
    # 404 on the serverless probe: this workspace's API does not surface it.
    NOT_EXPOSED_BY_WORKSPACE_API = "NOT_EXPOSED_BY_WORKSPACE_API"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    PLATFORM_API_UNAVAILABLE = "PLATFORM_API_UNAVAILABLE"
    TIMEOUT = "TIMEOUT"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    DISCOVERY_FAILED = "DISCOVERY_FAILED"


class FailureKind:
    """How a failed discovery call is classified, for diagnosis."""

    AUTHENTICATION = "AUTHENTICATION"  # 401
    AUTHORIZATION = "AUTHORIZATION"  # 403
    NOT_FOUND = "NOT_FOUND"  # 404
    OTHER = "OTHER"


@dataclass
class DiscoveryProbe:
    """
    One HTTP call made during discovery, recorded whether it succeeded or not.

    This is the diagnostic record: which resource type, which method and path,
    what status came back and what Databricks said about it. It deliberately
    holds no request headers, so a PAT can never reach it.
    """

    resource_type: str
    method: str
    path: str
    status_code: int | None = None
    ok: bool = False
    failure_kind: str | None = None
    platform_error_code: str | None = None
    platform_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "resource_type": self.resource_type,
            "method": self.method,
            "path": self.path,
            "status_code": self.status_code,
            "ok": self.ok,
        }
        for key in ("failure_kind", "platform_error_code", "platform_message"):
            value = getattr(self, key)
            if value:
                payload[key] = value
        return payload


@dataclass
class AceloResource:
    """One normalised compute resource. Never carries a credential."""

    platform: str
    resource_type: str
    resource_id: str
    name: str
    state: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "name": self.name,
            "state": self.state,
            "metadata": self.metadata,
        }


@dataclass
class ResourceTypeStatus:
    """
    The outcome for one resource type. A discovery response carries one of
    these per type so a caller can tell "none exist" (OK, count 0) apart from
    "we could not look" (UNAVAILABLE + reason).
    """

    resource_type: str
    status: str
    reason: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"resource_type": self.resource_type, "status": self.status}
        if self.reason:
            payload["reason"] = self.reason
        if self.message:
            payload["message"] = self.message
        return payload


@dataclass
class ResourceDiscovery:
    """Everything one discovery pass produced, successes and gaps alike."""

    resources: list[AceloResource] = field(default_factory=list)
    statuses: list[ResourceTypeStatus] = field(default_factory=list)
    probes: list[DiscoveryProbe] = field(default_factory=list)

    def of_type(self, resource_type: str) -> list[AceloResource]:
        return [r for r in self.resources if r.resource_type == resource_type]

    def status_of(self, resource_type: str) -> ResourceTypeStatus | None:
        for status in self.statuses:
            if status.resource_type == resource_type:
                return status
        return None

    def probes_of(self, resource_type: str) -> list[DiscoveryProbe]:
        return [p for p in self.probes if p.resource_type == resource_type]

    def compute(self) -> dict[str, Any]:
        """
        The ACELO Compute capability, grouped by category.

        Each category carries BOTH its resources and its status, so a caller can
        never read an unavailable category as an empty one: `sql_warehouses`
        with status UNAVAILABLE and an empty list means "we could not look",
        not "you have none".
        """
        groups = {
            "classic_clusters": ResourceType.CLASSIC_CLUSTER,
            "serverless_compute": ResourceType.SERVERLESS_COMPUTE,
            "sql_warehouses": ResourceType.SQL_WAREHOUSE,
        }
        payload: dict[str, Any] = {}
        for key, resource_type in groups.items():
            status = self.status_of(resource_type)
            payload[key] = {
                "resources": [r.to_dict() for r in self.of_type(resource_type)],
                "status": status.status if status else ResourceStatus.ERROR,
                "reason": status.reason if status else None,
                "message": status.message if status else None,
            }
        return payload

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": PLATFORM,
            # Grouped view of the Compute capability. `resources` below is the
            # same data flat, kept so existing callers keep working.
            "compute": self.compute(),
            "resources": [r.to_dict() for r in self.resources],
            "statuses": [s.to_dict() for s in self.statuses],
            "probes": [p.to_dict() for p in self.probes],
        }


def _copy_present(source: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """Copies only the keys the platform actually sent. Never fills a default."""
    return {key: source[key] for key in keys if key in source and source[key] is not None}


# --- normalisers -----------------------------------------------------------
# Each takes one raw Databricks API object and returns one AceloResource.
# Raw payloads are trusted for shape only: a missing key yields a missing
# metadata entry rather than an invented value.


_CLUSTER_EXTRA_KEYS = (
    "spark_version",
    "runtime_engine",
    "creator_user_name",
    "single_user_name",
    "data_security_mode",
    "policy_id",
    "start_time",
    "state_message",
)


def normalize_classic_cluster(raw: dict[str, Any]) -> AceloResource:
    """
    Normalises one object from GET /api/2.0/clusters/list.

    `cluster_type` is derived from `cluster_source`, which is what Databricks
    uses to separate an all-purpose (UI/API) cluster from a job cluster — it is
    a rename, not an invented field. Serverless compute never reaches here.
    """
    cluster_id = raw.get("cluster_id") or ""
    autoscale = raw.get("autoscale")

    metadata: dict[str, Any] = {
        "cluster_id": cluster_id,
        "cluster_type": raw.get("cluster_source"),
        "driver_node_type": raw.get("driver_node_type_id"),
        "worker_node_type": raw.get("node_type_id"),
        "num_workers": raw.get("num_workers"),
        # None means the cluster is fixed-size; Databricks omits `autoscale` then.
        "autoscaling": (
            {
                "min_workers": autoscale.get("min_workers"),
                "max_workers": autoscale.get("max_workers"),
            }
            if isinstance(autoscale, dict)
            else None
        ),
        "auto_termination_minutes": raw.get("autotermination_minutes"),
    }
    metadata.update(_copy_present(raw, _CLUSTER_EXTRA_KEYS))

    return AceloResource(
        platform=PLATFORM,
        resource_type=ResourceType.CLASSIC_CLUSTER,
        resource_id=cluster_id,
        name=raw.get("cluster_name") or cluster_id,
        state=raw.get("state"),
        metadata=metadata,
    )


# Identifier/name/state keys seen across the serverless-compute shapes
# Databricks exposes. The probe reads whichever are present rather than
# assuming one schema, because the serverless surface is not a single
# documented list endpoint the way clusters and warehouses are.
_SERVERLESS_ID_KEYS = ("id", "policy_id", "environment_id", "compute_id", "object_id", "name")
_SERVERLESS_NAME_KEYS = ("name", "display_name", "policy_name", "label")
_SERVERLESS_STATE_KEYS = ("state", "status", "lifecycle_state")


def _first_present(raw: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = raw.get(key)
        if value not in (None, ""):
            return value
    return None


def normalize_serverless_compute(raw: dict[str, Any], source: str | None = None) -> AceloResource:
    """
    Normalises one serverless compute object.

    Serverless compute is explicitly NOT a classic cluster: it has no driver or
    worker node type, no worker count and no auto-termination setting, so none
    of those keys are emitted here. `metadata` carries the raw object's own
    fields verbatim plus, when known, the API path it came from — so the
    operator can see exactly what Databricks reported.
    """
    identifier = _first_present(raw, _SERVERLESS_ID_KEYS)
    name = _first_present(raw, _SERVERLESS_NAME_KEYS)

    metadata: dict[str, Any] = {k: v for k, v in raw.items() if v is not None}
    if source:
        metadata["discovered_from"] = source

    return AceloResource(
        platform=PLATFORM,
        resource_type=ResourceType.SERVERLESS_COMPUTE,
        resource_id=str(identifier) if identifier is not None else "",
        name=str(name) if name is not None else (str(identifier) if identifier is not None else ""),
        state=_first_present(raw, _SERVERLESS_STATE_KEYS),
        metadata=metadata,
    )


_WAREHOUSE_EXTRA_KEYS = (
    "cluster_size",
    "min_num_clusters",
    "max_num_clusters",
    "num_clusters",
    "num_active_sessions",
    "enable_photon",
    "enable_serverless_compute",
    "spot_instance_policy",
    "creator_name",
    "health",
)


def normalize_sql_warehouse(raw: dict[str, Any]) -> AceloResource:
    """Normalises one object from GET /api/2.0/sql/warehouses."""
    warehouse_id = raw.get("id") or ""

    metadata: dict[str, Any] = {
        "warehouse_id": warehouse_id,
        "warehouse_type": raw.get("warehouse_type"),
        # Databricks reports t-shirt size as `cluster_size`; `size` is its alias here.
        "size": raw.get("cluster_size"),
        "auto_stop_mins": raw.get("auto_stop_mins"),
        "scaling": {
            "min_num_clusters": raw.get("min_num_clusters"),
            "max_num_clusters": raw.get("max_num_clusters"),
        }
        if ("min_num_clusters" in raw or "max_num_clusters" in raw)
        else None,
    }

    # Owner/creator is only present when the caller may see it.
    owner = raw.get("creator_name") or raw.get("owner")
    if owner:
        metadata["owner"] = owner

    metadata.update(_copy_present(raw, _WAREHOUSE_EXTRA_KEYS))

    return AceloResource(
        platform=PLATFORM,
        resource_type=ResourceType.SQL_WAREHOUSE,
        resource_id=warehouse_id,
        name=raw.get("name") or warehouse_id,
        state=raw.get("state"),
        metadata=metadata,
    )
