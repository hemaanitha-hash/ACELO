"""
Background execution, live events, notifications and run history.

Covers: the backend (not a tab) following a delegated run to completion with
the user's in-memory tokens; real events only (no synthetic progress); one
notification per terminal run; multiple concurrent runs; history filters and
search; details with safe parameters; platform-confirmed cancel; re-run with a
new ACELO Run ID; OneLake result reads with no SQL endpoint; no secrets in logs.
"""

import base64
import json
import time
import uuid
from datetime import datetime, timedelta

import pytest

from models import AnalysisJob, Connection, Environment, JobLog, JobRun, Notification
from platforms.base import RunResult, RunStatusResult, StartAnalysisResult
from services import execution_worker, job_service, run_events, token_vault

FABRIC_RUN_ID = "13d76c9c-0e29-4265-abd2-5d2204b8221b"


def _jwt(exp: float) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{body}.SIGNATURE-SECRET"


FRESH = _jwt(time.time() + 3600)
EXPIRED = _jwt(time.time() - 10)


@pytest.fixture(autouse=True)
def _clean_vault():
    token_vault.clear()
    yield
    token_vault.clear()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from main import app

    return TestClient(app)


@pytest.fixture
def delegated(db_session, customer):
    connection = Connection(
        id="conn-bg", customer_id=customer.id, platform="fabric", workspace="acelo demo",
        endpoint="https://api.fabric.microsoft.com/v1", auth_method="delegated",
        auth_metadata=json.dumps({"workspace_id": "ws-1", "cluster_result_table": "cluster_results"}),
        secret_encrypted=None, status="connected",
    )
    environment = Environment(
        id="env-bg", customer_id=customer.id, connection_id=connection.id, name="Fabric Prod",
        platform="fabric", auth_mode="user", workspace_id="ws-1", status="environment_ready",
    )
    db_session.add_all([connection, environment])
    db_session.commit()
    return connection


def _run(db, customer, connection, *, status="RUNNING", platform_run_id=FABRIC_RUN_ID, domain="cluster",
         created_at=None, request="Analyze my clusters"):
    job = AnalysisJob(customer_id=customer.id, connection_id=connection.id, request=request,
                      intent=domain, platform="fabric", status=status)
    db.add(job)
    db.commit()
    run = JobRun(analysis_job_id=job.id, domain=domain, platform="fabric", status=status,
                 platform_run_id=platform_run_id, execution_type="pipeline", environment_id="env-bg",
                 started_at=datetime.utcnow(), created_at=created_at or datetime.utcnow())
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


class FakeAdapter:
    """Stands in for the Fabric adapter; records the tokens it was given."""

    def __init__(self, statuses, activities=None, rows=None):
        self.statuses = list(statuses)
        self.activities = activities or []
        self.rows = rows if rows is not None else [{"acelo_run_id": "x", "cluster": "c1"}]
        self.delegated_token = None
        self.onelake_token = None
        self.sql_token = None
        self.cancel_calls = 0
        self.seen_tokens = []

    async def get_run_status(self, platform_run_id, domain):
        self.seen_tokens.append(self.delegated_token)
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return RunStatusResult(status=status, detail={"fabric_status": status.title()})

    async def query_activity_runs(self, platform_run_id):
        return self.activities

    async def get_run_result(self, platform_run_id, domain, acelo_run_id=None):
        self.seen_tokens.append(("onelake", self.onelake_token))
        return RunResult(result_reference="OneLake dbo.cluster_results",
                         payload={"rows": self.rows, "row_count": len(self.rows), "source": "onelake"})

    async def cancel_run(self, platform_run_id, domain):
        self.cancel_calls += 1
        from platforms.base import ConnectionResult
        return ConnectionResult(ok=True, message="Cancellation requested.")

    async def start_analysis(self, domain, parameters):
        return StartAnalysisResult(platform_run_id=str(uuid.uuid4()), status="STARTING")


@pytest.fixture
def fake(monkeypatch):
    holder = {}

    def install(adapter):
        def build(connection, db=None, token=None):
            adapter.delegated_token = token
            return adapter
        monkeypatch.setattr(job_service, "build_adapter", build)
        holder["adapter"] = adapter
        return adapter

    async def no_sleep(_):
        return None

    monkeypatch.setattr(execution_worker.asyncio, "sleep", no_sleep)
    return install


def _events(db, run):
    db.expire_all()
    return [e.event_type for e in db.query(JobLog).filter(JobLog.job_run_id == run.id).order_by(JobLog.timestamp)]


# --- token vault ----------------------------------------------------------------------

