import React, { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useMsal } from "@azure/msal-react";
import { ArrowRight, Check, ShieldCheck, X } from "lucide-react";
import Layout from "../components/Layout";
import PageHeader from "../components/PageHeader";
import Button from "../components/Button";
import {
  ConfirmationDialog,
  EmptyState,
  ErrorState,
  KeyValueGrid,
  LoadingState,
  Notice,
  StatusPill,
  WorkflowIndicator,
  formatDateTime,
  severityTone,
  titleCase,
} from "../components/ui";
import {
  RequestError,
  approveRecommendation,
  latestApprovalByRecommendation,
  lifecycleOf,
  listApprovalRecords,
  parseMaybeJson,
  rejectRecommendation,
  reviewerFromAccount,
  type ApprovalRecord,
} from "../services/stage1Api";

/**
 * Approvals — the human checkpoint between a recommendation and Execution.
 *
 * Reads persisted approval records only (GET /api/approvals/recommendations)
 * and reloads them after every decision, so counts and states always match the
 * backend. Approval records authorization; it never executes anything.
 * Approved recommendations continue in Execution as "Ready to Execute".
 */

type Tab = "PENDING" | "APPROVED" | "REJECTED";
const TABS: { id: Tab; label: string }[] = [
  { id: "PENDING", label: "Pending" },
  { id: "APPROVED", label: "Approved" },
  { id: "REJECTED", label: "Rejected" },
];

function tabFrom(value: string | null): Tab {
  const v = (value ?? "").toUpperCase();
  return v === "APPROVED" || v === "REJECTED" ? v : "PENDING";
}

interface Confirm {
  type: "approve" | "reject";
  record: ApprovalRecord;
}

