from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

EnvironmentPlatform = Literal["fabric", "databricks", "file"]

# "service_principal": ACELO's backend holds a client secret and authenticates
# itself (unattended background runs).
# "user": the signed-in organization Microsoft account's delegated token is
# supplied per request by the browser; no secret is stored.
# "personal" is retained as a legacy alias for "user" so environments created
# before the rename keep working.
AuthMode = Literal["service_principal", "user", "personal"]


class EnvironmentCreate(BaseModel):
    """
    Inbound configuration. `client_secret` is write-only — it is encrypted on
    arrival and there is no response model anywhere that can echo it back.
    """

    name: str = Field(..., description="Customer-facing label, e.g. 'Fabric Production'")
    platform: EnvironmentPlatform
    auth_mode: AuthMode = Field(
        "service_principal",
        description="'user' uses the signed-in Microsoft account's delegated token; no client_secret needed.",
    )
    tenant_id: str | None = Field(None, description="Azure AD tenant ID (Fabric)")
    workspace_id: str | None = Field(None, description="Platform workspace identifier")
    client_id: str | None = Field(None, description="Service principal / application ID")
    client_secret: str | None = Field(None, description="Write-only. Encrypted at rest, never returned.")
    endpoint: str | None = Field(None, description="Workspace URL (Databricks)")


class EnvironmentUpdate(BaseModel):
    name: str | None = None
    tenant_id: str | None = None
    workspace_id: str | None = None
    client_id: str | None = None
    client_secret: str | None = Field(None, description="Blank leaves the stored secret unchanged.")
    endpoint: str | None = None


class EnvironmentOut(BaseModel):
    """
    Safe projection of an Environment. Deliberately has no client_secret,
    client_id, token or auth_metadata field — the response model itself is the
    guarantee that credentials cannot leak, independent of caller discipline.
    """

    id: str
    customer_id: str
    # The connection this environment is built on. The UI needs it to tell a
    # runnable connection from one that merely reports "connected" but has no
    # environment, discovery or deployed notebook behind it. It is an internal
    # identifier, not a credential.
    connection_id: str
    name: str
    platform: str
    auth_mode: str | None = None
    tenant_id: str | None
    workspace_id: str | None
    workspace_name: str | None
    status: str
    last_error_code: str | None
    last_error_message: str | None
    last_verified_at: datetime | None
    last_discovered_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ConnectionTestOut(BaseModel):
    platform: str
    connected: bool
    workspace_id: str | None = None
    workspace_name: str | None = None
    message: str
    error_code: str | None = None
    last_verified_at: str | None = None


class DiscoveredItemOut(BaseModel):
    id: str
    display_name: str
    type: str


class DiscoveryOut(BaseModel):
    discovered: bool = True
    workspace: dict[str, Any] | None = None
    items: list[DiscoveredItemOut] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    discovered_at: str | None = None
    error_code: str | None = None
    message: str | None = None


class DomainReadinessOut(BaseModel):
    asset_available: bool
    deployed: bool
    display_name: str
    platform_resource_id: str | None = None
    ready: bool
    error_code: str | None = None
    message: str | None = None


class ProvisioningOut(BaseModel):
    """Safe provisioning state. Contains Fabric item IDs (not secret) and typed
    error codes — never credentials."""

    status: str
    step: str | None = None
    package_name: str
    package_version_available: str
    package_version_installed: str | None = None
    deployable: bool
    namespace: str
    last_provisioned_at: str | None = None
    domains: dict[str, DomainReadinessOut] = Field(default_factory=dict)
    error_code: str | None = None
    message: str | None = None
    missing_assets: list[str] = Field(default_factory=list)
    # Deployment bindings, all resolved from discovery/configuration (ids, names).
    default_lakehouse: dict[str, str] | None = None
    lakehouse_binding: dict[str, Any] | None = None
    fabric_environment: dict[str, str] | None = None
    execution_type: str = "notebook"
    pipeline: dict[str, Any] | None = None


class ReadinessOut(BaseModel):
    """Drives the Readiness panel. Every flag is derived from persisted real state."""

    authentication: bool
    workspace_access: bool
    environment_discovery: bool
    # Step 3: package + per-domain readiness. ready_for_analysis requires these.
    package_status: str
    cluster_ready: bool
    query_ready: bool
    storage_ready: bool
    ready_for_analysis: bool
    # Whether the Cluster domain alone can run. Cluster does not depend on the
    # Query or Storage notebooks, so an environment where those are missing or
    # failed can still be fully usable for Cluster optimization.
    cluster_ready_for_analysis: bool
    # Per-domain settings the Cluster notebook needs (source/result table).
    cluster_configured: bool
    cluster_blocked_reason: str | None = None
    status: str