def test_vault_is_memory_only_expiry_aware_and_discardable():
    token_vault.store("r1", FRESH, "ONELAKE")
    assert token_vault.has_usable_token("r1")
    assert token_vault.fabric_token("r1") == FRESH
    token_vault.store("r2", EXPIRED)
    assert not token_vault.has_usable_token("r2")
    assert token_vault.fabric_token("r2") is None
    token_vault.discard("r1")
    assert token_vault.fabric_token("r1") is None


def test_delegated_run_is_pollable_only_with_a_vault_token(db_session, customer, delegated):
    run = _run(db_session, customer, delegated)
    assert not execution_worker.can_poll_unattended(db_session, run)
    token_vault.store(run.id, FRESH)
    assert execution_worker.can_poll_unattended(db_session, run)


# --- the backend follows the run to completion -------------------------------------------

@pytest.mark.asyncio
async def test_worker_completes_delegated_run_without_any_browser(db_session, customer, delegated, fake):
    adapter = fake(FakeAdapter(
        ["RUNNING", "COMPLETED"],
        activities=[
            {"activity_name": "Run ACELO Cluster Notebook", "status": "Succeeded"},
            {"activity_name": "Update ACELO Approval Tracking", "status": "Succeeded"},
        ],
    ))
    run = _run(db_session, customer, delegated, status="STARTING")
    token_vault.store(run.id, FRESH, "ONELAKE-TOKEN")

    await execution_worker._watch(run.id)

    db_session.expire_all()
    run = db_session.get(JobRun, run.id)
    assert run.status == "COMPLETED"
    assert run_events.lifecycle_status(run) == "SUCCEEDED"
    assert run.result_json and json.loads(run.result_json)
    # The user's token from submission authenticated every poll; OneLake token read results.
    assert FRESH in adapter.seen_tokens
    assert ("onelake", "ONELAKE-TOKEN") in adapter.seen_tokens
    events = _events(db_session, run)
    for expected in ("PLATFORM_STATUS_CHANGED", "NOTEBOOK_STARTED", "NOTEBOOK_COMPLETED",
                     "APPROVAL_TRACKING_STARTED", "APPROVAL_TRACKING_COMPLETED",
                     "EXECUTION_COMPLETED", "RESULTS_RETRIEVED"):
        assert expected in events, expected
    # Activity events are not duplicated by repeated polls.
    assert events.count("NOTEBOOK_COMPLETED") == 1
    # Exactly one global notification, linking to the run.
    notes = db_session.query(Notification).filter(Notification.link == f"/runs/{run.id}").all()
    assert len(notes) == 1 and notes[0].type == "run_succeeded"
    assert notes[0].title == "Cluster Optimization completed"
    # Tokens are released once the run is terminal.
    assert token_vault.fabric_token(run.id) is None


@pytest.mark.asyncio
async def test_failed_run_emits_failure_and_notification(db_session, customer, delegated, fake):
    fake(FakeAdapter(["FAILED"]))
    run = _run(db_session, customer, delegated)
    token_vault.store(run.id, FRESH)
    await execution_worker._watch(run.id)
    db_session.expire_all()
    assert "EXECUTION_FAILED" in _events(db_session, run)
    note = db_session.query(Notification).filter(Notification.link == f"/runs/{run.id}").one()
    assert note.type == "run_failed"


@pytest.mark.asyncio
async def test_expired_token_pauses_monitoring_without_failing_the_run(db_session, customer, delegated, fake):
    adapter = fake(FakeAdapter(["COMPLETED"]))
    run = _run(db_session, customer, delegated)
    token_vault.store(run.id, EXPIRED)
    await execution_worker._watch(run.id)
    db_session.expire_all()
    run = db_session.get(JobRun, run.id)
    assert run.status == "RUNNING"  # last observed state; never invented
    assert adapter.seen_tokens == []
    assert "BACKGROUND_MONITORING_PAUSED" in _events(db_session, run)
    assert db_session.query(Notification).count() == 0


@pytest.mark.asyncio
async def test_no_secrets_in_events(db_session, customer, delegated, fake):
    fake(FakeAdapter(["COMPLETED"]))
    run = _run(db_session, customer, delegated)
    run_events.emit(db_session, run, "X", "hello", metadata={"access_token": FRESH, "bearer": "b",
                                                             "client_secret": "s", "result_table": "t"})
    token_vault.store(run.id, FRESH, "ONELAKE-TOKEN")
    await execution_worker._watch(run.id)
    db_session.expire_all()
    blob = " ".join(
        f"{e.message} {e.metadata_json}" for e in db_session.query(JobLog).filter(JobLog.job_run_id == run.id)
    )
    assert FRESH not in blob and "SIGNATURE-SECRET" not in blob and "ONELAKE-TOKEN" not in blob
    assert "client_secret" not in blob and "result_table" in blob


# --- API: submission, multiple runs, history, details --------------------------------------

