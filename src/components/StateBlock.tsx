import React from "react";
import { AlertCircle, AlertTriangle, CheckCircle2, Inbox, Loader2, Lock } from "lucide-react";

/**
 * The one block every page uses to say "there is nothing to show, and here is
 * why". Ten pages previously hand-rolled `surface py-16 text-center`, each with
 * slightly different wording and no way to tell these situations apart:
 *
 *   loading      — we are still asking
 *   empty        — we asked, the answer was genuinely zero
 *   unavailable  — we were not permitted to ask
 *   unsupported  — the platform offers no way to ask
 *   error        — the call failed
 *
 * Collapsing those into one grey "None found" is the specific confusion this
 * component exists to prevent: an unreadable resource must never look like an
 * absent one.
 */

export type StateKind = "loading" | "empty" | "unavailable" | "unsupported" | "error" | "success";

const PRESENTATION: Record<
  StateKind,
  { icon: typeof Inbox; tone: string; defaultTitle: string }
> = {
  loading: { icon: Loader2, tone: "text-ink-muted", defaultTitle: "Loading…" },
  empty: { icon: Inbox, tone: "text-ink-faint", defaultTitle: "Nothing here yet" },
  unavailable: { icon: Lock, tone: "text-signal-medium", defaultTitle: "Not available" },
  unsupported: { icon: AlertTriangle, tone: "text-ink-muted", defaultTitle: "Not supported" },
  error: { icon: AlertCircle, tone: "text-signal-high", defaultTitle: "Something went wrong" },
  success: { icon: CheckCircle2, tone: "text-signal-low", defaultTitle: "Done" },
};

export default function StateBlock({
  kind,
  title,
  detail,
  action,
  compact = false,
  className = "",
}: {
  kind: StateKind;
  /** Short statement of the situation. Falls back to a sensible default. */
  title?: React.ReactNode;
  /** Why it happened, and what the user can do about it. */
  detail?: React.ReactNode;
  /** The one action that resolves this state, when there is one. */
  action?: React.ReactNode;
  /** Inline variant for use inside an existing card. */
  compact?: boolean;
  className?: string;
}) {
  const { icon: Icon, tone, defaultTitle } = PRESENTATION[kind];

  return (
    <div
      role={kind === "error" ? "alert" : undefined}
      aria-busy={kind === "loading" || undefined}
      className={`flex flex-col items-center justify-center text-center ${
        compact ? "px-4 py-8" : "px-6 py-14"
      } ${className}`}
    >
      <Icon
        size={compact ? 16 : 20}
        className={`${tone} ${kind === "loading" ? "animate-spin" : ""}`}
        aria-hidden="true"
      />
      <p className="mt-3 text-sm font-medium text-ink">{title ?? defaultTitle}</p>
      {detail && (
        <p className="mt-1.5 max-w-md text-sm text-ink-muted">{detail}</p>
      )}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

/**
 * The same block wrapped in a card, which is how a page-level empty state is
 * shown. Kept separate so a StateBlock can also sit inside an existing card
 * without nesting two borders.
 */
export function StatePanel(props: React.ComponentProps<typeof StateBlock>) {
  return (
    <div className="surface">
      <StateBlock {...props} />
    </div>
  );
}
