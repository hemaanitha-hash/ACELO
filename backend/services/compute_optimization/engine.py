from __future__ import annotations

import hashlib
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from .models import ComputeEvidence, ComputeOptimizationResult, OptimizationFinding

LOW_CPU, LOW_MEMORY = 25.0, 40.0
HIGH_CPU, HIGH_MEMORY = 80.0, 80.0
IDLE_CPU, IDLE_MEMORY = 15.0, 25.0
MIN_IDLE_MINUTES, HIGH_IDLE_RATIO = 20.0, 0.25
HIGH_AUTOTERMINATION_MINUTES = 60.0
MAX_HEADROOM_RATIO, MIN_WORKER_TIME_RATIO, NEAR_MAX_RATIO = 1.50, 0.80, 0.90
SCALING_CHANGE_THRESHOLD = 6
MIN_OBSERVATION_MINUTES = 60.0

ACTIONABLE = {
    "OVERSIZED", "UNDERSIZED", "CPU_HEAVY", "MEMORY_HEAVY",
    "POST_WORKLOAD_IDLE", "EXCESSIVE_IDLE_RUNTIME",
    "AUTO_TERMINATION_DISABLED", "AUTO_TERMINATION_TOO_HIGH",
    "MAX_WORKERS_TOO_HIGH", "MIN_WORKERS_TOO_HIGH",
    "MAX_WORKERS_MAY_BE_TOO_LOW", "SCALING_INSTABILITY",
}

RECOMMENDATIONS = {
    "OVERSIZED": "Review reducing worker capacity. The cluster shows sustained low CPU and memory utilization.",
    "UNDERSIZED": "Review increasing worker capacity or changing the worker node type. CPU and memory utilization are both high.",
    "CPU_HEAVY": "Review a CPU-optimized worker node type before simply adding workers.",
    "MEMORY_HEAVY": "Review a memory-optimized worker node type before simply adding workers.",
    "POST_WORKLOAD_IDLE": "Review auto-termination because observed cluster activity continued after the latest observed job task ended.",
    "EXCESSIVE_IDLE_RUNTIME": "Review cluster lifecycle configuration because a meaningful portion of observed runtime was low-utilization idle time.",
    "AUTO_TERMINATION_DISABLED": "Review an appropriate auto-termination policy after validating the workload's startup overhead.",
    "AUTO_TERMINATION_TOO_HIGH": "Review the auto-termination timeout because meaningful idle time is present.",
    "MAX_WORKERS_TOO_HIGH": "Review max_workers relative to the historical peak; the evidence shows unused configured headroom.",
    "MIN_WORKERS_TOO_HIGH": "Review min_workers because the cluster spends most of its observed worker time at its minimum with low utilization.",
    "MAX_WORKERS_MAY_BE_TOO_LOW": "Review max_workers because the cluster frequently approaches its configured ceiling while utilization is high.",
    "SCALING_INSTABILITY": "Review workload burstiness and autoscaling boundaries because frequent worker-level changes were observed.",
}

RULE_IDS = {
    "CLUSTER_SIZING": "STAGE1.CLUSTER_SIZING",
    "RUNTIME_OPTIMIZATION": "STAGE1.CLUSTER_RUNTIME",
    "AUTOSCALING_OPTIMIZATION": "STAGE1.AUTOSCALING",
}

_SEVERITY = {
    "OVERSIZED": "MEDIUM",
    "UNDERSIZED": "HIGH",
    "CPU_HEAVY": "MEDIUM",
    "MEMORY_HEAVY": "MEDIUM",
    "POST_WORKLOAD_IDLE": "MEDIUM",
    "EXCESSIVE_IDLE_RUNTIME": "MEDIUM",
    "AUTO_TERMINATION_DISABLED": "MEDIUM",
    "AUTO_TERMINATION_TOO_HIGH": "LOW",
    "MAX_WORKERS_TOO_HIGH": "LOW",
    "MIN_WORKERS_TOO_HIGH": "MEDIUM",
    "MAX_WORKERS_MAY_BE_TOO_LOW": "HIGH",
    "SCALING_INSTABILITY": "MEDIUM",
}

