/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base URL of the ACELO backend API. Falls back to localhost in dev. */
  readonly VITE_API_BASE?: string;
  /** Azure App Registration (Application/client) ID — public client, no secret. */
  readonly VITE_ACELO_MSAL_CLIENT_ID?: string;
  /** Directory (tenant) ID; the authority is derived from it. */
  readonly VITE_ACELO_MSAL_TENANT_ID?: string;
  /** Must match a registered SPA redirect URI. */
  readonly VITE_ACELO_MSAL_REDIRECT_URI?: string;
  /** Optional full-authority override; normally derived from the tenant ID. */
  readonly VITE_ACELO_MSAL_AUTHORITY?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
