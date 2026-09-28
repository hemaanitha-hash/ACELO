import copy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from database import _ensure_recommendation_run_nullable, get_db
from main import app
from models import Customer, Environment, Recommendation
from services.compute_optimization.recommendations import (
    build_recommendations,
    persist_recommendations,
)


FINDING_TYPES = (
    "OVERSIZED",
    "UNDERSIZED",
    "CPU_HEAVY",
    "MEMORY_HEAVY",
    "POST_WORKLOAD_IDLE",
    "EXCESSIVE_IDLE_RUNTIME",
    "AUTO_TERMINATION_DISABLED",
    "AUTO_TERMINATION_TOO_HIGH",
    "MAX_WORKERS_TOO_HIGH",
    "MIN_WORKERS_TOO_HIGH",
    "MAX_WORKERS_MAY_BE_TOO_LOW",
    "SCALING_INSTABILITY",
)


def _finding(finding_type="OVERSIZED", *, customer_id="customer-1", env_id="env-1"):
    return {
        "resource": "analytics",
        "resource_type": "cluster",
        "resource_id": "cluster-1",
        "optimization_area": "CLUSTER_SIZING",
        "finding": finding_type,
        "observed_evidence": {"avg_cpu_percent": 12.5, "avg_memory_percent": 20},
        "recommendation": "Review capacity based on observed utilization.",
        "potential_issue": "Observed evidence indicates capacity may exceed workload demand.",
        "expected_impact": {"savings_status": "NOT_ESTIMATED"},
        "finding_id": f"finding-{finding_type}",
        "rule_id": f"STAGE1.TEST.{finding_type}",
        "domain": "CLUSTER_SIZING",
        "severity": "MEDIUM",
        "evidence_reference": {
            "resource_id": "cluster-1",
            "observation_start": "2026-09-01T00:00:00Z",
            "observation_end": "2026-09-01T02:00:00Z",
            "collected_at": "2026-09-01T02:01:00Z",
            "lineage": {
                "resource_id": "cluster-1",
                "cluster_id": "cluster-1",
                "resource_type": "CLASSIC_CLUSTER",
                "customer_id": customer_id,
                "environment_id": env_id,
                "workspace_name": "workspace-1",
            },
            "quality": {
                "source_available": True,
                "completeness": "COMPLETE",
                "freshness": "UNKNOWN",
                "timestamp_valid": True,
                "consistency": "CONSISTENT",
            },
        },
        "current_state": {"worker_count": 8, "min_autoscale_workers": 2, "max_autoscale_workers": 8},
        "observed_condition": "Existing deterministic rule triggered.",
        "rationale": "The supported evidence satisfies the deterministic rule.",
        "confidence": "low",
        "detected_at": "2026-09-01T02:01:00Z",
        "evaluation_status": "TRIGGERED",
        "proposed_direction": "Review worker capacity.",
    }


@pytest.mark.parametrize("finding_type", FINDING_TYPES)
def test_every_actual_stage1_finding_maps_to_directional_recommendation(finding_type):
    recommendation = build_recommendations([_finding(finding_type)])[0]

    assert recommendation["finding_type"] == finding_type
    assert recommendation["domain"] in {"CLUSTER_SIZING", "CLUSTER_RUNTIME", "AUTOSCALING"}
    assert recommendation["proposed_state"]["direction"]
    assert recommendation["proposed_state"]["reason"]
    assert recommendation["estimated_savings"] == {
        "status": "NOT_AVAILABLE",
        "estimated": None,
        "measured": None,
    }


@pytest.mark.parametrize("status", [
    "NO_FINDING", "NOT_EVALUABLE", "INSUFFICIENT_EVIDENCE", "FAILED",
    "STALE", "INVALID", "INCONSISTENT", "UNASSOCIATED",
])
def test_non_triggered_or_non_evaluable_findings_are_blocked(status):
    finding = _finding()
    finding["evaluation_status"] = status

    assert build_recommendations([finding]) == []


@pytest.mark.parametrize("quality_update", [
    {"completeness": "INSUFFICIENT"},
    {"completeness": "FAILED"},
    {"freshness": "STALE"},
    {"freshness": "EXPIRED"},
    {"timestamp_valid": False},
    {"consistency": "INVALID"},
    {"consistency": "INCONSISTENT"},
    {"source_available": False},
])
def test_invalid_evidence_quality_blocks_recommendations(quality_update):
    finding = _finding()
    finding["evidence_reference"]["quality"].update(quality_update)

    assert build_recommendations([finding]) == []


def test_missing_or_mismatched_resource_lineage_blocks_recommendation():
    missing = _finding()
    missing["evidence_reference"]["lineage"].pop("customer_id")
    mismatch = _finding()
    mismatch["evidence_reference"]["lineage"]["resource_id"] = "cluster-other"

    assert build_recommendations([missing, mismatch]) == []


