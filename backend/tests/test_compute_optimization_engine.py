from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from services.compute_optimization.engine import analyze_compute_evidence
from services.compute_optimization.models import ComputeEvidence, EvidenceQuality


def _utilization(cpu=50, memory=50, intervals=None):
    intervals = intervals or [
        ("2026-09-01T00:00:00Z", "2026-09-01T00:30:00Z"),
        ("2026-09-01T00:30:00Z", "2026-09-01T01:00:00Z"),
    ]
    return [
        {
            "cpu_user_percent": cpu * 0.7,
            "cpu_system_percent": cpu * 0.3,
            "mem_used_percent": memory,
            "duration_minutes": 30,
            "start_time": start,
            "end_time": end,
        }
        for start, end in intervals
    ]


def _quality(**overrides):
    values = {
        "source_available": True,
        "completeness": "COMPLETE",
        "freshness": "FRESH",
        "timestamp_valid": True,
        "consistency": "CONSISTENT",
    }
    values.update(overrides)
    return EvidenceQuality(**values)


def _evidence(
    *,
    cpu=50,
    memory=50,
    cluster=None,
    utilization=None,
    worker_levels=None,
    runtime=None,
    quality=None,
):
    return ComputeEvidence(
        cluster=cluster or {
            "cluster_id": "cluster-1",
            "cluster_name": "analytics",
            "worker_count": 4,
            "min_autoscale_workers": 2,
            "max_autoscale_workers": 4,
            "auto_termination_minutes": 30,
        },
        utilization=_utilization(cpu, memory) if utilization is None else utilization,
        worker_levels=worker_levels or [],
        runtime=runtime or {},
        quality=quality or _quality(),
        observation_start="2026-09-01T00:00:00Z",
        observation_end="2026-09-01T01:00:00Z",
        collected_at="2026-09-01T01:01:00Z",
        lineage={"resource_id": "cluster-1", "environment_id": "env-1"},
    )


def _evaluations(result):
    return result.summary["rule_evaluations"]


def _finding(result, kind):
    return next(finding for finding in result.findings if finding.finding == kind)


def _worker_levels(values, minutes=None):
    if minutes is None:
        minutes = [30] * len(values)
    timestamp = datetime(2026, 9, 1, tzinfo=timezone.utc)
    levels = []
    for workers, duration in zip(values, minutes):
        levels.append({
            "active_workers": workers,
            "minutes_at_level": duration,
            "start_time": timestamp.isoformat(),
        })
        timestamp += timedelta(minutes=duration)
    return levels


def test_sizing_triggers_with_sufficient_historical_evidence():
    result = analyze_compute_evidence(_evidence(cpu=20, memory=30))

    assert _finding(result, "OVERSIZED").evaluation_status == "TRIGGERED"


def test_sizing_does_not_trigger_for_balanced_utilization():
    result = analyze_compute_evidence(_evidence(cpu=50, memory=50))

    assert not any(f.optimization_area == "CLUSTER_SIZING" for f in result.findings)
    assert _evaluations(result)["STAGE1.CLUSTER_SIZING"]["status"] == "NO_FINDING"


@pytest.mark.parametrize("completeness", ["INSUFFICIENT", "FAILED", "UNKNOWN"])
def test_evidence_quality_blocks_positive_findings(completeness):
    result = analyze_compute_evidence(
        _evidence(cpu=5, memory=5, quality=_quality(completeness=completeness))
    )

    assert result.findings == []
    assert all(value["status"] == "NOT_EVALUABLE" for value in _evaluations(result).values())


def test_missing_cpu_does_not_become_zero():
    rows = _utilization(cpu=5, memory=5)
    for row in rows:
        row.pop("cpu_user_percent")
        row.pop("cpu_system_percent")

    result = analyze_compute_evidence(_evidence(utilization=rows))

    assert result.findings == []
    assert "CPU history is missing" in _evaluations(result)["STAGE1.CLUSTER_SIZING"]["reason"]


