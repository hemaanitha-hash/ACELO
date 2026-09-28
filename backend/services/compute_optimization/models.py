from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class EvidenceQuality:
    """Minimal quality contract for Stage 1 evidence collection."""

    source_available: bool = True
    completeness: str = "UNKNOWN"
    freshness: str = "UNKNOWN"
    timestamp_valid: bool = True
    consistency: str = "UNKNOWN"
    missing_fields: List[str] = field(default_factory=list)
    notes: str = ""


@dataclass
class ComputeEvidence:
    cluster: Dict[str, Any]
    utilization: List[Dict[str, Any]] = field(default_factory=list)
    billing: List[Dict[str, Any]] = field(default_factory=list)
    worker_levels: List[Dict[str, Any]] = field(default_factory=list)
    runtime: Dict[str, Any] = field(default_factory=dict)
    quality: EvidenceQuality = field(default_factory=EvidenceQuality)
    observation_start: str | None = None
    observation_end: str | None = None
    collected_at: str | None = None
    lineage: Dict[str, Any] = field(default_factory=dict)

@dataclass
class OptimizationFinding:
    resource: str
    resource_type: str
    resource_id: str
    optimization_area: str
    finding: str
    observed_evidence: Dict[str, Any]
    recommendation: str
    potential_issue: str
    expected_impact: Dict[str, Any]
    evidence_required: List[str] = field(default_factory=list)
    action_status: str = "PENDING"
    requires_human_approval: bool = True
    finding_id: str | None = None
    rule_id: str | None = None
    domain: str | None = None
    severity: str | None = None
    evidence_reference: Dict[str, Any] = field(default_factory=dict)
    current_state: Dict[str, Any] = field(default_factory=dict)
    observed_condition: str = ""
    rationale: str = ""
    confidence: str = "low"
    detected_at: str | None = None
    evaluation_status: str = "TRIGGERED"
    proposed_direction: str | None = None

@dataclass
class ComputeOptimizationResult:
    status: str
    evidence_source: str
    findings: List[OptimizationFinding]
    summary: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        return {
            "status": self.status,
            "evidence_source": self.evidence_source,
            "findings": [f.__dict__ for f in self.findings],
            "summary": self.summary,
        }
