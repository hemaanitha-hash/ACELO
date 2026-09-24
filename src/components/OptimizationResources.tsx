import React, { useEffect, useState } from "react";
import { AlertCircle, CheckCircle2, Loader2 } from "lucide-react";
import {
  ApiError,
  getOptimizationResources,
  saveOptimizationResources,
  type DomainResourceStatus,
  type OptimizationDomain,
  type OptimizationResources as Resources,
} from "../services/environmentApi";

const TITLES: Record<OptimizationDomain, string> = {
  cluster: "Cluster",
  query: "Query",
  storage: "Storage",
};

/** Admin-editable identifiers for Query / Storage (Cluster keeps its own form below). */
const MAPPING_FIELDS: { key: string; label: string; hint?: string }[] = [
  { key: "execution_type", label: "Execution type", hint: "notebook or pipeline" },
  { key: "notebook_id", label: "Notebook ID", hint: "blank = the notebook ACELO deployed" },
  { key: "pipeline_id", label: "Pipeline ID", hint: "only for pipeline execution" },
  { key: "lakehouse_id", label: "Lakehouse ID" },
  { key: "lakehouse_workspace_id", label: "Lakehouse workspace ID", hint: "only if in another workspace" },
  { key: "source_schema", label: "Source schema" },
  { key: "result_schema", label: "Result schema" },
  { key: "source_table", label: "Source table" },
  { key: "result_table", label: "Result table" },
  { key: "approval_tracking_table", label: "Approval tracking table" },
  { key: "validation_batch_size", label: "Validation batch size" },
];

/**
 * Environment Setup > Optimization Resources.
 *
 * Shows which resource each optimization runs with. End users never fill any
 * of this in: the AI Agent picks the domain from the request and the backend
 * loads the matching mapping. The mapping itself sits under "Advanced" for
 * administrators. Identifiers only - never credentials.
 */
export default function OptimizationResources({
  environmentId,
  refreshKey,
  children,
  onAccess,
}: {
  environmentId: string | null;
  refreshKey?: unknown;
  /** The existing Cluster mapping form, shown inside the Advanced section. */
  children?: React.ReactNode;
  /** Reports whether this user may edit the mapping (administrators only). */
  onAccess?: (canConfigure: boolean) => void;
}) {
  const [data, setData] = useState<Resources | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    if (!environmentId) return;
    try {
      const loaded = await getOptimizationResources(environmentId);
      setData(loaded);
      onAccess?.(loaded.access?.can_configure_resources ?? true);
      setError(null);
    } catch (e: unknown) {
      setError(e instanceof ApiError ? e.message : "Optimization resources could not be loaded.");
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [environmentId, refreshKey]);

  const readOnly = data?.access ? !data.access.can_configure_resources : false;

  return (
    <section className="surface p-6" data-testid="optimization-resources">
      <h2 className="text-lg font-semibold text-ink">Optimization Resources</h2>
      <p className="mt-1 text-sm text-ink-muted">
        What ACELO runs for each optimization. The AI Agent selects the optimization from the request;
        users never enter tables, lakehouses or pipelines.
      </p>

      {error && <p className="mt-3 text-sm text-[#D71920]">{error}</p>}
      {!environmentId && <p className="mt-3 text-sm text-ink-muted">Save and test a connection first.</p>}

      {data && (
        <div className="mt-4 grid gap-3 md:grid-cols-3">
          {data.domains.map((d) => (
            <DomainCard key={d.domain} status={d} />
          ))}
        </div>
      )}

      <details className="mt-5 rounded-md border border-panel-border p-4" data-testid="resource-mapping">
        <summary className="cursor-pointer text-sm font-medium text-ink">
          Advanced: resource mapping (administrators)
        </summary>
        {readOnly && (
          <p data-testid="mapping-read-only" className="mt-3 text-xs text-ink-muted">
            Read-only. The resource mapping is managed by your ACELO administrator.
          </p>
        )}
        <div className="mt-4 space-y-6">
          {children}
          {environmentId && data && (
            <>
              <DomainMapping environmentId={environmentId} status={data.domains.find((d) => d.domain === "query")}
                             onSaved={setData} readOnly={readOnly} />
              <DomainMapping environmentId={environmentId} status={data.domains.find((d) => d.domain === "storage")}
                             onSaved={setData} readOnly={readOnly} />
            </>
          )}
        </div>
      </details>
    </section>
  );
}