def test_missing_memory_does_not_become_zero():
    rows = _utilization(cpu=5, memory=5)
    for row in rows:
        row.pop("mem_used_percent")

    result = analyze_compute_evidence(_evidence(utilization=rows))

    assert result.findings == []
    assert "Memory history is missing" in _evaluations(result)["STAGE1.CLUSTER_SIZING"]["reason"]


def test_sizing_requires_minimum_observation_period():
    rows = _utilization(
        cpu=5,
        memory=5,
        intervals=[
            ("2026-09-01T00:00:00Z", "2026-09-01T00:20:00Z"),
            ("2026-09-01T00:20:00Z", "2026-09-01T00:40:00Z"),
        ],
    )

    result = analyze_compute_evidence(_evidence(utilization=rows))

    assert result.findings == []
    assert "shorter than 60 minutes" in _evaluations(result)["STAGE1.CLUSTER_SIZING"]["reason"]


def test_single_metric_point_cannot_trigger_sizing():
    rows = _utilization(cpu=5, memory=5)[:1]
    rows[0]["end_time"] = "2026-09-01T01:00:00Z"
    rows[0]["duration_minutes"] = 60

    result = analyze_compute_evidence(_evidence(utilization=rows))

    assert result.findings == []
    assert "two distinct" in _evaluations(result)["STAGE1.CLUSTER_SIZING"]["reason"]


def test_sizing_low_utilization_threshold_is_inclusive():
    result = analyze_compute_evidence(_evidence(cpu=25, memory=40))

    assert _finding(result, "OVERSIZED")


def test_sizing_high_utilization_threshold_is_inclusive():
    result = analyze_compute_evidence(_evidence(cpu=80, memory=80))

    assert _finding(result, "UNDERSIZED")


@pytest.mark.parametrize(
    ("cpu", "memory", "expected"),
    [(90, 40, "CPU_HEAVY"), (40, 90, "MEMORY_HEAVY")],
)
def test_sizing_identifies_imbalanced_utilization(cpu, memory, expected):
    result = analyze_compute_evidence(_evidence(cpu=cpu, memory=memory))

    assert _finding(result, expected)


def test_runtime_idle_rule_triggers_from_history():
    result = analyze_compute_evidence(_evidence(cpu=15, memory=25))

    assert _finding(result, "EXCESSIVE_IDLE_RUNTIME")
    assert result.summary["runtime_activity_state"] == "IDLE"


def test_active_workload_does_not_trigger_idle_finding():
    result = analyze_compute_evidence(_evidence(cpu=60, memory=60))

    assert not any(f.optimization_area == "RUNTIME_OPTIMIZATION" for f in result.findings)
    assert result.summary["runtime_activity_state"] == "ACTIVE"


def test_post_workload_idle_rule_triggers_at_existing_boundary():
    result = analyze_compute_evidence(
        _evidence(cpu=60, memory=60, runtime={"post_task_idle_minutes": 20})
    )

    assert _finding(result, "POST_WORKLOAD_IDLE")
    assert result.summary["runtime_activity_state"] == "POST_WORKLOAD_IDLE"


def test_missing_timeline_makes_runtime_not_evaluable():
    result = analyze_compute_evidence(_evidence(utilization=[]))

    assert not any(f.optimization_area == "RUNTIME_OPTIMIZATION" for f in result.findings)
    assert _evaluations(result)["STAGE1.CLUSTER_RUNTIME"]["status"] == "NOT_EVALUABLE"
    assert result.summary["runtime_activity_state"] == "UNKNOWN"


def test_runtime_rejects_insufficient_history():
    result = analyze_compute_evidence(
        _evidence(cpu=5, memory=5, utilization=_utilization(cpu=5, memory=5)[:1])
    )

    assert not any(f.optimization_area == "RUNTIME_OPTIMIZATION" for f in result.findings)


