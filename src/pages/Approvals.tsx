import React, { useEffect, useMemo, useState } from "react";
import { AlertCircle, Check, Clock3, RefreshCw, ShieldCheck, X } from "lucide-react";
import Layout from "../components/Layout";
import PageHeader from "../components/PageHeader";
import { StatePanel } from "../components/StateBlock";
import { useMsal } from "@azure/msal-react";

type ApprovalStatus = "pending" | "approved" | "rejected";
type TabStatus = "PENDING" | "APPROVED" | "REJECTED";

interface Stage1Recommendation {
  id: string;
  domain: string | null;
  resource: string | null;
  resource_id: string | null;
  resource_type: string | null;
  finding_id: string | null;
  rule_id: string | null;
  finding_type: string | null;
  title: string | null;
  summary: string | null;
  description: string | null;
  current_state: unknown;
  proposed_change: string | null;
  proposed_state: unknown;
  evidence: unknown;
  evidence_references: unknown;
  evidence_quality: unknown;
  observation_window: unknown;
  expected_impact: unknown;
  estimated_savings: unknown;
  confidence: number | null;
  severity: string | null;
  risk: string | null;
  approval_status: string | null;
  policy_status: string | null;
  execution_status: string | null;
  verification_status: string | null;
  customer_id: string | null;
  environment_id: string | null;
  workspace_name: string | null;
}

interface Stage1Approval {
  approval_id: string | null;
  recommendation_id: string;
  status: ApprovalStatus | null;
  requested_by: string | null;
  decided_by: string | null;
  created_at: string | null;
  decided_at: string | null;
  recommendation: Stage1Recommendation;
}

interface ConfirmState {
  type: "approve" | "reject";
  approval: Stage1Approval;
}

const TABS: { id: TabStatus; label: string }[] = [
  { id: "PENDING", label: "Pending" },
  { id: "APPROVED", label: "Approved" },
  { id: "REJECTED", label: "Rejected" },
];

function safeJson(value: unknown): string {
  if (value === null || value === undefined) {
    return "Not available";
  }

  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function formatDate(value: string | null): string {
  if (!value) return "Not available";

  const parsed = new Date(value);

  if (Number.isNaN(parsed.getTime())) {
    return value;
  }

  return parsed.toLocaleString();
}

function titleFor(approval: Stage1Approval): string {
  return (
    approval.recommendation.title?.trim() ||
    approval.recommendation.finding_type ||
    "Optimization recommendation"
  );
}

function resourceFor(approval: Stage1Approval): string {
  return (
    approval.recommendation.resource ||
    approval.recommendation.resource_id ||
    "Databricks resource"
  );
}

function statusLabel(status: ApprovalStatus | null): string {
  switch (status) {
    case "approved":
      return "Approved";
    case "rejected":
      return "Rejected";
    case "pending":
      return "Pending approval";
    default:
      return "Awaiting review";
  }
}

function statusClasses(status: ApprovalStatus | null): string {
  switch (status) {
    case "approved":
      return "border-emerald-200 bg-emerald-50 text-emerald-700";
    case "rejected":
      return "border-slate-200 bg-slate-100 text-slate-600";
    case "pending":
      return "border-red-200 bg-red-50 text-red-700";
    default:
      return "border-slate-200 bg-slate-50 text-slate-600";
  }
}

function actorFromMsal(instance: ReturnType<typeof useMsal>["instance"]): {
  id: string;
  name: string;
} {
  const account =
    instance.getActiveAccount() ?? instance.getAllAccounts()[0] ?? null;

  return {
    id:
      account?.localAccountId?.trim() ||
      account?.username?.trim() ||
      "demo-user",
    name:
      account?.name?.trim() ||
      account?.username?.trim() ||
      "Demo User",
  };
}

function formatDomain(domain: string | null): string {
  switch (domain) {
    case "cluster_sizing":
      return "Cluster sizing";
    case "cluster_runtime":
      return "Cluster runtime";
    case "autoscaling":
      return "Autoscaling";
    case "cluster":
      return "Cluster optimization";
    default:
      return domain || "Optimization";
  }
}

function confidenceLabel(value: number | null): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "Not available";
  }

  const percent = value <= 1 ? value * 100 : value;

  return `${Math.round(percent)}%`;
}

