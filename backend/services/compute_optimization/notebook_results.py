"""
Adapter: Cluster Optimization notebook result rows -> ACELO finding contract.

The notebook (compute_cost_agent_full) is the source of truth for cluster
optimization logic. It writes its conclusions to two Delta tables:

    default.compute_agent_findings       one row per (cluster, finding)
    default.compute_agent_action_queue   the human-approval queue for those findings

This module converts those rows into the finding shape that the EXISTING
recommendation contract already accepts, so the notebook's output flows through
the established path unchanged:

    notebook rows -> adapt_notebook_results() -> persist_recommendations()

What this module deliberately does NOT do:

  * analyse anything. Every optimization conclusion is the notebook's; nothing
    here re-derives, second-guesses or recomputes a finding.
  * read Databricks. The caller supplies rows (services.compute_optimization
    .databricks_reader already executes the SELECTs).
  * write to the database. The caller passes the result to the existing
    persist_recommendations().
  * call an LLM or any external service.

It is a pure function of (rows, context): the same input always produces the
same output, which is what makes the resulting recommendation identities stable
across runs.

Honesty rules this adapter follows:

  * A finding the existing mapping does not support is SKIPPED with a reason,
    never re-labelled as a finding that is supported. HIGH_UTILIZATION appears
    in real notebook output and is not an optimization finding in the existing
    contract, so it is rejected explicitly.
  * Severity and confidence are DERIVED from values present in the row. A row
    carrying no impact evidence at all is rejected rather than given an invented
    severity.
  * evidence_quality reports what the row actually contains. COMPLETE is only
    claimed when every required metric for that finding's family is present.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from services.compute_optimization.recommendations import _MAPPING

logger = logging.getLogger("acelo.notebook_results")

# The contract's own gate: only a TRIGGERED finding becomes a recommendation.
# Every row the notebook writes to compute_agent_findings IS a triggered
# finding - the notebook does not record non-findings - so this is a constant,
# not a judgement made here.
EVALUATION_STATUS = "TRIGGERED"

RESOURCE_TYPE = "CLASSIC_CLUSTER"

# Metrics that must ALL be present for a finding family before its evidence can
# be called COMPLETE. Chosen from the columns the notebook actually writes.
_REQUIRED_METRICS: dict[str, tuple[str, ...]] = {
    "CLUSTER_SIZING": ("avg_cpu_percent", "avg_memory_percent"),
    "CLUSTER_RUNTIME": ("idle_ratio", "post_task_idle_minutes"),
    "AUTOSCALING": ("min_autoscale_workers", "max_autoscale_workers"),
}

# Quantified impact, as the notebook's own savings model reports it. At least
# one must be present, otherwise severity cannot be derived from the row and the
# row is rejected rather than assigned a guessed severity.
_IMPACT_METRICS = (
    "predicted_savings_percent",
    "predicted_dbu_savings",
    "total_dbu",
)

# Everything else numeric the notebook reports, carried as observed evidence.
_SUPPORTING_METRICS = (
    "avg_cpu_percent", "peak_cpu_percent", "avg_memory_percent", "peak_memory_percent",
    "worker_count", "min_autoscale_workers", "max_autoscale_workers",
    "recommended_workers", "recommended_min_workers", "recommended_max_workers",
    "post_task_idle_minutes", "idle_node_minutes", "idle_ratio", "time_at_min_ratio",
    "observed_peak_workers", "observed_min_workers", "dbu_per_hour",
    "total_dbu", "predicted_dbu_after", "predicted_dbu_savings",
    "predicted_savings_percent", "predicted_savings_usd",
)

# Configuration the notebook observed, reported as the resource's current state.
_CURRENT_STATE_FIELDS = (
    "cluster_source", "policy_id", "worker_node_type", "worker_count",
    "min_autoscale_workers", "max_autoscale_workers", "dbu_per_hour",
)

# Approval-queue facts worth carrying onto the finding. They describe what the
# notebook judged actionable; they never create a recommendation of their own.
_ACTION_FIELDS = (
    "action_type", "action_supported", "action_status", "requires_human_approval",
)

# Severity thresholds on the notebook's own predicted saving, as a percentage.
_SEVERITY_HIGH_PERCENT = 50.0
_SEVERITY_MEDIUM_PERCENT = 10.0


@dataclass
class NotebookAdaptation:
    """Findings the notebook produced, plus every row that was not usable and why."""

    findings: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)

    def reasons(self) -> dict[str, int]:
        """Rejection reasons and their counts, for logging and diagnosis."""
        counts: dict[str, int] = {}
        for item in self.rejected:
            counts[item["reason"]] = counts.get(item["reason"], 0) + 1
        return counts


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _number(value: Any) -> float | None:
    """A row value as a number, or None. Booleans are not numbers here."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _flag(value: Any) -> bool | None:
    """The SQL reader returns booleans as the strings 'true'/'false'."""
    if isinstance(value, bool):
        return value
    text = _text(value)
    if text is None:
        return None
    lowered = text.lower()
    if lowered in {"true", "t", "1", "yes"}:
        return True
    if lowered in {"false", "f", "0", "no"}:
        return False
    return None