@pytest.mark.parametrize(
    ("auto_termination", "expected"),
    [(0, "AUTO_TERMINATION_DISABLED"), (61, "AUTO_TERMINATION_TOO_HIGH")],
)
def test_runtime_auto_termination_rules_require_idle_evidence(auto_termination, expected):
    rows = _utilization(
        cpu=50,
        memory=50,
        intervals=[
            ("2026-09-01T00:00:00Z", "2026-09-01T00:20:00Z"),
            ("2026-09-01T00:20:00Z", "2026-09-01T01:40:00Z"),
        ],
    )
    rows[0].update(cpu_user_percent=10, cpu_system_percent=5, mem_used_percent=20, duration_minutes=20)
    rows[1]["duration_minutes"] = 80
    evidence = _evidence(
        utilization=rows,
        cluster={"cluster_id": "cluster-1", "worker_count": 4, "auto_termination_minutes": auto_termination},
    )

    result = analyze_compute_evidence(evidence)

    assert _finding(result, expected)


def test_autoscaling_max_headroom_rule_triggers():
    evidence = _evidence(
        cpu=50,
        memory=50,
        cluster={"cluster_id": "cluster-1", "worker_count": 4, "min_autoscale_workers": 2, "max_autoscale_workers": 8},
        worker_levels=_worker_levels([4, 3]),
    )

    result = analyze_compute_evidence(evidence)

    assert _finding(result, "MAX_WORKERS_TOO_HIGH")


def test_autoscaling_stable_workload_does_not_trigger():
    evidence = _evidence(
        cluster={"cluster_id": "cluster-1", "worker_count": 3, "min_autoscale_workers": 2, "max_autoscale_workers": 4},
        worker_levels=_worker_levels([2, 3]),
    )

    result = analyze_compute_evidence(evidence)

    assert not any(f.optimization_area == "AUTOSCALING_OPTIMIZATION" for f in result.findings)
    assert _evaluations(result)["STAGE1.AUTOSCALING"]["status"] == "NO_FINDING"


def test_autoscaling_missing_worker_history_is_not_evaluable():
    result = analyze_compute_evidence(_evidence())

    assert _evaluations(result)["STAGE1.AUTOSCALING"]["status"] == "NOT_EVALUABLE"


@pytest.mark.parametrize(
    "limits",
    [
        {"cluster_id": "cluster-1", "worker_count": 4, "min_autoscale_workers": 2},
        {"cluster_id": "cluster-1", "worker_count": 4, "max_autoscale_workers": 8},
    ],
)
def test_autoscaling_requires_min_and_max_configuration(limits):
    evidence = _evidence(cluster=limits, worker_levels=_worker_levels([3, 4]))

    result = analyze_compute_evidence(evidence)

    assert _evaluations(result)["STAGE1.AUTOSCALING"]["status"] == "NOT_EVALUABLE"


def test_autoscaling_headroom_boundary_is_inclusive():
    evidence = _evidence(
        cluster={"cluster_id": "cluster-1", "worker_count": 4, "min_autoscale_workers": 2, "max_autoscale_workers": 6},
        worker_levels=_worker_levels([4, 3]),
    )

    result = analyze_compute_evidence(evidence)

    assert _finding(result, "MAX_WORKERS_TOO_HIGH")


def test_autoscaling_minimum_time_boundary_is_inclusive():
    evidence = _evidence(
        cpu=20,
        memory=30,
        cluster={"cluster_id": "cluster-1", "worker_count": 3, "min_autoscale_workers": 2, "max_autoscale_workers": 4},
        worker_levels=_worker_levels([2, 2, 3], [24, 24, 12]),
    )

    result = analyze_compute_evidence(evidence)

    assert _finding(result, "MIN_WORKERS_TOO_HIGH")


def test_autoscaling_maximum_low_boundary_is_inclusive():
    evidence = _evidence(
        cpu=90,
        memory=90,
        cluster={"cluster_id": "cluster-1", "worker_count": 9, "min_autoscale_workers": 1, "max_autoscale_workers": 10},
        worker_levels=_worker_levels([9, 9]),
    )

    result = analyze_compute_evidence(evidence)

    assert _finding(result, "MAX_WORKERS_MAY_BE_TOO_LOW")


