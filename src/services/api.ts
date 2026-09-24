// ============================================================================
// ACELO SERVICE LAYER - Connected to FastAPI Backend with Resilient Fallback
// ============================================================================

import type {
  ConnectedPlatform,
  OverviewData,
  Opportunity,
  AuditEntry,
  Domain,
} from "../types";
import { API_BASE } from "./apiBase";

async function fetchJson<T>(url: string, options?: RequestInit): Promise<T | null> {
  try {
    const res = await fetch(url, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        ...options?.headers,
      },
    });
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch {
    return null;
  }
}

export async function getOverview(): Promise<OverviewData> {
  // Real backend values are passed through untouched. The previous version
  // substituted demo figures (42850 cost, 14280 savings, health 78/75) whenever
  // the backend returned null or the request failed, so an account with no
  // analysis history still rendered a fully populated dashboard. Those
  // fallbacks are gone: no data now means an honest empty state.
  const data = await fetchJson<any>(`${API_BASE}/overview`);
  if (!data) {
    return {
      userName: "",
      kpis: { monthlyCost: null, potentialSavings: null, openOpportunities: null, optimizationHealth: null },
      health: [],
      lastAnalysisMinutesAgo: null,
      platformConnected: null,
      priorityOpportunities: [],
      hasData: false,
    };
  }

  const platformLabel = (p: string | null): ConnectedPlatform | null =>
    p === "fabric" ? "Microsoft Fabric" : p === "databricks" ? "Databricks" : null;

  return {
    userName: data.userName ?? "",
    kpis: {
      monthlyCost: data.kpis?.monthlyCost ?? null,
      potentialSavings: data.kpis?.potentialSavings ?? null,
      openOpportunities: data.kpis?.openOpportunities ?? null,
      optimizationHealth: data.kpis?.optimizationHealth ?? null,
    },
    health: (data.health ?? []).map((h: any) => ({
      domain: (h.domain.charAt(0).toUpperCase() + h.domain.slice(1)) as Domain,
      score: h.health ?? null,
      label:
        h.health == null
          ? "No data"
          : h.health >= 80
            ? "Healthy"
            : h.health >= 60
              ? "Attention"
              : "Critical",
    })),
    lastAnalysisMinutesAgo: data.lastAnalysisMinutesAgo ?? null,
    platformConnected: platformLabel(data.platformConnected ?? null),
    // Only real recommendations; no demo rows injected.
    priorityOpportunities: await getOptimizations(),
    hasData: Boolean(data.hasData),
    cluster: data.cluster ?? null,
    query: data.query ?? null,
    executions: data.executions ?? null,
  };
}

export async function getOptimizations(): Promise<Opportunity[]> {
  const data = await fetchJson<any[]>(`${API_BASE}/optimizations`);
  if (data && Array.isArray(data) && data.length > 0) {
    return data.map((item, idx) => ({
      id: item.id ?? `opt_${idx}`,
      title: item.title ?? item.resource,
      domain: (item.domain ? item.domain.charAt(0).toUpperCase() + item.domain.slice(1) : "Query") as Domain,
      resource: item.resource ?? "Compute Resource",
      impactMonthly: typeof item.estimated_savings_monthly === "number" ? item.estimated_savings_monthly : null,
      severity: (item.estimated_savings_monthly ?? 0) > 2000 ? "High" : "Medium",
      status: item.status === "approved" ? "Approved" : item.status === "rejected" ? "Rejected" : "Review",
      detectedAt: item.created_at ? new Date(item.created_at).toLocaleDateString() : "Not available",
      whyFlagged: item.details?.issue ?? "Not available",
      aiAnalysis: item.details?.evidence ?? item.description ?? "Not available",
      recommendedAction: item.details?.optimized_query ?? item.details?.recommendation ?? "Not available",
      risk: "Low",
      rollbackAvailable: true,
    }));
  }
  return [];
}

export async function getRecommendation(id: string): Promise<Opportunity | undefined> {
  const opps = await getOptimizations();
  return opps.find((o) => o.id === id);
}

// Approvals and executions: see services/approvalsApi.ts (real data only).

export async function getHistory(): Promise<AuditEntry[]> {
  const data = await fetchJson<any[]>(`${API_BASE}/history`);
  if (data && Array.isArray(data) && data.length > 0) {
    return data.map((item, idx) => ({
      id: item.id ?? `hist_${idx}`,
      date: item.created_at ? new Date(item.created_at).toLocaleDateString() : "Today",
      userRequest: item.summary ?? "Platform optimization executed",
      agentRoute: "All Capabilities",
      action: item.event_type.includes("approved") ? "Approve" : "Analyze",
      result: "Completed",
      detail: {
        agentDecision: item.summary,
        analysis: item.payload?.analysis ?? "Not available",
        recommendation: item.payload?.summary ?? "Not available",
        approval: item.payload?.approved_by ?? "Not available",
        execution: item.payload?.platform_run_id ?? "Not available",
        validation: item.payload?.validation ?? "Not available",
        rollback: item.payload?.rollback ?? "Not available",
      },
    }));
  }
  return [];
}
