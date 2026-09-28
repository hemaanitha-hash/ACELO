"""
Databricks App identity.

Running as a native Databricks App, ACELO authenticates with the App's own
identity against the workspace it is deployed in. The user enters no workspace
URL, no access token and no platform.

What these tests protect:
  * a stored PAT still wins, so every existing configured connection is unchanged
  * with no stored secret, the App identity supplies the Authorization header
  * the workspace host comes from the runtime when none is configured
  * discovery and the agent capability keep working through that identity
  * no credential is stored, returned to the browser, or logged
  * outside an App nothing changes — the missing-credential error still fires
"""

import uuid

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from main import app
from models import Connection, Environment
from platforms import databricks_auth
from platforms.databricks import DatabricksAdapter
from platforms.databricks_resources import ResourceType
from platforms.errors import ErrorCode, PlatformError
from services.app_bootstrap import APP_AUTH_METHOD, ensure_app_environment


HOST = "https://adb-7405617514546966.6.azuredatabricks.net"

APP_TOKEN = "app-identity-oauth-token"
STORED_PAT = "dapi-stored-personal-token"

CLUSTERS_URL = f"{HOST}/api/2.0/clusters/list"
WAREHOUSES_URL = f"{HOST}/api/2.0/sql/warehouses"
SQL_STATEMENTS_URL = f"{HOST}/api/2.0/sql/statements"


RAW_WAREHOUSES = [
    {
        "id": "b1c2",
        "name": "Serverless Starter Warehouse",
        "state": "RUNNING",
        "warehouse_type": "PRO",
    },
    {
        "id": "a9b8",
        "name": "warehouse_db",
        "state": "STOPPED",
        "warehouse_type": "CLASSIC",
    },
]


@pytest.fixture(autouse=True)
def _clean_auth_cache():
    databricks_auth.reset_cache()

    yield

    databricks_auth.reset_cache()


@pytest.fixture
def app_runtime(monkeypatch):
    """ACELO running inside a Databricks App, with a working App identity."""

    monkeypatch.setenv("DATABRICKS_HOST", HOST)
    monkeypatch.setenv("DATABRICKS_APP_NAME", "acelo")

    monkeypatch.setattr(
        databricks_auth,
        "app_auth_headers",
        lambda: {
            "Authorization": f"Bearer {APP_TOKEN}",
        },
    )


def _mock_workspace() -> None:
    respx.get(CLUSTERS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "clusters": [],
            },
        )
    )

    respx.get(WAREHOUSES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "warehouses": RAW_WAREHOUSES,
            },
        )
    )


def _sql_response(columns, rows):
    """
    Build the Databricks SQL Statement API response expected by
    DatabricksSQLReader.
    """

    return httpx.Response(
        200,
        json={
            "status": {
                "state": "SUCCEEDED",
            },
            "manifest": {
                "schema": {
                    "columns": [
                        {
                            "name": column_name,
                            "type_name": "STRING",
                            "type_text": "STRING",
                        }
                        for column_name in columns
                    ]
                }
            },
            "result": {
                "data_array": rows,
            },
        },
    )


