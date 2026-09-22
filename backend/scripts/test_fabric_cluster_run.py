#!/usr/bin/env python3
"""
Real, end-to-end Cluster Analysis test — exercises the ACTUAL ACELO backend
code (database.py, models, agent/orchestrator.py, services/job_service.py,
platforms/fabric.py) against your real Fabric workspace. Nothing in this
script is mocked; every HTTP/SQL call it triggers is real.

Run scripts/test_fabric_connection.py first — this script assumes that
already passed.

Usage (from backend/, with your .env values exported into the environment):
    python scripts/test_fabric_cluster_run.py

Exit code 0 only if the real Fabric notebook run reaches COMPLETED and the
result table read succeeds. Any other outcome exits non-zero with the exact
error printed — this script does not paper over failures.
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REQUIRED_ENV = [
    "SECRET_ENCRYPTION_KEY",
    "FABRIC_TENANT_ID",
    "FABRIC_CLIENT_ID",
    "FABRIC_CLIENT_SECRET",
    "FABRIC_WORKSPACE_ID",
    "FABRIC_NOTEBOOK_ITEM_ID",
    "FABRIC_SQL_ENDPOINT",
    "FABRIC_LAKEHOUSE_DATABASE",
]

POLL_INTERVAL_SECONDS = 15
TIMEOUT_SECONDS = int(os.environ.get("FABRIC_TEST_TIMEOUT_SECONDS", "900"))  # 15 minutes default

TEST_CUSTOMER_NAME = "Fabric Real Test Customer"
TEST_WORKSPACE_LABEL = "acelo_demo (real Fabric test)"


def _section(title: str) -> None:
    print(f"\n=== {title} ===")


async def main() -> None:
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        print(f"FAIL: missing environment variables: {', '.join(missing)}")
        print("Set these (see backend/.env.example) and re-run.")
        sys.exit(1)

    from agent.orchestrator import start_agent_job
    from agent.router import build_adapter
    from database import Base, SessionLocal, engine
    from models import AnalysisJob, Connection, Customer, JobStatus
    from platforms.base import PlatformCapabilityNotImplemented
    from services import job_service
    from services.crypto import get_cipher

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    try:
        # --- Customer + Connection setup (idempotent: reuse if this script ran before) ---
        _section("Setup")
        customer = db.query(Customer).filter(Customer.name == TEST_CUSTOMER_NAME).first()
        if not customer:
            customer = Customer(name=TEST_CUSTOMER_NAME)
            db.add(customer)
            db.commit()
            db.refresh(customer)
        print(f"Customer: {customer.id} ({customer.name})")

        connection = (
            db.query(Connection)
            .filter(Connection.customer_id == customer.id, Connection.workspace == TEST_WORKSPACE_LABEL)
            .first()
        )
        auth_metadata = {
            "tenant_id": os.environ["FABRIC_TENANT_ID"],
            "client_id": os.environ["FABRIC_CLIENT_ID"],
            "workspace_id": os.environ["FABRIC_WORKSPACE_ID"],
            "notebook_item_id": os.environ["FABRIC_NOTEBOOK_ITEM_ID"],
            "sql_endpoint": os.environ["FABRIC_SQL_ENDPOINT"],
            "lakehouse_database": os.environ["FABRIC_LAKEHOUSE_DATABASE"],
        }
        if not connection:
            connection = Connection(
                customer_id=customer.id,
                platform="fabric",
                workspace=TEST_WORKSPACE_LABEL,
                endpoint="https://api.fabric.microsoft.com",
                auth_method="service_principal",
                auth_metadata=json.dumps(auth_metadata),
                secret_encrypted=get_cipher().encrypt(os.environ["FABRIC_CLIENT_SECRET"]),
                status="pending",
            )
            db.add(connection)
            db.commit()
            db.refresh(connection)
            print(f"Connection created: {connection.id}")
        else:
            # Keep config in sync with the current environment in case it changed.
            connection.auth_metadata = json.dumps(auth_metadata)
            connection.secret_encrypted = get_cipher().encrypt(os.environ["FABRIC_CLIENT_SECRET"])
            db.commit()
            print(f"Connection reused: {connection.id}")

        # --- 1. Connection test (real Azure AD + Fabric API call) ---
        _section("1. Connection test")
        adapter = build_adapter(connection)
        test_result = await adapter.test_connection()
        if not test_result.ok:
            connection.status = "failed"
            connection.last_error = test_result.message
            db.commit()
            print(f"FAIL: {test_result.message}")
            sys.exit(1)
        connection.status = "connected"
        connection.last_error = None
        db.commit()
        print(f"PASS: {test_result.message} ({test_result.detail})")

        # --- 2/3. Create AnalysisJob/JobRun, start the real Fabric notebook ---
        _section("2/3. Starting Cluster Analysis (real Fabric notebook trigger)")
        try:
            analysis_job = await start_agent_job(db, customer.id, connection, "Analyze my cluster")
        except Exception as exc:  # noqa: BLE001 - surface the exact error, no bypass
            print(f"FAIL: could not start the job: {exc}")
            sys.exit(1)

        print(f"AnalysisJob: id={analysis_job.id} intent={analysis_job.intent} status={analysis_job.status}")
        if not analysis_job.job_runs:
            print("FAIL: no JobRun was created.")
            sys.exit(1)

        run = analysis_job.job_runs[0]
        print(f"JobRun: id={run.id} domain={run.domain} status={run.status}")

        if run.status == JobStatus.FAILED.value:
            print(f"FAIL: Fabric rejected the job at start time: {run.error}")
            sys.exit(1)

        if not run.platform_run_id:
            print("FAIL: no real Fabric run id was captured — this must not happen if start_analysis succeeded.")
            sys.exit(1)

        print(f"Fabric Run ID (real, from Fabric's Location header): {run.platform_run_id}")

        # --- 4. Poll GET-equivalent status until terminal ---
        _section("4. Polling real Fabric status")
        started = time.monotonic()
        last_status = run.status
        while run.status not in {s.value for s in JobStatus.terminal()}:
            if time.monotonic() - started > TIMEOUT_SECONDS:
                print(f"FAIL: timed out after {TIMEOUT_SECONDS}s waiting for Fabric to finish. Last status: {run.status}")
                sys.exit(1)
            time.sleep(POLL_INTERVAL_SECONDS)
            try:
                run = await job_service.sync_run_status(db, connection, run)
            except Exception as exc:  # noqa: BLE001
                print(f"FAIL: status poll raised an error: {exc}")
                sys.exit(1)
            if run.status != last_status:
                print(f"Status: {run.status}" + (f" (current_step={run.current_step})" if run.current_step else ""))
                last_status = run.status

        # --- Logs (whatever Fabric's Job Scheduler API actually exposes) ---
        _section("Logs (from the real Fabric job instance)")
        logs = await job_service.fetch_run_logs(db, connection, run)
        for log in logs:
            print(f"[{log.level}] {log.timestamp} — {log.message}")

        # --- 5. Did the real notebook succeed? ---
        _section("5. Final Fabric status")
        print(f"Status: {run.status}")
        if run.status != JobStatus.COMPLETED.value:
            print(f"FAIL: Fabric run did not complete successfully. error={run.error}")
            sys.exit(1)
        print("PASS: Fabric reports this run as Completed.")

        # --- 6/7/8. Read the actual result table ---
        _section("6/7/8. Reading data_Demo.acelo_cluster_optimization_results")
        try:
            result = await adapter.get_run_result(run.platform_run_id)
        except PlatformCapabilityNotImplemented as exc:
            print(f"FAIL: {exc}")
            sys.exit(1)
        except Exception as exc:  # noqa: BLE001 - real SQL/driver error, not swallowed
            print(f"FAIL: could not read the result table: {exc}")
            sys.exit(1)

        row_count = result.payload.get("row_count", 0)
        rows = result.payload.get("rows", [])
        print(f"Result table: {result.result_reference}")
        print(f"Result rows: {row_count}")
        for row in rows[:20]:
            print(f"  {row}")
        if row_count == 0:
            print(
                "NOTE: the query succeeded but returned 0 rows. The Fabric API flow is real and complete; "
                "check the notebook itself if you expected rows here."
            )

        # --- 9. History (direct DB query — no /api/history endpoint exists yet, see report) ---
        _section("9. Job history for this customer (direct DB query)")
        jobs = (
            db.query(AnalysisJob)
            .filter(AnalysisJob.customer_id == customer.id)
            .order_by(AnalysisJob.created_at.desc())
            .all()
        )
        for j in jobs:
            print(f"  {j.created_at} — {j.id} — status={j.status} — intent={j.intent}")

        _section("RESULT")
        print(f"SUCCESS: real Fabric Cluster Analysis completed. Run ID={run.platform_run_id}, rows={row_count}")

    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
