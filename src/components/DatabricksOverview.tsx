import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  Bot,
  CheckCircle2,
  CircleDollarSign,
  Clock,
  Cpu,
  FileText,
  Loader2,
  Play,
  Search,
  ServerCog,
  ShieldCheck,
  Sparkles,
  TrendingDown,
  XCircle,
} from "lucide-react";
import Button from "./Button";
import { MetricCard, SectionCard, StatusPill, TechnicalDetails, formatDateTime, type PillTone } from "./ui";
import { RunStateBadge } from "../pages/History";
import {
  DatabricksApiError,
  getDatabricksResources,
  type DatabricksResources,
} from "../services/databricksApi";
import { getRuntime } from "../services/runtime";
import { getActiveRuns, listRuns, type RunSummary } from "../services/runsApi";
import { getOptimizationResources, type DomainResourceStatus } from "../services/environmentApi";
import { getComputeAnalysis, subscribeComputeAnalysis, type ComputeAnalysisState } from "../services/computeAnalysis";
import { requestJson, RequestError, type RecommendationView } from "../services/stage1Api";
import { useRecommendationLifecycle } from "../hooks/useRecommendationLifecycle";

/**
 * Optimization Control Center — the Databricks App Overview.
 *
 * Every value comes from an existing API:
 *   GET /api/health                                   backend reachable
 *   GET /api/runtime (via getRuntime)                 Databricks App runtime
 *   GET /api/databricks/resources                     environment + discovered compute
 *   GET /api/environments/{id}/optimization-resources configured cluster notebook/job/tables
 *   GET /api/optimizations?domain=stage1              persisted recommendations
 *   GET /api/approvals/recommendations                persisted approval records
 *   GET /api/runs, /api/runs/active                   analysis runs
 *   GET /api/overview                                 cost / savings, when the backend has them
 *
 * Nothing here starts an analysis. A value the backend does not provide is
 * labelled "Not available yet" / "Not available" — never zero, never inferred.
 */

const NOT_YET = "Not available yet";
const NA = "Not available";

type Load<T> = { state: "loading" } | { state: "ok"; data: T } | { state: "error"; message: string; technical?: string | null };

interface OverviewCosts {
  kpis?: { monthlyCost?: number | null; potentialSavings?: number | null };
}

