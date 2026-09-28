import React, { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { CheckCircle2, Clock3, Send, Sparkles } from "lucide-react";
import Layout from "../components/Layout";
import PageHeader from "../components/PageHeader";
import Button from "../components/Button";
import StateBlock, { StatePanel } from "../components/StateBlock";
import OpportunityTable from "../components/OpportunityTable";
import StatusBadge from "../components/StatusBadge";
import { getOptimizations } from "../services/api";
import type { Opportunity } from "../types";
import { isDatabricksOnly } from "../services/experience";
import {
  AgentApiError,
  type AgentOpportunity,
  type Stage1Recommendation,
} from "../services/databricksAgentApi";
import {
  ensureComputeAnalysis,
  getComputeAnalysis,
  subscribeComputeAnalysis,
  type ComputeAnalysisState,
} from "../services/computeAnalysis";

/**
 * Recommendations — the end of the MVP journey.
 *
 *   AI Agent / Compute Optimization
 *     -> analyzeDatabricksCompute()   (the ONE analysis engine)
 *       -> DatabricksAgentResult
 *         -> computeAnalysis store
 *           -> this page
 *
 * Stage 1 approval flow ends at:
 *
 *   Recommendation
 *     -> Send to Approval
 *     -> Pending Approval
 *
 * Approval does NOT execute or mutate Databricks.
 *
 * It previously rendered legacy `getOptimizations()` data, which had nothing to
 * do with the Databricks analysis the user had just run. It now reads the SAME
 * result, so a finding shown on Compute Optimization is the finding shown here.
 *
 * Every field comes from the backend. There is no cost, saving, utilization,
 * confidence or score anywhere, because the backend measures none of those —
 * discovery reads configuration, not consumption. Each item is therefore a
 * POTENTIAL opportunity, and states what must be observed before anyone acts.
 */

const TYPE_LABELS: Record<string, string> = {
  CLASSIC_CLUSTER: "Classic cluster",
  SERVERLESS_COMPUTE: "Serverless compute",
  SQL_WAREHOUSE: "SQL warehouse",
};

type ApprovalUiState = "OPEN" | "PENDING" | "APPROVED" | "REJECTED";

async function requestStage1Approval(recommendationId: string): Promise<void> {
  const response = await fetch(
    `/api/approvals/recommendations/${encodeURIComponent(recommendationId)}/request`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
    }
  );

  if (!response.ok) {
    let detail = `Request failed (HTTP ${response.status}).`;

    try {
      const body = (await response.json()) as {
        detail?: string;
        message?: string;
      };

      detail = body.detail ?? body.message ?? detail;
    } catch {
      // Keep the HTTP error when the backend does not return JSON.
    }

    throw new Error(detail);
  }
}

export default function Recommendations() {
  if (!isDatabricksOnly()) return <LegacyRecommendations />;
  return <DatabricksRecommendations />;
}

// --- Databricks MVP ---------------------------------------------------------

