from datetime import datetime, timedelta, timezone

from services.compute_optimization.evidence_normalizer import (
    derive_post_task_idle,
    normalize_node_timeline,
)


def _timeline(cluster_id="cluster-a", bucket_count=4, start="2026-09-28T10:00:00Z"):
    first = datetime.fromisoformat(start.replace("Z", "+00:00"))
    rows = []
    for bucket_index in range(bucket_count):
        bucket_start = first + timedelta(minutes=30 * bucket_index)
        bucket_end = bucket_start + timedelta(minutes=30)
        for instance_id, driver, cpu, memory in (
            (f"{cluster_id}-driver", True, 10, 30),
            (f"{cluster_id}-worker-1", False, 15, 40),
            (f"{cluster_id}-worker-2", False, 20, 50),
        ):
            rows.append({
                "cluster_id": cluster_id,
                "instance_id": instance_id,
                "driver": driver,
                "start_time": bucket_start.isoformat().replace("+00:00", "Z"),
                "end_time": bucket_end.isoformat().replace("+00:00", "Z"),
                "cpu_user_percent": cpu,
                "cpu_system_percent": 2,
                "mem_used_percent": memory,
            })
    return rows


def test_node_rows_are_aggregated_into_distinct_observed_time_buckets():
    normalized = normalize_node_timeline("cluster-a", _timeline())

    assert len(normalized.utilization) == 4
    assert [row["instance_count"] for row in normalized.utilization] == [3] * 4
    assert normalized.utilization[0]["cpu_user_percent"] == 15
    assert normalized.utilization[0]["mem_used_percent"] == 40
    assert normalized.worker_history_available is True
    assert [row["active_workers"] for row in normalized.worker_levels] == [2] * 4
    assert sum(row["minutes_at_level"] for row in normalized.worker_levels) == 120


def test_one_shared_interval_is_one_sample_not_multiple_fake_samples():
    rows = _timeline(bucket_count=1)

    normalized = normalize_node_timeline("cluster-a", rows)

    assert len(normalized.utilization) == 1
    assert len(normalized.worker_levels) == 1


def test_short_observation_window_is_not_extended_or_duplicated():
    rows = _timeline(bucket_count=1)

    normalized = normalize_node_timeline("cluster-a", rows)

    assert normalized.utilization[0]["duration_minutes"] == 30
    assert normalized.utilization[0]["start_time"] != normalized.utilization[0]["end_time"]