def test_scaling_instability_triggers_at_existing_change_count():
    evidence = _evidence(
        cluster={"cluster_id": "cluster-1", "worker_count": 2, "min_autoscale_workers": 1, "max_autoscale_workers": 4.4},
        worker_levels=_worker_levels([2, 3, 2, 3, 2, 3, 2], [10] * 7),
    )

    result = analyze_compute_evidence(evidence)

    assert _finding(result, "SCALING_INSTABILITY")


def test_scaling_instability_requires_ordered_worker_timestamps():
    levels = _worker_levels([2, 3, 2, 3, 2, 3, 2], [10] * 7)
    for level in levels:
        level.pop("start_time")
    evidence = _evidence(
        cluster={"cluster_id": "cluster-1", "worker_count": 2, "min_autoscale_workers": 1, "max_autoscale_workers": 4.4},
        worker_levels=levels,
    )

    result = analyze_compute_evidence(evidence)

    assert not any(f.finding == "SCALING_INSTABILITY" for f in result.findings)
    assert _evaluations(result)["STAGE1.AUTOSCALING"]["status"] == "NOT_EVALUABLE"


def test_worker_history_rules_can_run_without_cpu_or_memory():
    rows = _utilization(cpu=50, memory=50)
    for row in rows:
        row.pop("cpu_user_percent")
        row.pop("cpu_system_percent")
        row.pop("mem_used_percent")
    evidence = _evidence(
        utilization=rows,
        cluster={"cluster_id": "cluster-1", "worker_count": 4, "min_autoscale_workers": 2, "max_autoscale_workers": 8},
        worker_levels=_worker_levels([4, 3]),
    )

    result = analyze_compute_evidence(evidence)

    assert [finding.finding for finding in result.findings] == ["MAX_WORKERS_TOO_HIGH"]
    assert _evaluations(result)["STAGE1.CLUSTER_SIZING"]["status"] == "NOT_EVALUABLE"


def test_stale_evidence_blocks_positive_findings():
    result = analyze_compute_evidence(
        _evidence(cpu=5, memory=5, quality=_quality(freshness="STALE"))
    )

    assert result.findings == []
    assert all("stale" in item["reason"] for item in _evaluations(result).values())


def test_unknown_freshness_caps_confidence():
    result = analyze_compute_evidence(
        _evidence(cpu=5, memory=5, quality=_quality(freshness="UNKNOWN"))
    )

    assert _finding(result, "OVERSIZED").confidence == "low"


def test_repeated_evaluation_has_stable_finding_identity_and_no_duplicates():
    evidence = _evidence(cpu=5, memory=5)
    first = analyze_compute_evidence(evidence)
    second = analyze_compute_evidence(deepcopy(evidence))

    assert [finding.finding_id for finding in first.findings] == [finding.finding_id for finding in second.findings]
    assert len({finding.finding_id for finding in first.findings}) == len(first.findings)


def test_finding_contains_rule_and_evidence_reference():
    finding = _finding(analyze_compute_evidence(_evidence(cpu=5, memory=5)), "OVERSIZED")

    assert finding.rule_id == "STAGE1.CLUSTER_SIZING.OVERSIZED"
    assert finding.evidence_reference["resource_id"] == "cluster-1"
    assert finding.evidence_reference["lineage"]["environment_id"] == "env-1"
    assert finding.current_state["worker_count"] == 4
    assert finding.proposed_direction


def test_engine_output_is_read_only_and_does_not_estimate_savings(monkeypatch):
    mutation_calls = []
    monkeypatch.setattr(
        "platforms.databricks.DatabricksAdapter.resize_cluster",
        lambda *args, **kwargs: mutation_calls.append((args, kwargs)),
        raising=False,
    )

    result = analyze_compute_evidence(_evidence(cpu=5, memory=5))

    assert mutation_calls == []
    assert result.summary["execution_enabled"] is False
    assert result.summary["savings_status"] == "NOT_ESTIMATED"
    assert all(f.expected_impact == {"savings_status": "NOT_ESTIMATED"} for f in result.findings)
