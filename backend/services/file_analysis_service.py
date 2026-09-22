"""
File-analysis execution path: uploaded CSV -> ACELO run -> Cluster optimizer.

The file is a platform adapter in all but name: it supplies rows, and the
analysis is the same `optimizers.cluster_optimizer` logic every platform uses.
Nothing here computes a recommendation itself.

Safety rules:
  * The upload is parsed as data only (pandas CSV reader, every cell read as a
    string first). It is never imported, evaluated or executed.
  * Size and row counts are bounded before parsing.
  * Stored under a random name in a private temp directory and deleted once the
    run finishes. The user's filename is kept only as a display label.
  * Errors returned to the client name columns and rows, never server paths,
    stack traces or configuration.
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd
from sqlalchemy.orm import Session

from database import SessionLocal
from models import AnalysisJob, Connection, Customer, JobRun, JobStatus
from optimizers.cluster_optimizer import (
    NOTEBOOK_FILLNA,
    NUMERIC_COLUMNS,
    REQUIRED_COLUMNS,
    ClusterOptimizerInputError,
    groq_llm_from_env,
    run_cluster_optimization,
)
from platforms.errors import ErrorCode
from services import job_service

logger = logging.getLogger("acelo.file_analysis")

FILE_PLATFORM = "file"
MAX_UPLOAD_BYTES = int(os.getenv("ACELO_MAX_UPLOAD_BYTES", str(5 * 1024 * 1024)))  # 5 MB
MAX_ROWS = int(os.getenv("ACELO_MAX_UPLOAD_ROWS", "50000"))
MAX_REPORTED_ERRORS = 20
ALLOWED_EXTENSIONS = (".csv",)

# Blank is acceptable only where the optimizer itself defines the value
# (the notebook's fillna, plus its coalesce(current_workers, 1)). Every other
# numeric field must be present: ACELO never invents telemetry.
_BLANK_ALLOWED = set(NOTEBOOK_FILLNA) | {"current_workers"}
_PERCENT_COLUMNS = {"avg_cpu_util", "avg_memory_util"}
_INTEGER_COLUMNS = {"current_workers", "min_workers", "max_workers", "total_jobs_run"}
_REQUIRED_TEXT = {"cluster_id"}


class FileValidationError(Exception):
    """A user-facing rejection of an upload. `status_code` maps to HTTP."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 422,
        missing_columns: list[str] | None = None,
        invalid_values: list[dict] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.missing_columns = missing_columns or []
        self.invalid_values = invalid_values or []

    def to_detail(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "missing_columns": self.missing_columns,
            "invalid_values": self.invalid_values,
            "required_columns": REQUIRED_COLUMNS,
        }


@dataclass
class ValidatedDataset:
    frame: pd.DataFrame
    row_count: int
    extra_columns: list[str] = field(default_factory=list)


def safe_display_name(filename: str | None) -> str:
    """Basename only, printable characters, bounded length. Never a path."""
    name = os.path.basename((filename or "").replace("\\", "/")) or "upload.csv"
    name = re.sub(r"[^\w.\- ()]", "_", name)
    return name[:120]


