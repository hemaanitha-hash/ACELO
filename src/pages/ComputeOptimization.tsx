import React, { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Sparkles } from "lucide-react";
import Layout from "../components/Layout";
import PageHeader from "../components/PageHeader";
import Button from "../components/Button";
import StateBlock, { StatePanel } from "../components/StateBlock";
import DatabricksAgentResult from "../components/DatabricksAgentResult";
import {
  AgentApiError,
  analyzeDatabricksCompute,
  type AgentAnalysisResult,
} from "../services/databricksAgentApi";
import { setComputeAnalysis } from "../services/computeAnalysis";

/**
 * Compute Optimization — the MVP's main capability.
 *
 * It runs the journey end to end in one place:
 *
 *   Databricks Discovery -> Evidence -> Analysis -> Findings -> Recommendations
 *
 * Deliberately NOT a new engine: it calls the same backend agent analysis the
 * AI Agent uses, and renders it with the same component. This page is the
 * direct, non-conversational entry to that capability for a user who already
 * knows what they want.
 *
 * Everything shown is discovered evidence. The backend states no cost or saving
 * because none is measured, and nothing here invents one.
 */

const STAGES = [
  { id: "discovery", label: "Discovery", detail: "Read the workspace's compute resources." },
  { id: "evidence", label: "Evidence", detail: "Record the configuration each resource reports." },
  { id: "analysis", label: "Analysis", detail: "Compare that evidence against optimization patterns." },
  { id: "findings", label: "Findings", detail: "Raise what the configuration suggests." },
  { id: "recommendations", label: "Recommendations", detail: "State what to review, and what to verify first." },
];

export default function ComputeOptimization() {
  const navigate = useNavigate();
  const [result, setResult] = useState<AgentAnalysisResult | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function analyze() {
    setRunning(true);
    setError(null);
    try {
      const analysis = await analyzeDatabricksCompute(
        "Analyze my Databricks compute and find optimization opportunities.",
      );
      setResult(analysis);
      // Share it so Recommendations shows THESE findings, not a second run.
      setComputeAnalysis(analysis);
    } catch (e: unknown) {
      setResult(null);
      setError(e instanceof AgentApiError ? e.message : "The compute analysis could not be completed.");
    } finally {
      setRunning(false);
    }
  }

  // The page's whole purpose is this analysis, so it starts on arrival rather
  // than making the user press a button to see anything at all.
  useEffect(() => {
    void analyze();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const findings = result?.analysis?.opportunities.length ?? 0;

  return (
    <Layout pageName="Compute Optimization" onRefresh={() => void analyze()}>
      <div className="flex flex-col gap-6">
        <PageHeader
          eyebrow="Databricks"
          title="Compute Optimization"
          description="Discovers the compute in this workspace, then reports what its configuration suggests. Read-only — nothing here changes a Databricks resource."
          action={
            <Button
              icon={<Sparkles size={16} />}
              variant="secondary"
              onClick={() => navigate("/agent")}
            >
              Ask the agent
            </Button>
          }
        />

        {/* The journey, stated once so a first-time user knows what they are
            looking at before the results appear. */}
        <ol className="grid gap-px overflow-hidden rounded-lg border border-panel-border bg-panel-border sm:grid-cols-5">
          {STAGES.map((stage, index) => (
            <li key={stage.id} className="bg-panel px-4 py-3">
              <p className="label-eyebrow">Step {index + 1}</p>
              <p className="mt-1 text-sm font-medium text-ink">{stage.label}</p>
              <p className="mt-1 text-xs text-ink-faint">{stage.detail}</p>
            </li>
          ))}
        </ol>

        {error && <StatePanel kind="error" title="Analysis failed" detail={error} />}

        {running && !result && (
          <StatePanel kind="loading" title="Analyzing your Databricks compute…" />
        )}

        {result && (
          <>
            {result.ok && (
              <div className="surface flex flex-wrap items-center gap-x-8 gap-y-3 px-5 py-4">
                <Metric
                  label="Resources analyzed"
                  value={result.analysis?.observed_facts.resource_count ?? 0}
                />
                <Metric label="Findings" value={findings} />
                <Metric label="Recommendations" value={findings} />
              </div>
            )}
            <DatabricksAgentResult result={result} running={running} />
          </>
        )}

        {result?.ok && findings === 0 && (
          <StateBlock
            compact
            kind="empty"
            title="No configuration findings"
            detail="Nothing in the visible configuration raised a question. Utilization, idle time and cost were not measured, so this is not a statement that the workspace is optimally configured."
          />
        )}
      </div>
    </Layout>
  );
}

/** One counted fact. Counts only — never a cost or a saving. */
function Metric({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <p className="tabular text-xl font-semibold text-ink">{value}</p>
      <p className="text-xs text-ink-muted">{label}</p>
    </div>
  );
}
