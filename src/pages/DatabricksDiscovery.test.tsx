import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import DatabricksDiscovery from "./DatabricksDiscovery";

/**
 * The discovery view renders exactly what the backend reports: real resources,
 * honest empty states, and an explicit unavailable state per resource type.
 * It must never invent a connection or a resource.
 */

const CLASSIC_CLUSTER = {
  platform: "databricks",
  resource_type: "CLASSIC_CLUSTER",
  resource_id: "0421-193742-abcd1234",
  name: "analytics-all-purpose",
  state: "TERMINATED",
  metadata: {
    cluster_type: "UI",
    driver_node_type: "Standard_DS3_v2",
    worker_node_type: "Standard_DS3_v2",
    autoscaling: { min_workers: 2, max_workers: 8 },
    auto_termination_minutes: 30,
  },
};

const SERVERLESS = [
  {
    platform: "databricks",
    resource_type: "SERVERLESS_COMPUTE",
    resource_id: "default-interactive",
    name: "Default Interactive Compute",
    state: "ENABLED",
    metadata: { id: "default-interactive", name: "Default Interactive Compute" },
  },
  {
    platform: "databricks",
    resource_type: "SERVERLESS_COMPUTE",
    resource_id: "default-automated",
    name: "Default Automated Compute",
    state: "ENABLED",
    metadata: { id: "default-automated", name: "Default Automated Compute" },
  },
];

const WAREHOUSES = [
  {
    platform: "databricks",
    resource_type: "SQL_WAREHOUSE",
    resource_id: "b1c2d3e4f5a60718",
    name: "Serverless Starter Warehouse",
    state: "RUNNING",
    metadata: { warehouse_type: "PRO", size: "2X-Small", auto_stop_mins: 10 },
  },
  {
    platform: "databricks",
    resource_type: "SQL_WAREHOUSE",
    resource_id: "a9b8c7d6e5f40312",
    name: "warehouse_db",
    state: "STOPPED",
    metadata: { warehouse_type: "CLASSIC", size: "Small", auto_stop_mins: 45 },
  },
];

const OK_STATUSES = [
  { resource_type: "CLASSIC_CLUSTER", status: "OK" },
  { resource_type: "SERVERLESS_COMPUTE", status: "OK" },
  { resource_type: "SQL_WAREHOUSE", status: "OK" },
];

function mockResponse(body: unknown, ok = true, status = 200) {
  return vi.fn().mockResolvedValue({
    ok,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  });
}

function renderView() {
  return render(
    <MemoryRouter>
      <DatabricksDiscovery />
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("DatabricksDiscovery", () => {
  it("renders the real resources the backend discovered, grouped by type", async () => {
    vi.stubGlobal(
      "fetch",
      mockResponse({
        platform: "databricks",
        environment_id: "env-1",
        workspace_name: "adb-test.azuredatabricks.net",
        connected: true,
        resources: [CLASSIC_CLUSTER, ...SERVERLESS, ...WAREHOUSES],
        statuses: OK_STATUSES,
      }),
    );

    renderView();

    expect(await screen.findByText("Connected")).toBeInTheDocument();
    expect(screen.getByText("Classic Clusters")).toBeInTheDocument();
    expect(screen.getByText("analytics-all-purpose")).toBeInTheDocument();
    expect(screen.getByText("Default Interactive Compute")).toBeInTheDocument();
    expect(screen.getByText("Default Automated Compute")).toBeInTheDocument();
    expect(screen.getByText("Serverless Starter Warehouse")).toBeInTheDocument();
    expect(screen.getByText("warehouse_db")).toBeInTheDocument();
  });

  it("says a successfully-empty resource type has none, rather than erroring", async () => {
    vi.stubGlobal(
      "fetch",
      mockResponse({
        platform: "databricks",
        environment_id: "env-1",
        workspace_name: "adb-test.azuredatabricks.net",
        connected: true,
        resources: [...SERVERLESS, ...WAREHOUSES],
        statuses: OK_STATUSES,
      }),
    );

    renderView();

    expect(await screen.findByText("None in this workspace")).toBeInTheDocument();
    expect(screen.getByText(/asked and reported none/i)).toBeInTheDocument();
    // The other sections still rendered their real resources.
    expect(screen.getByText("warehouse_db")).toBeInTheDocument();
  });

  it("reports an unavailable resource type without hiding the types that worked", async () => {
    vi.stubGlobal(
      "fetch",
      mockResponse({
        platform: "databricks",
        environment_id: "env-1",
        workspace_name: "adb-test.azuredatabricks.net",
        connected: true,
        resources: WAREHOUSES,
        statuses: [
          {
            resource_type: "CLASSIC_CLUSTER",
            status: "UNAVAILABLE",
            reason: "INSUFFICIENT_PERMISSIONS",
            message: "The configured identity does not have access to this resource.",
          },
          { resource_type: "SERVERLESS_COMPUTE", status: "OK" },
          { resource_type: "SQL_WAREHOUSE", status: "OK" },
        ],
      }),
    );

    renderView();

    // Human wording, not the raw reason code.
    expect(await screen.findByText("Not permitted")).toBeInTheDocument();
    expect(screen.queryByText(/INSUFFICIENT_PERMISSIONS/)).not.toBeInTheDocument();
    expect(screen.getByText("Serverless Starter Warehouse")).toBeInTheDocument();
  });

  it("distinguishes a broken call (ERROR) from a refused one (UNAVAILABLE)", async () => {
    vi.stubGlobal(
      "fetch",
      mockResponse({
        platform: "databricks",
        environment_id: "env-1",
        workspace_name: "adb-test.azuredatabricks.net",
        connected: true,
        resources: WAREHOUSES,
        statuses: [
          {
            resource_type: "CLASSIC_CLUSTER",
            status: "ERROR",
            reason: "AUTHENTICATION_FAILED",
            message: "Authentication failed.",
          },
          {
            resource_type: "SERVERLESS_COMPUTE",
            status: "UNAVAILABLE",
            reason: "NOT_EXPOSED_BY_WORKSPACE_API",
          },
          { resource_type: "SQL_WAREHOUSE", status: "OK" },
        ],
      }),
    );

    renderView();

    // A broken call and an unlistable resource type read differently, and
    // neither exposes its enum.
    expect(await screen.findByText("Authentication failed")).toBeInTheDocument();
    // Serverless says the API cannot enumerate it — never that it is
    // "unsupported", which reads as "your serverless compute is not supported".
    expect(screen.getByText("Unable to enumerate")).toBeInTheDocument();
    expect(
      screen.getByText(/does not mean serverless compute is unavailable or that none exist/i),
    ).toBeInTheDocument();
    expect(screen.queryByText("Unsupported")).not.toBeInTheDocument();
    expect(screen.queryByText(/NOT_EXPOSED_BY_WORKSPACE_API/)).not.toBeInTheDocument();
    // The one type that worked still lists its real resources.
    expect(screen.getByText("warehouse_db")).toBeInTheDocument();
  });

  it("never claims a connection when the backend request fails", async () => {
    vi.stubGlobal("fetch", mockResponse({ detail: "No Databricks environment is configured." }, false, 404));

    renderView();

    await waitFor(() =>
      expect(screen.getByText("No Databricks environment is configured.")).toBeInTheDocument(),
    );
    expect(screen.getByText("Not connected")).toBeInTheDocument();
    expect(screen.queryByText("Connected")).not.toBeInTheDocument();
  });
});
