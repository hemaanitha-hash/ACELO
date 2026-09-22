"""
ACELO Cluster Optimization — platform-independent core.

This is the scoring, labelling, rightsizing and XGBoost logic of
`optimization_package/cluster/Clusterfabric.ipynb`, expressed over a pandas
DataFrame instead of a Spark DataFrame so it can run anywhere: an uploaded
file, a Fabric table, a Databricks table. Only the I/O differs per platform;
the algorithm is this one.

Nothing here is new logic. Every weight, threshold, label and model
hyper-parameter is the notebook's, and `tests/test_cluster_optimizer_parity.py`
reads the notebook source and fails if the two ever drift apart.

Spark semantics that matter for identical results are reproduced explicitly:
  * `least` / `greatest` skip nulls in Spark -> `np.fmin` / `np.fmax`.
  * `approxQuantile` returns an element of the data, not an interpolation ->
    `method="inverted_cdf"`.
  * `.cast("int")` truncates toward zero -> `np.trunc`.

Deliberately NOT done here: reading or writing any platform table, and
persisting XGBoost models. The notebook trains both boosters on the data it is
analysing when no saved model exists; this module always takes that path, so a
run is a pure function of its input rows.
"""

from __future__ import annotations

import os
from typing import Callable

import numpy as np
import pandas as pd

REQUIRED_COLUMNS = [
    "cluster_id",
    "cluster_name",
    "node_type",
    "current_workers",
    "min_workers",
    "max_workers",
    "avg_cpu_util",
    "avg_memory_util",
    "idle_time_min",
    "cluster_uptime_hours",
    "total_jobs_run",
    "total_dbus_cost_usd",
]

NUMERIC_COLUMNS = [
    "current_workers",
    "min_workers",
    "max_workers",
    "avg_cpu_util",
    "avg_memory_util",
    "idle_time_min",
    "cluster_uptime_hours",
    "total_jobs_run",
    "total_dbus_cost_usd",
]

# The notebook fills exactly these before scoring; every other input is used as-is.
NOTEBOOK_FILLNA = {"avg_cpu_util": 0, "total_jobs_run": 0, "cluster_uptime_hours": 0}

SPECIALIZED_WORKLOADS = "stream|etl|burst|continuous"

WH_ORDER = {
    "2X-Small": 0,
    "Small": 1,
    "Medium": 2,
    "Large": 3,
    "X-Large": 4,
    "2X-Large": 5,
}

FEATURES = [
    "exec_norm",
    "cpu_norm",
    "scan_norm",
    "spill_norm",
    "shuffle_norm",
    "cost_score_norm",
    "wh_multiplier",
    "warehouse_size_enc",
    "cluster_id_enc",
]

XGB_PARAMS = dict(n_estimators=100, max_depth=4, learning_rate=0.1, random_state=42, verbosity=0)

LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
HEALTH_STABLE = "Health Stable. No AI action required."
DEFAULT_LLM_MODEL = "llama-3.3-70b-versatile"


class ClusterOptimizerInputError(ValueError):
    """The input cannot be analysed. The message is safe to show a user."""


# --- Spark-equivalent helpers ------------------------------------------------

def _least(*values):
    out = values[0]
    for v in values[1:]:
        out = np.fmin(out, v)
    return out


def _greatest(*values):
    out = values[0]
    for v in values[1:]:
        out = np.fmax(out, v)
    return out


def _approx_quantile(series: pd.Series, q: float) -> float:
    return float(np.quantile(series.dropna().to_numpy(dtype=float), q, method="inverted_cdf"))


# --- the optimizer -----------------------------------------------------------

