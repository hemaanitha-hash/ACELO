import httpx
import pytest
import respx

from platforms.databricks import DatabricksAdapter

ENDPOINT = "https://adb-test.azuredatabricks.net"


def _adapter():
    return DatabricksAdapter(endpoint=ENDPOINT, auth_metadata={}, secret="test-pat")


@pytest.mark.asyncio
@respx.mock
async def test_start_analysis_resolves_job_by_name_and_triggers_run():
    respx.get(f"{ENDPOINT}/api/2.1/jobs/list").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {"job_id": 111, "settings": {"name": "cluster job"}},
                    {"job_id": 222, "settings": {"name": "query job"}},
                ]
            },
        )
    )
    respx.post(f"{ENDPOINT}/api/2.1/jobs/run-now").mock(
        return_value=httpx.Response(200, json={"run_id": 9001, "number_in_job": 1})
    )

    adapter = _adapter()
    result = await adapter.start_analysis("cluster")

    assert result.platform_run_id == "9001"
    assert result.status == "STARTING"
    assert result.detail["job_id"] == 111

    run_now_request = respx.calls.last.request
    assert run_now_request.url == f"{ENDPOINT}/api/2.1/jobs/run-now"


@pytest.mark.asyncio
@respx.mock
async def test_start_analysis_raises_lookup_error_when_job_missing():
    respx.get(f"{ENDPOINT}/api/2.1/jobs/list").mock(return_value=httpx.Response(200, json={"jobs": []}))

    adapter = _adapter()
    with pytest.raises(LookupError):
        await adapter.start_analysis("cluster")


@pytest.mark.asyncio
@respx.mock
async def test_get_run_status_maps_databricks_states_to_job_status():
    respx.get(f"{ENDPOINT}/api/2.1/jobs/runs/get").mock(
        return_value=httpx.Response(
            200,
            json={
                "state": {"life_cycle_state": "TERMINATED", "result_state": "SUCCESS"},
                "run_page_url": "https://adb-test/run/9001",
            },
        )
    )

    adapter = _adapter()
    result = await adapter.get_run_status("9001")

    assert result.status == "COMPLETED"
    assert result.detail["run_page_url"] == "https://adb-test/run/9001"


@pytest.mark.asyncio
@respx.mock
async def test_get_run_status_running_state():
    respx.get(f"{ENDPOINT}/api/2.1/jobs/runs/get").mock(
        return_value=httpx.Response(200, json={"state": {"life_cycle_state": "RUNNING"}})
    )
    adapter = _adapter()
    result = await adapter.get_run_status("9001")
    assert result.status == "RUNNING"


@pytest.mark.asyncio
@respx.mock
async def test_cancel_run_success():
    respx.post(f"{ENDPOINT}/api/2.1/jobs/runs/cancel").mock(return_value=httpx.Response(200, json={}))
    adapter = _adapter()
    result = await adapter.cancel_run("9001")
    assert result.ok is True


@pytest.mark.asyncio
@respx.mock
async def test_test_connection_auth_failure_reported_clearly():
    respx.get(f"{ENDPOINT}/api/2.0/clusters/list").mock(return_value=httpx.Response(401, json={}))
    adapter = _adapter()
    result = await adapter.test_connection()
    assert result.ok is False
    assert "personal access token" in result.message.lower()
