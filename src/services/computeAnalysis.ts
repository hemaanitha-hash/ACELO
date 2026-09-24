// ============================================================================
// The latest Databricks compute analysis, shared across the MVP journey.
//
//   AI Agent  ─┐
//              ├─> analyzeDatabricksCompute()  ─> DatabricksAgentResult
//   Compute   ─┘                                        │
//   Optimization                                        ▼
//                                              this store (in memory)
//                                                       │
//                                                       ▼
//                                            Recommendations page
//
// There is ONE analysis engine — `analyzeDatabricksCompute` in
// databricksAgentApi — and this module does not add a second one. It only
// remembers the most recent result so the Recommendations page shows exactly
// the findings the user just saw on Compute Optimization, rather than a second
// run that could legitimately differ.
//
// Follows the same shape as platformContext.ts (module state + subscribe), so
// non-React callers and React pages read the same value.
//
// Deliberately in memory only: an analysis is a point-in-time reading of a live
// workspace. Persisting it would let a stale finding outlive the configuration
// that produced it, and the whole design of this feature is that nothing is
// asserted without current evidence.
// ============================================================================

import { analyzeDatabricksCompute, type AgentAnalysisResult } from "./databricksAgentApi";

export interface ComputeAnalysisState {
  result: AgentAnalysisResult | null;
  /** When the stored result was produced, for "as of" labelling. */
  analyzedAt: Date | null;
}

let current: ComputeAnalysisState = { result: null, analyzedAt: null };

type Listener = (state: ComputeAnalysisState) => void;
const listeners = new Set<Listener>();

export function getComputeAnalysis(): ComputeAnalysisState {
  return current;
}

/** Records a result produced elsewhere in the journey. */
export function setComputeAnalysis(result: AgentAnalysisResult | null): void {
  current = { result, analyzedAt: result ? new Date() : null };
  for (const listener of listeners) listener(current);
}

export function subscribeComputeAnalysis(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/**
 * The stored analysis, running one if the journey has not produced one yet.
 *
 * This is what makes a direct visit to Recommendations work: it runs the SAME
 * analysis the other two pages run, never a different code path.
 */
export async function ensureComputeAnalysis(prompt?: string): Promise<AgentAnalysisResult> {
  if (current.result) return current.result;
  const result = await analyzeDatabricksCompute(
    prompt ?? "Analyze my Databricks compute and find optimization opportunities.",
  );
  setComputeAnalysis(result);
  return result;
}

/** Test seam. */
export function resetComputeAnalysis(): void {
  current = { result: null, analyzedAt: null };
  for (const listener of listeners) listener(current);
}
