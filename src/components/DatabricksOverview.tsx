import React, { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { CheckCircle2, Sparkles } from "lucide-react";
import Button from "./Button";
import StateBlock, { StatePanel } from "./StateBlock";
import StatusBadge from "./StatusBadge";
import { explainDiscovery } from "../services/discoveryText";
import {
  DatabricksApiError,
  getDatabricksResources,
  groupByType,
  type DatabricksResourceType,
  type DatabricksResources,
} from "../services/databricksApi";
import { getRuntime } from "../services/runtime";

/**
 * The executive view of the connected Databricks environment.
 *
 * Every figure is a COUNT of something discovered. There is deliberately no
 * cost, saving or utilization number anywhere: discovery reads configuration,
 * not consumption, so any such figure would be invented. Where a resource type
 * could not be read, that is stated rather than shown as zero.
 */

const TYPES: { type: DatabricksResourceType; label: string }[] = [
  { type: "SQL_WAREHOUSE", label: "SQL Warehouses" },
  { type: "CLASSIC_CLUSTER", label: "Classic Clusters" },
  { type: "SERVERLESS_COMPUTE", label: "Serverless Compute" },
];

export default function DatabricksOverview() {
  const navigate = useNavigate();
  const [data, setData] = useState<DatabricksResources | null>(null);
  const [discoveredAt, setDiscoveredAt] = useState<Date | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    try {
      setData(await getDatabricksResources());
      setDiscoveredAt(new Date());
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

  const runtime = getRuntime();
  const grouped = groupByType(data?.resources ?? []);
  const statusByType = new Map((data?.statuses ?? []).map((s) => [s.resource_type, s]));
  const analyzed = data?.resources.length ?? 0;

  return (
    <div className="flex flex-col gap-6">
      {/* --- Databricks Environment --- */}
      <section className="surface px-5 py-4">
        <h2 className="text-sm font-semibold text-ink">Databricks Environment</h2>
        <dl className="mt-3 grid gap-x-8 gap-y-3 sm:grid-cols-3">
          <Fact label="Connection">
            {loading ? (
              <span className="text-ink-muted">Checking…</span>
            ) : data?.connected ? (
              <span className="flex items-center gap-1.5 font-medium text-signal-low">
                <CheckCircle2 size={14} />
                {runtime.databricks_app ? "Connected via Databricks App" : "Connected"}
              </span>
            ) : (
              <span className="font-medium text-signal-high">Not connected</span>
            )}
          </Fact>
          <Fact label="Workspace">
            <span className="break-all text-ink">
              {data?.workspace_name ??
                runtime.workspace_host?.replace("https://", "") ??
                "Current Databricks workspace"}
            </span>
          </Fact>
          <Fact label="Last discovery">
            <span className="text-ink">
              {discoveredAt ? discoveredAt.toLocaleTimeString() : "Not run yet"}
            </span>
          </Fact>
        </dl>
      </section>

      {error && <StatePanel kind="error" title="Could not reach the workspace" detail={error} />}

      {loading && !data && <StatePanel kind="loading" title="Discovering compute resources…" />}

      {data && (
        <>
          {/* --- Compute Overview --- */}
          <section className="surface overflow-hidden">
            <header className="px-5 py-4">
              <h2 className="text-sm font-semibold text-ink">Compute Overview</h2>
              <p className="mt-1 text-xs text-ink-faint">
                What this workspace reports, by resource type.
              </p>
            </header>
            <ul className="grid gap-px border-t border-panel-border bg-panel-border sm:grid-cols-3">
              {TYPES.map(({ type, label }) => {
                const count = grouped[type].length;
                const status = statusByType.get(type);
                const explanation = explainDiscovery(status?.status, status?.reason, count);
                const readable = explanation.kind === "success" || explanation.kind === "empty";

                return (
                  <li key={type} className="bg-panel px-5 py-4">
                    <p className="text-xs text-ink-muted">{label}</p>
                    {readable ? (
                      <p className="tabular mt-1 text-2xl font-semibold text-ink">{count}</p>
                    ) : (
                      <p className="mt-2">
                        <StatusBadge label={explanation.title} kind="status" />
                      </p>
                    )}
                    <p className="mt-1.5 text-xs text-ink-faint">
                      {readable ? "Discovered" : explanation.detail}
                    </p>
                  </li>
                );
              })}
            </ul>
          </section>

          {/* --- Optimization Status --- */}
          <section className="surface px-5 py-4">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div>
                <h2 className="text-sm font-semibold text-ink">Optimization Status</h2>
                <p className="mt-1 text-xs text-ink-faint">
                  Run a compute analysis to turn discovered evidence into findings.
                </p>
              </div>
              <Button
                icon={<Sparkles size={16} />}
                onClick={() => navigate("/compute")}
                className="shrink-0"
              >
                Analyze compute
              </Button>
            </div>

            {analyzed === 0 ? (
              <StateBlock
                compact
                kind="empty"
                title="Nothing discovered yet"
                detail="Findings and recommendations appear once compute has been discovered and analyzed."
              />
            ) : (
              <dl className="mt-4 flex flex-wrap gap-x-10 gap-y-3">
                <Metric label="Resources discovered" value={String(analyzed)} />
                {/* Findings and recommendations come from an analysis run, not
                    from discovery, so they are reported as not-yet-run rather
                    than as zero. */}
                <Metric label="Findings" value="Run analysis" muted />
                <Metric label="Recommendations" value="Run analysis" muted />
              </dl>
            )}
          </section>
        </>
      )}
    </div>
  );
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-ink-faint">{label}</dt>
      <dd className="mt-1 text-sm">{children}</dd>
    </div>
  );
}

function Metric({ label, value, muted }: { label: string; value: string; muted?: boolean }) {
  return (
    <div>
      <dd className={`tabular text-xl font-semibold ${muted ? "text-ink-muted" : "text-ink"}`}>
        {value}
      </dd>
      <dt className="text-xs text-ink-muted">{label}</dt>
    </div>
  );
}
