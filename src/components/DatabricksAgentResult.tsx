import React from "react";
import { AlertCircle, Check, Circle, Loader2, X } from "lucide-react";
import type {
  AgentAnalysisResult,
  AgentAnalysisStep,
  AgentOpportunity,
} from "../services/databricksAgentApi";

/**
 * The agent's Databricks compute analysis.
 *
 * Every value shown here comes from the backend: the steps reflect work that
 * actually completed, and the opportunities carry the evidence they were
 * derived from. Nothing is animated on a timer and no figure is computed in
 * the browser — the backend deliberately states no cost or saving, because
 * discovery measures configuration, not consumption.
 */

const TYPE_LABELS: Record<string, string> = {
  CLASSIC_CLUSTER: "Classic cluster",
  SERVERLESS_COMPUTE: "Serverless compute",
  SQL_WAREHOUSE: "SQL warehouse",
};

function StepRow({ step, isLast }: { step: AgentAnalysisStep; isLast: boolean }) {
  return (
    <li className="flex gap-3">
      <div className="flex flex-col items-center">
        <span
          className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-full border ${
            step.status === "done"
              ? "bg-brand-500/15 border-brand-500/40 text-brand-300"
              : step.status === "failed"
              ? "bg-signal-high/15 border-signal-high/40 text-signal-high"
              : "bg-canvas-raised border-panel-border text-ink-faint"
          }`}
        >
          {step.status === "done" && <Check size={13} />}
          {step.status === "failed" && <X size={13} />}
          {step.status === "pending" && <Circle size={7} className="fill-current" />}
        </span>
        {!isLast && (
          <span
            className={`w-px flex-1 ${step.status === "done" ? "bg-brand-500/40" : "bg-panel-border"}`}
            style={{ minHeight: "18px" }}
          />
        )}
      </div>
      <div className="pb-4">
        <p className={`text-sm ${step.status === "pending" ? "text-ink-faint" : "text-ink"}`}>
          {step.label}
        </p>
        {step.detail && <p className="mt-0.5 text-xs text-ink-faint">{step.detail}</p>}
      </div>
    </li>
  );
}

function Opportunity({ item, index }: { item: AgentOpportunity; index: number }) {
  const rows: { label: string; value: string; mono?: boolean }[] = [
    { label: "Observed evidence", value: item.observed_evidence, mono: true },
    { label: "Potential issue", value: item.potential_issue },
    { label: "Recommendation", value: item.recommendation },
    { label: "Evidence required before execution", value: item.evidence_required },
    { label: "Expected impact", value: item.expected_impact },
  ];

  return (
    <li className="border-t border-panel-border px-4 py-3 first:border-t-0">
      <p className="text-sm font-medium text-ink">
        {index}. {item.resource}
        <span className="ml-2 text-xs font-normal text-ink-faint">
          {TYPE_LABELS[item.resource_type] ?? item.resource_type}
        </span>
      </p>
      <dl className="mt-2 space-y-1">
        {rows.map((row) => (
          <div key={row.label} className="flex flex-col gap-0.5 sm:flex-row sm:gap-2">
            <dt className="shrink-0 text-xs text-ink-faint sm:w-56">{row.label}</dt>
            <dd className={`text-xs text-ink-muted ${row.mono ? "font-mono" : ""}`}>{row.value}</dd>
          </div>
        ))}
      </dl>
    </li>
  );
}

export default function DatabricksAgentResult({
  result,
  running,
}: {
  result: AgentAnalysisResult | null;
  /** True while the backend call is in flight, before any step has a verdict. */
  running?: boolean;
}) {
  if (running && !result) {
    return (
      <div className="surface px-4 py-4">
        <p className="flex items-center gap-2 text-sm text-ink-muted">
          <Loader2 size={14} className="animate-spin" />
          Analyzing Databricks environment…
        </p>
      </div>
    );
  }

  if (!result) return null;

  const analysis = result.analysis;

  return (
    <div className="flex flex-col gap-4">
      <div className="surface px-4 py-4">
        <ol className="space-y-0">
          {result.steps.map((step, index) => (
            <StepRow key={step.id} step={step} isLast={index === result.steps.length - 1} />
          ))}
        </ol>
      </div>

      {!result.ok && (
        <div className="surface flex items-start gap-2 px-4 py-3 text-sm text-signal-high">
          <AlertCircle size={15} className="mt-0.5 shrink-0" />
          <span>
            <strong className="font-medium">{result.status}</strong>
            {result.message ? ` — ${result.message}` : ""}
          </span>
        </div>
      )}

      {analysis && (
        <>
          {analysis.summary && (
            <div className="surface px-4 py-3">
              <p className="text-sm text-ink-muted">{analysis.summary}</p>
            </div>
          )}

          <section className="surface overflow-hidden">
            <header className="px-4 py-3">
              <h3 className="text-sm font-semibold text-ink">Observed Facts</h3>
              <p className="mt-1 text-xs text-ink-faint">
                {analysis.observed_facts.resource_count} resource(s) discovered
                {analysis.workspace_name ? ` in ${analysis.workspace_name}` : ""}.
              </p>
            </header>

            {analysis.classification.length > 0 && (
              <div className="overflow-x-auto border-t border-panel-border">
                <table className="w-full text-left text-xs">
                  <thead className="text-ink-faint">
                    <tr>
                      <th className="px-4 py-2 font-normal">Resource</th>
                      <th className="px-4 py-2 font-normal">Type</th>
                      <th className="px-4 py-2 font-normal">State</th>
                      <th className="px-4 py-2 font-normal">Relevant configuration</th>
                    </tr>
                  </thead>
                  <tbody>
                    {analysis.classification.map((row) => (
                      <tr key={`${row.type}:${row.resource}`} className="border-t border-panel-border">
                        <td className="px-4 py-2 text-ink">{row.resource}</td>
                        <td className="px-4 py-2 text-ink-muted">{TYPE_LABELS[row.type] ?? row.type}</td>
                        <td className="px-4 py-2 text-ink-muted">{row.state}</td>
                        <td className="px-4 py-2 text-ink-muted">{row.configuration}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="surface overflow-hidden">
            <header className="px-4 py-3">
              <h3 className="text-sm font-semibold text-ink">Missing Evidence</h3>
              <p className="mt-1 text-xs text-ink-faint">
                Discovery reads configuration, not behaviour. These were not observed.
              </p>
            </header>
            <ul className="border-t border-panel-border px-4 py-3">
              {analysis.missing_evidence.map((item) => (
                <li key={item} className="text-xs text-ink-muted">
                  • {item}
                </li>
              ))}
            </ul>
          </section>

          <section className="surface overflow-hidden">
            <header className="px-4 py-3">
              <h3 className="text-sm font-semibold text-ink">Potential Optimization Opportunities</h3>
              <p className="mt-1 text-xs text-ink-faint">
                Raised from configuration. Each needs its listed evidence before anyone acts.
              </p>
            </header>
            {analysis.opportunities.length === 0 ? (
              <p className="border-t border-panel-border px-4 py-6 text-center text-sm text-ink-muted">
                None raised — which is not a finding that the workspace is optimally configured.
              </p>
            ) : (
              <ul>
                {analysis.opportunities.map((item, index) => (
                  <Opportunity key={`${item.resource_id}:${index}`} item={item} index={index + 1} />
                ))}
              </ul>
            )}
          </section>
        </>
      )}
    </div>
  );
}