function DomainCard({ status }: { status: DomainResourceStatus }) {
  const r = status.resource;
  return (
    <div className="rounded-md border border-panel-border p-4" data-testid={`resource-${status.domain}`}>
      <div className="flex items-center justify-between">
        <p className="text-sm font-semibold text-ink">{TITLES[status.domain]}</p>
        {status.configured ? (
          <span className="flex items-center gap-1 text-xs font-medium text-signal-low">
            <CheckCircle2 size={14} /> Configured
          </span>
        ) : (
          <span className="flex items-center gap-1 text-xs font-medium text-signal-medium">
            <AlertCircle size={14} /> Not configured
          </span>
        )}
      </div>
      <p className="mt-2 text-xs text-ink-muted">
        {r ? (
          <>
            {r.type === "pipeline" ? "Pipeline" : "Notebook"}: {r.name ?? r.id}
            {r.source === "acelo-managed" ? " (deployed by ACELO)" : ""}
          </>
        ) : (
          "No notebook or pipeline registered"
        )}
      </p>
      {status.missing.length > 0 && (
        <p className="mt-1 text-xs text-signal-medium">Missing: {status.missing.join(", ")}</p>
      )}
      {status.domain === "query" && (
        <p className="mt-1 text-xs text-ink-muted" data-testid="llm-key-status">
          LLM API key: {status.llm_api_key_configured ? "set in backend configuration" : "not configured"}
        </p>
      )}
    </div>
  );
}

function DomainMapping({
  environmentId,
  status,
  onSaved,
  readOnly,
}: {
  environmentId: string;
  status: DomainResourceStatus | undefined;
  onSaved: (r: Resources) => void;
  readOnly: boolean;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    if (!status) return;
    const next: Record<string, string> = {};
    MAPPING_FIELDS.forEach((f) => {
      // The administrator's own values only. Values from backend configuration are
      // shown as placeholders so saving never copies them into the mapping, and
      // provisioned notebook/pipeline ids are shown in the card, not copied here.
      const admin = !status.sources || status.sources[f.key] === "admin";
      const v = status.settings[f.key];
      next[f.key] = f.key === "notebook_id" || f.key === "pipeline_id" || !admin ? "" : v ?? "";
    });
    if (!status.sources || status.sources.execution_type === "admin") {
      next.execution_type = status.settings.execution_type ?? "";
    }
    setValues(next);
  }, [status]);

  if (!status) return null;

  async function save() {
    setBusy(true);
    setMessage(null);
    try {
      onSaved(await saveOptimizationResources(environmentId, status!.domain, values));
      setMessage({ ok: true, text: `${TITLES[status!.domain]} resource mapping saved.` });
    } catch (e: unknown) {
      setMessage({ ok: false, text: e instanceof ApiError ? e.message : "The mapping could not be saved." });
    } finally {
      setBusy(false);
    }
  }

  return (
    <fieldset data-testid={`mapping-${status.domain}`} disabled={readOnly} className="min-w-0">
      <p className="text-sm font-semibold text-ink">{TITLES[status.domain]} resource mapping</p>
      <div className="mt-3 grid gap-3 md:grid-cols-2">
        {MAPPING_FIELDS.map((f) => (
          <label key={f.key} className="block text-xs text-ink-muted">
            {f.label}
            {status.sources?.[f.key] === "config" && (
              <span className="ml-1 text-[10px] text-ink-faint">(from configuration)</span>
            )}
            <input
              aria-label={`${TITLES[status.domain]} ${f.label}`}
              value={values[f.key] ?? ""}
              onChange={(e) => setValues({ ...values, [f.key]: e.target.value })}
              placeholder={(status.sources?.[f.key] === "config" && status.settings[f.key]) || f.hint || "Not configured"}
              className="mt-1 w-full rounded-md border border-panel-border bg-white px-3 py-2 text-sm text-ink outline-none focus:border-[#D71920]"
            />
          </label>
        ))}
      </div>
      <button
        type="button"
        onClick={() => void save()}
        disabled={busy}
        className="mt-3 inline-flex items-center gap-2 rounded-md bg-[#D71920] px-4 py-2 text-sm font-medium text-white hover:bg-[#b5141a] disabled:opacity-50"
      >
        {busy && <Loader2 size={14} className="animate-spin" />}
        Save {TITLES[status.domain]} mapping
      </button>
      {message && (
        <p className={`mt-2 text-xs ${message.ok ? "text-signal-low" : "text-[#D71920]"}`}>{message.text}</p>
      )}
    </fieldset>
  );
}
