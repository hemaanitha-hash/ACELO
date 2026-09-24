import React, { useEffect, useState } from "react";
import { AlertCircle, CheckCircle2, Database } from "lucide-react";
import Layout from "../components/Layout";
import PageHeader from "../components/PageHeader";
import StateBlock, { StatePanel } from "../components/StateBlock";
import StatusBadge from "../components/StatusBadge";
import { explainDiscovery } from "../services/discoveryText";
import {
  DatabricksApiError,
  getDatabricksResources,
  groupByType,
  type AceloResource,
  type DatabricksResourceType,
  type DatabricksResources,
  type ResourceTypeStatus,
} from "../services/databricksApi";

const SECTIONS: { type: DatabricksResourceType; title: string; hint: string }[] = [
  {
    type: "CLASSIC_CLUSTER",
    title: "Classic Clusters",
    hint: "All-purpose and job clusters visible to the connected identity.",
  },
  {
    type: "SERVERLESS_COMPUTE",
    title: "Serverless Compute",
    hint: "Serverless compute is a distinct Databricks object — not a classic cluster.",
  },
  {
    type: "SQL_WAREHOUSE",
    title: "SQL Warehouses",
    hint: "SQL warehouses reported by the Databricks SQL warehouse API.",
  },
];

/** The metadata worth showing per type. Anything absent is simply not rendered. */
const SUMMARY_KEYS: Record<DatabricksResourceType, string[]> = {
  CLASSIC_CLUSTER: [
    "cluster_type",
    "driver_node_type",
    "worker_node_type",
    "num_workers",
    "autoscaling",
    "auto_termination_minutes",
  ],
  SERVERLESS_COMPUTE: [],
  SQL_WAREHOUSE: ["warehouse_type", "size", "auto_stop_mins", "scaling", "owner"],
};

const LABELS: Record<string, string> = {
  cluster_type: "Cluster type",
  driver_node_type: "Driver node",
  worker_node_type: "Worker node",
  num_workers: "Workers",
  autoscaling: "Autoscaling",
  auto_termination_minutes: "Auto-termination (min)",
  warehouse_type: "Warehouse type",
  size: "Size",
  auto_stop_mins: "Auto-stop (min)",
  scaling: "Scaling",
  owner: "Owner",
};

function formatValue(value: unknown): string | null {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>).filter(
      ([, v]) => v !== null && v !== undefined,
    );
    if (entries.length === 0) return null;
    return entries.map(([k, v]) => `${k.replace(/_/g, " ")} ${String(v)}`).join(", ");
  }
  return String(value);
}

/**
 * Which metadata keys to show for a resource. Classic clusters and warehouses
 * have a curated list; serverless compute has no fixed schema, so whatever the
 * platform actually reported is shown as-is.
 */
function summaryKeysFor(resource: AceloResource): string[] {
  const curated = SUMMARY_KEYS[resource.resource_type];
  if (curated.length > 0) return curated;
  // Skip the probe path and anything already shown as the name, id or state,
  // so a serverless object does not render its own name twice.
  const shownAbove = new Set([resource.name, resource.resource_id, resource.state]);
  return Object.keys(resource.metadata).filter(
    (k) => k !== "discovered_from" && !shownAbove.has(resource.metadata[k] as string),
  );
}

function ResourceRow({ resource }: { resource: AceloResource }) {
  const details = summaryKeysFor(resource)
    .map((key) => ({ key, value: formatValue(resource.metadata[key]) }))
    .filter((d): d is { key: string; value: string } => d.value !== null);

  return (
    <li className="border-t border-panel-border px-4 py-3 first:border-t-0">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="text-sm font-medium text-ink">{resource.name}</span>
        {resource.state && (
          <span className="rounded-sm border border-panel-border px-2 py-0.5 text-[11px] uppercase tracking-wide text-ink-muted">
            {resource.state}
          </span>
        )}
      </div>

      {resource.resource_id && (
        <p className="mt-1 font-mono text-[11px] text-ink-faint">{resource.resource_id}</p>
      )}

      {details.length > 0 && (
        <dl className="mt-2 flex flex-wrap gap-x-6 gap-y-1">
          {details.map(({ key, value }) => (
            <div key={key} className="flex gap-1.5 text-xs">
              <dt className="text-ink-faint">{LABELS[key] ?? key.replace(/_/g, " ")}</dt>
              <dd className="text-ink-muted">{value}</dd>
            </div>
          ))}
        </dl>
      )}
    </li>
  );
}

