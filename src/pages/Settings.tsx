import React, { useEffect, useMemo, useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  Cloud,
  Database,
  Loader2,
  LogIn,
  Package,
  RefreshCw,
  ShieldCheck,
  Upload,
} from "lucide-react";
import { useMsal } from "@azure/msal-react";
import { InteractionStatus } from "@azure/msal-browser";
import type { AccountInfo } from "@azure/msal-browser";
import Layout from "../components/Layout";
import { isMsalConfigured, REDIRECT_URI_SETUP_HINT } from "../authConfig";
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
  getProvisioningStatus,
  getReadiness2,
  listEnvironments,
  provisionEnvironment,
  testEnvironment,
  updateEnvironment,
  type ConnectionTestResult,
  type DiscoveryResult,
  type Environment,
  type AuthMode,
  type EnvironmentPlatform,
  type ProvisioningState,
  type Readiness2,
} from "../services/environmentApi";

/**
 * Phase 1 — Environment Setup.
 *
 * Connection state shown here is ALWAYS the backend's verdict. There is no
 * setTimeout, no optimistic "Connected", and no demo fallback: if the API call
 * fails the user sees the failure. Credentials are write-only — the secret is
 * posted once and never read back.
 */

type Stage = "idle" | "testing" | "discovering" | "provisioning";

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

const COUNT_LABELS: Record<string, string> = {
  Folder: "Folders",
  Notebook: "Notebooks",
  Lakehouse: "Lakehouses",
  SQLEndpoint: "SQL Endpoints",
  Warehouse: "Warehouses",
  Experiment: "Experiments",
  Cluster: "Clusters",
  Job: "Jobs",
};

