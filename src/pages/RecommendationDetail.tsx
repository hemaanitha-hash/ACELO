import React, { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import Layout from "../components/Layout";
import RecommendationCard from "../components/RecommendationCard";
import ImpactCard from "../components/ImpactCard";
import StatusBadge from "../components/StatusBadge";
import Button from "../components/Button";
import Modal from "../components/Modal";
import { getRecommendation, requestApproval } from "../services/api";
import type { Opportunity } from "../types";

export default function RecommendationDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [opportunity, setOpportunity] = useState<Opportunity | null>(null);
  const [loading, setLoading] = useState(true);
  const [reviewOpen, setReviewOpen] = useState(false);
  const [requesting, setRequesting] = useState(false);
  const [requested, setRequested] = useState(false);

  useEffect(() => {
    if (!id) return;
    setLoading(true);
    getRecommendation(id).then((result) => {
      setOpportunity(result ?? null);
      setLoading(false);
    });
  }, [id]);

  async function handleRequestApproval() {
    if (!opportunity) return;
    setRequesting(true);
    await requestApproval(opportunity.id);
    setRequesting(false);
    setRequested(true);
  }

  return (
    <Layout pageName="Recommendation Detail">
      <button
        onClick={() => navigate("/optimizations")}
        className="mb-6 flex items-center gap-1.5 text-sm text-ink-muted hover:text-ink"
      >
        <ArrowLeft size={14} />
        Back to optimizations
      </button>

      {loading && (
        <div className="surface py-16 text-center text-sm text-ink-muted">
          Loading recommendation...
        </div>
      )}

      {!loading && !opportunity && (
        <div className="surface py-16 text-center">
          <p className="text-sm font-medium text-ink">Recommendation not found</p>
          <p className="mt-1 text-sm text-ink-muted">
            This opportunity may have been resolved or removed.
          </p>
        </div>
      )}

      {opportunity && (
        <div className="flex flex-col gap-6">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div>
              <p className="label-eyebrow">
                {opportunity.domain} Optimization &middot; {opportunity.id}
              </p>
              <h1 className="mt-1.5 text-display font-semibold text-ink">{opportunity.title}</h1>
              <p className="mt-1 text-sm text-ink-muted">{opportunity.resource}</p>
            </div>
            <StatusBadge label={requested ? "Approved" : opportunity.status} kind="status" />
          </div>

          <div className="grid gap-6 lg:grid-cols-[1fr_300px]">
            <div className="flex flex-col gap-5">
              <RecommendationCard eyebrow="Why ACELO flagged this" title="Detection summary">
                {opportunity.whyFlagged}
              </RecommendationCard>
              <RecommendationCard eyebrow="AI analysis" title="What ACELO found">
                {opportunity.aiAnalysis}
              </RecommendationCard>
              <RecommendationCard eyebrow="Recommended action" title="Next step">
                {opportunity.recommendedAction}
              </RecommendationCard>

              <div className="flex flex-wrap gap-3">
                <Button variant="secondary" onClick={() => setReviewOpen(true)}>
                  Review Proposed Change
                </Button>
                <Button onClick={handleRequestApproval} disabled={requesting || requested}>
                  {requested ? "Approval requested" : requesting ? "Requesting..." : "Request Approval"}
                </Button>
              </div>
              {requested && (
                <p className="text-sm text-brand-300">
                  This recommendation has been sent to the Approval Center for human review.
                </p>
              )}
            </div>

            <ImpactCard
              savingsMonthly={opportunity.impactMonthly}
              risk={opportunity.risk}
              rollbackAvailable={opportunity.rollbackAvailable}
            />
          </div>
        </div>
      )}

      <Modal
        open={reviewOpen}
        onClose={() => setReviewOpen(false)}
        title="Proposed change"
        subtitle={opportunity ? `${opportunity.id} · ${opportunity.resource}` : undefined}
      >
        <p className="text-sm leading-relaxed text-ink-muted">
          ACELO proposes a controlled configuration change scoped to this
          resource only. The change does not run automatically — it requires
          approval and executes through a checkpointed, rollback-protected
          workflow. No implementation detail is executed without your explicit
          approval in the Approval Center.
        </p>
        <div className="mt-5 flex justify-end">
          <Button onClick={() => setReviewOpen(false)}>Close</Button>
        </div>
      </Modal>
    </Layout>
  );
}
