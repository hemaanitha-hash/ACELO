import React from "react";
import { Check, Loader2, Circle } from "lucide-react";
import type { AgentStep } from "../types";

interface AgentTimelineProps {
  steps: AgentStep[];
}

export default function AgentTimeline({ steps }: AgentTimelineProps) {
  return (
    <ol className="space-y-0">
      {steps.map((step, index) => {
        const isLast = index === steps.length - 1;
        return (
          <li key={step.id} className="flex gap-3">
            <div className="flex flex-col items-center">
              <span
                className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-full border ${
                  step.status === "done"
                    ? "bg-brand-500/15 border-brand-500/40 text-brand-300"
                    : step.status === "active"
                    ? "bg-signal-info/15 border-signal-info/40 text-signal-info"
                    : "bg-canvas-raised border-panel-border text-ink-faint"
                }`}
              >
                {step.status === "done" && <Check size={13} />}
                {step.status === "active" && <Loader2 size={13} className="animate-spin" />}
                {step.status === "pending" && <Circle size={7} className="fill-current" />}
              </span>
              {!isLast && (
                <span
                  className={`w-px flex-1 ${
                    step.status === "done" ? "bg-brand-500/40" : "bg-panel-border"
                  }`}
                  style={{ minHeight: "20px" }}
                />
              )}
            </div>
            <div className="pb-5">
              <p
                className={`text-sm ${
                  step.status === "pending" ? "text-ink-faint" : "text-ink"
                }`}
              >
                {step.label}
              </p>
            </div>
          </li>
        );
      })}
    </ol>
  );
}
