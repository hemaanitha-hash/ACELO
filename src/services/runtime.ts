import { API_BASE } from "./apiBase";
// ============================================================================
// How this ACELO instance is deployed.
//
// As a native Databricks App the workspace and identity come from the runtime,
// so the UI must not ask for a workspace URL, an access token, or a platform —
// there is nothing for the user to decide. Outside an App this reports the
// existing configured-connection model and the UI is unchanged.
//
// The response carries no credential: only where we are and which mechanism is
// in use. The token itself never leaves the backend.
// ============================================================================

export interface RuntimeInfo {
  /** True when ACELO is running as a Databricks App. */
  databricks_app: boolean;
  /** The workspace the App is running in, e.g. https://adb-123.azuredatabricks.net */
  workspace_host: string | null;
  auth_mode: "databricks_app_identity" | "configured_connection";
}

const LOCAL: RuntimeInfo = {
  databricks_app: false,
  workspace_host: null,
  auth_mode: "configured_connection",
};

let current: RuntimeInfo = LOCAL;

export function getRuntime(): RuntimeInfo {
  return current;
}

/** True when the user must not be asked for Databricks credentials. */
export function isDatabricksApp(): boolean {
  return current.databricks_app;
}

/**
 * Reads the deployment model once at startup. A failure falls back to the
 * configured-connection model, which is the safe default: it asks for
 * credentials that an App would not need, rather than hiding the only way to
 * connect.
 */
export async function loadRuntime(): Promise<RuntimeInfo> {
  try {
    const res = await fetch(`${API_BASE}/runtime`, {
      headers: { "Content-Type": "application/json" },
    });
    if (!res.ok) return current;
    current = (await res.json()) as RuntimeInfo;
  } catch {
    current = LOCAL;
  }
  return current;
}

/** Test seam. */
export function setRuntime(info: RuntimeInfo): void {
  current = info;
}
