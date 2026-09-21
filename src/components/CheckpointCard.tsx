import React from "react";
import { ShieldCheck } from "lucide-react";

export default function CheckpointCard() {
  return (
    <div className="surface p-5 flex items-start gap-3">
      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-sm bg-brand-500/15 border border-brand-500/25">
        <ShieldCheck size={16} className="text-brand-300" />
      </span>
      <div>
        <div className="flex items-center gap-2">
          <p className="text-sm font-medium text-ink">Checkpoint protected</p>
          <span className="rounded-sm border border-brand-500/25 bg-brand-500/10 px-1.5 py-0.5 text-[11px] font-medium text-brand-300">
            Rollback Available
          </span>
        </div>
        <p className="mt-1 text-sm text-ink-muted">
          Pre-optimization state is available for rollback.
        </p>
      </div>
    </div>
  );
}
