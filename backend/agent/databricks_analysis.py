"""
Evidence-bound analysis of discovered Databricks compute.

The rule this module exists to enforce: every statement traces to a field
Databricks actually returned. Discovery sees CONFIGURATION, not behaviour — it
cannot see utilization, DBU consumption, idle time, cost or query latency. So
this analyzer:

  * derives findings only from configuration values present in the evidence
  * never computes or estimates a saving, a cost, or a percentage
  * states impact qualitatively
  * lists what it would need to observe before anyone acts, as Missing Evidence

A finding here is a QUESTION RAISED BY CONFIGURATION, not a verdict. "Auto-
termination is disabled" is a fact; "this cluster wastes $500/month" would be an
invention, and nothing in this file can produce one.

The optional LLM pass only rewrites the summary narrative. It is given the
evidence and is rejected outright if it emits a currency amount or a savings
percentage, so a talkative model cannot smuggle a fabricated number into the
answer.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from platforms.databricks_resources import ResourceStatus, ResourceType

logger = logging.getLogger(__name__)

# What discovery structurally cannot see. Listed on every analysis so the user
# is never left assuming these were checked and found fine.
MISSING_EVIDENCE = [
    "CPU and memory utilization — not exposed by the compute or SQL warehouse APIs.",
    "Idle time and uptime history — requires cluster event history, not read in this phase.",
    "DBU consumption and cost — requires billing/system tables, deliberately not read in this phase.",
    "Query volume, latency and queueing — requires query history, not read in this phase.",
    "Job run frequency against each resource — requires the jobs and runs APIs.",
]

# A recommendation is only actionable once these are observed. Kept per finding
# so the next phase knows exactly what to collect.
_EVIDENCE_FOR_SIZING = "Utilization and job history for this resource over a representative period."
_EVIDENCE_FOR_IDLE = "Cluster event history showing real idle periods between runs."
_EVIDENCE_FOR_WAREHOUSE_IDLE = "Warehouse query history showing idle gaps between queries."


@dataclass
class Finding:
    """One potential optimization opportunity, tied to its evidence."""

    resource: str
    resource_type: str
    resource_id: str
    observed_evidence: str
    potential_issue: str
    recommendation: str
    evidence_required: str
    expected_impact: str  # qualitative only — never a currency or percentage

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource": self.resource,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "observed_evidence": self.observed_evidence,
            "potential_issue": self.potential_issue,
            "recommendation": self.recommendation,
            "evidence_required": self.evidence_required,
            "expected_impact": self.expected_impact,
        }


@dataclass
class Analysis:
    """The full structured answer: facts, classification, gaps, opportunities."""

    workspace_name: str | None
    resources: list[dict[str, Any]] = field(default_factory=list)
    statuses: list[dict[str, Any]] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)
    summary: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_name": self.workspace_name,
            "observed_facts": {
                "resource_count": len(self.resources),
                "by_type": _counts_by_type(self.resources),
                "resources": self.resources,
            },
            "classification": [_classification_row(r) for r in self.resources],
            "statuses": self.statuses,
            "missing_evidence": self.missing_evidence,
            "opportunities": [f.to_dict() for f in self.findings],
            "summary": self.summary,
        }


def _counts_by_type(resources: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for resource in resources:
        counts[resource["resource_type"]] = counts.get(resource["resource_type"], 0) + 1
    return counts


def _relevant_config(resource: dict[str, Any]) -> str:
    """
    The configuration worth showing per type, built only from keys the platform
    actually returned. A key Databricks omitted is simply not shown.
    """
    md = resource.get("metadata") or {}
    parts: list[str] = []

    if resource["resource_type"] == ResourceType.CLASSIC_CLUSTER:
        if md.get("worker_node_type"):
            parts.append(f"worker {md['worker_node_type']}")
        autoscale = md.get("autoscaling")
        if isinstance(autoscale, dict):
            parts.append(f"autoscale {autoscale.get('min_workers')}-{autoscale.get('max_workers')}")
        elif md.get("num_workers") is not None:
            parts.append(f"fixed {md['num_workers']} workers")
        if md.get("auto_termination_minutes") is not None:
            parts.append(f"auto-termination {md['auto_termination_minutes']} min")
        if md.get("spark_version"):
            parts.append(f"runtime {md['spark_version']}")
    elif resource["resource_type"] == ResourceType.SQL_WAREHOUSE:
        if md.get("warehouse_type"):
            parts.append(str(md["warehouse_type"]))
        if md.get("size"):
            parts.append(f"size {md['size']}")
        if md.get("auto_stop_mins") is not None:
            parts.append(f"auto-stop {md['auto_stop_mins']} min")
        scaling = md.get("scaling")
        if isinstance(scaling, dict):
            parts.append(f"clusters {scaling.get('min_num_clusters')}-{scaling.get('max_num_clusters')}")
    else:
        # Serverless compute has no fixed schema; show what came back, minus
        # the fields already displayed as name/id/state.
        shown = {resource.get("name"), resource.get("resource_id"), resource.get("state")}
        parts = [f"{k} {v}" for k, v in md.items() if k != "discovered_from" and v not in shown]

    return ", ".join(str(p) for p in parts) if parts else "—"


def _classification_row(resource: dict[str, Any]) -> dict[str, Any]:
    return {
        "resource": resource.get("name"),
        "type": resource.get("resource_type"),
        "state": resource.get("state") or "UNKNOWN",
        "configuration": _relevant_config(resource),
    }


# --- findings ---------------------------------------------------------------
# Each rule fires ONLY on a value Databricks returned. A rule never fires on a
# missing field, because absence is not evidence.


def _classic_cluster_findings(resource: dict[str, Any]) -> list[Finding]:
    md = resource.get("metadata") or {}
    name = resource.get("name") or resource.get("resource_id") or ""
    findings: list[Finding] = []

    auto_termination = md.get("auto_termination_minutes")
    if auto_termination == 0:
        findings.append(
            Finding(
                resource=name,
                resource_type=ResourceType.CLASSIC_CLUSTER,
                resource_id=resource.get("resource_id", ""),
                observed_evidence="auto_termination_minutes = 0",
                potential_issue=(
                    "Auto-termination is disabled, so this cluster stays running until it is "
                    "stopped manually."
                ),
                recommendation="Consider enabling an auto-termination policy appropriate to how this cluster is used.",
                evidence_required=_EVIDENCE_FOR_IDLE,
                expected_impact="Qualitative: removes compute time that is paid for but not used.",
            )
        )
    elif isinstance(auto_termination, int) and auto_termination >= 120:
        findings.append(
            Finding(
                resource=name,
                resource_type=ResourceType.CLASSIC_CLUSTER,
                resource_id=resource.get("resource_id", ""),
                observed_evidence=f"auto_termination_minutes = {auto_termination}",
                potential_issue="The auto-termination window is long, so an idle cluster stays up for a while.",
                recommendation="Consider whether a shorter window suits this cluster's usage pattern.",
                evidence_required=_EVIDENCE_FOR_IDLE,
                expected_impact="Qualitative: shortens paid idle time between runs.",
            )
        )

    # Fixed-size clusters cannot shed capacity when a workload is small. This is
    # a configuration observation, NOT a claim that the cluster is oversized.
    if md.get("autoscaling") is None and isinstance(md.get("num_workers"), int) and md["num_workers"] > 0:
        findings.append(
            Finding(
                resource=name,
                resource_type=ResourceType.CLASSIC_CLUSTER,
                resource_id=resource.get("resource_id", ""),
                observed_evidence=f"autoscaling not configured, num_workers = {md['num_workers']}",
                potential_issue=(
                    "The cluster is a fixed size, so it holds the same worker count regardless of load."
                ),
                recommendation="Consider autoscaling if this cluster's workload varies.",
                evidence_required=_EVIDENCE_FOR_SIZING,
                expected_impact="Qualitative: matches capacity to demand instead of holding a fixed footprint.",
            )
        )

    if md.get("single_user_name") and md.get("data_security_mode") == "SINGLE_USER":
        findings.append(
            Finding(
                resource=name,
                resource_type=ResourceType.CLASSIC_CLUSTER,
                resource_id=resource.get("resource_id", ""),
                observed_evidence="data_security_mode = SINGLE_USER",
                potential_issue="This is a single-user cluster, which cannot be shared across a team.",
                recommendation="Consider whether a shared or serverless option fits this workload.",
                evidence_required=_EVIDENCE_FOR_SIZING,
                expected_impact="Qualitative: may reduce the number of separate clusters kept running.",
            )
        )

    return findings


def _sql_warehouse_findings(resource: dict[str, Any]) -> list[Finding]:
    md = resource.get("metadata") or {}
    name = resource.get("name") or resource.get("resource_id") or ""
    findings: list[Finding] = []

    auto_stop = md.get("auto_stop_mins")
    if auto_stop == 0:
        findings.append(
            Finding(
                resource=name,
                resource_type=ResourceType.SQL_WAREHOUSE,
                resource_id=resource.get("resource_id", ""),
                observed_evidence="auto_stop_mins = 0",
                potential_issue="Auto-stop is disabled, so this warehouse stays running between queries.",
                recommendation="Consider enabling auto-stop with a window suited to this warehouse's query pattern.",
                evidence_required=_EVIDENCE_FOR_WAREHOUSE_IDLE,
                expected_impact="Qualitative: removes warehouse uptime that serves no queries.",
            )
        )
    elif isinstance(auto_stop, int) and auto_stop >= 60:
        findings.append(
            Finding(
                resource=name,
                resource_type=ResourceType.SQL_WAREHOUSE,
                resource_id=resource.get("resource_id", ""),
                observed_evidence=f"auto_stop_mins = {auto_stop}",
                potential_issue="The auto-stop window is long, so the warehouse idles for a while after use.",
                recommendation="Consider whether a shorter auto-stop window suits this warehouse.",
                evidence_required=_EVIDENCE_FOR_WAREHOUSE_IDLE,
                expected_impact="Qualitative: shortens paid idle time between queries.",
            )
        )

    scaling = md.get("scaling")
    if isinstance(scaling, dict):
        minimum = scaling.get("min_num_clusters")
        maximum = scaling.get("max_num_clusters")
        if isinstance(minimum, int) and minimum > 1:
            findings.append(
                Finding(
                    resource=name,
                    resource_type=ResourceType.SQL_WAREHOUSE,
                    resource_id=resource.get("resource_id", ""),
                    observed_evidence=f"min_num_clusters = {minimum}",
                    potential_issue="The warehouse holds more than one cluster even at its minimum.",
                    recommendation="Consider whether the minimum cluster count matches real concurrency.",
                    evidence_required="Warehouse query history showing concurrent query volume.",
                    expected_impact="Qualitative: avoids holding concurrency capacity that is not used.",
                )
            )
        if isinstance(minimum, int) and isinstance(maximum, int) and minimum == maximum:
            findings.append(
                Finding(
                    resource=name,
                    resource_type=ResourceType.SQL_WAREHOUSE,
                    resource_id=resource.get("resource_id", ""),
                    observed_evidence=f"min_num_clusters = max_num_clusters = {minimum}",
                    potential_issue="Scaling is pinned, so the warehouse cannot add capacity under load.",
                    recommendation="Consider a scaling range if query concurrency varies.",
                    evidence_required="Warehouse query history showing queueing under peak load.",
                    expected_impact="Qualitative: affects query queueing at peak rather than cost.",
                )
            )

    return findings


_FINDING_RULES = {
    ResourceType.CLASSIC_CLUSTER: _classic_cluster_findings,
    ResourceType.SQL_WAREHOUSE: _sql_warehouse_findings,
    # Serverless compute exposes no sizing or termination configuration to
    # tune, so there is deliberately no rule for it. Inventing one would mean
    # inventing evidence.
}


# --- optional LLM narrative -------------------------------------------------

# Anything that looks like money or a savings percentage. The analyzer never
# produces these, so their presence means the model invented a number.
_FABRICATION_PATTERNS = (
    re.compile(r"[$£€]\s?\d"),
    re.compile(r"\b\d+(\.\d+)?\s?%"),
    re.compile(r"\b(usd|dbu|dbus)\b", re.IGNORECASE),
    re.compile(r"\bsave[sd]?\s+\d", re.IGNORECASE),
)


def looks_fabricated(text: str) -> bool:
    """True if the text contains a number this analysis has no evidence for."""
    return any(pattern.search(text) for pattern in _FABRICATION_PATTERNS)


def _summary_prompt(analysis: "Analysis") -> str:
    lines = [
        "You are an ACELO FinOps optimization agent. Summarise the analysis below in 2-4 sentences.",
        "",
        # The agent's execution context. It names the platform so the model
        # cannot reason about, or mention, the platform the user is not in.
        "ACTIVE PLATFORM: DATABRICKS",
        f"ACTIVE CONNECTION: {analysis.workspace_name or 'the connected Databricks workspace'}",
        "",
        "STRICT RULES:",
        "- Use Databricks resources, APIs and tools only.",
        "- Never mention Microsoft Fabric, or any Fabric resource, notebook, pipeline or lakehouse.",
        "- Use ONLY the evidence given. Do not add any resource, metric or number that is not listed.",
        "- Never state a cost, a saving, a percentage, a DBU figure or a utilization number.",
        "- These were NOT measured: utilization, idle time, cost, query volume.",
        "- Describe findings as configuration observations that need verification, not as proven waste.",
        "",
        f"Workspace: {analysis.workspace_name}",
        f"Resources discovered: {len(analysis.resources)}",
    ]
    for row in (_classification_row(r) for r in analysis.resources):
        lines.append(f"- {row['resource']} | {row['type']} | {row['state']} | {row['configuration']}")
    if analysis.findings:
        lines.append("")
        lines.append("Configuration observations:")
        for finding in analysis.findings:
            lines.append(f"- {finding.resource}: {finding.observed_evidence} -> {finding.potential_issue}")
    else:
        lines.append("")
        lines.append("No configuration observations were raised.")
    return "\n".join(lines)


def _deterministic_summary(analysis: "Analysis") -> str:
    counts = _counts_by_type(analysis.resources)
    parts = [f"{count} {label.replace('_', ' ').lower()}" for label, count in sorted(counts.items())]
    discovered = ", ".join(parts) if parts else "no compute resources"

    if not analysis.resources:
        return (
            "No compute resources were discovered in this workspace with the current permissions. "
            "See the status of each resource type below."
        )
    if not analysis.findings:
        return (
            f"Discovered {discovered}. No configuration observations were raised from the settings "
            "visible to discovery. Utilization, idle time and cost were not measured, so this is not "
            "a statement that the workspace is optimally configured."
        )
    return (
        f"Discovered {discovered}, and raised {len(analysis.findings)} configuration "
        f"observation{'s' if len(analysis.findings) != 1 else ''} worth reviewing. These come from "
        "configuration alone — utilization, idle time and cost were not measured, so each one needs "
        "the listed evidence before anyone acts on it."
    )


def analyze_compute(
    workspace_name: str | None,
    resources: list[dict[str, Any]],
    statuses: list[dict[str, Any]],
    llm: Callable[[str], str] | None = None,
) -> Analysis:
    """
    Turns discovered resources into an evidence-bound analysis.

    `llm` is optional and only writes the summary narrative. When it is absent,
    fails, or returns something containing an invented number, the deterministic
    summary is used instead — the analysis never degrades into guesswork.
    """
    analysis = Analysis(workspace_name=workspace_name, resources=resources, statuses=statuses)

    for resource in resources:
        rule = _FINDING_RULES.get(resource.get("resource_type", ""))
        if rule:
            analysis.findings.extend(rule(resource))

    analysis.missing_evidence = list(MISSING_EVIDENCE)
    # A resource type that could not be read is itself missing evidence: the
    # user must not read its absence as "you have none of these".
    for status in statuses:
        if status.get("status") != ResourceStatus.OK:
            analysis.missing_evidence.append(
                f"{status['resource_type']} could not be read "
                f"({status.get('reason', 'UNKNOWN')}), so none are included above."
            )

    analysis.summary = _deterministic_summary(analysis)

    if llm is not None:
        try:
            narrative = (llm(_summary_prompt(analysis)) or "").strip()
        except Exception:  # noqa: BLE001 - a model fault must not fail the analysis
            logger.exception("agent_summary_llm_failed")
            narrative = ""
        if narrative and not narrative.startswith("LLM_UNAVAILABLE"):
            if looks_fabricated(narrative):
                # The model produced a number nothing here supports. Drop it
                # rather than show an invented figure.
                logger.warning("agent_summary_rejected reason=fabricated_metric")
            else:
                analysis.summary = narrative

    return analysis
