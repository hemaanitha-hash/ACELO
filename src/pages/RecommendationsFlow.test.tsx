import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Recommendations from "./Recommendations";
import ComputeOptimization from "./ComputeOptimization";
import {
  getComputeAnalysis,
  resetComputeAnalysis,
  setComputeAnalysis,
} from "../services/computeAnalysis";
import type { AgentAnalysisResult, Stage1Recommendation } from "../services/databricksAgentApi";

/**
 * Recommendations consumes the SAME Databricks analysis the rest of the journey
 * produced:
 *
 *   AI Agent / Compute Optimization -> DatabricksAgentResult -> this page
 *
 * It must never fall back to the legacy getOptimizations() data, never invent a
 * cost, saving, utilization or confidence figure, and must state what evidence
 * each item still needs.
 */

const OPPORTUNITY = {
  resource: "analytics-all-purpose",
  resource_type: "CLASSIC_CLUSTER",
  resource_id: "0421-193742-abcd1234",
  observed_evidence: "auto_termination_minutes = 0",
  potential_issue: "Auto-termination is disabled, so this cluster stays running until stopped.",
  recommendation: "Consider enabling an auto-termination policy.",
  evidence_required: "Cluster event history showing real idle periods between runs.",
  expected_impact: "Qualitative: removes compute time that is paid for but not used.",
};

const ANALYSIS_RESULT: AgentAnalysisResult = {
  ok: true,
  status: "OK",
  message: null,
  environment_id: "env-1",
  steps: [
    { id: "environment", label: "Environment identified", status: "done" },
    { id: "auth", label: "Databricks authentication verified", status: "done" },
    { id: "discover", label: "Discovering compute resources", status: "done" },
    { id: "resources", label: "Resources discovered", status: "done" },
    { id: "analyze", label: "Analyzing optimization opportunities", status: "done" },
  ],
  analysis: {
    workspace_name: "adb-test.azuredatabricks.net",
    observed_facts: { resource_count: 3, by_type: { CLASSIC_CLUSTER: 1 }, resources: [] },
    classification: [],
    statuses: [],
    missing_evidence: ["CPU and memory utilization — not exposed by the compute API."],
    opportunities: [OPPORTUNITY],
    summary: "Discovered 3 resources and raised 1 configuration observation.",
  },
  markdown: "## Observed Facts",
};

const STAGE1_RECOMMENDATION: Stage1Recommendation = {
  recommendation_id: "stage1-rec-1",
  domain: "CLUSTER_SIZING",
  resource_type: "CLASSIC_CLUSTER",
  resource_id: "cluster-1",
  resource_name: "analytics",
  finding_id: "finding-1",
  rule_id: "STAGE1.CLUSTER_SIZING.OVERSIZED",
  finding_type: "OVERSIZED",
  title: "Review cluster capacity",
  summary: "Observed low utilization on this cluster.",
  description: "The deterministic rule found sustained low utilization.",
  evidence: { avg_cpu_percent: 12.5, avg_memory_percent: 20 },
  evidence_references: [{ resource_id: "cluster-1" }],
  current_state: { worker_count: 8 },
  proposed_state: { direction: "review_worker_capacity", reason: "Capacity may exceed demand." },
  expected_impact: { status: "POTENTIAL", description: "Potential reduction in excess worker capacity." },
  estimated_savings: { status: "NOT_AVAILABLE", estimated: null, measured: null },
  confidence: "low",
  severity: "MEDIUM",
  risk: "MEDIUM",
  policy_status: "NOT_EVALUATED",
  approval_status: "NOT_REQUESTED",
  execution_status: "NOT_STARTED",
  verification_status: "NOT_STARTED",
  status: "OPEN",
  customer_id: "customer-1",
  environment_id: "env-1",
  workspace_name: "workspace-1",
  observation_window: { start: "2026-09-01T00:00:00Z", end: "2026-09-01T02:00:00Z" },
  evidence_quality: { completeness: "COMPLETE", freshness: "UNKNOWN" },
  created_at: "2026-09-01T02:01:00Z",
  updated_at: "2026-09-01T02:01:00Z",
};

/**
 * The analysis endpoint answers with `body`; the persisted-list endpoints
 * (recommendations, approvals, runs) answer with `lists[path]` or an empty list,
 * as the real backend does for a fresh environment.
 */
