import React from "react";
import { useLocation } from "react-router-dom";
import Layout from "../components/Layout";
import AgentWorkspace from "../components/AgentWorkspace";

interface AgentLocationState {
  prompt?: string;
}

export default function AIAgent() {
  const location = useLocation();
  const state = (location.state as AgentLocationState | null) ?? null;

  return (
    <Layout pageName="AI Agent">
      <div className="flex flex-col gap-6">
        <AgentWorkspace key={state?.prompt ?? "fresh"} initialPrompt={state?.prompt} />
      </div>
    </Layout>
  );
}
