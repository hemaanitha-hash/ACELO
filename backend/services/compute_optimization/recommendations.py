from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy.orm import Session

from models import Recommendation


_MAPPING: dict[str, dict[str, str]] = {
    "OVERSIZED": {
        "domain": "CLUSTER_SIZING",
        "title": "Review cluster capacity",
        "direction": "review_worker_capacity",
        "impact": "Potential reduction in excess worker capacity.",
    },
    "UNDERSIZED": {
        "domain": "CLUSTER_SIZING",
        "title": "Review cluster capacity for workload demand",
        "direction": "review_additional_capacity",
        "impact": "Potential improvement in resource and workload alignment.",
    },
    "CPU_HEAVY": {
        "domain": "CLUSTER_SIZING",
        "title": "Review CPU-oriented resource configuration",
        "direction": "review_cpu_oriented_configuration",
        "impact": "Potential improvement in CPU resource and workload alignment.",
    },
    "MEMORY_HEAVY": {
        "domain": "CLUSTER_SIZING",
        "title": "Review memory-oriented resource configuration",
        "direction": "review_memory_oriented_configuration",
        "impact": "Potential improvement in memory resource and workload alignment.",
    },
    "POST_WORKLOAD_IDLE": {
        "domain": "CLUSTER_RUNTIME",
        "title": "Review post-workload idle runtime",
        "direction": "review_runtime_after_workload_completion",
        "impact": "Potential reduction in unnecessary post-workload idle compute.",
    },
    "EXCESSIVE_IDLE_RUNTIME": {
        "domain": "CLUSTER_RUNTIME",
        "title": "Review observed idle runtime",
        "direction": "review_idle_runtime_and_termination_policy",
        "impact": "Potential reduction in unnecessary idle compute.",
    },
    "AUTO_TERMINATION_DISABLED": {
        "domain": "CLUSTER_RUNTIME",
        "title": "Review cluster auto-termination policy",
        "direction": "review_auto_termination_policy",
        "impact": "Potential reduction in unnecessary idle compute.",
    },
    "AUTO_TERMINATION_TOO_HIGH": {
        "domain": "CLUSTER_RUNTIME",
        "title": "Review cluster auto-termination timeout",
        "direction": "review_auto_termination_timeout",
        "impact": "Potential reduction in unnecessary idle compute.",
    },
    "MAX_WORKERS_TOO_HIGH": {
        "domain": "AUTOSCALING",
        "title": "Review maximum worker configuration",
        "direction": "review_maximum_workers",
        "impact": "Potential reduction in excess autoscaling headroom.",
    },
    "MIN_WORKERS_TOO_HIGH": {
        "domain": "AUTOSCALING",
        "title": "Review minimum worker configuration",
        "direction": "review_minimum_workers",
        "impact": "Potential reduction in unnecessary baseline worker capacity.",
    },
    "MAX_WORKERS_MAY_BE_TOO_LOW": {
        "domain": "AUTOSCALING",
        "title": "Review maximum worker capacity for observed demand",
        "direction": "review_maximum_worker_capacity",
        "impact": "Potential improvement in capacity alignment during high utilization.",
    },
    "SCALING_INSTABILITY": {
        "domain": "AUTOSCALING",
        "title": "Review autoscaling stability",
        "direction": "review_autoscaling_behavior_and_boundaries",
        "impact": "Potential reduction in autoscaling instability.",
    },
}

_BLOCKED_COMPLETENESS = {"UNKNOWN", "FAILED", "INSUFFICIENT", "INSUFFICIENT_EVIDENCE"}
_BLOCKED_FRESHNESS = {"STALE", "EXPIRED"}
_BLOCKED_CONSISTENCY = {"INVALID", "INCONSISTENT", "FAILED"}
_VALID_SEVERITY = {"LOW", "MEDIUM", "HIGH"}


def _field(finding: Any, name: str, default: Any = None) -> Any:
    if isinstance(finding, dict):
        return finding.get(name, default)
    return getattr(finding, name, default)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _evidence_quality(reference: dict[str, Any]) -> dict[str, Any] | None:
    quality = reference.get("quality")
    if not isinstance(quality, dict):
        return None
    if quality.get("source_available") is not True:
        return None
    if quality.get("timestamp_valid") is not True:
        return None
    completeness = str(quality.get("completeness", "")).upper()
    if completeness in _BLOCKED_COMPLETENESS or completeness not in {"COMPLETE", "PARTIAL"}:
        return None
    if str(quality.get("freshness", "")).upper() in _BLOCKED_FRESHNESS:
        return None
    if str(quality.get("consistency", "")).upper() in _BLOCKED_CONSISTENCY:
        return None
    return dict(quality)


