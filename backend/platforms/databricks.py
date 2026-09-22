from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from platforms.base import (
    ConnectionResult,
    ConnectionValidation,
    DiscoveredResource,
    DiscoveredWorkspace,
    PlatformAdapter,
    PlatformCapabilityNotImplemented,
    RunLogEntry,
    RunResult,
    RunStatusResult,
    StartAnalysisResult,
)
from platforms.errors import ErrorCode, PlatformError, code_for_status

# Existing Databricks jobs are resolved by NAME, not hardcoded job_id, so the
# same adapter works across customer workspaces where these jobs already exist
# under these names.
_JOB_NAME_BY_DOMAIN = {
    "cluster": "cluster job",
    "query": "query job",
    "storage": "storage job",
    "all": "acelo_demo",
}

# Databricks life_cycle_state/result_state -> our JobStatus
_LIFECYCLE_TO_STATUS = {
    "PENDING": "QUEUED",
    "RUNNING": "RUNNING",
    "TERMINATING": "RUNNING",
    "BLOCKED": "RUNNING",
    "WAITING_FOR_RETRY": "RETRYING",
    "INTERNAL_ERROR": "FAILED",
}
_RESULT_TO_STATUS = {
    "SUCCESS": "COMPLETED",
    "FAILED": "FAILED",
    "TIMEDOUT": "FAILED",
    "CANCELED": "CANCELLED",
}


