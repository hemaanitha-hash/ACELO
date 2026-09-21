import React from "react";
import { ChevronRight } from "lucide-react";
import type { AuditEntry } from "../types";
import StatusBadge from "./StatusBadge";

interface AuditTableProps {
  entries: AuditEntry[];
  onRowClick: (entry: AuditEntry) => void;
}

export default function AuditTable({ entries, onRowClick }: AuditTableProps) {
  return (
    <div className="surface overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full min-w-[720px] text-left text-sm">
          <thead>
            <tr className="border-b border-panel-border text-xs text-ink-faint">
              <th className="px-5 py-3 font-medium">Date</th>
              <th className="px-5 py-3 font-medium">User Request</th>
              <th className="px-5 py-3 font-medium">Agent Route</th>
              <th className="px-5 py-3 font-medium">Action</th>
              <th className="px-5 py-3 font-medium">Result</th>
              <th className="px-5 py-3" />
            </tr>
          </thead>
          <tbody>
            {entries.map((entry) => (
              <tr
                key={entry.id}
                onClick={() => onRowClick(entry)}
                className="cursor-pointer border-b border-panel-border last:border-b-0 transition-colors hover:bg-panel-hover"
              >
                <td className="px-5 py-4 text-ink-muted whitespace-nowrap">{entry.date}</td>
                <td className="px-5 py-4 text-ink">{entry.userRequest}</td>
                <td className="px-5 py-4 text-ink-muted whitespace-nowrap">{entry.agentRoute}</td>
                <td className="px-5 py-4 text-ink-muted">{entry.action}</td>
                <td className="px-5 py-4">
                  <StatusBadge label={entry.result} kind="status" />
                </td>
                <td className="px-5 py-4 text-right">
                  <ChevronRight size={16} className="text-ink-faint" />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
