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
    silently lack additive columns. The Stage 1 recommendation extension also
    rebuilds the legacy recommendations table with its rows preserved when its
    required job_run_id must become nullable; this is safe to run on startup.
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
        "optimization_approvals": {
            "last_seen_run_id": "VARCHAR",
            "last_seen_at": "DATETIME",
            "requires_new_approval": "BOOLEAN",
            "latest_recommendation_json": "TEXT",
            "original_sql": "TEXT",
            "optimized_sql": "TEXT",
            "platform_validation_status": "VARCHAR",
        },
        "environments": {
            "auth_mode": "VARCHAR",
            "provisioning_status": "VARCHAR",
            "package_version": "VARCHAR",
            "provisioning_step": "VARCHAR",
            "provisioning_detail_json": "TEXT",
            "last_provisioned_at": "DATETIME",
        },
        "recommendations": {
            "recommendation_id": "VARCHAR",
            "customer_id": "VARCHAR",
            "environment_id": "VARCHAR",
            "workspace_name": "VARCHAR",
            "resource_id": "VARCHAR",
            "resource_type": "VARCHAR",
            "finding_id": "VARCHAR",
            "rule_id": "VARCHAR",
            "finding_type": "VARCHAR",
            "title": "VARCHAR",
            "summary": "TEXT",
            "description": "TEXT",
            "proposed_state": "TEXT",
            "evidence_json": "TEXT",
            "evidence_references_json": "TEXT",
            "evidence_quality_json": "TEXT",
            "observation_window_json": "TEXT",
            "expected_impact_json": "TEXT",
            "estimated_savings_json": "TEXT",
            "severity": "VARCHAR",
            "risk": "VARCHAR",
            "policy_status": "VARCHAR",
            "approval_status": "VARCHAR",
            "execution_status": "VARCHAR",
            "verification_status": "VARCHAR",
            "updated_at": "DATETIME",
        },
    }

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    _rebuild_if_empty_and_outdated(inspector, existing_tables)
    _ensure_recommendation_run_nullable(engine)
    inspector = inspect(engine)

    with engine.begin() as conn:
        for table, columns in additions.items():
            if table not in existing_tables:
                continue  # create_all() will build it with the columns already present
            present = {col["name"] for col in inspector.get_columns(table)}
            for name, sql_type in columns.items():
                if name not in present:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))

    with engine.begin() as conn:
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_recommendation_identity "
            "ON recommendations (recommendation_id)"
        ))


def _ensure_recommendation_run_nullable(target_engine) -> None:
    """Preserve legacy recommendation rows while making their run link optional."""
    from sqlalchemy import MetaData, Table, inspect, insert, select, text

    from models import Recommendation

    inspector = inspect(target_engine)
    if "recommendations" not in inspector.get_table_names():
        return
    columns = {column["name"]: column for column in inspector.get_columns("recommendations")}
    job_run_id = columns.get("job_run_id")
    if not job_run_id or job_run_id.get("nullable", True):
        return

    if target_engine.dialect.name == "sqlite":
        with target_engine.connect() as connection:
            connection.commit()
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
            try:
                with connection.begin():
                    old = Table("recommendations", MetaData(), autoload_with=connection)
                    replacement = Recommendation.__table__.to_metadata(
                        Recommendation.metadata, name="recommendations_phase5"
                    )
                    try:
                        replacement.create(connection)
                        common_columns = [name for name in old.c.keys() if name in replacement.c]
                        connection.execute(
                            insert(replacement).from_select(
                                common_columns,
                                select(*(old.c[name] for name in common_columns)),
                            )
                        )
                        connection.execute(text("DROP TABLE recommendations"))
                        connection.execute(text(
                            "ALTER TABLE recommendations_phase5 RENAME TO recommendations"
                        ))
                    finally:
                        Recommendation.metadata.remove(replacement)
            finally:
                connection.commit()
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.commit()
        return

    if target_engine.dialect.name == "postgresql":
        with target_engine.begin() as connection:
            connection.execute(text(
                "ALTER TABLE recommendations ALTER COLUMN job_run_id DROP NOT NULL"
            ))


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