export default function Settings() {
  // `accounts` from useMsal is reactive: it updates when MSAL resolves a sign-in,
  // including one completed by handleRedirectPromise() during startup. The
  // previous version seeded a useState initializer once at mount, so an account
  // that arrived afterwards was never picked up and the UI stayed signed-out.
  const { instance, accounts, inProgress } = useMsal();
  const [platform, setPlatform] = useState<EnvironmentPlatform>("fabric");
  const [authMode, setAuthMode] = useState<AuthMode>("service_principal");
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

  // Restore the persisted environment so a page refresh keeps safe state.
  // Only non-secret fields come back from the API.
  useEffect(() => {
    listEnvironments()
      .then((envs) => {
        const existing = envs.find((e) => e.platform === platform);
        if (!existing) return;
        setEnvironment(existing);
        if (existing.auth_mode) setAuthMode(existing.auth_mode);
        void getProvisioningStatus(existing.id).then(setProvisioning).catch(() => {});
        void getReadiness2(existing.id).then(setReadiness).catch(() => {});
        setForm((f) => ({
          ...f,
          name: existing.name,
          tenantId: existing.tenant_id ?? "",
          workspaceId: existing.workspace_id ?? "",
        }));
        if (existing.status === "connected" || existing.status === "environment_ready") {
          setTestResult({
            platform: existing.platform,
            connected: true,
            workspace_id: existing.workspace_id,
            workspace_name: existing.workspace_name,
            message: "Connection previously verified",
            last_verified_at: existing.last_verified_at,
          });
        } else if (existing.last_error_message) {
          setTestResult({
            platform: existing.platform,
            connected: false,
            message: existing.last_error_message,
            error_code: existing.last_error_code,
          });
        }
      })
      .catch((e: unknown) => setError(e instanceof ApiError ? e.message : String(e)));
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
  const canTest = useMemo(() => {
    if (!form.name.trim()) return false;
    if (isFabric) {
      if (authMode === "user") {
        // Needs only a signed-in Microsoft account and a workspace ID — never
        // a secret, and never a stale auth snapshot.
        return Boolean(account && form.workspaceId.trim());
      }
      return Boolean(form.tenantId && form.workspaceId && form.clientId && (form.clientSecret || environment));
    }
    if (platform === "databricks") {
      return Boolean(form.endpoint && (form.clientSecret || environment));
    }
    return false;
  }, [form, isFabric, platform, environment, authMode, account]);

  async function persistEnvironment(): Promise<Environment> {
    const userMode = isFabric && authMode === "user";
    const payload = {
      name: form.name.trim(),
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
      if (result.connected) setForm((f) => ({ ...f, clientSecret: "" }));
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
      setReadiness(await getReadiness2(environment.id).catch(() => null));
    } catch (e: unknown) {
      if (e instanceof FabricAuthError) setError(e.message);
      else setError(e instanceof ApiError ? e.message : "Unexpected error setting up ACELO.");
    } finally {
      setStage("idle");
    }
  }

  const connected = testResult?.connected === true;
  const discovered = discovery?.discovered === true;
  // Mirrors the backend rule: connection + discovery are not enough. A verified
  // ACELO package with a ready Cluster notebook is required.
  const readyForAnalysis =
    connected &&
    discovered &&
    provisioning?.status === "INSTALLED" &&
    Boolean(provisioning?.domains?.cluster?.ready);

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
          <h2 className="text-lg font-semibold text-ink">Platform</h2>

          <div className="mt-4 grid gap-4 md:grid-cols-3">
            {(Object.keys(PLATFORM_LABELS) as EnvironmentPlatform[]).map((p) => {
              const Icon = p === "fabric" ? Cloud : p === "databricks" ? Database : Upload;
              const active = platform === p;
              return (
                <button
                  key={p}
                  type="button"
                  onClick={() => {
                    setPlatform(p);
                    setEnvironment(null);
                    setTestResult(null);
                    setDiscovery(null);
                    setProvisioning(null);
                    setError(null);
                  }}
                  className={`rounded-md border p-4 text-left transition ${
                    active
                      ? "border-brand-500 bg-brand-500/5 ring-1 ring-brand-500/30"
                      : "border-panel-border bg-white hover:border-brand-500/50"
                  }`}
                >
                  <Icon size={20} className="text-brand-500" />
                  <p className="mt-3 text-sm font-semibold text-ink">{PLATFORM_LABELS[p]}</p>
                </button>
              );
            })}
          </div>

          {platform === "file" ? (
            <div className="mt-6 rounded-md border border-panel-border bg-canvas-raised p-4">
              <p className="text-sm font-medium text-ink">File analysis is not available yet</p>
              <p className="mt-1 text-sm text-ink-muted">
                File-based environments are planned for a later phase. Connect Microsoft Fabric or
                Databricks to continue.
              </p>
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
                        className="mt-3 inline-flex items-center gap-2 rounded-md bg-brand-500 px-5 py-2.5 text-sm font-medium text-white hover:bg-brand-600 disabled:opacity-50"
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

                {isFabric ? (
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

                {!isUserMode && (
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
                  className="inline-flex items-center gap-2 rounded-md bg-brand-500 px-5 py-2.5 text-sm font-medium text-white transition hover:bg-brand-600 disabled:opacity-50"
                >
                  {stage === "testing" && <Loader2 size={16} className="animate-spin" />}
                  {stage === "testing" ? "Testing connection..." : "Test Connection"}
                </button>

                {testResult && !testResult.connected && stage === "idle" && (
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
          <h2 className="text-lg font-semibold text-ink">Environment Discovery</h2>
          <p className="mt-1 text-sm text-ink-muted">
            Lists the resources in the connected workspace. This is read-only — nothing is executed.
          </p>

          <button
            type="button"
            onClick={handleDiscover}
            disabled={!connected || stage !== "idle"}
            className="mt-4 inline-flex items-center gap-2 rounded-md bg-brand-500 px-5 py-2.5 text-sm font-medium text-white transition hover:bg-brand-600 disabled:opacity-50"
          >
            {stage === "discovering" && <Loader2 size={16} className="animate-spin" />}
            {stage === "discovering" ? "Discovering environment..." : "Discover Environment"}
          </button>

          {!connected && (
            <p className="mt-3 text-xs text-ink-muted">
              Test the connection successfully before discovering the environment.
            </p>
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

                  {discovery.items.length === 0 && (
                    <p className="mt-4 text-sm text-ink-muted">
                      This workspace is empty — no items were returned by the platform.
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
              <h2 className="text-lg font-semibold text-ink">ACELO Installation</h2>
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
                provisioning?.status === "INSTALLED"
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
              disabled={!connected || stage !== "idle" || provisioning?.deployable === false}
              className="inline-flex items-center gap-2 rounded-md bg-brand-500 px-5 py-2.5 text-sm font-medium text-white transition hover:bg-brand-600 disabled:opacity-50"
            >
              {stage === "provisioning" && <Loader2 size={16} className="animate-spin" />}
              {provisioning?.status === "INSTALLED" ? "Re-run ACELO Setup" : "Set up ACELO in Fabric"}
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

          {!connected && (
            <p className="mt-3 text-xs text-ink-muted">
              Test the connection successfully before setting up ACELO.
            </p>
          )}
          {provisioning?.deployable === false && (
            <p className="mt-3 text-xs text-signal-medium">
              This ACELO build has no optimization notebooks bundled, so there is nothing to deploy.
            </p>
          )}
        </section>

        {/* ---------------- Readiness ---------------- */}
        <section className="surface p-6">
          <h2 className="text-lg font-semibold text-ink">Readiness</h2>
          <ul className="mt-4 space-y-2">
            <ReadinessRow label="Authentication" ok={connected} />
            <ReadinessRow label="Workspace Access" ok={connected && Boolean(testResult?.workspace_name)} />
            <ReadinessRow label="Environment Discovery" ok={discovered} />
            <ReadinessRow
              label="ACELO Package"
              ok={provisioning?.status === "INSTALLED"}
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
              <p className="mt-1 text-xs text-ink-muted">
                {readiness.cluster_blocked_reason}
              </p>
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
