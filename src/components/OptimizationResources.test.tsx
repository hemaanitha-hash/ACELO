import React from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../services/environmentApi", async () => {
  const actual = await vi.importActual<typeof import("../services/environmentApi")>("../services/environmentApi");
  return { ...actual, getOptimizationResources: vi.fn(), saveOptimizationResources: vi.fn() };
});

import * as api from "../services/environmentApi";
import OptimizationResources from "./OptimizationResources";

// This suite covers the legacy multi-platform experience (Fabric, the platform
// switcher, multiple environments). Databricks-only is the default for the MVP,
// so the legacy experience is selected explicitly here.
vi.mock("../services/experience", async () => {
  const actual = await vi.importActual<typeof import("../services/experience")>(
    "../services/experience"
  );
  return { ...actual, isDatabricksOnly: () => false };
});


const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

function status(domain: "cluster" | "query" | "storage", overrides: Partial<api.DomainResourceStatus> = {}) {
  return {
    domain,
    configured: true,
    missing: [],
    execution_type: "notebook" as const,
    resource: { type: "notebook" as const, id: `nb-${domain}`, name: `ACELO ${domain}`, source: "acelo-managed" },
    settings: { result_table: domain === "query" ? "query_email_input" : null },
    ...overrides,
  };
}

const DATA = {
  domains: [
    { ...status("cluster"), execution_type: "pipeline" as const,
      resource: { type: "pipeline" as const, id: "p-1", name: "ACELO_Cluster_Optimization_Pipeline", source: "acelo-managed" } },
    status("query"),
    status("storage", { configured: false, missing: ["notebook"], resource: null }),
  ],
  pipelines: [],
};

beforeEach(() => {
  vi.clearAllMocks();
  mocked.getOptimizationResources.mockResolvedValue(DATA);
  mocked.saveOptimizationResources.mockResolvedValue(DATA);
});

describe("Environment Setup: Optimization Resources", () => {
  it("shows each optimization's resource without asking for tables", async () => {
    render(<OptimizationResources environmentId="env-1" />);
    const cluster = await screen.findByTestId("resource-cluster");
    expect(cluster).toHaveTextContent("Configured");
    expect(cluster).toHaveTextContent("Pipeline: ACELO_Cluster_Optimization_Pipeline (deployed by ACELO)");
    expect(screen.getByTestId("resource-query")).toHaveTextContent("Notebook: ACELO query");
    expect(screen.getByTestId("resource-storage")).toHaveTextContent("Not configured");
    expect(screen.getByTestId("resource-storage")).toHaveTextContent("Missing: notebook");
    // The mapping is an administrator's Advanced section, collapsed by default.
    expect(screen.getByTestId("resource-mapping")).not.toHaveAttribute("open");
  });

  it("an administrator's Query mapping is saved for the query domain only", async () => {
    render(<OptimizationResources environmentId="env-1" />);
    const query = await screen.findByTestId("mapping-query");
    await waitFor(() => expect(within(query).getByLabelText("Query Result table")).toHaveValue("query_email_input"));
    fireEvent.change(within(query).getByLabelText("Query Result table"), { target: { value: "query_results_v2" } });
    fireEvent.click(within(query).getByRole("button", { name: "Save Query mapping" }));
    await waitFor(() => expect(mocked.saveOptimizationResources).toHaveBeenCalledTimes(1));
    const [envId, domain, values] = mocked.saveOptimizationResources.mock.calls[0];
    expect(envId).toBe("env-1");
    expect(domain).toBe("query");
    expect(values.result_table).toBe("query_results_v2");
    expect(await screen.findByText("Query resource mapping saved.")).toBeInTheDocument();
  });

  it("no credentials are ever shown", async () => {
    render(<OptimizationResources environmentId="env-1" />);
    await screen.findByTestId("resource-cluster");
    expect(document.body.textContent?.toLowerCase()).not.toMatch(/client secret|password|access token/);
  });
});

describe("administrator-only mapping", () => {
  it("is read-only for a normal user: inputs and save are disabled", async () => {
    mocked.getOptimizationResources.mockResolvedValue({
      ...DATA,
      access: { can_configure_resources: false, basis: "ACELO_ADMIN_USERS", user: "analyst@contoso.com" },
    });
    const onAccess = vi.fn();
    render(<OptimizationResources environmentId="env-1" onAccess={onAccess} />);
    expect(await screen.findByTestId("mapping-read-only")).toHaveTextContent("managed by your ACELO administrator");
    const query = screen.getByTestId("mapping-query");
    expect(query).toBeDisabled();
    expect(within(query).getByRole("button", { name: "Save Query mapping" })).toBeDisabled();
    expect(onAccess).toHaveBeenCalledWith(false);
  });

  it("shows whether the LLM key is configured, never the key, and no Key Vault fields", async () => {
    mocked.getOptimizationResources.mockResolvedValue({
      ...DATA,
      domains: DATA.domains.map((d) => (d.domain === "query" ? { ...d, llm_api_key_configured: true } : d)),
    });
    render(<OptimizationResources environmentId="env-1" />);
    expect(await screen.findByTestId("llm-key-status")).toHaveTextContent("LLM API key: set in backend configuration");
    expect(screen.queryByLabelText(/Key Vault/)).not.toBeInTheDocument();
  });

  it("values from backend configuration are placeholders, not copied into the admin mapping", async () => {
    mocked.getOptimizationResources.mockResolvedValue({
      ...DATA,
      domains: DATA.domains.map((d) =>
        d.domain === "query"
          ? { ...d, settings: { ...d.settings, lakehouse_id: "lh-from-env" }, sources: { lakehouse_id: "config" } }
          : d
      ),
    });
    render(<OptimizationResources environmentId="env-1" />);
    const input = await screen.findByLabelText("Query Lakehouse ID");
    await waitFor(() => expect(input).toHaveAttribute("placeholder", "lh-from-env"));
    expect(input).toHaveValue("");
  });
});
