from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from config import get_settings

settings = get_settings()

connect_args = {"check_same_thread": False} if settings.DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(settings.DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_columns() -> None:
    """
    Minimal additive migration for SQLite/Postgres.

    `Base.metadata.create_all()` creates missing TABLES but never adds missing
    COLUMNS to a table that already exists, so an existing ACELO database would
    silently lack the Step 2 execution columns. This adds only what is missing
    and never drops or alters anything, so it is safe to run on every startup.
    """
    from sqlalchemy import inspect, text

    additions = {
        "job_runs": {
            "error_code": "VARCHAR",
            "result_json": "TEXT",
            "platform_resource_id": "VARCHAR",
            "execution_type": "VARCHAR",
            "environment_id": "VARCHAR",
            "created_by": "VARCHAR",
            "retry_of_run_id": "VARCHAR",
        },
        "job_logs": {
            "event_type": "VARCHAR",
            "stage": "VARCHAR",
            "platform_run_id": "VARCHAR",
            "metadata_json": "TEXT",
        },
        "environments": {
            "auth_mode": "VARCHAR",
            "provisioning_status": "VARCHAR",
            "package_version": "VARCHAR",
            "provisioning_step": "VARCHAR",
            "provisioning_detail_json": "TEXT",
            "last_provisioned_at": "DATETIME",
        },
    }

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    _rebuild_if_empty_and_outdated(inspector, existing_tables)
    inspector = inspect(engine)

    with engine.begin() as conn:
        for table, columns in additions.items():
            if table not in existing_tables:
                continue  # create_all() will build it with the columns already present
            present = {col["name"] for col in inspector.get_columns(table)}
            for name, sql_type in columns.items():
                if name not in present:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))


def _rebuild_if_empty_and_outdated(inspector, existing_tables: set[str]) -> None:
    """
    optimization_approvals / approval_audit gained a nullable acelo_run_id and
    a source_ref identity. SQLite cannot relax NOT NULL in place, so an
    outdated copy of these tables is rebuilt ONLY when it holds no rows.
    A table with data is never dropped; it is left for a manual migration.
    """
    from sqlalchemy import text

    for name in ("approval_audit", "optimization_approvals"):
        if name not in existing_tables:
            continue
        columns = {c["name"]: c for c in inspector.get_columns(name)}
        outdated = not columns.get("acelo_run_id", {}).get("nullable", True) or (
            name == "optimization_approvals" and "source_ref" not in columns
        )
        if not outdated:
            continue
        with engine.begin() as conn:
            if conn.execute(text(f"SELECT COUNT(*) FROM {name}")).scalar():
                continue
        Base.metadata.tables[name].drop(bind=engine)
        Base.metadata.tables[name].create(bind=engine)
