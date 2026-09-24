import React from "react";
import { Routes, Route, Navigate } from "react-router-dom";
import Overview from "./pages/Overview";
import AIAgent from "./pages/AIAgent";
import Optimizations from "./pages/Optimizations";
import Recommendations from "./pages/Recommendations";
import RecommendationDetail from "./pages/RecommendationDetail";
import Approvals from "./pages/Approvals";
import ApprovalDetail from "./pages/ApprovalDetail";
import Execution from "./pages/Execution";
import Results from "./pages/Results";
import History from "./pages/History";
import Settings from "./pages/Settings";
import DatabricksDiscovery from "./pages/DatabricksDiscovery";
import ComputeOptimization from "./pages/ComputeOptimization";
import RunDetails from "./pages/RunDetails";
import { RunMonitorProvider } from "./components/RunMonitor";
import { isDatabricksOnly } from "./services/experience";
import PlatformRoute from "./components/PlatformRoute";

export default function App() {
  // The run monitor wraps every route so run notifications appear on any page.
  return (
    <RunMonitorProvider>
    <Routes>
      <Route path="/" element={<Overview />} />
      <Route path="/agent" element={<AIAgent />} />
      {/* The MVP's main capability: discovery -> evidence -> analysis ->
          findings -> recommendations, in one place. */}
      <Route path="/compute" element={<ComputeOptimization />} />
      <Route path="/optimizations" element={<Optimizations />} />
      <Route path="/optimizations/:id" element={<RecommendationDetail />} />
      <Route path="/recommendations" element={<Recommendations />} />
      <Route path="/recommendations/:id" element={<RecommendationDetail />} />
      <Route path="/approvals" element={<Approvals />} />
      <Route path="/approvals/:id" element={<ApprovalDetail />} />
      <Route path="/execution" element={<Execution />} />
      <Route path="/results" element={<Results />} />
      <Route path="/history" element={<History />} />
      <Route path="/runs" element={<History />} />
      <Route path="/runs/:id" element={<RunDetails />} />
      {/* Platform-specific route. Reachable only while Databricks is active —
          routing respects the context, not just the navigation menu. */}
      <Route
        path="/databricks"
        element={
          <PlatformRoute platform="databricks">
            <DatabricksDiscovery />
          </PlatformRoute>
        }
      />
      {/* Environment Setup is legacy-only. In the Databricks MVP the route is
          not registered at all, so a typed /settings URL falls through to the
          catch-all below and returns to Overview — the page is unreachable,
          not merely unlinked. The component itself is retained for the
          multi-platform experience. */}
      {!isDatabricksOnly() && <Route path="/settings" element={<Settings />} />}
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
    </RunMonitorProvider>
  );
}
