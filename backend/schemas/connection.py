from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Platform = Literal["databricks", "fabric"]


class ConnectionCreate(BaseModel):
    platform: Platform
    workspace: str = Field(..., description="Display name, e.g. 'Production Lakehouse'")
    endpoint: str = Field(..., description="Workspace URL (Databricks) or Fabric API base")
    auth_method: str
    auth_metadata: dict[str, Any] = Field(default_factory=dict, description="Non-secret fields, e.g. tenant_id, client_id")
    secret: str | None = Field(None, description="PAT or client secret — encrypted immediately, never echoed back")


class ConnectionOut(BaseModel):
    id: str
    platform: Platform
    workspace: str
    endpoint: str
    auth_method: str
    status: str
    last_error: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ConnectionTestResult(BaseModel):
    ok: bool
    message: str
    detail: dict[str, Any] | None = None
