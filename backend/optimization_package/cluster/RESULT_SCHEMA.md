# ACELO Cluster Optimization — input and result schema

Derived from `Clusterfabric.ipynb`, the product team's Fabric notebook, which is
the source of truth for this logic.

## Input (read-only)

`source_table` must expose these columns. The notebook validates them up front
and fails with a named list if any are absent — it never substitutes defaults
for missing telemetry.

| Column | Type | Used for |
|---|---|---|
| `cluster_id` | string | identity; feature encoding |
| `cluster_name` | string | identity; specialized-workload matching (`stream\|etl\|burst\|continuous`) |
| `node_type` | string | warehouse-size encoding fallback |
| `current_workers` | int | utilisation + rightsizing |
| `min_workers` | int | rightsizing floor |
| `max_workers` | int | rightsizing ceiling |
| `avg_cpu_util` | double (0–100) | `cpu_norm`, idle/oversized scoring |
| `avg_memory_util` | double (0–100) | `memory_norm`, idle/oversized scoring |
| `idle_time_min` | double | `idle_norm` |
| `cluster_uptime_hours` | double | rate normalisation; new-cluster gating |
| `total_jobs_run` | long | job density, worker utilisation |
| `total_dbus_cost_usd` | double | cost normalisation; XGBoost cost target |

Optional: `warehouse_size` (string) — when present it is used directly for
`warehouse_size_enc`; otherwise the value is derived from `node_type`.

## Column mapping

Customer tables rarely use these exact names. The notebook renames source
columns onto the expected ones via the `column_mapping` parameter, applied only
where the source column exists and the target does not.

Default mapping:

| Customer column | Optimizer column |
|---|---|
| `acme_cluster_id` | `cluster_id` |

Override per environment with a JSON object, e.g.
`{"acme_cluster_id": "cluster_id", "cpu_pct": "avg_cpu_util"}`.

**Missing columns are an error, never a substitution.** If a required column is
absent after mapping, the notebook raises listing both the missing names and the
columns the table actually has. No value is invented.

> **Demo environment:** the source table is
> `Data.dbo.realistic_cluster_dataset` in workspace
> `03e0392d-ed86-41e4-943c-146f8d845a1d`. It has 17 columns; ACELO has not been
> able to read its schema directly (that needs Fabric credentials), so the
> mapping beyond `acme_cluster_id` is unverified. A first run will name any
> further mismatches precisely.

> **Naming note:** `total_dbus_cost_usd` is a Databricks-era column name carried
> over from the original optimizer. It is the *cost* input, whatever the
> platform's billing unit. It was **not** renamed, because renaming it would
> change the contract the customer's telemetry table already satisfies. Mapping
> a differently-named cost column is a view/ETL concern on the customer side.

## Result (written to `result_table`)

Every field below is produced by the optimizer. Nothing is added to satisfy the
UI, and no field was invented.

### ACELO traceability

| Column | Type | Meaning |
|---|---|---|
| `acelo_run_id` | string | ACELO JobRun id — **the key ACELO filters on** |
| `acelo_environment_id` | string | originating ACELO environment |
| `acelo_analyzed_at` | string (ISO-8601 UTC) | when the run started |

### Deterministic scoring

| Column | Type | Meaning |
|---|---|---|
| `cpu_norm`, `memory_norm`, `idle_norm` | double | normalised utilisation (0–1) |
| `cost_norm`, `jobs_ph_norm`, `worker_util_norm` | double | p95-scaled rates (0–1) |
| `idle_score` | double | weighted idle composite |
| `idle_impact_score` | double | `idle_score·0.7 + cost_norm·0.3` |
| `oversized_score` | double | weighted oversizing composite |
| `underutilized_label` | string | Evaluating (New) / Specialized Workload / Highly Underutilized / Moderately Underutilized / Well Utilized |
| `idle_flag` | int | 1 when under-utilised |
| `oversized_label` | string | Evaluating (New) / Specialized Workload / Highly Oversized / Moderately Oversized / Optimal |
| `oversized_flag` | int | 1 when oversized |
| `job_density_norm` | double | jobs per worker-hour, normalised |
| `efficiency_score` | double | overall efficiency composite |
| `optimization_label` | string | **Optimized / Moderately Optimized / Risky** |
| `recommended_max_workers` | int | rightsizing recommendation |

### Model output

| Column | Type | Meaning |
|---|---|---|
| `predicted_cost_usd` | double | XGBoost predicted baseline cost |
| `predicted_savings_pct` | double | XGBoost predicted savings % (0–100) |
| `ml_cost_variance` | double | actual − predicted cost (waste overhead) |
| `potential_monthly_savings` | double | `cost × predicted_savings_pct/100`, floored at 0 |

### AI enrichment

| Column | Type | Meaning |
|---|---|---|
| `llm_optimization` | string | prescriptive remediation plan for `Risky` clusters |

`llm_optimization` carries one of:

- the model's remediation plan (LLM configured and reachable),
- `"Health Stable. No AI action required."` (cluster is not `Risky`),
- `"LLM_UNAVAILABLE"` (no credential configured),
- `"LLM_UNAVAILABLE: <ErrorType>"` (credential present, call failed)

The last two are **honest unavailability markers**, not recommendations. The
deterministic scoring above is unaffected and always present.

All feature columns (`exec_norm`, `scan_norm`, `spill_norm`, `shuffle_norm`,
`cost_score_norm`, `wh_multiplier`, `warehouse_size_enc`, `cluster_id_enc`) are
also written, plus every original source column — no source field is dropped.

## Write semantics

`append` with `mergeSchema`, into the ACELO-owned `result_table` only. The
original replaced the entire table on every run; appending preserves history,
and `acelo_run_id` makes each run's rows individually retrievable, so ACELO
never returns a stale result.
