import { useCallback, useEffect, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import toast from "react-hot-toast";
import { ArrowLeft, AlertTriangle, TrendingDown, ClipboardCheck, FileText, FileDown } from "lucide-react";
import { getPlan, getFractionalTrend, getChartChecks, reportUrl } from "../api/client";
import type { FractionalTrend, PlanSummary, TrendPoint } from "../types";
import { NavBar } from "../components/NavBar";
import { CouchTrackingTrend } from "../components/CouchTrackingTrend";
import { ChartCheckModal } from "../components/ChartCheckModal";

function TrendChart({ points, threshold }: { points: TrendPoint[]; threshold: number }) {
  const w = 720;
  const h = 280;
  const padL = 44;
  const padB = 36;
  const padT = 16;
  const padR = 16;
  const plotW = w - padL - padR;
  const plotH = h - padT - padB;

  if (points.length === 0) {
    return <p className="text-clinical-muted text-sm py-8 text-center">No fraction data yet.</p>;
  }

  const ys = points.map((p) => p.passing_rate);
  const yMin = Math.max(0, Math.min(...ys, threshold) - 4);
  const yMax = 100.5;

  const n = Math.max(1, points.length - 1);
  const px = (i: number) => padL + (plotW * i) / n;
  const py = (v: number) => padT + plotH * (1 - (v - yMin) / (yMax - yMin));

  const ticks = [yMin, yMin + (yMax - yMin) / 2, yMax].map((v) => Math.round(v));
  const linePts = points.map((p, i) => `${px(i)},${py(p.passing_rate)}`).join(" ");
  const thrY = py(threshold);

  return (
    <svg width="100%" viewBox={`0 0 ${w} ${h}`} className="overflow-visible">
      {ticks.map((v) => (
        <g key={v}>
          <line x1={padL} y1={py(v)} x2={w - padR} y2={py(v)} stroke="#E8E6DF" strokeWidth={1} />
          <text x={padL - 8} y={py(v) + 3} fontSize={10} textAnchor="end" fill="#888780">
            {v}
          </text>
        </g>
      ))}
      {/* action / tolerance line */}
      <line
        x1={padL}
        y1={thrY}
        x2={w - padR}
        y2={thrY}
        stroke="#EF9F27"
        strokeWidth={1.5}
        strokeDasharray="6 4"
      />
      <text x={w - padR} y={thrY - 5} fontSize={10} textAnchor="end" fill="#633806">
        action {threshold}%
      </text>
      {/* trend line */}
      <polyline points={linePts} fill="none" stroke="#D85A30" strokeWidth={2} />
      {points.map((p, i) => (
        <g key={i}>
          <circle
            cx={px(i)}
            cy={py(p.passing_rate)}
            r={4}
            fill={p.passing_rate >= threshold ? "#639922" : "#E24B4A"}
          />
          <text x={px(i)} y={h - padB + 16} fontSize={10} textAnchor="middle" fill="#888780">
            Fx {p.fraction}
          </text>
        </g>
      ))}
    </svg>
  );
}

export function FractionalTracker() {
  const { planId } = useParams<{ planId: string }>();
  const id = Number(planId);
  const navigate = useNavigate();
  const [plan, setPlan] = useState<PlanSummary | null>(null);
  const [trend, setTrend] = useState<FractionalTrend | null>(null);
  const [loading, setLoading] = useState(true);
  const [showChartCheckModal, setShowChartCheckModal] = useState(false);
  const [chartCheckCount, setChartCheckCount] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      const [p, t, checksData] = await Promise.all([
        getPlan(id),
        getFractionalTrend(id),
        getChartChecks(id).catch(() => null),
      ]);
      setPlan(p);
      setTrend(t);
      if (checksData) {
        setChartCheckCount(checksData.total_completed);
      }
    } catch {
      toast.error("Failed to load fractional trend.");
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);

  const logSeries = trend?.series?.log_vs_Rx ?? [];
  const logThreshold = trend?.thresholds?.log_vs_Rx ?? 90;
  const latest = logSeries.length ? logSeries[logSeries.length - 1] : null;

  return (
    <div className="min-h-screen bg-clinical-bg">
      <NavBar />
      <div className="max-w-4xl mx-auto px-4 py-6">
        <button
          onClick={() => navigate(-1)}
          className="flex items-center gap-1.5 text-xs text-clinical-muted hover:text-clinical-text mb-4"
        >
          <ArrowLeft size={14} /> Back
        </button>

        <div className="mb-6 flex flex-col sm:flex-row sm:items-center justify-between gap-4">
          <div>
            <h1 className="text-lg font-semibold text-clinical-text">Fractional tracker</h1>
            <p className="text-xs text-clinical-muted mt-0.5">
              {plan ? `${plan.plan_label} · log reconstruction gamma vs TPS across fractions` : "Loading…"}
            </p>
          </div>

          <div className="flex items-center gap-2">
            {/* Chart Check Action */}
            <button
              onClick={() => setShowChartCheckModal(true)}
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded-lg border border-indigo-500/30 bg-indigo-500/10 hover:bg-indigo-500/20 text-indigo-400 transition-colors shadow-xs"
              title="Physics Weekly & Continuing Chart Checks (TG-275 protocol)"
            >
              <ClipboardCheck size={14} />
              Chart Check
              {chartCheckCount !== null && chartCheckCount > 0 && (
                <span className="ml-1 px-1.5 py-0.2 rounded-full text-[10px] bg-indigo-500/30 text-indigo-200 font-bold">
                  {chartCheckCount}
                </span>
              )}
            </button>

            {/* Fractional Report Actions */}
            <div className="flex items-center rounded-lg border border-clinical-border bg-clinical-surface overflow-hidden shadow-xs">
              <a
                href={reportUrl(id, "html")}
                target="_blank"
                rel="noreferrer"
                className="flex items-center gap-1 px-3 py-1.5 text-xs text-clinical-muted hover:text-clinical-text hover:bg-clinical-border/30 border-r border-clinical-border transition-colors font-medium"
                title="View Streamlined Fractional Delivery QA Report"
              >
                <FileText size={13} />
                Fractional Report
              </a>
              <a
                href={reportUrl(id, "pdf")}
                download
                className="flex items-center gap-1 px-2 py-1.5 text-xs text-clinical-muted hover:text-clinical-text hover:bg-clinical-border/30 transition-colors"
                title="Download Fractional Report PDF"
              >
                <FileDown size={13} />
              </a>
            </div>
          </div>
        </div>

        {loading ? (
          <p className="text-clinical-muted">Loading…</p>
        ) : (
          <>
            {trend?.drift_alert && (
              <div className="rounded-lg border border-orange-500/30 bg-orange-500/10 p-4 mb-5 flex items-start gap-3">
                <TrendingDown size={18} className="text-orange-400 mt-0.5" />
                <div>
                  <h2 className="text-sm font-semibold text-orange-400">Drift alert</h2>
                  <p className="text-xs text-clinical-muted mt-0.5">
                    Gamma passing rate is trending downward over the last fractions and is
                    approaching the action threshold. Consider physicist review or a phantom measurement.
                  </p>
                </div>
              </div>
            )}

            {/* Summary chips */}
            <div className="grid grid-cols-3 gap-3 mb-5">
              <div className="rounded-lg border border-clinical-border bg-clinical-surface p-4">
                <div className="text-2xl font-bold text-clinical-text">{logSeries.length}</div>
                <div className="text-xs text-clinical-muted mt-1">Fractions analysed</div>
              </div>
              <div className="rounded-lg border border-clinical-border bg-clinical-surface p-4">
                <div className={`text-2xl font-bold ${latest && latest.passing_rate >= logThreshold ? "text-green-400" : "text-red-400"}`}>
                  {latest ? `${latest.passing_rate.toFixed(1)}%` : "—"}
                </div>
                <div className="text-xs text-clinical-muted mt-1">Latest passing rate</div>
              </div>
              <div className="rounded-lg border border-clinical-border bg-clinical-surface p-4">
                <div className="text-2xl font-bold text-clinical-text">{logThreshold}%</div>
                <div className="text-xs text-clinical-muted mt-1">Action threshold</div>
              </div>
            </div>

            <div className="rounded-lg border border-clinical-border bg-clinical-surface p-4 mb-5">
              <h2 className="text-sm font-semibold text-clinical-text mb-3">
                Gamma passing rate trend
              </h2>
              <TrendChart points={logSeries} threshold={logThreshold} />
            </div>

            <div className="mb-5">
              <CouchTrackingTrend planId={id} />
            </div>

            {/* Table */}
            {logSeries.length > 0 && (
              <div className="rounded-lg border border-clinical-border overflow-hidden">
                <table className="w-full text-sm">
                  <thead className="bg-clinical-surface border-b border-clinical-border">
                    <tr>
                      {["Fraction", "Field", "Passing rate", "Result", "Date"].map((h) => (
                        <th key={h} className="px-4 py-2.5 text-left text-xs font-medium text-clinical-muted uppercase tracking-wider">
                          {h}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-clinical-border">
                    {logSeries.map((p, i) => {
                      const passed = p.passing_rate >= logThreshold;
                      return (
                        <tr key={i}>
                          <td className="px-4 py-3 font-medium">Fx {p.fraction}</td>
                          <td className="px-4 py-3 text-clinical-muted">{p.field_name}</td>
                          <td className="px-4 py-3 tabular-nums">{p.passing_rate.toFixed(1)}%</td>
                          <td className="px-4 py-3">
                            <span className={passed ? "text-green-400" : "text-red-400"}>
                              {passed ? "Pass" : "Fail"}
                            </span>
                          </td>
                          <td className="px-4 py-3 text-clinical-muted text-xs">
                            {new Date(p.created_at).toLocaleDateString()}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}

            {logSeries.length === 0 && (
              <div className="rounded-lg border border-clinical-border bg-clinical-surface p-8 text-center text-clinical-muted text-sm flex flex-col items-center gap-2">
                <AlertTriangle size={20} className="text-clinical-muted" />
                No fraction deliveries analysed yet. Log reconstruction runs automatically
                when an RT Ion Record arrives for this plan.
              </div>
            )}
          </>
        )}
      </div>

      <ChartCheckModal
        planId={id}
        isOpen={showChartCheckModal}
        onClose={() => setShowChartCheckModal(false)}
        onCheckSaved={() => {
          load();
        }}
      />
    </div>
  );
}
