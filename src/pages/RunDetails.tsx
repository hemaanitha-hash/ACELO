import React, { useCallback, useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { useMsal } from "@azure/msal-react";
import Layout from "../components/Layout";
import { RunStateBadge } from "./History";
import { useRunMonitor } from "../components/RunMonitor";
import { ApiError } from "../services/environmentApi";
import { getFabricToken, getOneLakeToken, getSilentRunTokens } from "../services/fabricAuth";
import {
  RunDetails as Details,
  RunEvent,
  RunTokens,
  cancelRun,
  formatDuration,
  formatTime,
  getRun,
  getRunEvents,
  isActive,
  rerun,
} from "../services/runsApi";

const POLL_MS = 4000;

function Field({ label, value, mono }: { label: string; value: React.ReactNode; mono?: boolean }) {
  return (
    <div>
      <dt className="text-xs text-ink-faint">{label}</dt>
      <dd className={`mt-0.5 break-all text-sm text-ink ${mono ? "font-mono text-xs" : ""}`}>{value ?? "—"}</dd>
    </div>
  );
}

function levelTone(level: string) {
  if (level === "ERROR") return "text-[#D71920]";
  if (level === "WARNING") return "text-signal-medium";
  if (level === "SUCCESS") return "text-signal-low";
  return "text-ink-muted";
}

export default function RunDetails() {
  const { id = "" } = useParams();
  const { instance } = useMsal();
  const navigate = useNavigate();
  const location = useLocation();
  const { refresh: refreshMonitor } = useRunMonitor();
  const [run, setRun] = useState<Details | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const lastEventAt = useRef<string | null>(null);
  const resultsRef = useRef<HTMLDivElement | null>(null);

  const load = useCallback(async () => {
    try {
      const tokens = await getSilentRunTokens(instance).catch(() => ({ fabric: null, onelake: null }));
      const details = await getRun(id, tokens);
      setRun(details);
      setEvents(details.logs);
      lastEventAt.current = details.logs.length ? details.logs[details.logs.length - 1].timestamp : null;
      setError(null);
    } catch (e: unknown) {
      setError(e instanceof ApiError ? e.message : "Could not load this run.");
    }
  }, [id, instance]);

  useEffect(() => {
    setRun(null);
    setEvents([]);
    void load();
  }, [load]);

  // Live logs: structured polling for events newer than the last one shown.
  useEffect(() => {
    if (!run || !isActive(run.status)) return;
    const handle = window.setInterval(async () => {
      try {
        const body = await getRunEvents(id, lastEventAt.current);
        if (body.events.length) {
          setEvents((cur) => {
            const known = new Set(cur.map((e) => e.id));
            return [...cur, ...body.events.filter((e) => !known.has(e.id))];
          });
          lastEventAt.current = body.events[body.events.length - 1].timestamp;
        }
        if (body.status !== run.status || body.current_stage !== run.current_stage) void load();
      } catch {
        /* next tick retries */
      }
    }, POLL_MS);
    return () => window.clearInterval(handle);
  }, [id, run, load]);

  useEffect(() => {
    if (location.hash === "#results" && run?.result && resultsRef.current) {
      resultsRef.current.scrollIntoView?.({ behavior: "smooth" });
    }
  }, [location.hash, run?.result]);

  async function interactiveTokens(): Promise<RunTokens> {
    const account = instance.getActiveAccount() ?? instance.getAllAccounts()[0];
    if (!account) return {};
    const fabric = await getFabricToken(instance).catch(() => null);
    const onelake = await getOneLakeToken(instance).catch(() => null);
    return { fabric, onelake, userName: account.name ?? account.username };
  }

  async function handleCancel() {
    if (!run) return;
    setBusy(true);
    setActionError(null);
    try {
      await cancelRun(run.acelo_run_id, await interactiveTokens());
      await load();
      void refreshMonitor();
    } catch (e: unknown) {
      setActionError(e instanceof ApiError ? e.message : "Could not request cancellation.");
    } finally {
      setBusy(false);
    }
  }

  async function handleRerun() {
    if (!run) return;
    setBusy(true);
    setActionError(null);
    try {
      const created = await rerun(run.acelo_run_id, await interactiveTokens());
      void refreshMonitor();
      navigate(`/runs/${created.acelo_run_id}`);
    } catch (e: unknown) {
      setActionError(e instanceof ApiError ? e.message : "Could not start a re-run.");
    } finally {
      setBusy(false);
    }
  }

  if (error && !run) {
    return (
      <Layout pageName="Run Details">
        <div className="surface px-4 py-10 text-center text-sm text-[#D71920]">{error}</div>
      </Layout>
    );
  }
  if (!run) {
    return (
      <Layout pageName="Run Details">
        <div className="surface py-16 text-center text-sm text-ink-muted">Loading run...</div>
      </Layout>
    );
  }

  const rows = Array.isArray(run.result?.rows) ? (run.result!.rows as Record<string, unknown>[]) : [];
  const columns = rows.length ? Object.keys(rows[0]).slice(0, 10) : [];
  const timeline = events.filter((e) => e.event_type !== "LOG");

  return (
    <Layout pageName="Run Details" onRefresh={() => void load()}>
      <div className="flex flex-col gap-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <Link to="/history" className="text-xs text-ink-muted hover:underline">
              ← Run history
            </Link>
            <h1 className="mt-1 flex items-center gap-3 text-display font-semibold text-ink">
              {run.optimization} <RunStateBadge state={run.status} />
            </h1>
            <p className="mt-1 text-sm text-ink-muted">
              {run.current_stage ?? (isActive(run.status) ? "Waiting for platform status" : "")}
            </p>
          </div>
          <div className="flex gap-2">
            {run.can_cancel && (
              <button
                disabled={busy}
                onClick={() => void handleCancel()}
                className="rounded-sm border border-[#D71920] px-3 py-2 text-sm font-medium text-[#D71920] hover:bg-[#D71920]/5 disabled:opacity-50"
              >
                Cancel run
              </button>
            )}
            {run.can_rerun && run.platform !== "file" && (
              <button
                disabled={busy}
                onClick={() => void handleRerun()}
                className="rounded-sm bg-[#D71920] px-3 py-2 text-sm font-medium text-white hover:bg-[#b5141a] disabled:opacity-50"
              >
                Re-run
              </button>
            )}
          </div>
        </div>
        {actionError && <div className="surface px-4 py-3 text-sm text-[#D71920]">{actionError}</div>}
        {run.status === "CANCEL_REQUESTED" && (
          <div className="surface px-4 py-3 text-sm text-ink-muted">
            Cancellation was requested. The run is shown as cancelled only once the platform confirms it.
          </div>
        )}

        <section className="surface p-5">
          <h2 className="mb-4 text-sm font-semibold text-ink">Overview</h2>
          <dl className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Field label="ACELO Run ID" value={run.acelo_run_id} mono />
            <Field label="Environment" value={run.environment_name} />
            <Field label="Started" value={formatTime(run.started_at ?? run.created_at)} />
            <Field label="Completed" value={formatTime(run.completed_at)} />
            <Field label="Duration" value={formatDuration(run.duration_seconds)} />
            <Field label="Started by" value={run.created_by} />
            <Field label="Request" value={run.request} />
            {run.retry_of_run_id && (
              <Field
                label="Re-run of"
                value={
                  <Link to={`/runs/${run.retry_of_run_id}`} className="font-mono text-xs text-[#D71920] hover:underline">
                    {run.retry_of_run_id}
                  </Link>
                }
              />
            )}
            {run.reruns.length > 0 && (
              <Field
                label="Re-runs"
                value={run.reruns.map((r) => (
                  <Link key={r} to={`/runs/${r}`} className="block font-mono text-xs text-[#D71920] hover:underline">
                    {r}
                  </Link>
                ))}
              />
            )}
          </dl>
        </section>

        <section className="surface p-5">
          <h2 className="mb-4 text-sm font-semibold text-ink">Platform execution</h2>
          <dl className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Field label="Platform" value={<span className="capitalize">{run.platform}</span>} />
            <Field label="Execution type" value={run.execution_type} />
            <Field label="Platform Run ID" value={run.platform_run_id} mono />
            <Field label="Platform status" value={run.platform_status} />
            <Field label="Item ID" value={run.resource_id} mono />
          </dl>
        </section>

        {Object.keys(run.parameters).length > 0 && (
          <section className="surface p-5">
            <h2 className="mb-4 text-sm font-semibold text-ink">Parameters</h2>
            <dl className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {Object.entries(run.parameters).map(([k, v]) => (
                <Field key={k} label={k} value={v} mono />
              ))}
            </dl>
          </section>
        )}

        {(run.error_message || run.status === "FAILED") && (
          <section className="surface border-[#D71920]/30 p-5">
            <h2 className="mb-2 text-sm font-semibold text-[#D71920]">Error</h2>
            {run.error_code && <p className="font-mono text-xs text-ink-muted">{run.error_code}</p>}
            <p className="mt-1 whitespace-pre-wrap text-sm text-ink">{run.error_message ?? "No platform detail."}</p>
          </section>
        )}

        <section className="surface p-5">
          <h2 className="mb-4 text-sm font-semibold text-ink">Timeline</h2>
          {timeline.length === 0 ? (
            <p className="text-sm text-ink-muted">No execution events recorded yet.</p>
          ) : (
            <ol className="flex flex-col gap-3" aria-label="Run timeline">
              {timeline.map((e) => (
                <li key={e.id} className="flex gap-3">
                  <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${e.level === "ERROR" ? "bg-[#D71920]" : e.level === "SUCCESS" ? "bg-signal-low" : e.level === "WARNING" ? "bg-signal-medium" : "bg-ink-faint"}`} />
                  <div className="min-w-0">
                    <p className="text-sm text-ink">{e.message}</p>
                    <p className="text-[11px] text-ink-faint">
                      {formatTime(e.timestamp)} · {e.event_type}
                      {e.stage ? ` · ${e.stage}` : ""}
                    </p>
                  </div>
                </li>
              ))}
            </ol>
          )}
        </section>

        <section className="surface p-5">
          <h2 className="mb-3 text-sm font-semibold text-ink">
            Logs {isActive(run.status) && <span className="ml-2 text-xs font-normal text-ink-faint">live</span>}
          </h2>
          <div className="max-h-96 overflow-y-auto rounded-sm bg-canvas p-3 font-mono text-xs" role="log">
            {events.length === 0 ? (
              <p className="text-ink-muted">No log entries.</p>
            ) : (
              events.map((e) => (
                <p key={e.id} className="whitespace-pre-wrap break-all">
                  <span className="text-ink-faint">{formatTime(e.timestamp)}</span>{" "}
                  <span className={levelTone(e.level)}>{e.level}</span> {e.message}
                </p>
              ))
            )}
          </div>
        </section>

        <section className="surface p-5" id="results" ref={resultsRef}>
          <h2 className="mb-3 text-sm font-semibold text-ink">Results</h2>
          {run.status !== "SUCCEEDED" ? (
            <p className="text-sm text-ink-muted">Results are available once the run succeeds.</p>
          ) : run.domain === "query" && run.result ? (
            <QueryRunResult result={run.result} />
          ) : !run.result ? (
            <p className="text-sm text-ink-muted">
              The run succeeded but its results have not been retrieved yet. They are read from OneLake when you are
              signed in; refresh to try again.
            </p>
          ) : (
            <>
              <p className="mb-3 text-xs text-ink-muted">
                {run.result.row_count ?? rows.length} row(s)
                {typeof run.result.table === "string" ? ` from ${run.result.table}` : ""}.{" "}
                <Link to="/results" className="text-[#D71920] hover:underline">
                  Open in Results
                </Link>
              </p>
              {rows.length > 0 && (
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-xs">
                    <thead className="border-b border-panel-border text-ink-faint">
                      <tr>
                        {columns.map((c) => (
                          <th key={c} className="px-2 py-2 font-medium">
                            {c}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {rows.slice(0, 50).map((row, i) => (
                        <tr key={i} className="border-b border-panel-border last:border-0">
                          {columns.map((c) => (
                            <td key={c} className="px-2 py-1.5 text-ink">
                              {row[c] === null || row[c] === undefined ? "—" : String(row[c])}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </section>
      </div>
    </Layout>
  );
}

/** A query run: detection counts + where its optimizations go (the Approval Center). */
function QueryRunResult({ result }: { result: NonNullable<Details["result"]> }) {
  const source = (result.source_payload ?? {}) as Record<string, unknown>;
  const rows = Array.isArray(source.rows) ? (source.rows as Record<string, unknown>[]) : [];
  const states = rows.reduce<Record<string, number>>((acc, r) => {
    const s = String(r.workflow_status ?? "unknown").toLowerCase();
    acc[s] = (acc[s] ?? 0) + 1;
    return acc;
  }, {});
  const known = (v: unknown) => (typeof v === "number" ? String(v) : "Not available");
  return (
    <div data-testid="query-run-result">
      <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Field label="Unhealthy queries" value={known(source.unhealthy_queries)} />
        <Field label="Optimization opportunities" value={known(source.optimization_opportunities)} />
        <Field label="Verified" value={String(states.verified ?? 0)} />
        <Field label="Review required" value={String(states.review_required ?? 0)} />
      </dl>
      <p className="mt-3 text-xs text-ink-muted">
        {typeof source.table === "string" ? `Read from ${source.table}. ` : ""}
        Validated optimizations are reviewed in the{" "}
        <Link to="/approvals" className="text-[#D71920] hover:underline">
          Approval Center
        </Link>
        .
      </p>
    </div>
  );
}
