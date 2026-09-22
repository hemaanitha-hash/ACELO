from datetime import datetime
from typing import Any

from pydantic import BaseModel


class JobRunOut(BaseModel):
    id: str
    domain: str
    platform: str
    platform_run_id: str | None
    status: str
    current_step: str | None
    progress: float | None
    error: str | None
    error_code: str | None = None  # a platforms.errors.ErrorCode value, safe for the UI
    result_reference: str | None = None
    platform_resource_id: str | None = None
    execution_type: str | None = None  # "notebook" | "pipeline" for Fabric runs
    started_at: datetime | None
    completed_at: datetime | None

    model_config = {"from_attributes": True}


class AnalysisJobOut(BaseModel):
    id: str
    connection_id: str
    request: str
    intent: str
    platform: str
    status: str
    current_step: str | None
    progress: float | None
    error: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    job_runs: list[JobRunOut]

    model_config = {"from_attributes": True}


class JobLogOut(BaseModel):
    id: str
    job_run_id: str
    timestamp: datetime
    level: str
    message: str
    source: str | None

    model_config = {"from_attributes": True}


class JobResultOut(BaseModel):
    job_run_id: str
    domain: str
    status: str
    available: bool
    payload: dict[str, Any] | None
    error: str | None = None
    error_code: str | None = None