def _mock_compute_evidence() -> None:
    """
    Mock all six SELECT statements executed by DatabricksSQLReader.

    The order matches read_compute_evidence():
      1. cluster
      2. node_timeline
      3. node_types
      4. instance_events
      5. billing_usage
      6. job_task_run_timeline
    """

    responses = [
        # ------------------------------------------------------------------
        # 1. cluster
        # ------------------------------------------------------------------
        _sql_response(
            [
                "workspace_id",
                "cluster_id",
                "cluster_name",
                "cluster_source",
                "change_time",
                "delete_time",
                "worker_node_type",
                "driver_node_type",
                "worker_count",
                "min_autoscale_workers",
                "max_autoscale_workers",
                "auto_termination_minutes",
                "policy_id",
                "dbr_version",
            ],
            [
                [
                    "workspace-demo",
                    "cluster-demo-001",
                    "ACELO Demo Cluster",
                    "UI",
                    "2026-09-25T08:00:00Z",
                    None,
                    "m5.2xlarge",
                    "m5.2xlarge",
                    2,
                    2,
                    8,
                    120,
                    "policy-demo",
                    "15.4.x-scala2.12",
                ]
            ],
        ),

        # ------------------------------------------------------------------
        # 2. node_timeline
        # ------------------------------------------------------------------
        _sql_response(
            [
                "workspace_id",
                "cluster_id",
                "instance_id",
                "start_time",
                "end_time",
                "driver",
                "cpu_user_percent",
                "cpu_system_percent",
                "cpu_wait_percent",
                "mem_used_percent",
                "mem_swap_percent",
                "network_receive_bytes",
                "network_transmit_bytes",
            ],
            [
                [
                    "workspace-demo",
                    "cluster-demo-001",
                    "instance-driver-001",
                    "2026-09-25T08:00:00Z",
                    "2026-09-25T09:00:00Z",
                    True,
                    10.0,
                    2.0,
                    1.0,
                    25.0,
                    0.0,
                    1000000,
                    2000000,
                ],
                [
                    "workspace-demo",
                    "cluster-demo-001",
                    "instance-worker-001",
                    "2026-09-25T08:00:00Z",
                    "2026-09-25T09:00:00Z",
                    False,
                    11.0,
                    2.0,
                    1.0,
                    27.0,
                    0.0,
                    1000000,
                    2000000,
                ],
                [
                    "workspace-demo",
                    "cluster-demo-001",
                    "instance-worker-002",
                    "2026-09-25T08:00:00Z",
                    "2026-09-25T09:00:00Z",
                    False,
                    10.0,
                    2.0,
                    1.0,
                    26.0,
                    0.0,
                    1000000,
                    2000000,
                ],
            ],
        ),

        # ------------------------------------------------------------------
        # 3. node_types
        # ------------------------------------------------------------------
        _sql_response(
            [
                "node_type",
                "core_count",
                "memory_mb",
                "gpu_count",
                "dbu_per_hour",
            ],
            [
                [
                    "m5.2xlarge",
                    8,
                    32768,
                    0,
                    0.75,
                ]
            ],
        ),

        # ------------------------------------------------------------------
        # 4. instance_events
        # ------------------------------------------------------------------
        _sql_response(
            [
                "workspace_id",
                "cluster_id",
                "instance_id",
                "event_type",
                "event_time",
            ],
            [
                [
                    "workspace-demo",
                    "cluster-demo-001",
                    "instance-worker-001",
                    "START",
                    "2026-09-25T08:00:00Z",
                ]
            ],
        ),

        # ------------------------------------------------------------------
        # 5. billing_usage
        # ------------------------------------------------------------------
        _sql_response(
            [
                "workspace_id",
                "cluster_id",
                "usage_quantity",
                "usage_start_time",
                "usage_end_time",
            ],
            [
                [
                    "workspace-demo",
                    "cluster-demo-001",
                    18.0,
                    "2026-09-25T08:00:00Z",
                    "2026-09-25T09:00:00Z",
                ]
            ],
        ),

        # ------------------------------------------------------------------
        # 6. job_task_run_timeline
        # ------------------------------------------------------------------
        _sql_response(
            [
                "workspace_id",
                "job_id",
                "task_run_id",
                "cluster_id",
                "start_time",
                "end_time",
            ],
            [
                [
                    "workspace-demo",
                    "job-demo-001",
                    "task-demo-001",
                    "cluster-demo-001",
                    "2026-09-25T08:00:00Z",
                    "2026-09-25T08:30:00Z",
                ]
            ],
        ),
    ]

    route = respx.post(SQL_STATEMENTS_URL)

    for response in responses:
        route.side_effect = responses

        break


# ---------------------------------------------------------------------------
# Runtime detection
# ---------------------------------------------------------------------------


def test_app_runtime_needs_both_a_host_and_an_app_marker(monkeypatch):
    monkeypatch.delenv("DATABRICKS_APP_NAME", raising=False)
    monkeypatch.delenv("DATABRICKS_APP_PORT", raising=False)
    monkeypatch.delenv("DATABRICKS_WORKSPACE_ID", raising=False)

    monkeypatch.setenv("DATABRICKS_HOST", HOST)

    # A developer with DATABRICKS_HOST exported for the CLI is NOT in an App.
    assert databricks_auth.is_app_runtime() is False

    monkeypatch.setenv("DATABRICKS_APP_NAME", "acelo")

    assert databricks_auth.is_app_runtime() is True


