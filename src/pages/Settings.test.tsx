/**
 * Step 5A — Settings authentication-mode rendering.
 *
 * Verifies that Personal mode hides the Service Principal credential fields and
 * offers Microsoft sign-in, that Service Principal mode is unchanged, and that
 * no token ever reaches the rendered DOM.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
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
  getActiveAccount: vi.fn(() => null as typeof ACCOUNT | null),
  getAllAccounts: vi.fn(() => [] as (typeof ACCOUNT)[]),
  setActiveAccount: vi.fn(),
  loginPopup: vi.fn(async () => ({ account: ACCOUNT })),
  acquireTokenSilent: vi.fn(async () => ({ accessToken: "SECRET-DELEGATED-TOKEN" })),
  acquireTokenPopup: vi.fn(),
  logoutPopup: vi.fn(async () => undefined),
};

// `accounts` is the reactive channel the component reads; `mockAccounts` lets a
// test simulate MSAL resolving a sign-in after mount (the redirect-callback case).
let mockAccounts: (typeof ACCOUNT)[] = [];
let mockEnvironments: unknown[] = [];
let mockProvisioning: Record<string, unknown> | null = null;

/** Provisioning payload shaped like the backend's get_provisioning_state(). */
function provisioningState(
  status: string,
  domains: Record<string, Partial<{ ready: boolean; deployed: boolean; asset_available: boolean; error_code: string }>>
) {
  const domain = (d: Partial<{ ready: boolean; deployed: boolean; asset_available: boolean; error_code: string }>) => ({
    asset_available: d.asset_available ?? true,
    deployed: d.deployed ?? false,
    display_name: "ACELO",
    platform_resource_id: d.deployed ? "real-item-1" : null,
    ready: d.ready ?? false,
    error_code: d.error_code ?? null,
    message: null,
  });
  return {
    status,
    step: null,
    package_name: "ACELO Optimization Package",
    package_version_available: "1.0.0",
    package_version_installed: status === "INSTALLED" ? "1.0.0" : null,
    deployable: true,
    namespace: "ACELO",
    last_provisioned_at: null,
    domains: {
      cluster: domain(domains.cluster ?? {}),
      query: domain(domains.query ?? {}),
      storage: domain(domains.storage ?? {}),
    },
    error_code: status === "FAILED" ? "PROVISIONING_FAILED" : null,
    message: null,
    missing_assets: Object.entries(domains)
      .filter(([, d]) => d.asset_available === false)
      .map(([k]) => k),
  };
}

vi.mock("@azure/msal-react", () => ({
  useMsal: () => ({ instance: msalInstance, accounts: mockAccounts, inProgress: "none" }),
  MsalProvider: ({ children }: { children: React.ReactNode }) => children,
}));

// An App Registration is configured in this deployment.
vi.mock("../authConfig", async () => {
  const actual = await vi.importActual<typeof import("../authConfig")>("../authConfig");
  return { ...actual, isMsalConfigured: () => true, MSAL_CLIENT_ID: "test-client-id" };
});

// The environment list request must not hit the network.
vi.mock("../services/environmentApi", async () => {
  const actual = await vi.importActual<typeof import("../services/environmentApi")>(
    "../services/environmentApi"
  );
  return {
    ...actual,
    listEnvironments: vi.fn(async () => mockEnvironments),
    getProvisioningStatus: vi.fn(async () => {
      if (!mockProvisioning) throw new Error("not provisioned");
      return mockProvisioning;
    }),
    testEnvironment: vi.fn(async () => ({
      platform: "fabric",
      connected: true,
      workspace_id: "ws-1",
      workspace_name: "Production Workspace",
      message: "Fabric connection verified",
      last_verified_at: "2026-09-21T10:00:00Z",
    })),
    updateEnvironment: vi.fn(async () => mockEnvironments[0]),
    createEnvironment: vi.fn(async () => mockEnvironments[0]),
    discoverEnvironment: vi.fn(async () => ({
      discovered: true,
      workspace: { id: "ws-1", name: "Production Workspace" },
      items: [],
      counts: { Notebook: 3 },
    })),
  };
});

import Settings from "./Settings";

function renderSettings() {
  return render(
    <MemoryRouter>
      <Settings />
    </MemoryRouter>
  );
}

async function selectUserMode(user: ReturnType<typeof userEvent.setup>) {
  const radios = await screen.findAllByRole("radio");
  // First radio is the Microsoft Account (delegated user) option.
  await user.click(radios[0]);
}

beforeEach(() => {
  vi.clearAllMocks();
  mockAccounts = [];
  mockEnvironments = [];
  mockProvisioning = null;
  msalInstance.getActiveAccount.mockReturnValue(null);
  msalInstance.getAllAccounts.mockReturnValue([]);
});

