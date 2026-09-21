/**
 * Guards the safety property of the dev diagnostics: they must never be able to
 * print a credential.
 */

import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const SRC = join(process.cwd(), "src");

function read(relative: string): string {
  return readFileSync(join(SRC, relative), "utf8");
}

describe("no credential logging", () => {
  const authFiles = [
    "services/authDiagnostics.ts",
    "services/fabricAuth.ts",
    "main.tsx",
    "redirect.ts",
    "pages/Settings.tsx",
    "authConfig.ts",
  ];

  it("never logs a token or authorization code", () => {
    for (const file of authFiles) {
      const source = read(file);
      const logCalls = source.match(/console\.(log|info|warn|error|debug)\([^)]*\)/g) ?? [];
      for (const call of logCalls) {
        expect(call, `${file}: ${call}`).not.toMatch(/accessToken|access_token/);
        expect(call, `${file}: ${call}`).not.toMatch(/idToken|id_token/);
        expect(call, `${file}: ${call}`).not.toMatch(/refreshToken|refresh_token/);
        expect(call, `${file}: ${call}`).not.toMatch(/\bcode\b\s*[,)]/);
        expect(call, `${file}: ${call}`).not.toMatch(/clientSecret|client_secret/);
        // The whole AuthenticationResult must never be dumped.
        expect(call, `${file}: ${call}`).not.toMatch(/console\.\w+\(\s*result\s*\)/);
      }
    }
  });

  it("gates all diagnostics behind DEV so nothing ships to production", () => {
    const source = read("services/authDiagnostics.ts");
    expect(source).toContain("import.meta.env?.DEV");
    // Every exported logger must consult the gate.
    const exported = source.match(/export function (\w+)/g) ?? [];
    expect(exported.length).toBeGreaterThan(3);
    for (const fn of exported) {
      const name = fn.replace("export function ", "");
      if (name === "logAuthEnvironment") continue;
      const body = source.slice(source.indexOf(`export function ${name}`));
      expect(body.slice(0, 400), name).toContain("enabled()");
    }
  });

  it("keeps handleRedirectPromise in the application startup path", () => {
    // Regression guard for the earlier callback bug. Startup lives in main.tsx
    // again now that redirect.html owns the authentication response.
    const main = read("main.tsx");
    expect(main).toContain("handleRedirectPromise");
    expect(main.indexOf("initialize()")).toBeLessThan(main.indexOf("handleRedirectPromise"));
  });

  it("uses loginPopup as primary with loginRedirect only as fallback", () => {
    const source = read("services/fabricAuth.ts");
    expect(source).toContain("loginPopup");
    expect(source).toContain("loginRedirect");
    // The redirect must sit inside the popup-blocked branch.
    const blockedBranch = source.slice(source.indexOf("isPopupBlocked(error)"));
    expect(blockedBranch.slice(0, 300)).toContain("loginRedirect");
  });

  it("does not hand-roll OAuth — MSAL owns the response", () => {
    for (const file of authFiles) {
      const source = read(file);
      expect(source, file).not.toContain("oauth2/v2.0/authorize");
      expect(source, file).not.toContain("grant_type=");
      expect(source, file).not.toMatch(/URLSearchParams\([^)]*code/);
    }
  });
});
