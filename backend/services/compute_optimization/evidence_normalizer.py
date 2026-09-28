from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import isfinite
from typing import Any


@dataclass
class TimelineNormalization:
    utilization: list[dict[str, Any]] = field(default_factory=list)
    worker_levels: list[dict[str, Any]] = field(default_factory=list)
    timestamp_valid: bool = True
    consistency: str = "CONSISTENT"
    error: str | None = None
    worker_history_available: bool = False


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _numeric_percent(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(result) or not 0 <= result <= 100:
        return None
    return result


def _driver_flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    return None


def normalize_node_timeline(
    cluster_id: str,
    rows: list[dict[str, Any]],
) -> TimelineNormalization:
    """Aggregate observed per-instance rows into one record per real time bucket."""
    buckets: dict[tuple[datetime, datetime], list[dict[str, Any]]] = {}
    target_rows = [row for row in rows if row.get("cluster_id") == cluster_id]

    for index, row in enumerate(target_rows):
        start = _timestamp(row.get("start_time"))
        end = _timestamp(row.get("end_time"))
        if start is None or end is None or end <= start:
            return TimelineNormalization(
                timestamp_valid=False,
                consistency="INCONSISTENT",
                error=f"Node timeline row {index + 1} has an invalid observation interval.",
            )
        cpu_user = _numeric_percent(row.get("cpu_user_percent"))
        cpu_system = _numeric_percent(row.get("cpu_system_percent"))
        memory = _numeric_percent(row.get("mem_used_percent"))
        if cpu_user is None or cpu_system is None or memory is None:
            return TimelineNormalization(
                consistency="INCONSISTENT",
                error=f"Node timeline row {index + 1} lacks valid CPU or memory values.",
            )
        buckets.setdefault((start, end), []).append({
            "instance_id": row.get("instance_id"),
            "driver": _driver_flag(row.get("driver")),
            "cpu_user_percent": cpu_user,
            "cpu_system_percent": cpu_system,
            "mem_used_percent": memory,
        })

    utilization: list[dict[str, Any]] = []
    worker_levels: list[dict[str, Any]] = []
    worker_history_available = bool(target_rows)
    ordered_intervals = sorted(buckets)
    for previous, current in zip(ordered_intervals, ordered_intervals[1:]):
        if current[0] != previous[1]:
            return TimelineNormalization(
                consistency="INCONSISTENT",
                error="Node timeline contains a gap or overlap between observation buckets.",
            )

    for start, end in ordered_intervals:
        samples = buckets[(start, end)]
        instance_ids = [sample["instance_id"] for sample in samples]
        if any(not instance_id for instance_id in instance_ids):
            worker_history_available = False
        elif len(set(instance_ids)) != len(instance_ids):
            return TimelineNormalization(
                consistency="INCONSISTENT",
                error="Node timeline contains duplicate instance IDs in one observation bucket.",
            )

        driver_flags = [sample["driver"] for sample in samples]
        if any(flag is None for flag in driver_flags):
            worker_history_available = False

        duration_minutes = (end - start).total_seconds() / 60
        utilization.append({
            "cluster_id": cluster_id,
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "duration_minutes": duration_minutes,
            "cpu_user_percent": sum(sample["cpu_user_percent"] for sample in samples) / len(samples),
            "cpu_system_percent": sum(sample["cpu_system_percent"] for sample in samples) / len(samples),
            "mem_used_percent": sum(sample["mem_used_percent"] for sample in samples) / len(samples),
            "instance_count": len(samples),
        })

        if all(flag is not None for flag in driver_flags) and all(instance_ids):
            worker_levels.append({
                "cluster_id": cluster_id,
                "start_time": start.isoformat(),
                "minutes_at_level": duration_minutes,
                "active_workers": sum(not flag for flag in driver_flags),
            })

    return TimelineNormalization(
        utilization=utilization,
        worker_levels=worker_levels if worker_history_available else [],
        worker_history_available=worker_history_available and bool(worker_levels),
    )


def derive_post_task_idle(
    cluster_id: str,
    utilization: list[dict[str, Any]],
    task_rows: list[dict[str, Any]],
    *,
    idle_cpu_threshold: float,
    idle_memory_threshold: float,
) -> dict[str, Any] | None:
    """Derive idle duration only from contiguous observed buckets immediately after a same-cluster task end."""
    if not utilization or not task_rows:
        return None

    buckets = []
    for row in utilization:
        start = _timestamp(row.get("start_time"))
        end = _timestamp(row.get("end_time"))
        cpu_user = _numeric_percent(row.get("cpu_user_percent"))
        cpu_system = _numeric_percent(row.get("cpu_system_percent"))
        memory = _numeric_percent(row.get("mem_used_percent"))
        if start is None or end is None or end <= start or cpu_user is None or cpu_system is None or memory is None:
            return None
        buckets.append((start, end, min(100.0, cpu_user + cpu_system), memory))
    buckets.sort(key=lambda item: item[0])

    completions = []
    for task in task_rows:
        if task.get("cluster_id") != cluster_id:
            continue
        task_start = _timestamp(task.get("start_time"))
        task_end = _timestamp(task.get("end_time"))
        if task_start is None or task_end is None or task_end <= task_start:
            continue
        if buckets[0][0] <= task_end <= buckets[-1][1]:
            completions.append((task_end, task))

    for task_end, task in sorted(completions, key=lambda item: item[0], reverse=True):
        first_index = next(
            (index for index, bucket in enumerate(buckets) if bucket[0] == task_end),
            None,
        )
        if first_index is None:
            continue

        idle_minutes = 0.0
        expected_start = task_end
        final_end = task_end
        for start, end, cpu, memory in buckets[first_index:]:
            if start != expected_start:
                break
            if cpu > idle_cpu_threshold or memory > idle_memory_threshold:
                break
            idle_minutes += (end - start).total_seconds() / 60
            final_end = end
            expected_start = end

        return {
            "cluster_id": cluster_id,
            "task_run_id": task.get("task_run_id"),
            "task_completed_at": task_end.isoformat(),
            "idle_observation_end": final_end.isoformat(),
            "post_task_idle_minutes": idle_minutes,
        }

    return None