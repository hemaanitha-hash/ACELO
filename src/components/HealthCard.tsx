import React from "react";
import type { DomainHealth } from "../types";

const barColor: Record<DomainHealth["label"], string> = {
  "No data": "bg-ink-faint",
  Healthy: "bg-brand-500",
  Attention: "bg-signal-medium",
  Critical: "bg-signal-high",
};

const textColor: Record<DomainHealth["label"], string> = {
  "No data": "text-ink-faint",
  Healthy: "text-brand-300",
  Attention: "text-signal-medium",
  Critical: "text-signal-high",
};

export default function HealthCard({ domain, score, label }: DomainHealth) {
  return (
    <div className="surface p-5">
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium text-ink">{domain}</span>
        <span className={`text-xs font-medium ${textColor[label]}`}>{label}</span>
      </div>
      <div className="mt-4 flex items-end justify-between">
        <span className="tabular text-xl font-semibold text-ink">{score == null ? "—" : `${score}%`}</span>
      </div>
      <div className="mt-3 h-1.5 w-full rounded-full bg-canvas-raised overflow-hidden">
        <div
          className={`h-full rounded-full ${barColor[label]}`}
          style={{ width: `${score ?? 0}%` }}
        />
      </div>
    </div>
  );
}
