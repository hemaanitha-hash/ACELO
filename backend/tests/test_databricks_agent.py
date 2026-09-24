"""
The ACELO Optimization Agent's Databricks compute analysis (Phase 1).

    user -> agent -> discovery capability -> existing authenticated adapter
         -> Databricks REST API -> real resources -> analysis -> recommendations

What these tests protect:
  * the agent reaches Databricks through the EXISTING adapter and the existing
    encrypted credential — not a second client or a localhost HTTP hop
  * it reasons only over discovered evidence, and never states a cost, a saving
    or a utilization figure
  * it says what it could NOT observe
  * partial discovery still produces an answer
  * authentication/authorization failures come back structured, not as a 500
  * no token reaches the response, the analysis, or an LLM prompt

Every Databricks call is mocked; nothing here needs a live workspace, and
nothing here executes anything.
"""

import json
import re
import uuid

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from agent.databricks_agent import (
    StepStatus,
    analyze_databricks_compute,
    is_databricks_compute_request,
)
from agent.databricks_analysis import analyze_compute, looks_fabricated
from agent.databricks_report import render_markdown
from agent.capabilities import CapabilityStatus, discover_databricks_resources
from main import app
from models import Environment
from platforms.databricks_resources import ResourceType
from services.crypto import get_cipher

ENDPOINT = "https://adb-test.azuredatabricks.net"
TOKEN = "dapi-super-secret-token"

CLUSTERS_URL = f"{ENDPOINT}/api/2.0/clusters/list"
WAREHOUSES_URL = f"{ENDPOINT}/api/2.0/sql/warehouses"
# Serverless compute has no list API: ACELO confirms operator-supplied ids
# through the Permissions API. These ids are set by the autouse fixture below.
SERVERLESS_IDS = (
    "aaaa1111-0000-4000-8000-000000000001",
    "bbbb2222-0000-4000-8000-000000000002",
)
SERVERLESS_NAMES = ("Default Interactive Compute", "Default Automated Compute")
SERVERLESS_URLS = tuple(
    f"{ENDPOINT}/api/2.0/permissions/serverless-compute/{i}" for i in SERVERLESS_IDS
)


@pytest.fixture(autouse=True)
def _configured_serverless_ids(monkeypatch):
    """The two serverless objects this workspace's operator has identified."""
    monkeypatch.setenv(
        "ACELO_DATABRICKS_SERVERLESS_IDS",
        ",".join(f"{i}={n}" for i, n in zip(SERVERLESS_IDS, SERVERLESS_NAMES)),
    )

DENIED = {"error_code": "PERMISSION_DENIED", "message": "User does not have permission."}
UNAUTHORIZED = {"error_code": "UNAUTHORIZED", "message": "Invalid access token."}

# A cluster with auto-termination disabled and a fixed worker count: both are
# real configuration values, so both may be reported as observations.
RAW_CLUSTER = {
    "cluster_id": "0421-193742-abcd1234",
    "cluster_name": "analytics-all-purpose",
    "state": "RUNNING",
    "cluster_source": "UI",
    "driver_node_type_id": "Standard_DS3_v2",
    "node_type_id": "Standard_DS3_v2",
    "num_workers": 8,
    "autotermination_minutes": 0,
    "spark_version": "14.3.x-scala2.12",
}
# A real Permissions API response for a serverless-compute object. It carries
# no display name — the name comes from the operator-supplied label.
def _serverless_permissions(object_id: str) -> dict:
    return {
        "object_id": f"/serverless-compute/{object_id}",
        "object_type": "serverless-compute",
        "access_control_list": [
            {"group_name": "users", "all_permissions": [{"permission_level": "CAN_USE"}]}
        ],
    }
RAW_WAREHOUSES = [
    {
        "id": "b1c2",
        "name": "Serverless Starter Warehouse",
        "state": "RUNNING",
        "warehouse_type": "PRO",
        "cluster_size": "2X-Small",
        "auto_stop_mins": 10,
        "min_num_clusters": 1,
        "max_num_clusters": 1,
    },
    {
        "id": "a9b8",
        "name": "warehouse_db",
        "state": "STOPPED",
        "warehouse_type": "CLASSIC",
        "cluster_size": "Small",
        "auto_stop_mins": 0,
        "min_num_clusters": 1,
        "max_num_clusters": 3,
    },
]


