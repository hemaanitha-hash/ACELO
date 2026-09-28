import React, { useCallback, useEffect, useMemo, useState } from "react";
import { CheckCircle2, Clock3, RefreshCw, XCircle, AlertCircle } from "lucide-react";
import { useMsal } from "@azure/msal-react";
import Layout from "../components/Layout";
import { platformHeaders } from "../services/platformContext";

type Stage1Status = "PENDING" | "APPROVED" | "REJECTED";

interface Stage1Recommendation {
  recommendation_id: string;
  customer_id?: string;
  environment_id?: string;
  workspace_name?: string | null;
  resource_id?: string | null;
  resource_type?: string | null;
  finding_id?: string | null;
  rule_id?: string | null;
  finding_type?: string | null;
  title?: string | null;
  summary?: string | null;
  description?: string | null;
  resource?: string | null;
  domain?: string | null;
  current_state?: unknown;
  proposed_state?: unknown;
  evidence?: unknown;
  evidence_references?: unknown;
  evidence_quality?: unknown;
  observation_window?: {
    start?: string | null;
    end?: string | null;
  } | null;
  expected_impact?: {
    description?: string;
  } | null;
  estimated_savings?: unknown;
  confidence?: string | null;
  severity?: string | null;
  risk?: string | null;
  status?: string | null;
  policy_status?: string | null;
  approval_status?: string | null;
  execution_status?: string | null;
  verification_status?: string | null;
}

interface Stage1Approval {
  approval_id: string;
  recommendation_id: string;
  status: Stage1Status;
  requested_by?: string | null;
  decided_by?: string | null;
  created_at?: string | null;
  decided_at?: string | null;
  recommendation: Stage1Recommendation;
}

interface RejectPayload {
  reason: string;
}

const STATUS_FILTERS: { value: Stage1Status; label: string; icon: React.ReactNode }[] = [
  { value: "PENDING", label: "Pending Approvals", icon: <Clock3 size={18} /> },
  { value: "APPROVED", label: "Approved", icon: <CheckCircle2 size={18} /> },
  { value: "REJECTED", label: "Rejected", icon: <XCircle size={18} /> },
];

function jsonText(value: unknown): string {
  if (value === null || value === undefined) return "Not available";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function displayValue(value: string | null | undefined): string {
  return value && value.trim() ? value : "Not available";
}

async function parseError(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: string; message?: string; };
    return body.detail ?? body.message ?? `Request failed (HTTP ${response.status}).`;
  } catch {
    return `Request failed (HTTP ${response.status}).`;
  }
}

function normalizeApproval(value: unknown): Stage1Approval {
  const raw = value as Record<string, unknown>;
  const recommendation = (raw.recommendation as Record<string, unknown> | undefined) ?? {};

  return {
    approval_id: String(raw.approval_id ?? ""),
    recommendation_id: String(raw.recommendation_id ?? recommendation.recommendation_id ?? ""),
    status: String(raw.status ?? "PENDING").toUpperCase() as Stage1Status,
    requested_by: raw.requested_by == null ? null : String(raw.requested_by),
    decided_by: raw.decided_by == null ? null : String(raw.decided_by),
    created_at: raw.created_at == null ? null : String(raw.created_at),
    decided_at: raw.decided_at == null ? null : String(raw.decided_at),
    recommendation: {
      ...recommendation,
      recommendation_id: String(recommendation.recommendation_id ?? raw.recommendation_id ?? ""),
      title: recommendation.title == null ? null : String(recommendation.title),
      resource: recommendation.resource == null ? null : String(recommendation.resource),
      resource_id: recommendation.resource_id == null ? null : String(recommendation.resource_id),
      domain: recommendation.domain == null ? null : String(recommendation.domain),
      finding_type: recommendation.finding_type == null ? null : String(recommendation.finding_type),
      workspace_name: recommendation.workspace_name == null ? null : String(recommendation.workspace_name),
      rule_id: recommendation.rule_id == null ? null : String(recommendation.rule_id),
      risk: recommendation.risk == null ? null : String(recommendation.risk),
      confidence: recommendation.confidence == null ? null : String(recommendation.confidence),
    } as Stage1Recommendation,
  };
}

