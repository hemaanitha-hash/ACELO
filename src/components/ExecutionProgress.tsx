import React from "react";
import { Check } from "lucide-react";
import type { ExecutionStage } from "../types";

const stages: ExecutionStage[] = ["Approval", "Checkpoint", "Execution", "Validation"];

interface ExecutionProgressProps {
  currentStage: ExecutionStage;
  progressPercent: number;
  workloadsProcessed: number;
  workloadsTotal: number;
  elapsedSeconds: number;
}

function formatElapsed(seconds: number): string {
  const m = Math.floor(seconds / 60)
    .toString()
    .padStart(2, "0");
  const s = Math.floor(seconds % 60)
    .toString()
    .padStart(2, "0");
  return `${m}:${s}`;
}

export default function ExecutionProgress({
  currentStage,
  progressPercent,
  workloadsProcessed,
  workloadsTotal,
  elapsedSeconds,
}: ExecutionProgressProps) {
  const currentIndex = stages.indexOf(currentStage);

  return (
    <div className="surface p-6">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
        {stages.map((stage, index) => {
          const isDone = index < currentIndex || progressPercent >= 100;
          const isActive = index === currentIndex && progressPercent < 100;
          return (
            <div key={stage} className="flex items-center gap-2">
              <span
                className={`flex h-6 w-6 items-center justify-center rounded-full border text-xs ${
                  isDone
                    ? "bg-brand-500/15 border-brand-500/40 text-brand-300"
                    : isActive
                    ? "bg-signal-info/15 border-signal-info/40 text-signal-info"
                    : "bg-canvas-raised border-panel-border text-ink-faint"
                }`}
              >
                {isDone ? <Check size={12} /> : index + 1}
              </span>
              <span className={`text-sm ${isDone || isActive ? "text-ink" : "text-ink-faint"}`}>
                {stage}
              </span>
              {index < stages.length - 1 && (
                <span className="hidden h-px w-8 bg-panel-border sm:block" />
              )}
            </div>
          );
        })}
      </div>

      <div className="mt-6">
        <div className="flex items-center justify-between text-sm">
          <span className="text-ink-muted">Progress</span>
          <span className="tabular font-medium text-ink">{Math.round(progressPercent)}%</span>
        </div>
        <div className="mt-2 h-2 w-full overflow-hidden rounded-full bg-canvas-raised">
          <div
            className="h-full rounded-full bg-brand-500 transition-[width] duration-500 ease-out"
            style={{ width: `${progressPercent}%` }}
          />
        </div>
      </div>

      <div className="mt-5 grid grid-cols-2 gap-4 border-t border-panel-border pt-5 sm:grid-cols-2">
        <div>
          <p className="text-xs text-ink-faint">Workloads processed</p>
          <p className="tabular mt-1 text-sm font-medium text-ink">
            {workloadsProcessed.toLocaleString()} / {workloadsTotal.toLocaleString()}
          </p>
        </div>
        <div>
          <p className="text-xs text-ink-faint">Elapsed</p>
          <p className="tabular mt-1 text-sm font-medium text-ink">{formatElapsed(elapsedSeconds)}</p>
        </div>
      </div>
    </div>
  );
}