def score_clusters(source: pd.DataFrame) -> pd.DataFrame:
    """Deterministic scoring and labelling — the notebook's Spark section."""
    missing = [c for c in REQUIRED_COLUMNS if c not in source.columns]
    if missing:
        raise ClusterOptimizerInputError(
            "Missing required columns: " + ", ".join(missing)
            + ". ACELO does not substitute values for missing telemetry."
        )
    if len(source) == 0:
        raise ClusterOptimizerInputError("The dataset contains no rows, so there is nothing to analyse.")

    df = source.copy()
    for column in NUMERIC_COLUMNS:
        df[column] = pd.to_numeric(df[column], errors="raise").astype(float)

    df = df.fillna(NOTEBOOK_FILLNA)
    df["current_workers"] = _greatest(df["current_workers"].fillna(1), 1)

    uptime_floor = _greatest(df["cluster_uptime_hours"], 1)
    cost_ph = df["total_dbus_cost_usd"] / uptime_floor
    jobs_ph = df["total_jobs_run"] / uptime_floor
    worker_util = df["total_jobs_run"] / _greatest(df["current_workers"], 1)

    max_cost_ph = max(_approx_quantile(cost_ph, 0.95), 1.0)
    max_jobs_ph = max(_approx_quantile(jobs_ph, 0.95), 1.0)
    max_worker_util = max(_approx_quantile(worker_util, 0.95), 1.0)

    df["cpu_norm"] = _least(df["avg_cpu_util"] / 100, 1.0)
    df["memory_norm"] = _least(df["avg_memory_util"] / 100, 1.0)
    df["idle_norm"] = _least(df["idle_time_min"] / _greatest(df["cluster_uptime_hours"] * 60, 1), 1.0)
    df["cost_norm"] = _least(cost_ph / max_cost_ph, 1.0)
    df["jobs_ph_norm"] = _least(jobs_ph / max_jobs_ph, 1.0)
    df["worker_util_norm"] = _least(worker_util / max_worker_util, 1.0)

    df["idle_score"] = np.where(
        df["cluster_uptime_hours"] > 1,
        (1 - df["cpu_norm"]) * 0.30
        + (1 - df["memory_norm"]) * 0.25
        + df["idle_norm"] * 0.25
        + (1 - df["jobs_ph_norm"]) * 0.20,
        (1 - df["cpu_norm"]) * 0.45
        + (1 - df["memory_norm"]) * 0.25
        + df["idle_norm"] * 0.10
        + (1 - df["jobs_ph_norm"]) * 0.20,
    )
    df["idle_impact_score"] = df["idle_score"] * 0.7 + df["cost_norm"] * 0.3
    df["oversized_score"] = (
        (1 - df["cpu_norm"]) * 0.35
        + (1 - df["memory_norm"]) * 0.35
        + (1 - df["worker_util_norm"]) * 0.30
    )

    if len(df) < 10:
        raw_idle_p75, raw_idle_p90 = 0.60, 0.75
        raw_over_p75, raw_over_p90 = 0.60, 0.75
    else:
        raw_idle_p75 = _approx_quantile(df["idle_impact_score"], 0.75)
        raw_idle_p90 = _approx_quantile(df["idle_impact_score"], 0.90)
        raw_over_p75 = _approx_quantile(df["oversized_score"], 0.75)
        raw_over_p90 = _approx_quantile(df["oversized_score"], 0.90)

    mod_idle_thresh = min(max(raw_idle_p75, 0.50), 0.75)
    high_idle_thresh = max(min(max(raw_idle_p90, 0.65), 0.85), mod_idle_thresh + 0.05)
    mod_over_thresh = min(max(raw_over_p75, 0.50), 0.75)
    high_over_thresh = max(min(max(raw_over_p90, 0.65), 0.85), mod_over_thresh + 0.05)

    is_new = df["cluster_uptime_hours"] <= 1
    is_specialized = df["cluster_name"].astype(str).str.lower().str.contains(SPECIALIZED_WORKLOADS, regex=True)

    df["underutilized_label"] = np.select(
        [
            is_new,
            is_specialized,
            df["idle_impact_score"] > high_idle_thresh,
            df["idle_impact_score"] > mod_idle_thresh,
        ],
        ["Evaluating (New)", "Specialized Workload", "Highly Underutilized", "Moderately Underutilized"],
        default="Well Utilized",
    )
    df["idle_flag"] = df["underutilized_label"].isin(
        ["Highly Underutilized", "Moderately Underutilized"]
    ).astype(int)

    df["oversized_label"] = np.select(
        [
            is_new,
            is_specialized,
            df["oversized_score"] > high_over_thresh,
            df["oversized_score"] > mod_over_thresh,
        ],
        ["Evaluating (New)", "Specialized Workload", "Highly Oversized", "Moderately Oversized"],
        default="Optimal",
    )
    df["oversized_flag"] = df["oversized_label"].isin(
        ["Highly Oversized", "Moderately Oversized"]
    ).astype(int)

    df["job_density_norm"] = _least(
        df["total_jobs_run"] / _greatest(df["cluster_uptime_hours"] * df["current_workers"], 1) / 2.0,
        1.0,
    )
    df["efficiency_score"] = (
        (1 - df["idle_flag"]) * 0.3
        + (1 - df["oversized_flag"]) * 0.3
        + df["job_density_norm"] * 0.2
        + (1 - (df["avg_cpu_util"] / 100)) * 0.2
    )
    df["optimization_label"] = np.select(
        [df["efficiency_score"] > 0.70, df["efficiency_score"] >= 0.40],
        ["Optimized", "Moderately Optimized"],
        default="Risky",
    )

    recommended = np.select(
        [df["optimization_label"] == "Risky", df["optimization_label"] == "Moderately Optimized"],
        [
            _greatest(df["min_workers"], np.trunc(df["current_workers"] * 0.7)),
            _greatest(df["min_workers"], np.trunc(df["current_workers"] * 0.9)),
        ],
        default=df["max_workers"],
    )
    df["recommended_max_workers"] = _greatest(recommended, df["min_workers"], 1).astype(int)

    return df