def _eligible_finding(finding: Any) -> tuple[dict[str, Any], dict[str, Any]] | None:
    finding_type = str(_field(finding, "finding", "")).upper()
    mapping = _MAPPING.get(finding_type)
    if mapping is None or str(_field(finding, "evaluation_status", "")).upper() != "TRIGGERED":
        return None

    finding_id = _field(finding, "finding_id")
    rule_id = _field(finding, "rule_id")
    resource_id = _field(finding, "resource_id")
    resource_type = _field(finding, "resource_type")
    reference = _field(finding, "evidence_reference", {})
    if not all(isinstance(value, str) and value.strip() for value in (finding_id, rule_id, resource_id)):
        return None
    if str(resource_type or "").upper() not in {"CLUSTER", "CLASSIC_CLUSTER"}:
        return None
    if not isinstance(reference, dict):
        return None

    lineage = reference.get("lineage")
    if not isinstance(lineage, dict):
        return None
    lineage_resource_ids = [
        value
        for value in (lineage.get("resource_id"), lineage.get("cluster_id"))
        if value is not None
    ]
    if not lineage_resource_ids or any(value != resource_id for value in lineage_resource_ids):
        return None
    customer_id = lineage.get("customer_id")
    environment_id = lineage.get("environment_id")
    if not all(isinstance(value, str) and value.strip() for value in (customer_id, environment_id)):
        return None

    quality = _evidence_quality(reference)
    if quality is None:
        return None

    severity = str(_field(finding, "severity", "")).upper()
    confidence = str(_field(finding, "confidence", "")).lower()
    if severity not in _VALID_SEVERITY or confidence not in {"low", "medium", "high"}:
        return None

    return mapping, {
        "finding_id": finding_id,
        "rule_id": rule_id,
        "finding_type": finding_type,
        "resource_id": resource_id,
        "resource_type": "CLASSIC_CLUSTER",
        "customer_id": customer_id,
        "environment_id": environment_id,
        "workspace_name": lineage.get("workspace_name"),
        "resource_name": _field(finding, "resource") or resource_id,
        "severity": severity,
        "confidence": confidence,
        "evidence_reference": dict(reference),
        "evidence_quality": quality,
        "observation_window": {
            "start": reference.get("observation_start"),
            "end": reference.get("observation_end"),
        },
        "current_state": dict(_field(finding, "current_state", {}) or {}),
        "observed_evidence": dict(_field(finding, "observed_evidence", {}) or {}),
        "summary": _field(finding, "potential_issue") or _field(finding, "observed_condition") or "",
        "description": _field(finding, "rationale") or _field(finding, "recommendation") or "",
        "direction_reason": _field(finding, "recommendation") or _field(finding, "rationale") or "",
        "direction": _field(finding, "proposed_direction") or mapping["direction"],
        "detected_at": _field(finding, "detected_at"),
    }


