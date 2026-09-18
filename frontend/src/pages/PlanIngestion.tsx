import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, AlertTriangle, CheckCircle, Play } from "lucide-react";
import { getPlan, getPlanFields } from "../api/client";
import type { FieldSummary, PlanSummary } from "../types";

export function PlanIngestion() {
  const { planId } = useParams<{ planId: string }>();
  const navigate = useNavigate();
  const [plan, setPlan] = useState<PlanSummary | null>(null);
  const [fields, setFields] = useState<FieldSummary[]>([]);
  const [loading, setLoading] = useState(true);

  // For freshly uploaded plans, the ingestion result is passed via location state
  const ingestionResult = (
    window.history.state?.usr as {
      warnings?: string[];
      dicom_files_found?: Record<string, number>;
      patient_name?: string;
      patient_id?: string;
    } | undefined
  );

  useEffect(() => {
    if (!planId) return;
    const id = parseInt(planId, 10);
    Promise.all([getPlan(id), getPlanFields(id)])
      .then(([p, f]) => {
        setPlan(p);
        setFields(f);
      })
      .catch(() => navigate("/"))
      .finally(() => setLoading(false));
  }, [planId, navigate]);

  if (loading) {
    return (
      <div className="min-h-screen bg-clinical-bg flex items-center justify-center text-clinical-muted text-sm">
        Loading plan…
      </div>
    );
  }

  if (!plan) return null;

  const warnings = ingestionResult?.warnings ?? [];
  const dicomFound = ingestionResult?.dicom_files_found ?? {};
  const patientName = ingestionResult?.patient_name ?? "";
  const patientId = ingestionResult?.patient_id ?? "";

  return (
    <div className="min-h-screen bg-clinical-bg">
      {/* Header */}
      <div className="border-b border-clinical-border bg-clinical-surface">
        <div className="max-w-5xl mx-auto px-4 py-4 flex items-center gap-4">
          <button
            onClick={() => navigate("/")}
            className="text-clinical-muted hover:text-clinical-text transition-colors"
          >
            <ArrowLeft size={18} />
          </button>
          <div className="flex-1">
            <h1 className="text-base font-semibold text-clinical-text">
              Plan ingestion
            </h1>
            <p className="text-xs text-clinical-muted">
              {patientId && `${patientId} · `}
              {patientName && `${patientName} · `}
              {plan.plan_label}
            </p>
          </div>
          <button
            onClick={() => navigate(`/plans/${planId}/simulation`)}
            className="flex items-center gap-2 bg-green-600 hover:bg-green-500 text-white px-4 py-2 rounded-md text-sm font-medium transition-colors"
          >
            <Play size={14} />
            Run QA simulation
          </button>
        </div>
      </div>

      <div className="max-w-5xl mx-auto px-4 py-6 space-y-6">
        {/* Plan summary */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          {[
            { label: "Plan label", value: plan.plan_label },
            { label: "Plan name", value: plan.plan_name },
            { label: "Fractions", value: plan.number_of_fractions ?? "—" },
            { label: "Fields", value: plan.number_of_fields },
          ].map(({ label, value }) => (
            <div
              key={label}
              className="bg-clinical-surface border border-clinical-border rounded-lg p-4"
            >
              <p className="text-xs text-clinical-muted mb-1">{label}</p>
              <p className="text-lg font-semibold text-clinical-text">{value}</p>
            </div>
          ))}
        </div>

        {/* DICOM files found */}
        {Object.keys(dicomFound).length > 0 && (
          <div className="bg-clinical-surface border border-clinical-border rounded-lg p-4">
            <p className="text-xs font-medium text-clinical-muted uppercase tracking-wider mb-3">
              DICOM files found
            </p>
            <div className="flex flex-wrap gap-3">
              {Object.entries(dicomFound).map(([modality, count]) => (
                <span
                  key={modality}
                  className="inline-flex items-center gap-1.5 px-3 py-1 bg-clinical-bg border border-clinical-border rounded-md text-xs text-clinical-text"
                >
                  <CheckCircle size={12} className="text-green-400" />
                  {count} {modality}
                </span>
              ))}
            </div>
          </div>
        )}

        {/* Warnings */}
        {warnings.length > 0 && (
          <div className="space-y-2">
            {warnings.map((w, i) => (
              <div
                key={i}
                className="flex items-start gap-3 bg-amber-500/10 border border-amber-500/20 rounded-lg px-4 py-3 text-sm text-amber-300"
              >
                <AlertTriangle size={16} className="shrink-0 mt-0.5" />
                {w}
              </div>
            ))}
          </div>
        )}

        {/* Field table */}
        <div>
          <h2 className="text-sm font-medium text-clinical-text mb-3">Treatment fields</h2>
          <div className="rounded-lg border border-clinical-border overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-clinical-surface border-b border-clinical-border">
                <tr>
                  {["Field name", "Gantry (°)", "Energy (MeV)", "Layers", "Spots", "MU"].map(
                    (h) => (
                      <th
                        key={h}
                        className="px-4 py-3 text-left text-xs font-medium text-clinical-muted uppercase tracking-wider"
                      >
                        {h}
                      </th>
                    )
                  )}
                </tr>
              </thead>
              <tbody className="divide-y divide-clinical-border">
                {fields.length === 0 ? (
                  <tr>
                    <td colSpan={6} className="px-4 py-8 text-center text-clinical-muted text-xs">
                      No field data available
                    </td>
                  </tr>
                ) : (
                  fields.map((f, i) => (
                    <tr key={i} className="hover:bg-clinical-surface/40">
                      <td className="px-4 py-3 font-medium">{f.beam_name}</td>
                      <td className="px-4 py-3 text-clinical-muted">{f.gantry_angle.toFixed(1)}</td>
                      <td className="px-4 py-3 text-clinical-muted">
                        {f.energy_min_mev.toFixed(0)}–{f.energy_max_mev.toFixed(0)}
                      </td>
                      <td className="px-4 py-3 text-clinical-muted">{f.number_of_layers}</td>
                      <td className="px-4 py-3 text-clinical-muted">
                        {f.total_spots.toLocaleString()}
                      </td>
                      <td className="px-4 py-3 text-clinical-muted">{f.total_mu.toFixed(2)}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
}
