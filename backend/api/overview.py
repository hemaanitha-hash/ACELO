import json
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from api.deps import get_current_customer
from api.platform_context import ActivePlatform, active_context
from database import get_db
from models import AnalysisJob, ClusterRecommendation, Connection, Customer, JobRun, OptimizationApproval
from services import approval_service, cluster_state

router = APIRouter(prefix="/api/overview", tags=["overview"])

OPEN_STATUSES = (approval_service.PENDING, approval_service.APPROVED)


def _latest_run(db: Session, customer_id: str, domain: str, completed_only: bool = False) -> JobRun | None:
    query = (
        db.query(JobRun)
        .join(AnalysisJob, JobRun.analysis_job_id == AnalysisJob.id)
        .filter(AnalysisJob.customer_id == customer_id, JobRun.domain == domain)
    )
    if completed_only:
        query = query.filter(JobRun.status == "COMPLETED")
    return query.order_by(JobRun.created_at.desc()).first()


def _run_ref(run: JobRun | None) -> dict | None:
    if run is None:
        return None
    return {
        "acelo_run_id": run.id,
        "platform_run_id": run.platform_run_id,
        "status": run.status,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
    }


def _money(values) -> float | None:
    """Sum of the values that are known; None (not 0) when none are."""
    known = [v for v in values if v is not None]
    return round(sum(known), 2) if known else None


@router.get("")
def get_overview_data(
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
    context: ActivePlatform = Depends(active_context),
):
    """
    Dashboard figures from REAL data only, CLUSTER and QUERY kept apart:

    * cluster: current cluster optimization state (one row per cluster, however
      many runs) - reporting only, no approvals.
    * query:   query approval items (one per query_id) + the latest query run's
      detection counts.
    * executions: every run (append-only history).

    Unknown values are null (the UI shows "Not available"), never 0.
    """
    # The platform the user is actually in. Previously this picked the first
    # connected connection, so with both a Fabric and a Databricks connection
    # configured the Overview showed whichever happened to sort first.
    connection = context.connection

    clusters = cluster_state.summary(db, customer.id)
    last_cluster = _latest_run(db, customer.id, "cluster")

    query_items = (
        db.query(OptimizationApproval)
        .filter(OptimizationApproval.customer_id == customer.id, OptimizationApproval.domain == "query")
        .all()
    )
    last_query = _latest_run(db, customer.id, "query")
    last_query_done = _latest_run(db, customer.id, "query", completed_only=True)
    detection = {}
    if last_query_done and last_query_done.result_json:
        detection = (json.loads(last_query_done.result_json).get("source_payload") or {})

    runs = (
        db.query(JobRun)
        .join(AnalysisJob, JobRun.analysis_job_id == AnalysisJob.id)
        .filter(AnalysisJob.customer_id == customer.id)
    )
    executions = {
        "total": runs.count(),
        "succeeded": runs.filter(JobRun.status == "COMPLETED").count(),
        "failed": runs.filter(JobRun.status == "FAILED").count(),
        "cancelled": runs.filter(JobRun.status == "CANCELLED").count(),
    }
    executions["active"] = executions["total"] - executions["succeeded"] - executions["failed"] - executions["cancelled"]

    last_job = (
        db.query(AnalysisJob)
        .filter(AnalysisJob.customer_id == customer.id)
        .order_by(AnalysisJob.created_at.desc())
        .first()
    )
    last_minutes = None
    if last_job and last_job.completed_at:
        delta = datetime.utcnow() - last_job.completed_at
        last_minutes = max(1, int(delta.total_seconds() // 60))

    open_queries = [a for a in query_items if a.status in OPEN_STATUSES]
    cluster_block = {
        "clustersAnalyzed": clusters["clusters_analyzed"],
        "risky": clusters["by_label"].get("Risky", 0),
        "moderatelyOptimized": clusters["by_label"].get("Moderately Optimized", 0),
        "optimized": clusters["by_label"].get("Optimized", 0),
        "potentialSavings": clusters["potential_savings"],
        "monthlyCost": clusters["monthly_cost"],
        "latestRun": _run_ref(last_cluster),
    }
    query_block = {
        "unhealthyQueries": detection.get("unhealthy_queries"),
        "optimizationOpportunities": detection.get("optimization_opportunities"),
        "approvalItems": len(query_items),
        "byStatus": approval_service.summary(db, customer.id, "query"),
        "potentialSavings": _money(a.potential_monthly_savings for a in open_queries),
        "latestRun": _run_ref(last_query),
    }

    has_data = bool(clusters["clusters_analyzed"] or query_items or executions["succeeded"])
    return {
        "userName": customer.name,
        "platformConnected": connection.platform if connection else None,
        "workspace": connection.workspace if connection else None,
        "lastAnalysisMinutesAgo": last_minutes,
        "hasData": has_data,
        "completedRuns": executions["succeeded"],
        "cluster": cluster_block,
        "query": query_block,
        "executions": executions,
        # Headline KPIs, each from its own domain (never mixed into one count).
        "kpis": {
            "monthlyCost": cluster_block["monthlyCost"],
            "potentialSavings": _money([cluster_block["potentialSavings"], query_block["potentialSavings"]]),
            "openOpportunities": (clusters["by_label"].get("Risky", 0)
                                  + clusters["by_label"].get("Moderately Optimized", 0) + len(open_queries)),
            "optimizationHealth": None,
        },
        "health": [
            {"domain": "cluster", "label": "Cluster Health", "health": None,
             "monthlyCost": cluster_block["monthlyCost"], "potentialSavings": cluster_block["potentialSavings"],
             "openFindings": cluster_block["risky"] + cluster_block["moderatelyOptimized"]},
            {"domain": "query", "label": "Query Health", "health": None, "monthlyCost": None,
             "potentialSavings": query_block["potentialSavings"], "openFindings": len(open_queries)},
            {"domain": "storage", "label": "Storage Health", "health": None, "monthlyCost": None,
             "potentialSavings": None, "openFindings": 0},
        ],
    }


clusters_router = APIRouter(prefix="/api/clusters", tags=["clusters"])


@clusters_router.get("")
def list_cluster_recommendations(
    label: str | None = None,
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
):
    """Current cluster optimization state: one row per cluster (latest run wins)."""
    query = db.query(ClusterRecommendation).filter(ClusterRecommendation.customer_id == customer.id)
    if label:
        query = query.filter(ClusterRecommendation.optimization_label == label)
    rows = query.all()
    rows.sort(key=lambda r: (r.potential_monthly_savings is None, -(r.potential_monthly_savings or 0)))
    return {"summary": cluster_state.summary(db, customer.id), "clusters": [cluster_state.serialize(r) for r in rows]}
