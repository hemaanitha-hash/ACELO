import React, { useEffect, useMemo, useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  Cloud,
  Database,
  Loader2,
  LogIn,
  Package,
  Plus,
  RefreshCw,
  ShieldCheck,
  Upload,
} from "lucide-react";
import { Link, useNavigate } from "react-router-dom";
import { useMsal } from "@azure/msal-react";
import { InteractionStatus } from "@azure/msal-browser";
import type { AccountInfo } from "@azure/msal-browser";
import Layout from "../components/Layout";
import OptimizationResources from "../components/OptimizationResources";
import Modal from "../components/Modal";
import { isMsalConfigured, REDIRECT_URI_SETUP_HINT } from "../authConfig";
import {
  getActiveContext,
  setActiveContext,
  subscribe,
  type ActivePlatformId,
} from "../services/platformContext";
import { listPlatformConnections } from "../services/platformConnections";
import { explainState } from "../services/discoveryText";
import { getRuntime } from "../services/runtime";
import { isDatabricksOnly } from "../services/experience";
import {
  describeAccount,
  FabricAuthError,
  getFabricToken,
  signIn,
  signOut,
} from "../services/fabricAuth";
import {
  ApiError,
  createEnvironment,
  describeProvisioningError,
  discoverEnvironment,
  getClusterSettings,
  getEnvironment,
  getPersistedDiscovery,
  getProvisioningStatus,
  getReadiness2,
  listEnvironments,
  provisionEnvironment,
  saveClusterSettings,
  setRequestIdentity,
  testEnvironment,
  updateEnvironment,
  type ClusterExecution,
  type ClusterSettings,
  type ClusterSettingsState,
  type ConnectionTestResult,
  type DiscoveryResult,
  type Environment,
  type AuthMode,
  type EnvironmentPlatform,
  type ProvisioningState,
  type Readiness2,
  type WorkspacePipeline,
} from "../services/environmentApi";

/**
 * Phase 1 — Environment Setup.
 *
 * Connection state shown here is ALWAYS the backend's verdict. There is no
 * setTimeout, no optimistic "Connected", and no demo fallback: if the API call
 * fails the user sees the failure. Credentials are write-only — the secret is
 * posted once and never read back.
 */

type Stage = "idle" | "testing" | "discovering" | "provisioning" | "saving";

const EMPTY_CLUSTER_SETTINGS: ClusterSettings = {
  source_table: "",
  result_table: "",
  source_lakehouse: "",
  result_lakehouse: "",
  lakehouse_database: "",
  sql_endpoint: "",
  column_mapping: "",
  fabric_environment_id: "",
  execution_type: "",
  source_schema: "",
  result_schema: "",
  pipeline_id: "",
  lakehouse_id: "",
  lakehouse_workspace_id: "",
  approval_tracking_table: "",
};

/** Package states in which the ACELO notebooks are deployed and usable. */
const INSTALLED_STATES = ["INSTALLED", "UPDATE_AVAILABLE"];

/** Connection result implied by a persisted environment (after a page reload). */
function persistedTestResult(env: Environment): ConnectionTestResult | null {
  if (env.status === "connected" || env.status === "environment_ready" || env.status === "discovery_failed") {
    return {
      platform: env.platform,
      connected: true,
      workspace_id: env.workspace_id,
      workspace_name: env.workspace_name,
      message: "Connection previously verified",
      last_verified_at: env.last_verified_at,
    };
  }
  if (env.last_error_message) {
    return {
      platform: env.platform,
      connected: false,
      message: env.last_error_message,
      error_code: env.last_error_code,
    };
  }
  return null;
}

const PLATFORM_LABELS: Record<EnvironmentPlatform, string> = {
  fabric: "Microsoft Fabric",
  databricks: "Databricks",
  file: "File Analysis",
};

// Order the discovery summary so the counts that matter most read first.
const COUNT_ORDER = [
  "Folder",
  "Notebook",
  "Lakehouse",
  "SQLEndpoint",
  "Warehouse",
  "Experiment",
  "Cluster",
  "Job",
];

/** Labels for the normalised resource types used by per-type discovery states. */
const RESOURCE_TYPE_LABELS: Record<string, string> = {
  CLASSIC_CLUSTER: "Classic clusters",
  SERVERLESS_COMPUTE: "Serverless compute",
  SQL_WAREHOUSE: "SQL warehouses",
};

/** Short platform names, for inline sentences where the full label reads oddly. */
const SHORT_PLATFORM_LABELS: Record<EnvironmentPlatform, string> = {
  fabric: "Fabric",
  databricks: "Databricks",
  file: "File analysis",
};

const COUNT_LABELS: Record<string, string> = {
  Folder: "Folders",
  Notebook: "Notebooks",
  Lakehouse: "Lakehouses",
  SQLEndpoint: "SQL Endpoints",
  Warehouse: "Warehouses",
  Experiment: "Experiments",
  Cluster: "Clusters",
  Job: "Jobs",
  SQLWarehouse: "SQL Warehouses",
  ServerlessCompute: "Serverless Compute",
};

