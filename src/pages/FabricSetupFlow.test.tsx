/**
 * Fabric setup flow: select Fabric -> authenticate -> Test Connection ->
 * Discover -> Cluster settings -> Set up ACELO -> AI Agent.
 *
 * Regression cover for the "controls look disabled / can't progress" report:
 * every step must become enabled from the BACKEND's responses, and every
 * unavailable step must say why.
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const ACCOUNT = {
  homeAccountId: "h",
  environment: "login.microsoftonline.com",
  tenantId: "t",
  username: "user@contoso.com",
  localAccountId: "l",
  name: "Test User",
};

const msalInstance = {
  getActiveAccount: vi.fn(() => ACCOUNT as typeof ACCOUNT | null),
  getAllAccounts: vi.fn(() => [ACCOUNT]),
  setActiveAccount: vi.fn(),
  loginPopup: vi.fn(async () => ({ account: ACCOUNT })),
  acquireTokenSilent: vi.fn(async () => ({ accessToken: "SECRET-DELEGATED-TOKEN" })),
  acquireTokenPopup: vi.fn(),
  logoutPopup: vi.fn(async () => undefined),
};

vi.mock("@azure/msal-react", () => ({
  useMsal: () => ({ instance: msalInstance, accounts: [ACCOUNT], inProgress: "none" }),
  MsalProvider: ({ children }: { children: React.ReactNode }) => children,
}));

vi.mock("../authConfig", async () => {
  const actual = await vi.importActual<typeof import("../authConfig")>("../authConfig");
  return { ...actual, isMsalConfigured: () => true, MSAL_CLIENT_ID: "test-client-id" };
});

// --- a tiny in-memory backend. Each call mutates it the way the real one does. ---
const { api, backend, BASE_ENV, EMPTY_SETTINGS } = vi.hoisted(() => {
type Env = Record<string, unknown>;
const backend: {
  env: Env | null;
  discovered: boolean;
  installed: boolean;
  settings: Record<string, string>;
  provisioningExtra?: Record<string, unknown>;
} = { env: null, discovered: false, installed: false, settings: {} };

const BASE_ENV: Env = {
  id: "env-1",
  customer_id: "c",
  connection_id: "conn-1",
  name: "Fabric Production",
  platform: "fabric",
  auth_mode: "user",
  tenant_id: null,
  workspace_id: "ws-1",
  workspace_name: null,
  status: "not_configured",
  last_error_code: null,
  last_error_message: null,
  last_verified_at: null,
  last_discovered_at: null,
  created_at: "2026-09-21T09:00:00Z",
  updated_at: "2026-09-21T09:00:00Z",
};

const EMPTY_SETTINGS = {
  source_table: "",
  result_table: "",
  source_lakehouse: "",
  result_lakehouse: "",
  lakehouse_database: "",
  sql_endpoint: "",
  column_mapping: "",
  fabric_environment_id: "",
  execution_type: "",
  source_schema: "",
  result_schema: "",
  pipeline_id: "",
  lakehouse_id: "",
  lakehouse_workspace_id: "",
  approval_tracking_table: "",
};

// Pipelines the fake workspace contains (the real API lists discovered + ACELO ones).
const PIPELINES = [
  { id: "pl-acelo", name: "ACELO_Cluster_Optimization_Pipeline", managed: true },
  { id: "pl-other", name: "Customer_Own_Pipeline", managed: false },
];

function missing() {
  return ["source_table", "result_table"].filter((k) => !backend.settings[k]);
}

/** Same shape and resolution rules as GET/PUT /cluster-settings. */
function clusterState() {
  const pipelineMode = backend.settings.execution_type === "pipeline";
  const configured = PIPELINES.find((p) => p.id === backend.settings.pipeline_id);
  const chosen = configured ?? PIPELINES[0];
  return {
    settings: { ...backend.settings },
    missing: missing(),
    execution: {
      execution_type: pipelineMode ? "pipeline" : "notebook",
      pipeline: { id: chosen.id, name: chosen.name, source: configured ? "configured" : "acelo-managed" },
    },
    pipelines: PIPELINES,
  };
}

