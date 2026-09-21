import React, { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { AlertCircle } from "lucide-react";
import Layout from "../components/Layout";
import { ApiError } from "../services/environmentApi";
import { display, listApprovals, usd, type Approval } from "../services/approvalsApi";

/**
 * Executions of APPROVED optimizations — real records only. An execution exists
 * here only after someone approved a recommendation AND explicitly executed it;
 * approved-but-not-executed items are listed as ready so the step stays visible.
 */
export default function Execution() {
  const navigate = useNavigate();
  const [rows, setRows] = useState<Approval[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    try {
      setRows(await listApprovals({ status: ["APPROVED", "EXECUTING", "COMPLETED", "FAILED"] }));
      setError(null);
    } catch (e: unknown) {
      setError(e instanceof ApiError ? e.message : "Unable to load executions.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  return (
    <Layout pageName="Execution" onRefresh={() => void load()}>
      <div className="flex flex-col gap-6">
        <div>
          <h1 className="text-display font-semibold text-ink">Execution</h1>
          <p className="mt-2 text-sm text-ink-muted">
            Approved optimizations and their real platform executions.
          </p>
        </div>

        {loading && <div className="surface py-16 text-center text-sm text-ink-muted">Loading executions...</div>}

        {!loading && error && (
          <div className="surface border-l-4 border-l-signal-high p-5">
            <div className="flex items-start gap-3">
              <AlertCircle size={18} className="mt-0.5 shrink-0 text-signal-high" />
              <p className="text-sm text-ink">{error}</p>
            </div>
          </div>
        )}

        {!loading && !error && rows.length === 0 && (
          <div className="surface py-16 text-center">
            <p className="text-sm font-medium text-ink">No approved optimizations yet.</p>
            <p className="mt-1 text-sm text-ink-muted">
              Approve a recommendation in Approvals, then execute it from its review page.
            </p>
          </div>
        )}

        {!loading && !error && rows.length > 0 && (
          <section className="surface p-5">
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead>
                  <tr className="border-b border-panel-border text-xs uppercase text-ink-muted">
                    <th className="py-2 pr-4 font-medium">Cluster</th>
                    <th className="py-2 pr-4 font-medium">Status</th>
                    <th className="py-2 pr-4 font-medium">Execution ID</th>
                    <th className="py-2 pr-4 font-medium">Validation</th>
                    <th className="py-2 pr-4 font-medium">Savings</th>
                    <th className="py-2 pr-4 font-medium">Approved by</th>
                    <th className="py-2 pr-4 font-medium" />
                  </tr>
                </thead>
                <tbody>
                  {rows.map((a) => (
                    <tr key={a.approval_id} className="border-b border-panel-border/60">
                      <td className="py-2 pr-4 text-ink">{a.resource_name}</td>
                      <td className="py-2 pr-4 text-xs font-semibold text-ink">
                        {a.status === "APPROVED" ? "APPROVED · ready to execute" : a.status}
                        {a.status === "FAILED" && a.execution_error && (
                          <span className="block font-normal text-signal-high">{a.execution_error}</span>
                        )}
                      </td>
                      <td className="py-2 pr-4 font-mono text-xs text-ink-muted">{a.execution_id ?? "—"}</td>
                      <td className="py-2 pr-4 text-ink-muted">{a.validation_status ?? "—"}</td>
                      <td className="tabular py-2 pr-4 text-ink">{display(a.potential_monthly_savings, usd)}</td>
                      <td className="py-2 pr-4 text-ink-muted">{a.approved_by ?? "Not available"}</td>
                      <td className="py-2 pr-4">
                        <button
                          type="button"
                          onClick={() => navigate(`/approvals/${a.approval_id}`)}
                          className="rounded-sm border border-brand-500 px-3 py-1 text-xs font-medium text-brand-500 hover:bg-brand-500/10"
                        >
                          Open
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        )}
      </div>
    </Layout>
  );
}
