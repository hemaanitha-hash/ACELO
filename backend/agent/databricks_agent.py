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
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy.orm import Session

from agent.capabilities import CapabilityStatus, discover_databricks_resources
from agent.databricks_analysis import analyze_compute
from agent.databricks_report import render_markdown
from models import Environment

from services.compute_optimization.databricks_reader import DatabricksSQLReader
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


def _cluster_configuration(resource: dict[str, Any], sql_rows: list[dict[str, Any]]) -> tuple[dict[str, Any], bool]:
    resource_id = resource.get("resource_id")
    sql_cluster = next(
        (row for row in sql_rows if row.get("cluster_id") == resource_id),
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
        configuration["auto_termination_minutes"] = metadata.get("auto_termination_minutes")
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
        if row.get("cluster_id") == cluster_id
    ]
    runtime = derive_post_task_idle(
        cluster_id or "",
        normalized.utilization,
        task_rows,
        idle_cpu_threshold=IDLE_CPU,
        idle_memory_threshold=IDLE_MEMORY,
    ) or {}

    node_source_failed = errors.get("node_timeline") == "READ_FAILED"
    missing_fields = []
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

    notes = []
    if normalized.error:
        notes.append(normalized.error)
    if node_source_failed:
        notes.append("Node timeline source failed to read.")
    if not runtime:
        notes.append("No exact same-cluster task-end to contiguous idle-bucket mapping was available.")
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
            if row.get("cluster_id") == cluster_id
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


async def analyze_databricks_compute(
    db: Session,
    environment: Environment,
    llm: Callable[[str], str] | None = None,
    access_token: str | None = None,
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
            if result.status in (
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

    # The credential worked: at least one resource type was listed.
    steps["auth"].status = StepStatus.DONE
    steps["discover"].status = StepStatus.DONE

    resources = result.data.get("resources", [])
    statuses = result.data.get("statuses", [])

    steps["resources"].status = StepStatus.DONE
    steps["resources"].detail = f"{len(resources)} resource(s)"

    # Read compute evidence through Databricks SQL.
    reader = DatabricksSQLReader(
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
        logger.exception("agent_compute_evidence_read_failed env_id=%s", environment.id)
        evidence_rows = {table: [] for table in evidence_tables}
        evidence_rows["errors"] = {table: "READ_FAILED" for table in evidence_tables}

        # ------------------------------------------------------------------
    # Stage 1 demo cluster inventory
    #
    # Prefer live classic-cluster discovery when it is available.
    # For the current demo workspace, classic-cluster listing is not
    # permitted, so use the existing Databricks evidence table
    # `databricks_ws.agent.cluster` as the authoritative cluster inventory.
    #
    # This is still real Databricks workspace data. No cluster is created,
    # modified, started, stopped, or otherwise mutated.
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

    if not discovered_clusters:
        table_clusters: dict[str, dict[str, Any]] = {}

        for row in evidence_rows.get("cluster", []):
            cluster_id = row.get("cluster_id")

            if cluster_id is None or str(cluster_id).strip() == "":
                continue

            cluster_id = str(cluster_id).strip()

            # One evidence-table row per cluster is enough to build the
            # Stage 1 demo inventory. Keep the first valid row so that
            # repeated source rows do not create duplicate resources.
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
            key=lambda resource: str(resource.get("resource_id") or ""),
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

    discovered_ids = {
        resource.get("resource_id")
        for resource in discovered_clusters
        if resource.get("resource_id")
    }

    collected_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    cluster_results = []
    all_findings = []

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
        cluster_results.append({
            "cluster_id": resource.get("resource_id"),
            "cluster_name": resource.get("name"),
            "lineage": dict(evidence.lineage),
            "collected_at": evidence.collected_at,
            "evidence_quality": {
                "source_available": evidence.quality.source_available,
                "completeness": evidence.quality.completeness,
                "freshness": evidence.quality.freshness,
                "timestamp_valid": evidence.quality.timestamp_valid,
                "consistency": evidence.quality.consistency,
                "missing_fields": evidence.quality.missing_fields,
                "notes": evidence.quality.notes,
            },
            "observation_start": evidence.observation_start,
            "observation_end": evidence.observation_end,
            "runtime_activity_state": cluster_result.summary["runtime_activity_state"],
            "rule_evaluations": cluster_result.summary["rule_evaluations"],
            "findings_count": len(cluster_result.findings),
        })

    unmatched_rows = {}
    for table_name in ("cluster", "node_timeline", "billing_usage", "job_task_run_timeline"):
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
    recommendations = persist_recommendations(db, all_findings)
    optimization_result = {
        "status": "ANALYZED",
        "evidence_source": "databricks_sql",
        "cluster_inventory_source": cluster_inventory_source,
        "findings": [finding.__dict__ for finding in all_findings],
        "summary": {
            "clusters_analyzed": len(discovered_clusters),
            "clusters_evaluable": evaluable_clusters,
            "actionable_findings": len(all_findings),
            "recommendations_created_or_updated": len(recommendations),
            "cluster_results": cluster_results,
            "failed_sources": sorted(evidence_rows.get("errors", {}).keys()),
            "ignored_unmatched_evidence_rows": unmatched_rows,
            "execution_enabled": False,
            "human_approval_required": True,
            "savings_status": "NOT_ESTIMATED",
        },
        "recommendations": recommendations,
    }

    # 5. Existing analysis contract.
    # This remains intact so the existing UI/reporting behavior does not break.
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
        analysis={
            **analysis.to_dict(),
            "compute_optimization": optimization_result,
        },
        markdown=render_markdown(analysis),
    )