def _mock(clusters=None, serverless=None, warehouses=None) -> None:
    respx.get(CLUSTERS_URL).mock(return_value=clusters or httpx.Response(200, json={"clusters": [RAW_CLUSTER]}))
    for object_id, url in zip(SERVERLESS_IDS, SERVERLESS_URLS):
        respx.get(url).mock(
            return_value=serverless or httpx.Response(200, json=_serverless_permissions(object_id))
        )
    respx.get(WAREHOUSES_URL).mock(
        return_value=warehouses or httpx.Response(200, json={"warehouses": RAW_WAREHOUSES})
    )


@pytest.fixture
def databricks_environment(db_session, customer, databricks_connection):
    """A Databricks environment whose PAT is encrypted at rest, as in production."""
    databricks_connection.secret_encrypted = get_cipher().encrypt(TOKEN)
    environment = Environment(
        id=str(uuid.uuid4()),
        customer_id=customer.id,
        connection_id=databricks_connection.id,
        name="Databricks PoC",
        platform="databricks",
        auth_mode="pat",
        workspace_name="adb-test.azuredatabricks.net",
        status="connected",
    )
    db_session.add(environment)
    db_session.commit()
    db_session.refresh(environment)
    return environment


# --- intent -----------------------------------------------------------------


def test_agent_recognises_a_databricks_compute_analysis_request():
    assert is_databricks_compute_request("Analyze my Databricks compute and find optimization opportunities.")
    assert is_databricks_compute_request("review databricks clusters and warehouses")


def test_non_databricks_prompts_stay_on_the_existing_path():
    """An established prompt must not be silently rerouted by this new flow."""
    assert not is_databricks_compute_request("Check my cluster utilization")
    assert not is_databricks_compute_request("Find unhealthy queries")
    assert not is_databricks_compute_request("Analyze this cluster file")
    # Names Databricks but asks for something else entirely.
    assert not is_databricks_compute_request("What is Databricks?")


# --- the capability reuses the existing adapter and credential ---------------


@pytest.mark.asyncio
@respx.mock
async def test_capability_uses_the_existing_encrypted_credential_and_adapter(db_session, databricks_environment):
    _mock()

    result = await discover_databricks_resources(db_session, databricks_environment)

    assert result.ok is True
    assert result.status == CapabilityStatus.OK
    # The PAT stored encrypted on the Connection was decrypted by the existing
    # environment service and sent by the existing adapter.
    assert respx.calls.call_count > 0
    assert respx.calls[0].request.headers["Authorization"] == f"Bearer {TOKEN}"
    # Every call the capability made was a read.
    for call in respx.calls:
        assert call.request.method == "GET"


@pytest.mark.asyncio
@respx.mock
async def test_capability_returns_normalised_resources_of_all_three_types(db_session, databricks_environment):
    _mock()

    result = await discover_databricks_resources(db_session, databricks_environment)
    by_type: dict[str, list[str]] = {}
    for resource in result.data["resources"]:
        by_type.setdefault(resource["resource_type"], []).append(resource["name"])

    assert by_type[ResourceType.CLASSIC_CLUSTER] == ["analytics-all-purpose"]
    assert by_type[ResourceType.SERVERLESS_COMPUTE] == [
        "Default Interactive Compute",
        "Default Automated Compute",
    ]
    assert by_type[ResourceType.SQL_WAREHOUSE] == ["Serverless Starter Warehouse", "warehouse_db"]
    # A SQL warehouse must never be classified as a cluster.
    assert "Serverless Starter Warehouse" not in by_type.get(ResourceType.CLASSIC_CLUSTER, [])


@pytest.mark.asyncio
async def test_capability_refuses_a_non_databricks_environment(db_session, customer, fabric_connection):
    environment = Environment(
        id=str(uuid.uuid4()),
        customer_id=customer.id,
        connection_id=fabric_connection.id,
        name="Fabric env",
        platform="fabric",
        status="connected",
    )
    db_session.add(environment)
    db_session.commit()

    result = await discover_databricks_resources(db_session, environment)

    assert result.ok is False
    assert result.status == CapabilityStatus.NOT_CONFIGURED


