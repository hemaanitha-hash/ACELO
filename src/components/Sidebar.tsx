import React, { useEffect, useState } from "react";
import { NavLink } from "react-router-dom";
import {
  getActiveContext,
  PLATFORM_LABELS,
  resolveInitial,
  setActiveContext,
  subscribe,
  type ActiveContext,
  type ActivePlatformId,
  type PlatformConnection,
} from "../services/platformContext";
import { isDatabricksOnly } from "../services/experience";
import { getRuntime } from "../services/runtime";
import { listPlatformConnections } from "../services/platformConnections";
import {
  LayoutGrid,
  Sparkles,
  SlidersHorizontal,
  FileText,
  ShieldCheck,
  PlayCircle,
  History,
  Settings,
  Database,
  ChevronDown,
  Check,
  X,
} from "lucide-react";

interface NavItem {
  to: string;
  label: string;
  icon: typeof Database;
  end?: boolean;
  badge?: number;
}

/**
 * Navigation shared by every platform. The pages are the same; what differs is
 * the data behind them, which the active platform context decides.
 */
const sharedNavItems: NavItem[] = [
  { to: "/", label: "Overview", icon: LayoutGrid, end: true },
  { to: "/agent", label: "AI Agent", icon: Sparkles },
  { to: "/optimizations", label: "Optimizations", icon: SlidersHorizontal },
  { to: "/recommendations", label: "Recommendations", icon: FileText },
  // No badge: the count here was hardcoded to 3, so the sidebar claimed three
  // pending approvals in every workspace forever. A real count needs a real
  // request; an invented one is worse than none.
  { to: "/approvals", label: "Approvals", icon: ShieldCheck },
  { to: "/execution", label: "Execution", icon: PlayCircle },
  { to: "/history", label: "Run History", icon: History },
];

/**
 * Platform-specific entries. Only the ACTIVE platform's item is rendered — the
 * Databricks discovery view is never reachable while Fabric is active, and the
 * route itself refuses to load out of context (see App.tsx).
 */
/**
 * The MVP journey, in order:
 *   Overview -> AI Agent -> Compute Optimization -> Recommendations
 *
 * Approvals, Execution and Run History remain implemented and routable; they
 * are simply not part of the primary Databricks compute-optimization journey,
 * so they are kept out of the main navigation rather than deleted.
 */
const mvpNavItems: NavItem[] = [
  { to: "/", label: "Overview", icon: LayoutGrid, end: true },
  { to: "/agent", label: "AI Agent", icon: Sparkles },
  { to: "/compute", label: "Compute Optimization", icon: Database },
  { to: "/recommendations", label: "Recommendations", icon: FileText },
];

const platformNavItems: Record<ActivePlatformId, NavItem[]> = {
  // Databricks has its own discovery page. Fabric's resources are listed inside
  // Environment Setup, which already has its own entry below — a second link to
  // the same page read as a separate feature that did not exist.
  databricks: [{ to: "/databricks", label: "Compute discovery", icon: Database }],
  fabric: [],
};

interface SidebarProps {
  open: boolean;
  onClose: () => void;
}

