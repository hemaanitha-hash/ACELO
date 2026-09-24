import React from "react";

/**
 * The standard page header: title, one line of purpose, and an optional action.
 *
 * Thirteen pages each hand-rolled this block, which is why titles, description
 * spacing and action placement had drifted apart. One component keeps the
 * hierarchy identical everywhere, so a user learns the layout once.
 */
export default function PageHeader({
  title,
  description,
  action,
  eyebrow,
}: {
  title: React.ReactNode;
  /** One sentence saying what this page is for — answers "where am I?". */
  description?: React.ReactNode;
  /** The page's primary action, right-aligned on wide screens. */
  action?: React.ReactNode;
  /** Small label above the title, e.g. the platform or parent section. */
  eyebrow?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
      <div className="min-w-0">
        {eyebrow && <p className="label-eyebrow mb-1.5">{eyebrow}</p>}
        <h1 className="text-display font-semibold text-ink">{title}</h1>
        {description && (
          <p className="mt-2 max-w-2xl text-sm text-ink-muted">{description}</p>
        )}
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}
