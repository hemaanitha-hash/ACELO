/**
 * Centralized MSAL configuration for ACELO's "Microsoft Account" Fabric
 * authentication mode — the signed-in ORGANIZATION (work/school) user, via
 * Entra ID.
 *
 * This is a PUBLIC client using Authorization Code + PKCE. Only public
 * configuration belongs here: there is no client secret, and no token is ever
 * written to an environment variable or to source.
 *
 * Service Principal mode does not use any of this — it stays entirely in the
 * backend (see backend/platforms/fabric.py).
 */

import type { Configuration, PopupRequest, SilentRequest } from "@azure/msal-browser";
import { LogLevel } from "@azure/msal-browser";

/**
 * Azure App Registration (Application/client) ID. Supplied at build time; never
 * hardcoded, because it is deployment-specific.
 */
export const MSAL_CLIENT_ID = (import.meta.env?.VITE_ACELO_MSAL_CLIENT_ID as string | undefined) ?? "";

/** Directory (tenant) ID the App Registration belongs to. */
export const MSAL_TENANT_ID =
  (import.meta.env?.VITE_ACELO_MSAL_TENANT_ID as string | undefined) ?? "";

/**
 * Authority — pinned to the tenant, not `/common` and not `/consumers`.
 *
 * The App Registration is "My organization only" (single-tenant), so a
 * tenant-scoped authority is the correct match: `/common` would let a user from
 * any directory reach the sign-in page only to be rejected by Entra, and
 * `/consumers` is wrong outright because a consumer Microsoft account cannot
 * own or be granted access to a Fabric workspace.
 *
 * VITE_ACELO_MSAL_AUTHORITY overrides the whole URL if a deployment needs
 * something else; otherwise it is derived from the tenant ID.
 */
export const MSAL_AUTHORITY =
  (import.meta.env?.VITE_ACELO_MSAL_AUTHORITY as string | undefined) ??
  (MSAL_TENANT_ID ? `https://login.microsoftonline.com/${MSAL_TENANT_ID}` : "");

/** Origin the application is served from. */
export const APP_ORIGIN =
  (import.meta.env?.VITE_ACELO_MSAL_REDIRECT_URI as string | undefined) ??
  window.location.origin;

/**
 * THE canonical MSAL redirect URI — the dedicated bridge page, not the app.
 *
 * Every authentication request uses this one value; there is deliberately no
 * second popup-specific constant. Sending authentication back to the app root
 * is what previously caused the popup to load the whole ACELO SPA instead of
 * relaying its result.
 *
 * Must EXACTLY match a Single-page application redirect URI registered in
 * Azure App Registration -> Authentication.
 */
export const MSAL_REDIRECT_URI = `${APP_ORIGIN}/redirect.html`;

/**
 * Delegated Fabric scopes, derived from the Fabric endpoints ACELO actually
 * calls. Nothing broader is requested.
 *
 *   Workspace.Read.All  — GET /v1/workspaces, GET /v1/workspaces/{id}
 *                         (connection test, workspace discovery)
 *   Item.Read.All       — GET /v1/workspaces/{id}/items, /folders,
 *                         GET .../jobs/instances/{id}   (discovery, polling)
 *   Item.ReadWrite.All  — POST /v1/workspaces/{id}/notebooks,
 *                         POST .../updateDefinition
 *                         (ACELO package provisioning)
 *   Workspace.ReadWrite.All
 *                       — POST /v1/workspaces/{id}/folders
 *                         (creating the ACELO namespace folder). Folder
 *                         creation is a WORKSPACE-level write, which
 *                         Item.ReadWrite.All does not cover: without this
 *                         scope Fabric returns 403 InsufficientScopes even
 *                         though reads succeed.
 *   Item.Execute.All    — POST .../jobs/RunNotebook/instances,
 *                         POST .../jobs/instances/{id}/cancel
 *                         (cluster analysis execution and cancellation)
 */
export const FABRIC_SCOPES = [
  "https://api.fabric.microsoft.com/Workspace.Read.All",
  "https://api.fabric.microsoft.com/Workspace.ReadWrite.All",
  "https://api.fabric.microsoft.com/Item.Read.All",
  "https://api.fabric.microsoft.com/Item.ReadWrite.All",
  "https://api.fabric.microsoft.com/Item.Execute.All",
];

/** Read-only subset, for deployments that only need connect + discover. */
export const FABRIC_READ_SCOPES = [
  "https://api.fabric.microsoft.com/Workspace.Read.All",
  "https://api.fabric.microsoft.com/Item.Read.All",
];

export const msalConfig: Configuration = {
  auth: {
    clientId: MSAL_CLIENT_ID,
    authority: MSAL_AUTHORITY,
    redirectUri: MSAL_REDIRECT_URI,
    postLogoutRedirectUri: MSAL_REDIRECT_URI,
    // NOTE: msal-browser v5 removed `navigateToLoginRequestUrl` — a redirect
    // sign-in now always lands on redirectUri and does not navigate back. The
    // auth fragment is cleared by handleRedirectPromise() in main.tsx.
  },
  cache: {
    // sessionStorage, not localStorage: tokens are cleared when the tab closes
    // and are not shared across tabs.
    cacheLocation: "sessionStorage",
  },
  system: {
    loggerOptions: {
      // PII (which includes tokens) is never logged.
      piiLoggingEnabled: false,
      logLevel: LogLevel.Warning,
      loggerCallback: (level, message, containsPii) => {
        if (containsPii) return;
        if (level === LogLevel.Error) console.error(message);
        else if (level === LogLevel.Warning) console.warn(message);
      },
    },
  },
};

/**
 * Interactive sign-in via popup.
 *
 * Uses MSAL_REDIRECT_URI — the dedicated bridge page. The popup lands on
 * redirect.html, which relays the response to this window and closes itself, so
 * the ACELO application is never loaded into the popup.
 */
export const loginRequest: PopupRequest = {
  scopes: ["User.Read", ...FABRIC_SCOPES],
  redirectUri: MSAL_REDIRECT_URI,
};

/**
 * Interactive sign-in via REDIRECT (popup-blocked fallback). Same URI; here the
 * app itself is the navigation target and bootstrap()'s handleRedirectPromise()
 * completes it.
 */
export const redirectLoginRequest = {
  scopes: ["User.Read", ...FABRIC_SCOPES],
  redirectUri: MSAL_REDIRECT_URI,
};

/** Silent token request used on every subsequent Fabric call. */
export const fabricTokenRequest: Omit<SilentRequest, "account"> = {
  scopes: FABRIC_SCOPES,
};

/** Interactive token top-up. Same registered redirect URI as sign-in. */
export const fabricTokenPopupRequest = {
  scopes: FABRIC_SCOPES,
  redirectUri: MSAL_REDIRECT_URI,
};

/** True when the deployment has been given an App Registration to use. */
export const isMsalConfigured = (): boolean => Boolean(MSAL_CLIENT_ID && MSAL_AUTHORITY);

/**
 * Guidance shown when Entra rejects the redirect URI. ACELO never edits the App
 * Registration itself — this tells the operator exactly what to add by hand.
 */
export const REDIRECT_URI_SETUP_HINT =
  `Add ${MSAL_REDIRECT_URI} as a Single-page application redirect URI in ` +
  "Azure App Registration \u2192 Authentication.";
