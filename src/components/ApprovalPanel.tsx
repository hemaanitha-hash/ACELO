import React, { useState } from "react";
import { Check, X as XIcon, Code2, ChevronDown, ChevronUp } from "lucide-react";
import type { ApprovalRequest } from "../types";
import StatusBadge from "./StatusBadge";
import ImpactCard from "./ImpactCard";
import Button from "./Button";

interface ApprovalPanelProps {
  approval: ApprovalRequest;
  onReview: (id: string) => void;
  onApprove: (id: string) => void;
  onReject?: (id: string) => void;
  approving?: boolean;
  approved?: boolean;
  rejected?: boolean;
}

export default function ApprovalPanel({
  approval,
  onReview,
  onApprove,
  onReject,
  approving = false,
  approved = false,
  rejected = false,
}: ApprovalPanelProps) {
  const allPassed = approval.checklist.every((c) => c.passed);
  const [showSql, setShowSql] = useState(false);

  // Sample or actual SQL query snippet for side-by-side view
  const isQuery = approval.domain === "Query";
  const originalSql = `-- Original Query (${approval.title})\nSELECT o.order_id, c.customer_name, SUM(oi.price) as total_rev\nFROM orders o\nJOIN customers c ON o.customer_id = c.customer_id\nJOIN order_items oi ON o.order_id = oi.order_id\nGROUP BY o.order_id, c.customer_name\nHAVING total_rev > 1000;`;
  const optimizedSql = `/* ACELO FinOps Refactored: Broadcast join small tables & pre-aggregate */\nWITH agg_items AS (\n  SELECT order_id, SUM(price) AS total_rev\n  FROM order_items GROUP BY order_id\n)\nSELECT /*+ BROADCAST(c) */ o.order_id, c.customer_name, ai.total_rev\nFROM agg_items ai\nJOIN orders o ON ai.order_id = o.order_id\nJOIN customers c ON o.customer_id = c.customer_id\nWHERE ai.total_rev > 1000;`;

  return (
    <div className="surface p-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="label-eyebrow">{approval.id} &middot; {approval.domain}</p>
          <h3 className="mt-1 text-sm font-semibold text-ink">{approval.title}</h3>
          <p className="mt-1 text-xs text-ink-muted">Requested by {approval.requestedBy}</p>
        </div>
        {approved ? (
          <StatusBadge label="Approved & Executing" kind="status" />
        ) : rejected ? (
          <StatusBadge label="Rejected" kind="status" />
        ) : (
          <StatusBadge label="Review Required" kind="status" />
        )}
      </div>

      <div className="mt-5 grid gap-5 md:grid-cols-[1fr_260px]">
        <div>
          <p className="label-eyebrow mb-2">Platform validation checklist</p>
          <ul className="space-y-2">
            {approval.checklist.map((item) => (
              <li key={item.label} className="flex items-center gap-2 text-sm">
                <span
                  className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-full ${
                    item.passed
                      ? "bg-brand-500/15 text-brand-500"
                      : "bg-signal-high/15 text-signal-high"
                  }`}
                >
                  {item.passed ? <Check size={12} /> : <XIcon size={12} />}
                </span>
                <span className={item.passed ? "text-ink" : "text-ink-muted"}>{item.label}</span>
              </li>
            ))}
          </ul>

          {isQuery && (
            <div className="mt-4">
              <button
                type="button"
                onClick={() => setShowSql(!showSql)}
                className="flex items-center gap-1.5 text-xs font-medium text-brand-500 hover:text-brand-600 transition-colors"
              >
                <Code2 size={14} />
                {showSql ? "Hide SQL Comparison" : "Inspect Side-by-Side SQL Diff"}
                {showSql ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
              </button>

              {showSql && (
                <div className="mt-3 grid gap-3 sm:grid-cols-2">
                  <div className="rounded border border-panel-border bg-canvas-raised p-3">
                    <p className="text-[11px] font-semibold uppercase tracking-wider text-ink-muted mb-1">
                      Original Query (Spill / High Scan)
                    </p>
                    <pre className="font-mono text-xs text-ink overflow-x-auto whitespace-pre p-2 bg-white rounded border border-panel-border/50">
                      {originalSql}
                    </pre>
                  </div>
                  <div className="rounded border border-brand-500/30 bg-brand-500/5 p-3">
                    <p className="text-[11px] font-semibold uppercase tracking-wider text-brand-500 mb-1">
                      AI Optimized Query (Zero Spill)
                    </p>
                    <pre className="font-mono text-xs text-ink overflow-x-auto whitespace-pre p-2 bg-white rounded border border-brand-500/20">
                      {optimizedSql}
                    </pre>
                  </div>
                </div>
              )}
            </div>
          )}

          <div className="mt-6 flex flex-wrap items-center gap-3">
            <Button variant="secondary" onClick={() => onReview(approval.opportunityId)}>
              Review Details
            </Button>
            {onReject && !approved && !rejected && (
              <button
                type="button"
                onClick={() => onReject(approval.id)}
                className="rounded-sm border border-panel-border bg-white px-4 py-2 text-xs font-medium text-ink hover:bg-canvas-raised transition-colors"
              >
                Reject
              </button>
            )}
            <Button
              onClick={() => onApprove(approval.id)}
              disabled={!allPassed || approving || approved || rejected}
            >
              {approved ? "Approved" : approving ? "Submitting..." : "Approve & Execute"}
            </Button>
          </div>
          {!allPassed && !approved && (
            <p className="mt-2 text-xs text-signal-high">
              All validation checks must pass before this can be approved.
            </p>
          )}
        </div>

        <ImpactCard
          savingsMonthly={approval.potentialSavings}
          risk={approval.risk}
          rollbackAvailable={approval.rollbackAvailable}
        />
      </div>
    </div>
  );
}
