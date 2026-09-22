"""
Runtime parameter trace for real Fabric Cluster executions.

Proves, at the Fabric HTTP boundary (respx), that:
  * ACELO logs exactly what it sends ([FABRIC_EXECUTION_REQUEST]) and what
    Fabric answered ([FABRIC_EXECUTION_RESPONSE] / [FABRIC_EXECUTION_FAILED]);
  * the platform run id is the one Fabric put in the Location header;
  * status transitions are logged ([FABRIC_RUN_STATUS]);
  * result rows are checked against the submitted parameters
    ([NOTEBOOK_PARAMETER_MATCH]);
  * no token, secret or credential pointer ever reaches a log line.
"""

import json
import logging
from datetime import datetime

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from database import get_db
from main import app
from models import AnalysisJob, Connection, Environment, JobLog, JobRun, Resource
from platforms.errors import ErrorCode, PlatformError
from platforms.fabric import FabricAdapter, redact_parameters
from services import job_service, provisioning_service

FABRIC_BASE = "https://api.fabric.microsoft.com/v1"
WORKSPACE_ID = "0a0a0a0a-1111-2222-3333-444444444444"
NOTEBOOK_ID = "5b5b5b5b-6666-7777-8888-999999999999"
FABRIC_RUN_ID = "f2d65699-dd22-4889-980c-15226deb0e1b"
USER_TOKEN = "eyJ0eXAiOiJKV1QiLCJ-SECRET-DELEGATED-TOKEN"
CLIENT_SECRET = "sp-client-secret-value-XYZ"
KEY_VAULT_URI = "https://acelo-kv.vault.azure.net/"
SECRET_NAME = "groq-api-key"


@pytest.fixture
def client(db_session):
    def _override():
        yield db_session

    app.dependency_overrides[get_db] = _override
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def fabric_env(db_session, customer):
    """A delegated Fabric environment whose Cluster notebook was registered by provisioning."""
    connection = Connection(
        id="conn-trace",
        customer_id=customer.id,
        platform="fabric",
        workspace="acelo demo",
        endpoint=FABRIC_BASE,
        auth_method="delegated",
        auth_metadata=json.dumps(
            {
                "workspace_id": WORKSPACE_ID,
                "cluster_source_table": "realistic_cluster_dataset",
                "cluster_result_table": "acelo_cluster_results",
                "lakehouse_database": "Data",
                "llm_key_vault_uri": KEY_VAULT_URI,
                "llm_secret_name": SECRET_NAME,
            }
        ),
        secret_encrypted=None,
        status="connected",
    )
    environment = Environment(
        id="env-trace",
        customer_id=customer.id,
        connection_id=connection.id,
        name="acelo demo",
        platform="fabric",
        auth_mode="user",
        workspace_id=WORKSPACE_ID,
        workspace_name="acelo demo",
        status="environment_ready",
        provisioning_status=provisioning_service.INSTALLED,
        last_verified_at=datetime.utcnow(),
        last_discovered_at=datetime.utcnow(),
    )
    db_session.add_all([connection, environment])
    db_session.add(
        Resource(
            environment_id=environment.id,
            platform="fabric",
            resource_type="Notebook",
            display_name="ACELO Cluster Optimization",
            platform_resource_id=NOTEBOOK_ID,
            status=provisioning_service.ACELO_OWNED,
            detail_json=json.dumps({"domain": "cluster", "managed_by": "acelo"}),
        )
    )
    db_session.commit()
    return connection, environment


def _start_url():
    return f"{FABRIC_BASE}/workspaces/{WORKSPACE_ID}/items/{NOTEBOOK_ID}/jobs/instances"


def _accept():
    return httpx.Response(
        202,
        headers={"Location": f"{_start_url()}/{FABRIC_RUN_ID}", "Retry-After": "60"},
    )


def _trace_lines(caplog, tag):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(f"[{tag}]")]


def _all_log_text(caplog, db_session) -> str:
    db_text = " ".join(l.message for l in db_session.query(JobLog).all())
    return " ".join(r.getMessage() for r in caplog.records) + db_text


def _run_agent(client, customer, connection, prompt="Check my cluster utilization"):
    return client.post(
        "/api/jobs",
        json={"connection_id": connection.id, "prompt": prompt},
        headers={"X-Customer-Id": customer.id, "X-Fabric-Access-Token": USER_TOKEN},
    )


