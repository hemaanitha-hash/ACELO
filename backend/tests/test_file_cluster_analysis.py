"""
Upload -> validate -> ACELO run -> existing Cluster optimizer -> persisted
results -> existing results API.
"""

import io
import json
import os
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from agent.orchestrator import UnsupportedFileRequest, plan_file_request
from database import get_db
from main import app
from models import Connection, JobRun
from optimizers import cluster_optimizer
from services import file_analysis_service as files

SAMPLE = Path(__file__).parent / "fixtures" / "cluster_sample.csv"
UPLOAD_URL = "/api/file-analysis/cluster"


@pytest.fixture
def client(db_session, tmp_path, monkeypatch):
    monkeypatch.setenv("ACELO_UPLOAD_DIR", str(tmp_path))
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def _upload(client, customer, content: bytes, name="clusters.csv", prompt=None):
    data = {"prompt": prompt} if prompt is not None else {}
    return client.post(
        UPLOAD_URL,
        files={"file": (name, io.BytesIO(content), "text/csv")},
        data=data,
        headers={"X-Customer-Id": customer.id},
    )


def _results(client, customer, db_session, job_id):
    db_session.expire_all()  # the worker wrote through its own session
    resp = client.get(f"/api/jobs/{job_id}/results", headers={"X-Customer-Id": customer.id})
    assert resp.status_code == 200
    return resp.json()