describe("Fabric authentication mode", () => {
  it("offers both authentication methods for Fabric", async () => {
    renderSettings();
    expect(await screen.findByText("Authentication Method")).toBeInTheDocument();
    expect(screen.getByText("Microsoft Account")).toBeInTheDocument();
    expect(screen.getByText("Service Principal")).toBeInTheDocument();
  });

  it("defaults to Service Principal and preserves its existing fields", async () => {
    renderSettings();
    expect(await screen.findByText("Tenant ID")).toBeInTheDocument();
    expect(screen.getByText("Client ID")).toBeInTheDocument();
    expect(screen.getByText("Client Secret")).toBeInTheDocument();
    expect(screen.getByText("Workspace ID")).toBeInTheDocument();
    expect(screen.getByText("Environment name")).toBeInTheDocument();
  });

  it("hides Tenant ID, Client ID and Client Secret in Microsoft Account mode", async () => {
    const user = userEvent.setup();
    renderSettings();
    await selectUserMode(user);

    await waitFor(() => expect(screen.queryByText("Client Secret")).not.toBeInTheDocument());
    expect(screen.queryByText("Tenant ID")).not.toBeInTheDocument();
    expect(screen.queryByText("Client ID")).not.toBeInTheDocument();
    // ...but the fields Personal mode does need remain.
    expect(screen.getByText("Environment name")).toBeInTheDocument();
    expect(screen.getByText("Workspace ID")).toBeInTheDocument();
  });

  it("shows the Microsoft sign-in action in Microsoft Account mode", async () => {
    const user = userEvent.setup();
    renderSettings();
    await selectUserMode(user);

    expect(
      await screen.findByRole("button", { name: /sign in with microsoft/i })
    ).toBeInTheDocument();
  });

  it("shows the signed-in account after a successful sign-in", async () => {
    const user = userEvent.setup();
    renderSettings();
    await selectUserMode(user);

    await user.click(await screen.findByRole("button", { name: /sign in with microsoft/i }));

    expect(await screen.findByText("Signed in as:")).toBeInTheDocument();
    expect(screen.getByText("Test User (user@contoso.com)")).toBeInTheDocument();
    expect(msalInstance.loginPopup).toHaveBeenCalled();
  });

  it("reports a cancelled sign-in without claiming a connection", async () => {
    msalInstance.loginPopup.mockRejectedValueOnce({ errorCode: "user_cancelled" });
    const user = userEvent.setup();
    renderSettings();
    await selectUserMode(user);

    await user.click(await screen.findByRole("button", { name: /sign in with microsoft/i }));

    expect(await screen.findByText(/sign-in was cancelled/i)).toBeInTheDocument();
    expect(screen.queryByText("Signed in as:")).not.toBeInTheDocument();
    // Connection status must remain Not Connected.
    expect(screen.getByText("Not Connected")).toBeInTheDocument();
  });

  it("never renders the delegated token into the DOM", async () => {
    const user = userEvent.setup();
    const { container } = renderSettings();
    await selectUserMode(user);
    await user.click(await screen.findByRole("button", { name: /sign in with microsoft/i }));
    await screen.findByText("Signed in as:");

    expect(container.innerHTML).not.toContain("SECRET-DELEGATED-TOKEN");
    expect(container.innerHTML).not.toContain("Bearer ");
  });

  it("switching back to Service Principal restores the credential fields", async () => {
    const user = userEvent.setup();
    renderSettings();
    await selectUserMode(user);
    await waitFor(() => expect(screen.queryByText("Client Secret")).not.toBeInTheDocument());

    const radios = screen.getAllByRole("radio");
    await user.click(radios[1]);

    expect(await screen.findByText("Client Secret")).toBeInTheDocument();
    expect(screen.getByText("Tenant ID")).toBeInTheDocument();
    expect(screen.getByText("Client ID")).toBeInTheDocument();
  });

  it("shows the account when MSAL resolved it during the redirect callback", async () => {
    // Simulates handleRedirectPromise() having signed the user in before mount.
    // The old one-shot useState initializer missed exactly this case.
    mockAccounts = [ACCOUNT];
    msalInstance.getActiveAccount.mockReturnValue(ACCOUNT);

    const user = userEvent.setup();
    renderSettings();
    await selectUserMode(user);

    expect(await screen.findByText("Signed in as:")).toBeInTheDocument();
    expect(screen.getByText("Test User (user@contoso.com)")).toBeInTheDocument();
    // The user must NOT be asked to sign in again.
    expect(
      screen.queryByRole("button", { name: /sign in with microsoft/i })
    ).not.toBeInTheDocument();
  });

  it("enables Test Connection once signed in and a workspace ID is entered", async () => {
    mockAccounts = [ACCOUNT];
    msalInstance.getActiveAccount.mockReturnValue(ACCOUNT);

    const user = userEvent.setup();
    renderSettings();
    await selectUserMode(user);
    await screen.findByText("Signed in as:");

    const testButton = screen.getByRole("button", { name: /^test connection$/i });
    expect(testButton).toBeDisabled();

    await user.type(screen.getByPlaceholderText("Fabric Production"), "Fabric Prod");
    await user.type(
      screen.getByPlaceholderText("Fabric workspace ID"),
      "86f4fe0c-b270-46b1-9731-76f1a993e083"
    );

    await waitFor(() => expect(testButton).toBeEnabled());
  });

  it("does not leave a permanent Signing in... state after cancellation", async () => {
    msalInstance.loginPopup.mockRejectedValueOnce({ errorCode: "user_cancelled" });
    const user = userEvent.setup();
    renderSettings();
    await selectUserMode(user);

    await user.click(await screen.findByRole("button", { name: /sign in with microsoft/i }));

    // Resolves back to an actionable button, not a stuck spinner.
    expect(
      await screen.findByRole("button", { name: /sign in with microsoft/i })
    ).toBeEnabled();
    expect(screen.queryByText(/signing in/i)).not.toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// Readiness must reflect the real provisioning state
// --------------------------------------------------------------------------

describe("Readiness status", () => {
  const ENVIRONMENT = {
    id: "env-1",
    customer_id: "cust-1",
    name: "Fabric Production",
    platform: "fabric",
    auth_mode: "user",
    tenant_id: "t",
    workspace_id: "ws-1",
    workspace_name: "Production Workspace",
    status: "connected",
    last_error_code: null,
    last_error_message: null,
    last_verified_at: "2026-09-21T10:00:00Z",
    last_discovered_at: "2026-09-21T10:01:00Z",
    created_at: "2026-09-21T09:00:00Z",
    updated_at: "2026-09-21T10:01:00Z",
  };

  /** Signs in, tests the connection and discovers — i.e. steps 1-3 all pass. */
  async function connectAndDiscover(user: ReturnType<typeof userEvent.setup>) {
    mockEnvironments = [ENVIRONMENT];
    mockAccounts = [ACCOUNT];
    msalInstance.getActiveAccount.mockReturnValue(ACCOUNT);

    renderSettings();
    await selectUserMode(user);
    await screen.findByText("Signed in as:");

    await user.click(screen.getByRole("button", { name: /^test connection$/i }));
    await screen.findByText(/^Connected$/);

    await user.click(screen.getByRole("button", { name: /^discover environment$/i }));
    await screen.findByText(/workspace connected/i);
  }

  it("REGRESSION: connection + discovery pass but provisioning FAILED => NOT READY", async () => {
    // The reported bug: all three early checks green, folder creation rejected
    // with InsufficientScopes, yet the UI announced READY FOR ANALYSIS.
    mockProvisioning = provisioningState("FAILED", {
      cluster: { deployed: false, ready: false, error_code: "PERMISSION_DENIED" },
      query: { asset_available: false },
      storage: { asset_available: false },
    });

    const user = userEvent.setup();
    await connectAndDiscover(user);

    expect(await screen.findByTestId("overall-readiness")).toHaveTextContent("NOT READY");
    expect(screen.queryByText("READY FOR ANALYSIS")).not.toBeInTheDocument();
  });

  it("provisioning INSTALLED with Cluster ready => READY FOR ANALYSIS", async () => {
    mockProvisioning = provisioningState("INSTALLED", {
      cluster: { deployed: true, ready: true },
      query: { asset_available: false },
      storage: { asset_available: false },
    });

    const user = userEvent.setup();
    await connectAndDiscover(user);

    expect(await screen.findByText("READY FOR ANALYSIS")).toBeInTheDocument();
  });

  it("INSTALLED but no Cluster deployed => NOT READY (nothing runnable)", async () => {
    mockProvisioning = provisioningState("INSTALLED", {
      cluster: { deployed: false, ready: false },
      query: { asset_available: false },
      storage: { asset_available: false },
    });

    const user = userEvent.setup();
    await connectAndDiscover(user);

    expect(await screen.findByTestId("overall-readiness")).toHaveTextContent("NOT READY");
  });

  it("missing Query/Storage assets are not shown as ready", async () => {
    mockProvisioning = provisioningState("INSTALLED", {
      cluster: { deployed: true, ready: true },
      query: { asset_available: false },
      storage: { asset_available: false },
    });

    const user = userEvent.setup();
    await connectAndDiscover(user);
    await screen.findByText("READY FOR ANALYSIS");

    // Cluster alone is runnable; Query and Storage must not claim readiness.
    const rows = screen.getAllByRole("listitem");
    const rowFor = (label: string) =>
      rows.find((li) => li.textContent?.startsWith(label));

    expect(rowFor("Query")?.querySelector("svg")).toBeNull();
    expect(rowFor("Storage")?.querySelector("svg")).toBeNull();
    expect(rowFor("Cluster")?.querySelector("svg")).not.toBeNull();
  });
});