# --- agent flow -------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_agent_calls_discovery_and_analyses_only_real_resources(db_session, databricks_environment):
    _mock()

    result = await analyze_databricks_compute(db_session, databricks_environment)

    assert result.ok is True
    assert [s.status for s in result.steps] == [StepStatus.DONE] * 5

    # The analysis names exactly the resources Databricks returned — no others.
    names = {row["resource"] for row in result.analysis["classification"]}
    assert names == {
        "analytics-all-purpose",
        "Default Interactive Compute",
        "Default Automated Compute",
        "Serverless Starter Warehouse",
        "warehouse_db",
    }


@pytest.mark.asyncio
@respx.mock
async def test_agent_findings_are_tied_to_observed_configuration(db_session, databricks_environment):
    _mock()

    result = await analyze_databricks_compute(db_session, databricks_environment)
    evidence = {f["observed_evidence"] for f in result.analysis["opportunities"]}

    # Both come from values actually present in the mocked payloads.
    assert "auto_termination_minutes = 0" in evidence
    assert "auto_stop_mins = 0" in evidence
    # Every opportunity carries evidence and what is still required.
    for opportunity in result.analysis["opportunities"]:
        assert opportunity["observed_evidence"]
        assert opportunity["evidence_required"]
        assert opportunity["recommendation"]


@pytest.mark.asyncio
@respx.mock
async def test_agent_never_invents_cost_utilization_or_savings(db_session, databricks_environment):
    _mock()

    result = await analyze_databricks_compute(db_session, databricks_environment)
    text = json.dumps(result.to_dict())

    # No currency, no savings percentage, no DBU figure anywhere in the answer.
    # (The word "savings" may appear only in the disclaimer that none is given,
    # so this checks for fabricated NUMBERS, which is the actual risk.)
    assert not re.search(r"[$£€]\s?\d", text)
    assert not re.search(r"\d+(\.\d+)?\s?%", text)
    assert not re.search(r"\d+\s*(dbu|dbus)\b", text, re.IGNORECASE)
    assert not re.search(r"sav\w*\s+[$£€]?\d", text, re.IGNORECASE)
    assert "/month" not in text
    # Utilization is never asserted — it is listed as unmeasured instead.
    assert not re.search(r"utilization (is|was|of)\s*\d", text, re.IGNORECASE)

    # The report states impact qualitatively instead.
    assert "Qualitative" in result.markdown


@pytest.mark.asyncio
@respx.mock
async def test_agent_reports_what_it_could_not_observe(db_session, databricks_environment):
    _mock()

    result = await analyze_databricks_compute(db_session, databricks_environment)
    missing = " ".join(result.analysis["missing_evidence"]).lower()

    assert "## Missing Evidence" in result.markdown
    assert "utilization" in missing
    assert "cost" in missing
    assert "idle time" in missing


@pytest.mark.asyncio
@respx.mock
async def test_agent_report_has_the_required_sections(db_session, databricks_environment):
    _mock()

    markdown = (await analyze_databricks_compute(db_session, databricks_environment)).markdown

    for heading in (
        "## Observed Facts",
        "## Resource Classification",
        "## Missing Evidence",
        "## Potential Optimization Opportunities",
    ):
        assert heading in markdown
    assert "| Resource | Type | State | Relevant Configuration |" in markdown


@pytest.mark.asyncio
@respx.mock
async def test_agent_survives_partial_discovery(db_session, databricks_environment):
    """Serverless refused; clusters and warehouses must still be analysed."""
    _mock(serverless=httpx.Response(403, json=DENIED))

    result = await analyze_databricks_compute(db_session, databricks_environment)

    assert result.ok is True
    types = {row["type"] for row in result.analysis["classification"]}
    assert types == {ResourceType.CLASSIC_CLUSTER, ResourceType.SQL_WAREHOUSE}
    # The gap is declared rather than passed off as "you have no serverless".
    assert any("SERVERLESS_COMPUTE" in m for m in result.analysis["missing_evidence"])
    assert "SERVERLESS_COMPUTE" in result.markdown