export default function Approvals() {
  const navigate = useNavigate();
  const { instance } = useMsal();
  const [params, setParams] = useSearchParams();
  const tab = tabFrom(params.get("status"));
  const [records, setRecords] = useState<ApprovalRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<{ text: string; technical: string | null } | null>(null);
  const [notice, setNotice] = useState<{ tone: "success" | "danger"; text: string; technical?: string | null } | null>(null);
  const [confirm, setConfirm] = useState<Confirm | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setRecords(await listApprovalRecords());
      setError(null);
    } catch (e: unknown) {
      setError({
        text: e instanceof RequestError ? e.message : "Could not load approvals.",
        technical: e instanceof RequestError ? e.technical : null,
      });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // One row per recommendation: its newest approval record.
  const current = useMemo(() => [...latestApprovalByRecommendation(records).values()], [records]);
  const counts = useMemo(() => {
    const c: Record<Tab, number> = { PENDING: 0, APPROVED: 0, REJECTED: 0 };
    for (const r of current) {
      const s = (r.status ?? "").toUpperCase();
      if (s === "PENDING" || s === "APPROVED" || s === "REJECTED") c[s] += 1;
    }
    return c;
  }, [current]);
  const visible = current
    .filter((r) => (r.status ?? "").toUpperCase() === tab)
    .sort((a, b) => ((b.decided_at ?? b.created_at ?? "") > (a.decided_at ?? a.created_at ?? "") ? 1 : -1));

  function selectTab(next: Tab) {
    const p = new URLSearchParams(params);
    p.set("status", next);
    setParams(p, { replace: true });
  }

  function reviewer() {
    let account = null;
    try {
      account = instance.getActiveAccount() ?? instance.getAllAccounts()[0] ?? null;
    } catch {
      account = null;
    }
    return reviewerFromAccount(account);
  }

  async function decide() {
    if (!confirm) return;
    const { type, record } = confirm;
    if (type === "reject" && reason.trim().length < 3) return;
    setBusy(true);
    setNotice(null);
    try {
      if (type === "approve") {
        await approveRecommendation(record.recommendation_id, reviewer());
        setNotice({
          tone: "success",
          text: "Approved. The recommendation is now in Execution as Ready to Execute. Nothing was executed.",
        });
      } else {
        await rejectRecommendation(record.recommendation_id, reviewer(), reason.trim());
        setNotice({ tone: "success", text: "Rejected. The decision and reason were recorded." });
      }
      setConfirm(null);
      setReason("");
    } catch (e: unknown) {
      setNotice({
        tone: "danger",
        text: e instanceof RequestError ? e.message : "The decision could not be recorded.",
        technical: e instanceof RequestError ? e.technical : null,
      });
      setConfirm(null);
    } finally {
      setBusy(false);
      await load();
    }
  }

  return (
    <Layout pageName="Approvals" onRefresh={() => void load()}>
      <div className="flex flex-col gap-6">
        <PageHeader
          eyebrow="Lifecycle"
          title="Approvals"
          description="Review evidence-backed recommendations. Approval records authorization only — it never executes anything. Approved recommendations move to Execution as Ready to Execute."
        />
        <WorkflowIndicator current="approval" />

        {notice && (
          <Notice tone={notice.tone} testId={notice.tone === "success" ? "approval-success" : "approval-error"}>
            {notice.text}
            {notice.technical && <span className="mt-1 block font-mono text-[11px] opacity-80">{notice.technical}</span>}
          </Notice>
        )}

        <div role="tablist" aria-label="Approval status" className="grid gap-2 sm:grid-cols-3">
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              role="tab"
              aria-selected={tab === t.id}
              onClick={() => selectTab(t.id)}
              className={`card flex items-center justify-between px-4 py-3 text-sm font-medium transition-colors ${
                tab === t.id ? "border-brand-500 bg-brand-500 text-white" : "text-ink-muted hover:border-brand-300 hover:text-ink"
              }`}
            >
              <span>{t.label}</span>
              <span
                className={`tabular rounded-full px-2 py-0.5 text-xs ${
                  tab === t.id ? "bg-white/20 text-white" : "bg-canvas-raised text-ink-muted"
                }`}
              >
                {loading && records.length === 0 ? "—" : counts[t.id]}
              </span>
            </button>
          ))}
        </div>

        {loading && records.length === 0 && <LoadingState title="Loading approvals…" />}

        {error && (
          <ErrorState
            title="Could not load approvals"
            detail={error.text}
            technical={error.technical}
            action={
              <Button variant="secondary" onClick={() => void load()}>
                Try again
              </Button>
            }
          />
        )}

        {!loading && !error && visible.length === 0 && (
          <EmptyState
            title={`No ${tab.toLowerCase()} approvals`}
            detail={
              tab === "PENDING"
                ? "Recommendations sent for approval from the Recommendations page appear here for review."
                : `No recommendation is currently ${tab.toLowerCase()}.`
            }
            action={
              tab === "PENDING" ? (
                <Button variant="secondary" onClick={() => navigate("/recommendations")}>
                  Open Recommendations
                </Button>
              ) : undefined
            }
          />
        )}

        {!error && visible.length > 0 && (
          <ol className="flex flex-col gap-4">
            {visible.map((record) => (
              <ApprovalCard
                key={record.approval_id ?? record.recommendation_id}
                record={record}
                disabled={busy}
                onApprove={() => setConfirm({ type: "approve", record })}
                onReject={() => {
                  setReason("");
                  setConfirm({ type: "reject", record });
                }}
                onOpenExecution={() => navigate("/execution")}
              />
            ))}
          </ol>
        )}
      </div>

      {confirm && (
        <ConfirmationDialog
          title={confirm.type === "approve" ? "Approve this recommendation?" : "Reject this recommendation?"}
          confirmLabel={confirm.type === "approve" ? "Approve" : "Reject"}
          confirmTone={confirm.type === "approve" ? "primary" : "danger"}
          busy={busy}
          confirmDisabled={confirm.type === "reject" && reason.trim().length < 3}
          onConfirm={() => void decide()}
          onCancel={() => {
            if (!busy) setConfirm(null);
          }}
        >
          <p className="font-medium text-ink">
            {confirm.record.recommendation.title ?? "Recommendation"} ·{" "}
            {confirm.record.recommendation.resource ?? confirm.record.recommendation.resource_id}
          </p>
          {confirm.type === "approve" ? (
            <p className="mt-2">
              Approval records your authorization. It does not change anything in Databricks — the recommendation moves
              to Execution as Ready to Execute.
            </p>
          ) : (
            <label className="mt-3 block">
              <span className="text-xs font-medium text-ink">Reason (required, at least 3 characters)</span>
              <textarea
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                rows={3}
                className="input-field mt-1"
                aria-label="Rejection reason"
              />
            </label>
          )}
        </ConfirmationDialog>
      )}
    </Layout>
  );
}

