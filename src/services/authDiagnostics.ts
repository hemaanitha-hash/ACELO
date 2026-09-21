/**
 * Development-only authentication diagnostics.
 *
 * Exists to make origin/redirect-URI mismatches obvious in the console, which
 * is the class of bug that produced ERR_CONNECTION_REFUSED in the sign-in popup.
 *
 * SAFETY: this module never receives and never prints a credential. There is no
 * code path here that can log an access token, ID token, refresh token or
 * authorization code — only origins, configuration and an account username.
 * Everything is gated behind import.meta.env.DEV, so it is stripped from
 * production builds.
 */

import { MSAL_AUTHORITY, MSAL_CLIENT_ID, MSAL_REDIRECT_URI } from "../authConfig";

const PREFIX = "[ACELO auth]";

function enabled(): boolean {
  return Boolean(import.meta.env?.DEV);
}

/** Logged once at startup: the single most useful signal for callback failures. */
export function logAuthEnvironment(): void {
  if (!enabled()) return;

  const origin = window.location.origin;
  const matches = origin === MSAL_REDIRECT_URI;

  console.info(`${PREFIX} window origin      : ${origin}`);
  console.info(`${PREFIX} configured redirect: ${MSAL_REDIRECT_URI}`);
  console.info(`${PREFIX} authority          : ${MSAL_AUTHORITY}`);
  console.info(`${PREFIX} client id          : ${MSAL_CLIENT_ID || "(not configured)"}`);

  if (!matches) {
    console.warn(
      `${PREFIX} ORIGIN MISMATCH — the app is served from ${origin} but MSAL is ` +
        `configured to redirect to ${MSAL_REDIRECT_URI}. The sign-in popup will ` +
        `navigate to the configured URI; if nothing is listening there you get ` +
        `ERR_CONNECTION_REFUSED. Align VITE_ACELO_MSAL_REDIRECT_URI with the ` +
        `origin, and register that exact value in Azure.`
    );
  }
}

/** Whether a redirect response was present in the URL at startup. */
export function logRedirectCallback(hadResponse: boolean): void {
  if (!enabled()) return;
  console.info(
    `${PREFIX} handleRedirectPromise: ${hadResponse ? "consumed a response" : "no response (normal load)"}`
  );
}

export function logInteractionStatus(status: string): void {
  if (!enabled()) return;
  console.info(`${PREFIX} interaction status : ${status}`);
}

export function logLoginAttempt(method: "popup" | "redirect"): void {
  if (!enabled()) return;
  console.info(`${PREFIX} login method       : ${method}`);
}

/** Username only — never a token. */
export function logLoginResult(outcome: "success" | "cancelled" | "failed", username?: string): void {
  if (!enabled()) return;
  console.info(`${PREFIX} login ${outcome}${username ? ` : ${username}` : ""}`);
}
