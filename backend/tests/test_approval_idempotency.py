"""
Idempotent current state, with run identity and entity identity kept apart.

* Every execution stays in Run History (job_runs), with its own result.
* CLUSTER current state (reporting only, no approvals): one row per
  (environment, cluster) - same cluster -> update, new cluster -> insert.
* QUERY approval items: one per (environment, query_id); decisions are never
  reset or duplicated by a later run or refresh.
"""

import json
from datetime import datetime, timedelta

import pytest

from models import (
    AnalysisJob, ApprovalAudit, ClusterRecommendation, Connection, Environment, JobLog, JobRun, Notification,
    OptimizationApproval,
)
from services import approval_service as approvals
from services import cluster_state, job_service, run_events


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from main import app

    return TestClient(app)


@pytest.fixture
def env(db_session, customer):
    connection = Connection(
        id="conn-idem", customer_id=customer.id, platform="fabric", workspace="w",
        endpoint="https://api.fabric.microsoft.com/v1", auth_method="delegated",
        auth_metadata="{}", secret_encrypted=None, status="connected",
    )
    environment = Environment(
        id="env-idem", customer_id=customer.id, connection_id=connection.id, name="Fabric Dev",
        platform="fabric", auth_mode="user", workspace_id="ws", status="environment_ready",
    )
    db_session.add_all([connection, environment])
    db_session.commit()
    return connection, environment


def rows(ids, *, savings=100.0, label="Risky", workers=8, analyzed_at="2026-09-22T10:00:00"):
    return [
        {
            "cluster_id": f"cluster-{i}",
            "cluster_name": f"cluster-{i}",
            "optimization_label": label,
            "current_workers": workers,
            "recommended_max_workers": workers // 2,
            "avg_cpu_util": 12.5,
            "total_dbus_cost_usd": 1000.0 + i,
            "potential_monthly_savings": savings + i,
            "llm_optimization": f"Reduce max workers on cluster-{i}.",
            "acelo_analyzed_at": analyzed_at,
        }
        for i in ids
    ]


def complete_run(db, customer, connection, result_rows, *, created_at=None, domain="cluster"):
    """A COMPLETED run whose result is routed exactly as the worker routes it."""
    job = AnalysisJob(customer_id=customer.id, connection_id=connection.id, request="Analyze my clusters",
                      intent=domain, platform="fabric", status="COMPLETED")
    db.add(job)
    db.commit()
    run = JobRun(analysis_job_id=job.id, domain=domain, platform="fabric", status="COMPLETED",
                 platform_run_id=f"fabric-{job.id}", environment_id="env-idem",
                 created_at=created_at or datetime.utcnow(), completed_at=datetime.utcnow())
    payload = {"source_payload": {"rows": result_rows, "row_count": len(result_rows)}}
    run.result_json = json.dumps(payload)
    db.add(run)
    db.commit()
    job_service.route_result(db, run, payload)
    return run


def clusters(db, customer):
    db.expire_all()
    return db.query(ClusterRecommendation).filter(ClusterRecommendation.customer_id == customer.id).all()


H = lambda customer: {"X-Customer-Id": customer.id}  # noqa: E731


# CLUSTER - TEST 1 ------------------------------------------------------------------------

def test_same_100_clusters_twice_gives_100_current_records(db_session, customer, env):
    connection, _ = env
    complete_run(db_session, customer, connection, rows(range(1, 101)))
    complete_run(db_session, customer, connection, rows(range(1, 101)))
    records = clusters(db_session, customer)
    assert len(records) == 100
    assert len({(r.environment_key, r.cluster_id) for r in records}) == 100
    # Cluster is reporting only: nothing entered the approval queue.
    assert db_session.query(OptimizationApproval).count() == 0


# CLUSTER - TEST 2 ------------------------------------------------------------------------

def test_95_same_plus_5_new_gives_105(db_session, customer, env):
    connection, _ = env
    complete_run(db_session, customer, connection, rows(range(1, 101)))
    complete_run(db_session, customer, connection, rows(list(range(1, 96)) + list(range(101, 106))))
    assert len(clusters(db_session, customer)) == 105


# CLUSTER - TEST 3 ------------------------------------------------------------------------

def test_changed_values_update_existing_records(db_session, customer, env):
    connection, _ = env
    run_a = complete_run(db_session, customer, connection, rows(range(1, 101), savings=100.0))
    run_b = complete_run(db_session, customer, connection, rows(range(1, 101), savings=500.0, workers=16))
    records = clusters(db_session, customer)
    assert len(records) == 100
    one = next(r for r in records if r.cluster_id == "cluster-1")
    assert one.potential_monthly_savings == 501.0
    assert one.current_workers == 16 and one.recommended_max_workers == 8
    assert one.first_run_id == run_a.id and one.last_run_id == run_b.id
    assert one.last_platform_run_id == run_b.platform_run_id


