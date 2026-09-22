"""
Step 5 — live Fabric validation of the Cluster optimization path.

Drives ACELO's REAL service layer end to end: connect -> discover -> provision ->
register -> execute -> poll -> retrieve. Nothing here is mocked and nothing is
simulated; every step either performs a real Fabric call or stops.

    python scripts/live_cluster_validation.py

Required environment (see .env.example):
    SECRET_ENCRYPTION_KEY, FABRIC_TENANT_ID, FABRIC_CLIENT_ID,
    FABRIC_CLIENT_SECRET, FABRIC_WORKSPACE_ID

Required for reading results back:
    FABRIC_SQL_ENDPOINT, FABRIC_LAKEHOUSE_DATABASE

Required for the notebook to have something to analyse:
    ACELO_CLUSTER_SOURCE_TABLE   customer telemetry table (see RESULT_SCHEMA.md)
    ACELO_CLUSTER_RESULT_TABLE   ACELO-owned result table (created on first write)

Optional (LLM enrichment; without it scoring still runs and the LLM column
records unavailability):
    ACELO_LLM_KEY_VAULT_URI, ACELO_LLM_SECRET_NAME

Exit code 0 only if a real Fabric run completed and real rows came back.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REQUIRED = [
    "SECRET_ENCRYPTION_KEY",
    "FABRIC_TENANT_ID",
    "FABRIC_CLIENT_ID",
    "FABRIC_CLIENT_SECRET",
    "FABRIC_WORKSPACE_ID",
]

POLL_TIMEOUT = int(os.getenv("ACELO_LIVE_TIMEOUT_SECONDS", "1800"))
POLL_INTERVAL = 15


def fail(step: str, detail: str) -> None:
    print(f"\nFAIL [{step}] {detail}")
    print("\n=== RESULT ===\nLIVE FABRIC EXECUTION NOT VERIFIED.")
    sys.exit(1)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


async def main() -> None:
    section("0. Configuration")
    missing = [name for name in REQUIRED if not os.getenv(name)]
    if missing:
        fail("config", "missing environment variables: " + ", ".join(missing))

    source_table = os.getenv("ACELO_CLUSTER_SOURCE_TABLE", "").strip()
    result_table = os.getenv("ACELO_CLUSTER_RESULT_TABLE", "").strip()
    if not source_table or not result_table:
        fail(
            "config",
            "ACELO_CLUSTER_SOURCE_TABLE and ACELO_CLUSTER_RESULT_TABLE must be set. "
            "The notebook refuses to guess which table holds customer telemetry.",
        )
    print("All required configuration present.")

    from database import Base, SessionLocal, engine, ensure_columns
    from models import Customer
    from services import environment_service, job_service, provisioning_service

    Base.metadata.create_all(bind=engine)
    ensure_columns()
    db = SessionLocal()

    try:
        customer = db.query(Customer).filter(Customer.name == "Live Validation").first()
        if not customer:
            customer = Customer(name="Live Validation")
            db.add(customer)
            db.commit()
            db.refresh(customer)
        print(f"Customer: {customer.id}")

        # ---- 1. Environment + real connection test -----------------------
        section("1. Connection")
        environment = environment_service.create_environment(
            db,
            customer.id,
            name="Live Fabric Validation",
            platform="fabric",
            tenant_id=os.environ["FABRIC_TENANT_ID"],
            workspace_id=os.environ["FABRIC_WORKSPACE_ID"],
            client_id=os.environ["FABRIC_CLIENT_ID"],
            client_secret=os.environ["FABRIC_CLIENT_SECRET"],
            endpoint=None,
        )
        # Carry the notebook's data configuration on the connection so
        # build_run_parameters() passes it through to Fabric.
        connection = db.query(type(environment).connection.property.mapper.class_).filter_by(
            id=environment.connection_id
        ).first()
        metadata = json.loads(connection.auth_metadata)
        metadata.update(
            {
                "cluster_source_table": source_table,
                "cluster_result_table": result_table,
                "sql_endpoint": os.getenv("FABRIC_SQL_ENDPOINT", ""),
                "lakehouse_database": os.getenv("FABRIC_LAKEHOUSE_DATABASE", ""),
            }
        )
        for key, env_name in (
            ("llm_key_vault_uri", "ACELO_LLM_KEY_VAULT_URI"),
            ("llm_secret_name", "ACELO_LLM_SECRET_NAME"),
        ):
            if os.getenv(env_name):
                metadata[key] = os.environ[env_name]
        connection.auth_metadata = json.dumps(metadata)
        db.commit()

        test = await environment_service.test_environment_connection(db, environment)
        if not test["connected"]:
            fail("connection", f"{test.get('error_code')}: {test['message']}")
        print(f"PASS: {test['message']}")
        print(f"  workspace: {test['workspace_name']} ({test['workspace_id']})")

        # ---- 2. Discovery -------------------------------------------------
        section("2. Workspace discovery")
        discovery = await environment_service.discover_environment(db, environment)
        if not discovery.get("discovered", True) is not False and discovery.get("error_code"):
            fail("discovery", f"{discovery['error_code']}: {discovery['message']}")
        print(f"Discovered counts: {discovery.get('counts')}")
        for item in discovery.get("items", [])[:25]:
            print(f"  {item['type']:14} {item['display_name']}  ({item['id']})")

        # ---- 3. Provisioning ---------------------------------------------
        section("3. ACELO package provisioning")
        state = await provisioning_service.provision_environment(db, environment)
        print(f"Status: {state['status']}  package v{state.get('package_version_installed')}")
        for domain, info in state["domains"].items():
            print(
                f"  {domain:8} deployed={info['deployed']} "
                f"id={info['platform_resource_id']} {info.get('error_code') or ''}"
            )
        if state["status"] != provisioning_service.INSTALLED:
            fail("provisioning", f"{state.get('error_code')}: {state.get('message')}")

        cluster_item_id = state["domains"]["cluster"]["platform_resource_id"]
        if not cluster_item_id:
            fail("provisioning", "no Cluster notebook item ID was registered")
        print(f"PASS: ACELO Cluster notebook item ID = {cluster_item_id}")

        # ---- 4. Execute through the real job pipeline ---------------------
        section("4. Cluster execution")
        from agent.orchestrator import start_agent_job

        analysis_job = await start_agent_job(db, customer.id, connection, "Analyze my cluster")
        cluster_run = next((r for r in analysis_job.job_runs if r.domain == "cluster"), None)
        if cluster_run is None:
            fail("execution", "no cluster JobRun was created")
        if cluster_run.status == "FAILED":
            fail("execution", f"{cluster_run.error_code}: {cluster_run.error}")
        if not cluster_run.platform_run_id:
            fail("execution", "Fabric did not return a job instance ID")

        print(f"ACELO run id       : {cluster_run.id}")
        print(f"Fabric job instance: {cluster_run.platform_run_id}")
        print(f"Notebook item id   : {cluster_run.platform_resource_id}")
        print(f"Workspace id       : {environment.workspace_id}")
        started = time.time()

        # ---- 5. Poll the real job ----------------------------------------
        section("5. Polling real Fabric status")
        last = None
        while time.time() - started < POLL_TIMEOUT:
            await job_service.sync_run_status(db, connection, cluster_run)
            if cluster_run.status != last:
                print(f"  [{int(time.time() - started):5}s] {cluster_run.status}")
                last = cluster_run.status
            if cluster_run.status in job_service.TERMINAL_STATUSES:
                break
            await asyncio.sleep(POLL_INTERVAL)

        if cluster_run.status not in job_service.TERMINAL_STATUSES:
            fail("polling", f"still {cluster_run.status} after {POLL_TIMEOUT}s")
        if cluster_run.status != "COMPLETED":
            for log in await job_service.fetch_run_logs(db, connection, cluster_run):
                print(f"  [{log.level}] {log.message}")
            fail("execution", f"Fabric reported {cluster_run.status}: {cluster_run.error}")
        print(f"PASS: Fabric reports COMPLETED after {int(time.time() - started)}s")

        # ---- 6. Retrieve the real result ----------------------------------
        section("6. Result retrieval (scoped to this run)")
        result = await job_service.fetch_and_store_result(db, connection, cluster_run)
        if result is None:
            fail("results", f"{cluster_run.error_code}: {cluster_run.error}")

        rows = result["source_payload"].get("rows", [])
        print(f"Result table : {result['result_reference']}")
        print(f"acelo_run_id : {result['source_payload'].get('acelo_run_id')}")
        print(f"Row count    : {result['row_count']}")
        if not rows:
            fail(
                "results",
                "the run completed but no rows carried this acelo_run_id — "
                "check that the notebook wrote to the configured result table",
            )

        for row in rows[:3]:
            print(
                "  "
                + json.dumps(
                    {
                        k: row.get(k)
                        for k in (
                            "cluster_name",
                            "optimization_label",
                            "efficiency_score",
                            "current_workers",
                            "recommended_max_workers",
                            "predicted_savings_pct",
                            "potential_monthly_savings",
                            "acelo_run_id",
                        )
                    },
                    default=str,
                )
            )

        mismatched = [r for r in rows if r.get("acelo_run_id") != cluster_run.id]
        if mismatched:
            fail("results", f"{len(mismatched)} rows do not belong to this run")

        llm_states = {str(r.get("llm_optimization", ""))[:60] for r in rows}
        print(f"LLM column states: {len(llm_states)} distinct")
        for state_text in list(llm_states)[:3]:
            print(f"  {state_text}")

        section("RESULT")
        print("LIVE VERIFIED")
        print(f"  ACELO run        : {cluster_run.id}")
        print(f"  Fabric job       : {cluster_run.platform_run_id}")
        print(f"  Notebook item    : {cluster_run.platform_resource_id}")
        print(f"  Rows for this run: {len(rows)}")
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
