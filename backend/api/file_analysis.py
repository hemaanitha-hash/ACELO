from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from agent.orchestrator import UnsupportedFileRequest, plan_file_request
from api.deps import get_current_customer
from database import get_db
from models import Customer
from schemas.job import AnalysisJobOut
from services import file_analysis_service as files

router = APIRouter(prefix="/api/file-analysis", tags=["file-analysis"])

_READ_CHUNK = 64 * 1024


async def _read_bounded(upload: UploadFile) -> bytes:
    """Reads at most MAX_UPLOAD_BYTES + 1, so an oversized upload is never fully buffered."""
    chunks, total = [], 0
    while True:
        chunk = await upload.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        chunks.append(chunk)
        if total > files.MAX_UPLOAD_BYTES:
            break
    return b"".join(chunks)


def _error(exc: files.FileValidationError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.to_detail()})


@router.post("/cluster", response_model=AnalysisJobOut, status_code=202)
async def analyze_cluster_file(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    prompt: str | None = Form(default=None),
    db: Session = Depends(get_db),
    customer: Customer = Depends(get_current_customer),
):
    """
    Upload a Cluster telemetry CSV and analyse it with the ACELO Cluster
    optimizer. No Fabric or Databricks connection is involved.

    Validation happens before a run is created, so a bad file never becomes a
    run. The optimizer then executes in the background; poll
    GET /api/jobs/{id} and read GET /api/jobs/{id}/results as for any run.
    """
    try:
        text, focus = plan_file_request(prompt)
    except UnsupportedFileRequest as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": {"code": "UNSUPPORTED_DOMAIN", "message": str(exc),
                                "missing_columns": [], "invalid_values": []}},
        )

    display_name = files.safe_display_name(file.filename)
    raw = await _read_bounded(file)
    try:
        files.validate_cluster_csv(raw, display_name)
    except files.FileValidationError as exc:
        return _error(exc)

    job, run = files.create_file_run(db, customer, text, display_name, focus)
    stored = files.store_upload(raw)
    background.add_task(files.execute_file_run, run.id, stored, display_name, focus)

    db.refresh(job)
    return job


@router.get("/cluster/schema")
def cluster_schema():
    """What a Cluster upload must contain. Lets the UI show requirements up front."""
    return {
        "required_columns": files.REQUIRED_COLUMNS,
        "numeric_columns": files.NUMERIC_COLUMNS,
        "max_upload_bytes": files.MAX_UPLOAD_BYTES,
        "max_rows": files.MAX_ROWS,
        "formats": [".csv"],
    }
