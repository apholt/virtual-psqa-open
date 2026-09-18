import type { ReactNode } from "react";
import { CheckCircle2, Circle, AlertTriangle } from "lucide-react";

interface Props {
  title: string;
  subtitle?: string;
  available: boolean;
  status?: "ok" | "warn" | "fail";
  children?: ReactNode;
}

export function EvidenceCard({ title, subtitle, available, status = "ok", children }: Props) {
  const Icon = !available ? Circle : status === "ok" ? CheckCircle2 : AlertTriangle;
  const iconColor = !available
    ? "text-clinical-muted/40"
    : status === "ok"
    ? "text-green-400"
    : status === "warn"
    ? "text-orange-400"
    : "text-red-400";

  return (
    <div
      className={`rounded-lg border bg-clinical-surface p-4 ${
        available ? "border-clinical-border" : "border-clinical-border/50 opacity-70"
      }`}
    >
      <div className="flex items-start justify-between mb-3">
        <div>
          <h3 className="text-sm font-semibold text-clinical-text">{title}</h3>
          {subtitle && <p className="text-xs text-clinical-muted mt-0.5">{subtitle}</p>}
        </div>
        <Icon size={18} className={iconColor} />
      </div>
      {available ? (
        <div className="space-y-1.5">{children}</div>
      ) : (
        <p className="text-xs text-clinical-muted italic">Evidence not yet available</p>
      )}
    </div>
  );
}

export function MetricRow({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex items-center justify-between text-xs">
      <span className="text-clinical-muted">{label}</span>
      <span className="font-medium text-clinical-text tabular-nums">{value}</span>
    </div>
  );
}