def _timestamp_is_valid(value: Any) -> bool:
    """True when generated_at is a timestamp we can actually read."""
    text = _text(value)
    if text is None:
        return False
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def finding_identity(cluster_id: str, finding: str) -> str:
    """
    Stable identity for one notebook finding.

    (cluster, finding) is the notebook's own row identity - it writes at most
    one row per pair - so hashing it keeps a recommendation's identity constant
    across re-runs. The caller's persist_recommendations() combines this with
    customer and environment to form the final recommendation_id.
    """
    digest = hashlib.sha256(
        "\x1f".join((cluster_id, finding)).encode("utf-8")
    ).hexdigest()
    return f"nb-{digest[:32]}"


def rule_identity(finding: str, optimization_area: str | None, domain: str) -> str:
    """
    Stable rule identifier for a notebook finding.

    Grouped by the notebook's own optimization_area when the row carries one,
    falling back to the contract's domain for that finding so the id stays
    deterministic even for a row that omits the column.
    """
    group = (optimization_area or domain).strip().lower().replace(" ", "_")
    return f"notebook.{group}.{finding.lower()}"


def _action_index(action_rows: Iterable[dict[str, Any]] | None) -> dict[tuple[str, str], dict[str, Any]]:
    """
    Action-queue rows keyed by (cluster_id, finding).

    The queue mirrors the findings table one-for-one, so it is used ONLY to
    enrich the matching finding. It never yields a recommendation of its own,
    which is what keeps one notebook finding to one ACELO recommendation.
    """
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for row in action_rows or []:
        if not isinstance(row, dict):
            continue
        cluster_id = _text(row.get("cluster_id"))
        finding = _text(row.get("finding"))
        if cluster_id is None or finding is None:
            continue
        index.setdefault((cluster_id, finding.upper()), row)
    return index


def _derive_severity(row: dict[str, Any]) -> str | None:
    """
    Severity from the notebook's own predicted saving. None when the row carries
    no impact evidence at all - the caller then rejects the row rather than
    assigning a severity nothing in the data supports.
    """
    percent = _number(row.get("predicted_savings_percent"))
    if percent is not None:
        if percent >= _SEVERITY_HIGH_PERCENT:
            return "HIGH"
        if percent >= _SEVERITY_MEDIUM_PERCENT:
            return "MEDIUM"
        return "LOW"
    # No percentage, but an absolute saving still proves the notebook quantified
    # an impact. Without a ratio there is no basis to call it more than LOW.
    for name in ("predicted_dbu_savings", "total_dbu"):
        if _number(row.get(name)) is not None:
            return "LOW"
    return None


def _evidence_completeness(row: dict[str, Any], domain: str) -> tuple[str, int, int]:
    """(completeness, present, required) for this finding family."""
    required = _REQUIRED_METRICS.get(domain, ())
    present = sum(1 for name in required if _number(row.get(name)) is not None)
    if not required:
        return "PARTIAL", 0, 0
    if present == len(required):
        return "COMPLETE", present, len(required)
    if present > 0:
        return "PARTIAL", present, len(required)
    return "INSUFFICIENT", present, len(required)


def _derive_confidence(completeness: str, has_impact: bool) -> str:
    """
    Confidence in the evidence behind the finding, never in the finding itself -
    the notebook already decided that.
    """
    if completeness == "COMPLETE":
        return "high" if has_impact else "medium"
    return "low"


def _observed_evidence(row: dict[str, Any], action_row: dict[str, Any] | None) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    for name in _SUPPORTING_METRICS:
        value = _number(row.get(name))
        if value is not None:
            evidence[name] = value
    area = _text(row.get("optimization_area"))
    if area:
        evidence["optimization_area"] = area
    if action_row is not None:
        for name in _ACTION_FIELDS:
            if name == "action_supported" or name == "requires_human_approval":
                value: Any = _flag(action_row.get(name))
            else:
                value = _text(action_row.get(name))
            if value is not None:
                evidence[name] = value
    return evidence


def _current_state(row: dict[str, Any]) -> dict[str, Any]:
    state: dict[str, Any] = {}
    for name in _CURRENT_STATE_FIELDS:
        numeric = _number(row.get(name))
        value = numeric if numeric is not None else _text(row.get(name))
        if value is not None:
            state[name] = value
    return state


def _reject(rejected: list[dict[str, Any]], row: dict[str, Any], reason: str) -> None:
    rejected.append({
        "cluster_id": _text(row.get("cluster_id")),
        "finding": _text(row.get("finding")),
        "reason": reason,
    })


