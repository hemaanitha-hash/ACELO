// ============================================================================
// Stage 1 compute recommendations and their approval lifecycle.
//
// Persisted state only. Every value here comes from the existing backend:
//
//   GET  /api/optimizations?domain=stage1                  persisted recommendations
//   GET  /api/approvals/recommendations[?status=]          persisted approval records
//   POST /api/approvals/recommendations/{id}/request       Send for Approval
//   POST /api/approvals/recommendations/{id}/approve       (X-Acelo-User-* headers)
//   POST /api/approvals/recommendations/{id}/reject        (X-Acelo-User-* headers, {reason})
//
// The lifecycle shown in the UI is DERIVED from those records, never kept in
// React state, so a browser refresh always shows what the backend holds.
// ============================================================================

import { API_BASE } from "./apiBase";

/** A request that did not produce the expected response. */
export class RequestError extends Error {
  constructor(
    /** Human-readable, safe to show as the primary message. */
    message: string,
    /** HTTP status; 0 for network failure, -1 for timeout, -2 for a malformed body. */
    readonly status: number,
    /** The backend's own detail, for a secondary "technical details" area. */
    readonly technical: string | null = null,
  ) {
    super(message);
  }
}

const DEFAULT_TIMEOUT_MS = 30_000;

function describeStatus(status: number, action: string): string {
  if (status === 400) return `${action}: the request was not accepted.`;
  if (status === 401) return `${action}: your session is not signed in.`;
  if (status === 403) return `${action}: you do not have permission for this action.`;
  if (status === 404) return `${action}: the item no longer exists.`;
  if (status === 409) return `${action}: the item changed since it was loaded. Refresh and try again.`;
  if (status === 422) return `${action}: the request was incomplete or invalid.`;
  if (status >= 500) return `${action}: the ACELO backend reported an internal error.`;
  return `${action} (HTTP ${status}).`;
}

/**
 * One JSON request against the ACELO backend, with every failure mode turned
 * into a RequestError carrying a readable message and the raw detail.
 */
export async function requestJson<T>(
  path: string,
  action: string,
  init: RequestInit = {},
  timeoutMs = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      ...init,
      signal: controller.signal,
      headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
    });
  } catch (e: unknown) {
    if (e instanceof DOMException && e.name === "AbortError") {
      throw new RequestError(`${action}: the ACELO backend did not respond in time.`, -1);
    }
    throw new RequestError(`${action}: ACELO could not reach its backend.`, 0);
  } finally {
    clearTimeout(timer);
  }

  let body: unknown = null;
  let parsed = true;
  try {
    const text = await res.text();
    body = text ? JSON.parse(text) : null;
  } catch {
    parsed = false;
  }

  if (!res.ok) {
    const detail =
      body && typeof body === "object"
        ? ((body as { detail?: unknown; message?: unknown }).detail ??
          (body as { message?: unknown }).message)
        : null;
    // FastAPI 422 bodies carry a list of validation errors.
    const technical =
      typeof detail === "string" ? detail : detail != null ? JSON.stringify(detail) : null;
    // A business-rule refusal (409) or a sign-in requirement (401) is already
    // written for people by the backend, so it leads when present.
    const message =
      (res.status === 409 || res.status === 401) && typeof detail === "string"
        ? detail
        : describeStatus(res.status, action);
    throw new RequestError(message, res.status, technical ?? `HTTP ${res.status}`);
  }

  if (!parsed) {
    throw new RequestError(`${action}: the backend returned an unreadable response.`, -2);
  }
  return body as T;
}

// --- Types ------------------------------------------------------------------

/** One persisted Stage 1 recommendation (GET /api/optimizations, kind "stage1"). */
export interface PersistedRecommendation {
  id: string;
  recommendation_id: string;
  kind: "stage1";
  resource: string | null;
  resource_id: string | null;
  domain: string;
  stage1_domain: string | null;
  environment_id: string | null;
  title: string | null;
  description: string | null;
  optimization_label: string | null;
  finding_id: string | null;
  rule_id: string | null;
  severity: string | null;
  confidence: string | null;
  risk: string | null;
  created_at: string | null;
  updated_at: string | null;
  details: RecommendationDetails;
}

