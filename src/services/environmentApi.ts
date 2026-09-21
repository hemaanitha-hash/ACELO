// ============================================================================
// ACELO ENVIRONMENT API (Phase 1)
//
// Connection + discovery only. Unlike services/api.ts, this module deliberately
// has NO demo fallback: if the backend fails, the caller gets a real error and
// the UI shows a real failure state. A connection status must never be invented
// in the browser.
//
// The browser never holds a platform credential. client_secret is write-only on
// the way in, and no response type below has a field that could carry one back.
// ============================================================================

const API_BASE =
  (import.meta.env?.VITE_API_BASE as string | undefined) ?? "http://localhost:8000/api";

export type EnvironmentPlatform = "fabric" | "databricks" | "file";

export type EnvironmentStatus =
  | "not_configured"
  | "connected"
  | "connection_failed"
  | "environment_ready"
  | "discovery_failed";

export interface Environment {
  id: string;
  customer_id: string;
  /** The connection this environment is built on. */
  connection_id: string;
  name: string;
  platform: EnvironmentPlatform;
  auth_mode: AuthMode | null;
  tenant_id: string | null;
  workspace_id: string | null;
  workspace_name: string | null;
  status: EnvironmentStatus;
  last_error_code: string | null;
  last_error_message: string | null;
  last_verified_at: string | null;
  last_discovered_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ConnectionTestResult {
  platform: string;
  connected: boolean;
  workspace_id?: string | null;
  workspace_name?: string | null;
  message: string;
  error_code?: string | null;
  last_verified_at?: string | null;
}

export interface DiscoveredItem {
  id: string;
  display_name: string;
  type: string;
}

export interface DiscoveryResult {
  discovered: boolean;
  workspace?: { id: string; name: string } | null;
  items: DiscoveredItem[];
  counts: Record<string, number>;
  discovered_at?: string | null;
  error_code?: string | null;
  message?: string | null;
}

export interface Readiness {
  authentication: boolean;
  workspace_access: boolean;
  environment_discovery: boolean;
  ready_for_analysis: boolean;
  status: string;
}

/** Safe, human-readable text for provisioning error codes. */
export const PROVISIONING_MESSAGES: Record<string, string> = {
  ASSET_MISSING:
    "This ACELO build does not bundle an optimization notebook for this domain, so it cannot be deployed.",
  PROVISIONING_FAILED: "ACELO could not be set up in this workspace.",
  NOT_PROVISIONED: "ACELO is not set up in this workspace yet.",
  PERMISSION_DENIED:
    "The configured identity cannot create items in this workspace. Contributor access or higher is required.",
  AUTHENTICATION_FAILED: "ACELO could not authenticate with Fabric. Check the environment credentials.",
  WORKSPACE_NOT_FOUND: "The configured workspace could not be found.",
  UNSUPPORTED: "Package provisioning is not supported for this platform.",
  PLATFORM_API_UNAVAILABLE: "The Fabric API is unavailable. Try again shortly.",
  TIMEOUT: "Fabric did not respond in time.",
};

export function describeProvisioningError(code?: string | null, fallback?: string | null): string {
  if (code && PROVISIONING_MESSAGES[code]) return PROVISIONING_MESSAGES[code];
  return fallback || "ACELO setup could not be completed.";
}

/** "user" = signed-in organization Microsoft account (delegated).
 *  "personal" is the legacy spelling of "user", still accepted by the backend. */
export type AuthMode = "service_principal" | "user" | "personal";

export interface CreateEnvironmentInput {
  name: string;
  platform: EnvironmentPlatform;
  /** "user" uses the signed-in Microsoft account's delegated token; no client_secret. */
  auth_mode?: AuthMode;
  tenant_id?: string;
  workspace_id?: string;
  client_id?: string;
  /** Write-only. Encrypted by the backend on arrival; never returned. */
  client_secret?: string;
  endpoint?: string;
}

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "ApiError";
  }
}

/**
 * Delegated Fabric token header, used only by "personal" authentication mode.
 * The backend consumes it for the duration of the request and discards it — it
 * is never persisted, logged, or echoed back.
 */
