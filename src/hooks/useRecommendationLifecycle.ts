import { useCallback, useEffect, useRef, useState } from "react";
import {
  joinRecommendations,
  listApprovalRecords,
  listRecommendations,
  RequestError,
  type ApprovalRecord,
  type RecommendationView,
} from "../services/stage1Api";

export interface LifecycleData {
  views: RecommendationView[];
  approvals: ApprovalRecord[];
  loading: boolean;
  /** Readable message, when the persisted state could not be loaded. */
  error: string | null;
  /** Backend detail for the technical-details area. */
  technical: string | null;
  reload: () => Promise<void>;
}

/**
 * Persisted Stage 1 recommendations joined with their persisted approval
 * records. Every page that shows a recommendation's state reads it through
 * here, and reloads it after any action — never from local component state.
 */
export function useRecommendationLifecycle(): LifecycleData {
  const [views, setViews] = useState<RecommendationView[]>([]);
  const [approvals, setApprovals] = useState<ApprovalRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [technical, setTechnical] = useState<string | null>(null);
  const alive = useRef(true);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const [recommendations, records] = await Promise.all([listRecommendations(), listApprovalRecords()]);
      if (!alive.current) return;
      setApprovals(records);
      setViews(joinRecommendations(recommendations, records));
      setError(null);
      setTechnical(null);
    } catch (e: unknown) {
      if (!alive.current) return;
      setError(e instanceof RequestError ? e.message : "Could not load recommendations.");
      setTechnical(e instanceof RequestError ? e.technical : null);
    } finally {
      if (alive.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    void reload();
    return () => {
      alive.current = false;
    };
  }, [reload]);

  return { views, approvals, loading, error, technical, reload };
}
