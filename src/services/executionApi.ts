// ============================================================================
// ACELO EXECUTION API (Step 2)
//
// Real analysis runs only. Like environmentApi.ts and unlike the legacy
// services/api.ts, this module has NO demo fallback: a backend failure surfaces
// as an error, never as invented progress or results.
//
// The browser never sees a platform credential — it only ever talks to the
// ACELO backend, which owns Fabric/Databricks authentication.
// ============================================================================

import { ApiError, fabricTokenHeader } from "./environmentApi";

const API_BASE =
  (import.meta.env?.VITE_API_BASE as string | undefined) ?? "http://localhost:8000/api";

/** Mirrors backend models/enums.py JobStatus. */
export type RunStatus =
  | "QUEUED"
  | "STARTING"
  | "RUNNING"
  | "COMPLETED"
  | "FAILED"
  | "CANCEL_REQUESTED"
  | "CANCELLED"
  | "RETRYING";

export const TERMINAL_STATUSES: RunStatus[] = ["COMPLETED", "FAILED", "CANCELLED"];

/** Human-readable labels. Nothing here implies success unless the backend said so. */
export const STATUS_LABELS: Record<RunStatus, string> = {
  QUEUED: "Queued",
  STARTING: "Starting",
  RUNNING: "Running",
  COMPLETED: "Completed",
  FAILED: "Failed",
  CANCEL_REQUESTED: "Cancel Requested",
  CANCELLED: "Canceled",
  RETRYING: "Retrying",
};

/**
 * Safe, human-readable text for a backend error code. Raw platform
 * diagnostics (AAD messages, stack traces) never reach the browser, so this
 * maps only the typed codes the API is allowed to return.
 */
export const ERROR_MESSAGES: Record<string, string> = {
  NOT_CONFIGURED: "This platform is not configured yet.",
  AUTHENTICATION_FAILED: "ACELO could not authenticate with the platform. Check the environment credentials.",
  PERMISSION_DENIED: "The configured identity does not have permission to run this analysis.",
  WORKSPACE_NOT_FOUND: "The configured workspace could not be found.",
  RESOURCE_NOT_FOUND: "The notebook or run could not be found in the workspace.",
  PLATFORM_API_UNAVAILABLE: "The platform API is unavailable. Try again shortly.",
  TIMEOUT: "The platform did not respond in time.",
  INVALID_CONFIGURATION: "This environment is not configured correctly.",
  EXECUTION_FAILED: "The analysis run failed on the platform.",
  RESULT_RETRIEVAL_FAILED: "The run finished, but its results could not be retrieved.",
  DISCOVERY_FAILED: "The workspace contents could not be listed.",
  CANCELED: "This run was canceled.",
  UNSUPPORTED: "This operation is not supported for this environment.",
  CLUSTER_SOURCE_TABLE_NOT_CONFIGURED:
    "The Cluster source table is not configured for this environment. Set it in Environment Setup.",
  CLUSTER_RESULT_TABLE_NOT_CONFIGURED:
    "The Cluster result table is not configured for this environment. Set it in Environment Setup.",
  NOTEBOOK_NOT_CONFIGURED:
    "The optimization notebook for this domain is not registered in this environment. Run 'Set up ACELO in Fabric' in Environment Setup.",
};

/**
 * The backend's own message is preferred whenever it has one: it names the
 * actual platform failure (missing setting, Fabric error code) and is already
 * scrubbed of credentials. The generic per-code text is only a fallback, so a
 * real cause is never replaced by "The analysis run failed on the platform."
 */
export function describeError(code?: string | null, fallback?: string | null): string {
  const detail = fallback?.trim();
  if (detail) return detail;
  if (code && ERROR_MESSAGES[code]) return ERROR_MESSAGES[code];
  return "Something went wrong running this analysis.";
}