# --- routing, notebook resolution, runtime parameters ------------------------------

@respx.mock
def test_agent_cluster_prompt_submits_resolved_notebook_with_runtime_parameters(
    client, customer, fabric_env, caplog
):
    connection, environment = fabric_env
    route = respx.post(_start_url()).mock(return_value=_accept())
    caplog.set_level(logging.INFO)

    resp = _run_agent(client, customer, connection)

    assert resp.status_code == 200
    runs = resp.json()["job_runs"]
    assert [r["domain"] for r in runs] == ["cluster"]  # routed to Cluster only
    run = runs[0]
    # The notebook id came from the Resource table, not from config or code.
    assert route.called
    assert NOTEBOOK_ID in str(route.calls[0].request.url)

    sent = json.loads(route.calls[0].request.content)["executionData"]["parameters"]
    assert sent["acelo_run_id"]["value"] == run["id"]
    assert sent["environment_id"]["value"] == environment.id
    assert sent["source_table"]["value"] == "realistic_cluster_dataset"
    assert sent["result_table"]["value"] == "acelo_cluster_results"
    assert sent["source_lakehouse"]["value"] == "Data"
    assert sent["result_lakehouse"]["value"] == "Data"


@respx.mock
def test_request_and_response_are_traced_with_real_run_id(client, customer, fabric_env, caplog):
    connection, environment = fabric_env
    respx.post(_start_url()).mock(return_value=_accept())
    caplog.set_level(logging.INFO)

    run = _run_agent(client, customer, connection).json()["job_runs"][0]

    [request] = _trace_lines(caplog, "FABRIC_EXECUTION_REQUEST")
    assert f"acelo_run_id={run['id']}" in request
    assert f"environment_id={environment.id}" in request
    assert f"workspace_id={WORKSPACE_ID}" in request
    assert f"notebook_id={NOTEBOOK_ID}" in request
    assert "platform=fabric" in request and "domain=cluster" in request
    params = json.loads(request.split("parameters=", 1)[1])
    assert params["source_table"] == "realistic_cluster_dataset"
    assert params["acelo_run_id"] == run["id"]

    [response] = _trace_lines(caplog, "FABRIC_EXECUTION_RESPONSE")
    assert "http_status=202" in response
    assert f"platform_run_id={FABRIC_RUN_ID}" in response
    assert "retry_after=60" in response

    # The run holds Fabric's id, not a locally generated one.
    assert run["platform_run_id"] == FABRIC_RUN_ID
    assert run["status"] == "STARTING"


@respx.mock
def test_refused_submission_is_traced_and_never_becomes_a_run_id(client, customer, fabric_env, caplog, db_session):
    connection, _ = fabric_env
    respx.post(_start_url()).mock(
        return_value=httpx.Response(400, json={"errorCode": "InvalidParameter", "message": "bad param"})
    )
    caplog.set_level(logging.INFO)

    run = _run_agent(client, customer, connection).json()["job_runs"][0]

    assert run["status"] == "FAILED"
    assert run["platform_run_id"] is None
    [failed] = _trace_lines(caplog, "FABRIC_EXECUTION_FAILED")
    assert "http_status=400" in failed
    assert "error_code=InvalidParameter" in failed
    assert "error_message=bad param" in failed
    assert not _trace_lines(caplog, "FABRIC_EXECUTION_RESPONSE")


def test_missing_cluster_configuration_fails_before_fabric_is_called(client, customer, fabric_env, db_session):
    connection, _ = fabric_env
    metadata = json.loads(connection.auth_metadata)
    metadata.pop("cluster_source_table")
    connection.auth_metadata = json.dumps(metadata)
    db_session.commit()

    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(_start_url()).mock(return_value=_accept())
        run = _run_agent(client, customer, connection).json()["job_runs"][0]

    assert not route.called
    assert run["status"] == "FAILED"
    assert run["error_code"] == ErrorCode.CLUSTER_SOURCE_TABLE_NOT_CONFIGURED


