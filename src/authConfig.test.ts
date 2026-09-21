/**
 * Step 5A — MSAL configuration.
 *
 * Asserts the deployment's public configuration resolves correctly and that no
 * secret-bearing value can reach the browser bundle.
 */

import { describe, expect, it } from "vitest";

import {
  APP_ORIGIN,
  FABRIC_SCOPES,
  MSAL_AUTHORITY,
  MSAL_CLIENT_ID,
  MSAL_REDIRECT_URI,
  MSAL_TENANT_ID,
  isMsalConfigured,
  msalConfig,
  REDIRECT_URI_SETUP_HINT,
} from "./authConfig";

const EXPECTED_CLIENT_ID = "0b5d07fa-4ecf-4441-b27e-69a000f01443";
const EXPECTED_TENANT_ID = "356c9888-5c4b-4abb-b5f6-6bfe8dbc6fdb";
const EXPECTED_ORIGIN = "http://localhost:5173";
// MSAL redirects to the dedicated bridge page, not the application root.
const EXPECTED_REDIRECT = "http://localhost:5173/redirect.html";

describe("MSAL configuration", () => {
  it("loads the Application (client) ID from the environment", () => {
    expect(MSAL_CLIENT_ID).toBe(EXPECTED_CLIENT_ID);
    expect(msalConfig.auth.clientId).toBe(EXPECTED_CLIENT_ID);
    expect(isMsalConfigured()).toBe(true);
  });

  it("uses the correct Directory (tenant) ID", () => {
    expect(MSAL_TENANT_ID).toBe(EXPECTED_TENANT_ID);
  });

  it("pins the authority to the tenant, not /common or /consumers", () => {
    // The App Registration is single-tenant ("My organization only").
    expect(MSAL_AUTHORITY).toBe(`https://login.microsoftonline.com/${EXPECTED_TENANT_ID}`);
    expect(MSAL_AUTHORITY).not.toContain("/common");
    expect(MSAL_AUTHORITY).not.toContain("/consumers");
    expect(msalConfig.auth.authority).toBe(MSAL_AUTHORITY);
  });

  it("uses the registered SPA redirect URI — the dedicated bridge page", () => {
    expect(APP_ORIGIN).toBe(EXPECTED_ORIGIN);
    expect(MSAL_REDIRECT_URI).toBe(EXPECTED_REDIRECT);
    expect(msalConfig.auth.redirectUri).toBe(EXPECTED_REDIRECT);
    expect(msalConfig.auth.postLogoutRedirectUri).toBe(EXPECTED_REDIRECT);
  });

  it("requests only the Fabric scopes the backend actually calls", () => {
    expect(FABRIC_SCOPES).toEqual([
      "https://api.fabric.microsoft.com/Workspace.Read.All",
      // Folder creation is a workspace-level write; Item.ReadWrite.All does not
      // cover it, and its absence produced 403 InsufficientScopes.
      "https://api.fabric.microsoft.com/Workspace.ReadWrite.All",
      "https://api.fabric.microsoft.com/Item.Read.All",
      "https://api.fabric.microsoft.com/Item.ReadWrite.All",
      "https://api.fabric.microsoft.com/Item.Execute.All",
    ]);
    // Nothing broader than the endpoints in use.
    expect(FABRIC_SCOPES.join(" ")).not.toContain("Tenant.Read.All");
    expect(FABRIC_SCOPES.join(" ")).not.toContain(".default");
  });

  it("caches tokens in sessionStorage and never logs PII", () => {
    expect(msalConfig.cache?.cacheLocation).toBe("sessionStorage");
    expect(msalConfig.system?.loggerOptions?.piiLoggingEnabled).toBe(false);
  });

  it("carries no client secret or token", () => {
    const serialized = JSON.stringify(msalConfig);
    expect(serialized).not.toMatch(/client_?secret/i);
    expect(serialized).not.toMatch(/access_?token/i);
    expect(serialized).not.toMatch(/refresh_?token/i);
  });

  it("spells out the manual redirect-URI fix", () => {
    expect(REDIRECT_URI_SETUP_HINT).toContain(EXPECTED_REDIRECT);
    expect(REDIRECT_URI_SETUP_HINT).toMatch(/Single-page application redirect URI/);
  });
});