_PROPOSED_DIRECTION = {
    "OVERSIZED": "Review a lower worker capacity against workload requirements.",
    "UNDERSIZED": "Review more worker capacity or a better-matched node type.",
    "CPU_HEAVY": "Review a CPU-optimized node type.",
    "MEMORY_HEAVY": "Review a memory-optimized node type.",
    "POST_WORKLOAD_IDLE": "Review lifecycle policy for observed post-workload idle time.",
    "EXCESSIVE_IDLE_RUNTIME": "Review lifecycle policy for observed idle runtime.",
    "AUTO_TERMINATION_DISABLED": "Review enabling an appropriate auto-termination policy.",
    "AUTO_TERMINATION_TOO_HIGH": "Review a lower auto-termination timeout.",
    "MAX_WORKERS_TOO_HIGH": "Review a lower maximum near observed peak demand.",
    "MIN_WORKERS_TOO_HIGH": "Review a lower minimum worker floor.",
    "MAX_WORKERS_MAY_BE_TOO_LOW": "Review a higher maximum worker ceiling.",
    "SCALING_INSTABILITY": "Review autoscaling boundaries against workload variation.",
}


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _quality_block_reason(evidence: ComputeEvidence) -> str | None:
    quality = evidence.quality
    completeness = str(quality.completeness or "UNKNOWN").upper()
    freshness = str(quality.freshness or "UNKNOWN").upper()
    consistency = str(quality.consistency or "UNKNOWN").upper()

    if not quality.source_available:
        return "Evidence source is unavailable."
    if completeness in {"FAILED", "INSUFFICIENT", "INSUFFICIENT_EVIDENCE", "UNKNOWN"}:
        return f"Evidence completeness is {completeness}; no positive rule can be evaluated."
    if not quality.timestamp_valid:
        return "Evidence timestamps are marked invalid."
    if freshness in {"STALE", "EXPIRED"}:
        return "Evidence is explicitly marked stale."
    if consistency in {"INCONSISTENT", "INVALID", "FAILED"}:
        return "Evidence is marked inconsistent."
    resource_id = evidence.cluster.get("cluster_id") or evidence.lineage.get("resource_id")
    if not resource_id:
        return "Evidence is not associated with a cluster resource ID."
    return None


def _normalise_utilization(rows: list[dict[str, Any]]) -> tuple[list[dict[str, float | datetime]], str | None]:
    if not rows:
        return [], "Historical utilization is missing."

    normalized: list[dict[str, float | datetime]] = []
    for index, row in enumerate(rows):
        cpu_user = _number(row.get("cpu_user_percent"))
        cpu_system = _number(row.get("cpu_system_percent"))
        memory = _number(row.get("mem_used_percent"))
        duration = _number(row.get("duration_minutes"))
        start = _parse_time(row.get("start_time"))
        end = _parse_time(row.get("end_time"))

        if cpu_user is None or cpu_system is None:
            return [], f"CPU history is missing or invalid in utilization sample {index + 1}."
        if memory is None:
            return [], f"Memory history is missing or invalid in utilization sample {index + 1}."
        if not 0 <= cpu_user <= 100 or not 0 <= cpu_system <= 100 or not 0 <= memory <= 100:
            return [], f"Utilization sample {index + 1} contains a value outside 0-100 percent."
        if start is None or end is None or end <= start:
            return [], f"Utilization sample {index + 1} has no valid observation interval."
        if duration is None:
            duration = (end - start).total_seconds() / 60
        if duration <= 0:
            return [], f"Utilization sample {index + 1} has no positive duration."

        normalized.append({
            "cpu": min(100.0, cpu_user + cpu_system),
            "memory": memory,
            "duration": duration,
            "start": start,
            "end": end,
        })

    unique_samples = {sample["end"] for sample in normalized}
    window_start = min(sample["start"] for sample in normalized)
    window_end = max(sample["end"] for sample in normalized)
    window_minutes = (window_end - window_start).total_seconds() / 60
    if len(unique_samples) < 2:
        return [], "At least two distinct historical utilization samples are required."
    if window_minutes < MIN_OBSERVATION_MINUTES:
        return [], f"Observation window is shorter than {MIN_OBSERVATION_MINUTES:g} minutes."
    return normalized, None