function ApprovalCard({
  record,
  disabled,
  onApprove,
  onReject,
  onOpenExecution,
}: {
  record: ApprovalRecord;
  disabled: boolean;
  onApprove: () => void;
  onReject: () => void;
  onOpenExecution: () => void;
}) {
  const rec = record.recommendation;
  const status = (record.status ?? "").toUpperCase();
  const lifecycle = lifecycleOf(rec.approval_status ?? status, rec.execution_status);
  const proposed = parseMaybeJson(rec.proposed_state) as { direction?: unknown; reason?: unknown } | null;

  return (
    <li className="card overflow-hidden" data-testid="approval-card">
      <header className="flex flex-wrap items-start justify-between gap-3 px-5 py-4">
        <div className="min-w-0">
          <p className="text-base font-semibold text-ink">{rec.title ?? rec.finding_type ?? "Recommendation"}</p>
          <p className="mt-1 text-xs text-ink-muted">
            <span className="font-medium text-ink">{rec.resource ?? "Cluster"}</span>
            {rec.resource_id && rec.resource_id !== rec.resource && <span className="ml-1.5 font-mono">{rec.resource_id}</span>}
            {rec.rule_id && <span className="ml-1.5 font-mono text-ink-faint">{rec.rule_id}</span>}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          {rec.severity && <StatusPill tone={severityTone(rec.severity)}>{titleCase(rec.severity)} severity</StatusPill>}
          {status === "PENDING" && <StatusPill tone="warning">Pending Approval</StatusPill>}
          {status === "APPROVED" && <StatusPill tone="success">Approved</StatusPill>}
          {status === "REJECTED" && <StatusPill tone="danger">Rejected</StatusPill>}
          {lifecycle === "READY_TO_EXECUTE" && <StatusPill tone="brand">Ready to Execute</StatusPill>}
        </div>
      </header>

      <div className="grid gap-5 border-t border-panel-border px-5 py-4 lg:grid-cols-2">
        <div className="flex flex-col gap-3 text-xs">
          {rec.summary && <p className="text-sm text-ink">{rec.summary}</p>}
          <div>
            <p className="text-ink-faint">Recommended action</p>
            <p className="mt-0.5 font-medium text-ink">
              {proposed && typeof proposed === "object" && proposed.direction ? String(proposed.direction) : "Not available"}
            </p>
            {proposed && typeof proposed === "object" && proposed.reason ? (
              <p className="mt-0.5 text-ink-muted">{String(proposed.reason)}</p>
            ) : null}
          </div>
          <dl className="grid grid-cols-2 gap-3">
            <div>
              <dt className="text-ink-faint">Requested</dt>
              <dd className="mt-0.5 text-ink">{formatDateTime(record.created_at)}</dd>
            </div>
            <div>
              <dt className="text-ink-faint">{status === "PENDING" ? "Decision" : status === "APPROVED" ? "Approved by" : "Rejected by"}</dt>
              <dd className="mt-0.5 text-ink">{status === "PENDING" ? "Awaiting review" : record.decided_by ?? "Not available"}</dd>
            </div>
            {status !== "PENDING" && (
              <div>
                <dt className="text-ink-faint">{status === "APPROVED" ? "Approved at" : "Rejected at"}</dt>
                <dd className="mt-0.5 text-ink">{formatDateTime(record.decided_at)}</dd>
              </div>
            )}
          </dl>
        </div>
        <div>
          <p className="text-xs font-medium text-ink-faint">Evidence</p>
          <div className="mt-2 rounded-sm border border-panel-border bg-canvas-raised p-3">
            <KeyValueGrid data={parseMaybeJson(rec.evidence)} empty="No evidence fields were recorded." />
          </div>
        </div>
      </div>

      <footer className="flex flex-wrap items-center justify-between gap-3 border-t border-panel-border bg-canvas-raised px-5 py-3">
        <p className="flex items-center gap-1.5 text-xs text-ink-muted">
          <ShieldCheck size={14} aria-hidden="true" />
          {status === "PENDING"
            ? "Approving records authorization only. Nothing is executed."
            : status === "APPROVED"
              ? "Authorized. Continue in Execution."
              : "Rejected recommendations can be sent for approval again from Recommendations."}
        </p>
        <div className="flex gap-2">
          {status === "PENDING" && (
            <>
              <button type="button" className="btn-secondary" onClick={onReject} disabled={disabled}>
                <X size={15} aria-hidden="true" /> Reject
              </button>
              <button type="button" className="btn-primary" onClick={onApprove} disabled={disabled}>
                <Check size={15} aria-hidden="true" /> Approve
              </button>
            </>
          )}
          {status === "APPROVED" && (
            <button type="button" className="btn-secondary" onClick={onOpenExecution}>
              Open Execution <ArrowRight size={15} aria-hidden="true" />
            </button>
          )}
        </div>
      </footer>
    </li>
  );
}
