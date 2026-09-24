import React from "react";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Settings from "./Settings";
import {
  getActiveContext,
  resetActiveContext,
  setActiveContext,
  type PlatformConnection,
} from "../services/platformContext";

// This suite covers the legacy multi-platform experience (Fabric, the platform
// switcher, multiple environments). Databricks-only is the default for the MVP,
// so the legacy experience is selected explicitly here.
vi.mock("../services/experience", async () => {
  const actual = await vi.importActual<typeof import("../services/experience")>(
    "../services/experience"
  );
  return { ...actual, isDatabricksOnly: () => false };
});


/**
 * Environment Setup must derive its form from the GLOBAL active platform.
 *
 * The bug: Settings held its own `useState<EnvironmentPlatform>("fabric")` — a
 * second source of truth that ignored the connected platform. With Databricks
 * active the page still selected Microsoft Fabric, loaded the Fabric
 * environment and applied its Microsoft Account auth mode, so Fabric fields
 * rendered over a Databricks connection.
 */

vi.mock("@azure/msal-react", () => ({
  useMsal: () => ({
    instance: { getActiveAccount: () => null, getAllAccounts: () => [], setActiveAccount: () => {} },
    accounts: [],
    inProgress: "none",
  }),
}));

const DATABRICKS: PlatformConnection = {
  id: "db-prod",
  platform: "databricks",
  name: "Databricks Demo",
  status: "connected",
};
const FABRIC: PlatformConnection = {
  id: "fabric-prod",
  platform: "fabric",
  name: "Fabric Workspace",
  status: "connected",
};

/** Environments for BOTH platforms, so a wrong pick would be observable. */
const ENVIRONMENTS = [
  {
    id: "env-fabric",
    customer_id: "c1",
    connection_id: "fabric-prod",
    name: "Fabric Production",
    platform: "fabric",
    auth_mode: "user", // Microsoft Account — the mode that leaked into Databricks
    tenant_id: "tenant-abc",
    workspace_id: "fabric-workspace-id",
    workspace_name: "Fabric Workspace",
    status: "connected",
    last_error_code: null,
    last_error_message: null,
    last_verified_at: null,
    last_discovered_at: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  },
  {
    id: "env-databricks",
    customer_id: "c1",
    connection_id: "db-prod",
    name: "Databricks Demo",
    platform: "databricks",
    auth_mode: "pat",
    tenant_id: null,
    workspace_id: null,
    workspace_name: "adb-test.azuredatabricks.net",
    status: "connected",
    last_error_code: null,
    last_error_message: null,
    last_verified_at: null,
    last_discovered_at: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  },
];

const EMPTY_CLUSTER_STATE = {
  settings: {
    source_table: "", result_table: "", source_lakehouse: "", result_lakehouse: "",
    lakehouse_database: "", sql_endpoint: "", column_mapping: "", fabric_environment_id: "",
    execution_type: "", source_schema: "", result_schema: "", pipeline_id: "",
    lakehouse_id: "", lakehouse_workspace_id: "", approval_tracking_table: "",
  },
  missing: [],
  execution: null,
  pipelines: [],
};

/**
 * Routes each request to a realistic body. Order matters: the more specific
 * environment sub-resources are matched before the collection itself.
 */
// The API module is mocked (as the existing Settings test does) so this test
// exercises platform routing, not the network layer.
vi.mock("../services/environmentApi", async () => {
  const actual = await vi.importActual<typeof import("../services/environmentApi")>(
    "../services/environmentApi"
  );
  return {
    ...actual,
    listEnvironments: vi.fn(async () => ENVIRONMENTS),
    getEnvironment: vi.fn(async (id: string) => ENVIRONMENTS.find((e) => e.id === id)),
    getProvisioningStatus: vi.fn(async () => {
      throw new Error("not provisioned");
    }),
    getReadiness2: vi.fn(async () => {
      throw new Error("not ready");
    }),
    getClusterSettings: vi.fn(async () => {
      throw new Error("no settings");
    }),
    getPersistedDiscovery: vi.fn(async () => {
      throw new Error("not discovered");
    }),
  };
});

vi.mock("../services/platformConnections", () => ({
  listPlatformConnections: vi.fn(async () => [DATABRICKS, FABRIC]),
}));

function renderSettings() {
  return render(
    <MemoryRouter>
      <Settings />
    </MemoryRouter>,
  );
}

/** What the page says it is configuring — the platform is stated, not asked. */
async function configuringText(): Promise<string> {
  const label = await screen.findByText("Configuring");
  return label.parentElement?.textContent ?? "";
}

/** The input belonging to a Field, which wraps its input in a <label>. */
function fieldInput(label: string): HTMLInputElement {
  // The form field is the first occurrence; a label may also appear in the
  // read-only connection summary below it.
  const span = screen.getAllByText(label)[0];
  const input = span.closest("label")?.querySelector("input");
  if (!input) throw new Error(`No input found for field "${label}"`);
  return input as HTMLInputElement;
}

