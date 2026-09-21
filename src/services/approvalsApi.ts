// ============================================================================
// ACELO APPROVALS API
//
// In-app approvals for real optimizer recommendations. No demo data, no email,
// and no silent success: every failure reaches the caller as an ApiError.
// Actions carry the signed-in reviewer's identity; the backend refuses them
// without one rather than attributing them to a default user.
// ============================================================================

import type { AccountInfo } from "@azure/msal-browser";
import { ApiError, fabricTokenHeader } from "./environmentApi";

const API_BASE =
  (import.meta.env?.VITE_API_BASE as string | undefined) ?? "http://localhost:8000/api";

export const APPROVAL_STATUSES = [
  "PENDING",
  "APPROVED",
  "REJECTED",
  "EXECUTING",
  "COMPLETED",
  "FAILED",
  "CANCELLED",
] as const;
export type ApprovalStatus = (typeof APPROVAL_STATUSES)[number];

export interface ApprovalHistoryEntry {
  action: string;
  previous_status: ApprovalStatus | null;
  new_status: ApprovalStatus;
  user_id: string | null;
  user_name: string | null;
  reason: string | null;
  timestamp: string | null;
}

/** Mirrors backend services/approval_service.serialize. Unknown values are null. */
export interface Approval {
  approval_id: string;
  /** Null for candidates imported from the Fabric tracking table. */
  acelo_run_id: string | null;
  /** "run" (ACELO run results) or "tracking_table" (Fabric Delta table). */
  source?: string;
  /** The Delta row's own status, informational only (ACELO owns approval state). */
  tracking_status?: string | null;
  customer_id: string;
  environment_id: string | null;
  platform: string;
  domain: string;
  resource_id: string;
  resource_name: string;
  optimization_label: string | null;
  status: ApprovalStatus;
  current_workers: number | null;
  recommended_max_workers: number | null;
  total_dbus_cost_usd: number | null;
  potential_monthly_savings: number | null;
  llm_optimization: string | null;
  evidence: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
  approved_by: string | null;
  approved_at: string | null;
  rejected_by: string | null;
  rejected_at: string | null;
  rejection_reason: string | null;
  execution_id: string | null;
  validation_status: string | null;
  execution_error: string | null;
  history?: ApprovalHistoryEntry[];
}

export type ApprovalSummary = Record<ApprovalStatus, number>;

/** Who is acting — taken from the signed-in Microsoft account, never invented. */
export interface Reviewer {
  userId: string;
  userName: string;
}

