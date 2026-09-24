"""
Safe, user-facing error codes for platform connection/discovery.

Platform SDKs and REST APIs return a wide range of failures — HTTP status
codes, MSAL error dicts, socket errors. The API layer must never leak those
raw (stack traces, tokens, connection strings) to the browser, but the user
still needs an actionable reason. `PlatformError` is the single translation
point: adapters raise it with a stable code, the API layer renders code +
message, and the technical detail stays server-side in `log_detail`.
"""


class ErrorCode:
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    WORKSPACE_NOT_FOUND = "WORKSPACE_NOT_FOUND"
    PLATFORM_API_UNAVAILABLE = "PLATFORM_API_UNAVAILABLE"
    INVALID_CONFIGURATION = "INVALID_CONFIGURATION"
    DISCOVERY_FAILED = "DISCOVERY_FAILED"
    TIMEOUT = "TIMEOUT"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    RESULT_RETRIEVAL_FAILED = "RESULT_RETRIEVAL_FAILED"
    CANCELED = "CANCELED"
    UNSUPPORTED = "UNSUPPORTED"
    ASSET_MISSING = "ASSET_MISSING"
    PROVISIONING_FAILED = "PROVISIONING_FAILED"
    NOT_PROVISIONED = "NOT_PROVISIONED"
    # The domain's notebook is not registered for this environment. This is a
    # configuration state, NOT an unimplemented adapter capability.
    NOTEBOOK_NOT_CONFIGURED = "NOTEBOOK_NOT_CONFIGURED"
    # Per-parameter configuration gaps. Distinguished from one another so the
    # UI can name the single setting to fix instead of a generic list.
    CLUSTER_SOURCE_TABLE_NOT_CONFIGURED = "CLUSTER_SOURCE_TABLE_NOT_CONFIGURED"
    CLUSTER_RESULT_TABLE_NOT_CONFIGURED = "CLUSTER_RESULT_TABLE_NOT_CONFIGURED"


# What the user is told. Deliberately free of platform internals.
_DEFAULT_MESSAGES = {
    ErrorCode.AUTHENTICATION_FAILED: (
        "Authentication failed. Verify the configured credentials for this environment."
    ),
    ErrorCode.PERMISSION_DENIED: (
        "Authentication succeeded, but the configured identity does not have access to this resource."
    ),
    ErrorCode.WORKSPACE_NOT_FOUND: (
        "The configured workspace could not be found. Verify the workspace ID."
    ),
    ErrorCode.PLATFORM_API_UNAVAILABLE: (
        "The platform API could not be reached. Try again shortly."
    ),
    ErrorCode.INVALID_CONFIGURATION: (
        "This environment is not configured correctly. Check the required fields."
    ),
    ErrorCode.DISCOVERY_FAILED: (
        "The workspace was reached, but its contents could not be listed."
    ),
    ErrorCode.TIMEOUT: "The platform did not respond in time. Try again shortly.",
    ErrorCode.NOT_CONFIGURED: "This platform is not configured.",
    ErrorCode.RESOURCE_NOT_FOUND: (
        "The required resource could not be found in the connected workspace."
    ),
    ErrorCode.EXECUTION_FAILED: "The platform run failed. See the run logs for details.",
    ErrorCode.RESULT_RETRIEVAL_FAILED: (
        "The run finished, but its results could not be retrieved."
    ),
    ErrorCode.CANCELED: "This run was canceled.",
    ErrorCode.UNSUPPORTED: "This operation is not supported for this platform.",
    ErrorCode.ASSET_MISSING: (
        "The ACELO optimization asset for this domain is not available in this "
        "ACELO build, so it cannot be deployed."
    ),
    ErrorCode.PROVISIONING_FAILED: "ACELO could not be set up in this workspace.",
    ErrorCode.NOT_PROVISIONED: (
        "ACELO is not set up in this workspace yet. Run 'Set up ACELO in Fabric' first."
    ),
    ErrorCode.CLUSTER_SOURCE_TABLE_NOT_CONFIGURED: (
        "The Cluster source table is not configured for this environment. Set it "
        "in Environment Setup before running a Cluster analysis."
    ),
    ErrorCode.CLUSTER_RESULT_TABLE_NOT_CONFIGURED: (
        "The Cluster result table is not configured for this environment. Set it "
        "in Environment Setup before running a Cluster analysis."
    ),
    ErrorCode.NOTEBOOK_NOT_CONFIGURED: (
        "The optimization notebook for this domain is not registered in this "
        "environment. Run 'Set up ACELO in Fabric' in Environment Setup."
    ),
}


class PlatformError(Exception):
    """
    A platform failure already translated into something safe to show a user.

    `message` is user-facing. `log_detail` carries the technical specifics for
    the server log and is never serialised into an API response.
    """

    def __init__(
        self,
        code: str,
        message: str | None = None,
        log_detail: str = "",
        status_code: int | None = None,
        platform_error_code: str | None = None,
        platform_message: str | None = None,
    ) -> None:
        self.code = code
        self.message = message or _DEFAULT_MESSAGES.get(code, "An unexpected platform error occurred.")
        self.log_detail = log_detail
        # Structured facts about the underlying call, so a caller can diagnose
        # WHICH request failed and how without re-parsing `log_detail`. These
        # carry the platform's own HTTP status and error body — never a request
        # header, and never a credential.
        self.status_code = status_code
        self.platform_error_code = platform_error_code
        self.platform_message = platform_message
        super().__init__(self.message)


# Failures worth retrying: the request may succeed unchanged a moment later.
# Everything else is terminal - retrying an auth or config failure just burns
# quota and delays the real error reaching the user.
TRANSIENT_CODES = {
    ErrorCode.PLATFORM_API_UNAVAILABLE,
    ErrorCode.TIMEOUT,
}


def is_transient(code: str) -> bool:
    return code in TRANSIENT_CODES


def code_for_status(status_code: int) -> str:
    """Maps an HTTP status from a platform REST API onto our error codes."""
    if status_code == 400:
        # A malformed/rejected request will be rejected identically on retry.
        return ErrorCode.INVALID_CONFIGURATION
    if status_code == 401:
        return ErrorCode.AUTHENTICATION_FAILED
    if status_code == 403:
        return ErrorCode.PERMISSION_DENIED
    if status_code == 404:
        return ErrorCode.WORKSPACE_NOT_FOUND
    if status_code == 408:
        return ErrorCode.TIMEOUT
    if status_code == 429 or status_code >= 500:
        # Rate limiting and server-side faults are the classic transient cases.
        return ErrorCode.PLATFORM_API_UNAVAILABLE
    return ErrorCode.PLATFORM_API_UNAVAILABLE
