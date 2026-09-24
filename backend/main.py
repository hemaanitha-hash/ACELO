import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from error_handlers import add_exception_handlers
from api import approvals, connections, databricks, environments, file_analysis, history, jobs, optimizations, overview, runs
from config import get_settings
from database import Base, SessionLocal, engine, ensure_columns

Base.metadata.create_all(bind=engine)
ensure_columns()  # additive-only migration for pre-existing databases

# NOTE: startup no longer seeds demo data. It previously called
# init_default_data(), which inserted a hardcoded customer plus two fabricated
# platform connections (invented tenant/client GUIDs) and four hardcoded
# "recommendations" into every database on every boot. Dashboard screens read
# those rows, so the product displayed invented costs and savings as if they
# were analysis output. Screens now read real data only and show an honest
# empty state until a real run produces results.

settings = get_settings()

# Operational logging. Environment/platform operations log IDs, outcomes and
# safe error codes only - never secrets or tokens (see services/environment_service).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

# The development encryption key is published in config.py, so using it in
# production means stored connection secrets are readable by anyone with the
# source. A Databricks App stores none, so this is a warning rather than a
# refusal to boot — but it must never pass unnoticed.
if settings.is_production and settings.uses_dev_encryption_key:
    logging.getLogger("acelo").error(
        "SECRET_ENCRYPTION_KEY is unset — falling back to the built-in development key. "
        "Set it (an App secret in Databricks Apps) before storing any credential."
    )

app = FastAPI(title="ACELO Enterprise API", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(connections.router)
app.include_router(databricks.router)
app.include_router(environments.router)
app.include_router(jobs.router)
app.include_router(file_analysis.router)
app.include_router(overview.router)
app.include_router(overview.clusters_router)
app.include_router(approvals.router)
add_exception_handlers(app)
app.include_router(optimizations.router)
app.include_router(history.router)
app.include_router(runs.router)
app.include_router(runs.notifications_router)


# --- Databricks App runtime -------------------------------------------------
#
# When ACELO runs as a Databricks App the workspace is decided by the runtime,
# so the App's own workspace is registered as the active environment on start.
# Outside an App this is a no-op and the existing configured-connection flow is
# untouched.
try:
    _session = SessionLocal()
    try:
        from services.app_bootstrap import ensure_app_environment

        ensure_app_environment(_session)
    finally:
        _session.close()
except Exception:  # noqa: BLE001 - a bootstrap fault must not stop the API booting
    logging.getLogger("acelo").exception("databricks_app_bootstrap_failed")


@app.get("/api/runtime")
def runtime():
    """
    How this ACELO instance is deployed and authenticated.

    The frontend reads this to decide whether to ask for a workspace URL and
    token at all. It carries no credential — only where we are and which
    mechanism is in use.
    """
    from platforms import databricks_auth

    return databricks_auth.describe()


@app.get("/api/health")
def health():
    return {"ok": True, "env": settings.APP_ENV, "greeting": "Good morning, Hema"}


# --- built frontend ---------------------------------------------------------
#
# A Databricks App runs ONE process, so the API also serves the built SPA when
# `dist/` is present. In local development Vite serves the frontend instead and
# this mount simply does not exist, so nothing about the dev workflow changes.
_DIST = Path(__file__).resolve().parent.parent / "dist"
if _DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="frontend")
    logging.getLogger("acelo").info("serving_frontend_from path=%s", _DIST)