def test_recommendation_is_traceable_and_inherits_confidence_and_severity():
    recommendation = build_recommendations([_finding()])[0]

    assert recommendation["finding_id"] == "finding-OVERSIZED"
    assert recommendation["rule_id"] == "STAGE1.TEST.OVERSIZED"
    assert recommendation["resource_id"] == "cluster-1"
    assert recommendation["evidence_references"][0]["lineage"]["environment_id"] == "env-1"
    assert recommendation["evidence_quality"]["freshness"] == "UNKNOWN"
    assert recommendation["confidence"] == "low"
    assert recommendation["severity"] == "MEDIUM"
    assert recommendation["risk"] == "MEDIUM"
    assert recommendation["current_state"]["worker_count"] == 8
    assert "direction" in recommendation["proposed_state"]
    assert recommendation["expected_impact"]["status"] == "POTENTIAL"


def test_identity_is_deterministic_and_scoped_to_customer_environment():
    original = _finding()
    repeated = copy.deepcopy(original)
    other_environment = _finding(env_id="env-2")
    other_customer = _finding(customer_id="customer-2")

    first = build_recommendations([original])[0]
    again = build_recommendations([repeated])[0]
    other_env = build_recommendations([other_environment])[0]
    other_tenant = build_recommendations([other_customer])[0]

    assert first["recommendation_id"] == again["recommendation_id"]
    assert first["recommendation_id"] != other_env["recommendation_id"]
    assert first["recommendation_id"] != other_tenant["recommendation_id"]


def test_recommendation_decision_does_not_depend_on_evidence_source_label():
    agent_finding = _finding()
    future_provider_finding = copy.deepcopy(agent_finding)
    agent_finding["evidence_reference"]["source"] = "databricks_ws.agent"
    future_provider_finding["evidence_reference"]["source"] = "future_system_tables_provider"

    current = build_recommendations([agent_finding])[0]
    future = build_recommendations([future_provider_finding])[0]

    assert current["recommendation_id"] == future["recommendation_id"]
    assert current["title"] == future["title"]
    assert current["proposed_state"] == future["proposed_state"]
    assert current["confidence"] == future["confidence"]


def test_recommendation_savings_and_current_cost_remain_unavailable(db_session, customer):
    env = Environment(
        id="no-cost-env",
        customer_id=customer.id,
        name="No cost data",
        platform="databricks",
        status="connected",
        workspace_name="workspace-1",
    )
    db_session.add(env)
    db_session.commit()

    item = persist_recommendations(
        db_session,
        [_finding(customer_id=customer.id, env_id=env.id)],
    )[0]
    record = db_session.query(Recommendation).filter_by(
        recommendation_id=item["recommendation_id"]
    ).one()

    assert item["estimated_savings"] == {
        "status": "NOT_AVAILABLE",
        "estimated": None,
        "measured": None,
    }
    assert record.estimated_monthly_savings is None
    assert record.current_monthly_cost is None


def test_multi_cluster_findings_build_independent_recommendations():
    cluster_b = _finding()
    cluster_b["resource_id"] = "cluster-2"
    cluster_b["resource"] = "batch"
    cluster_b["finding_id"] = "finding-cluster-2"
    cluster_b["evidence_reference"]["resource_id"] = "cluster-2"
    cluster_b["evidence_reference"]["lineage"]["resource_id"] = "cluster-2"
    cluster_b["evidence_reference"]["lineage"]["cluster_id"] = "cluster-2"
    cluster_b["evidence_reference"]["lineage"]["environment_id"] = "env-1"

    recommendations = build_recommendations([_finding(), cluster_b])

    assert {item["resource_id"] for item in recommendations} == {"cluster-1", "cluster-2"}


def test_run_linked_legacy_recommendation_model_remains_supported(db_session, customer):
    from models import AnalysisJob, Connection, JobRun

    connection = Connection(
        customer_id=customer.id,
        platform="file",
        workspace="File",
        endpoint="file://legacy",
        auth_method="none",
    )
    db_session.add(connection)
    db_session.flush()
    job = AnalysisJob(
        customer_id=customer.id,
        connection_id=connection.id,
        request="legacy recommendation test",
        intent="cluster",
        platform="file",
    )
    db_session.add(job)
    db_session.flush()
    run = JobRun(analysis_job_id=job.id, domain="cluster", platform="file")
    db_session.add(run)
    db_session.flush()
    legacy = Recommendation(
        job_run_id=run.id,
        resource="legacy-resource",
        domain="cluster",
        proposed_change="Existing run recommendation",
    )
    db_session.add(legacy)
    db_session.commit()

    assert db_session.query(Recommendation).filter_by(id=legacy.id).one().job_run_id == run.id


