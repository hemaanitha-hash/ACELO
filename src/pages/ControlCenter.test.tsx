import React from "react";
import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../services/experience", async () => {
  const actual = await vi.importActual<typeof import("../services/experience")>("../services/experience");
  return { ...actual, isDatabricksOnly: () => true };
});

import DatabricksOverview from "../components/DatabricksOverview";
import { resetComputeAnalysis } from "../services/computeAnalysis";
import { setRuntime } from "../services/runtime";

type Routes = Record<string, unknown | ((method: string) => unknown)>;
let calls: string[];

function respond(body: unknown, status = 200) {
  return { ok: status < 400, status, text: async () => JSON.stringify(body), json: async () => body };
}

function mockBackend(routes: Routes, opts: { down?: boolean } = {}) {
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const u = String(url);
      calls.push(`${init?.method ?? "GET"} ${u}`);
      if (opts.down) throw new TypeError("Failed to fetch");
      const key = Object.keys(routes).sort((a, b) => b.length - a.length).find((k) => u.includes(k));
      if (!key) return respond({ detail: "Not Found" }, 404);
      const v = routes[key];
      return respond(typeof v === "function" ? (v as (m: string) => unknown)(init?.method ?? "GET") : v);
    }),
  );
}

const BASE_ROUTES: Routes = {
  "/health": { ok: true },
  "/databricks/resources": {
    platform: "databricks",
    environment_id: "env-1",
    workspace_name: "adb-123.azuredatabricks.net",
    connected: true,
    resources: [{ platform: "databricks", resource_type: "CLASSIC_CLUSTER", resource_id: "c1", name: "etl", state: "RUNNING", metadata: {} }],
    statuses: [],
  },
  "/environments/env-1/optimization-resources": {
    domains: [
      {
        domain: "cluster",
        configured: true,
        missing: [],
        execution_type: "notebook",
        resource: { type: "notebook", id: "/Workspace/Users/x/ACELO compute_cost_agent_full", name: null, source: "configured" },
        settings: { source_table: "databricks_ws.agent.cluster", result_table: "default.compute_agent_findings" },
      },
    ],
    pipelines: [],
  },
  "/optimizations": [],
  "/approvals/recommendations": [],
  "/runs/active": { count: 0, runs: [] },
  "/runs": { total: 0, runs: [] },
  "/overview": { kpis: { monthlyCost: null, potentialSavings: null } },
};

function renderPage() {
  return render(
    <MemoryRouter>
      <DatabricksOverview />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  resetComputeAnalysis();
  setRuntime({ databricks_app: true, workspace_host: "https://adb-123.azuredatabricks.net", auth_mode: "databricks_app_identity" });
});
afterEach(() => vi.unstubAllGlobals());

describe("Optimization Control Center", () => {
  it("shows real runtime values and never starts an analysis", async () => {
    mockBackend(BASE_ROUTES);
    renderPage();

    expect(await screen.findByText("Optimization Control Center")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("System Healthy")).toBeInTheDocument());
    expect(await screen.findByText("/Workspace/Users/x/ACELO compute_cost_agent_full")).toBeInTheDocument();
    expect(screen.getByText("default.compute_agent_findings")).toBeInTheDocument();
    expect(screen.getByText("Databricks Jobs API (runs/submit)")).toBeInTheDocument();
    expect(calls.some((c) => c.includes("/agent/analyze"))).toBe(false);
    expect(calls.some((c) => c.startsWith("POST"))).toBe(false);
  });

  it("labels metrics the backend does not provide instead of inventing them", async () => {
    mockBackend(BASE_ROUTES);
    renderPage();
    await screen.findByText("System Healthy");

    expect(screen.getAllByText("Not available yet").length).toBeGreaterThanOrEqual(2); // cost + savings
    for (const area of ["Cost", "Jobs & Pipelines", "SQL", "Storage"]) {
      const row = screen.getByRole("link", { name: area }).closest("li")!;
      expect(within(row).getByText("Not activated")).toBeInTheDocument();
    }
    expect(document.body.textContent).not.toMatch(/\$\s?\d/);
  });

  it("never presents the legacy reference notebook as active", async () => {
    mockBackend({
      ...BASE_ROUTES,
      "/environments/env-1/optimization-resources": {
        domains: [
          {
            domain: "cluster", configured: true, missing: [], execution_type: "notebook",
            resource: { type: "notebook", id: "legacy_reference/compute_cost_agent_full.ipynb", name: null, source: "configured" },
            settings: {},
          },
        ],
        pipelines: [],
      },
    });
    renderPage();
    await screen.findByText("System Healthy");
    await waitFor(() => expect(screen.getByText("Optimization Notebook")).toBeInTheDocument());
    expect(document.body.textContent).not.toMatch(/legacy_reference/);
  });

  it("says the backend is unavailable — not that Databricks is disconnected", async () => {
    mockBackend({}, { down: true });
    renderPage();
    expect(await screen.findByText("ACELO backend unavailable")).toBeInTheDocument();
    expect(screen.getByText("Backend Unavailable")).toBeInTheDocument();
    expect(screen.queryByText("System Healthy")).not.toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/Databricks disconnected/i);
  });

  it("distinguishes an unreachable Databricks environment and never shows Healthy", async () => {
    mockBackend({
      ...BASE_ROUTES,
      "/databricks/resources": {
        platform: "databricks", environment_id: "env-1", workspace_name: null, connected: false, resources: [],
        statuses: [{ resource_type: "CLASSIC_CLUSTER", status: "ERROR", reason: "AUTHENTICATION_FAILED" }],
      },
    });
    renderPage();
    expect(await screen.findByText("Databricks environment unavailable")).toBeInTheDocument();
    expect(within(screen.getByTestId("system-status")).getByText("Needs Attention")).toBeInTheDocument();
    expect(screen.queryByText("System Healthy")).not.toBeInTheDocument();
    expect(screen.getByText(/CLASSIC_CLUSTER: ERROR \(AUTHENTICATION_FAILED\)/)).toBeInTheDocument();
  });

  it("builds attention and activity from real persisted records", async () => {
    mockBackend({
      ...BASE_ROUTES,
      "/optimizations": [
        {
          id: "r1", recommendation_id: "r1", kind: "stage1", resource: "production-etl-01", resource_id: "c1", domain: "cluster",
          title: "CPU-heavy cluster", severity: "HIGH", created_at: "2026-09-30T08:00:00Z",
          details: { approval_status: "APPROVED", execution_status: "NOT_STARTED" },
        },
      ],
      "/approvals/recommendations": [
        {
          approval_id: "a1", recommendation_id: "r1", status: "approved", requested_by: null, decided_by: "Riley",
          created_at: "2026-09-30T09:00:00Z", decided_at: "2026-09-30T10:00:00Z",
          recommendation: { id: "x", title: "CPU-heavy cluster", resource: "production-etl-01", approval_status: "APPROVED", execution_status: "NOT_STARTED" },
        },
      ],
    });
    renderPage();
    const attention = await screen.findByTestId("attention-list");
    expect(within(attention).getByText("Approved recommendations ready for execution")).toBeInTheDocument();
    expect(within(attention).getByText("production-etl-01")).toBeInTheDocument();
    const activity = screen.getByTestId("recent-activity");
    expect(within(activity).getByText("Approved · Ready to Execute")).toBeInTheDocument();
    expect(within(activity).getByText("Sent for approval")).toBeInTheDocument();
    expect(within(activity).getByText("Recommendation created")).toBeInTheDocument();
  });
});
