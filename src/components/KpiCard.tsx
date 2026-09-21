import React from "react";
import type { LucideIcon } from "lucide-react";

interface KpiCardProps {
  label: string;
  value: string;
  helper?: string;
  icon: LucideIcon;
  tone?: "default" | "positive";
}

export default function KpiCard({ label, value, helper, icon: Icon, tone = "default" }: KpiCardProps) {
  return (
    <div className="surface p-5 flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <span className="label-eyebrow">{label}</span>
        <span className="flex h-8 w-8 items-center justify-center rounded-sm bg-canvas-raised border border-panel-border">
          <Icon size={16} className="text-brand-400" strokeWidth={1.75} />
        </span>
      </div>
      <div>
        <p
          className={`tabular text-2xl font-semibold ${
            tone === "positive" ? "text-brand-300" : "text-ink"
          }`}
        >
          {value}
        </p>
        {helper && <p className="mt-1 text-xs text-ink-muted">{helper}</p>}
      </div>
    </div>
  );
}