# CLUSTER - TEST 4 ------------------------------------------------------------------------

def test_both_runs_stay_in_history_while_current_state_has_100(client, db_session, customer, env):
    connection, _ = env
    a = complete_run(db_session, customer, connection, rows(range(1, 101)),
                     created_at=datetime.utcnow() - timedelta(minutes=5))
    b = complete_run(db_session, customer, connection, rows(range(1, 101)))
    history = client.get("/api/runs", headers=H(customer)).json()
    assert history["total"] == 2
    assert {r["acelo_run_id"] for r in history["runs"]} == {a.id, b.id}
    assert {r["platform_run_id"] for r in history["runs"]} == {a.platform_run_id, b.platform_run_id}
    # Each run keeps its own results.
    assert client.get(f"/api/runs/{a.id}", headers=H(customer)).json()["result"]["source_payload"]["row_count"] == 100
    body = client.get("/api/clusters", headers=H(customer)).json()
    assert len(body["clusters"]) == 100 and body["summary"]["clusters_analyzed"] == 100
    assert client.get("/api/approvals", headers=H(customer)).json() == []


# CLUSTER - TEST 5 ------------------------------------------------------------------------

def test_replaying_a_run_adds_nothing(client, db_session, customer, env):
    connection, _ = env
    run = complete_run(db_session, customer, connection, rows(range(1, 101)))
    payload = json.loads(run.result_json)
    for _ in range(3):
        job_service.route_result(db_session, run, payload)  # worker replay / reopened Results
        assert len(client.get("/api/clusters", headers=H(customer)).json()["clusters"]) == 100


# CLUSTER - TEST 6 ------------------------------------------------------------------------

def test_overview_is_stable_across_reruns_and_refreshes(client, db_session, customer, env):
    connection, _ = env
    complete_run(db_session, customer, connection, rows(range(1, 101)))
    first = client.get("/api/overview", headers=H(customer)).json()
    complete_run(db_session, customer, connection, rows(range(1, 101)))
    for _ in range(3):
        body = client.get("/api/overview", headers=H(customer)).json()
        assert body["kpis"] == first["kpis"]
        assert body["cluster"]["clustersAnalyzed"] == 100 and body["cluster"]["risky"] == 100
        # Cluster never inflates query approval numbers.
        assert body["query"]["approvalItems"] == 0 and body["query"]["byStatus"]["PENDING"] == 0
        assert body["executions"]["succeeded"] == 2
    assert first["cluster"]["potentialSavings"] == round(sum(100.0 + i for i in range(1, 101)), 2)
    # Unknown query values are null ("Not available"), never 0.
    assert first["query"]["unhealthyQueries"] is None and first["query"]["potentialSavings"] is None


# CLUSTER - TEST 7 ------------------------------------------------------------------------

def test_duplicate_source_rows_become_one_record_with_the_latest_values(db_session, customer, env):
    connection, _ = env
    older = rows([1, 2], savings=10.0, analyzed_at="2026-09-22T09:00:00")
    newer = rows([1], savings=90.0, analyzed_at="2026-09-22T11:00:00")
    complete_run(db_session, customer, connection, older + newer + older)
    records = clusters(db_session, customer)
    assert len(records) == 2
    assert next(r for r in records if r.cluster_id == "cluster-1").potential_monthly_savings == 91.0


def test_cluster_results_show_optimized_cost_and_run_ids(client, db_session, customer, env):
    connection, _ = env
    run = complete_run(db_session, customer, connection, rows([7]))
    item = client.get("/api/clusters", headers=H(customer)).json()["clusters"][0]
    assert item["current_cost_usd"] == 1007.0 and item["potential_monthly_savings"] == 107.0
    assert item["optimized_cost_usd"] == 900.0  # current cost - potential savings
    assert item["avg_cpu_util"] == 12.5 and item["recommendation"] == "Reduce max workers on cluster-7."
    assert item["last_run_id"] == run.id and item["last_platform_run_id"] == run.platform_run_id
    assert item["analyzed_at"] == "2026-09-22T10:00:00"


# QUERY ------------------------------------------------------------------------------------

def qrow(query_id, *, status="verified", savings=4.0, sql=None):
    return {"query_id": query_id, "query_text": f"SELECT * FROM t WHERE k = '{query_id}'",
            "llm_refactor_suggestion": sql or f"SELECT k FROM t WHERE k = '{query_id}'",
            "workflow_status": status, "potential_savings_usd": savings, "actual_cost_usd": 10.0,
            "bottleneck_type": "DATA_SKEW", "primary_action": "Enable skew join"}


