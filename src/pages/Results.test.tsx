/**
 * Results page renders only what the run's result rows contain: real values,
 * a real 0 as 0, and "Not available" where the data has no value.
 */

import { render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { exec, msal } = vi.hoisted(() => ({
  exec: { getClusterResultForJob: vi.fn(), getLatestClusterResult: vi.fn() },
  // Stable identity, like the real MSAL instance (the page's effect depends on it).
  msal: {
    instance: { getActiveAccount: () => null, getAllAccounts: () => [] },
    accounts: [],
    inProgress: "none",
  },
}));

vi.mock("@azure/msal-react", () => ({ useMsal: () => msal }));

vi.mock("../services/executionApi", async () => {
  const actual = await vi.importActual<typeof import("../services/executionApi")>(
    "../services/executionApi"
  );
  return { ...actual, ...exec };
});

import Results from "./Results";

const FABRIC_RUN_ID = "f2d65699-dd22-4889-980c-15226deb0e1b";

function fabricResult(rows: Record<string, unknown>[]) {
  return {
    jobId: "job-1",
    runId: "acelo-run-1",
    platform: "fabric",
    platformRunId: FABRIC_RUN_ID,
    executionType: "pipeline",
    runStatus: "COMPLETED",
    resultTable: "acelo_cluster_recommendations",
    retrievedAt: "2026-09-21T10:00:00Z",
    sourceFile: null,
    focus: null,
    parameterVerification: null,
    rows,
  };
}

function renderResults() {
  return render(
    <MemoryRouter initialEntries={["/results?job=job-1"]}>
      <Routes>
        <Route path="/results" element={<Results />} />
      </Routes>
    </MemoryRouter>
  );
}

const kpi = (label: string) => screen.getByText(label).parentElement!;

beforeEach(() => vi.clearAllMocks());

describe("Results page", () => {
  it("shows metrics computed from the real rows and the pipeline run", async () => {
    exec.getClusterResultForJob.mockResolvedValue(
      fabricResult([
        { cluster_id: "c1", cluster_name: "etl", optimization_label: "Risky", avg_cpu_util: 20,
          avg_memory_util: 30, idle_flag: 1, oversized_flag: 1, total_dbus_cost_usd: 100,
          potential_monthly_savings: 40, max_workers: 8, recommended_max_workers: 5 },
        { cluster_id: "c2", cluster_name: "ml", optimization_label: "Optimized", avg_cpu_util: 80,
          avg_memory_util: 70, idle_flag: 0, oversized_flag: 0, total_dbus_cost_usd: 300,
          potential_monthly_savings: 0, max_workers: 4, recommended_max_workers: 4 },
      ])
    );
    renderResults();

    expect(await screen.findByText("Cluster optimization results")).toBeInTheDocument();
    expect(kpi("Total clusters")).toHaveTextContent("2");
    expect(kpi("Avg CPU utilization")).toHaveTextContent("50.0%");
    expect(kpi("Idle clusters")).toHaveTextContent("1");
    expect(kpi("Current cost")).toHaveTextContent("$400.00");
    expect(kpi("Estimated savings")).toHaveTextContent("$40.00/mo");
    expect(screen.getByText("Fabric pipeline run ID", { selector: "dt" }).parentElement).toHaveTextContent(FABRIC_RUN_ID);
    expect(screen.getByText("Execution", { selector: "dt" }).parentElement).toHaveTextContent("Pipeline · COMPLETED");
    expect(screen.getByText("ACELO run", { selector: "dt" }).parentElement).toHaveTextContent("acelo-run-1");
    expect(screen.getByText(/Reduce max workers from 8 to 5/)).toBeInTheDocument();
  });

  it("missing metrics read 'Not available', never 0", async () => {
    exec.getClusterResultForJob.mockResolvedValue(
      fabricResult([{ cluster_id: "c1", optimization_label: "Optimized" }])
    );
    renderResults();

    await screen.findByText("Cluster optimization results");
    for (const label of ["Current cost", "Estimated savings", "Idle clusters", "Oversized clusters",
                         "Avg CPU utilization", "Avg memory utilization"]) {
      expect(within(kpi(label)).getByText("Not available")).toBeInTheDocument();
    }
  });

  it("a genuine zero is shown as zero", async () => {
    exec.getClusterResultForJob.mockResolvedValue(
      fabricResult([{ cluster_id: "c1", optimization_label: "Optimized", idle_flag: 0, oversized_flag: 0,
                      total_dbus_cost_usd: 0, potential_monthly_savings: 0 }])
    );
    renderResults();

    await screen.findByText("Cluster optimization results");
    expect(kpi("Idle clusters")).toHaveTextContent("0");
    expect(kpi("Estimated savings")).toHaveTextContent("$0.00/mo");
    expect(kpi("Current cost")).toHaveTextContent("$0.00");
  });

  it("no result yet is an honest empty state", async () => {
    exec.getClusterResultForJob.mockResolvedValue(null);
    renderResults();
    expect(await screen.findByText("No results yet")).toBeInTheDocument();
  });
});
