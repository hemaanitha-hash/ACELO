// ============================================================================
// The customer's real platform connections, for the platform switcher.
//
// Kept separate from platformContext.ts so that module stays free of network
// calls: the context is state, this is how the state's options are loaded.
// ============================================================================

import type { ActivePlatformId, PlatformConnection } from "./platformContext";
import { API_BASE } from "./apiBase";

interface ConnectionRow {
  id: string;
  platform: string;
  workspace?: string | null;
  status?: string | null;
}

const SELECTABLE: ActivePlatformId[] = ["databricks", "fabric"];

/**
 * Lists the connections a user may switch between.
 *
 * The internal "file" connection backs uploaded-file runs and is not a platform
 * anyone selects, so it is filtered out — as is any platform ACELO does not
 * have a context for, rather than rendering an entry that cannot work.
 */
export async function listPlatformConnections(): Promise<PlatformConnection[]> {
  const res = await fetch(`${API_BASE}/connections`, {
    headers: { "Content-Type": "application/json" },
  });
  if (!res.ok) throw new Error(`Connections could not be loaded (HTTP ${res.status}).`);

  const rows = (await res.json()) as ConnectionRow[];
  return rows
    .filter((row): row is ConnectionRow & { platform: ActivePlatformId } =>
      SELECTABLE.includes(row.platform as ActivePlatformId),
    )
    .map((row) => ({
      id: row.id,
      platform: row.platform,
      name: row.workspace || row.id,
      status: row.status ?? null,
    }));
}
