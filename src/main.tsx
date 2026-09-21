import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { EventType, PublicClientApplication } from "@azure/msal-browser";
import type { AuthenticationResult } from "@azure/msal-browser";
import { MsalProvider } from "@azure/msal-react";

import App from "./App";
import "./index.css";
import { msalConfig } from "./authConfig";
import { logAuthEnvironment, logRedirectCallback } from "./services/authDiagnostics";

/**
 * ACELO application entry point.
 *
 * This file contains NO popup detection. Authentication responses never land
 * here: MSAL is configured to redirect to the dedicated bridge page
 * (redirect.html), which relays the result back to this window. That is the
 * official MSAL v5 arrangement and it is what stops a sign-in popup from
 * loading the whole application.
 *
 * Startup order matters:
 *   1. initialize()            — required by msal-browser v3+ before any auth API
 *   2. handleRedirectPromise() — consumes a redirect-flow response the bridge
 *                                cached for us; returns null on a normal load
 *   3. set the active account
 *   4. render React
 *
 * MSAL is used only for the optional "Microsoft Account" Fabric mode. Service
 * Principal environments never touch it.
 */
const msalInstance = new PublicClientApplication(msalConfig);

function render() {
  ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
    <React.StrictMode>
      <MsalProvider instance={msalInstance}>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </MsalProvider>
    </React.StrictMode>
  );
}

async function bootstrap(): Promise<void> {
  logAuthEnvironment();

  try {
    await msalInstance.initialize();

    const result: AuthenticationResult | null = await msalInstance.handleRedirectPromise();
    logRedirectCallback(Boolean(result));
    if (result?.account) {
      msalInstance.setActiveAccount(result.account);
    }

    // Restore a previously signed-in account across reloads.
    if (!msalInstance.getActiveAccount()) {
      const [first] = msalInstance.getAllAccounts();
      if (first) msalInstance.setActiveAccount(first);
    }

    msalInstance.addEventCallback((event) => {
      if (
        (event.eventType === EventType.LOGIN_SUCCESS ||
          event.eventType === EventType.ACQUIRE_TOKEN_SUCCESS) &&
        event.payload
      ) {
        const payload = event.payload as AuthenticationResult;
        if (payload.account) msalInstance.setActiveAccount(payload.account);
      }
    });
  } catch (error) {
    // Never block the app on an auth bootstrap failure — Service Principal mode
    // does not need MSAL at all. The message deliberately carries no token.
    console.error("Microsoft sign-in could not be initialised.", (error as Error)?.message);
  } finally {
    render();
  }
}

void bootstrap();
