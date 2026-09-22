import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const msal = {
  instance: {
    getActiveAccount: () => ({ name: "Hema", username: "hema@contoso.com" }),
    getAllAccounts: () => [],
  },
};
vi.mock("@azure/msal-react", () => ({ useMsal: () => msal }));
vi.mock("../services/fabricAuth", async () => {
  const actual = await vi.importActual<typeof import("../services/fabricAuth")>("../services/fabricAuth");
  return {
    ...actual,
    getSilentRunTokens: vi.fn(async () => ({ fabric: "FABRIC", onelake: "ONELAKE" })),
    getFabricToken: vi.fn(async () => "FABRIC"),
    getOneLakeToken: vi.fn(async () => "ONELAKE"),
  };
});
vi.mock("../services/runsApi", async () => {
  const actual = await vi.importActual<typeof import("../services/runsApi")>("../services/runsApi");
  return {
    ...actual,
    getActiveRuns: vi.fn(),
    listNotifications: vi.fn(),
    markNotificationRead: vi.fn(async () => ({})),
    markAllNotificationsRead: vi.fn(async () => ({ ok: true })),
    listRuns: vi.fn(),
    getRun: vi.fn(),
    getRunEvents: vi.fn(),
    cancelRun: vi.fn(),
    rerun: vi.fn(),
  };
});
vi.mock("../services/executionApi", async () => {
  const actual = await vi.importActual<typeof import("../services/executionApi")>("../services/executionApi");
  return { ...actual, cancelJob: vi.fn(), getJob: vi.fn(), startAnalysis: vi.fn() };
});

import * as runs from "../services/runsApi";
import * as exec from "../services/executionApi";
import { RunMonitorProvider } from "./RunMonitor";
import Topbar from "./Topbar";
import History from "../pages/History";
import RunDetails from "../pages/RunDetails";

const api = runs as unknown as Record<string, ReturnType<typeof vi.fn>>;

const RUN_ID = "64f3cb01-4935-45c7-9f5c-ed27611b40db";
const FABRIC_RUN_ID = "13d76c9c-0e29-4265-abd2-5d2204b8221b";

function summary(overrides: Partial<runs.RunSummary> = {}): runs.RunSummary {
  return {
    acelo_run_id: RUN_ID,
    job_id: "job-1",
    optimization: "Cluster Optimization",
    domain: "cluster",
    platform: "fabric",
    environment_id: "env-1",
    environment_name: "Fabric Prod",
    execution_type: "pipeline",
    status: "RUNNING",
    platform_status: "RUNNING",
    current_stage: "Cluster notebook running",
    started_at: "2026-09-22T10:00:00",
    completed_at: null,
    created_at: "2026-09-22T10:00:00",
    duration_seconds: null,
    platform_run_id: FABRIC_RUN_ID,
    resource_id: "pipe-1",
    created_by: "Hema",
    retry_of_run_id: null,
    error_code: null,
    error_message: null,
    request: "Analyze my clusters",
    ...overrides,
  };
}

function note(overrides: Partial<runs.AppNotification> = {}): runs.AppNotification {
  return {
    id: "n-1",
    type: "run_succeeded",
    title: "Cluster Optimization completed",
    body: "Your cluster optimization run has completed.",
    link: `/runs/${RUN_ID}`,
    read: false,
    created_at: "2026-09-22T10:05:00",
    acelo_run_id: RUN_ID,
    ...overrides,
  };
}

function Page({ name }: { name: string }) {
  return (
    <div>
      <Topbar pageName={name} onMenuClick={() => undefined} />
      <p>{name} page</p>
    </div>
  );
}

function renderShell(path = "/approvals") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <RunMonitorProvider>
        <Routes>
          <Route path="/approvals" element={<Page name="Approvals" />} />
          <Route path="/settings" element={<Page name="Settings" />} />
          <Route path="/runs/:id" element={<p>run details for route</p>} />
        </Routes>
      </RunMonitorProvider>
    </MemoryRouter>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  api.getActiveRuns.mockResolvedValue({ count: 0, runs: [] });
  api.listNotifications.mockResolvedValue({ unread: 0, notifications: [] });
});
afterEach(() => vi.useRealTimers());