function useControlCenter() {
  const [health, setHealth] = useState<Load<true>>({ state: "loading" });
  const [resources, setResources] = useState<Load<DatabricksResources>>({ state: "loading" });
  const [activeRuns, setActiveRuns] = useState<Load<RunSummary[]>>({ state: "loading" });
  const [runs, setRuns] = useState<Load<RunSummary[]>>({ state: "loading" });
  const [costs, setCosts] = useState<Load<OverviewCosts>>({ state: "loading" });
  const [cluster, setCluster] = useState<Load<DomainResourceStatus | null>>({ state: "loading" });
  const lifecycle = useRecommendationLifecycle();

  const load = useCallback(async () => {
    setHealth({ state: "loading" });
    try {
      await requestJson("/health", "The ACELO backend did not respond");
      setHealth({ state: "ok", data: true });
    } catch (e: unknown) {
      setHealth({
        state: "error",
        message: e instanceof RequestError ? e.message : "The ACELO backend did not respond.",
        technical: e instanceof RequestError ? e.technical ?? `status ${e.status}` : null,
      });
    }

    getActiveRuns()
      .then((b) => setActiveRuns({ state: "ok", data: b.runs }))
      .catch((e: Error) => setActiveRuns({ state: "error", message: e.message }));
    listRuns({ limit: 10 })
      .then((b) => setRuns({ state: "ok", data: b.runs }))
      .catch((e: Error) => setRuns({ state: "error", message: e.message }));
    requestJson<OverviewCosts>("/overview", "Could not load cost figures")
      .then((d) => setCosts({ state: "ok", data: d }))
      .catch((e: Error) => setCosts({ state: "error", message: e.message }));

    try {
      const data = await getDatabricksResources();
      setResources({ state: "ok", data });
      if (data.environment_id) {
        getOptimizationResources(data.environment_id)
          .then((r) => setCluster({ state: "ok", data: r.domains.find((d) => d.domain === "cluster") ?? null }))
          .catch((e: Error) => setCluster({ state: "error", message: e.message }));
      } else {
        setCluster({ state: "ok", data: null });
      }
    } catch (e: unknown) {
      setResources({
        state: "error",
        message: e instanceof DatabricksApiError ? e.message : "Databricks discovery failed.",
      });
      setCluster({ state: "error", message: "The environment could not be resolved." });
    }
    void lifecycle.reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return { health, resources, activeRuns, runs, costs, cluster, lifecycle, reload: load };
}

// --- helpers ------------------------------------------------------------------

function money(value: number | null | undefined): string {
  return typeof value === "number" ? `$${value.toLocaleString(undefined, { maximumFractionDigits: 0 })}` : NOT_YET;
}

function latestClusterRun(runs: RunSummary[]): RunSummary | null {
  return runs.find((r) => r.domain === "cluster") ?? null;
}

type SystemState = "checking" | "healthy" | "attention" | "backend_down";

// --- page ---------------------------------------------------------------------

export default function DatabricksOverview() {
  const navigate = useNavigate();
  const runtime = getRuntime();
  const { health, resources, activeRuns, runs, costs, cluster, lifecycle, reload } = useControlCenter();
  const [analysis, setAnalysis] = useState<ComputeAnalysisState>(getComputeAnalysis());
  useEffect(() => subscribeComputeAnalysis(setAnalysis), []);

  const views = lifecycle.views;
  const res = resources.state === "ok" ? resources.data : null;
  const runList = runs.state === "ok" ? runs.data : [];
  const active = activeRuns.state === "ok" ? activeRuns.data : [];
  const latest = latestClusterRun(runList) ?? runList[0] ?? null;
  const analysisRunning = !!analysis.running || active.some((r) => r.domain === "cluster");
  const latestFailed = latest?.status === "FAILED";

  const counts = useMemo(() => {
    const c = { open: 0, pending: 0, ready: 0, rejected: 0, high: 0 };
    for (const v of views) {
      if (v.lifecycle === "OPEN") c.open += 1;
      if (v.lifecycle === "PENDING") c.pending += 1;
      if (v.lifecycle === "READY_TO_EXECUTE") c.ready += 1;
      if (v.lifecycle === "REJECTED") c.rejected += 1;
      if ((v.recommendation.severity ?? "").toUpperCase() === "HIGH") c.high += 1;
    }
    return c;
  }, [views]);
  const findings = new Set(views.map((v) => v.recommendation.finding_id ?? v.recommendation.recommendation_id)).size;

  const envAvailable = !!res?.connected;
  const system: SystemState =
    health.state === "loading"
      ? "checking"
      : health.state === "error"
        ? "backend_down"
        : resources.state === "loading"
          ? "checking"
          : !envAvailable || latestFailed
            ? "attention"
            : "healthy";

  const lifecycleValue = (n: number) =>
    lifecycle.loading && !views.length ? "—" : lifecycle.error ? NOT_YET : n;
  const costData = costs.state === "ok" ? costs.data.kpis : undefined;

  return (
    <div className="flex flex-col gap-6">
      {/* 1. Header */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div className="min-w-0">
          <p className="label-eyebrow mb-1.5">ACELO / Optimization Center</p>
          <h1 className="text-display font-semibold text-ink">Optimization Control Center</h1>
          <p className="mt-2 max-w-2xl text-sm text-ink-muted">
            Monitor, analyze and optimize your Databricks environment with ACELO.
          </p>
        </div>
        <Button icon={<Sparkles size={16} />} onClick={() => navigate("/agent")} className="shrink-0">
          Ask ACELO
        </Button>
      </div>

      {/* 2. System status bar */}
      <SystemStatusBar
        system={system}
        latest={latest}
        runsLoaded={runs.state !== "loading"}
        runtimeLabel={runtime.databricks_app ? "Running as Databricks App" : "Not running as a Databricks App"}
        workspace={res?.workspace_name ?? runtime.workspace_host?.replace("https://", "") ?? null}
        envAvailable={envAvailable}
        envLoading={resources.state === "loading"}
      />

      {/* Distinct environment states */}
      <EnvironmentStates
        health={health}
        resources={resources}
        analysisRunning={analysisRunning}
        latest={latest}
        noAnalysisYet={runs.state === "ok" && runList.length === 0 && !lifecycle.loading && views.length === 0}
        onRetry={() => void reload()}
      />

      {/* 3. KPI cards */}
      <section aria-label="Key metrics" className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <MetricCard
          label="Compute Resources"
          icon={Cpu}
          value={resources.state === "loading" ? "—" : res ? res.resources.length : NOT_YET}
          helper="Discovered in this environment"
        />
        <MetricCard label="Optimization Findings" icon={Search} value={lifecycleValue(findings)} helper="Distinct findings behind recommendations" to="/compute" />
        <MetricCard label="Recommendations" icon={FileText} value={lifecycleValue(views.length)} helper="Persisted, all states" to="/recommendations" />
        <MetricCard label="Pending Approvals" icon={ShieldCheck} value={lifecycleValue(counts.pending)} to="/approvals?status=PENDING" />
        <MetricCard label="Ready to Execute" icon={Play} value={lifecycleValue(counts.ready)} emphasis helper="Approved, not executed" to="/execution" />
        <MetricCard label="Active Runs" icon={Activity} value={activeRuns.state === "ok" ? active.length : activeRuns.state === "loading" ? "—" : NOT_YET} to="/history" />
        <MetricCard
          label="Latest Analysis"
          icon={Clock}
          value={runs.state === "loading" ? "—" : latest ? <RunStateBadge state={latest.status} /> : NOT_YET}
          helper={latest ? formatDateTime(latest.started_at ?? latest.created_at) : undefined}
          to={latest ? `/runs/${latest.acelo_run_id}` : undefined}
        />
        <div className="grid grid-cols-2 gap-4 sm:col-span-2 lg:col-span-1 lg:grid-cols-1">
          <CompactMetric icon={CircleDollarSign} label="Monthly Platform Cost" value={costs.state === "loading" ? "—" : money(costData?.monthlyCost)} />
          <CompactMetric icon={TrendingDown} label="Potential Savings" value={costs.state === "loading" ? "—" : money(costData?.potentialSavings)} />
        </div>
      </section>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)]">
        {/* 4. Optimization health */}
        <OptimizationHealth views={views} loading={lifecycle.loading} failed={!!lifecycle.error} counts={counts} />
        {/* 5. AI Agent */}
        <AgentPanel running={analysisRunning} />
      </div>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
        {/* 6. Attention */}
        <AttentionList
          counts={counts}
          views={views}
          active={active}
          latest={latest}
          analysisRunning={analysisRunning}
          loading={lifecycle.loading || runs.state === "loading"}
        />
        {/* 7. Runtime card */}
        <RuntimeCard
          runtimeLabel={runtime.databricks_app ? "Running as Databricks App" : "Not running as a Databricks App"}
          appIdentity={runtime.databricks_app && runtime.auth_mode === "databricks_app_identity" ? "Databricks App Identity" : NA}
          agentReady={envAvailable}
          agentLoading={resources.state === "loading"}
          workspace={runtime.workspace_host ?? res?.workspace_name ?? null}
          cluster={cluster}
          latest={latest}
        />
      </div>

      {/* 8. Workflow */}
      <WorkflowStepper />

      {/* 9. Recent activity */}
      <RecentActivity runs={runList} views={views} approvals={lifecycle.approvals} loading={runs.state === "loading" || lifecycle.loading} />
    </div>
  );
}

// --- sections -------------------------------------------------------------------

function SystemStatusBar({
  system,
  latest,
  runsLoaded,
  runtimeLabel,
  workspace,
  envAvailable,
  envLoading,
}: {
  system: SystemState;
  latest: RunSummary | null;
  runsLoaded: boolean;
  runtimeLabel: string;
  workspace: string | null;
  envAvailable: boolean;
  envLoading: boolean;
}) {
  const overall: { label: string; tone: PillTone; icon: typeof CheckCircle2 } =
    system === "healthy"
      ? { label: "System Healthy", tone: "success", icon: CheckCircle2 }
      : system === "attention"
        ? { label: "Needs Attention", tone: "warning", icon: AlertTriangle }
        : system === "backend_down"
          ? { label: "Backend Unavailable", tone: "danger", icon: XCircle }
          : { label: "Checking status…", tone: "neutral", icon: Loader2 };
  const Icon = overall.icon;
  return (
    <section
      aria-label="System status"
      data-testid="system-status"
      className="card grid gap-px overflow-hidden bg-panel-border sm:grid-cols-2 lg:grid-cols-4"
    >
      <StatusCell label="Overall">
        <span className="flex items-center gap-2">
          <Icon size={15} className={system === "checking" ? "animate-spin text-ink-faint" : ""} aria-hidden="true" />
          <StatusPill tone={overall.tone}>{overall.label}</StatusPill>
        </span>
      </StatusCell>
      <StatusCell label="Last analysis">
        {!runsLoaded ? "—" : latest ? (
          <span className="flex flex-wrap items-center gap-2">
            <RunStateBadge state={latest.status} />
            <span className="text-xs text-ink-muted">{formatDateTime(latest.completed_at ?? latest.started_at ?? latest.created_at)}</span>
          </span>
        ) : (
          <span className="text-ink-muted">{NOT_YET}</span>
        )}
      </StatusCell>
      <StatusCell label="Runtime">{runtimeLabel}</StatusCell>
      <StatusCell label="Databricks">
        <span className="block truncate" title={workspace ?? undefined}>
          {workspace ?? NA}
        </span>
        <span className={`text-xs ${envLoading ? "text-ink-faint" : envAvailable ? "text-signal-low" : "text-signal-high"}`}>
          {envLoading ? "Checking…" : envAvailable ? "Environment reachable" : "Environment not reachable"}
        </span>
      </StatusCell>
    </section>
  );
}

function StatusCell({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0 bg-panel px-5 py-3.5">
      <p className="text-[11px] font-medium uppercase tracking-wider text-ink-faint">{label}</p>
      <div className="mt-1 text-sm font-medium text-ink">{children}</div>
    </div>
  );
}

function CompactMetric({ icon: Icon, label, value }: { icon: typeof Cpu; label: string; value: string }) {
  const unavailable = value === NOT_YET;
  return (
    <div className="card flex min-w-0 items-center gap-3 px-4 py-3">
      <Icon size={16} className="shrink-0 text-ink-faint" aria-hidden="true" />
      <div className="min-w-0">
        <p className="truncate text-xs text-ink-muted">{label}</p>
        <p className={unavailable ? "text-xs italic text-ink-faint" : "tabular text-base font-semibold text-ink"}>{value}</p>
      </div>
    </div>
  );
}

function EnvironmentStates({
  health,
  resources,
  analysisRunning,
  latest,
  noAnalysisYet,
  onRetry,
}: {
  health: Load<true>;
  resources: Load<DatabricksResources>;
  analysisRunning: boolean;
  latest: RunSummary | null;
  noAnalysisYet: boolean;
  onRetry: () => void;
}) {
  const navigate = useNavigate();
  if (health.state === "error") {
    return (
      <StateBanner
        tone="danger"
        title="ACELO backend unavailable"
        detail="The ACELO service did not respond, so no status or data can be shown. This is a problem with the ACELO App service itself, not with Databricks."
        action={<Button variant="secondary" onClick={onRetry}>Retry</Button>}
        technical={`${health.message}${health.technical ? ` · ${health.technical}` : ""}`}
      />
    );
  }
  if (resources.state === "error") {
    return (
      <StateBanner
        tone="danger"
        title="Databricks environment unavailable"
        detail="The ACELO backend is running, but it could not read the Databricks environment."
        action={<Button variant="secondary" onClick={onRetry}>Retry</Button>}
        technical={resources.message}
      />
    );
  }
  if (resources.state === "ok" && !resources.data.connected) {
    const statuses = resources.data.statuses.map((s) => `${s.resource_type}: ${s.status}${s.reason ? ` (${s.reason})` : ""}`).join(" · ");
    return (
      <StateBanner
        tone="warning"
        title="Databricks environment unavailable"
        detail="ACELO is running, but its Databricks App identity could not read compute in this environment. Analysis cannot run until access is available."
        action={<Button variant="secondary" onClick={onRetry}>Check again</Button>}
        technical={statuses || "No discovery status was returned."}
      />
    );
  }
  if (analysisRunning) {
    return (
      <StateBanner
        tone="info"
        title="Analysis running"
        detail="A Databricks compute analysis is in progress. Results appear as recommendations when it finishes."
        action={<Button variant="secondary" onClick={() => navigate("/compute")}>View progress</Button>}
      />
    );
  }
  if (latest?.status === "FAILED") {
    return (
      <StateBanner
        tone="danger"
        title="Latest analysis failed"
        detail="The most recent analysis did not complete. Existing recommendations are unchanged."
        action={<Button variant="secondary" onClick={() => navigate(`/runs/${latest.acelo_run_id}`)}>View run</Button>}
        technical={[latest.error_code, latest.error_message].filter(Boolean).join(" · ") || undefined}
      />
    );
  }
  if (resources.state === "ok" && resources.data.connected && resources.data.resources.length === 0) {
    return (
      <StateBanner
        tone="info"
        title="No compute resources discovered"
        detail="The environment is reachable, but no compute resources were returned. Analysis can still run on the configured evidence sources."
        action={<Button variant="secondary" onClick={() => navigate("/compute")}>Open Compute</Button>}
      />
    );
  }
  if (noAnalysisYet) {
    return (
      <StateBanner
        tone="info"
        title="No analysis performed yet"
        detail="Run a compute analysis to produce findings and recommendations. Nothing runs until you ask."
        action={<Button onClick={() => navigate("/compute")}>Analyze Compute</Button>}
      />
    );
  }
  return null;
}

function StateBanner({
  tone,
  title,
  detail,
  action,
  technical,
}: {
  tone: "danger" | "warning" | "info";
  title: string;
  detail: string;
  action?: React.ReactNode;
  technical?: string;
}) {
  const border =
    tone === "danger" ? "border-l-signal-high" : tone === "warning" ? "border-l-signal-medium" : "border-l-signal-info";
  const Icon = tone === "info" ? Loader2 : tone === "warning" ? AlertTriangle : XCircle;
  return (
    <section role={tone === "danger" ? "alert" : "status"} data-testid="environment-state" className={`card border-l-4 ${border} px-5 py-4`}>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex min-w-0 items-start gap-3">
          <Icon
            size={18}
            aria-hidden="true"
            className={`mt-0.5 shrink-0 ${tone === "danger" ? "text-signal-high" : tone === "warning" ? "text-signal-medium" : "animate-none text-signal-info"}`}
          />
          <div className="min-w-0">
            <p className="text-sm font-semibold text-ink">{title}</p>
            <p className="mt-1 text-sm text-ink-muted">{detail}</p>
            {technical && <TechnicalDetails>{technical}</TechnicalDetails>}
          </div>
        </div>
        {action}
      </div>
    </section>
  );
}

const DOMAINS: { id: string; label: string; to: string }[] = [
  { id: "compute", label: "Compute", to: "/compute" },
  { id: "cost", label: "Cost", to: "/optimization/cost" },
  { id: "jobs", label: "Jobs & Pipelines", to: "/optimization/jobs" },
  { id: "sql", label: "SQL", to: "/optimization/sql" },
  { id: "storage", label: "Storage", to: "/optimization/storage" },
];

function OptimizationHealth({
  views,
  loading,
  failed,
  counts,
}: {
  views: RecommendationView[];
  loading: boolean;
  failed: boolean;
  counts: { open: number; pending: number; ready: number; high: number };
}) {
  const unresolved = counts.open + counts.pending;
  return (
    <SectionCard
      title="Optimization Health"
      description="By optimization area. ACELO provides no health score, so none is shown."
    >
      <ul className="flex flex-col gap-3">
        {DOMAINS.map((d) => {
          const compute = d.id === "compute";
          let status: { label: string; tone: PillTone };
          let detail: string;
          if (!compute) {
            status = { label: "Not activated", tone: "neutral" };
            detail = "Analysis for this area is not activated in this ACELO Environment.";
          } else if (loading && !views.length) {
            status = { label: "Loading", tone: "neutral" };
            detail = "Reading recommendations…";
          } else if (failed) {
            status = { label: NOT_YET, tone: "neutral" };
            detail = "Recommendations could not be loaded.";
          } else if (views.length === 0) {
            status = { label: "No findings yet", tone: "neutral" };
            detail = "No compute recommendations have been produced yet.";
          } else if (unresolved > 0) {
            status = { label: "Attention", tone: "warning" };
            detail = `${unresolved} recommendation${unresolved === 1 ? "" : "s"} not yet approved${counts.high ? ` · ${counts.high} high severity` : ""}.`;
          } else {
            status = { label: "Reviewed", tone: "success" };
            detail = "Every recommendation has been reviewed.";
          }
          return (
            <li key={d.id} className="flex items-center justify-between gap-3 rounded-sm border border-panel-border px-4 py-3">
              <div className="min-w-0">
                <Link to={d.to} className={`text-sm font-medium ${compute ? "text-ink hover:text-brand-600" : "text-ink-muted hover:text-ink"}`}>
                  {d.label}
                </Link>
                <p className="mt-0.5 text-xs text-ink-faint">{detail}</p>
              </div>
              <StatusPill tone={status.tone}>{status.label}</StatusPill>
            </li>
          );
        })}
      </ul>
    </SectionCard>
  );
}

function AgentPanel({ running }: { running: boolean }) {
  const navigate = useNavigate();
  return (
    <section className="card relative overflow-hidden" aria-label="AI Agent">
      <div className="absolute inset-y-0 left-0 w-1 bg-brand-500" aria-hidden="true" />
      <div className="flex h-full flex-col gap-5 p-6">
        <div className="flex items-start gap-3">
          <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-sm bg-brand-500 text-white">
            <Bot size={20} aria-hidden="true" />
          </span>
          <div>
            <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-brand-600">AI Agent</p>
            <h2 className="mt-1 text-xl font-semibold text-ink">What should we optimize?</h2>
            <p className="mt-1 max-w-md text-sm text-ink-muted">
              Ask ACELO in natural language. ACELO selects the appropriate optimization capability.
            </p>
          </div>
        </div>
        {running && (
          <p className="flex items-center gap-2 rounded-sm border border-signal-info/30 bg-signal-info/5 px-3 py-2 text-xs text-signal-info">
            <Loader2 size={13} className="animate-spin" aria-hidden="true" /> An analysis is already running — starting another will join it.
          </p>
        )}
        <div className="mt-auto flex flex-wrap gap-2">
          <Button icon={<Cpu size={15} />} onClick={() => navigate("/agent", { state: { prompt: "Analyze my compute environment" } })}>
            Analyze my compute
          </Button>
          <Button
            variant="secondary"
            icon={<Search size={15} />}
            onClick={() => navigate("/agent", { state: { prompt: "Identify compute optimization opportunities" } })}
          >
            Find optimization opportunities
          </Button>
          <Button variant="ghost" icon={<Sparkles size={15} />} onClick={() => navigate("/agent")}>
            Ask ACELO
          </Button>
        </div>
      </div>
    </section>
  );
}

interface AttentionItem {
  key: string;
  tone: PillTone;
  chip: string;
  title: string;
  detail: string;
  resource?: string | null;
  value: React.ReactNode;
  action: { label: string; to: string };
}

function AttentionList({
  counts,
  views,
  active,
  latest,
  analysisRunning,
  loading,
}: {
  counts: { open: number; pending: number; ready: number; high: number };
  views: RecommendationView[];
  active: RunSummary[];
  latest: RunSummary | null;
  analysisRunning: boolean;
  loading: boolean;
}) {
  const navigate = useNavigate();
  const first = (lc: string) => views.find((v) => v.lifecycle === lc)?.recommendation;
  const items: AttentionItem[] = [];
  const pendingRec = first("PENDING");
  if (counts.pending > 0)
    items.push({
      key: "pending", tone: "warning", chip: "Pending approval", title: "Recommendations waiting for review",
      detail: "Approve or reject to move them forward. Approval records authorization only.",
      resource: pendingRec?.resource, value: counts.pending, action: { label: "Review", to: "/approvals?status=PENDING" },
    });
  const readyRec = first("READY_TO_EXECUTE");
  if (counts.ready > 0)
    items.push({
      key: "ready", tone: "brand", chip: "Ready to Execute", title: "Approved recommendations ready for execution",
      detail: "Approved, not executed. Execution happens only when explicitly chosen.",
      resource: readyRec?.resource, value: counts.ready, action: { label: "Open Execution", to: "/execution" },
    });
  const activeRun = active.find((r) => r.domain === "cluster") ?? active[0];
  if (analysisRunning || activeRun)
    items.push({
      key: "active", tone: "info", chip: "Running", title: "Analysis in progress",
      detail: activeRun?.current_stage ?? "Waiting for the Databricks run status.",
      resource: activeRun?.platform_run_id ? `Databricks run ${activeRun.platform_run_id}` : null,
      value: activeRun ? <RunStateBadge state={activeRun.status} /> : "Running",
      action: { label: "View run", to: activeRun ? `/runs/${activeRun.acelo_run_id}` : "/compute" },
    });
  if (latest?.status === "FAILED")
    items.push({
      key: "failed", tone: "danger", chip: "Failed", title: "Latest analysis failed",
      detail: latest.error_message ?? latest.error_code ?? "The run did not complete.",
      resource: latest.platform_run_id ? `Databricks run ${latest.platform_run_id}` : latest.acelo_run_id,
      value: <RunStateBadge state={latest.status} />, action: { label: "View run", to: `/runs/${latest.acelo_run_id}` },
    });
  const openRec = first("OPEN");
  if (counts.open > 0)
    items.push({
      key: "open", tone: counts.high ? "danger" : "neutral", chip: counts.high ? `${counts.high} high severity` : "New findings",
      title: "Recommendations not yet sent for approval",
      detail: "Review the evidence and send them for approval.",
      resource: openRec?.resource, value: counts.open, action: { label: "Review", to: "/recommendations?filter=OPEN" },
    });

  return (
    <SectionCard title="What needs your attention?" description="Built from persisted recommendations, approvals and runs." flush>
      {loading && items.length === 0 ? (
        <p className="px-5 py-8 text-center text-sm text-ink-muted">Loading…</p>
      ) : items.length === 0 ? (
        <p className="px-5 py-8 text-center text-sm text-ink-muted">Nothing needs your attention right now.</p>
      ) : (
        <ul className="divide-y divide-panel-border" data-testid="attention-list">
          {items.map((item) => (
            <li key={item.key} className="flex flex-wrap items-center gap-4 px-5 py-4">
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <StatusPill tone={item.tone}>{item.chip}</StatusPill>
                  <p className="text-sm font-semibold text-ink">{item.title}</p>
                </div>
                <p className="mt-1 text-xs text-ink-muted">{item.detail}</p>
                {item.resource && <p className="mt-1 truncate font-mono text-[11px] text-ink-faint">{item.resource}</p>}
              </div>
              <div className="tabular text-xl font-semibold text-ink">{item.value}</div>
              <button type="button" className="btn-secondary" onClick={() => navigate(item.action.to)}>
                {item.action.label} <ArrowRight size={14} aria-hidden="true" />
              </button>
            </li>
          ))}
        </ul>
      )}
    </SectionCard>
  );
}

/** Never present the legacy reference notebook as the active one. */
function activeNotebook(value: string | null | undefined): string | null {
  if (!value) return null;
  return /legacy_reference/i.test(value) ? null : value;
}

function RuntimeCard({
  runtimeLabel,
  appIdentity,
  agentReady,
  agentLoading,
  workspace,
  cluster,
  latest,
}: {
  runtimeLabel: string;
  appIdentity: string;
  agentReady: boolean;
  agentLoading: boolean;
  workspace: string | null;
  cluster: Load<DomainResourceStatus | null>;
  latest: RunSummary | null;
}) {
  const cfg = cluster.state === "ok" ? cluster.data : null;
  const kind = cfg?.resource?.type as string | undefined;
  const target = kind === "notebook" ? activeNotebook(cfg?.resource?.id) : null;
  const job = kind === "job" ? cfg?.resource?.id ?? null : null;
  const method =
    kind === "job" ? "Databricks Jobs API (run-now)" : kind === "notebook" && target ? "Databricks Jobs API (runs/submit)" : NA;
  const pending = cluster.state === "loading" ? "—" : NA;
  const rows: { label: string; value: React.ReactNode; mono?: boolean }[] = [
    { label: "Runtime", value: runtimeLabel },
    {
      label: "Agent Status",
      value: agentLoading ? "—" : agentReady ? <StatusPill tone="success">Ready</StatusPill> : <StatusPill tone="warning">Needs Attention</StatusPill>,
    },
    { label: "Workspace", value: workspace ?? NA, mono: true },
    { label: "App Identity", value: appIdentity },
    { label: "Optimization Notebook", value: target ?? (job ? `Job ${job}` : pending), mono: true },
    { label: "Execution Method", value: cluster.state === "loading" ? "—" : method },
    { label: "Result Store (configured)", value: cfg?.settings?.result_table ?? pending, mono: true },
    {
      label: "Evidence Sources (configured)",
      value: cfg?.settings?.source_table ? (
        <>
          <span className="block">{cfg.settings.source_table}</span>
          <span className="font-sans text-[11px] italic text-ink-faint">Additional evidence tables: {NA}</span>
        </>
      ) : (
        pending
      ),
      mono: true,
    },
    {
      label: "Latest Run",
      value: latest ? (
        <span className="flex flex-wrap items-center gap-2">
          <RunStateBadge state={latest.status} />
          <Link to={`/runs/${latest.acelo_run_id}`} className="font-mono text-[11px] text-brand-600 hover:underline">
            {latest.platform_run_id ? `Databricks run ${latest.platform_run_id}` : latest.acelo_run_id}
          </Link>
        </span>
      ) : (
        NOT_YET
      ),
    },
  ];
  return (
    <SectionCard
      title={
        <span className="flex items-center gap-2">
          <ServerCog size={15} aria-hidden="true" /> ACELO Runtime
        </span>
      }
      description="Technical details, as reported by the ACELO backend."
    >
      <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-[auto_minmax(0,1fr)]">
        {rows.map((r) => (
          <React.Fragment key={r.label}>
            <dt className="text-xs text-ink-faint sm:pt-0.5">{r.label}</dt>
            <dd className={`min-w-0 break-all text-sm ${r.mono ? "font-mono text-xs" : ""} ${r.value === NA || r.value === NOT_YET ? "italic text-ink-faint" : "text-ink"}`}>
              {r.value}
            </dd>
          </React.Fragment>
        ))}
      </dl>
      {cluster.state === "error" && <TechnicalDetails>{cluster.message}</TechnicalDetails>}
    </SectionCard>
  );
}

const STEPS: { n: number; label: string; note: string; to: string }[] = [
  { n: 1, label: "Analyze", note: "ACELO submits the Databricks analysis and waits for it", to: "/compute" },
  { n: 2, label: "Findings", note: "The analysis writes evidence-backed findings", to: "/compute" },
  { n: 3, label: "Recommendations", note: "ACELO turns findings into recommendations", to: "/recommendations" },
  { n: 4, label: "Approval", note: "A reviewer approves or rejects — authorization only", to: "/approvals" },
  { n: 5, label: "Ready to Execute", note: "Approved, not yet executed", to: "/execution" },
  { n: 6, label: "Execution", note: "Only when explicitly chosen", to: "/execution" },
  { n: 7, label: "History", note: "Every analysis run is recorded", to: "/history" },
];

function WorkflowStepper() {
  return (
    <SectionCard
      title="How ACELO optimizes"
      description="Approval records authorization; it does not mean execution occurred. The current demo phase ends at Ready to Execute."
    >
      <ol className="grid gap-2 sm:grid-cols-2 lg:grid-cols-7" data-testid="workflow-stepper">
        {STEPS.map((s) => {
          const demoEnd = s.n === 5;
          const beyond = s.n === 6;
          return (
            <li key={s.n} className="min-w-0">
              <Link
                to={s.to}
                className={`flex h-full flex-col rounded-sm border px-3 py-3 transition-colors ${
                  demoEnd
                    ? "border-brand-500 bg-brand-50"
                    : beyond
                      ? "border-dashed border-panel-borderStrong bg-canvas-raised"
                      : "border-panel-border bg-panel hover:border-brand-300"
                }`}
              >
                <span
                  className={`flex h-6 w-6 items-center justify-center rounded-full text-xs font-semibold ${
                    demoEnd ? "bg-brand-500 text-white" : beyond ? "bg-panel text-ink-faint" : "bg-brand-50 text-brand-700"
                  }`}
                >
                  {s.n}
                </span>
                <span className={`mt-2 text-sm font-semibold ${beyond ? "text-ink-muted" : "text-ink"}`}>{s.label}</span>
                <span className="mt-1 text-[11px] leading-4 text-ink-muted">{s.note}</span>
                {demoEnd && <span className="mt-2 text-[10px] font-semibold uppercase tracking-wider text-brand-600">Demo phase ends here</span>}
              </Link>
            </li>
          );
        })}
      </ol>
    </SectionCard>
  );
}

interface ActivityEvent {
  at: string;
  label: string;
  detail: string;
  tone: PillTone;
  to: string;
}

function RecentActivity({
  runs,
  views,
  approvals,
  loading,
}: {
  runs: RunSummary[];
  views: RecommendationView[];
  approvals: import("../services/stage1Api").ApprovalRecord[];
  loading: boolean;
}) {
  const events: ActivityEvent[] = [];
  for (const r of runs) {
    const ref = r.platform_run_id ? `Databricks run ${r.platform_run_id}` : `ACELO run ${r.acelo_run_id}`;
    const started = r.started_at ?? r.created_at;
    if (started) events.push({ at: started, label: "Analysis started", detail: `${r.optimization} · ${ref}`, tone: "info", to: `/runs/${r.acelo_run_id}` });
    if (r.completed_at)
      events.push({
        at: r.completed_at,
        label: r.status === "SUCCEEDED" ? "Analysis completed" : r.status === "FAILED" ? "Analysis failed" : `Analysis ${r.status.toLowerCase()}`,
        detail: `${r.optimization} · ${ref}`,
        tone: r.status === "SUCCEEDED" ? "success" : r.status === "FAILED" ? "danger" : "neutral",
        to: `/runs/${r.acelo_run_id}`,
      });
  }
  for (const v of views) {
    if (v.recommendation.created_at)
      events.push({
        at: v.recommendation.created_at,
        label: "Recommendation created",
        detail: `${v.recommendation.title ?? "Recommendation"} · ${v.recommendation.resource ?? ""}`,
        tone: "neutral",
        to: `/recommendations?highlight=${encodeURIComponent(v.recommendation.recommendation_id)}`,
      });
  }
  for (const a of approvals) {
    const name = `${a.recommendation.title ?? "Recommendation"} · ${a.recommendation.resource ?? ""}`;
    if (a.created_at) events.push({ at: a.created_at, label: "Sent for approval", detail: name, tone: "warning", to: "/approvals?status=PENDING" });
    if (a.decided_at && a.status === "approved")
      events.push({ at: a.decided_at, label: "Approved · Ready to Execute", detail: `${name}${a.decided_by ? ` · by ${a.decided_by}` : ""}`, tone: "brand", to: "/execution" });
    if (a.decided_at && a.status === "rejected")
      events.push({ at: a.decided_at, label: "Rejected", detail: `${name}${a.decided_by ? ` · by ${a.decided_by}` : ""}`, tone: "danger", to: "/approvals?status=REJECTED" });
  }
  const recent = events
    .filter((e) => !Number.isNaN(new Date(e.at).getTime()))
    .sort((a, b) => new Date(b.at).getTime() - new Date(a.at).getTime())
    .slice(0, 8);

  return (
    <SectionCard title="Recent Activity" description="Real events from runs, recommendations and approvals." flush>
      {loading && recent.length === 0 ? (
        <p className="px-5 py-6 text-center text-sm text-ink-muted">Loading…</p>
      ) : recent.length === 0 ? (
        <p className="px-5 py-6 text-center text-sm text-ink-muted">No activity recorded yet.</p>
      ) : (
        <ul className="divide-y divide-panel-border" data-testid="recent-activity">
          {recent.map((e, i) => (
            <li key={`${e.at}-${e.label}-${i}`}>
              <Link to={e.to} className="flex flex-wrap items-center gap-x-4 gap-y-1 px-5 py-3 hover:bg-panel-hover">
                <span className="tabular w-40 shrink-0 text-xs text-ink-faint">{formatDateTime(e.at)}</span>
                <StatusPill tone={e.tone}>{e.label}</StatusPill>
                <span className="min-w-0 flex-1 truncate text-xs text-ink-muted">{e.detail}</span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </SectionCard>
  );
}