export default function Settings() {
  // `accounts` from useMsal is reactive: it updates when MSAL resolves a sign-in,
  // including one completed by handleRedirectPromise() during startup. The
  // previous version seeded a useState initializer once at mount, so an account
  // that arrived afterwards was never picked up and the UI stayed signed-out.
  const { instance, accounts, inProgress } = useMsal();
  // Identity (not a token) so the backend can tell administrators from users.
  useEffect(() => {
    setRequestIdentity((instance.getActiveAccount?.() ?? accounts[0]) || null);
  }, [instance, accounts]);
  const navigate = useNavigate();
  /**
   * Which platform this page configures.
   *
   * Seeded from the GLOBAL active platform context — the same one the Sidebar,
   * breadcrumb and every API call use. It was previously `useState("fabric")`,
   * a second source of truth that ignored the connected platform entirely: with
   * Databricks active the page still selected Microsoft Fabric, then loaded the
   * Fabric environment and its Microsoft Account auth mode, rendering Fabric
   * fields over a Databricks connection.
   *
   * Local state remains because "file" is a setup view rather than a platform
   * anyone is "in", and because a platform can be configured here before any
   * connection for it exists. For databricks/fabric the context is authoritative
   * and is kept in step in both directions.
   */
  const [platform, setPlatform] = useState<EnvironmentPlatform>(
    // Databricks-only is the MVP, so it is also the fallback when no context
    // has resolved yet. Falling back to Fabric here rendered the Fabric
    // authentication block for a moment in a Databricks-only build.
    () => getActiveContext().platform ?? (isDatabricksOnly() ? "databricks" : "fabric")
  );
  const [authMode, setAuthMode] = useState<AuthMode>("service_principal");
  /**
   * "Add connection" mode. While true the page configures a NEW platform the
   * user explicitly chose, rather than the one they are connected to — which is
   * why the platform choice lives in that flow and nowhere else.
   */
  const [addOpen, setAddOpen] = useState(false);
  const [addingConnection, setAddingConnection] = useState(false);
  /** The connection the user is working in, for the "Configuring" heading. */
  const activeConnectionName = getActiveContext().connection?.name ?? null;
  /**
   * As a Databricks App the workspace and identity come from the runtime, so
   * ACELO must not ask for a workspace URL, an access token, or a platform —
   * there is nothing for the user to decide and nothing to store.
   */
  const runtime = getRuntime();
  /**
   * The Databricks-only MVP manages its own connection through the Databricks
   * App runtime. Credential entry, the platform picker and Add connection are
   * ABSENT from this experience — not merely disabled — because there is
   * nothing for the user to decide.
   */
  const databricksOnly = isDatabricksOnly();
  const appManaged = databricksOnly || (runtime.databricks_app && platform === "databricks");
  const [signedInAccount, setSignedInAccount] = useState<AccountInfo | null>(null);
  const [signingIn, setSigningIn] = useState(false);

  const account: AccountInfo | null =
    signedInAccount ?? instance.getActiveAccount() ?? accounts[0] ?? null;

  // Adopt an account MSAL resolves after mount (the redirect-callback case).
  useEffect(() => {
    if (signedInAccount) return;
    const resolved = instance.getActiveAccount() ?? accounts[0] ?? null;
    if (resolved) setSignedInAccount(resolved);
  }, [accounts, instance, signedInAccount]);
  const [environment, setEnvironment] = useState<Environment | null>(null);
  const [stage, setStage] = useState<Stage>("idle");

  const [form, setForm] = useState({
    name: "",
    tenantId: "",
    workspaceId: "",
    clientId: "",
    clientSecret: "",
    endpoint: "",
  });

  const [testResult, setTestResult] = useState<ConnectionTestResult | null>(null);
  const [discovery, setDiscovery] = useState<DiscoveryResult | null>(null);
  const [provisioning, setProvisioning] = useState<ProvisioningState | null>(null);
  // Backend-computed readiness. It is the authority on whether Cluster can run.
  const [readiness, setReadiness] = useState<Readiness2 | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [clusterSettings, setClusterSettings] = useState<ClusterSettings>(EMPTY_CLUSTER_SETTINGS);
  const [clusterMissing, setClusterMissing] = useState<string[]>([]);
  // What the backend persisted on the last successful save — the confirmation
  // shows THIS, never the form's local values.
  const [savedState, setSavedState] = useState<ClusterSettingsState | null>(null);
  // Resource mapping is administrator configuration; read-only for everyone else.
  const [canConfigure, setCanConfigure] = useState(true);
  const [saveError, setSaveError] = useState<string | null>(null);
  // Backend-resolved execution path and the workspace's pipelines.
  const [clusterExecution, setClusterExecution] = useState<ClusterExecution | null>(null);
  const [pipelines, setPipelines] = useState<WorkspacePipeline[]>([]);

  /**
   * Re-reads everything this page shows from the backend. Called on load and
   * after EVERY operation, so no control can stay disabled because local state
   * missed an update. Each read is independent: one failing does not blank the
   * others.
   */
  async function refresh(envId: string, opts: { restoreConnection?: boolean } = {}) {
    const [env, prov, ready, settings] = await Promise.all([
      getEnvironment(envId).catch(() => null),
      getProvisioningStatus(envId).catch(() => null),
      getReadiness2(envId).catch(() => null),
      getClusterSettings(envId).catch(() => null),
    ]);
    if (env) {
      setEnvironment(env);
      if (opts.restoreConnection) setTestResult(persistedTestResult(env));
      // Discovery persisted by the backend survives reloads and later steps.
      const persisted = await getPersistedDiscovery(env).catch(() => null);
      if (persisted) setDiscovery(persisted);
    }
    if (prov) setProvisioning(prov);
    if (ready) setReadiness(ready);
    if (settings) applyClusterState(settings);
  }

  function applyClusterState(state: ClusterSettingsState) {
    setClusterSettings(state.settings);
    setClusterMissing(state.missing);
    setClusterExecution(state.execution ?? null);
    setPipelines(state.pipelines ?? []);
  }

  /**
   * Clears everything belonging to the platform being left.
   *
   * Without this the previous platform's form values and auth mode survived a
   * switch, so a Fabric tenant/workspace could sit in a Databricks form — the
   * two platforms' configuration must never mix.
   */
  /**
   * Points the global context at the chosen platform's connection. If none
   * exists yet (first-time setup) the page still switches locally so it can be
   * configured — a connection is never invented to satisfy the switch.
   */
  async function switchActivePlatform(next: ActivePlatformId) {
    try {
      const connections = await listPlatformConnections();
      const match = connections.find((c) => c.platform === next);
      if (match) setActiveContext(match);
    } catch {
      /* the local view still switched; pages surface their own errors */
    }
  }

  /** Starts configuring a platform the user picked from the Add flow. */
  function beginAddConnection(next: EnvironmentPlatform) {
    setAddOpen(false);
    clearPlatformState();
    setAddingConnection(true);
    setPlatform(next);
  }

  /** Abandons the new connection and returns to the one in use. */
  function cancelAddConnection() {
    setAddingConnection(false);
    clearPlatformState();
    setPlatform(getActiveContext().platform ?? (isDatabricksOnly() ? "databricks" : "fabric"));
  }

  function clearPlatformState() {
    setForm({ name: "", tenantId: "", workspaceId: "", clientId: "", clientSecret: "", endpoint: "" });
    setAuthMode("service_principal");
    setReadiness(null);
    setClusterSettings(EMPTY_CLUSTER_SETTINGS);
    setClusterMissing([]);
    setEnvironment(null);
    setTestResult(null);
    setDiscovery(null);
    setProvisioning(null);
    setSavedState(null);
    setError(null);
  }

  // The global context is authoritative: switching platform anywhere in the
  // app (the Sidebar switcher included) re-points this page too.
  useEffect(
    () =>
      subscribe((ctx) => {
        // While adding a connection the user is deliberately configuring a
        // different platform; a context update must not yank them back.
        if (addingConnection) return;
        if (ctx.platform && ctx.platform !== platform) {
          clearPlatformState();
          setPlatform(ctx.platform);
        }
      }),
    [platform, addingConnection]
  );

  // Restore the persisted environment so a page refresh keeps safe state.
  // Only non-secret fields come back from the API.
  useEffect(() => {
    listEnvironments()
      .then((envs) => {
        const existing = envs.find((e) => e.platform === platform);
        if (!existing) return;
        if (existing.auth_mode) setAuthMode(existing.auth_mode);
        setForm((f) => ({
          ...f,
          name: existing.name,
          tenantId: existing.tenant_id ?? "",
          workspaceId: existing.workspace_id ?? "",
        }));
        return refresh(existing.id, { restoreConnection: true });
      })
      .catch((e: unknown) => setError(e instanceof ApiError ? e.message : String(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [platform]);

  const isFabric = platform === "fabric";
  // "user" is the signed-in organization Microsoft account (delegated auth).
  const isUserMode = isFabric && authMode === "user";

  /**
   * Delegated token for the current request. Service Principal mode returns
   * null — the backend authenticates itself and ignores the header.
   */
  async function currentFabricToken(): Promise<string | null> {
    if (!isUserMode) return null;
    return await getFabricToken(instance);
  }

  async function handleSignIn() {
    setSigningIn(true);
    setError(null);
    try {
      setSignedInAccount(await signIn(instance));
    } catch (e: unknown) {
      if (e instanceof FabricAuthError) {
        if (e.code === "REDIRECTING") {
          // The browser is navigating to Microsoft; not an error.
        } else if (e.code === "REDIRECT_MISMATCH") {
          // The most common first-run failure, so spell out the exact fix.
          setError(`${e.message} ${REDIRECT_URI_SETUP_HINT}`);
        } else {
          setError(e.message);
        }
      } else {
        setError("Microsoft sign-in failed.");
      }
    } finally {
      setSigningIn(false);
    }
  }

  async function handleSignOut() {
    try {
      await signOut(instance);
    } finally {
      setSignedInAccount(null);
      setTestResult(null);
      setDiscovery(null);
      setProvisioning(null);
      setReadiness(null);
    }
  }
  /**
   * Why Test Connection is unavailable, or null when it can run. The button
   * used to be disabled silently (e.g. an empty environment name), which left
   * users with a dead control and no explanation.
   */
  const testBlockedReason = useMemo((): string | null => {
    if (isFabric) {
      if (authMode === "user") {
        // Needs only a signed-in Microsoft account and a workspace ID — never
        // a secret, and never a stale auth snapshot.
        if (!isMsalConfigured()) return "Microsoft sign-in is not configured for this deployment.";
        if (!account) return "Sign in with Microsoft to test the Fabric connection.";
        if (!form.workspaceId.trim()) return "Enter the Fabric Workspace ID.";
      } else {
        const missing = [
          !form.tenantId.trim() && "Tenant ID",
          !form.workspaceId.trim() && "Workspace ID",
          !form.clientId.trim() && "Client ID",
          !form.clientSecret && !environment && "Client Secret",
        ].filter(Boolean);
        if (missing.length) return `Enter ${missing.join(", ")}.`;
      }
    } else if (platform === "databricks") {
      if (!form.endpoint.trim()) return "Enter the Databricks workspace URL.";
      if (!form.clientSecret && !environment) return "Enter the access token.";
    } else {
      return "File analysis does not need a connection.";
    }
    return null;
  }, [form, isFabric, platform, environment, authMode, account]);
  const canTest = testBlockedReason === null;

  /** A name is required by the backend; default it rather than block on it. */
  function environmentName(): string {
    return form.name.trim() || (isFabric ? "Fabric Production" : "Databricks Production");
  }

  async function persistEnvironment(): Promise<Environment> {
    const userMode = isFabric && authMode === "user";
    const payload = {
      name: environmentName(),
      platform,
      auth_mode: isFabric ? authMode : undefined,
      // Personal mode never sends tenant/client/secret — there is no secret to
      // send, and the user's own token carries the tenant.
      tenant_id: userMode ? undefined : form.tenantId || undefined,
      workspace_id: form.workspaceId || undefined,
      client_id: userMode ? undefined : form.clientId || undefined,
      // Blank means "keep the stored secret" — the backend treats it that way.
      client_secret: userMode ? undefined : form.clientSecret || undefined,
      endpoint: form.endpoint || undefined,
    };

    if (environment) {
      const updated = await updateEnvironment(environment.id, payload);
      setEnvironment(updated);
      return updated;
    }
    const created = await createEnvironment(payload);
    setEnvironment(created);
    return created;
  }

  async function handleTestConnection() {
    setStage("testing");
    setError(null);
    setTestResult(null);
    setDiscovery(null);
    try {
      const token = await currentFabricToken();
      const env = await persistEnvironment();
      const result = await testEnvironment(env.id, token);
      setTestResult(result);
      // Clear the secret from component state once the backend holds it.
      if (result.connected) setForm((f) => ({ ...f, name: env.name, clientSecret: "" }));
      await refresh(env.id);
    } catch (e: unknown) {
      if (e instanceof FabricAuthError) setError(e.message);
      else setError(e instanceof ApiError ? e.message : "Unexpected error testing the connection.");
    } finally {
      setStage("idle");
    }
  }

  async function handleDiscover() {
    if (!environment) return;
    setStage("discovering");
    setError(null);
    try {
      const result = await discoverEnvironment(environment.id, await currentFabricToken());
      setDiscovery(result);
      await refresh(environment.id);
      // A failed discovery must stay visible even if an older one was persisted.
      if (!result.discovered) setDiscovery(result);
    } catch (e: unknown) {
      if (e instanceof FabricAuthError) setError(e.message);
      else setError(e instanceof ApiError ? e.message : "Unexpected error during discovery.");
    } finally {
      setStage("idle");
    }
  }

  async function handleProvision() {
    if (!environment) return;
    setStage("provisioning");
    setError(null);
    try {
      setProvisioning(await provisionEnvironment(environment.id, await currentFabricToken()));
      await refresh(environment.id);
    } catch (e: unknown) {
      if (e instanceof FabricAuthError) setError(e.message);
      else setError(e instanceof ApiError ? e.message : "Unexpected error setting up ACELO.");
    } finally {
      setStage("idle");
    }
  }

  async function handleSaveClusterSettings() {
    if (!environment) return;
    setStage("saving");
    setSaveError(null);
    setSavedState(null);
    try {
      const payload = { ...clusterSettings };
      if (payload.execution_type === "pipeline") {
        // Persist the pipeline actually shown as selected, not an implicit default.
        payload.pipeline_id = payload.pipeline_id || defaultPipelineId;
      } else {
        // Notebook needs no pipeline; don't persist a stale selection with it.
        payload.pipeline_id = "";
      }
      const saved = await saveClusterSettings(environment.id, payload);
      applyClusterState(saved);
      setSavedState(saved);
      await refresh(environment.id);
    } catch (e: unknown) {
      setSaveError(e instanceof ApiError ? e.message : "Unexpected error.");
    } finally {
      setStage("idle");
    }
  }

  // What the pipeline dropdown shows when no pipeline was explicitly chosen:
  // the one the backend resolves (ACELO's), so UI and execution agree.
  const defaultPipelineId =
    clusterExecution?.pipeline?.id ?? pipelines.find((p) => p.managed)?.id ?? pipelines[0]?.id ?? "";

  const connected = testResult?.connected === true;
  const discovered = discovery?.discovered === true;
  const installed = INSTALLED_STATES.includes(provisioning?.status ?? "");
  // Mirrors the backend rule: connection + discovery are not enough. A verified
  // ACELO package with a ready Cluster notebook is required.
  const readyForAnalysis =
    connected && discovered && installed && Boolean(provisioning?.domains?.cluster?.ready);

  // The actual reason each later step is unavailable — never a dead button.
  // Named for the ACTIVE platform: this string was hardcoded to Fabric and
  // appeared even while configuring Databricks. The short name is used so the
  // Fabric wording is unchanged ("Fabric connection required.").
  const connectionRequired = `${SHORT_PLATFORM_LABELS[platform]} connection required.`;
  const discoverBlockedReason = !environment || !connected ? connectionRequired : null;
  const provisionBlockedReason = !connected
    ? connectionRequired
    : !discovered
      ? "Environment discovery required."
      : provisioning?.deployable === false
        ? "This ACELO build has no optimization notebooks bundled, so there is nothing to deploy."
        : null;
  const settingsBlockedReason = !environment || !connected ? connectionRequired : null;

  // Cluster is independent of Query and Storage. The backend is the authority
  // on this; the local expression is only a fallback before readiness loads.
  const clusterReady =
    readiness?.cluster_ready_for_analysis ??
    (connected && discovered && Boolean(provisioning?.domains?.cluster?.ready));

  return (
    <Layout pageName="Environment Setup">
      <div className="flex flex-col gap-6">
        <div>
          <h1 className="text-display font-semibold text-ink">Environment Setup</h1>
          <p className="mt-2 text-sm text-ink-muted">
            Connect your data platform to ACELO. Credentials are stored and used only by the
            ACELO backend.
          </p>
        </div>

        {/* ---------------- Configuration ---------------- */}
        <section className="surface p-6">
          {/* The active connection already decided the platform, so this page
              no longer asks again. It states what is being configured and
              offers a separate, explicit flow for adding a different platform —
              "change what I am connected to" and "connect something new" are
              different intents and were previously the same row of cards. */}
          {addingConnection ? (
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="min-w-0">
                <p className="label-eyebrow">New connection</p>
                <h2 className="mt-1 text-lg font-semibold text-ink">
                  {PLATFORM_LABELS[platform]}
                </h2>
                <p className="mt-1 text-sm text-ink-muted">
                  Configure this platform, then test the connection to save it.
                </p>
              </div>
              <button type="button" className="btn-secondary" onClick={cancelAddConnection}>
                Cancel
              </button>
            </div>
          ) : (
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="min-w-0">
                <p className="label-eyebrow">Configuring</p>
                <h2 className="mt-1 flex flex-wrap items-center gap-2 text-lg font-semibold text-ink">
                  {PLATFORM_LABELS[platform]}
                  {activeConnectionName && (
                    <span className="truncate text-sm font-normal text-ink-muted">
                      · {activeConnectionName}
                    </span>
                  )}
                </h2>
                <p className="mt-1 text-sm text-ink-muted">
                  {databricksOnly
                    ? "ACELO runs inside this Databricks workspace and connects with the Databricks App identity."
                    : "Settings for the connection you are currently working in. Switch platform from the selector in the sidebar."}
                </p>
              </div>
              {!runtime.databricks_app && !databricksOnly && (
                <button type="button" className="btn-secondary" onClick={() => setAddOpen(true)}>
                  <Plus size={15} />
                  Add connection
                </button>
              )}
            </div>
          )}

          {platform === "file" ? (
            <div className="mt-6 rounded-md border border-panel-border bg-canvas-raised p-4">
              <p className="text-sm font-medium text-ink">No setup needed for file analysis</p>
              <p className="mt-1 text-sm text-ink-muted">
                Upload a Cluster CSV directly in the AI Agent. It is analysed by the ACELO Cluster
                optimizer without any platform connection.
              </p>
              <Link
                to="/agent"
                className="mt-3 inline-flex items-center gap-2 rounded-md border border-brand-500 bg-white px-4 py-2 text-sm font-medium text-brand-500 hover:bg-brand-50"
              >
                <Upload size={16} />
                Go to AI Agent
              </Link>
            </div>
          ) : (
            <>
              {isFabric && (
                <div className="mt-6">
                  <p className="mb-3 text-sm font-medium text-ink">Authentication Method</p>
                  <div className="grid gap-4 md:grid-cols-2">
                    {(
                      [
                        {
                          mode: "user" as AuthMode,
                          title: "Microsoft Account",
                          blurb:
                            "Sign in with your organization Microsoft account. ACELO uses your delegated Fabric permissions.",
                        },
                        {
                          mode: "service_principal" as AuthMode,
                          title: "Service Principal",
                          blurb: "ACELO authenticates itself for unattended background runs.",
                        },
                      ]
                    ).map((option) => (
                      <label
                        key={option.mode}
                        className={`cursor-pointer rounded-md border p-4 transition ${
                          authMode === option.mode
                            ? "border-brand-500 bg-brand-500/5 ring-1 ring-brand-500/30"
                            : "border-panel-border bg-white hover:border-brand-500/50"
                        }`}
                      >
                        <span className="flex items-start gap-3">
                          <input
                            type="radio"
                            name="fabric-auth-mode"
                            className="mt-1 accent-brand-500"
                            checked={authMode === option.mode}
                            onChange={() => {
                              setAuthMode(option.mode);
                              setTestResult(null);
                              setDiscovery(null);
                              setError(null);
                            }}
                          />
                          <span>
                            <span className="block text-sm font-semibold text-ink">{option.title}</span>
                            <span className="mt-1 block text-xs text-ink-muted">{option.blurb}</span>
                          </span>
                        </span>
                      </label>
                    ))}
                  </div>
                </div>
              )}

              {isUserMode && (
                <div className="mt-5 rounded-md border border-panel-border bg-canvas-raised p-4">
                  {!isMsalConfigured() ? (
                    <p className="text-sm text-signal-medium">
                      Microsoft sign-in is not configured for this deployment. Set
                      <span className="font-mono"> VITE_ACELO_MSAL_CLIENT_ID </span>
                      and
                      <span className="font-mono"> VITE_ACELO_MSAL_TENANT_ID </span>
                      in the frontend <span className="font-mono">.env</span>, then restart the dev
                      server.
                    </p>
                  ) : account ? (
                    <div className="flex items-center justify-between gap-4">
                      <div>
                        <p className="text-sm font-medium text-ink">Signed in as:</p>
                        <p className="mt-0.5 text-sm text-ink-muted">{describeAccount(account)}</p>
                      </div>
                      <button
                        type="button"
                        onClick={() => void handleSignOut()}
                        className="rounded-md border border-panel-border bg-white px-4 py-2 text-sm text-ink hover:bg-canvas-raised"
                      >
                        Sign out
                      </button>
                    </div>
                  ) : (
                    <>
                      <p className="text-sm text-ink-muted">
                        Sign in with your organization Microsoft account that has access to this
                        Fabric workspace.
                      </p>
                      <button
                        type="button"
                        onClick={() => void handleSignIn()}
                        disabled={signingIn || inProgress !== InteractionStatus.None}
                        className="btn-primary mt-3 "
                      >
                        {signingIn ? <Loader2 size={16} className="animate-spin" /> : <LogIn size={16} />}
                        {signingIn || inProgress !== InteractionStatus.None
                          ? "Signing in..."
                          : "Sign in with Microsoft"}
                      </button>
                    </>
                  )}
                </div>
              )}

              <div className="mt-6 grid gap-5 md:grid-cols-2">
                <Field
                  label="Environment name"
                  value={form.name}
                  onChange={(v) => setForm({ ...form, name: v })}
                  placeholder={isFabric ? "Fabric Production" : "Databricks Production"}
                />

                {appManaged ? (
                  <div className="sm:col-span-2">
                    <p className="text-sm font-medium text-ink">Workspace</p>
                    <p className="mt-1 break-all font-mono text-xs text-ink-muted">
                      {runtime.workspace_host}
                    </p>
                    <p className="mt-3 text-sm text-ink-muted">
                      ACELO is running as a Databricks App and authenticates with the App's own
                      identity in this workspace. There is no workspace URL or access token to
                      enter, and no credential is stored.
                    </p>
                  </div>
                ) : isFabric ? (
                  <>
                    {/* Tenant / Client / Secret are Service Principal only. */}
                    {!isUserMode && (
                      <Field
                        label="Tenant ID"
                        value={form.tenantId}
                        onChange={(v) => setForm({ ...form, tenantId: v })}
                        placeholder="Azure AD tenant ID"
                      />
                    )}
                    <Field
                      label="Workspace ID"
                      value={form.workspaceId}
                      onChange={(v) => setForm({ ...form, workspaceId: v })}
                      placeholder="Fabric workspace ID"
                    />
                    {!isUserMode && (
                      <Field
                        label="Client ID"
                        value={form.clientId}
                        onChange={(v) => setForm({ ...form, clientId: v })}
                        placeholder="Service principal application ID"
                      />
                    )}
                  </>
                ) : (
                  <Field
                    label="Workspace URL"
                    value={form.endpoint}
                    onChange={(v) => setForm({ ...form, endpoint: v })}
                    placeholder="https://adb-xxxxxxxx.azuredatabricks.net"
                  />
                )}

                {!isUserMode && !appManaged && (
                  <Field
                    label={isFabric ? "Client Secret" : "Access Token"}
                    type="password"
                    value={form.clientSecret}
                    onChange={(v) => setForm({ ...form, clientSecret: v })}
                    placeholder={environment ? "Leave blank to keep the stored value" : "Stored securely in the backend"}
                  />
                )}
              </div>

              <p className="mt-4 text-xs text-ink-muted">
                {isUserMode
                  ? "Your Microsoft sign-in stays in this browser session. ACELO passes a short-lived Fabric token to its backend per request and never stores it. Tenant and client ID come from the app configuration \u2014 you do not enter them."
                  : "Authentication is performed by the ACELO backend. Secrets are encrypted at rest and are never sent to the browser."}
              </p>

              <div className="mt-6 flex gap-3">
                <button
                  type="button"
                  onClick={handleTestConnection}
                  disabled={!canTest || stage !== "idle"}
                  className="btn-primary"
                >
                  {stage === "testing" && <Loader2 size={16} className="animate-spin" />}
                  {stage === "testing" ? "Testing connection..." : "Test Connection"}
                </button>

                {testResult && !testResult.connected && stage === "idle" && canTest && (
                  <button
                    type="button"
                    onClick={handleTestConnection}
                    className="inline-flex items-center gap-2 rounded-md border border-brand-500 bg-white px-5 py-2.5 text-sm font-medium text-brand-500 hover:bg-brand-50"
                  >
                    <RefreshCw size={16} />
                    Retry
                  </button>
                )}
              </div>
              {testBlockedReason && (
                <p data-testid="test-blocked-reason" className="mt-3 text-xs text-ink-muted">
                  {testBlockedReason}
                </p>
              )}
            </>
          )}
        </section>

        {error && (
          <div className="surface border-l-4 border-l-signal-high p-4">
            <div className="flex items-start gap-3">
              <AlertCircle size={18} className="mt-0.5 shrink-0 text-signal-high" />
              <div>
                <p className="text-sm font-medium text-ink">Request failed</p>
                <p className="mt-1 text-sm text-ink-muted">{error}</p>
              </div>
            </div>
          </div>
        )}

        {/* ---------------- Connection status ---------------- */}
        <section className="surface p-6">
          <h2 className="text-lg font-semibold text-ink">Connection Status</h2>

          {!testResult && stage !== "testing" && (
            <p className="mt-3 flex items-center gap-2 text-sm text-ink-muted">
              <span className="h-2 w-2 rounded-full bg-ink-faint" />
              Not Connected
            </p>
          )}

          {stage === "testing" && (
            <p className="mt-3 flex items-center gap-2 text-sm text-ink-muted">
              <Loader2 size={14} className="animate-spin text-brand-500" />
              Testing connection...
            </p>
          )}

          {testResult && stage === "idle" && (
            <div className="mt-3">
              {connected ? (
                <>
                  <p className="flex items-center gap-2 text-sm font-medium text-signal-low">
                    <CheckCircle2 size={16} />
                    Connected
                  </p>
                  <dl className="mt-4 grid gap-3 sm:grid-cols-2">
                    <Detail label="Platform" value={PLATFORM_LABELS[platform]} />
                    <Detail label="Workspace" value={testResult.workspace_name ?? "—"} />
                    <Detail label="Workspace ID" value={maskId(testResult.workspace_id)} />
                    <Detail
                      label="Last verified"
                      value={
                        testResult.last_verified_at
                          ? new Date(testResult.last_verified_at).toLocaleString()
                          : "—"
                      }
                    />
                    {isUserMode && account && (
                      <Detail label="Signed in as" value={describeAccount(account)} />
                    )}
                  </dl>
                </>
              ) : (
                <>
                  <p className="flex items-center gap-2 text-sm font-medium text-signal-high">
                    <AlertCircle size={16} />
                    Connection Failed
                  </p>
                  <p className="mt-2 text-sm text-ink-muted">
                    <span className="font-medium text-ink">Reason: </span>
                    {testResult.message}
                  </p>
                  {testResult.error_code && (
                    <p className="mt-1 font-mono text-xs text-ink-faint">{testResult.error_code}</p>
                  )}
                </>
              )}
            </div>
          )}
        </section>

        {/* ---------------- Discovery ---------------- */}
        <section className="surface p-6">
          <h2 className="flex items-center gap-2 text-lg font-semibold text-ink">
            Environment Discovery
            {discovered && stage !== "discovering" && (
              <span data-testid="discovery-complete" className="flex items-center gap-1 text-sm font-medium text-signal-low">
                <CheckCircle2 size={16} /> Complete
              </span>
            )}
          </h2>
          <p className="mt-1 text-sm text-ink-muted">
            Lists the resources in the connected workspace. This is read-only — nothing is executed.
          </p>

          <button
            type="button"
            onClick={handleDiscover}
            disabled={discoverBlockedReason !== null || stage !== "idle"}
            className="btn-primary mt-4 "
          >
            {stage === "discovering" && <Loader2 size={16} className="animate-spin" />}
            {stage === "discovering" ? "Discovering environment..." : "Discover Environment"}
          </button>

          {discoverBlockedReason && (
            <p className="mt-3 text-xs text-ink-muted">{discoverBlockedReason}</p>
          )}

          {discovery && stage === "idle" && (
            <div className="mt-5">
              {discovered ? (
                <>
                  <p className="flex items-center gap-2 text-sm font-medium text-signal-low">
                    <CheckCircle2 size={16} />
                    Workspace connected — {discovery.workspace?.name}
                  </p>

                  <div className="mt-4 grid gap-3 sm:grid-cols-3">
                    {orderedCounts(discovery.counts).map(([type, count]) => (
                      <div key={type} className="rounded-md border border-panel-border bg-white p-3">
                        <p className="tabular text-xl font-semibold text-ink">{count}</p>
                        <p className="text-xs text-ink-muted">{COUNT_LABELS[type] ?? type}</p>
                      </div>
                    ))}
                  </div>

                  {/* Per-resource-type outcome. A type that could not be read
                      is reported as such — never folded into "empty". The
                      wording comes from the shared explainer so this page and
                      Compute discovery say the same thing about the same code,
                      and neither prints a raw enum at the user. */}
                  {(discovery.resource_states ?? []).some(
                    (s) => s.state !== "SUCCESS_WITH_RESOURCES" && s.state !== "SUCCESS_EMPTY"
                  ) && (
                    <ul className="mt-4 space-y-2" data-testid="discovery-states">
                      {(discovery.resource_states ?? [])
                        .filter(
                          (s) => s.state !== "SUCCESS_WITH_RESOURCES" && s.state !== "SUCCESS_EMPTY"
                        )
                        .map((s) => {
                          const explanation = explainState(s.state);
                          return (
                            <li key={s.resource_type} className="flex flex-wrap gap-x-2 text-sm">
                              <span className="font-medium text-ink">
                                {RESOURCE_TYPE_LABELS[s.resource_type] ?? s.resource_type}
                              </span>
                              <span
                                className={
                                  explanation.kind === "error"
                                    ? "text-signal-high"
                                    : explanation.kind === "unavailable"
                                      ? "text-signal-medium"
                                      : "text-ink-muted"
                                }
                              >
                                {explanation.title}
                              </span>
                              <span className="w-full text-xs text-ink-faint">
                                {explanation.detail}
                              </span>
                            </li>
                          );
                        })}
                    </ul>
                  )}

                  {/* "Empty" is claimed ONLY when every supported call succeeded
                      and returned zero. With no per-type states (other
                      platforms), fall back to the item count as before. */}
                  {discovery.items.length === 0 &&
                    ((discovery.resource_states ?? []).length === 0
                      ? true
                      : (discovery.resource_states ?? []).every(
                          (s) => s.state === "SUCCESS_EMPTY"
                        )) && (
                      <p className="mt-4 text-sm text-ink-muted">
                        This workspace is empty — the platform reported no items.
                      </p>
                    )}

                  {discovery.items.length > 0 && (
                    <div className="mt-5 overflow-x-auto">
                      <table className="w-full text-left text-sm">
                        <thead>
                          <tr className="border-b border-panel-border text-xs uppercase text-ink-muted">
                            <th className="py-2 pr-4 font-medium">Name</th>
                            <th className="py-2 pr-4 font-medium">Type</th>
                          </tr>
                        </thead>
                        <tbody>
                          {discovery.items.map((item) => (
                            <tr key={`${item.type}-${item.id}`} className="border-b border-panel-border/60">
                              <td className="py-2 pr-4 text-ink">{item.display_name}</td>
                              <td className="py-2 pr-4 text-ink-muted">{item.type}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </>
              ) : (
                <>
                  <p className="flex items-center gap-2 text-sm font-medium text-signal-high">
                    <AlertCircle size={16} />
                    Discovery Failed
                  </p>
                  <p className="mt-2 text-sm text-ink-muted">{discovery.message}</p>
                  <button
                    type="button"
                    onClick={handleDiscover}
                    className="mt-4 inline-flex items-center gap-2 rounded-md border border-brand-500 bg-white px-5 py-2.5 text-sm font-medium text-brand-500 hover:bg-brand-50"
                  >
                    <RefreshCw size={16} />
                    Retry
                  </button>
                </>
              )}
            </div>
          )}
        </section>

        {/* ---------------- ACELO installation ---------------- */}
        <section className="surface p-6">
          <div className="flex items-start gap-3">
            <Package size={19} className="mt-0.5 text-brand-500" />
            <div>
              <h2 className="flex items-center gap-2 text-lg font-semibold text-ink">
                ACELO Installation
                {installed && stage !== "provisioning" && (
                  <span data-testid="install-complete" className="flex items-center gap-1 text-sm font-medium text-signal-low">
                    <CheckCircle2 size={16} /> Installed
                  </span>
                )}
              </h2>
              <p className="mt-1 text-sm text-ink-muted">
                Deploys ACELO&apos;s optimization notebooks into your workspace under the{" "}
                <span className="font-medium text-ink">{provisioning?.namespace ?? "ACELO"}</span>{" "}
                namespace. Your existing items are never modified or deleted.
              </p>
            </div>
          </div>

          <p className="mt-4 text-sm">
            <span className="text-ink-muted">Status: </span>
            <span
              className={
                installed
                  ? "font-medium text-signal-low"
                  : provisioning?.status === "FAILED"
                    ? "font-medium text-signal-high"
                    : "font-medium text-ink"
              }
            >
              {stage === "provisioning"
                ? "Installing"
                : (provisioning?.status ?? "NOT_INSTALLED").replace(/_/g, " ")}
            </span>
            {provisioning?.package_version_installed && (
              <span className="ml-2 text-xs text-ink-faint">
                v{provisioning.package_version_installed}
              </span>
            )}
          </p>

          {/* Real backend step, not an animation. */}
          {stage === "provisioning" && (
            <p className="mt-2 flex items-center gap-2 text-sm text-ink-muted">
              <Loader2 size={14} className="animate-spin text-brand-500" />
              {provisioning?.step ?? "Preparing..."}
            </p>
          )}

          {provisioning && provisioning.missing_assets.length > 0 && (
            <div className="mt-4 flex items-start gap-3 rounded-md border-l-4 border-l-signal-medium bg-canvas-raised p-4">
              <AlertCircle size={18} className="mt-0.5 shrink-0 text-signal-medium" />
              <div>
                <p className="text-sm font-medium text-ink">Some optimization assets are unavailable</p>
                <p className="mt-1 text-sm text-ink-muted">
                  This ACELO build does not include a notebook for:{" "}
                  {provisioning.missing_assets.join(", ")}. Those capabilities cannot be deployed.
                </p>
              </div>
            </div>
          )}

          {provisioning && (
            <div className="mt-4 space-y-2">
              {Object.entries(provisioning.domains).map(([domain, d]) => (
                <div
                  key={domain}
                  className="flex items-start justify-between gap-4 border-b border-panel-border/60 py-2"
                >
                  <div className="min-w-0">
                    <p className="text-sm text-ink capitalize">{domain}</p>
                    {d.platform_resource_id && (
                      <p className="mt-0.5 break-all font-mono text-[11px] text-ink-faint">
                        {d.display_name} · {d.platform_resource_id}
                      </p>
                    )}
                    {d.error_code && (
                      <p className="mt-0.5 text-xs text-signal-medium">
                        {describeProvisioningError(d.error_code, d.message)}
                      </p>
                    )}
                  </div>
                  <span className="shrink-0 text-xs">
                    {d.ready ? (
                      <span className="flex items-center gap-1 text-signal-low">
                        <CheckCircle2 size={14} /> Ready
                      </span>
                    ) : (
                      <span className="text-ink-faint">
                        {d.asset_available ? "Not deployed" : "Asset missing"}
                      </span>
                    )}
                  </span>
                </div>
              ))}
            </div>
          )}

          {provisioning?.status === "FAILED" && (
            <div className="mt-4 flex items-start gap-3 rounded-md border-l-4 border-l-signal-high bg-canvas-raised p-4">
              <AlertCircle size={18} className="mt-0.5 shrink-0 text-signal-high" />
              <div>
                <p className="text-sm font-medium text-ink">Provisioning Failed</p>
                <p className="mt-1 text-sm text-ink-muted">
                  {describeProvisioningError(provisioning.error_code, provisioning.message)}
                </p>
              </div>
            </div>
          )}

          <div className="mt-5 flex gap-3">
            <button
              type="button"
              onClick={() => void handleProvision()}
              disabled={provisionBlockedReason !== null || stage !== "idle"}
              className="btn-primary"
            >
              {stage === "provisioning" && <Loader2 size={16} className="animate-spin" />}
              {provisioning?.status === "UPDATE_AVAILABLE"
                ? "Update ACELO in Fabric"
                : provisioning?.status === "INSTALLED"
                  ? "Re-run ACELO Setup"
                  : "Set up ACELO in Fabric"}
            </button>

            {provisioning?.status === "FAILED" && stage === "idle" && (
              <button
                type="button"
                onClick={() => void handleProvision()}
                className="inline-flex items-center gap-2 rounded-md border border-brand-500 bg-white px-5 py-2.5 text-sm font-medium text-brand-500 hover:bg-brand-50"
              >
                <RefreshCw size={16} />
                Retry
              </button>
            )}
          </div>

          {provisionBlockedReason && stage === "idle" && (
            <p data-testid="provision-blocked-reason" className="mt-3 text-xs text-ink-muted">
              {provisionBlockedReason}
            </p>
          )}
          {provisioning && discovered && (
            provisioning.default_lakehouse ? (
              <div data-testid="default-lakehouse" className="mt-3 text-xs text-ink-muted">
                <p>
                  Default Lakehouse for the notebook:{" "}
                  <span className="font-medium text-ink">
                    {provisioning.default_lakehouse.name || provisioning.default_lakehouse.id}
                  </span>
                </p>
                {/* What Fabric reported back after the last setup — evidence, not intent. */}
                {provisioning.lakehouse_binding?.status === "VERIFIED" && (
                  <p className="mt-1 flex items-center gap-1 text-signal-low">
                    <CheckCircle2 size={12} /> Verified as the notebook&apos;s default Lakehouse in
                    Fabric.
                  </p>
                )}
                {provisioning.lakehouse_binding &&
                  provisioning.lakehouse_binding.status !== "VERIFIED" && (
                    <p data-testid="lakehouse-binding-problem" className="mt-1 text-signal-medium">
                      {provisioning.lakehouse_binding.status === "MISSING"
                        ? "The deployed notebook has NO default Lakehouse. Pipeline runs will fail with \"No default context found\"."
                        : provisioning.lakehouse_binding.status === "MISMATCH"
                          ? `The deployed notebook's default Lakehouse is ${provisioning.lakehouse_binding.bound_name ?? provisioning.lakehouse_binding.bound_id}, not the configured one.`
                          : "The notebook's default Lakehouse could not be verified."}{" "}
                      Click &quot;{provisioning.status === "UPDATE_AVAILABLE" ? "Update" : "Re-run"}
                      &quot; below to redeploy with the binding.
                    </p>
                  )}
                {!provisioning.lakehouse_binding && (
                  <p className="mt-1">Not yet verified — run setup to deploy and read back the binding.</p>
                )}
              </div>
            ) : (
              <p data-testid="no-lakehouse-warning" className="mt-3 text-xs text-signal-medium">
                No default Lakehouse can be bound: discovery found none matching the Cluster
                settings in this workspace. Pipeline runs will fail with &quot;No default context
                found&quot;. Enter the Lakehouse ID (and its workspace ID if it is in another
                workspace) in Cluster Settings, then re-run setup.
              </p>
            )
          )}
          {provisioning && (
            <div className="mt-3 space-y-1 text-xs text-ink-muted">
              <p data-testid="fabric-environment">
                Spark libraries:{" "}
                {provisioning.fabric_environment ? (
                  <span className="font-medium text-ink">
                    Fabric Environment {provisioning.fabric_environment.name}
                  </span>
                ) : (
                  <span>
                    workspace default Environment. The notebook needs xgboost there, or set a
                    Fabric Environment ID in Cluster Settings and re-run setup.
                  </span>
                )}
              </p>
              <p data-testid="execution-path">
                Execution:{" "}
                <span className="font-medium text-ink">
                  {provisioning.execution_type === "pipeline" ? "Pipeline" : "Direct notebook"}
                </span>
                {provisioning.pipeline && (
                  <>
                    {" · "}
                    {provisioning.pipeline.name}:{" "}
                    {provisioning.pipeline.ready ? (
                      <span className="text-signal-low">deployed</span>
                    ) : (
                      <span className="text-signal-medium">
                        {provisioning.pipeline.message ?? "not deployed"}
                      </span>
                    )}
                  </>
                )}
              </p>
            </div>
          )}
          {provisioning?.status === "UPDATE_AVAILABLE" && (
            <p className="mt-3 text-xs text-signal-medium">
              The notebook deployed in Fabric is v{provisioning.package_version_installed}; this ACELO
              build ships v{provisioning.package_version_available}. Update it so runs include the
              runtime parameter trace.
            </p>
          )}
        </section>

        {/* ---------------- Optimization resources (per-domain registry) ----------------
            Users never fill these in to run anything: the AI Agent picks the domain from the
            request and the backend loads that domain's mapping. The Cluster form below is the
            administrator's Cluster mapping, kept inside "Advanced". */}
        <OptimizationResources environmentId={environment?.id ?? null} refreshKey={savedState}
                               onAccess={setCanConfigure}>
        <fieldset data-testid="cluster-mapping" disabled={!canConfigure} className="min-w-0">
          <h3 className="flex items-center gap-2 text-base font-semibold text-ink">
            Cluster Settings
            {environment && clusterMissing.length === 0 && connected && (
              <span className="flex items-center gap-1 text-sm font-medium text-signal-low">
                <CheckCircle2 size={16} /> Configured
              </span>
            )}
          </h3>
          <p className="mt-1 text-sm text-ink-muted">
            Cluster resource mapping: what the Cluster notebook reads and writes. Passed to the
            Cluster notebook only, as runtime parameters. Names only — no credentials.
          </p>

          <div className="mt-5 grid gap-5 md:grid-cols-2">
            <Field
              label="Source table"
              value={clusterSettings.source_table}
              onChange={(v) => setClusterSettings({ ...clusterSettings, source_table: v })}
              placeholder={REQUIRED_PLACEHOLDER}
            />
            <Field
              label="Result table"
              value={clusterSettings.result_table}
              onChange={(v) => setClusterSettings({ ...clusterSettings, result_table: v })}
              placeholder={REQUIRED_PLACEHOLDER}
            />
            <Field
              label="Lakehouse"
              value={clusterSettings.lakehouse_database}
              onChange={(v) => setClusterSettings({ ...clusterSettings, lakehouse_database: v })}
              placeholder={OPTIONAL_PLACEHOLDER}
            />
            <Field
              label="Table schema"
              value={clusterSettings.source_schema}
              onChange={(v) =>
                // One schema for source and result: a schema-enabled Lakehouse uses dbo for both.
                setClusterSettings({ ...clusterSettings, source_schema: v, result_schema: v })
              }
              placeholder={`${OPTIONAL_PLACEHOLDER} (e.g. dbo for a schema-enabled Lakehouse)`}
            />
            <Field
              label="Lakehouse ID"
              value={clusterSettings.lakehouse_id}
              onChange={(v) => setClusterSettings({ ...clusterSettings, lakehouse_id: v })}
              placeholder={`${OPTIONAL_PLACEHOLDER} (bound as the notebook's default Lakehouse)`}
            />
            <Field
              label="Lakehouse workspace ID"
              value={clusterSettings.lakehouse_workspace_id}
              onChange={(v) => setClusterSettings({ ...clusterSettings, lakehouse_workspace_id: v })}
              placeholder={`${OPTIONAL_PLACEHOLDER} (only if the Lakehouse is in another workspace)`}
            />
            <Field
              label="Approval tracking table"
              value={clusterSettings.approval_tracking_table}
              onChange={(v) => setClusterSettings({ ...clusterSettings, approval_tracking_table: v })}
              placeholder={`${OPTIONAL_PLACEHOLDER} (Delta table the Approvals tab reads via OneLake)`}
            />
            <Field
              label="SQL analytics endpoint"
              value={clusterSettings.sql_endpoint}
              onChange={(v) => setClusterSettings({ ...clusterSettings, sql_endpoint: v })}
              placeholder={`${OPTIONAL_PLACEHOLDER} (only for the Results page; not used by Approvals)`}
            />
            <Field
              label="Fabric Environment ID"
              value={clusterSettings.fabric_environment_id}
              onChange={(v) => setClusterSettings({ ...clusterSettings, fabric_environment_id: v })}
              placeholder={`${OPTIONAL_PLACEHOLDER} (Spark libraries such as xgboost)`}
            />
            <label className="block">
              <span className="mb-2 block text-sm font-medium text-ink">Execution type</span>
              <select
                aria-label="Execution type"
                value={clusterSettings.execution_type || "notebook"}
                onChange={(e) =>
                  setClusterSettings({ ...clusterSettings, execution_type: e.target.value })
                }
                className="w-full rounded-md border border-panel-border bg-white px-3 py-2.5 text-sm text-ink outline-none focus:border-brand-500 focus:ring-1 focus:ring-brand-500"
              >
                <option value="notebook">Notebook</option>
                <option value="pipeline">Pipeline</option>
              </select>
            </label>
            {clusterSettings.execution_type === "pipeline" && (
              <label className="block">
                <span className="mb-2 block text-sm font-medium text-ink">Pipeline</span>
                {pipelines.length === 0 ? (
                  <p data-testid="no-pipelines" className="text-sm text-signal-medium">
                    No pipeline found in this workspace. Run &quot;Set up ACELO in Fabric&quot; to
                    deploy ACELO_Cluster_Optimization_Pipeline, then Discover Environment.
                  </p>
                ) : (
                  <select
                    aria-label="Pipeline"
                    value={clusterSettings.pipeline_id || defaultPipelineId}
                    onChange={(e) =>
                      setClusterSettings({ ...clusterSettings, pipeline_id: e.target.value })
                    }
                    className="w-full rounded-md border border-panel-border bg-white px-3 py-2.5 text-sm text-ink outline-none focus:border-brand-500 focus:ring-1 focus:ring-brand-500"
                  >
                    {pipelines.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name}
                        {p.managed ? " (deployed by ACELO)" : ""}
                      </option>
                    ))}
                  </select>
                )}
              </label>
            )}
          </div>

          <div className="mt-5 flex items-center gap-3">
            <button
              type="button"
              onClick={() => void handleSaveClusterSettings()}
              disabled={settingsBlockedReason !== null || stage !== "idle"}
              className="btn-primary"
            >
              {stage === "saving" && <Loader2 size={16} className="animate-spin" />}
              Save Cluster Settings
            </button>
          </div>

          {/* Confirmation reflects what the BACKEND persisted, not the form. */}
          {savedState && stage === "idle" && (
            <div data-testid="settings-saved" className="mt-4 rounded-md border border-panel-border bg-canvas-raised p-4 text-sm">
              <p className="flex items-center gap-2 font-medium text-signal-low">
                <CheckCircle2 size={16} /> Cluster settings saved
              </p>
              <dl className="mt-2 grid gap-1 text-ink-muted sm:grid-cols-2">
                <SavedRow
                  label="Execution type"
                  value={savedState.execution?.execution_type === "pipeline" ? "Pipeline" : "Notebook"}
                />
                {savedState.execution?.execution_type === "pipeline" && (
                  <SavedRow
                    label="Pipeline"
                    value={savedState.execution.pipeline?.name ?? savedState.execution.pipeline?.id ?? null}
                  />
                )}
                <SavedRow label="Source" value={savedState.settings.source_table || null} />
                <SavedRow label="Result" value={savedState.settings.result_table || null} />
                <SavedRow label="Lakehouse" value={savedState.settings.lakehouse_database || null} />
                <SavedRow label="Schema" value={savedState.settings.source_schema || null} />
                <SavedRow label="SQL analytics endpoint" value={savedState.settings.sql_endpoint || null} />
              </dl>
            </div>
          )}
          {saveError && stage === "idle" && (
            <div data-testid="settings-save-failed" className="mt-4 flex items-start gap-3 rounded-md border-l-4 border-l-signal-high bg-canvas-raised p-4">
              <AlertCircle size={18} className="mt-0.5 shrink-0 text-signal-high" />
              <div>
                <p className="text-sm font-medium text-ink">Failed to save Cluster settings</p>
                <p className="mt-1 text-sm text-ink-muted">{saveError}</p>
              </div>
            </div>
          )}
          {settingsBlockedReason ? (
            <p className="mt-3 text-xs text-ink-muted">{settingsBlockedReason}</p>
          ) : (
            clusterMissing.length > 0 && (
              <p data-testid="cluster-settings-missing" className="mt-3 text-xs text-signal-medium">
                Required configuration missing: {clusterMissing.join(", ")}.
              </p>
            )
          )}
        </fieldset>
        </OptimizationResources>

        {/* ---------------- Readiness ---------------- */}
        <section className="surface p-6">
          <h2 className="text-lg font-semibold text-ink">Readiness</h2>
          <ul className="mt-4 space-y-2">
            <ReadinessRow label="Authentication" ok={connected} />
            <ReadinessRow label="Workspace Access" ok={connected && Boolean(testResult?.workspace_name)} />
            <ReadinessRow label="Environment Discovery" ok={discovered} />
            <ReadinessRow label="ACELO Package" ok={installed} />
            <ReadinessRow
              label="Cluster Settings"
              ok={Boolean(environment) && connected && clusterMissing.length === 0}
            />
            <ReadinessRow label="Cluster" ok={Boolean(provisioning?.domains?.cluster?.ready)} />
            <ReadinessRow label="Query" ok={Boolean(provisioning?.domains?.query?.ready)} />
            <ReadinessRow label="Storage" ok={Boolean(provisioning?.domains?.storage?.ready)} />
          </ul>

          <div className="mt-5 rounded-md border border-panel-border bg-canvas-raised p-4">
            <p className="text-sm font-medium text-ink">
              Status:{" "}
              <span
                data-testid="overall-readiness"
                className={readyForAnalysis ? "text-signal-low" : "text-ink-muted"}
              >
                {readyForAnalysis ? "READY FOR ANALYSIS" : "NOT READY"}
              </span>
            </p>
            {/*
              A Cluster-only environment is a legitimate, fully usable state.
              Reporting it separately means a missing Query or Storage notebook
              does not read as "nothing works", without the overall status
              claiming a readiness the environment does not have.
            */}
            <p className="mt-2 text-sm text-ink-muted">
              Cluster optimization:{" "}
              <span
                data-testid="cluster-readiness"
                className={clusterReady ? "text-signal-low" : "text-ink-muted"}
              >
                {clusterReady ? "READY TO RUN" : "NOT READY"}
              </span>
            </p>
            {!clusterReady && readiness?.cluster_blocked_reason && (
              <p data-testid="cluster-blocked-reason" className="mt-1 text-xs text-ink-muted">
                {readiness.cluster_blocked_reason}
              </p>
            )}
            {clusterReady && (
              <button
                type="button"
                onClick={() => navigate("/agent")}
                className="btn-primary mt-4 "
              >
                Go to AI Agent
              </button>
            )}
          </div>
        </section>

        <div className="surface p-5">
          <div className="flex items-start gap-3">
            <ShieldCheck size={19} className="mt-0.5 text-ink-muted" />
            <div>
              <p className="text-sm font-medium text-ink">ACELO execution policy</p>
              <p className="mt-1 text-sm leading-relaxed text-ink-muted">
                Connection and discovery are read-only. ACELO does not run analysis or modify your
                environment during setup.
              </p>
            </div>
          </div>
        </div>
      </div>
      {/* Choosing a platform happens HERE and only here — the deliberate act of
          connecting something new, kept apart from the settings of the
          connection already in use. */}
      <Modal
        open={addOpen}
        onClose={() => setAddOpen(false)}
        title="Add connection"
        subtitle="Choose the platform to connect. Your current connection is unaffected."
      >
        <ul className="space-y-2">
          {(Object.keys(PLATFORM_LABELS) as EnvironmentPlatform[]).map((p) => {
            const Icon = p === "fabric" ? Cloud : p === "databricks" ? Database : Upload;
            return (
              <li key={p}>
                <button
                  type="button"
                  onClick={() => beginAddConnection(p)}
                  className="flex w-full items-center gap-3 rounded-md border border-panel-border bg-white p-4 text-left transition hover:border-brand-500/50"
                >
                  <Icon size={18} className="shrink-0 text-brand-500" />
                  <span>
                    <span className="block text-sm font-semibold text-ink">
                      {PLATFORM_LABELS[p]}
                    </span>
                    <span className="mt-0.5 block text-xs text-ink-muted">
                      {p === "file"
                        ? "Analyse an uploaded Cluster CSV — no connection needed."
                        : `Connect a ${PLATFORM_LABELS[p]} workspace.`}
                    </span>
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      </Modal>
    </Layout>
  );
}

function orderedCounts(counts: Record<string, number>): [string, number][] {
  const entries = Object.entries(counts);
  return entries.sort((a, b) => {
    const ai = COUNT_ORDER.indexOf(a[0]);
    const bi = COUNT_ORDER.indexOf(b[0]);
    return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
  });
}

/** Workspace IDs are not secret, but there's no reason to show them in full. */
function maskId(id: string | null | undefined): string {
  if (!id) return "—";
  if (id.length <= 8) return id;
  return `${id.slice(0, 4)}••••${id.slice(-4)}`;
}

function Detail({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-ink-muted">{label}</dt>
      <dd className="mt-1 text-sm text-ink">{value}</dd>
    </div>
  );
}

const REQUIRED_PLACEHOLDER = "Required — not configured";
const OPTIONAL_PLACEHOLDER = "Optional — not configured";

/** One persisted setting; an unset value reads "Not configured", never a sample. */
function SavedRow({ label, value }: { label: string; value: string | null }) {
  return (
    <div className="flex gap-2">
      <dt>{label}:</dt>
      <dd className={value ? "font-medium text-ink" : "italic"}>{value ?? "Not configured"}</dd>
    </div>
  );
}

function ReadinessRow({ label, ok }: { label: string; ok: boolean }) {
  return (
    <li className="flex items-center justify-between border-b border-panel-border/60 py-2 text-sm">
      <span className="text-ink">{label}</span>
      {ok ? (
        <CheckCircle2 size={16} className="text-signal-low" />
      ) : (
        <span className="h-2 w-2 rounded-full bg-ink-faint" />
      )}
    </li>
  );
}

function Field({
  label,
  value,
  onChange,
  placeholder,
  type = "text",
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  type?: string;
}) {
  return (
    <label className="block">
      <span className="mb-2 block text-sm font-medium text-ink">{label}</span>
      <input
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        autoComplete={type === "password" ? "new-password" : "off"}
        className="w-full rounded-md border border-panel-border bg-white px-3 py-2.5 text-sm text-ink outline-none placeholder:text-ink-muted focus:border-brand-500 focus:ring-1 focus:ring-brand-500"
      />
    </label>
  );
}
