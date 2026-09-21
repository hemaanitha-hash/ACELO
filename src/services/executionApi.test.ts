import { describe, expect, it, vi, afterEach } from "vitest";
import { resolveConnectionId, startAnalysis } from "./executionApi";
import { ApiError } from "./environmentApi";

/**
 * Regression cover for two live failures in the Agent -> Cluster flow:
 *
 *  1. Runs were dispatched against a seeded connection that reports
 *     "connected" but has no Environment, so nothing was provisioned or
 *     configured and execution failed as INVALID_CONFIGURATION.
 *  2. The fix for (1) matched on `connection_id`, which the environments
 *     endpoint did not return. Every comparison was against `undefined`, so a
 *     real environment looked like no environment at all.
 */

const SEEDED = {
  id: "conn_fabric_prod",
  platform: "fabric",
  workspace: "ACELO Fabric Production",
  status: "connected",
};
const REAL = {
  id: "real-conn",
  platform: "fabric",
  workspace: "acelo demo",
  status: "connected",
};

/** Records every call so a test can assert what the UI actually requested. */
function mockApi(handlers: Record<string, () => Response | Promise<Response>>) {
  const calls: { url: string; method: string }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, options?: RequestInit) => {
      const path = String(url).replace(/^.*\/api/, "");
      calls.push({ url: path, method: options?.method ?? "GET" });
      const key = Object.keys(handlers).find((k) => path.startsWith(k));
      if (!key) throw new Error(`unexpected request: ${path}`);
      return handlers[key]();
    })
  );
  return calls;
}

const ok = (body: unknown) => () => ({ ok: true, json: async () => body }) as Response;
const fails = (status: number, detail: string) => () =>
  ({ ok: false, status, json: async () => ({ detail }) }) as Response;

afterEach(() => vi.unstubAllGlobals());

describe("resolveConnectionId", () => {
  it("detects the real environment and returns its connection", async () => {
    mockApi({
      "/connections": ok([SEEDED, REAL]),
      "/environments": ok([{ id: "env-1", connection_id: "real-conn" }]),
    });
    await expect(resolveConnectionId()).resolves.toBe("real-conn");
  });

  it("ignores a connected connection that has no environment behind it", async () => {
    mockApi({
      "/connections": ok([SEEDED, REAL]),
      "/environments": ok([{ id: "env-1", connection_id: "real-conn" }]),
    });
    await expect(resolveConnectionId()).resolves.not.toBe("conn_fabric_prod");
  });

  it("does not turn an API failure into 'no environments'", async () => {
    mockApi({
      "/connections": ok([REAL]),
      "/environments": fails(500, "Database unavailable"),
    });
    // The real fault must surface, not the misleading setup prompt.
    await expect(resolveConnectionId()).rejects.toMatchObject({
      status: 500,
      message: "Database unavailable",
    });
  });

  it("reports honestly when no connection has an environment", async () => {
    mockApi({ "/connections": ok([SEEDED]), "/environments": ok([]) });
    await expect(resolveConnectionId()).rejects.toBeInstanceOf(ApiError);
  });

  it("reports when nothing is connected at all", async () => {
    mockApi({ "/connections": ok([]), "/environments": ok([]) });
    await expect(resolveConnectionId()).rejects.toBeInstanceOf(ApiError);
  });
});

describe("analysis submission", () => {
  it("reaches POST /api/jobs with the real connection when an environment exists", async () => {
    const calls = mockApi({
      "/connections": ok([SEEDED, REAL]),
      "/environments": ok([{ id: "env-1", connection_id: "real-conn" }]),
      "/jobs": ok({ id: "job-1", connection_id: "real-conn", job_runs: [] }),
    });

    const connectionId = await resolveConnectionId();
    await startAnalysis("Check my cluster utilization", connectionId, "tok");

    const post = calls.find((c) => c.method === "POST");
    expect(post?.url).toBe("/jobs");
    expect(connectionId).toBe("real-conn");
  });
});