export interface JobRun {
  id: string;
  domain: string;
  platform: string;
  platform_run_id: string | null;
  status: RunStatus;
  current_step: string | null;
  progress: number | null;
  error: string | null;
  error_code: string | null;
  result_reference: string | null;
  platform_resource_id: string | null;
  /** "notebook" | "pipeline" for Fabric runs; null for other platforms. */
  execution_type?: string | null;
  started_at: string | null;
  completed_at: string | null;
}

export interface AnalysisJob {
  id: string;
  connection_id: string;
  request: string;
  intent: string;
  platform: string;
  status: RunStatus;
  error: string | null;
  job_runs: JobRun[];
}

export interface JobResult {
  job_run_id: string;
  domain: string;
  status: RunStatus;
  available: boolean;
  payload: Record<string, unknown> | null;
  error: string | null;
  error_code: string | null;
}

/** Minimal environment projection this module depends on. */
export interface EnvironmentRef {
  id: string;
  connection_id: string;
  /** "user" / "personal" = Microsoft Account (delegated) sign-in. */
  auth_mode?: string | null;
}

/** Where an analysis runs, and whether it needs the user's delegated token. */
export interface ExecutionTarget {
  connectionId: string;
  delegated: boolean;
}

export const SESSION_EXPIRED_MESSAGE =
  "Your Microsoft Fabric session has expired. Please sign in again.";