def test_persistence_repeated_finding_updates_one_open_record(db_session, customer):
    env = Environment(
        id="env-1",
        customer_id=customer.id,
        name="Demo",
        platform="databricks",
        status="connected",
        workspace_name="workspace-1",
    )
    db_session.add(env)
    db_session.commit()
    finding = _finding(customer_id=customer.id, env_id=env.id)

    first = persist_recommendations(db_session, [finding])
    second = persist_recommendations(db_session, [copy.deepcopy(finding)])

    assert len(first) == len(second) == 1
    assert first[0]["recommendation_id"] == second[0]["recommendation_id"]
    assert db_session.query(Recommendation).filter_by(customer_id=customer.id).count() == 1
    stored = db_session.query(Recommendation).filter_by(customer_id=customer.id).one()
    assert stored.job_run_id is None
    assert stored.status == "OPEN"
    assert stored.estimated_monthly_savings is None


def test_existing_optimizations_api_lists_and_retrieves_stage1_recommendations(db_session, customer):
    env = Environment(
        id="api-env",
        customer_id=customer.id,
        name="API environment",
        platform="databricks",
        status="connected",
        workspace_name="api-workspace",
    )
    db_session.add(env)
    db_session.commit()
    finding = _finding(customer_id=customer.id, env_id=env.id)
    finding["finding_id"] = "api-finding"
    recommendation = persist_recommendations(db_session, [finding])[0]

    def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            headers = {"X-Customer-Id": customer.id}
            listed = client.get("/api/optimizations?domain=stage1", headers=headers)
            detail = client.get(
                f"/api/optimizations/recommendations/{recommendation['recommendation_id']}",
                headers=headers,
            )
            assert listed.status_code == detail.status_code == 200
            assert listed.json()[0]["finding_id"] == "api-finding"
            assert detail.json()["details"]["rule_id"] == "STAGE1.TEST.OVERSIZED"
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_stage1_recommendation_api_is_customer_scoped(db_session, customer):
    other = Customer(id="stage1-api-other", name="Other customer")
    db_session.add(other)
    env = Environment(
        id="scoped-env",
        customer_id=customer.id,
        name="Scoped",
        platform="databricks",
        status="connected",
        workspace_name="scoped-workspace",
    )
    db_session.add(env)
    db_session.commit()
    finding = _finding(customer_id=customer.id, env_id=env.id)
    finding["finding_id"] = "scoped-finding"
    recommendation = persist_recommendations(db_session, [finding])[0]

    def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            response = client.get(
                f"/api/optimizations/recommendations/{recommendation['recommendation_id']}",
                headers={"X-Customer-Id": other.id},
            )
            assert response.status_code == 404
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_sqlite_schema_migration_preserves_legacy_recommendation_and_reference():
    migration_engine = create_engine("sqlite:///:memory:")
    with migration_engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE recommendations ("
            "id VARCHAR PRIMARY KEY, job_run_id VARCHAR NOT NULL, resource VARCHAR NOT NULL, "
            "domain VARCHAR NOT NULL, current_state TEXT, proposed_change TEXT NOT NULL, "
            "estimated_monthly_savings FLOAT, current_monthly_cost FLOAT, confidence VARCHAR, "
            "status VARCHAR, created_at DATETIME)"
        ))
        connection.execute(text(
            "CREATE TABLE approval_requests (id VARCHAR PRIMARY KEY, "
            "recommendation_id VARCHAR NOT NULL REFERENCES recommendations(id))"
        ))
        connection.execute(text(
            "INSERT INTO recommendations (id, job_run_id, resource, domain, proposed_change) "
            "VALUES ('legacy-rec', 'legacy-run', 'legacy-cluster', 'cluster', 'legacy action')"
        ))
        connection.execute(text(
            "INSERT INTO approval_requests (id, recommendation_id) VALUES ('legacy-approval', 'legacy-rec')"
        ))

    _ensure_recommendation_run_nullable(migration_engine)

    columns = {column["name"]: column for column in inspect(migration_engine).get_columns("recommendations")}
    with migration_engine.connect() as connection:
        legacy = connection.execute(text(
            "SELECT id, job_run_id, proposed_change FROM recommendations WHERE id='legacy-rec'"
        )).one()
        approval_reference = connection.execute(text(
            "SELECT recommendation_id FROM approval_requests WHERE id='legacy-approval'"
        )).scalar_one()
    assert columns["job_run_id"]["nullable"] is True
    assert tuple(legacy) == ("legacy-rec", "legacy-run", "legacy action")
    assert approval_reference == "legacy-rec"
    migration_engine.dispose()
