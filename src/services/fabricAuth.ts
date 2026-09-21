/**
 * Delegated Fabric token acquisition.
 *
 * Silent first, interactive only when MSAL says interaction is required. The
 * token is handed straight to the ACELO backend for the duration of one request
 * and is never logged, never stored by ACELO, and never placed in a URL.
 */

import {
  InteractionRequiredAuthError,
  type AccountInfo,
  type IPublicClientApplication,
} from "@azure/msal-browser";

import {
  fabricTokenPopupRequest,
  FABRIC_SCOPES,
  loginRequest,
  redirectLoginRequest,
} from "../authConfig";
import { logLoginAttempt, logLoginResult } from "./authDiagnostics";

export class FabricAuthError extends Error {
  constructor(
    message: string,
    readonly code:
      | "NOT_CONFIGURED"
      | "NO_ACCOUNT"
      | "CONSENT_REQUIRED"
      | "REDIRECT_MISMATCH"
      | "REDIRECTING"
      | "CANCELLED"
      | "FAILED"
  ) {
    super(message);
    this.name = "FabricAuthError";
  }
}

/** True when MSAL reports the user cancelled the popup rather than failing. */
function isUserCancellation(error: unknown): boolean {
  const code = (error as { errorCode?: string } | null)?.errorCode ?? "";
  return code === "user_cancelled" || code === "interaction_in_progress";
}

/**
 * True when the browser blocked the popup. This is NOT a cancellation — the
 * user never got the chance to choose — so it warrants a redirect fallback
 * rather than an error. Previously this was lumped in with cancellation, which
 * made a blocked popup look like the user had changed their mind.
 */
function isPopupBlocked(error: unknown): boolean {
  const code = (error as { errorCode?: string } | null)?.errorCode ?? "";
  return code === "popup_window_error" || code === "empty_window_error";
}

/**
 * True when Entra rejected the redirect URI. AADSTS50011 is what a missing or
 * mismatched SPA redirect URI looks like, and it is the most common first-run
 * failure, so it gets its own actionable code.
 */
function isRedirectMismatch(error: unknown): boolean {
  const message = (error as { message?: string } | null)?.message ?? "";
  const code = (error as { errorCode?: string } | null)?.errorCode ?? "";
  return message.includes("AADSTS50011") || code === "redirect_uri_mismatch";
}

/** True when Entra is asking for (admin) consent to the Fabric scopes. */
function isConsentRequired(error: unknown): boolean {
  const code = (error as { errorCode?: string } | null)?.errorCode ?? "";
  const message = (error as { message?: string } | null)?.message ?? "";
  return (
    code === "consent_required" ||
    message.includes("AADSTS65001") || // user/admin has not consented
    message.includes("AADSTS900144")
  );
}

/**
 * Interactive sign-in.
 *
 * Popup is the primary flow: it keeps the user on the Settings page and avoids
 * a full app reload. If the browser blocks the popup we fall back to a redirect,
 * which navigates away and is completed by handleRedirectPromise() in main.tsx
 * on the way back. Both paths end with the account set as active.
 */
export async function signIn(instance: IPublicClientApplication): Promise<AccountInfo> {
  logLoginAttempt("popup");
  try {
    const result = await instance.loginPopup(loginRequest);
    instance.setActiveAccount(result.account);
    logLoginResult("success", result.account?.username);
    return result.account;
  } catch (error) {
    if (isPopupBlocked(error)) {
      logLoginAttempt("redirect");
      // Navigates away; this promise never resolves. main.tsx picks the
      // response up on return.
      // Redirect returns to the APP, not the popup callback.
      await instance.loginRedirect(redirectLoginRequest);
      throw new FabricAuthError("Redirecting to Microsoft sign-in...", "REDIRECTING");
    }
    if (isUserCancellation(error)) {
      logLoginResult("cancelled");
      throw new FabricAuthError("Sign-in was cancelled.", "CANCELLED");
    }
    logLoginResult("failed");
    if (isConsentRequired(error)) {
      throw new FabricAuthError(
        "This application has not been granted the Fabric permissions it needs. " +
          "An administrator must consent to the requested Fabric scopes.",
        "CONSENT_REQUIRED"
      );
    }
    if (isRedirectMismatch(error)) {
      throw new FabricAuthError(
        "Microsoft rejected the sign-in redirect URI.",
        "REDIRECT_MISMATCH"
      );
    }
    throw new FabricAuthError("Microsoft sign-in failed.", "FAILED");
  }
}

export async function signOut(instance: IPublicClientApplication): Promise<void> {
  const account = instance.getActiveAccount() ?? instance.getAllAccounts()[0];
  if (account) await instance.logoutPopup({ account });
}

/**
 * Returns a delegated Fabric access token.
 *
 * Silent acquisition is attempted first; interactive acquisition runs only when
 * MSAL raises InteractionRequiredAuthError (expired session, revoked consent,
 * MFA challenge).
 */
export async function getFabricToken(instance: IPublicClientApplication): Promise<string> {
  const account = instance.getActiveAccount() ?? instance.getAllAccounts()[0];
  if (!account) {
    throw new FabricAuthError("Sign in with Microsoft before connecting.", "NO_ACCOUNT");
  }

  try {
    const result = await instance.acquireTokenSilent({ scopes: FABRIC_SCOPES, account });
    return result.accessToken;
  } catch (error) {
    if (!(error instanceof InteractionRequiredAuthError)) {
      if (isConsentRequired(error)) {
        throw new FabricAuthError(
          "This application has not been granted the Fabric permissions it needs. " +
            "An administrator must consent to the requested Fabric scopes.",
          "CONSENT_REQUIRED"
        );
      }
      throw new FabricAuthError("Could not obtain a Fabric access token.", "FAILED");
    }

    // Interactive fallback — the only path that shows UI.
    try {
      const result = await instance.acquireTokenPopup({ ...fabricTokenPopupRequest, account });
      return result.accessToken;
    } catch (interactiveError) {
      if (isUserCancellation(interactiveError)) {
        throw new FabricAuthError("Sign-in was cancelled.", "CANCELLED");
      }
      if (isConsentRequired(interactiveError)) {
        throw new FabricAuthError(
          "This application has not been granted the Fabric permissions it needs. " +
            "An administrator must consent to the requested Fabric scopes.",
          "CONSENT_REQUIRED"
        );
      }
      throw new FabricAuthError("Could not obtain a Fabric access token.", "FAILED");
    }
  }
}

/** Display label for the signed-in account. Never includes a token. */
export function describeAccount(account: AccountInfo | null): string {
  if (!account) return "";
  return account.name ? `${account.name} (${account.username})` : account.username;
}