def _csv(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode()


def _sample_df() -> pd.DataFrame:
    return pd.read_csv(SAMPLE, dtype=str)


# --- upload success, execution, persistence, retrieval ------------------------

def test_csv_upload_runs_optimizer_and_persists_results(client, customer, db_session):
    resp = _upload(client, customer, SAMPLE.read_bytes())
    assert resp.status_code == 202, resp.text
    job = resp.json()
    assert job["platform"] == "file"
    assert job["intent"] == "cluster"
    assert [r["domain"] for r in job["job_runs"]] == ["cluster"]

    results = _results(client, customer, db_session, job["id"])
    assert len(results) == 1
    result = results[0]
    assert result["status"] == "COMPLETED"
    assert result["available"] is True

    payload = result["payload"]
    rows = payload["source_payload"]["rows"]
    assert payload["row_count"] == len(rows) == 12
    assert payload["source_file"] == "clusters.csv"
    # Every optimizer output column is present on every row.
    for column in (
        "optimization_label", "underutilized_label", "oversized_label", "idle_flag",
        "oversized_flag", "efficiency_score", "recommended_max_workers",
        "predicted_cost_usd", "predicted_savings_pct", "potential_monthly_savings",
        "llm_optimization",
    ):
        assert all(column in row for row in rows), column
    # Harmless extra columns are carried through, not dropped.
    assert payload["source_payload"]["extra_columns"] == ["owner_team"]
    assert rows[0]["owner_team"] == "analytics"

    # Persisted on the run itself, so reads never recompute.
    db_session.expire_all()
    run = db_session.query(JobRun).filter(JobRun.id == result["job_run_id"]).one()
    assert json.loads(run.result_json)["row_count"] == 12
    assert run.platform_run_id is None


def test_results_match_a_direct_optimizer_run_no_fake_metrics(client, customer, db_session):
    """Every figure returned is exactly what the optimizer computes from the file."""
    resp = _upload(client, customer, SAMPLE.read_bytes())
    payload = _results(client, customer, db_session, resp.json()["id"])[0]["payload"]

    frame = files.validate_cluster_csv(SAMPLE.read_bytes(), "x.csv").frame
    expected = cluster_optimizer.run_cluster_optimization(frame)

    rows = payload["source_payload"]["rows"]
    assert [r["cluster_id"] for r in rows] == list(expected["cluster_id"])
    assert [r["optimization_label"] for r in rows] == list(expected["optimization_label"])
    assert [r["recommended_max_workers"] for r in rows] == list(expected["recommended_max_workers"])
    assert [r["potential_monthly_savings"] for r in rows] == pytest.approx(
        list(expected["potential_monthly_savings"])
    )

    summary = payload["summary"]
    assert summary["total_clusters"] == len(expected)
    assert summary["critical"] == int((expected["optimization_label"] == "Risky").sum())
    assert summary["at_risk"] == int((expected["optimization_label"] == "Moderately Optimized").sum())
    assert summary["healthy"] == int((expected["optimization_label"] == "Optimized").sum())
    assert summary["idle_clusters"] == int(expected["idle_flag"].sum())
    assert summary["oversized_clusters"] == int(expected["oversized_flag"].sum())
    assert summary["avg_cpu_util"] == pytest.approx(expected["avg_cpu_util"].mean(), abs=0.01)
    assert summary["estimated_monthly_savings_usd"] == pytest.approx(
        expected["potential_monthly_savings"].sum(), abs=0.01
    )
    # No LLM configured -> honest marker on Risky rows, never an invented plan.
    risky = [r for r in rows if r["optimization_label"] == "Risky"]
    assert risky and all(r["llm_optimization"] == "LLM_UNAVAILABLE" for r in risky)


def test_different_input_gives_different_results(client, customer, db_session):
    """Guards against hardcoded output: changing the data changes the answer."""
    df = _sample_df()
    first = _results(client, customer, db_session, _upload(client, customer, _csv(df)).json()["id"])

    df["avg_cpu_util"] = "95"
    df["avg_memory_util"] = "95"
    second = _results(client, customer, db_session, _upload(client, customer, _csv(df)).json()["id"])

    s1, s2 = first[0]["payload"]["summary"], second[0]["payload"]["summary"]
    assert s1["avg_cpu_util"] != s2["avg_cpu_util"]
    assert s2["avg_cpu_util"] == 95.0
    assert s1["oversized_clusters"] > s2["oversized_clusters"]


def test_upload_is_deleted_after_the_run(client, customer, db_session, tmp_path):
    _upload(client, customer, SAMPLE.read_bytes())
    assert list(tmp_path.iterdir()) == []


def test_results_appear_in_job_list_and_logs(client, customer, db_session):
    job_id = _upload(client, customer, SAMPLE.read_bytes()).json()["id"]
    db_session.expire_all()
    jobs = client.get("/api/jobs", headers={"X-Customer-Id": customer.id}).json()
    listed = next(j for j in jobs if j["id"] == job_id)
    assert listed["status"] == "COMPLETED"

    logs = client.get(f"/api/jobs/{job_id}/logs", headers={"X-Customer-Id": customer.id}).json()
    assert any("Analysed 12 clusters" in log["message"] for log in logs)


def test_results_are_customer_scoped(client, customer, db_session):
    from models import Customer

    other = Customer(id="other-customer", name="Other")
    db_session.add(other)
    db_session.commit()
    job_id = _upload(client, customer, SAMPLE.read_bytes()).json()["id"]
    resp = client.get(f"/api/jobs/{job_id}/results", headers={"X-Customer-Id": other.id})
    assert resp.status_code == 404


# --- validation ---------------------------------------------------------------

def test_missing_required_column_is_named(client, customer):
    df = _sample_df().drop(columns=["avg_memory_util", "idle_time_min"])
    resp = _upload(client, customer, _csv(df))
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "MISSING_COLUMNS"
    assert detail["missing_columns"] == ["avg_memory_util", "idle_time_min"]
    assert "avg_memory_util" in detail["message"]


def test_invalid_numeric_value_reports_row_and_column(client, customer):
    df = _sample_df()
    df.loc[2, "avg_cpu_util"] = "high"
    df.loc[4, "total_dbus_cost_usd"] = "-5"
    resp = _upload(client, customer, _csv(df))
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "INVALID_VALUES"
    found = {(e["row"], e["column"], e["reason"]) for e in detail["invalid_values"]}
    assert (4, "avg_cpu_util", "is not a number") in found
    assert (6, "total_dbus_cost_usd", "must not be negative") in found


@pytest.mark.parametrize(
    "column,value,reason",
    [
        ("avg_memory_util", "150", "must be a percentage between 0 and 100"),
        ("max_workers", "2.5", "must be a whole number"),
        ("total_dbus_cost_usd", "", "is required"),
        ("cluster_id", "", "is required"),
        ("idle_time_min", "inf", "is not a number"),
    ],
)
def test_numeric_rules(client, customer, column, value, reason):
    df = _sample_df()
    df.loc[0, column] = value
    detail = _upload(client, customer, _csv(df)).json()["detail"]
    assert {"row": 2, "column": column, "value": value, "reason": reason} in detail["invalid_values"]


def test_blank_allowed_where_the_optimizer_defines_the_value(client, customer, db_session):
    df = _sample_df()
    df.loc[0, "avg_cpu_util"] = ""  # the notebook fills this with 0
    resp = _upload(client, customer, _csv(df))
    assert resp.status_code == 202
    assert _results(client, customer, db_session, resp.json()["id"])[0]["status"] == "COMPLETED"


def test_min_greater_than_max_rejected(client, customer):
    df = _sample_df()
    df.loc[0, "min_workers"] = "20"
    detail = _upload(client, customer, _csv(df)).json()["detail"]
    assert any(e["reason"] == "is greater than max_workers" for e in detail["invalid_values"])


@pytest.mark.parametrize(
    "content,code",
    [
        (b"", "EMPTY_FILE"),
        (b"   \n\n", "EMPTY_FILE"),
        (",".join(cluster_optimizer.REQUIRED_COLUMNS).encode() + b"\n", "EMPTY_FILE"),
    ],
)
def test_empty_file_rejected(client, customer, content, code):
    resp = _upload(client, customer, content)
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == code


def test_too_large_file_rejected(client, customer, monkeypatch):
    monkeypatch.setattr(files, "MAX_UPLOAD_BYTES", 1024)
    resp = _upload(client, customer, SAMPLE.read_bytes() * 10)
    assert resp.status_code == 413
    assert resp.json()["detail"]["code"] == "FILE_TOO_LARGE"


def test_too_many_rows_rejected(client, customer, monkeypatch):
    monkeypatch.setattr(files, "MAX_ROWS", 5)
    resp = _upload(client, customer, SAMPLE.read_bytes())
    assert resp.status_code == 413
    assert resp.json()["detail"]["code"] == "TOO_MANY_ROWS"


@pytest.mark.parametrize(
    "content,name,status,code",
    [
        (b"\x89PNG\r\n\x1a\n\x00\x00binary", "clusters.csv", 400, "MALFORMED_FILE"),
        (b"\xff\xfe\xfa not utf8", "clusters.csv", 400, "MALFORMED_FILE"),
        (b'a,b\n1,2,3,4\n"unterminated', "clusters.csv", 400, "MALFORMED_FILE"),
        (b"import os; os.system('x')", "payload.py", 415, "UNSUPPORTED_FILE_TYPE"),
        (SAMPLE.read_bytes(), "clusters.xlsx", 415, "UNSUPPORTED_FILE_TYPE"),
    ],
)
def test_invalid_files_rejected_safely(client, customer, content, name, status, code):
    resp = _upload(client, customer, content, name=name)
    assert resp.status_code == status
    body = resp.text
    assert resp.json()["detail"]["code"] == code
    # Never leaks server internals.
    for leak in ("Traceback", os.path.sep + "Users", "tmp", "SECRET", "\\\\"):
        assert leak not in body


def test_rejected_upload_creates_no_run(client, customer, db_session):
    _upload(client, customer, b"")
    assert db_session.query(JobRun).count() == 0


def test_path_in_filename_is_stripped(client, customer, db_session):
    resp = _upload(client, customer, SAMPLE.read_bytes(), name="../../etc/clusters.csv")
    payload = _results(client, customer, db_session, resp.json()["id"])[0]["payload"]
    assert payload["source_file"] == "clusters.csv"


def test_optimizer_crash_fails_run_without_leaking(client, customer, db_session):
    with patch.object(files, "run_cluster_optimization", side_effect=RuntimeError("C:\\secret\\path boom")):
        resp = _upload(client, customer, SAMPLE.read_bytes())
    assert resp.status_code == 202
    result = _results(client, customer, db_session, resp.json()["id"])[0]
    assert result["status"] == "FAILED"
    assert result["available"] is False
    assert result["error_code"] == "EXECUTION_FAILED"
    assert "secret" not in (result["error"] or "")


def test_file_runs_cannot_be_retried(client, customer, db_session):
    with patch.object(files, "run_cluster_optimization", side_effect=RuntimeError("x")):
        job_id = _upload(client, customer, SAMPLE.read_bytes()).json()["id"]
    db_session.expire_all()
    resp = client.post(f"/api/jobs/{job_id}/retry", headers={"X-Customer-Id": customer.id})
    assert resp.status_code == 400


def test_schema_endpoint(client):
    body = client.get(f"{UPLOAD_URL}/schema").json()
    assert body["required_columns"] == cluster_optimizer.REQUIRED_COLUMNS


# --- AI agent routing -----------------------------------------------------------

@pytest.mark.parametrize(
    "prompt,focus",
    [
        ("Analyze this cluster file", None),
        ("Check my cluster utilization", None),
        ("Find idle clusters", "idle"),
        ("Find oversized clusters", "oversized"),
        ("", None),
        (None, None),
        ("Analyze this file", None),
    ],
)
def test_agent_routes_file_requests_to_cluster(prompt, focus):
    text, detected = plan_file_request(prompt)
    assert detected == focus
    assert text


def test_agent_refuses_query_or_storage_file_requests():
    with pytest.raises(UnsupportedFileRequest):
        plan_file_request("Optimize my SQL queries")
    with pytest.raises(UnsupportedFileRequest):
        plan_file_request("Check storage tables")


def test_agent_prompt_with_file_uses_file_path_not_platform(client, customer, db_session, fabric_connection):
    resp = _upload(client, customer, SAMPLE.read_bytes(), prompt="Find idle clusters")
    assert resp.status_code == 202
    job = resp.json()
    assert job["platform"] == "file"
    assert job["connection_id"] != fabric_connection.id
    assert job["request"] == "Find idle clusters"
    payload = _results(client, customer, db_session, job["id"])[0]["payload"]
    assert payload["focus"] == "idle"


def test_agent_query_prompt_with_file_rejected(client, customer, db_session):
    resp = _upload(client, customer, SAMPLE.read_bytes(), prompt="Optimize my SQL queries")
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "UNSUPPORTED_DOMAIN"
    assert db_session.query(JobRun).count() == 0


# --- platform integrations unchanged ----------------------------------------------

def test_file_connection_hidden_from_platform_connections(client, customer, db_session, fabric_connection):
    _upload(client, customer, SAMPLE.read_bytes())
    db_session.expire_all()
    assert db_session.query(Connection).filter(Connection.platform == "file").count() == 1
    listed = client.get("/api/connections", headers={"X-Customer-Id": customer.id}).json()
    assert [c["platform"] for c in listed] == ["fabric"]


def test_fabric_adapter_registry_unchanged():
    from agent.router import _ADAPTERS
    from platforms.databricks import DatabricksAdapter
    from platforms.fabric import FabricAdapter

    assert _ADAPTERS == {"databricks": DatabricksAdapter, "fabric": FabricAdapter}
