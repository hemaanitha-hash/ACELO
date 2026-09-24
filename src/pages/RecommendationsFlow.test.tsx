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
import type { AgentAnalysisResult } from "../services/databricksAgentApi";

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

function mockAnalyze(body: unknown, ok = true, status = 200) {
  const fetchMock = vi.fn().mockResolvedValue({
    ok,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** Fails the test if the legacy optimizations endpoint is ever called. */
function forbidLegacyEndpoint(fetchMock: ReturnType<typeof vi.fn>) {
  const called = fetchMock.mock.calls.map((c) => String(c[0]));
  expect(called.some((url) => url.includes("/optimizations"))).toBe(false);
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
    forbidLegacyEndpoint(fetchMock);
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

  it("runs the same analysis when opened directly, not the legacy endpoint", async () => {
    const fetchMock = mockAnalyze(ANALYSIS_RESULT);
    // Nothing stored: a user deep-linked straight to Recommendations.
    expect(getComputeAnalysis().result).toBeNull();

    renderPage(<Recommendations />);

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const called = fetchMock.mock.calls.map((c) => String(c[0]));
    expect(called.some((url) => url.includes("/databricks/agent/analyze"))).toBe(true);
    forbidLegacyEndpoint(fetchMock);
    expect(await screen.findByText(/analytics-all-purpose/)).toBeInTheDocument();
  });

  it("reports an analysis failure instead of showing stale or empty data", async () => {
    mockAnalyze({
      ok: false,
      status: "AUTHENTICATION_FAILED",
      message: "Databricks authentication failed.",
      environment_id: "env-1",
      steps: [],
      analysis: null,
      markdown: null,
    });

    renderPage(<Recommendations />);

    expect(await screen.findByText("AUTHENTICATION_FAILED")).toBeInTheDocument();
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
    mockAnalyze(ANALYSIS_RESULT);

    renderPage(<ComputeOptimization />);

    await waitFor(() =>
      expect(getComputeAnalysis().result?.analysis?.opportunities).toHaveLength(1),
    );
    expect(getComputeAnalysis().result?.analysis?.opportunities[0].resource).toBe(
      "analytics-all-purpose",
    );
  });
});