def test_create_job_hands_run_to_backend_with_owner_and_environment(client, customer, delegated, fake, monkeypatch):
    fake(FakeAdapter(["RUNNING"]))
    watched = []
    monkeypatch.setattr(execution_worker, "watch_job_run", watched.append)
    monkeypatch.setattr(job_service, "missing_required_configuration", lambda *a: [])
    monkeypatch.setattr(job_service, "missing_required_parameters", lambda *a: [])
    resp = client.post(
        "/api/jobs", json={"connection_id": delegated.id, "prompt": "optimize my clusters"},
        headers={"X-Customer-Id": customer.id, "X-Fabric-Access-Token": FRESH,
                 "X-OneLake-Token": "ONELAKE", "X-Acelo-User-Name": "Hema"},
    )
    assert resp.status_code == 200, resp.text
    run_id = resp.json()["job_runs"][0]["id"]
    assert watched == [run_id]
    assert token_vault.fabric_token(run_id) == FRESH
    assert token_vault.onelake_token(run_id) == "ONELAKE"
    detail = client.get(f"/api/runs/{run_id}", headers={"X-Customer-Id": customer.id}).json()
    assert detail["created_by"] == "Hema"
    assert detail["environment_id"] == "env-bg"
    assert "RUN_CREATED" in [e["event_type"] for e in detail["timeline"]]


def test_multiple_active_runs_are_tracked_by_id(client, db_session, customer, delegated):
    a = _run(db_session, customer, delegated, status="RUNNING")
    b = _run(db_session, customer, delegated, status="STARTING", platform_run_id="other-run")
    _run(db_session, customer, delegated, status="COMPLETED")
    body = client.get("/api/runs/active", headers={"X-Customer-Id": customer.id}).json()
    assert body["count"] == 2
    assert {r["acelo_run_id"] for r in body["runs"]} == {a.id, b.id}
    statuses = {r["acelo_run_id"]: r["status"] for r in body["runs"]}
    assert statuses[b.id] == "SUBMITTED" and statuses[a.id] == "RUNNING"


def test_history_filters_and_search(client, db_session, customer, delegated):
    old = _run(db_session, customer, delegated, status="FAILED", platform_run_id="plat-old",
               created_at=datetime.utcnow() - timedelta(days=10))
    new = _run(db_session, customer, delegated, status="COMPLETED", platform_run_id=FABRIC_RUN_ID)
    q = _run(db_session, customer, delegated, status="COMPLETED", domain="query", platform_run_id="plat-q")
    h = {"X-Customer-Id": customer.id}

    def ids(**params):
        return {r["acelo_run_id"] for r in client.get("/api/runs", params=params, headers=h).json()["runs"]}

    assert ids() == {old.id, new.id, q.id}
    assert ids(status="SUCCEEDED") == {new.id, q.id}
    assert ids(status="FAILED") == {old.id}
    assert ids(domain="query") == {q.id}
    assert ids(platform="fabric") == {old.id, new.id, q.id}
    assert ids(since=(datetime.utcnow() - timedelta(days=1)).isoformat()) == {new.id, q.id}
    assert ids(search=FABRIC_RUN_ID[:8]) == {new.id}
    assert ids(search=old.id) == {old.id}
    assert client.get("/api/runs", params={"status": "BOGUS"}, headers=h).status_code == 422


def test_history_is_customer_scoped(client, db_session, customer, delegated):
    from models import Customer
    other = Customer(id="other", name="Other")
    db_session.add(other)
    db_session.commit()
    run = _run(db_session, customer, delegated, status="COMPLETED")
    assert client.get("/api/runs", headers={"X-Customer-Id": "other"}).json()["total"] == 0
    assert client.get(f"/api/runs/{run.id}", headers={"X-Customer-Id": "other"}).status_code == 404


def test_details_show_safe_parameters_timeline_and_result(client, db_session, customer, delegated):
    run = _run(db_session, customer, delegated, status="COMPLETED")
    run.result_json = json.dumps({"rows": [{"cluster": "c1"}], "row_count": 1})
    db_session.commit()
    job_service.append_log(
        db_session, run, "INFO",
        job_service.SENT_PARAMETERS_PREFIX + json.dumps(
            {"acelo_run_id": run.id, "result_table": "cluster_results", "fabric_token": "SHOULD-NOT-SHOW"}),
    )
    run_events.emit(db_session, run, "PLATFORM_RUN_ID_RECEIVED", "Pipeline submitted.", stage="submission")
    body = client.get(f"/api/runs/{run.id}", headers={"X-Customer-Id": customer.id}).json()
    assert body["platform_run_id"] == FABRIC_RUN_ID
    assert body["parameters"].get("result_table") == "cluster_results"
    assert "fabric_token" not in body["parameters"]
    assert [e["event_type"] for e in body["timeline"]] == ["PLATFORM_RUN_ID_RECEIVED"]
    assert body["result"]["row_count"] == 1
    assert body["can_rerun"] and not body["can_cancel"]