def test_unregistered_notebook_is_a_configuration_error(client, customer, fabric_env, db_session, caplog):
    connection, environment = fabric_env
    db_session.query(Resource).filter(Resource.environment_id == environment.id).delete()
    db_session.commit()
    caplog.set_level(logging.INFO)

    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(_start_url()).mock(return_value=_accept())
        run = _run_agent(client, customer, connection).json()["job_runs"][0]

    assert not route.called
    assert run["error_code"] == ErrorCode.NOTEBOOK_NOT_CONFIGURED
    [failed] = _trace_lines(caplog, "FABRIC_EXECUTION_FAILED")
    assert "error_code=NOTEBOOK_NOT_CONFIGURED" in failed


# --- status polling -----------------------------------------------------------------

@respx.mock
def test_status_transitions_are_traced(client, customer, fabric_env, caplog):
    connection, _ = fabric_env
    respx.post(_start_url()).mock(return_value=_accept())
    job = _run_agent(client, customer, connection).json()
    run_id = job["job_runs"][0]["id"]
    status = respx.get(f"{_start_url()}/{FABRIC_RUN_ID}")
    caplog.set_level(logging.INFO)

    headers = {"X-Customer-Id": customer.id, "X-Fabric-Access-Token": USER_TOKEN}
    status.mock(return_value=httpx.Response(200, json={"id": FABRIC_RUN_ID, "status": "InProgress"}))
    client.get(f"/api/jobs/{job['id']}", headers=headers)
    status.mock(
        return_value=httpx.Response(
            200, json={"id": FABRIC_RUN_ID, "status": "Failed", "failureReason": {"message": "boom"}}
        )
    )
    final = client.get(f"/api/jobs/{job['id']}", headers=headers).json()

    lines = _trace_lines(caplog, "FABRIC_RUN_STATUS")
    assert len(lines) == 2
    assert f"acelo_run_id={run_id}" in lines[0] and f"platform_run_id={FABRIC_RUN_ID}" in lines[0]
    assert "status=InProgress" in lines[0]
    assert "status=Failed" in lines[1]
    assert "error_code=EXECUTION_FAILED" in lines[1]
    assert "boom" in lines[1]
    assert final["job_runs"][0]["status"] == "FAILED"


# --- parameter verification (ACELO SENT vs NOTEBOOK RECEIVED) ------------------------

def _completed_run(db_session, connection, sent: dict | None, run_id="run-verify"):
    job = AnalysisJob(
        id=f"job-{run_id}", customer_id=connection.customer_id, connection_id=connection.id,
        request="Check my cluster utilization", intent="cluster", platform="fabric", status="COMPLETED",
    )
    run = JobRun(
        id=run_id, analysis_job_id=job.id, domain="cluster", platform="fabric",
        platform_run_id=FABRIC_RUN_ID, status="COMPLETED",
    )
    db_session.add_all([job, run])
    db_session.commit()
    if sent is not None:
        job_service.append_log(
            db_session, run, "INFO", job_service.SENT_PARAMETERS_PREFIX + json.dumps(sent)
        )
    db_session.refresh(run)
    return run


SENT = {
    "acelo_run_id": "run-verify",
    "environment_id": "env-trace",
    "source_table": "realistic_cluster_dataset",
    "result_table": "acelo_cluster_results",
    "llm_key_vault_uri": "<set>",
}


def test_parameters_matched(db_session, fabric_env, caplog):
    connection, _ = fabric_env
    run = _completed_run(db_session, connection, SENT)
    received = {k: v for k, v in SENT.items() if not k.startswith("llm_")}
    caplog.set_level(logging.INFO)

    outcome = job_service.verify_notebook_parameters(
        run, {"rows": [{"acelo_run_id": "run-verify", "acelo_received_parameters": json.dumps(received)}]}
    )

    assert outcome["status"] == "MATCHED"
    assert outcome["correlation_id_matched"] is True
    assert "status=MATCHED" in _trace_lines(caplog, "NOTEBOOK_PARAMETER_MATCH")[0]


def test_parameter_mismatch_is_reported(db_session, fabric_env):
    connection, _ = fabric_env
    run = _completed_run(db_session, connection, SENT)
    received = dict(SENT, source_table="some_other_table")

    outcome = job_service.verify_notebook_parameters(
        run, {"rows": [{"acelo_run_id": "run-verify", "acelo_received_parameters": json.dumps(received)}]}
    )

    assert outcome["status"] == "MISMATCH"
    assert outcome["mismatches"] == [
        {"parameter": "source_table", "sent": "realistic_cluster_dataset", "received": "some_other_table"}
    ]