function provisioning() {
  const domain = (ready: boolean) => ({
    asset_available: true,
    deployed: ready,
    display_name: "ACELO Cluster Optimization",
    platform_resource_id: ready ? "nb-real-1" : null,
    ready,
    error_code: null,
    message: null,
  });
  return {
    status: backend.installed ? "INSTALLED" : "NOT_INSTALLED",
    step: null,
    package_name: "ACELO",
    package_version_available: "1.2.0",
    package_version_installed: backend.installed ? "1.2.0" : null,
    deployable: true,
    namespace: "ACELO",
    last_provisioned_at: null,
    domains: { cluster: domain(backend.installed), query: domain(false), storage: domain(false) },
    error_code: null,
    message: null,
    missing_assets: [],
    ...(backend.provisioningExtra ?? {}),
  };
}

const api = {
  listEnvironments: vi.fn(async () => (backend.env ? [backend.env] : [])),
  createEnvironment: vi.fn(async () => {
    backend.env = { ...BASE_ENV };
    return backend.env;
  }),
  updateEnvironment: vi.fn(async () => backend.env),
  getEnvironment: vi.fn(async () => backend.env),
  testEnvironment: vi.fn(async () => {
    backend.env = {
      ...backend.env,
      status: "connected",
      workspace_name: "acelo demo",
      last_verified_at: "2026-09-21T10:00:00Z",
    };
    return {
      platform: "fabric",
      connected: true,
      workspace_id: "ws-1",
      workspace_name: "acelo demo",
      message: "Fabric connection verified",
      last_verified_at: "2026-09-21T10:00:00Z",
    };
  }),
  discoverEnvironment: vi.fn(async () => {
    backend.discovered = true;
    backend.env = { ...backend.env, status: "environment_ready", last_discovered_at: "2026-09-21T10:01:00Z" };
    return {
      discovered: true,
      workspace: { id: "ws-1", name: "acelo demo" },
      items: [{ id: "lh-1", display_name: "Data", type: "Lakehouse" }],
      counts: { Lakehouse: 1 },
    };
  }),
  getPersistedDiscovery: vi.fn(async () =>
    backend.discovered
      ? {
          discovered: true,
          workspace: { id: "ws-1", name: "acelo demo" },
          items: [{ id: "lh-1", display_name: "Data", type: "Lakehouse" }],
          counts: { Lakehouse: 1 },
        }
      : null
  ),
  provisionEnvironment: vi.fn(async () => {
    backend.installed = true;
    return provisioning();
  }),
  getProvisioningStatus: vi.fn(async () => provisioning()),
  getReadiness2: vi.fn(async () => {
    const connected = backend.env?.status !== "not_configured";
    const ready = connected && backend.discovered && backend.installed && missing().length === 0;
    return {
      authentication: connected,
      workspace_access: connected,
      environment_discovery: backend.discovered,
      ready_for_analysis: ready,
      status: String(backend.env?.status),
      package_status: provisioning().status,
      cluster_ready: backend.installed,
      query_ready: false,
      storage_ready: false,
      cluster_ready_for_analysis: ready,
      cluster_configured: missing().length === 0,
      cluster_blocked_reason: ready
        ? null
        : !backend.installed
          ? "The Cluster optimization notebook is not deployed."
          : `Missing Cluster settings: ${missing().join(", ")}.`,
    };
  }),
  getClusterSettings: vi.fn(async () => clusterState()),
  saveClusterSettings: vi.fn(async (_id: string, s: Record<string, string>) => {
    backend.settings = { ...backend.settings, ...s };
    return clusterState();
  }),
};
return { api, backend, BASE_ENV, EMPTY_SETTINGS };
});

vi.mock("../services/environmentApi", async () => {
  const actual = await vi.importActual<typeof import("../services/environmentApi")>(
    "../services/environmentApi"
  );
  return { ...actual, ...api };
});

import Settings from "./Settings";

function renderSettings() {
  return render(
    <MemoryRouter initialEntries={["/settings"]}>
      <Routes>
        <Route path="/settings" element={<Settings />} />
        <Route path="/agent" element={<p>AI AGENT PAGE</p>} />
      </Routes>
    </MemoryRouter>
  );
}

async function chooseMicrosoftAccount(user: ReturnType<typeof userEvent.setup>) {
  const radios = await screen.findAllByRole("radio");
  await user.click(radios[0]);
  await screen.findByText("Signed in as:");
}

const section = (heading: RegExp) => screen.getByRole("heading", { name: heading }).closest("section")!;

beforeEach(() => {
  vi.clearAllMocks();
  Object.assign(backend, { env: null, discovered: false, installed: false, settings: { ...EMPTY_SETTINGS } });
});