describe("global run monitor", () => {
  it("tracks several active runs by id in the header", async () => {
    api.getActiveRuns.mockResolvedValue({
      count: 2,
      runs: [summary(), summary({ acelo_run_id: "run-2", optimization: "Query Optimization", status: "SUBMITTED" })],
    });
    renderShell();
    expect(await screen.findByRole("button", { name: "2 active runs" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "2 active runs" }));
    expect(screen.getByText(RUN_ID)).toBeInTheDocument();
    expect(screen.getByText("run-2")).toBeInTheDocument();
    // Polls with silent tokens so the backend can advance delegated runs.
    expect(api.getActiveRuns).toHaveBeenCalledWith({ fabric: "FABRIC", onelake: "ONELAKE" });
  });

  it("shows a completion toast on a different page, with View Results", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderShell("/settings");
    await waitFor(() => expect(api.listNotifications).toHaveBeenCalledTimes(1));
    api.listNotifications.mockResolvedValue({ unread: 1, notifications: [note()] });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(21000);
    });
    expect(await screen.findByText("Cluster Optimization completed", { selector: "p" })).toBeInTheDocument();
    expect(screen.getByText("Settings page")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "View Results" }));
    expect(await screen.findByText("run details for route")).toBeInTheDocument();
    expect(api.markNotificationRead).toHaveBeenCalledWith("n-1");
  });

  it("shows a failure toast with View Details", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderShell();
    await waitFor(() => expect(api.listNotifications).toHaveBeenCalled());
    api.listNotifications.mockResolvedValue({
      unread: 1,
      notifications: [note({ id: "n-2", type: "run_failed", title: "Cluster Optimization failed", body: "Open execution details to view the error." })],
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(21000);
    });
    expect(await screen.findByRole("button", { name: "View Details" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "View Results" })).not.toBeInTheDocument();
  });

  it("after a refresh, existing notifications are in the bell (no toast burst)", async () => {
    api.listNotifications.mockResolvedValue({ unread: 1, notifications: [note()] });
    renderShell();
    expect(await screen.findByRole("button", { name: "Notifications (1 unread)" })).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Notifications (1 unread)" }));
    expect(screen.getByText("Cluster Optimization completed")).toBeInTheDocument();
  });

  it("never requests browser notification permission on load; only on explicit opt-in", async () => {
    const requestPermission = vi.fn(async () => "granted" as NotificationPermission);
    const NotificationStub = Object.assign(vi.fn(), { permission: "default", requestPermission });
    vi.stubGlobal("Notification", NotificationStub);
    try {
      renderShell();
      await waitFor(() => expect(api.listNotifications).toHaveBeenCalled());
      expect(requestPermission).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole("button", { name: "Notifications" }));
      fireEvent.click(screen.getByRole("button", { name: "Enable browser notifications" }));
      await waitFor(() => expect(requestPermission).toHaveBeenCalledTimes(1));
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe("run history", () => {
  it("lists runs and sends filters and search to the backend", async () => {
    api.listRuns.mockResolvedValue({ total: 1, runs: [summary({ status: "SUCCEEDED", duration_seconds: 125 })] });
    render(
      <MemoryRouter>
        <History />
      </MemoryRouter>
    );
    expect(await screen.findByText(FABRIC_RUN_ID)).toBeInTheDocument();
    expect(screen.getByText("2m 5s")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "FAILED" } });
    fireEvent.change(screen.getByLabelText("Optimization"), { target: { value: "cluster" } });
    fireEvent.change(screen.getByLabelText("Platform"), { target: { value: "fabric" } });
    fireEvent.change(screen.getByLabelText("Search runs"), { target: { value: FABRIC_RUN_ID } });
    await waitFor(() =>
      expect(api.listRuns).toHaveBeenLastCalledWith(
        expect.objectContaining({ status: "FAILED", domain: "cluster", platform: "fabric", search: FABRIC_RUN_ID })
      )
    );
  });

  it("shows the backend's error instead of an empty table", async () => {
    const { ApiError } = await import("../services/environmentApi");
    api.listRuns.mockRejectedValue(new ApiError("Could not reach the ACELO backend.", 0));
    render(
      <MemoryRouter>
        <History />
      </MemoryRouter>
    );
    expect(await screen.findByText("Could not reach the ACELO backend.")).toBeInTheDocument();
  });
});

function details(overrides: Partial<runs.RunDetails> = {}): runs.RunDetails {
  return {
    ...summary(),
    parameters: { acelo_run_id: RUN_ID, result_table: "cluster_results" },
    timeline: [],
    logs: [
      { id: "e1", acelo_run_id: RUN_ID, timestamp: "2026-09-22T10:00:01", level: "SUCCESS", stage: "submission",
        event_type: "PLATFORM_RUN_ID_RECEIVED", message: `Pipeline submitted — fabric run ID ${FABRIC_RUN_ID}.`,
        platform: "fabric", platform_run_id: FABRIC_RUN_ID, metadata: {} },
    ],
    result: null,
    reruns: [],
    can_cancel: true,
    can_rerun: false,
    ...overrides,
  };
}