export interface RecommendationDetails {
  recommendation_id?: string | null;
  resource_type?: string | null;
  resource_name?: string | null;
  finding_type?: string | null;
  title?: string | null;
  summary?: string | null;
  description?: string | null;
  evidence?: unknown;
  evidence_references?: unknown;
  current_state?: unknown;
  proposed_state?: unknown;
  expected_impact?: unknown;
  estimated_savings?: unknown;
  estimated_monthly_savings?: number | null;
  approval_status?: string | null;
  execution_status?: string | null;
  verification_status?: string | null;
  policy_status?: string | null;
  workspace_name?: string | null;
  observation_window?: unknown;
  evidence_quality?: unknown;
  detected_at?: string | null;
}

export type ApprovalRecordStatus = "pending" | "approved" | "rejected";

/** One persisted approval record (GET /api/approvals/recommendations). */
export interface ApprovalRecord {
  approval_id: string | null;
  recommendation_id: string;
  status: ApprovalRecordStatus | null;
  requested_by: string | null;
  decided_by: string | null;
  created_at: string | null;
  decided_at: string | null;
  recommendation: {
    id: string;
    domain: string | null;
    resource: string | null;
    resource_id: string | null;
    resource_type: string | null;
    finding_id: string | null;
    rule_id: string | null;
    finding_type: string | null;
    title: string | null;
    summary: string | null;
    description: string | null;
    current_state: unknown;
    proposed_state: unknown;
    evidence: unknown;
    expected_impact: unknown;
    estimated_savings: unknown;
    observation_window: unknown;
    severity: string | null;
    risk: string | null;
    approval_status: string | null;
    execution_status: string | null;
    workspace_name: string | null;
  };
}

// --- Reads ------------------------------------------------------------------

function isStage1(item: unknown): item is PersistedRecommendation {
  return (
    !!item &&
    typeof item === "object" &&
    (item as { kind?: unknown }).kind === "stage1" &&
    typeof (item as { recommendation_id?: unknown }).recommendation_id === "string"
  );
}

export async function listRecommendations(): Promise<PersistedRecommendation[]> {
  const body = await requestJson<unknown>(
    "/optimizations?domain=stage1",
    "Could not load recommendations",
  );
  if (!Array.isArray(body)) {
    throw new RequestError("Could not load recommendations: the response was not a list.", -2);
  }
  return body.filter(isStage1);
}

export async function listApprovalRecords(
  status?: ApprovalRecordStatus[],
): Promise<ApprovalRecord[]> {
  const query = status?.length ? `?status=${status.join(",")}` : "";
  const body = await requestJson<unknown>(
    `/approvals/recommendations${query}`,
    "Could not load approvals",
  );
  if (!Array.isArray(body)) {
    throw new RequestError("Could not load approvals: the response was not a list.", -2);
  }
  return body.filter(
    (row): row is ApprovalRecord =>
      !!row &&
      typeof row === "object" &&
      typeof (row as { recommendation_id?: unknown }).recommendation_id === "string" &&
      !!(row as { recommendation?: unknown }).recommendation,
  );
}

// --- Writes -----------------------------------------------------------------

export interface Reviewer {
  id: string;
  name: string;
}

function reviewerHeaders(reviewer: Reviewer): Record<string, string> {
  return { "X-Acelo-User-Id": reviewer.id, "X-Acelo-User-Name": reviewer.name };
}

export function requestApproval(recommendationId: string): Promise<unknown> {
  return requestJson(
    `/approvals/recommendations/${encodeURIComponent(recommendationId)}/request`,
    "Could not send for approval",
    { method: "POST" },
  );
}

export function approveRecommendation(recommendationId: string, reviewer: Reviewer): Promise<unknown> {
  return requestJson(
    `/approvals/recommendations/${encodeURIComponent(recommendationId)}/approve`,
    "Could not record the approval",
    { method: "POST", headers: reviewerHeaders(reviewer) },
  );
}

export function rejectRecommendation(
  recommendationId: string,
  reviewer: Reviewer,
  reason: string,
): Promise<unknown> {
  return requestJson(
    `/approvals/recommendations/${encodeURIComponent(recommendationId)}/reject`,
    "Could not record the rejection",
    { method: "POST", headers: reviewerHeaders(reviewer), body: JSON.stringify({ reason }) },
  );
}

