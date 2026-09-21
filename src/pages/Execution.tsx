import React, { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { CheckCircle2, PlayCircle } from "lucide-react";
import Layout from "../components/Layout";
import ExecutionProgress from "../components/ExecutionProgress";
import CheckpointCard from "../components/CheckpointCard";
import Button from "../components/Button";
import { getExecutionStatus } from "../services/api";
import type { ExecutionState } from "../types";

export default function Execution() {
  const navigate = useNavigate();
  const [execution, setExecution] = useState<ExecutionState | null>(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [completed, setCompleted] = useState(false);
  const interval = useRef<number | null>(null);

  useEffect(() => {
    setLoading(true);
    getExecutionStatus("EXE-901").then((result) => {
      setExecution(result);
      setLoading(false);
    });
    return () => {
      if (interval.current) window.clearInterval(interval.current);
    };
  }, []);

  function runDemoExecution() {
    if (!execution || running || completed) return;
    setRunning(true);

    const totalDurationMs = 4000;
    const tickMs = 120;
    const totalTicks = totalDurationMs / tickMs;
    let tick = 0;

    const startProgress = execution.progressPercent;
    const startProcessed = execution.workloadsProcessed;
    const startElapsed = execution.elapsedSeconds;

    interval.current = window.setInterval(() => {
      tick += 1;
      const ratio = Math.min(tick / totalTicks, 1);

      setExecution((prev) => {
        if (!prev) return prev;
        const progressPercent = startProgress + (100 - startProgress) * ratio;
        const workloadsProcessed = Math.round(
          startProcessed + (prev.workloadsTotal - startProcessed) * ratio
        );
        const elapsedSeconds = startElapsed + Math.round(ratio * 45);
        return {
          ...prev,
          progressPercent,
          workloadsProcessed,
          elapsedSeconds,
          stage: ratio >= 1 ? "Validation" : "Execution",
        };
      });

      if (ratio >= 1) {
        if (interval.current) window.clearInterval(interval.current);
        setRunning(false);
        setCompleted(true);
      }
    }, tickMs);
  }

  return (
    <Layout pageName="Execution">
      <div className="flex flex-col gap-6">
        <div>
          <h1 className="text-display font-semibold text-ink">Execution Center</h1>
          <p className="mt-2 text-sm text-ink-muted">
            Controlled, checkpointed execution with validation at every stage.
          </p>
        </div>

        {loading && (
          <div className="surface py-16 text-center text-sm text-ink-muted">Loading execution...</div>
        )}

        {execution && (
          <>
            <div className="surface flex flex-wrap items-center justify-between gap-4 p-5">
              <div>
                <p className="label-eyebrow">{execution.id}</p>
                <h2 className="mt-1 text-sm font-semibold text-ink">{execution.title}</h2>
                <p className="mt-1 text-xs text-ink-muted">{execution.resource}</p>
              </div>
              {!completed ? (
                <Button
                  icon={<PlayCircle size={16} />}
                  onClick={runDemoExecution}
                  disabled={running}
                >
                  {running ? "Running..." : "Run Demo Execution"}
                </Button>
              ) : (
                <span className="flex items-center gap-2 text-sm font-medium text-brand-300">
                  <CheckCircle2 size={16} />
                  Completed
                </span>
              )}
            </div>

            <ExecutionProgress
              currentStage={execution.stage}
              progressPercent={execution.progressPercent}
              workloadsProcessed={execution.workloadsProcessed}
              workloadsTotal={execution.workloadsTotal}
              elapsedSeconds={execution.elapsedSeconds}
            />

            <CheckpointCard />

            {completed && (
              <div className="surface p-6 text-center">
                <p className="text-sm font-semibold text-ink">Optimization completed successfully</p>
                <p className="mt-1 text-sm text-ink-muted">Post-execution validation passed.</p>
                <Button className="mt-4" onClick={() => navigate("/results")}>
                  View Result
                </Button>
              </div>
            )}
          </>
        )}
      </div>
    </Layout>
  );
}