def test_host_is_normalised_to_an_https_origin(monkeypatch):
    monkeypatch.setenv(
        "DATABRICKS_HOST",
        "adb-123.azuredatabricks.net/",
    )

    assert (
        databricks_auth.app_host()
        == "https://adb-123.azuredatabricks.net"
    )


def test_describe_reports_the_mode_without_a_credential(
    monkeypatch,
    app_runtime,
):
    described = databricks_auth.describe()

    assert described["databricks_app"] is True
    assert described["auth_mode"] == "databricks_app_identity"
    assert described["workspace_host"] == HOST

    assert APP_TOKEN not in str(described)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


def test_a_stored_pat_still_wins(app_runtime):
    """Existing configured connections must behave exactly as before."""

    adapter = DatabricksAdapter(
        endpoint=HOST,
        auth_metadata={},
        secret=STORED_PAT,
    )

    assert adapter._headers()["Authorization"] == (
        f"Bearer {STORED_PAT}"
    )


def test_without_a_stored_secret_the_app_identity_authenticates(
    app_runtime,
):
    adapter = DatabricksAdapter(
        endpoint="",
        auth_metadata={},
        secret=None,
    )

    assert adapter._headers()["Authorization"] == (
        f"Bearer {APP_TOKEN}"
    )

    # Workspace comes from the runtime.
    assert adapter._endpoint == HOST


def test_outside_an_app_a_missing_credential_is_still_an_error(
    monkeypatch,
):
    monkeypatch.delenv("DATABRICKS_HOST", raising=False)
    monkeypatch.delenv("DATABRICKS_APP_NAME", raising=False)

    monkeypatch.setattr(
        databricks_auth,
        "app_auth_headers",
        lambda: None,
    )

    adapter = DatabricksAdapter(
        endpoint="",
        auth_metadata={},
        secret=None,
    )

    with pytest.raises(PlatformError) as excinfo:
        adapter._require_config()

    assert excinfo.value.code == ErrorCode.NOT_CONFIGURED
    assert "workspace_url" in excinfo.value.message
    assert "access_token" in excinfo.value.message


def test_app_identity_satisfies_the_configuration_check(
    app_runtime,
):
    DatabricksAdapter(
        endpoint="",
        auth_metadata={},
        secret=None,
    )._require_config()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_discovery_runs_on_the_app_identity(app_runtime):
    """Existing discovery works through the App identity."""

    _mock_workspace()

    adapter = DatabricksAdapter(
        endpoint="",
        auth_metadata={},
        secret=None,
    )

    discovery = await adapter.discover_compute_resources()

    assert [
        r.name
        for r in discovery.of_type(ResourceType.SQL_WAREHOUSE)
    ] == [
        "Serverless Starter Warehouse",
        "warehouse_db",
    ]

    assert respx.calls.call_count > 0

    for call in respx.calls:
        assert call.request.headers["Authorization"] == (
            f"Bearer {APP_TOKEN}"
        )

        assert str(call.request.url).startswith(HOST)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def test_bootstrap_registers_the_workspace_without_storing_a_credential(
    db_session,
    app_runtime,
):
    environment = ensure_app_environment(db_session)

    assert environment is not None
    assert environment.platform == "databricks"

    connection = (
        db_session.query(Connection)
        .filter(Connection.id == environment.connection_id)
        .one()
    )

    assert connection.endpoint == HOST
    assert connection.auth_method == APP_AUTH_METHOD

    # App identity means there is no secret at rest.
    assert connection.secret_encrypted is None


def test_bootstrap_is_idempotent(
    db_session,
    app_runtime,
):
    first = ensure_app_environment(db_session)
    second = ensure_app_environment(db_session)

    assert first.id == second.id

    assert (
        db_session.query(Connection)
        .filter(Connection.auth_method == APP_AUTH_METHOD)
        .count()
        == 1
    )


def test_bootstrap_does_nothing_outside_an_app(
    db_session,
    monkeypatch,
):
    monkeypatch.delenv("DATABRICKS_HOST", raising=False)
    monkeypatch.delenv("DATABRICKS_APP_NAME", raising=False)

    assert ensure_app_environment(db_session) is None
    assert db_session.query(Connection).count() == 0


