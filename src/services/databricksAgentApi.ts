// ============================================================================
// ACELO OPTIMIZATION AGENT — DATABRICKS COMPUTE ANALYSIS
//
// Discover -> analyze -> recommend. Phase 1: no execution.
//
// The browser never holds a Databricks credential and never talks to
// Databricks. It asks the ACELO backend, which runs the agent in-process
// against the selected environment's stored connection.
//
// Step statuses come from the backend and reflect work that ACTUALLY
// completed — this module never advances a step on a timer.
// ============================================================================

import { isActive, platformHeaders } from "./platformContext";
import { API_BASE } from "./apiBase";

export type AgentStepStatus = "pending" | "done" | "failed";

export interface AgentAnalysisStep {
  id: string;
  label: string;
  status: AgentStepStatus;
  detail?: string | null;
}

export interface AgentOpportunity {
  resource: string;
  resource_type: string;
  resource_id: string;
  observed_evidence: string;
  potential_issue: string;
  recommendation: string;
  evidence_required: string;
  /** Qualitative by design — the backend never states a cost or saving. */
  expected_impact: string;
}

export interface AgentClassificationRow {
  resource: string;
  type: string;
  state: string;
  configuration: string;
}

export interface AgentAnalysis {
  workspace_name: string | null;
  observed_facts: {
    resource_count: number;
    by_type: Record<string, number>;
    resources: unknown[];
  };
  classification: AgentClassificationRow[];
  statuses: { resource_type: string; status: string; reason?: string | null }[];
  missing_evidence: string[];
  opportunities: AgentOpportunity[];
  summary: string | null;
}

export interface AgentAnalysisResult {
  ok: boolean;
  /** OK | AUTHENTICATION_FAILED | AUTHORIZATION_FAILED | NOT_CONFIGURED | DISCOVERY_FAILED */
  status: string;
  message: string | null;
  environment_id: string | null;
  steps: AgentAnalysisStep[];
  analysis: AgentAnalysis | null;
  markdown: string | null;
}

export class AgentApiError extends Error {}

/**
 * Whether this prompt asks the agent to analyse real Databricks compute.
 * Mirrors the backend's `is_databricks_compute_request` so the UI routes the
 * same way the backend would; the backend remains the authority.
 */
export function isDatabricksComputeRequest(prompt: string): boolean {
  const lowered = (prompt || "").toLowerCase();
  const compute = ["compute", "cluster", "clusters", "warehouse", "warehouses", "serverless", "resource", "resources"];
  const analysis = ["analyz", "analys", "optimi", "review", "inspect", "audit", "find", "discover", "check", "look"];
  const listing = ["show", "list", "what", "which", "any", "all", "see", "get", "tell"];
  const mentionsCompute = compute.some((w) => lowered.includes(w));

  // Naming Databricks works from any context.
  if (lowered.includes("databricks")) {
    return mentionsCompute && analysis.some((w) => lowered.includes(w));
  }

  // With Databricks ACTIVE the platform is already known, so "show me all
  // clusters" means Databricks clusters — the user should not have to say so.
  // With Fabric active the same words must NOT route here.
  if (isActive("databricks")) {
    return mentionsCompute && [...analysis, ...listing].some((w) => lowered.includes(w));
  }

  return false;
}

export async function analyzeDatabricksCompute(
  prompt: string,
  environmentId?: string | null,
): Promise<AgentAnalysisResult> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/databricks/agent/analyze`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...platformHeaders() },
      body: JSON.stringify({ prompt, environment_id: environmentId ?? null }),
    });
  } catch {
    throw new AgentApiError("ACELO could not reach the backend.");
  }

  if (!res.ok) {
    // A discovery failure comes back as HTTP 200 with ok=false, so a non-2xx
    // here is a genuine request fault (no environment, bad request).
    let detail = `The analysis could not be started (HTTP ${res.status}).`;
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new AgentApiError(detail);
  }

  return (await res.json()) as AgentAnalysisResult;
}