@pytest.mark.parametrize(
    "sent,rows,reason",
    [
        (SENT, [], "No result rows"),
        (None, [{"acelo_run_id": "run-verify"}], "No record of the submitted parameters"),
        (SENT, [{"acelo_run_id": "run-verify"}], "predates runtime-parameter tracing"),
    ],
)
def test_unverifiable_is_never_reported_as_matched(db_session, fabric_env, sent, rows, reason):
    connection, _ = fabric_env
    run = _completed_run(db_session, connection, sent)
    outcome = job_service.verify_notebook_parameters(run, {"rows": rows})
    assert outcome["status"] == "UNVERIFIED"
    assert reason in outcome["reason"]


# --- result retrieval in delegated mode -----------------------------------------------

SQL_TOKEN = "eyJ-SQL-AUDIENCE-DELEGATED-TOKEN"


class _FakeCursor:
    description = [("cluster_id",), ("acelo_run_id",)]

    def __init__(self, rows):
        self._rows = rows
        self.executed = []

    def execute(self, sql, *params):
        self.executed.append((sql, params))

    def fetchall(self):
        return self._rows


def _fake_pyodbc(monkeypatch, drivers=("ODBC Driver 18 for SQL Server",), rows=(("c-1", "run-1"),)):
    import sys
    import types

    calls = {}
    cursor = _FakeCursor(list(rows))

    class _Conn:
        def cursor(self):
            return cursor

        def close(self):
            pass

    def connect(conn_str, **kwargs):
        calls["conn_str"] = conn_str
        calls["kwargs"] = kwargs
        return _Conn()

    module = types.SimpleNamespace(connect=connect, drivers=lambda: list(drivers))
    monkeypatch.setitem(sys.modules, "pyodbc", module)
    return calls, cursor


def _delegated_result_adapter(sql_token=None):
    adapter = FabricAdapter(
        endpoint=FABRIC_BASE,
        auth_metadata={"workspace_id": WORKSPACE_ID, "sql_endpoint": "x.datawarehouse.fabric.microsoft.com",
                       "lakehouse_database": "Data", "cluster_result_table": "acelo_cluster_recommendations"},
        secret=None,
    )
    adapter.delegated_mode = True
    adapter.delegated_token = USER_TOKEN
    adapter.sql_token = sql_token
    return adapter


@pytest.mark.asyncio
async def test_delegated_result_read_uses_the_users_sql_token_not_a_service_principal(monkeypatch):
    import struct

    calls, cursor = _fake_pyodbc(monkeypatch)
    result = await _delegated_result_adapter(SQL_TOKEN).get_run_result(
        FABRIC_RUN_ID, "cluster", acelo_run_id="run-1"
    )

    # Entra access token handed to msodbcsql (SQL_COPT_SS_ACCESS_TOKEN = 1256).
    encoded = SQL_TOKEN.encode("utf-16-le")
    assert calls["kwargs"]["attrs_before"][1256] == struct.pack(f"<I{len(encoded)}s", len(encoded), encoded)
    # No service-principal login of any kind.
    for forbidden in ("Authentication=", "UID=", "PWD="):
        assert forbidden not in calls["conn_str"]
    assert "Driver={ODBC Driver 18 for SQL Server}" in calls["conn_str"]
    assert "Database=Data" in calls["conn_str"]
    # Scoped to this run, parameterised.
    assert cursor.executed == [
        ("SELECT * FROM acelo_cluster_recommendations WHERE acelo_run_id = ?", ("run-1",))
    ]
    assert result.payload["rows"] == [{"cluster_id": "c-1", "acelo_run_id": "run-1"}]


@pytest.mark.asyncio
async def test_delegated_result_read_without_sql_token_is_refused_not_faked(monkeypatch):
    calls, _ = _fake_pyodbc(monkeypatch)
    with pytest.raises(PlatformError) as exc:
        await _delegated_result_adapter(None).get_run_result(FABRIC_RUN_ID, "cluster", acelo_run_id="run-1")
    assert exc.value.code == ErrorCode.RESULT_RETRIEVAL_FAILED
    assert "Sign in again" in exc.value.message
    assert "user_impersonation" in exc.value.message
    assert calls == {}  # never even attempted a connection


