import asyncio
import base64
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
import msal

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

# Runtime trace for real executions. Tagged lines ([FABRIC_EXECUTION_REQUEST],
# [FABRIC_EXECUTION_RESPONSE], [FABRIC_EXECUTION_FAILED]) let an operator prove
# what ACELO sent and what Fabric answered. IDs, table and lakehouse names only.
trace_logger = logging.getLogger("acelo.fabric.trace")

# Parameter names whose VALUES are never logged — they point at credentials.
SENSITIVE_PARAMETERS = frozenset({"llm_key_vault_uri", "llm_secret_name"})


def redact_parameters(parameters: dict[str, Any] | None) -> dict[str, str]:
    """Parameters as they may be logged: credential pointers reduced to <set>."""
    return {
        name: "<set>" if name in SENSITIVE_PARAMETERS else str(value)
        for name, value in sorted((parameters or {}).items())
    }


def trace(tag: str, **fields: Any) -> None:
    """One structured, grep-able trace event: `[TAG] key=value ...`."""
    rendered = " ".join(
        f"{k}={json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else v}"
        for k, v in fields.items()
    )
    trace_logger.info("[%s] %s", tag, rendered)


SESSION_EXPIRED_MESSAGE = "Your Microsoft Fabric session has expired. Please sign in again."

# Result retrieval over the Lakehouse SQL analytics endpoint.
ODBC_DRIVER = "ODBC Driver 18 for SQL Server"
SQL_COPT_SS_ACCESS_TOKEN = 1256  # msodbcsql pre-connect attribute for an Entra access token


ONELAKE_SCOPE = "https://storage.azure.com/.default"


def read_delta_rows(
    uri: str, storage_options: dict[str, str], filters: list[tuple] | None = None
) -> list[dict[str, Any]]:
    """Reads a Delta table (local path or OneLake ABFS URI), optionally filtered."""
    from deltalake import DeltaTable

    table = DeltaTable(uri, storage_options=storage_options or None)
    return table.to_pyarrow_table(filters=filters or None).to_pylist()


def _classify_delta_error(exc: Exception, table: str) -> tuple[str, str]:
    text = f"{type(exc).__name__} {exc}".lower()
    if "tablenotfound" in text or "no files in log segment" in text or "not a delta table" in text or "404" in text:
        return (
            ErrorCode.RESOURCE_NOT_FOUND,
            f"The Delta table '{table}' was not found in the Lakehouse. Run the ACELO Cluster "
            "pipeline (it creates the tracking table), or check the table name in Cluster Settings.",
        )
    if "403" in text or "forbidden" in text or "authorizationpermissionmismatch" in text:
        return (
            ErrorCode.PERMISSION_DENIED,
            f"The signed-in account cannot read '{table}' in OneLake. It needs at least Viewer "
            "access to the workspace that holds the Lakehouse.",
        )
    if "401" in text or "unauthorized" in text or "invalidauthenticationinfo" in text:
        return (
            ErrorCode.AUTHENTICATION_FAILED,
            "OneLake rejected the sign-in. Sign in again to Microsoft Fabric.",
        )
    return (ErrorCode.RESULT_RETRIEVAL_FAILED, f"The Delta table '{table}' could not be read from OneLake.")


class ResultConfigurationError(PlatformError, ValueError):
    """Result read impossible because configuration is missing. Typed for the UI,
    and still a ValueError for callers that already handle it as one."""


def _odbc_token_struct(token: str) -> bytes:
    """Entra access token in the layout msodbcsql expects: UTF-16-LE, length-prefixed."""
    import struct

    encoded = token.encode("utf-16-le")
    return struct.pack(f"<I{len(encoded)}s", len(encoded), encoded)

FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
FABRIC_API_BASE = "https://api.fabric.microsoft.com/v1"
# Item creation is a long-running operation; bound how long we chase it.
FABRIC_OPERATION_MAX_POLLS = 30

# Fabric Job Scheduler status -> our JobStatus.
# https://learn.microsoft.com/rest/api/fabric/core/job-scheduler/get-item-job-instance
_FABRIC_STATUS_MAP = {
    "NotStarted": "QUEUED",
    "InProgress": "RUNNING",
    "Completed": "COMPLETED",
    "Failed": "FAILED",
    "Cancelled": "CANCELLED",
    "Deduped": "CANCELLED",  # Fabric returned an existing identical run instead of starting a new one
}


def extract_job_instance_id(location: str | None) -> str | None:
    """
    Pulls the job instance ID out of Fabric's Location header.

    Uses a path-aware regex rather than splitting on "/" because the header can
    carry a query string (e.g. ".../instances/{id}?api-version=..."), which the
    naive split would fold into the ID and silently break status polling.
    Same approach as the ElevateDM reference implementation.
    """
    if not location:
        return None
    path = urlparse(location).path
    match = re.search(r"/instances/([^/?]+)", path)
    return match.group(1) if match else None


