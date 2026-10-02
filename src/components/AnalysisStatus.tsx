import React, { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Check, Circle, Loader2 } from "lucide-react";
import { getActiveRuns, type RunSummary } from "../services/runsApi";
import type { AgentAnalysisResult } from "../services/databricksAgentApi";
import { ErrorState, MetricCard, Notice, TechnicalDetails } from "./ui";

/**
 * Live progress and final outcome of a Databricks compute analysis.
 *
 * The analysis request itself answers only when it is finished, so progress is
 * read from the ACELO run the backend records for it (GET /api/runs/active):
 * the run appears when the Databricks run is submitted, reports RUNNING while
 * the notebook works, and leaves the active list when Databricks finishes.
 * No stage is advanced on a timer.
 */

const POLL_MS = 4000;

type Stage = "preparing" | "submitting" | "running" | "collecting";

const STEPS: { id: Stage | "building" | "done"; label: string }[] = [
  { id: "preparing", label: "Preparing analysis" },
  { id: "submitting", label: "Submitting Databricks analysis" },
  { id: "running", label: "Running optimization analysis" },
  { id: "collecting", label: "Collecting results" },
  { id: "building", label: "Building recommendations" },
  { id: "done", label: "Analysis completed" },
];

function stageOf(run: RunSummary | null, seenRun: boolean): Stage {
  if (!run) return seenRun ? "collecting" : "preparing";
  if (run.status === "RUNNING") return "running";
  return "submitting";
}

function elapsed(from: Date, now: number): string {
  const seconds = Math.max(0, Math.floor((now - from.getTime()) / 1000));
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m ? `${m}m ${String(s).padStart(2, "0")}s` : `${s}s`;
}

export function AnalysisProgress({ startedAt: startedAtProp }: { startedAt?: Date | null }) {
  // Fixed for the life of this view, so re-renders never restart polling.
  const fallback = useRef(new Date());
  const startedAt = startedAtProp ?? fallback.current;
  const startedMs = startedAt.getTime();
  const [run, setRun] = useState<RunSummary | null>(null);
  const [lastRun, setLastRun] = useState<RunSummary | null>(null);
  const seen = useRef(false);
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const tick = async () => {
      try {
        const body = await getActiveRuns();
        if (cancelled) return;
        // The cluster run started by THIS analysis: created after it began
        // (with a small allowance for clock difference).
        const mine =
          body.runs.find(
            (r) =>
              r.domain === "cluster" &&
              new Date(r.created_at ?? r.started_at ?? 0).getTime() >= startedMs - 60_000,
          ) ?? null;
        if (mine) {
          seen.current = true;
          setLastRun(mine);
        }
        setRun(mine);
      } catch {
        /* progress is best-effort; the analysis request reports the outcome */
      }
      if (!cancelled) timer = window.setTimeout(tick, POLL_MS);
    };
    void tick();
    const clock = window.setInterval(() => setNow(Date.now()), 1000);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      window.clearInterval(clock);
    };
  }, [startedMs]);

  const stage = stageOf(run, seen.current);
  const currentIndex = STEPS.findIndex((s) => s.id === stage);
  const reference = run ?? lastRun;

  return (
    <div className="card p-5" aria-live="polite" data-testid="analysis-progress">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="flex items-center gap-2 text-sm font-semibold text-ink">
            <Loader2 size={15} className="animate-spin text-brand-500" aria-hidden="true" />
            {STEPS[currentIndex].label}…
          </p>
          <p className="mt-1 text-xs text-ink-muted">
            ACELO runs the Databricks optimization analysis and waits for it to finish. This usually takes several
            minutes; you can leave this page — the run continues and appears in Run History.
          </p>
        </div>
        <span className="tabular rounded-full border border-panel-border bg-canvas-raised px-2.5 py-0.5 text-xs text-ink-muted">
          Elapsed {elapsed(startedAt, now)}
        </span>
      </div>

      <ol className="mt-5 grid gap-2 sm:grid-cols-3 lg:grid-cols-6">
        {STEPS.map((step, i) => {
          const state = i < currentIndex ? "done" : i === currentIndex ? "current" : "upcoming";
          return (
            <li
              key={step.id}
              className={`flex items-center gap-2 rounded-sm border px-3 py-2 text-xs ${
                state === "current"
                  ? "border-brand-300 bg-brand-50 font-medium text-brand-700"
                  : state === "done"
                    ? "border-panel-border bg-panel text-ink"
                    : "border-panel-border bg-canvas-raised text-ink-faint"
              }`}
            >
              {state === "done" ? (
                <Check size={13} className="text-signal-low" aria-hidden="true" />
              ) : state === "current" ? (
                <Loader2 size={13} className="animate-spin" aria-hidden="true" />
              ) : (
                <Circle size={7} className="fill-current" aria-hidden="true" />
              )}
              {step.label}
            </li>
          );
        })}
      </ol>

      {reference && (
        <p className="mt-4 text-xs text-ink-muted">
          {reference.platform_run_id ? (
            <>
              Databricks run <span className="font-mono text-ink">{reference.platform_run_id}</span> ·{" "}
            </>
          ) : null}
          ACELO run <span className="font-mono">{reference.acelo_run_id}</span> ·{" "}
          <Link to={`/runs/${reference.acelo_run_id}`} className="font-medium text-brand-600 hover:underline">
            View run details
          </Link>
        </p>
      )}
    </div>
  );
}