@pytest.mark.asyncio
async def test_missing_odbc_driver_18_is_an_explicit_host_error(monkeypatch):
    calls, _ = _fake_pyodbc(monkeypatch, drivers=("SQL Server",))
    with pytest.raises(PlatformError) as exc:
        await _delegated_result_adapter(SQL_TOKEN).get_run_result(FABRIC_RUN_ID, "cluster", acelo_run_id="run-1")
    assert "ODBC Driver 18" in exc.value.message
    assert calls == {}


@pytest.mark.asyncio
async def test_missing_sql_endpoint_is_a_typed_configuration_error(monkeypatch):
    _fake_pyodbc(monkeypatch)
    adapter = _delegated_result_adapter(SQL_TOKEN)
    adapter.auth_metadata.pop("sql_endpoint")
    with pytest.raises(PlatformError) as exc:
        await adapter.get_run_result(FABRIC_RUN_ID, "cluster", acelo_run_id="run-1")
    assert exc.value.code == ErrorCode.RESULT_RETRIEVAL_FAILED
    assert "Cluster Settings" in exc.value.message


@pytest.mark.asyncio
async def test_service_principal_result_read_is_unchanged(monkeypatch):
    calls, _ = _fake_pyodbc(monkeypatch)
    adapter = FabricAdapter(
        endpoint=FABRIC_BASE,
        auth_metadata={"tenant_id": "t", "client_id": "client-1", "workspace_id": WORKSPACE_ID,
                       "sql_endpoint": "x.datawarehouse.fabric.microsoft.com", "lakehouse_database": "Data"},
        secret="sp-secret",
    )
    await adapter.get_run_result(FABRIC_RUN_ID, "cluster", acelo_run_id="run-1")
    assert "Authentication=ActiveDirectoryServicePrincipal;" in calls["conn_str"]
    assert "UID=client-1;" in calls["conn_str"]
    assert "attrs_before" not in calls["kwargs"]


def test_results_api_passes_the_sql_token_and_never_logs_it(client, customer, fabric_env, db_session, caplog, monkeypatch):
    connection, _ = fabric_env
    metadata = json.loads(connection.auth_metadata)
    metadata.update(sql_endpoint="x.datawarehouse.fabric.microsoft.com")
    connection.auth_metadata = json.dumps(metadata)
    job = AnalysisJob(id="job-res", customer_id=customer.id, connection_id=connection.id,
                      request="x", intent="cluster", platform="fabric", status="COMPLETED")
    db_session.add_all([job, JobRun(id="run-1", analysis_job_id=job.id, domain="cluster", platform="fabric",
                                    platform_run_id=FABRIC_RUN_ID, status="COMPLETED")])
    db_session.commit()
    calls, _ = _fake_pyodbc(monkeypatch)
    caplog.set_level(logging.DEBUG)

    resp = client.get(
        "/api/jobs/job-res/results",
        headers={"X-Customer-Id": customer.id, "X-Fabric-Access-Token": USER_TOKEN,
                 "X-Fabric-Sql-Token": SQL_TOKEN},
    )

    [result] = resp.json()
    assert result["available"] is True
    assert result["payload"]["row_count"] == 1
    assert 1256 in calls["kwargs"]["attrs_before"]
    assert SQL_TOKEN not in _all_log_text(caplog, db_session)
    assert SQL_TOKEN not in resp.text
    assert "[FABRIC_RESULT_AUTH] auth_mode=delegated sql_token_present=True" in caplog.text


# --- no secret leakage ---------------------------------------------------------------------

def test_redaction_of_credential_pointers():
    assert redact_parameters({"llm_key_vault_uri": KEY_VAULT_URI, "llm_secret_name": SECRET_NAME, "a": 1}) == {
        "a": "1", "llm_key_vault_uri": "<set>", "llm_secret_name": "<set>",
    }


