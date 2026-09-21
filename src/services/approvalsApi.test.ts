import { afterEach, describe, expect, it, vi } from "vitest";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import {
  approve,
  detectApprovalIntent,
  display,
  reject,
  requiresApproval,
  reviewerFrom,
  usd,
} from "./approvalsApi";
import { ApiError } from "./environmentApi";

afterEach(() => vi.unstubAllGlobals());

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("approval actions", () => {
  const reviewer = { userId: "oid-1", userName: "Priya Reviewer" };

  it("sends the signed-in reviewer's identity", async () => {
    const fetchMock = vi.fn(async () => json(200, { status: "APPROVED" }));
    vi.stubGlobal("fetch", fetchMock);
    await approve("ap-1", reviewer);
    const [url, options] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toMatch(/\/approvals\/ap-1\/approve$/);
    expect(options.headers).toMatchObject({ "X-Acelo-User-Id": "oid-1", "X-Acelo-User-Name": "Priya Reviewer" });
  });

  it("refuses to act without a signed-in reviewer (no default user)", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await expect(approve("ap-1", null)).rejects.toBeInstanceOf(ApiError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("a failed save is an error, never a silent success", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(409, { detail: "This recommendation is APPROVED; it cannot be approved." })));
    await expect(approve("ap-1", reviewer)).rejects.toThrow("cannot be approved");
  });

  it("reject carries the reason", async () => {
    const fetchMock = vi.fn(async () => json(200, { status: "REJECTED" }));
    vi.stubGlobal("fetch", fetchMock);
    await reject("ap-1", reviewer, "Needed for month-end");
    const [, options] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(JSON.parse(String(options.body))).toEqual({ reason: "Needed for month-end" });
  });

  it("reviewer comes from the MSAL account", () => {
    expect(reviewerFrom(null)).toBeNull();
    expect(reviewerFrom({ localAccountId: "oid", name: "Priya", username: "p@x.com", homeAccountId: "h" } as never))
      .toEqual({ userId: "oid", userName: "Priya" });
  });
});

describe("values", () => {
  it("real zero stays zero, missing is Not available", () => {
    expect(display(0)).toBe("0");
    expect(display(0, usd)).toBe("$0.00");
    expect(display(null)).toBe("Not available");
    expect(display(undefined, usd)).toBe("Not available");
  });

  it("uses the backend's approval rule", () => {
    expect(requiresApproval({ optimization_label: "Risky", cluster_name: "a" })).toBe(true);
    expect(requiresApproval({ optimization_label: "Moderately Optimized", cluster_name: "a" })).toBe(true);
    expect(requiresApproval({ optimization_label: "Optimized", cluster_name: "a" })).toBe(false);
    expect(requiresApproval({ optimization_label: "Risky", cluster_name: " " })).toBe(false);
  });
});

describe("AI Agent approval intents", () => {
  it.each([
    ["Show me pending cluster approvals", { kind: "list", status: "PENDING" }],
    ["Show me the cluster recommendations awaiting approval", { kind: "list", status: "PENDING" }],
    ["Show approved cluster optimizations", { kind: "list", status: "APPROVED" }],
    ["Why is cluster etl-heavy waiting for approval?", { kind: "review", cluster: "etl-heavy" }],
    ["Approve cluster etl-heavy", { kind: "approve", cluster: "etl-heavy" }],
    ["Reject cluster bi-adhoc", { kind: "reject", cluster: "bi-adhoc" }],
  ])("%s", (prompt, expected) => {
    expect(detectApprovalIntent(prompt)).toEqual(expected);
  });

  it.each(["Check my cluster utilization", "Analyze everything", "Find idle clusters", "Approve all"])(
    "%s is not an approval request",
    (prompt) => expect(detectApprovalIntent(prompt)).toBeNull()
  );
});

describe("no dummy approval data in production code", () => {
  function sources(dir: string): string[] {
    return readdirSync(dir).flatMap((name) => {
      const path = join(dir, name);
      if (statSync(path).isDirectory()) return sources(path);
      return /\.(ts|tsx)$/.test(name) && !/\.test\.(ts|tsx)$/.test(name) && !path.includes("test") ? [path] : [];
    });
  }

  it("runtime code has no demo data, fake reviewers or canned SQL", () => {
    const root = join(process.cwd(), "src");
    for (const file of sources(root)) {
      const text = readFileSync(file, "utf8");
      expect(text, file).not.toMatch(/demoData|demoApprovals|demoExecution/);
      expect(text, file).not.toMatch(/"Hema"/);
      expect(text, file).not.toMatch(/ACELO FinOps Refactored|smtplib|gmail/i);
    }
  });
});

describe("refreshFromTracking", () => {
  it("sends the OneLake token in its own header and no SQL token", async () => {
    const { refreshFromTracking } = await import("./approvalsApi");
    const fetchMock = vi.fn(async () => json(200, { sources: [], summary: {} }));
    vi.stubGlobal("fetch", fetchMock);
    await refreshFromTracking("FABRIC", "ONELAKE");
    const [url, options] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toMatch(/\/approvals\/refresh$/);
    expect(options.method).toBe("POST");
    expect(options.headers).toMatchObject({ "X-OneLake-Token": "ONELAKE", "X-Fabric-Access-Token": "FABRIC" });
    expect(options.headers).not.toHaveProperty("X-Fabric-Sql-Token");
  });
});
