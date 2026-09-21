import React, { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import Layout from "../components/Layout";
import ApprovalPanel from "../components/ApprovalPanel";
import { getApprovals, approveOptimization, rejectOptimization } from "../services/api";
import type { ApprovalRequest } from "../types";

export default function Approvals() {
  const navigate = useNavigate();
  const [approvals, setApprovals] = useState<ApprovalRequest[]>([]);
  const [loading, setLoading] = useState(true);
  const [approvingId, setApprovingId] = useState<string | null>(null);
  const [approvedIds, setApprovedIds] = useState<Set<string>>(new Set());
  const [rejectedIds, setRejectedIds] = useState<Set<string>>(new Set());

  async function load() {
    setLoading(true);
    const result = await getApprovals();
    setApprovals(result);
    setLoading(false);
  }

  useEffect(() => {
    load();
  }, []);

  async function handleApprove(approvalId: string) {
    setApprovingId(approvalId);
    await approveOptimization(approvalId);
    setApprovingId(null);
    setApprovedIds((prev) => new Set(prev).add(approvalId));
  }

  async function handleReject(approvalId: string) {
    await rejectOptimization(approvalId);
    setRejectedIds((prev) => new Set(prev).add(approvalId));
  }

  return (
    <Layout pageName="Approval Center" onRefresh={load}>
      <div className="flex flex-col gap-6">
        <div>
          <h1 className="text-display font-semibold text-ink">Approval Center</h1>
          <p className="mt-2 text-sm text-ink-muted">
            Human approval remains in control before optimization execution.
          </p>
        </div>

        {loading && (
          <div className="surface py-16 text-center text-sm text-ink-muted">Loading approvals...</div>
        )}

        {!loading && approvals.length === 0 && (
          <div className="surface py-16 text-center">
            <p className="text-sm font-medium text-ink">No approvals pending</p>
            <p className="mt-1 text-sm text-ink-muted">
              New AI recommendations will appear here once requested.
            </p>
          </div>
        )}

        <div className="flex flex-col gap-4">
          {approvals.map((approval) => (
            <ApprovalPanel
              key={approval.id}
              approval={approval}
              onReview={(opportunityId) => navigate(`/optimizations/${opportunityId}`)}
              onApprove={handleApprove}
              onReject={handleReject}
              approving={approvingId === approval.id}
              approved={approvedIds.has(approval.id)}
              rejected={rejectedIds.has(approval.id)}
            />
          ))}
        </div>
      </div>
    </Layout>
  );
}
