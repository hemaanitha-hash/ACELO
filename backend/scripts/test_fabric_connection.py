#!/usr/bin/env python3
"""
Standalone Fabric connectivity diagnostic.

Run this BEFORE test_fabric_cluster_run.py. It does not touch the ACELO
database or API — it only proves the configured service principal can reach
every system ACELO depends on, using the real Azure AD / Fabric / SQL
endpoints. Never prints the client secret or any access token.

Usage (from backend/, with your .env values exported into the environment):
    python scripts/test_fabric_connection.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import msal

FABRIC_API_BASE = "https://api.fabric.microsoft.com/v1"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"

REQUIRED_ENV = [
    "FABRIC_TENANT_ID",
    "FABRIC_CLIENT_ID",
    "FABRIC_CLIENT_SECRET",
    "FABRIC_WORKSPACE_ID",
    "FABRIC_NOTEBOOK_ITEM_ID",
    "FABRIC_SQL_ENDPOINT",
    "FABRIC_LAKEHOUSE_DATABASE",
]

_results: list[tuple[str, bool, str]] = []


def report(step: str, ok: bool, message: str = "") -> None:
    _results.append((step, ok, message))
    status = "PASS" if ok else "FAIL"
    line = f"{step}: {status}"
    if message:
        line += f" — {message}"
    print(line)


def summarize_and_exit() -> None:
    print("\n=== Summary ===")
    for step, ok, _ in _results:
        print(f"{step}: {'PASS' if ok else 'FAIL'}")

    all_pass = bool(_results) and all(ok for _, ok, _ in _results)
    if all_pass:
        print("\nAll checks passed. Safe to run scripts/test_fabric_cluster_run.py next.")
        sys.exit(0)
    else:
        print("\nOne or more checks failed. Fix the issue above before attempting a real Cluster Analysis run.")
        sys.exit(1)


def main() -> None:
    print("=== ACELO / Fabric connectivity diagnostic ===\n")

    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        report("Configuration", False, f"Missing environment variables: {', '.join(missing)}")
        print("\nSet these (see backend/.env.example) and re-run.")
        sys.exit(1)
    report("Configuration", True, "All required environment variables are set.")

    tenant_id = os.environ["FABRIC_TENANT_ID"]
    client_id = os.environ["FABRIC_CLIENT_ID"]
    client_secret = os.environ["FABRIC_CLIENT_SECRET"]
    workspace_id = os.environ["FABRIC_WORKSPACE_ID"]
    notebook_item_id = os.environ["FABRIC_NOTEBOOK_ITEM_ID"]
    sql_endpoint = os.environ["FABRIC_SQL_ENDPOINT"]
    lakehouse_database = os.environ["FABRIC_LAKEHOUSE_DATABASE"]

    # --- Step 1: Azure AD authentication ---
    try:
        app = msal.ConfidentialClientApplication(
            client_id=client_id,
            client_credential=client_secret,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
        )
        token_result = app.acquire_token_for_client(scopes=[FABRIC_SCOPE])
    except Exception as exc:  # noqa: BLE001 - MSAL raises on bad tenant_id / unreachable authority
        report("Azure AD authentication", False, str(exc))
        summarize_and_exit()
        return
    token = token_result.get("access_token")
    if not token:
        report("Azure AD authentication", False, token_result.get("error_description", "Unknown Azure AD error"))
        summarize_and_exit()
    report("Azure AD authentication", True, "Service principal signed in.")
    headers = {"Authorization": f"Bearer {token}"}

    # --- Step 2: Fabric API connectivity ---
    try:
        resp = httpx.get(f"{FABRIC_API_BASE}/workspaces", headers=headers, timeout=15)
    except httpx.RequestError as exc:
        report("Fabric API connectivity", False, f"Could not reach Fabric API: {exc}")
        summarize_and_exit()
        return

    if resp.status_code != 200:
        report("Fabric API connectivity", False, f"HTTP {resp.status_code}: {resp.text[:300]}")
        summarize_and_exit()
        return
    workspace_count = len(resp.json().get("value", []))
    report("Fabric API connectivity", True, f"{workspace_count} workspace(s) visible to this service principal.")

    # --- Step 3: target workspace ---
    ws_resp = httpx.get(f"{FABRIC_API_BASE}/workspaces/{workspace_id}", headers=headers, timeout=15)
    if ws_resp.status_code != 200:
        report(
            "Workspace access (acelo_demo)",
            False,
            f"HTTP {ws_resp.status_code}: {ws_resp.text[:300]} "
            "— check FABRIC_WORKSPACE_ID and that the service principal was added to this workspace.",
        )
        summarize_and_exit()
        return
    ws_name = ws_resp.json().get("displayName", "?")
    report("Workspace access (acelo_demo)", True, f"Resolved workspace: '{ws_name}'")

    # --- Step 4: Cluster notebook item ---
    item_resp = httpx.get(
        f"{FABRIC_API_BASE}/workspaces/{workspace_id}/items/{notebook_item_id}", headers=headers, timeout=15
    )
    if item_resp.status_code != 200:
        report(
            "Cluster notebook access",
            False,
            f"HTTP {item_resp.status_code}: {item_resp.text[:300]} — check FABRIC_NOTEBOOK_ITEM_ID.",
        )
        summarize_and_exit()
        return
    item = item_resp.json()
    report(
        "Cluster notebook access",
        True,
        f"Resolved item: '{item.get('displayName', '?')}' (type={item.get('type', '?')})",
    )

    # --- Step 5: SQL analytics endpoint ---
    try:
        import pyodbc
    except ImportError as exc:
        report(
            "SQL analytics endpoint",
            False,
            f"pyodbc / ODBC Driver 18 for SQL Server not available on this host: {exc}",
        )
        summarize_and_exit()
        return

    conn_str = (
        "Driver={ODBC Driver 18 for SQL Server};"
        f"Server={sql_endpoint},1433;"
        f"Database={lakehouse_database};"
        "Authentication=ActiveDirectoryServicePrincipal;"
        f"UID={client_id};"
        f"PWD={client_secret};"
        "Encrypt=Yes;TrustServerCertificate=No;"
    )
    try:
        conn = pyodbc.connect(conn_str, timeout=15)
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchall()
        conn.close()
    except Exception as exc:  # noqa: BLE001 - report the real driver/auth error, nothing hidden
        report("SQL analytics endpoint", False, str(exc))
        summarize_and_exit()
        return
    report("SQL analytics endpoint", True, "Connected and ran a test query.")

    summarize_and_exit()


if __name__ == "__main__":
    main()
