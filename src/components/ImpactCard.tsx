import React from "react";
import { TrendingDown, ShieldAlert, RotateCcw } from "lucide-react";

interface ImpactCardProps {
  savingsMonthly: number | null;
  risk: "Low" | "Medium" | "High";
  rollbackAvailable: boolean;
}

const riskColor: Record<ImpactCardProps["risk"], string> = {
  Low: "text-brand-300",
  Medium: "text-signal-medium",
  High: "text-signal-high",
};

export default function ImpactCard({ savingsMonthly, risk, rollbackAvailable }: ImpactCardProps) {
  return (
    <div className="surface p-5">
      <p className="label-eyebrow">Potential savings</p>
      <div className="mt-2 flex items-center gap-2">
        <TrendingDown size={18} className="text-brand-400" />
        <p className="tabular text-2xl font-semibold text-brand-300">
          {savingsMonthly === null ? "Not available" : `$${savingsMonthly.toFixed(2)} / month`}
        </p>
      </div>

      <div className="mt-5 grid grid-cols-2 gap-4 border-t border-panel-border pt-5">
        <div>
          <div className="flex items-center gap-1.5 text-xs text-ink-faint">
            <ShieldAlert size={13} />
            Risk
          </div>
          <p className={`mt-1 text-sm font-medium ${riskColor[risk]}`}>{risk}</p>
        </div>
        <div>
          <div className="flex items-center gap-1.5 text-xs text-ink-faint">
            <RotateCcw size={13} />
            Rollback
          </div>
          <p className="mt-1 text-sm font-medium text-ink">
            {rollbackAvailable ? "Available" : "Not available"}
          </p>
        </div>
      </div>
    </div>
  );
}
