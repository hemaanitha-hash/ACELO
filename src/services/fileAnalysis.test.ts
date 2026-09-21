import { afterEach, describe, expect, it, vi } from "vitest";
import {
  describeClusterRecommendation,
  summariseClusterRows,
  uploadClusterFile,
  UploadValidationError,
} from "./executionApi";

afterEach(() => vi.unstubAllGlobals());

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("uploadClusterFile", () => {
  it("posts multipart to the file-analysis endpoint, with no platform token", async () => {
    const fetchMock = vi.fn(async () => json(202, { id: "job1", platform: "file", job_runs: [] }));
    vi.stubGlobal("fetch", fetchMock);

    const job = await uploadClusterFile(new File(["a,b"], "c.csv"), "Find idle clusters");

    expect(job.platform).toBe("file");
    const [url, options] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toMatch(/\/api\/file-analysis\/cluster$/);
    expect(options.body).toBeInstanceOf(FormData);
    expect((options.body as FormData).get("prompt")).toBe("Find idle clusters");
    expect(options.headers).toBeUndefined();
  });

  it("surfaces missing columns and invalid values from the backend", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        json(422, {
          detail: {
            code: "MISSING_COLUMNS",
            message: "The file is missing required columns: idle_time_min.",
            missing_columns: ["idle_time_min"],
            invalid_values: [{ row: 3, column: "avg_cpu_util", value: "x", reason: "is not a number" }],
          },
        })
      )
    );

    const error = await uploadClusterFile(new File(["x"], "c.csv")).catch((e) => e);
    expect(error).toBeInstanceOf(UploadValidationError);
    expect(error.code).toBe("MISSING_COLUMNS");
    expect(error.missingColumns).toEqual(["idle_time_min"]);
    expect(error.invalidValues[0].row).toBe(3);
  });
});

describe("summariseClusterRows", () => {
  it("derives every KPI from the rows only", () => {
    const s = summariseClusterRows([
      { optimization_label: "Optimized", avg_cpu_util: 60, avg_memory_util: 70, idle_flag: 0, oversized_flag: 0, potential_monthly_savings: 10, total_dbus_cost_usd: 100 },
      { optimization_label: "Moderately Optimized", avg_cpu_util: 30, avg_memory_util: 40, idle_flag: 1, oversized_flag: 1, potential_monthly_savings: 20, total_dbus_cost_usd: 200 },
      { optimization_label: "Risky", avg_cpu_util: 0, avg_memory_util: 10, idle_flag: 1, oversized_flag: 0, potential_monthly_savings: 30, total_dbus_cost_usd: 300 },
    ]);
    expect(s).toMatchObject({
      clusterCount: 3,
      healthy: 1,
      atRisk: 1,
      critical: 1,
      idleCount: 2,
      oversizedCount: 1,
      monthlySavings: 60,
      currentCost: 600,
      avgCpuUtil: 30,
      avgMemoryUtil: 40,
    });
  });

  it("reports no averages rather than zero when the data has none", () => {
    expect(summariseClusterRows([]).avgCpuUtil).toBeNull();
  });
});

describe("describeClusterRecommendation", () => {
  it("builds text from optimizer fields", () => {
    const text = describeClusterRecommendation({
      max_workers: 10,
      recommended_max_workers: 5,
      idle_flag: 1,
      underutilized_label: "Highly Underutilized",
      oversized_flag: 0,
    });
    expect(text).toContain("Reduce max workers from 10 to 5");
    expect(text).toContain("Highly Underutilized");
  });

  it("returns null when the optimizer recommends no change", () => {
    expect(
      describeClusterRecommendation({ max_workers: 8, recommended_max_workers: 8, idle_flag: 0, oversized_flag: 0 })
    ).toBeNull();
  });
});
