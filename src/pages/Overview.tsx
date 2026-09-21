import React, { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { DollarSign, TrendingDown, ListChecks, Activity, Sparkles, CheckCircle2 } from "lucide-react";
import Layout from "../components/Layout";
import KpiCard from "../components/KpiCard";
import HealthCard from "../components/HealthCard";
import OpportunityTable from "../components/OpportunityTable";
import Button from "../components/Button";
import { getOverview } from "../services/api";
import { getApprovalSummary, type ApprovalSummary } from "../services/approvalsApi";
import type { OverviewData } from "../types";

const quickActions = [
  { label: "Analyze Queries", prompt: "Analyze my SQL workloads" },
  { label: "Analyze Cluster", prompt: "Check my cluster utilization" },
  { label: "Analyze Storage", prompt: "Find storage cost opportunities" },
  { label: "Analyze Everything", prompt: "Analyze everything" },
];

export default function Overview() {
  const navigate = useNavigate();
  const [data, setData] = useState<OverviewData | null>(null);
  const [loading, setLoading] = useState(true);
  // Real approval counts; null when they could not be loaded (never faked as 0).
  const [approvals, setApprovals] = useState<ApprovalSummary | null>(null);

  async function load() {
    setLoading(true);
    const [result, counts] = await Promise.all([
      getOverview(),
      getApprovalSummary().catch(() => null),
    ]);
    setData(result);
    setApprovals(counts);
    setLoading(false);
  }

  useEffect(() => {
    load();
  }, []);

  return (
    <Layout pageName="Overview" onRefresh={load}>
      <div className="flex flex-col gap-8">
        <div className="flex flex-col gap-5 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h1 className="text-display font-semibold text-ink">
              {data?.userName ? `Good morning, ${data.userName}` : "Good morning"}
            </h1>
            <p className="mt-2 max-w-xl text-sm text-ink-muted">
              Monitor, analyze and optimize your data platform with ACELO AI.
            </p>
          </div>
          <Button icon={<Sparkles size={16} />} onClick={() => navigate("/agent")} className="shrink-0">
            Ask ACELO
          </Button>
        </div>

        <div className="flex flex-wrap items-center gap-x-6 gap-y-2 rounded-sm border border-panel-border bg-panel px-5 py-3 text-sm">
          <span className="flex items-center gap-2 text-ink-muted">
            <CheckCircle2 size={15} className="text-brand-400" />
            Last analysis:{" "}
            {data?.lastAnalysisMinutesAgo != null
              ? `${data.lastAnalysisMinutesAgo} minutes ago`
              : "No analysis yet"}
          </span>
          <span className="hidden h-4 w-px bg-panel-border sm:block" />
          <span className="flex items-center gap-2 text-ink-muted">
            <span className="h-1.5 w-1.5 rounded-full bg-brand-400" />
            {data?.platformConnected ? `${data.platformConnected} Connected` : "No platform connected"}
          </span>
        </div>

        <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          <KpiCard
            label="Monthly Platform Cost"
            value={loading ? "—" : `$${data?.kpis.monthlyCost.toLocaleString()}`}
            icon={DollarSign}
          />
          <KpiCard
            label="Potential Savings"
            value={loading ? "—" : `$${data?.kpis.potentialSavings.toLocaleString()}`}
            icon={TrendingDown}
            tone="positive"
          />
          <KpiCard
            label="Open Opportunities"
            value={loading ? "—" : `${data?.kpis.openOpportunities}`}
            icon={ListChecks}
          />
          <KpiCard
            label="Optimization Health"
            value={loading || data?.kpis.optimizationHealth == null ? "—" : `${data.kpis.optimizationHealth}%`}
            icon={Activity}
          />
        </div>

        {/* Approval KPIs from the real approval records. */}
        <section>
          <h2 className="mb-4 text-sm font-semibold text-ink">Approvals</h2>
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
            {(
              [
                ["PENDING", "Pending Approvals"],
                ["APPROVED", "Approved"],
                ["REJECTED", "Rejected"],
                ["EXECUTING", "Executing"],
                ["COMPLETED", "Completed"],
              ] as const
            ).map(([status, label]) => (
              <button
                key={status}
                type="button"
                data-testid={`approval-kpi-${status}`}
                onClick={() => navigate(`/approvals?status=${status}`)}
                className="text-left"
              >
                <KpiCard
                  label={label}
                  value={loading ? "—" : approvals ? String(approvals[status]) : "Not available"}
                  icon={ListChecks}
                />
              </button>
            ))}
          </div>
        </section>

        <section>
          <h2 className="mb-4 text-sm font-semibold text-ink">Optimization health</h2>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
            {data?.health.map((h) => <HealthCard key={h.domain} {...h} />)}
          </div>
        </section>

        <section className="surface p-6 sm:p-8">
          <div className="flex items-start gap-3">
            <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-sm bg-brand-500/15 border border-brand-500/25">
              <Sparkles size={16} className="text-brand-300" />
            </span>
            <div>
              <h2 className="text-base font-semibold text-ink">What should we optimize?</h2>
              <p className="mt-1 text-sm text-ink-muted max-w-xl">
                Ask ACELO in natural language. The agent selects the right
                capability automatically.
              </p>
            </div>
          </div>
          <div className="mt-5 flex flex-wrap gap-2">
            {quickActions.map((action) => (
              <button
                key={action.label}
                onClick={() => navigate("/agent", { state: { prompt: action.prompt } })}
                className="rounded-sm border border-panel-border bg-panel px-3 py-2 text-sm text-ink-muted transition-colors hover:border-panel-borderStrong hover:text-ink hover:bg-panel-hover"
              >
                {action.label}
              </button>
            ))}
          </div>
        </section>

        <section>
          <div className="mb-4 flex items-center justify-between">
            <h2 className="text-sm font-semibold text-ink">Priority opportunities</h2>
            <button
              onClick={() => navigate("/optimizations")}
              className="text-sm text-brand-300 hover:text-brand-200"
            >
              View all
            </button>
          </div>
          <OpportunityTable opportunities={data?.priorityOpportunities ?? []} />
        </section>
      </div>
    </Layout>
  );
}
