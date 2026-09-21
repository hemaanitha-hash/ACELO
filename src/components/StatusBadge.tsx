import React from "react";

type Tone = "high" | "medium" | "low" | "info" | "neutral" | "success";

const toneClass: Record<Tone, string> = {
  high: "bg-signal-high/10 text-signal-high border-signal-high/25",
  medium: "bg-signal-medium/10 text-signal-medium border-signal-medium/25",
  low: "bg-signal-low/10 text-signal-low border-signal-low/25",
  success: "bg-brand-500/10 text-brand-300 border-brand-500/25",
  info: "bg-signal-info/10 text-signal-info border-signal-info/25",
  neutral: "bg-ink-faint/10 text-ink-muted border-panel-borderStrong",
};

function toneForSeverity(value: string): Tone {
  switch (value) {
    case "High":
      return "high";
    case "Medium":
      return "medium";
    case "Low":
      return "low";
    default:
      return "neutral";
  }
}

function toneForStatus(value: string): Tone {
  switch (value) {
    case "Review":
      return "medium";
    case "Approved":
      return "info";
    case "In Progress":
      return "info";
    case "Completed":
      return "success";
    case "Rejected":
      return "high";
    case "Pending":
      return "medium";
    case "Failed":
      return "high";
    case "Healthy":
      return "success";
    case "Attention":
      return "medium";
    case "Critical":
      return "high";
    default:
      return "neutral";
  }
}

interface StatusBadgeProps {
  label: string;
  kind?: "severity" | "status" | "auto";
  className?: string;
}

export default function StatusBadge({ label, kind = "auto", className = "" }: StatusBadgeProps) {
  const tone =
    kind === "severity" ? toneForSeverity(label) : toneForStatus(label);

  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-sm border px-2 py-0.5 text-xs font-medium ${toneClass[tone]} ${className}`}
    >
      <span className="h-1.5 w-1.5 rounded-full bg-current" />
      {label}
    </span>
  );
}
