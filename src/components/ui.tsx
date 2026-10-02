import React, { useEffect, useRef } from "react";
import { Check, ChevronRight, type LucideIcon } from "lucide-react";
import { NavLink } from "react-router-dom";
import StateBlock from "./StateBlock";
import { LIFECYCLE_LABELS, type Lifecycle } from "../services/stage1Api";

/**
 * The ACELO design system's shared building blocks. Pages compose these instead
 * of hand-rolling colours, radii and spacing, so every screen reads as one
 * product. All colours come from the Tailwind tokens (brand / signal / ink /
 * panel / canvas) — never raw palette values.
 */

// --- StatusPill -------------------------------------------------------------

export type PillTone = "brand" | "success" | "warning" | "danger" | "info" | "neutral";

const PILL_TONES: Record<PillTone, string> = {
  brand: "bg-brand-50 text-brand-700 border-brand-100",
  success: "bg-signal-low/10 text-signal-low border-signal-low/25",
  warning: "bg-signal-medium/10 text-signal-medium border-signal-medium/25",
  danger: "bg-signal-high/10 text-signal-high border-signal-high/25",
  info: "bg-signal-info/10 text-signal-info border-signal-info/25",
  neutral: "bg-canvas-raised text-ink-muted border-panel-border",
};

export function StatusPill({
  tone = "neutral",
  children,
  className = "",
}: {
  tone?: PillTone;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border px-2.5 py-0.5 text-xs font-medium ${PILL_TONES[tone]} ${className}`}
    >
      <span className="h-1.5 w-1.5 rounded-full bg-current" aria-hidden="true" />
      {children}
    </span>
  );
}

const LIFECYCLE_TONES: Record<Lifecycle, PillTone> = {
  OPEN: "neutral",
  PENDING: "warning",
  REJECTED: "danger",
  READY_TO_EXECUTE: "brand",
  EXECUTING: "info",
  EXECUTED: "success",
  EXECUTION_FAILED: "danger",
};

export function LifecyclePill({ lifecycle }: { lifecycle: Lifecycle }) {
  return <StatusPill tone={LIFECYCLE_TONES[lifecycle]}>{LIFECYCLE_LABELS[lifecycle]}</StatusPill>;
}

export function severityTone(value: string | null | undefined): PillTone {
  const v = (value ?? "").toUpperCase();
  if (v === "HIGH") return "danger";
  if (v === "MEDIUM") return "warning";
  if (v === "LOW") return "success";
  return "neutral";
}

export function titleCase(value: string | null | undefined): string {
  const v = (value ?? "").trim();
  return v ? v.charAt(0).toUpperCase() + v.slice(1).toLowerCase() : "";
}

// --- MetricCard -------------------------------------------------------------

export function MetricCard({
  label,
  value,
  helper,
  icon: Icon,
  emphasis = false,
  to,
  testId,
}: {
  label: string;
  /** Already formatted; pass "—" while loading and a word, never a fake 0, when unknown. */
  value: React.ReactNode;
  helper?: React.ReactNode;
  icon?: LucideIcon;
  emphasis?: boolean;
  to?: string;
  testId?: string;
}) {
  const body = (
    <>
      <div className="flex items-center justify-between gap-3">
        <span className="text-xs font-medium text-ink-muted">{label}</span>
        {Icon && (
          <span
            className={`flex h-8 w-8 items-center justify-center rounded-sm border ${
              emphasis ? "border-brand-100 bg-brand-50 text-brand-500" : "border-panel-border bg-canvas-raised text-ink-muted"
            }`}
          >
            <Icon size={15} strokeWidth={1.9} aria-hidden="true" />
          </span>
        )}
      </div>
      <p className={`tabular mt-3 text-2xl font-semibold ${emphasis ? "text-brand-600" : "text-ink"}`}>{value}</p>
      {helper && <p className="mt-1 text-xs text-ink-faint">{helper}</p>}
    </>
  );
  const className = "card block p-5";
  return to ? (
    <NavLink to={to} data-testid={testId} className={`${className} card-interactive`}>
      {body}
    </NavLink>
  ) : (
    <div data-testid={testId} className={className}>
      {body}
    </div>
  );
}

// --- SectionCard ------------------------------------------------------------

export function SectionCard({
  title,
  description,
  action,
  children,
  flush = false,
  className = "",
}: {
  title?: React.ReactNode;
  description?: React.ReactNode;
  action?: React.ReactNode;
  children?: React.ReactNode;
  /** No inner padding, for tables and lists that run edge to edge. */
  flush?: boolean;
  className?: string;
}) {
  return (
    <section className={`card overflow-hidden ${className}`}>
      {(title || action) && (
        <header className="flex flex-wrap items-start justify-between gap-3 border-b border-panel-border px-5 py-4">
          <div className="min-w-0">
            {title && <h2 className="text-sm font-semibold text-ink">{title}</h2>}
            {description && <p className="mt-1 text-xs text-ink-muted">{description}</p>}
          </div>
          {action && <div className="shrink-0">{action}</div>}
        </header>
      )}
      <div className={flush ? "" : "p-5"}>{children}</div>
    </section>
  );
}

// --- State panels -----------------------------------------------------------

/** Secondary, collapsed technical detail — never the primary message. */
export function TechnicalDetails({ children }: { children: React.ReactNode }) {
  return (
    <details className="mt-3 w-full max-w-xl text-left">
      <summary className="cursor-pointer text-xs font-medium text-ink-muted hover:text-ink">
        Technical details
      </summary>
      <div className="mt-2 break-words rounded-sm border border-panel-border bg-canvas-raised px-3 py-2 font-mono text-[11px] leading-5 text-ink-muted">
        {children}
      </div>
    </details>
  );
}

export function LoadingState({ title, detail }: { title: string; detail?: React.ReactNode }) {
  return (
    <div className="card">
      <StateBlock kind="loading" title={title} detail={detail} />
    </div>
  );
}

export function EmptyState({
  title,
  detail,
  action,
}: {
  title: string;
  detail?: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <div className="card">
      <StateBlock kind="empty" title={title} detail={detail} action={action} />
    </div>
  );
}

export function ErrorState({
  title,
  detail,
  technical,
  action,
}: {
  title: string;
  detail?: React.ReactNode;
  technical?: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <div className="card border-signal-high/30">
      <StateBlock
        kind="error"
        title={title}
        detail={
          detail || technical ? (
            <>
              {detail}
              {technical && <TechnicalDetails>{technical}</TechnicalDetails>}
            </>
          ) : undefined
        }
        action={action}
      />
    </div>
  );
}

/** A one-line banner for the result of an action on the page. */
export function Notice({
  tone,
  children,
  testId,
}: {
  tone: "success" | "danger" | "info";
  children: React.ReactNode;
  testId?: string;
}) {
  const cls =
    tone === "success"
      ? "border-signal-low/30 bg-signal-low/5 text-signal-low"
      : tone === "danger"
        ? "border-signal-high/30 bg-signal-high/5 text-signal-high"
        : "border-signal-info/30 bg-signal-info/5 text-signal-info";
  return (
    <div data-testid={testId} role={tone === "danger" ? "alert" : "status"} className={`rounded-sm border px-4 py-3 text-sm ${cls}`}>
      {children}
    </div>
  );
}

// --- WorkflowIndicator ------------------------------------------------------

export type WorkflowStep = "analyze" | "recommendations" | "approval" | "execution" | "history";

const WORKFLOW: { id: WorkflowStep; label: string; to: string }[] = [
  { id: "analyze", label: "Analyze", to: "/compute" },
  { id: "recommendations", label: "Recommendations", to: "/recommendations" },
  { id: "approval", label: "Approval", to: "/approvals" },
  { id: "execution", label: "Execution", to: "/execution" },
  { id: "history", label: "History", to: "/history" },
];

/**
 * The ACELO lifecycle, with the page's own stage highlighted. Each stage links
 * to the page where that stage happens, so it doubles as orientation and
 * navigation.
 */
export function WorkflowIndicator({ current }: { current: WorkflowStep }) {
  const index = WORKFLOW.findIndex((s) => s.id === current);
  return (
    <nav aria-label="ACELO lifecycle" className="overflow-x-auto">
      <ol className="flex min-w-max items-center gap-1 text-xs">
        {WORKFLOW.map((step, i) => {
          const state = i < index ? "done" : i === index ? "current" : "upcoming";
          return (
            <li key={step.id} className="flex items-center gap-1">
              <NavLink
                to={step.to}
                aria-current={state === "current" ? "step" : undefined}
                className={`flex items-center gap-1.5 rounded-full border px-3 py-1 font-medium transition-colors ${
                  state === "current"
                    ? "border-brand-500 bg-brand-500 text-white"
                    : state === "done"
                      ? "border-brand-100 bg-brand-50 text-brand-700 hover:border-brand-300"
                      : "border-panel-border bg-panel text-ink-muted hover:text-ink"
                }`}
              >
                {state === "done" ? (
                  <Check size={12} aria-hidden="true" />
                ) : (
                  <span className="tabular" aria-hidden="true">{i + 1}</span>
                )}
                {step.label}
              </NavLink>
              {i < WORKFLOW.length - 1 && <ChevronRight size={14} className="text-ink-faint" aria-hidden="true" />}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

// --- ConfirmationDialog -----------------------------------------------------

export function ConfirmationDialog({
  title,
  children,
  confirmLabel,
  confirmTone = "primary",
  busy = false,
  confirmDisabled = false,
  onConfirm,
  onCancel,
}: {
  title: string;
  children: React.ReactNode;
  confirmLabel: string;
  confirmTone?: "primary" | "danger";
  busy?: boolean;
  confirmDisabled?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const cancelRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    cancelRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) onCancel();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [busy, onCancel]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 px-4" role="presentation">
      <div role="dialog" aria-modal="true" aria-labelledby="confirm-title" className="card w-full max-w-lg p-6 shadow-elevated">
        <h2 id="confirm-title" className="text-base font-semibold text-ink">
          {title}
        </h2>
        <div className="mt-3 text-sm text-ink-muted">{children}</div>
        <div className="mt-6 flex justify-end gap-2">
          <button ref={cancelRef} type="button" className="btn-secondary" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className={confirmTone === "danger" ? "btn-danger" : "btn-primary"}
            onClick={onConfirm}
            disabled={busy || confirmDisabled}
          >
            {busy ? "Saving…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

// --- Key/value evidence -----------------------------------------------------

function formatScalar(value: unknown): string {
  if (value === null || value === undefined || value === "") return "Not available";
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toFixed(2);
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

export function humanizeKey(key: string): string {
  return key.replace(/[_.]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/** A flat object as a compact definition list; nested values stay as text. */
export function KeyValueGrid({ data, empty = "Not available" }: { data: unknown; empty?: string }) {
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    return <p className="text-xs text-ink-muted">{data ? formatScalar(data) : empty}</p>;
  }
  const entries = Object.entries(data as Record<string, unknown>);
  if (!entries.length) return <p className="text-xs text-ink-muted">{empty}</p>;
  return (
    <dl className="grid gap-x-6 gap-y-2 sm:grid-cols-2">
      {entries.map(([key, value]) => (
        <div key={key} className="min-w-0">
          <dt className="text-[11px] text-ink-faint">{humanizeKey(key)}</dt>
          <dd className="break-words font-mono text-xs text-ink">{formatScalar(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "Not available";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}
