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
JOB and hands it to the background execution worker. This one finishes inside
the request and never touches the worker.

Cluster optimization has two sources, chosen by configuration, never guessed:

  * When the Environment Resource Registry has a cluster execution target
    (a registered job, or a notebook such as ACELO_CLUSTER_NOTEBOOK_ID), the
    Cluster Optimization NOTEBOOK is the source of truth. The agent starts it
    through the existing job_service / DatabricksAdapter path, waits for the
    real Databricks run to finish, reads the notebook's result tables and turns
    them into recommendations through the existing contract. No optimization
    logic is re-implemented here.
  * With no target registered, the earlier read-only evidence analysis runs
    unchanged.

Neither source changes a cluster: the notebook's own executor stays disabled
(ALLOW_API_ACTIONS = False) and every recommendation still needs approval.

Steps are recorded as they ACTUALLY complete. A step is never marked done in
advance, and when one fails the remaining steps stay pending, so the UI cannot
show progress that did not happen.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy.orm import Session

from agent.capabilities import CapabilityStatus, discover_databricks_resources
from agent.databricks_analysis import analyze_compute
from agent.databricks_report import render_markdown
from models import Connection, Environment, JobStatus

from services import job_service, resource_registry
from services.compute_optimization.databricks_reader import DatabricksSQLReader
from services.compute_optimization.notebook_results import adapt_notebook_results
from services.execution_worker import (
    POLL_BACKOFF_FACTOR,
    POLL_INITIAL_INTERVAL,
    POLL_MAX_INTERVAL,
    POLL_TIMEOUT_SECONDS,
)
from services.compute_optimization.engine import (
    IDLE_CPU,
    IDLE_MEMORY,
    analyze_compute_evidence,
)
from services.compute_optimization.evidence_normalizer import (
    TimelineNormalization,
    derive_post_task_idle,
    normalize_node_timeline,
)
from services.compute_optimization.models import ComputeEvidence, EvidenceQuality
from services.compute_optimization.recommendations import persist_recommendations


logger = logging.getLogger(__name__)


# Whether a prompt is asking for a real look at Databricks compute.
# Kept separate from orchestrator.detect_intent(), which routes platform JOBS —
# this must not change how any existing request is routed.
_COMPUTE_WORDS = (
    "compute",
    "cluster",
    "clusters",
    "warehouse",
    "warehouses",
    "serverless",
    "resource",
    "resources",
)

_ANALYSIS_WORDS = (
    "analyz",
    "analys",
    "optimi",
    "review",
    "inspect",
    "audit",
    "find",
    "discover",
    "check",
    "look",
)


# Requests that are about compute regardless of any verb:
# "show me all clusters", "what warehouses do I have".
# These only route to Databricks when Databricks is the ACTIVE platform —
# the platform context supplies the noun the user no longer has to type.
_LISTING_WORDS = (
    "show",
    "list",
    "what",
    "which",
    "any",
    "all",
    "see",
    "get",
    "tell",
)


def is_databricks_compute_request(
    prompt: str,
    active_platform: str | None = None,
) -> bool:
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
        payload = {
            "id": self.id,
            "label": self.label,
            "status": self.status,
        }

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


def _classic_cluster_listing_unavailable(
    statuses: list[dict[str, Any]] | None,
) -> bool:
    """
    Return True only when discovery explicitly indicates that the classic
    cluster listing is unavailable.

    This is deliberately stricter than checking `resources == []`.

    An empty resource list can mean "there are no live classic clusters".
    We must not manufacture table-backed inventory in that case because the
    existing discovery contract/tests intentionally distinguish an empty
    workspace from a forbidden/unavailable cluster listing.

    The production Databricks App case is different: the workspace can return
    a capability status indicating that classic-cluster discovery is forbidden,
    unavailable, unauthorized, or otherwise inaccessible. In that case the
    real databricks_ws.agent.cluster table is a valid Stage 1 inventory source.
    """
    for item in statuses or []:
        if not isinstance(item, dict):
            continue

        resource_type = str(
            item.get("resource_type")
            or item.get("resourceType")
            or item.get("resource")
            or item.get("type")
            or ""
        ).upper()

        # Match the classic-cluster capability without assuming one exact
        # field naming convention.
        is_classic_cluster = (
            "CLASSIC_CLUSTER" in resource_type
            or resource_type == "CLUSTER"
            or resource_type.endswith("_CLUSTER")
        )
        if not is_classic_cluster:
            continue

        if item.get("ok") is False or item.get("available") is False:
            return True

        status_text = str(
            item.get("status")
            or item.get("state")
            or item.get("status_code")
            or item.get("code")
            or ""
        ).lower()

        message_text = str(
            item.get("message")
            or item.get("detail")
            or item.get("error")
            or ""
        ).lower()

        combined = f"{status_text} {message_text}"

        unavailable_markers = (
            "forbidden",
            "unauthorized",
            "permission",
            "not_permitted",
            "not permitted",
            "not_authorized",
            "not authorized",
            "unavailable",
            "not available",
            "access denied",
            "access_denied",
            "403",
        )

        if any(marker in combined for marker in unavailable_markers):
            return True

    return False

