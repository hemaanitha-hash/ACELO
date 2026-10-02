import React from "react";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The ACELO lifecycle against a stateful fake of the EXISTING backend routes:
 *
 *   Recommendation -> Send to Approval -> Pending -> Approve -> Ready to Execute
 *
 * Every screen must read persisted state (so a remount — a browser refresh —
 * shows the same thing), use the same recommendation id throughout, and never
 * call an execution endpoint.
 */

vi.mock("@azure/msal-react", () => ({
  useMsal: () => ({
    instance: {
      getActiveAccount: () => ({ localAccountId: "u-1", username: "reviewer@example.com", name: "Riley Reviewer" }),
      getAllAccounts: () => [],
    },
  }),
}));
vi.mock("../services/experience", async () => {
  const actual = await vi.importActual<typeof import("../services/experience")>("../services/experience");
  return { ...actual, isDatabricksOnly: () => true };
});

import Recommendations from "./Recommendations";
import Approvals from "./Approvals";
import Execution from "./Execution";
import { resetComputeAnalysis } from "../services/computeAnalysis";

const REC_ID = "rec-stable-42";

interface Db {
  approval_status: string;
  approval: { status: string; decided_by: string | null; decided_at: string | null } | null;
}

let db: Db;
let calls: { url: string; method: string; headers: Record<string, string>; body?: string }[];

function recommendation() {
  return {
    id: REC_ID,
    recommendation_id: REC_ID,
    kind: "stage1",
    resource: "production-etl-01",
    resource_id: "0901-cluster",
    domain: "cluster",
    title: "CPU-heavy cluster",
    description: "Sustained CPU saturation on the driver.",
    rule_id: "STAGE1.CLUSTER_SIZING.CPU",
    severity: "HIGH",
    risk: "MEDIUM",
    created_at: "2026-09-30T08:00:00Z",
    details: {
      approval_status: db.approval_status,
      execution_status: "NOT_STARTED",
      evidence: { avg_cpu_percent: 91 },
      proposed_state: { direction: "increase_worker_capacity", reason: "CPU bound" },
    },
  };
}

function approvalRows() {
  if (!db.approval) return [];
  return [
    {
      approval_id: "apr-1",
      recommendation_id: REC_ID,
      status: db.approval.status,
      requested_by: null,
      decided_by: db.approval.decided_by,
      created_at: "2026-09-30T09:00:00Z",
      decided_at: db.approval.decided_at,
      recommendation: {
        id: "row-1",
        title: "CPU-heavy cluster",
        resource: "production-etl-01",
        resource_id: "0901-cluster",
        rule_id: "STAGE1.CLUSTER_SIZING.CPU",
        summary: "Sustained CPU saturation on the driver.",
        proposed_state: JSON.stringify({ direction: "increase_worker_capacity" }),
        evidence: { avg_cpu_percent: 91 },
        severity: "HIGH",
        approval_status: db.approval_status,
        execution_status: "NOT_STARTED",
      },
    },
  ];
}

function respond(body: unknown, status = 200) {
  return { ok: status < 400, status, text: async () => JSON.stringify(body), json: async () => body };
}

