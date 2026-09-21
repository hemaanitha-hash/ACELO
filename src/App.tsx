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

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Overview />} />
      <Route path="/agent" element={<AIAgent />} />
      <Route path="/optimizations" element={<Optimizations />} />
      <Route path="/optimizations/:id" element={<RecommendationDetail />} />
      <Route path="/recommendations" element={<Recommendations />} />
      <Route path="/recommendations/:id" element={<RecommendationDetail />} />
      <Route path="/approvals" element={<Approvals />} />
      <Route path="/approvals/:id" element={<ApprovalDetail />} />
      <Route path="/execution" element={<Execution />} />
      <Route path="/results" element={<Results />} />
      <Route path="/history" element={<History />} />
      <Route path="/settings" element={<Settings />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