def test_bootstrap_leaves_a_fabric_connection_alone(
    db_session,
    customer,
    fabric_connection,
    app_runtime,
):
    ensure_app_environment(db_session)

    db_session.refresh(fabric_connection)

    assert fabric_connection.platform == "fabric"
    assert fabric_connection.auth_method == "service_principal"


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------


def test_runtime_endpoint_reports_app_mode_and_leaks_nothing(
    customer,
    app_runtime,
):
    with TestClient(app) as client:
        resp = client.get(
            "/api/runtime",
            headers={
                "X-Customer-Id": customer.id,
            },
        )

    assert resp.status_code == 200

    body = resp.json()

    assert body["databricks_app"] is True
    assert body["auth_mode"] == "databricks_app_identity"

    assert APP_TOKEN not in resp.text
    assert "Bearer" not in resp.text


@respx.mock
def test_existing_discovery_endpoint_works_with_no_configured_connection(
    db_session,
    customer,
    app_runtime,
):
    """
    GET /api/databricks/resources must keep working without
    a configured credential.
    """

    _mock_workspace()

    environment = ensure_app_environment(db_session)

    with TestClient(app) as client:
        resp = client.get(
            "/api/databricks/resources",
            headers={
                "X-Customer-Id": environment.customer_id,
                "X-Acelo-Platform": "databricks",
                "X-Acelo-Connection-Id": environment.connection_id,
            },
        )

    assert resp.status_code == 200

    body = resp.json()

    assert body["connected"] is True

    assert [
        r["name"]
        for r in body["compute"]["sql_warehouses"]["resources"]
    ] == [
        "Serverless Starter Warehouse",
        "warehouse_db",
    ]

    # No credential reaches the browser.
    assert APP_TOKEN not in resp.text
    assert "Bearer" not in resp.text


