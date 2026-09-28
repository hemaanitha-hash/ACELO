import { afterEach, describe, expect, it, vi } from "vitest";
import { getOptimizations } from "./api";

const STAGE1_RECORD = {
  id: "stage1-rec-1",
  recommendation_id: "stage1-rec-1",
  kind: "stage1",
  resource: "analytics",
  resource_id: "cluster-1",
  domain: "cluster",
  stage1_domain: "CLUSTER_SIZING",
  title: "Review cluster capacity",
  description: "Observed low utilization.",
  estimated_savings_monthly: null,
  status: "open",
  finding_id: "finding-1",
  rule_id: "STAGE1.CLUSTER_SIZING.OVERSIZED",
  severity: "MEDIUM",
  confidence: "low",
  risk: "MEDIUM",
  details: {
    finding_id: "finding-1",
    rule_id: "STAGE1.CLUSTER_SIZING.OVERSIZED",
    summary: "Observed low utilization.",
    description: "The rule was supported by historical evidence.",
    confidence: "low",
    severity: "MEDIUM",
    risk: "MEDIUM",
    evidence: { avg_cpu_percent: 12.5 },
    evidence_quality: { completeness: "COMPLETE", freshness: "UNKNOWN" },
    current_state: { worker_count: 8 },
    proposed_state: { direction: "review_worker_capacity", reason: "Review capacity." },
  },
};

afterEach(() => vi.unstubAllGlobals());

describe("Stage 1 optimization API adapter", () => {
  it("preserves finding traceability and unavailable savings in the existing opportunity contract", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => [STAGE1_RECORD],
    });
    vi.stubGlobal("fetch", fetchMock);

    const [opportunity] = await getOptimizations();

    expect(opportunity.stage1Recommendation).toBe(true);
    expect(opportunity.findingId).toBe("finding-1");
    expect(opportunity.ruleId).toBe("STAGE1.CLUSTER_SIZING.OVERSIZED");
    expect(opportunity.impactMonthly).toBeNull();
    expect(opportunity.status).toBe("Review");
    expect(opportunity.rollbackAvailable).toBe(false);
    expect(opportunity.currentState).toEqual({ worker_count: 8 });
    expect(opportunity.proposedState).toEqual({ direction: "review_worker_capacity", reason: "Review capacity." });
    expect(String(fetchMock.mock.calls[0][0])).toContain("/optimizations");
  });
});