/** Short, scannable chip per outcome. The full wording lives in the block. */
const CHIP_LABELS: Record<string, string> = {
  empty: "None",
  unavailable: "Restricted",
  unsupported: "Unable to list",
  error: "Error",
  loading: "Loading",
  success: "OK",
};

function Section({
  title,
  hint,
  resources,
  status,
}: {
  title: string;
  hint: string;
  resources: AceloResource[];
  status?: ResourceTypeStatus;
}) {
  // One translation point decides what the user reads. Previously the raw
  // reason code was printed ("Unavailable — NOT_EXPOSED_BY_WORKSPACE_API"),
  // and a type that simply cannot be listed was otherwise indistinguishable
  // from one the workspace genuinely has none of.
  const explanation = explainDiscovery(status?.status, status?.reason, resources.length);
  const hasResources = resources.length > 0;

  return (
    <section className="surface overflow-hidden">
      <header className="flex flex-wrap items-start justify-between gap-x-4 gap-y-1 px-5 py-4">
        <div className="min-w-0">
          <h2 className="text-sm font-semibold text-ink">{title}</h2>
          <p className="mt-1 text-xs text-ink-faint">{hint}</p>
        </div>
        {hasResources ? (
          <span className="tabular shrink-0 text-xs text-ink-muted">
            {resources.length} found
          </span>
        ) : (
          // A short chip for scanning. Deliberately NOT the explanation's title,
          // which the block below already states — the same sentence twice in
          // one card reads as two separate problems.
          <StatusBadge label={CHIP_LABELS[explanation.kind]} kind="status" className="shrink-0" />
        )}
      </header>

      {hasResources ? (
        <ul className="border-t border-panel-border">
          {resources.map((resource) => (
            <ResourceRow key={`${resource.resource_type}:${resource.resource_id}`} resource={resource} />
          ))}
        </ul>
      ) : (
        <div className="border-t border-panel-border">
          <StateBlock
            compact
            kind={explanation.kind}
            title={explanation.title}
            detail={explanation.detail || status?.message || undefined}
          />
        </div>
      )}
    </section>
  );
}

/**
 * Databricks discovery / connection test.
 *
 * Read-only proof that ACELO reaches the real workspace and sees the real
 * compute. No recommendations, no execution controls: this phase only
 * discovers. Everything rendered comes from the backend — nothing about the
 * connection or the resource list is assumed in the browser.
 */
export default function DatabricksDiscovery() {
  const [data, setData] = useState<DatabricksResources | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    try {
      setData(await getDatabricksResources());
      setError(null);
    } catch (e: unknown) {
      setData(null);
      setError(e instanceof DatabricksApiError ? e.message : "Databricks discovery failed.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  const grouped = groupByType(data?.resources ?? []);
  const statusByType = new Map((data?.statuses ?? []).map((s) => [s.resource_type, s]));

  return (
    <Layout pageName="Databricks Discovery" onRefresh={() => void load()}>
      <div className="flex flex-col gap-6">
        <PageHeader
          eyebrow="Databricks"
          title="Compute discovery"
          description="Compute resources found in the connected workspace. Read-only — nothing here changes a Databricks resource."
        />

        <div className="surface flex items-center gap-2.5 px-4 py-3">
          <Database size={15} className="text-ink-muted" />
          <span className="text-sm text-ink-muted">Connection</span>
          {loading ? (
            <span className="text-sm text-ink-muted">Checking…</span>
          ) : data?.connected ? (
            <span className="flex items-center gap-1.5 text-sm font-medium text-signal-low">
              <CheckCircle2 size={14} />
              Connected
            </span>
          ) : (
            <span className="flex items-center gap-1.5 text-sm font-medium text-signal-high">
              <AlertCircle size={14} />
              Not connected
            </span>
          )}
          {data?.workspace_name && (
            <span className="ml-auto truncate text-xs text-ink-faint">{data.workspace_name}</span>
          )}
        </div>

        {error && <StatePanel kind="error" title="Discovery failed" detail={error} />}

        {loading && !data && (
          <StatePanel kind="loading" title="Discovering compute resources…" />
        )}

        {data &&
          SECTIONS.map(({ type, title, hint }) => (
            <Section
              key={type}
              title={title}
              hint={hint}
              resources={grouped[type]}
              status={statusByType.get(type)}
            />
          ))}
      </div>
    </Layout>
  );
}
