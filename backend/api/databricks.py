"""
Databricks compute capability discovery API (read-only).

Follows the same customer-scoping rule as api/environments.py: the environment
is resolved through environment_service for THIS customer, so another
customer's environment is a 404 — never discoverable.

Security: the response models below have no field that can carry a token,
client secret or PAT. The browser calls this endpoint; only this backend ever
talks to Databricks.

This phase creates, starts, stops and executes nothing, and does not read
system.billing.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.deps import get_current_customer
from api.platform_context import ActivePlatform, require_databricks
from database import get_db
from models import Customer, Environment
from platforms.databricks_resources import ResourceStatus
from services import environment_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/databricks", tags=["databricks"])


class AceloResourceOut(BaseModel):
    platform: str
    resource_type: str
    resource_id: str
    name: str
    state: str | None = None
    metadata: dict = {}


class ResourceTypeStatusOut(BaseModel):
    resource_type: str
    status: str
    reason: str | None = None
    message: str | None = None


class DiscoveryProbeOut(BaseModel):
    """
    One discovery call, for diagnosis. Carries the platform's own status and
    error body — never a request header, so no token can appear here.
    """

    resource_type: str
    method: str
    path: str
    status_code: int | None = None
    ok: bool
    failure_kind: str | None = None
    platform_error_code: str | None = None
    platform_message: str | None = None


class ComputeGroupOut(BaseModel):
    """One Compute category: its resources AND whether we could read them."""

    resources: list[AceloResourceOut] = []
    status: str
    reason: str | None = None
    message: str | None = None


class ComputeOut(BaseModel):
    """The ACELO Compute capability, classified by Databricks resource type."""

    classic_clusters: ComputeGroupOut
    serverless_compute: ComputeGroupOut
    sql_warehouses: ComputeGroupOut


class DatabricksResourcesOut(BaseModel):
    platform: str = "databricks"
    environment_id: str
    workspace_name: str | None = None
    connected: bool
    compute: ComputeOut
    resources: list[AceloResourceOut]
    statuses: list[ResourceTypeStatusOut]
    probes: list[DiscoveryProbeOut] = []


def _resolve_environment(
    db: Session,
    customer: Customer,
    environment_id: str | None,
    context: ActivePlatform | None = None,
) -> Environment:
    """
    Picks the Databricks environment to discover.

    Precedence: an explicit id, then the ACTIVE CONNECTION the user selected,
    then the customer's Databricks environment. The active connection is what
    makes this honour the platform switcher instead of picking one arbitrarily
    when several Databricks environments exist.
    """
    if environment_id:
        environment = environment_service.get_environment_for_customer(db, environment_id, customer.id)
        if not environment:
            raise HTTPException(status_code=404, detail="Environment not found")
        if environment.platform != "databricks":
            raise HTTPException(
                status_code=400,
                detail=f"Environment '{environment.name}' is a {environment.platform} environment, not Databricks.",
            )
        return environment

    # The user's selected connection decides, when one was sent.
    if context is not None and context.environment is not None:
        if context.environment.platform == "databricks":
            return context.environment

    candidates = [
        env for env in environment_service.list_environments(db, customer.id) if env.platform == "databricks"
    ]
    if not candidates:
        raise HTTPException(
            status_code=404,
            detail="No Databricks environment is configured. Add one in Environment Setup first.",
        )
    return candidates[0]


@router.get("/resources", response_model=DatabricksResourcesOut)
async def list_databricks_resources(
    environment_id: str | None = None,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
    context: ActivePlatform = Depends(require_databricks),
):
    """
    Discovers the real compute resources visible to the authenticated Databricks
    identity: classic clusters, serverless compute and SQL warehouses.

    A resource type that cannot be read does NOT fail the request — it comes
    back as a status entry with status=UNAVAILABLE and a reason, alongside the
    types that succeeded.
    """
    environment = _resolve_environment(db, customer, environment_id, context)
    adapter = environment_service.build_adapter(db, environment)

    discovery = await adapter.discover_compute_resources()
    payload = discovery.to_dict()

    # "Connected" means at least one resource type was actually listed. It is
    # never asserted from configuration alone.
    connected = any(s["status"] == ResourceStatus.OK for s in payload["statuses"])

    logger.info(
        "databricks_discovery env_id=%s customer_id=%s connected=%s resources=%d outcomes=%s",
        environment.id,
        customer.id,
        connected,
        len(payload["resources"]),
        {s["resource_type"]: s["status"] for s in payload["statuses"]},
    )

    return DatabricksResourcesOut(
        environment_id=environment.id,
        workspace_name=environment.workspace_name or environment.name,
        connected=connected,
        compute=ComputeOut(**payload["compute"]),
        resources=[AceloResourceOut(**r) for r in payload["resources"]],
        statuses=[ResourceTypeStatusOut(**s) for s in payload["statuses"]],
        probes=[DiscoveryProbeOut(**p) for p in payload["probes"]],
    )


# --- agent analysis ---------------------------------------------------------


class AgentStepOut(BaseModel):
    id: str
    label: str
    status: str
    detail: str | None = None


class AgentAnalysisRequest(BaseModel):
    prompt: str
    environment_id: str | None = None


class AgentAnalysisOut(BaseModel):
    ok: bool
    status: str
    message: str | None = None
    environment_id: str | None = None
    steps: list[AgentStepOut]
    analysis: dict | None = None
    markdown: str | None = None


@router.post("/agent/analyze", response_model=AgentAnalysisOut)
async def analyze_databricks_compute_endpoint(
    payload: AgentAnalysisRequest,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
    context: ActivePlatform = Depends(require_databricks),
):
    """
    The ACELO Optimization Agent's Databricks compute analysis.

    Discover -> analyze -> recommend, in-process: the agent calls the discovery
    capability directly rather than looping back through this API over HTTP.
    Read-only — it starts no job, touches no background worker, and creates,
    modifies or terminates nothing.

    A discovery failure is returned as a structured status with ok=false and
    HTTP 200, not an exception: the agent has a real answer to give ("I could
    not authenticate"), and the UI must render it rather than a generic error.
    """
    from agent.databricks_agent import analyze_databricks_compute
    from optimizers.cluster_optimizer import groq_llm_from_env

    environment = _resolve_environment(db, customer, payload.environment_id, context)

    # The same optional LLM the cluster optimizer uses. Absent key -> None, and
    # the analysis falls back to its deterministic summary rather than inventing
    # one. The prompt it receives carries discovered configuration only; no
    # credential is ever placed in it.
    result = await analyze_databricks_compute(db, environment, llm=groq_llm_from_env())

    logger.info(
        "agent_databricks_analyze env_id=%s customer_id=%s status=%s",
        environment.id,
        customer.id,
        result.status,
    )
    return AgentAnalysisOut(**result.to_dict())