export interface ConnectionSummary {
  id: string;
  platform: string;
  workspace: string;
  status: string;
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      ...options,
      headers: { "Content-Type": "application/json", ...options?.headers },
    });
  } catch {
    throw new ApiError("Could not reach the ACELO backend. Check that the API is running.", 0);
  }

  if (!res.ok) {
    let detail = `Request failed (HTTP ${res.status}).`;
    try {
      const body = await res.json();
      if (body?.detail) detail = String(body.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(detail, res.status);
  }
  return (await res.json()) as T;
}

/**
 * Resolves the connection to run against. Deliberately looked up at runtime —
 * connection IDs are customer-specific and must never be hardcoded in the UI.
 *
 * A connection is only runnable if an Environment is actually set up on top of
 * it: that is where discovery and the deployed ACELO notebooks live. Choosing
 * by connection status alone picked up rows that merely claim "connected" but
 * have no environment, no provisioned notebook and no table configuration, so
 * every run failed as INVALID_CONFIGURATION against a workspace that is not
 * the customer's.
 */
export async function resolveConnectionId(): Promise<string> {
  return (await resolveExecutionTarget()).connectionId;
}

/** The connection to run against plus its auth mode, from the backend's records. */
export async function resolveExecutionTarget(): Promise<ExecutionTarget> {
  const connections = await request<ConnectionSummary[]>("/connections");
  if (connections.length === 0) {
    throw new ApiError(
      "No platform environment is connected yet. Connect one in Environment Setup first.",
      0
    );
  }

  // Deliberately NOT wrapped in a catch. If this call fails, that is a real
  // fault to report — swallowing it into an empty list would look identical to
  // "no environment exists" and send the user to re-run setup they already did.
  const environments = await request<EnvironmentRef[]>("/environments");

  const backed = connections.filter((c) =>
    environments.some((e) => e.connection_id === c.id)
  );
  if (backed.length === 0) {
    throw new ApiError(
      "No environment is set up yet. Complete Environment Setup before running an analysis.",
      0
    );
  }

  const chosen = backed.find((c) => c.status === "connected") ?? backed[0];
  const environment = environments.find((e) => e.connection_id === chosen.id);
  const mode = environment?.auth_mode ?? "";
  return { connectionId: chosen.id, delegated: mode === "user" || mode === "personal" };
}

/**
 * Starts a REAL analysis. Returns as soon as the platform accepts the job.
 *
 * `token` is the delegated Fabric token for "Microsoft Account" environments.
 * Without it the backend has no way to authenticate as the signed-in user and
 * the run fails with AUTHENTICATION_FAILED. Service-principal environments
 * ignore it — the backend uses its own stored credential.
 */
export async function startAnalysis(
  prompt: string,
  connectionId: string,
  token?: string | null,
  extra?: { onelakeToken?: string | null; userName?: string | null }
): Promise<AnalysisJob> {
  // The OneLake token lets the backend read results itself when the run
  // finishes — even if this tab is closed. Held in backend memory only.
  return request<AnalysisJob>("/jobs", {
    method: "POST",
    headers: {
      ...fabricTokenHeader(token),
      ...(extra?.onelakeToken ? { "X-OneLake-Token": extra.onelakeToken } : {}),
      ...(extra?.userName ? { "X-Acelo-User-Name": extra.userName } : {}),
    },
    body: JSON.stringify({ connection_id: connectionId, prompt }),
  });
}

/**
 * Reads a job, polling the platform for fresh status on the way through.
 *
 * For delegated environments this is the ONLY thing that advances a run: the
 * backend's background worker has no user token, so the token must be supplied
 * on every read.
 */
export function getJob(jobId: string, token?: string | null): Promise<AnalysisJob> {
  return request<AnalysisJob>(`/jobs/${jobId}`, { headers: fabricTokenHeader(token) });
}

/**
 * `sqlToken` is the delegated token for the Lakehouse SQL endpoint, needed by
 * Microsoft Account environments to READ a completed run's result table. It is
 * sent only on this call, in its own header, and never stored.
 */
export function getJobResults(
  jobId: string,
  token?: string | null,
  sqlToken?: string | null,
  onelakeToken?: string | null
): Promise<JobResult[]> {
  return request<JobResult[]>(`/jobs/${jobId}/results`, {
    headers: {
      ...fabricTokenHeader(token),
      ...(sqlToken ? { "X-Fabric-Sql-Token": sqlToken } : {}),
      ...(onelakeToken ? { "X-OneLake-Token": onelakeToken } : {}),
    },
  });
}

export function cancelJob(jobId: string, token?: string | null): Promise<AnalysisJob> {
  return request<AnalysisJob>(`/jobs/${jobId}/cancel`, {
    method: "POST",
    headers: fabricTokenHeader(token),
  });
}

export function isTerminal(job: AnalysisJob): boolean {
  return job.job_runs.every((r) => TERMINAL_STATUSES.includes(r.status));
}

// ---------------------------------------------------------------------------
// Cluster results — real rows produced by the Fabric notebook
// ---------------------------------------------------------------------------

/** One row of the ACELO cluster result table. Fields come from the notebook. */
export interface ClusterResultRow {
  cluster_id?: string;
  cluster_name?: string;
  node_type?: string;
  current_workers?: number;
  min_workers?: number;
  max_workers?: number;
  recommended_max_workers?: number;
  avg_cpu_util?: number;
  avg_memory_util?: number;
  efficiency_score?: number;
  optimization_label?: string;
  underutilized_label?: string;
  oversized_label?: string;
  idle_flag?: number;
  oversized_flag?: number;
  idle_time_min?: number;
  total_dbus_cost_usd?: number;
  predicted_cost_usd?: number;
  predicted_savings_pct?: number;
  potential_monthly_savings?: number;
  llm_optimization?: string;
  acelo_run_id?: string;
  [key: string]: unknown;
}

export interface ClusterResult {
  jobId: string;
  runId: string;
  platform: string;
  platformRunId: string | null;
  /** How the Fabric run was started: "notebook" or "pipeline". */
  executionType: string | null;
  /** The run's final platform status. */
  runStatus: string | null;
  resultTable: string | null;
  retrievedAt: string | null;
  /** Uploaded file name, for file analyses. */
  sourceFile: string | null;
  /** "idle" | "oversized" when the request asked for one; only affects highlighting. */
  focus: string | null;
  /** ACELO SENT vs NOTEBOOK RECEIVED, computed by the backend from the result rows. */
  parameterVerification: ParameterVerification | null;
  rows: ClusterResultRow[];
}

export interface ParameterVerification {
  status: "MATCHED" | "MISMATCH" | "UNVERIFIED";
  sent: Record<string, string> | null;
  received: Record<string, string> | null;
  mismatches: { parameter: string; sent: string; received: string | null }[];
  correlation_id_matched?: boolean;
  reason?: string;
}

function toClusterResult(job: AnalysisJob, results: JobResult[]): ClusterResult | null {
  const clusterResult = results.find((r) => r.domain === "cluster" && r.available);
  if (!clusterResult?.payload) return null;

  const payload = clusterResult.payload as Record<string, unknown>;
  const source = (payload.source_payload ?? {}) as Record<string, unknown>;
  const rows = (source.rows ?? []) as ClusterResultRow[];
  if (rows.length === 0) return null;

  const run = job.job_runs.find((r) => r.domain === "cluster");
  return {
    jobId: job.id,
    runId: String(payload.run_id ?? run?.id ?? job.id),
    platform: String(payload.platform ?? job.platform),
    platformRunId: run?.platform_run_id ?? null,
    executionType: run?.execution_type ?? null,
    runStatus: run?.status ?? null,
    resultTable: (payload.result_reference as string | null) ?? null,
    retrievedAt: (payload.retrieved_at as string | null) ?? null,
    sourceFile: (payload.source_file as string | null) ?? null,
    focus: (payload.focus as string | null) ?? null,
    parameterVerification:
      (payload.parameter_verification as ParameterVerification | undefined) ?? null,
    rows,
  };
}

/**
 * Most recent COMPLETED cluster run with retrievable results — from any
 * source (Fabric, Databricks or an uploaded file).
 *
 * Returns null when no run has produced results — the caller must render an
 * honest empty state rather than substituting demo figures.
 */
export async function getLatestClusterResult(
  token?: string | null,
  sqlToken?: string | null
): Promise<ClusterResult | null> {
  const jobs = await request<AnalysisJob[]>("/jobs", {
    headers: fabricTokenHeader(token),
  });
  const candidates = jobs
    .filter((j) => j.job_runs.some((r) => r.domain === "cluster" && r.status === "COMPLETED"))
    .reverse();

  for (const job of candidates) {
    const result = toClusterResult(job, await getJobResults(job.id, token, sqlToken));
    if (result) return result;
  }
  return null;
}

/** The cluster result of one specific job, or null if it has none (yet). */
export async function getClusterResultForJob(
  jobId: string,
  token?: string | null,
  sqlToken?: string | null
): Promise<ClusterResult | null> {
  const job = await getJob(jobId, token);
  return toClusterResult(job, await getJobResults(jobId, token, sqlToken));
}

// ---------------------------------------------------------------------------
// File analysis — upload a Cluster CSV, analysed by the same optimizer
// ---------------------------------------------------------------------------

export interface InvalidValue {
  row: number;
  column: string;
  value: string;
  reason: string;
}

/** A rejected upload. Carries the specific columns/rows to fix. */
export class UploadValidationError extends ApiError {
  constructor(
    message: string,
    status: number,
    readonly code: string,
    readonly missingColumns: string[],
    readonly invalidValues: InvalidValue[]
  ) {
    super(message, status);
    this.name = "UploadValidationError";
  }
}

export const MAX_UPLOAD_MB = 5;

/**
 * Uploads a Cluster CSV. Returns once the file is validated and the run is
 * created; the optimizer then runs in the backend — poll getJob() as usual.
 * No platform connection or token is involved.
 */
export async function uploadClusterFile(file: File, prompt?: string): Promise<AnalysisJob> {
  const form = new FormData();
  form.append("file", file);
  if (prompt?.trim()) form.append("prompt", prompt.trim());

  let res: Response;
  try {
    // No Content-Type header: the browser sets the multipart boundary.
    res = await fetch(`${API_BASE}/file-analysis/cluster`, { method: "POST", body: form });
  } catch {
    throw new ApiError("Could not reach the ACELO backend. Check that the API is running.", 0);
  }

  if (!res.ok) {
    let body: any = null;
    try {
      body = await res.json();
    } catch {
      /* non-JSON error body */
    }
    const detail = body?.detail;
    if (detail && typeof detail === "object") {
      throw new UploadValidationError(
        String(detail.message ?? "The file could not be analysed."),
        res.status,
        String(detail.code ?? "INVALID_FILE"),
        Array.isArray(detail.missing_columns) ? detail.missing_columns : [],
        Array.isArray(detail.invalid_values) ? detail.invalid_values : []
      );
    }
    throw new ApiError(
      typeof detail === "string" ? detail : `Upload failed (HTTP ${res.status}).`,
      res.status
    );
  }
  return (await res.json()) as AnalysisJob;
}

/** Health bands shown in the UI, mapped 1:1 from the optimizer's optimization_label. */
export const HEALTH_BANDS = {
  Optimized: "Healthy",
  "Moderately Optimized": "At Risk",
  Risky: "Critical",
} as const;

/** KPIs computed from the REAL result rows. Nothing is assumed or defaulted. */
export function summariseClusterRows(rows: ClusterResultRow[]) {
  const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : 0);
  const mean = (key: keyof ClusterResultRow) => {
    const values = rows.filter((r) => typeof r[key] === "number");
    return values.length ? values.reduce((t, r) => t + num(r[key]), 0) / values.length : null;
  };

  // Missing is not zero: a total or count is null when NO row carries the value.
  // A genuine 0 (every row reports 0) stays 0.
  const total = (key: keyof ClusterResultRow): number | null => {
    const values = rows.filter((r) => typeof r[key] === "number");
    return values.length ? values.reduce((t, r) => t + num(r[key]), 0) : null;
  };
  const flagged = (key: "idle_flag" | "oversized_flag"): number | null =>
    rows.some((r) => typeof r[key] === "number") ? rows.filter((r) => r[key] === 1).length : null;

  const monthlySavings = total("potential_monthly_savings");
  const currentCost = total("total_dbus_cost_usd");
  const byLabel = rows.reduce<Record<string, number>>((acc, r) => {
    const label = String(r.optimization_label ?? "Unknown");
    acc[label] = (acc[label] ?? 0) + 1;
    return acc;
  }, {});

  return {
    clusterCount: rows.length,
    riskyCount: byLabel["Risky"] ?? 0,
    healthy: byLabel["Optimized"] ?? 0,
    atRisk: byLabel["Moderately Optimized"] ?? 0,
    critical: byLabel["Risky"] ?? 0,
    idleCount: flagged("idle_flag"),
    oversizedCount: flagged("oversized_flag"),
    monthlySavings,
    currentCost,
    savingsPct:
      currentCost !== null && currentCost > 0 && monthlySavings !== null
        ? (monthlySavings / currentCost) * 100
        : null,
    avgCpuUtil: mean("avg_cpu_util"),
    avgMemoryUtil: mean("avg_memory_util"),
    byLabel,
  };
}

/**
 * Plain-language recommendation for one cluster, assembled only from fields
 * the optimizer produced. Returns null when the optimizer recommends no change.
 */
export function describeClusterRecommendation(row: ClusterResultRow): string | null {
  const parts: string[] = [];
  const rec = row.recommended_max_workers;
  const max = row.max_workers;
  if (typeof rec === "number" && typeof max === "number" && rec < max) {
    parts.push(`Reduce max workers from ${max} to ${rec}`);
  }
  if (row.idle_flag === 1 && row.underutilized_label) {
    parts.push(`${row.underutilized_label} — tighten auto-termination`);
  }
  if (row.oversized_flag === 1 && row.oversized_label) {
    parts.push(`${row.oversized_label} — consider a smaller node type than ${row.node_type ?? "the current one"}`);
  }
  return parts.length ? parts.join("; ") : null;
}
