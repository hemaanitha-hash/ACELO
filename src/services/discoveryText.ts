// ============================================================================
// Human wording for discovery outcomes.
//
// The backend's reason codes (NOT_EXPOSED_BY_WORKSPACE_API,
// INSUFFICIENT_PERMISSIONS, …) are a stable contract between services — they
// are not sentences. They were being printed straight into the page, so a user
// read "Unavailable — NOT_EXPOSED_BY_WORKSPACE_API" and learned nothing about
// what to do.
//
// This module is the single translation point: codes stay exact in the API and
// the logs, and the UI shows what happened and what to do about it.
// ============================================================================

import type { StateKind } from "../components/StateBlock";

export interface DiscoveryExplanation {
  kind: StateKind;
  title: string;
  detail: string;
}

/** Reason codes the backend attaches to an unavailable resource type. */
const BY_REASON: Record<string, DiscoveryExplanation> = {
  INSUFFICIENT_PERMISSIONS: {
    kind: "unavailable",
    title: "Not permitted",
    detail:
      "The connected identity is not allowed to list these. Grant it read access, then discover again.",
  },
  NOT_EXPOSED_BY_WORKSPACE_API: {
    kind: "unsupported",
    title: "Unable to enumerate",
    // Says plainly that this is an API limitation, not a verdict about the
    // workspace. "Unsupported" read as "your serverless compute is not
    // supported", which is the opposite of what the state means.
    detail:
      "Databricks does not currently provide an API that allows ACELO to list these serverless compute resources. This does not mean serverless compute is unavailable or that none exist.",
  },
  NOT_EXPOSED: {
    kind: "unsupported",
    title: "Not offered by this workspace",
    detail: "This workspace does not expose these resources over its API.",
  },
  AUTHENTICATION_FAILED: {
    kind: "error",
    title: "Authentication failed",
    detail: "The stored credential was rejected. Re-enter it in Environment Setup.",
  },
  PLATFORM_API_UNAVAILABLE: {
    kind: "error",
    title: "Platform unreachable",
    detail: "The platform API could not be reached. This is usually temporary — try again shortly.",
  },
  TIMEOUT: {
    kind: "error",
    title: "Timed out",
    detail: "The platform did not respond in time. Try again shortly.",
  },
  NOT_CONFIGURED: {
    kind: "unavailable",
    title: "Not configured",
    detail: "Finish configuring this environment before discovering resources.",
  },
  DISCOVERY_FAILED: {
    kind: "error",
    title: "Could not be read",
    detail: "The platform was reached, but these resources could not be listed.",
  },
};

/** The six per-resource-type states Environment Discovery reports. */
const BY_STATE: Record<string, DiscoveryExplanation> = {
  SUCCESS_WITH_RESOURCES: {
    kind: "success",
    title: "Discovered",
    detail: "",
  },
  SUCCESS_EMPTY: {
    kind: "empty",
    title: "None in this workspace",
    detail: "The platform was asked and reported none of these.",
  },
  AUTHORIZATION_FAILED: BY_REASON.INSUFFICIENT_PERMISSIONS,
  AUTHENTICATION_FAILED: BY_REASON.AUTHENTICATION_FAILED,
  NOT_SUPPORTED: BY_REASON.NOT_EXPOSED_BY_WORKSPACE_API,
  API_ERROR: BY_REASON.DISCOVERY_FAILED,
};

const UNKNOWN: DiscoveryExplanation = {
  kind: "error",
  title: "Could not be read",
  detail: "ACELO could not determine the state of these resources.",
};

export function explainReason(reason?: string | null): DiscoveryExplanation {
  return (reason && BY_REASON[reason]) || UNKNOWN;
}

export function explainState(state?: string | null): DiscoveryExplanation {
  return (state && BY_STATE[state]) || UNKNOWN;
}

/**
 * What to show for one resource type, combining its status with how many
 * resources came back. A successful call returning zero is "none", which is a
 * different message from a call that could not be made.
 */
export function explainDiscovery(
  status: string | null | undefined,
  reason: string | null | undefined,
  count: number,
): DiscoveryExplanation {
  if (status === "OK") {
    return count > 0
      ? BY_STATE.SUCCESS_WITH_RESOURCES
      : BY_STATE.SUCCESS_EMPTY;
  }
  return explainReason(reason);
}
