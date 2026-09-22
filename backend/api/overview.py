from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from api.deps import get_current_customer
from database import get_db
from models import AnalysisJob, Connection, Customer, JobRun, Recommendation

router = APIRouter(prefix="/api/overview", tags=["overview"])


@router.get("")
def get_overview_data(
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
):
    """
    Dashboard KPIs derived from REAL data only.

    This endpoint previously seeded demo rows on every call and fell back to
    hardcoded figures (42850.0 monthly cost, 14280.0 savings, health 78) whenever
    the database was empty — so an account that had never run an analysis still
    saw a fully populated dashboard. Those fallbacks are gone: with no completed
    runs, every figure is zero and `has_data` is false so the UI can show an
    honest empty state.
    """
    connection = (
        db.query(Connection)
        .filter(
            Connection.customer_id == customer.id,
            Connection.status == "connected",
            Connection.platform != "file",  # internal upload connection, not a platform
        )
        .first()
    )

    # Recommendations are scoped to this customer through their job runs.
    customer_job_run_ids = [
        run.id
        for run in db.query(JobRun)
        .join(AnalysisJob, JobRun.analysis_job_id == AnalysisJob.id)
        .filter(AnalysisJob.customer_id == customer.id)
        .all()
    ]
    recommendations = (
        db.query(Recommendation).filter(Recommendation.job_run_id.in_(customer_job_run_ids)).all()
        if customer_job_run_ids
        else []
    )
    open_recs = [r for r in recommendations if r.status in ("open", "approval_pending")]

    completed_runs = (
        db.query(JobRun)
        .join(AnalysisJob, JobRun.analysis_job_id == AnalysisJob.id)
        .filter(AnalysisJob.customer_id == customer.id, JobRun.status == "COMPLETED")
        .count()
    )

    total_cost = sum(r.current_monthly_cost or 0 for r in recommendations)
    total_savings = sum(r.estimated_monthly_savings or 0 for r in open_recs)

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

    def domain_block(domain: str, label: str):
        recs = [r for r in recommendations if r.domain == domain]
        return {
            "domain": domain,
            "label": label,
            # No synthetic health score: without real analysis output there is
            # nothing to score, so this stays null rather than inventing a number.
            "health": None,
            "monthlyCost": round(sum(r.current_monthly_cost or 0 for r in recs), 2),
            "potentialSavings": round(
                sum(r.estimated_monthly_savings or 0 for r in recs if r.status in ("open", "approval_pending")),
                2,
            ),
            "openFindings": len([r for r in recs if r.status in ("open", "approval_pending")]),
        }

    return {
        "userName": customer.name,
        "platformConnected": connection.platform if connection else None,
        "workspace": connection.workspace if connection else None,
        "lastAnalysisMinutesAgo": last_minutes,
        "hasData": bool(recommendations) or completed_runs > 0,
        "completedRuns": completed_runs,
        "kpis": {
            "monthlyCost": round(total_cost, 2),
            "potentialSavings": round(total_savings, 2),
            "openOpportunities": len(open_recs),
            "optimizationHealth": None,
        },
        "health": [
            domain_block("cluster", "Cluster Health"),
            domain_block("query", "Query Health"),
            domain_block("storage", "Storage Health"),
        ],
    }
