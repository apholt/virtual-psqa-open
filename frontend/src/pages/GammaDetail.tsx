import { useCallback, useEffect, useState } from "react";
import { useParams, useNavigate, Link } from "react-router-dom";
import toast from "react-hot-toast";
import { ArrowLeft, Layers } from "lucide-react";
import { getPlan, getPlanResults } from "../api/client";
import type { GammaResult, PlanSummary, ComparisonType } from "../types";
import { NavBar } from "../components/NavBar";

const COMPARISON_LABEL: Record<ComparisonType, string> = {
  mcSquare_vs_TPS: "MCsquare vs TPS",
  log_vs_TPS: "Log reconstruction vs TPS",
  log_vs_Rx: "Log reconstruction vs Rx",
  mcSquare_vs_log: "MCsquare vs Log",
};

function InlineGammaBar({ passingRate, threshold }: { passingRate: number; threshold: number }) {
  const pct = Math.max(0, Math.min(100, passingRate));
  const passed = passingRate >= threshold;
  return (
    <div className="flex items-center gap-3">
      <div className="relative flex-1 h-2 bg-clinical-bg rounded-full overflow-hidden">
        <div
          className={`h-full rounded-full ${passed ? "bg-green-500" : "bg-red-500"}`}
          style={{ width: `${pct}%` }}
        />
        <div
          className="absolute top-0 h-full w-0.5 bg-clinical-text/70"
          style={{ left: `${threshold}%` }}
          title={`Threshold ${threshold}%`}
        />
      </div>
      <span className="text-xs tabular-nums w-12 text-right font-medium">
        {pct.toFixed(1)}%
      </span>
    </div>
  );
}

export function GammaDetail() {
  const { planId } = useParams<{ planId: string }>();
  const id = Number(planId);
  const navigate = useNavigate();
  const [plan, setPlan] = useState<PlanSummary | null>(null);
  const [rows, setRows] = useState<GammaResult[]>([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      const [p, g] = await Promise.all([getPlan(id), getPlanResults(id)]);
      setPlan(p);
      setRows(g);
    } catch {
      toast.error("Failed to load gamma results.");
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);

  const grouped = rows.reduce<Record<string, GammaResult[]>>((acc, r) => {
    (acc[r.comparison_type] ||= []).push(r);
    return acc;
  }, {});

  return (
    <div className="min-h-screen bg-clinical-bg">
      <NavBar />
      <div className="max-w-5xl mx-auto px-4 py-6">
        <button
          onClick={() => navigate(-1)}
          className="flex items-center gap-1.5 text-xs text-clinical-muted hover:text-clinical-text mb-4"
        >
          <ArrowLeft size={14} /> Back
        </button>

        <div className="flex items-center justify-between mb-6">
          <div>
            <h1 className="text-lg font-semibold text-clinical-text">Gamma analysis detail</h1>
            <p className="text-xs text-clinical-muted mt-0.5">
              {plan ? `${plan.plan_label} · ${plan.number_of_fields} fields` : "Loading…"}
            </p>
          </div>
          <Link
            to={`/plans/${id}/comparison`}
            className="flex items-center gap-1.5 px-3 py-2 text-sm bg-clinical-surface border border-clinical-border rounded-md text-clinical-text hover:border-clinical-accent transition-colors"
          >
            <Layers size={14} /> Dose viewer
          </Link>
        </div>

        {loading ? (
          <p className="text-clinical-muted">Loading…</p>
        ) : rows.length === 0 ? (
          <div className="rounded-lg border border-clinical-border bg-clinical-surface p-8 text-center text-clinical-muted text-sm">
            No gamma results yet. Run the simulation pipeline for this plan first.
          </div>
        ) : (
          (Object.keys(grouped) as ComparisonType[]).map((comp) => {
            const g = grouped[comp];
            const allPass = g.every((r) => r.passed);
            return (
              <div key={comp} className="rounded-lg border border-clinical-border overflow-hidden mb-5">
                <div className="px-4 py-3 bg-clinical-surface border-b border-clinical-border flex items-center justify-between">
                  <h2 className="text-sm font-semibold text-clinical-text">
                    {COMPARISON_LABEL[comp] || comp}
                  </h2>
                  <span className="text-xs text-clinical-muted">
                    {g[0].dd_percent}%/{g[0].dta_mm}mm · pass ≥ {g[0].threshold}% ·{" "}
                    <span className={allPass ? "text-green-400" : "text-red-400"}>
                      {allPass ? "all pass" : "review"}
                    </span>
                  </span>
                </div>
                <table className="w-full text-sm">
                  <thead className="bg-clinical-surface/50 border-b border-clinical-border">
                    <tr>
                      {["Field", "Fraction", "Passing rate", "Result"].map((h) => (
                        <th key={h} className="px-4 py-2.5 text-left text-xs font-medium text-clinical-muted uppercase tracking-wider">
                          {h}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-clinical-border">
                    {g.map((r) => (
                      <tr key={r.id}>
                        <td className="px-4 py-3 font-medium">{r.field_name}</td>
                        <td className="px-4 py-3 text-clinical-muted">
                          {r.fraction_number ?? "Composite"}
                        </td>
                        <td className="px-4 py-3 w-1/2">
                          <InlineGammaBar passingRate={r.passing_rate} threshold={r.threshold} />
                        </td>
                        <td className="px-4 py-3">
                          <span className={r.passed ? "text-green-400" : "text-red-400"}>
                            {r.passed ? "Pass" : "Fail"}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
