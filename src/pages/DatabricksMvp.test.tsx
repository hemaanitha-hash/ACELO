import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Sidebar from "../components/Sidebar";
import Settings from "./Settings";
import App from "../App";
import { setRuntime } from "../services/runtime";
import { resetActiveContext } from "../services/platformContext";

/**
 * The Databricks-only MVP.
 *
 * ACELO ships as a native Databricks App: one platform, one workspace, no
 * credentials to enter. The old multi-platform connection UI must be ABSENT
 * from this experience, not merely disabled — a hidden-but-present field is
 * still a field a user can be sent to.
 *
 * These tests use the real `experience` module (Databricks-only by default),
 * unlike the legacy suites which opt out of it explicitly.
 */

vi.mock("@azure/msal-react", () => ({
  useMsal: () => ({
    instance: { getActiveAccount: () => null, getAllAccounts: () => [], setActiveAccount: () => {} },
    accounts: [],
    inProgress: "none",
  }),
}));

vi.mock("../services/environmentApi", async () => {
  const actual = await vi.importActual<typeof import("../services/environmentApi")>(
    "../services/environmentApi"
  );
  return {
    ...actual,
    listEnvironments: vi.fn(async () => []),
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
  listPlatformConnections: vi.fn(async () => []),
}));

const WORKSPACE = "https://adb-7405617514546966.6.azuredatabricks.net";

beforeEach(() => {
  resetActiveContext();
  setRuntime({
    databricks_app: true,
    workspace_host: WORKSPACE,
    auth_mode: "databricks_app_identity",
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Databricks-only navigation", () => {
  function renderSidebar() {
    return render(
      <MemoryRouter>
        <Sidebar open onClose={() => {}} />
      </MemoryRouter>,
    );
  }

  it("shows only the MVP journey", async () => {
    renderSidebar();

    for (const label of ["Overview", "AI Agent", "Compute Optimization", "Recommendations"]) {
      expect(await screen.findByRole("link", { name: new RegExp(label) })).toBeInTheDocument();
    }
  });

  it("keeps non-MVP pages out of the primary journey", async () => {
    renderSidebar();
    await screen.findByRole("link", { name: /Overview/ });

    // Still routable and still implemented — just not in the MVP navigation.
    for (const label of ["Approvals", "Execution", "Run History", "Optimizations"]) {
      expect(screen.queryByRole("link", { name: new RegExp(`^${label}$`) })).not.toBeInTheDocument();
    }
  });

  it("states the Databricks workspace instead of offering a platform selector", async () => {
    renderSidebar();

    expect(await screen.findByTestId("workspace-context")).toHaveTextContent(
      "adb-7405617514546966.6.azuredatabricks.net",
    );
    expect(screen.getByText("Connected via Databricks App")).toBeInTheDocument();
    // No switcher: there is nothing to switch to.
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(screen.queryByText("Microsoft Fabric")).not.toBeInTheDocument();
  });
});

describe("Environment Setup is absent from the Databricks MVP", () => {
  it("offers no Settings link anywhere in the navigation", async () => {
    render(
      <MemoryRouter>
        <Sidebar open onClose={() => {}} />
      </MemoryRouter>,
    );
    await screen.findByRole("link", { name: /Overview/ });

    expect(screen.queryByRole("link", { name: /Settings/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Environment Setup/i })).not.toBeInTheDocument();
    // Nothing points the user at a page they cannot open.
    expect(document.body.textContent).not.toMatch(/Environment Setup/i);
  });

  it("does not route /settings — a typed URL returns to Overview", async () => {
    render(
      <MemoryRouter initialEntries={["/settings"]}>
        <App />
      </MemoryRouter>,
    );

    // The route is not registered, so the catch-all redirects to Overview.
    // Environment Setup must not render at all.
    await waitFor(() =>
      expect(screen.queryByText("Environment Setup")).not.toBeInTheDocument(),
    );
    expect(screen.queryByText("Test Connection")).not.toBeInTheDocument();
    expect(screen.queryByText("Workspace URL")).not.toBeInTheDocument();
    expect(screen.queryByText("Access Token")).not.toBeInTheDocument();
  });
});

describe("Databricks-only Environment Setup component", () => {
  function renderSettings() {
    return render(
      <MemoryRouter>
        <Settings />
      </MemoryRouter>,
    );
  }

  it("never asks for a workspace URL or access token", async () => {
    renderSettings();
    await screen.findByText("Configuring");

    expect(screen.queryByText("Workspace URL")).not.toBeInTheDocument();
    expect(screen.queryByText("Access Token")).not.toBeInTheDocument();
    expect(screen.queryByText("Client Secret")).not.toBeInTheDocument();
    expect(screen.queryByText("Tenant ID")).not.toBeInTheDocument();
  });

  it("removes Fabric and Add connection from the experience", async () => {
    renderSettings();
    await screen.findByText("Configuring");

    expect(screen.queryByRole("button", { name: /add connection/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Microsoft Fabric" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "File Analysis" })).not.toBeInTheDocument();
    expect(screen.queryByText("Authentication Method")).not.toBeInTheDocument();
  });

  it("states how ACELO is connected instead", async () => {
    renderSettings();

    // The workspace and the authentication mechanism are stated, not asked for.
    expect(await screen.findByText(WORKSPACE)).toBeInTheDocument();
    expect(screen.getByText(/no credential is stored/i)).toBeInTheDocument();
    expect(screen.getAllByText(/Databricks App/).length).toBeGreaterThan(0);
  });
});
