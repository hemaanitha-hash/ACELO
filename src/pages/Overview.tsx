import React, { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { DollarSign, TrendingDown, ListChecks, Sparkles, CheckCircle2 } from "lucide-react";
import Layout from "../components/Layout";
import PageHeader from "../components/PageHeader";
import { PLATFORM_LABELS, type ActivePlatformId } from "../services/platformContext";
import KpiCard from "../components/KpiCard";
import HealthCard from "../components/HealthCard";
import OpportunityTable from "../components/OpportunityTable";
import Button from "../components/Button";
import { getOverview } from "../services/api";
import DatabricksOverview from "../components/DatabricksOverview";
import { isDatabricksOnly } from "../services/experience";
import type { OverviewData } from "../types";

const quickActions = [
  { label: "Analyze Cluster", prompt: "Check my cluster utilization" },
  { label: "Find Unhealthy Queries", prompt: "Find unhealthy queries that can be optimized" },
];

/** A known amount, or "Not available" — never a fabricated 0. */
function amount(value: number | null | undefined): string {
  return value === null || value === undefined
    ? "Not available"
    : `$${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}

function count(value: number | null | undefined): string {
  return value === null || value === undefined ? "Not available" : String(value);
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs text-ink-faint">{label}</dt>
      <dd className="tabular text-lg font-semibold text-ink">{value}</dd>
    </div>
  );
}

export default function Overview() {
  const navigate = useNavigate();
  const [data, setData] = useState<OverviewData | null>(null);
  const [loading, setLoading] = useState(true);
  async function load() {
    setLoading(true);
    setData(await getOverview());
    setLoading(false);
  }

  useEffect(() => {
    load();
  }, []);

  // The Databricks-only MVP shows the connected workspace and its compute,
  // not the legacy multi-platform cost dashboard.
  if (isDatabricksOnly()) {
    return (
      <Layout pageName="Overview" onRefresh={load}>
        <div className="flex flex-col gap-6">
          <PageHeader
            eyebrow="Databricks"
            title="Overview"
            description="The Databricks workspace ACELO is running in, and the compute it can see."
            action={
              <Button icon={<Sparkles size={16} />} onClick={() => navigate("/agent")}>
                Ask ACELO
              </Button>
            }
          />
          <DatabricksOverview />
        </div>
      </Layout>
    );
  }

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
            {/* The backend returns the platform id ("databricks"); show the
                name the rest of the UI uses. */}
            {data?.platformConnected
              ? `${PLATFORM_LABELS[data.platformConnected as ActivePlatformId] ?? data.platformConnected} connected`
              : "No platform connected"}
          </span>
        </div>

        <div className="grid grid-cols-2 gap-4 lg:grid-cols-3">
          <KpiCard
            label="Monthly Platform Cost"
            value={loading ? "—" : amount(data?.kpis.monthlyCost)}
            icon={DollarSign}
          />
          <KpiCard
            label="Potential Savings"
            value={loading ? "—" : amount(data?.kpis.potentialSavings)}
            icon={TrendingDown}
            tone="positive"
          />
          <KpiCard
            label="Open Opportunities"
            value={loading ? "—" : count(data?.kpis.openOpportunities)}
            icon={ListChecks}
          />
        </div>

        {/* CLUSTER and QUERY are separate: cluster is reporting only (no approvals),
            query goes through the Approval Center. Their counts never mix. */}
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <section className="surface p-5" data-testid="cluster-metrics">
            <div className="flex items-start justify-between gap-3">
              <div>
                <h2 className="text-sm font-semibold text-ink">Cluster optimization</h2>
                <p className="mt-1 text-xs text-ink-muted">Latest recommendation per cluster · reporting only</p>
              </div>
              <button onClick={() => navigate("/results")} className="text-xs text-[#D71920] hover:underline">
                View results
              </button>
            </div>
            <dl className="mt-4 grid grid-cols-2 gap-4 sm:grid-cols-3">
              <Stat label="Clusters analyzed" value={loading ? "—" : count(data?.cluster?.clustersAnalyzed)} />
              <Stat label="Risky" value={loading ? "—" : count(data?.cluster?.risky)} />
              <Stat label="Moderately optimized" value={loading ? "—" : count(data?.cluster?.moderatelyOptimized)} />
              <Stat label="Optimized" value={loading ? "—" : count(data?.cluster?.optimized)} />
              <Stat label="Potential savings" value={loading ? "—" : amount(data?.cluster?.potentialSavings)} />
              <Stat
                label="Latest cluster run"
                value={loading ? "—" : data?.cluster?.latestRun?.status ?? "No runs yet"}
              />
            </dl>
            {data?.cluster?.latestRun?.platform_run_id && (
              <p className="mt-3 break-all text-[11px] text-ink-faint">
                {/* Platform-neutral: this panel is shown for whichever platform
                    is active, so naming Fabric here was wrong half the time. */}
                Last run ID: <span className="font-mono">{data.cluster.latestRun.platform_run_id}</span>
              </p>
            )}
          </section>

          <section className="surface p-5" data-testid="query-metrics">
            <div className="flex items-start justify-between gap-3">
              <div>
                <h2 className="text-sm font-semibold text-ink">Query optimization</h2>
                <p className="mt-1 text-xs text-ink-muted">Validated optimizations · approved in ACELO</p>
              </div>
              <button onClick={() => navigate("/approvals")} className="text-xs text-[#D71920] hover:underline">
                Open approvals
              </button>
            </div>
            <dl className="mt-4 grid grid-cols-2 gap-4 sm:grid-cols-4">
              <Stat label="Unhealthy queries" value={loading ? "—" : count(data?.query?.unhealthyQueries)} />
              <Stat
                label="Optimization opportunities"
                value={loading ? "—" : count(data?.query?.optimizationOpportunities)}
              />
              <Stat label="Potential savings" value={loading ? "—" : amount(data?.query?.potentialSavings)} />
              <Stat label="Latest query run" value={loading ? "—" : data?.query?.latestRun?.status ?? "No runs yet"} />
            </dl>
            <div className="mt-4 grid grid-cols-3 gap-2 sm:grid-cols-5">
              {(
                [
                  ["PENDING", "Pending"],
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
                  className="rounded-sm border border-panel-border px-2 py-2 text-left hover:border-[#D71920]/40"
                >
                  <span className="block text-[11px] text-ink-faint">{label}</span>
                  <span className="tabular text-base font-semibold text-ink">
                    {loading ? "—" : data?.query?.byStatus ? String(data.query.byStatus[status] ?? 0) : "Not available"}
                  </span>
                </button>
              ))}
            </div>
          </section>
        </div>

        <section className="surface p-5" data-testid="execution-metrics">
          <h2 className="text-sm font-semibold text-ink">Executions</h2>
          <p className="mt-1 text-xs text-ink-muted">Every optimization run, as recorded in Run History.</p>
          <dl className="mt-4 grid grid-cols-2 gap-4 sm:grid-cols-4">
            {(
              [
                ["total", "Runs"],
                ["succeeded", "Succeeded"],
                ["failed", "Failed"],
                ["active", "Active"],
              ] as const
            ).map(([key, label]) => (
              <Stat key={key} label={label} value={loading ? "—" : count(data?.executions?.[key])} />
            ))}
          </dl>
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
