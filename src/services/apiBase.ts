// ============================================================================
// Where the ACELO backend lives, for every service module.
//
// This was duplicated across nine files, and one of them (services/api.ts) had
// the localhost URL hardcoded with no override at all — so the MVP's
// Recommendations page would have called the *user's own machine* from inside
// a Databricks App.
//
// The rule is decided by how ACELO is served, not by configuration:
//
//   production build  ->  "/api"  — FastAPI serves the built SPA and the API
//                         from the same origin (see backend/main.py), so a
//                         relative path is correct and needs no build-time
//                         variable. This is what runs inside a Databricks App.
//
//   dev server        ->  "http://localhost:8000/api" — Vite serves the SPA on
//                         5173 and uvicorn the API on 8000, two origins.
//
// VITE_API_BASE still overrides both, for anyone running the two halves apart.
// ============================================================================

// `import.meta.env.DEV` is written WITHOUT optional chaining on purpose: Vite
// statically replaces that exact token at build time, so the production bundle
// folds down to the "/api" branch and the localhost string disappears entirely.
// Writing `import.meta.env?.DEV` defeats the replacement and ships localhost —
// which is precisely the bug this module exists to remove.
const DEV_API_BASE = "http://localhost:8000/api";
const SAME_ORIGIN_API_BASE = "/api";

export const API_BASE: string =
  (import.meta.env.VITE_API_BASE as string | undefined) ||
  (import.meta.env.DEV ? DEV_API_BASE : SAME_ORIGIN_API_BASE);
