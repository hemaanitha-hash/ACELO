/**
 * MSAL v5 dedicated redirect-bridge architecture.
 *
 * Authentication responses land on redirect.html — a separate Vite entry that
 * loads only the MSAL bridge — and never on the ACELO application. These tests
 * pin that separation and the single canonical redirect URI.
 *
 * Structural assertions run against comment-stripped source, so the prose that
 * documents a forbidden construct cannot satisfy or fail a check.
 */

import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import {
  APP_ORIGIN,
  fabricTokenPopupRequest,
  loginRequest,
  msalConfig,
  MSAL_REDIRECT_URI,
  redirectLoginRequest,
} from "./authConfig";

const ROOT = process.cwd();
const readRaw = (p: string) => readFileSync(join(ROOT, p), "utf8");

function stripComments(source: string): string {
  return source
    .replace(/<!--[\s\S]*?-->/g, "")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|[^:])\/\/.*$/gm, "$1");
}

const read = (p: string) => stripComments(readRaw(p));

const REDIRECT_HTML = read("redirect.html");
const REDIRECT_ENTRY = read("src/redirect.ts");
const MAIN_ENTRY = read("src/main.tsx");
const AUTH_CONFIG = read("src/authConfig.ts");
const VITE_CONFIG = read("vite.config.ts");

// --------------------------------------------------------------------------
// 2-7. The dedicated bridge page
// --------------------------------------------------------------------------

describe("redirect.html", () => {
  it("exists at the Vite project root", () => {
    expect(existsSync(join(ROOT, "redirect.html"))).toBe(true);
  });

  it("loads the bridge entry and nothing else", () => {
    const scripts = REDIRECT_HTML.match(/<script[^>]*>/g) ?? [];
    expect(scripts).toHaveLength(1);
    expect(REDIRECT_HTML).toContain("/src/redirect.ts");
    expect(REDIRECT_HTML).not.toContain("/src/main.tsx");
  });

  it("imports @azure/msal-browser/redirect-bridge", () => {
    expect(REDIRECT_ENTRY).toContain("@azure/msal-browser/redirect-bridge");
    expect(REDIRECT_ENTRY).toContain("broadcastResponseToMainFrame");
  });

  it("imports exactly one module", () => {
    const imports = REDIRECT_ENTRY.match(/^\s*import\s[^;]+;/gm) ?? [];
    expect(imports).toHaveLength(1);
    expect(imports[0]).toContain("@azure/msal-browser/redirect-bridge");
  });

  it("does not import React, App, BrowserRouter, MsalProvider or Settings", () => {
    for (const forbidden of [
      "react",
      "React",
      "./App",
      "BrowserRouter",
      "MsalProvider",
      "Settings",
      "createRoot",
    ]) {
      expect(REDIRECT_ENTRY, forbidden).not.toContain(forbidden);
      expect(REDIRECT_HTML, forbidden).not.toContain(forbidden);
    }
  });

  it("does not call handleRedirectPromise — that belongs to the app window", () => {
    expect(REDIRECT_ENTRY).not.toContain("handleRedirectPromise");
    expect(REDIRECT_ENTRY).not.toContain("PublicClientApplication");
  });

  it("handles bridge errors without logging the raw error", () => {
    expect(REDIRECT_ENTRY).toContain(".catch(");
    // The raw error can carry the authentication response.
    expect(REDIRECT_ENTRY).not.toMatch(/console\.\w+\([^)]*,\s*error\s*\)/);
  });
});

// --------------------------------------------------------------------------
// 8-9, 14-16. The application entry is clean
// --------------------------------------------------------------------------

describe("main.tsx", () => {
  it("contains no popup detection", () => {
    for (const forbidden of [
      "isPopupAuthCallback",
      "isAuthCallback",
      "window.name",
      "window.opener",
      "hasAuthResponse",
    ]) {
      expect(MAIN_ENTRY, forbidden).not.toContain(forbidden);
    }
  });

  it("does not call broadcastResponseToMainFrame — the bridge page does", () => {
    expect(MAIN_ENTRY).not.toContain("broadcastResponseToMainFrame");
  });

  it("contains no manual window.close() authentication logic", () => {
    expect(MAIN_ENTRY).not.toContain("window.close()");
  });

  it("keeps initialize -> handleRedirectPromise -> setActiveAccount -> render", () => {
    expect(MAIN_ENTRY.indexOf("initialize()")).toBeLessThan(
      MAIN_ENTRY.indexOf("handleRedirectPromise")
    );
    expect(MAIN_ENTRY.indexOf("handleRedirectPromise")).toBeLessThan(
      MAIN_ENTRY.indexOf("setActiveAccount")
    );
    expect(MAIN_ENTRY).toContain("createRoot");
    expect(MAIN_ENTRY).toContain("MsalProvider");
    expect(MAIN_ENTRY).toContain("BrowserRouter");
  });

  it("the custom detection module is gone", () => {
    expect(existsSync(join(ROOT, "src/authPopupDetect.ts"))).toBe(false);
    expect(existsSync(join(ROOT, "src/bootstrap.tsx"))).toBe(false);
  });
});

// --------------------------------------------------------------------------
// 10-13. One canonical redirect URI
// --------------------------------------------------------------------------

describe("redirect URI", () => {
  it("is exactly http://localhost:5173/redirect.html", () => {
    expect(MSAL_REDIRECT_URI).toBe("http://localhost:5173/redirect.html");
    expect(APP_ORIGIN).toBe("http://localhost:5173");
  });

  it("is used by every authentication request", () => {
    const uris = new Set([
      msalConfig.auth.redirectUri,
      msalConfig.auth.postLogoutRedirectUri,
      loginRequest.redirectUri,
      redirectLoginRequest.redirectUri,
      fabricTokenPopupRequest.redirectUri,
    ]);
    expect([...uris]).toEqual(["http://localhost:5173/redirect.html"]);
  });

  it("defines exactly one MSAL redirect constant", () => {
    expect(AUTH_CONFIG).toContain("export const MSAL_REDIRECT_URI");
    for (const competing of [
      "MSAL_POPUP_REDIRECT_URI",
      "MSAL_MAIN_REDIRECT_URI",
      "POPUP_REDIRECT_URI",
    ]) {
      expect(AUTH_CONFIG, competing).not.toContain(`export const ${competing}`);
    }
  });

  it("references no /auth/popup.html anywhere", () => {
    for (const [name, source] of Object.entries({
      AUTH_CONFIG,
      MAIN_ENTRY,
      REDIRECT_ENTRY,
      REDIRECT_HTML,
      VITE_CONFIG,
    })) {
      expect(source, name).not.toContain("auth/popup");
    }
  });

  it("keeps a single origin — never mixes localhost and 127.0.0.1", () => {
    expect(MSAL_REDIRECT_URI).not.toContain("127.0.0.1");
  });
});

// --------------------------------------------------------------------------
// Build configuration
// --------------------------------------------------------------------------

describe("vite build", () => {
  it("declares both entries so redirect.html is a real page, not an SPA route", () => {
    expect(VITE_CONFIG).toContain("index.html");
    expect(VITE_CONFIG).toContain("redirect.html");
    expect(VITE_CONFIG).toMatch(/input:\s*\{/);
  });
});
