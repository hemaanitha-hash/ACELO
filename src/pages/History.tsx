import React, { useEffect, useState } from "react";
import Layout from "../components/Layout";
import AuditTable from "../components/AuditTable";
import Modal from "../components/Modal";
import StatusBadge from "../components/StatusBadge";
import { getHistory } from "../services/api";
import type { AuditEntry } from "../types";

const detailRows: { key: keyof AuditEntry["detail"]; label: string }[] = [
  { key: "agentDecision", label: "Agent Decision" },
  { key: "analysis", label: "Analysis" },
  { key: "recommendation", label: "Recommendation" },
  { key: "approval", label: "Approval" },
  { key: "execution", label: "Execution" },
  { key: "validation", label: "Validation" },
  { key: "rollback", label: "Rollback" },
];

export default function History() {
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<AuditEntry | null>(null);

  async function load() {
    setLoading(true);
    const result = await getHistory();
    setEntries(result);
    setLoading(false);
  }

  useEffect(() => {
    load();
  }, []);

  return (
    <Layout pageName="History" onRefresh={load}>
      <div className="flex flex-col gap-6">
        <div>
          <h1 className="text-display font-semibold text-ink">History &amp; audit trail</h1>
          <p className="mt-2 text-sm text-ink-muted">
            Every request, decision and action ACELO has taken is recorded and
            traceable.
          </p>
        </div>

        {loading ? (
          <div className="surface py-16 text-center text-sm text-ink-muted">Loading history...</div>
        ) : (
          <AuditTable entries={entries} onRowClick={setSelected} />
        )}
      </div>

      <Modal
        open={selected !== null}
        onClose={() => setSelected(null)}
        title={selected ? selected.userRequest : ""}
        subtitle={selected ? `${selected.date} · ${selected.agentRoute}` : undefined}
        widthClassName="max-w-xl"
      >
        {selected && (
          <div className="flex flex-col gap-4">
            <div className="flex items-center gap-2">
              <span className="text-xs text-ink-faint">Action</span>
              <StatusBadge label={selected.action} kind="status" />
              <span className="text-xs text-ink-faint ml-2">Result</span>
              <StatusBadge label={selected.result} kind="status" />
            </div>
            <div className="divide-y divide-panel-border border-t border-panel-border">
              {detailRows.map((row) => (
                <div key={row.key} className="py-3">
                  <p className="text-xs text-ink-faint">{row.label}</p>
                  <p className="mt-1 text-sm text-ink">{selected.detail[row.key]}</p>
                </div>
              ))}
            </div>
          </div>
        )}
      </Modal>
    </Layout>
  );
}
