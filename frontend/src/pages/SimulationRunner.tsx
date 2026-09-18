import { useEffect, useState, useCallback, useRef } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import toast from "react-hot-toast";
import {
  getPlan,
  getPlanJobs,
  runJob,
  cancelJob,
} from "../api/client";
import type { PlanSummary, QAJobResponse } from "../types";
import { JobProgressCard } from "../components/JobProgressCard";

const JOB_TYPES = [
  {
    type: "mcSquare",
    title: "MCsquare Monte Carlo simulation",
    description:
      "Independent dose computation in a water phantom matched to the TPS dose grid.",
  },
  {
    type: "log_reconstruction",
    title: "Delivery log reconstruction",
    description:
      "Reconstructs delivered dose from the RT Ion Record using a pencil-beam model.",
  },
];

export function SimulationRunner() {
  const { planId } = useParams<{ planId: string }>();
  const navigate = useNavigate();
  const id = planId ? parseInt(planId, 10) : NaN;

  const [plan, setPlan] = useState<PlanSummary | null>(null);
  const [jobs, setJobs] = useState<Record<string, QAJobResponse>>({});
  const pollRef = useRef<number | null>(null);

  // Latest job per job_type
  const refreshJobs = useCallback(async () => {
    if (Number.isNaN(id)) return;
    const all = await getPlanJobs(id);
    const latest: Record<string, QAJobResponse> = {};
    for (const j of all) {
      if (!latest[j.job_type]) latest[j.job_type] = j; // list is desc by id
    }
    setJobs(latest);
    return latest;
  }, [id]);

  useEffect(() => {
    if (Number.isNaN(id)) {
      navigate("/");
      return;
    }
    getPlan(id).then(setPlan).catch(() => navigate("/"));
    refreshJobs();
  }, [id, navigate, refreshJobs]);

  // Poll while any job is active
  useEffect(() => {
    const anyActive = Object.values(jobs).some(
      (j) => j.status === "queued" || j.status === "running"
    );
    if (anyActive && pollRef.current === null) {
      pollRef.current = window.setInterval(refreshJobs, 1000);
    } else if (!anyActive && pollRef.current !== null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
    return () => {
      if (pollRef.current !== null) {
        window.clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [jobs, refreshJobs]);

  const handleRun = async (jobType: string) => {
    try {
      const job = await runJob(id, jobType);
      setJobs((prev) => ({ ...prev, [jobType]: job }));
      toast.success(`${jobType} job started`);
    } catch {
      toast.error(`Failed to start ${jobType} job`);
    }
  };

  const handleCancel = async (jobType: string) => {
    const job = jobs[jobType];
    if (!job) return;
    try {
      await cancelJob(job.id);
      toast("Cancellation requested", { icon: "🛑" });
      refreshJobs();
    } catch {
      toast.error("Failed to cancel job");
    }
  };

  const allComplete =
    JOB_TYPES.length > 0 &&
    JOB_TYPES.every((jt) => jobs[jt.type]?.status === "complete");
  const anyComplete = Object.values(jobs).some((j) => j.status === "complete");

  return (
    <div className="min-h-screen bg-clinical-bg">
      <div className="border-b border-clinical-border bg-clinical-surface">
        <div className="max-w-4xl mx-auto px-4 py-4 flex items-center gap-4">
          <button
            onClick={() => navigate(`/plans/${id}/ingestion`)}
            className="text-clinical-muted hover:text-clinical-text transition-colors"
          >
            <ArrowLeft size={18} />
          </button>
          <div className="flex-1">
            <h1 className="text-base font-semibold text-clinical-text">
              QA simulation
            </h1>
            <p className="text-xs text-clinical-muted">
              {plan ? `${plan.plan_label} · ${plan.number_of_fields} fields` : "Loading…"}
            </p>
          </div>
        </div>
      </div>

      <div className="max-w-4xl mx-auto px-4 py-6 space-y-4">
        {JOB_TYPES.map((jt) => (
          <JobCardRow
            key={jt.type}
            jobType={jt}
            job={jobs[jt.type] ?? null}
            onRun={() => handleRun(jt.type)}
            onCancel={() => handleCancel(jt.type)}
          />
        ))}

        {anyComplete && (
          <div className="flex items-center justify-between bg-clinical-surface border border-clinical-border rounded-lg p-4">
            <p className="text-sm text-clinical-text">
              {allComplete
                ? "Both dose computations are complete."
                : "A dose computation is complete."}{" "}
              <span className="text-clinical-muted">
                Open the comparison viewer to run gamma analysis.
              </span>
            </p>
            <button
              onClick={() => navigate(`/plans/${id}/comparison`)}
              className="shrink-0 flex items-center gap-2 bg-clinical-accent hover:bg-blue-500 text-white px-4 py-2 rounded-md text-sm font-medium transition-colors"
            >
              Dose comparison →
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function JobCardRow({
  jobType,
  job,
  onRun,
  onCancel,
}: {
  jobType: { type: string; title: string; description: string };
  job: QAJobResponse | null;
  onRun: () => void;
  onCancel: () => void;
}) {
  return (
    <JobProgressCard
      title={jobType.title}
      description={jobType.description}
      job={job}
      onRun={onRun}
      onCancel={onCancel}
    />
  );
}
