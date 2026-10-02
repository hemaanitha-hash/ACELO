import React, { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Cpu, FileText, ShieldCheck, Sparkles, TriangleAlert } from "lucide-react";
import Layout from "../components/Layout";
import PageHeader from "../components/PageHeader";
import Button from "../components/Button";
import DatabricksAgentResult from "../components/DatabricksAgentResult";
import { AnalysisFailure, AnalysisProgress, AnalysisSummary } from "../components/AnalysisStatus";
import {
  EmptyState,
  ErrorState,
  LifecyclePill,
  LoadingState,
  MetricCard,
  SectionCard,
  StatusPill,
  WorkflowIndicator,
  formatDateTime,
  humanizeKey,
  severityTone,
  titleCase,
} from "../components/ui";
import { RunStateBadge } from "./History";
import { AgentApiError } from "../services/databricksAgentApi";
import {
  getComputeAnalysis,
  runComputeAnalysis,
  subscribeComputeAnalysis,
  type ComputeAnalysisState,
} from "../services/computeAnalysis";
import { listRuns, type RunSummary } from "../services/runsApi";
import { parseMaybeJson } from "../services/stage1Api";
import { useRecommendationLifecycle } from "../hooks/useRecommendationLifecycle";

/**
 * Compute Optimization.
 *
 * Opening this page never starts an analysis: with a registered cluster
 * notebook every analysis is a real Databricks run. The page shows the latest
 * persisted recommendations and the latest analysis run, and starts a new
 * analysis only when the user asks for one. The analysis is shared with the AI
 * Agent, so the two can never start a second run while one is in flight.
 */