// --- Lifecycle --------------------------------------------------------------

/**
 * Where a recommendation is in the ACELO lifecycle, derived from persisted
 * backend fields. EXECUTING / EXECUTED / EXECUTION_FAILED are reached only when
 * the backend's execution_status says so; nothing in the UI advances them.
 */
export type Lifecycle =
  | "OPEN"
  | "PENDING"
  | "REJECTED"
  | "READY_TO_EXECUTE"
  | "EXECUTING"
  | "EXECUTED"
  | "EXECUTION_FAILED";

export const LIFECYCLE_LABELS: Record<Lifecycle, string> = {
  OPEN: "Open",
  PENDING: "Pending Approval",
  REJECTED: "Rejected",
  READY_TO_EXECUTE: "Ready to Execute",
  EXECUTING: "Executing",
  EXECUTED: "Executed",
  EXECUTION_FAILED: "Execution Failed",
};

export function lifecycleOf(
  approvalStatus: string | null | undefined,
  executionStatus: string | null | undefined,
): Lifecycle {
  const approval = (approvalStatus ?? "").trim().toUpperCase();
  const execution = (executionStatus ?? "").trim().toUpperCase();
  if (approval === "PENDING") return "PENDING";
  if (approval === "REJECTED") return "REJECTED";
  if (approval === "APPROVED") {
    if (execution === "RUNNING" || execution === "EXECUTING" || execution === "IN_PROGRESS") return "EXECUTING";
    if (execution === "SUCCEEDED" || execution === "COMPLETED" || execution === "EXECUTED") return "EXECUTED";
    if (execution === "FAILED") return "EXECUTION_FAILED";
    return "READY_TO_EXECUTE";
  }
  return "OPEN";
}

/** The newest approval record per recommendation (the list is newest-first). */
export function latestApprovalByRecommendation(
  records: ApprovalRecord[],
): Map<string, ApprovalRecord> {
  const latest = new Map<string, ApprovalRecord>();
  for (const record of records) {
    const existing = latest.get(record.recommendation_id);
    if (!existing || (record.created_at ?? "") > (existing.created_at ?? "")) {
      latest.set(record.recommendation_id, record);
    }
  }
  return latest;
}

/** A persisted recommendation joined with its persisted approval record. */
export interface RecommendationView {
  recommendation: PersistedRecommendation;
  approval: ApprovalRecord | null;
  lifecycle: Lifecycle;
}

export function joinRecommendations(
  recommendations: PersistedRecommendation[],
  approvals: ApprovalRecord[],
): RecommendationView[] {
  const latest = latestApprovalByRecommendation(approvals);
  return recommendations.map((recommendation) => {
    const approval = latest.get(recommendation.recommendation_id) ?? null;
    // The recommendation row is the source of truth for its own state; the
    // approval record supplies who decided and when.
    const approvalStatus =
      recommendation.details?.approval_status ??
      (approval?.status ? approval.status.toUpperCase() : null);
    return {
      recommendation,
      approval,
      lifecycle: lifecycleOf(approvalStatus, recommendation.details?.execution_status),
    };
  });
}

/** Parses a value the backend may store as JSON text. */
export function parseMaybeJson(value: unknown): unknown {
  if (typeof value !== "string") return value;
  const text = value.trim();
  if (!text || !(text.startsWith("{") || text.startsWith("["))) return value;
  try {
    return JSON.parse(text);
  } catch {
    return value;
  }
}

/**
 * Who is recording an approval decision. The backend requires both headers.
 *
 * Inside a Databricks App there is usually no Microsoft account in the browser
 * and /api/runtime does not expose the Databricks user, so the reviewer is
 * recorded truthfully as the App's user rather than an invented person.
 */
export function reviewerFromAccount(
  account: { localAccountId?: string; username?: string; name?: string } | null | undefined,
): Reviewer {
  const id = account?.localAccountId?.trim() || account?.username?.trim();
  const name = account?.name?.trim() || account?.username?.trim();
  return {
    id: id || "databricks-app-user",
    name: name || "Databricks App user",
  };
}
