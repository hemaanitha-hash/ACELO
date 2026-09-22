"""
FileAdapter — architecture only.

Phase 1 deliberately implements nothing here beyond the contract. The point of
this module existing now is that `Environment.platform` can already be "file",
and the API/service layer routes it through exactly the same PlatformAdapter
interface as Fabric and Databricks — so Phase 9 adds behaviour without
reshaping anything above it.

Every capability raises PlatformCapabilityNotImplemented rather than returning
an empty or optimistic result, so a "file" environment can never appear
connected or discovered before the feature actually exists.
"""

from typing import Any

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
from platforms.errors import ErrorCode


class FileAdapter(PlatformAdapter):
    """
    A customer-supplied file as an analysis source.

    Phase 9 will support: a file uploaded directly to ACELO, and a file already
    hosted in the customer's platform (OneLake / DBFS / Volumes). Both will
    surface here as resources of type "File".
    """

    platform_name = "file"

    # --- Phase 1 contract ---

    async def validate_connection(self) -> ConnectionValidation:
        # Honest negative result: a file environment is never "connected" yet.
        return ConnectionValidation(
            connected=False,
            message="File-based environments are not available yet.",
            error_code=ErrorCode.NOT_CONFIGURED,
        )

    async def discover_workspace(self) -> DiscoveredWorkspace:
        raise PlatformCapabilityNotImplemented(
            self.platform_name, "discover_workspace", "File analysis is Phase 9."
        )

    async def discover_resources(self) -> list[DiscoveredResource]:
        raise PlatformCapabilityNotImplemented(
            self.platform_name, "discover_resources", "File analysis is Phase 9."
        )

    async def discover_files(self) -> list[DiscoveredResource]:
        raise PlatformCapabilityNotImplemented(
            self.platform_name, "discover_files", "File analysis is Phase 9."
        )

    # --- inherited abstract members: declared, not implemented ---

    async def connect(self) -> ConnectionResult:
        return ConnectionResult(ok=False, message="File-based environments are not available yet.")

    async def test_connection(self) -> ConnectionResult:
        return await self.connect()

    async def get_environment(self) -> dict[str, Any]:
        raise PlatformCapabilityNotImplemented(self.platform_name, "get_environment", "Phase 9.")

    async def start_analysis(
        self, domain: str, parameters: dict[str, Any] | None = None
    ) -> StartAnalysisResult:
        raise PlatformCapabilityNotImplemented(self.platform_name, "start_analysis", "Phase 9.")

    async def get_run_status(self, platform_run_id: str, domain: str = "cluster") -> RunStatusResult:
        raise PlatformCapabilityNotImplemented(self.platform_name, "get_run_status", "Phase 9.")

    async def get_run_logs(self, platform_run_id: str, domain: str = "cluster") -> list[RunLogEntry]:
        raise PlatformCapabilityNotImplemented(self.platform_name, "get_run_logs", "Phase 9.")

    async def get_run_result(
        self, platform_run_id: str, domain: str = "cluster", acelo_run_id: str | None = None
    ) -> RunResult:
        raise PlatformCapabilityNotImplemented(self.platform_name, "get_run_result", "Phase 9.")

    async def cancel_run(self, platform_run_id: str, domain: str = "cluster") -> ConnectionResult:
        raise PlatformCapabilityNotImplemented(self.platform_name, "cancel_run", "Phase 9.")

    async def retry_run(self, domain: str, platform_run_id: str) -> StartAnalysisResult:
        raise PlatformCapabilityNotImplemented(self.platform_name, "retry_run", "Phase 9.")
