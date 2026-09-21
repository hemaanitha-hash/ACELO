import { defineConfig } from "vite";
import { resolve } from "node:path";

import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      // Two entries. redirect.html is the MSAL redirect bridge and is built as
      // a real, separate page so it is never served by the SPA fallback and
      // never pulls in the ACELO application bundle.
      input: {
        main: resolve(__dirname, "index.html"),
        redirect: resolve(__dirname, "redirect.html"),
      },
    },
  },
  server: {
    // Dual-stack bind.
    //
    // Vite's default (`server.host: "localhost"`) resolves through Node, which
    // on Windows with Node 17+ prefers IPv6 — so the dev server bound to
    // [::1]:5173 ONLY, with nothing on 127.0.0.1:5173. Any client that resolved
    // "localhost" to IPv4 got ERR_CONNECTION_REFUSED. An already-loaded SPA tab
    // kept working (it is client-side routed and makes no new document request),
    // which is why only the MSAL sign-in popup — a genuinely new navigation to
    // the redirect URI — appeared to fail.
    //
    // Listening on "::" gives a dual-stack socket (Node leaves ipv6Only off), so
    // http://localhost:5173 works regardless of how the client resolves it, and
    // the registered redirect URI stays http://localhost:5173.
    host: "::",
    port: 5173,
    // Never silently fall back to 5174. A drifted port would leave nothing on
    // 5173, producing exactly the same ERR_CONNECTION_REFUSED in the popup while
    // the main tab carried on working.
    strictPort: true,
  },
  preview: {
    host: "::",
    port: 4173,
    strictPort: true,
  },
});
