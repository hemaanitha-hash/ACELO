import React, { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import Layout from "../components/Layout";
import { ApiError } from "../services/environmentApi";
import {
  RUN_STATES,
  RUN_STATE_LABELS,
  RunFilters,
  RunState,
  RunSummary,
  formatDuration,
  formatTime,
  listRuns,
} from "../services/runsApi";

export function RunStateBadge({ state }: { state: RunState | string }) {
  const tone =
    state === "SUCCEEDED"
      ? "bg-signal-low/10 text-signal-low border-signal-low/25"
      : state === "FAILED"
        ? "bg-[#D71920]/10 text-[#D71920] border-[#D71920]/25"
        : state === "CANCELLED"
          ? "bg-ink-faint/10 text-ink-muted border-panel-borderStrong"
          : "bg-signal-info/10 text-signal-info border-signal-info/25";
  return (
    <span className={`inline-flex items-center rounded-sm border px-2 py-0.5 text-xs font-medium ${tone}`}>
      {RUN_STATE_LABELS[state as RunState] ?? state}
    </span>
  );
}

const OPTIMIZATIONS = [
  { value: "", label: "All optimizations" },
  { value: "cluster", label: "Cluster Optimization" },
  { value: "query", label: "Query Optimization" },
  { value: "storage", label: "Storage Optimization" },
];
const PLATFORMS = [
  { value: "", label: "All platforms" },
  { value: "fabric", label: "Microsoft Fabric" },
  { value: "databricks", label: "Databricks" },
  { value: "file", label: "Uploaded file" },
];

const input =
  "rounded-sm border border-panel-border bg-white px-2.5 py-1.5 text-sm text-ink focus:border-[#D71920] focus:outline-none";

export default function History() {
  const navigate = useNavigate();
  const [filters, setFilters] = useState<RunFilters>({});
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const body = await listRuns({
        ...filters,
        // Date inputs are local days; the backend compares UTC timestamps.
        since: filters.since ? new Date(`${filters.since}T00:00:00`).toISOString() : undefined,
        until: filters.until ? new Date(`${filters.until}T23:59:59`).toISOString() : undefined,
      });
      setRuns(body.runs);
      setTotal(body.total);
    } catch (e: unknown) {
      setError(e instanceof ApiError ? e.message : "Could not load run history.");
    } finally {
      setLoading(false);
    }
  }, [filters]);

  useEffect(() => {
    const handle = window.setTimeout(() => void load(), 250);
    return () => window.clearTimeout(handle);
  }, [load]);

  const set = (key: keyof RunFilters) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setFilters((f) => ({ ...f, [key]: e.target.value || undefined }));

  return (
    <Layout pageName="Run History" onRefresh={() => void load()}>
      <div className="flex flex-col gap-6">
        <div>
          <h1 className="text-display font-semibold text-ink">Run history</h1>
          <p className="mt-2 text-sm text-ink-muted">
            Every optimization run ACELO has executed, with its platform run ID, status and outcome.
          </p>
        </div>

        <div className="surface flex flex-wrap items-end gap-3 p-4">
          <label className="flex min-w-[16rem] flex-1 flex-col gap-1 text-xs text-ink-muted">
            Search
            <input
              className={input}
              placeholder="ACELO Run ID or Platform Run ID"
              value={filters.search ?? ""}
              onChange={set("search")}
              aria-label="Search runs"
            />
          </label>
          <label className="flex flex-col gap-1 text-xs text-ink-muted">
            Status
            <select className={input} value={filters.status ?? ""} onChange={set("status")} aria-label="Status">
              <option value="">All statuses</option>
              {RUN_STATES.map((s) => (
                <option key={s} value={s}>
                  {RUN_STATE_LABELS[s]}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs text-ink-muted">
            Optimization
            <select className={input} value={filters.domain ?? ""} onChange={set("domain")} aria-label="Optimization">
              {OPTIMIZATIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs text-ink-muted">
            Platform
            <select className={input} value={filters.platform ?? ""} onChange={set("platform")} aria-label="Platform">
              {PLATFORMS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs text-ink-muted">
            From
            <input type="date" className={input} value={filters.since ?? ""} onChange={set("since")} aria-label="From date" />
          </label>
          <label className="flex flex-col gap-1 text-xs text-ink-muted">
            To
            <input type="date" className={input} value={filters.until ?? ""} onChange={set("until")} aria-label="To date" />
          </label>
        </div>

        {error ? (
          <div className="surface border-[#D71920]/30 px-4 py-6 text-sm text-[#D71920]">{error}</div>
        ) : loading && runs.length === 0 ? (
          <div className="surface py-16 text-center text-sm text-ink-muted">Loading run history...</div>
        ) : runs.length === 0 ? (
          <div className="surface py-16 text-center text-sm text-ink-muted">No runs match these filters.</div>
        ) : (
          <div className="surface overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="border-b border-panel-border text-xs uppercase tracking-wide text-ink-faint">
                <tr>
                  <th className="px-4 py-3">Optimization</th>
                  <th className="px-4 py-3">Status</th>
                  <th className="px-4 py-3">Platform</th>
                  <th className="px-4 py-3">Environment</th>
                  <th className="px-4 py-3">Started</th>
                  <th className="px-4 py-3">Duration</th>
                  <th className="px-4 py-3">ACELO Run ID</th>
                  <th className="px-4 py-3">Platform Run ID</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => (
                  <tr
                    key={run.acelo_run_id}
                    onClick={() => navigate(`/runs/${run.acelo_run_id}`)}
                    className="cursor-pointer border-b border-panel-border last:border-0 hover:bg-panel-hover"
                  >
                    <td className="px-4 py-3 font-medium text-ink">
                      {run.optimization}
                      {run.retry_of_run_id && <span className="ml-2 text-xs text-ink-faint">re-run</span>}
                    </td>
                    <td className="px-4 py-3">
                      <RunStateBadge state={run.status} />
                    </td>
                    <td className="px-4 py-3 capitalize text-ink-muted">{run.platform}</td>
                    <td className="px-4 py-3 text-ink-muted">{run.environment_name ?? "—"}</td>
                    <td className="px-4 py-3 text-ink-muted">{formatTime(run.started_at ?? run.created_at)}</td>
                    <td className="px-4 py-3 text-ink-muted">{formatDuration(run.duration_seconds)}</td>
                    <td className="px-4 py-3 font-mono text-xs text-ink-muted">{run.acelo_run_id}</td>
                    <td className="px-4 py-3 font-mono text-xs text-ink-muted">{run.platform_run_id ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="px-4 py-2 text-xs text-ink-faint">
              Showing {runs.length} of {total} run{total === 1 ? "" : "s"}
            </p>
          </div>
        )}
      </div>
    </Layout>
  );
}