@respx.mock
def test_no_token_secret_or_credential_pointer_in_any_log(client, customer, fabric_env, caplog, db_session):
    connection, _ = fabric_env
    respx.post(_start_url()).mock(return_value=_accept())
    respx.get(f"{_start_url()}/{FABRIC_RUN_ID}").mock(
        return_value=httpx.Response(200, json={"id": FABRIC_RUN_ID, "status": "InProgress"})
    )
    caplog.set_level(logging.DEBUG)

    job = _run_agent(client, customer, connection).json()
    client.get(
        f"/api/jobs/{job['id']}",
        headers={"X-Customer-Id": customer.id, "X-Fabric-Access-Token": USER_TOKEN},
    )

    text = _all_log_text(caplog, db_session)
    assert "[FABRIC_EXECUTION_REQUEST]" in text  # the trace did run
    for secret in (USER_TOKEN, "Bearer", CLIENT_SECRET, KEY_VAULT_URI, SECRET_NAME):
        assert secret not in text


@respx.mock
def test_service_principal_secret_never_logged(db_session, customer, caplog, monkeypatch):
    from services.crypto import get_cipher

    connection = Connection(
        id="conn-sp", customer_id=customer.id, platform="fabric", workspace="sp",
        endpoint=FABRIC_BASE, auth_method="service_principal",
        auth_metadata=json.dumps({
            "tenant_id": "t", "client_id": "c", "workspace_id": WORKSPACE_ID,
            "cluster_notebook_id": NOTEBOOK_ID,
            "cluster_source_table": "src", "cluster_result_table": "dst",
        }),
        secret_encrypted=get_cipher().encrypt(CLIENT_SECRET), status="connected",
    )
    db_session.add(connection)
    db_session.commit()
    monkeypatch.setattr(FabricAdapter, "_acquire_token", lambda self: ("sp-access-token-ABC", None))
    respx.post(_start_url()).mock(return_value=_accept())
    caplog.set_level(logging.DEBUG)

    import asyncio
    job = job_service.create_analysis_job(db_session, customer.id, connection, "x", "cluster")
    run = job_service.create_job_run(db_session, job, "cluster")
    asyncio.run(job_service.start_job_run(db_session, connection, run))

    assert run.platform_run_id == FABRIC_RUN_ID
    text = _all_log_text(caplog, db_session)
    assert CLIENT_SECRET not in text
    assert "sp-access-token-ABC" not in text


# --- cluster settings (the configuration surface the UI needs) ------------------------------

def test_cluster_settings_round_trip_and_readiness(client, customer, fabric_env, db_session):
    connection, environment = fabric_env
    headers = {"X-Customer-Id": customer.id}
    url = f"/api/environments/{environment.id}/cluster-settings"

    cleared = client.put(url, json={"source_table": "", "result_table": ""}, headers=headers).json()
    assert cleared["missing"] == ["source_table", "result_table"]
    readiness = client.get(f"/api/environments/{environment.id}/readiness", headers=headers).json()
    assert readiness["cluster_blocked_reason"] == "Missing Cluster settings: source_table, result_table."

    saved = client.put(
        url,
        json={"source_table": "realistic_cluster_dataset", "result_table": "acelo_results",
              "sql_endpoint": "abc.datawarehouse.fabric.microsoft.com"},
        headers=headers,
    ).json()
    assert saved["missing"] == []
    assert saved["settings"]["source_table"] == "realistic_cluster_dataset"

    db_session.refresh(connection)
    params = job_service.build_run_parameters(connection, JobRun(id="r1", domain="cluster"), db_session)
    assert params["source_table"] == "realistic_cluster_dataset"
    assert params["result_table"] == "acelo_results"


@pytest.mark.parametrize(
    "payload",
    [
        {"source_table": "x; DROP TABLE y"},
        {"sql_endpoint": "https://evil/path"},
        {"column_mapping": "not json"},
        {"client_secret": "nope"},
    ],
)
def test_cluster_settings_reject_bad_values(client, customer, fabric_env, payload):
    _, environment = fabric_env
    resp = client.put(
        f"/api/environments/{environment.id}/cluster-settings",
        json=payload, headers={"X-Customer-Id": customer.id},
    )
    assert resp.status_code == 422


def test_cluster_settings_are_customer_scoped(client, fabric_env, db_session):
    from models import Customer

    other = Customer(id="someone-else", name="Other")
    db_session.add(other)
    db_session.commit()
    _, environment = fabric_env
    resp = client.get(
        f"/api/environments/{environment.id}/cluster-settings", headers={"X-Customer-Id": other.id}
    )
    assert resp.status_code == 404


