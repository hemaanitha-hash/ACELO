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

import {
  getActiveContext,
  isActive,
  platformHeaders,
  resolveInitial,
  setActiveContext,
} from "./platformContext";
import { listPlatformConnections } from "./platformConnections";
import { isDatabricksOnly } from "./experience";
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
  compute_optimization?: {
    findings: Record<string, unknown>[];
    recommendations: Stage1Recommendation[];
    summary: Record<string, unknown>;
  };
}

export interface Stage1Recommendation {
  recommendation_id: string;
  domain: "CLUSTER_SIZING" | "CLUSTER_RUNTIME" | "AUTOSCALING";
  resource_type: "CLASSIC_CLUSTER";
  resource_id: string;
  resource_name: string;
  finding_id: string;
  rule_id: string;
  finding_type: string;
  title: string;
  summary: string;
  description: string;
  evidence: Record<string, unknown>;
  evidence_references: Record<string, unknown>[];
  current_state: Record<string, unknown>;
  proposed_state: { direction: string; reason: string };
  expected_impact: { status: "POTENTIAL"; description: string };
  estimated_savings: { status: "NOT_AVAILABLE"; estimated: null; measured: null };
  confidence: "low" | "medium" | "high";
  severity: "LOW" | "MEDIUM" | "HIGH";
  risk: "LOW" | "MEDIUM" | "HIGH";
  policy_status: string;
  approval_status: string;
  execution_status: string;
  verification_status: string;
  status: "OPEN";
  customer_id: string;
  environment_id: string;
  workspace_name: string | null;
  observation_window: { start: string | null; end: string | null };
  evidence_quality: Record<string, unknown>;
  created_at: string;
  updated_at: string;
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

async function ensureMvpDatabricksContext(): Promise<void> {
  if (!isDatabricksOnly()) return;
  const active = getActiveContext();
  if (active.platform === "databricks" && active.connection?.platform === "databricks") return;

  let connections;
  try {
    connections = await listPlatformConnections();
  } catch {
    setActiveContext(null);
    throw new AgentApiError("Databricks connections could not be loaded. Verify the Databricks workspace connection and try again.");
  }

  const databricks = resolveInitial(connections, "databricks");
  setActiveContext(databricks);
  if (!databricks) {
    throw new AgentApiError("No Databricks connection is configured for Stage 1. Connect a Databricks workspace to continue.");
  }
}

/**
 * Whether this prompt asks the agent to analyse real Databricks compute.
 * Mirrors the backend's `is_databricks_compute_request` so the UI routes the
 * same way the backend would; the backend remains the authority.
 */
export function isDatabricksComputeRequest(prompt: string): boolean {
  const lowered = (prompt || "").trim().toLowerCase();

  if (!lowered) return false;

  const compute = [
    "compute",
    "cluster",
    "clusters",
    "warehouse",
    "warehouses",
    "serverless",
    "resource",
    "resources",
  ];

  const analysis = [
    "analyz",
    "analys",
    "optimi",
    "review",
    "inspect",
    "audit",
    "find",
    "discover",
    "check",
    "look",
    "identify",
  ];

  const listing = [
    "show",
    "list",
    "what",
    "which",
    "any",
    "all",
    "see",
    "get",
    "tell",
  ];

  const mentionsCompute = compute.some((w) => lowered.includes(w));
  const asksAnalysis = analysis.some((w) => lowered.includes(w));
  const asksListing = listing.some((w) => lowered.includes(w));

  // Explicit Databricks request.
  if (lowered.includes("databricks")) {
    return mentionsCompute && asksAnalysis;
  }

  // In the Databricks-only MVP, Databricks is the platform by definition.
  // This allows routing even before the global active context is initialized.
  if (isDatabricksOnly()) {
    return mentionsCompute && (asksAnalysis || asksListing);
  }

  // In the legacy multi-platform experience, only route when
  // Databricks is actually the active platform.
  if (isActive("databricks")) {
    return mentionsCompute && (asksAnalysis || asksListing);
  }

  return false;
}

export async function analyzeDatabricksCompute(
  prompt: string,
  environmentId?: string | null,
): Promise<AgentAnalysisResult> {
  await ensureMvpDatabricksContext();
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