def predict_savings(scored: pd.DataFrame) -> pd.DataFrame:
    """XGBoost cost/savings models — the notebook's pandas section."""
    import xgboost as xgb

    pdf = scored.copy()

    if "warehouse_size" in pdf.columns:
        pdf["warehouse_size_enc"] = pdf["warehouse_size"].map(WH_ORDER).fillna(0)
    else:
        pdf["warehouse_size_enc"] = pdf["node_type"].apply(
            lambda x: 1 if "DS3" in str(x) else (2 if "DS4" in str(x) else (3 if "DS5" in str(x) else 0))
        )

    pdf["cluster_id_enc"] = pd.factorize(pdf["cluster_id"])[0]
    pdf["exec_norm"] = pdf["worker_util_norm"].fillna(0)
    pdf["scan_norm"] = pdf["jobs_ph_norm"].fillna(0)
    pdf["spill_norm"] = (1.0 - pdf["memory_norm"]).fillna(0)
    pdf["shuffle_norm"] = (1.0 - pdf["cpu_norm"]).fillna(0)
    pdf["cost_score_norm"] = pdf["cost_norm"].fillna(0)
    pdf["wh_multiplier"] = pdf["current_workers"].fillna(1).astype(float)

    train_df = pdf[FEATURES + ["total_dbus_cost_usd", "oversized_score"]].fillna(0)
    x_train = train_df[FEATURES].astype(float)
    y_cost = train_df["total_dbus_cost_usd"].astype(float)
    y_save = (train_df["oversized_score"] * 100).clip(0, 100).astype(float)

    model_cost = xgb.XGBRegressor(**XGB_PARAMS)
    model_cost.fit(x_train, y_cost)
    model_savings = xgb.XGBRegressor(**XGB_PARAMS)
    model_savings.fit(x_train, y_save)

    x_mat = pdf[FEATURES].fillna(0).astype(float)
    pdf["predicted_cost_usd"] = model_cost.predict(x_mat).astype(float)
    pdf["predicted_savings_pct"] = model_savings.predict(x_mat).astype(float)
    pdf["ml_cost_variance"] = pdf["total_dbus_cost_usd"] - pdf["predicted_cost_usd"]
    pdf["potential_monthly_savings"] = (
        pdf["total_dbus_cost_usd"] * (pdf["predicted_savings_pct"] / 100.0)
    ).clip(lower=0.0).fillna(0.0)
    return pdf


