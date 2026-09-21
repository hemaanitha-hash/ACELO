import React from "react";

interface RecommendationCardProps {
  eyebrow: string;
  title: string;
  children: React.ReactNode;
}

export default function RecommendationCard({ eyebrow, title, children }: RecommendationCardProps) {
  return (
    <div className="surface p-6">
      <p className="label-eyebrow">{eyebrow}</p>
      <h3 className="mt-1.5 text-sm font-semibold text-ink">{title}</h3>
      <p className="mt-3 text-sm leading-relaxed text-ink-muted">{children}</p>
    </div>
  );
}
