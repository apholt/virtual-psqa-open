import React, { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  FileDown,
  Layers,
  Loader2,
  RefreshCw,
  Shield,
  Target,
  XOctagon,
} from "lucide-react";
import toast from "react-hot-toast";
import { getSyntheticCTDVH, syntheticCTReportUrl } from "../api/client";
import type {
  ComparisonReference,
  DeformedOARCoverage,
  DeformedTargetCoverage,
  SyntheticCTDVHResponse,
} from "../types";

export interface DeformedDVHCardProps {
  planId: number;
  fractionNumber: number;
  hasDose?: boolean;
  hasDvh?: boolean;
  className?: string;
  activeReference?: string;
  onReferenceChange?: (ref: string) => void;
  availableReferences?: ComparisonReference[];
}

const PALETTE = [
  "#ef4444",
  "#3b82f6",
  "#10b981",
  "#f59e0b",
  "#8b5cf6",
  "#06b6d4",
  "#ec4899",
  "#84cc16",
  "#f97316",
  "#6366f1",
];

export const DeformedDVHCard: React.FC<DeformedDVHCardProps> = ({
  planId,
  fractionNumber,
  hasDose = false,
  hasDvh = false,
  className = "",
  activeReference = "tps",
  onReferenceChange,
  availableReferences,
}) => {
  const [data, setData] = useState<SyntheticCTDVHResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [recomputing, setRecomputing] = useState<boolean>(false);
  const [selectedRois, setSelectedRois] = useState<Set<number>>(new Set());
  const [plotMode, setPlotMode] = useState<"targets" | "oars" | "side_by_side">("targets");
  const [hoverDose, setHoverDose] = useState<number | null>(null);

  const isMCReference = activeReference === "mcsquare" || activeReference === "mcsquare_prev";
  const refLabel = isMCReference
    ? (activeReference === "mcsquare_prev" ? "Prior Fraction MC" : "Baseline MCsquare")
    : "Planned TPS";
  const shortRefLabel = isMCReference ? "MC Ref" : "Plan";

  const targetSvgRef = useRef<SVGSVGElement | null>(null);
  const oarSvgRef = useRef<SVGSVGElement | null>(null);
  const clipId = useId();

  const loadDVH = useCallback(
    async (forceRecompute: boolean = false) => {
      if (!hasDose && !hasDvh) return;
      try {
        if (forceRecompute) setRecomputing(true);
        else setLoading(true);

        const res = await getSyntheticCTDVH(planId, fractionNumber, forceRecompute);
        setData(res);

        // Select all targets and up to 4 OARs by default
        const defaultSelected = new Set<number>();
        res.targets.forEach((t) => defaultSelected.add(t.roi_number));
        res.oars.slice(0, 4).forEach((o) => defaultSelected.add(o.roi_number));
        if (defaultSelected.size === 0) {
          res.oars.forEach((o) => defaultSelected.add(o.roi_number));
        }
        setSelectedRois(defaultSelected);

        if (forceRecompute) {
          toast.success(
            `Deformed DVH recalculated for Fraction ${fractionNumber} (${res.overall_target_coverage})`
          );
        }
      } catch (err: any) {
        if (forceRecompute) {
          toast.error(err?.response?.data?.detail || "Failed to calculate deformed DVH");
        }
        setData(null);
      } finally {
        setLoading(false);
        setRecomputing(false);
      }
    },
    [planId, fractionNumber, hasDose, hasDvh]
  );

  useEffect(() => {
    if (hasDose || hasDvh) {
      loadDVH(false);
    } else {
      setData(null);
    }
  }, [planId, fractionNumber, hasDose, hasDvh, loadDVH]);

  const toggleRoi = (roiNum: number) => {
    setSelectedRois((prev) => {
      const next = new Set(prev);
      if (next.has(roiNum)) next.delete(roiNum);
      else next.add(roiNum);
      return next;
    });
  };

  const selectAll = () => {
    if (!data) return;
    const next = new Set<number>();
    data.targets.forEach((t) => next.add(t.roi_number));
    data.oars.forEach((o) => next.add(o.roi_number));
    setSelectedRois(next);
  };

  const clearAll = () => {
    setSelectedRois(new Set());
  };

  const selectTargetsOnly = () => {
    if (!data) return;
    setSelectedRois(new Set(data.targets.map((t) => t.roi_number)));
    setPlotMode("targets");
  };

  const selectOarsOnly = () => {
    if (!data) return;
    setSelectedRois(new Set(data.oars.map((o) => o.roi_number)));
    setPlotMode("oars");
  };

  // Shared geometry computation
  const maxDose = useMemo(() => {
    if (!data) return 60;
    let highest = data.prescription_dose_gy ? data.prescription_dose_gy * 1.15 : 60;
    const all = [...data.targets, ...data.oars];
    for (const r of all) {
      const bins = r.dvh?.dose_bins_gy;
      if (bins && bins.length > 0) {
        highest = Math.max(highest, bins[bins.length - 1]);
      }
    }
    return Math.ceil(highest / 5) * 5;
  }, [data]);

  // Chart coordinate scalers
  const getScalers = (width: number, height: number, margin = { top: 20, right: 24, bottom: 38, left: 46 }) => {
    const innerW = width - margin.left - margin.right;
    const innerH = height - margin.top - margin.bottom;

    const scaleX = (doseGy: number) => margin.left + (Math.max(0, doseGy) / maxDose) * innerW;
    const scaleY = (volPct: number) => margin.top + innerH - (Math.max(0, Math.min(100, volPct)) / 100) * innerH;
    const invertX = (pixelX: number) => {
      const clamped = Math.max(margin.left, Math.min(width - margin.right, pixelX));
      return ((clamped - margin.left) / innerW) * maxDose;
    };

    return { width, height, margin, innerW, innerH, scaleX, scaleY, invertX };
  };

  const handleSvgMouseMove = (
    e: React.MouseEvent<SVGSVGElement>,
    svgRef: React.RefObject<SVGSVGElement | null>,
    scalers: ReturnType<typeof getScalers>
  ) => {
    if (!svgRef.current) return;
    const rect = svgRef.current.getBoundingClientRect();
    const pixelX = ((e.clientX - rect.left) / rect.width) * scalers.width;
    if (pixelX >= scalers.margin.left && pixelX <= scalers.width - scalers.margin.right) {
      setHoverDose(scalers.invertX(pixelX));
    } else {
      setHoverDose(null);
    }
  };

  // Helper to interpolate volume % at hover dose
  const getVolumeAtDose = (roi: DeformedTargetCoverage | DeformedOARCoverage, dose: number) => {
    const bins = roi.dvh.dose_bins_gy;
    const refVol = isMCReference && roi.dvh.mcsquare_volume_pct && roi.dvh.mcsquare_volume_pct.length > 0
      ? roi.dvh.mcsquare_volume_pct
      : roi.dvh.tps_volume_pct;
    const sct = roi.dvh.sct_volume_pct;
    if (!bins || bins.length === 0) return { ref: 0, sct: 0 };

    // Find nearest bin
    let idx = 0;
    let minDiff = Infinity;
    for (let i = 0; i < bins.length; i++) {
      const diff = Math.abs(bins[i] - dose);
      if (diff < minDiff) {
        minDiff = diff;
        idx = i;
      }
    }
    return {
      ref: refVol[idx] ?? 0,
      sct: sct[idx] ?? 0,
    };
  };

  // Render SVG Chart for a given subset of structures
  const renderSvgChart = (
    structures: (DeformedTargetCoverage | DeformedOARCoverage)[],
    svgRef: React.RefObject<SVGSVGElement | null>,
    chartWidth: number,
    chartHeight: number,
    chartTitle: string,
    showRxLine: boolean = false
  ) => {
    const scalers = getScalers(chartWidth, chartHeight);
    const visibleStructures = structures.filter((s) => selectedRois.has(s.roi_number));

    return (
      <div className="relative rounded-lg border border-clinical-border bg-clinical-bg/50 p-2 overflow-hidden">
        <div className="flex items-center justify-between px-2 pt-1 pb-2">
          <div className="flex items-center gap-2">
            <span className="text-xs font-bold text-clinical-text uppercase tracking-wide">
              {chartTitle}
            </span>
            <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-clinical-card text-clinical-muted border border-clinical-border">
              {visibleStructures.length} / {structures.length} visible
            </span>
          </div>

          {hoverDose !== null && (
            <div className="text-[11px] font-mono font-medium text-amber-400 bg-black/60 px-2 py-0.5 rounded border border-amber-400/30">
              Dose: {hoverDose.toFixed(1)} Gy
            </div>
          )}
        </div>

        <svg
          ref={svgRef}
          viewBox={`0 0 ${scalers.width} ${scalers.height}`}
          className="w-full h-auto cursor-crosshair select-none"
          onMouseMove={(e) => handleSvgMouseMove(e, svgRef, scalers)}
          onMouseLeave={() => setHoverDose(null)}
        >
          <defs>
            <clipPath id={`${clipId}-${chartTitle.replace(/\s+/g, "_")}`}>
              <rect
                x={scalers.margin.left}
                y={scalers.margin.top}
                width={scalers.innerW}
                height={scalers.innerH}
              />
            </clipPath>
          </defs>

          {/* Horizontal Grid Lines - Volume (%) */}
          {[0, 20, 40, 60, 80, 95, 100].map((vol) => {
            const y = scalers.scaleY(vol);
            const isRef = vol === 0 || vol === 100 || vol === 95;
            return (
              <g key={vol}>
                <line
                  x1={scalers.margin.left}
                  y1={y}
                  x2={scalers.width - scalers.margin.right}
                  y2={y}
                  stroke={vol === 95 ? "#10b981" : "currentColor"}
                  strokeOpacity={vol === 95 ? 0.35 : isRef ? 0.25 : 0.08}
                  strokeDasharray={vol === 95 ? "4 2" : isRef ? undefined : "3 3"}
                  strokeWidth={1}
                />
                <text
                  x={scalers.margin.left - 6}
                  y={y + 3}
                  textAnchor="end"
                  fontSize={9}
                  fill="currentColor"
                  opacity={vol === 95 ? 0.9 : 0.55}
                  fontWeight={vol === 95 ? "bold" : "normal"}
                  className={vol === 95 ? "fill-emerald-400" : ""}
                >
                  {vol}%
                </text>
              </g>
            );
          })}

          {/* Vertical Grid Lines - Absorbed Dose (Gy) */}
          {Array.from(
            { length: Math.floor(maxDose / 10) + 1 },
            (_, i) => i * 10
          ).map((dose) => {
            const x = scalers.scaleX(dose);
            return (
              <g key={dose}>
                <line
                  x1={x}
                  y1={scalers.margin.top}
                  x2={x}
                  y2={scalers.height - scalers.margin.bottom}
                  stroke="currentColor"
                  strokeOpacity={dose === 0 ? 0.25 : 0.08}
                  strokeDasharray={dose === 0 ? undefined : "3 3"}
                />
                <text
                  x={x}
                  y={scalers.height - scalers.margin.bottom + 14}
                  textAnchor="middle"
                  fontSize={9}
                  fill="currentColor"
                  opacity={0.6}
                >
                  {dose}
                </text>
              </g>
            );
          })}

          {/* Prescription Dose Marker Line (for targets) */}
          {showRxLine && data?.prescription_dose_gy && data.prescription_dose_gy <= maxDose && (
            <g>
              <line
                x1={scalers.scaleX(data.prescription_dose_gy)}
                y1={scalers.margin.top}
                x2={scalers.scaleX(data.prescription_dose_gy)}
                y2={scalers.height - scalers.margin.bottom}
                stroke="#ef4444"
                strokeWidth={1.5}
                strokeDasharray="4 3"
              />
              <text
                x={scalers.scaleX(data.prescription_dose_gy) + 4}
                y={scalers.margin.top + 10}
                fontSize={9}
                fontWeight="bold"
                fill="#ef4444"
              >
                Rx {data.prescription_dose_gy.toFixed(0)} Gy
              </text>
            </g>
          )}

          {/* Axis Labels */}
          <text
            x={scalers.margin.left + scalers.innerW / 2}
            y={scalers.height - 4}
            textAnchor="middle"
            fontSize={10}
            fontWeight="600"
            fill="currentColor"
            opacity={0.75}
          >
            Absorbed Dose (Gy)
          </text>
          <text
            transform="rotate(-90)"
            x={-(scalers.margin.top + scalers.innerH / 2)}
            y={12}
            textAnchor="middle"
            fontSize={10}
            fontWeight="600"
            fill="currentColor"
            opacity={0.75}
          >
            Volume (%)
          </text>

          {/* Curves */}
          <g clipPath={`url(#${clipId}-${chartTitle.replace(/\s+/g, "_")})`}>
            {visibleStructures.map((roi, idx) => {
              const bins = roi.dvh.dose_bins_gy;
              const refVol = isMCReference && roi.dvh.mcsquare_volume_pct && roi.dvh.mcsquare_volume_pct.length > 0
                ? roi.dvh.mcsquare_volume_pct
                : roi.dvh.tps_volume_pct;
              const sct = roi.dvh.sct_volume_pct;
              const color = roi.color || PALETTE[idx % PALETTE.length];

              const refPoints = bins
                .map((d, i) => `${scalers.scaleX(d)},${scalers.scaleY(refVol[i])}`)
                .join(" L ");
              const sctPoints = bins
                .map((d, i) => `${scalers.scaleX(d)},${scalers.scaleY(sct[i])}`)
                .join(" L ");

              return (
                <g key={roi.roi_number}>
                  {/* Reference Planned / Baseline (Dashed) */}
                  <path
                    d={`M ${refPoints}`}
                    fill="none"
                    stroke={color}
                    strokeWidth={1.75}
                    strokeDasharray="4 3"
                    strokeOpacity={0.65}
                  />
                  {/* Daily sCT Deformed (Solid) */}
                  <path
                    d={`M ${sctPoints}`}
                    fill="none"
                    stroke={color}
                    strokeWidth={2.4}
                    strokeOpacity={1.0}
                  />
                </g>
              );
            })}

            {/* Hover Crosshair */}
            {hoverDose !== null && (
              <line
                x1={scalers.scaleX(hoverDose)}
                y1={scalers.margin.top}
                x2={scalers.scaleX(hoverDose)}
                y2={scalers.height - scalers.margin.bottom}
                stroke="#f59e0b"
                strokeWidth={1.5}
                strokeDasharray="2 2"
              />
            )}
          </g>
        </svg>

        {/* Hover Readout Tooltip (Bottom Bar) */}
        {hoverDose !== null && visibleStructures.length > 0 && (
          <div className="mt-2 p-2 bg-clinical-card/90 rounded border border-clinical-border flex flex-wrap items-center gap-3 text-xs">
            <span className="font-semibold text-clinical-text">
              At {hoverDose.toFixed(1)} Gy:
            </span>
            {visibleStructures.map((roi) => {
              const vols = getVolumeAtDose(roi, hoverDose);
              const color = roi.color || "#3b82f6";
              const diff = vols.sct - vols.ref;
              return (
                <div key={roi.roi_number} className="flex items-center gap-1.5 text-[11px]">
                  <span
                    className="w-2.5 h-2.5 rounded-full shrink-0"
                    style={{ backgroundColor: color }}
                  />
                  <span className="font-medium text-clinical-text">{roi.name}:</span>
                  <span className="font-mono text-clinical-muted">
                    sCT {vols.sct.toFixed(1)}% ({shortRefLabel} {vols.ref.toFixed(1)}%,{" "}
                    <span className={diff < -5 ? "text-red-400 font-bold" : diff > 5 ? "text-amber-400" : "text-clinical-muted"}>
                      {diff >= 0 ? `+${diff.toFixed(1)}%` : `${diff.toFixed(1)}%`}
                    </span>
                    )
                  </span>
                </div>
              );
            })}
          </div>
        )}
      </div>
    );
  };

  if (loading) {
    return (
      <div className={`bg-clinical-surface border border-clinical-border rounded-xl p-6 ${className}`}>
        <div className="flex items-center justify-center gap-3 text-xs text-clinical-muted py-12">
          <Loader2 size={18} className="animate-spin text-blue-500" />
          <span>Computing deformable target DVH &amp; clinical coverage metrics…</span>
        </div>
      </div>
    );
  }

  if (!hasDose && !hasDvh) {
    return (
      <div className={`bg-clinical-surface border border-clinical-border rounded-xl p-6 ${className}`}>
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <Target className="text-blue-500" size={18} />
            <h3 className="text-sm font-bold text-clinical-text">
              Deformed Target Coverage &amp; Adaptive DVH
            </h3>
          </div>
          <span className="text-[10px] px-2 py-0.5 rounded bg-amber-500/10 text-amber-400 border border-amber-500/30 font-bold uppercase">
            Awaiting Dose Calculation
          </span>
        </div>

        <div className="rounded-lg border border-dashed border-clinical-border p-8 text-center bg-clinical-card/20">
          <Layers size={32} className="mx-auto text-clinical-muted mb-2 opacity-60" />
          <h4 className="text-xs font-semibold text-clinical-text">
            Adaptive DVH Available After openMCsquare Calculation
          </h4>
          <p className="text-xs text-clinical-muted max-w-md mx-auto mt-1">
            Run openMCsquare dose recalculation on this fraction&apos;s Synthetic CT to evaluate
            deformed target coverage metrics (D95%, V95%, V100%) and OAR sparing against the planned TPS baseline.
          </p>
        </div>
      </div>
    );
  }

  if (!data) {
    return (
      <div className={`bg-clinical-surface border border-clinical-border rounded-xl p-6 ${className}`}>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Target className="text-blue-500" size={18} />
            <h3 className="text-sm font-bold text-clinical-text">
              Deformed Target Coverage &amp; Adaptive DVH · Fraction {fractionNumber}
            </h3>
          </div>
          <button
            onClick={() => loadDVH(true)}
            disabled={recomputing}
            className="px-3 py-1.5 bg-blue-600 hover:bg-blue-500 text-white text-xs font-semibold rounded-lg flex items-center gap-1.5 transition-colors disabled:opacity-50"
          >
            <RefreshCw size={13} className={recomputing ? "animate-spin" : ""} />
            Calculate DVH
          </button>
        </div>
        <p className="text-xs text-clinical-muted mt-3">
          No DVH metrics currently cached for this fraction. Click &quot;Calculate DVH&quot; to propagate
          planning ROIs through the deformable vector field and compute daily coverage.
        </p>
      </div>
    );
  }

  const allTargets = data.targets || [];
  const allOars = data.oars || [];

  return (
    <div className={`bg-clinical-surface border border-clinical-border rounded-xl p-5 shadow-xs space-y-5 ${className}`}>
      {/* 1. Header Bar */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-3 pb-4 border-b border-clinical-border/70">
        <div>
          <div className="flex items-center gap-2.5 flex-wrap">
            <Target className="text-blue-500 shrink-0" size={20} />
            <h3 className="text-base font-bold text-clinical-text">
              Deformed Target Coverage &amp; Adaptive DVH
            </h3>
            <span className="text-xs font-semibold px-2 py-0.5 rounded bg-clinical-card border border-clinical-border text-clinical-muted">
              Fraction {fractionNumber}
            </span>

            {/* Active Reference Badge */}
            <span className="text-[11px] px-2 py-0.5 rounded bg-blue-500/10 text-blue-400 border border-blue-500/30 font-semibold flex items-center gap-1">
              Ref: {refLabel}
            </span>

            {/* Overall Verdict Pill */}
            <span
              className={`px-2.5 py-0.5 rounded text-xs font-bold uppercase tracking-wide border flex items-center gap-1.5 ${
                data.overall_target_coverage === "PASS"
                  ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/30"
                  : data.overall_target_coverage === "WARNING"
                  ? "bg-amber-500/10 text-amber-400 border-amber-500/30"
                  : "bg-red-500/10 text-red-400 border-red-500/30"
              }`}
            >
              {data.overall_target_coverage === "PASS" ? (
                <CheckCircle2 size={13} />
              ) : data.overall_target_coverage === "WARNING" ? (
                <AlertTriangle size={13} />
              ) : (
                <XOctagon size={13} />
              )}
              {data.overall_target_coverage === "PASS"
                ? "Target Coverage Maintained"
                : data.overall_target_coverage === "WARNING"
                ? "Marginal Target Coverage"
                : "Target Under-coverage (Adaptation Recommended)"}
            </span>
          </div>

          <p className="text-xs text-clinical-muted mt-1.5">
            Planning contours resampled onto daily CBCT anatomy via Deformable Image Registration (DIR).
            Dose evaluated on deformed target masks vs{" "}
            <strong className="text-clinical-text">
              {isMCReference ? "baseline/prior openMCsquare simulation" : "nominal planned TPS baseline"}
            </strong>.
            {data.prescription_dose_gy ? ` Reference Rx: ${data.prescription_dose_gy.toFixed(1)} Gy.` : ""}
          </p>
        </div>

        {/* Action Buttons & Reference Toggle */}
        <div className="flex items-center gap-2 flex-wrap shrink-0">
          {onReferenceChange && (
            <div className="flex items-center gap-1 bg-clinical-card p-0.5 rounded-lg border border-clinical-border text-xs">
              {(availableReferences && availableReferences.length > 0
                ? availableReferences
                : [
                    { id: "tps", name: "Planned TPS", short_name: "Plan" },
                    { id: "mcsquare", name: "Baseline MC", short_name: "MC" },
                  ]
              ).map((ref) => {
                const isSel = activeReference === ref.id;
                return (
                  <button
                    key={ref.id}
                    onClick={() => onReferenceChange(ref.id)}
                    className={`px-2.5 py-1 rounded font-medium transition-colors ${
                      isSel
                        ? "bg-blue-600 text-white shadow-xs"
                        : "text-clinical-muted hover:text-clinical-text"
                    }`}
                    title={ref.description || `Compare against ${ref.name}`}
                  >
                    {ref.id === "tps" ? "🎯 " : ref.id === "mcsquare_prev" ? "⏮️ " : "⚡ "}
                    {ref.name || ref.short_name}
                  </button>
                );
              })}
            </div>
          )}

          <button
            onClick={() => loadDVH(true)}
            disabled={recomputing}
            className="px-3 py-1.5 bg-clinical-card hover:bg-clinical-surface border border-clinical-border text-clinical-text text-xs font-semibold rounded-lg flex items-center gap-1.5 transition-colors disabled:opacity-50"
            title="Re-run mask warping and recalculate DVH metrics"
          >
            <RefreshCw size={13} className={recomputing ? "animate-spin text-blue-400" : ""} />
            Re-evaluate DVH
          </button>

          <a
            href={syntheticCTReportUrl(planId, fractionNumber)}
            target="_blank"
            rel="noopener noreferrer"
            className="px-3 py-1.5 bg-blue-600 hover:bg-blue-500 text-white text-xs font-semibold rounded-lg flex items-center gap-1.5 transition-colors"
          >
            <FileDown size={13} />
            Adaptive Report
          </a>
        </div>
      </div>

      {/* 2. Clinical Alert Banner */}
      <div
        className={`p-3.5 rounded-lg border flex items-start gap-3 text-xs leading-relaxed ${
          data.overall_target_coverage === "PASS"
            ? "bg-emerald-500/10 border-emerald-500/30 text-emerald-300"
            : data.overall_target_coverage === "WARNING"
            ? "bg-amber-500/10 border-amber-500/30 text-amber-300"
            : "bg-red-500/10 border-red-500/30 text-red-300"
        }`}
      >
        {data.overall_target_coverage === "PASS" ? (
          <CheckCircle2 size={16} className="mt-0.5 shrink-0 text-emerald-400" />
        ) : data.overall_target_coverage === "WARNING" ? (
          <AlertTriangle size={16} className="mt-0.5 shrink-0 text-amber-400" />
        ) : (
          <XOctagon size={16} className="mt-0.5 shrink-0 text-red-400" />
        )}
        <div className="flex-1">
          <span className="font-bold block mb-0.5">
            {data.overall_target_coverage === "PASS"
              ? "Clinical Evaluation: Target Coverage Meets Criteria"
              : data.overall_target_coverage === "WARNING"
              ? "Clinical Evaluation: Marginal Target Coverage Alert"
              : "Clinical Evaluation: Severe Target Under-coverage Alert"}
          </span>
          <span>{data.overall_note}</span>
        </div>
      </div>

      {/* 3. Mode Switcher & Quick Filters */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 bg-clinical-card/40 p-2.5 rounded-lg border border-clinical-border">
        {/* Mode Switcher Tabs */}
        <div className="flex items-center gap-1 bg-clinical-surface p-1 rounded-md border border-clinical-border">
          <button
            onClick={() => setPlotMode("targets")}
            className={`px-3 py-1 rounded text-xs font-semibold flex items-center gap-1.5 transition-all ${
              plotMode === "targets"
                ? "bg-blue-600 text-white shadow-xs"
                : "text-clinical-muted hover:text-clinical-text"
            }`}
          >
            <Target size={13} />
            🎯 Targets Only ({allTargets.length})
          </button>
          <button
            onClick={() => setPlotMode("oars")}
            className={`px-3 py-1 rounded text-xs font-semibold flex items-center gap-1.5 transition-all ${
              plotMode === "oars"
                ? "bg-blue-600 text-white shadow-xs"
                : "text-clinical-muted hover:text-clinical-text"
            }`}
          >
            <Shield size={13} />
            🛡️ OARs Only ({allOars.length})
          </button>
          <button
            onClick={() => setPlotMode("side_by_side")}
            className={`px-3 py-1 rounded text-xs font-semibold flex items-center gap-1.5 transition-all ${
              plotMode === "side_by_side"
                ? "bg-blue-600 text-white shadow-xs"
                : "text-clinical-muted hover:text-clinical-text"
            }`}
          >
            <Activity size={13} />
            📊 Side-by-Side
          </button>
        </div>

        {/* Legend Indicator */}
        <div className="flex items-center gap-4 text-xs text-clinical-muted">
          <div className="flex items-center gap-1.5">
            <span className="w-4 h-0.5 border-t-2 border-dashed border-clinical-muted/70" />
            <span className="text-[11px]">{refLabel} (Dashed)</span>
          </div>
          <div className="flex items-center gap-1.5">
            <span className="w-4 h-0.5 bg-clinical-text rounded" />
            <span className="text-[11px] font-semibold text-clinical-text">Daily sCT MC (Solid)</span>
          </div>
        </div>

        {/* Quick Selection Filter Actions */}
        <div className="flex items-center gap-1 text-[11px]">
          <button
            onClick={selectAll}
            className="px-2 py-1 rounded bg-clinical-card hover:bg-clinical-surface text-clinical-text border border-clinical-border"
          >
            All
          </button>
          <button
            onClick={selectTargetsOnly}
            className="px-2 py-1 rounded bg-clinical-card hover:bg-clinical-surface text-clinical-text border border-clinical-border"
          >
            Targets
          </button>
          <button
            onClick={selectOarsOnly}
            className="px-2 py-1 rounded bg-clinical-card hover:bg-clinical-surface text-clinical-text border border-clinical-border"
          >
            OARs
          </button>
          <button
            onClick={clearAll}
            className="px-2 py-1 rounded bg-clinical-card hover:bg-clinical-surface text-clinical-muted border border-clinical-border"
          >
            None
          </button>
        </div>
      </div>

      {/* 4. ROI Filter Chips */}
      <div className="space-y-1.5">
        <div className="text-[11px] font-semibold text-clinical-muted uppercase tracking-wider">
          Active Segmented Structures
        </div>
        <div className="flex flex-wrap gap-1.5">
          {[...allTargets, ...allOars].map((roi, idx) => {
            const isSelected = selectedRois.has(roi.roi_number);
            const color = roi.color || PALETTE[idx % PALETTE.length];
            return (
              <button
                key={roi.roi_number}
                onClick={() => toggleRoi(roi.roi_number)}
                className={`flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs transition-all border ${
                  isSelected
                    ? "bg-clinical-surface border-clinical-border text-clinical-text font-medium shadow-2xs"
                    : "bg-clinical-bg/30 border-clinical-border/40 text-clinical-muted opacity-50 hover:opacity-80"
                }`}
              >
                <span
                  className="w-2.5 h-2.5 rounded-full shrink-0"
                  style={{
                    backgroundColor: color,
                    boxShadow: isSelected ? `0 0 5px ${color}` : "none",
                  }}
                />
                <span className="truncate max-w-[150px]">{roi.name}</span>
                <span
                  className={`text-[9px] px-1 py-0.2 rounded font-bold uppercase ${
                    roi.is_target
                      ? "bg-red-500/10 text-red-400 border border-red-500/30"
                      : "bg-blue-500/10 text-blue-400 border border-blue-500/30"
                  }`}
                >
                  {roi.is_target ? "Target" : "OAR"}
                </span>
              </button>
            );
          })}
        </div>
      </div>

      {/* 5. Interactive SVG Charts */}
      {plotMode === "targets" && (
        renderSvgChart(
          allTargets,
          targetSvgRef,
          800,
          360,
          `Deformed Target Volume Coverage (Dashed: ${shortRefLabel}, Solid: Daily sCT)`,
          true
        )
      )}

      {plotMode === "oars" && (
        renderSvgChart(
          allOars,
          oarSvgRef,
          800,
          360,
          `Critical Organs-At-Risk Sparing (Dashed: ${shortRefLabel}, Solid: Daily sCT)`,
          false
        )
      )}

      {plotMode === "side_by_side" && (
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
          {renderSvgChart(
            allTargets,
            targetSvgRef,
            500,
            330,
            `Targets (vs ${shortRefLabel})`,
            true
          )}
          {renderSvgChart(
            allOars,
            oarSvgRef,
            500,
            330,
            `Organs at Risk (vs ${shortRefLabel})`,
            false
          )}
        </div>
      )}

      {/* 6. Target Coverage Table */}
      {allTargets.length > 0 && (
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Target size={15} className="text-red-400" />
              <h4 className="text-xs font-bold text-clinical-text uppercase tracking-wider">
                Deformed Target Coverage Analysis
              </h4>
            </div>
            <span className="text-[11px] text-clinical-muted">
              Acceptance criteria: V95% &ge; 95.0% and D95 &ge; 95% Rx
            </span>
          </div>

          <div className="border border-clinical-border rounded-lg overflow-x-auto bg-clinical-surface">
            <table className="w-full text-left text-xs border-collapse">
              <thead>
                <tr className="bg-clinical-card/60 text-[11px] font-bold text-clinical-muted border-b border-clinical-border">
                  <th className="py-2.5 px-3">Target Structure</th>
                  <th className="py-2.5 px-3">Volume (Plan &rarr; sCT)</th>
                  <th className="py-2.5 px-3">&Delta; Vol %</th>
                  <th className="py-2.5 px-3">D95 ({shortRefLabel} &rarr; sCT)</th>
                  <th className="py-2.5 px-3">&Delta; D95</th>
                  <th className="py-2.5 px-3">V95% ({shortRefLabel} &rarr; sCT)</th>
                  <th className="py-2.5 px-3">&Delta; V95%</th>
                  <th className="py-2.5 px-3">Dmean ({shortRefLabel} &rarr; sCT)</th>
                  <th className="py-2.5 px-3">Status</th>
                  <th className="py-2.5 px-3 min-w-[200px]">Clinical Evaluation</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-clinical-border/60">
                {allTargets.map((t, idx) => {
                  const color = t.color || PALETTE[idx % PALETTE.length];
                  const refM = isMCReference && t.mcsquare_metrics ? t.mcsquare_metrics : t.planned_metrics;
                  const deltaM = isMCReference && t.delta_mcsquare_metrics ? t.delta_mcsquare_metrics : t.delta_metrics;
                  return (
                    <tr key={t.roi_number} className="hover:bg-clinical-card/40 transition-colors">
                      <td className="py-2.5 px-3 font-medium text-clinical-text flex items-center gap-2">
                        <span
                          className="w-2.5 h-2.5 rounded-full shrink-0"
                          style={{ backgroundColor: color }}
                        />
                        <span>{t.name}</span>
                      </td>
                      <td className="py-2.5 px-3 font-mono text-clinical-muted">
                        {t.planned_volume_cc.toFixed(1)} &rarr;{" "}
                        <span className="text-clinical-text font-semibold">
                          {t.deformed_volume_cc.toFixed(1)} cc
                        </span>
                      </td>
                      <td className="py-2.5 px-3 font-mono font-medium">
                        <span
                          className={
                            Math.abs(t.volume_change_pct) > 10
                              ? "text-amber-400 font-bold"
                              : "text-clinical-muted"
                          }
                        >
                          {t.volume_change_pct >= 0
                            ? `+${t.volume_change_pct.toFixed(1)}%`
                            : `${t.volume_change_pct.toFixed(1)}%`}
                        </span>
                      </td>
                      <td className="py-2.5 px-3 font-mono text-clinical-muted">
                        {refM.d95.toFixed(1)} &rarr;{" "}
                        <span className="text-clinical-text font-semibold">
                          {t.deformed_metrics.d95.toFixed(1)} Gy
                        </span>
                      </td>
                      <td className="py-2.5 px-3 font-mono font-medium">
                        <span
                          className={
                            deltaM.d95 < -2.0
                              ? "text-red-400 font-bold"
                              : deltaM.d95 < 0
                              ? "text-amber-400"
                              : "text-emerald-400"
                          }
                        >
                          {deltaM.d95 >= 0
                            ? `+${deltaM.d95.toFixed(1)}`
                            : deltaM.d95.toFixed(1)}{" "}
                          Gy
                        </span>
                      </td>
                      <td className="py-2.5 px-3 font-mono text-clinical-muted">
                        {(refM.v95_pct ?? 0).toFixed(1)}% &rarr;{" "}
                        <span className="text-clinical-text font-semibold">
                          {(t.deformed_metrics.v95_pct ?? 0).toFixed(1)}%
                        </span>
                      </td>
                      <td className="py-2.5 px-3 font-mono font-medium">
                        <span
                          className={
                            deltaM.v95_pct < -5.0
                              ? "text-red-400 font-bold"
                              : deltaM.v95_pct < 0
                              ? "text-amber-400"
                              : "text-emerald-400"
                          }
                        >
                          {deltaM.v95_pct >= 0
                            ? `+${deltaM.v95_pct.toFixed(1)}%`
                            : `${deltaM.v95_pct.toFixed(1)}%`}
                        </span>
                      </td>
                      <td className="py-2.5 px-3 font-mono text-clinical-muted">
                        {refM.d_mean.toFixed(1)} &rarr;{" "}
                        {t.deformed_metrics.d_mean.toFixed(1)} Gy
                      </td>
                      <td className="py-2.5 px-3">
                        <span
                          className={`px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider border ${
                            t.coverage_status === "PASS"
                              ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/30"
                              : t.coverage_status === "WARNING"
                              ? "bg-amber-500/10 text-amber-400 border-amber-500/30"
                              : "bg-red-500/10 text-red-400 border-red-500/30"
                          }`}
                        >
                          {t.coverage_status}
                        </span>
                      </td>
                      <td className="py-2.5 px-3 text-[11px] text-clinical-muted">
                        {t.coverage_note}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* 7. Critical OAR Sparing Table */}
      {allOars.length > 0 && (
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Shield size={15} className="text-blue-400" />
              <h4 className="text-xs font-bold text-clinical-text uppercase tracking-wider">
                Critical Organs-At-Risk (OAR) Sparing
              </h4>
            </div>
            <span className="text-[11px] text-clinical-muted">
              Evaluation: Sparing preserved if mean dose increase &le; 1.0 Gy
            </span>
          </div>

          <div className="border border-clinical-border rounded-lg overflow-x-auto bg-clinical-surface">
            <table className="w-full text-left text-xs border-collapse">
              <thead>
                <tr className="bg-clinical-card/60 text-[11px] font-bold text-clinical-muted border-b border-clinical-border">
                  <th className="py-2.5 px-3">OAR Structure</th>
                  <th className="py-2.5 px-3">Volume (Plan &rarr; sCT)</th>
                  <th className="py-2.5 px-3">&Delta; Vol %</th>
                  <th className="py-2.5 px-3">Dmean ({shortRefLabel} &rarr; sCT)</th>
                  <th className="py-2.5 px-3">&Delta; Dmean</th>
                  <th className="py-2.5 px-3">D2% Near-Max ({shortRefLabel} &rarr; sCT)</th>
                  <th className="py-2.5 px-3">&Delta; D2%</th>
                  <th className="py-2.5 px-3">Status</th>
                  <th className="py-2.5 px-3 min-w-[200px]">Sparing Evaluation</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-clinical-border/60">
                {allOars.map((o, idx) => {
                  const color = o.color || PALETTE[(allTargets.length + idx) % PALETTE.length];
                  const refM = isMCReference && o.mcsquare_metrics ? o.mcsquare_metrics : o.planned_metrics;
                  const deltaM = isMCReference && o.delta_mcsquare_metrics ? o.delta_mcsquare_metrics : o.delta_metrics;
                  return (
                    <tr key={o.roi_number} className="hover:bg-clinical-card/40 transition-colors">
                      <td className="py-2.5 px-3 font-medium text-clinical-text flex items-center gap-2">
                        <span
                          className="w-2.5 h-2.5 rounded-full shrink-0"
                          style={{ backgroundColor: color }}
                        />
                        <span>{o.name}</span>
                      </td>
                      <td className="py-2.5 px-3 font-mono text-clinical-muted">
                        {o.planned_volume_cc.toFixed(1)} &rarr;{" "}
                        <span className="text-clinical-text font-semibold">
                          {o.deformed_volume_cc.toFixed(1)} cc
                        </span>
                      </td>
                      <td className="py-2.5 px-3 font-mono font-medium text-clinical-muted">
                        {o.volume_change_pct >= 0
                          ? `+${o.volume_change_pct.toFixed(1)}%`
                          : `${o.volume_change_pct.toFixed(1)}%`}
                      </td>
                      <td className="py-2.5 px-3 font-mono text-clinical-muted">
                        {refM.d_mean.toFixed(1)} &rarr;{" "}
                        <span className="text-clinical-text font-semibold">
                          {o.deformed_metrics.d_mean.toFixed(1)} Gy
                        </span>
                      </td>
                      <td className="py-2.5 px-3 font-mono font-medium">
                        <span
                          className={
                            deltaM.d_mean > 2.0
                              ? "text-red-400 font-bold"
                              : deltaM.d_mean > 0.8
                              ? "text-amber-400"
                              : "text-emerald-400"
                          }
                        >
                          {deltaM.d_mean >= 0
                            ? `+${deltaM.d_mean.toFixed(1)}`
                            : deltaM.d_mean.toFixed(1)}{" "}
                          Gy
                        </span>
                      </td>
                      <td className="py-2.5 px-3 font-mono text-clinical-muted">
                        {refM.d2.toFixed(1)} &rarr;{" "}
                        {o.deformed_metrics.d2.toFixed(1)} Gy
                      </td>
                      <td className="py-2.5 px-3 font-mono font-medium">
                        <span
                          className={
                            deltaM.d2 > 3.0
                              ? "text-red-400 font-bold"
                              : deltaM.d2 > 1.0
                              ? "text-amber-400"
                              : "text-clinical-muted"
                          }
                        >
                          {deltaM.d2 >= 0
                            ? `+${deltaM.d2.toFixed(1)}`
                            : deltaM.d2.toFixed(1)}{" "}
                          Gy
                        </span>
                      </td>
                      <td className="py-2.5 px-3">
                        <span
                          className={`px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider border ${
                            o.sparing_status === "PASS"
                              ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/30"
                              : o.sparing_status === "WARNING"
                              ? "bg-amber-500/10 text-amber-400 border-amber-500/30"
                              : "bg-red-500/10 text-red-400 border-red-500/30"
                          }`}
                        >
                          {o.sparing_status}
                        </span>
                      </td>
                      <td className="py-2.5 px-3 text-[11px] text-clinical-muted">
                        {o.sparing_note}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
};