export function reviewerFrom(account: AccountInfo | null | undefined): Reviewer | null {
  if (!account) return null;
  const userId = account.localAccountId || account.homeAccountId;
  const userName = account.name || account.username;
  return userId && userName ? { userId, userName } : null;
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

function actorHeaders(reviewer: Reviewer | null): Record<string, string> {
  if (!reviewer) {
    throw new ApiError("Sign in with your Microsoft account to review optimizations.", 401);
  }
  return { "X-Acelo-User-Id": reviewer.userId, "X-Acelo-User-Name": reviewer.userName };
}

export function listApprovals(filter: { status?: ApprovalStatus[]; aceloRunId?: string } = {}) {
  const params = new URLSearchParams();
  if (filter.status?.length) params.set("status", filter.status.join(","));
  if (filter.aceloRunId) params.set("acelo_run_id", filter.aceloRunId);
  const query = params.toString();
  return request<Approval[]>(`/approvals${query ? `?${query}` : ""}`);
}

export function getApprovalSummary() {
  return request<ApprovalSummary>("/approvals/summary");
}

/** Reads one approval; an EXECUTING one is re-validated against the platform. */
export function getApproval(id: string, fabricToken?: string | null) {
  return request<Approval>(`/approvals/${id}`, { headers: fabricTokenHeader(fabricToken) });
}

export async function approve(id: string, reviewer: Reviewer | null) {
  return request<Approval>(`/approvals/${id}/approve`, {
    method: "POST",
    headers: actorHeaders(reviewer),
    body: "{}",
  });
}

export async function reject(id: string, reviewer: Reviewer | null, reason: string) {
  return request<Approval>(`/approvals/${id}/reject`, {
    method: "POST",
    headers: actorHeaders(reviewer),
    body: JSON.stringify({ reason }),
  });
}

export async function execute(id: string, reviewer: Reviewer | null, fabricToken?: string | null) {
  return request<Approval>(`/approvals/${id}/execute`, {
    method: "POST",
    headers: { ...actorHeaders(reviewer), ...fabricTokenHeader(fabricToken) },
    body: "{}",
  });
}

/** Outcome of reading one environment's approval tracking Delta table. */
export interface TrackingSource {
  environment_id: string;
  table: string;
  status: "ok" | "failed";
  rows_read?: number;
  candidates?: number;
  created?: number;
  error_code?: string;
  message?: string;
}

/**
 * Reads the Fabric approval tracking Delta table(s) directly from OneLake and
 * imports new candidates as PENDING. Idempotent. Needs the user's OneLake token;
 * no SQL analytics endpoint is involved.
 */
export function refreshFromTracking(fabricToken?: string | null, onelakeToken?: string | null) {
  return request<{ sources: TrackingSource[]; summary: ApprovalSummary }>("/approvals/refresh", {
    method: "POST",
    headers: {
      ...fabricTokenHeader(fabricToken),
      ...(onelakeToken ? { "X-OneLake-Token": onelakeToken } : {}),
    },
    body: "{}",
  });
}

/** Results page "Send to Approval": idempotent, flagged rows only. */
export function sendToApproval(jobRunId: string, resourceId: string) {
  return request<Approval[]>("/approvals/from-run", {
    method: "POST",
    body: JSON.stringify({ job_run_id: jobRunId, resource_id: resourceId }),
  });
}

export const STATUS_TEXT: Record<ApprovalStatus, string> = {
  PENDING: "Pending Approval",
  APPROVED: "Approved",
  REJECTED: "Rejected",
  EXECUTING: "Executing",
  COMPLETED: "Completed",
  FAILED: "Failed",
  CANCELLED: "Cancelled",
};

/** Same rule the backend uses to decide which rows need approval. */
export function requiresApproval(row: { optimization_label?: unknown; cluster_name?: unknown }): boolean {
  const label = String(row.optimization_label ?? "");
  return (label === "Risky" || label === "Moderately Optimized") && Boolean(String(row.cluster_name ?? "").trim());
}

export const NOT_AVAILABLE = "Not available";

/** A value the record does not carry is "Not available"; a real 0 is shown as 0. */
export function display(value: number | null | undefined, format: (v: number) => string = String): string {
  return value === null || value === undefined ? NOT_AVAILABLE : format(value);
}

export function usd(value: number): string {
  return `$${value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

// ---------------------------------------------------------------------------
// AI Agent: approval-related requests (routed to the Approvals UI, never acted
// on silently).
// ---------------------------------------------------------------------------

export type ApprovalIntent =
  | { kind: "list"; status: ApprovalStatus }
  | { kind: "review"; cluster: string }
  | { kind: "approve"; cluster: string }
  | { kind: "reject"; cluster: string };

export function detectApprovalIntent(prompt: string): ApprovalIntent | null {
  const text = prompt.trim();
  const lowered = text.toLowerCase();
  const cluster = (verb: string) =>
    text.match(new RegExp(`${verb}\\s+(?:the\\s+)?(?:cluster\\s+)?([\\w.\\-]+)`, "i"))?.[1];

  const approveMatch = /^\s*approve\b/i.test(text) ? cluster("approve") : undefined;
  if (approveMatch && approveMatch.toLowerCase() !== "all") return { kind: "approve", cluster: approveMatch };
  const rejectMatch = /^\s*reject\b/i.test(text) ? cluster("reject") : undefined;
  if (rejectMatch) return { kind: "reject", cluster: rejectMatch };

  const why = text.match(/why\s+is\s+(?:the\s+)?(?:cluster\s+)?([\w.\-]+)\s+(?:waiting|pending|awaiting)/i);
  if (why) return { kind: "review", cluster: why[1] };

  if (/\bapproved\b/.test(lowered) && /(optimi[sz]ation|cluster|recommendation)/.test(lowered)) {
    return { kind: "list", status: "APPROVED" };
  }
  if (/\brejected\b/.test(lowered) && /(optimi[sz]ation|cluster|recommendation)/.test(lowered)) {
    return { kind: "list", status: "REJECTED" };
  }
  if (/(pending|awaiting|waiting for)\s+(cluster\s+)?approval|approvals?\s+(pending|awaiting)|pending\s+.*approvals?/.test(lowered)) {
    return { kind: "list", status: "PENDING" };
  }
  return null;
}