function renderDetails() {
  return render(
    <MemoryRouter initialEntries={[`/runs/${RUN_ID}`]}>
      <Routes>
        <Route path="/runs/:id" element={<RunDetails />} />
      </Routes>
    </MemoryRouter>
  );
}

describe("run details", () => {
  it("shows overview, platform execution, parameters, timeline and logs", async () => {
    api.getRun.mockResolvedValue(details());
    api.getRunEvents.mockResolvedValue({ status: "RUNNING", current_stage: "Cluster notebook running", events: [] });
    renderDetails();
    expect(await screen.findByText("Cluster notebook running")).toBeInTheDocument();
    expect(screen.getAllByText(FABRIC_RUN_ID).length).toBeGreaterThan(0);
    expect(screen.getByText("cluster_results")).toBeInTheDocument();
    expect(screen.getByRole("log")).toHaveTextContent("Pipeline submitted");
    expect(screen.getByLabelText("Run timeline")).toHaveTextContent("PLATFORM_RUN_ID_RECEIVED");
  });

  it("cancel goes to the platform and shows Cancelling until the platform confirms", async () => {
    api.getRun.mockResolvedValueOnce(details());
    api.getRun.mockResolvedValue(details({ status: "CANCEL_REQUESTED", can_cancel: false }));
    api.getRunEvents.mockResolvedValue({ status: "CANCEL_REQUESTED", current_stage: null, events: [] });
    api.cancelRun.mockResolvedValue(summary({ status: "CANCEL_REQUESTED" }));
    renderDetails();
    fireEvent.click(await screen.findByRole("button", { name: "Cancel run" }));
    await waitFor(() => expect(api.cancelRun).toHaveBeenCalledWith(RUN_ID, expect.objectContaining({ fabric: "FABRIC" })));
    expect(await screen.findByText(/shown as cancelled only once the platform confirms/)).toBeInTheDocument();
    expect(screen.getByText("Cancelling")).toBeInTheDocument();
  });

  it("re-run creates a new ACELO run and opens it", async () => {
    api.getRun.mockResolvedValue(details({ status: "FAILED", can_cancel: false, can_rerun: true, error_message: "Notebook failed" }));
    api.rerun.mockResolvedValue(summary({ acelo_run_id: "new-run", retry_of_run_id: RUN_ID }));
    renderDetails();
    expect(await screen.findByText("Notebook failed")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Re-run" }));
    await waitFor(() => expect(api.rerun).toHaveBeenCalledWith(RUN_ID, expect.objectContaining({ userName: "Hema" })));
    await waitFor(() => expect(api.getRun).toHaveBeenCalledWith("new-run", expect.anything()));
  });

  it("shows retrieved results for a succeeded run", async () => {
    api.getRun.mockResolvedValue(
      details({ status: "SUCCEEDED", can_cancel: false, can_rerun: true,
        result: { rows: [{ cluster_name: "etl-heavy", optimization_label: "Risky" }], row_count: 1, table: "OneLake dbo.cluster_results" } })
    );
    renderDetails();
    expect(await screen.findByText("etl-heavy")).toBeInTheDocument();
    expect(screen.getByText(/from OneLake dbo.cluster_results/)).toBeInTheDocument();
  });
});

describe("leaving the agent", () => {
  it("unmounting the Agent never cancels the backend run", async () => {
    const { default: AgentWorkspace } = await import("./AgentWorkspace");
    window.sessionStorage.setItem("acelo.agent.lastJobId", "job-1");
    (exec.getJob as ReturnType<typeof vi.fn>).mockResolvedValue({
      id: "job-1", platform: "fabric", status: "RUNNING", request: "x", intent: "cluster",
      job_runs: [{ id: RUN_ID, domain: "cluster", status: "RUNNING", platform_run_id: FABRIC_RUN_ID }],
    });
    const view = render(
      <MemoryRouter>
        <AgentWorkspace />
      </MemoryRouter>
    );
    // Returning to the Agent restores the run from the backend.
    expect(await screen.findByText(FABRIC_RUN_ID)).toBeInTheDocument();
    view.unmount();
    expect(exec.cancelJob).not.toHaveBeenCalled();
    expect(api.cancelRun).not.toHaveBeenCalled();
  });
});
