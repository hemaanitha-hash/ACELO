import React from "react";
import { useNavigate } from "react-router-dom";
import { ChevronRight } from "lucide-react";
import type { Opportunity } from "../types";
import StatusBadge from "./StatusBadge";

interface OpportunityTableProps {
  opportunities: Opportunity[];
  emptyLabel?: string;
}

function formatCurrency(value: number): string {
  return `$${value.toFixed(2)}/month`;
}

export default function OpportunityTable({
  opportunities,
  emptyLabel = "No opportunities match the current filters.",
}: OpportunityTableProps) {
  const navigate = useNavigate();

  if (opportunities.length === 0) {
    return (
      <div className="surface flex flex-col items-center justify-center gap-1 py-16 text-center">
        <p className="text-sm font-medium text-ink">Nothing to show here</p>
        <p className="text-sm text-ink-muted">{emptyLabel}</p>
      </div>
    );
  }

  return (
    <div className="surface overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full min-w-[760px] text-left text-sm">
          <thead>
            <tr className="border-b border-panel-border text-xs text-ink-faint">
              <th className="px-5 py-3 font-medium">Opportunity</th>
              <th className="px-5 py-3 font-medium">Domain</th>
              <th className="px-5 py-3 font-medium">Resource</th>
              <th className="px-5 py-3 font-medium">Impact</th>
              <th className="px-5 py-3 font-medium">Severity</th>
              <th className="px-5 py-3 font-medium">Status</th>
              <th className="px-5 py-3" />
            </tr>
          </thead>
          <tbody>
            {opportunities.map((opp) => (
              <tr
                key={opp.id}
                onClick={() => navigate(`/optimizations/${opp.id}`)}
                className="cursor-pointer border-b border-panel-border last:border-b-0 transition-colors hover:bg-panel-hover"
              >
                <td className="px-5 py-4">
                  <p className="font-medium text-ink">{opp.title}</p>
                  <p className="text-xs text-ink-faint">{opp.id}</p>
                </td>
                <td className="px-5 py-4 text-ink-muted">{opp.domain}</td>
                <td className="px-5 py-4 text-ink-muted">{opp.resource}</td>
                <td className="px-5 py-4 tabular text-ink">
                  {opp.impactMonthly === null ? "Not available" : formatCurrency(opp.impactMonthly)}
                </td>
                <td className="px-5 py-4">
                  <StatusBadge label={opp.severity} kind="severity" />
                </td>
                <td className="px-5 py-4">
                  <StatusBadge label={opp.status} kind="status" />
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
