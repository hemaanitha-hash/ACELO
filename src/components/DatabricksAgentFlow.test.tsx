import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import AgentWorkspace from "./AgentWorkspace";
import { isDatabricksComputeRequest } from "../services/databricksAgentApi";

/**
 * The Databricks compute analysis in the existing AI Agent page.
 *
 * What this protects: the agent calls the backend analysis endpoint (not the
 * job/execution path), renders only steps the backend reported as complete,
 * and shows the evidence behind each recommendation. Progress is never faked.
 */

vi.mock("@azure/msal-react", () => ({
  useMsal: () => ({ instance: { getActiveAccount: () => null, getAllAccounts: () => [] } }),
}));

const STEPS = [
  { id: "environment", label: "Environment identified", status: "done", detail: "adb-test" },
  { id: "auth", label: "Databricks authentication verified", status: "done" },
  { id: "discover", label: "Discovering compute resources", status: "done" },
  { id: "resources", label: "Resources discovered", status: "done", detail: "2 resource(s)" },
  { id: "analyze", label: "Analyzing optimization opportunities", status: "done" },
];

const ANALYSIS = {
  workspace_name: "adb-test.azuredatabricks.net",
  observed_facts: { resource_count: 2, by_type: { CLASSIC_CLUSTER: 1, SQL_WAREHOUSE: 1 }, resources: [] },
  classification: [
    {
      resource: "analytics-all-purpose",
      type: "CLASSIC_CLUSTER",
      state: "RUNNING",
      configuration: "worker Standard_DS3_v2, auto-termination 0 min",
    },
    { resource: "warehouse_db", type: "SQL_WAREHOUSE", state: "STOPPED", configuration: "CLASSIC, size Small" },
  ],
  statuses: [{ resource_type: "SERVERLESS_COMPUTE", status: "UNAVAILABLE", reason: "INSUFFICIENT_PERMISSIONS" }],
  missing_evidence: ["CPU and memory utilization — not exposed by the compute API."],
  opportunities: [
    {
      resource: "analytics-all-purpose",
      resource_type: "CLASSIC_CLUSTER",
      resource_id: "c1",
      observed_evidence: "auto_termination_minutes = 0",
      potential_issue: "Auto-termination is disabled.",
      recommendation: "Consider enabling an auto-termination policy.",
      evidence_required: "Cluster event history showing real idle periods.",
      expected_impact: "Qualitative: removes compute time that is paid for but not used.",
    },
  ],
  summary: "Discovered 2 resources and raised 1 configuration observation.",
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

function renderAgent() {
  return render(
    <MemoryRouter>
      <AgentWorkspace />
    </MemoryRouter>,
  );
}

async function ask(text: string) {
  const user = userEvent.setup();
  const box = screen.getByRole("textbox");
  await user.clear(box);
  await user.type(box, text);
  await user.keyboard("{Enter}");
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Databricks agent flow", () => {
  it("routes only Databricks compute prompts to the analysis path", () => {
    expect(isDatabricksComputeRequest("Analyze my Databricks compute and find optimization opportunities.")).toBe(
      true,
    );
    // Existing prompts keep their current behaviour.
    expect(isDatabricksComputeRequest("Check my cluster utilization")).toBe(false);
    expect(isDatabricksComputeRequest("Find unhealthy queries")).toBe(false);
  });

  it("calls the agent analysis endpoint, not the job execution path", async () => {
    const fetchMock = mockAnalyze({
      ok: true,
      status: "OK",
      message: null,
      environment_id: "env-1",
      steps: STEPS,
      analysis: ANALYSIS,
      markdown: "## Observed Facts",
    });

    renderAgent();
    await ask("Analyze my Databricks compute and find optimization opportunities.");

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const called = fetchMock.mock.calls.map((c) => String(c[0]));
    expect(called.some((url) => url.includes("/databricks/agent/analyze"))).toBe(true);
    // The platform job endpoints must not be touched by this path.
    expect(called.some((url) => url.endsWith("/jobs"))).toBe(false);
  });

  it("renders the backend's real steps and the evidence behind each recommendation", async () => {
    mockAnalyze({
      ok: true,
      status: "OK",
      message: null,
      environment_id: "env-1",
      steps: STEPS,
      analysis: ANALYSIS,
      markdown: "## Observed Facts",
    });

    renderAgent();
    await ask("Analyze my Databricks compute and find optimization opportunities.");

    expect(await screen.findByText("Resources discovered")).toBeInTheDocument();
    expect(screen.getByText("Databricks authentication verified")).toBeInTheDocument();

    // Classification, evidence and the qualitative impact are all shown.
    expect(screen.getByText("analytics-all-purpose")).toBeInTheDocument();
    expect(screen.getByText("auto_termination_minutes = 0")).toBeInTheDocument();
    expect(screen.getByText(/Consider enabling an auto-termination policy/)).toBeInTheDocument();
    expect(screen.getByText(/Qualitative:/)).toBeInTheDocument();

    // No invented money anywhere on screen.
    expect(document.body.textContent).not.toMatch(/[$£€]\s?\d/);
  });

  it("shows what could not be observed", async () => {
    mockAnalyze({
      ok: true,
      status: "OK",
      message: null,
      environment_id: "env-1",
      steps: STEPS,
      analysis: ANALYSIS,
      markdown: "## Observed Facts",
    });

    renderAgent();
    await ask("Analyze my Databricks compute.");

    expect(await screen.findByText("Missing Evidence")).toBeInTheDocument();
    expect(screen.getByText(/CPU and memory utilization/)).toBeInTheDocument();
  });

  it("does not mark later steps complete when authentication fails", async () => {
    mockAnalyze({
      ok: false,
      status: "AUTHENTICATION_FAILED",
      message: "Databricks authentication failed.",
      environment_id: "env-1",
      steps: [
        { id: "environment", label: "Environment identified", status: "done" },
        { id: "auth", label: "Databricks authentication verified", status: "failed" },
        { id: "discover", label: "Discovering compute resources", status: "pending" },
        { id: "resources", label: "Resources discovered", status: "pending" },
        { id: "analyze", label: "Analyzing optimization opportunities", status: "pending" },
      ],
      analysis: null,
      markdown: null,
    });

    renderAgent();
    await ask("Analyze my Databricks compute.");

    expect(await screen.findByText("AUTHENTICATION_FAILED")).toBeInTheDocument();
    expect(screen.getByText(/Databricks authentication failed/)).toBeInTheDocument();
    // No analysis is rendered, so nothing implies work that never happened.
    expect(screen.queryByText("Missing Evidence")).not.toBeInTheDocument();
    expect(screen.queryByText("Potential Optimization Opportunities")).not.toBeInTheDocument();
  });
});