def _resolve_sql_warehouse_id(
    resources: list[dict[str, Any]] | None,
) -> str | None:
    """
    Resolve the SQL warehouse used by the read-only evidence reader.

    Priority:
      1. Explicit ACELO_DATABRICKS_SQL_WAREHOUSE_ID configuration.
      2. A SQL_WAREHOUSE discovered from the active Databricks workspace.

    The agent never hardcodes a warehouse ID.
    """
    configured = os.getenv(
        "ACELO_DATABRICKS_SQL_WAREHOUSE_ID",
        "",
    ).strip()

    if configured:
        return configured

    warehouse_ids: list[str] = []

    for resource in resources or []:
        if not isinstance(resource, dict):
            continue

        resource_type = str(
            resource.get("resource_type") or ""
        ).upper()

        if resource_type != "SQL_WAREHOUSE":
            continue

        resource_id = resource.get("resource_id")

        if resource_id is None or str(resource_id).strip() == "":
            continue

        warehouse_ids.append(str(resource_id).strip())

    if not warehouse_ids:
        return None

    return sorted(set(warehouse_ids))[0]

def _cluster_configuration(
    resource: dict[str, Any],
    sql_rows: list[dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    resource_id = resource.get("resource_id")

    sql_cluster = next(
        (
            row
            for row in sql_rows
            if str(row.get("cluster_id") or "") == str(resource_id or "")
        ),
        None,
    )

    configuration = dict(sql_cluster or {})
    metadata = resource.get("metadata") or {}

    configuration.setdefault("cluster_id", resource_id)
    configuration.setdefault("cluster_name", resource.get("name"))

    if configuration.get("worker_count") is None:
        configuration["worker_count"] = metadata.get("num_workers")

    autoscaling = metadata.get("autoscaling") or {}

    if configuration.get("min_autoscale_workers") is None:
        configuration["min_autoscale_workers"] = autoscaling.get("min_workers")

    if configuration.get("max_autoscale_workers") is None:
        configuration["max_autoscale_workers"] = autoscaling.get("max_workers")

    if configuration.get("auto_termination_minutes") is None:
        configuration["auto_termination_minutes"] = metadata.get(
            "auto_termination_minutes"
        )

    return configuration, sql_cluster is not None


def _compute_evidence_for_cluster(
    *,
    environment: Environment,
    resource: dict[str, Any],
    evidence_rows: dict[str, Any],
    workspace_name: str | None,
    collected_at: str,
) -> ComputeEvidence:
    cluster_id = resource.get("resource_id")

    cluster, cluster_row_available = _cluster_configuration(
        resource,
        evidence_rows.get("cluster", []),
    )

    raw_timeline = evidence_rows.get("node_timeline", [])

    normalized = (
        normalize_node_timeline(cluster_id, raw_timeline)
        if cluster_id
        else TimelineNormalization(
            timestamp_valid=False,
            consistency="INCONSISTENT",
            error="Discovered cluster has no resource ID.",
        )
    )

    errors = evidence_rows.get("errors", {})

    task_rows = [
        row
        for row in evidence_rows.get("job_task_run_timeline", [])
        if str(row.get("cluster_id") or "") == str(cluster_id or "")
    ]

    runtime = (
        derive_post_task_idle(
            cluster_id or "",
            normalized.utilization,
            task_rows,
            idle_cpu_threshold=IDLE_CPU,
            idle_memory_threshold=IDLE_MEMORY,
        )
        or {}
    )

    node_source_failed = errors.get("node_timeline") == "READ_FAILED"

    missing_fields: list[str] = []

    if not cluster_row_available:
        missing_fields.append("cluster_sql_row")

    if node_source_failed or not normalized.utilization:
        missing_fields.append("node_timeline")

    if not normalized.worker_history_available:
        missing_fields.append("worker_history")

    if not runtime:
        missing_fields.append("task_to_idle_mapping")

    for table_name, state in errors.items():
        if state == "READ_FAILED" and table_name not in missing_fields:
            missing_fields.append(f"{table_name}")

    notes: list[str] = []

    if normalized.error:
        notes.append(normalized.error)

    if node_source_failed:
        notes.append("Node timeline source failed to read.")

    if not runtime:
        notes.append(
            "No exact same-cluster task-end to contiguous idle-bucket "
            "mapping was available."
        )

    quality = EvidenceQuality(
        source_available=bool(cluster_id),
        completeness=(
            "INSUFFICIENT"
            if (
                node_source_failed
                or not normalized.utilization
                or not cluster_id
                or not cluster_row_available
            )
            else "COMPLETE" if cluster_row_available else "PARTIAL"
        ),
        freshness="UNKNOWN",
        timestamp_valid=normalized.timestamp_valid,
        consistency=normalized.consistency,
        missing_fields=missing_fields,
        notes=" ".join(notes),
    )

    starts = [row["start_time"] for row in normalized.utilization]
    ends = [row["end_time"] for row in normalized.utilization]

    return ComputeEvidence(
        cluster=cluster,
        utilization=normalized.utilization,
        billing=[
            dict(row)
            for row in evidence_rows.get("billing_usage", [])
            if str(row.get("cluster_id") or "") == str(cluster_id or "")
        ],
        worker_levels=normalized.worker_levels,
        runtime=runtime,
        quality=quality,
        observation_start=min(starts) if starts else None,
        observation_end=max(ends) if ends else None,
        collected_at=collected_at,
        lineage={
            "resource_id": cluster_id,
            "cluster_id": cluster_id,
            "resource_type": "CLASSIC_CLUSTER",
            "environment_id": environment.id,
            "customer_id": environment.customer_id,
            "workspace_name": workspace_name,
        },
    )


# ---------------------------------------------------------------------------
# Cluster Optimization notebook execution
# ---------------------------------------------------------------------------

# The tables the Cluster Optimization notebook writes. They are the notebook's
# own output contract - fixed in its source, not a notebook parameter (it
# declares no widgets) - so they are named here exactly as the notebook names
# them rather than read from configuration the notebook cannot receive.
NOTEBOOK_FINDINGS_TABLE = "default.compute_agent_findings"
NOTEBOOK_ACTION_QUEUE_TABLE = "default.compute_agent_action_queue"

# Recorded on the AnalysisJob. The agent receives no prompt text, so this names
# the request rather than pretending to quote the user.
_NOTEBOOK_REQUEST = "Cluster optimization requested from the ACELO agent"

# Tolerance for clock difference between this process and Databricks when
# deciding whether a result row was written by THIS run. The notebook stamps
# every findings row at write time, minutes after the run starts, so a small
# allowance cannot admit the rows of a run that finished before this one began.
_RESULT_CLOCK_SKEW = timedelta(seconds=60)

# Indirection so tests drive polling without real sleeps.
_sleep = asyncio.sleep


def _run_timeout_seconds() -> int:
    """
    How long an agent request waits for the notebook run.

    The agent answers inside one HTTP request, so it cannot wait the background
    worker's full hour. Configurable, and never longer than the worker's own cap.
    """
    raw = (os.getenv("ACELO_AGENT_RUN_TIMEOUT_SECONDS") or "").strip()
    try:
        configured = int(raw) if raw else 900
    except ValueError:
        configured = 900
    return max(0, min(configured, POLL_TIMEOUT_SECONDS))


def _registered_cluster_target(db: Session, environment: Environment) -> dict[str, str] | None:
    """
    The cluster execution target the Environment Resource Registry resolves for
    this environment, or None.

    Read through resource_registry.get() - the same effective configuration
    apply_to_adapter() hands the DatabricksAdapter - so the agent and the adapter
    can never disagree about whether a target exists. A notebook path arrives
    from ACELO_CLUSTER_NOTEBOOK_ID through the registry's configuration defaults;
    nothing is hardcoded here.
    """
    config = resource_registry.get(db, environment, "cluster")
    target = {
        key: str(config.get(key)).strip()
        for key in ("job_id", "notebook_id")
        if config.get(key) and str(config.get(key)).strip()
    }
    return target or None


async def _await_terminal_status(
    db: Session,
    connection: Connection,
    job_run: Any,
    access_token: str | None,
) -> bool:
    """
    Polls the run through the existing job_service.sync_run_status until it
    reaches a terminal state. Returns False on timeout.

    Polling happens inside this request on purpose: a Databricks App connection
    stores no secret, so the background worker would decline to poll it.
    Backoff reuses the worker's own intervals.
    """
    deadline = time.monotonic() + _run_timeout_seconds()
    interval = POLL_INITIAL_INTERVAL

    while job_run.status not in job_service.TERMINAL_STATUSES:
        if time.monotonic() >= deadline:
            return False
        await _sleep(interval)
        await job_service.sync_run_status(db, connection, job_run, access_token)
        interval = min(interval * POLL_BACKOFF_FACTOR, POLL_MAX_INTERVAL)

    return True


def _parse_generated_at(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _rows_not_from_this_run(
    rows: list[dict[str, Any]],
    run_started_at: datetime | None,
) -> list[dict[str, Any]]:
    """
    Result rows that cannot be attributed to the run that just finished.

    The notebook overwrites the findings table on every run and stamps each row
    with generated_at at write time; it records no run id. So a row is this
    run's only if it was written after the run started. A row with no readable
    timestamp - or a run with no recorded start - cannot be attributed at all.
    """
    if run_started_at is None:
        return list(rows)
    started = (
        run_started_at if run_started_at.tzinfo else run_started_at.replace(tzinfo=timezone.utc)
    )
    threshold = started - _RESULT_CLOCK_SKEW
    stale = []
    for row in rows:
        generated_at = _parse_generated_at(row.get("generated_at"))
        if generated_at is None or generated_at < threshold:
            stale.append(row)
    return stale


async def _analyze_with_notebook(
    *,
    db: Session,
    environment: Environment,
    access_token: str | None,
    llm: Callable[[str], str] | None,
    steps: dict[str, Step],
    order: list[Step],
    discovery: dict[str, Any],
    target: dict[str, str],
) -> AgentAnalysisResult:
    """
    Cluster optimization from the Cluster Optimization notebook.

        start the registered job/notebook (existing job_service + adapter)
        -> real Databricks run_id
        -> wait for a terminal state (existing job_service.sync_run_status)
        -> read the notebook's result tables (existing DatabricksSQLReader)
        -> adapt_notebook_results()
        -> persist_recommendations()

    Any failure returns a structured error naming the run, and creates no
    recommendation: a failed, timed-out or unattributable run never yields
    results, and the earlier Python analysis is never substituted for it.
    """
    resources = discovery.get("resources", [])
    statuses = discovery.get("statuses", [])
    workspace_name = discovery.get("workspace_name")

    def failed(status: str, message: str, job_run: Any = None) -> AgentAnalysisResult:
        steps["analyze"].status = StepStatus.FAILED
        steps["analyze"].detail = message
        logger.warning(
            "agent_notebook_run_failed env_id=%s status=%s acelo_run_id=%s platform_run_id=%s",
            environment.id,
            status,
            getattr(job_run, "id", None),
            getattr(job_run, "platform_run_id", None),
        )
        return AgentAnalysisResult(
            ok=False,
            status=status,
            steps=order,
            message=message,
            environment_id=environment.id,
        )

    connection = (
        db.query(Connection).filter(Connection.id == environment.connection_id).first()
    )
    if connection is None:
        return failed(
            "NOTEBOOK_RUN_NOT_STARTED",
            "This Databricks environment has no connection to run the Cluster "
            "Optimization notebook through.",
        )

    # 1-3. Start the run through the existing execution path. The adapter
    # resolves the registered job or notebook itself and returns Databricks'
    # own run id; start_job_run records it on the JobRun.
    analysis_job = job_service.create_analysis_job(
        db, environment.customer_id, connection, request=_NOTEBOOK_REQUEST, intent="cluster"
    )
    job_run = job_service.create_job_run(db, analysis_job, "cluster")
    await job_service.start_job_run(db, connection, job_run, access_token)

    if not job_run.platform_run_id:
        return failed(
            "NOTEBOOK_RUN_NOT_STARTED",
            "The Cluster Optimization notebook could not be started: "
            f"{job_run.error or 'Databricks issued no run id.'}",
            job_run,
        )

    run_id = job_run.platform_run_id
    logger.info(
        "agent_notebook_run_started env_id=%s acelo_run_id=%s platform_run_id=%s target=%s",
        environment.id,
        job_run.id,
        run_id,
        sorted(target),
    )

    # 4. Wait for the real run to finish.
    if not await _await_terminal_status(db, connection, job_run, access_token):
        return failed(
            "NOTEBOOK_RUN_TIMEOUT",
            f"Databricks run {run_id} did not finish within "
            f"{_run_timeout_seconds()} seconds. It may still be running; no "
            "recommendations were created from it.",
            job_run,
        )

    # 5. A run that did not succeed produces nothing - its tables may still
    # hold an earlier run's rows, which must never be read as this run's.
    if job_run.status != JobStatus.COMPLETED.value:
        return failed(
            "NOTEBOOK_RUN_FAILED",
            f"Databricks run {run_id} ended {job_run.status}"
            + (f": {job_run.error}" if job_run.error else ".")
            + " No recommendations were created.",
            job_run,
        )

    # 6. Read the notebook's result tables with the existing SELECT-only reader.
    reader = DatabricksSQLReader(
        warehouse_id=_resolve_sql_warehouse_id(resources),
        access_token=access_token,
    )
    try:
        finding_rows = await reader.execute(f"SELECT * FROM {NOTEBOOK_FINDINGS_TABLE}")
    except Exception as exc:  # noqa: BLE001 - reader and transport faults alike are reported, never masked
        return failed(
            "NOTEBOOK_RESULTS_UNAVAILABLE",
            f"Databricks run {run_id} completed, but {NOTEBOOK_FINDINGS_TABLE} "
            f"could not be read: {str(exc)[:300]}",
            job_run,
        )

    # The action queue only enriches findings; the notebook writes it only when
    # it has actions. Its absence is recorded, not fatal.
    action_queue_error = None
    try:
        action_rows = await reader.execute(f"SELECT * FROM {NOTEBOOK_ACTION_QUEUE_TABLE}")
    except Exception as exc:  # noqa: BLE001 - optional enrichment
        action_rows = []
        action_queue_error = str(exc)[:300]

    # Stale-result protection: every findings row must have been written by
    # THIS run. If any predates it, the table does not reflect this run alone.
    stale = _rows_not_from_this_run(finding_rows, job_run.started_at)
    if stale:
        return failed(
            "NOTEBOOK_RESULTS_STALE",
            f"Databricks run {run_id} completed, but {len(stale)} of "
            f"{len(finding_rows)} row(s) in {NOTEBOOK_FINDINGS_TABLE} were not "
            "written by this run, so they cannot be attributed to it. No "
            "recommendations were created.",
            job_run,
        )

    # 7. Notebook rows -> the existing finding contract.
    adaptation = adapt_notebook_results(
        finding_rows,
        action_rows,
        customer_id=environment.customer_id,
        environment_id=environment.id,
        workspace_name=workspace_name,
    )

    # 8. The existing persistence - the same records the approval flow uses.
    recommendations = persist_recommendations(db, adaptation.findings)

    # The configuration analysis of discovered resources is unchanged; it feeds
    # classification, opportunities and the report exactly as before.
    analysis = analyze_compute(
        workspace_name=workspace_name,
        resources=resources,
        statuses=statuses,
        llm=llm,
    )

    optimization_result = {
        "status": "ANALYZED",
        "evidence_source": "cluster_optimization_notebook",
        "execution": {
            "acelo_run_id": job_run.id,
            "platform_run_id": run_id,
            "platform_resource_id": job_run.platform_resource_id,
            "status": job_run.status,
        },
        "result_tables": {
            NOTEBOOK_FINDINGS_TABLE: {"rows": len(finding_rows)},
            NOTEBOOK_ACTION_QUEUE_TABLE: {
                "rows": len(action_rows),
                "error": action_queue_error,
            },
        },
        "findings": adaptation.findings,
        "summary": {
            "clusters_analyzed": len({f["resource_id"] for f in adaptation.findings}),
            "actionable_findings": len(adaptation.findings),
            "recommendations_created_or_updated": len(recommendations),
            "rejected_notebook_rows": adaptation.reasons(),
            "execution_enabled": False,
            "human_approval_required": True,
            "savings_status": "NOT_ESTIMATED",
        },
        "recommendations": recommendations,
    }

    steps["analyze"].status = StepStatus.DONE
    steps["analyze"].detail = (
        f"Databricks run {run_id}: {len(recommendations)} recommendation"
        f"{'' if len(recommendations) == 1 else 's'} from the Cluster "
        "Optimization notebook"
    )

    logger.info(
        "agent_notebook_analysis env_id=%s platform_run_id=%s rows=%d findings=%d "
        "recommendations=%d rejected=%s",
        environment.id,
        run_id,
        len(finding_rows),
        len(adaptation.findings),
        len(recommendations),
        adaptation.reasons(),
    )

    return AgentAnalysisResult(
        ok=True,
        status=CapabilityStatus.OK,
        steps=order,
        environment_id=environment.id,
        analysis={
            **analysis.to_dict(),
            "compute_optimization": optimization_result,
        },
        markdown=render_markdown(analysis),
    )


async def analyze_databricks_compute(
    db: Session,
    environment: Environment,
    llm: Callable[[str], str] | None = None,
    access_token: str | None = None,
) -> AgentAnalysisResult:
    """
    Runs the full discover -> analyze -> recommend flow against the given ACELO
    environment.

    With a registered cluster execution target, cluster optimization comes
    from the Cluster Optimization notebook run through the existing execution
    path (see _analyze_with_notebook). Without one, the read-only evidence
    analysis runs as before. Neither changes a Databricks resource.

    The environment argument is what determines the Databricks workspace —
    no workspace URL, workspace id, customer, cluster or warehouse is hardcoded
    anywhere in this flow.
    """

    steps = _steps()
    order = list(steps.values())

    # 1. The agent knows which environment (and therefore which workspace)
    # it is working against only because the caller resolved one.
    steps["environment"].status = StepStatus.DONE
    steps["environment"].detail = (
        environment.workspace_name or environment.name
    )

    # 2-4. Call the capability. This is where the real Databricks calls happen.
    result = await discover_databricks_resources(
        db,
        environment,
        access_token=access_token,
    )

    if not result.ok:
        # Authentication/authorization is proven by the call, so a failure
        # marks the auth step failed and leaves everything after it pending.
        failed_step = (
            "auth"
            if result.status
            in (
                CapabilityStatus.AUTHENTICATION_FAILED,
                CapabilityStatus.AUTHORIZATION_FAILED,
                CapabilityStatus.NOT_CONFIGURED,
            )
            else "discover"
        )

        steps[failed_step].status = StepStatus.FAILED
        steps[failed_step].detail = result.message

        return AgentAnalysisResult(
            ok=False,
            status=result.status,
            steps=order,
            message=result.message,
            environment_id=environment.id,
        )

    # The credential worked.
    steps["auth"].status = StepStatus.DONE
    steps["discover"].status = StepStatus.DONE

    resources = result.data.get("resources", [])
    statuses = result.data.get("statuses", [])

    steps["resources"].status = StepStatus.DONE
    steps["resources"].detail = f"{len(resources)} resource(s)"

    # ------------------------------------------------------------------
    # Cluster optimization from the notebook, when one is registered.
    #
    # The registry decides, not this code: a registered job or notebook makes
    # the Cluster Optimization notebook the source of truth. Once a target is
    # registered, a failed run is reported as failed - the evidence analysis
    # below is never substituted for it.
    # ------------------------------------------------------------------
    cluster_target = _registered_cluster_target(db, environment)
    if cluster_target is not None:
        return await _analyze_with_notebook(
            db=db,
            environment=environment,
            access_token=access_token,
            llm=llm,
            steps=steps,
            order=order,
            discovery=result.data,
            target=cluster_target,
        )

    # ------------------------------------------------------------------
    # Read compute evidence through Databricks SQL.
    #
    # These are the actual ACELO evidence sources.
    # ------------------------------------------------------------------
    sql_warehouse_id = _resolve_sql_warehouse_id(resources)

    reader = DatabricksSQLReader(
    warehouse_id=sql_warehouse_id,
    access_token=access_token,
)

    evidence_tables = (
        "cluster",
        "node_timeline",
        "node_types",
        "instance_events",
        "billing_usage",
        "job_task_run_timeline",
    )

    try:
        evidence_rows = await reader.read_compute_evidence()
    except Exception:
        logger.exception(
            "agent_compute_evidence_read_failed env_id=%s",
            environment.id,
        )
        evidence_rows = {table: [] for table in evidence_tables}
        evidence_rows["errors"] = {
            table: "READ_FAILED"
            for table in evidence_tables
        }

    # ------------------------------------------------------------------
    # Stage 1 compute inventory
    #
    # Preserve the existing REST discovery contract by default.
    #
    # IMPORTANT:
    # A table-backed inventory is used only when Databricks explicitly tells
    # us that classic-cluster discovery is unavailable. We do NOT treat a
    # merely empty resource list as permission failure.
    #
    # This allows the existing discovery tests to remain valid while allowing
    # the Databricks App demo workspace to analyze the real
    # databricks_ws.agent.cluster table when classic-cluster listing is
    # unavailable.
    # ------------------------------------------------------------------
    discovered_clusters = sorted(
        (
            resource
            for resource in resources
            if resource.get("resource_type") == "CLASSIC_CLUSTER"
        ),
        key=lambda resource: str(resource.get("resource_id") or ""),
    )

    cluster_inventory_source = "databricks_discovery"

    if (
        not discovered_clusters
        and _classic_cluster_listing_unavailable(statuses)
    ):
        table_clusters: dict[str, dict[str, Any]] = {}

        for row in evidence_rows.get("cluster", []):
            cluster_id = row.get("cluster_id")

            if cluster_id is None or str(cluster_id).strip() == "":
                continue

            cluster_id = str(cluster_id).strip()

            # Keep one inventory record per cluster. The evidence table may
            # contain multiple observations for the same cluster.
            if cluster_id in table_clusters:
                continue

            cluster_name = row.get("cluster_name")

            if cluster_name is None or str(cluster_name).strip() == "":
                cluster_name = cluster_id

            table_clusters[cluster_id] = {
                "platform": "databricks",
                "resource_type": "CLASSIC_CLUSTER",
                "resource_id": cluster_id,
                "name": str(cluster_name),
                "state": row.get("state"),
                "metadata": dict(row),
            }

        discovered_clusters = sorted(
            table_clusters.values(),
            key=lambda resource: str(
                resource.get("resource_id") or ""
            ),
        )

        if discovered_clusters:
            cluster_inventory_source = "databricks_ws.agent.cluster"

            steps["discover"].detail = (
                "Live classic-cluster listing unavailable; "
                f"using {len(discovered_clusters)} cluster(s) from "
                "databricks_ws.agent.cluster for Stage 1 analysis."
            )

            steps["resources"].detail = (
                f"{len(discovered_clusters)} Stage 1 cluster(s) "
                "from databricks_ws.agent.cluster"
            )
        else:
            steps["discover"].detail = (
                "Live classic-cluster listing unavailable and "
                "databricks_ws.agent.cluster returned no usable cluster rows."
            )

            steps["resources"].detail = (
                "0 Stage 1 clusters available from "
                "databricks_ws.agent.cluster"
            )

    discovered_ids = {
        resource.get("resource_id")
        for resource in discovered_clusters
        if resource.get("resource_id")
    }

    collected_at = (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )

    cluster_results: list[dict[str, Any]] = []
    all_findings = []

    # ------------------------------------------------------------------
    # Evidence-backed Stage 1 analysis.
    #
    # Every cluster here is either:
    #   1. a real REST-discovered classic cluster, or
    #   2. a real cluster row from databricks_ws.agent.cluster when the
    #      classic-cluster API is explicitly unavailable.
    #
    # Utilization, worker history, runtime, billing and task evidence are
    # always read from the approved databricks_ws.agent.* tables.
    # ------------------------------------------------------------------
    for resource in discovered_clusters:
        evidence = _compute_evidence_for_cluster(
            environment=environment,
            resource=resource,
            evidence_rows=evidence_rows,
            workspace_name=result.data.get("workspace_name"),
            collected_at=collected_at,
        )

        cluster_result = analyze_compute_evidence(
            evidence,
            evidence_source="databricks_sql",
        )

        all_findings.extend(cluster_result.findings)

        cluster_results.append(
            {
                "cluster_id": resource.get("resource_id"),
                "cluster_name": resource.get("name"),
                "lineage": dict(evidence.lineage),
                "collected_at": evidence.collected_at,
                "evidence_quality": {
                    "source_available": (
                        evidence.quality.source_available
                    ),
                    "completeness": evidence.quality.completeness,
                    "freshness": evidence.quality.freshness,
                    "timestamp_valid": (
                        evidence.quality.timestamp_valid
                    ),
                    "consistency": evidence.quality.consistency,
                    "missing_fields": (
                        evidence.quality.missing_fields
                    ),
                    "notes": evidence.quality.notes,
                },
                "observation_start": evidence.observation_start,
                "observation_end": evidence.observation_end,
                "runtime_activity_state": (
                    cluster_result.summary["runtime_activity_state"]
                ),
                "rule_evaluations": (
                    cluster_result.summary["rule_evaluations"]
                ),
                "findings_count": len(cluster_result.findings),
            }
        )

    # ------------------------------------------------------------------
    # Evidence rows that do not belong to an analyzed cluster remain visible
    # as ignored/unmatched evidence rather than being silently fabricated into
    # resources.
    # ------------------------------------------------------------------
    unmatched_rows: dict[str, int] = {}

    for table_name in (
        "cluster",
        "node_timeline",
        "billing_usage",
        "job_task_run_timeline",
    ):
        unmatched_rows[table_name] = sum(
            row.get("cluster_id") not in discovered_ids
            for row in evidence_rows.get(table_name, [])
        )

    evaluable_clusters = sum(
        any(
            evaluation["status"] != "NOT_EVALUABLE"
            for evaluation in cluster["rule_evaluations"].values()
        )
        for cluster in cluster_results
    )

    recommendations = persist_recommendations(
        db,
        all_findings,
    )

    optimization_result = {
        "status": "ANALYZED",
        "evidence_source": "databricks_sql",
        "cluster_inventory_source": cluster_inventory_source,
        "evidence_tables": {
            table_name: {
                "source": f"databricks_ws.agent.{table_name}",
                "rows": len(evidence_rows.get(table_name, [])),
            }
            for table_name in evidence_tables
        },
        "findings": [
            finding.__dict__
            for finding in all_findings
        ],
        "summary": {
            "clusters_analyzed": len(discovered_clusters),
            "clusters_evaluable": evaluable_clusters,
            "actionable_findings": len(all_findings),
            "recommendations_created_or_updated": len(
                recommendations
            ),
            "cluster_results": cluster_results,
            "failed_sources": sorted(
                evidence_rows.get("errors", {}).keys()
            ),
            "ignored_unmatched_evidence_rows": unmatched_rows,
            "execution_enabled": False,
            "human_approval_required": True,
            "savings_status": "NOT_ESTIMATED",
        },
        "recommendations": recommendations,
    }

    # ------------------------------------------------------------------
    # 5. Existing analysis contract.
    #
    # DO NOT replace this with discovered_clusters or statuses=[].
    # Existing tests and UI contracts depend on REST discovery being supplied
    # to the established configuration analyzer.
    #
    # Stage 1 evidence/recommendations are carried separately in
    # compute_optimization above.
    # ------------------------------------------------------------------
    analysis = analyze_compute(
        workspace_name=result.data.get("workspace_name"),
        resources=resources,
        statuses=statuses,
        llm=llm,
    )

    steps["analyze"].status = StepStatus.DONE
    steps["analyze"].detail = (
        f"{len(analysis.findings)} potential "
        f"opportunit{'y' if len(analysis.findings) == 1 else 'ies'}"
    )

    logger.info(
        "agent_databricks_analysis env_id=%s resources=%d "
        "stage1_clusters=%d findings=%d",
        environment.id,
        len(resources),
        len(discovered_clusters),
        len(analysis.findings),
    )

    return AgentAnalysisResult(
        ok=True,
        status=CapabilityStatus.OK,
        steps=order,
        environment_id=environment.id,
        analysis={
            **analysis.to_dict(),
            "compute_optimization": optimization_result,
        },
        markdown=render_markdown(analysis),
    )