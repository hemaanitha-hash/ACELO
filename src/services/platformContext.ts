// ============================================================================
// ACTIVE PLATFORM CONTEXT — the single source of truth for which platform the
// application is in.
//
// Before this module the Sidebar's platform dropdown was a hardcoded array in
// local component state that nothing else read, and every API call resolved a
// connection by picking `connections.find(connected) ?? connections[0]`. With
// both a Fabric and a Databricks connection configured that is an arbitrary
// choice — which is exactly how one platform's data appeared while the other
// was selected.
//
// Now: the user's selection lives here, is persisted per browser, and is sent
// on EVERY backend request as
//
//     X-Acelo-Platform:      "databricks" | "fabric"
//     X-Acelo-Connection-Id: <connection id>
//
// so the backend filters by platform rather than the frontend hiding rows it
// already fetched.
// ============================================================================

export type ActivePlatformId = "databricks" | "fabric";

export interface PlatformConnection {
  id: string;
  platform: ActivePlatformId;
  /** The workspace/lakehouse name shown under the platform in the switcher. */
  name: string;
  status?: string | null;
}

export interface ActiveContext {
  platform: ActivePlatformId | null;
  connection: PlatformConnection | null;
}

export const PLATFORM_LABELS: Record<ActivePlatformId, string> = {
  databricks: "Databricks",
  fabric: "Microsoft Fabric",
};

const STORAGE_KEY = "acelo.activeConnectionId";

// Module-level state so the non-React service layer can read the context when
// building a request, without every caller having to thread it through.
let current: ActiveContext = { platform: null, connection: null };

type Listener = (context: ActiveContext) => void;
const listeners = new Set<Listener>();

export function getActiveContext(): ActiveContext {
  return current;
}

export function setActiveContext(connection: PlatformConnection | null): void {
  current = connection ? { platform: connection.platform, connection } : { platform: null, connection: null };
  try {
    if (connection) window.localStorage.setItem(STORAGE_KEY, connection.id);
    else window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* storage unavailable (private window, blocked site data) */
  }
  for (const listener of listeners) listener(current);
}

export function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** The connection id this browser last selected, if any. */
export function rememberedConnectionId(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

/**
 * Picks which connection should be active given what the backend reports.
 * Prefers the remembered one, then a connected one, then the first.
 */
export function resolveInitial(connections: PlatformConnection[]): PlatformConnection | null {
  if (connections.length === 0) return null;
  const remembered = rememberedConnectionId();
  return (
    connections.find((c) => c.id === remembered) ??
    connections.find((c) => c.status === "connected") ??
    connections[0]
  );
}

/**
 * The headers that carry the active context to the backend. Every service
 * module spreads these into its requests, so there is one place that decides
 * what "active" means.
 */
export function platformHeaders(): Record<string, string> {
  const { platform, connection } = current;
  const headers: Record<string, string> = {};
  if (platform) headers["X-Acelo-Platform"] = platform;
  if (connection) headers["X-Acelo-Connection-Id"] = connection.id;
  return headers;
}

/** True when `platform` is the one currently active. */
export function isActive(platform: ActivePlatformId): boolean {
  return current.platform === platform;
}

/** Test seam: clears the context and the remembered selection. */
export function resetActiveContext(): void {
  setActiveContext(null);
}