def _actor():
    return approvals.Actor(user_id="u-1", user_name="Hema")


def test_same_queries_twice_create_no_duplicates(db_session, customer, env):
    _, environment = env
    first = approvals.sync_query_tracking(db_session, environment, [qrow(f"q-{i}") for i in range(10)], "t")
    second = approvals.sync_query_tracking(db_session, environment, [qrow(f"q-{i}") for i in range(10)], "t")
    assert first["created"] == 10 and second["created"] == 0 and second["unchanged"] == 10
    assert db_session.query(OptimizationApproval).count() == 10


def test_duplicate_query_rows_are_one_item(db_session, customer, env):
    _, environment = env
    outcome = approvals.sync_query_tracking(db_session, environment, [qrow("q-1"), qrow("q-1"), qrow("q-2")], "t")
    assert outcome["unique_business_keys"] == 2 and outcome["duplicates_removed"] == 1
    assert db_session.query(OptimizationApproval).count() == 2


# QUERY - TEST 8 ---------------------------------------------------------------------------

def test_decided_query_is_not_duplicated_and_keeps_its_decision(client, db_session, customer, env):
    _, environment = env
    approvals.sync_query_tracking(db_session, environment, [qrow("q-1"), qrow("q-2"), qrow("q-3")], "t")
    items = {a.resource_id: a for a in db_session.query(OptimizationApproval)}
    approvals.approve(db_session, items["q-1"], _actor())
    approvals.reject(db_session, items["q-2"], _actor(), "Changes NULL semantics")

    # Same rows again: nothing changes, nothing is flagged.
    approvals.sync_query_tracking(db_session, environment, [qrow("q-1"), qrow("q-2"), qrow("q-3")], "t")
    db_session.refresh(items["q-1"])
    assert items["q-1"].status == "APPROVED" and not items["q-1"].requires_new_approval

    # A different optimization for the same queries: decisions kept, new values held for review.
    new_sql = [qrow(q, sql="SELECT /*+ BROADCAST */ k FROM t") for q in ("q-1", "q-2", "q-3")]
    approvals.sync_query_tracking(db_session, environment, new_sql, "t")
    db_session.expire_all()
    items = {a.resource_id: a for a in db_session.query(OptimizationApproval)}
    assert len(items) == 3
    assert items["q-1"].status == "APPROVED" and items["q-1"].approved_by == "Hema"
    assert items["q-1"].optimized_sql == "SELECT k FROM t WHERE k = 'q-1'"  # what was approved
    assert items["q-1"].requires_new_approval
    assert json.loads(items["q-1"].latest_recommendation_json)["optimized_sql"] == "SELECT /*+ BROADCAST */ k FROM t"
    assert items["q-2"].status == "REJECTED" and items["q-2"].requires_new_approval
    assert items["q-3"].status == "PENDING" and items["q-3"].optimized_sql == "SELECT /*+ BROADCAST */ k FROM t"

    body = client.post(f"/api/approvals/{items['q-1'].id}/reopen",
                       headers={**H(customer), "X-Acelo-User-Id": "u-1", "X-Acelo-User-Name": "Hema"}).json()
    assert body["status"] == "PENDING" and body["optimized_sql"] == "SELECT /*+ BROADCAST */ k FROM t"
    actions = [h["action"] for h in body["history"]]
    assert actions[0] == "CREATED" and "APPROVED" in actions and "NEW_RECOMMENDATION" in actions
    assert actions[-1] == "REOPENED"
    assert db_session.query(OptimizationApproval).count() == 3


def test_reopen_without_a_newer_recommendation_is_refused(client, db_session, customer, env):
    _, environment = env
    approvals.sync_query_tracking(db_session, environment, [qrow("q-1")], "t")
    item = db_session.query(OptimizationApproval).one()
    resp = client.post(f"/api/approvals/{item.id}/reopen",
                       headers={**H(customer), "X-Acelo-User-Id": "u-1", "X-Acelo-User-Name": "Hema"})
    assert resp.status_code == 409


def test_query_run_result_imports_items_once(db_session, customer, env):
    connection, _ = env
    run = complete_run(db_session, customer, connection, [qrow("q-1"), qrow("q-2", status="pending")],
                       domain="query")
    payload = json.loads(run.result_json)
    job_service.route_result(db_session, run, payload)  # replayed completion
    assert db_session.query(OptimizationApproval).count() == 1
    events = db_session.query(JobLog).filter(JobLog.job_run_id == run.id,
                                             JobLog.event_type == "APPROVALS_IMPORTED").count()
    assert events == 1
    assert db_session.query(ClusterRecommendation).count() == 0


