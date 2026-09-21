import React, { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { AlertCircle, ArrowUp, FileText, Paperclip, Sparkles, X } from "lucide-react";
import { suggestedPrompts } from "../data/prompts";
import { detectApprovalIntent } from "../services/approvalsApi";
import type { AgentStep } from "../types";
import AgentTimeline from "./AgentTimeline";
import Button from "./Button";
import { useMsal } from "@azure/msal-react";
import { ApiError } from "../services/environmentApi";
import { FabricAuthError, getFabricToken, getSqlEndpointToken } from "../services/fabricAuth";
import {
  cancelJob,
  describeError,
  getJob,
  getJobResults,
  isTerminal,
  MAX_UPLOAD_MB,
  resolveExecutionTarget,
  SESSION_EXPIRED_MESSAGE,
  startAnalysis,
  STATUS_LABELS,
  uploadClusterFile,
  UploadValidationError,
  type AnalysisJob,
  type JobResult,
} from "../services/executionApi";

/**
 * Step 2: this component now drives a REAL platform run.
 *
 * It previously animated a fixed set of steps with setTimeout and never called
 * the backend. Every state shown below now comes from the backend's view of the
 * actual platform job — including failures, which are surfaced rather than
 * hidden behind a demo result.
 */

type RunState = "idle" | "starting" | "running" | "done" | "error";

const POLL_INTERVAL_MS = 3000;

/** Requests that make sense for an uploaded Cluster dataset. */
const FILE_PROMPTS = [
  "Analyze this cluster file",
  "Check my cluster utilization",
  "Find idle clusters",
  "Find oversized clusters",
];

interface AgentWorkspaceProps {
  initialPrompt?: string;
}

export default function AgentWorkspace({ initialPrompt }: AgentWorkspaceProps) {
  const navigate = useNavigate();
  const { instance } = useMsal();
  const [prompt, setPrompt] = useState(initialPrompt ?? "");
  const [runState, setRunState] = useState<RunState>("idle");
  const [job, setJob] = useState<AnalysisJob | null>(null);
  const [results, setResults] = useState<JobResult[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [uploadError, setUploadError] = useState<UploadValidationError | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const fileInput = useRef<HTMLInputElement | null>(null);
  const poller = useRef<number | null>(null);
  // Whether the current run's environment uses delegated (Microsoft Account) auth.
  const delegated = useRef(false);

  useEffect(() => {
    return () => stopPolling();
  }, []);

  useEffect(() => {
    if (initialPrompt) startRun(initialPrompt);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /**
   * Delegated Fabric token for this request.
   *
   * "Microsoft Account" environments need it — the backend cannot authenticate
   * as the signed-in user without it. Service-principal environments have no
   * MSAL account, so this returns null and the backend uses its own stored
   * credential.
   */
  async function fabricToken(): Promise<string | null> {
    const account = instance.getActiveAccount() ?? instance.getAllAccounts()[0];
    if (!account) return null;
    try {
      return await getFabricToken(instance);
    } catch (e: unknown) {
      if (e instanceof FabricAuthError) return null;
      throw e;
    }
  }

  /**
   * A fresh delegated Fabric token, or a clean "sign in again" error.
   * getFabricToken() renews silently and only prompts when MSAL requires it.
   */
  async function requireDelegatedToken(): Promise<string> {
    try {
      const token = await getFabricToken(instance);
      if (token) return token;
    } catch (e: unknown) {
      if (!(e instanceof FabricAuthError)) throw e;
      if (e.code === "CONSENT_REQUIRED") throw new ApiError(e.message, 401);
    }
    throw new ApiError(SESSION_EXPIRED_MESSAGE, 401);
  }

  function stopPolling() {
    if (poller.current) {
      window.clearInterval(poller.current);
      poller.current = null;
    }
  }

  async function startRun(text: string) {
    // With a file attached an empty prompt is fine: the file itself is the request.
    const trimmed = text.trim() || (file ? FILE_PROMPTS[0] : "");
    if (!trimmed || runState === "starting" || runState === "running") return;
    if (file && !text.trim()) setPrompt(trimmed);

    // Approval requests go to the Approvals UI — checked BEFORE analysis routing,
    // since "pending cluster approvals" must not start a cluster run. The agent
    // never approves or rejects by itself: it opens the confirmation workflow.
    const intent = file ? null : detectApprovalIntent(trimmed);
    if (intent) {
      navigate(
        intent.kind === "list"
          ? `/approvals?status=${intent.status}`
          : `/approvals?cluster=${encodeURIComponent(intent.cluster)}${
              intent.kind === "review" ? "" : `&action=${intent.kind}`
            }`
      );
      return;
    }

    stopPolling();
    setError(null);
    setUploadError(null);
    setResults([]);
    setJob(null);
    setRunState("starting");

    try {
      // A request with an uploaded file always takes the file-analysis path:
      // no Fabric/Databricks connection or token is needed.
      let started: AnalysisJob;
      if (file) {
        started = await uploadClusterFile(file, trimmed);
      } else {
        const target = await resolveExecutionTarget();
        delegated.current = target.delegated;
        // Delegated environments authenticate ONLY with the user's own token, so
        // a fresh one is required on every submission. Without it the request is
        // never sent — the backend has no other credential to use.
        const token = target.delegated ? await requireDelegatedToken() : await fabricToken();
        started = await startAnalysis(trimmed, target.connectionId, token);
      }
      setJob(started);

      if (isTerminal(started)) {
        await finish(started.id);
        return;
      }

      setRunState("running");
      poller.current = window.setInterval(() => void poll(started.id), POLL_INTERVAL_MS);
    } catch (e: unknown) {
      setRunState("error");
      if (e instanceof UploadValidationError) setUploadError(e);
      setError(e instanceof ApiError ? e.message : "Could not start the analysis.");
    }
  }

  async function poll(jobId: string) {
    try {
      const token = await fabricToken();
      // A delegated poll without a token would authenticate as nobody and could
      // fail a healthy run. Skip this tick; the next one retries.
      if (delegated.current && !token) return;
      const latest = await getJob(jobId, token);
      setJob(latest);
      if (isTerminal(latest)) {
        stopPolling();
        await finish(jobId);
      }
    } catch (e: unknown) {
      // A transient polling failure should not destroy a live run's view;
      // the next tick retries. Only report it if it keeps failing.
      if (e instanceof ApiError && e.status === 404) {
        stopPolling();
        setRunState("error");
        setError("This analysis job could no longer be found.");
      }
    }
  }

  async function finish(jobId: string) {
    setRunState("done");
    try {
      // Delegated environments read the result table as the user, which needs a
      // token for the Lakehouse SQL endpoint in addition to the Fabric one.
      const sqlToken = delegated.current ? await getSqlEndpointToken(instance) : null;
      setResults(await getJobResults(jobId, await fabricToken(), sqlToken));
    } catch {
      // Results genuinely unavailable — the run panel reports that honestly.
      setResults([]);
    }
  }

  async function handleCancel() {
    if (!job) return;
    try {
      setJob(await cancelJob(job.id, await fabricToken()));
    } catch (e: unknown) {
      setError(e instanceof ApiError ? e.message : "Could not cancel this run.");
    }
  }

  function reset() {
    stopPolling();
    setPrompt("");
    setRunState("idle");
    setJob(null);
    setResults([]);
    setError(null);
    setUploadError(null);
    setFile(null);
  }

  function pickFile(selected: File | null) {
    setError(null);
    if (selected && !selected.name.toLowerCase().endsWith(".csv")) {
      setError("Only CSV files are supported for cluster analysis right now.");
      return;
    }
    if (selected && selected.size > MAX_UPLOAD_MB * 1024 * 1024) {
      setError(`The file is larger than the ${MAX_UPLOAD_MB} MB upload limit.`);
      return;
    }
    setFile(selected);
  }

  // Timeline steps are derived from the real runs — one per domain actually
  // dispatched to the platform.
  const steps: AgentStep[] = (job?.job_runs ?? []).map((run) => ({
    id: run.id,
    label: `${run.domain.toUpperCase()} — ${STATUS_LABELS[run.status] ?? run.status}`,
    status:
      run.status === "COMPLETED"
        ? "done"
        : run.status === "FAILED" || run.status === "CANCELLED"
          ? "pending"
          : "active",
  }));

  const anyRunning = job?.job_runs.some(
    (r) => !["COMPLETED", "FAILED", "CANCELLED"].includes(r.status)
  );

  return (
    <div className="surface p-6 sm:p-8">
      {runState === "idle" && (
        <>
          <div className="flex items-start gap-3">
            <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-sm bg-brand-500/15 border border-brand-500/25">
              <Sparkles size={16} className="text-brand-300" />
            </span>
            <div>
              <h2 className="text-base font-semibold text-ink">Your optimization copilot</h2>
              <p className="mt-1 text-sm text-ink-muted max-w-xl">
                Describe what you want to analyze. ACELO understands the intent
                and runs the matching optimization notebook in your connected
                workspace.
              </p>
            </div>
          </div>

          <form
            onSubmit={(e) => {
              e.preventDefault();
              void startRun(prompt);
            }}
            className="mt-6 flex items-center gap-2 rounded-sm border border-panel-border bg-canvas-raised px-4 py-3 focus-within:border-brand-500 transition-colors"
          >
            <input
              ref={fileInput}
              type="file"
              accept=".csv,text/csv"
              className="hidden"
              aria-label="Upload cluster CSV"
              onChange={(e) => {
                pickFile(e.target.files?.[0] ?? null);
                e.target.value = "";
              }}
            />
            <button
              type="button"
              onClick={() => fileInput.current?.click()}
              aria-label="Attach a cluster CSV"
              title="Attach a cluster CSV"
              className="flex h-8 w-8 items-center justify-center rounded-sm text-ink-muted transition-colors hover:text-ink"
            >
              <Paperclip size={16} />
            </button>
            <input
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder={
                file ? "Analyze this cluster file" : "Ask ACELO to analyze your platform..."
              }
              className="flex-1 bg-transparent text-sm text-ink placeholder:text-ink-faint outline-none"
            />
            <button
              type="submit"
              disabled={!prompt.trim() && !file}
              aria-label="Send"
              className="flex h-8 w-8 items-center justify-center rounded-sm bg-brand-500 text-white transition-colors hover:bg-brand-400 disabled:opacity-40 disabled:cursor-not-allowed"
            >
              <ArrowUp size={16} />
            </button>
          </form>

          {file && (
            <div className="mt-3 flex flex-wrap items-center gap-2 text-sm text-ink">
              <FileText size={14} className="text-brand-300" />
              <span className="truncate">{file.name}</span>
              <span className="text-xs text-ink-faint">{(file.size / 1024).toFixed(1)} KB</span>
              <button
                type="button"
                onClick={() => setFile(null)}
                aria-label="Remove file"
                className="text-ink-muted hover:text-ink"
              >
                <X size={14} />
              </button>
              <span className="text-xs text-ink-muted">
                Analysed by the ACELO Cluster optimizer. No platform connection needed.
              </span>
            </div>
          )}
          {error && <p className="mt-3 text-sm text-signal-high">{error}</p>}

          <div className="mt-4 flex flex-wrap gap-2">
            {(file ? FILE_PROMPTS : suggestedPrompts).map((p) => (
              <button
                key={p}
                onClick={() => {
                  setPrompt(p);
                  void startRun(p);
                }}
                className="rounded-sm border border-panel-border bg-panel px-3 py-1.5 text-xs text-ink-muted transition-colors hover:border-panel-borderStrong hover:text-ink"
              >
                {p}
              </button>
            ))}
          </div>
        </>
      )}

      {runState !== "idle" && (
        <div>
          <div className="flex items-center justify-between gap-4">
            <div className="min-w-0">
              <p className="label-eyebrow">Request</p>
              <p className="mt-1 truncate text-sm font-medium text-ink">{prompt}</p>
            </div>
            <div className="flex shrink-0 gap-2">
              {anyRunning && (
                <Button variant="ghost" onClick={() => void handleCancel()}>
                  Cancel
                </Button>
              )}
              <Button variant="ghost" onClick={reset}>
                New request
              </Button>
            </div>
          </div>

          {error && (
            <div className="mt-5 flex items-start gap-3 rounded-sm border-l-4 border-l-signal-high bg-canvas-raised p-4">
              <AlertCircle size={18} className="mt-0.5 shrink-0 text-signal-high" />
              <div>
                <p className="text-sm font-medium text-ink">Analysis could not be performed</p>
                <p className="mt-1 text-sm text-ink-muted">{error}</p>
                {uploadError && uploadError.missingColumns.length > 0 && (
                  <p className="mt-2 text-sm text-ink">
                    Missing columns:{" "}
                    <span className="font-mono text-xs">
                      {uploadError.missingColumns.join(", ")}
                    </span>
                  </p>
                )}
                {uploadError && uploadError.invalidValues.length > 0 && (
                  <ul className="mt-2 space-y-1 text-xs text-ink-muted">
                    {uploadError.invalidValues.map((v) => (
                      <li key={`${v.row}-${v.column}`}>
                        Row {v.row}, <span className="font-mono">{v.column}</span>
                        {v.value ? ` = "${v.value}"` : ""} {v.reason}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
          )}

          <div className="mt-6 grid gap-8 lg:grid-cols-[1fr_260px]">
            <div>
              <p className="label-eyebrow mb-4">Agent activity</p>
              {runState === "starting" && (
                <p className="text-sm text-ink-muted">
                  {file
                    ? "Uploading and validating the file..."
                    : "Starting the analysis on your platform..."}
                </p>
              )}
              {steps.length > 0 && <AgentTimeline steps={steps} />}
            </div>

            {job && (
              <div className="surface bg-canvas-raised p-5 h-fit">
                <p className="text-xs text-ink-faint">Source</p>
                <p className="mt-1 break-all text-sm font-medium text-brand-300">
                  {job.platform === "file"
                    ? `Uploaded file${file ? ` · ${file.name}` : ""}`
                    : job.platform}
                </p>

                <div className="mt-5 space-y-4 border-t border-panel-border pt-5">
                  {job.job_runs.map((run) => {
                    const result = results.find((r) => r.job_run_id === run.id);
                    return (
                      <div key={run.id}>
                        <p className="text-sm font-medium text-ink">
                          {run.domain} — {STATUS_LABELS[run.status] ?? run.status}
                        </p>

                        {/* Only ever the id the platform returned; never generated here. */}
                        {run.platform_run_id && (
                          <p className="mt-1 break-all text-[11px] text-ink-faint">
                            {job.platform === "fabric" ? "Fabric Run ID" : "Platform run"}:{" "}
                            <span data-testid="platform-run-id" className="font-mono">
                              {run.platform_run_id}
                            </span>
                          </p>
                        )}
                        <p className="mt-0.5 break-all text-[11px] text-ink-faint">
                          ACELO run: <span className="font-mono">{run.id}</span>
                        </p>

                        {run.error_code && (
                          <p className="mt-1 text-xs text-signal-high">
                            {describeError(run.error_code, run.error)}
                          </p>
                        )}

                        {/* Result counts come from the real notebook output only. */}
                        {result?.available && (
                          <p className="mt-1 text-xs text-ink-muted">
                            {String((result.payload as any)?.row_count ?? 0)} result rows
                          </p>
                        )}
                        {result?.available && (result.payload as any)?.parameter_verification && (
                          <p data-testid="parameter-verification" className="mt-1 text-xs text-ink-muted">
                            Notebook parameters:{" "}
                            {String((result.payload as any).parameter_verification.status)}
                          </p>
                        )}
                        {run.status === "COMPLETED" && result && !result.available && (
                          <p className="mt-1 text-xs text-signal-medium">
                            {describeError(
                              result.error_code,
                              result.error ?? "Results are not available for this run."
                            )}
                          </p>
                        )}
                      </div>
                    );
                  })}
                </div>

                {results.some((r) => r.available && r.domain === "cluster") && (
                  <Button
                    className="mt-5 w-full"
                    onClick={() => navigate(`/results?job=${job.id}`)}
                  >
                    View Results
                  </Button>
                )}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