function ApprovalCard({
  approval,
  onApprove,
  onReject,
  busy,
}: {
  approval: Stage1Approval;
  onApprove: () => void;
  onReject: () => void;
  busy: boolean;
}) {
  const recommendation = approval.recommendation;
  const isPending = approval.status === "pending";

  return (
    <article
      data-testid="stage1-approval-card"
      className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm transition-shadow hover:shadow-md"
    >
      <div className="flex flex-col gap-5">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className="inline-flex items-center gap-1.5 rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 text-xs font-medium text-slate-600">
                <ShieldCheck size={13} />
                {formatDomain(recommendation.domain)}
              </span>

              {recommendation.resource_type && (
                <span className="rounded-full border border-slate-200 bg-white px-2.5 py-1 text-xs text-slate-500">
                  {recommendation.resource_type}
                </span>
              )}
            </div>

            <h2 className="mt-3 text-lg font-semibold tracking-tight text-slate-900">
              {titleFor(approval)}
            </h2>

            <p className="mt-1 text-sm text-slate-500">
              {resourceFor(approval)}
            </p>
          </div>

          <span
            data-testid="approval-status"
            className={`inline-flex shrink-0 items-center rounded-full border px-3 py-1 text-xs font-semibold ${statusClasses(
              approval.status
            )}`}
          >
            {statusLabel(approval.status)}
          </span>
        </div>

        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          <InfoBlock
            label="Cluster / Resource"
            value={resourceFor(approval)}
          />
          <InfoBlock
            label="Rule"
            value={recommendation.rule_id || "Not available"}
          />
          <InfoBlock
            label="Finding"
            value={recommendation.finding_type || "Not available"}
          />
          <InfoBlock
            label="Confidence"
            value={confidenceLabel(recommendation.confidence)}
          />
        </div>

        {recommendation.summary && (
          <section className="rounded-xl border border-slate-200 bg-slate-50 p-4">
            <p className="text-xs font-semibold uppercase tracking-[0.12em] text-slate-400">
              Summary
            </p>
            <p className="mt-2 text-sm leading-6 text-slate-700">
              {recommendation.summary}
            </p>
          </section>
        )}

        <div className="grid gap-4 lg:grid-cols-2">
          <JsonBlock
            label="Current state"
            value={recommendation.current_state}
          />
          <JsonBlock
            label="Proposed state"
            value={recommendation.proposed_state}
          />
        </div>

        {recommendation.proposed_change && (
          <section className="rounded-xl border border-red-100 bg-red-50/60 p-4">
            <p className="text-xs font-semibold uppercase tracking-[0.12em] text-red-500">
              Proposed direction
            </p>
            <p className="mt-2 text-sm leading-6 text-slate-800">
              {recommendation.proposed_change}
            </p>
          </section>
        )}

        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          <InfoBlock
            label="Risk"
            value={recommendation.risk || "Not available"}
          />
          <InfoBlock
            label="Severity"
            value={recommendation.severity || "Not available"}
          />
          <InfoBlock
            label="Created"
            value={formatDate(approval.created_at)}
          />
          <InfoBlock
            label="Requested by"
            value={approval.requested_by || "ACELO Agent"}
          />
        </div>

        <div className="grid gap-4 lg:grid-cols-2">
          <JsonBlock
            label="Evidence"
            value={recommendation.evidence}
          />
          <JsonBlock
            label="Expected impact"
            value={recommendation.expected_impact}
          />
        </div>

        <JsonBlock
          label="Evidence quality"
          value={recommendation.evidence_quality}
        />

        {approval.status !== "pending" && (
          <div className="flex flex-wrap items-center gap-2 rounded-xl border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-slate-600">
            <Clock3 size={15} />
            <span>
              {approval.status === "approved"
                ? `Approved by ${
                    approval.decided_by || "reviewer"
                  }`
                : `Rejected by ${
                    approval.decided_by || "reviewer"
                  }`}
              {approval.decided_at
                ? ` · ${formatDate(approval.decided_at)}`
                : ""}
            </span>
          </div>
        )}

        {isPending && (
          <div className="flex flex-col gap-3 border-t border-slate-100 pt-4 sm:flex-row sm:justify-end">
            <button
              type="button"
              data-testid="reject-button"
              disabled={busy}
              onClick={onReject}
              className="inline-flex items-center justify-center gap-2 rounded-xl border border-slate-300 bg-white px-4 py-2.5 text-sm font-semibold text-slate-700 transition hover:border-red-300 hover:text-red-600 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <X size={15} />
              Reject
            </button>

            <button
              type="button"
              data-testid="approve-button"
              disabled={busy}
              onClick={onApprove}
              className="inline-flex items-center justify-center gap-2 rounded-xl bg-red-600 px-5 py-2.5 text-sm font-semibold text-white shadow-sm transition hover:bg-red-700 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <Check size={15} />
              Approve
            </button>
          </div>
        )}
      </div>
    </article>
  );
}

