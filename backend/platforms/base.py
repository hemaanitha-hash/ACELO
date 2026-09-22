from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


class PlatformCapabilityNotImplemented(NotImplementedError):
    """
    Raised deliberately when a platform adapter does not yet support a
    capability. This is a controlled signal, not a bug: callers (job_service,
    API layer) catch this specifically and surface "not yet supported" to the
    user instead of pretending the operation succeeded.
    """

    def __init__(self, platform: str, capability: str, detail: str = "") -> None:
        self.platform = platform
        self.capability = capability
        message = f"{platform} adapter does not implement '{capability}' yet."
        if detail:
            message += f" {detail}"
        super().__init__(message)


@dataclass
class ConnectionResult:
    ok: bool
    message: str
    detail: dict[str, Any] | None = None


@dataclass
class StartAnalysisResult:
    """What starting a real platform job gives us back immediately."""

    platform_run_id: str
    status: str  # a models.enums.JobStatus value
    detail: dict[str, Any] | None = None


@dataclass
class RunStatusResult:
    status: str  # a models.enums.JobStatus value
    current_step: str | None = None
    progress: float | None = None  # 0-100, only when the platform reports real progress
    error: str | None = None
    detail: dict[str, Any] | None = None


@dataclass
class RunLogEntry:
    timestamp: str  # ISO-8601
    level: str  # a models.enums.LogLevel value
    message: str
    source: str  # e.g. "databricks" | "fabric"


@dataclass
class RunResult:
    result_reference: str | None
    payload: dict[str, Any]


@dataclass
class DiscoveredResource:
    """
    One item found in a customer's workspace. Deliberately platform-neutral:
    `resource_type` is normalised (Notebook / Lakehouse / SQLEndpoint /
    Experiment / Folder / Job / Cluster / File) so the UI renders Fabric and
    Databricks the same way.
    """

    platform_resource_id: str
    display_name: str
    resource_type: str
    detail: dict[str, Any] | None = None


@dataclass
class DiscoveredWorkspace:
    """The connected workspace itself — never includes credentials."""

    workspace_id: str
    workspace_name: str
    detail: dict[str, Any] | None = None


@dataclass
class ConnectionValidation:
    """
    Result of a real connection test. `error_code` is an errors.ErrorCode
    value when connected is False, and is what the frontend renders.
    """

    connected: bool
    message: str
    workspace_id: str | None = None
    workspace_name: str | None = None
    error_code: str | None = None


class PlatformAdapter(ABC):
    """
    Common asynchronous job contract every connected platform must implement.
    The agent and job service only ever talk to this interface — they never
    know whether they're driving Databricks or Fabric underneath, and they
    never block waiting for a job to finish: start_analysis() returns as soon
    as the platform has accepted the job, and get_run_status()/get_run_logs()/
    get_run_result() are polled afterward (by API reads in Phase 1; by a
    background worker from Phase 4 onward).
    """

    platform_name: str = "unknown"

    def __init__(self, endpoint: str, auth_metadata: dict[str, Any], secret: str | None) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.auth_metadata = auth_metadata
        self.secret = secret

    # --- connection ---

    @abstractmethod
    async def connect(self) -> ConnectionResult:
        """Establish/validate whatever session state the platform needs before use."""

    @abstractmethod
    async def test_connection(self) -> ConnectionResult:
        """Make one lightweight authenticated call to confirm the credentials work."""

    @abstractmethod
    async def get_environment(self) -> dict[str, Any]:
        """Return a small summary of the connected workspace (name, resource counts)."""

    # --- analysis job lifecycle (maps 1:1 to a JobRun) ---

    @abstractmethod
    async def start_analysis(
        self, domain: str, parameters: dict[str, Any] | None = None
    ) -> StartAnalysisResult:
        """Trigger the real platform job for this domain and return its platform_run_id."""

    @abstractmethod
    async def get_run_status(self, platform_run_id: str, domain: str = "cluster") -> RunStatusResult:
        """Poll the platform for this run's current, real status."""

    @abstractmethod
    async def get_run_logs(self, platform_run_id: str, domain: str = "cluster") -> list[RunLogEntry]:
        """Fetch whatever logs the platform exposes for this run."""

    @abstractmethod
    async def get_run_result(
        self, platform_run_id: str, domain: str = "cluster", acelo_run_id: str | None = None
    ) -> RunResult:
        """Fetch the completed run's output."""

    @abstractmethod
    async def cancel_run(self, platform_run_id: str, domain: str = "cluster") -> ConnectionResult:
        """Ask the platform to cancel an in-flight run."""

    @abstractmethod
    async def retry_run(self, domain: str, platform_run_id: str) -> StartAnalysisResult:
        """Re-trigger a failed run. Default behavior may just call start_analysis again."""

    # --- Phase 1: connection validation & environment discovery ---
    # These are separate from the analysis lifecycle above on purpose: they only
    # ever READ the customer's environment, and must work before any job exists.

    async def validate_connection(self) -> "ConnectionValidation":
        """
        Authenticate and confirm the configured workspace is actually reachable.
        Must reflect real platform state — never optimistic.
        """
        raise PlatformCapabilityNotImplemented(self.platform_name, "validate_connection")

    async def discover_workspace(self) -> "DiscoveredWorkspace":
        """Fetch the real workspace identity (id + display name)."""
        raise PlatformCapabilityNotImplemented(self.platform_name, "discover_workspace")

    async def discover_resources(self) -> list["DiscoveredResource"]:
        """List the real items in the workspace. Returns [] only if it is genuinely empty."""
        raise PlatformCapabilityNotImplemented(self.platform_name, "discover_resources")

    async def discover_files(self) -> list["DiscoveredResource"]:
        """List data files/tables where the platform exposes them. Phase 9 for most platforms."""
        raise PlatformCapabilityNotImplemented(self.platform_name, "discover_files")

    async def read_delta_table(
        self, lakehouse_id: str, table: str, schema: str | None = None, workspace_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Reads a Delta table directly from platform storage (e.g. approval tracking)."""
        raise PlatformCapabilityNotImplemented(self.platform_name, "read_delta_table")

    # --- execution (Phase 7/8 — interface exists now, workflow does not yet) ---

    async def start_execution(self, action: dict[str, Any]) -> StartAnalysisResult:
        raise PlatformCapabilityNotImplemented(self.platform_name, "start_execution")

    async def validate_execution(self, platform_run_id: str) -> RunStatusResult:
        raise PlatformCapabilityNotImplemented(self.platform_name, "validate_execution")

    async def rollback(self, platform_run_id: str) -> ConnectionResult:
        raise PlatformCapabilityNotImplemented(self.platform_name, "rollback")