def test_gap_between_time_buckets_is_marked_inconsistent():
    rows = _timeline()
    for row in rows:
        if row["start_time"] >= "2026-09-28T11:00:00Z":
            start = datetime.fromisoformat(row["start_time"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(row["end_time"].replace("Z", "+00:00"))
            row["start_time"] = (start + timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
            row["end_time"] = (end + timedelta(minutes=10)).isoformat().replace("+00:00", "Z")

    normalized = normalize_node_timeline("cluster-a", rows)

    assert normalized.utilization == []
    assert normalized.consistency == "INCONSISTENT"
    assert "gap or overlap" in normalized.error


def test_missing_cpu_rejects_only_that_clusters_timeline():
    rows = _timeline()
    for row in rows:
        if row["cluster_id"] == "cluster-a":
            row.pop("cpu_user_percent")
    rows.extend(_timeline(cluster_id="cluster-b"))

    invalid = normalize_node_timeline("cluster-a", rows)
    valid = normalize_node_timeline("cluster-b", rows)

    assert invalid.utilization == []
    assert invalid.consistency == "INCONSISTENT"
    assert len(valid.utilization) == 4


def test_missing_memory_rejects_timeline_without_zero_substitution():
    rows = _timeline()
    for row in rows:
        row.pop("mem_used_percent")

    normalized = normalize_node_timeline("cluster-a", rows)

    assert normalized.utilization == []
    assert "CPU or memory" in normalized.error


def test_invalid_timestamp_is_explicit_and_does_not_enter_observation_window():
    rows = _timeline()
    rows[0]["start_time"] = "not-a-timestamp"

    normalized = normalize_node_timeline("cluster-a", rows)

    assert normalized.timestamp_valid is False
    assert normalized.consistency == "INCONSISTENT"
    assert normalized.utilization == []


def test_other_cluster_timestamps_are_never_aggregated():
    rows = _timeline(cluster_id="cluster-b") + _timeline(cluster_id="cluster-a", bucket_count=1)

    normalized = normalize_node_timeline("cluster-a", rows)

    assert len(normalized.utilization) == 1
    assert {row["cluster_id"] for row in normalized.utilization} == {"cluster-a"}


def test_worker_history_requires_node_identity_and_driver_flag():
    rows = _timeline()
    for row in rows:
        row.pop("driver")

    normalized = normalize_node_timeline("cluster-a", rows)

    assert len(normalized.utilization) == 4
    assert normalized.worker_levels == []
    assert normalized.worker_history_available is False


def test_invalid_driver_flag_does_not_create_worker_history():
    rows = _timeline()
    rows[0]["driver"] = "maybe"

    normalized = normalize_node_timeline("cluster-a", rows)

    assert len(normalized.utilization) == 4
    assert normalized.worker_levels == []
    assert normalized.worker_history_available is False


def test_worker_history_has_observed_multiple_levels_without_inventing_values():
    rows = _timeline(bucket_count=2)
    # First bucket has two workers, second has one worker; each count comes
    # from distinct non-driver instance IDs present in that observed bucket.
    second_start = "2026-09-28T10:30:00Z"
    rows = [
        row for row in rows
        if not (row["start_time"] == second_start and row["instance_id"].endswith("worker-2"))
    ]

    normalized = normalize_node_timeline("cluster-a", rows)

    assert [row["active_workers"] for row in normalized.worker_levels] == [2, 1]


def test_worker_records_can_be_sorted_by_real_bucket_timestamps():
    normalized = normalize_node_timeline("cluster-a", list(reversed(_timeline())))

    assert [row["start_time"] for row in normalized.worker_levels] == sorted(
        row["start_time"] for row in normalized.worker_levels
    )


def test_task_completion_maps_to_contiguous_same_cluster_idle_buckets():
    rows = _timeline(bucket_count=4)
    for row in rows:
        if row["start_time"] >= "2026-09-28T11:00:00Z":
            row["cpu_user_percent"] = 5
            row["cpu_system_percent"] = 2
            row["mem_used_percent"] = 20
    normalized = normalize_node_timeline("cluster-a", rows)
    task_rows = [{
        "cluster_id": "cluster-a",
        "task_run_id": "task-1",
        "start_time": "2026-09-28T10:00:00Z",
        "end_time": "2026-09-28T11:00:00Z",
    }]

    runtime = derive_post_task_idle(
        "cluster-a", normalized.utilization, task_rows,
        idle_cpu_threshold=15, idle_memory_threshold=25,
    )

    assert runtime["post_task_idle_minutes"] == 60
    assert runtime["task_run_id"] == "task-1"


def test_unrelated_task_does_not_map_to_cluster_idle():
    normalized = normalize_node_timeline("cluster-a", _timeline())
    task_rows = [{
        "cluster_id": "cluster-a",
        "task_run_id": "unrelated-task",
        "start_time": "2026-09-28T08:00:00Z",
        "end_time": "2026-09-28T09:00:00Z",
    }]

    runtime = derive_post_task_idle(
        "cluster-a", normalized.utilization, task_rows,
        idle_cpu_threshold=15, idle_memory_threshold=25,
    )

    assert runtime is None


def test_cross_cluster_task_never_maps_to_another_clusters_idle():
    rows = _timeline()
    for row in rows:
        row["cpu_user_percent"] = 5
        row["cpu_system_percent"] = 2
        row["mem_used_percent"] = 20
    normalized = normalize_node_timeline("cluster-a", rows)
    task_rows = [{
        "cluster_id": "cluster-b",
        "task_run_id": "task-b",
        "start_time": "2026-09-28T09:30:00Z",
        "end_time": "2026-09-28T10:00:00Z",
    }]

    runtime = derive_post_task_idle(
        "cluster-a", normalized.utilization, task_rows,
        idle_cpu_threshold=15, idle_memory_threshold=25,
    )

    assert runtime is None


def test_invalid_task_timestamps_do_not_create_idle_mapping():
    normalized = normalize_node_timeline("cluster-a", _timeline())
    task_rows = [{
        "cluster_id": "cluster-a",
        "task_run_id": "task-invalid",
        "start_time": "bad",
        "end_time": "2026-09-28T10:00:00Z",
    }]

    runtime = derive_post_task_idle(
        "cluster-a", normalized.utilization, task_rows,
        idle_cpu_threshold=15, idle_memory_threshold=25,
    )

    assert runtime is None


def test_missing_task_evidence_is_not_evaluable():
    normalized = normalize_node_timeline("cluster-a", _timeline())

    runtime = derive_post_task_idle(
        "cluster-a", normalized.utilization, [],
        idle_cpu_threshold=15, idle_memory_threshold=25,
    )

    assert runtime is None