def build_recommendations(findings: Iterable[Any]) -> list[dict[str, Any]]:
    """Build deterministic Stage 1 recommendations from eligible Phase 4 findings only."""
    recommendations = []
    seen: set[str] = set()
    for finding in findings:
        eligible = _eligible_finding(finding)
        if eligible is None:
            continue
        mapping, data = eligible
        identity = hashlib.sha256(
            "\x1f".join((data["customer_id"], data["environment_id"], data["finding_id"])).encode("utf-8")
        ).hexdigest()
        recommendation_id = f"stage1-{identity}"
        if recommendation_id in seen:
            continue
        seen.add(recommendation_id)
        recommendations.append({
            "recommendation_id": recommendation_id,
            "domain": mapping["domain"],
            "resource_type": data["resource_type"],
            "resource_id": data["resource_id"],
            "resource_name": data["resource_name"],
            "finding_id": data["finding_id"],
            "rule_id": data["rule_id"],
            "finding_type": data["finding_type"],
            "title": mapping["title"],
            "summary": data["summary"],
            "description": data["description"],
            "evidence": data["observed_evidence"],
            "evidence_references": [data["evidence_reference"]],
            "current_state": data["current_state"],
            "proposed_state": {
                "direction": data["direction"],
                "reason": data["direction_reason"],
            },
            "expected_impact": {"status": "POTENTIAL", "description": mapping["impact"]},
            "estimated_savings": {
                "status": "NOT_AVAILABLE",
                "estimated": None,
                "measured": None,
            },
            "confidence": data["confidence"],
            "severity": data["severity"],
            "risk": data["severity"],
            "policy_status": "NOT_EVALUATED",
            "approval_status": "NOT_REQUESTED",
            "execution_status": "NOT_STARTED",
            "verification_status": "NOT_STARTED",
            "status": "OPEN",
            "customer_id": data["customer_id"],
            "environment_id": data["environment_id"],
            "workspace_name": data["workspace_name"],
            "observation_window": data["observation_window"],
            "evidence_quality": data["evidence_quality"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "detected_at": data["detected_at"],
        })
    return recommendations


def serialize_recommendation(record: Recommendation) -> dict[str, Any]:
    def load(value: str | None, default: Any) -> Any:
        if not value:
            return default
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default

    return {
        "recommendation_id": record.recommendation_id,
        "id": record.recommendation_id or record.id,
        "domain": record.domain,
        "resource_type": record.resource_type or "cluster",
        "resource_id": record.resource_id,
        "resource_name": record.resource,
        "finding_id": record.finding_id,
        "rule_id": record.rule_id,
        "finding_type": record.finding_type,
        "title": record.title or record.resource,
        "summary": record.summary,
        "description": record.description,
        "evidence": load(record.evidence_json, {}),
        "evidence_references": load(record.evidence_references_json, []),
        "current_state": load(record.current_state, {}),
        "proposed_state": load(record.proposed_state, {}),
        "expected_impact": load(record.expected_impact_json, {}),
        "estimated_savings": load(record.estimated_savings_json, {"status": "NOT_AVAILABLE", "estimated": None, "measured": None}),
        "estimated_monthly_savings": record.estimated_monthly_savings,
        "confidence": record.confidence,
        "severity": record.severity,
        "risk": record.risk,
        "policy_status": record.policy_status,
        "approval_status": record.approval_status,
        "execution_status": record.execution_status,
        "verification_status": record.verification_status,
        "status": record.status,
        "customer_id": record.customer_id,
        "environment_id": record.environment_id,
        "workspace_name": record.workspace_name,
        "observation_window": load(record.observation_window_json, {}),
        "evidence_quality": load(record.evidence_quality_json, {}),
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
        "detected_at": record.created_at.isoformat() if record.created_at else None,
    }


def persist_recommendations(db: Session, findings: Iterable[Any]) -> list[dict[str, Any]]:
    """Idempotently upsert OPEN recommendations by stable finding identity."""
    built = build_recommendations(findings)
    persisted = []
    json_columns = {
        "current_state": "current_state",
        "proposed_state": "proposed_state",
        "evidence": "evidence_json",
        "evidence_references": "evidence_references_json",
        "evidence_quality": "evidence_quality_json",
        "observation_window": "observation_window_json",
        "expected_impact": "expected_impact_json",
        "estimated_savings": "estimated_savings_json",
    }
    scalar_columns = (
        "recommendation_id", "customer_id", "environment_id", "workspace_name",
        "resource_id", "resource_type", "finding_id", "rule_id", "finding_type",
        "title", "summary", "description", "domain", "severity", "risk",
        "confidence", "policy_status", "approval_status", "execution_status",
        "verification_status", "status",
    )
    for item in built:
        record = db.query(Recommendation).filter(
            Recommendation.customer_id == item["customer_id"],
            Recommendation.recommendation_id == item["recommendation_id"],
        ).first()
        if record is not None and str(record.status or "").upper() != "OPEN":
            persisted.append(serialize_recommendation(record))
            continue
        if record is None:
            record = Recommendation(
                job_run_id=None,
                resource=item["resource_name"],
                domain=item["domain"],
                proposed_change=item["proposed_state"]["direction"],
                estimated_monthly_savings=None,
                current_monthly_cost=None,
                current_state=_json(item["current_state"]),
                status="OPEN",
            )
            db.add(record)
        for name in scalar_columns:
            setattr(record, name, item[name])
        record.resource = item["resource_name"]
        for source_name, column_name in json_columns.items():
            setattr(record, column_name, _json(item[source_name]))
        record.proposed_change = item["proposed_state"]["direction"]
        record.estimated_monthly_savings = None
        record.current_monthly_cost = None
        persisted.append(serialize_recommendation(record))
    db.commit()
    for item in persisted:
        record = db.query(Recommendation).filter(
            Recommendation.recommendation_id == item["recommendation_id"],
            Recommendation.customer_id == item["customer_id"],
        ).first()
        if record is not None:
            item.update(serialize_recommendation(record))
    return persisted
