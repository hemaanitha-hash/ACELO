# Real Fabric integration test — Windows PowerShell walkthrough

This proves ACELO executes a **real** notebook in **your** Fabric workspace and
reads back its **real** result. Nothing in this document is mocked.

> **Step 2 change — read this first.** ACELO no longer has any fallback
> execution path. Previously, if Fabric was unreachable or misconfigured, the
> backend invented a run ID (`fab_clu_...`), reported `COMPLETED`, and returned
> hardcoded optimization figures. All of that is gone. If any step below fails,
> you will now see a typed error instead of a fake success. A green result here
> therefore means Fabric genuinely ran your notebook.

Prerequisites: Python 3.11+, and (for the result step) the **ODBC Driver 18 for
SQL Server**.

## 0. Install dependencies

```powershell
pip install fastapi "uvicorn[standard]" sqlalchemy pydantic pydantic-settings httpx msal python-multipart cryptography pyodbc
```

For running the test suite as well:

```powershell
pip install pytest pytest-asyncio respx
```

## 1. Configuration

Nothing customer-specific is hardcoded in ACELO. Everything below is supplied at
runtime and stored encrypted. **Never commit a filled-in `.env`.**

Copy `.env.example` to `.env`, fill it in, then load it into your session
(PowerShell does not read `.env` automatically):

```powershell
Copy-Item .env.example .env
notepad .env

Get-Content .env | ForEach-Object {
    if ($_ -match '^\s*#' -or $_ -notmatch '=') { return }
    $name, $value = $_.Split('=', 2)
    [System.Environment]::SetEnvironmentVariable($name.Trim(), $value.Trim())
}
```

### Required

| Variable | Where to get it |
|---|---|
| `SECRET_ENCRYPTION_KEY` | Generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `FABRIC_TENANT_ID` | Entra ID → Overview → Tenant ID |
| `FABRIC_CLIENT_ID` | App registrations → your app → Application (client) ID |
| `FABRIC_CLIENT_SECRET` | App registrations → Certificates & secrets → the secret **value** |
| `FABRIC_WORKSPACE_ID` | Fabric workspace URL: `.../groups/{workspaceId}/...` |
| `FABRIC_NOTEBOOK_ITEM_ID` | Notebook URL: `.../synapsenotebooks/{notebookItemId}` |
| `FABRIC_SQL_ENDPOINT` | Lakehouse → SQL analytics endpoint host (no protocol/port) |
| `FABRIC_LAKEHOUSE_DATABASE` | The `Database=` value for that endpoint (your Lakehouse name) |

### Optional — background polling

| Variable | Default | Meaning |
|---|---|---|
| `ACELO_POLL_TIMEOUT_SECONDS` | `3600` | Give up monitoring after this long. The run is **not** marked failed — ACELO does not assert a state it never observed. |
| `ACELO_POLL_INITIAL_INTERVAL` | `5` | Seconds before the first status poll. |
| `ACELO_POLL_MAX_INTERVAL` | `30` | Backoff ceiling. |
| `ACELO_BACKGROUND_POLLING` | `1` | Set `0` to disable background pollers (the test suite does this). |

### Required setup in Azure AD / Fabric

1. App registration exists with a client secret.
2. Fabric Admin Portal → Tenant settings → Developer settings →
   **"Service principals can use Fabric APIs"** is ENABLED for this principal.
3. The service principal is added to the workspace with at least **Contributor**.
4. The service principal can query the Lakehouse SQL analytics endpoint. If step
   8 below fails on login, see "Known limitations".

## 2. Connectivity diagnostic

```powershell
python scripts\test_fabric_connection.py
```

Checks Azure AD, the Fabric API, your workspace, the notebook, and the SQL
endpoint. Every line must say PASS before continuing.

## 3. End-to-end cluster run

```powershell
python scripts\test_fabric_cluster_run.py
```

This drives the same `agent/orchestrator.py` → `services/job_service.py` →
`platforms/fabric.py` code path the API uses. Override the timeout if your
notebook is slow:

```powershell
$env:FABRIC_TEST_TIMEOUT_SECONDS = "1800"
```

## 4. Through the HTTP API (what the UI does)

Start the server:

```powershell
uvicorn main:app --reload
```

In a second window:

