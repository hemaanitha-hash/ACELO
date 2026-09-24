import React, { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { AlertCircle, RefreshCw } from "lucide-react";
import { useMsal } from "@azure/msal-react";
import Layout from "../components/Layout";
import PageHeader from "../components/PageHeader";
import { StatePanel } from "../components/StateBlock";
import { ApiError } from "../services/environmentApi";
import { FabricAuthError, getFabricToken, getOneLakeToken } from "../services/fabricAuth";
import {
  display,
  getApprovalSummary,
  listApprovals,
  refreshFromTracking,
  type TrackingSource,
  STATUS_TEXT,
  usd,
  type Approval,
  type ApprovalStatus,
  type ApprovalSummary,
} from "../services/approvalsApi";

/**
 * Approval Center. Every row is a real recommendation from an optimizer run —
 * there is no demo data and no email: review happens here.
 *
 * `?status=APPROVED` filters (default PENDING); `?cluster=name` narrows to one
 * cluster, and with `&action=approve|reject` opens its review directly (used by
 * the AI Agent, which never approves or rejects on its own).
 */

const FILTERS: { status: ApprovalStatus; label: string }[] = [
  { status: "PENDING", label: "Pending" },
  { status: "APPROVED", label: "Approved" },
  { status: "REJECTED", label: "Rejected" },
  { status: "EXECUTING", label: "Executing" },
  { status: "COMPLETED", label: "Completed" },
  { status: "FAILED", label: "Failed" },
];

export function ValidationBadge({ status }: { status: string | null }) {
  if (!status) return <span className="text-xs text-ink-muted">Not available</span>;
  const verified = status === "verified";
  return (
    <span
      className={`inline-flex rounded-sm border px-2 py-0.5 text-xs font-medium ${
        verified
          ? "border-signal-low/30 bg-signal-low/10 text-signal-low"
          : "border-signal-medium/30 bg-signal-medium/10 text-signal-medium"
      }`}
    >
      {verified ? "Verified" : "Review required"}
    </span>
  );
}

export default function Approvals() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const status = (params.get("status") ?? "PENDING").toUpperCase() as ApprovalStatus;
  const cluster = params.get("cluster");
  const action = params.get("action");

  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [summary, setSummary] = useState<ApprovalSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const { instance } = useMsal();
  // Outcome of reading the Fabric approval tracking Delta table(s).
  const [sources, setSources] = useState<TrackingSource[]>([]);
  const [trackingNote, setTrackingNote] = useState<string | null>(null);

  /**
   * Imports new candidates from the Fabric tracking table, read directly from
   * OneLake. A failed read is SHOWN — it never produces stand-in rows; approvals
   * already recorded in ACELO still display because they are real.
   */
  async function refreshTracking() {
    setTrackingNote(null);
    try {
      let fabricToken: string | null = null;
      try {
        fabricToken = await getFabricToken(instance);
      } catch (e: unknown) {
        if (!(e instanceof FabricAuthError)) throw e;
      }
      const onelake = await getOneLakeToken(instance);
      const result = await refreshFromTracking(fabricToken, onelake);
      setSources(result.sources);
    } catch (e: unknown) {
      setSources([]);
      setTrackingNote(e instanceof ApiError ? e.message : "The approval tracking table could not be read.");
    }
  }

  async function load(withRefresh = true) {
    setLoading(true);
    setError(null);
    if (withRefresh) await refreshTracking();
    try {
      const [rows, counts] = await Promise.all([
        listApprovals({ status: cluster ? undefined : [status] }),
        getApprovalSummary(),
      ]);
      const filtered = cluster
        ? rows.filter((a) => a.resource_name.toLowerCase() === cluster.toLowerCase() ||
                             a.resource_id.toLowerCase() === cluster.toLowerCase())
        : rows;
      setApprovals(filtered);
      setSummary(counts);

      // Agent deep link: open the one matching review (approve/reject still need confirmation there).
      if (cluster && filtered.length === 1) {
        const suffix = action === "approve" || action === "reject" ? `?action=${action}` : "";
        navigate(`/approvals/${filtered[0].approval_id}${suffix}`, { replace: true });
      }
    } catch (e: unknown) {
      setError(e instanceof ApiError ? e.message : "Unable to load optimization recommendations.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, cluster, action]);

  return (
    <Layout pageName="Approval Center" onRefresh={() => void load()}>
      <div className="flex flex-col gap-6">
        <PageHeader
          title="Approvals"
          description="Recommendations from real optimization runs. Nothing is applied without an approval and an explicit execution."
        />

        <div className="flex flex-wrap items-center gap-3">
          <button
            type="button"
            onClick={() => void load()}
            disabled={loading}
            className="inline-flex items-center gap-2 rounded-sm border border-brand-500 px-3 py-1.5 text-xs font-medium text-brand-500 hover:bg-brand-500/10 disabled:opacity-50"
          >
            <RefreshCw size={14} /> Refresh tracking
          </button>
          {sources
            .filter((src) => src.status === "ok")
            .map((src) => (
              <span key={src.environment_id} data-testid="tracking-ok" className="text-xs text-ink-muted">
                Read {src.rows_read} rows from {src.table} · {src.unique_business_keys ?? src.candidates} clusters ·{" "}
                {src.created ?? 0} new · {src.updated ?? 0} updated
              </span>
            ))}
        </div>

        {/* A tracking-table read that failed is reported, never papered over. */}
        {(trackingNote || sources.some((src) => src.status === "failed")) && (
          <div data-testid="tracking-error" className="surface border-l-4 border-l-signal-high p-4">
            <p className="text-sm font-medium text-ink">
              Unable to load optimization recommendations from the approval tracking table.
            </p>
            {trackingNote && <p className="mt-1 text-sm text-ink-muted">{trackingNote}</p>}
            {sources
              .filter((src) => src.status === "failed")
              .map((src) => (
                <p key={src.environment_id} className="mt-1 text-sm text-ink-muted">
                  {src.table}: {src.message}
                </p>
              ))}
          </div>
        )}

        {/* Counts from the real approval records. */}
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
          {(["PENDING", "APPROVED", "REJECTED", "EXECUTING", "COMPLETED"] as ApprovalStatus[]).map((s) => (
            <button
              key={s}
              type="button"
              data-testid={`count-${s}`}
              onClick={() => setParams({ status: s })}
              className={`surface p-5 text-left transition hover:border-brand-500/50 ${
                status === s && !cluster ? "ring-1 ring-brand-500/40" : ""
              }`}
            >
              <p className="label-eyebrow">{s === "PENDING" ? "Pending Approvals" : STATUS_TEXT[s]}</p>
              <p className="tabular mt-2 text-xl font-semibold text-ink">
                {summary ? summary[s] : "—"}
              </p>
            </button>
          ))}
        </div>

        <div className="flex flex-wrap gap-2">
          {FILTERS.map((f) => (
            <button
              key={f.status}
              type="button"
              onClick={() => setParams({ status: f.status })}
              className={`rounded-sm border px-3 py-1 text-xs transition-colors ${
                status === f.status && !cluster
                  ? "border-brand-500 bg-brand-500/15 text-ink"
                  : "border-panel-border text-ink-muted hover:text-ink"
              }`}
            >
              {f.label}
            </button>
          ))}
          {cluster && (
            <span className="rounded-sm border border-brand-500 px-3 py-1 text-xs text-ink">
              Cluster: {cluster}
            </span>
          )}
        </div>

        {loading && (
          <StatePanel kind="loading" title="Loading approvals…" />
        )}

        {!loading && error && (
          <StatePanel
            kind="error"
            title="Unable to load optimization recommendations"
            detail={error}
          />
        )}

        {!loading && !error && approvals.length === 0 && (
          <StatePanel
            kind="empty"
            title={
              cluster
                ? `No approval found for cluster ${cluster}.`
                : status === "PENDING"
                  ? "No pending approvals"
                  : `No ${STATUS_TEXT[status].toLowerCase()} recommendations`
            }
            detail="Query optimizations appear here after a Query Optimization run validates them. Cluster recommendations are reporting only and never need approval."
          />
        )}

        {!loading && !error && approvals.length > 0 && (
          <section className="surface p-5">
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead>
                  <tr className="border-b border-panel-border text-xs uppercase text-ink-muted">
                    <th className="py-2 pr-4 font-medium">Query ID</th>
                    <th className="py-2 pr-4 font-medium">Root cause</th>
                    <th className="py-2 pr-4 font-medium">Validation</th>
                    <th className="py-2 pr-4 font-medium">Cost</th>
                    <th className="py-2 pr-4 font-medium">Savings</th>
                    <th className="py-2 pr-4 font-medium">Status</th>
                    <th className="py-2 pr-4 font-medium">Created</th>
                    <th className="py-2 pr-4 font-medium">Action</th>
                  </tr>
                </thead>
                <tbody>
                  {approvals.map((a) => (
                    <tr key={a.approval_id} className="border-b border-panel-border/60">
                      <td className="py-2 pr-4 font-mono text-xs text-ink">
                        {a.resource_name}
                        {a.requires_new_approval && (
                          <span className="ml-2 font-sans text-[11px] text-[#D71920]">new recommendation</span>
                        )}
                      </td>
                      <td className="py-2 pr-4 text-ink-muted">{a.optimization_label ?? "Not available"}</td>
                      <td className="py-2 pr-4">
                        <ValidationBadge status={a.platform_validation_status ?? null} />
                      </td>
                      <td className="tabular py-2 pr-4 text-ink-muted">{display(a.total_dbus_cost_usd, usd)}</td>
                      <td className="tabular py-2 pr-4 text-ink">{display(a.potential_monthly_savings, usd)}</td>
                      <td className="py-2 pr-4 text-xs font-medium text-ink">{a.status}</td>
                      <td className="py-2 pr-4 text-xs text-ink-muted">
                        {a.created_at ? new Date(a.created_at).toLocaleString() : "Not available"}
                      </td>
                      <td className="py-2 pr-4">
                        <button
                          type="button"
                          onClick={() => navigate(`/approvals/${a.approval_id}`)}
                          className="rounded-sm border border-brand-500 px-3 py-1 text-xs font-medium text-brand-500 hover:bg-brand-500/10"
                        >
                          Review
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        )}
      </div>
    </Layout>
  );
}