@respx.mock
def test_the_agent_analyses_the_app_workspace_without_configuration(
    db_session,
    customer,
    app_runtime,
):
    """
    End-to-end authentication test for the Databricks App identity.

    The test verifies that:

      App identity
          ↓
      Agent API
          ↓
      DatabricksSQLReader
          ↓
      SQL Statement API
          ↓
      Compute evidence
          ↓
      Optimization analysis

    works without a stored Databricks PAT.
    """

    _mock_workspace()

    # ------------------------------------------------------------------
    # Mock the six SQL evidence queries.
    # ------------------------------------------------------------------

    responses = [
        # 1. cluster
        _sql_response(
            [
                "workspace_id",
                "cluster_id",
                "cluster_name",
                "cluster_source",
                "change_time",
                "delete_time",
                "worker_node_type",
                "driver_node_type",
                "worker_count",
                "min_autoscale_workers",
                "max_autoscale_workers",
                "auto_termination_minutes",
                "policy_id",
                "dbr_version",
            ],
            [
                [
                    "workspace-demo",
                    "cluster-demo-001",
                    "ACELO Demo Cluster",
                    "UI",
                    "2026-09-25T08:00:00Z",
                    None,
                    "m5.2xlarge",
                    "m5.2xlarge",
                    2,
                    2,
                    8,
                    120,
                    "policy-demo",
                    "15.4.x-scala2.12",
                ]
            ],
        ),

        # 2. node_timeline
        _sql_response(
            [
                "workspace_id",
                "cluster_id",
                "instance_id",
                "start_time",
                "end_time",
                "driver",
                "cpu_user_percent",
                "cpu_system_percent",
                "cpu_wait_percent",
                "mem_used_percent",
                "mem_swap_percent",
                "network_receive_bytes",
                "network_transmit_bytes",
            ],
            [
                [
                    "workspace-demo",
                    "cluster-demo-001",
                    "instance-driver-001",
                    "2026-09-25T08:00:00Z",
                    "2026-09-25T09:00:00Z",
                    True,
                    10.0,
                    2.0,
                    1.0,
                    25.0,
                    0.0,
                    1000000,
                    2000000,
                ],
                [
                    "workspace-demo",
                    "cluster-demo-001",
                    "instance-worker-001",
                    "2026-09-25T08:00:00Z",
                    "2026-09-25T09:00:00Z",
                    False,
                    11.0,
                    2.0,
                    1.0,
                    27.0,
                    0.0,
                    1000000,
                    2000000,
                ],
                [
                    "workspace-demo",
                    "cluster-demo-001",
                    "instance-worker-002",
                    "2026-09-25T08:00:00Z",
                    "2026-09-25T09:00:00Z",
                    False,
                    10.0,
                    2.0,
                    1.0,
                    26.0,
                    0.0,
                    1000000,
                    2000000,
                ],
            ],
        ),

        # 3. node_types
        _sql_response(
            [
                "node_type",
                "core_count",
                "memory_mb",
                "gpu_count",
                "dbu_per_hour",
            ],
            [
                [
                    "m5.2xlarge",
                    8,
                    32768,
                    0,
                    0.75,
                ]
            ],
        ),

        # 4. instance_events
        _sql_response(
            [
                "workspace_id",
                "cluster_id",
                "instance_id",
                "event_type",
                "event_time",
            ],
            [
                [
                    "workspace-demo",
                    "cluster-demo-001",
                    "instance-worker-001",
                    "START",
                    "2026-09-25T08:00:00Z",
                ]
            ],
        ),

        # 5. billing_usage
        _sql_response(
            [
                "workspace_id",
                "cluster_id",
                "usage_quantity",
                "usage_start_time",
                "usage_end_time",
            ],
            [
                [
                    "workspace-demo",
                    "cluster-demo-001",
                    18.0,
                    "2026-09-25T08:00:00Z",
                    "2026-09-25T09:00:00Z",
                ]
            ],
        ),

        # 6. job_task_run_timeline
        _sql_response(
            [
                "workspace_id",
                "job_id",
                "task_run_id",
                "cluster_id",
                "start_time",
                "end_time",
            ],
            [
                [
                    "workspace-demo",
                    "job-demo-001",
                    "task-demo-001",
                    "cluster-demo-001",
                    "2026-09-25T08:00:00Z",
                    "2026-09-25T08:30:00Z",
                ]
            ],
        ),
    ]

    sql_route = respx.post(SQL_STATEMENTS_URL)

    # respx calls the side effect once for each SQL request.
    call_index = {"value": 0}

    def sql_side_effect(request):
        index = call_index["value"]

        if index >= len(responses):
            return httpx.Response(
                500,
                json={
                    "error": "Unexpected extra SQL request",
                },
            )

        call_index["value"] += 1

        response = responses[index]

        # Verify that the App identity is being used.
        assert request.headers["Authorization"] == (
            f"Bearer {APP_TOKEN}"
        )

        return response

    sql_route.side_effect = sql_side_effect

    # ------------------------------------------------------------------
    # Bootstrap the Databricks App environment.
    # ------------------------------------------------------------------

    environment = ensure_app_environment(db_session)

    # ------------------------------------------------------------------
    # Execute the actual agent API.
    # ------------------------------------------------------------------

    with TestClient(app) as client:
        resp = client.post(
            "/api/databricks/agent/analyze",
            headers={
                "X-Customer-Id": environment.customer_id,
                "X-Acelo-Platform": "databricks",
                "X-Acelo-Connection-Id": environment.connection_id,
            },
            json={
                "prompt": "show me all clusters",
            },
        )

    # ------------------------------------------------------------------
    # Validate API response.
    # ------------------------------------------------------------------

    assert resp.status_code == 200

    body = resp.json()

    assert body["ok"] is True
    assert body["environment_id"] == environment.id

    # ------------------------------------------------------------------
    # Security validation.
    # ------------------------------------------------------------------

    # The App identity must never be returned to the browser.
    assert APP_TOKEN not in resp.text
    assert "Bearer" not in resp.text

    # ------------------------------------------------------------------
    # Validate that all six evidence queries were executed.
    # ------------------------------------------------------------------

    assert call_index["value"] == 6

    # ------------------------------------------------------------------
    # Validate the agent completed its analysis workflow.
    # ------------------------------------------------------------------

    assert body["steps"]

    step_ids = {
        step["id"]
        for step in body["steps"]
    }

    assert "environment" in step_ids
    assert "auth" in step_ids
    assert "discover" in step_ids
    assert "resources" in step_ids
    assert "analyze" in step_ids