@pytest.mark.asyncio
@respx.mock
async def test_agent_returns_structured_authentication_failure(db_session, databricks_environment):
    _mock(
        clusters=httpx.Response(401, json=UNAUTHORIZED),
        serverless=httpx.Response(401, json=UNAUTHORIZED),
        warehouses=httpx.Response(401, json=UNAUTHORIZED),
    )

    result = await analyze_databricks_compute(db_session, databricks_environment)

    assert result.ok is False
    assert result.status == "AUTHENTICATION_FAILED"
    # The auth step failed and nothing after it claims to have happened.
    steps = {s.id: s.status for s in result.steps}
    assert steps["auth"] == StepStatus.FAILED
    assert steps["resources"] == StepStatus.PENDING
    assert steps["analyze"] == StepStatus.PENDING
    assert result.analysis is None


@pytest.mark.asyncio
@respx.mock
async def test_agent_returns_structured_authorization_failure(db_session, databricks_environment):
    _mock(
        clusters=httpx.Response(403, json=DENIED),
        serverless=httpx.Response(403, json=DENIED),
        warehouses=httpx.Response(403, json=DENIED),
    )

    result = await analyze_databricks_compute(db_session, databricks_environment)

    assert result.ok is False
    assert result.status == "AUTHORIZATION_FAILED"
    assert "permission" in (result.message or "").lower()


@pytest.mark.asyncio
@respx.mock
async def test_agent_does_not_claim_an_optimal_workspace_when_nothing_is_found(db_session, databricks_environment):
    """An empty result must not become a clean bill of health."""
    _mock(
        clusters=httpx.Response(200, json={"clusters": []}),
        serverless=httpx.Response(200, json={"compute": []}),
        warehouses=httpx.Response(200, json={"warehouses": []}),
    )

    result = await analyze_databricks_compute(db_session, databricks_environment)

    assert result.ok is True
    assert result.analysis["opportunities"] == []
    assert "not" in result.markdown.lower()
    assert "optimally configured" in result.markdown


# --- LLM guardrail ----------------------------------------------------------


def test_a_model_that_invents_a_number_is_rejected():
    """The LLM writes the narrative only, and cannot smuggle in a figure."""
    resources = [
        {
            "platform": "databricks",
            "resource_type": ResourceType.CLASSIC_CLUSTER,
            "resource_id": "c1",
            "name": "c1",
            "state": "RUNNING",
            "metadata": {"auto_termination_minutes": 0},
        }
    ]

    fabricating = analyze_compute("ws", resources, [], llm=lambda _: "This will save $500/month.")
    honest = analyze_compute("ws", resources, [], llm=lambda _: "Auto-termination is disabled on one cluster.")

    assert "$500" not in fabricating.summary
    assert "configuration observation" in fabricating.summary
    assert honest.summary == "Auto-termination is disabled on one cluster."


def test_fabrication_detector_catches_money_and_percentages():
    assert looks_fabricated("saves $500/month")
    assert looks_fabricated("a 40% reduction")
    assert looks_fabricated("reduces DBU consumption")
    assert not looks_fabricated("Auto-termination is disabled on one cluster.")


@pytest.mark.asyncio
@respx.mock
async def test_the_llm_prompt_never_contains_the_token(db_session, databricks_environment):
    _mock()
    seen: list[str] = []

    def spy(prompt: str) -> str:
        seen.append(prompt)
        return "Configuration observations were raised."

    await analyze_databricks_compute(db_session, databricks_environment, llm=spy)

    assert seen, "the LLM was never called"
    for prompt in seen:
        assert TOKEN not in prompt
        assert "dapi" not in prompt
        assert "Bearer" not in prompt
        assert "Authorization" not in prompt


# --- endpoint ---------------------------------------------------------------


