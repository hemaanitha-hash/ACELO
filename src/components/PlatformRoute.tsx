import React, { useEffect, useState } from "react";
import { Navigate } from "react-router-dom";
import {
  getActiveContext,
  subscribe,
  type ActiveContext,
  type ActivePlatformId,
} from "../services/platformContext";

/**
 * Guards a platform-specific route.
 *
 * Hiding a nav entry is not enough: the URL is still typeable and the page
 * would fetch the other platform's data. A page for a platform that is not
 * active never mounts, so it never issues its request.
 *
 * While the context is still resolving (no platform yet) the child renders —
 * the backend is the final authority and refuses a genuine mismatch, so a
 * brief unknown state must not blank a legitimate page.
 */
export default function PlatformRoute({
  platform,
  children,
}: {
  platform: ActivePlatformId;
  children: React.ReactNode;
}) {
  const [context, setContext] = useState<ActiveContext>(getActiveContext());
  useEffect(() => subscribe(setContext), []);

  if (context.platform && context.platform !== platform) {
    return <Navigate to="/" replace />;
  }
  return <>{children}</>;
}
