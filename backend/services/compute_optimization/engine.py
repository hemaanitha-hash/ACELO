from typing import Any, Dict, List
from .models import ComputeEvidence, ComputeOptimizationResult, OptimizationFinding

LOW_CPU, LOW_MEMORY = 25.0, 40.0
HIGH_CPU, HIGH_MEMORY = 80.0, 80.0
IDLE_CPU, IDLE_MEMORY = 15.0, 25.0
MIN_IDLE_MINUTES, HIGH_IDLE_RATIO = 20.0, 0.25
HIGH_AUTOTERMINATION_MINUTES = 60
MAX_HEADROOM_RATIO, MIN_WORKER_TIME_RATIO, NEAR_MAX_RATIO = 1.50, 0.80, 0.90
SCALING_CHANGE_THRESHOLD = 6

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
    "AUTO_TERMINATION_DISABLED": "Enable an appropriate auto-termination policy after validating the workload's startup overhead.",
    "AUTO_TERMINATION_TOO_HIGH": "Review lowering the auto-termination timeout because meaningful idle time is present.",
    "MAX_WORKERS_TOO_HIGH": "Review lowering max_workers toward the historical peak rather than maintaining unused headroom.",
    "MIN_WORKERS_TOO_HIGH": "Review lowering min_workers because the cluster spends most of its observed worker time near its minimum with low utilization.",
    "MAX_WORKERS_MAY_BE_TOO_LOW": "Review increasing max_workers because the cluster frequently approaches the configured ceiling while utilization is high.",
    "SCALING_INSTABILITY": "Review workload burstiness and autoscaling boundaries because frequent worker-level changes were observed.",
    "HIGH_UTILIZATION": "Runtime is not the primary optimization target; investigate cluster sizing or workload efficiency.",
}

def n(v, default=0.0):
    try: return float(v) if v is not None else default
    except (TypeError, ValueError): return default

def utilization_metrics(rows):
    if not rows:
        return dict(avg_cpu_percent=0, peak_cpu_percent=0, avg_memory_percent=0,
                    peak_memory_percent=0, low_cpu_ratio=0, low_memory_ratio=0,
                    node_minutes=0, idle_node_minutes=0)
    cpus, mems, low_cpu, low_mem, node_min, idle_min = [], [], 0, 0, 0.0, 0.0
    for r in rows:
        cpu = min(100.0, n(r.get("cpu_user_percent")) + n(r.get("cpu_system_percent")))
        mem = n(r.get("mem_used_percent")); dur = n(r.get("duration_minutes"))
        cpus.append(cpu); mems.append(mem); node_min += max(0, dur)
        low_cpu += cpu <= LOW_CPU; low_mem += mem <= LOW_MEMORY
        if cpu <= IDLE_CPU and mem <= IDLE_MEMORY: idle_min += max(0, dur)
    return dict(avg_cpu_percent=sum(cpus)/len(cpus), peak_cpu_percent=max(cpus),
                avg_memory_percent=sum(mems)/len(mems), peak_memory_percent=max(mems),
                low_cpu_ratio=low_cpu/len(rows), low_memory_ratio=low_mem/len(rows),
                node_minutes=node_min, idle_node_minutes=idle_min)

def dbu_metrics(evidence, metrics):
    total = sum(n(x.get("usage_quantity")) for x in evidence.billing)
    hourly = total / (metrics["node_minutes"] / 60) if metrics["node_minutes"] else 0
    return total, hourly

def sizing_finding(m):
    if m["low_cpu_ratio"] >= .70 and m["low_memory_ratio"] >= .70: return "OVERSIZED"
    if m["avg_cpu_percent"] >= HIGH_CPU and m["avg_memory_percent"] >= HIGH_MEMORY: return "UNDERSIZED"
    if m["avg_cpu_percent"] >= HIGH_CPU and m["avg_memory_percent"] < 50: return "CPU_HEAVY"
    if m["avg_memory_percent"] >= HIGH_MEMORY and m["avg_cpu_percent"] < 50: return "MEMORY_HEAVY"
    return "BALANCED"

