// ============================================================================
// DEMO DATA
// ----------------------------------------------------------------------------
// This file contains static demo/mock values ONLY, used to power the UI
// prototype before the FastAPI backend is connected. Every value here is
// illustrative and should be treated as a fixture, not real telemetry.
//
// When the backend is ready, the functions in `src/services/api.ts` should
// stop importing from this file and instead call the live ACELO API. No
// component should import this file directly — always go through the
// service layer so the swap is a one-file change.
// ============================================================================

import type {
  Opportunity,
  OverviewData,
  ApprovalRequest,
  ExecutionState,
  ExecutionResult,
  AuditEntry,
  AgentRoute,
  AgentStep,
} from "../types";

export const demoOpportunities: Opportunity[] = [
  {
    id: "OPT-1024",
    title: "High scan workload",
    domain: "Query",
    resource: "Customer Transactions",
    impactMonthly: 45.2,
    severity: "High",
    status: "Review",
    detectedAt: "2026-09-17",
    whyFlagged:
      "Repeated expensive execution and high scan volume detected across the workload. The same query pattern re-reads a much larger volume of data than the result set requires.",
    aiAnalysis:
      "ACELO identified that this workload repeatedly scans the full table instead of pruning by date range. Adding a partitioning strategy aligned with the existing query filters is expected to cut scanned data significantly without changing query results.",
    recommendedAction:
      "Review the proposed optimization, validate expected impact, and request approval before any controlled change is executed.",
    risk: "Medium",
    rollbackAvailable: true,
  },
  {
    id: "OPT-1031",
    title: "Idle compute capacity",
    domain: "Cluster",
    resource: "Analytics Compute",
    impactMonthly: 31.4,
    severity: "Medium",
    status: "Review",
    detectedAt: "2026-09-16",
    whyFlagged:
      "Compute capacity has remained provisioned well above observed utilization during off-peak hours over the last two weeks.",
    aiAnalysis:
      "Utilization on this compute resource averages well below its provisioned capacity outside business hours. An autoscaling policy tuned to the observed usage pattern is expected to reduce idle spend while preserving peak-hour performance.",
    recommendedAction:
      "Review the proposed optimization, validate expected impact, and request approval before any controlled change is executed.",
    risk: "Low",
    rollbackAvailable: true,
  },
  {
    id: "OPT-1038",
    title: "Small file accumulation",
    domain: "Storage",
    resource: "sales_transactions",
    impactMonthly: 22.1,
    severity: "Medium",
    status: "Review",
    detectedAt: "2026-09-15",
    whyFlagged:
      "A large number of small files have accumulated in this table, increasing metadata overhead and slowing downstream reads.",
    aiAnalysis:
      "File sizes in this table's most recent partitions fall well below the optimal range, which increases open-file overhead on every read. A compaction pass is expected to reduce file count and improve both storage efficiency and read latency.",
    recommendedAction:
      "Review the proposed optimization, validate expected impact, and request approval before any controlled change is executed.",
    risk: "Low",
    rollbackAvailable: true,
  },
  {
    id: "OPT-1042",
    title: "Redundant join broadcast",
    domain: "Query",
    resource: "Order Fulfillment",
    impactMonthly: 18.6,
    severity: "Low",
    status: "Review",
    detectedAt: "2026-09-14",
    whyFlagged:
      "A recurring join is broadcasting a larger-than-optimal table on every execution, increasing shuffle and memory pressure.",
    aiAnalysis:
      "The broadcast threshold configured for this workload is larger than the recommended guideline for the observed data size, causing unnecessary network and memory overhead on each run.",
    recommendedAction:
      "Review the proposed optimization, validate expected impact, and request approval before any controlled change is executed.",
    risk: "Low",
    rollbackAvailable: true,
  },
  {
    id: "OPT-1047",
    title: "Over-provisioned warehouse",
    domain: "Cluster",
    resource: "Reporting Warehouse",
    impactMonthly: 27.8,
    severity: "Medium",
    status: "Review",
    detectedAt: "2026-09-13",
    whyFlagged:
      "This warehouse is sized for peak concurrency that occurs only a few hours per week, with idle spend the remainder of the time.",
    aiAnalysis:
      "Concurrency on this warehouse rarely approaches provisioned capacity. A scheduled resize aligned with the observed weekly pattern is expected to reduce cost with minimal impact to peak-hour reporting.",
    recommendedAction:
      "Review the proposed optimization, validate expected impact, and request approval before any controlled change is executed.",
    risk: "Medium",
    rollbackAvailable: true,
  },
  {
    id: "OPT-1051",
    title: "Unpartitioned large table",
    domain: "Storage",
    resource: "event_logs",
    impactMonthly: 33.5,
    severity: "High",
    status: "Review",
    detectedAt: "2026-09-12",
    whyFlagged:
      "This table has grown past the size where a full scan is economical, but has no partitioning strategy applied.",
    aiAnalysis:
      "Nearly every downstream query against this table filters by event date, but the table is not partitioned on that column. Partitioning is expected to substantially reduce scanned bytes across dependent workloads.",
    recommendedAction:
      "Review the proposed optimization, validate expected impact, and request approval before any controlled change is executed.",
    risk: "Medium",
    rollbackAvailable: true,
  },
];

