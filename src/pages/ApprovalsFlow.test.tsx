/**
 * Approvals UI: real records only, explicit confirmation for every decision,
 * a mandatory rejection reason, execution as a separate step, and dashboard /
 * AI Agent entry points that never act on their own.
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { api, msal } = vi.hoisted(() => ({
  api: {
    listApprovals: vi.fn(),
    getApprovalSummary: vi.fn(),
    getApproval: vi.fn(),
    approve: vi.fn(),
    reject: vi.fn(),
    execute: vi.fn(),
    refreshFromTracking: vi.fn(),
  },
  msal: {
    instance: {
      getActiveAccount: () => ({ localAccountId: "oid-1", homeAccountId: "h", name: "Priya Reviewer", username: "p@x.com" }),
      getAllAccounts: () => [],
    },
    accounts: [],
    inProgress: "none",
  },
}));

vi.mock("@azure/msal-react", () => ({ useMsal: () => msal }));
vi.mock("../services/fabricAuth", async () => {
  const actual = await vi.importActual<typeof import("../services/fabricAuth")>("../services/fabricAuth");
  return {
    ...actual,
    getFabricToken: vi.fn(async () => "FABRIC-TOKEN"),
    getOneLakeToken: vi.fn(async () => "ONELAKE-TOKEN"),
  };
});
vi.mock("../services/approvalsApi", async () => {
  const actual = await vi.importActual<typeof import("../services/approvalsApi")>("../services/approvalsApi");
  return { ...actual, ...api };
});
vi.mock("../services/api", async () => {
  const actual = await vi.importActual<typeof import("../services/api")>("../services/api");
  return {
    ...actual,
    getOverview: vi.fn(async () => ({
      userName: "", kpis: { monthlyCost: 0, potentialSavings: 0, openOpportunities: 0, optimizationHealth: null },
      health: [], lastAnalysisMinutesAgo: null, platformConnected: null, priorityOpportunities: [], hasData: false,
    })),
  };
});

import Approvals from "./Approvals";
import ApprovalDetail from "./ApprovalDetail";
import Overview from "./Overview";
import { ApiError } from "../services/environmentApi";

function approval(overrides: Record<string, unknown> = {}) {
  return {
    approval_id: "ap-1", acelo_run_id: "run-77", customer_id: "c", environment_id: "env-9",
    platform: "fabric", domain: "cluster", resource_id: "id-etl", resource_name: "etl-heavy",
    optimization_label: "Risky", status: "PENDING", current_workers: 8, recommended_max_workers: 5,
    total_dbus_cost_usd: 1200.5, potential_monthly_savings: 340.25,
    llm_optimization: "Reduce max workers to 5 and set auto-termination to 20 minutes.",
    evidence: { efficiency_score: 0.31, avg_cpu_util: 12.5 }, created_at: "2026-09-21T10:00:00Z",
    updated_at: null, approved_by: null, approved_at: null, rejected_by: null, rejected_at: null,
    rejection_reason: null, execution_id: null, validation_status: null, execution_error: null,
    history: [{ action: "CREATED", previous_status: null, new_status: "PENDING", user_id: null,
                user_name: null, reason: null, timestamp: "2026-09-21T10:00:00Z" }],
    ...overrides,
  };
}

const SUMMARY = { PENDING: 1, APPROVED: 0, REJECTED: 2, EXECUTING: 0, COMPLETED: 0, FAILED: 0, CANCELLED: 0 };

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/" element={<Overview />} />
        <Route path="/approvals" element={<Approvals />} />
        <Route path="/approvals/:id" element={<ApprovalDetail />} />
      </Routes>
    </MemoryRouter>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  api.getApprovalSummary.mockResolvedValue(SUMMARY);
  api.listApprovals.mockResolvedValue([approval()]);
  api.getApproval.mockResolvedValue(approval());
  api.refreshFromTracking.mockResolvedValue({ sources: [], summary: SUMMARY });
});

describe("Approvals list", () => {
  it("shows real records and counts from the API", async () => {
    renderAt("/approvals");
    const row = (await screen.findByText("etl-heavy")).closest("tr")!;
    expect(row).toHaveTextContent("Risky");
    expect(row).toHaveTextContent("$1,200.50");
    expect(row).toHaveTextContent("$340.25");
    expect(row).toHaveTextContent("PENDING");
    expect(screen.getByTestId("count-PENDING")).toHaveTextContent("1");
    expect(screen.getByTestId("count-REJECTED")).toHaveTextContent("2");
    expect(api.listApprovals).toHaveBeenCalledWith({ status: ["PENDING"] });
  });

  it("empty state is 'No pending approvals.' — no dummy rows", async () => {
    api.listApprovals.mockResolvedValue([]);
    renderAt("/approvals");
    expect(await screen.findByText("No pending approvals.")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("load failure says so instead of showing data", async () => {
    api.listApprovals.mockRejectedValue(new ApiError("backend down", 500));
    renderAt("/approvals");
    expect(await screen.findByText("Unable to load optimization recommendations.")).toBeInTheDocument();
  });

  it("missing values read 'Not available'; a real 0 stays 0", async () => {
    api.listApprovals.mockResolvedValue([
      approval({ potential_monthly_savings: 0, total_dbus_cost_usd: null, current_workers: null }),
    ]);
    renderAt("/approvals");
    const row = (await screen.findByText("etl-heavy")).closest("tr")!;
    expect(row).toHaveTextContent("$0.00");
    expect(within(row).getAllByText("Not available")).toHaveLength(2);
  });
});

describe("Review, approve, reject", () => {
  it("shows the actual recommendation", async () => {
    renderAt("/approvals/ap-1");
    expect(await screen.findByTestId("llm-recommendation")).toHaveTextContent("Reduce max workers to 5");
    expect(screen.getByText("run-77")).toBeInTheDocument();
    expect(screen.getByText("env-9")).toBeInTheDocument();
    expect(screen.getByText("Source", { selector: "dt" }).parentElement).toHaveTextContent("Microsoft Fabric");
    expect(screen.getByText("efficiency score")).toBeInTheDocument();
  });

  it("approve needs confirmation and records the reviewer", async () => {
    api.approve.mockResolvedValue(approval({ status: "APPROVED", approved_by: "Priya Reviewer",
                                             approved_at: "2026-09-21T11:00:00Z" }));
    const user = userEvent.setup();
    renderAt("/approvals/ap-1");
    await user.click(await screen.findByRole("button", { name: "Approve" }));
    expect(api.approve).not.toHaveBeenCalled(); // not yet — confirmation first

    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("Approve this optimization?");
    expect(dialog).toHaveTextContent("8 workers");
    expect(dialog).toHaveTextContent("max 5 workers");
    expect(dialog).toHaveTextContent("$340.25/mo");
    await user.click(within(dialog).getByRole("button", { name: "Confirm Approval" }));

    expect(api.approve).toHaveBeenCalledWith("ap-1", { userId: "oid-1", userName: "Priya Reviewer" });
    expect(await screen.findByText("Approved. Ready to execute.")).toBeInTheDocument();
    expect(screen.getByText(/Approved by/)).toHaveTextContent("Priya Reviewer");
    expect(api.execute).not.toHaveBeenCalled(); // approval is not execution
  });

  it("cancel closes without approving", async () => {
    const user = userEvent.setup();
    renderAt("/approvals/ap-1");
    await user.click(await screen.findByRole("button", { name: "Approve" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));
    expect(api.approve).not.toHaveBeenCalled();
  });

  it("reject cannot be confirmed without a reason", async () => {
    api.reject.mockResolvedValue(approval({ status: "REJECTED", rejected_by: "Priya Reviewer",
                                            rejection_reason: "Needed for month-end close" }));
    const user = userEvent.setup();
    renderAt("/approvals/ap-1");
    await user.click(await screen.findByRole("button", { name: "Reject" }));
    const confirm = screen.getByRole("button", { name: "Confirm Rejection" });
    expect(confirm).toBeDisabled();

    await user.type(screen.getByLabelText("Rejection reason"), "Needed for month-end close");
    expect(confirm).toBeEnabled();
    await user.click(confirm);
    expect(api.reject).toHaveBeenCalledWith("ap-1", expect.anything(), "Needed for month-end close");
    expect(await screen.findByText(/Rejection reason: Needed for month-end close/)).toBeInTheDocument();
  });

  it("a failed save shows the required message", async () => {
    api.approve.mockRejectedValue(new ApiError("server error", 500));
    const user = userEvent.setup();
    renderAt("/approvals/ap-1");
    await user.click(await screen.findByRole("button", { name: "Approve" }));
    await user.click(screen.getByRole("button", { name: "Confirm Approval" }));
    expect(await screen.findByTestId("dialog-error")).toHaveTextContent("Approval could not be saved. Please try again.");
  });

  it("decided records offer no approve/reject", async () => {
    api.getApproval.mockResolvedValue(approval({ status: "REJECTED", rejected_by: "Priya", rejection_reason: "No" }));
    renderAt("/approvals/ap-1");
    await screen.findByText("No changes will be applied for this recommendation.");
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();
  });
});

describe("Execution after approval", () => {
  it("is a separate, confirmed step", async () => {
    api.getApproval.mockResolvedValue(approval({ status: "APPROVED", approved_by: "Priya" }));
    api.execute.mockResolvedValue(approval({ status: "EXECUTING", execution_id: "exec-42" }));
    const user = userEvent.setup();
    renderAt("/approvals/ap-1");
    await user.click(await screen.findByRole("button", { name: "Execute Optimization" }));
    expect(api.execute).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Confirm Execution" }));
    expect(api.execute).toHaveBeenCalledWith("ap-1", expect.anything(), "FABRIC-TOKEN");
    expect(await screen.findByText(/exec-42/)).toBeInTheDocument();
  });

  it("an unsupported platform keeps it approved and says why", async () => {
    api.getApproval.mockResolvedValue(approval({ status: "APPROVED" }));
    api.execute.mockRejectedValue(new ApiError("Automated execution is not available for fabric yet.", 409));
    const user = userEvent.setup();
    renderAt("/approvals/ap-1");
    await user.click(await screen.findByRole("button", { name: "Execute Optimization" }));
    await user.click(screen.getByRole("button", { name: "Confirm Execution" }));
    expect(await screen.findByTestId("dialog-error")).toHaveTextContent(
      "Optimization execution failed. Automated execution is not available for fabric yet."
    );
  });

  it("a failed execution shows the real execution ID", async () => {
    api.getApproval.mockResolvedValue(approval({ status: "FAILED", execution_id: "exec-42",
                                                 execution_error: "Pool resize rejected" }));
    renderAt("/approvals/ap-1");
    const failed = await screen.findByTestId("execution-failed");
    expect(failed).toHaveTextContent("Optimization execution failed.");
    expect(failed).toHaveTextContent("Pool resize rejected");
    expect(failed).toHaveTextContent("exec-42");
  });
});

describe("AI Agent deep links never act silently", () => {
  it("'Approve cluster X' opens the confirmation, nothing more", async () => {
    renderAt("/approvals?cluster=etl-heavy&action=approve");
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Approve this optimization?");
    expect(api.approve).not.toHaveBeenCalled();
  });

  it("'Reject cluster X' opens the reason dialog", async () => {
    renderAt("/approvals?cluster=etl-heavy&action=reject");
    expect(await screen.findByText("Why are you rejecting this recommendation?")).toBeInTheDocument();
    expect(api.reject).not.toHaveBeenCalled();
  });
});

describe("Dashboard approval KPIs", () => {
  it("come from the real summary and link to Approvals", async () => {
    const user = userEvent.setup();
    renderAt("/");
    await waitFor(() => expect(screen.getByTestId("approval-kpi-PENDING")).toHaveTextContent("1"));
    expect(screen.getByTestId("approval-kpi-REJECTED")).toHaveTextContent("2");
    expect(screen.getByTestId("approval-kpi-COMPLETED")).toHaveTextContent("0");
    await user.click(screen.getByTestId("approval-kpi-PENDING"));
    expect(await screen.findByText("Approvals", { selector: "h1" })).toBeInTheDocument();
  });

  it("an unavailable summary is not shown as zero", async () => {
    api.getApprovalSummary.mockRejectedValue(new ApiError("down", 500));
    renderAt("/");
    await waitFor(() => expect(screen.getByTestId("approval-kpi-PENDING")).toHaveTextContent("Not available"));
  });

  it("no invented greeting or platform", async () => {
    renderAt("/");
    expect(await screen.findByText("Good morning")).toBeInTheDocument();
    expect(screen.getByText("No platform connected")).toBeInTheDocument();
    expect(screen.queryByText(/Hema|Databricks Connected|System Healthy/)).not.toBeInTheDocument();
  });
});

describe("Approvals read from the Fabric tracking Delta table (OneLake, no SQL endpoint)", () => {
  it("refreshes from the tracking table with the OneLake token on load", async () => {
    api.refreshFromTracking.mockResolvedValue({
      sources: [{ environment_id: "env-9", table: "cluster_optimization_tracking", status: "ok",
                  rows_read: 3, candidates: 2, created: 2 }],
      summary: SUMMARY,
    });
    renderAt("/approvals");
    expect(await screen.findByTestId("tracking-ok")).toHaveTextContent(
      "Read 3 rows from cluster_optimization_tracking · 2 new"
    );
    expect(api.refreshFromTracking).toHaveBeenCalledWith("FABRIC-TOKEN", "ONELAKE-TOKEN");
    // The list shown is what the backend persisted after the import.
    expect(await screen.findByText("etl-heavy")).toBeInTheDocument();
  });

  it("a failed Delta read is shown as a clear error and adds no rows", async () => {
    api.listApprovals.mockResolvedValue([]);
    api.refreshFromTracking.mockResolvedValue({
      sources: [{ environment_id: "env-9", table: "cluster_optimization_tracking", status: "failed",
                  error_code: "RESOURCE_NOT_FOUND",
                  message: "The Delta table 'dbo.cluster_optimization_tracking' was not found in the Lakehouse." }],
      summary: SUMMARY,
    });
    renderAt("/approvals");
    const error = await screen.findByTestId("tracking-error");
    expect(error).toHaveTextContent("Unable to load optimization recommendations from the approval tracking table.");
    expect(error).toHaveTextContent("was not found in the Lakehouse");
    expect(await screen.findByText("No pending approvals.")).toBeInTheDocument();
  });

  it("an unconfigured tracking table is reported, not faked", async () => {
    api.listApprovals.mockResolvedValue([]);
    api.refreshFromTracking.mockRejectedValue(
      new ApiError("No approval tracking table is configured. Set it in Settings → Cluster Settings.", 409)
    );
    renderAt("/approvals");
    expect(await screen.findByTestId("tracking-error")).toHaveTextContent("No approval tracking table is configured");
  });

  it("tracking-table approvals show their source and run", async () => {
    api.getApproval.mockResolvedValue(approval({
      source: "tracking_table", acelo_run_id: null, tracking_status: "PENDING",
      evidence: { source_acelo_run_id: "run-from-delta" },
    }));
    renderAt("/approvals/ap-1");
    await screen.findByTestId("llm-recommendation");
    expect(screen.getByText("Source", { selector: "dt" }).parentElement).toHaveTextContent(
      "Microsoft Fabric · approval tracking table"
    );
    expect(screen.getByText("ACELO Run ID", { selector: "dt" }).parentElement).toHaveTextContent("run-from-delta");
  });
});
