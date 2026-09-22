import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMsal } from "@azure/msal-react";
import { CheckCircle2, XCircle, Ban, X } from "lucide-react";
import { getSilentRunTokens } from "../services/fabricAuth";
import {
  AppNotification,
  RunSummary,
  getActiveRuns,
  listNotifications,
  markAllNotificationsRead,
  markNotificationRead,
} from "../services/runsApi";

/**
 * Global run monitor, mounted once in the app shell.
 *
 * It tracks every active run BY ID (never a single "isRunning" flag) and the
 * backend's persisted notifications, so completion/failure notices appear on
 * whatever page the user is on — and come back after a refresh or re-login.
 * It never cancels or owns a run: leaving a page changes nothing on the backend.
 */

const ACTIVE_POLL_MS = 5000;
const IDLE_POLL_MS = 20000;
const TOAST_MS = 12000;
const BROWSER_OPT_IN_KEY = "acelo.browserNotifications";

export interface RunMonitorValue {
  activeRuns: RunSummary[];
  notifications: AppNotification[];
  unread: number;
  refresh: () => Promise<void>;
  markRead: (id: string) => Promise<void>;
  markAllRead: () => Promise<void>;
  browserNotifications: "unsupported" | "default" | "granted" | "denied" | "off";
  enableBrowserNotifications: () => Promise<void>;
  disableBrowserNotifications: () => void;
}

const noop = async () => undefined;
const RunMonitorContext = createContext<RunMonitorValue>({
  activeRuns: [],
  notifications: [],
  unread: 0,
  refresh: noop,
  markRead: noop,
  markAllRead: noop,
  browserNotifications: "unsupported",
  enableBrowserNotifications: noop,
  disableBrowserNotifications: () => undefined,
});

export function useRunMonitor(): RunMonitorValue {
  return useContext(RunMonitorContext);
}

function readOptIn(): boolean {
  try {
    return window.localStorage.getItem(BROWSER_OPT_IN_KEY) === "1";
  } catch {
    return false;
  }
}

function writeOptIn(on: boolean) {
  try {
    if (on) window.localStorage.setItem(BROWSER_OPT_IN_KEY, "1");
    else window.localStorage.removeItem(BROWSER_OPT_IN_KEY);
  } catch {
    /* storage unavailable: preference is simply not remembered */
  }
}

function browserState(optedIn: boolean): RunMonitorValue["browserNotifications"] {
  if (typeof window === "undefined" || !("Notification" in window)) return "unsupported";
  const permission = window.Notification.permission;
  if (permission === "granted") return optedIn ? "granted" : "off";
  return permission as "default" | "denied";
}

interface Toast {
  id: string;
  notification: AppNotification;
}