def test_events_endpoint_returns_only_newer_events(client, db_session, customer, delegated):
    run = _run(db_session, customer, delegated)
    first = run_events.emit(db_session, run, "EXECUTION_STARTED", "start")
    cutoff = datetime.utcnow()
    time.sleep(0.01)
    run_events.emit(db_session, run, "PLATFORM_STATUS_CHANGED", "Fabric status: InProgress.")
    body = client.get(f"/api/runs/{run.id}/events", params={"after": cutoff.isoformat()},
                      headers={"X-Customer-Id": customer.id}).json()
    assert [e["event_type"] for e in body["events"]] == ["PLATFORM_STATUS_CHANGED"]
    assert first is not None


# --- cancel / re-run -------------------------------------------------------------------------

def test_cancel_goes_through_platform_and_is_not_marked_cancelled_locally(
        client, db_session, customer, delegated, fake, monkeypatch):
    adapter = fake(FakeAdapter(["RUNNING"]))
    monkeypatch.setattr(execution_worker, "watch_job_run", lambda _id: None)
    run = _run(db_session, customer, delegated)
    body = client.post(f"/api/runs/{run.id}/cancel",
                       headers={"X-Customer-Id": customer.id, "X-Fabric-Access-Token": FRESH}).json()
    assert adapter.cancel_calls == 1
    assert body["status"] == "CANCEL_REQUESTED"
    assert "CANCEL_REQUESTED" in _events(db_session, run)
    assert db_session.query(Notification).count() == 0


def test_rerun_creates_new_run_linked_to_original(client, db_session, customer, delegated, fake, monkeypatch):
    fake(FakeAdapter(["RUNNING"]))
    monkeypatch.setattr(execution_worker, "watch_job_run", lambda _id: None)
    monkeypatch.setattr(job_service, "missing_required_configuration", lambda *a: [])
    monkeypatch.setattr(job_service, "missing_required_parameters", lambda *a: [])
    original = _run(db_session, customer, delegated, status="FAILED", request="optimize my clusters")
    h = {"X-Customer-Id": customer.id, "X-Fabric-Access-Token": FRESH}
    new = client.post(f"/api/runs/{original.id}/rerun", headers=h).json()
    assert new["acelo_run_id"] != original.id
    assert new["retry_of_run_id"] == original.id
    assert client.get(f"/api/runs/{original.id}", headers=h).json()["reruns"] == [new["acelo_run_id"]]
    active = _run(db_session, customer, delegated, status="RUNNING")
    assert client.post(f"/api/runs/{active.id}/rerun", headers=h).status_code == 409


# --- notifications ------------------------------------------------------------------------------

def test_notifications_survive_reload_and_can_be_marked_read(client, db_session, customer, delegated):
    run = _run(db_session, customer, delegated, status="COMPLETED")
    run_events.notify_terminal(db_session, run)
    run_events.notify_terminal(db_session, run)  # idempotent
    h = {"X-Customer-Id": customer.id}
    body = client.get("/api/notifications", headers=h).json()
    assert body["unread"] == 1
    note = body["notifications"][0]
    assert note["acelo_run_id"] == run.id and note["link"] == f"/runs/{run.id}"
    client.post(f"/api/notifications/{note['id']}/read", headers=h)
    assert client.get("/api/notifications", headers=h).json()["unread"] == 0


# --- OneLake results, no SQL endpoint -------------------------------------------------------------

@pytest.mark.asyncio
async def test_results_read_from_onelake_filtered_by_run_without_sql_endpoint():
    from platforms.fabric import FabricAdapter

    adapter = FabricAdapter(endpoint="https://api.fabric.microsoft.com/v1", auth_metadata={
        "workspace_id": "ws-1", "cluster_result_table": "dbo.cluster_results",
        "cluster_result_lakehouse_id": "lh-1",
    }, secret=None)
    calls = []

    async def read(lakehouse_id, table, schema=None, workspace_id=None, filters=None):
        calls.append((lakehouse_id, table, schema, filters))
        return [{"acelo_run_id": "run-9", "cluster": "c1"}]

    adapter.read_delta_table = read
    result = await adapter.get_run_result("plat", "cluster", acelo_run_id="run-9")
    assert calls == [("lh-1", "cluster_results", "dbo", [("acelo_run_id", "=", "run-9")])]
    assert result.payload["source"] == "onelake" and result.payload["row_count"] == 1
    assert "sql" not in result.result_reference.lower()