describe("Environment Setup follows the active platform", () => {
  it("Databricks active → renders the Databricks form, never the Fabric one", async () => {
    setActiveContext(DATABRICKS);
    renderSettings();

    // Databricks-only fields.
    expect(await screen.findByText("Workspace URL")).toBeInTheDocument();
    expect(screen.getByText("Access Token")).toBeInTheDocument();

    // No Fabric fields or Fabric authentication anywhere.
    expect(screen.queryByText("Tenant ID")).not.toBeInTheDocument();
    expect(screen.queryByText("Client ID")).not.toBeInTheDocument();
    expect(screen.queryByText("Client Secret")).not.toBeInTheDocument();
    expect(screen.queryByText("Authentication Method")).not.toBeInTheDocument();
    expect(screen.queryByText("Microsoft Account")).not.toBeInTheDocument();

    // The page states Databricks, rather than defaulting to Fabric as before.
    expect(await configuringText()).toContain("Databricks");
  });

  it("Fabric active → renders the Fabric form, never the Databricks one", async () => {
    setActiveContext(FABRIC);
    renderSettings();

    expect(await screen.findByText("Authentication Method")).toBeInTheDocument();
    expect(screen.getByText("Microsoft Account")).toBeInTheDocument();
    // Fabric mode shows the Workspace ID in the form and again in the
    // connection summary, so both occurrences are expected.
    expect(screen.getAllByText("Workspace ID").length).toBeGreaterThan(0);

    // Databricks-only fields must be absent.
    expect(screen.queryByText("Workspace URL")).not.toBeInTheDocument();
    expect(screen.queryByText("Access Token")).not.toBeInTheDocument();

    expect(await configuringText()).toContain("Microsoft Fabric");
  });

  it("does not require a Fabric connection while Databricks is active", async () => {
    setActiveContext(DATABRICKS);
    renderSettings();

    await screen.findByText("Workspace URL");
    // The blocked-step messages must name Databricks, not Fabric.
    expect(screen.queryByText(/Fabric connection required/i)).not.toBeInTheDocument();
  });

  it("switching Databricks → Fabric updates the form", async () => {
    setActiveContext(DATABRICKS);
    renderSettings();
    await screen.findByText("Workspace URL");

    setActiveContext(FABRIC);

    expect(await screen.findByText("Authentication Method")).toBeInTheDocument();
    expect(screen.queryByText("Workspace URL")).not.toBeInTheDocument();
    await waitFor(() => expect(getActiveContext().platform).toBe("fabric"));
  });

  it("switching Fabric → Databricks updates the form", async () => {
    setActiveContext(FABRIC);
    renderSettings();
    await screen.findByText("Authentication Method");

    setActiveContext(DATABRICKS);

    expect(await screen.findByText("Workspace URL")).toBeInTheDocument();
    expect(screen.queryByText("Authentication Method")).not.toBeInTheDocument();
    await waitFor(() => expect(getActiveContext().platform).toBe("databricks"));
  });

  it("does not ask for the platform again once a connection decides it", async () => {
    setActiveContext(DATABRICKS);
    renderSettings();

    await screen.findByText("Workspace URL");
    // The three selectable platform cards are gone from the settings surface:
    // the active connection already answered that question.
    expect(screen.queryByRole("button", { name: "File Analysis" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Microsoft Fabric" })).not.toBeInTheDocument();
    expect(await configuringText()).toContain("Databricks Demo");
  });

  it("keeps adding a platform as a separate, explicit flow", async () => {
    setActiveContext(DATABRICKS);
    renderSettings();
    await screen.findByText("Workspace URL");

    await userEvent.click(screen.getByRole("button", { name: /add connection/i }));

    // Platform choice lives in the dialog and only there. ("Add connection" is
    // both the trigger and the dialog heading, so match the dialog.)
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: /Microsoft Fabric/ }));

    // Now configuring a NEW Fabric connection, clearly labelled as such...
    expect(await screen.findByText("New connection")).toBeInTheDocument();
    expect(await screen.findByText("Authentication Method")).toBeInTheDocument();
    // ...without changing which platform the rest of the app is in.
    expect(getActiveContext().platform).toBe("databricks");

    // Cancelling returns to the connection in use.
    await userEvent.click(screen.getByRole("button", { name: /^cancel$/i }));
    expect(await screen.findByText("Configuring")).toBeInTheDocument();
    expect(await screen.findByText("Workspace URL")).toBeInTheDocument();
  });

  it("never mixes one platform's connection data into the other's form", async () => {
    setActiveContext(FABRIC);
    renderSettings();

    // Fabric's stored identifiers are loaded.
    await waitFor(() => expect(fieldInput("Workspace ID")).toHaveValue("fabric-workspace-id"));

    setActiveContext(DATABRICKS);

    await screen.findByText("Workspace URL");
    const workspaceUrl = fieldInput("Workspace URL");
    // The Fabric tenant/workspace must not survive into the Databricks form.
    expect(workspaceUrl).toHaveValue("");
    expect(document.body.textContent).not.toContain("fabric-workspace-id");
    expect(document.body.textContent).not.toContain("tenant-abc");
  });
});
