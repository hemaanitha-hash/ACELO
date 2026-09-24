import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Sidebar from "./Sidebar";
import PlatformRoute from "./PlatformRoute";
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
 * Navigation and routing follow the ACTIVE platform.
 *
 * The switcher used to be a hardcoded array in local state that nothing read.
 * It now lists the customer's real connections and switching it changes the
 * application's context — including which platform-specific pages exist.
 */

const CONNECTIONS = [
  { id: "db-prod", platform: "databricks", workspace: "Production Lakehouse", status: "connected" },
  { id: "fabric-prod", platform: "fabric", workspace: "Fabric Workspace", status: "connected" },
];

const DATABRICKS: PlatformConnection = {
  id: "db-prod",
  platform: "databricks",
  name: "Production Lakehouse",
  status: "connected",
};
const FABRIC: PlatformConnection = {
  id: "fabric-prod",
  platform: "fabric",
  name: "Fabric Workspace",
  status: "connected",
};

beforeEach(() => {
  resetActiveContext();
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => CONNECTIONS,
      text: async () => JSON.stringify(CONNECTIONS),
    }),
  );
});

afterEach(() => {
  vi.restoreAllMocks();
});

function renderSidebar() {
  return render(
    <MemoryRouter>
      <Sidebar open onClose={() => {}} />
    </MemoryRouter>,
  );
}

describe("platform-scoped navigation", () => {
  it("lists the customer's real connections, not a hardcoded pair", async () => {
    renderSidebar();

    // Connections load asynchronously; opening the dropdown before they arrive
    // would assert against an empty list. Wait for the switcher to show one.
    const switcher = await screen.findByRole("button", { name: /Production Lakehouse/ });
    await userEvent.click(switcher);

    // Scoped to the dropdown: the active connection's name also appears on the
    // switcher button itself, which is not what this test is about.
    const options = await screen.findAllByRole("option");
    const names = options.map((o) => o.textContent ?? "");
    expect(names.some((n) => n.includes("Production Lakehouse"))).toBe(true);
    expect(names.some((n) => n.includes("Fabric Workspace"))).toBe(true);
    expect(options).toHaveLength(2);
  });

  it("shows the Databricks-only page while Databricks is active", async () => {
    setActiveContext(DATABRICKS);
    renderSidebar();

    expect(await screen.findByRole("link", { name: /Compute discovery/ })).toBeInTheDocument();
  });

  it("hides the Databricks-only page while Fabric is active", async () => {
    setActiveContext(FABRIC);
    renderSidebar();

    await screen.findByRole("link", { name: /Overview/ });
    expect(screen.queryByRole("link", { name: /Compute discovery/ })).not.toBeInTheDocument();
  });

  it("does not show a second link to Environment Setup", async () => {
    setActiveContext(FABRIC);
    renderSidebar();

    // "Fabric resources" pointed at /settings, which the Settings link below
    // already reaches — one destination must not appear twice.
    const settingsLinks = (await screen.findAllByRole("link")).filter(
      (l) => l.getAttribute("href") === "/settings",
    );
    expect(settingsLinks).toHaveLength(1);
  });

  it("never shows an invented pending-approvals count", async () => {
    setActiveContext(DATABRICKS);
    renderSidebar();

    const approvals = await screen.findByRole("link", { name: /Approvals/ });
    // The badge was hardcoded to 3 in every workspace.
    expect(approvals.textContent).not.toMatch(/\d/);
  });

  it("keeps the shared pages available on both platforms", async () => {
    setActiveContext(FABRIC);
    renderSidebar();

    for (const label of ["Overview", "AI Agent", "Optimizations", "Approvals", "Run History"]) {
      expect(await screen.findByRole("link", { name: new RegExp(label) })).toBeInTheDocument();
    }
  });

  it("switching the selector changes the global context", async () => {
    setActiveContext(FABRIC);
    renderSidebar();

    await userEvent.click(await screen.findByRole("button", { name: /Microsoft Fabric/ }));
    await userEvent.click(await screen.findByRole("option", { name: /Production Lakehouse/ }));

    await waitFor(() => expect(getActiveContext().platform).toBe("databricks"));
    expect(getActiveContext().connection?.id).toBe("db-prod");
    // The Databricks-only page becomes reachable after the switch.
    expect(await screen.findByRole("link", { name: /Compute discovery/ })).toBeInTheDocument();
  });
});

describe("platform-scoped routing", () => {
  function renderGuarded() {
    return render(
      <MemoryRouter>
        <PlatformRoute platform="databricks">
          <p>Databricks discovery</p>
        </PlatformRoute>
      </MemoryRouter>,
    );
  }

  it("renders a Databricks page while Databricks is active", () => {
    setActiveContext(DATABRICKS);
    renderGuarded();

    expect(screen.getByText("Databricks discovery")).toBeInTheDocument();
  });

  it("does not mount a Databricks page while Fabric is active", () => {
    setActiveContext(FABRIC);
    renderGuarded();

    // Not merely hidden — never mounted, so it never issues its request.
    expect(screen.queryByText("Databricks discovery")).not.toBeInTheDocument();
  });
});