class FabricAdapter(PlatformAdapter):
    """
    Auth: Azure AD (Entra ID) service principal, client-credentials flow.

    auth_metadata must contain:
      tenant_id           - Azure AD tenant GUID
      client_id            - App registration (service principal) client ID
      workspace_id          - Fabric workspace GUID
      notebook_item_id       - GUID of the existing Cluster-analysis notebook item
      sql_endpoint           - Lakehouse SQL analytics endpoint host, e.g.
                                "xxxxx.datawarehouse.fabric.microsoft.com"
      lakehouse_database      - the Database= value for that SQL endpoint (the Lakehouse name)

    `secret` is the Azure AD app's client secret.

    Setup required in the customer's Azure AD tenant / Fabric workspace, once:
      1. App registration with a client secret.
      2. Fabric Admin Portal: enable "Service principals can use Fabric APIs".
      3. Add the service principal to the workspace (Contributor or higher) so it
         can both call the Job Scheduler API and query the SQL analytics endpoint.

    Phase 2 status: Cluster analysis is real end-to-end (start/status/logs/result).
    Query and Storage analysis remain PlatformCapabilityNotImplemented — Phase 3+.
    Execution (start_execution/validate_execution/rollback) remains
    PlatformCapabilityNotImplemented — Phase 7/8, deliberately out of scope here.
    """

    platform_name = "fabric"

    # A delegated (user) access token, supplied per request by the frontend when
    # the environment uses "personal" authentication. It is never persisted and
    # never logged — see api/environments.py.
    delegated_token: str | None = None

    # True when the environment is configured for Microsoft Account (delegated)
    # sign-in. Such an environment has no service-principal credentials, so a
    # missing delegated token must fail as an expired session - it must NEVER
    # fall through to the client-credentials flow and its client_id lookup.
    delegated_mode: bool = False

    # Delegated token for the Lakehouse SQL endpoint (audience database.windows.net),
    # supplied per request for result reads only. Never persisted or logged.
    sql_token: str | None = None

    # Delegated OneLake token (audience storage.azure.com), supplied per request
    # for direct Delta reads (approval tracking). Never persisted or logged.
    onelake_token: str | None = None

    # The existing Cluster notebook writes its output here — see the Job Scheduler
    # run trigger below. Kept as a named constant so it's the one place to change
    # if the table location ever moves.
    CLUSTER_RESULT_TABLE = "data_Demo.acelo_cluster_optimization_results"

    def _tenant_id(self) -> str:
        tenant_id = self.auth_metadata.get("tenant_id")
        if not tenant_id:
            raise ValueError("Fabric connection is missing tenant_id in auth_metadata.")
        return tenant_id

    def _client_id(self) -> str:
        client_id = self.auth_metadata.get("client_id")
        if not client_id:
            raise ValueError("Fabric connection is missing client_id in auth_metadata.")
        return client_id

    def _workspace_id(self) -> str:
        workspace_id = self.auth_metadata.get("workspace_id")
        if not workspace_id:
            raise ValueError("Fabric connection is missing workspace_id in auth_metadata.")
        return workspace_id

    def _notebook_item_id(self) -> str:
        item_id = self.auth_metadata.get("notebook_item_id")
        if not item_id:
            raise ValueError("Fabric connection is missing notebook_item_id in auth_metadata.")
        return item_id

    def _acquire_token(self) -> tuple[str | None, str | None]:
        # Delegated mode: the user already signed in through the browser and the
        # frontend passed their Fabric-scoped token in. There is no client secret
        # to exchange, so this short-circuits the confidential-client flow.
        if self.delegated_token:
            return self.delegated_token, None
        if self.delegated_mode:
            # No service-principal fallback for delegated environments.
            return None, "delegated environment: no Microsoft Fabric token supplied with the request"

        try:
            app = msal.ConfidentialClientApplication(
                client_id=self._client_id(),
                client_credential=self.secret,
                authority=f"https://login.microsoftonline.com/{self._tenant_id()}",
            )
            result = app.acquire_token_for_client(scopes=[FABRIC_SCOPE])
        except Exception as exc:  # noqa: BLE001 - MSAL raises on authority/discovery failure (bad tenant_id,
            # unreachable network, malformed config) instead of returning an error dict like it does for
            # auth failures. Without this, a bad tenant_id crashes every caller instead of failing cleanly.
            return None, str(exc)

        if "access_token" in result:
            return result["access_token"], None
        return None, result.get("error_description", "Unknown Azure AD error")

    # --- connection (unchanged from Phase 1 — already real) ---

    async def connect(self) -> ConnectionResult:
        return await self.test_connection()

    async def test_connection(self) -> ConnectionResult:
        token, error = self._acquire_token()
        if not token:
            return ConnectionResult(ok=False, message=f"Azure AD sign-in failed: {error}")

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{FABRIC_API_BASE}/workspaces",
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.RequestError as exc:
            return ConnectionResult(ok=False, message=f"Could not reach Fabric API: {exc}")

        if resp.status_code == 200:
            workspaces = resp.json().get("value", [])
            return ConnectionResult(ok=True, message="Connected", detail={"workspace_count": len(workspaces)})
        if resp.status_code in (401, 403):
            return ConnectionResult(
                ok=False,
                message=(
                    "Authentication succeeded with Azure AD, but Fabric rejected the request. "
                    "Confirm the service principal is enabled for Fabric APIs and has workspace access."
                ),
            )
        return ConnectionResult(ok=False, message=f"Fabric returned HTTP {resp.status_code}: {resp.text[:300]}")

    async def get_environment(self) -> dict[str, Any]:
        token, error = self._acquire_token()
        if not token:
            raise RuntimeError(f"Azure AD sign-in failed: {error}")
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{FABRIC_API_BASE}/workspaces", headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
        workspaces = resp.json().get("value", [])
        return {"platform": "Microsoft Fabric", "workspace_count": len(workspaces)}

    # --- Phase 1: connection validation & environment discovery ---
    # Read-only. Nothing here triggers a notebook or modifies the workspace.

    # Fabric item types -> ACELO normalised resource_type vocabulary.
    _ITEM_TYPE_MAP = {
        "Notebook": "Notebook",
        "Lakehouse": "Lakehouse",
        "SQLEndpoint": "SQLEndpoint",
        "SQLAnalyticsEndpoint": "SQLEndpoint",
        "MLExperiment": "Experiment",
        "Warehouse": "Warehouse",
        "SemanticModel": "SemanticModel",
        "Report": "Report",
        "DataPipeline": "DataPipeline",
        "KQLDatabase": "KQLDatabase",
        "Environment": "SparkEnvironment",
    }

    def _require_config(self) -> tuple[str, str, str, str]:
        """Validates configuration up front so a misconfigured environment fails
        as INVALID_CONFIGURATION rather than as a confusing auth error."""
        if self.delegated_token or self.delegated_mode:
            # Delegated mode carries its own identity; only the workspace is needed.
            # A missing token surfaces from _token_or_raise as an expired session.
            missing = [] if self.auth_metadata.get("workspace_id") else ["workspace_id"]
            if missing:
                raise PlatformError(
                    ErrorCode.INVALID_CONFIGURATION,
                    "Fabric environment is missing required configuration: workspace_id.",
                    log_detail="missing fabric config keys: ['workspace_id']",
                )
            return ("", "", self.auth_metadata["workspace_id"], "")

        missing = [
            key
            for key in ("tenant_id", "client_id", "workspace_id")
            if not self.auth_metadata.get(key)
        ]
        if not self.secret:
            missing.append("client_secret")
        if missing:
            raise PlatformError(
                ErrorCode.INVALID_CONFIGURATION,
                "Fabric environment is missing required configuration: " + ", ".join(missing) + ".",
                log_detail="missing fabric config keys: {}".format(missing),
            )
        return (
            self.auth_metadata["tenant_id"],
            self.auth_metadata["client_id"],
            self.auth_metadata["workspace_id"],
            self.secret,
        )

    def _token_or_raise(self) -> str:
        token, error = self._acquire_token()
        if not token:
            raise PlatformError(
                ErrorCode.AUTHENTICATION_FAILED,
                SESSION_EXPIRED_MESSAGE if self.delegated_mode else None,
                log_detail="Azure AD sign-in failed: {}".format(error),
            )
        return token

    async def _get(self, url: str, token: str) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                return await client.get(url, headers={"Authorization": "Bearer " + token})
        except httpx.TimeoutException as exc:
            raise PlatformError(ErrorCode.TIMEOUT, log_detail="timeout calling {}: {}".format(url, exc)) from exc
        except httpx.RequestError as exc:
            raise PlatformError(
                ErrorCode.PLATFORM_API_UNAVAILABLE, log_detail="request error calling {}: {}".format(url, exc)
            ) from exc

    async def _fetch_workspace(self, token: str, workspace_id: str) -> dict[str, Any]:
        """Real GET /v1/workspaces/{id}. Raises a mapped PlatformError on failure."""
        resp = await self._get(FABRIC_API_BASE + "/workspaces/" + workspace_id, token)
        if resp.status_code == 200:
            return resp.json()

        code = code_for_status(resp.status_code)
        if code == ErrorCode.PERMISSION_DENIED:
            message = (
                "Fabric authentication succeeded, but the configured identity does not have "
                "access to this workspace. Add the service principal to the workspace, and make "
                "sure 'Service principals can use Fabric APIs' is enabled for the tenant."
            )
        elif code == ErrorCode.WORKSPACE_NOT_FOUND:
            message = "The configured Fabric workspace could not be found. Verify the workspace ID."
        else:
            message = None
        raise PlatformError(
            code,
            message,
            log_detail="GET /workspaces/{} -> {}: {}".format(workspace_id, resp.status_code, resp.text[:300]),
        )

    async def validate_connection(self) -> ConnectionValidation:
        """
        Real end-to-end check: config present -> Azure AD token -> the specific
        configured workspace is readable. Only returns connected=True when all
        three actually succeed against the live Fabric API.
        """
        try:
            _, _, workspace_id, _ = self._require_config()
            token = self._token_or_raise()
            workspace = await self._fetch_workspace(token, workspace_id)
        except PlatformError as exc:
            return ConnectionValidation(connected=False, message=exc.message, error_code=exc.code)

        return ConnectionValidation(
            connected=True,
            message="Fabric connection verified",
            workspace_id=workspace.get("id", workspace_id),
            workspace_name=workspace.get("displayName") or workspace_id,
        )

    async def discover_workspace(self) -> DiscoveredWorkspace:
        _, _, workspace_id, _ = self._require_config()
        token = self._token_or_raise()
        workspace = await self._fetch_workspace(token, workspace_id)
        return DiscoveredWorkspace(
            workspace_id=workspace.get("id", workspace_id),
            workspace_name=workspace.get("displayName") or workspace_id,
            detail={
                "description": workspace.get("description"),
                "capacity_id": workspace.get("capacityId"),
                "type": workspace.get("type"),
            },
        )

    async def discover_resources(self) -> list[DiscoveredResource]:
        """
        Real GET /v1/workspaces/{id}/items, following continuationToken so large
        workspaces are not silently truncated. Also lists real folders where the
        tenant exposes the (newer) folders endpoint - absence of that endpoint is
        not treated as a discovery failure.
        """
        _, _, workspace_id, _ = self._require_config()
        token = self._token_or_raise()

        resources: list[DiscoveredResource] = []
        url = FABRIC_API_BASE + "/workspaces/" + workspace_id + "/items"
        seen_tokens: set[str] = set()

        while url:
            resp = await self._get(url, token)
            if resp.status_code != 200:
                raise PlatformError(
                    code_for_status(resp.status_code),
                    log_detail="GET items -> {}: {}".format(resp.status_code, resp.text[:300]),
                )
            body = resp.json()
            for item in body.get("value", []):
                raw_type = item.get("type", "Unknown")
                resources.append(
                    DiscoveredResource(
                        platform_resource_id=item.get("id", ""),
                        display_name=item.get("displayName") or item.get("id", ""),
                        resource_type=self._ITEM_TYPE_MAP.get(raw_type, raw_type),
                        detail={
                            "description": item.get("description"),
                            "fabric_type": raw_type,
                            "folder_id": item.get("folderId"),
                        },
                    )
                )
            continuation = body.get("continuationToken")
            # Guard against a server that keeps handing back the same token.
            if continuation and continuation not in seen_tokens:
                seen_tokens.add(continuation)
                url = FABRIC_API_BASE + "/workspaces/" + workspace_id + "/items?continuationToken=" + continuation
            else:
                url = ""

        resources.extend(await self._discover_folders(token, workspace_id))
        return resources

    async def _discover_folders(self, token: str, workspace_id: str) -> list[DiscoveredResource]:
        """Folders are a newer Fabric API and not available in every tenant.
        A non-200 here means this tenant has no folders endpoint, which is not
        an error - so it degrades to an empty list rather than failing discovery."""
        try:
            resp = await self._get(FABRIC_API_BASE + "/workspaces/" + workspace_id + "/folders", token)
        except PlatformError:
            return []
        if resp.status_code != 200:
            return []
        return [
            DiscoveredResource(
                platform_resource_id=folder.get("id", ""),
                display_name=folder.get("displayName") or folder.get("id", ""),
                resource_type="Folder",
                detail={"parent_folder_id": folder.get("parentFolderId")},
            )
            for folder in resp.json().get("value", [])
        ]

    async def discover_files(self) -> list[DiscoveredResource]:
        # OneLake file/table enumeration is Phase 9. Declared, not faked.
        raise PlatformCapabilityNotImplemented(
            self.platform_name, "discover_files", "OneLake file discovery is Phase 9."
        )
    # --- Step 3: ACELO package provisioning (real Fabric item/folder APIs) ---
    #
    # Creation only. Nothing here deletes or overwrites a customer-owned item:
    # creation is skipped when an item of the same name already exists, and an
    # update targets only items ACELO itself previously created and registered.

    async def _request(
        self, method: str, url: str, token: str, json_body: dict | None = None, timeout: float = 60
    ) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                return await client.request(
                    method, url, headers={"Authorization": f"Bearer {token}"}, json=json_body
                )
        except httpx.TimeoutException as exc:
            raise PlatformError(ErrorCode.TIMEOUT, log_detail=f"timeout {method} {url}: {exc}") from exc
        except httpx.RequestError as exc:
            raise PlatformError(
                ErrorCode.PLATFORM_API_UNAVAILABLE, log_detail=f"network error {method} {url}: {exc}"
            ) from exc

    async def _await_operation(self, resp: httpx.Response, token: str) -> dict[str, Any]:
        """
        Fabric item creation is a long-running operation: 201 returns the item
        directly, 202 returns an operation URL that must be polled. Returning the
        202 body as if it were the item would leave us with no real item ID, so
        this resolves the operation before anyone sees a result.
        """
        if resp.status_code in (200, 201):
            return resp.json() if resp.text else {}

        operation_url = resp.headers.get("Location")
        if not operation_url:
            raise PlatformError(
                ErrorCode.PROVISIONING_FAILED,
                "Fabric accepted the request but returned no operation to track.",
                log_detail=f"202 without Location header: {resp.text[:300]}",
            )

        retry_after = float(resp.headers.get("Retry-After") or 2)
        for _ in range(FABRIC_OPERATION_MAX_POLLS):
            await asyncio.sleep(min(retry_after, 10))
            poll = await self._request("GET", operation_url, token)
            if poll.status_code != 200:
                raise PlatformError(
                    code_for_status(poll.status_code),
                    log_detail=f"operation poll -> {poll.status_code}: {poll.text[:300]}",
                )
            body = poll.json() if poll.text else {}
            state = (body.get("status") or "").lower()
            if state == "succeeded":
                # The completed operation's payload lives at /result for item LROs.
                result = await self._request("GET", operation_url.rstrip("/") + "/result", token)
                if result.status_code == 200 and result.text:
                    return result.json()
                return body
            if state in ("failed", "undefined"):
                raise PlatformError(
                    ErrorCode.PROVISIONING_FAILED,
                    "Fabric reported the item operation as failed.",
                    log_detail=f"operation failed: {json.dumps(body)[:400]}",
                )

        raise PlatformError(
            ErrorCode.TIMEOUT,
            "Fabric did not finish creating the item in time.",
            log_detail=f"operation still pending after {FABRIC_OPERATION_MAX_POLLS} polls",
        )

    async def list_folders(self) -> list[dict[str, Any]]:
        """Real folder listing. Returns [] when the tenant has no folders API,
        which is a capability gap rather than an error."""
        _, _, workspace_id, _ = self._require_config()
        token = self._token_or_raise()
        resp = await self._request("GET", f"{FABRIC_API_BASE}/workspaces/{workspace_id}/folders", token)
        if resp.status_code != 200:
            return []
        return resp.json().get("value", [])

    async def ensure_folder(self, display_name: str, parent_folder_id: str | None = None) -> dict[str, Any]:
        """
        Creates a workspace folder, or returns the existing one with that name.

        Folders are a newer Fabric API and are not enabled in every tenant. When
        unavailable this returns {"supported": False} and the caller falls back to
        flat, name-prefixed items — it never reports a folder it did not create.
        """
        _, _, workspace_id, _ = self._require_config()
        token = self._token_or_raise()

        for folder in await self.list_folders():
            if folder.get("displayName") == display_name and (
                parent_folder_id is None or folder.get("parentFolderId") == parent_folder_id
            ):
                return {"supported": True, "created": False, "id": folder.get("id"), "displayName": display_name}

        body: dict[str, Any] = {"displayName": display_name}
        if parent_folder_id:
            body["parentFolderId"] = parent_folder_id

        resp = await self._request(
            "POST", f"{FABRIC_API_BASE}/workspaces/{workspace_id}/folders", token, json_body=body
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            return {"supported": True, "created": True, "id": data.get("id"), "displayName": display_name}
        if resp.status_code in (404, 501):
            # Tenant/API does not expose folders at all.
            return {"supported": False, "created": False, "id": None, "displayName": display_name}
        if resp.status_code == 403:
            raise PlatformError(
                ErrorCode.PERMISSION_DENIED,
                "The configured identity cannot create folders in this workspace. "
                "Contributor access or higher is required.",
                log_detail=f"create folder -> 403: {resp.text[:300]}",
            )
        raise PlatformError(
            code_for_status(resp.status_code),
            log_detail=f"create folder '{display_name}' -> {resp.status_code}: {resp.text[:300]}",
        )

    async def find_notebook_by_name(self, display_name: str) -> dict[str, Any] | None:
        """Looks for an existing notebook so provisioning never blindly overwrites
        an item a customer already owns."""
        for resource in await self.discover_resources():
            if resource.resource_type == "Notebook" and resource.display_name == display_name:
                return {"id": resource.platform_resource_id, "displayName": resource.display_name}
        return None

    @staticmethod
    def _definition(payload_base64: str) -> dict[str, Any]:
        """Fabric notebook item definition: the .ipynb as a single base64 part."""
        return {
            "format": "ipynb",
            "parts": [
                {
                    "path": "notebook-content.ipynb",
                    "payload": payload_base64,
                    "payloadType": "InlineBase64",
                }
            ],
        }

    async def create_notebook(
        self, display_name: str, payload_base64: str, folder_id: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Creates a REAL Fabric notebook and returns its REAL item ID."""
        _, _, workspace_id, _ = self._require_config()
        token = self._token_or_raise()

        body: dict[str, Any] = {
            "displayName": display_name,
            "definition": self._definition(payload_base64),
        }
        if description:
            body["description"] = description
        if folder_id:
            body["folderId"] = folder_id

        resp = await self._request(
            "POST", f"{FABRIC_API_BASE}/workspaces/{workspace_id}/notebooks", token, json_body=body
        )
        if resp.status_code == 403:
            raise PlatformError(
                ErrorCode.PERMISSION_DENIED,
                "The configured identity cannot create notebooks in this workspace. "
                "Contributor access or higher is required.",
                log_detail=f"create notebook -> 403: {resp.text[:300]}",
            )
        if resp.status_code not in (200, 201, 202):
            raise PlatformError(
                code_for_status(resp.status_code),
                log_detail=f"create notebook '{display_name}' -> {resp.status_code}: {resp.text[:300]}",
            )

        item = await self._await_operation(resp, token)
        item_id = item.get("id")
        if not item_id:
            raise PlatformError(
                ErrorCode.PROVISIONING_FAILED,
                "Fabric created the notebook but did not return its item ID.",
                log_detail=f"create notebook response without id: {json.dumps(item)[:300]}",
            )
        return {"id": item_id, "displayName": item.get("displayName", display_name)}

    async def update_notebook_definition(self, item_id: str, payload_base64: str) -> None:
        """Updates an ACELO-owned notebook in place. Callers must only pass item
        IDs ACELO itself created and registered."""
        _, _, workspace_id, _ = self._require_config()
        token = self._token_or_raise()

        resp = await self._request(
            "POST",
            f"{FABRIC_API_BASE}/workspaces/{workspace_id}/notebooks/{item_id}/updateDefinition",
            token,
            json_body={"definition": self._definition(payload_base64)},
        )
        if resp.status_code not in (200, 202):
            raise PlatformError(
                code_for_status(resp.status_code),
                log_detail=f"update notebook {item_id} -> {resp.status_code}: {resp.text[:300]}",
            )
        if resp.status_code == 202:
            await self._await_operation(resp, token)

    # --- Data Pipeline (orchestrates the SAME Cluster notebook) ---------------

    # Parameters the pipeline accepts and forwards verbatim to the notebook's
    # parameters cell. Mirrors job_service's notebook parameters.
    PIPELINE_PARAMETERS = (
        "acelo_run_id",
        "environment_id",
        "source_table",
        "result_table",
        "source_lakehouse",
        "result_lakehouse",
        "source_schema",
        "result_schema",
        "column_mapping",
        "approval_tracking_table",
        "model_dir",
        "llm_key_vault_uri",
        "llm_secret_name",
        "llm_model_name",
    )
    PIPELINE_NOTEBOOK_ACTIVITY = "Run ACELO Cluster Notebook"
    PIPELINE_TRACKING_ACTIVITY = "Update ACELO Approval Tracking"
    # What the tracking notebook needs, forwarded from the same pipeline parameters.
    TRACKING_PARAMETERS = ("acelo_run_id", "result_table", "result_schema", "approval_tracking_table")

    @classmethod
    def _notebook_activity(cls, name: str, notebook_id: str, workspace_id: str, parameters, depends_on=None):
        return {
            "name": name,
            "type": "TridentNotebook",
            "dependsOn": depends_on or [],
            "policy": {
                "timeout": "0.12:00:00",
                "retry": 0,
                "retryIntervalInSeconds": 30,
                "secureOutput": False,
                "secureInput": False,
            },
            "typeProperties": {
                "notebookId": notebook_id,
                "workspaceId": workspace_id,
                "parameters": {
                    name: {
                        "value": {"value": f"@pipeline().parameters.{name}", "type": "Expression"},
                        "type": "string",
                    }
                    for name in parameters
                },
            },
        }

    @classmethod
    def pipeline_definition(
        cls, notebook_id: str, workspace_id: str, tracking_notebook_id: str | None = None
    ) -> dict[str, Any]:
        """
        pipeline-content.json for a pipeline with ONE Notebook activity that runs
        the existing ACELO Cluster notebook. Every pipeline parameter is passed
        through to the notebook parameter of the same name, so the notebook
        receives exactly what ACELO sent. Nothing is hardcoded but the ids passed in.
        """
        activities = [
            cls._notebook_activity(cls.PIPELINE_NOTEBOOK_ACTIVITY, notebook_id, workspace_id, cls.PIPELINE_PARAMETERS)
        ]
        if tracking_notebook_id:
            # Runs only after the Cluster notebook SUCCEEDED; the Cluster step is unchanged.
            activities.append(
                cls._notebook_activity(
                    cls.PIPELINE_TRACKING_ACTIVITY, tracking_notebook_id, workspace_id, cls.TRACKING_PARAMETERS,
                    depends_on=[{"activity": cls.PIPELINE_NOTEBOOK_ACTIVITY, "dependencyConditions": ["Succeeded"]}],
                )
            )
        content = {
            "properties": {
                "activities": activities,
                "parameters": {
                    name: {"type": "string", "defaultValue": ""} for name in cls.PIPELINE_PARAMETERS
                },
            }
        }
        payload = base64.b64encode(json.dumps(content).encode("utf-8")).decode()
        return {
            "parts": [
                {"path": "pipeline-content.json", "payload": payload, "payloadType": "InlineBase64"}
            ]
        }

    async def find_item_by_name(self, item_type: str, display_name: str) -> dict[str, Any] | None:
        """An existing item of this normalised type and exact name, so nothing is duplicated."""
        for resource in await self.discover_resources():
            if resource.resource_type == item_type and resource.display_name == display_name:
                return {"id": resource.platform_resource_id, "displayName": resource.display_name}
        return None

    async def create_item(
        self, item_type: str, display_name: str, definition: dict[str, Any],
        folder_id: str | None = None, description: str | None = None,
    ) -> dict[str, Any]:
        """Creates a REAL Fabric item (e.g. DataPipeline) and returns its REAL id."""
        _, _, workspace_id, _ = self._require_config()
        token = self._token_or_raise()
        body: dict[str, Any] = {"displayName": display_name, "type": item_type, "definition": definition}
        if description:
            body["description"] = description
        if folder_id:
            body["folderId"] = folder_id

        resp = await self._request("POST", f"{FABRIC_API_BASE}/workspaces/{workspace_id}/items", token, json_body=body)
        if resp.status_code == 403:
            raise PlatformError(
                ErrorCode.PERMISSION_DENIED,
                f"The signed-in identity cannot create a {item_type} in this workspace.",
                log_detail=f"create {item_type} -> 403: {resp.text[:300]}",
            )
        if resp.status_code not in (200, 201, 202):
            raise PlatformError(
                code_for_status(resp.status_code),
                log_detail=f"create {item_type} '{display_name}' -> {resp.status_code}: {resp.text[:300]}",
            )
        item = await self._await_operation(resp, token)
        if not item.get("id"):
            raise PlatformError(
                ErrorCode.PROVISIONING_FAILED,
                f"Fabric created the {item_type} but did not return its item ID.",
                log_detail=f"create {item_type} response without id: {json.dumps(item)[:300]}",
            )
        return {"id": item["id"], "displayName": item.get("displayName", display_name)}

    async def update_item_definition(self, item_id: str, definition: dict[str, Any]) -> None:
        """Replaces the definition of an ACELO-owned item in place."""
        _, _, workspace_id, _ = self._require_config()
        token = self._token_or_raise()
        resp = await self._request(
            "POST",
            f"{FABRIC_API_BASE}/workspaces/{workspace_id}/items/{item_id}/updateDefinition",
            token,
            json_body={"definition": definition},
        )
        if resp.status_code not in (200, 202):
            raise PlatformError(
                code_for_status(resp.status_code),
                log_detail=f"update item {item_id} -> {resp.status_code}: {resp.text[:300]}",
            )
        if resp.status_code == 202:
            await self._await_operation(resp, token)

    async def get_item(self, item_id: str) -> dict[str, Any]:
        """Post-provision verification: confirms the item really exists and is
        readable with the configured identity."""
        _, _, workspace_id, _ = self._require_config()
        token = self._token_or_raise()

        resp = await self._request("GET", f"{FABRIC_API_BASE}/workspaces/{workspace_id}/items/{item_id}", token)
        if resp.status_code == 404:
            raise PlatformError(
                ErrorCode.RESOURCE_NOT_FOUND,
                "The deployed item could not be found in the workspace.",
                log_detail=f"get item {item_id} -> 404",
            )
        if resp.status_code != 200:
            raise PlatformError(
                code_for_status(resp.status_code),
                log_detail=f"get item {item_id} -> {resp.status_code}: {resp.text[:300]}",
            )
        return resp.json()

    async def notebook_definition_exists(self, item_id: str) -> bool:
        """Verifies the notebook actually carries a definition — an item can exist
        while its content failed to upload."""
        _, _, workspace_id, _ = self._require_config()
        token = self._token_or_raise()

        resp = await self._request(
            "POST", f"{FABRIC_API_BASE}/workspaces/{workspace_id}/notebooks/{item_id}/getDefinition", token
        )
        if resp.status_code == 202:
            try:
                body = await self._await_operation(resp, token)
            except PlatformError:
                return False
            return bool((body.get("definition") or {}).get("parts"))
        if resp.status_code != 200:
            return False
        body = resp.json() if resp.text else {}
        return bool((body.get("definition") or {}).get("parts"))

    async def get_notebook_dependencies(self, item_id: str) -> dict[str, Any]:
        """
        The `metadata.dependencies` (attached lakehouse / Environment) of the
        notebook as it exists in Fabric right now. Used so a redeploy never
        silently strips a binding someone configured in Fabric. {} when none.
        """
        _, _, workspace_id, _ = self._require_config()
        token = self._token_or_raise()
        resp = await self._request(
            "POST",
            f"{FABRIC_API_BASE}/workspaces/{workspace_id}/notebooks/{item_id}/getDefinition?format=ipynb",
            token,
        )
        if resp.status_code == 202:
            body = await self._await_operation(resp, token)
        elif resp.status_code == 200:
            body = resp.json() if resp.text else {}
        else:
            raise PlatformError(
                code_for_status(resp.status_code),
                log_detail=f"getDefinition {item_id} -> {resp.status_code}: {resp.text[:300]}",
            )
        for part in (body.get("definition") or {}).get("parts", []):
            if str(part.get("path", "")).endswith(".ipynb"):
                notebook = json.loads(base64.b64decode(part.get("payload", "")))
                return (notebook.get("metadata") or {}).get("dependencies") or {}
        return {}

    # --- Direct Delta table reads through OneLake (no SQL analytics endpoint) ------

    def _onelake_token(self) -> str:
        """
        Token for OneLake (audience https://storage.azure.com/). The Fabric API
        token is NOT accepted by OneLake. Delegated environments must supply the
        user's storage-scoped token; there is no service-principal fallback.
        """
        if self.onelake_token:
            return self.onelake_token
        if self.delegated_mode:
            raise PlatformError(
                ErrorCode.AUTHENTICATION_FAILED,
                "ACELO has no Microsoft sign-in for OneLake, so the approval tracking table "
                "cannot be read. Sign in again; if this persists, the ACELO app registration "
                "needs the delegated 'Azure Storage / user_impersonation' permission (the Entra "
                "permission Fabric OneLake uses; no storage account is involved).",
                log_detail="delegated Delta read without X-OneLake-Token",
            )
        try:
            app = msal.ConfidentialClientApplication(
                client_id=self._client_id(),
                client_credential=self.secret,
                authority=f"https://login.microsoftonline.com/{self._tenant_id()}",
            )
            result = app.acquire_token_for_client(scopes=[ONELAKE_SCOPE])
        except Exception as exc:  # noqa: BLE001
            raise PlatformError(ErrorCode.AUTHENTICATION_FAILED, log_detail=f"onelake token: {exc}") from exc
        if "access_token" not in result:
            raise PlatformError(
                ErrorCode.AUTHENTICATION_FAILED,
                log_detail=f"onelake token: {result.get('error_description')}",
            )
        return result["access_token"]

    def _delta_location(
        self, workspace_id: str, lakehouse_id: str, table: str, schema: str | None
    ) -> tuple[str, dict[str, str]]:
        """ABFS URI of a Lakehouse Delta table on OneLake, plus delta-rs storage options."""
        path = f"Tables/{schema}/{table}" if schema else f"Tables/{table}"
        uri = f"abfss://{workspace_id}@onelake.dfs.fabric.microsoft.com/{lakehouse_id}/{path}"
        return uri, {"bearer_token": self._onelake_token(), "use_fabric_endpoint": "true"}

    async def read_delta_table(
        self,
        lakehouse_id: str,
        table: str,
        schema: str | None = None,
        workspace_id: str | None = None,
        filters: list[tuple] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Reads a Lakehouse Delta table directly from OneLake — the Delta log and
        parquet files the notebook wrote. No SQL endpoint, no Spark, no ODBC.
        Raises a typed PlatformError on every failure; never returns stand-in rows.
        """
        workspace = workspace_id or self._workspace_id()
        uri, options = self._delta_location(workspace, lakehouse_id, table, schema)
        display = f"{schema + '.' if schema else ''}{table}"
        trace("FABRIC_DELTA_READ", status="started", workspace_id=workspace, lakehouse_id=lakehouse_id, table=display)
        try:
            rows = await asyncio.to_thread(read_delta_rows, uri, options, filters)
        except Exception as exc:  # noqa: BLE001 - classified below; the token is never in the message
            code, message = _classify_delta_error(exc, display)
            trace("FABRIC_DELTA_READ", status="failed", table=display, error_code=code, error=type(exc).__name__)
            raise PlatformError(code, message, log_detail=f"delta read {display}: {type(exc).__name__}: {str(exc)[:300]}") from exc
        trace("FABRIC_DELTA_READ", status="success", table=display, rows=len(rows))
        return rows

    async def query_activity_runs(self, platform_run_id: str) -> list[dict[str, Any]]:
        """
        Per-activity status of a pipeline run (Fabric "Query Activity Runs").
        Normalized to activity_name / status / start / end / error. Raises on
        failure; callers treat this as optional detail, never as run status.
        """
        workspace_id = self._workspace_id()
        token = self._token_or_raise()
        now = datetime.now(timezone.utc)
        body = {
            "filters": [],
            "orderBy": [{"orderBy": "ActivityRunStart", "order": "ASC"}],
            "lastUpdatedAfter": "2020-01-01T00:00:00Z",
            "lastUpdatedBefore": now.replace(year=now.year + 1).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        resp = await self._request(
            "POST",
            f"{FABRIC_API_BASE}/workspaces/{workspace_id}/datapipelines/pipelineruns/{platform_run_id}/queryactivityruns",
            token,
            json_body=body,
        )
        if resp.status_code != 200:
            raise PlatformError(
                code_for_status(resp.status_code),
                log_detail=f"queryactivityruns {platform_run_id} -> {resp.status_code}: {resp.text[:300]}",
            )
        data = resp.json() if resp.text else {}
        items = data.get("value", data if isinstance(data, list) else [])
        activities = []
        for item in items:
            error = item.get("error") or {}
            activities.append({
                "activity_name": item.get("activityName"),
                "activity_type": item.get("activityType"),
                "activity_run_id": item.get("activityRunId"),
                "status": item.get("status"),
                "start": item.get("activityRunStart"),
                "end": item.get("activityRunEnd"),
                "error": (error.get("message") if isinstance(error, dict) else str(error)) or None,
            })
        return activities

    # --- Phase 2 / Step 2: REAL analysis execution via the Fabric Job Scheduler API ---
    #
    # Every method below either performs a real Fabric API call or raises a typed
    # error. There is deliberately no fallback path: no synthetic run IDs, no
    # assumed "Completed" status, no substituted results. If Fabric cannot be
    # reached or the operation is unsupported, the caller finds out.

    # Per-domain notebook resolution. The service layer injects the resolved
    # Resource for each domain as "{domain}_notebook_id"; "notebook_item_id" is
    # the legacy single-notebook key and is honoured for cluster only, so
    # existing connections keep working.
    def _resolve_notebook_id(self, domain: str) -> str:
        item_id = self.auth_metadata.get(f"{domain}_notebook_id")
        if item_id:
            return item_id
        if domain == "cluster":
            legacy = self.auth_metadata.get("notebook_item_id")
            if legacy:
                return legacy
        # The adapter DOES implement start_analysis — the notebook for this
        # domain simply is not registered. Reporting it as an unimplemented
        # capability sent users looking for missing code instead of missing
        # configuration.
        raise PlatformError(
            ErrorCode.NOTEBOOK_NOT_CONFIGURED,
            f"No Fabric notebook is registered for the '{domain}' domain in this "
            f"environment. Install the ACELO package in Environment Setup.",
            log_detail=f"no {domain}_notebook_id resolved for workspace "
            f"{self.auth_metadata.get('workspace_id')}",
        )

    def execution_type(self, domain: str) -> str:
        """"notebook" (default, direct RunNotebook) or "pipeline" (ACELO pipeline)."""
        value = (self.auth_metadata.get(f"{domain}_execution_type") or "notebook").strip().lower()
        return "pipeline" if value == "pipeline" else "notebook"

    def _resolve_pipeline_id(self, domain: str) -> str:
        item_id = self.auth_metadata.get(f"{domain}_pipeline_id")
        if item_id:
            return item_id
        raise PlatformError(
            ErrorCode.NOTEBOOK_NOT_CONFIGURED,
            f"This environment is set to run {domain} analysis through the ACELO pipeline, "
            "but no pipeline is registered. Run 'Set up ACELO in Fabric' to deploy it, or "
            "switch the execution type back to Notebook.",
            log_detail=f"no {domain}_pipeline_id resolved for workspace {self.auth_metadata.get('workspace_id')}",
        )

    def _run_item_id(self, domain: str) -> str:
        """The item whose job instances represent this domain's runs."""
        if self.execution_type(domain) == "pipeline":
            return self._resolve_pipeline_id(domain)
        return self._resolve_notebook_id(domain)

    @staticmethod
    def _pipeline_execution_body(parameters: dict[str, Any] | None) -> dict[str, Any]:
        """Pipeline run payload: plain values, forwarded by the pipeline to the notebook."""
        values = {
            name: str(value)
            for name, value in (parameters or {}).items()
            if value is not None and str(value) != ""
        }
        return {"executionData": {"parameters": values}} if values else {}

    @staticmethod
    def _execution_body(parameters: dict[str, Any] | None) -> dict[str, Any]:
        """
        Fabric's RunNotebook payload. Parameters are how one deployed notebook
        serves every customer: the source/result tables and the ACELO run id are
        supplied per run rather than baked into the notebook source.
        """
        # Empty values are omitted entirely. Fabric rejects a parameter whose
        # value is blank with HTTP 400 "missing or invalid information", which
        # fails the whole run before the notebook is even reached.
        typed = {
            name: {"value": str(value), "type": "string"}
            for name, value in (parameters or {}).items()
            if value is not None and str(value) != ""
        }
        if not typed:
            return {}
        return {"executionData": {"parameters": typed}}

    async def start_analysis(
        self, domain: str, parameters: dict[str, Any] | None = None
    ) -> StartAnalysisResult:
        """
        Triggers the real notebook for this domain and returns Fabric's own job
        instance ID. Raises rather than inventing a run when anything is missing
        or the platform refuses.
        """
        # Configuration is resolved before any network call so an unsupported
        # domain or missing config never results in a wasted/ambiguous request.
        params = parameters or {}
        acelo_run_id = params.get("acelo_run_id")
        execution_type = self.execution_type(domain)
        try:
            notebook_id = self._resolve_notebook_id(domain)
            item_id = self._run_item_id(domain)
        except PlatformError as exc:
            trace(
                "FABRIC_EXECUTION_FAILED", acelo_run_id=acelo_run_id, domain=domain,
                http_status="-", error_code=exc.code, error_message=exc.message,
            )
            raise
        workspace_id = self._workspace_id()

        if self.delegated_mode:
            # Presence only - the token itself is never logged.
            trace("FABRIC_AUTH", auth_mode="delegated", token_present=bool(self.delegated_token))

        token, error = self._acquire_token()
        if not token:
            trace(
                "FABRIC_EXECUTION_FAILED", acelo_run_id=acelo_run_id, domain=domain,
                http_status="-", error_code=ErrorCode.AUTHENTICATION_FAILED,
                error_message="Could not obtain a Fabric access token.",
            )
            raise PlatformError(
                ErrorCode.AUTHENTICATION_FAILED,
                SESSION_EXPIRED_MESSAGE if self.delegated_mode else None,
                log_detail=f"Azure AD sign-in failed while starting {domain} analysis: {error}",
            )

        is_pipeline = execution_type == "pipeline"
        body = (
            self._pipeline_execution_body(parameters) if is_pipeline else self._execution_body(parameters)
        )
        headers = {"Authorization": f"Bearer {token}"}

        # Exactly what Fabric will receive as parameters (blank values are dropped),
        # so this line is the "ACELO SENT" evidence.
        sent = {
            name: (spec["value"] if isinstance(spec, dict) else spec)
            for name, spec in body.get("executionData", {}).get("parameters", {}).items()
        }
        request_fields: dict[str, Any] = {
            "acelo_run_id": acelo_run_id,
            "environment_id": params.get("environment_id") or "-",
            "platform": "fabric",
            "auth_mode": "delegated" if self.delegated_token else "service_principal",
            "execution_type": execution_type,
            "workspace_id": workspace_id,
            "notebook_id": notebook_id,
        }
        if is_pipeline:
            request_fields["pipeline_id"] = item_id
        trace(
            "FABRIC_EXECUTION_REQUEST",
            **request_fields,
            domain=domain,
            parameters=redact_parameters(sent),
        )

        # Microsoft's documented "Run On Demand Item Job" endpoint. The older
        # path shape (.../jobs/RunNotebook/instances) is kept as a fallback for
        # tenants that still route it, so a 404 on the documented URL does not
        # break execution.
        job_type = "Pipeline" if is_pipeline else "RunNotebook"
        documented = (
            f"{FABRIC_API_BASE}/workspaces/{workspace_id}/items/{item_id}"
            f"/jobs/instances?jobType={job_type}"
        )
        legacy = f"{FABRIC_API_BASE}/workspaces/{workspace_id}/items/{item_id}/jobs/{job_type}/instances"

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(documented, headers=headers, json=body)
                if resp.status_code == 404:
                    resp = await client.post(legacy, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            trace(
                "FABRIC_EXECUTION_FAILED", acelo_run_id=acelo_run_id, domain=domain,
                http_status="-", error_code=ErrorCode.TIMEOUT, error_message="Fabric did not respond in time.",
            )
            raise PlatformError(ErrorCode.TIMEOUT, log_detail=f"timeout starting {domain}: {exc}") from exc
        except httpx.RequestError as exc:
            trace(
                "FABRIC_EXECUTION_FAILED", acelo_run_id=acelo_run_id, domain=domain,
                http_status="-", error_code=ErrorCode.PLATFORM_API_UNAVAILABLE,
                error_message=type(exc).__name__,
            )
            raise PlatformError(
                ErrorCode.PLATFORM_API_UNAVAILABLE, log_detail=f"network error starting {domain}: {exc}"
            ) from exc

        if resp.status_code != 202:
            # RuntimeError (not PlatformError) keeps the established adapter
            # contract that job_service already converts into a FAILED run.
            detail = resp.text[:400]
            fabric_code = ""
            fabric_message = ""
            try:
                parsed = resp.json()
                fabric_code = str(parsed.get("errorCode", ""))
                fabric_message = str(parsed.get("message", ""))
            except ValueError:
                pass

            summary = f"Fabric refused to start the {domain} notebook (HTTP {resp.status_code})"
            if fabric_code:
                summary += f": {fabric_code}"
            if fabric_message:
                summary += f": {fabric_message}"

            trace(
                "FABRIC_EXECUTION_FAILED", acelo_run_id=acelo_run_id, domain=domain,
                http_status=resp.status_code, error_code=fabric_code or ErrorCode.EXECUTION_FAILED,
                error_message=fabric_message or "-",
            )
            raise PlatformError(
                ErrorCode.EXECUTION_FAILED,
                summary,
                log_detail=f"start {domain} -> {resp.status_code}: {detail}",
            )

        job_instance_id = extract_job_instance_id(resp.headers.get("Location"))
        if not job_instance_id:
            trace(
                "FABRIC_EXECUTION_FAILED", acelo_run_id=acelo_run_id, domain=domain,
                http_status=resp.status_code, error_code=ErrorCode.EXECUTION_FAILED,
                error_message="202 without a job instance id in Location",
            )
            raise RuntimeError(
                "Fabric accepted the job but returned no job instance ID in the Location header, "
                "so the run cannot be tracked."
            )

        trace(
            "FABRIC_EXECUTION_RESPONSE",
            acelo_run_id=acelo_run_id,
            http_status=resp.status_code,
            platform_run_id=job_instance_id,
            retry_after=resp.headers.get("Retry-After") or "-",
            workspace_id=workspace_id,
            execution_type=execution_type,
            notebook_id=notebook_id,
            **({"pipeline_id": item_id} if is_pipeline else {}),
        )
        detail: dict[str, Any] = {"workspace_id": workspace_id, "item_id": item_id}
        if is_pipeline:
            detail.update(execution_type="pipeline", notebook_id=notebook_id)
        return StartAnalysisResult(platform_run_id=job_instance_id, status="STARTING", detail=detail)

    async def _get_job_instance(self, platform_run_id: str, domain: str = "cluster") -> dict[str, Any]:
        """Real GET of one job instance. Raises on every failure - callers must
        never receive an assumed status."""
        item_id = self._run_item_id(domain)  # notebook or pipeline, per execution type
        workspace_id = self._workspace_id()

        token, error = self._acquire_token()
        if not token:
            raise PlatformError(
                ErrorCode.AUTHENTICATION_FAILED,
                log_detail=f"Azure AD sign-in failed while polling {platform_run_id}: {error}",
            )

        url = f"{FABRIC_API_BASE}/workspaces/{workspace_id}/items/{item_id}/jobs/instances/{platform_run_id}"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        except httpx.TimeoutException as exc:
            raise PlatformError(ErrorCode.TIMEOUT, log_detail=f"timeout polling {platform_run_id}: {exc}") from exc
        except httpx.RequestError as exc:
            raise PlatformError(
                ErrorCode.PLATFORM_API_UNAVAILABLE, log_detail=f"network error polling {platform_run_id}: {exc}"
            ) from exc

        if resp.status_code == 404:
            raise PlatformError(
                ErrorCode.RESOURCE_NOT_FOUND,
                "The platform run could not be found. It may have been removed from the workspace.",
                log_detail=f"job instance {platform_run_id} -> 404",
            )
        if resp.status_code != 200:
            raise PlatformError(
                code_for_status(resp.status_code),
                log_detail=f"GET job instance {platform_run_id} -> {resp.status_code}: {resp.text[:300]}",
            )
        return resp.json()

    async def get_run_status(self, platform_run_id: str, domain: str = "cluster") -> RunStatusResult:
        data = await self._get_job_instance(platform_run_id, domain)
        fabric_status = data.get("status", "NotStarted")
        status = _FABRIC_STATUS_MAP.get(fabric_status, "RUNNING")
        failure_reason = data.get("failureReason")

        return RunStatusResult(
            status=status,
            current_step=None,  # Fabric's Job Scheduler API exposes no step detail
            progress=100.0 if status == "COMPLETED" else None,
            error=str(failure_reason) if failure_reason else None,
            detail={"exit_value": data.get("exitValue"), "fabric_status": fabric_status},
        )

    async def get_run_logs(self, platform_run_id: str, domain: str = "cluster") -> list[RunLogEntry]:
        # Fabric's Job Scheduler API does not expose a granular execution log
        # stream for notebook runs (unlike Databricks' get-output). What is
        # genuinely available is the job instance's own status fields - so that
        # is what gets surfaced, rather than synthesised step-by-step logs.
        data = await self._get_job_instance(platform_run_id, domain)
        entries: list[RunLogEntry] = []

        entries.append(
            RunLogEntry(
                timestamp=data.get("startTimeUtc") or datetime.now(timezone.utc).isoformat(),
                level="INFO",
                message=f"Job instance {data.get('id')} status: {data.get('status')}",
                source="fabric",
            )
        )
        if data.get("failureReason"):
            entries.append(
                RunLogEntry(
                    timestamp=data.get("endTimeUtc") or datetime.now(timezone.utc).isoformat(),
                    level="ERROR",
                    message=str(data["failureReason"]),
                    source="fabric",
                )
            )
        if data.get("exitValue"):
            entries.append(
                RunLogEntry(
                    timestamp=data.get("endTimeUtc") or datetime.now(timezone.utc).isoformat(),
                    level="INFO",
                    message=f"exitValue: {data['exitValue']}",
                    source="fabric",
                )
            )
        return entries

    async def _get_run_result_onelake(
        self, domain: str, lakehouse_id: str, acelo_run_id: str | None
    ) -> RunResult:
        """
        Reads THIS run's rows of the result Delta table directly from OneLake —
        no SQL analytics endpoint, no ODBC. Rows are filtered by acelo_run_id so
        an append-only result table never returns an earlier run's output.
        """
        table = self._result_table(domain)
        schema = (self.auth_metadata.get(f"{domain}_result_schema") or "").strip() or None
        if "." in table:  # an already-qualified name: "<schema>.<table>"
            schema, table = table.rsplit(".", 1)
        workspace = self.auth_metadata.get(f"{domain}_result_lakehouse_workspace_id") or None
        filters = [("acelo_run_id", "=", acelo_run_id)] if acelo_run_id else None
        rows = await self.read_delta_table(lakehouse_id, table, schema, workspace_id=workspace, filters=filters)
        reference = f"OneLake {schema + '.' if schema else ''}{table}"
        return RunResult(
            result_reference=reference,
            payload={
                "table": reference,
                "row_count": len(rows),
                "rows": rows,
                "acelo_run_id": acelo_run_id,
                "source": "onelake",
            },
        )

    def _result_table(self, domain: str = "cluster") -> str:
        """The table the existing optimization notebook writes to. Configurable
        per environment; falls back to the established default so existing
        deployments keep working. The notebook itself is NOT modified."""
        return (
            self.auth_metadata.get(f"{domain}_result_table")
            or self.auth_metadata.get("cluster_result_table")
            or self.CLUSTER_RESULT_TABLE
        )

    async def get_run_result(
        self, platform_run_id: str, domain: str = "cluster", acelo_run_id: str | None = None
    ) -> RunResult:
        """
        Reads the notebook's actual output: directly from OneLake (Delta) when
        the result Lakehouse is known, otherwise the legacy SQL analytics
        endpoint. There is no substitute result: if neither works, this raises.

        When `acelo_run_id` is supplied, the read is scoped to that run. The
        result table accumulates a row set per run, so an unscoped read would
        return rows from earlier runs — a stale result presented as this one's.
        """
        lakehouse_id = self.auth_metadata.get(f"{domain}_result_lakehouse_id")
        if lakehouse_id:
            return await self._get_run_result_onelake(domain, lakehouse_id, acelo_run_id)

        sql_endpoint = self.auth_metadata.get("sql_endpoint")
        database = self.auth_metadata.get("lakehouse_database")

        if not sql_endpoint or not database:
            missing = [n for n, v in (("sql_endpoint", sql_endpoint), ("lakehouse_database", database)) if not v]
            raise ResultConfigurationError(
                ErrorCode.RESULT_RETRIEVAL_FAILED,
                "The run finished, but ACELO cannot read its results: no Lakehouse is configured for "
                "reading the result table from OneLake. Set the Lakehouse ID in Environment Setup > "
                "Cluster Settings. (Legacy SQL reads would also need: " + " and ".join(missing) + ".)",
                log_detail=f"result read not configured: sql_endpoint={bool(sql_endpoint)} database={bool(database)}",
            )

        try:
            import pyodbc
        except ImportError as exc:
            raise RuntimeError(
                "Reading Fabric results requires the pyodbc package and the ODBC Driver 18 "
                "for SQL Server. Install both on the ACELO backend host."
            ) from exc
        if pyodbc is None:  # sys.modules entry explicitly set to None
            raise RuntimeError(
                "Reading Fabric results requires the pyodbc package and the ODBC Driver 18 "
                "for SQL Server. Install both on the ACELO backend host."
            )

        drivers = getattr(pyodbc, "drivers", None)
        installed = drivers() if callable(drivers) else None
        if isinstance(installed, list) and ODBC_DRIVER not in installed:
            raise PlatformError(
                ErrorCode.RESULT_RETRIEVAL_FAILED,
                f"The run finished, but the ACELO backend host cannot read results: '{ODBC_DRIVER}' "
                "is not installed. Install Microsoft ODBC Driver 18 for SQL Server on the backend host.",
                log_detail=f"installed ODBC drivers: {installed}",
            )

        table = self._result_table(domain)
        base = (
            f"Driver={{{ODBC_DRIVER}}};"
            f"Server={sql_endpoint},1433;"
            f"Database={database};"
            "Encrypt=Yes;TrustServerCertificate=No;"
        )
        connect_kwargs: dict[str, Any] = {"timeout": 30}

        if self.delegated_mode:
            # Microsoft Account environments read results AS THE SIGNED-IN USER,
            # with an Entra token for the SQL endpoint (audience
            # https://database.windows.net/) acquired by the browser. There is no
            # service-principal fallback: without that token, the read is refused.
            trace("FABRIC_RESULT_AUTH", auth_mode="delegated", sql_token_present=bool(self.sql_token))
            if not self.sql_token:
                raise PlatformError(
                    ErrorCode.RESULT_RETRIEVAL_FAILED,
                    "The run finished, but ACELO has no Microsoft sign-in for the Lakehouse SQL "
                    "endpoint, so its results cannot be read. Sign in again; if this persists, the "
                    "ACELO app registration needs the delegated 'Azure SQL Database / "
                    "user_impersonation' permission.",
                    log_detail="delegated result read without X-Fabric-Sql-Token",
                )
            conn_str = base
            connect_kwargs["attrs_before"] = {SQL_COPT_SS_ACCESS_TOKEN: _odbc_token_struct(self.sql_token)}
        else:
            conn_str = base + (
                "Authentication=ActiveDirectoryServicePrincipal;"
                f"UID={self._client_id()};"
                f"PWD={self.secret};"
            )

        try:
            conn = pyodbc.connect(conn_str, **connect_kwargs)
        except Exception as exc:  # noqa: BLE001 - pyodbc raises driver-specific errors
            # The connection string contains the client secret, so it must never
            # reach the message. Only the driver's own text is surfaced.
            raise PlatformError(
                ErrorCode.RESULT_RETRIEVAL_FAILED,
                "Could not connect to the Lakehouse SQL analytics endpoint to read the result.",
                log_detail=f"pyodbc connect failed for {sql_endpoint}/{database}: {exc}",
            ) from exc

        try:
            cursor = conn.cursor()
            if acelo_run_id:
                # Parameterised to keep the run id out of the SQL string.
                cursor.execute(f"SELECT * FROM {table} WHERE acelo_run_id = ?", acelo_run_id)
            else:
                cursor.execute(f"SELECT * FROM {table}")
            columns = [col[0] for col in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        except Exception as exc:  # noqa: BLE001
            raise PlatformError(
                ErrorCode.RESULT_RETRIEVAL_FAILED,
                f"The run completed, but its result table could not be read.",
                log_detail=f"query failed on {table}: {exc}",
            ) from exc
        finally:
            conn.close()

        return RunResult(
            result_reference=table,
            payload={
                "table": table,
                "row_count": len(rows),
                "rows": rows,
                "acelo_run_id": acelo_run_id,
            },
        )

    async def cancel_run(self, platform_run_id: str, domain: str = "cluster") -> ConnectionResult:
        """Asks Fabric to cancel. Reports the platform's real answer - a failed
        cancel is reported as failed, never as success."""
        item_id = self._run_item_id(domain)
        workspace_id = self._workspace_id()

        token, error = self._acquire_token()
        if not token:
            raise PlatformError(
                ErrorCode.AUTHENTICATION_FAILED,
                log_detail=f"Azure AD sign-in failed while cancelling {platform_run_id}: {error}",
            )

        url = (
            f"{FABRIC_API_BASE}/workspaces/{workspace_id}/items/{item_id}"
            f"/jobs/instances/{platform_run_id}/cancel"
        )
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, headers={"Authorization": f"Bearer {token}"})
        except httpx.TimeoutException as exc:
            raise PlatformError(ErrorCode.TIMEOUT, log_detail=f"timeout cancelling {platform_run_id}: {exc}") from exc
        except httpx.RequestError as exc:
            raise PlatformError(
                ErrorCode.PLATFORM_API_UNAVAILABLE,
                log_detail=f"network error cancelling {platform_run_id}: {exc}",
            ) from exc

        if resp.status_code in (200, 202):
            # Fabric acknowledges asynchronously: this is "cancel requested",
            # and only a subsequent poll returning Cancelled proves it happened.
            return ConnectionResult(
                ok=True,
                message="Cancel requested",
                detail={"http_status": resp.status_code, "confirmed": False},
            )

        return ConnectionResult(
            ok=False,
            message=f"Fabric could not cancel this run: HTTP {resp.status_code}.",
            detail={"http_status": resp.status_code},
        )

    async def retry_run(
        self, domain: str, platform_run_id: str, parameters: dict[str, Any] | None = None
    ) -> StartAnalysisResult:
        return await self.start_analysis(domain, parameters)

    # --- execution of approved changes (Phase 7/8 - deliberately not built) ---

    async def start_execution(self, action: dict[str, Any]) -> StartAnalysisResult:
        raise PlatformCapabilityNotImplemented(
            self.platform_name, "start_execution", "Applying optimizations is Phase 7/8, not built yet."
        )

    async def validate_execution(self, platform_run_id: str) -> RunStatusResult:
        raise PlatformCapabilityNotImplemented(
            self.platform_name, "validate_execution", "Phase 8."
        )

    async def rollback(self, platform_run_id: str) -> ConnectionResult:
        raise PlatformCapabilityNotImplemented(self.platform_name, "rollback", "Phase 8.")