def test_outdated_package_reports_update_available(client, customer, fabric_env, db_session):
    _, environment = fabric_env
    environment.package_version = "1.0.0"
    db_session.commit()
    state = client.get(
        f"/api/environments/{environment.id}/provision/status", headers={"X-Customer-Id": customer.id}
    ).json()
    assert state["status"] == "UPDATE_AVAILABLE"


# --- default lakehouse binding at deploy time ----------------------------------------

def _lakehouse(db_session, environment, name, item_id):
    db_session.add(
        Resource(
            environment_id=environment.id, platform="fabric", resource_type="Lakehouse",
            display_name=name, platform_resource_id=item_id, status="discovered",
        )
    )
    db_session.commit()


def test_default_lakehouse_resolved_from_discovery_by_configured_name(db_session, fabric_env):
    _, environment = fabric_env
    _lakehouse(db_session, environment, "Other", "lh-other")
    _lakehouse(db_session, environment, "Data", "lh-data")

    assert provisioning_service.default_lakehouse(db_session, environment) == {
        "id": "lh-data", "name": "Data", "workspace_id": WORKSPACE_ID, "source": "discovered",
    }


def test_ambiguous_or_missing_lakehouse_is_not_guessed(db_session, fabric_env):
    _, environment = fabric_env
    assert provisioning_service.default_lakehouse(db_session, environment) is None
    _lakehouse(db_session, environment, "A", "lh-a")
    _lakehouse(db_session, environment, "B", "lh-b")
    assert provisioning_service.default_lakehouse(db_session, environment) is None


def test_deployed_notebook_payload_carries_the_lakehouse(db_session, fabric_env):
    import base64
    from services import package_registry

    _, environment = fabric_env
    _lakehouse(db_session, environment, "Data", "lh-data")
    asset = package_registry.load_package().asset_for("cluster")

    payload = provisioning_service.notebook_payload(
        asset, provisioning_service.default_lakehouse(db_session, environment)
    )
    notebook = json.loads(base64.b64decode(payload))
    binding = notebook["metadata"]["dependencies"]["lakehouse"]
    assert binding == {
        "default_lakehouse": "lh-data",
        "default_lakehouse_name": "Data",
        "default_lakehouse_workspace_id": WORKSPACE_ID,
        "known_lakehouses": [{"id": "lh-data"}],
    }
    # Only metadata changes: the notebook code is deployed exactly as packaged.
    original = json.loads(asset.read_bytes())
    assert notebook["cells"] == original["cells"]
    # And with no lakehouse, the packaged bytes go out untouched.
    assert provisioning_service.notebook_payload(asset, None) == asset.payload_base64()


# --- delegated authentication: never fall back to service-principal ------------------

@pytest.fixture
def no_client_credentials(monkeypatch):
    """Fails the test if anything attempts the service-principal (client_id) flow."""
    import msal

    calls = []

    def _forbidden(*args, **kwargs):
        calls.append(kwargs)
        raise AssertionError("service-principal auth attempted for a delegated environment")

    monkeypatch.setattr(msal, "ConfidentialClientApplication", _forbidden)
    return calls


def _run_without_token(client, customer, connection):
    return client.post(
        "/api/jobs",
        json={"connection_id": connection.id, "prompt": "Check my cluster utilization"},
        headers={"X-Customer-Id": customer.id},  # no X-Fabric-Access-Token
    )


@respx.mock
def test_A_delegated_with_token_submits_to_fabric(client, customer, fabric_env, caplog, no_client_credentials):
    connection, _ = fabric_env
    route = respx.post(_start_url()).mock(return_value=_accept())
    caplog.set_level(logging.INFO)

    run = _run_agent(client, customer, connection).json()["job_runs"][0]

    assert route.called
    assert route.calls[0].request.headers["Authorization"] == f"Bearer {USER_TOKEN}"
    assert run["platform_run_id"] == FABRIC_RUN_ID
    assert _trace_lines(caplog, "FABRIC_AUTH") == ["[FABRIC_AUTH] auth_mode=delegated token_present=True"]
    assert no_client_credentials == []