beforeEach(() => {
  resetComputeAnalysis();
  db = { approval_status: "NOT_REQUESTED", approval: null };
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const u = String(url);
      const method = init?.method ?? "GET";
      calls.push({ url: u, method, headers: (init?.headers ?? {}) as Record<string, string>, body: init?.body as string });
      if (u.includes("/optimizations?domain=stage1")) return respond([recommendation()]);
      if (u.endsWith(`/approvals/recommendations/${REC_ID}/request`) && method === "POST") {
        db.approval_status = "PENDING";
        db.approval = { status: "pending", decided_by: null, decided_at: null };
        return respond({ ok: true });
      }
      if (u.endsWith(`/approvals/recommendations/${REC_ID}/approve`) && method === "POST") {
        db.approval_status = "APPROVED";
        db.approval = { status: "approved", decided_by: "Riley Reviewer", decided_at: "2026-09-30T10:00:00Z" };
        return respond({ ok: true });
      }
      if (u.includes("/approvals/recommendations")) {
        const wanted = new URL(u, "http://x").searchParams.get("status");
        const rows = approvalRows().filter((r) => !wanted || wanted.split(",").includes(r.status));
        return respond(rows);
      }
      if (u.includes("/runs")) return respond({ total: 0, count: 0, runs: [] });
      if (u.includes("/notifications")) return respond({ unread: 0, notifications: [] });
      if (u.includes("/connections")) return respond([]);
      return respond({ detail: "Not Found" }, 404);
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderAt(ui: React.ReactElement, path = "/") {
  return render(<MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>);
}

describe("ACELO lifecycle on persisted state", () => {
  it("Send to Approval persists, and a refresh still shows Pending Approval", async () => {
    const first = renderAt(<Recommendations />, "/recommendations");
    const card = await screen.findByTestId("recommendation-card");
    expect(within(card).getByText("Open")).toBeInTheDocument();

    await userEvent.click(within(card).getByRole("button", { name: /Send to Approval/ }));
    await waitFor(() => expect(within(screen.getByTestId("recommendation-card")).getByText("Pending Approval")).toBeInTheDocument(), { timeout: 8000 });
    // The state came from a reload of the backend, not from local state.
    expect(calls.filter((c) => c.url.includes("/optimizations?domain=stage1")).length).toBeGreaterThanOrEqual(2);

    first.unmount(); // browser refresh
    renderAt(<Recommendations />, "/recommendations");
    const again = await screen.findByTestId("recommendation-card");
    await waitFor(() => expect(within(again).getByText("Pending Approval")).toBeInTheDocument());
  }, 20_000);

  it("Approvals respects ?status= and approving sends the reviewer headers, then reloads", async () => {
    db = { approval_status: "PENDING", approval: { status: "pending", decided_by: null, decided_at: null } };
    renderAt(<Approvals />, "/approvals?status=PENDING");

    const card = await screen.findByTestId("approval-card");
    await userEvent.click(within(card).getByRole("button", { name: /Approve/ }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Approve" }));

    await waitFor(() => expect(screen.getByTestId("approval-success")).toBeInTheDocument());
    const approve = calls.find((c) => c.url.endsWith(`/${REC_ID}/approve`));
    expect(approve?.headers["X-Acelo-User-Id"]).toBe("u-1");
    expect(approve?.headers["X-Acelo-User-Name"]).toBe("Riley Reviewer");
    // Reloaded: the pending tab is now empty.
    expect(await screen.findByText("No pending approvals")).toBeInTheDocument();
  });

  it("an approved recommendation appears in Execution as Ready to Execute, and Execute calls nothing", async () => {
    db = {
      approval_status: "APPROVED",
      approval: { status: "approved", decided_by: "Riley Reviewer", decided_at: "2026-09-30T10:00:00Z" },
    };
    renderAt(<Execution />, "/execution");

    const card = await screen.findByTestId("execution-card");
    expect(within(card).getByText("CPU-heavy cluster")).toBeInTheDocument();
    expect(within(card).getByText("production-etl-01")).toBeInTheDocument();
    expect(within(card).getAllByText("Ready to Execute").length).toBeGreaterThan(0);
    expect(within(card).getByText("Riley Reviewer")).toBeInTheDocument();

    const execute = within(card).getByRole("button", { name: /Execute/ });
    expect(execute).toBeDisabled();
    expect(
      within(card).getByText(/Execution action will be enabled when the execution backend is activated/),
    ).toBeInTheDocument();
    expect(calls.some((c) => c.method === "POST")).toBe(false);
    expect(calls.some((c) => c.url.includes("/execute"))).toBe(false);
    expect(document.body.textContent).not.toMatch(/not supported|unavailable|approval-only/i);
  });
});
