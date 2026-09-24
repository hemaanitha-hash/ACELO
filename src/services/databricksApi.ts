// ============================================================================
// ACELO DATABRICKS DISCOVERY API
//
// Read-only compute capability discovery. Like environmentApi, this module has
// NO demo fallback: a failed request surfaces a real error, and a connection
// status is never invented in the browser.
//
// The browser never holds a Databricks credential. It calls the ACELO backend,
// which holds the PAT server-side — no type below has a field that could carry
// a token back out.
// ============================================================================

import { platformHeaders } from "./platformContext";
import { API_BASE } from "./apiBase";

export type DatabricksResourceType =
  | "CLASSIC_CLUSTER"
  | "SERVERLESS_COMPUTE"
  | "SQL_WAREHOUSE";

/** One resource in the common ACELO resource model. */
export interface AceloResource {
  platform: string;
  resource_type: DatabricksResourceType;
  resource_id: string;
  name: string;
  state: string | null;
  /** Values copied verbatim from Databricks; shape varies by resource type. */
  metadata: Record<string, unknown>;
}

/**
 * Per-resource-type outcome. This is what separates "the workspace has none"
 * (OK, zero resources) from "we asked and were refused" (UNAVAILABLE — a 403
 * or 404) from "the call broke" (ERROR — 401, 5xx, timeout).
 */
export interface ResourceTypeStatus {
  resource_type: DatabricksResourceType;
  status: "OK" | "UNAVAILABLE" | "ERROR";
  reason?: string | null;
  message?: string | null;
}

/**
 * One discovery call, for diagnosis. Carries the platform's own status and
 * error body — the backend never puts a request header or token in here.
 */
export interface DiscoveryProbe {
  resource_type: DatabricksResourceType;
  method: string;
  path: string;
  status_code: number | null;
  ok: boolean;
  failure_kind?: string | null;
  platform_error_code?: string | null;
  platform_message?: string | null;
}

export interface DatabricksResources {
  platform: string;
  environment_id: string;
  workspace_name: string | null;
  connected: boolean;
  resources: AceloResource[];
  statuses: ResourceTypeStatus[];
  probes?: DiscoveryProbe[];
}

export class DatabricksApiError extends Error {}

export async function getDatabricksResources(
  environmentId?: string | null,
): Promise<DatabricksResources> {
  const query = environmentId ? `?environment_id=${encodeURIComponent(environmentId)}` : "";
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/databricks/resources${query}`, {
      headers: { "Content-Type": "application/json", ...platformHeaders() },
    });
  } catch {
    throw new DatabricksApiError("ACELO could not reach the backend.");
  }

  if (!res.ok) {
    // The backend returns a safe, user-facing `detail`; anything else is generic.
    let detail = `Discovery failed (HTTP ${res.status}).`;
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new DatabricksApiError(detail);
  }

  return (await res.json()) as DatabricksResources;
}

/** Groups a flat resource list by normalised type, preserving backend order. */
export function groupByType(
  resources: AceloResource[],
): Record<DatabricksResourceType, AceloResource[]> {
  const grouped: Record<DatabricksResourceType, AceloResource[]> = {
    CLASSIC_CLUSTER: [],
    SERVERLESS_COMPUTE: [],
    SQL_WAREHOUSE: [],
  };
  for (const resource of resources) {
    grouped[resource.resource_type]?.push(resource);
  }
  return grouped;
}