def test_B_delegated_without_token_fails_before_fabric(client, customer, fabric_env, caplog, no_client_credentials, db_session):
    connection, _ = fabric_env
    caplog.set_level(logging.INFO)

    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(_start_url()).mock(return_value=_accept())
        run = _run_without_token(client, customer, connection).json()["job_runs"][0]

    assert not route.called  # Fabric API never called
    assert run["status"] == "FAILED"
    assert run["platform_run_id"] is None  # no platform run created
    assert run["error_code"] == ErrorCode.AUTHENTICATION_FAILED
    assert run["error"] == "Your Microsoft Fabric session has expired. Please sign in again."
    assert _trace_lines(caplog, "FABRIC_AUTH") == ["[FABRIC_AUTH] auth_mode=delegated token_present=False"]
    assert not _trace_lines(caplog, "FABRIC_EXECUTION_REQUEST")
    assert no_client_credentials == []
    # The old fallback's symptom must be gone.
    assert "client_id" not in _all_log_text(caplog, db_session)


@respx.mock
def test_C_delegated_without_client_id_still_submits_with_token(client, customer, fabric_env, no_client_credentials):
    connection, _ = fabric_env
    assert "client_id" not in json.loads(connection.auth_metadata)
    route = respx.post(_start_url()).mock(return_value=_accept())

    run = _run_agent(client, customer, connection).json()["job_runs"][0]

    assert route.called
    assert run["status"] == "STARTING"
    assert run["platform_run_id"] == FABRIC_RUN_ID


def test_D_delegated_without_client_id_or_token_fails_cleanly(client, customer, fabric_env, no_client_credentials):
    connection, _ = fabric_env
    assert "client_id" not in json.loads(connection.auth_metadata)

    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(_start_url()).mock(return_value=_accept())
        resp = _run_without_token(client, customer, connection)

    assert resp.status_code == 200  # a clean, typed failure - not a 500
    run = resp.json()["job_runs"][0]
    assert not route.called
    assert run["error_code"] == ErrorCode.AUTHENTICATION_FAILED
    assert "sign in again" in run["error"]
    assert "client_id" not in run["error"]


def test_delegated_poll_without_token_does_not_use_client_credentials(client, customer, fabric_env, db_session, no_client_credentials):
    connection, _ = fabric_env
    job = AnalysisJob(id="job-poll", customer_id=customer.id, connection_id=connection.id,
                      request="x", intent="cluster", platform="fabric", status="RUNNING")
    db_session.add_all([job, JobRun(id="run-poll", analysis_job_id=job.id, domain="cluster",
                                    platform="fabric", platform_run_id=FABRIC_RUN_ID, status="RUNNING")])
    db_session.commit()

    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(f"{_start_url()}/{FABRIC_RUN_ID}")
        client.get("/api/jobs/job-poll", headers={"X-Customer-Id": customer.id})

    assert not route.called
    assert no_client_credentials == []
    # A missing session is not a platform verdict: the live run is left as-is.
    db_session.expire_all()
    assert db_session.get(JobRun, "run-poll").status == "RUNNING"


def test_service_principal_still_uses_client_credentials(monkeypatch):
    """Explicit service-principal environments keep their existing flow."""
    import msal

    seen = {}

    class FakeApp:
        def __init__(self, client_id, client_credential, authority):
            seen.update(client_id=client_id, authority=authority)

        def acquire_token_for_client(self, scopes):
            return {"access_token": "sp-token"}

    monkeypatch.setattr(msal, "ConfidentialClientApplication", FakeApp)
    adapter = FabricAdapter(
        endpoint=FABRIC_BASE,
        auth_metadata={"tenant_id": "tenant-1", "client_id": "client-1", "workspace_id": WORKSPACE_ID},
        secret="s",
    )
    assert adapter.delegated_mode is False
    assert adapter._acquire_token() == ("sp-token", None)
    assert seen["client_id"] == "client-1"


def test_adapters_are_marked_delegated_from_stored_configuration(db_session, fabric_env):
    from agent.router import build_adapter
    from services import environment_service

    connection, environment = fabric_env
    assert build_adapter(connection, db_session).delegated_mode is True
    assert environment_service.build_adapter(db_session, environment).delegated_mode is True
