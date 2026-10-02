import React, { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { ArrowRight, Send, Sparkles } from "lucide-react";
import Layout from "../components/Layout";
import PageHeader from "../components/PageHeader";
import Button from "../components/Button";
import { StatePanel } from "../components/StateBlock";
import OpportunityTable from "../components/OpportunityTable";
import { AnalysisFailure } from "../components/AnalysisStatus";
import {
  EmptyState,
  ErrorState,
  KeyValueGrid,
  LifecyclePill,
  LoadingState,
  Notice,
  StatusPill,
  WorkflowIndicator,
  formatDateTime,
  severityTone,
  titleCase,
} from "../components/ui";
import { getOptimizations } from "../services/api";
import type { Opportunity } from "../types";
import { isDatabricksOnly } from "../services/experience";
import type { AgentOpportunity, Stage1Recommendation } from "../services/databricksAgentApi";
import {
  getComputeAnalysis,
  subscribeComputeAnalysis,
  type ComputeAnalysisState,
} from "../services/computeAnalysis";
import {
  LIFECYCLE_LABELS,
  RequestError,
  lifecycleOf,
  parseMaybeJson,
  requestApproval,
  type Lifecycle,
  type PersistedRecommendation,
  type RecommendationView,
} from "../services/stage1Api";
import { useRecommendationLifecycle } from "../hooks/useRecommendationLifecycle";

/**
 * Recommendations — persisted, evidence-backed recommendations and where each
 * one is in the ACELO lifecycle:
 *
 *   Open -> Send for Approval -> Pending Approval -> Approved (Ready to Execute)
 *
 * Opening this page never starts an analysis. Recommendations come from the
 * backend (GET /api/optimizations, kind "stage1") and their state from the
 * persisted approval records, so a browser refresh shows exactly what the
 * backend holds. After "Send to Approval" the page reloads that state rather
 * than editing it locally.
 */

const TYPE_LABELS: Record<string, string> = {
  CLASSIC_CLUSTER: "Classic cluster",
  SERVERLESS_COMPUTE: "Serverless compute",
  SQL_WAREHOUSE: "SQL warehouse",
};

type Filter = "ALL" | Lifecycle;

const FILTERS: Filter[] = ["ALL", "OPEN", "PENDING", "READY_TO_EXECUTE", "REJECTED"];

export default function Recommendations() {
  if (!isDatabricksOnly()) return <LegacyRecommendations />;
  return <DatabricksRecommendations />;
}

// --- Databricks -------------------------------------------------------------

/**
 * A recommendation the current session's analysis produced. It is persisted by
 * the backend in the same request, so normally the persisted list already has
 * it; this only covers the moment before that list is reloaded.
 */
function fromSession(rec: Stage1Recommendation): PersistedRecommendation {
  return {
    id: rec.recommendation_id,
    recommendation_id: rec.recommendation_id,
    kind: "stage1",
    resource: rec.resource_name,
    resource_id: rec.resource_id,
    domain: "cluster",
    stage1_domain: rec.domain,
    environment_id: rec.environment_id,
    title: rec.title,
    description: rec.summary,
    optimization_label: rec.finding_type,
    finding_id: rec.finding_id,
    rule_id: rec.rule_id,
    severity: rec.severity,
    confidence: rec.confidence,
    risk: rec.risk,
    created_at: rec.created_at,
    updated_at: rec.updated_at,
    details: { ...(rec as unknown as Record<string, unknown>), resource_name: rec.resource_name },
  };
}

function DatabricksRecommendations() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const lifecycle = useRecommendationLifecycle();
  const [analysis, setAnalysis] = useState<ComputeAnalysisState>(getComputeAnalysis());
  const [sending, setSending] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ tone: "success" | "danger"; text: string; technical?: string | null } | null>(null);
  const highlight = params.get("highlight");
  const filterParam = (params.get("filter") ?? "ALL").toUpperCase() as Filter;
  const filter: Filter = FILTERS.includes(filterParam) ? filterParam : "ALL";

  useEffect(() => subscribeComputeAnalysis(setAnalysis), []);

  // A finished analysis persisted new recommendations: show them.
  const lastAnalyzed = analysis.analyzedAt?.getTime();
  const firstRender = useRef(true);
  useEffect(() => {
    if (firstRender.current) {
      firstRender.current = false;
      return;
    }
    if (lastAnalyzed) void lifecycle.reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lastAnalyzed]);

  const session = analysis.result;
  const sessionRecommendations =
    (session?.ok ? session.analysis?.compute_optimization?.recommendations : undefined) ?? [];

  const views: RecommendationView[] = useMemo(() => {
    const persistedIds = new Set(lifecycle.views.map((v) => v.recommendation.recommendation_id));
    const extra = sessionRecommendations
      .filter((rec) => rec?.recommendation_id && !persistedIds.has(rec.recommendation_id))
      .map((rec) => ({
        recommendation: fromSession(rec),
        approval: null,
        lifecycle: lifecycleOf(rec.approval_status, rec.execution_status),
      }));
    return [...lifecycle.views, ...extra];
  }, [lifecycle.views, sessionRecommendations]);

  const counts = useMemo(() => {
    const c: Record<Filter, number> = {
      ALL: views.length,
      OPEN: 0,
      PENDING: 0,
      REJECTED: 0,
      READY_TO_EXECUTE: 0,
      EXECUTING: 0,
      EXECUTED: 0,
      EXECUTION_FAILED: 0,
    };
    for (const v of views) c[v.lifecycle] += 1;
    return c;
  }, [views]);

  const visible = filter === "ALL" ? views : views.filter((v) => v.lifecycle === filter);
  const observations: AgentOpportunity[] = (session?.ok ? session.analysis?.opportunities : undefined) ?? [];
  const limitations: string[] = (session?.ok ? session.analysis?.missing_evidence : undefined) ?? [];

  // Scroll a linked recommendation into view once it is on screen.
  useEffect(() => {
    if (!highlight) return;
    document.getElementById(`rec-${highlight}`)?.scrollIntoView?.({ behavior: "smooth", block: "center" });
  }, [highlight, views.length]);

  async function send(recommendationId: string) {
    setSending(recommendationId);
    setNotice(null);
    try {
      await requestApproval(recommendationId);
      setNotice({ tone: "success", text: "Sent for approval. It now appears in Approvals as Pending." });
    } catch (e: unknown) {
      setNotice({
        tone: "danger",
        text: e instanceof RequestError ? e.message : "Could not send for approval.",
        technical: e instanceof RequestError ? e.technical : null,
      });
    } finally {
      setSending(null);
      // Always show what the backend now holds, success or not.
      await lifecycle.reload();
    }
  }

  function setFilter(next: Filter) {
    const p = new URLSearchParams(params);
    if (next === "ALL") p.delete("filter");
    else p.set("filter", next);
    setParams(p, { replace: true });
  }

  const nothing = !lifecycle.loading && views.length === 0 && observations.length === 0;

  return (
    <Layout pageName="Recommendations" onRefresh={() => void lifecycle.reload()}>
      <div className="flex flex-col gap-6">
        <PageHeader
          eyebrow="Compute Optimization"
          title="Recommendations"
          description="Evidence-backed recommendations from the Databricks compute analysis. Each one needs approval before it can move to Execution — approval never executes anything by itself."
          action={
            <Button icon={<Sparkles size={16} />} variant="secondary" onClick={() => navigate("/compute")}>
              Compute analysis
            </Button>
          }
        />
        <WorkflowIndicator current="recommendations" />

        {session && !session.ok && (
          <AnalysisFailure status={session.status} message={session.message} />
        )}

        {notice && (
          <Notice tone={notice.tone} testId="recommendation-notice">
            {notice.text}
            {notice.technical && <span className="mt-1 block font-mono text-[11px] opacity-80">{notice.technical}</span>}
          </Notice>
        )}

        {lifecycle.error && (
          <ErrorState
            title="Could not load recommendations"
            detail={lifecycle.error}
            technical={lifecycle.technical}
            action={
              <Button variant="secondary" onClick={() => void lifecycle.reload()}>
                Try again
              </Button>
            }
          />
        )}

        {lifecycle.loading && views.length === 0 && <LoadingState title="Loading recommendations…" />}

        {nothing && !lifecycle.error && (
          <EmptyState
            title="No recommendations"
            detail={
              session?.ok
                ? "The latest analysis raised no recommendations — which is not a finding that the workspace is optimally configured."
                : "No recommendations are available yet. Run a compute analysis to evaluate this Databricks environment."
            }
            action={
              <Button icon={<Sparkles size={16} />} onClick={() => navigate("/compute")}>
                Go to Compute analysis
              </Button>
            }
          />
        )}

        {views.length > 0 && (
          <section className="flex flex-col gap-4" aria-label="Compute recommendations">
            <div className="flex flex-wrap items-end justify-between gap-3">
              <h2 className="text-sm font-semibold text-ink">Compute recommendations</h2>
              <div role="tablist" aria-label="Filter by status" className="flex flex-wrap gap-1.5">
                {FILTERS.map((f) => (
                  <button
                    key={f}
                    type="button"
                    role="tab"
                    aria-selected={filter === f}
                    onClick={() => setFilter(f)}
                    className={`rounded-full border px-3 py-1 text-xs font-medium transition-colors ${
                      filter === f
                        ? "border-brand-500 bg-brand-500 text-white"
                        : "border-panel-border bg-panel text-ink-muted hover:text-ink"
                    }`}
                  >
                    {f === "ALL" ? "All" : LIFECYCLE_LABELS[f]} <span className="tabular opacity-80">{counts[f]}</span>
                  </button>
                ))}
              </div>
            </div>
            {visible.length === 0 ? (
              <p className="card px-5 py-8 text-center text-sm text-ink-muted">
                No recommendations are {LIFECYCLE_LABELS[filter as Lifecycle]?.toLowerCase() ?? "in this view"}.
              </p>
            ) : (
              <ol className="flex flex-col gap-4">
                {visible.map((view) => (
                  <RecommendationCard
                    key={view.recommendation.recommendation_id}
                    view={view}
                    highlighted={highlight === view.recommendation.recommendation_id}
                    sending={sending === view.recommendation.recommendation_id}
                    disabled={sending !== null}
                    onSend={() => void send(view.recommendation.recommendation_id)}
                  />
                ))}
              </ol>
            )}
          </section>
        )}

        {observations.length > 0 && (
          <section className="flex flex-col gap-3" aria-label="Configuration observations">
            <div>
              <h2 className="text-sm font-semibold text-ink">Configuration Observations</h2>
              <p className="mt-1 text-xs text-ink-muted">
                From the configuration discovery in this session's analysis. Each states the evidence it needs before
                anyone acts.
              </p>
            </div>
            <ol className="flex flex-col gap-4">
              {observations.map((item, index) => (
                <ObservationItem key={`${item.resource_id}:${index}`} item={item} index={index + 1} />
              ))}
            </ol>
          </section>
        )}

        {limitations.length > 0 && (
          <section className="card overflow-hidden">
            <header className="px-5 py-4">
              <h2 className="text-sm font-semibold text-ink">Limitations</h2>
              <p className="mt-1 text-xs text-ink-faint">Signals the configuration discovery step did not observe.</p>
            </header>
            <ul className="border-t border-panel-border px-5 py-3">
              {limitations.map((line) => (
                <li key={line} className="text-xs text-ink-muted">
                  • {line}
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>
    </Layout>
  );
}

function describeSavings(value: unknown): string {
  const v = parseMaybeJson(value) as { status?: string; estimated?: unknown; measured?: unknown } | null;
  if (v && typeof v === "object") {
    if (typeof v.measured === "number") return `Measured: ${v.measured}`;
    if (typeof v.estimated === "number") return `Estimated: ${v.estimated}`;
  }
  return "Not available. No savings estimate or measurement has been produced for this recommendation.";
}

function describeImpact(value: unknown): string | null {
  const v = parseMaybeJson(value);
  if (!v) return null;
  if (typeof v === "string") return v;
  if (typeof v === "object" && typeof (v as { description?: unknown }).description === "string") {
    return (v as { description: string }).description;
  }
  return null;
}

function RecommendationCard({
  view,
  highlighted,
  sending,
  disabled,
  onSend,
}: {
  view: RecommendationView;
  highlighted: boolean;
  sending: boolean;
  disabled: boolean;
  onSend: () => void;
}) {
  const navigate = useNavigate();
  const { recommendation: r, approval, lifecycle } = view;
  const d = r.details ?? {};
  const proposed = parseMaybeJson(d.proposed_state) as { direction?: unknown; reason?: unknown } | string | null;
  const direction =
    proposed && typeof proposed === "object" ? proposed.direction : typeof proposed === "string" ? proposed : null;
  const reason = proposed && typeof proposed === "object" ? proposed.reason : null;
  const observed = parseMaybeJson(d.observation_window) as { start?: string | null; end?: string | null } | null;
  const impact = describeImpact(d.expected_impact);

  return (
    <li
      id={`rec-${r.recommendation_id}`}
      data-testid="recommendation-card"
      className={`card overflow-hidden ${highlighted ? "ring-2 ring-brand-300" : ""}`}
    >
      <header className="flex flex-wrap items-start justify-between gap-3 px-5 py-4">
        <div className="min-w-0">
          <p className="text-base font-semibold text-ink">{r.title ?? r.optimization_label ?? "Recommendation"}</p>
          <p className="mt-1 text-xs text-ink-muted">
            <span className="font-medium text-ink">{r.resource ?? d.resource_name ?? "Cluster"}</span>
            {r.resource_id && r.resource_id !== r.resource && <span className="ml-1.5 font-mono">{r.resource_id}</span>}
            {d.resource_type && <span className="ml-1.5">· {TYPE_LABELS[d.resource_type] ?? d.resource_type}</span>}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          {r.severity && <StatusPill tone={severityTone(r.severity)}>{titleCase(r.severity)} severity</StatusPill>}
          {r.risk && <StatusPill tone="neutral">{titleCase(r.risk)} risk</StatusPill>}
          <LifecyclePill lifecycle={lifecycle} />
        </div>
      </header>

      <div className="grid gap-5 border-t border-panel-border px-5 py-4 lg:grid-cols-2">
        <div className="flex flex-col gap-4">
          {(r.description ?? d.summary) && <p className="text-sm text-ink">{r.description ?? d.summary}</p>}
          <dl className="grid gap-3 text-xs">
            <div>
              <dt className="text-ink-faint">Recommended action</dt>
              <dd className="mt-0.5 font-medium text-ink">{direction ? String(direction) : "Not available"}</dd>
              {reason ? <dd className="mt-0.5 text-ink-muted">{String(reason)}</dd> : null}
            </div>
            {impact && (
              <div>
                <dt className="text-ink-faint">Expected impact</dt>
                <dd className="mt-0.5 text-ink-muted">{impact}</dd>
              </div>
            )}
            <div>
              <dt className="text-ink-faint">Estimated savings</dt>
              <dd className="mt-0.5 text-ink-muted">{describeSavings(d.estimated_savings)}</dd>
            </div>
            {r.rule_id && (
              <div>
                <dt className="text-ink-faint">Rule</dt>
                <dd className="mt-0.5 font-mono text-ink-muted">{r.rule_id}</dd>
              </div>
            )}
          </dl>
        </div>
        <div>
          <p className="text-xs font-medium text-ink-faint">Evidence</p>
          <div className="mt-2 rounded-sm border border-panel-border bg-canvas-raised p-3">
            <KeyValueGrid data={parseMaybeJson(d.evidence)} empty="No evidence fields were recorded." />
          </div>
          <p className="mt-2 text-[11px] text-ink-faint">
            {observed?.start || observed?.end
              ? `Observed ${formatDateTime(observed?.start)} – ${formatDateTime(observed?.end)} · `
              : ""}
            Created {formatDateTime(r.created_at)}
          </p>
        </div>
      </div>

      <footer className="flex flex-wrap items-center justify-between gap-3 border-t border-panel-border bg-canvas-raised px-5 py-3">
        <p className="text-xs text-ink-muted">
          {lifecycle === "OPEN" && "Not yet sent for approval."}
          {lifecycle === "PENDING" && `Waiting for a reviewer${approval?.created_at ? ` since ${formatDateTime(approval.created_at)}` : ""}.`}
          {lifecycle === "READY_TO_EXECUTE" &&
            `Approved${approval?.decided_by ? ` by ${approval.decided_by}` : ""}${
              approval?.decided_at ? ` on ${formatDateTime(approval.decided_at)}` : ""
            }. Ready to Execute.`}
          {lifecycle === "REJECTED" &&
            `Rejected${approval?.decided_by ? ` by ${approval.decided_by}` : ""}${
              approval?.decided_at ? ` on ${formatDateTime(approval.decided_at)}` : ""
            }.`}
          {(lifecycle === "EXECUTING" || lifecycle === "EXECUTED" || lifecycle === "EXECUTION_FAILED") &&
            `Execution status: ${LIFECYCLE_LABELS[lifecycle]}.`}
        </p>
        {(lifecycle === "OPEN" || lifecycle === "REJECTED") && (
          <button type="button" className="btn-primary" onClick={onSend} disabled={disabled}>
            <Send size={15} aria-hidden="true" />
            {sending ? "Sending…" : lifecycle === "REJECTED" ? "Send to Approval again" : "Send to Approval"}
          </button>
        )}
        {lifecycle === "PENDING" && (
          <button type="button" className="btn-secondary" onClick={() => navigate("/approvals?status=PENDING")}>
            Open Approvals <ArrowRight size={15} aria-hidden="true" />
          </button>
        )}
        {(lifecycle === "READY_TO_EXECUTE" || lifecycle === "EXECUTING" || lifecycle === "EXECUTED" || lifecycle === "EXECUTION_FAILED") && (
          <button type="button" className="btn-secondary" onClick={() => navigate("/execution")}>
            Open Execution <ArrowRight size={15} aria-hidden="true" />
          </button>
        )}
      </footer>
    </li>
  );
}

function ObservationItem({ item, index }: { item: AgentOpportunity; index: number }) {
  const rows: { label: string; value: string; mono?: boolean }[] = [
    { label: "Finding", value: item.potential_issue },
    { label: "Observed evidence", value: item.observed_evidence, mono: true },
    { label: "Recommendation", value: item.recommendation },
    {
      label: "Rationale",
      value: `Raised because ${item.observed_evidence} was observed on this resource, which indicates: ${item.potential_issue}`,
    },
    { label: "Evidence required before action", value: item.evidence_required },
    { label: "Expected impact", value: item.expected_impact },
  ];

  return (
    <li className="card overflow-hidden">
      <header className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2 px-5 py-4">
        <div className="min-w-0">
          <p className="text-sm font-medium text-ink">
            {index}. {item.resource}
          </p>
          <p className="mt-0.5 text-xs text-ink-faint">
            {TYPE_LABELS[item.resource_type] ?? item.resource_type}
            {item.resource_id ? ` · ${item.resource_id}` : ""}
          </p>
        </div>
        <StatusPill tone="info">Potential Optimization Opportunity</StatusPill>
      </header>
      <dl className="border-t border-panel-border px-5 py-3">
        {rows.map((row) => (
          <div key={row.label} className="flex flex-col gap-0.5 py-1 sm:flex-row sm:gap-3">
            <dt className="shrink-0 text-xs text-ink-faint sm:w-56">{row.label}</dt>
            <dd className={`text-xs text-ink-muted ${row.mono ? "font-mono" : ""}`}>{row.value}</dd>
          </div>
        ))}
      </dl>
    </li>
  );
}

// --- Legacy multi-platform experience (unchanged) ----------------------------

function LegacyRecommendations() {
  const [opportunities, setOpportunities] = useState<Opportunity[]>([]);
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    const result = await getOptimizations();
    setOpportunities(result);
    setLoading(false);
  }

  useEffect(() => {
    load();
  }, []);

  const pending = opportunities.filter((o) => o.status === "Review");

  return (
    <Layout pageName="Recommendations" onRefresh={load}>
      <div className="flex flex-col gap-6">
        <PageHeader
          title="Recommendations"
          description="AI-generated recommendations awaiting review before approval."
        />

        {loading ? (
          <StatePanel
            kind="loading"
            title="Loading recommendations…"
          />
        ) : (
          <OpportunityTable
            opportunities={pending}
            emptyLabel="Every recommendation has moved past review."
          />
        )}
      </div>
    </Layout>
  );
}
