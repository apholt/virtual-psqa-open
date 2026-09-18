import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { getPatientPlans } from "../api/client";
import type { PlanSummary, QAStatus } from "../types";
import { StatusBadge } from "../components/StatusBadge";
import { Topbar } from "../components/Topbar";

export function PatientPlans() {
  const { patientId } = useParams<{ patientId: string }>();
  const navigate = useNavigate();
  const [plans, setPlans] = useState<PlanSummary[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!patientId) return;
    getPatientPlans(parseInt(patientId, 10))
      .then(setPlans)
      .finally(() => setLoading(false));
  }, [patientId]);

  return (
    <div className="min-h-screen bg-clinical-bg">
      <Topbar
        breadcrumb={[
          { label: "Dashboard", to: "/" },
          { label: "Worklist", to: "/#worklist" },
          { label: `Patient #${patientId} Plans` },
        ]}
      />

      <div className="max-w-5xl mx-auto px-4 py-6">
        {loading ? (
          <p className="text-clinical-muted text-sm">Loading…</p>
        ) : plans.length === 0 ? (
          <p className="text-clinical-muted text-sm">No plans found for this patient.</p>
        ) : (
          <div className="rounded-lg border border-clinical-border overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-clinical-surface border-b border-clinical-border">
                <tr>
                  {["Plan label", "Site", "Fractions", "Fields", "Status", "Created", ""].map((h) => (
                    <th key={h} className="px-4 py-3 text-left text-xs font-medium text-clinical-muted uppercase tracking-wider">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-clinical-border">
                {plans.map((p) => (
                  <tr
                    key={p.id}
                    onClick={() => navigate(`/plans/${p.id}`)}
                    className="cursor-pointer hover:bg-clinical-surface/60 transition-colors"
                  >
                    <td className="px-4 py-3 font-medium">{p.plan_label}</td>
                    <td className="px-4 py-3 text-clinical-muted">{p.treatment_site ?? "—"}</td>
                    <td className="px-4 py-3 text-clinical-muted text-center">{p.number_of_fractions ?? "—"}</td>
                    <td className="px-4 py-3 text-clinical-muted text-center">{p.number_of_fields}</td>
                    <td className="px-4 py-3"><StatusBadge status={p.qa_status as QAStatus} /></td>
                    <td className="px-4 py-3 text-clinical-muted text-xs">{new Date(p.created_at).toLocaleDateString()}</td>
                    <td className="px-4 py-3 text-right"><span className="text-clinical-accent text-xs">View →</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