def validate_cluster_csv(raw: bytes, filename: str | None = None) -> ValidatedDataset:
    """Parses and validates an uploaded Cluster dataset. Raises FileValidationError."""
    if filename and not safe_display_name(filename).lower().endswith(ALLOWED_EXTENSIONS):
        raise FileValidationError(
            "UNSUPPORTED_FILE_TYPE",
            "Only CSV files are supported for cluster analysis right now.",
            status_code=415,
        )
    if len(raw) > MAX_UPLOAD_BYTES:
        raise FileValidationError(
            "FILE_TOO_LARGE",
            f"The file is larger than the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit.",
            status_code=413,
        )
    if not raw.strip():
        raise FileValidationError("EMPTY_FILE", "The uploaded file is empty.", status_code=400)
    if b"\x00" in raw:
        raise FileValidationError(
            "MALFORMED_FILE", "The file is not a text CSV file.", status_code=400
        )

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise FileValidationError(
            "MALFORMED_FILE", "The file is not valid UTF-8 text. Save it as a UTF-8 CSV.", status_code=400
        ) from None

    try:
        frame = pd.read_csv(
            io.StringIO(text),
            dtype=str,
            keep_default_na=False,
            skip_blank_lines=True,
            nrows=MAX_ROWS + 1,
        )
    except pd.errors.EmptyDataError:
        raise FileValidationError("EMPTY_FILE", "The uploaded file has no header row.", status_code=400) from None
    except (pd.errors.ParserError, ValueError) as exc:
        logger.info("file_analysis event=csv_parse_failed error=%s", type(exc).__name__)
        raise FileValidationError(
            "MALFORMED_FILE",
            "The file could not be read as CSV. Check that every row has the same number of columns.",
            status_code=400,
        ) from None

    frame.columns = [str(c).strip().lower() for c in frame.columns]
    duplicates = sorted({c for c in frame.columns if list(frame.columns).count(c) > 1})
    if duplicates:
        raise FileValidationError(
            "DUPLICATE_COLUMNS", "The file has duplicate columns: " + ", ".join(duplicates) + "."
        )

    missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
    if missing:
        raise FileValidationError(
            "MISSING_COLUMNS",
            "The file is missing required columns: " + ", ".join(missing) + ".",
            missing_columns=missing,
        )

    if len(frame) == 0:
        raise FileValidationError("EMPTY_FILE", "The file has a header but no data rows.", status_code=400)
    if len(frame) > MAX_ROWS:
        raise FileValidationError(
            "TOO_MANY_ROWS", f"The file has more than {MAX_ROWS:,} rows.", status_code=413
        )

    frame = frame.apply(lambda s: s.str.strip())
    errors: list[dict] = []

    def reject(row_index: int, column: str, value: str, reason: str) -> None:
        if len(errors) < MAX_REPORTED_ERRORS:
            # +2: one for the header line, one because CSV rows are 1-based.
            errors.append({"row": int(row_index) + 2, "column": column, "value": value[:40], "reason": reason})

    for column in _REQUIRED_TEXT:
        for idx in frame.index[frame[column] == ""]:
            reject(idx, column, "", "is required")

    typed = frame.copy()
    for column in NUMERIC_COLUMNS:
        values = frame[column]
        parsed = pd.to_numeric(values.where(values != ""), errors="coerce")
        for idx in frame.index:
            raw_value = values[idx]
            number = parsed[idx]
            if raw_value == "":
                if column not in _BLANK_ALLOWED:
                    reject(idx, column, raw_value, "is required")
                continue
            if pd.isna(number) or not math.isfinite(float(number)):
                reject(idx, column, raw_value, "is not a number")
            elif number < 0:
                reject(idx, column, raw_value, "must not be negative")
            elif column in _PERCENT_COLUMNS and number > 100:
                reject(idx, column, raw_value, "must be a percentage between 0 and 100")
            elif column in _INTEGER_COLUMNS and float(number) != int(number):
                reject(idx, column, raw_value, "must be a whole number")
        typed[column] = parsed.astype(float)

    bad_bounds = typed.index[typed["min_workers"] > typed["max_workers"]]
    for idx in bad_bounds:
        reject(idx, "min_workers", frame.at[idx, "min_workers"], "is greater than max_workers")

    if errors:
        raise FileValidationError(
            "INVALID_VALUES",
            f"{len(errors)}{'+' if len(errors) == MAX_REPORTED_ERRORS else ''} invalid value(s) found. "
            "Fix them and upload again.",
            invalid_values=errors,
        )

    extras = [c for c in typed.columns if c not in REQUIRED_COLUMNS]
    return ValidatedDataset(frame=typed, row_count=len(typed), extra_columns=extras)


# --- storage -----------------------------------------------------------------

def _upload_dir() -> str:
    path = os.getenv("ACELO_UPLOAD_DIR") or os.path.join(tempfile.gettempdir(), "acelo_uploads")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def store_upload(raw: bytes) -> str:
    """Writes the upload under a random name. Returns the server-side path (never sent to clients)."""
    path = os.path.join(_upload_dir(), f"{uuid.uuid4().hex}.csv")
    with open(path, "wb") as fh:
        fh.write(raw)
    return path


