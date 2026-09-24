import React, { useEffect, useMemo, useState } from "react";
import Layout from "../components/Layout";
import PageHeader from "../components/PageHeader";
import { StatePanel } from "../components/StateBlock";
import FilterBar from "../components/FilterBar";
import OpportunityTable from "../components/OpportunityTable";
import { getOptimizations } from "../services/api";
import type { Opportunity } from "../types";

const domainTabs = ["All", "Query", "Cluster", "Storage"];

export default function Optimizations() {
  const [opportunities, setOpportunities] = useState<Opportunity[]>([]);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState("All");
  const [search, setSearch] = useState("");
  const [severity, setSeverity] = useState("All severities");
  const [status, setStatus] = useState("All statuses");

  async function load() {
    setLoading(true);
    const result = await getOptimizations();
    setOpportunities(result);
    setLoading(false);
  }

  useEffect(() => {
    load();
  }, []);

  const filtered = useMemo(() => {
    return opportunities.filter((o) => {
      const matchesTab = activeTab === "All" || o.domain === activeTab;
      const query = search.trim().toLowerCase();
      const matchesSearch =
        !query ||
        o.title.toLowerCase().includes(query) ||
        o.resource.toLowerCase().includes(query) ||
        o.id.toLowerCase().includes(query);
      const matchesSeverity = severity === "All severities" || o.severity === severity;
      const matchesStatus = status === "All statuses" || o.status === status;
      return matchesTab && matchesSearch && matchesSeverity && matchesStatus;
    });
  }, [opportunities, activeTab, search, severity, status]);

  return (
    <Layout pageName="Optimizations" onRefresh={load}>
      <div className="flex flex-col gap-6">
        <PageHeader
          title="Optimization opportunities"
          description="AI-detected opportunities across Query, Cluster and Storage."
        />

        <FilterBar
          tabs={domainTabs}
          activeTab={activeTab}
          onTabChange={setActiveTab}
          searchValue={search}
          onSearchChange={setSearch}
          searchPlaceholder="Search opportunities..."
          trailing={
            <div className="flex gap-2">
              <select
                value={severity}
                onChange={(e) => setSeverity(e.target.value)}
                className="input-field w-auto text-ink-muted"
              >
                {["All severities", "High", "Medium", "Low"].map((s) => (
                  <option key={s}>{s}</option>
                ))}
              </select>
              <select
                value={status}
                onChange={(e) => setStatus(e.target.value)}
                className="input-field w-auto text-ink-muted"
              >
                {["All statuses", "Review", "Approved", "In Progress", "Completed", "Rejected"].map(
                  (s) => (
                    <option key={s}>{s}</option>
                  )
                )}
              </select>
            </div>
          }
        />

        {loading ? (
          <StatePanel kind="loading" title="Loading opportunities…" />
        ) : (
          <OpportunityTable opportunities={filtered} />
        )}
      </div>
    </Layout>
  );
}
