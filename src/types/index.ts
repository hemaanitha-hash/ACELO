export type Domain = "Query" | "Cluster" | "Storage";

export type ConnectedPlatform = "Databricks" | "Microsoft Fabric";

export type Severity = "High" | "Medium" | "Low";

export type OpportunityStatus =
  | "Review"
  | "Approved"
  | "In Progress"
  | "Completed"
  | "Rejected";

export interface Opportunity {
  id: string;
  title: string;
  domain: Domain;
  resource: string;
  impactMonthly: number;
  severity: Severity;
  status: OpportunityStatus;
  detectedAt: string;
  whyFlagged: string;
  aiAnalysis: string;
  recommendedAction: string;
  risk: "Low" | "Medium" | "High";
  rollbackAvailable: boolean;
}

export interface DomainHealth {
  domain: Domain;
  /** null when no real analysis has produced a score yet. */
  score: number | null;
  label: "Healthy" | "Attention" | "Critical" | "No data";
}

export interface OverviewKpis {
  monthlyCost: number;
  potentialSavings: number;
  openOpportunities: number;
  /** null until real analysis data exists. Never substituted with a demo value. */
  optimizationHealth: number | null;
}

export interface OverviewData {
  userName: string;
  kpis: OverviewKpis;
  health: DomainHealth[];
  lastAnalysisMinutesAgo: number | null;
  platformConnected: ConnectedPlatform | null;
  priorityOpportunities: Opportunity[];
  /** false when no completed run has produced results; drives the empty state. */
  hasData: boolean;
}

export type AgentRoute = "Query Agent" | "Cluster Agent" | "Storage Agent" | "All Capabilities";

export interface AgentStep {
  id: string;
  label: string;
  status: "pending" | "active" | "done";
}

export interface AgentRunResult {
  route: AgentRoute;
  opportunitiesFound: number;
  potentialSavings: number;
  steps: AgentStep[];
}

export interface ApprovalRequest {
  id: string;
  opportunityId: string;
  title: string;
  domain: Domain;
  requestedBy: string;
  potentialSavings: number;
  risk: "Low" | "Medium" | "High";
  rollbackAvailable: boolean;
  checklist: { label: string; passed: boolean }[];
}

export type ExecutionStage = "Approval" | "Checkpoint" | "Execution" | "Validation";

export interface ExecutionState {
  id: string;
  opportunityId: string;
  title: string;
  resource: string;
  stage: ExecutionStage;
  progressPercent: number;
  workloadsProcessed: number;
  workloadsTotal: number;
  elapsedSeconds: number;
  status: "running" | "completed" | "idle";
}

export interface ExecutionResult {
  id: string;
  title: string;
  costBefore: number;
  costAfter: number;
  reductionPercent: number;
  estimatedSavingsMonthly: number;
  validations: string[];
}

export type AuditAction = "Analyze" | "Recommend" | "Approve" | "Execute" | "Rollback";
export type AuditResult = "Completed" | "Pending" | "Failed";

export interface AuditEntry {
  id: string;
  date: string;
  userRequest: string;
  agentRoute: AgentRoute;
  action: AuditAction;
  result: AuditResult;
  detail: {
    agentDecision: string;
    analysis: string;
    recommendation: string;
    approval: string;
    execution: string;
    validation: string;
    rollback: string;
  };
}
