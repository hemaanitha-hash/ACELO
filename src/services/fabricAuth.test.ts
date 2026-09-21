/**
 * Step 5A — delegated Fabric token acquisition.
 *
 * Covers sign-in success, user cancellation, silent acquisition, the
 * interactive fallback, and consent errors. MSAL is mocked at the
 * IPublicClientApplication boundary so the real branching logic runs.
 */

import { InteractionRequiredAuthError } from "@azure/msal-browser";
import type {
  AccountInfo,
  AuthenticationResult,
  IPublicClientApplication,
} from "@azure/msal-browser";
import { describe, expect, it, vi } from "vitest";

import { FabricAuthError, describeAccount, getFabricToken, signIn } from "./fabricAuth";
import { FABRIC_SCOPES } from "../authConfig";

const ACCOUNT = {
  homeAccountId: "h",
  environment: "login.microsoftonline.com",
  tenantId: "t",
  username: "user@contoso.com",
  localAccountId: "l",
  name: "Test User",
} as AccountInfo;

/** Minimal but type-correct AuthenticationResult for mocking. */
function authResult(accessToken: string): AuthenticationResult {
  return {
    authority: "https://login.microsoftonline.com/common",
    uniqueId: "u",
    tenantId: "t",
    scopes: FABRIC_SCOPES,
    account: ACCOUNT,
    idToken: "",
    idTokenClaims: {},
    accessToken,
    fromCache: false,
    expiresOn: new Date(Date.now() + 3_600_000),
    tokenType: "Bearer",
    correlationId: "c",
  } as AuthenticationResult;
}

function makeInstance(overrides: Partial<IPublicClientApplication> = {}) {
  return {
    getActiveAccount: vi.fn(() => ACCOUNT),
    getAllAccounts: vi.fn(() => [ACCOUNT]),
    setActiveAccount: vi.fn(),
    loginPopup: vi.fn(),
    acquireTokenSilent: vi.fn(),
    acquireTokenPopup: vi.fn(),
    logoutPopup: vi.fn(),
    ...overrides,
  } as unknown as IPublicClientApplication;
}

describe("signIn", () => {
  it("returns the account and makes it active on success", async () => {
    const instance = makeInstance({
      loginPopup: vi.fn(async () => authResult("login-token")),
    } as Partial<IPublicClientApplication>);

    const account = await signIn(instance);

    expect(account.username).toBe("user@contoso.com");
    expect(instance.setActiveAccount).toHaveBeenCalledWith(ACCOUNT);
  });

  it("reports cancellation distinctly rather than as a failure", async () => {
    const instance = makeInstance({
      loginPopup: vi.fn(async () => {
        throw { errorCode: "user_cancelled" };
      }),
    } as Partial<IPublicClientApplication>);

    await expect(signIn(instance)).rejects.toMatchObject({
      code: "CANCELLED",
    });
  });

  it("surfaces a consent requirement so the UI can explain it", async () => {
    const instance = makeInstance({
      loginPopup: vi.fn(async () => {
        throw { errorCode: "invalid_grant", message: "AADSTS65001: no consent" };
      }),
    } as Partial<IPublicClientApplication>);

    const error = await signIn(instance).then(
      () => null,
      (e: unknown) => e as FabricAuthError
    );
    expect(error?.code).toBe("CONSENT_REQUIRED");
    expect(error?.message).toMatch(/administrator must consent/i);
  });
});

describe("getFabricToken", () => {
  it("acquires silently and never shows UI when the session is valid", async () => {
    const acquireTokenPopup = vi.fn();
    const instance = makeInstance({
      acquireTokenSilent: vi.fn(async () => authResult("silent-token")),
      acquireTokenPopup,
    } as Partial<IPublicClientApplication>);

    const token = await getFabricToken(instance);

    expect(token).toBe("silent-token");
    expect(acquireTokenPopup).not.toHaveBeenCalled();
    expect(instance.acquireTokenSilent).toHaveBeenCalledWith(
      expect.objectContaining({ scopes: FABRIC_SCOPES, account: ACCOUNT })
    );
  });

  it("falls back to interactive only when MSAL says interaction is required", async () => {
    const instance = makeInstance({
      acquireTokenSilent: vi.fn(async () => {
        throw new InteractionRequiredAuthError("interaction_required", "interaction required");
      }),
      acquireTokenPopup: vi.fn(async () => authResult("interactive-token")),
    } as Partial<IPublicClientApplication>);

    const token = await getFabricToken(instance);

    expect(token).toBe("interactive-token");
    expect(instance.acquireTokenPopup).toHaveBeenCalled();
  });

  it("does not fall back interactively for non-interaction errors", async () => {
    const acquireTokenPopup = vi.fn();
    const instance = makeInstance({
      acquireTokenSilent: vi.fn(async () => {
        throw new Error("network down");
      }),
      acquireTokenPopup,
    } as Partial<IPublicClientApplication>);

    await expect(getFabricToken(instance)).rejects.toMatchObject({ code: "FAILED" });
    expect(acquireTokenPopup).not.toHaveBeenCalled();
  });

  it("reports cancellation of the interactive fallback", async () => {
    const instance = makeInstance({
      acquireTokenSilent: vi.fn(async () => {
        throw new InteractionRequiredAuthError("interaction_required", "interaction required");
      }),
      acquireTokenPopup: vi.fn(async () => {
        throw { errorCode: "user_cancelled" };
      }),
    } as Partial<IPublicClientApplication>);

    await expect(getFabricToken(instance)).rejects.toMatchObject({ code: "CANCELLED" });
  });

  it("requires a signed-in account", async () => {
    const instance = makeInstance({
      getActiveAccount: vi.fn(() => null),
      getAllAccounts: vi.fn(() => []),
    } as Partial<IPublicClientApplication>);

    await expect(getFabricToken(instance)).rejects.toMatchObject({ code: "NO_ACCOUNT" });
  });
});

describe("describeAccount", () => {
  it("shows name and email, and never a token", () => {
    expect(describeAccount(ACCOUNT)).toBe("Test User (user@contoso.com)");
    expect(describeAccount(null)).toBe("");
  });
});