def adapt_notebook_results(
    finding_rows: Iterable[dict[str, Any]] | None,
    action_rows: Iterable[dict[str, Any]] | None = None,
    *,
    customer_id: str,
    environment_id: str,
    workspace_name: str | None = None,
) -> NotebookAdaptation:
    """
    Convert notebook result rows into findings the existing recommendation
    contract accepts.

    `customer_id` and `environment_id` are the caller's real run context and
    become the finding's lineage - nothing about the tenant is read from the
    notebook row, whose own `workspace_id` column describes the workspace the
    notebook analysed, not the ACELO environment that ran it.

    Rows that cannot become a finding are reported in `rejected` with a reason
    rather than dropped silently or forced through with filled-in values.
    """
    if not _text(customer_id) or not _text(environment_id):
        raise ValueError(
            "adapt_notebook_results requires the run's customer_id and environment_id; "
            "the notebook row does not carry ACELO tenant context."
        )

    findings: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()
    actions = _action_index(action_rows)

    for row in finding_rows or []:
        if not isinstance(row, dict):
            rejected.append({"cluster_id": None, "finding": None, "reason": "NOT_A_ROW"})
            continue

        finding_type = (_text(row.get("finding")) or "").upper()
        if not finding_type:
            _reject(rejected, row, "MISSING_FINDING")
            continue

        mapping = _MAPPING.get(finding_type)
        if mapping is None:
            # e.g. HIGH_UTILIZATION: a real notebook observation, but not an
            # optimization finding in the existing contract. Never re-labelled
            # as a finding that IS supported.
            _reject(rejected, row, "UNSUPPORTED_FINDING")
            continue

        cluster_id = _text(row.get("cluster_id"))
        if cluster_id is None:
            _reject(rejected, row, "MISSING_CLUSTER_ID")
            continue

        if not _timestamp_is_valid(row.get("generated_at")):
            _reject(rejected, row, "INVALID_GENERATED_AT")
            continue

        severity = _derive_severity(row)
        if severity is None:
            _reject(rejected, row, "NO_IMPACT_EVIDENCE")
            continue

        domain = mapping["domain"]
        completeness, _present, _required = _evidence_completeness(row, domain)
        if completeness == "INSUFFICIENT":
            _reject(rejected, row, "INSUFFICIENT_EVIDENCE")
            continue

        finding_id = finding_identity(cluster_id, finding_type)
        if finding_id in seen:
            # The notebook overwrites its findings table each run, but a caller
            # could hand us the same row twice. One finding, once.
            _reject(rejected, row, "DUPLICATE_ROW")
            continue
        seen.add(finding_id)

        action_row = actions.get((cluster_id, finding_type))
        generated_at = _text(row.get("generated_at"))
        cluster_name = _text(row.get("cluster_name")) or cluster_id
        has_impact = any(_number(row.get(name)) is not None for name in _IMPACT_METRICS)

        quality = {
            "source_available": True,
            "timestamp_valid": True,
            "completeness": completeness,
            # The row states when it was generated, not how stale its inputs
            # were. UNKNOWN is the honest answer and is not a blocked value.
            "freshness": "UNKNOWN",
            "consistency": "CONSISTENT",
            "missing_fields": [
                name for name in _REQUIRED_METRICS.get(domain, ())
                if _number(row.get(name)) is None
            ],
            "notes": f"Derived from notebook finding row for {cluster_id}.",
        }

        evidence_reference = {
            "source": "default.compute_agent_findings",
            "quality": quality,
            # The notebook reports when it generated the finding, not the start
            # of the window it examined, so only the end is stated.
            "observation_start": None,
            "observation_end": generated_at,
            "lineage": {
                "resource_id": cluster_id,
                "cluster_id": cluster_id,
                "resource_type": RESOURCE_TYPE,
                "environment_id": environment_id,
                "customer_id": customer_id,
                "workspace_name": workspace_name,
                # The workspace the NOTEBOOK analysed, kept distinct from the
                # ACELO environment that ran it.
                "notebook_workspace_id": _text(row.get("workspace_id")),
            },
        }

        area = _text(row.get("optimization_area"))
        findings.append({
            "finding": finding_type,
            "evaluation_status": EVALUATION_STATUS,
            "finding_id": finding_id,
            "rule_id": rule_identity(finding_type, area, domain),
            "resource_id": cluster_id,
            "resource_type": RESOURCE_TYPE,
            "resource": cluster_name,
            "severity": severity,
            "confidence": _derive_confidence(completeness, has_impact),
            "evidence_reference": evidence_reference,
            "observed_evidence": _observed_evidence(row, action_row),
            "current_state": _current_state(row),
            # A restatement of the row, not a new judgement.
            "observed_condition": (
                f"The Cluster Optimization notebook reported {finding_type} for "
                f"cluster {cluster_name}" + (f" under {area}." if area else ".")
            ),
            # The notebook's own deterministic wording, carried verbatim.
            "recommendation": _text(row.get("deterministic_recommendation")),
            "detected_at": generated_at,
        })

    logger.info(
        "notebook_results_adapted findings=%d rejected=%d reasons=%s",
        len(findings),
        len(rejected),
        NotebookAdaptation(findings, rejected).reasons(),
    )
    return NotebookAdaptation(findings=findings, rejected=rejected)
