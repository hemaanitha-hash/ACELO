/**
 * MSAL v5 redirect bridge entry.
 *
 * Loaded ONLY by redirect.html, which is the single registered MSAL redirect
 * URI. It must never import React, App, BrowserRouter, MsalProvider or any
 * ACELO module — that is the entire point of a dedicated bridge page.
 *
 * It also must not call handleRedirectPromise(): that belongs to the
 * application window, which owns the MSAL instance and its cache.
 */

import { broadcastResponseToMainFrame } from "@azure/msal-browser/redirect-bridge";

broadcastResponseToMainFrame().catch((error: unknown) => {
  // Log the error type only. The raw error can carry the authentication
  // response, which must never reach the console.
  console.error("MSAL redirect bridge error:", (error as Error)?.name ?? "Error");

  const status = document.querySelector("p");
  if (status) {
    status.textContent =
      "Sign-in could not be completed. You can close this window and try again.";
  }
});
