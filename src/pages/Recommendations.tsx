import React, { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Sparkles } from "lucide-react";
import Layout from "../components/Layout";
import PageHeader from "../components/PageHeader";
import Button from "../components/Button";
import StateBlock, { StatePanel } from "../components/StateBlock";
import OpportunityTable from "../components/OpportunityTable";
import StatusBadge from "../components/StatusBadge";
import { getOptimizations } from "../services/api";
import type { Opportunity } from "../types";
import { isDatabricksOnly } from "../services/experience";
import { AgentApiError, type AgentOpportunity } from "../services/databricksAgentApi";
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
      setError(e instanceof AgentApiError ? e.message : "The analysis could not be completed.");
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

        {error && <StatePanel kind="error" title="Analysis failed" detail={error} />}

        {loading && !analysis && (
          <StatePanel kind="loading" title="Analyzing your Databricks compute…" />
        )}

        {result && !result.ok && (
          <StatePanel
            kind="error"
            title={result.status}
            detail={result.message ?? "The Databricks analysis could not be completed."}
          />
        )}

        {analysis && (
          <>
            {state.analyzedAt && (
              <p className="text-xs text-ink-faint">
                Based on the compute analysis from {state.analyzedAt.toLocaleTimeString()}
                {analysis.workspace_name ? ` · ${analysis.workspace_name}` : ""}
              </p>
            )}

            {opportunities.length === 0 ? (
              <StateBlock
                kind="empty"
                title="No recommendations"
                detail="Nothing in the visible configuration raised a question. Utilization, idle time and cost were not measured, so this is not a finding that the workspace is optimally configured."
              />
            ) : (
              <ol className="flex flex-col gap-4">
                {opportunities.map((item, index) => (
                  <RecommendationItem
                    key={`${item.resource_id}:${index}`}
                    item={item}
                    index={index + 1}
                  />
                ))}
              </ol>
            )}

            {/* The limits of the whole analysis, stated once. A reader reaches
                every recommendation already knowing what was not measured. */}
            {analysis.missing_evidence.length > 0 && (
              <section className="surface overflow-hidden">
                <header className="px-5 py-4">
                  <h2 className="text-sm font-semibold text-ink">Limitations</h2>
                  <p className="mt-1 text-xs text-ink-faint">
                    Discovery reads configuration, not behaviour. These were not observed.
                  </p>
                </header>
                <ul className="border-t border-panel-border px-5 py-3">
                  {analysis.missing_evidence.map((line) => (
                    <li key={line} className="text-xs text-ink-muted">
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

function RecommendationItem({ item, index }: { item: AgentOpportunity; index: number }) {
  const rows: { label: string; value: string; mono?: boolean }[] = [
    { label: "Finding", value: item.potential_issue },
    { label: "Observed evidence", value: item.observed_evidence, mono: true },
    { label: "Recommendation", value: item.recommendation },
    // Rationale links the two facts the backend supplied — the evidence and
    // what it implies. It composes existing fields rather than asserting
    // anything the backend did not.
    {
      label: "Rationale",
      value: `Raised because ${item.observed_evidence} was observed on this resource, which indicates: ${item.potential_issue}`,
    },
    { label: "Evidence required before action", value: item.evidence_required },
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
        {/* Never "recommended" or "confirmed": every item here derives from
            configuration alone and needs verification before anyone acts. */}
        <StatusBadge label="Potential Optimization Opportunity" kind="status" className="shrink-0" />
      </header>

      <dl className="border-t border-panel-border px-5 py-3">
        {rows.map((row) => (
          <div key={row.label} className="flex flex-col gap-0.5 py-1 sm:flex-row sm:gap-3">
            <dt className="shrink-0 text-xs text-ink-faint sm:w-56">{row.label}</dt>
            <dd className={`text-xs text-ink-muted ${row.mono ? "font-mono" : ""}`}>{row.value}</dd>
          </div>
        ))}
      </dl>
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
          <StatePanel kind="loading" title="Loading recommendations…" />
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