function InfoBlock({
  label,
  value,
}: {
  label: string;
  value: string;
}) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4">
      <p className="text-xs font-semibold uppercase tracking-[0.1em] text-slate-400">
        {label}
      </p>
      <p className="mt-2 break-words text-sm font-medium text-slate-800">
        {value}
      </p>
    </div>
  );
}

function JsonBlock({
  label,
  value,
}: {
  label: string;
  value: unknown;
}) {
  const unavailable =
    value === null ||
    value === undefined ||
    (typeof value === "object" &&
      value !== null &&
      Object.keys(value as Record<string, unknown>).length === 0);

  return (
    <section className="rounded-xl border border-slate-200 bg-white p-4">
      <p className="text-xs font-semibold uppercase tracking-[0.1em] text-slate-400">
        {label}
      </p>

      {unavailable ? (
        <p className="mt-2 text-sm text-slate-400">Not available</p>
      ) : (
        <pre className="mt-3 max-h-56 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-slate-50 p-3 font-mono text-xs leading-5 text-slate-700">
          {safeJson(value)}
        </pre>
      )}
    </section>
  );
}

function ConfirmationDialog({
  state,
  reason,
  setReason,
  onCancel,
  onConfirm,
  busy,
}: {
  state: ConfirmState;
  reason: string;
  setReason: (value: string) => void;
  onCancel: () => void;
  onConfirm: () => void;
  busy: boolean;
}) {
  const isApprove = state.type === "approve";
  const reasonValid = reason.trim().length >= 3;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/45 p-4">
      <div
        role="dialog"
        aria-modal="true"
        className="w-full max-w-lg rounded-2xl border border-slate-200 bg-white p-6 shadow-2xl"
      >
        <div className="flex items-start justify-between gap-4">
          <div>
            <p className="text-xs font-semibold uppercase tracking-[0.12em] text-red-600">
              Stage 1 approval
            </p>
            <h2 className="mt-2 text-xl font-semibold text-slate-900">
              {isApprove ? "Confirm approval" : "Reject recommendation"}
            </h2>
          </div>

          <button
            type="button"
            onClick={onCancel}
            className="rounded-lg p-2 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
            aria-label="Close"
          >
            <X size={18} />
          </button>
        </div>

        <p className="mt-4 text-sm leading-6 text-slate-600">
          {isApprove
            ? "This will mark the recommendation as APPROVED. No Databricks resource will be changed."
            : "This will mark the recommendation as REJECTED. A reason is required for the audit record."}
        </p>

        <div className="mt-5 rounded-xl border border-slate-200 bg-slate-50 p-4">
          <p className="text-xs font-semibold uppercase tracking-[0.1em] text-slate-400">
            Recommendation
          </p>
          <p className="mt-2 text-sm font-semibold text-slate-800">
            {titleFor(state.approval)}
          </p>
          <p className="mt-1 text-xs text-slate-500">
            {resourceFor(state.approval)}
          </p>
        </div>

        {!isApprove && (
          <div className="mt-5">
            <label
              htmlFor="rejection-reason"
              className="text-sm font-semibold text-slate-800"
            >
              Rejection reason
            </label>

            <textarea
              id="rejection-reason"
              data-testid="reject-reason"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              rows={4}
              placeholder="Enter at least 3 characters..."
              className="mt-2 w-full resize-none rounded-xl border border-slate-300 bg-white px-3 py-3 text-sm text-slate-900 outline-none transition focus:border-red-500 focus:ring-2 focus:ring-red-100"
            />

            <p className="mt-1 text-xs text-slate-400">
              Minimum 3 characters.
            </p>
          </div>
        )}

        <div className="mt-6 flex flex-col-reverse gap-3 sm:flex-row sm:justify-end">
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="rounded-xl border border-slate-300 bg-white px-4 py-2.5 text-sm font-semibold text-slate-700 hover:bg-slate-50 disabled:opacity-50"
          >
            Cancel
          </button>

          <button
            type="button"
            data-testid="confirm-action"
            onClick={onConfirm}
            disabled={busy || (!isApprove && !reasonValid)}
            className={`rounded-xl px-5 py-2.5 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-50 ${
              isApprove
                ? "bg-red-600 hover:bg-red-700"
                : "bg-slate-800 hover:bg-slate-900"
            }`}
          >
            {busy
              ? "Saving..."
              : isApprove
                ? "Confirm approval"
                : "Confirm rejection"}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function Approvals() {
  const { instance } = useMsal();

  const [approvals, setApprovals] = useState<Stage1Approval[]>([]);
  const [activeTab, setActiveTab] = useState<TabStatus>("PENDING");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [confirmState, setConfirmState] = useState<ConfirmState | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);

  const counts = useMemo(
    () => ({
      PENDING: approvals.filter((a) => a.status === "pending").length,
      APPROVED: approvals.filter((a) => a.status === "approved").length,
      REJECTED: approvals.filter((a) => a.status === "rejected").length,
    }),
    [approvals]
  );

  const visibleApprovals = useMemo(
    () =>
      approvals.filter(
        (approval) =>
          (approval.status || "").toUpperCase() === activeTab
      ),
    [approvals, activeTab]
  );

  async function loadApprovals(showLoader = true) {
    if (showLoader) {
      setLoading(true);
    } else {
      setRefreshing(true);
    }

    setError(null);

    try {
      const response = await fetch("/api/approvals/recommendations");

      if (!response.ok) {
        let detail = `Unable to load Stage 1 approvals (HTTP ${response.status}).`;

        try {
          const body = (await response.json()) as {
            detail?: string;
            message?: string;
          };

          detail = body.detail ?? body.message ?? detail;
        } catch {
          // Keep HTTP fallback.
        }

        throw new Error(detail);
      }

      const body = (await response.json()) as unknown;

      if (!Array.isArray(body)) {
        throw new Error("The Stage 1 approval response is invalid.");
      }

      setApprovals(body as Stage1Approval[]);
    } catch (e: unknown) {
      setError(
        e instanceof Error
          ? e.message
          : "Unable to load Stage 1 approvals."
      );
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }

  useEffect(() => {
    void loadApprovals();
  }, []);

  function openConfirm(
    type: "approve" | "reject",
    approval: Stage1Approval
  ) {
    setFeedback(null);
    setReason("");
    setConfirmState({ type, approval });
  }

  function closeConfirm() {
    if (busy) return;

    setConfirmState(null);
    setReason("");
  }

  async function confirmAction() {
    if (!confirmState) return;

    const approval = confirmState.approval;
    const recommendationId = approval.recommendation_id;

    if (!recommendationId) {
      setFeedback("This approval has no recommendation ID.");
      return;
    }

    if (
      confirmState.type === "reject" &&
      reason.trim().length < 3
    ) {
      setFeedback("A rejection reason of at least 3 characters is required.");
      return;
    }

    setBusy(true);
    setError(null);
    setFeedback(null);

    try {
      const actor = actorFromMsal(instance);

      const endpoint =
        confirmState.type === "approve"
          ? `/api/approvals/recommendations/${encodeURIComponent(
              recommendationId
            )}/approve`
          : `/api/approvals/recommendations/${encodeURIComponent(
              recommendationId
            )}/reject`;

      const response = await fetch(endpoint, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Acelo-User-Id": actor.id,
          "X-Acelo-User-Name": actor.name,
        },
        ...(confirmState.type === "reject"
          ? {
              body: JSON.stringify({
                reason: reason.trim(),
              }),
            }
          : {}),
      });

      if (!response.ok) {
        let detail = `Unable to save approval decision (HTTP ${response.status}).`;

        try {
          const body = (await response.json()) as {
            detail?: string;
            message?: string;
          };

          detail = body.detail ?? body.message ?? detail;
        } catch {
          // Keep HTTP fallback.
        }

        throw new Error(detail);
      }

      setConfirmState(null);
      setReason("");

      setFeedback(
        confirmState.type === "approve"
          ? "Recommendation approved successfully."
          : "Recommendation rejected successfully."
      );

      await loadApprovals(false);
    } catch (e: unknown) {
      setError(
        e instanceof Error
          ? e.message
          : "The approval action could not be completed."
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <Layout pageName="Approvals">
      <div className="mx-auto flex w-full max-w-7xl flex-col gap-6">
        <PageHeader
          title="Approvals"
          description="Review evidence-backed Databricks optimization recommendations before any action is allowed."
        />

        <section className="rounded-2xl border border-red-100 bg-gradient-to-r from-red-50 to-white p-5">
          <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
            <div>
              <div className="flex items-center gap-2">
                <ShieldCheck size={18} className="text-red-600" />
                <p className="text-sm font-semibold text-slate-900">
                  Stage 1 governance checkpoint
                </p>
              </div>
              <p className="mt-1 max-w-3xl text-sm leading-6 text-slate-600">
                Approval changes the recommendation state only. It does not
                create an execution or modify Databricks.
              </p>
            </div>

            <button
              type="button"
              onClick={() => void loadApprovals(false)}
              disabled={refreshing}
              className="inline-flex items-center justify-center gap-2 rounded-xl border border-slate-300 bg-white px-4 py-2.5 text-sm font-semibold text-slate-700 shadow-sm transition hover:border-red-300 hover:text-red-600 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <RefreshCw
                size={15}
                className={refreshing ? "animate-spin" : ""}
              />
              Refresh
            </button>
          </div>
        </section>

        {feedback && (
          <div
            data-testid="approval-success"
            className="flex items-start gap-3 rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3"
          >
            <Check size={18} className="mt-0.5 shrink-0 text-emerald-600" />
            <p className="text-sm font-medium text-emerald-800">
              {feedback}
            </p>
          </div>
        )}

        {error && (
          <div
            data-testid="approval-error"
            className="flex items-start gap-3 rounded-xl border border-red-200 bg-red-50 px-4 py-3"
          >
            <AlertCircle
              size={18}
              className="mt-0.5 shrink-0 text-red-600"
            />
            <div>
              <p className="text-sm font-semibold text-red-800">
                Approval action could not be completed.
              </p>
              <p className="mt-1 text-sm text-red-700">{error}</p>
            </div>
          </div>
        )}

        <section className="rounded-2xl border border-slate-200 bg-white p-2 shadow-sm">
          <div
            role="tablist"
            aria-label="Approval status"
            className="grid gap-2 sm:grid-cols-3"
          >
            {TABS.map((tab) => {
              const active = activeTab === tab.id;
              const count = counts[tab.id];

              return (
                <button
                  key={tab.id}
                  type="button"
                  role="tab"
                  aria-selected={active}
                  onClick={() => setActiveTab(tab.id)}
                  className={`flex items-center justify-between rounded-xl px-4 py-3 text-sm font-semibold transition ${
                    active
                      ? "bg-red-600 text-white shadow-sm"
                      : "text-slate-600 hover:bg-slate-50"
                  }`}
                >
                  <span>{tab.label}</span>
                  <span
                    className={`rounded-full px-2 py-0.5 text-xs ${
                      active
                        ? "bg-white/15 text-white"
                        : "bg-slate-100 text-slate-600"
                    }`}
                  >
                    {count}
                  </span>
                </button>
              );
            })}
          </div>
        </section>

        {loading && (
          <StatePanel
            kind="loading"
            title="Loading Stage 1 approvals..."
          />
        )}

        {!loading && !error && visibleApprovals.length === 0 && (
          <section className="rounded-2xl border border-dashed border-slate-300 bg-white p-10 text-center">
            <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-slate-100">
              <ShieldCheck size={20} className="text-slate-500" />
            </div>

            <h2 className="mt-4 text-base font-semibold text-slate-900">
              No {activeTab.toLowerCase()} approvals
            </h2>

            <p className="mx-auto mt-2 max-w-lg text-sm leading-6 text-slate-500">
              Recommendations sent from the Databricks optimization flow will
              appear here for human review.
            </p>
          </section>
        )}

        {!loading && !error && visibleApprovals.length > 0 && (
          <div className="flex flex-col gap-5">
            {visibleApprovals.map((approval) => (
              <ApprovalCard
                key={approval.approval_id || approval.recommendation_id}
                approval={approval}
                busy={
                  busy &&
                  confirmState?.approval.recommendation_id ===
                    approval.recommendation_id
                }
                onApprove={() => openConfirm("approve", approval)}
                onReject={() => openConfirm("reject", approval)}
              />
            ))}
          </div>
        )}

        {!loading && (
          <p className="text-xs text-slate-400">
            Stage 1 ends at APPROVED / REJECTED. No execution API is called
            from this page.
          </p>
        )}
      </div>

      {confirmState && (
        <ConfirmationDialog
          state={confirmState}
          reason={reason}
          setReason={setReason}
          onCancel={closeConfirm}
          onConfirm={() => void confirmAction()}
          busy={busy}
        />
      )}
    </Layout>
  );
}