export default function Sidebar({ open, onClose }: SidebarProps) {
  const databricksOnly = isDatabricksOnly();
  const [platformOpen, setPlatformOpen] = useState(false);
  const [connections, setConnections] = useState<PlatformConnection[]>([]);
  const [context, setContext] = useState<ActiveContext>(getActiveContext());

  // The switcher lists the customer's REAL connections, not a hardcoded array.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const loaded = await listPlatformConnections();
        if (cancelled) return;
        setConnections(loaded);
        // Adopt a selection only if nothing is active yet, so switching is
        // never undone by a later refresh.
        if (!getActiveContext().connection) setActiveContext(resolveInitial(loaded));
      } catch {
        /* the switcher stays empty; pages surface their own errors */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => subscribe(setContext), []);

  const activePlatform = context.platform;
  const activeConnection = context.connection;
  const navItems: NavItem[] = databricksOnly
    ? mvpNavItems
    : [...sharedNavItems, ...(activePlatform ? platformNavItems[activePlatform] : [])];

  return (
    <>
      {open && (
        <div
          className="fixed inset-0 z-30 bg-black/50 lg:hidden"
          onClick={onClose}
          aria-hidden="true"
        />
      )}

      <aside
        className={`fixed inset-y-0 left-0 z-40 flex w-64 flex-col border-r border-panel-border bg-canvas-raised transition-transform duration-200 lg:static lg:translate-x-0 ${
          open ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-5">
          <div className="flex items-center gap-2.5">
            <span className="flex h-8 w-8 items-center justify-center rounded-sm bg-brand-500/15 border border-brand-500/25">
              <svg viewBox="0 0 32 32" className="h-4 w-4">
                <path
                  d="M16 6 L25 24 H20.5 L16 14.5 L11.5 24 H7 Z"
                  fill="#C41E3A"
                />
              </svg>
            </span>

            <span className="text-[15px] font-semibold tracking-tight text-ink">
              ACELO
            </span>
          </div>

          <button
            onClick={onClose}
            className="rounded-sm p-1 text-ink-faint hover:text-ink lg:hidden"
            aria-label="Close navigation"
          >
            <X size={18} />
          </button>
        </div>

        {/* Databricks-only MVP: the workspace comes from the Databricks App
            runtime, so there is nothing to select. The switcher below exists
            only for the legacy multi-platform experience. */}
        {databricksOnly && (
          <div className="mx-3 mb-4 rounded-sm border border-panel-border bg-panel px-3 py-2.5">
            <div className="flex items-center gap-2.5">
              <Database size={15} className="shrink-0 text-ink-muted" />
              <div className="min-w-0">
                <span className="block truncate text-sm font-medium leading-tight text-ink">
                  Databricks
                </span>
                <span
                  data-testid="workspace-context"
                  className="mt-0.5 block truncate text-xs leading-tight text-ink-faint"
                >
                  {getRuntime().workspace_host?.replace("https://", "") ??
                    "Current Databricks workspace"}
                </span>
              </div>
            </div>
          </div>
        )}

        {/* Platform / Environment Selector */}
        {!databricksOnly && (
        <div className="relative mx-3 mb-4">
          <button
            onClick={() => setPlatformOpen((v) => !v)}
            aria-haspopup="listbox"
            aria-expanded={platformOpen}
            className="flex w-full items-center justify-between rounded-sm border border-panel-border bg-panel px-3 py-2.5 text-left transition-colors hover:bg-panel-hover"
          >
            <div className="flex min-w-0 items-center gap-2.5">
              <Database
                size={15}
                className="shrink-0 text-ink-muted"
              />

              <div className="min-w-0">
                <span className="block truncate text-sm font-medium leading-tight text-ink">
                  {activePlatform ? PLATFORM_LABELS[activePlatform] : "No platform"}
                </span>

                <span className="mt-0.5 block truncate text-xs leading-tight text-ink-faint">
                  {activeConnection?.name ?? "No connection"}
                </span>
              </div>
            </div>

            <ChevronDown
              size={14}
              className={`shrink-0 text-ink-faint transition-transform ${
                platformOpen ? "rotate-180" : ""
              }`}
            />
          </button>

          {platformOpen && (
            <ul
              role="listbox"
              className="absolute left-0 right-0 top-full z-50 mt-1 overflow-hidden rounded-sm border border-panel-border bg-panel shadow-elevated"
            >
              {connections.map((platform) => {
                const isActive = activeConnection?.id === platform.id;

                return (
                  <li key={platform.id}>
                    <button
                      role="option"
                      aria-selected={isActive}
                      onClick={() => {
                        // Switching platform changes the whole application
                        // context, not just this label.
                        setActiveContext(platform);
                        setPlatformOpen(false);
                      }}
                      className={`flex w-full items-center justify-between gap-2 px-3 py-3 text-left transition-colors ${
                        isActive
                          ? "bg-panel-hover"
                          : "hover:bg-panel-hover"
                      }`}
                    >
                      <span className="min-w-0">
                        <span
                          className={`block truncate text-sm ${
                            isActive
                              ? "font-medium text-ink"
                              : "text-ink"
                          }`}
                        >
                          {PLATFORM_LABELS[platform.platform]}
                        </span>

                        <span className="mt-0.5 block truncate text-xs text-ink-faint">
                          {platform.name}
                        </span>
                      </span>

                      {isActive && (
                        <Check
                          size={15}
                          className="shrink-0 text-brand-500"
                        />
                      )}
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        )}

        {/* Navigation */}
        <nav className="flex-1 space-y-0.5 px-3">
          {navItems.map(
            ({ to, label, icon: Icon, end, badge }) => (
              <NavLink
                key={to}
                to={to}
                end={end}
                onClick={onClose}
                className={({ isActive }) =>
                  `flex items-center justify-between rounded-sm px-3 py-2 text-sm transition-colors ${
                    isActive
                      ? "bg-brand-500/12 text-brand-300 border border-brand-500/20"
                      : "text-ink-muted hover:bg-panel-hover hover:text-ink border border-transparent"
                  }`
                }
              >
                <span className="flex items-center gap-2.5">
                  <Icon size={16} strokeWidth={1.75} />
                  {label}
                </span>

                {badge && (
                  <span className="flex h-5 min-w-5 items-center justify-center rounded-full bg-signal-medium px-1 text-[11px] font-semibold text-white">
                    {badge}
                  </span>
                )}
              </NavLink>
            )
          )}
        </nav>

        {/* Bottom Section */}
        <div className="mt-auto border-t border-panel-border px-3 py-4 space-y-0.5">
          <div className="flex items-center gap-2.5 rounded-sm px-3 py-2">
            <span className="h-1.5 w-1.5 rounded-full bg-brand-400" />

            <div className="min-w-0">
              <p className="text-[11px] text-ink-faint leading-none">
                {databricksOnly ? "Connection" : "Connected Platform"}
              </p>

              <p className="text-sm text-ink leading-tight mt-0.5">
                {databricksOnly
                  ? getRuntime().databricks_app
                    ? "Connected via Databricks App"
                    : "Databricks"
                  : activePlatform
                    ? PLATFORM_LABELS[activePlatform]
                    : "None"}
              </p>
            </div>
          </div>

          {/* Environment Setup exists only for the legacy multi-platform
              experience. A Databricks App runs inside the workspace and
              authenticates with its own identity, so there is no environment
              for the user to configure and no link to offer. */}
          {!databricksOnly && (
            <NavLink
              to="/settings"
              onClick={onClose}
              className={({ isActive }) =>
                `flex items-center gap-2.5 rounded-sm px-3 py-2 text-sm transition-colors ${
                  isActive
                    ? "text-ink bg-panel-hover"
                    : "text-ink-muted hover:bg-panel-hover hover:text-ink"
                }`
              }
            >
              <Settings size={16} strokeWidth={1.75} />
              Settings
            </NavLink>
          )}
        </div>
      </aside>
    </>
  );
}