describe("Microsoft Fabric selection", () => {
  it("is selected and stays selected (re-clicking does not wipe state)", async () => {
    backend.env = { ...BASE_ENV, status: "connected", workspace_name: "acelo demo", last_verified_at: "x" };
    const user = userEvent.setup();
    renderSettings();

    const fabric = await screen.findByRole("button", { name: "Microsoft Fabric" });
    expect(fabric).toHaveAttribute("aria-pressed", "true");
    await screen.findByText(/^Connected$/);

    await user.click(fabric);
    expect(fabric).toHaveAttribute("aria-pressed", "true");
    // The loaded connection must survive the click.
    expect(screen.getByText(/^Connected$/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^discover environment$/i })).toBeEnabled();
  });

  it("the file card points to the AI Agent instead of a stale 'not available' message", async () => {
    const user = userEvent.setup();
    renderSettings();
    await user.click(await screen.findByRole("button", { name: "File Analysis" }));
    expect(screen.queryByText(/not available yet/i)).not.toBeInTheDocument();
    expect(screen.getByText(/no setup needed for file analysis/i)).toBeInTheDocument();
  });
});

describe("Test Connection", () => {
  it("explains why it is unavailable instead of silently disabling", async () => {
    const user = userEvent.setup();
    renderSettings();
    await chooseMicrosoftAccount(user);

    expect(screen.getByRole("button", { name: /^test connection$/i })).toBeDisabled();
    expect(screen.getByTestId("test-blocked-reason")).toHaveTextContent("Enter the Fabric Workspace ID.");
  });

  it("does not require an environment name (the old silent gate)", async () => {
    const user = userEvent.setup();
    renderSettings();
    await chooseMicrosoftAccount(user);
    await user.type(screen.getByPlaceholderText("Fabric workspace ID"), "ws-1");

    const button = screen.getByRole("button", { name: /^test connection$/i });
    expect(button).toBeEnabled();
    await user.click(button);

    expect(api.createEnvironment).toHaveBeenCalledWith(
      expect.objectContaining({ name: "Fabric Production", workspace_id: "ws-1", auth_mode: "user" })
    );
    // The delegated token is sent to the backend, never shown.
    expect(api.testEnvironment).toHaveBeenCalledWith("env-1", "SECRET-DELEGATED-TOKEN");
    expect(await screen.findByText(/^Connected$/)).toBeInTheDocument();
  });

  it("shows a failed connection with its reason and keeps later steps locked", async () => {
    api.testEnvironment.mockResolvedValueOnce({
      platform: "fabric",
      connected: false,
      message: "The signed-in account has no access to this workspace.",
      error_code: "PERMISSION_DENIED",
    } as never);
    const user = userEvent.setup();
    renderSettings();
    await chooseMicrosoftAccount(user);
    await user.type(screen.getByPlaceholderText("Fabric workspace ID"), "ws-1");
    await user.click(screen.getByRole("button", { name: /^test connection$/i }));

    expect(await screen.findByText("Connection Failed")).toBeInTheDocument();
    expect(screen.getByText(/no access to this workspace/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^discover environment$/i })).toBeDisabled();
    expect(within(section(/environment discovery/i)).getByText("Fabric connection required.")).toBeInTheDocument();
  });
});