def _discard(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# --- run lifecycle -----------------------------------------------------------

def get_or_create_file_connection(db: Session, customer: Customer) -> Connection:
    """
    AnalysisJob requires a connection. Uploaded files get one internal
    connection per customer: no endpoint, no credential, never polled.
    """
    connection = (
        db.query(Connection)
        .filter(Connection.customer_id == customer.id, Connection.platform == FILE_PLATFORM)
        .first()
    )
    if connection:
        return connection
    connection = Connection(
        customer_id=customer.id,
        platform=FILE_PLATFORM,
        workspace="File upload",
        endpoint="file://upload",
        auth_method="none",
        status="connected",
    )
    db.add(connection)
    db.commit()
    db.refresh(connection)
    return connection


def create_file_run(
    db: Session, customer: Customer, prompt: str, display_name: str, focus: str | None
) -> tuple[AnalysisJob, JobRun]:
    connection = get_or_create_file_connection(db, customer)
    job = job_service.create_analysis_job(db, customer.id, connection, request=prompt, intent="cluster")
    run = job_service.create_job_run(db, job, "cluster")
    # No platform_run_id: there is no external platform job. That also keeps the
    # platform polling paths (sync, logs, results fetch) from ever engaging.
    run.result_reference = f"upload:{display_name}"
    run.current_step = f"focus:{focus}" if focus else None
    db.commit()
    job_service.append_log(db, run, "INFO", f"Received file '{display_name}' for cluster analysis.", source="file")
    return job, run


def execute_file_run(job_run_id: str, stored_path: str, display_name: str, focus: str | None = None) -> None:
    """
    Background worker body. Runs in FastAPI's thread pool (the optimizer is
    CPU-bound), with its own DB session. Never raises: every outcome is
    recorded on the run.
    """
    db = SessionLocal()
    try:
        run = db.query(JobRun).filter(JobRun.id == job_run_id).first()
        if not run:
            return
        if run.status in job_service.TERMINAL_STATUSES:  # cancelled before it started
            return

        run.status = JobStatus.RUNNING.value
        run.started_at = datetime.utcnow()
        db.commit()
        job_service._recompute_analysis_job_status(db, run.analysis_job_id)
        job_service.append_log(db, run, "INFO", "Running ACELO Cluster optimizer.", source="file")

        try:
            with open(stored_path, "rb") as fh:
                dataset = validate_cluster_csv(fh.read())
            llm = groq_llm_from_env()
            result = run_cluster_optimization(dataset.frame, llm=llm)
        except (FileValidationError, ClusterOptimizerInputError) as exc:
            _fail(db, run, str(exc), ErrorCode.INVALID_CONFIGURATION)
            return
        except Exception:  # noqa: BLE001 - detail goes to the server log only
            logger.exception("file_analysis event=optimizer_failed run_id=%s", job_run_id)
            _fail(db, run, "The cluster optimizer failed while analysing this file.", ErrorCode.EXECUTION_FAILED)
            return

        db.refresh(run)
        if run.status in job_service.TERMINAL_STATUSES:  # cancelled while running
            return

        rows = _json_rows(result)
        payload = {
            "platform": FILE_PLATFORM,
            "environment_connection_id": run.analysis_job.connection_id,
            "optimization_type": "cluster",
            "run_id": run.id,
            "platform_run_id": None,
            "platform_resource_id": None,
            "result_reference": run.result_reference,
            "retrieved_at": datetime.utcnow().isoformat(),
            "row_count": len(rows),
            "source_file": display_name,
            "focus": focus,
            "llm_enabled": llm is not None,
            "summary": summarise(rows),
            "source_payload": {"rows": rows, "row_count": len(rows), "extra_columns": dataset.extra_columns},
        }
        run.result_json = json.dumps(payload)
        run.status = JobStatus.COMPLETED.value
        run.progress = 100.0
        run.current_step = None
        run.completed_at = datetime.utcnow()
        db.commit()
        job_service.append_log(db, run, "INFO", f"Analysed {len(rows)} clusters.", source="file")
        job_service._recompute_analysis_job_status(db, run.analysis_job_id)
        from services import approval_service

        approval_service.safe_sync(db, run, payload)
    except Exception:  # noqa: BLE001 - a worker failure must never take the app down
        logger.exception("file_analysis event=worker_error run_id=%s", job_run_id)
    finally:
        db.close()
        _discard(stored_path)


def _fail(db: Session, run: JobRun, message: str, code: str) -> None:
    run.status = JobStatus.FAILED.value
    run.error = message
    run.error_code = code
    run.completed_at = datetime.utcnow()
    db.commit()
    job_service.append_log(db, run, "ERROR", message, source="file")
    job_service._recompute_analysis_job_status(db, run.analysis_job_id)


def _json_rows(frame: pd.DataFrame) -> list[dict]:
    """Optimizer output as plain JSON values (numpy scalars and NaN normalised)."""
    return json.loads(frame.to_json(orient="records", double_precision=10))


def summarise(rows: list[dict]) -> dict:
    """Aggregates over the optimizer's own output fields. Nothing is estimated here."""

    def nums(key: str) -> list[float]:
        return [float(r[key]) for r in rows if isinstance(r.get(key), (int, float))]

    def total(values: list[float]) -> float | None:
        return round(sum(values), 2) if values else None

    def mean(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 2) if values else None

    labels = [r.get("optimization_label") for r in rows]
    return {
        "total_clusters": len(rows),
        # Presentation names for the optimizer's three optimization_label values.
        "healthy": labels.count("Optimized"),
        "at_risk": labels.count("Moderately Optimized"),
        "critical": labels.count("Risky"),
        "avg_cpu_util": mean(nums("avg_cpu_util")),
        "avg_memory_util": mean(nums("avg_memory_util")),
        "idle_clusters": sum(1 for r in rows if r.get("idle_flag") == 1),
        "oversized_clusters": sum(1 for r in rows if r.get("oversized_flag") == 1),
        # None (not 0) when no row carries the value: missing is not zero.
        "current_cost_usd": total(nums("total_dbus_cost_usd")),
        "estimated_monthly_savings_usd": total(nums("potential_monthly_savings")),
    }