export default function Approvals() {
  const { instance } = useMsal();
  const [status, setStatus] = useState<Stage1Status>("PENDING");
  const [allApprovals, setAllApprovals] = useState<Stage1Approval[]>([]);
  const [loading, setLoading] = useState(true);
  const [actionId, setActionId] = useState<string | null>(null);
  const [rejectingId, setRejectingId] = useState<string | null>(null);
  const [rejectReason, setRejectReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [successMsg, setSuccessMsg] = useState<string | null>(null);

  const actorHeaders = useMemo(() => {
    const account = instance?.getActiveAccount?.() ?? instance?.getAllAccounts?.()?.[0];
    return {
      "X-Acelo-User-Id": account?.localAccountId ?? account?.homeAccountId ?? account?.username ?? "demo-user",
      "X-Acelo-User-Name": account?.name ?? account?.username ?? account?.localAccountId ?? "Demo User",
    };
  }, [instance]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setSuccessMsg(null);
    try {
      const response = await fetch(`/api/approvals/recommendations`, {
        method: "GET",
        headers: { "Content-Type": "application/json", ...platformHeaders() },
      });
      if (!response.ok) throw new Error(await parseError(response));
      const body = await response.json();
      const rows = Array.isArray(body) ? body : [];
      setAllApprovals(rows.map(normalizeApproval));
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Unable to load Stage 1 approvals.");
      setAllApprovals([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  async function approveRecommendation(recommendationId: string): Promise<void> {
    if (!window.confirm("Approve this optimization recommendation?\n\nNo Databricks change will be executed in this stage.")) return;
    setActionId(recommendationId);
    setError(null);
    setSuccessMsg(null);
    try {
      const response = await fetch(`/api/approvals/recommendations/${encodeURIComponent(recommendationId)}/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...platformHeaders(), ...actorHeaders },
      });
      if (!response.ok) throw new Error(await parseError(response));
      setSuccessMsg("Recommendation approved successfully.");
      await load();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "The recommendation could not be approved.");
    } finally {
      setActionId(null);
    }
  }

  async function rejectRecommendation(recommendationId: string): Promise<void> {
    const reason = rejectReason.trim();
    if (reason.length < 3) {
      setError("Please provide a rejection reason (minimum 3 characters).");
      return;
    }
    setActionId(recommendationId);
    setError(null);
    setSuccessMsg(null);
    try {
      const response = await fetch(`/api/approvals/recommendations/${encodeURIComponent(recommendationId)}/reject`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...platformHeaders(), ...actorHeaders },
        body: JSON.stringify({ reason } satisfies RejectPayload),
      });
      if (!response.ok) throw new Error(await parseError(response));
      setSuccessMsg("Recommendation rejected successfully.");
      setRejectingId(null);
      setRejectReason("");
      await load();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "The recommendation could not be rejected.");
    } finally {
      setActionId(null);
    }
  }

  const counts = {
    PENDING: allApprovals.filter((a) => a.status === "PENDING").length,
    APPROVED: allApprovals.filter((a) => a.status === "APPROVED").length,
    REJECTED: allApprovals.filter((a) => a.status === "REJECTED").length,
  };
  const displayedApprovals = allApprovals.filter((a) => a.status === status);

  return (
    <Layout pageName="Approval Center" onRefresh={() => void load()}>
      <div className="flex flex-col gap-6 max-w-7xl mx-auto w-full pb-12">
        <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 border-b border-gray-200 pb-6">
          <div>
            <h1 className="text-2xl font-bold text-gray-900 tracking-tight">Approval Center</h1>
            <p className="text-sm text-gray-500 mt-1">Review evidence-backed Databricks recommendations. Approval changes workflow state only.</p>
          </div>
          <button onClick={() => void load()} disabled={loading} className="inline-flex items-center gap-2 px-4 py-2 bg-white border border-gray-300 rounded-md shadow-sm text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50 transition-colors">
            <RefreshCw size={16} className={loading ? "animate-spin" : ""} />
            Refresh
          </button>
        </div>

        <div className="grid gap-4 sm:grid-cols-3">
          {STATUS_FILTERS.map((filter) => (
            <button key={filter.value} type="button" onClick={() => setStatus(filter.value)} className={`relative flex flex-col p-6 bg-white rounded-xl border transition-all text-left overflow-hidden ${status === filter.value ? "border-red-500 shadow-md ring-1 ring-red-500" : "border-gray-200 shadow-sm hover:border-gray-300 hover:shadow-md"}`}>
              {status === filter.value && <div className="absolute top-0 left-0 w-full h-1 bg-red-600" />}
              <div className="flex items-center justify-between w-full">
                <span className={`text-sm font-medium ${status === filter.value ? 'text-red-700' : 'text-gray-500'}`}>{filter.label}</span>
                <div className={`${status === filter.value ? 'text-red-600' : 'text-gray-400'}`}>{filter.icon}</div>
              </div>
              <p className="mt-4 text-3xl font-bold text-gray-900 tracking-tight">{counts[filter.value]}</p>
            </button>
          ))}
        </div>

        {error && (
          <div className="flex items-center gap-3 p-4 text-sm text-red-800 border border-red-200 rounded-lg bg-red-50">
            <AlertCircle size={18} className="text-red-600" />
            <span className="font-medium">{error}</span>
          </div>
        )}
        
        {successMsg && (
          <div className="flex items-center gap-3 p-4 text-sm text-green-800 border border-green-200 rounded-lg bg-green-50">
            <CheckCircle2 size={18} className="text-green-600" />
            <span className="font-medium">{successMsg}</span>
          </div>
        )}

        {loading ? (
          <div className="flex flex-col items-center justify-center p-12 bg-white rounded-xl border border-gray-200 shadow-sm">
            <RefreshCw size={24} className="animate-spin text-gray-400 mb-4" />
            <p className="text-gray-500 font-medium">Loading approvals...</p>
          </div>
        ) : displayedApprovals.length === 0 ? (
          <div className="flex flex-col items-center justify-center p-12 bg-white rounded-xl border border-gray-200 shadow-sm text-center">
            <div className="w-12 h-12 bg-gray-50 rounded-full flex items-center justify-center mb-4 border border-gray-100"><CheckCircle2 size={24} className="text-gray-400" /></div>
            <h3 className="text-lg font-medium text-gray-900">No {status.toLowerCase()} approvals</h3>
            <p className="mt-1 text-sm text-gray-500">Recommendations will appear here after they are sent into the Stage 1 workflow.</p>
          </div>
        ) : (
          <div className="space-y-6">
            {displayedApprovals.map((approval) => {
              const recommendation = approval.recommendation;
              const isPending = approval.status === "PENDING";
              const isBusy = actionId === recommendation.recommendation_id;
              return (
                <div key={approval.approval_id} className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden flex flex-col">
                  <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4 px-6 py-5 border-b border-gray-100 bg-gray-50/50">
                    <div>
                      <h3 className="text-lg font-semibold text-gray-900">{displayValue(recommendation.title)}</h3>
                      <div className="flex flex-wrap items-center gap-2 mt-1.5 text-sm text-gray-500">
                        <span className="font-medium text-gray-700">{displayValue(recommendation.resource)}</span>
                        <span>&bull;</span>
                        <span className="font-mono text-xs bg-gray-100 px-1.5 py-0.5 rounded border border-gray-200">{displayValue(recommendation.resource_id)}</span>
                        <span>&bull;</span>
                        <span>{displayValue(recommendation.domain)}</span>
                      </div>
                    </div>
                    <span className={`inline-flex items-center px-2.5 py-1 rounded-full text-xs font-semibold uppercase tracking-wide ${approval.status === "PENDING" ? "bg-amber-50 text-amber-700 border border-amber-200" : approval.status === "APPROVED" ? "bg-green-50 text-green-700 border border-green-200" : "bg-gray-100 text-gray-700 border border-gray-200"}`}>
                      {approval.status}
                    </span>
                  </div>

                  <div className="px-6 py-6 grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-6">
                    <div className="space-y-6">
                      <div>
                        <h4 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">Context</h4>
                        <dl className="space-y-3 text-sm">
                          <div className="flex flex-col sm:flex-row sm:justify-between gap-1">
                            <dt className="text-gray-500">Finding Type:</dt>
                            <dd className="font-medium text-gray-900 text-right">{displayValue(recommendation.finding_type)}</dd>
                          </div>
                          <div className="flex flex-col sm:flex-row sm:justify-between gap-1">
                            <dt className="text-gray-500">Environment:</dt>
                            <dd className="font-medium text-gray-900 text-right">{displayValue(recommendation.workspace_name)}</dd>
                          </div>
                          <div className="flex flex-col sm:flex-row sm:justify-between gap-1">
                            <dt className="text-gray-500">Rule ID:</dt>
                            <dd className="font-mono text-xs text-gray-600 bg-gray-50 px-1.5 py-0.5 rounded border border-gray-100 text-right">{displayValue(recommendation.rule_id)}</dd>
                          </div>
                          <div className="flex flex-col sm:flex-row sm:justify-between gap-1">
                            <dt className="text-gray-500">Requested By:</dt>
                            <dd className="font-medium text-gray-900 text-right">{displayValue(approval.requested_by)}</dd>
                          </div>
                          <div className="flex flex-col sm:flex-row sm:justify-between gap-1">
                            <dt className="text-gray-500">Created At:</dt>
                            <dd className="font-medium text-gray-900 text-right">{approval.created_at ? new Date(approval.created_at).toLocaleString() : "Not available"}</dd>
                          </div>
                        </dl>
                      </div>

                      <div>
                        <h4 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">Analysis</h4>
                        <dl className="space-y-3 text-sm">
                          <div className="flex flex-col sm:flex-row sm:justify-between gap-1">
                            <dt className="text-gray-500">Risk:</dt>
                            <dd className="font-medium text-gray-900 text-right">{displayValue(recommendation.risk)}</dd>
                          </div>
                          <div className="flex flex-col sm:flex-row sm:justify-between gap-1">
                            <dt className="text-gray-500">Confidence:</dt>
                            <dd className="font-medium text-gray-900 text-right">{displayValue(recommendation.confidence)}</dd>
                          </div>
                          <div className="flex flex-col sm:flex-row sm:justify-between gap-1">
                            <dt className="text-gray-500">Impact / Savings:</dt>
                            <dd className="font-medium text-green-700 text-right">
                              {recommendation.estimated_savings ? jsonText(recommendation.estimated_savings) : displayValue(recommendation.expected_impact?.description)}
                            </dd>
                          </div>
                        </dl>
                      </div>
                    </div>

                    <div className="space-y-6">
                      <div>
                        <h4 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">State Changes</h4>
                        <div className="space-y-4">
                          <div>
                            <span className="block text-xs text-gray-500 mb-1">Current State</span>
                            <div className="bg-gray-50 border border-gray-200 rounded-md p-3 text-xs font-mono text-gray-700 overflow-x-auto whitespace-pre-wrap max-h-32 overflow-y-auto">{jsonText(recommendation.current_state)}</div>
                          </div>
                          <div>
                            <span className="block text-xs text-gray-500 mb-1">Proposed Direction</span>
                            <div className="bg-red-50/50 border border-red-100 rounded-md p-3 text-xs font-mono text-gray-900 overflow-x-auto whitespace-pre-wrap max-h-32 overflow-y-auto">{jsonText(recommendation.proposed_state)}</div>
                          </div>
                        </div>
                      </div>
                      <div>
                        <h4 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">Evidence</h4>
                        <div className="bg-gray-50 border border-gray-200 rounded-md p-3 text-xs font-mono text-gray-700 overflow-x-auto whitespace-pre-wrap max-h-32 overflow-y-auto">{jsonText(recommendation.evidence)}</div>
                      </div>
                    </div>
                  </div>

                  <div className="px-6 py-4 bg-gray-50 border-t border-gray-200">
                    {isPending ? (
                      <div>
                        {rejectingId === recommendation.recommendation_id ? (
                          <div className="bg-white border border-gray-300 rounded-lg p-4 shadow-sm">
                            <label htmlFor="reason" className="block text-sm font-medium text-gray-900 mb-1">Rejection Reason</label>
                            <p className="text-xs text-gray-500 mb-3">A reason is required (min 3 chars) and will be recorded in the audit log.</p>
                            <textarea id="reason" value={rejectReason} onChange={(e) => setRejectReason(e.target.value)} rows={2} className="block w-full rounded-md border border-gray-300 px-3 py-2 text-sm text-gray-900 placeholder-gray-400 focus:border-red-500 focus:outline-none focus:ring-1 focus:ring-red-500" placeholder="Enter reason for rejecting this recommendation..." />
                            <div className="mt-4 flex gap-3">
                              <button onClick={() => void rejectRecommendation(recommendation.recommendation_id)} disabled={isBusy || rejectReason.trim().length < 3} className="inline-flex justify-center rounded-md bg-red-600 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-red-700 focus:outline-none focus:ring-2 focus:ring-red-500 focus:ring-offset-2 disabled:opacity-50 transition-colors">
                                {isBusy ? "Processing..." : "Confirm Rejection"}
                              </button>
                              <button onClick={() => { setRejectingId(null); setRejectReason(""); }} disabled={isBusy} className="inline-flex justify-center rounded-md bg-white border border-gray-300 px-4 py-2 text-sm font-medium text-gray-700 shadow-sm hover:bg-gray-50 focus:outline-none transition-colors">
                                Cancel
                              </button>
                            </div>
                          </div>
                        ) : (
                          <div className="flex flex-wrap gap-3 justify-end items-center">
                            <span className="text-xs text-gray-500 mr-auto flex items-center gap-1.5">
                              <AlertCircle size={14} /> Execution will not start automatically.
                            </span>
                            <button onClick={() => { setRejectingId(recommendation.recommendation_id); setRejectReason(""); setError(null); }} disabled={isBusy} className="inline-flex justify-center rounded-md bg-white border border-gray-300 px-4 py-2 text-sm font-medium text-gray-700 shadow-sm hover:bg-gray-50 focus:outline-none transition-colors">
                              Reject
                            </button>
                            <button onClick={() => void approveRecommendation(recommendation.recommendation_id)} disabled={isBusy} className="inline-flex justify-center rounded-md bg-red-600 px-6 py-2 text-sm font-medium text-white shadow-sm hover:bg-red-700 focus:outline-none focus:ring-2 focus:ring-red-500 focus:ring-offset-2 disabled:opacity-50 transition-colors">
                              {isBusy ? "Processing..." : "Approve"}
                            </button>
                          </div>
                        )}
                      </div>
                    ) : (
                      <div className="flex items-center text-sm text-gray-600">
                        {approval.status === "APPROVED" ? (
                          <div className="flex items-center gap-2">
                            <CheckCircle2 size={16} className="text-green-600" />
                            <span>
                              Approved by <span className="font-medium text-gray-900">{displayValue(approval.decided_by)}</span>
                              {approval.decided_at ? ` on ${new Date(approval.decided_at).toLocaleString()}` : ""}. Execution is not started.
                            </span>
                          </div>
                        ) : (
                          <div className="flex items-center gap-2">
                            <XCircle size={16} className="text-gray-400" />
                            <span>
                              Rejected by <span className="font-medium text-gray-900">{displayValue(approval.decided_by)}</span>
                              {approval.decided_at ? ` on ${new Date(approval.decided_at).toLocaleString()}` : ""}.
                            </span>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </Layout>
  );
}
