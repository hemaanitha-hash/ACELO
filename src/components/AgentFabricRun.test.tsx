/**
 * AI Agent -> Cluster on Fabric (no file attached).
 *
 * The UI must show only what the backend reports for the real platform run:
 * Starting -> Running -> Completed, the Fabric run id exactly as returned, and
 * failures as failures.
 */

import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const FABRIC_RUN_ID = "f2d65699-dd22-4889-980c-15226deb0e1b";

const { exec, fabricTokenMock, sqlTokenMock } = vi.hoisted(() => ({
  sqlTokenMock: vi.fn(async (_instance?: unknown): Promise<string | null> => "SQL-ENDPOINT-TOKEN"),
  fabricTokenMock: vi.fn(async (_instance?: unknown): Promise<string> => "DELEGATED-TOKEN"),
  exec: {
    resolveExecutionTarget: vi.fn(async () => ({ connectionId: "conn-1", delegated: true })),
    startAnalysis: vi.fn(),
    getJob: vi.fn(),
    getJobResults: vi.fn(async () => [] as unknown[]),
    cancelJob: vi.fn(),
    uploadClusterFile: vi.fn(),
  },
}));

vi.mock("@azure/msal-react", () => ({
  useMsal: () => ({
    instance: { getActiveAccount: () => ({ username: "u" }), getAllAccounts: () => [] },
    accounts: [],
    inProgress: "none",
  }),
}));

vi.mock("../services/fabricAuth", async () => {
  const actual = await vi.importActual<typeof import("../services/fabricAuth")>("../services/fabricAuth");
  return { ...actual, getFabricToken: fabricTokenMock, getSqlEndpointToken: sqlTokenMock };
});

vi.mock("../services/executionApi", async () => {
  const actual = await vi.importActual<typeof import("../services/executionApi")>(
    "../services/executionApi"
  );
  return { ...actual, ...exec };
});

import AgentWorkspace from "./AgentWorkspace";
import { ApiError } from "../services/environmentApi";

function job(status: string, extra: Record<string, unknown> = {}) {
  return {
    id: "job-1",
    connection_id: "conn-1",
    request: "Check my cluster utilization",
    intent: "cluster",
    platform: "fabric",
    status,
    error: null,
    job_runs: [
      {
        id: "acelo-run-1",
        domain: "cluster",
        platform: "fabric",
        platform_run_id: status === "FAILED_TO_START" ? null : FABRIC_RUN_ID,
        status: status === "FAILED_TO_START" ? "FAILED" : status,
        current_step: null,
        progress: null,
        error: null,
        error_code: null,
        result_reference: null,
        platform_resource_id: "nb-1",
        started_at: null,
        completed_at: null,
        ...extra,
      },
    ],
  };
}

function renderAgent() {
  return render(
    <MemoryRouter initialEntries={["/agent"]}>
      <Routes>
        <Route path="/agent" element={<AgentWorkspace />} />
        <Route path="/results" element={<p>RESULTS PAGE</p>} />
      </Routes>
    </MemoryRouter>
  );
}

async function submit(user: ReturnType<typeof userEvent.setup>) {
  await user.type(
    screen.getByPlaceholderText("Ask ACELO to analyze your platform..."),
    "Check my cluster utilization"
  );
  await user.click(screen.getByRole("button", { name: "Send" }));
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.clearAllMocks();
  exec.resolveExecutionTarget.mockResolvedValue({ connectionId: "conn-1", delegated: true });
  fabricTokenMock.mockResolvedValue("DELEGATED-TOKEN");
  exec.getJobResults.mockResolvedValue([]);
});

afterEach(() => vi.useRealTimers());