export const demoOverview: OverviewData = {
  userName: "Hema",
  kpis: {
    monthlyCost: 2840,
    potentialSavings: 718,
    openOpportunities: 24,
    optimizationHealth: 92,
  },
  health: [
    { domain: "Query", score: 92, label: "Healthy" },
    { domain: "Cluster", score: 87, label: "Attention" },
    { domain: "Storage", score: 94, label: "Healthy" },
  ],
  lastAnalysisMinutesAgo: 12,
  platformConnected: "Databricks",
  priorityOpportunities: demoOpportunities.slice(0, 3),
  hasData: true,
};

// ----------------------------------------------------------------------------
// Agent simulation
// ----------------------------------------------------------------------------

const routeKeywords: { keywords: string[]; route: AgentRoute }[] = [
  { keywords: ["everything", "all", "full platform", "whole platform"], route: "All Capabilities" },
  { keywords: ["cluster", "compute", "warehouse", "utilization"], route: "Cluster Agent" },
  { keywords: ["storage", "file", "table size", "partition"], route: "Storage Agent" },
  { keywords: ["query", "sql", "scan", "join", "performance"], route: "Query Agent" },
];

export function resolveAgentRoute(prompt: string): AgentRoute {
  const lower = prompt.toLowerCase();
  for (const entry of routeKeywords) {
    if (entry.keywords.some((k) => lower.includes(k))) return entry.route;
  }
  return "Query Agent";
}

function stepsForRoute(route: AgentRoute): AgentStep[] {
  const capability =
    route === "All Capabilities" ? "All optimization capabilities selected" : `${route.replace(" Agent", "")} optimization capability selected`;

  return [
    { id: "s1", label: "Understanding request", status: "pending" },
    { id: "s2", label: capability, status: "pending" },
    { id: "s3", label: "Running optimization analysis", status: "pending" },
    { id: "s4", label: "Identifying optimization opportunities", status: "pending" },
  ];
}

const routeOutcomes: Record<AgentRoute, { opportunitiesFound: number; potentialSavings: number }> = {
  "Query Agent": { opportunitiesFound: 18, potentialSavings: 87.94 },
  "Cluster Agent": { opportunitiesFound: 11, potentialSavings: 59.2 },
  "Storage Agent": { opportunitiesFound: 9, potentialSavings: 55.6 },
  "All Capabilities": { opportunitiesFound: 38, potentialSavings: 202.74 },
};

export function buildAgentRun(prompt: string) {
  const route = resolveAgentRoute(prompt);
  return {
    route,
    steps: stepsForRoute(route),
    outcome: routeOutcomes[route],
  };
}

export const suggestedPrompts: string[] = [
  "Analyze my SQL workloads",
  "Find storage cost opportunities",
  "Check my cluster utilization",
  "Analyze everything",
];

// ----------------------------------------------------------------------------
// Approvals
// ----------------------------------------------------------------------------

