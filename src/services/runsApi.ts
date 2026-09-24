// ============================================================================
// ACELO RUNS API
//
// Run history, run details, live execution events, cancel/re-run and in-app
// notifications. The backend owns every run's lifecycle; these calls only read
// or request changes. Tokens travel as headers on a single request and are
// never stored by the browser.
// ============================================================================

import { ApiError, fabricTokenHeader } from "./environmentApi";
import { platformHeaders } from "./platformContext";
import { API_BASE } from "./apiBase";

export const RUN_STATES = [
  "QUEUED",
  "STARTING",
  "SUBMITTED",
  "RUNNING",
  "CANCEL_REQUESTED",
  "SUCCEEDED",
  "FAILED",
  "CANCELLED",
] as const;
export type RunState = (typeof RUN_STATES)[number];

export const ACTIVE_RUN_STATES: RunState[] = ["QUEUED", "STARTING", "SUBMITTED", "RUNNING", "CANCEL_REQUESTED"];

export const RUN_STATE_LABELS: Record<RunState, string> = {
  QUEUED: "Queued",
  STARTING: "Starting",
  SUBMITTED: "Submitted",
  RUNNING: "Running",
  CANCEL_REQUESTED: "Cancelling",
  SUCCEEDED: "Succeeded",
  FAILED: "Failed",
  CANCELLED: "Cancelled",
};

/** Mirrors backend services/run_events.run_summary. */
export interface RunSummary {
  acelo_run_id: string;
  job_id: string;
  optimization: string;
  domain: string;
  platform: string;
  environment_id: string | null;
  environment_name: string | null;
  execution_type: string | null;
  status: RunState;
  platform_status: string;
  current_stage: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string | null;
  duration_seconds: number | null;
  platform_run_id: string | null;
  resource_id: string | null;
  created_by: string | null;
  retry_of_run_id: string | null;
  /** The original run this re-run was started from (never overwritten). */
  parent_run_id?: string | null;
  /** "AI Agent" | "Re-run" | "File upload". */
  trigger?: string | null;
  error_code: string | null;
  error_message: string | null;
  request: string | null;
}

export interface RunEvent {
  id: string;
  acelo_run_id: string;
  timestamp: string | null;
  level: string;
  stage: string | null;
  event_type: string;
  message: string;
  platform: string | null;
  platform_run_id: string | null;
  metadata: Record<string, unknown>;
}

export interface RunDetails extends RunSummary {
  parameters: Record<string, string>;
  timeline: RunEvent[];
  logs: RunEvent[];
  result: { rows?: Record<string, unknown>[]; row_count?: number; [key: string]: unknown } | null;
  reruns: string[];
  can_cancel: boolean;
  can_rerun: boolean;
}

export interface AppNotification {
  id: string;
  type: string;
  title: string;
  body: string | null;
  link: string | null;
  read: boolean;
  created_at: string | null;
  acelo_run_id: string | null;
}

export interface RunTokens {
  fabric?: string | null;
  onelake?: string | null;
  userName?: string | null;
}

export function tokenHeaders(tokens?: RunTokens): Record<string, string> {
  return {
    ...fabricTokenHeader(tokens?.fabric),
    ...(tokens?.onelake ? { "X-OneLake-Token": tokens.onelake } : {}),
    ...(tokens?.userName ? { "X-Acelo-User-Name": tokens.userName } : {}),
  };
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      ...options,
      headers: { "Content-Type": "application/json", ...platformHeaders(), ...options?.headers },
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

export interface RunFilters {
  status?: string;
  domain?: string;
  platform?: string;
  since?: string;
  until?: string;
  search?: string;
  limit?: number;
  offset?: number;
}

export function listRuns(filters: RunFilters = {}) {
  const params = new URLSearchParams();
  Object.entries(filters).forEach(([key, value]) => {
    if (value !== undefined && value !== null && String(value).trim() !== "") params.set(key, String(value));
  });
  const query = params.toString();
  return request<{ total: number; runs: RunSummary[] }>(`/runs${query ? `?${query}` : ""}`);
}

export function getActiveRuns(tokens?: RunTokens) {
  return request<{ count: number; runs: RunSummary[] }>("/runs/active", { headers: tokenHeaders(tokens) });
}

export function getRun(runId: string, tokens?: RunTokens) {
  return request<RunDetails>(`/runs/${runId}`, { headers: tokenHeaders(tokens) });
}

export function getRunEvents(runId: string, after?: string | null) {
  const query = after ? `?after=${encodeURIComponent(after)}` : "";
  return request<{ status: RunState; current_stage: string | null; events: RunEvent[] }>(
    `/runs/${runId}/events${query}`
  );
}

export function cancelRun(runId: string, tokens?: RunTokens) {
  return request<RunSummary>(`/runs/${runId}/cancel`, { method: "POST", headers: tokenHeaders(tokens) });
}

export function rerun(runId: string, tokens?: RunTokens) {
  return request<RunSummary>(`/runs/${runId}/rerun`, { method: "POST", headers: tokenHeaders(tokens) });
}

export function listNotifications(unreadOnly = false) {
  return request<{ unread: number; notifications: AppNotification[] }>(
    `/notifications${unreadOnly ? "?unread_only=true" : ""}`
  );
}

export function markNotificationRead(id: string) {
  return request<AppNotification>(`/notifications/${id}/read`, { method: "POST" });
}

export function markAllNotificationsRead() {
  return request<{ ok: boolean }>("/notifications/read-all", { method: "POST" });
}

export function isActive(state: RunState | string): boolean {
  return (ACTIVE_RUN_STATES as string[]).includes(state);
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  // Backend timestamps are naive UTC.
  const date = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}
