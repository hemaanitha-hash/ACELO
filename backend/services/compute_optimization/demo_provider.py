from datetime import datetime
from .models import ComputeEvidence

def _duration(row):
    if row.get("duration_minutes") is not None: return row["duration_minutes"]
    try:
        a = datetime.fromisoformat(str(row["start_time"]).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(row["end_time"]).replace("Z", "+00:00"))
        return max(0, (b-a).total_seconds()/60)
    except Exception:
        return 0.0

def from_demo_rows(cluster, node_timeline, billing_usage, worker_levels=None, runtime=None):
    utilization=[]
    for row in node_timeline:
        item=dict(row); item["duration_minutes"]=_duration(item); utilization.append(item)
    return ComputeEvidence(cluster=dict(cluster), utilization=utilization,
                           billing=[dict(x) for x in billing_usage],
                           worker_levels=[dict(x) for x in (worker_levels or [])],
                           runtime=dict(runtime or {}))