def remediation_prompt(row) -> str:
    """The notebook's LLM prompt, unchanged."""
    return f"""
Analyze this specific cluster deployment:

Cluster Instance Profile:
{row['cluster_name']} ({row['node_type']})

Configuration:
{row['current_workers']} workers active
Autoscale limits: {row['min_workers']}-{row['max_workers']}

Current Recommended Target Max Worker Limit:
{row['recommended_max_workers']}

Uptime:
{row['cluster_uptime_hours']} hours

Production Jobs:
{row['total_jobs_run']}

XGBOOST PREDICTIVE PERFORMANCE SIGNALS:

Predicted Baseline Cost Target:
${row['predicted_cost_usd']:.2f}/hr

Observed Waste Overhead Variance:
${row['ml_cost_variance']:.2f}/hr

Model-Driven Proportional Savings:
{row['predicted_savings_pct']:.1f}%

Projected Monthly Savings:
${row['potential_monthly_savings']:.2f}/month

RESOURCE LOAD DISTRIBUTIONS:

CPU Waste Factor:
{row['shuffle_norm']:.2f}

Observed Avg CPU:
{row['avg_cpu_util']}%

Memory Waste Factor:
{row['spill_norm']:.2f}

Observed Avg Memory:
{row['avg_memory_util']}%

Idle Duration Overhead:
{row['idle_norm']:.2f}

Idle Time:
{row['idle_time_min']} minutes

Generate a highly prescriptive remediation plan.

1. Validate or adjust the recommended Max Workers value
   ({row['recommended_max_workers']}) based on the model signals.

2. Recommend downsized compute nodes or confirm that the
   instance family matches workload constraints.

3. Define strict Auto-Termination timeout parameters
   matching the current idle footprint.
"""


def groq_llm_from_env() -> Callable[[str], str] | None:
    """
    The notebook's non-Key-Vault credential path: GROQ_API_KEY from the
    environment. Returns None when no key is configured, in which case Risky
    rows are marked LLM_UNAVAILABLE — never given an invented plan.
    """
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        return None
    model = os.getenv("ACELO_LLM_MODEL") or DEFAULT_LLM_MODEL

    def call(prompt_text: str) -> str:
        try:
            from groq import Groq

            completion = Groq(api_key=api_key).chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "You are an expert FinOps Engine Optimization Agent."},
                    {"role": "user", "content": prompt_text},
                ],
                temperature=0,
            )
            return completion.choices[0].message.content.strip()
        except Exception as exc:  # noqa: BLE001 - honest failure marker, as in the notebook
            return f"{LLM_UNAVAILABLE}: {type(exc).__name__}"

    return call


def run_cluster_optimization(
    source: pd.DataFrame, llm: Callable[[str], str] | None = None
) -> pd.DataFrame:
    """
    Full optimizer: scoring -> XGBoost -> AI enrichment. Returns one row per
    input cluster with every source column plus the RESULT_SCHEMA.md columns.
    """
    pdf = predict_savings(score_clusters(source))

    def enrich(row) -> str:
        if row["optimization_label"] != "Risky":
            return HEALTH_STABLE
        if llm is None:
            return LLM_UNAVAILABLE
        return llm(remediation_prompt(row))

    pdf["llm_optimization"] = pdf.apply(enrich, axis=1).fillna(HEALTH_STABLE)
    return pdf