class DatabricksAdapter(PlatformAdapter):
    """
    Auth: Personal Access Token (PAT), sent as a Bearer token.
    `endpoint` is the workspace URL, e.g. https://adb-xxxxxxxxxxxx.xx.azuredatabricks.net

    Note: Databricks' REST API does not send CORS headers, so it can only ever
    be called from a server (this backend) — never directly from the browser.
    https://kb.databricks.com/security/cors-policy-error-when-trying-to-run-databricks-api-from-a-browser-based-application
    """

    platform_name = "databricks"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.secret}", "Content-Type": "application/json"}

    async def connect(self) -> ConnectionResult:
        return await self.test_connection()

    async def test_connection(self) -> ConnectionResult:
        url = f"{self.endpoint}/api/2.0/clusters/list"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url, headers=self._headers())
        except httpx.RequestError as exc:
            return ConnectionResult(ok=False, message=f"Could not reach workspace: {exc}")

        if resp.status_code == 200:
            count = len(resp.json().get("clusters", []))
            return ConnectionResult(ok=True, message="Connected", detail={"cluster_count": count})
        if resp.status_code in (401, 403):
            return ConnectionResult(ok=False, message="Authentication failed — check the personal access token.")
        return ConnectionResult(ok=False, message=f"Databricks returned HTTP {resp.status_code}: {resp.text[:300]}")

    async def get_environment(self) -> dict[str, Any]:
        url = f"{self.endpoint}/api/2.0/clusters/list"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=self._headers())
        resp.raise_for_status()
        clusters = resp.json().get("clusters", [])
        return {
            "platform": "Databricks",
            "cluster_count": len(clusters),
            "running_clusters": sum(1 for c in clusters if c.get("state") == "RUNNING"),
        }

    # --- Phase 1: connection validation & environment discovery ---
    # Architecture complete and real. If no Databricks credentials are configured
    # this reports NOT_CONFIGURED - it never reports a connection it did not make.

    def _require_config(self) -> None:
        missing = []
        if not self.endpoint:
            missing.append("workspace_url")
        if not self.secret:
            missing.append("access_token")
        if missing:
            raise PlatformError(
                ErrorCode.NOT_CONFIGURED,
                "Databricks is not configured. Missing: " + ", ".join(missing) + ".",
                log_detail="missing databricks config: {}".format(missing),
            )

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = self.endpoint + path
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url, headers=self._headers(), params=params)
        except httpx.TimeoutException as exc:
            raise PlatformError(ErrorCode.TIMEOUT, log_detail="timeout calling {}: {}".format(path, exc)) from exc
        except httpx.RequestError as exc:
            raise PlatformError(
                ErrorCode.PLATFORM_API_UNAVAILABLE, log_detail="request error calling {}: {}".format(path, exc)
            ) from exc

        if resp.status_code != 200:
            raise PlatformError(
                code_for_status(resp.status_code),
                log_detail="GET {} -> {}: {}".format(path, resp.status_code, resp.text[:300]),
            )
        return resp.json()

    async def validate_connection(self) -> ConnectionValidation:
        """Real authenticated call against the configured workspace."""
        try:
            self._require_config()
            await self._get_json("/api/2.0/clusters/list")
        except PlatformError as exc:
            return ConnectionValidation(connected=False, message=exc.message, error_code=exc.code)

        workspace_name = urlparse(self.endpoint).hostname or self.endpoint
        return ConnectionValidation(
            connected=True,
            message="Databricks connection verified",
            workspace_id=self.auth_metadata.get("workspace_id") or workspace_name,
            workspace_name=workspace_name,
        )

    async def discover_workspace(self) -> DiscoveredWorkspace:
        self._require_config()
        await self._get_json("/api/2.0/clusters/list")
        host = urlparse(self.endpoint).hostname or self.endpoint
        return DiscoveredWorkspace(
            workspace_id=self.auth_metadata.get("workspace_id") or host,
            workspace_name=host,
            detail={"endpoint": self.endpoint},
        )

    async def discover_resources(self) -> list[DiscoveredResource]:
        """Lists the real clusters and jobs visible to this token."""
        self._require_config()
        resources: list[DiscoveredResource] = []

        clusters = await self._get_json("/api/2.0/clusters/list")
        for cluster in clusters.get("clusters", []):
            resources.append(
                DiscoveredResource(
                    platform_resource_id=cluster.get("cluster_id", ""),
                    display_name=cluster.get("cluster_name") or cluster.get("cluster_id", ""),
                    resource_type="Cluster",
                    detail={
                        "state": cluster.get("state"),
                        "spark_version": cluster.get("spark_version"),
                        "node_type_id": cluster.get("node_type_id"),
                    },
                )
            )

        # Jobs are paginated; follow the cursor rather than capping at one page.
        page_token: str | None = None
        seen_tokens: set[str] = set()
        while True:
            params: dict[str, Any] = {"limit": 100}
            if page_token:
                params["page_token"] = page_token
            body = await self._get_json("/api/2.1/jobs/list", params=params)
            for job in body.get("jobs", []):
                settings = job.get("settings", {})
                resources.append(
                    DiscoveredResource(
                        platform_resource_id=str(job.get("job_id", "")),
                        display_name=settings.get("name") or str(job.get("job_id", "")),
                        resource_type="Job",
                        detail={"created_time": job.get("created_time")},
                    )
                )
            page_token = body.get("next_page_token")
            if not page_token or page_token in seen_tokens:
                break
            seen_tokens.add(page_token)

        return resources

    async def discover_files(self) -> list[DiscoveredResource]:
        # DBFS/Volumes enumeration is Phase 9. Declared, not faked.
        raise PlatformCapabilityNotImplemented(
            self.platform_name, "discover_files", "DBFS/Volumes file discovery is Phase 9."
        )

    async def _resolve_job_id(self, client: httpx.AsyncClient, domain: str) -> int:
        job_name = _JOB_NAME_BY_DOMAIN.get(domain)
        if not job_name:
            raise ValueError(f"No Databricks job mapping for domain '{domain}'.")

        resp = await client.get(f"{self.endpoint}/api/2.1/jobs/list", headers=self._headers(), params={"limit": 100})
        resp.raise_for_status()
        jobs = resp.json().get("jobs", [])
        for job in jobs:
            settings = job.get("settings", {})
            if settings.get("name") == job_name:
                return job["job_id"]

        raise LookupError(
            f"No Databricks job named '{job_name}' was found in this workspace. "
            f"The '{domain}' optimization requires a job with that exact name to already exist."
        )

    async def start_analysis(
        self, domain: str, parameters: dict[str, Any] | None = None
    ) -> StartAnalysisResult:
        async with httpx.AsyncClient(timeout=30) as client:
            job_id = await self._resolve_job_id(client, domain)
            resp = await client.post(
                f"{self.endpoint}/api/2.1/jobs/run-now",
                headers=self._headers(),
                json={"job_id": job_id},
            )
        resp.raise_for_status()
        run_id = resp.json()["run_id"]
        return StartAnalysisResult(platform_run_id=str(run_id), status="STARTING", detail={"job_id": job_id})

    async def get_run_status(self, platform_run_id: str, domain: str = "cluster") -> RunStatusResult:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.endpoint}/api/2.1/jobs/runs/get",
                headers=self._headers(),
                params={"run_id": platform_run_id},
            )
        resp.raise_for_status()
        data = resp.json()
        state = data.get("state", {})
        life_cycle = state.get("life_cycle_state", "PENDING")
        result_state = state.get("result_state")

        if life_cycle == "TERMINATED" and result_state:
            status = _RESULT_TO_STATUS.get(result_state, "FAILED")
        else:
            status = _LIFECYCLE_TO_STATUS.get(life_cycle, "RUNNING")

        return RunStatusResult(
            status=status,
            current_step=state.get("state_message"),
            progress=None,  # Databricks does not expose a numeric run-progress signal
            error=state.get("state_message") if status == "FAILED" else None,
            detail={"run_page_url": data.get("run_page_url")},
        )

    async def get_run_logs(self, platform_run_id: str, domain: str = "cluster") -> list[RunLogEntry]:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.endpoint}/api/2.1/jobs/runs/get-output",
                headers=self._headers(),
                params={"run_id": platform_run_id},
            )
        resp.raise_for_status()
        data = resp.json()
        now = datetime.now(timezone.utc).isoformat()
        entries: list[RunLogEntry] = []

        logs_text = data.get("logs")
        if logs_text:
            entries.append(RunLogEntry(timestamp=now, level="INFO", message=logs_text, source="databricks"))
        error = data.get("error")
        if error:
            entries.append(RunLogEntry(timestamp=now, level="ERROR", message=error, source="databricks"))
        if not entries:
            entries.append(RunLogEntry(timestamp=now, level="INFO", message="No logs reported yet.", source="databricks"))
        return entries

    async def get_run_result(
        self, platform_run_id: str, domain: str = "cluster", acelo_run_id: str | None = None
    ) -> RunResult:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.endpoint}/api/2.1/jobs/runs/get-output",
                headers=self._headers(),
                params={"run_id": platform_run_id},
            )
        resp.raise_for_status()
        data = resp.json()
        notebook_output = data.get("notebook_output", {})
        return RunResult(
            result_reference=data.get("metadata", {}).get("run_page_url"),
            payload=notebook_output or {"raw": data},
        )

    async def cancel_run(self, platform_run_id: str, domain: str = "cluster") -> ConnectionResult:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{self.endpoint}/api/2.1/jobs/runs/cancel",
                headers=self._headers(),
                json={"run_id": int(platform_run_id)},
            )
        if resp.status_code == 200:
            return ConnectionResult(ok=True, message="Cancel requested")
        return ConnectionResult(ok=False, message=f"Cancel failed: HTTP {resp.status_code}: {resp.text[:300]}")

    async def retry_run(self, domain: str, platform_run_id: str) -> StartAnalysisResult:
        # Phase 1 simplification: re-trigger the same job by name rather than using
        # the Jobs API's task-level `repair` endpoint, which needs per-task repair
        # history we don't track yet. Revisit if partial-task retry becomes required.
        return await self.start_analysis(domain)

    async def start_execution(self, action: dict[str, Any]) -> StartAnalysisResult:
        raise PlatformCapabilityNotImplemented(
            self.platform_name, "start_execution", "Execution workflow is Phase 7/8, not built yet."
        )

    async def validate_execution(self, platform_run_id: str) -> RunStatusResult:
        raise PlatformCapabilityNotImplemented(self.platform_name, "validate_execution", "Phase 8.")

    async def rollback(self, platform_run_id: str) -> ConnectionResult:
        raise PlatformCapabilityNotImplemented(self.platform_name, "rollback", "Phase 8.")
