import React, { useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { AlertCircle, ArrowLeft, CheckCircle2 } from "lucide-react";
import { useMsal } from "@azure/msal-react";
import Layout from "../components/Layout";
import Modal from "../components/Modal";
import Button from "../components/Button";
import { ApiError } from "../services/environmentApi";
import { FabricAuthError, getFabricToken } from "../services/fabricAuth";
import {
  approve,
  display,
  execute,
  getApproval,
  reject,
  reviewerFrom,
  usd,
  type Approval,
} from "../services/approvalsApi";

/**
 * Review of ONE real recommendation. Every value shown comes from the approval
 * record, which was copied from the optimizer's result row. Approve needs a
 * confirmation, Reject needs a reason, and execution is a separate, explicit
 * step that only exists once approved.
 */

type Dialog = "approve" | "reject" | "execute" | null;

const SOURCE_LABELS: Record<string, string> = {
  fabric: "Microsoft Fabric",
  databricks: "Databricks",
  file: "File upload",
};

export default function ApprovalDetail() {
  const { id = "" } = useParams();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const { instance } = useMsal();
  const [approval, setApproval] = useState<Approval | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [dialog, setDialog] = useState<Dialog>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const reviewer = reviewerFrom(instance.getActiveAccount() ?? instance.getAllAccounts()[0]);

  async function fabricToken(): Promise<string | null> {
    if (!(instance.getActiveAccount() ?? instance.getAllAccounts()[0])) return null;
    try {
      return await getFabricToken(instance);
    } catch (e: unknown) {
      if (e instanceof FabricAuthError) return null;
      throw e;
    }
  }

  async function load() {
    try {
      setApproval(await getApproval(id, await fabricToken()));
      setLoadError(null);
    } catch (e: unknown) {
      setLoadError(
        e instanceof ApiError && e.status === 404
          ? "This approval could not be found."
          : "Unable to load optimization recommendations."
      );
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  // AI Agent deep link ("Approve cluster X"): open the confirmation, never act.
  useEffect(() => {
    const requested = params.get("action");
    if (approval?.status === "PENDING" && (requested === "approve" || requested === "reject")) {
      setDialog(requested);
    }
  }, [approval, params]);

  function close() {
    setDialog(null);
    setReason("");
    setActionError(null);
  }

  async function run(kind: Exclude<Dialog, null>) {
    if (!approval) return;
    setBusy(true);
    setActionError(null);
    try {
      const updated =
        kind === "approve"
          ? await approve(approval.approval_id, reviewer)
          : kind === "reject"
            ? await reject(approval.approval_id, reviewer, reason.trim())
            : await execute(approval.approval_id, reviewer, await fabricToken());
      setApproval(updated);
      close();
    } catch (e: unknown) {
      const detail = e instanceof ApiError ? e.message : "";
      setActionError(
        kind === "execute"
          ? `Optimization execution failed. ${detail}`.trim()
          : kind === "approve"
            ? `Approval could not be saved. Please try again. ${detail}`.trim()
            : `Rejection could not be saved. Please try again. ${detail}`.trim()
      );
    } finally {
      setBusy(false);
    }
  }

  const a = approval;
  const reasonValid = reason.trim().length >= 3;

  return (
    <Layout pageName="Approval Review">
      <div className="flex flex-col gap-6">
        <button
          type="button"
          onClick={() => navigate("/approvals")}
          className="flex items-center gap-1 self-start text-sm text-ink-muted hover:text-ink"
        >
          <ArrowLeft size={14} /> Back to Approvals
        </button>

        {loadError && (
          <div className="surface border-l-4 border-l-signal-high p-5">
            <div className="flex items-start gap-3">
              <AlertCircle size={18} className="mt-0.5 shrink-0 text-signal-high" />
              <p className="text-sm text-ink">{loadError}</p>
            </div>
          </div>
        )}

        {a && (
          <>
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div>
                <p className="label-eyebrow">Cluster optimization</p>
                <h1 className="mt-1 text-display font-semibold text-ink">{a.resource_name}</h1>
              </div>
              <span data-testid="approval-status" className="rounded-sm border border-panel-border px-3 py-1 text-xs font-semibold text-ink">
                {a.status}
              </span>
            </div>

            <section className="surface p-5">
              <dl className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                <Field label="Cluster" value={a.resource_name} />
                <Field label="Optimization Label" value={a.optimization_label ?? "Not available"} />
                <Field label="Current Workers" value={display(a.current_workers)} />
                <Field label="Recommended Max Workers" value={display(a.recommended_max_workers)} />
                <Field label="Current Compute Cost" value={display(a.total_dbus_cost_usd, usd)} />
                <Field label="Potential Monthly Savings" value={display(a.potential_monthly_savings, usd)} />
              </dl>
            </section>

            <section className="surface p-5">
              <p className="label-eyebrow mb-2">AI / LLM Recommendation</p>
              <p data-testid="llm-recommendation" className="whitespace-pre-line text-sm leading-relaxed text-ink">
                {a.llm_optimization ?? "Not available"}
              </p>
            </section>

            <section className="surface p-5">
              <p className="label-eyebrow mb-2">Evidence</p>
              {Object.keys(a.evidence).length === 0 ? (
                <p className="text-sm text-ink-muted">Not available</p>
              ) : (
                <dl className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                  {Object.entries(a.evidence).map(([key, value]) => (
                    <Field key={key} label={key.replace(/_/g, " ")} value={String(value)} mono />
                  ))}
                </dl>
              )}
            </section>

            <section className="surface p-5">
              <dl className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
                <Field
                  label="Source"
                  value={`${SOURCE_LABELS[a.platform] ?? a.platform}${a.source === "tracking_table" ? " · approval tracking table" : ""}`}
                />
                <Field label="ACELO Run ID" value={a.acelo_run_id ?? String(a.evidence.source_acelo_run_id ?? "Not available")} mono />
                <Field label="Environment" value={a.environment_id ?? "Not available"} mono />
                <Field label="Created" value={a.created_at ? new Date(a.created_at).toLocaleString() : "Not available"} />
              </dl>
            </section>

            {/* Actions for the current state only. */}
            <section className="surface p-5">
              {a.status === "PENDING" && (
                <div className="flex flex-wrap gap-3">
                  <Button onClick={() => setDialog("approve")}>Approve</Button>
                  <Button variant="secondary" onClick={() => setDialog("reject")}>Reject</Button>
                </div>
              )}
              {a.status === "APPROVED" && (
                <div>
                  <p className="flex items-center gap-2 text-sm font-medium text-signal-low">
                    <CheckCircle2 size={16} /> Approved. Ready to execute.
                  </p>
                  <Button className="mt-3" onClick={() => setDialog("execute")}>Execute Optimization</Button>
                </div>
              )}
              {a.status === "EXECUTING" && (
                <p className="text-sm text-ink">
                  Executing on the platform — execution ID{" "}
                  <span className="font-mono text-xs">{a.execution_id}</span>. Refresh to check validation.
                </p>
              )}
              {a.status === "COMPLETED" && (
                <p className="text-sm text-signal-low">
                  Completed and validated. Execution ID <span className="font-mono text-xs">{a.execution_id}</span>.
                </p>
              )}
              {a.status === "FAILED" && (
                <div data-testid="execution-failed">
                  <p className="text-sm font-medium text-signal-high">Optimization execution failed.</p>
                  <p className="mt-1 text-sm text-ink-muted">{a.execution_error}</p>
                  {a.execution_id && (
                    <p className="mt-1 text-xs text-ink-muted">
                      Execution ID <span className="font-mono">{a.execution_id}</span>
                    </p>
                  )}
                </div>
              )}
              {(a.status === "REJECTED" || a.status === "CANCELLED") && (
                <p className="text-sm text-ink-muted">No changes will be applied for this recommendation.</p>
              )}
              {actionError && !dialog && (
                <p data-testid="action-error" className="mt-3 text-sm text-signal-high">{actionError}</p>
              )}
            </section>

            <section className="surface p-5">
              <p className="label-eyebrow mb-3">Approval History</p>
              {a.approved_by && (
                <p className="text-sm text-ink">
                  Approved by <span className="font-medium">{a.approved_by}</span>
                  {a.approved_at ? ` at ${new Date(a.approved_at).toLocaleString()}` : ""}
                </p>
              )}
              {a.rejected_by && (
                <p className="text-sm text-ink">
                  Rejected by <span className="font-medium">{a.rejected_by}</span>
                  {a.rejected_at ? ` at ${new Date(a.rejected_at).toLocaleString()}` : ""}
                  <span className="block text-ink-muted">Rejection reason: {a.rejection_reason}</span>
                </p>
              )}
              <ul className="mt-3 space-y-1 text-xs text-ink-muted">
                {(a.history ?? []).map((h, i) => (
                  <li key={i}>
                    {h.timestamp ? new Date(h.timestamp).toLocaleString() : ""} · {h.action}
                    {h.previous_status ? ` (${h.previous_status} → ${h.new_status})` : ` (${h.new_status})`}
                    {h.user_name ? ` · ${h.user_name}` : " · ACELO"}
                    {h.reason ? ` · "${h.reason}"` : ""}
                  </li>
                ))}
              </ul>
            </section>
          </>
        )}
      </div>

      {a && (
        <>
          <Modal open={dialog === "approve"} onClose={close} title="Approve this optimization?">
            <Summary approval={a} />
            <p className="mt-3 text-xs text-ink-muted">
              Approving records your decision. Nothing is changed until you choose Execute Optimization.
            </p>
            <DialogError message={actionError} />
            <div className="mt-5 flex justify-end gap-3">
              <Button variant="ghost" onClick={close}>Cancel</Button>
              <Button onClick={() => void run("approve")} disabled={busy}>Confirm Approval</Button>
            </div>
          </Modal>

          <Modal open={dialog === "reject"} onClose={close} title="Why are you rejecting this recommendation?">
            <label className="block">
              <span className="sr-only">Rejection reason</span>
              <textarea
                aria-label="Rejection reason"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                rows={4}
                className="w-full rounded-md border border-panel-border bg-white px-3 py-2 text-sm text-ink outline-none focus:border-brand-500"
              />
            </label>
            {!reasonValid && <p className="mt-1 text-xs text-ink-muted">A reason is required.</p>}
            <DialogError message={actionError} />
            <div className="mt-5 flex justify-end gap-3">
              <Button variant="ghost" onClick={close}>Cancel</Button>
              <Button onClick={() => void run("reject")} disabled={busy || !reasonValid}>Confirm Rejection</Button>
            </div>
          </Modal>

          <Modal open={dialog === "execute"} onClose={close} title="Execute this optimization?">
            <Summary approval={a} />
            <p className="mt-3 text-xs text-ink-muted">
              ACELO will ask {SOURCE_LABELS[a.platform] ?? a.platform} to apply this change and then validate it.
            </p>
            <DialogError message={actionError} />
            <div className="mt-5 flex justify-end gap-3">
              <Button variant="ghost" onClick={close}>Cancel</Button>
              <Button onClick={() => void run("execute")} disabled={busy}>Confirm Execution</Button>
            </div>
          </Modal>
        </>
      )}
    </Layout>
  );
}

function Summary({ approval: a }: { approval: Approval }) {
  return (
    <dl className="grid gap-2 text-sm">
      <Row label="Cluster" value={a.resource_name} />
      <Row label="Current configuration" value={`${display(a.current_workers)} workers`} />
      <Row label="Proposed configuration" value={`max ${display(a.recommended_max_workers)} workers`} />
      <Row label="Estimated savings" value={display(a.potential_monthly_savings, (v) => `${usd(v)}/mo`)} />
    </dl>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-4">
      <dt className="text-ink-muted">{label}</dt>
      <dd className="font-medium text-ink">{value}</dd>
    </div>
  );
}

function DialogError({ message }: { message: string | null }) {
  return message ? <p data-testid="dialog-error" className="mt-3 text-sm text-signal-high">{message}</p> : null;
}

function Field({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs uppercase tracking-wide text-ink-muted">{label}</dt>
      <dd className={`mt-1 break-all text-sm text-ink ${mono ? "font-mono text-xs" : ""}`}>{value}</dd>
    </div>
  );
}