# Legacy duplicates -------------------------------------------------------------------------

def test_existing_duplicates_are_merged_on_the_next_sync_with_history_kept(db_session, customer, env):
    _, environment = env
    old_a = OptimizationApproval(customer_id=customer.id, environment_id=environment.id, source="tracking_table",
                                 source_ref="tracking:A:q-1", platform="fabric", domain="query",
                                 resource_id="q-1", resource_name="q-1", status="PENDING")
    old_b = OptimizationApproval(customer_id=customer.id, environment_id=environment.id, source="tracking_table",
                                 source_ref="tracking:B:q-1", platform="fabric", domain="query",
                                 resource_id="q-1", resource_name="q-1", status="APPROVED", approved_by="Hema")
    db_session.add_all([old_a, old_b])
    db_session.flush()
    db_session.add(ApprovalAudit(approval_id=old_a.id, customer_id=customer.id, action="CREATED", new_status="PENDING"))
    db_session.commit()

    approvals.sync_query_tracking(db_session, environment, [qrow("q-1")], "t")
    db_session.expire_all()
    records = db_session.query(OptimizationApproval).all()
    assert len(records) == 1 and records[0].status == "APPROVED"  # the human decision survives
    actions = [a.action for a in db_session.query(ApprovalAudit).filter(ApprovalAudit.approval_id == records[0].id)]
    assert "CREATED" in actions and "DUPLICATES_MERGED" in actions


def test_collapse_duplicates_repairs_an_old_database(db_session, customer, env):
    _, environment = env
    for ref in ("run:A:c", "run:B:c", "tracking:x:c"):
        db_session.add(OptimizationApproval(customer_id=customer.id, environment_id=environment.id, source="run",
                                            source_ref=ref, platform="fabric", domain="query",
                                            resource_id="c", resource_name="c", status="PENDING"))
    db_session.commit()
    assert approvals.collapse_duplicates(db_session, customer.id) == 2
    db_session.expire_all()
    records = db_session.query(OptimizationApproval).all()
    assert len(records) == 1 and records[0].source_ref == "current:env-idem|query|c"


# Notifications -------------------------------------------------------------------------------

def test_completion_processed_twice_creates_one_notification(db_session, customer, env):
    connection, _ = env
    run = complete_run(db_session, customer, connection, rows([1]))
    for _ in range(3):
        run_events.on_status_change(db_session, run, "RUNNING")
    notes = db_session.query(Notification).filter(Notification.link == f"/runs/{run.id}").all()
    assert len(notes) == 1 and notes[0].body == "Cluster Optimization completed."
    events = db_session.query(JobLog).filter(JobLog.job_run_id == run.id,
                                             JobLog.event_type == "EXECUTION_COMPLETED").count()
    assert events == 1


# Tracking notebook -----------------------------------------------------------------------------

def test_tracking_notebook_merge_is_idempotent_by_business_key():
    from pathlib import Path

    nb = json.loads((Path(__file__).resolve().parents[1] / "optimization_package" / "approvals" /
                     "ClusterApprovalTracking.ipynb").read_text(encoding="utf-8"))
    source = "\n".join("".join(c["source"]) for c in nb["cells"])
    lower = source.lower()
    assert "row_number() over" in lower and "partition by coalesce(cluster_id, cluster_name)" in lower
    assert "using acelo_tracking_latest s" in lower
    assert "t.cluster_id = s.cluster_id" in lower
    assert "on t.acelo_run_id" not in lower
    matched = lower.split("when matched then update set")[1].split("when not matched")[0]
    assert "t.potential_monthly_savings = s.potential_monthly_savings" in matched
    assert "t.status" not in matched
    assert "target_duplicates_removed" in lower and "[approval_merge]" in lower
    for field in ("source_rows", "unique_business_keys", "inserted", "updated", "duplicates_removed"):
        assert f"{field}=" in lower


# Fabric cleanup notebook -----------------------------------------------------------------------

def test_cleanup_notebook_is_guarded_and_never_deployed():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "optimization_package"
    nb = json.loads((root / "maintenance" / "ACELO_Dev_Cleanup.ipynb").read_text(encoding="utf-8"))
    source = "\n".join("".join(c["source"]) for c in nb["cells"])
    assert 'confirm != "DELETE ACELO TEST DATA"' in source
    assert "realistic_cluster_dataset" in source and "Refusing to clean" in source
    assert "DROP " not in source.upper()
    assert "maintenance" not in (root / "manifest.json").read_text(encoding="utf-8")
