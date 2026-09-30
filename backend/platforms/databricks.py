import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
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
from platforms import databricks_auth
from platforms.errors import ErrorCode, PlatformError, code_for_status

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # import cycle: databricks_discovery imports nothing from here at runtime
    from platforms.databricks_resources import ResourceDiscovery

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
        """
        The Authorization header for this call.

        A stored PAT wins, so every existing configured connection behaves
        exactly as before. Only when there is no stored secret does ACELO fall
        back to the Databricks App's own identity — which is how the App
        deployment authenticates without the user entering anything.
        """
        delegated_token = getattr(self, "delegated_token", None)

        if delegated_token:
            return {
            "Authorization": f"Bearer {delegated_token}",
            "Content-Type": "application/json",
        }
        if self.secret:
            return {"Authorization": f"Bearer {self.secret}", "Content-Type": "application/json"}

        app_headers = databricks_auth.app_auth_headers()
        if app_headers:
            return {**app_headers, "Content-Type": "application/json"}

        # No credential at all. _require_config() reports this as NOT_CONFIGURED
        # rather than letting an unauthenticated request reach the platform.
        return {"Content-Type": "application/json"}

    @property
    def _endpoint(self) -> str:
        """
        The workspace to call. A configured endpoint wins; otherwise the App
        runtime already knows which workspace it is in, so the user never
        supplies a URL.
        """
        return self.endpoint or (databricks_auth.app_host() or "")

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
        """
        Confirms ACELO can reach this workspace at all.

        Either half may come from the App runtime instead of stored
        configuration, so both are checked against the resolved values rather
        than the constructor arguments.
        """
        missing = []
        if not self._endpoint:
            missing.append("workspace_url")
        if not self.secret and not databricks_auth.app_auth_headers():
            missing.append("access_token")
        if missing:
            raise PlatformError(
                ErrorCode.NOT_CONFIGURED,
                "Databricks is not configured. Missing: " + ", ".join(missing) + ".",
                log_detail="missing databricks config: {}".format(missing),
            )

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = self._endpoint + path
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
            # Databricks reports failures as {"error_code": ..., "message": ...}.
            # Both are platform-generated and safe to surface; the request
            # headers (which hold the PAT) are never read here.
            platform_error_code = None
            platform_message = None
            try:
                body = resp.json()
                if isinstance(body, dict):
                    platform_error_code = body.get("error_code")
                    platform_message = body.get("message")
            except ValueError:  # non-JSON error body (HTML error page, empty)
                pass

            raise PlatformError(
                code_for_status(resp.status_code),
                log_detail="GET {} -> {}: {}".format(path, resp.status_code, resp.text[:300]),
                status_code=resp.status_code,
                platform_error_code=platform_error_code,
                platform_message=(platform_message or resp.text[:300]) or None,
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
        """
        Confirms the workspace is reachable and returns its identity.

        The cluster listing is only a reachability probe here — the workspace
        identity comes from the endpoint itself. So a 403 on that listing must
        NOT fail workspace discovery: an identity that may not list clusters can
        still read SQL warehouses, and failing here aborted the entire
        Environment Discovery before those were ever requested. A credential
        failure (401) or an unreachable host still fails, as it should.
        """
        self._require_config()
        try:
            await self._get_json("/api/2.0/clusters/list")
        except PlatformError as exc:
            if exc.code != ErrorCode.PERMISSION_DENIED:
                raise
            logger.info(
                "databricks_workspace_probe_forbidden status=%s detail=%s — continuing, "
                "workspace identity comes from the endpoint",
                exc.status_code,
                exc.log_detail,
            )
        host = urlparse(self.endpoint).hostname or self.endpoint
        return DiscoveredWorkspace(
            workspace_id=self.auth_metadata.get("workspace_id") or host,
            workspace_name=host,
            detail={"endpoint": self.endpoint},
        )

    async def discover_resources(self) -> list[DiscoveredResource]:
        """
        Lists the real clusters and jobs visible to this token.

        Each listing is independent: a token scoped to clusters but not jobs
        (or the reverse) still gets the half it may read. Previously ONE
        refused listing raised and failed the whole environment discovery with
        a single top-level "identity does not have access" error, which hid the
        resources the token could actually see. A listing is only fatal when
        every listing failed — otherwise "no access to jobs" would be
        indistinguishable from "this workspace is empty".
        """
        self._require_config()
        resources: list[DiscoveredResource] = []
        failures: list[PlatformError] = []

        try:
            clusters = await self._get_json("/api/2.0/clusters/list")
        except PlatformError as exc:
            logger.info(
                "databricks_inventory_listing_unavailable listing=clusters status=%s error_code=%s detail=%s",
                exc.status_code,
                exc.code,
                exc.log_detail,
            )
            failures.append(exc)
            clusters = {}

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
            try:
                body = await self._get_json("/api/2.1/jobs/list", params=params)
            except PlatformError as exc:
                logger.info(
                    "databricks_inventory_listing_unavailable listing=jobs status=%s error_code=%s detail=%s",
                    exc.status_code,
                    exc.code,
                    exc.log_detail,
                )
                failures.append(exc)
                break
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

        # SQL warehouses and serverless compute are part of the workspace
        # inventory too, and neither appears in clusters/list or jobs/list.
        # Without this, a workspace whose only compute is serverless plus
        # warehouses listed as EMPTY — the clusters call honestly returned zero
        # and nothing ever asked about the rest.
        #
        # Reuses the same read-only compute discovery the /api/databricks/
        # resources endpoint uses, so there is one discovery implementation.
        compute = await self.discover_compute_resources()
        from platforms.databricks_discovery import discovery_states
        from platforms.databricks_resources import ResourceType

        _INVENTORY_TYPE = {
            ResourceType.SQL_WAREHOUSE: "SQLWarehouse",
            ResourceType.SERVERLESS_COMPUTE: "ServerlessCompute",
        }
        for item in compute.resources:
            # Classic clusters already came from the clusters/list call above.
            inventory_type = _INVENTORY_TYPE.get(item.resource_type)
            if not inventory_type:
                continue
            resources.append(
                DiscoveredResource(
                    platform_resource_id=item.resource_id,
                    display_name=item.name or item.resource_id,
                    resource_type=inventory_type,
                    detail={"state": item.state, **(item.metadata or {})},
                )
            )

        # Per-type states for the caller, so "empty" is only ever reported when
        # every supported call actually returned zero. Read by
        # environment_service; carried on the adapter because discover_resources
        # returns a plain list by contract.
        self.last_discovery_states = discovery_states(compute)

        # Nothing at all could be listed: that is a real failure, not an empty
        # workspace, so the first error is surfaced rather than swallowed.
        # Only when the compute discovery found nothing either — otherwise the
        # caller has real resources to show.
        if failures and len(failures) == 2 and not resources:
            raise failures[0]

        return resources

    async def discover_compute_resources(self) -> "ResourceDiscovery":
        """
        Read-only capability discovery: classic clusters, serverless compute and
        SQL warehouses, normalised into the common ACELO resource model.

        Separate from discover_resources() above, which builds the workspace
        *inventory* (clusters + jobs) that environment setup persists. This one
        answers "what compute can this identity see, and what could it not
        look at" and creates nothing.
        """
        from platforms.databricks_discovery import discover_compute_resources

        return await discover_compute_resources(self)

    async def discover_files(self) -> list[DiscoveredResource]:
        # DBFS/Volumes enumeration is Phase 9. Declared, not faked.
        raise PlatformCapabilityNotImplemented(
            self.platform_name, "discover_files", "DBFS/Volumes file discovery is Phase 9."
        )

    # Domains ACELO can execute on Databricks. A domain outside this set is a
    # caller mistake, not a configuration gap, so it is rejected before any
    # workspace call is made.
    _EXECUTABLE_DOMAINS = frozenset(_JOB_NAME_BY_DOMAIN)

    # Domains whose execution resource MUST come from the Environment Resource
    # Registry. Resolving one of these by job name would run whichever job in
    # the workspace happens to carry that name, which is not necessarily the
    # job ACELO was registered against.
    _REGISTRY_ONLY_DOMAINS = frozenset({"cluster"})

    def _registered_job_id(self, domain: str) -> str | None:
        """
        The Databricks job the Environment Resource Registry registered for this
        domain, as "{domain}_job_id".

        The service layer flattens the registry row onto the adapter's
        auth_metadata (services/resource_registry.apply_to_adapter), so no job
        id is hardcoded here and none is chosen by guessing.
        """
        value = self.auth_metadata.get(f"{domain}_job_id")
        text = "" if value is None else str(value).strip()
        return text or None

    def _registered_job_id_as_int(self, domain: str) -> int:
        """The registered job id, as the integer the Jobs API requires."""
        registered = self._registered_job_id(domain)
        if registered is None:
            # start_analysis IS implemented - the job for this domain simply is
            # not registered. Reporting that as an unimplemented capability sent
            # users looking for missing code instead of missing configuration.
            raise PlatformError(
                ErrorCode.NOTEBOOK_NOT_CONFIGURED,
                f"No Databricks job is registered for the '{domain}' domain in this "
                f"environment. Register the job that runs the {domain} optimization "
                f"notebook in Environment Setup before starting a run.",
                log_detail=f"no {domain}_job_id in adapter metadata for workspace "
                f"{self.auth_metadata.get('workspace_id')}",
            )
        try:
            return int(registered)
        except (TypeError, ValueError):
            raise PlatformError(
                ErrorCode.INVALID_CONFIGURATION,
                f"The Databricks job registered for the '{domain}' domain is not a valid "
                f"job ID. Correct it in Environment Setup.",
                log_detail=f"{domain}_job_id is not an integer",
            ) from None

    def _registered_notebook_path(self, domain: str) -> str | None:
        """
        The workspace notebook the registry registered for this domain, as
        "{domain}_notebook_id".

        Same source as the job id: services/resource_registry.apply_to_adapter
        flattens the domain's row onto auth_metadata. No path is hardcoded here.
        """
        value = self.auth_metadata.get(f"{domain}_notebook_id")
        text = "" if value is None else str(value).strip()
        return text or None

    def _registered_existing_cluster_id(self, domain: str) -> str | None:
        """
        An existing all-purpose cluster to run a submitted notebook on, when the
        registry names one. Absent, the task carries no compute block and the
        workspace runs it on serverless jobs compute - so ACELO never invents a
        cluster id or a node type.
        """
        value = self.auth_metadata.get(f"{domain}_existing_cluster_id")
        text = "" if value is None else str(value).strip()
        return text or None

    async def _resolve_execution_target(
        self, client: httpx.AsyncClient, domain: str
    ) -> tuple[str, Any, str]:
        """
        What to run for this domain: ("job", job_id, source) or
        ("notebook", path, source).

        Precedence, highest first:
          1. a registered job id      -> Jobs API run-now
          2. a registered notebook    -> Jobs API runs/submit (one-shot run)
          3. legacy resolution by job NAME, for domains that have no
             registry-backed execution resource yet

        A domain in _REGISTRY_ONLY_DOMAINS never reaches step 3: it fails with a
        configuration error rather than running an unrelated job. This is what
        lets a freshly installed App run the registered notebook without an
        administrator first creating a Job by hand.
        """
        # A job id that is present but malformed is an error, never a reason to
        # silently fall through to the notebook.
        if self._registered_job_id(domain):
            return "job", self._registered_job_id_as_int(domain), "registry"

        notebook_path = self._registered_notebook_path(domain)
        if notebook_path:
            return "notebook", notebook_path, "registry"

        if domain in self._REGISTRY_ONLY_DOMAINS:
            raise PlatformError(
                ErrorCode.NOTEBOOK_NOT_CONFIGURED,
                f"No Databricks job or notebook is registered for the '{domain}' domain "
                f"in this environment. Register the {domain} optimization notebook (or a "
                f"job that runs it) in Environment Setup before starting a run.",
                log_detail=f"neither {domain}_job_id nor {domain}_notebook_id in adapter "
                f"metadata for workspace {self.auth_metadata.get('workspace_id')}",
            )

        job_name = _JOB_NAME_BY_DOMAIN.get(domain)
        if not job_name:
            raise ValueError(f"No Databricks job mapping for domain '{domain}'.")

        resp = await client.get(f"{self.endpoint}/api/2.1/jobs/list", headers=self._headers(), params={"limit": 100})
        resp.raise_for_status()
        jobs = resp.json().get("jobs", [])
        for job in jobs:
            settings = job.get("settings", {})
            if settings.get("name") == job_name:
                return "job", job["job_id"], "name"

        raise LookupError(
            f"No Databricks job named '{job_name}' was found in this workspace. "
            f"The '{domain}' optimization requires a job with that exact name to already exist."
        )

    def _submit_body(self, domain: str, notebook_path: str) -> dict[str, Any]:
        """
        The runs/submit payload for a one-shot notebook run.

        Compute: an existing cluster when the registry names one, otherwise no
        compute block at all, which runs the task on the workspace's serverless
        jobs compute. Either way no cluster id, node type or runtime version is
        invented here.
        """
        task: dict[str, Any] = {
            "task_key": f"acelo_{domain}_optimization",
            "notebook_task": {"notebook_path": notebook_path},
        }
        existing_cluster_id = self._registered_existing_cluster_id(domain)
        if existing_cluster_id:
            task["existing_cluster_id"] = existing_cluster_id
        return {
            "run_name": f"ACELO {domain} optimization",
            "tasks": [task],
        }

    async def start_analysis(
        self, domain: str, parameters: dict[str, Any] | None = None
    ) -> StartAnalysisResult:
        """
        Starts this domain's analysis on Databricks and returns the platform's
        own run id.

        A registered JOB runs through run-now. A registered NOTEBOOK with no job
        runs through runs/submit as a one-shot run - the same Jobs API, so the
        run id it returns is monitored by the existing runs/get polling with no
        second execution framework and no Job object to create first.

        Parameters: the registered notebook's configuration is fixed in the
        notebook itself - it declares no dbutils widgets, so it can consume no
        run-now or runs/submit parameter. Sending notebook_params it cannot read
        would assert a contract that does not exist, so ACELO's runtime
        parameters are recorded on the result for diagnosis and withheld from
        the payload.
        """
        if domain not in self._EXECUTABLE_DOMAINS:
            raise PlatformError(
                ErrorCode.UNSUPPORTED,
                f"ACELO does not run a '{domain}' analysis on Databricks.",
                log_detail=f"unknown domain '{domain}' for databricks start_analysis",
            )

        async with httpx.AsyncClient(timeout=30) as client:
            kind, target, target_source = await self._resolve_execution_target(client, domain)
            if kind == "notebook":
                path = f"{self.endpoint}/api/2.1/jobs/runs/submit"
                body = self._submit_body(domain, target)
            else:
                path = f"{self.endpoint}/api/2.1/jobs/run-now"
                body = {"job_id": target}
            resp = await client.post(path, headers=self._headers(), json=body)
        resp.raise_for_status()
        # The run id is Databricks' own. A run is reported as started only
        # because the platform issued one - it is never generated here.
        run_id = resp.json()["run_id"]
        logger.info(
            "databricks_run_started domain=%s execution_kind=%s target=%s source=%s run_id=%s",
            domain, kind, target, target_source, run_id,
        )
        detail: dict[str, Any] = {
            "execution_kind": kind,
            "job_source": target_source,
            # Built by the registry, deliberately not sent: see the note above.
            "parameters_withheld": sorted(parameters or {}),
        }
        if kind == "notebook":
            # job_service records detail["item_id"] as the resource that ran, so
            # a submitted run still names the notebook it executed.
            detail["notebook_path"] = target
            detail["item_id"] = target
        else:
            detail["job_id"] = target
        return StartAnalysisResult(
            platform_run_id=str(run_id),
            status="STARTING",
            detail=detail,
        )

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