export function RunMonitorProvider({ children }: { children: React.ReactNode }) {
  const { instance } = useMsal();
  const navigate = useNavigate();
  const [activeRuns, setActiveRuns] = useState<RunSummary[]>([]);
  const [notifications, setNotifications] = useState<AppNotification[]>([]);
  const [unread, setUnread] = useState(0);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [optedIn, setOptedIn] = useState<boolean>(readOptIn);
  const seen = useRef<Set<string> | null>(null);
  const timer = useRef<number | null>(null);
  const activeCount = useRef(0);
  const optedInRef = useRef(optedIn);
  optedInRef.current = optedIn;

  const announce = useCallback((fresh: AppNotification[]) => {
    if (fresh.length === 0) return;
    setToasts((current) => [...fresh.map((n) => ({ id: n.id, notification: n })), ...current].slice(0, 4));
    fresh.forEach((n) => {
      window.setTimeout(() => setToasts((cur) => cur.filter((t) => t.id !== n.id)), TOAST_MS);
      if (optedInRef.current && "Notification" in window && window.Notification.permission === "granted") {
        try {
          new window.Notification(n.title, { body: n.body ?? undefined, tag: n.id });
        } catch {
          /* some browsers only allow notifications from a service worker */
        }
      }
    });
  }, []);

  const refresh = useCallback(async () => {
    try {
      // Silent tokens only: lets the backend advance delegated runs whose
      // submission token expired. Never prompts.
      const tokens = await getSilentRunTokens(instance).catch(() => ({ fabric: null, onelake: null }));
      const active = await getActiveRuns(tokens);
      setActiveRuns(active.runs);
      activeCount.current = active.count;
    } catch {
      /* backend unreachable: keep the last known state; next tick retries */
    }
    try {
      const body = await listNotifications();
      setNotifications(body.notifications);
      setUnread(body.unread);
      if (seen.current === null) {
        // First load (page refresh / re-login): existing notices go to the bell,
        // not a burst of toasts.
        seen.current = new Set(body.notifications.map((n) => n.id));
      } else {
        const fresh = body.notifications.filter((n) => !n.read && !seen.current!.has(n.id));
        fresh.forEach((n) => seen.current!.add(n.id));
        announce(fresh);
      }
    } catch {
      /* next tick retries */
    }
  }, [instance, announce]);

  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      await refresh();
      if (cancelled) return;
      timer.current = window.setTimeout(tick, activeCount.current > 0 ? ACTIVE_POLL_MS : IDLE_POLL_MS);
    };
    void tick();
    return () => {
      cancelled = true;
      if (timer.current) window.clearTimeout(timer.current);
    };
  }, [refresh]);

  const markRead = useCallback(async (id: string) => {
    await markNotificationRead(id).catch(() => undefined);
    setNotifications((list) => list.map((n) => (n.id === id ? { ...n, read: true } : n)));
    setUnread((u) => Math.max(0, u - 1));
  }, []);

  const markAllRead = useCallback(async () => {
    await markAllNotificationsRead().catch(() => undefined);
    setNotifications((list) => list.map((n) => ({ ...n, read: true })));
    setUnread(0);
  }, []);

  // Permission is requested ONLY from this explicit user action, never on load.
  const enableBrowserNotifications = useCallback(async () => {
    if (!("Notification" in window)) return;
    const permission =
      window.Notification.permission === "default"
        ? await window.Notification.requestPermission()
        : window.Notification.permission;
    const on = permission === "granted";
    writeOptIn(on);
    setOptedIn(on);
  }, []);

  const disableBrowserNotifications = useCallback(() => {
    writeOptIn(false);
    setOptedIn(false);
  }, []);

  function open(toast: Toast, section?: "results") {
    const runId = toast.notification.acelo_run_id;
    setToasts((cur) => cur.filter((t) => t.id !== toast.id));
    void markRead(toast.notification.id);
    if (runId) navigate(`/runs/${runId}${section ? "#results" : ""}`);
    else if (toast.notification.link) navigate(toast.notification.link);
  }

  const value: RunMonitorValue = {
    activeRuns,
    notifications,
    unread,
    refresh,
    markRead,
    markAllRead,
    browserNotifications: browserState(optedIn),
    enableBrowserNotifications,
    disableBrowserNotifications,
  };

  return (
    <RunMonitorContext.Provider value={value}>
      {children}
      <div
        className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-[22rem] max-w-[calc(100vw-2rem)] flex-col gap-2"
        aria-live="polite"
      >
        {toasts.map((toast) => (
          <RunToast
            key={toast.id}
            notification={toast.notification}
            onDismiss={() => setToasts((cur) => cur.filter((t) => t.id !== toast.id))}
            onViewRun={() => open(toast)}
            onViewResults={() => open(toast, "results")}
          />
        ))}
      </div>
    </RunMonitorContext.Provider>
  );
}

function RunToast({
  notification,
  onDismiss,
  onViewRun,
  onViewResults,
}: {
  notification: AppNotification;
  onDismiss: () => void;
  onViewRun: () => void;
  onViewResults: () => void;
}) {
  const kind = notification.type;
  const Icon = kind === "run_succeeded" ? CheckCircle2 : kind === "run_failed" ? XCircle : Ban;
  const tone =
    kind === "run_succeeded" ? "text-signal-low" : kind === "run_failed" ? "text-[#D71920]" : "text-ink-muted";
  return (
    <div role="status" className="pointer-events-auto surface border border-panel-border bg-white p-4 shadow-lg">
      <div className="flex items-start gap-3">
        <Icon size={18} className={`mt-0.5 shrink-0 ${tone}`} />
        <div className="min-w-0 flex-1">
          <p className="text-sm font-semibold text-ink">{notification.title}</p>
          {notification.body && <p className="mt-0.5 text-xs text-ink-muted">{notification.body}</p>}
          {notification.acelo_run_id && (
            <p className="mt-1 truncate font-mono text-[11px] text-ink-faint">Run {notification.acelo_run_id}</p>
          )}
          <div className="mt-3 flex gap-2">
            {kind === "run_succeeded" && (
              <button
                onClick={onViewResults}
                className="rounded-sm bg-[#D71920] px-3 py-1.5 text-xs font-medium text-white hover:bg-[#b5141a]"
              >
                View Results
              </button>
            )}
            <button
              onClick={onViewRun}
              className="rounded-sm border border-panel-border px-3 py-1.5 text-xs font-medium text-ink hover:bg-panel-hover"
            >
              {kind === "run_failed" ? "View Details" : "View Run"}
            </button>
          </div>
        </div>
        <button onClick={onDismiss} aria-label="Dismiss notification" className="text-ink-faint hover:text-ink">
          <X size={14} />
        </button>
      </div>
    </div>
  );
}
