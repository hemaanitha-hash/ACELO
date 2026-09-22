# ACELO Optimization Package

This directory is the **source of truth** for what ACELO deploys into a
customer's Microsoft Fabric workspace. `manifest.json` declares one asset per
optimization domain; `services/package_registry.py` reads it and refuses to
provision an asset whose source file is absent.

## Status

| Domain | Expected path | Present |
|---|---|---|
| cluster | `cluster/Clusterfabric.ipynb` | **YES** |
| query | `query/query_optimization.ipynb` | NO |
| storage | `storage/storage_optimization.ipynb` | NO |

Cluster is deployable. Query and storage still report `ASSET_MISSING` and are
skipped by provisioning — see "Why nothing was substituted".

## Cluster (converted in Step 4)

`cluster/Clusterfabric.ipynb` is ACELO's real cluster optimizer, supplied
by the product team as the source of truth and
from Databricks to Fabric. The scoring, thresholds and XGBoost models are
carried over unchanged; only the platform bindings were converted. See
`cluster/RESULT_SCHEMA.md` for the input and result contracts, and
`PARAMETERS.md` for the runtime parameters.

It is a straight port, not a rewrite: `dbutils`/`display()` removed, Unity
Catalog table names replaced with parameters, model storage moved from the
Databricks workspace tree to the attached Lakehouse Files area, the embedded API
key replaced with a Key Vault lookup, and results stamped with `acelo_run_id`
and appended rather than replacing the table each run.

## Why query and storage were not substituted

Real query optimization logic exists outside this repository (a loose
`query_testing.ipynb` under Downloads). No storage optimizer exists anywhere.
The query notebook was deliberately NOT copied in, for three reasons:

1. **They are Databricks notebooks, not Fabric notebooks.** They use `dbutils`,
   `display()`, and three-part Unity Catalog names (`databricks_ws.default.*`).
   None of that resolves in a Fabric Spark session, so deploying them to Fabric
   would produce a notebook that fails at run time.
2. **They contain a hardcoded live API key.** A `gsk_...` Groq credential is
   embedded in the source. Deploying that into a customer workspace would leak
   it to the customer and into ACELO's own deployment pipeline.
3. **They take no parameters.** Table names are hardcoded, so one copy cannot
   serve multiple customers — which is the entire point of the package model.

Fabricating replacements was also rejected: a toy notebook that prints fake
metrics would make provisioning *look* successful while deploying nothing of
value. Converting them is the same mechanical exercise already done for
cluster — see that notebook as the worked example.

## To add an asset

1. Place the Fabric-compatible `.ipynb` at the path given in `manifest.json`.
2. Make it parameter-aware (see `PARAMETERS.md`).
3. Remove any embedded credentials — use the parameters instead.
4. Bump `package_version`.

Provisioning picks it up automatically; no backend code change is required.
