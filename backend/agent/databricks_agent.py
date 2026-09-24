"""
The ACELO Optimization Agent's Databricks compute analysis flow.

    USER
     -> agent (this module)
     -> discovery capability (agent/capabilities.py)
     -> existing authenticated Databricks adapter
     -> Databricks REST API
     -> real resources
     -> evidence-bound analysis (agent/databricks_analysis.py)
     -> recommendations

Separate from `agent/orchestrator.py` on purpose: that path starts a platform
JOB and hands it to the background execution worker. This one runs no job,
touches no worker, and finishes inside the request — it only reads.

Steps are recorded as they ACTUALLY complete. A step is never marked done in
advance, and when one fails the remaining steps stay pending, so the UI cannot
show progress that did not happen.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy.orm import Session

from agent.capabilities import CapabilityStatus, discover_databricks_resources
from agent.databricks_analysis import analyze_compute
from agent.databricks_report import render_markdown
from models import Environment

logger = logging.getLogger(__name__)

# Whether a prompt is asking for a real look at Databricks compute. Kept
# separate from orchestrator.detect_intent(), which routes platform JOBS — this
# must not change how any existing request is routed.
_COMPUTE_WORDS = ("compute", "cluster", "clusters", "warehouse", "warehouses", "serverless", "resource", "resources")
_ANALYSIS_WORDS = ("analyz", "analys", "optimi", "review", "inspect", "audit", "find", "discover", "check", "look")


# Requests that are about compute regardless of any verb: "show me all
# clusters", "what warehouses do I have". These only route to Databricks when
# Databricks is the ACTIVE platform — the platform context supplies the noun
# the user no longer has to type.
_LISTING_WORDS = ("show", "list", "what", "which", "any", "all", "see", "get", "tell")


def is_databricks_compute_request(prompt: str, active_platform: str | None = None) -> bool:
    """
    True when the agent should answer from real Databricks compute discovery.

    Two ways to qualify:

    * the prompt names Databricks explicitly (works from any context), or
    * DATABRICKS is the active platform and the prompt is about compute.

    The second is the point of the platform context: with Databricks active,
    "show me all clusters" means Databricks clusters and the user should not
    have to say so. With Fabric active the same words must NOT reach here, so
    an unset or non-Databricks context falls back to requiring the explicit name.
    """
    lowered = (prompt or "").lower()
    mentions_compute = any(w in lowered for w in _COMPUTE_WORDS)

    if "databricks" in lowered:
        return mentions_compute and any(w in lowered for w in _ANALYSIS_WORDS)

    if (active_platform or "").lower() == "databricks":
        # Analysis verbs OR a plain listing request both count here.
        return mentions_compute and any(
            w in lowered for w in _ANALYSIS_WORDS + _LISTING_WORDS
        )

    return False


class StepStatus:
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Step:
    id: str
    label: str
    status: str = StepStatus.PENDING
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {"id": self.id, "label": self.label, "status": self.status}
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass
class AgentAnalysisResult:
    ok: bool
    status: str
    steps: list[Step] = field(default_factory=list)
    message: str | None = None
    environment_id: str | None = None
    analysis: dict[str, Any] | None = None
    markdown: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "message": self.message,
            "environment_id": self.environment_id,
            "steps": [s.to_dict() for s in self.steps],
            "analysis": self.analysis,
            "markdown": self.markdown,
        }


def _steps() -> dict[str, Step]:
    """The flow's steps, all pending until the work behind each one finishes."""
    return {
        "environment": Step("environment", "Environment identified"),
        "auth": Step("auth", "Databricks authentication verified"),
        "discover": Step("discover", "Discovering compute resources"),
        "resources": Step("resources", "Resources discovered"),
        "analyze": Step("analyze", "Analyzing optimization opportunities"),
    }


async def analyze_databricks_compute(
    db: Session,
    environment: Environment,
    llm: Callable[[str], str] | None = None,
) -> AgentAnalysisResult:
    """
    Runs the full discover -> analyze -> recommend flow against the given ACELO
    environment. Read-only: it creates nothing and executes nothing.

    The environment argument is what determines the Databricks workspace —
    no workspace URL, workspace id, customer, cluster or warehouse is hardcoded
    anywhere in this flow.
    """
    steps = _steps()
    order = list(steps.values())

    # 1. The agent knows which environment (and therefore which workspace) it is
    #    working against only because the caller resolved one.
    steps["environment"].status = StepStatus.DONE
    steps["environment"].detail = environment.workspace_name or environment.name

    # 2-4. Call the capability. This is where the real Databricks calls happen.
    result = await discover_databricks_resources(db, environment)

    if not result.ok:
        # Authentication/authorization is proven by the call, so a failure marks
        # the auth step failed and leaves everything after it pending.
        failed_step = "auth" if result.status in (
            CapabilityStatus.AUTHENTICATION_FAILED,
            CapabilityStatus.AUTHORIZATION_FAILED,
            CapabilityStatus.NOT_CONFIGURED,
        ) else "discover"
        steps[failed_step].status = StepStatus.FAILED
        steps[failed_step].detail = result.message
        return AgentAnalysisResult(
            ok=False,
            status=result.status,
            steps=order,
            message=result.message,
            environment_id=environment.id,
        )

    # The credential worked: at least one resource type was listed.
    steps["auth"].status = StepStatus.DONE
    steps["discover"].status = StepStatus.DONE

    resources = result.data.get("resources", [])
    statuses = result.data.get("statuses", [])
    steps["resources"].status = StepStatus.DONE
    steps["resources"].detail = f"{len(resources)} resource(s)"

    # 5. Analysis reasons ONLY over what discovery returned.
    analysis = analyze_compute(
        workspace_name=result.data.get("workspace_name"),
        resources=resources,
        statuses=statuses,
        llm=llm,
    )
    steps["analyze"].status = StepStatus.DONE
    steps["analyze"].detail = (
        f"{len(analysis.findings)} potential opportunit{'y' if len(analysis.findings) == 1 else 'ies'}"
    )

    logger.info(
        "agent_databricks_analysis env_id=%s resources=%d findings=%d",
        environment.id,
        len(resources),
        len(analysis.findings),
    )

    return AgentAnalysisResult(
        ok=True,
        status=CapabilityStatus.OK,
        steps=order,
        environment_id=environment.id,
        analysis=analysis.to_dict(),
        markdown=render_markdown(analysis),
    )