describe("full setup flow", () => {
  it("connect -> discover -> settings -> set up -> ready -> AI Agent", async () => {
    const user = userEvent.setup();
    renderSettings();
    await chooseMicrosoftAccount(user);

    // Before connecting, every later step says what it needs.
    expect(screen.getByRole("button", { name: /^discover environment$/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /set up acelo in fabric/i })).toBeDisabled();
    expect(screen.getByTestId("provision-blocked-reason")).toHaveTextContent("Fabric connection required.");

    await user.type(screen.getByPlaceholderText("Fabric workspace ID"), "ws-1");
    await user.click(screen.getByRole("button", { name: /^test connection$/i }));
    await screen.findByText(/^Connected$/);

    // Connected: Discover enabled, Set up waits for discovery.
    const discover = screen.getByRole("button", { name: /^discover environment$/i });
    await waitFor(() => expect(discover).toBeEnabled());
    expect(screen.getByTestId("provision-blocked-reason")).toHaveTextContent(
      "Environment discovery required."
    );

    await user.click(discover);
    expect(await screen.findByTestId("discovery-complete")).toHaveTextContent("Complete");

    const setup = screen.getByRole("button", { name: /set up acelo in fabric/i });
    await waitFor(() => expect(setup).toBeEnabled());

    // Cluster settings are required configuration, and say so.
    expect(await screen.findByTestId("cluster-settings-missing")).toHaveTextContent(
      "Required configuration missing: source_table, result_table."
    );
    await user.type(screen.getByLabelText("Source table"), "realistic_cluster_dataset");
    await user.type(
      screen.getByLabelText("Result table"),
      "acelo_cluster_optimization_results"
    );
    await user.click(screen.getByRole("button", { name: /save cluster settings/i }));
    await waitFor(() => expect(screen.queryByTestId("cluster-settings-missing")).not.toBeInTheDocument());
    expect(api.saveClusterSettings).toHaveBeenCalledWith(
      "env-1",
      expect.objectContaining({ source_table: "realistic_cluster_dataset" })
    );

    await user.click(setup);
    expect(await screen.findByTestId("install-complete")).toHaveTextContent("Installed");

    // Readiness comes from the backend after the last operation.
    await waitFor(() => expect(screen.getByTestId("cluster-readiness")).toHaveTextContent("READY TO RUN"));
    expect(screen.getByTestId("overall-readiness")).toHaveTextContent("READY FOR ANALYSIS");

    await user.click(screen.getByRole("button", { name: /go to ai agent/i }));
    expect(await screen.findByText("AI AGENT PAGE")).toBeInTheDocument();
  });

  it("restores Connected / Discovery Complete / Installed from the backend after a reload", async () => {
    backend.env = {
      ...BASE_ENV,
      status: "environment_ready",
      workspace_name: "acelo demo",
      last_verified_at: "2026-09-21T10:00:00Z",
      last_discovered_at: "2026-09-21T10:01:00Z",
    };
    backend.discovered = true;
    backend.installed = true;
    backend.settings = { ...EMPTY_SETTINGS, source_table: "src", result_table: "dst" };

    renderSettings();

    expect(await screen.findByText(/^Connected$/)).toBeInTheDocument();
    expect(await screen.findByTestId("discovery-complete")).toBeInTheDocument();
    expect(await screen.findByTestId("install-complete")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("cluster-readiness")).toHaveTextContent("READY TO RUN"));
    expect(screen.getByRole("button", { name: /re-run acelo setup/i })).toBeEnabled();
  });

  it("shows the backend's reason when Cluster is not ready", async () => {
    backend.env = {
      ...BASE_ENV,
      status: "environment_ready",
      workspace_name: "acelo demo",
      last_verified_at: "x",
      last_discovered_at: "y",
    };
    backend.discovered = true;
    backend.installed = true;

    renderSettings();

    expect(await screen.findByTestId("cluster-blocked-reason")).toHaveTextContent(
      "Missing Cluster settings: source_table, result_table."
    );
    expect(screen.queryByRole("button", { name: /go to ai agent/i })).not.toBeInTheDocument();
  });

  it("reports a provisioning failure without claiming installation", async () => {
    backend.env = {
      ...BASE_ENV,
      status: "environment_ready",
      workspace_name: "acelo demo",
      last_verified_at: "x",
      last_discovered_at: "y",
    };
    backend.discovered = true;
    api.provisionEnvironment.mockRejectedValueOnce(
      new (await import("../services/environmentApi")).ApiError("Fabric refused: InsufficientScopes", 403)
    );
    const user = userEvent.setup();
    renderSettings();

    const setup = await screen.findByRole("button", { name: /set up acelo in fabric/i });
    await waitFor(() => expect(setup).toBeEnabled());
    await user.click(setup);

    expect(await screen.findByText(/InsufficientScopes/)).toBeInTheDocument();
    expect(screen.queryByTestId("install-complete")).not.toBeInTheDocument();
  });
});

describe("execution path and Spark libraries", () => {
  it("saves the pipeline execution type and a Fabric Environment id", async () => {
    backend.env = { ...BASE_ENV, status: "environment_ready", workspace_name: "acelo demo", last_verified_at: "x", last_discovered_at: "y" };
    backend.discovered = true;
    const user = userEvent.setup();
    renderSettings();

    const select = await screen.findByLabelText("Execution type");
    await user.selectOptions(select, "pipeline");
    await user.type(
      screen.getByLabelText("Fabric Environment ID"),
      "9e9e9e9e-1111-2222-3333-444444444444"
    );
    await user.click(screen.getByRole("button", { name: /save cluster settings/i }));

    await waitFor(() =>
      expect(api.saveClusterSettings).toHaveBeenCalledWith(
        "env-1",
        expect.objectContaining({
          execution_type: "pipeline",
          fabric_environment_id: "9e9e9e9e-1111-2222-3333-444444444444",
        })
      )
    );
  });

  it("says when the notebook runs on the workspace default environment", async () => {
    backend.env = { ...BASE_ENV, status: "environment_ready", workspace_name: "acelo demo", last_verified_at: "x", last_discovered_at: "y" };
    backend.discovered = true;
    backend.installed = true;
    renderSettings();
    expect(await screen.findByTestId("fabric-environment")).toHaveTextContent(/workspace default Environment/);
    expect(screen.getByTestId("execution-path")).toHaveTextContent("Direct notebook");
  });
});