export const demoApprovals: ApprovalRequest[] = [
  {
    id: "APR-501",
    opportunityId: "OPT-1024",
    title: "Query Optimization — High scan workload",
    domain: "Query",
    requestedBy: "ACELO AI",
    potentialSavings: 45.2,
    risk: "Medium",
    rollbackAvailable: true,
    checklist: [
      { label: "Technical validation passed", passed: true },
      { label: "Data safety validation passed", passed: true },
      { label: "Pre-execution checkpoint available", passed: true },
    ],
  },
  {
    id: "APR-502",
    opportunityId: "OPT-1031",
    title: "Cluster Optimization — Idle compute capacity",
    domain: "Cluster",
    requestedBy: "ACELO AI",
    potentialSavings: 31.4,
    risk: "Low",
    rollbackAvailable: true,
    checklist: [
      { label: "Technical validation passed", passed: true },
      { label: "Data safety validation passed", passed: true },
      { label: "Pre-execution checkpoint available", passed: true },
    ],
  },
  {
    id: "APR-503",
    opportunityId: "OPT-1038",
    title: "Storage Optimization — Small file accumulation",
    domain: "Storage",
    requestedBy: "ACELO AI",
    potentialSavings: 22.1,
    risk: "Low",
    rollbackAvailable: true,
    checklist: [
      { label: "Technical validation passed", passed: true },
      { label: "Data safety validation passed", passed: false },
      { label: "Pre-execution checkpoint available", passed: true },
    ],
  },
];

// ----------------------------------------------------------------------------
// Execution
// ----------------------------------------------------------------------------

export const demoExecution: ExecutionState = {
  id: "EXE-901",
  opportunityId: "OPT-1024",
  title: "Query Optimization — OPT-1024",
  resource: "Customer Transactions",
  stage: "Execution",
  progressPercent: 68,
  workloadsProcessed: 6596,
  workloadsTotal: 9700,
  elapsedSeconds: 161,
  status: "idle",
};

export const demoExecutionResult: ExecutionResult = {
  id: "OPT-1024",
  title: "Query Optimization — OPT-1024",
  costBefore: 315.11,
  costAfter: 227.17,
  reductionPercent: 27.9,
  estimatedSavingsMonthly: 87.94,
  validations: [
    "Query execution validated",
    "Results validated",
    "No critical errors",
    "Optimization completed",
  ],
};

// ----------------------------------------------------------------------------
// History / Audit
// ----------------------------------------------------------------------------

export const demoAuditEntries: AuditEntry[] = [
  {
    id: "AUD-2001",
    date: "17 Sep 2026",
    userRequest: "Analyze SQL workloads",
    agentRoute: "Query Agent",
    action: "Recommend",
    result: "Completed",
    detail: {
      agentDecision: "Routed to Query Agent based on intent classification of the request.",
      analysis: "Identified 18 optimization opportunities across scan-heavy and join-heavy workloads.",
      recommendation: "Recommended partition pruning for the Customer Transactions workload.",
      approval: "Pending human review before execution.",
      execution: "Not started.",
      validation: "N/A — awaiting approval.",
      rollback: "N/A",
    },
  },
  {
    id: "AUD-2000",
    date: "16 Sep 2026",
    userRequest: "Optimize storage",
    agentRoute: "Storage Agent",
    action: "Analyze",
    result: "Pending",
    detail: {
      agentDecision: "Routed to Storage Agent based on intent classification of the request.",
      analysis: "Scan of storage layout in progress across monitored tables.",
      recommendation: "Not yet available — analysis in progress.",
      approval: "N/A",
      execution: "N/A",
      validation: "N/A",
      rollback: "N/A",
    },
  },
  {
    id: "AUD-1999",
    date: "15 Sep 2026",
    userRequest: "Check compute utilization",
    agentRoute: "Cluster Agent",
    action: "Execute",
    result: "Completed",
    detail: {
      agentDecision: "Routed to Cluster Agent based on intent classification of the request.",
      analysis: "Detected idle capacity on Analytics Compute outside business hours.",
      recommendation: "Recommended an autoscaling policy tuned to observed usage.",
      approval: "Approved by Hema on 15 Sep 2026.",
      execution: "Autoscaling policy applied successfully.",
      validation: "Post-execution validation passed with no critical errors.",
      rollback: "Available — not used.",
    },
  },
  {
    id: "AUD-1998",
    date: "14 Sep 2026",
    userRequest: "Rollback optimization",
    agentRoute: "Query Agent",
    action: "Rollback",
    result: "Completed",
    detail: {
      agentDecision: "Manual rollback request routed to Query Agent for the affected workload.",
      analysis: "Rollback validated against the stored pre-optimization checkpoint.",
      recommendation: "N/A — rollback of a previously executed change.",
      approval: "Approved by Hema on 14 Sep 2026.",
      execution: "Reverted to pre-optimization query plan.",
      validation: "Workload behavior confirmed to match pre-change baseline.",
      rollback: "Completed successfully.",
    },
  },
];
