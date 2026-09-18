import { CheckCircle, XCircle, Loader2, Clock, Ban } from "lucide-react";
import type { QAJobResponse } from "../types";

interface Props {
  title: string;
  description: string;
  job: QAJobResponse | null;
  onRun: () => void;
  onCancel: () => void;
  disabled?: boolean;
}

const STATUS_META: Record<
  string,
  { label: string; color: string; icon: typeof CheckCircle }
> = {
  queued: { label: "Queued", color: "text-amber-400", icon: Clock },
  running: { label: "Running", color: "text-blue-400", icon: Loader2 },
  complete: { label: "Complete", color: "text-green-400", icon: CheckCircle },
  error: { label: "Error", color: "text-red-400", icon: XCircle },
  cancelled: { label: "Cancelled", color: "text-clinical-muted", icon: Ban },
};

export function JobProgressCard({
  title,
  description,
  job,
  onRun,
  onCancel,
  disabled,
}: Props) {
  const status = job?.status;
  const meta = status ? STATUS_META[status] : null;
  const Icon = meta?.icon;
  const isActive = status === "queued" || status === "running";
  const progressPct = Math.round((job?.progress ?? 0) * 100);

  return (
    <div className="bg-clinical-surface border border-clinical-border rounded-lg p-5">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h3 className="text-sm font-semibold text-clinical-text">{title}</h3>
          <p className="text-xs text-clinical-muted mt-1">{description}</p>
        </div>
        {meta && Icon && (
          <span className={`flex items-center gap-1.5 text-xs font-medium ${meta.color}`}>
            <Icon size={14} className={status === "running" ? "animate-spin" : ""} />
            {meta.label}
          </span>
        )}
      </div>

      {/* Progress bar */}
      {(isActive || status === "complete") && (
        <div className="mt-4">
          <div className="h-2 bg-clinical-bg rounded-full overflow-hidden">
            <div
              className={`h-full rounded-full transition-all duration-300 ${
                status === "complete" ? "bg-green-500" : "bg-clinical-accent"
              }`}
              style={{ width: `${progressPct}%` }}
            />
          </div>
          <p className="text-xs text-clinical-muted mt-1.5">{progressPct}%</p>
        </div>
      )}

      {/* Error message */}
      {status === "error" && job?.error_message && (
        <div className="mt-3 text-xs text-red-400 bg-red-500/10 border border-red-500/20 rounded-md p-2.5">
          {job.error_message}
        </div>
      )}

      {/* Actions */}
      <div className="mt-4 flex gap-2">
        {isActive ? (
          <button
            onClick={onCancel}
            className="px-3 py-1.5 text-xs font-medium text-red-400 border border-red-500/30 rounded-md hover:bg-red-500/10 transition-colors"
          >
            Cancel
          </button>
        ) : (
          <button
            onClick={onRun}
            disabled={disabled}
            className="px-3 py-1.5 text-xs font-medium bg-clinical-accent hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed text-white rounded-md transition-colors"
          >
            {status === "complete" ? "Re-run" : "Run"}
          </button>
        )}
      </div>
    </div>
  );
}
