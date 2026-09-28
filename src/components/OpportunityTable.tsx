import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { CheckCircle2, ChevronRight, Clock3, Send } from "lucide-react";
import type { Opportunity } from "../types";
import StatusBadge from "./StatusBadge";
import Button from "./Button";

interface OpportunityTableProps {
  opportunities: Opportunity[];
  emptyLabel?: string;

  /**
   * Called when the operator asks to send a recommendation
   * into the Stage 1 approval workflow.
   *
   * The parent owns the real API call. This keeps the table reusable
   * and prevents approval business logic from being embedded in UI.
   */
  onRequestApproval?: (recommendationId: string) => Promise<void> | void;
}

function formatCurrency(value: number): string {
  return `$${value.toFixed(2)}/month`;
}

function approvalAction(status: string): {
  label: string;
  icon: React.ReactNode;
  disabled: boolean;
} | null {
  const normalized = status.trim().toLowerCase();

  if (normalized === "review" || normalized === "open") {
    return {
      label: "Send to Approval",
      icon: <Send size={14} />,
      disabled: false,
    };
  }

  if (normalized === "pending approval" || normalized === "pending") {
    return {
      label: "Pending Approval",
      icon: <Clock3 size={14} />,
      disabled: true,
    };
  }

  if (normalized === "approved") {
    return {
      label: "Approved",
      icon: <CheckCircle2 size={14} />,
      disabled: true,
    };
  }

  return null;
}

export default function OpportunityTable({
  opportunities,
  emptyLabel = "No opportunities match the current filters.",
  onRequestApproval,
}: OpportunityTableProps) {
  const navigate = useNavigate();
  const [requestingId, setRequestingId] = useState<string | null>(null);

  if (opportunities.length === 0) {
    return (
      <div className="surface flex flex-col items-center justify-center gap-1 py-16 text-center">
        <p className="text-sm font-medium text-ink">Nothing to show here</p>
        <p className="text-sm text-ink-muted">{emptyLabel}</p>
      </div>
    );
  }

  async function handleRequestApproval(
    event: React.MouseEvent,
    recommendationId: string,
  ) {
    event.stopPropagation();

    if (!onRequestApproval || requestingId === recommendationId) {
      return;
    }

    setRequestingId(recommendationId);

    try {
      await onRequestApproval(recommendationId);
    } finally {
      setRequestingId(null);
    }
  }

  return (
    <div className="surface overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full min-w-[900px] text-left text-sm">
          <thead>
            <tr className="border-b border-panel-border text-xs text-ink-faint">
              <th className="px-5 py-3 font-medium">Opportunity</th>
              <th className="px-5 py-3 font-medium">Domain</th>
              <th className="px-5 py-3 font-medium">Resource</th>
              <th className="px-5 py-3 font-medium">Impact</th>
              <th className="px-5 py-3 font-medium">Severity</th>
              <th className="px-5 py-3 font-medium">Status</th>
              <th className="px-5 py-3 font-medium">Approval</th>
              <th className="px-5 py-3" />
            </tr>
          </thead>

          <tbody>
            {opportunities.map((opp) => {
              const action = approvalAction(opp.status);
              const isRequesting = requestingId === opp.id;

              return (
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
                    {opp.impactMonthly === null
                      ? "Not available"
                      : formatCurrency(opp.impactMonthly)}
                  </td>

                  <td className="px-5 py-4">
                    <StatusBadge
                      label={opp.severity}
                      kind="severity"
                    />
                  </td>

                  <td className="px-5 py-4">
                    <StatusBadge
                      label={opp.status}
                      kind="status"
                    />
                  </td>

                  <td className="px-5 py-4">
                    {action ? (
                      <Button
                        type="button"
                        variant={
                          action.disabled
                            ? "secondary"
                            : "primary"
                        }
                        disabled={
                          action.disabled ||
                          !onRequestApproval ||
                          isRequesting
                        }
                        onClick={(event) =>
                          void handleRequestApproval(
                            event,
                            opp.id,
                          )
                        }
                        className="whitespace-nowrap"
                      >
                        {isRequesting
                          ? "Sending..."
                          : action.label}
                        <span className="ml-1">
                          {action.icon}
                        </span>
                      </Button>
                    ) : (
                      <span className="text-xs text-ink-faint">
                        —
                      </span>
                    )}
                  </td>

                  <td className="px-5 py-4 text-right">
                    <ChevronRight
                      size={16}
                      className="text-ink-faint"
                    />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}