describe("Cluster Settings persistence (backend is the source of truth)", () => {
  const READY_ENV = () => ({
    ...BASE_ENV,
    status: "environment_ready",
    workspace_name: "acelo demo",
    last_verified_at: "x",
    last_discovered_at: "y",
  });

  async function fillAndSave(user: ReturnType<typeof userEvent.setup>) {
    await user.type(await screen.findByLabelText("Source table"), "realistic_cluster_dataset");
    await user.type(screen.getByLabelText("Result table"), "acelo_cluster_recommendations");
    await user.type(screen.getByLabelText("Lakehouse"), "Data");
    await user.type(screen.getByLabelText("Table schema"), "dbo");
    await user.selectOptions(screen.getByLabelText("Execution type"), "pipeline");
    await user.click(screen.getByRole("button", { name: /save cluster settings/i }));
  }

  it("save -> full reload restores Pipeline from the backend, not local state", async () => {
    backend.env = READY_ENV();
    backend.discovered = true;
    const user = userEvent.setup();
    const first = renderSettings();
    await fillAndSave(user);
    await screen.findByTestId("settings-saved");

    // Simulate a browser refresh: throw away all component state.
    first.unmount();
    renderSettings();

    const select = await screen.findByLabelText("Execution type");
    await waitFor(() => expect(select).toHaveValue("pipeline"));
    expect(screen.getByLabelText("Pipeline")).toHaveValue("pl-acelo");
    expect(screen.getByLabelText("Source table")).toHaveValue("realistic_cluster_dataset");
    expect(screen.getByLabelText("Table schema")).toHaveValue("dbo");
    // Restored values came from GET, i.e. what the backend persisted.
    expect(backend.settings).toMatchObject({
      execution_type: "pipeline",
      pipeline_id: "pl-acelo",
      source_schema: "dbo",
      result_schema: "dbo",
    });
  });

  it("confirms with the persisted values, including the pipeline name", async () => {
    backend.env = READY_ENV();
    backend.discovered = true;
    const user = userEvent.setup();
    renderSettings();
    await fillAndSave(user);

    const saved = await screen.findByTestId("settings-saved");
    expect(saved).toHaveTextContent("Cluster settings saved");
    expect(saved).toHaveTextContent("Execution type:Pipeline");
    expect(saved).toHaveTextContent("Pipeline:ACELO_Cluster_Optimization_Pipeline");
    expect(saved).toHaveTextContent("Source:realistic_cluster_dataset");
    expect(saved).toHaveTextContent("Result:acelo_cluster_recommendations");
    expect(saved).toHaveTextContent("Lakehouse:Data");
    // Unset optional values read "Not configured" — never a sample or 0.
    expect(saved).toHaveTextContent("SQL analytics endpoint:Not configured");
    expect(saved).not.toHaveTextContent("xxxx");
  });

  it("a chosen pipeline is what gets persisted", async () => {
    backend.env = READY_ENV();
    backend.discovered = true;
    const user = userEvent.setup();
    renderSettings();
    await user.selectOptions(await screen.findByLabelText("Execution type"), "pipeline");
    await user.selectOptions(screen.getByLabelText("Pipeline"), "pl-other");
    await user.click(screen.getByRole("button", { name: /save cluster settings/i }));
    await waitFor(() =>
      expect(api.saveClusterSettings).toHaveBeenCalledWith(
        "env-1",
        expect.objectContaining({ execution_type: "pipeline", pipeline_id: "pl-other" })
      )
    );
    expect(await screen.findByTestId("settings-saved")).toHaveTextContent("Pipeline:Customer_Own_Pipeline");
  });

  it("switching back to Notebook clears the pipeline selection", async () => {
    backend.env = READY_ENV();
    backend.discovered = true;
    backend.settings = { ...backend.settings, execution_type: "pipeline", pipeline_id: "pl-other" };
    const user = userEvent.setup();
    renderSettings();
    await waitFor(async () => expect(await screen.findByLabelText("Execution type")).toHaveValue("pipeline"));
    await user.selectOptions(screen.getByLabelText("Execution type"), "notebook");
    expect(screen.queryByLabelText("Pipeline")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /save cluster settings/i }));
    await waitFor(() =>
      expect(api.saveClusterSettings).toHaveBeenCalledWith(
        "env-1",
        expect.objectContaining({ execution_type: "notebook", pipeline_id: "" })
      )
    );
  });

  it("a failed save says so with the backend's error and shows no confirmation", async () => {
    backend.env = READY_ENV();
    backend.discovered = true;
    const { ApiError } = await import("../services/environmentApi");
    api.saveClusterSettings.mockRejectedValueOnce(
      new ApiError("sql_endpoint 'xxxx.datawarehouse.fabric.microsoft.com' is a placeholder, not a real value.", 422)
    );
    const user = userEvent.setup();
    renderSettings();
    await user.type(await screen.findByLabelText("SQL analytics endpoint"), "xxxx.datawarehouse.fabric.microsoft.com");
    await user.click(screen.getByRole("button", { name: /save cluster settings/i }));

    const failed = await screen.findByTestId("settings-save-failed");
    expect(failed).toHaveTextContent("Failed to save Cluster settings");
    expect(failed).toHaveTextContent("is a placeholder");
    expect(screen.queryByTestId("settings-saved")).not.toBeInTheDocument();
  });

  it("unset fields are empty with a 'not configured' hint, never a sample value", async () => {
    backend.env = READY_ENV();
    backend.discovered = true;
    renderSettings();
    const endpoint = await screen.findByLabelText("SQL analytics endpoint");
    expect(endpoint).toHaveValue("");
    expect(endpoint.getAttribute("placeholder")).toMatch(/^Optional — not configured/);
    expect(document.body.innerHTML).not.toContain("xxxx");
  });
});