// --- Outcome ----------------------------------------------------------------

const FAILURE_TEXT: Record<string, string> = {
  NOTEBOOK_RUN_NOT_STARTED: "The Databricks analysis run could not be started.",
  NOTEBOOK_RUN_TIMEOUT:
    "The Databricks analysis did not finish within the time ACELO waits. It may still be running — check Run History.",
  NOTEBOOK_RUN_FAILED: "The Databricks analysis run did not complete successfully.",
  NOTEBOOK_RESULTS_UNAVAILABLE: "The analysis finished, but its results could not be read.",
  NOTEBOOK_RESULTS_STALE:
    "The analysis finished, but its results could not be confirmed as produced by this run, so none were used.",
  AUTHENTICATION_FAILED: "ACELO could not authenticate to the Databricks environment.",
  AUTHORIZATION_FAILED: "The ACELO App identity is not permitted to perform this analysis.",
  NOT_CONFIGURED: "The ACELO Environment is not fully configured for this analysis.",
  DISCOVERY_FAILED: "ACELO could not read the Databricks compute resources.",
};

/** A failed analysis: plain-language heading, raw status in the details. */
export function AnalysisFailure({
  status,
  message,
  requestError,
}: {
  status?: string | null;
  message?: string | null;
  requestError?: string | null;
}) {
  const detail = requestError
    ? requestError
    : (status && FAILURE_TEXT[status]) ?? "The Databricks analysis returned an error.";
  return (
    <ErrorState
      title="Analysis could not be completed"
      detail={
        <>
          {detail} Existing recommendations are unchanged.
        </>
      }
      technical={
        status || message ? (
          <>
            {status && <code className="block">{status}</code>}
            {message && <span className="mt-1 block">{message}</span>}
          </>
        ) : undefined
      }
    />
  );
}

/** A successful analysis: what the backend reports it produced. */
export function AnalysisSummary({ result }: { result: AgentAnalysisResult }) {
  const optimization = result.analysis?.compute_optimization as
    | {
        summary?: Record<string, unknown>;
        execution?: { acelo_run_id?: string; platform_run_id?: string };
        recommendations?: unknown[];
      }
    | undefined;
  if (!optimization) return null;
  const summary = optimization.summary ?? {};
  const num = (v: unknown) => (typeof v === "number" ? String(v) : "Not available");
  const rejected = summary.rejected_notebook_rows;
  return (
    <div className="flex flex-col gap-4" data-testid="analysis-summary">
      <Notice tone="success">
        Analysis completed.{" "}
        {typeof summary.recommendations_created_or_updated === "number"
          ? `${summary.recommendations_created_or_updated} recommendation${
              summary.recommendations_created_or_updated === 1 ? "" : "s"
            } created or updated.`
          : ""}
      </Notice>
      <div className="grid gap-4 sm:grid-cols-3">
        <MetricCard label="Clusters with findings" value={num(summary.clusters_analyzed)} />
        <MetricCard label="Actionable findings" value={num(summary.actionable_findings)} />
        <MetricCard label="Recommendations" value={num(summary.recommendations_created_or_updated)} emphasis />
      </div>
      {(optimization.execution?.platform_run_id || optimization.execution?.acelo_run_id) && (
        <p className="text-xs text-ink-muted">
          {optimization.execution?.platform_run_id && (
            <>
              Databricks run <span className="font-mono text-ink">{optimization.execution.platform_run_id}</span>
            </>
          )}
          {optimization.execution?.acelo_run_id && (
            <>
              {" "}
              ·{" "}
              <Link to={`/runs/${optimization.execution.acelo_run_id}`} className="font-medium text-brand-600 hover:underline">
                View run details
              </Link>
            </>
          )}
        </p>
      )}
      {!!rejected && typeof rejected === "object" && Object.keys(rejected as object).length > 0 && (
        <TechnicalDetails>
          Notebook rows not used: <code>{JSON.stringify(rejected)}</code>
        </TechnicalDetails>
      )}
    </div>
  );
}