export default function ComputeOptimization() {
  const navigate = useNavigate();
  const [analysis, setAnalysis] = useState<ComputeAnalysisState>(getComputeAnalysis());
  const lifecycle = useRecommendationLifecycle();
  const [latestRun, setLatestRun] = useState<RunSummary | null>(null);
  const [runsError, setRunsError] = useState(false);

  useEffect(() => subscribeComputeAnalysis(setAnalysis), []);

  async function loadLatestRun() {
    try {
      const body = await listRuns({ domain: "cluster", limit: 1 });
      setLatestRun(body.runs[0] ?? null);
      setRunsError(false);
    } catch {
      setRunsError(true);
    }
  }

  useEffect(() => {
    void loadLatestRun();
  }, []);

  async function analyze() {
    try {
      await runComputeAnalysis();
    } catch (e: unknown) {
      if (!(e instanceof AgentApiError)) throw e;
    } finally {
      // The analysis persists recommendations and records a run; show them.
      void lifecycle.reload();
      void loadLatestRun();
    }
  }

  function refresh() {
    void lifecycle.reload();
    void loadLatestRun();
  }

  const result = analysis.result;
  const views = lifecycle.views;
  const clusters = new Set(views.map((v) => v.recommendation.resource_id ?? v.recommendation.resource)).size;
  const high = views.filter((v) => (v.recommendation.severity ?? "").toUpperCase() === "HIGH").length;
  const pending = views.filter((v) => v.lifecycle === "PENDING").length;
  const hasAnything = views.length > 0 || !!latestRun || !!result;

  return (
    <Layout pageName="Compute" onRefresh={refresh}>
      <div className="flex flex-col gap-6">
        <PageHeader
          eyebrow="Optimization"
          title="Compute Optimization"
          description="ACELO runs the Databricks cluster optimization analysis — cluster configuration together with node utilization, instance events, billing usage and job task history — and turns its findings into recommendations for approval."
          action={
            <Button icon={<Sparkles size={16} />} onClick={() => void analyze()} disabled={!!analysis.running}>
              {analysis.running ? "Analysis running…" : "Analyze Compute"}
            </Button>
          }
        />
        <WorkflowIndicator current="analyze" />

        {analysis.running && <AnalysisProgress startedAt={analysis.startedAt} />}

        {!analysis.running && analysis.requestError && <AnalysisFailure requestError={analysis.requestError} />}

        {!analysis.running && result && !result.ok && (
          <AnalysisFailure status={result.status} message={result.message} />
        )}

        {!analysis.running && result?.ok && <AnalysisSummary result={result} />}

        {lifecycle.loading && views.length === 0 && <LoadingState title="Loading compute recommendations…" />}

        {lifecycle.error && (
          <ErrorState
            title="Could not load compute recommendations"
            detail={lifecycle.error}
            technical={lifecycle.technical}
            action={
              <Button variant="secondary" onClick={() => void lifecycle.reload()}>
                Try again
              </Button>
            }
          />
        )}

        {!lifecycle.loading && !lifecycle.error && !hasAnything && !analysis.running && (
          <EmptyState
            title="No compute analysis is available yet."
            detail="Run an analysis to evaluate the clusters in this Databricks environment. ACELO submits the analysis to Databricks, waits for it to finish and creates recommendations from its findings."
            action={
              <Button icon={<Sparkles size={16} />} onClick={() => void analyze()}>
                Analyze Compute
              </Button>
            }
          />
        )}

        {!lifecycle.error && hasAnything && (
          <>
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <MetricCard label="Recommendations" value={lifecycle.loading ? "—" : views.length} icon={FileText} emphasis to="/recommendations" />
              <MetricCard label="Clusters with findings" value={lifecycle.loading ? "—" : clusters} icon={Cpu} />
              <MetricCard label="High severity" value={lifecycle.loading ? "—" : high} icon={TriangleAlert} />
              <MetricCard label="Pending approval" value={lifecycle.loading ? "—" : pending} icon={ShieldCheck} to="/approvals?status=PENDING" />
            </div>

            <SectionCard
              title="Latest analysis run"
              description="The most recent compute analysis recorded in Run History."
            >
              {latestRun ? (
                <div className="flex flex-wrap items-center gap-x-8 gap-y-3 text-sm">
                  <RunStateBadge state={latestRun.status} />
                  <span className="text-ink-muted">
                    Started <span className="text-ink">{formatDateTime(latestRun.started_at ?? latestRun.created_at)}</span>
                  </span>
                  {latestRun.platform_run_id && (
                    <span className="text-ink-muted">
                      Databricks run <span className="font-mono text-ink">{latestRun.platform_run_id}</span>
                    </span>
                  )}
                  <button
                    type="button"
                    onClick={() => navigate(`/runs/${latestRun.acelo_run_id}`)}
                    className="font-medium text-brand-600 hover:underline"
                  >
                    View run details
                  </button>
                </div>
              ) : (
                <p className="text-sm text-ink-muted">
                  {runsError ? "Run History could not be loaded." : "No compute analysis run has been recorded yet."}
                </p>
              )}
            </SectionCard>

            <SectionCard
              title="Findings and recommendations"
              description="Persisted recommendations from the compute analysis, with the evidence behind each."
              action={
                <Button variant="secondary" onClick={() => navigate("/recommendations")}>
                  Open Recommendations
                </Button>
              }
              flush
            >
              {views.length === 0 ? (
                <p className="px-5 py-8 text-center text-sm text-ink-muted">
                  {lifecycle.loading ? "Loading…" : "The latest analysis produced no recommendations."}
                </p>
              ) : (
                <ul className="divide-y divide-panel-border">
                  {views.map(({ recommendation: r, lifecycle: state }) => {
                    const evidence = parseMaybeJson(r.details?.evidence);
                    const entries =
                      evidence && typeof evidence === "object" && !Array.isArray(evidence)
                        ? Object.entries(evidence as Record<string, unknown>).slice(0, 4)
                        : [];
                    return (
                      <li key={r.recommendation_id} className="px-5 py-4">
                        <div className="flex flex-wrap items-start justify-between gap-3">
                          <div className="min-w-0">
                            <p className="text-sm font-semibold text-ink">{r.title ?? r.optimization_label ?? "Recommendation"}</p>
                            <p className="mt-0.5 text-xs text-ink-muted">
                              {r.resource ?? "Cluster"}
                              {r.resource_id && r.resource_id !== r.resource && (
                                <span className="ml-1 font-mono text-ink-faint">{r.resource_id}</span>
                              )}
                              {r.rule_id && <span className="ml-2 font-mono text-ink-faint">{r.rule_id}</span>}
                            </p>
                          </div>
                          <div className="flex flex-wrap gap-2">
                            {r.severity && <StatusPill tone={severityTone(r.severity)}>{titleCase(r.severity)} severity</StatusPill>}
                            <LifecyclePill lifecycle={state} />
                          </div>
                        </div>
                        {entries.length > 0 && (
                          <dl className="mt-3 flex flex-wrap gap-x-6 gap-y-1">
                            {entries.map(([key, value]) => (
                              <div key={key} className="text-xs">
                                <dt className="inline text-ink-faint">{humanizeKey(key)}: </dt>
                                <dd className="inline font-mono text-ink">
                                  {typeof value === "object" ? JSON.stringify(value) : String(value)}
                                </dd>
                              </div>
                            ))}
                          </dl>
                        )}
                        <button
                          type="button"
                          onClick={() => navigate(`/recommendations?highlight=${encodeURIComponent(r.recommendation_id)}`)}
                          className="mt-3 text-xs font-medium text-brand-600 hover:underline"
                        >
                          View recommendation
                        </button>
                      </li>
                    );
                  })}
                </ul>
              )}
            </SectionCard>
          </>
        )}

        {result && (
          <details className="card px-5 py-4">
            <summary className="cursor-pointer text-sm font-semibold text-ink">Analysis details from this session</summary>
            <div className="mt-4">
              <DatabricksAgentResult result={result} showOutcome={false} />
            </div>
          </details>
        )}
      </div>
    </Layout>
  );
}