describe("AI Agent -> Fabric Cluster run", () => {
  it("submits the request to the Fabric connection with the delegated token, not the file path", async () => {
    exec.startAnalysis.mockResolvedValue(job("STARTING"));
    exec.getJob.mockResolvedValue(job("STARTING"));
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderAgent();
    await submit(user);

    expect(exec.startAnalysis).toHaveBeenCalledWith("Check my cluster utilization", "conn-1", "DELEGATED-TOKEN", expect.anything());
    expect(exec.uploadClusterFile).not.toHaveBeenCalled();
  });

  it("shows Starting -> Running -> Completed from backend polls, with the real Fabric run id", async () => {
    exec.startAnalysis.mockResolvedValue(job("STARTING"));
    exec.getJob
      .mockResolvedValueOnce(job("RUNNING"))
      .mockResolvedValueOnce(job("COMPLETED"));
    exec.getJobResults.mockResolvedValue([
      {
        job_run_id: "acelo-run-1",
        domain: "cluster",
        status: "COMPLETED",
        available: true,
        payload: { row_count: 12, parameter_verification: { status: "MATCHED" } },
        error: null,
        error_code: null,
      },
    ]);
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderAgent();
    await submit(user);

    expect(await screen.findByText("cluster — Starting")).toBeInTheDocument();
    expect(screen.getByTestId("platform-run-id")).toHaveTextContent(FABRIC_RUN_ID);
    expect(screen.getByText(/Fabric Run ID/)).toBeInTheDocument();

    await act(() => vi.advanceTimersByTimeAsync(3000));
    expect(await screen.findByText("cluster — Running")).toBeInTheDocument();

    await act(() => vi.advanceTimersByTimeAsync(3000));
    expect(await screen.findByText("cluster — Completed")).toBeInTheDocument();
    expect(await screen.findByText("12 result rows")).toBeInTheDocument();
    expect(screen.getByTestId("parameter-verification")).toHaveTextContent("MATCHED");

    await user.click(screen.getByRole("button", { name: "View Results" }));
    expect(await screen.findByText("RESULTS PAGE")).toBeInTheDocument();
  });

  it("a refused submission shows the platform error and no run id", async () => {
    exec.startAnalysis.mockResolvedValue(
      job("FAILED_TO_START", {
        error: "Fabric refused to start the cluster notebook (HTTP 400): InvalidParameter",
        error_code: "EXECUTION_FAILED",
      })
    );
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderAgent();
    await submit(user);

    expect(await screen.findByText("cluster — Failed")).toBeInTheDocument();
    expect(screen.getByText(/InvalidParameter/)).toBeInTheDocument();
    expect(screen.queryByTestId("platform-run-id")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "View Results" })).not.toBeInTheDocument();
  });

  it("a completed run whose results cannot be read says so instead of showing results", async () => {
    exec.startAnalysis.mockResolvedValue(job("COMPLETED"));
    exec.getJobResults.mockResolvedValue([
      {
        job_run_id: "acelo-run-1",
        domain: "cluster",
        status: "COMPLETED",
        available: false,
        payload: null,
        error: "ACELO can only read the result table with a service principal.",
        error_code: "RESULT_RETRIEVAL_FAILED",
      },
    ]);
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderAgent();
    await submit(user);

    expect(await screen.findByText("cluster — Completed")).toBeInTheDocument();
    // The backend's specific reason, not a generic placeholder.
    expect(await screen.findByText(/only read the result table with a service principal/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "View Results" })).not.toBeInTheDocument();
  });

  it("no environment set up -> actionable error, nothing submitted", async () => {
    exec.resolveExecutionTarget.mockRejectedValue(
      new ApiError("No environment is set up yet. Complete Environment Setup before running an analysis.", 0)
    );
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderAgent();
    await submit(user);

    expect(await screen.findByText(/Complete Environment Setup/)).toBeInTheDocument();
    expect(exec.startAnalysis).not.toHaveBeenCalled();
  });
});