function DatabricksRecommendations() {
  const navigate = useNavigate();
  const [state, setState] = useState<ComputeAnalysisState>(getComputeAnalysis());
  const [loading, setLoading] = useState(!getComputeAnalysis().result);
  const [error, setError] = useState<string | null>(null);

  // UI-only approval state for this page.
  const [approvalStates, setApprovalStates] = useState<Record<string, ApprovalUiState>>({});
  const [requestingApproval, setRequestingApproval] = useState<Record<string, boolean>>({});
  const [approvalErrors, setApprovalErrors] = useState<Record<string, string | null>>({});

  // Follow the store, so arriving here after a fresh analysis shows it.
  useEffect(() => subscribeComputeAnalysis(setState), []);

  async function load() {
    setLoading(true);
    setError(null);

    try {
      // Uses the stored result when the journey already produced one, and
      // otherwise runs the same analysis — never a second engine.
      await ensureComputeAnalysis();
      setState(getComputeAnalysis());
    } catch (e: unknown) {
      setError(
        e instanceof AgentApiError
          ? e.message
          : "The analysis could not be completed."
      );
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (!getComputeAnalysis().result) void load();
    else setLoading(false);

    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const result = state.result;
  const analysis = result?.analysis ?? null;
  const opportunities = analysis?.opportunities ?? [];
  const stage1Recommendations =
    analysis?.compute_optimization?.recommendations ?? [];

  async function handleSendToApproval(recommendationId: string) {
    setRequestingApproval((current) => ({
      ...current,
      [recommendationId]: true,
    }));

    setApprovalErrors((current) => ({
      ...current,
      [recommendationId]: null,
    }));

    try {
      await requestStage1Approval(recommendationId);

      setApprovalStates((current) => ({
        ...current,
        [recommendationId]: "PENDING",
      }));
    } catch (e: unknown) {
      setApprovalErrors((current) => ({
        ...current,
        [recommendationId]:
          e instanceof Error
            ? e.message
            : "The approval request could not be created.",
      }));
    } finally {
      setRequestingApproval((current) => ({
        ...current,
        [recommendationId]: false,
      }));
    }
  }

  return (
    <Layout pageName="Recommendations" onRefresh={() => void load()}>
      <div className="flex flex-col gap-6">
        <PageHeader
          eyebrow="Databricks"
          title="Recommendations"
          description="What the discovered configuration suggests reviewing, and the evidence each one still needs. Read-only — nothing here changes a Databricks resource."
          action={
            <Button
              icon={<Sparkles size={16} />}
              variant="secondary"
              onClick={() => navigate("/compute")}
            >
              View analysis
            </Button>
          }
        />

        {error && (
          <StatePanel
            kind="error"
            title="Analysis failed"
            detail={error}
          />
        )}

        {loading && !analysis && (
          <StatePanel
            kind="loading"
            title="Analyzing your Databricks compute…"
          />
        )}

        {result && !result.ok && (
          <StatePanel
            kind="error"
            title={result.status}
            detail={
              result.message ??
              "The Databricks analysis could not be completed."
            }
          />
        )}

        {analysis && (
          <>
            {state.analyzedAt && (
              <p className="text-xs text-ink-faint">
                Based on the compute analysis from{" "}
                {state.analyzedAt.toLocaleTimeString()}
                {analysis.workspace_name
                  ? ` · ${analysis.workspace_name}`
                  : ""}
              </p>
            )}

            {stage1Recommendations.length > 0 && (
              <section
                className="flex flex-col gap-3"
                aria-label="Stage 1 recommendations"
              >
                <h2 className="text-sm font-semibold text-ink">
                  Stage 1 Recommendations
                </h2>

                <ol className="flex flex-col gap-4">
                  {stage1Recommendations.map((recommendation) => (
                    <Stage1RecommendationItem
                      key={recommendation.recommendation_id}
                      recommendation={recommendation}
                      approvalState={
                        approvalStates[recommendation.recommendation_id] ??
                        getInitialApprovalState(recommendation.status)
                      }
                      requesting={
                        requestingApproval[recommendation.recommendation_id] ??
                        false
                      }
                      error={
                        approvalErrors[recommendation.recommendation_id] ??
                        null
                      }
                      onSendToApproval={() =>
                        handleSendToApproval(
                          recommendation.recommendation_id
                        )
                      }
                    />
                  ))}
                </ol>
              </section>
            )}

            {opportunities.length === 0 &&
              stage1Recommendations.length === 0 && (
                <StateBlock
                  kind="empty"
                  title="No recommendations"
                  detail="Nothing in the visible configuration raised a question. Utilization, idle time and cost were not measured, so this is not a finding that the workspace is optimally configured."
                />
              )}

            {opportunities.length > 0 && (
              <section
                className="flex flex-col gap-3"
                aria-label="Configuration observations"
              >
                <h2 className="text-sm font-semibold text-ink">
                  Configuration Observations
                </h2>

                <ol className="flex flex-col gap-4">
                  {opportunities.map((item, index) => (
                    <RecommendationItem
                      key={`${item.resource_id}:${index}`}
                      item={item}
                      index={index + 1}
                    />
                  ))}
                </ol>
              </section>
            )}

            {analysis.missing_evidence.length > 0 && (
              <section className="surface overflow-hidden">
                <header className="px-5 py-4">
                  <h2 className="text-sm font-semibold text-ink">
                    Limitations
                  </h2>
                  <p className="mt-1 text-xs text-ink-faint">
                    Discovery reads configuration, not behaviour. These were
                    not observed.
                  </p>
                </header>

                <ul className="border-t border-panel-border px-5 py-3">
                  {analysis.missing_evidence.map((line) => (
                    <li
                      key={line}
                      className="text-xs text-ink-muted"
                    >
                      • {line}
                    </li>
                  ))}
                </ul>
              </section>
            )}
          </>
        )}
      </div>
    </Layout>
  );
}

function getInitialApprovalState(status: string): ApprovalUiState {
  const normalized = status.trim().toUpperCase();

  if (normalized === "APPROVED") return "APPROVED";
  if (normalized === "REJECTED") return "REJECTED";
  if (normalized === "PENDING") return "PENDING";

  return "OPEN";
}

function RecommendationItem({
  item,
  index,
}: {
  item: AgentOpportunity;
  index: number;
}) {
  const rows: { label: string; value: string; mono?: boolean }[] = [
    { label: "Finding", value: item.potential_issue },
    {
      label: "Observed evidence",
      value: item.observed_evidence,
      mono: true,
    },
    { label: "Recommendation", value: item.recommendation },
    {
      label: "Rationale",
      value: `Raised because ${item.observed_evidence} was observed on this resource, which indicates: ${item.potential_issue}`,
    },
    {
      label: "Evidence required before action",
      value: item.evidence_required,
    },
    { label: "Expected impact", value: item.expected_impact },
  ];

  return (
    <li className="surface overflow-hidden">
      <header className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2 px-5 py-4">
        <div className="min-w-0">
          <p className="text-sm font-medium text-ink">
            {index}. {item.resource}
          </p>

          <p className="mt-0.5 text-xs text-ink-faint">
            {TYPE_LABELS[item.resource_type] ?? item.resource_type}
            {item.resource_id ? ` · ${item.resource_id}` : ""}
          </p>
        </div>

        <StatusBadge
          label="Potential Optimization Opportunity"
          kind="status"
          className="shrink-0"
        />
      </header>

      <dl className="border-t border-panel-border px-5 py-3">
        {rows.map((row) => (
          <div
            key={row.label}
            className="flex flex-col gap-0.5 py-1 sm:flex-row sm:gap-3"
          >
            <dt className="shrink-0 text-xs text-ink-faint sm:w-56">
              {row.label}
            </dt>

            <dd
              className={`text-xs text-ink-muted ${
                row.mono ? "font-mono" : ""
              }`}
            >
              {row.value}
            </dd>
          </div>
        ))}
      </dl>
    </li>
  );
}

function Stage1RecommendationItem({
  recommendation,
}: {
  recommendation: Stage1Recommendation;
}) {
  const navigate = useNavigate();
  const [requesting, setRequesting] = useState(false);
  const [requested, setRequested] = useState(
    recommendation.status?.toUpperCase() === "PENDING_APPROVAL",
  );
  const [error, setError] = useState<string | null>(null);

  const rows: { label: string; value: string; mono?: boolean }[] = [
    { label: "Finding", value: recommendation.finding_type },
    { label: "Rule", value: recommendation.rule_id, mono: true },
    { label: "Summary", value: recommendation.summary },
    {
      label: "Observed evidence",
      value: JSON.stringify(recommendation.evidence),
      mono: true,
    },
    {
      label: "Evidence reference",
      value: JSON.stringify(recommendation.evidence_references),
      mono: true,
    },
    {
      label: "Current state",
      value: JSON.stringify(recommendation.current_state),
      mono: true,
    },
    {
      label: "Proposed direction",
      value: recommendation.proposed_state.direction,
    },
    {
      label: "Direction rationale",
      value: recommendation.proposed_state.reason,
    },
    {
      label: "Expected impact",
      value: recommendation.expected_impact.description,
    },
    {
      label: "Evidence quality",
      value: JSON.stringify(recommendation.evidence_quality),
      mono: true,
    },
    {
      label: "Observation window",
      value: `${recommendation.observation_window.start ?? "Not available"} to ${
        recommendation.observation_window.end ?? "Not available"
      }`,
      mono: true,
    },
    {
      label: "Confidence / severity / risk",
      value: `${recommendation.confidence} / ${recommendation.severity} / ${recommendation.risk}`,
    },
    {
      label: "Savings",
      value: "Not available. No savings estimate or measurement is provided.",
    },
  ];

  async function sendToApproval() {
    if (requesting || requested) return;

    setRequesting(true);
    setError(null);

    try {
      const response = await fetch(
        `/api/approvals/recommendations/${encodeURIComponent(
          recommendation.recommendation_id,
        )}/request`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
        },
      );

      const text = await response.text();

      if (!response.ok) {
        let message = `Unable to send recommendation to approval (HTTP ${response.status}).`;

        try {
          const body = JSON.parse(text) as { detail?: string };
          if (body.detail) message = body.detail;
        } catch {
          // Keep the HTTP fallback message when the response is not JSON.
        }

        throw new Error(message);
      }

      setRequested(true);

      // Move directly to the Approval Center so the demo follows
      // Recommendations → Approval Center.
      navigate("/approvals");
    } catch (e: unknown) {
      setError(
        e instanceof Error
          ? e.message
          : "Unable to send this recommendation for approval.",
      );
    } finally {
      setRequesting(false);
    }
  }

  return (
    <li className="overflow-hidden rounded-2xl border border-red-100 bg-white shadow-sm">
      <header className="flex flex-wrap items-start justify-between gap-4 border-b border-red-50 px-6 py-5">
        <div className="min-w-0">
          <p className="text-base font-semibold text-ink">
            {recommendation.title}
          </p>
          <p className="mt-1 text-xs text-ink-faint">
            {recommendation.resource_name} · {recommendation.resource_id} ·{" "}
            {recommendation.domain.replace(/_/g, " ")}
          </p>
        </div>

        <StatusBadge
          label={requested ? "PENDING APPROVAL" : "OPEN · REPORTING ONLY"}
          kind="status"
          className="shrink-0"
        />
      </header>

      <p className="border-b border-panel-border px-6 py-4 text-sm leading-6 text-ink-muted">
        {recommendation.description}
      </p>

      <dl className="px-6 py-4">
        {rows.map((row) => (
          <div
            key={row.label}
            className="flex flex-col gap-1 py-2 sm:flex-row sm:gap-4"
          >
            <dt className="shrink-0 text-xs font-medium text-ink-faint sm:w-56">
              {row.label}
            </dt>
            <dd
              className={`break-words text-xs leading-5 text-ink-muted ${
                row.mono ? "font-mono" : ""
              }`}
            >
              {row.value}
            </dd>
          </div>
        ))}
      </dl>

      {error && (
        <div className="mx-6 mb-4 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {error}
        </div>
      )}

      <footer className="flex flex-wrap items-center justify-between gap-3 border-t border-red-50 bg-red-50/30 px-6 py-4">
        <div className="flex items-center gap-2 text-xs text-ink-faint">
          {requested ? (
            <>
              <Clock3 size={15} />
              Waiting for human approval
            </>
          ) : (
            <>
              <CheckCircle2 size={15} />
              No Databricks resource has been changed
            </>
          )}
        </div>

        {requested ? (
          <button
            type="button"
            onClick={() => navigate("/approvals")}
            className="inline-flex items-center gap-2 rounded-xl border border-red-200 bg-white px-4 py-2.5 text-sm font-semibold text-red-700 transition hover:border-red-300 hover:bg-red-50"
          >
            <Clock3 size={16} />
            Open Approval Center
          </button>
        ) : (
          <button
            type="button"
            onClick={() => void sendToApproval()}
            disabled={requesting}
            className="inline-flex items-center gap-2 rounded-xl bg-red-600 px-4 py-2.5 text-sm font-semibold text-white shadow-sm transition hover:bg-red-700 disabled:cursor-not-allowed disabled:opacity-60"
          >
            <Send size={16} />
            {requesting ? "Sending…" : "Send to Approval"}
          </button>
        )}
      </footer>
    </li>
  );
}

// --- legacy multi-platform experience ---------------------------------------

function LegacyRecommendations() {
  const [opportunities, setOpportunities] = useState<Opportunity[]>([]);
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    const result = await getOptimizations();
    setOpportunities(result);
    setLoading(false);
  }

  useEffect(() => {
    load();
  }, []);

  const pending = opportunities.filter((o) => o.status === "Review");

  return (
    <Layout pageName="Recommendations" onRefresh={load}>
      <div className="flex flex-col gap-6">
        <PageHeader
          title="Recommendations"
          description="AI-generated recommendations awaiting review before approval."
        />

        {loading ? (
          <StatePanel
            kind="loading"
            title="Loading recommendations…"
          />
        ) : (
          <OpportunityTable
            opportunities={pending}
            emptyLabel="Every recommendation has moved past review."
          />
        )}
      </div>
    </Layout>
  );
}