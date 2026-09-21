import React, { useEffect, useState } from "react";
import Layout from "../components/Layout";
import OpportunityTable from "../components/OpportunityTable";
import { getOptimizations } from "../services/api";
import type { Opportunity } from "../types";

export default function Recommendations() {
  const [opportunities, setOpportunities] = useState<Opportunity[]>([]);
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    const result = await getOptimizations();
    setOpportunities(result);
    setLoading(false);
  }

  useEffect(() => {
    load();
  }, []);

  const pending = opportunities.filter((o) => o.status === "Review");

  return (
    <Layout pageName="Recommendations" onRefresh={load}>
      <div className="flex flex-col gap-6">
        <div>
          <h1 className="text-display font-semibold text-ink">Recommendations</h1>
          <p className="mt-2 text-sm text-ink-muted">
            AI-generated recommendations awaiting review before approval.
          </p>
        </div>

        {loading ? (
          <div className="surface py-16 text-center text-sm text-ink-muted">
            Loading recommendations...
          </div>
        ) : (
          <OpportunityTable
            opportunities={pending}
            emptyLabel="Every recommendation has moved past review."
          />
        )}
      </div>
    </Layout>
  );
}
