# Notebook parameter contract

How ACELO passes per-customer configuration into a deployed notebook, so one
copy serves every customer without editing its source.

## Mechanism

ACELO sends parameters on Fabric's `RunNotebook` job, built by
`services/job_service.build_run_parameters()` and serialised by
`FabricAdapter._execution_body()`:

```json
{
  "executionData": {
    "parameters": {
      "acelo_run_id": { "value": "…", "type": "string" }
    }
  }
}
```

The notebook receives them through a cell tagged `parameters` (Fabric replaces
that cell's assignments at run time).

## Parameters

| Parameter | Required | Supplied from | Purpose |
|---|---|---|---|
| `acelo_run_id` | **yes** | `JobRun.id` | Stamped on every result row; ACELO filters results by it |
| `source_table` | **yes** | connection `cluster_source_table` / `source_table` | Customer telemetry table to read |
| `result_table` | **yes** | connection `cluster_result_table` / `result_table` | ACELO-owned table to write |
| `source_lakehouse` | no | connection `lakehouse_database` | Qualifies `source_table` when it is unqualified |
| `result_lakehouse` | no | connection `lakehouse_database` | Qualifies `result_table` when it is unqualified |
| `environment_id` | no | connection `environment_id` | Traceability |
| `column_mapping` | no | connection `column_mapping` | JSON `{"source_column": "expected_column"}`; renames customer columns onto the optimizer's names |
| `model_dir` | no | connection `model_dir` | Where XGBoost models persist; defaults to `/lakehouse/default/Files/acelo/models/<domain>` |
| `llm_key_vault_uri` | no | connection `llm_key_vault_uri` | Azure Key Vault holding the LLM API key |
| `llm_secret_name` | no | connection `llm_secret_name` | Secret name in that vault |
| `llm_model_name` | no | connection `llm_model_name` | Defaults to `llama-3.3-70b-versatile` |

**Per-domain override:** any key may be prefixed with the domain
(`cluster_source_table`, `query_result_table`, …). The prefixed value wins, so
one connection can point each domain at different tables.

Required parameters default to `""` in the notebook. Missing values raise a
named `ValueError` before any data is read — the notebook never silently
analyses the wrong table.

## Secrets

**No credential is ever passed as a parameter.** `llm_key_vault_uri` and
`llm_secret_name` are non-secret *references*; the notebook resolves the actual
key at run time via `notebookutils.credentials.getSecret()` using the Fabric
workspace identity, and deletes it from the session namespace immediately.

When no vault is configured the deterministic analysis still runs in full and
the `llm_optimization` column records unavailability explicitly. Recommendation
text is never fabricated.

## Adding a new domain's notebook

1. Place the `.ipynb` at the path in `manifest.json`.
2. Give it a cell tagged `parameters` declaring the names above.
3. Validate required parameters up front and fail loudly.
4. Stamp `acelo_run_id` on every written row; append rather than replace.
5. Never embed a credential.

No backend change is needed — `package_registry` and provisioning pick it up
automatically. `cluster/Clusterfabric.ipynb` is the worked example.
