from dataclasses import dataclass, field
from typing import Any, Dict, List

@dataclass
class ComputeEvidence:
    cluster: Dict[str, Any]
    utilization: List[Dict[str, Any]] = field(default_factory=list)
    billing: List[Dict[str, Any]] = field(default_factory=list)
    worker_levels: List[Dict[str, Any]] = field(default_factory=list)
    runtime: Dict[str, Any] = field(default_factory=dict)

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
