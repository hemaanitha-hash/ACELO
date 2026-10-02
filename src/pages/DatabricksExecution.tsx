import React, { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { CheckCircle2, Info, Play } from "lucide-react";
import Layout from "../components/Layout";
import PageHeader from "../components/PageHeader";
import Button from "../components/Button";
import {
  EmptyState,
  ErrorState,
  LifecyclePill,
  LoadingState,
  MetricCard,
  StatusPill,
  WorkflowIndicator,
  formatDateTime,
  severityTone,
  titleCase,
} from "../components/ui";
import {
  RequestError,
  latestApprovalByRecommendation,
  lifecycleOf,
  listApprovalRecords,
  parseMaybeJson,
  type ApprovalRecord,
  type Lifecycle,
} from "../services/stage1Api";

/**
 * Execution — the lifecycle stage after approval, for the Databricks App.
 *
 *   Approved -> Ready to Execute -> Executing -> Completed / Failed
 *
 * Lists every approved compute recommendation from the persisted approval
 * records. Each shows its real execution state from the recommendation's own
 * execution_status. The Execute action is shown but inactive: no execution
 * endpoint is called, nothing in Databricks is changed, and no state is
 * advanced by the UI.
 */

export const EXECUTE_INACTIVE_MESSAGE =
  "Execution action will be enabled when the execution backend is activated.";

interface Row {
  record: ApprovalRecord;
  lifecycle: Lifecycle;
}

export default function DatabricksExecution() {
  const navigate = useNavigate();
  const [rows, setRows] = useState<Row[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<{ text: string; technical: string | null } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const records = await listApprovalRecords(["approved"]);
      const latest = [...latestApprovalByRecommendation(records).values()];
      setRows(
        latest
          .map((record) => ({
            record,
            lifecycle: lifecycleOf(record.recommendation.approval_status ?? "APPROVED", record.recommendation.execution_status),
          }))
          .sort((a, b) => ((b.record.decided_at ?? "") > (a.record.decided_at ?? "") ? 1 : -1)),
      );
      setError(null);
    } catch (e: unknown) {
      setError({
        text: e instanceof RequestError ? e.message : "Could not load approved recommendations.",
        technical: e instanceof RequestError ? e.technical : null,
      });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const ready = rows.filter((r) => r.lifecycle === "READY_TO_EXECUTE").length;
  const executing = rows.filter((r) => r.lifecycle === "EXECUTING").length;
  const done = rows.filter((r) => r.lifecycle === "EXECUTED" || r.lifecycle === "EXECUTION_FAILED").length;

  return (
    <Layout pageName="Execution" onRefresh={() => void load()}>
      <div className="flex flex-col gap-6">
        <PageHeader
          eyebrow="Lifecycle"
          title="Execution"
          description="Approved recommendations and their execution state. A recommendation is executed only when someone explicitly chooses Execute."
        />
        <WorkflowIndicator current="execution" />

        <div className="flex items-start gap-3 rounded-sm border border-brand-100 bg-brand-50 px-4 py-3 text-sm text-brand-700">
          <Info size={16} className="mt-0.5 shrink-0" aria-hidden="true" />
          <p>
            Approved recommendations wait here as <strong>Ready to Execute</strong>. {EXECUTE_INACTIVE_MESSAGE} No
            Databricks resource is changed from this page until then.
          </p>
        </div>

        <div className="grid gap-4 sm:grid-cols-3">
          <MetricCard label="Ready to Execute" value={loading && !rows.length ? "—" : ready} icon={Play} emphasis />
          <MetricCard label="Executing" value={loading && !rows.length ? "—" : executing} />
          <MetricCard label="Execution finished" value={loading && !rows.length ? "—" : done} icon={CheckCircle2} />
        </div>

        {loading && rows.length === 0 && <LoadingState title="Loading approved recommendations…" />}

        {error && (
          <ErrorState
            title="Could not load approved recommendations"
            detail={error.text}
            technical={error.technical}
            action={
              <Button variant="secondary" onClick={() => void load()}>
                Try again
              </Button>
            }
          />
        )}

        {!loading && !error && rows.length === 0 && (
          <EmptyState
            title="No approved recommendations yet"
            detail="When a recommendation is approved it appears here as Ready to Execute."
            action={
              <Button variant="secondary" onClick={() => navigate("/approvals?status=PENDING")}>
                Open Approvals
              </Button>
            }
          />
        )}

        {!error && rows.length > 0 && (
          <ol className="flex flex-col gap-4">
            {rows.map(({ record, lifecycle }) => (
              <ExecutionCard key={record.recommendation_id} record={record} lifecycle={lifecycle} />
            ))}
          </ol>
        )}
      </div>
    </Layout>
  );
}

function ExecutionCard({ record, lifecycle }: { record: ApprovalRecord; lifecycle: Lifecycle }) {
  const navigate = useNavigate();
  const rec = record.recommendation;
  const proposed = parseMaybeJson(rec.proposed_state) as { direction?: unknown } | null;

  return (
    <li className="card overflow-hidden" data-testid="execution-card">
      <div className="grid gap-5 px-5 py-4 md:grid-cols-[1fr_auto]">
        <div className="min-w-0">
          <p className="text-base font-semibold text-ink">{rec.title ?? rec.finding_type ?? "Recommendation"}</p>
          <p className="mt-1 text-xs text-ink-muted">
            <span className="font-medium text-ink">{rec.resource ?? "Cluster"}</span>
            {rec.resource_id && rec.resource_id !== rec.resource && <span className="ml-1.5 font-mono">{rec.resource_id}</span>}
          </p>
          {proposed && typeof proposed === "object" && proposed.direction ? (
            <p className="mt-2 text-xs text-ink-muted">
              Recommended action: <span className="font-medium text-ink">{String(proposed.direction)}</span>
            </p>
          ) : null}
          <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-3 text-xs sm:grid-cols-4">
            <div>
              <dt className="text-ink-faint">Approval</dt>
              <dd className="mt-1 flex items-center gap-1 font-medium text-signal-low">
                <CheckCircle2 size={13} aria-hidden="true" /> Approved
              </dd>
            </div>
            <div>
              <dt className="text-ink-faint">Execution</dt>
              <dd className="mt-1">
                <LifecyclePill lifecycle={lifecycle} />
              </dd>
            </div>
            <div>
              <dt className="text-ink-faint">Approved by</dt>
              <dd className="mt-1 text-ink">{record.decided_by ?? "Not available"}</dd>
            </div>
            <div>
              <dt className="text-ink-faint">Approved at</dt>
              <dd className="mt-1 text-ink">{formatDateTime(record.decided_at)}</dd>
            </div>
          </dl>
          {rec.severity && (
            <div className="mt-3">
              <StatusPill tone={severityTone(rec.severity)}>{titleCase(rec.severity)} severity</StatusPill>
            </div>
          )}
        </div>
        <div className="flex flex-row items-start gap-2 md:flex-col md:items-stretch">
          <button
            type="button"
            className="btn-secondary"
            onClick={() => navigate(`/recommendations?highlight=${encodeURIComponent(record.recommendation_id)}`)}
          >
            View Details
          </button>
          {lifecycle === "READY_TO_EXECUTE" && (
            <span title={EXECUTE_INACTIVE_MESSAGE} className="inline-flex">
              <button
                type="button"
                className="btn-primary w-full"
                disabled
                aria-disabled="true"
                aria-describedby={`exec-note-${record.recommendation_id}`}
              >
                <Play size={15} aria-hidden="true" /> Execute
              </button>
            </span>
          )}
        </div>
      </div>
      {lifecycle === "READY_TO_EXECUTE" && (
        <p
          id={`exec-note-${record.recommendation_id}`}
          className="border-t border-panel-border bg-canvas-raised px-5 py-2.5 text-xs text-ink-muted"
        >
          This recommendation has been approved and is ready for execution. {EXECUTE_INACTIVE_MESSAGE}
        </p>
      )}
    </li>
  );
}
