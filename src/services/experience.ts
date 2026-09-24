// ============================================================================
// Which ACELO experience this build presents.
//
// The MVP ships as a native Databricks App: one platform, one workspace, no
// credentials to enter. Everything the old multi-platform architecture needed
// — the Fabric selector, multiple environments, Workspace URL, Access Token,
// Add connection — is not merely disabled but absent from this experience.
//
// The Fabric implementation itself is untouched and still fully tested. It is
// reachable only by explicitly opting out of the Databricks-only experience,
// which is what the legacy test suites do. That keeps one codebase rather than
// a parallel UI, which is what was asked for.
// ============================================================================

/**
 * True when ACELO presents the Databricks-only MVP.
 *
 * Databricks-only is the DEFAULT: this build exists to run as a Databricks App.
 * Set VITE_ACELO_LEGACY_PLATFORMS=true to restore the multi-platform UI.
 */
export function isDatabricksOnly(): boolean {
  const legacy = (import.meta.env?.VITE_ACELO_LEGACY_PLATFORMS as string | undefined) ?? "";
  return legacy.toLowerCase() !== "true";
}

/** The platform this experience operates in. */
export const MVP_PLATFORM = "databricks" as const;
