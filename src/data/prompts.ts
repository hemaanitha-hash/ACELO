/**
 * Suggested requests shown in the AI Agent. Prompt text only — never data.
 * Each one routes to exactly one optimization (cluster OR query), never both.
 */
export const suggestedPrompts: string[] = [
  "Analyze my compute environment",
  "Find idle compute resources",
  "Identify compute optimization opportunities",
  "Show me my compute resources",
  "Generate recommendations",
];

/**
 * The legacy multi-platform prompts. Kept for the non-Databricks experience,
 * where Query and Storage optimizations are also available.
 */
export const legacyPrompts: string[] = [
  "Check my cluster utilization",
  "Analyze my clusters",
  "Find unhealthy queries that can be optimized",
  "Show query optimization opportunities",
  "Show me pending query approvals",
];
