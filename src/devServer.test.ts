/**
 * Regression tests for ERR_CONNECTION_REFUSED in the MSAL sign-in popup.
 *
 * Root cause: Vite's default host resolved to IPv6 on Windows and bound
 * [::1]:5173 only. A client resolving "localhost" to IPv4 got connection
 * refused. An already-loaded SPA tab kept working because it makes no new
 * document request; the sign-in popup navigating to the redirect URI did not.
 *
 * The URL-serving tests below need a running dev server. They are skipped
 * (not failed) when one is not up, so `npm test` stays usable offline —
 * run `npm run dev` first to exercise them.
 */

import { describe, expect, it } from "vitest";

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { APP_ORIGIN, MSAL_AUTHORITY, MSAL_CLIENT_ID, MSAL_REDIRECT_URI } from "./authConfig";

// Read the config as source rather than importing it: importing vite.config.ts
// pulls esbuild into the jsdom environment, which it cannot run in.
const viteConfigSource = readFileSync(join(process.cwd(), "vite.config.ts"), "utf8");

const ORIGIN = APP_ORIGIN;

async function reachable(url: string): Promise<boolean> {
  try {
    const res = await fetch(url, { redirect: "manual" });
    return res.status < 500;
  } catch {
    return false;
  }
}

// Probed at COLLECTION time via top-level await. `it.runIf` is evaluated when
// the suite is collected, so a flag set in beforeAll would always still be
// false and every URL test would silently skip.
const serverUp = await reachable(`${ORIGIN}/`);
if (!serverUp) {
  console.warn(
    `[devServer.test] ${ORIGIN} is not responding — URL tests skipped. ` +
      "Run `npm run dev` to exercise them."
  );
}

describe("Vite dev server configuration", () => {
  it("binds dual-stack so both IPv4 and IPv6 clients can connect", () => {
    // "::" yields a dual-stack socket (Node leaves ipv6Only off), which is what
    // makes http://localhost:5173 work regardless of how a client resolves it.
    expect(viteConfigSource).toMatch(/host:\s*"::"/);
  });

  it("pins the port so it can never drift to 5174", () => {
    // A drifted port leaves nothing on 5173 and reproduces the same refusal in
    // the popup while the already-loaded main tab carries on working.
    expect(viteConfigSource).toMatch(/port:\s*5173/);
    expect(viteConfigSource).toMatch(/strictPort:\s*true/);
  });

  it("serves the app on the origin the redirect URI belongs to", () => {
    expect(APP_ORIGIN).toBe("http://localhost:5173");
    expect(MSAL_REDIRECT_URI.startsWith(APP_ORIGIN)).toBe(true);
    expect(viteConfigSource).toMatch(/port:\s*5173/);
  });

  it("serves the MSAL redirect bridge page", async () => {
    // Must be a real page, never the SPA fallback.
    expect(await reachable(`${ORIGIN}/redirect.html`)).toBe(true);
  });

  it("has no stale generated config that would shadow vite.config.ts", () => {
    // `tsc -b` used to emit vite.config.js beside the source, and Vite loads
    // .js in preference to .ts — so edits to the .ts were silently ignored.
    const { existsSync } = require("node:fs") as typeof import("node:fs");
    expect(existsSync(join(process.cwd(), "vite.config.js"))).toBe(false);
  });
});

describe("callback URLs are actually served", () => {
  it.runIf(serverUp)("serves /", async () => {
    expect(await reachable(`${ORIGIN}/`)).toBe(true);
  });

  it.runIf(serverUp)("serves /settings", async () => {
    expect(await reachable(`${ORIGIN}/settings`)).toBe(true);
  });

  it.runIf(serverUp)("serves the MSAL callback shape /#code=test", async () => {
    // The fragment never reaches the server, but this is the document request
    // the popup makes; it must not be refused. "test" is never exchanged.
    expect(await reachable(`${ORIGIN}/#code=test`)).toBe(true);
  });

  it.runIf(serverUp)("is reachable over IPv4 loopback", async () => {
    // This is the exact request that was refused before the fix.
    expect(await reachable("http://127.0.0.1:5173/")).toBe(true);
  });

  it.runIf(serverUp)("is reachable over IPv6 loopback", async () => {
    expect(await reachable("http://[::1]:5173/")).toBe(true);
  });
});

describe("MSAL configuration matches the served origin", () => {
  it("loads the client ID", () => {
    expect(MSAL_CLIENT_ID).toBe("0b5d07fa-4ecf-4441-b27e-69a000f01443");
  });

  it("loads the tenant-scoped authority", () => {
    expect(MSAL_AUTHORITY).toBe(
      "https://login.microsoftonline.com/356c9888-5c4b-4abb-b5f6-6bfe8dbc6fdb"
    );
  });

  it("uses one origin consistently — never mixes localhost and 127.0.0.1", () => {
    expect(MSAL_REDIRECT_URI).toBe("http://localhost:5173/redirect.html");
    expect(MSAL_REDIRECT_URI).not.toContain("127.0.0.1");
  });
});
