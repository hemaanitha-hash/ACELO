"""
ACELO Agent capabilities (tools).

A capability is what the agent calls when it needs real information it cannot
reason its way to. Each one wraps an EXISTING service — it never opens its own
connection, builds its own client, or handles credentials itself.

Call path for the Databricks discovery capability:

    agent -> discover_databricks_resources()
          -> services.environment_service.build_adapter()   (decrypts the PAT)
          -> platforms.databricks.DatabricksAdapter
          -> platforms.databricks_discovery                 (read-only GETs)
          -> Databricks REST API

In-process throughout: the agent never calls ACELO's own HTTP API over
localhost, so there is no second authentication hop and no token on the wire.

Credential rule: the PAT is decrypted inside build_adapter() and lives only on
the adapter instance for the duration of the call. Nothing this module returns
carries it, so it cannot reach the agent's message history, a prompt, an LLM,
or a log line.

This phase is read-only. No capability here creates, starts, stops, resizes or
terminates anything, and none reads system tables.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from models import Environment
from platforms.databricks_resources import ResourceStatus
from platforms.errors import ErrorCode, PlatformError

logger = logging.getLogger(__name__)


class CapabilityStatus:
    """Outcome of a capability call, as the agent sees it."""

    OK = "OK"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    AUTHORIZATION_FAILED = "AUTHORIZATION_FAILED"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    DISCOVERY_FAILED = "DISCOVERY_FAILED"


@dataclass
class CapabilityResult:
    """
    What a capability hands back to the agent.

    `ok` False means the agent must say so rather than reason about an empty
    list — "nothing was found" and "we could not look" are different answers and
    must never collapse into each other.
    """

    status: str
    ok: bool
    message: str | None = None
    resource_type: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"status": self.status, "ok": self.ok}
        for key in ("message", "resource_type"):
            value = getattr(self, key)
            if value:
                payload[key] = value
        payload.update(self.data)
        return payload


# The whole-request failure codes. A per-resource-type refusal is NOT one of
# these: it is reported inside `statuses` and discovery still succeeds.
_FATAL_STATUS_BY_ERROR_CODE = {
    ErrorCode.AUTHENTICATION_FAILED: CapabilityStatus.AUTHENTICATION_FAILED,
    ErrorCode.PERMISSION_DENIED: CapabilityStatus.AUTHORIZATION_FAILED,
    ErrorCode.NOT_CONFIGURED: CapabilityStatus.NOT_CONFIGURED,
}


async def discover_databricks_resources(db: Session, environment: Environment) -> CapabilityResult:
    """
    Discovers the real compute resources visible to the environment's Databricks
    identity: classic clusters, serverless compute and SQL warehouses.

    The workspace is whichever one the given ACELO environment points at —
    nothing here hardcodes a workspace URL, workspace id, customer, cluster or
    warehouse.

    Partial discovery succeeds: if one resource type is refused, the others are
    still returned and the refusal is reported in `statuses`. Only a failure
    that prevents ALL discovery (bad credential, unconfigured environment) makes
    this return ok=False.
    """
    from services import environment_service

    if environment.platform != "databricks":
        return CapabilityResult(
            status=CapabilityStatus.NOT_CONFIGURED,
            ok=False,
            message=(
                f"The selected environment '{environment.name}' is a "
                f"{environment.platform} environment, not Databricks."
            ),
        )

    try:
        adapter = environment_service.build_adapter(db, environment)
        discovery = await adapter.discover_compute_resources()
    except PlatformError as exc:
        status = _FATAL_STATUS_BY_ERROR_CODE.get(exc.code, CapabilityStatus.DISCOVERY_FAILED)
        logger.info(
            "agent_capability_failed capability=discover_databricks_resources env_id=%s status=%s detail=%s",
            environment.id,
            status,
            exc.log_detail,  # technical detail stays server-side
        )
        return CapabilityResult(status=status, ok=False, message=exc.message)
    except Exception as exc:  # noqa: BLE001 - a capability fault must not 500 the agent
        logger.exception("agent_capability_error capability=discover_databricks_resources env_id=%s", environment.id)
        return CapabilityResult(
            status=CapabilityStatus.DISCOVERY_FAILED,
            ok=False,
            message="Databricks resources could not be discovered.",
        )

    payload = discovery.to_dict()
    statuses = payload["statuses"]

    # Every type failed for the same credential reason: that is an
    # authentication/authorization problem with the connection itself, not a
    # per-type gap, so the agent is told plainly instead of analysing nothing.
    if statuses and all(s["status"] != ResourceStatus.OK for s in statuses):
        reasons = {s.get("reason") for s in statuses}
        if reasons == {"AUTHENTICATION_FAILED"}:
            return CapabilityResult(
                status=CapabilityStatus.AUTHENTICATION_FAILED,
                ok=False,
                message="Databricks authentication failed.",
            )
        if reasons == {"INSUFFICIENT_PERMISSIONS"}:
            return CapabilityResult(
                status=CapabilityStatus.AUTHORIZATION_FAILED,
                ok=False,
                resource_type="ALL",
                message="Current identity does not have permission to access this resource.",
            )

    logger.info(
        "agent_capability_ok capability=discover_databricks_resources env_id=%s resources=%d outcomes=%s",
        environment.id,
        len(payload["resources"]),
        {s["resource_type"]: s["status"] for s in statuses},
    )

    return CapabilityResult(
        status=CapabilityStatus.OK,
        ok=True,
        data={
            "platform": payload["platform"],
            "environment_id": environment.id,
            "workspace_name": environment.workspace_name or environment.name,
            "resources": payload["resources"],
            "statuses": statuses,
            "probes": payload["probes"],
        },
    )