@respx.mock
def test_endpoint_runs_the_full_flow_and_returns_real_steps(customer, databricks_environment):
    _mock()

    with TestClient(app) as client:
        resp = client.post(
            "/api/databricks/agent/analyze",
            headers={"X-Customer-Id": customer.id},
            json={"prompt": "Analyze my Databricks compute and find optimization opportunities."},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert [s["status"] for s in body["steps"]] == ["done"] * 5
    assert body["markdown"].startswith("## Observed Facts")
    assert len(body["analysis"]["classification"]) == 5


@respx.mock
def test_endpoint_returns_200_with_a_structured_failure_not_a_500(customer, databricks_environment):
    _mock(
        clusters=httpx.Response(401, json=UNAUTHORIZED),
        serverless=httpx.Response(401, json=UNAUTHORIZED),
        warehouses=httpx.Response(401, json=UNAUTHORIZED),
    )

    with TestClient(app) as client:
        resp = client.post(
            "/api/databricks/agent/analyze",
            headers={"X-Customer-Id": customer.id},
            json={"prompt": "Analyze my Databricks compute."},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["status"] == "AUTHENTICATION_FAILED"


@respx.mock
def test_endpoint_response_never_leaks_the_token(customer, databricks_environment):
    _mock()

    with TestClient(app) as client:
        resp = client.post(
            "/api/databricks/agent/analyze",
            headers={"X-Customer-Id": customer.id},
            json={"prompt": "Analyze my Databricks compute."},
        )

    raw = resp.text
    assert TOKEN not in raw
    assert "dapi" not in raw
    assert "Bearer" not in raw
    assert "Authorization" not in raw


def test_endpoint_does_not_start_a_job_or_touch_the_execution_worker(customer, databricks_environment, db_session):
    """
    Phase 1 is discover -> analyze -> recommend. The agent analysis must create
    no AnalysisJob and no JobRun, so the background execution engine is not
    involved at all.
    """
    from models import AnalysisJob, JobRun

    with respx.mock:
        _mock()
        with TestClient(app) as client:
            client.post(
                "/api/databricks/agent/analyze",
                headers={"X-Customer-Id": customer.id},
                json={"prompt": "Analyze my Databricks compute."},
            )

    assert db_session.query(AnalysisJob).count() == 0
    assert db_session.query(JobRun).count() == 0


# --- rendering --------------------------------------------------------------


def test_report_renders_a_serverless_resource_without_cluster_fields():
    resources = [
        {
            "platform": "databricks",
            "resource_type": ResourceType.SERVERLESS_COMPUTE,
            "resource_id": "default-interactive",
            "name": "Default Interactive Compute",
            "state": "ENABLED",
            "metadata": {"id": "default-interactive", "name": "Default Interactive Compute"},
        }
    ]

    markdown = render_markdown(analyze_compute("ws", resources, []))

    assert "Default Interactive Compute" in markdown
    assert "Serverless compute" in markdown
    # Serverless exposes no sizing levers, so no opportunity is invented for it.
    assert "auto_termination" not in markdown


# --- D. generic requests resolve within the active platform -----------------
#
# The point of the platform context: with Databricks active the user should not
# have to type "Databricks" in every request.


def test_generic_compute_requests_route_to_databricks_when_it_is_active():
    for prompt in (
        "show me all clusters",
        "list my warehouses",
        "what compute do I have",
        "show me all compute clusters",
    ):
        assert is_databricks_compute_request(prompt, active_platform="databricks"), prompt


def test_the_same_generic_requests_do_not_route_to_databricks_when_fabric_is_active():
    """In Fabric context these must stay on the Fabric path."""
    for prompt in ("show me all clusters", "list my warehouses", "what compute do I have"):
        assert not is_databricks_compute_request(prompt, active_platform="fabric"), prompt
        # No context at all is equally not-Databricks.
        assert not is_databricks_compute_request(prompt), prompt


def test_naming_databricks_still_works_from_any_context():
    prompt = "Analyze my Databricks compute and find optimization opportunities."
    assert is_databricks_compute_request(prompt, active_platform="fabric")
    assert is_databricks_compute_request(prompt, active_platform=None)


def test_non_compute_prompts_never_route_to_compute_discovery():
    for prompt in ("what is the weather", "summarise last month", "who approved this"):
        assert not is_databricks_compute_request(prompt, active_platform="databricks"), prompt


@pytest.mark.asyncio
@respx.mock
async def test_the_agent_context_names_the_active_platform_and_forbids_fabric(
    db_session, databricks_environment
):
    """The LLM is told which platform it is in, and told not to leave it."""
    _mock()
    seen: list[str] = []

    await analyze_databricks_compute(
        db_session, databricks_environment, llm=lambda p: seen.append(p) or "Observations were raised."
    )

    assert seen, "the LLM was never called"
    prompt = seen[0]
    assert "ACTIVE PLATFORM: DATABRICKS" in prompt
    assert "ACTIVE CONNECTION:" in prompt
    assert "Never mention Microsoft Fabric" in prompt