function mockAnalyze(body: unknown, ok = true, status = 200, lists: Record<string, unknown> = {}) {
  const fetchMock = vi.fn().mockImplementation(async (url: string) => {
    const u = String(url);
    const key = Object.keys(lists).find((k) => u.includes(k));
    const payload = key
      ? lists[key]
      : u.includes("/optimizations") || u.includes("/approvals/recommendations")
        ? []
        : u.includes("/runs")
          ? { total: 0, count: 0, runs: [] }
          : body;
    const good = key || !u.includes("/databricks/agent/analyze") ? true : ok;
    return {
      ok: good,
      status: good ? 200 : status,
      json: async () => payload,
      text: async () => JSON.stringify(payload),
    };
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** Recommendations come from the persisted Stage 1 list, never a fresh analysis. */
function expectPersistedSource(fetchMock: ReturnType<typeof vi.fn>) {
  const called = fetchMock.mock.calls.map((c) => String(c[0]));
  expect(called.some((url) => url.includes("/optimizations?domain=stage1"))).toBe(true);
  expect(called.some((url) => url.includes("/databricks/agent/analyze"))).toBe(false);
}

function renderPage(ui: React.ReactElement) {
  return render(<MemoryRouter>{ui}</MemoryRouter>);
}

beforeEach(() => {
  resetComputeAnalysis();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Recommendations consumes the Databricks analysis", () => {
  it("renders deterministic Stage 1 recommendations with evidence and no action controls", async () => {
    const result: AgentAnalysisResult = {
      ...ANALYSIS_RESULT,
      analysis: {
        ...ANALYSIS_RESULT.analysis!,
        compute_optimization: {
          findings: [],
          recommendations: [STAGE1_RECOMMENDATION],
          summary: {},
        },
      },
    };
    mockAnalyze(result);
    setComputeAnalysis(result);

    renderPage(<Recommendations />);

    expect(await screen.findByText("Compute recommendations")).toBeInTheDocument();
    expect(screen.getByText("STAGE1.CLUSTER_SIZING.OVERSIZED")).toBeInTheDocument();
    expect(screen.getByText(/review_worker_capacity/)).toBeInTheDocument();
    expect(screen.getByText(/Potential reduction in excess worker capacity/)).toBeInTheDocument();
    expect(screen.getByText(/Not available. No savings estimate or measurement/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /approve|execute/i })).not.toBeInTheDocument();
  });

  it("renders the findings the journey already produced, without re-running", async () => {
    const fetchMock = mockAnalyze(ANALYSIS_RESULT);
    // The journey produced this on Compute Optimization / the AI Agent.
    setComputeAnalysis(ANALYSIS_RESULT);

    renderPage(<Recommendations />);

    expect(await screen.findByText(/analytics-all-purpose/)).toBeInTheDocument();
    // Already had a result, so no second ANALYSIS was run. (Layout's run
    // indicator polls independently; only the analyze call matters here.)
    const analyzeCalls = fetchMock.mock.calls
      .map((c) => String(c[0]))
      .filter((url) => url.includes("/databricks/agent/analyze"));
    expect(analyzeCalls).toHaveLength(0);
    await waitFor(() => expectPersistedSource(fetchMock));
  });

  it("shows every evidence field the backend supplied", async () => {
    mockAnalyze(ANALYSIS_RESULT);
    setComputeAnalysis(ANALYSIS_RESULT);

    renderPage(<Recommendations />);
    await screen.findByText(/analytics-all-purpose/);

    expect(screen.getByText("Finding")).toBeInTheDocument();
    expect(screen.getByText("Observed evidence")).toBeInTheDocument();
    expect(screen.getByText("Recommendation")).toBeInTheDocument();
    expect(screen.getByText("Rationale")).toBeInTheDocument();
    expect(screen.getByText("Evidence required before action")).toBeInTheDocument();
    expect(screen.getByText("Expected impact")).toBeInTheDocument();

    expect(screen.getByText("auto_termination_minutes = 0")).toBeInTheDocument();
    expect(screen.getByText(/Cluster event history showing real idle periods/)).toBeInTheDocument();
  });

  it("labels each item a potential opportunity and states the limitations", async () => {
    mockAnalyze(ANALYSIS_RESULT);
    setComputeAnalysis(ANALYSIS_RESULT);

    renderPage(<Recommendations />);

    expect(await screen.findByText("Potential Optimization Opportunity")).toBeInTheDocument();
    expect(screen.getByText("Limitations")).toBeInTheDocument();
    expect(screen.getByText(/CPU and memory utilization/)).toBeInTheDocument();
  });

  it("never invents a cost, saving, utilization or confidence figure", async () => {
    mockAnalyze(ANALYSIS_RESULT);
    setComputeAnalysis(ANALYSIS_RESULT);

    renderPage(<Recommendations />);
    await screen.findByText(/analytics-all-purpose/);

    const text = document.body.textContent ?? "";
    expect(text).not.toMatch(/[$£€]\s?\d/);
    expect(text).not.toMatch(/\d+(\.\d+)?\s?%/);
    expect(text).not.toMatch(/confidence/i);
    expect(text).not.toMatch(/\/month/);
  });

  it("never starts an analysis when opened directly; it loads persisted recommendations", async () => {
    const persisted = {
      id: "stage1-rec-1",
      recommendation_id: "stage1-rec-1",
      kind: "stage1",
      resource: "analytics",
      resource_id: "cluster-1",
      domain: "cluster",
      title: "Review cluster capacity",
      description: "Observed low utilization on this cluster.",
      rule_id: "STAGE1.CLUSTER_SIZING.OVERSIZED",
      severity: "MEDIUM",
      risk: "MEDIUM",
      created_at: "2026-09-01T02:01:00Z",
      details: { ...STAGE1_RECOMMENDATION, approval_status: "NOT_REQUESTED" },
    };
    const fetchMock = mockAnalyze(ANALYSIS_RESULT, true, 200, { "/optimizations": [persisted] });
    expect(getComputeAnalysis().result).toBeNull();

    renderPage(<Recommendations />);

    expect(await screen.findByText("Review cluster capacity")).toBeInTheDocument();
    expectPersistedSource(fetchMock);
    expect(screen.getByRole("button", { name: /Send to Approval/ })).toBeInTheDocument();
  });

  it("reports an analysis failure in plain language, with the status in the details", async () => {
    mockAnalyze(ANALYSIS_RESULT);
    setComputeAnalysis({
      ok: false,
      status: "AUTHENTICATION_FAILED",
      message: "Databricks authentication failed.",
      environment_id: "env-1",
      steps: [],
      analysis: null,
      markdown: null,
    });

    renderPage(<Recommendations />);

    expect(await screen.findByText("Analysis could not be completed")).toBeInTheDocument();
    expect(screen.getByText("AUTHENTICATION_FAILED")).toBeInTheDocument();
    expect(screen.getByText(/Databricks authentication failed/)).toBeInTheDocument();
    expect(screen.queryByText("Potential Optimization Opportunity")).not.toBeInTheDocument();
  });

  it("says so honestly when the analysis raised nothing", async () => {
    const empty = {
      ...ANALYSIS_RESULT,
      analysis: { ...ANALYSIS_RESULT.analysis!, opportunities: [] },
    };
    mockAnalyze(empty);
    setComputeAnalysis(empty);

    renderPage(<Recommendations />);

    expect(await screen.findByText("No recommendations")).toBeInTheDocument();
    // Not a clean bill of health.
    expect(screen.getByText(/not a finding that the workspace is optimally configured/i))
      .toBeInTheDocument();
  });
});

describe("Compute Optimization shares its result", () => {
  it("stores the analysis so Recommendations shows the same findings", async () => {
    const fetchMock = mockAnalyze(ANALYSIS_RESULT, true, 200, {
      "/connections": [{ id: "db-1", platform: "databricks", name: "workspace", status: "connected" }],
    });

    renderPage(<ComputeOptimization />);

    // Opening the page starts nothing; the user asks for the analysis.
    await screen.findAllByRole("button", { name: /Analyze Compute/ });
    expect(fetchMock.mock.calls.some((c) => String(c[0]).includes("/databricks/agent/analyze"))).toBe(false);
    (await screen.findAllByRole("button", { name: /Analyze Compute/ }))[0].click();

    await waitFor(() =>
      expect(getComputeAnalysis().result?.analysis?.opportunities).toHaveLength(1),
    );
    expect(getComputeAnalysis().result?.analysis?.opportunities[0].resource).toBe(
      "analytics-all-purpose",
    );
  });
});