describe("default Lakehouse binding (pipeline Spark context)", () => {
  const READY = () => {
    backend.env = { ...BASE_ENV, status: "environment_ready", workspace_name: "acelo demo", last_verified_at: "x", last_discovered_at: "y" };
    backend.discovered = true;
    backend.installed = true;
  };
  beforeEach(() => {
    backend.provisioningExtra = undefined;
  });

  it("warns when no default Lakehouse can be bound and points to the Lakehouse ID setting", async () => {
    READY();
    backend.provisioningExtra = { default_lakehouse: null };
    renderSettings();
    const warning = await screen.findByTestId("no-lakehouse-warning");
    expect(warning).toHaveTextContent("No default context found");
    expect(warning).toHaveTextContent("Lakehouse ID");
  });

  it("shows the binding as verified only when Fabric reported it back", async () => {
    READY();
    backend.provisioningExtra = {
      default_lakehouse: { id: "lh-1", name: "Data", workspace_id: "ws-1", source: "configured" },
      lakehouse_binding: { status: "VERIFIED", bound_id: "lh-1", bound_name: "Data" },
    };
    renderSettings();
    const block = await screen.findByTestId("default-lakehouse");
    expect(block).toHaveTextContent("Default Lakehouse for the notebook: Data");
    expect(block).toHaveTextContent("Verified as the notebook's default Lakehouse in Fabric");
  });

  it("a deployed notebook without the binding is flagged, not shown as fine", async () => {
    READY();
    backend.provisioningExtra = {
      default_lakehouse: { id: "lh-1", name: "Data", workspace_id: "ws-1" },
      lakehouse_binding: { status: "MISSING", bound_id: null },
    };
    renderSettings();
    expect(await screen.findByTestId("lakehouse-binding-problem")).toHaveTextContent(
      "The deployed notebook has NO default Lakehouse"
    );
  });

  it("persists the Lakehouse ID and its workspace", async () => {
    READY();
    const user = userEvent.setup();
    renderSettings();
    await user.type(await screen.findByLabelText("Lakehouse ID"), "1b1b1b1b-2222-3333-4444-555555555555");
    await user.type(screen.getByLabelText("Lakehouse workspace ID"), "2c2c2c2c-3333-4444-5555-666666666666");
    await user.click(screen.getByRole("button", { name: /save cluster settings/i }));
    await waitFor(() =>
      expect(api.saveClusterSettings).toHaveBeenCalledWith(
        "env-1",
        expect.objectContaining({
          lakehouse_id: "1b1b1b1b-2222-3333-4444-555555555555",
          lakehouse_workspace_id: "2c2c2c2c-3333-4444-5555-666666666666",
        })
      )
    );
  });
});
