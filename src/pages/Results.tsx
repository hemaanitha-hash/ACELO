import React, { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { AlertCircle, CheckCircle2, HistoryIcon, Upload } from "lucide-react";
import Layout from "../components/Layout";
import Button from "../components/Button";
import { useMsal } from "@azure/msal-react";
import { ApiError } from "../services/environmentApi";
import { FabricAuthError, getFabricToken, getSqlEndpointToken } from "../services/fabricAuth";
import {
  describeClusterRecommendation,
  getClusterResultForJob,
  getLatestClusterResult,
  HEALTH_BANDS,
  summariseClusterRows,
  type ClusterResult,
  type ClusterResultRow,
  type ParameterVerification,
} from "../services/executionApi";
import {
  listApprovals,
  requiresApproval,
  sendToApproval,
  STATUS_TEXT,
  type Approval,
} from "../services/approvalsApi";

/**
 * Cluster optimization results.
 *
 * Every figure on this page is computed from the rows the ACELO Cluster
 * optimizer produced for one run — whether it ran as the Fabric notebook or on
 * an uploaded file. There are no demo fixtures: when no run has produced
 * results the page says so rather than showing numbers.
 *
 * `?job=<id>` shows that specific run; otherwise the most recent one.
 */

type Filter = "all" | "critical" | "idle" | "oversized";

export default function Results() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const jobId = params.get("job");
  const { instance } = useMsal();
  const [result, setResult] = useState<ClusterResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<Filter>("all");

  useEffect(() => {
    // Delegated environments need the user's Fabric token to read results back.
    const withToken = async () => {
      const account = instance.getActiveAccount() ?? instance.getAllAccounts()[0];
      let token: string | null = null;
      if (account) {
        try {
          token = await getFabricToken(instance);
        } catch (e: unknown) {
          if (!(e instanceof FabricAuthError)) throw e;
        }
      }
      // Needed only when a result must still be read from the Lakehouse SQL
      // endpoint as the signed-in user; results already stored ignore it.
      const sqlToken = account ? await getSqlEndpointToken(instance) : null;
      return jobId
        ? getClusterResultForJob(jobId, token, sqlToken)
        : getLatestClusterResult(token, sqlToken);
    };

    setLoading(true);
    withToken()
      .then((r) => {
        setResult(r);
        // A request like "Find idle clusters" opens on the matching clusters.
        if (r?.focus === "idle" || r?.focus === "oversized") setFilter(r.focus);
      })
      .catch((e: unknown) =>
        setError(e instanceof ApiError ? e.message : "Could not load results.")
      )
      .finally(() => setLoading(false));
  }, [instance, jobId]);

  const summary = result ? summariseClusterRows(result.rows) : null;

  const recommendations = useMemo(
    () =>
      (result?.rows ?? [])
        .map((row) => ({ row, text: describeClusterRecommendation(row) }))
        .filter((r): r is { row: ClusterResultRow; text: string } => r.text !== null)
        .sort((a, b) => num(b.row.potential_monthly_savings) - num(a.row.potential_monthly_savings)),
    [result]
  );

  const visibleRows = (result?.rows ?? []).filter((r) =>
    filter === "critical"
      ? r.optimization_label === "Risky"
      : filter === "idle"
        ? r.idle_flag === 1
        : filter === "oversized"
          ? r.oversized_flag === 1
          : true
  );

  const isFile = result?.platform === "file";

  // Approval state per cluster for THIS run, from the real approval records.
  const [approvalsByResource, setApprovalsByResource] = useState<Record<string, Approval>>({});
  const [approvalError, setApprovalError] = useState<string | null>(null);
  const [sending, setSending] = useState<string | null>(null);

  async function loadApprovals(runId: string) {
    try {
      const rows = await listApprovals({ aceloRunId: runId });
      setApprovalsByResource(Object.fromEntries(rows.map((a) => [a.resource_id, a])));
    } catch {
      setApprovalError("Approval status could not be loaded.");
    }
  }

  useEffect(() => {
    if (result?.runId) void loadApprovals(result.runId);
  }, [result?.runId]);

  async function handleSendToApproval(resourceId: string) {
    if (!result) return;
    setSending(resourceId);
    setApprovalError(null);
    try {
      await sendToApproval(result.runId, resourceId);
      await loadApprovals(result.runId);
    } catch (e: unknown) {
      setApprovalError(e instanceof ApiError ? e.message : "Approval could not be saved. Please try again.");
    } finally {
      setSending(null);
    }
  }

  return (
    <Layout pageName="Results">
      <div className="flex flex-col gap-6">
        {loading && (
          <div className="surface py-16 text-center text-sm text-ink-muted">
            Loading results...
          </div>
        )}

        {!loading && error && (
          <div className="surface border-l-4 border-l-signal-high p-5">
            <div className="flex items-start gap-3">
              <AlertCircle size={18} className="mt-0.5 shrink-0 text-signal-high" />
              <div>
                <p className="text-sm font-medium text-ink">Results unavailable</p>
                <p className="mt-1 text-sm text-ink-muted">{error}</p>
              </div>
            </div>
          </div>
        )}

        {/* Honest empty state — never demo numbers. */}
        {!loading && !error && !result && (
          <div className="surface p-8 text-center">
            <h1 className="text-display font-semibold text-ink">No results yet</h1>
            <p className="mx-auto mt-2 max-w-md text-sm text-ink-muted">
              {jobId
                ? "This analysis has not produced results yet. It may still be running."
                : "Run a cluster analysis on your connected platform, or upload a cluster CSV in the AI Agent, to see real optimization results here."}
            </p>
            <Button className="mt-5" onClick={() => navigate("/agent")}>
              Run an analysis
            </Button>
          </div>
        )}

        {!loading && result && summary && (
          <>
            <div className="flex items-center gap-3">
              <span className="flex h-10 w-10 items-center justify-center rounded-full border border-brand-500/25 bg-brand-500/15">
                <CheckCircle2 size={18} className="text-brand-500" />
              </span>
              <div className="min-w-0">
                <h1 className="text-display font-semibold text-ink">
                  Cluster optimization results
                </h1>
                <p className="truncate text-sm text-ink-muted">
                  {summary.clusterCount} clusters analysed
                  {isFile
                    ? ` · uploaded file ${result.sourceFile ?? ""}`
                    : result.resultTable
                      ? ` · ${result.resultTable}`
                      : ""}
                </p>
              </div>
            </div>

            {/* KPIs computed from the real rows. */}
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <Kpi label="Total clusters" value={String(summary.clusterCount)} />
              <HealthKpi
                healthy={summary.healthy}
                atRisk={summary.atRisk}
                critical={summary.critical}
              />
              <Kpi label="Avg CPU utilization" value={pct(summary.avgCpuUtil)} />
              <Kpi label="Avg memory utilization" value={pct(summary.avgMemoryUtil)} />
              <Kpi
                label="Idle clusters"
                value={orNotAvailable(summary.idleCount, String)}
                tone={(summary.idleCount ?? 0) > 0 ? "warn" : undefined}
              />
              <Kpi
                label="Oversized clusters"
                value={orNotAvailable(summary.oversizedCount, String)}
                tone={(summary.oversizedCount ?? 0) > 0 ? "warn" : undefined}
              />
              <Kpi label="Current cost" value={orNotAvailable(summary.currentCost, usd)} />
              <Kpi
                label="Estimated savings"
                value={orNotAvailable(summary.monthlySavings, (v) => `${usd(v)}/mo`)}
                tone={summary.monthlySavings !== null ? "good" : undefined}
              />
            </div>

            {/* Distribution, drawn from real label counts. */}
            <section className="surface p-5">
              <p className="label-eyebrow mb-3">Optimization label distribution</p>
              <div className="space-y-2">
                {Object.entries(summary.byLabel)
                  .sort((a, b) => b[1] - a[1])
                  .map(([label, count]) => (
                    <div key={label} className="flex items-center gap-3">
                      <span className="w-48 shrink-0 text-sm text-ink">
                        {healthName(label)}{" "}
                        <span className="text-xs text-ink-faint">({label})</span>
                      </span>
                      <div className="h-2 flex-1 overflow-hidden rounded-full bg-canvas-raised">
                        <div
                          className={`h-full rounded-full ${
                            label === "Risky"
                              ? "bg-signal-high"
                              : label === "Moderately Optimized"
                                ? "bg-signal-medium"
                                : "bg-brand-500"
                          }`}
                          style={{
                            width: `${(count / Math.max(summary.clusterCount, 1)) * 100}%`,
                          }}
                        />
                      </div>
                      <span className="tabular w-10 text-right text-sm text-ink-muted">
                        {count}
                      </span>
                    </div>
                  ))}
              </div>
            </section>

            {/* Cluster-level recommendations, built only from optimizer fields. */}
            <section className="surface p-5">
              <p className="label-eyebrow mb-3">Cluster recommendations</p>
              {recommendations.length === 0 ? (
                <p className="text-sm text-ink-muted">
                  The optimizer recommends no configuration changes for these clusters.
                </p>
              ) : (
                <ul className="divide-y divide-panel-border/60">
                  {recommendations.map(({ row, text }, index) => (
                    <li
                      key={String(row.cluster_id ?? index)}
                      className="flex flex-col gap-1 py-3 sm:flex-row sm:items-start sm:justify-between sm:gap-6"
                    >
                      <div className="min-w-0">
                        <p className="text-sm font-medium text-ink">
                          {String(row.cluster_name ?? row.cluster_id ?? "Cluster")}{" "}
                          <span className={`text-xs ${labelColour(row.optimization_label)}`}>
                            {healthName(row.optimization_label)}
                          </span>
                        </p>
                        <p className="mt-0.5 text-sm text-ink-muted">{text}</p>
                      </div>
                      <p className="tabular shrink-0 text-sm text-signal-low">
                        {typeof row.potential_monthly_savings === "number" &&
                        row.potential_monthly_savings > 0
                          ? `${usd(row.potential_monthly_savings)}/mo`
                          : "—"}
                      </p>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            {/* Detailed table of the actual analysed clusters. */}
            <section className="surface p-5">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
                <p className="label-eyebrow">Analysed clusters</p>
                <div className="flex flex-wrap gap-2">
                  {(
                    [
                      ["all", `All (${summary.clusterCount})`],
                      ["critical", `Critical (${summary.critical})`],
                      ["idle", `Idle (${summary.idleCount})`],
                      ["oversized", `Oversized (${summary.oversizedCount})`],
                    ] as [Filter, string][]
                  ).map(([key, label]) => (
                    <button
                      key={key}
                      onClick={() => setFilter(key)}
                      className={`rounded-sm border px-3 py-1 text-xs transition-colors ${
                        filter === key
                          ? "border-brand-500 bg-brand-500/15 text-ink"
                          : "border-panel-border text-ink-muted hover:text-ink"
                      }`}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </div>
              {approvalError && (
                <p data-testid="approval-error" className="mb-3 text-xs text-signal-high">{approvalError}</p>
              )}
              <div className="overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <thead>
                    <tr className="border-b border-panel-border text-xs uppercase text-ink-muted">
                      <th className="py-2 pr-4 font-medium">Cluster</th>
                      <th className="py-2 pr-4 font-medium">Node type</th>
                      <th className="py-2 pr-4 font-medium">Health</th>
                      <th className="py-2 pr-4 font-medium">CPU %</th>
                      <th className="py-2 pr-4 font-medium">Mem %</th>
                      <th className="py-2 pr-4 font-medium">Idle min</th>
                      <th className="py-2 pr-4 font-medium">Workers (min–max)</th>
                      <th className="py-2 pr-4 font-medium">Rec. max</th>
                      <th className="py-2 pr-4 font-medium">Utilization</th>
                      <th className="py-2 pr-4 font-medium">Sizing</th>
                      <th className="py-2 pr-4 font-medium">Efficiency</th>
                      <th className="py-2 pr-4 font-medium">Cost</th>
                      <th className="py-2 pr-4 font-medium">Savings / mo</th>
                      <th className="py-2 pr-4 font-medium">Approval</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visibleRows.map((row, index) => (
                      <tr
                        key={String(row.cluster_id ?? index)}
                        className="border-b border-panel-border/60"
                      >
                        <td className="py-2 pr-4 text-ink">
                          {String(row.cluster_name ?? row.cluster_id ?? "—")}
                          <span className="block font-mono text-[11px] text-ink-faint">
                            {String(row.cluster_id ?? "")}
                          </span>
                        </td>
                        <td className="py-2 pr-4 text-ink-muted">{String(row.node_type ?? "—")}</td>
                        <td className="py-2 pr-4">
                          <span className={labelColour(row.optimization_label)}>
                            {healthName(row.optimization_label)}
                          </span>
                        </td>
                        <td className="tabular py-2 pr-4 text-ink-muted">{fmt(row.avg_cpu_util, 1)}</td>
                        <td className="tabular py-2 pr-4 text-ink-muted">{fmt(row.avg_memory_util, 1)}</td>
                        <td className="tabular py-2 pr-4 text-ink-muted">{fmt(row.idle_time_min, 0)}</td>
                        <td className="tabular py-2 pr-4 text-ink-muted">
                          {fmt(row.current_workers, 0)} ({fmt(row.min_workers, 0)}–{fmt(row.max_workers, 0)})
                        </td>
                        <td className="tabular py-2 pr-4 text-ink">{fmt(row.recommended_max_workers, 0)}</td>
                        <td className="py-2 pr-4 text-xs text-ink-muted">
                          {String(row.underutilized_label ?? "—")}
                        </td>
                        <td className="py-2 pr-4 text-xs text-ink-muted">
                          {String(row.oversized_label ?? "—")}
                        </td>
                        <td className="tabular py-2 pr-4 text-ink-muted">{fmt(row.efficiency_score, 2)}</td>
                        <td className="tabular py-2 pr-4 text-ink-muted">
                          {typeof row.total_dbus_cost_usd === "number" ? usd(row.total_dbus_cost_usd) : "—"}
                        </td>
                        <td className="tabular py-2 pr-4 text-ink">
                          {typeof row.potential_monthly_savings === "number"
                            ? usd(row.potential_monthly_savings)
                            : "—"}
                        </td>
                        <td className="py-2 pr-4 text-xs">
                          <ApprovalCell
                            row={row}
                            approval={approvalsByResource[resourceIdOf(row)]}
                            sending={sending === resourceIdOf(row)}
                            onSend={() => void handleSendToApproval(resourceIdOf(row))}
                            onOpen={(id) => navigate(`/approvals/${id}`)}
                          />
                        </td>
                      </tr>
                    ))}
                    {visibleRows.length === 0 && (
                      <tr>
                        <td colSpan={14} className="py-4 text-center text-sm text-ink-muted">
                          No clusters match this filter.
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </section>

            {/* AI remediation, only where the optimizer produced one. */}
            <AiRemediation rows={result.rows} />

            <section className="surface p-5">
              <p className="label-eyebrow mb-2">Run</p>
              <dl className="grid gap-3 sm:grid-cols-3 lg:grid-cols-5">
                <Detail
                  label="Source"
                  value={isFile ? `Uploaded file · ${result.sourceFile ?? "—"}` : result.platform}
                />
                <Detail label="ACELO run" value={result.runId} mono />
                <Detail
                  label={
                    isFile
                      ? "Platform job"
                      : result.platform === "fabric"
                        ? result.executionType === "pipeline"
                          ? "Fabric pipeline run ID"
                          : "Fabric run ID"
                        : `${result.platform} job`
                  }
                  value={result.platformRunId ?? "—"}
                  mono
                />
                {!isFile && (
                  <Detail
                    label="Execution"
                    value={`${result.executionType === "pipeline" ? "Pipeline" : "Notebook"} · ${
                      result.runStatus ?? "—"
                    }`}
                  />
                )}
                <Detail
                  label="Analysed"
                  value={
                    result.retrievedAt ? new Date(result.retrievedAt).toLocaleString() : "—"
                  }
                />
              </dl>
            </section>

            {result.parameterVerification && (
              <ParameterEvidence verification={result.parameterVerification} />
            )}

            <div className="flex gap-3">
              <Button variant="ghost" onClick={() => navigate("/agent")}>
                <Upload size={16} className="mr-2" />
                Analyze another file
              </Button>
              <Button variant="ghost" onClick={() => navigate("/history")}>
                <HistoryIcon size={16} className="mr-2" />
                View history
              </Button>
            </div>
          </>
        )}
      </div>
    </Layout>
  );
}

/** Same identity the backend uses for approvals: cluster_id, else cluster_name. */
function resourceIdOf(row: ClusterResultRow): string {
  return String(row.cluster_id ?? "").trim() || String(row.cluster_name ?? "").trim();
}

function ApprovalCell({
  row,
  approval,
  sending,
  onSend,
  onOpen,
}: {
  row: ClusterResultRow;
  approval: Approval | undefined;
  sending: boolean;
  onSend: () => void;
  onOpen: (approvalId: string) => void;
}) {
  if (approval) {
    return (
      <button
        type="button"
        data-testid="approval-status"
        onClick={() => onOpen(approval.approval_id)}
        className="font-medium text-brand-500 hover:underline"
      >
        {STATUS_TEXT[approval.status]}
      </button>
    );
  }
  if (!requiresApproval(row)) return <span className="text-ink-faint">Not required</span>;
  return (
    <button
      type="button"
      onClick={onSend}
      disabled={sending}
      className="rounded-sm border border-brand-500 px-2 py-0.5 font-medium text-brand-500 hover:bg-brand-500/10 disabled:opacity-50"
    >
      {sending ? "Sending..." : "Send to Approval"}
    </button>
  );
}

function num(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function fmt(value: unknown, digits: number): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "—";
}

const NOT_AVAILABLE = "Not available";

/** A metric the data does not provide is "Not available" — never a fabricated 0. */
function orNotAvailable(value: number | null, format: (v: number) => string): string {
  return value === null ? NOT_AVAILABLE : format(value);
}

function pct(value: number | null): string {
  return orNotAvailable(value, (v) => `${v.toFixed(1)}%`);
}

function usd(value: number): string {
  return `$${value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function healthName(label: unknown): string {
  return HEALTH_BANDS[String(label) as keyof typeof HEALTH_BANDS] ?? String(label ?? "—");
}

function labelColour(label: unknown): string {
  return label === "Risky"
    ? "text-signal-high"
    : label === "Moderately Optimized"
      ? "text-signal-medium"
      : "text-ink-muted";
}

/** Renders only rows where the optimizer actually returned a recommendation. */
function AiRemediation({ rows }: { rows: ClusterResultRow[] }) {
  const withPlan = rows.filter((r) => {
    const text = String(r.llm_optimization ?? "");
    return (
      text &&
      !text.startsWith("LLM_UNAVAILABLE") &&
      text !== "Health Stable. No AI action required."
    );
  });

  const unavailable = rows.some((r) =>
    String(r.llm_optimization ?? "").startsWith("LLM_UNAVAILABLE")
  );

  if (withPlan.length === 0) {
    return (
      <section className="surface p-5">
        <p className="label-eyebrow mb-2">AI remediation</p>
        <p className="text-sm text-ink-muted">
          {unavailable
            ? "No LLM credential is configured, so AI remediation plans were not generated. The deterministic scoring above is unaffected."
            : "No cluster required an AI remediation plan in this run."}
        </p>
      </section>
    );
  }

  return (
    <section className="surface p-5">
      <p className="label-eyebrow mb-3">AI remediation</p>
      <div className="space-y-4">
        {withPlan.map((row, index) => (
          <div key={String(row.cluster_id ?? index)}>
            <p className="text-sm font-medium text-ink">
              {String(row.cluster_name ?? row.cluster_id ?? "Cluster")}
            </p>
            <p className="mt-1 whitespace-pre-line text-sm leading-relaxed text-ink-muted">
              {String(row.llm_optimization)}
            </p>
          </div>
        ))}
      </div>
    </section>
  );
}

function Kpi({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "good" | "warn";
}) {
  const colour =
    tone === "good" ? "text-signal-low" : tone === "warn" ? "text-signal-high" : "text-ink";
  return (
    <div className="surface p-5">
      <p className="label-eyebrow">{label}</p>
      <p className={`tabular mt-2 text-xl font-semibold ${colour}`}>{value}</p>
    </div>
  );
}

function HealthKpi({
  healthy,
  atRisk,
  critical,
}: {
  healthy: number;
  atRisk: number;
  critical: number;
}) {
  return (
    <div className="surface p-5">
      <p className="label-eyebrow">Healthy / At risk / Critical</p>
      <p className="tabular mt-2 text-xl font-semibold">
        <span className="text-signal-low">{healthy}</span>
        <span className="text-ink-faint"> / </span>
        <span className="text-signal-medium">{atRisk}</span>
        <span className="text-ink-faint"> / </span>
        <span className="text-signal-high">{critical}</span>
      </p>
    </div>
  );
}

/** ACELO SENT vs NOTEBOOK RECEIVED for this run — evidence, not a claim. */
function ParameterEvidence({ verification }: { verification: ParameterVerification }) {
  const colour =
    verification.status === "MATCHED"
      ? "text-signal-low"
      : verification.status === "MISMATCH"
        ? "text-signal-high"
        : "text-signal-medium";
  const names = Object.keys(verification.sent ?? {}).sort();
  return (
    <section className="surface p-5">
      <p className="label-eyebrow mb-2">Notebook runtime parameters</p>
      <p className="text-sm">
        <span className="text-ink-muted">Verification: </span>
        <span data-testid="verification-status" className={`font-medium ${colour}`}>
          {verification.status}
        </span>
        {verification.correlation_id_matched !== undefined && (
          <span className="ml-3 text-xs text-ink-muted">
            correlation id {verification.correlation_id_matched ? "matched" : "did NOT match"}
          </span>
        )}
      </p>
      {verification.reason && <p className="mt-1 text-xs text-ink-muted">{verification.reason}</p>}
      {names.length > 0 && (
        <div className="mt-3 overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-panel-border text-xs uppercase text-ink-muted">
                <th className="py-2 pr-4 font-medium">Parameter</th>
                <th className="py-2 pr-4 font-medium">ACELO sent</th>
                <th className="py-2 pr-4 font-medium">Notebook received</th>
              </tr>
            </thead>
            <tbody>
              {names.map((name) => (
                <tr key={name} className="border-b border-panel-border/60">
                  <td className="py-2 pr-4 font-mono text-xs text-ink">{name}</td>
                  <td className="py-2 pr-4 font-mono text-xs text-ink-muted">
                    {verification.sent?.[name]}
                  </td>
                  <td className="py-2 pr-4 font-mono text-xs text-ink-muted">
                    {verification.received ? verification.received[name] ?? "(not received)" : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function Detail({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs uppercase tracking-wide text-ink-muted">{label}</dt>
      <dd className={`mt-1 truncate text-sm text-ink ${mono ? "font-mono text-[11px]" : ""}`}>
        {value}
      </dd>
    </div>
  );
}