def autoscaling_finding(cluster, evidence, m):
    levels = evidence.worker_levels; max_w = cluster.get("max_autoscale_workers")
    if not levels or max_w is None: return "FIXED_SIZE_CLUSTER"
    observed = [n(x.get("active_workers")) for x in levels]
    peak, minimum = max(observed), min(observed)
    total_min = sum(n(x.get("minutes_at_level")) for x in levels)
    min_min = sum(n(x.get("minutes_at_level")) for x in levels if n(x.get("active_workers")) == minimum)
    changes = sum(observed[i] != observed[i-1] for i in range(1, len(observed)))
    headroom = n(max_w)/peak if peak else 0
    min_ratio = min_min/total_min if total_min else 0
    near_max = peak/n(max_w) if n(max_w) else 0
    if headroom >= MAX_HEADROOM_RATIO: return "MAX_WORKERS_TOO_HIGH"
    if cluster.get("min_autoscale_workers") is not None and min_ratio >= MIN_WORKER_TIME_RATIO and m["avg_cpu_percent"] <= LOW_CPU and m["avg_memory_percent"] <= LOW_MEMORY: return "MIN_WORKERS_TOO_HIGH"
    if near_max >= NEAR_MAX_RATIO and m["peak_cpu_percent"] >= HIGH_CPU and m["peak_memory_percent"] >= HIGH_MEMORY: return "MAX_WORKERS_MAY_BE_TOO_LOW"
    if changes >= SCALING_CHANGE_THRESHOLD: return "SCALING_INSTABILITY"
    return "AUTOSCALING_NORMAL"

def runtime_finding(cluster, evidence, m):
    rt = evidence.runtime or {}; post = n(rt.get("post_task_idle_minutes"))
    idle_ratio = m["idle_node_minutes"]/m["node_minutes"] if m["node_minutes"] else 0
    auto = n(cluster.get("auto_termination_minutes"))
    if post >= MIN_IDLE_MINUTES: return "POST_WORKLOAD_IDLE"
    if m["idle_node_minutes"] >= MIN_IDLE_MINUTES and idle_ratio >= HIGH_IDLE_RATIO: return "EXCESSIVE_IDLE_RUNTIME"
    if auto == 0 and m["idle_node_minutes"] >= MIN_IDLE_MINUTES: return "AUTO_TERMINATION_DISABLED"
    if auto > HIGH_AUTOTERMINATION_MINUTES and m["idle_node_minutes"] >= MIN_IDLE_MINUTES: return "AUTO_TERMINATION_TOO_HIGH"
    if m["avg_cpu_percent"] >= HIGH_CPU and m["avg_memory_percent"] >= HIGH_MEMORY: return "HIGH_UTILIZATION"
    return "NORMAL_RUNTIME"

def analyze_compute_evidence(evidence: ComputeEvidence, evidence_source="demo") -> ComputeOptimizationResult:
    cluster = evidence.cluster; m = utilization_metrics(evidence.utilization); total_dbu, dbu_hour = dbu_metrics(evidence, m)
    findings: List[OptimizationFinding] = []
    common = {k: round(v, 4) for k,v in m.items()}
    common.update(total_dbu=round(total_dbu,4), historical_dbu_per_hour=round(dbu_hour,4))

    for area, finding in [
        ("CLUSTER_SIZING", sizing_finding(m)),
        ("AUTOSCALING_OPTIMIZATION", autoscaling_finding(cluster, evidence, m)),
        ("RUNTIME_OPTIMIZATION", runtime_finding(cluster, evidence, m)),
    ]:
        if finding not in ACTIONABLE: continue
        issue = {
            "CLUSTER_SIZING": "Cluster sizing may not match observed workload utilization.",
            "AUTOSCALING_OPTIMIZATION": "Autoscaling boundaries may not match observed worker demand.",
            "RUNTIME_OPTIMIZATION": "Observed runtime contains idle or lifecycle inefficiency.",
        }[area]
        findings.append(OptimizationFinding(
            resource=cluster.get("cluster_name") or cluster.get("cluster_id", "unknown"),
            resource_type="cluster", resource_id=cluster.get("cluster_id", "unknown"),
            optimization_area=area, finding=finding, observed_evidence=common,
            recommendation=RECOMMENDATIONS.get(finding, "No immediate optimization change is indicated."),
            potential_issue=issue,
            expected_impact={"predicted_dbu_savings": 0.0, "predicted_savings_percent": 0.0},
            evidence_required=["Historical CPU utilization", "Historical memory utilization", "Worker configuration", "Billing/DBU usage for savings estimate"],
        ))
    return ComputeOptimizationResult(
        status="ANALYZED", evidence_source=evidence_source, findings=findings,
        summary={"clusters_analyzed": 1, "actionable_findings": len(findings),
                 "total_dbu_observed": round(total_dbu,4), "execution_enabled": False,
                 "human_approval_required": True}
    )