export function fabricTokenHeader(token?: string | null): Record<string, string> {
  return token ? { "X-Fabric-Access-Token": token } : {};
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      ...options,
      headers: { "Content-Type": "application/json", ...options?.headers },
    });
  } catch {
    // Network-level failure. Surfaced, never swallowed into a demo result.
    throw new ApiError(
      "Could not reach the ACELO backend. Check that the API is running.",
      0
    );
  }

  if (!res.ok) {
    let detail = `Request failed (HTTP ${res.status}).`;
    try {
      const body = await res.json();
      if (body?.detail) detail = String(body.detail);
    } catch {
      /* non-JSON error body; keep the generic message */
    }
    throw new ApiError(detail, res.status);
  }

  return (await res.json()) as T;
}

export function listEnvironments(): Promise<Environment[]> {
  return request<Environment[]>("/environments");
}

export function createEnvironment(input: CreateEnvironmentInput): Promise<Environment> {
  return request<Environment>("/environments", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function updateEnvironment(
  id: string,
  input: Partial<CreateEnvironmentInput>
): Promise<Environment> {
  return request<Environment>(`/environments/${id}`, {
    method: "PATCH",
    body: JSON.stringify(input),
  });
}

/** Real backend-performed platform connection test. */
export function testEnvironment(id: string, token?: string | null): Promise<ConnectionTestResult> {
  return request<ConnectionTestResult>(`/environments/${id}/test`, {
    method: "POST",
    headers: fabricTokenHeader(token),
  });
}

/** Real read-only workspace discovery. Runs no notebooks. */
export function discoverEnvironment(id: string, token?: string | null): Promise<DiscoveryResult> {
  return request<DiscoveryResult>(`/environments/${id}/discover`, {
    method: "POST",
    headers: fabricTokenHeader(token),
  });
}

export function getReadiness(id: string): Promise<Readiness> {
  return request<Readiness>(`/environments/${id}/readiness`);
}

// ---------------------------------------------------------------------------
// Step 3 — ACELO package provisioning
// ---------------------------------------------------------------------------

export type ProvisioningStatus =
  | "NOT_INSTALLED"
  | "INSTALLING"
  | "INSTALLED"
  | "UPDATE_AVAILABLE"
  | "UPDATING"
  | "FAILED";

export interface DomainProvisioning {
  asset_available: boolean;
  deployed: boolean;
  display_name: string;
  /** The REAL Fabric item ID, once deployed. Not a secret. */
  platform_resource_id: string | null;
  ready: boolean;
  error_code: string | null;
  message: string | null;
}

export interface ProvisioningState {
  status: ProvisioningStatus;
  step: string | null;
  package_name: string;
  package_version_available: string;
  package_version_installed: string | null;
  deployable: boolean;
  namespace: string;
  last_provisioned_at: string | null;
  domains: Record<string, DomainProvisioning>;
  error_code: string | null;
  message: string | null;
  missing_assets: string[];
  /** Lakehouse the notebook is attached to; null means Spark table access will fail. */
  default_lakehouse?: { id: string; name: string; workspace_id: string; source?: string } | null;
  /** The default Lakehouse the deployed notebook was READ BACK as carrying. */
  lakehouse_binding?: {
    status: "VERIFIED" | "MISMATCH" | "MISSING" | "UNVERIFIED";
    bound_id?: string | null;
    bound_name?: string | null;
    reason?: string;
  } | null;
  /** Fabric Environment providing the notebook's libraries; null = workspace default. */
  fabric_environment?: { id: string; name: string; workspace_id: string; source: string } | null;
  execution_type?: string;
  pipeline?: {
    name: string;
    platform_resource_id: string | null;
    ready: boolean;
    error_code: string | null;
    message: string | null;
  } | null;
}

export interface Readiness2 extends Readiness {
  package_status: string;
  cluster_ready: boolean;
  query_ready: boolean;
  storage_ready: boolean;
  /**
   * Whether Cluster optimization alone can run. Cluster never calls into the
   * Query or Storage notebooks, so those being absent must not block it.
   */
  cluster_ready_for_analysis: boolean;
  cluster_configured: boolean;
  cluster_blocked_reason: string | null;
}

/** Deploys the ACELO package. Explicit user action only. */
export function provisionEnvironment(
  id: string,
  token?: string | null
): Promise<ProvisioningState> {
  return request<ProvisioningState>(`/environments/${id}/provision`, {
    method: "POST",
    headers: fabricTokenHeader(token),
  });
}

export function getProvisioningStatus(id: string): Promise<ProvisioningState> {
  return request<ProvisioningState>(`/environments/${id}/provision/status`);
}

export function getReadiness2(id: string): Promise<Readiness2> {
  return request<Readiness2>(`/environments/${id}/readiness`);
}

export function getEnvironment(id: string): Promise<Environment> {
  return request<Environment>(`/environments/${id}`);
}

interface PersistedResources {
  environment_id: string;
  counts: Record<string, number>;
  resources: { platform_resource_id: string; display_name: string; resource_type: string }[];
}

/**
 * The result of the LAST successful discovery, as persisted by the backend.
 * Lets the page show "Discovery complete" after a reload without re-running it.
 */
export async function getPersistedDiscovery(env: Environment): Promise<DiscoveryResult | null> {
  if (!env.last_discovered_at) return null;
  const data = await request<PersistedResources>(`/environments/${env.id}/resources`);
  return {
    discovered: true,
    workspace:
      env.workspace_id && env.workspace_name ? { id: env.workspace_id, name: env.workspace_name } : null,
    items: data.resources.map((r) => ({
      id: r.platform_resource_id,
      display_name: r.display_name,
      type: r.resource_type,
    })),
    counts: data.counts,
    discovered_at: env.last_discovered_at,
  };
}

/** Non-secret settings the Cluster notebook needs (tables, lakehouse, SQL endpoint). */
export interface ClusterSettings {
  source_table: string;
  result_table: string;
  source_lakehouse: string;
  result_lakehouse: string;
  lakehouse_database: string;
  sql_endpoint: string;
  column_mapping: string;
  /** Fabric Environment (Spark libraries, e.g. xgboost) attached to the notebook. */
  fabric_environment_id: string;
  /** "notebook" (direct) or "pipeline" (a Fabric Data Pipeline). */
  execution_type: string;
  /** Schema of a schema-enabled Lakehouse (e.g. "dbo"): tables resolve to dbo.<table>. */
  source_schema: string;
  result_schema: string;
  /** The pipeline to run when execution_type is "pipeline"; blank = ACELO's pipeline. */
  pipeline_id: string;
  /** Lakehouse item bound as the notebook's DEFAULT Lakehouse (needed when discovery can't see it). */
  lakehouse_id: string;
  /** Workspace of that Lakehouse, if different from this environment's. */
  lakehouse_workspace_id: string;
  /** Delta table ACELO reads approval candidates from (e.g. cluster_optimization_tracking). */
  approval_tracking_table: string;
}

/** A Fabric pipeline known to this environment (discovered or ACELO-deployed). */
export interface WorkspacePipeline {
  id: string;
  name: string;
  managed: boolean;
}

/** The execution path the backend will ACTUALLY use for a Cluster run. */
export interface ClusterExecution {
  execution_type: "notebook" | "pipeline";
  pipeline: { id: string; name: string | null; source: "configured" | "acelo-managed" } | null;
}

export interface ClusterSettingsState {
  settings: ClusterSettings;
  /** Required settings still missing; empty when Cluster is configured. */
  missing: string[];
  execution?: ClusterExecution;
  pipelines?: WorkspacePipeline[];
}

/** As the API returns them: anything not configured is null, never "" or a sample. */
type StoredClusterSettings = { [K in keyof ClusterSettings]: string | null };

function fromStored(settings: StoredClusterSettings): ClusterSettings {
  const out = {} as ClusterSettings;
  (Object.keys(settings) as (keyof ClusterSettings)[]).forEach((k) => {
    out[k] = settings[k] ?? "";
  });
  return out;
}

async function clusterSettingsRequest(path: string, options?: RequestInit): Promise<ClusterSettingsState> {
  const raw = await request<Omit<ClusterSettingsState, "settings"> & { settings: StoredClusterSettings }>(
    path,
    options
  );
  return { ...raw, settings: fromStored(raw.settings) };
}

export function getClusterSettings(id: string): Promise<ClusterSettingsState> {
  return clusterSettingsRequest(`/environments/${id}/cluster-settings`);
}

export function saveClusterSettings(
  id: string,
  settings: Partial<ClusterSettings>
): Promise<ClusterSettingsState> {
  // Blank means "not configured": sent as "" so the backend removes the value
  // (it then reads back as null). Nothing is ever defaulted to a sample.
  return clusterSettingsRequest(`/environments/${id}/cluster-settings`, {
    method: "PUT",
    body: JSON.stringify(settings),
  });
}
