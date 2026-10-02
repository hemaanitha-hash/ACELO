import React from "react";
import { Navigate, useNavigate, useParams } from "react-router-dom";
import { Cpu } from "lucide-react";
import Layout from "../components/Layout";
import PageHeader from "../components/PageHeader";
import Button from "../components/Button";
import { EmptyState } from "../components/ui";

/**
 * Optimization areas other than Compute. The backend has no Databricks
 * analysis for these yet, so each page states its scope and status plainly and
 * offers no action that would call a nonexistent API.
 */
const AREAS: Record<string, { title: string; scope: string }> = {
  cost: {
    title: "Cost Optimization",
    scope: "Spend across Databricks compute, attributed to workloads and owners.",
  },
  jobs: {
    title: "Jobs & Pipelines Optimization",
    scope: "Job and pipeline scheduling, retries and task-level efficiency.",
  },
  sql: {
    title: "SQL Optimization",
    scope: "SQL warehouse sizing and query performance.",
  },
  storage: {
    title: "Storage Optimization",
    scope: "Table layout, file sizes and retention.",
  },
};

export default function OptimizationArea() {
  const { area = "" } = useParams();
  const navigate = useNavigate();
  const info = AREAS[area];
  if (!info) return <Navigate to="/compute" replace />;

  return (
    <Layout pageName={info.title}>
      <div className="flex flex-col gap-6">
        <PageHeader eyebrow="Optimization" title={info.title} description={info.scope} />
        <EmptyState
          title="This optimization area is not yet activated in this ACELO Environment."
          detail="Compute Optimization is active today: its findings, including utilization and billing evidence, appear as recommendations for approval."
          action={
            <Button icon={<Cpu size={16} />} onClick={() => navigate("/compute")}>
              Open Compute Optimization
            </Button>
          }
        />
      </div>
    </Layout>
  );
}