describe("delegated Fabric authentication on every execution", () => {
  it("acquires a fresh token for EVERY submission and sends it each time", async () => {
    exec.startAnalysis.mockResolvedValue(job("COMPLETED"));
    let issued = 0;
    fabricTokenMock.mockImplementation(async () => `TOKEN-${++issued}`);
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderAgent();

    await submit(user);
    await screen.findByText("cluster — Completed");
    await user.click(screen.getByRole("button", { name: "New request" }));
    await submit(user);
    await screen.findByText("cluster — Completed");

    const [first, second] = exec.startAnalysis.mock.calls.map((c) => c[2]);
    expect(first).toMatch(/^TOKEN-/);
    expect(second).toMatch(/^TOKEN-/);
    // A newly acquired token, not a reused one.
    expect(second).not.toBe(first);
  });

  it("expired session: tells the user to sign in again and never submits", async () => {
    const { FabricAuthError } = await import("../services/fabricAuth");
    fabricTokenMock.mockRejectedValue(new FabricAuthError("Could not obtain a Fabric access token.", "FAILED"));
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderAgent();
    await submit(user);

    expect(
      await screen.findByText("Your Microsoft Fabric session has expired. Please sign in again.")
    ).toBeInTheDocument();
    expect(exec.startAnalysis).not.toHaveBeenCalled();
  });

  it("an empty token is treated as an expired session, not sent", async () => {
    fabricTokenMock.mockResolvedValue("");
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderAgent();
    await submit(user);

    expect(await screen.findByText(/session has expired/)).toBeInTheDocument();
    expect(exec.startAnalysis).not.toHaveBeenCalled();
  });

  it("service-principal environments do not require a delegated token", async () => {
    exec.resolveExecutionTarget.mockResolvedValue({ connectionId: "conn-sp", delegated: false });
    const { FabricAuthError } = await import("../services/fabricAuth");
    fabricTokenMock.mockRejectedValue(new FabricAuthError("no account", "NO_ACCOUNT"));
    exec.startAnalysis.mockResolvedValue(job("STARTING"));
    exec.getJob.mockResolvedValue(job("STARTING"));
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderAgent();
    await submit(user);

    await screen.findByText("cluster — Starting");
    expect(exec.startAnalysis).toHaveBeenCalledWith("Check my cluster utilization", "conn-sp", null, expect.anything());
  });
});

describe("delegated result read", () => {
  it("reads a completed run's results with the user's SQL endpoint token", async () => {
    exec.startAnalysis.mockResolvedValue(job("COMPLETED"));
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderAgent();
    await submit(user);
    await screen.findByText("cluster — Completed");
    expect(exec.getJobResults).toHaveBeenCalledWith("job-1", "DELEGATED-TOKEN", "SQL-ENDPOINT-TOKEN", null);
  });

  it("service-principal environments never request a SQL token", async () => {
    exec.resolveExecutionTarget.mockResolvedValue({ connectionId: "conn-sp", delegated: false });
    exec.startAnalysis.mockResolvedValue(job("COMPLETED"));
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderAgent();
    await submit(user);
    await screen.findByText("cluster — Completed");
    expect(sqlTokenMock).not.toHaveBeenCalled();
    expect(exec.getJobResults).toHaveBeenCalledWith("job-1", "DELEGATED-TOKEN", null, null);
  });
});

describe("approval requests in the AI Agent", () => {
  it.each([
    "Show me pending cluster approvals",
    "Approve cluster etl-heavy",
    "Reject cluster etl-heavy",
    "Why is cluster etl-heavy waiting for approval?",
  ])("'%s' opens Approvals and never starts a cluster run", async (prompt) => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    render(
      <MemoryRouter initialEntries={["/agent"]}>
        <Routes>
          <Route path="/agent" element={<AgentWorkspace />} />
          <Route path="/approvals" element={<p>APPROVALS PAGE</p>} />
        </Routes>
      </MemoryRouter>
    );
    await user.type(screen.getByPlaceholderText("Ask ACELO to analyze your platform..."), prompt);
    await user.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByText("APPROVALS PAGE")).toBeInTheDocument();
    expect(exec.startAnalysis).not.toHaveBeenCalled();
    expect(exec.resolveExecutionTarget).not.toHaveBeenCalled();
  });
});