def utilization_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Return metrics only when every sample carries valid CPU, memory and time data."""
    normalized, error = _normalise_utilization(rows)
    if error:
        return {
            "avg_cpu_percent": None,
            "peak_cpu_percent": None,
            "avg_memory_percent": None,
            "peak_memory_percent": None,
            "low_cpu_ratio": None,
            "low_memory_ratio": None,
            "node_minutes": None,
            "idle_node_minutes": None,
            "history_error": error,
        }

    cpus = [sample["cpu"] for sample in normalized]
    memories = [sample["memory"] for sample in normalized]
    node_minutes = sum(sample["duration"] for sample in normalized)
    idle_minutes = sum(
        sample["duration"]
        for sample in normalized
        if sample["cpu"] <= IDLE_CPU and sample["memory"] <= IDLE_MEMORY
    )
    return {
        "avg_cpu_percent": sum(cpus) / len(cpus),
        "peak_cpu_percent": max(cpus),
        "avg_memory_percent": sum(memories) / len(memories),
        "peak_memory_percent": max(memories),
        "low_cpu_ratio": sum(cpu <= LOW_CPU for cpu in cpus) / len(cpus),
        "low_memory_ratio": sum(memory <= LOW_MEMORY for memory in memories) / len(memories),
        "node_minutes": node_minutes,
        "idle_node_minutes": idle_minutes,
        "history_error": None,
        "observation_start": min(sample["start"] for sample in normalized).isoformat(),
        "observation_end": max(sample["end"] for sample in normalized).isoformat(),
        "observation_minutes": (
            max(sample["end"] for sample in normalized)
            - min(sample["start"] for sample in normalized)
        ).total_seconds() / 60,
        "sample_count": len(normalized),
    }


def dbu_metrics(evidence: ComputeEvidence, metrics: dict[str, Any]) -> tuple[float | None, float | None]:
    usage = [_number(row.get("usage_quantity")) for row in evidence.billing]
    if not usage or any(value is None for value in usage):
        return None, None
    total = sum(value for value in usage if value is not None)
    node_minutes = metrics.get("node_minutes")
    hourly = total / (node_minutes / 60) if node_minutes else None
    return total, hourly


def sizing_finding(metrics: dict[str, Any]) -> str:
    if metrics.get("low_cpu_ratio") is None or metrics.get("low_memory_ratio") is None:
        return "NOT_EVALUABLE"
    if metrics["low_cpu_ratio"] >= 0.70 and metrics["low_memory_ratio"] >= 0.70:
        return "OVERSIZED"
    if metrics["avg_cpu_percent"] >= HIGH_CPU and metrics["avg_memory_percent"] >= HIGH_MEMORY:
        return "UNDERSIZED"
    if metrics["avg_cpu_percent"] >= HIGH_CPU and metrics["avg_memory_percent"] < 50:
        return "CPU_HEAVY"
    if metrics["avg_memory_percent"] >= HIGH_MEMORY and metrics["avg_cpu_percent"] < 50:
        return "MEMORY_HEAVY"
    return "BALANCED"


def _normalise_worker_levels(levels: list[dict[str, Any]]) -> tuple[list[tuple[float, float]], str | None]:
    if not levels:
        return [], "Historical worker-level evidence is missing."
    normalized: list[tuple[float, float]] = []
    for index, row in enumerate(levels):
        workers = _number(row.get("active_workers"))
        minutes = _number(row.get("minutes_at_level"))
        if workers is None or minutes is None or workers < 0 or minutes <= 0:
            return [], f"Worker-level sample {index + 1} is incomplete or invalid."
        normalized.append((workers, minutes))
    if len(normalized) < 2:
        return [], "At least two historical worker-level samples are required."
    if sum(minutes for _, minutes in normalized) < MIN_OBSERVATION_MINUTES:
        return [], f"Worker-level observation is shorter than {MIN_OBSERVATION_MINUTES:g} minutes."
    return normalized, None


def autoscaling_finding(cluster: dict[str, Any], evidence: ComputeEvidence, metrics: dict[str, Any]) -> str:
    levels, error = _normalise_worker_levels(evidence.worker_levels)
    minimum_workers = _number(cluster.get("min_autoscale_workers"))
    maximum_workers = _number(cluster.get("max_autoscale_workers"))
    current_workers = _number(cluster.get("worker_count", cluster.get("num_workers")))
    if error or minimum_workers is None or maximum_workers is None or current_workers is None:
        return "NOT_EVALUABLE"
    if minimum_workers < 0 or maximum_workers <= 0 or maximum_workers < minimum_workers or current_workers < 0:
        return "NOT_EVALUABLE"
    observed = [workers for workers, _ in levels]
    peak, minimum = max(observed), min(observed)
    total_minutes = sum(minutes for _, minutes in levels)
    minimum_minutes = sum(minutes for workers, minutes in levels if workers == minimum)
    headroom = maximum_workers / peak if peak else None
    minimum_time_ratio = minimum_minutes / total_minutes if total_minutes else None
    near_max_ratio = peak / maximum_workers if maximum_workers else None

    if headroom is not None and headroom >= MAX_HEADROOM_RATIO:
        return "MAX_WORKERS_TOO_HIGH"
    if (
        minimum_time_ratio is not None
        and minimum_time_ratio >= MIN_WORKER_TIME_RATIO
        and metrics.get("avg_cpu_percent") is not None
        and metrics.get("avg_memory_percent") is not None
        and metrics["avg_cpu_percent"] <= LOW_CPU
        and metrics["avg_memory_percent"] <= LOW_MEMORY
    ):
        return "MIN_WORKERS_TOO_HIGH"
    if (
        near_max_ratio is not None
        and near_max_ratio >= NEAR_MAX_RATIO
        and metrics.get("peak_cpu_percent") is not None
        and metrics.get("peak_memory_percent") is not None
        and metrics["peak_cpu_percent"] >= HIGH_CPU
        and metrics["peak_memory_percent"] >= HIGH_MEMORY
    ):
        return "MAX_WORKERS_MAY_BE_TOO_LOW"

    timed_levels = []
    for row, (workers, _) in zip(evidence.worker_levels, levels):
        timestamp = _parse_time(
            row.get("start_time") or row.get("timestamp") or row.get("event_time")
        )
        if timestamp is None:
            return "NOT_EVALUABLE"
        timed_levels.append((timestamp, workers))
    timed_levels.sort(key=lambda item: item[0])
    if len({timestamp for timestamp, _ in timed_levels}) != len(timed_levels):
        return "NOT_EVALUABLE"
    ordered_workers = [workers for _, workers in timed_levels]
    changes = sum(
        ordered_workers[index] != ordered_workers[index - 1]
        for index in range(1, len(ordered_workers))
    )
    if changes >= SCALING_CHANGE_THRESHOLD:
        return "SCALING_INSTABILITY"
    return "AUTOSCALING_NORMAL"


def runtime_finding(cluster: dict[str, Any], evidence: ComputeEvidence, metrics: dict[str, Any]) -> str:
    if metrics.get("idle_node_minutes") is None or not metrics.get("node_minutes"):
        return "NOT_EVALUABLE"
    runtime = evidence.runtime or {}
    post_task_idle = _number(runtime.get("post_task_idle_minutes"))
    idle_minutes = metrics["idle_node_minutes"]
    idle_ratio = idle_minutes / metrics["node_minutes"]
    auto_termination = _number(cluster.get("auto_termination_minutes"))

    if post_task_idle is not None and post_task_idle >= MIN_IDLE_MINUTES:
        return "POST_WORKLOAD_IDLE"
    if idle_minutes >= MIN_IDLE_MINUTES and idle_ratio >= HIGH_IDLE_RATIO:
        return "EXCESSIVE_IDLE_RUNTIME"
    if auto_termination == 0 and idle_minutes >= MIN_IDLE_MINUTES:
        return "AUTO_TERMINATION_DISABLED"
    if auto_termination is not None and auto_termination > HIGH_AUTOTERMINATION_MINUTES and idle_minutes >= MIN_IDLE_MINUTES:
        return "AUTO_TERMINATION_TOO_HIGH"
    if metrics["avg_cpu_percent"] >= HIGH_CPU and metrics["avg_memory_percent"] >= HIGH_MEMORY:
        return "HIGH_UTILIZATION"
    return "NORMAL_RUNTIME"


def runtime_activity_state(evidence: ComputeEvidence, metrics: dict[str, Any]) -> str:
    if metrics.get("history_error"):
        return "UNKNOWN"
    post_task_idle = _number((evidence.runtime or {}).get("post_task_idle_minutes"))
    if post_task_idle is not None and post_task_idle >= MIN_IDLE_MINUTES:
        return "POST_WORKLOAD_IDLE"
    if metrics["idle_node_minutes"] >= MIN_IDLE_MINUTES:
        return "IDLE"
    if metrics["avg_cpu_percent"] is not None and metrics["avg_memory_percent"] is not None:
        if metrics["avg_cpu_percent"] > IDLE_CPU or metrics["avg_memory_percent"] > IDLE_MEMORY:
            return "ACTIVE"
    return "UNKNOWN"


def _quality_confidence(evidence: ComputeEvidence) -> str:
    quality = evidence.quality
    completeness = str(quality.completeness or "UNKNOWN").upper()
    freshness = str(quality.freshness or "UNKNOWN").upper()
    consistency = str(quality.consistency or "UNKNOWN").upper()
    if completeness == "COMPLETE" and freshness == "FRESH" and consistency in {"CONSISTENT", "VALID"} and quality.timestamp_valid:
        return "high"
    if completeness == "COMPLETE" and freshness not in {"UNKNOWN", "STALE", "EXPIRED"} and quality.timestamp_valid:
        return "medium"
    return "low"


def _evaluation(status: str, reason: str | None = None) -> dict[str, str]:
    result = {"status": status}
    if reason:
        result["reason"] = reason
    return result


def _current_state(cluster: dict[str, Any]) -> dict[str, Any]:
    return {
        key: cluster.get(key)
        for key in (
            "cluster_id", "cluster_name", "worker_count", "num_workers",
            "min_autoscale_workers", "max_autoscale_workers", "auto_termination_minutes",
        )
        if cluster.get(key) is not None
    }


def _finding(
    evidence: ComputeEvidence,
    evidence_source: str,
    metrics: dict[str, Any],
    area: str,
    finding_type: str,
) -> OptimizationFinding:
    cluster = evidence.cluster
    resource_id = str(cluster.get("cluster_id") or evidence.lineage["resource_id"])
    domain = {
        "CLUSTER_SIZING": "CLUSTER_SIZING",
        "RUNTIME_OPTIMIZATION": "CLUSTER_RUNTIME",
        "AUTOSCALING_OPTIMIZATION": "AUTOSCALING",
    }[area]
    rule_id = f"{RULE_IDS[area]}.{finding_type}"
    finding_id = hashlib.sha256(f"{rule_id}\x1f{resource_id}".encode("utf-8")).hexdigest()[:24]
    condition = f"{finding_type} condition was met using validated historical evidence."
    observed = {key: value for key, value in metrics.items() if key != "history_error"}
    observed.update({"finding_type": finding_type, "resource_id": resource_id})
    if area == "AUTOSCALING_OPTIMIZATION":
        observed["worker_levels"] = [dict(row) for row in evidence.worker_levels]
    if area == "RUNTIME_OPTIMIZATION":
        observed["runtime"] = dict(evidence.runtime or {})
        observed["activity_state"] = runtime_activity_state(evidence, metrics)
    reference = {
        "source": evidence_source,
        "resource_id": resource_id,
        "lineage": dict(evidence.lineage),
        "observation_start": metrics.get("observation_start") or evidence.observation_start,
        "observation_end": metrics.get("observation_end") or evidence.observation_end,
        "collected_at": evidence.collected_at,
        "quality": {
            "source_available": evidence.quality.source_available,
            "completeness": evidence.quality.completeness,
            "freshness": evidence.quality.freshness,
            "timestamp_valid": evidence.quality.timestamp_valid,
            "consistency": evidence.quality.consistency,
        },
    }
    issue = {
        "CLUSTER_SIZING": "Cluster sizing may not match observed workload utilization.",
        "RUNTIME_OPTIMIZATION": "Observed runtime contains idle or lifecycle inefficiency.",
        "AUTOSCALING_OPTIMIZATION": "Autoscaling boundaries may not match observed worker demand.",
    }[area]
    return OptimizationFinding(
        resource=cluster.get("cluster_name") or resource_id,
        resource_type="cluster",
        resource_id=resource_id,
        optimization_area=area,
        finding=finding_type,
        observed_evidence=observed,
        recommendation=RECOMMENDATIONS[finding_type],
        potential_issue=issue,
        expected_impact={"savings_status": "NOT_ESTIMATED"},
        evidence_required=["Validated historical CPU and memory", "Cluster configuration", "Observation window"],
        finding_id=finding_id,
        rule_id=rule_id,
        domain=domain,
        severity=_SEVERITY[finding_type],
        evidence_reference=reference,
        current_state=_current_state(cluster),
        observed_condition=condition,
        rationale=f"{condition} {RECOMMENDATIONS[finding_type]}",
        confidence=_quality_confidence(evidence),
        detected_at=evidence.collected_at,
        evaluation_status="TRIGGERED",
        proposed_direction=_PROPOSED_DIRECTION[finding_type],
    )


def analyze_compute_evidence(evidence: ComputeEvidence, evidence_source: str = "demo") -> ComputeOptimizationResult:
    """Evaluate read-only Stage 1 rules, refusing positive findings on unsupported evidence."""
    cluster = evidence.cluster
    metrics = utilization_metrics(evidence.utilization)
    total_dbu, dbu_hour = dbu_metrics(evidence, metrics)
    findings: list[OptimizationFinding] = []
    evaluations: dict[str, dict[str, str]] = {}
    global_error = _quality_block_reason(evidence)

    for area, evaluator in (
        ("CLUSTER_SIZING", sizing_finding),
        ("AUTOSCALING_OPTIMIZATION", lambda values: autoscaling_finding(cluster, evidence, values)),
        ("RUNTIME_OPTIMIZATION", lambda values: runtime_finding(cluster, evidence, values)),
    ):
        rule_id = RULE_IDS[area]
        if global_error:
            evaluations[rule_id] = _evaluation("NOT_EVALUABLE", global_error)
            continue
        if metrics.get("history_error") and area != "AUTOSCALING_OPTIMIZATION":
            evaluations[rule_id] = _evaluation("NOT_EVALUABLE", metrics["history_error"])
            continue
        if area == "CLUSTER_SIZING":
            current_workers = _number(cluster.get("worker_count", cluster.get("num_workers")))
            if current_workers is None or current_workers <= 0:
                evaluations[rule_id] = _evaluation("NOT_EVALUABLE", "Current worker count is missing or invalid.")
                continue
        if area == "AUTOSCALING_OPTIMIZATION":
            levels, levels_error = _normalise_worker_levels(evidence.worker_levels)
            if levels_error:
                evaluations[rule_id] = _evaluation("NOT_EVALUABLE", levels_error)
                continue
            if _number(cluster.get("min_autoscale_workers")) is None or _number(cluster.get("max_autoscale_workers")) is None:
                evaluations[rule_id] = _evaluation("NOT_EVALUABLE", "Both autoscaling minimum and maximum configuration are required.")
                continue
            if _number(cluster.get("worker_count", cluster.get("num_workers"))) is None:
                evaluations[rule_id] = _evaluation("NOT_EVALUABLE", "Current worker count is missing.")
                continue

        finding_type = evaluator(metrics)
        if finding_type in ACTIONABLE:
            finding = _finding(evidence, evidence_source, metrics, area, finding_type)
            findings.append(finding)
            evaluations[rule_id] = _evaluation("TRIGGERED")
        elif finding_type in {"NOT_EVALUABLE", "FIXED_SIZE_CLUSTER"}:
            reason = (
                "Scaling-instability evaluation requires unique worker-level timestamps."
                if area == "AUTOSCALING_OPTIMIZATION"
                else "Required rule evidence or configuration is unavailable."
            )
            evaluations[rule_id] = _evaluation("NOT_EVALUABLE", reason)
        else:
            evaluations[rule_id] = _evaluation("NO_FINDING")

    common = {
        key: round(value, 4) if isinstance(value, (int, float)) else value
        for key, value in metrics.items()
        if key != "history_error"
    }
    common.update(
        total_dbu_observed=round(total_dbu, 4) if total_dbu is not None else None,
        historical_dbu_per_hour=round(dbu_hour, 4) if dbu_hour is not None else None,
    )
    activity_state = runtime_activity_state(evidence, metrics)
    return ComputeOptimizationResult(
        status="ANALYZED",
        evidence_source=evidence_source,
        findings=findings,
        summary={
            "clusters_analyzed": int(bool(cluster.get("cluster_id") or evidence.lineage.get("resource_id"))),
            "actionable_findings": len(findings),
            "rule_evaluations": evaluations,
            "observed_evidence": common,
            "runtime_activity_state": activity_state,
            "execution_enabled": False,
            "human_approval_required": True,
            "savings_status": "NOT_ESTIMATED",
        },
    )