```powershell
$base = "http://127.0.0.1:8000/api"

# --- Phase 1: create and verify the environment ---
$env_body = @{
    name         = "Fabric Production"
    platform     = "fabric"
    tenant_id    = $env:FABRIC_TENANT_ID
    client_id    = $env:FABRIC_CLIENT_ID
    client_secret = $env:FABRIC_CLIENT_SECRET
    workspace_id = $env:FABRIC_WORKSPACE_ID
} | ConvertTo-Json

$envRec = Invoke-RestMethod -Uri "$base/environments" -Method Post -Body $env_body -ContentType "application/json"

# PROOF 1 + 2: real authentication, real workspace access
Invoke-RestMethod -Uri "$base/environments/$($envRec.id)/test" -Method Post

# PROOF 3: real resource discovery — your actual notebooks/lakehouses
$discovery = Invoke-RestMethod -Uri "$base/environments/$($envRec.id)/discover" -Method Post
$discovery.counts
$discovery.items | Format-Table display_name, type

# --- Step 2: real execution ---
# The connection carrying the notebook + SQL endpoint config:
$conn_body = @{
    platform      = "fabric"
    workspace     = "acelo_demo"
    endpoint      = "https://api.fabric.microsoft.com/v1"
    auth_method   = "service_principal"
    auth_metadata = @{
        tenant_id          = $env:FABRIC_TENANT_ID
        client_id          = $env:FABRIC_CLIENT_ID
        workspace_id       = $env:FABRIC_WORKSPACE_ID
        notebook_item_id   = $env:FABRIC_NOTEBOOK_ITEM_ID
        sql_endpoint       = $env:FABRIC_SQL_ENDPOINT
        lakehouse_database = $env:FABRIC_LAKEHOUSE_DATABASE
    }
    secret = $env:FABRIC_CLIENT_SECRET
} | ConvertTo-Json -Depth 5

$connection = Invoke-RestMethod -Uri "$base/connections" -Method Post -Body $conn_body -ContentType "application/json"

# PROOF 4 + 5: starts a real Fabric job, returns immediately with Fabric's own
# job instance GUID. If this fails you get a typed error, never a fake run.
$jobBody = @{ connection_id = $connection.id; prompt = "Analyze my cluster" } | ConvertTo-Json
$job = Invoke-RestMethod -Uri "$base/jobs" -Method Post -Body $jobBody -ContentType "application/json"
$job.job_runs[0].platform_run_id   # <- a real GUID from Fabric's Location header

# PROOF 6 + 7: real polling to a real terminal state.
# Background polling is already running; this shows the current persisted state.
Invoke-RestMethod -Uri "$base/jobs/$($job.id)"
Invoke-RestMethod -Uri "$base/jobs/$($job.id)/logs"

# PROOF 8 + 9 + 10: the real result, read from your Lakehouse and returned to the UI
$results = Invoke-RestMethod -Uri "$base/jobs/$($job.id)/results"
$results[0].available
$results[0].payload.row_count
$results[0].payload.source_payload.rows | Select-Object -First 3
```

### Cross-check it in the Fabric portal

Open **Monitoring Hub** in Fabric and confirm a notebook run exists with the same
job instance GUID that `platform_run_id` returned. That is the decisive proof
that ACELO started a real job.

### Cancellation

```powershell
Invoke-RestMethod -Uri "$base/jobs/$($job.id)/cancel" -Method Post
```

The run becomes `CANCEL_REQUESTED`, **not** `CANCELLED`. It is only promoted to
`CANCELLED` once a poll observes Fabric itself reporting the run as cancelled.

## 5. What "verified" requires

A live verification means all ten of these were observed against a real tenant:

1. ACELO authenticates (`/environments/{id}/test` → `connected: true`)
2. ACELO reaches the configured workspace (real `workspace_name` returned)
3. ACELO resolves the target notebook (it appears in `/discover` output)
4. ACELO starts a real Fabric job (HTTP 202 from the Job Scheduler API)
5. ACELO receives a real job instance ID (GUID, matches Monitoring Hub)
6. ACELO polls the real job (status transitions appear in `/logs`)
7. ACELO observes a real terminal state (`COMPLETED` / `FAILED` / `CANCELLED`)
8. ACELO retrieves the real result (rows from your Lakehouse table)
9. ACELO stores the result (`job_runs.result_json` is populated)
10. ACELO returns it (`/jobs/{id}/results` → `available: true`)

If you cannot complete these, the correct statement is
**"LIVE NOT VERIFIED"** — mocked tests do not substitute for this.

## Failure diagnostics

Errors now arrive as typed codes. The message shown in the UI is always safe;
the technical detail stays in the backend log.

| Error code | Meaning | Fix |
|---|---|---|
| `INVALID_CONFIGURATION` | A required field is missing | Check tenant/client/workspace/secret |
| `AUTHENTICATION_FAILED` | Azure AD rejected the credentials | Wrong tenant/client ID, or bad/expired secret |
| `PERMISSION_DENIED` | Signed in, but no access | Add the SP to the workspace; enable SP Fabric API access |
| `WORKSPACE_NOT_FOUND` | Bad workspace ID | Re-copy from the workspace URL |
| `RESOURCE_NOT_FOUND` | Notebook or run instance missing | Notebook renamed/moved/deleted |
| `PLATFORM_API_UNAVAILABLE` | 429/5xx or network fault | Transient — ACELO retries these automatically |
| `TIMEOUT` | Fabric did not respond | Transient — retried |
| `EXECUTION_FAILED` | The notebook itself failed | Check the failure reason in `/logs` and Monitoring Hub |
| `RESULT_RETRIEVAL_FAILED` | Run completed, result unreadable | ODBC driver missing, or SP lacks SQL endpoint access |
| `UNSUPPORTED` | No notebook configured for that domain | Configure a `{domain}_notebook_id` for query/storage |

Note the distinction: `EXECUTION_FAILED` means Fabric ran your notebook and it
failed. `RESULT_RETRIEVAL_FAILED` means it succeeded but ACELO could not read the
output table. These are no longer conflated.

## Known limitations

- Fabric's Job Scheduler API exposes no granular execution logs — only status
  transitions, `exitValue` and `failureReason`. That is all `get_run_logs()`
  surfaces. Deeper Spark logs require Fabric's separate Monitoring API.
- `get_run_result()` issues an unscoped `SELECT * FROM <result table>`. To scope
  results to a single run, the notebook needs to write a run-identifying column;
  ACELO already has the `platform_run_id` to filter on once it does.
- Only `cluster` resolves from the legacy `notebook_item_id`. `query` and
  `storage` require an explicit `query_notebook_id` / `storage_notebook_id` in
  the connection's `auth_metadata`, and otherwise return `UNSUPPORTED` rather
  than running the cluster notebook against the wrong domain.
- Community reports exist of `Authentication=ActiveDirectoryServicePrincipal`
  failing against Fabric SQL endpoints in some tenant configurations. If step 8
  fails on login despite workspace access, you may need
  `CREATE USER ... FROM EXTERNAL PROVIDER` for the SP inside the Lakehouse SQL
  endpoint.
