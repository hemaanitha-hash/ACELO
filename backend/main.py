import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from error_handlers import add_exception_handlers
from api import approvals, connections, environments, file_analysis, history, jobs, optimizations, overview, runs
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

app = FastAPI(title="ACELO Enterprise API", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(connections.router)
app.include_router(environments.router)
app.include_router(jobs.router)
app.include_router(file_analysis.router)
app.include_router(overview.router)
app.include_router(approvals.router)
add_exception_handlers(app)
app.include_router(optimizations.router)
app.include_router(history.router)
app.include_router(runs.router)
app.include_router(runs.notifications_router)


@app.get("/api/health")
def health():
    return {"ok": True, "env": settings.APP_ENV, "greeting": "Good morning, Hema"}

