"""
Databricks App identity.

When ACELO runs as a native Databricks App, the runtime already knows which
workspace it is in and already holds an identity for it. There is nothing for a
user to type: no workspace URL, no personal access token, no platform choice.

This module is the ONE place that asks the Databricks SDK for that identity. It
deliberately does not replace the adapter's HTTP layer — discovery keeps making
the same `httpx` calls against the same documented endpoints. All that changes
is where the Authorization header comes from:

    stored PAT (local/dev, existing behaviour)  ->  Bearer <decrypted secret>
    Databricks App runtime                      ->  SDK-issued credential

So this is not a second authentication system; it is a second *source* for the
one header the adapter already sends.

Nothing here logs, returns or stores a token beyond the header it hands to the
adapter for a single call.
"""

import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

# Set by the Databricks Apps runtime. Presence of the host plus one of the app
# markers is what tells ACELO it is running inside an App rather than on a
# developer's machine.
_HOST_VARS = ("DATABRICKS_HOST",)
_APP_MARKERS = ("DATABRICKS_APP_NAME", "DATABRICKS_APP_PORT", "DATABRICKS_WORKSPACE_ID")

# The SDK config is cheap to build but not free, and _headers() is called on
# every discovery request. Built once, guarded for thread safety.
_lock = threading.Lock()
_config: Any = None
_config_failed = False


def _env(names: tuple[str, ...]) -> str | None:
    for name in names:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return None


def app_host() -> str | None:
    """The workspace URL this App is running in, normalised to an https origin."""
    host = _env(_HOST_VARS)
    if not host:
        return None
    if not host.startswith("http"):
        host = f"https://{host}"
    return host.rstrip("/")


def is_app_runtime() -> bool:
    """
    True when ACELO is running as a Databricks App.

    Requires BOTH a workspace host and an App marker: a developer with
    DATABRICKS_HOST exported for the CLI is not running inside an App, and must
    keep the existing configured-connection behaviour.
    """
    return bool(app_host()) and _env(_APP_MARKERS) is not None


def _load_config() -> Any:
    """Builds the SDK config once. Returns None if the SDK cannot authenticate."""
    global _config, _config_failed
    if _config is not None or _config_failed:
        return _config

    with _lock:
        if _config is not None or _config_failed:
            return _config
        try:
            from databricks.sdk.core import Config

            # No arguments: the SDK resolves the App's own credentials from the
            # runtime environment. Nothing is passed in, so nothing can be
            # hardcoded here.
            _config = Config()
        except Exception as exc:  # noqa: BLE001 - absence of App auth is a state, not a crash
            _config_failed = True
            logger.info("databricks_app_auth_unavailable error=%s", type(exc).__name__)
            return None
        return _config


def app_auth_headers() -> dict[str, str] | None:
    """
    The Authorization header for the App's own identity, or None when ACELO is
    not running with one.

    The token is fetched per call and handed straight to the request; it is
    never returned to a caller that could persist it, never logged, and never
    written to the database.
    """
    config = _load_config()
    if config is None:
        return None
    try:
        headers = config.authenticate()
    except Exception as exc:  # noqa: BLE001 - expired/absent credentials must not 500
        logger.info("databricks_app_auth_failed error=%s", type(exc).__name__)
        return None

    if not isinstance(headers, dict) or not headers.get("Authorization"):
        return None
    return {"Authorization": headers["Authorization"]}


def available() -> bool:
    """True when an App identity can currently be used to call Databricks."""
    return is_app_runtime() and app_auth_headers() is not None


def reset_cache() -> None:
    """Test seam: forget the resolved SDK config."""
    global _config, _config_failed
    with _lock:
        _config = None
        _config_failed = False


def describe() -> dict[str, Any]:
    """
    Safe summary for the frontend and logs: where we are and how we authenticate.
    Carries no credential.
    """
    return {
        "databricks_app": is_app_runtime(),
        "workspace_host": app_host(),
        "auth_mode": "databricks_app_identity" if is_app_runtime() else "configured_connection",
    }
