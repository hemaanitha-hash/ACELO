import React, { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Menu, RefreshCw, Bell, Loader2 } from "lucide-react";
import { useMsal } from "@azure/msal-react";
import { useRunMonitor } from "./RunMonitor";
import { RUN_STATE_LABELS, formatTime } from "../services/runsApi";
import {
  getActiveContext,
  PLATFORM_LABELS,
  subscribe,
  type ActiveContext,
} from "../services/platformContext";

interface TopbarProps {
  pageName: string;
  onMenuClick: () => void;
  onRefresh?: () => void;
}

function useDismiss(open: boolean, close: () => void) {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) close();
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open, close]);
  return ref;
}

/** Header indicator: every run still executing on the backend, by ACELO Run ID. */
function RunIndicator() {
  const { activeRuns } = useRunMonitor();
  const [open, setOpen] = useState(false);
  const ref = useDismiss(open, () => setOpen(false));
  if (activeRuns.length === 0) return null;
  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 rounded-sm border border-[#D71920]/30 bg-[#D71920]/5 px-2.5 py-1.5 text-xs font-medium text-[#D71920]"
        aria-label={`${activeRuns.length} active run${activeRuns.length === 1 ? "" : "s"}`}
      >
        <Loader2 size={13} className="animate-spin" />
        {activeRuns.length} running
      </button>
      {open && (
        <div className="absolute right-0 z-40 mt-2 w-80 surface border border-panel-border bg-white p-2 shadow-lg">
          {activeRuns.map((run) => (
            <Link
              key={run.acelo_run_id}
              to={`/runs/${run.acelo_run_id}`}
              onClick={() => setOpen(false)}
              className="block rounded-sm px-2 py-2 hover:bg-panel-hover"
            >
              <p className="text-sm font-medium text-ink">
                {run.optimization} · {RUN_STATE_LABELS[run.status] ?? run.status}
              </p>
              <p className="text-xs text-ink-muted">{run.current_stage ?? "Waiting for platform status"}</p>
              <p className="truncate font-mono text-[11px] text-ink-faint">{run.acelo_run_id}</p>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}

function NotificationBell() {
  const {
    notifications,
    unread,
    markRead,
    markAllRead,
    browserNotifications,
    enableBrowserNotifications,
    disableBrowserNotifications,
  } = useRunMonitor();
  const [open, setOpen] = useState(false);
  const ref = useDismiss(open, () => setOpen(false));
  const navigate = useNavigate();

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen((v) => !v)}
        className="relative rounded-sm p-2 text-ink-muted hover:text-ink hover:bg-panel-hover transition-colors"
        aria-label={unread ? `Notifications (${unread} unread)` : "Notifications"}
      >
        <Bell size={16} />
        {unread > 0 && (
          <span className="absolute right-0.5 top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-[#D71920] px-1 text-[10px] font-semibold text-white">
            {unread > 9 ? "9+" : unread}
          </span>
        )}
      </button>
      {open && (
        <div className="absolute right-0 z-40 mt-2 w-80 surface border border-panel-border bg-white shadow-lg">
          <div className="flex items-center justify-between border-b border-panel-border px-3 py-2">
            <p className="text-sm font-semibold text-ink">Notifications</p>
            {unread > 0 && (
              <button onClick={() => void markAllRead()} className="text-xs text-[#D71920] hover:underline">
                Mark all read
              </button>
            )}
          </div>
          <div className="max-h-80 overflow-y-auto">
            {notifications.length === 0 ? (
              <p className="px-3 py-6 text-center text-xs text-ink-muted">No notifications yet.</p>
            ) : (
              notifications.map((n) => (
                <button
                  key={n.id}
                  onClick={() => {
                    void markRead(n.id);
                    setOpen(false);
                    if (n.link) navigate(n.link);
                  }}
                  className={`block w-full px-3 py-2 text-left hover:bg-panel-hover ${n.read ? "" : "bg-[#D71920]/5"}`}
                >
                  <p className="text-sm font-medium text-ink">{n.title}</p>
                  {n.body && <p className="text-xs text-ink-muted">{n.body}</p>}
                  <p className="text-[11px] text-ink-faint">{formatTime(n.created_at)}</p>
                </button>
              ))
            )}
          </div>
          {browserNotifications !== "unsupported" && (
            <div className="border-t border-panel-border px-3 py-2 text-xs text-ink-muted">
              {browserNotifications === "granted" ? (
                <button onClick={disableBrowserNotifications} className="hover:underline">
                  Turn off browser notifications
                </button>
              ) : browserNotifications === "denied" ? (
                <span>Browser notifications are blocked in your browser settings.</span>
              ) : (
                <button onClick={() => void enableBrowserNotifications()} className="text-[#D71920] hover:underline">
                  Enable browser notifications
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** Initials of the signed-in Microsoft account; nothing when signed out (never a placeholder user). */
function useInitials(): string | null {
  const { instance } = useMsal();
  let name = "";
  try {
    const account = instance.getActiveAccount() ?? instance.getAllAccounts()[0];
    name = account?.name || account?.username || "";
  } catch {
    return null; // MSAL not initialised (e.g. service-principal only)
  }
  const parts = name.replace(/@.*/, "").split(/[\s._-]+/).filter(Boolean);
  if (!parts.length) return null;
  return (parts[0][0] + (parts.length > 1 ? parts[parts.length - 1][0] : "")).toUpperCase();
}

export default function Topbar({ pageName, onMenuClick, onRefresh }: TopbarProps) {
  // Re-render when the user switches platform so the breadcrumb follows.
  const [context, setContext] = useState<ActiveContext>(getActiveContext());
  useEffect(() => subscribe(setContext), []);
  const platform = context.platform;

  const initials = useInitials();
  return (
    <header className="flex h-16 items-center justify-between border-b border-panel-border bg-canvas/95 backdrop-blur px-4 sm:px-6">
      <div className="flex items-center gap-3 min-w-0">
        <button
          onClick={onMenuClick}
          className="rounded-sm p-1.5 text-ink-muted hover:text-ink hover:bg-panel-hover lg:hidden"
          aria-label="Open navigation"
        >
          <Menu size={18} />
        </button>
        {/* ACELO / <active platform> / <page> — the breadcrumb states which
            platform's data the page is showing. */}
        <p className="truncate text-sm text-ink-muted">
          <span className="text-ink-faint">ACELO</span>
          <span className="mx-1.5 text-ink-faint">/</span>
          {platform && (
            <>
              <span data-testid="breadcrumb-platform" className="text-ink-faint">
                {PLATFORM_LABELS[platform]}
              </span>
              <span className="mx-1.5 text-ink-faint">/</span>
            </>
          )}
          <span className="font-medium text-ink">{pageName}</span>
        </p>
      </div>
      <div className="flex items-center gap-1">
        <RunIndicator />
        <button
          onClick={onRefresh}
          className="rounded-sm p-2 text-ink-muted hover:text-ink hover:bg-panel-hover transition-colors"
          aria-label="Refresh"
        >
          <RefreshCw size={16} />
        </button>
        <NotificationBell />
        {initials && (
          <span
            className="ml-2 flex h-8 w-8 items-center justify-center rounded-full bg-brand-500/15 border border-brand-500/25 text-xs font-semibold text-brand-300"
            aria-label="Signed-in account"
          >
            {initials}
          </span>
        )}
      </div>
    </header>
  );
}
