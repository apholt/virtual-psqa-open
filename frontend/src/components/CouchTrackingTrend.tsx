import React, { useEffect, useState, useMemo } from "react";
import {
  Move,
  AlertTriangle,
  Activity,
} from "lucide-react";
import { getCouchTrends } from "../api/client";
import type { CouchTrendsResponse, CouchTrendPoint } from "../types";

interface Props {
  planId: number;
}

type ViewMode = "delta_pos" | "angles" | "abs_pos";
type AbsSubMode = "stacked" | "lat" | "long" | "vert";

export const CouchTrackingTrend: React.FC<Props> = ({ planId }) => {
  const [data, setData] = useState<CouchTrendsResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  // Controls
  const [selectedBeam, setSelectedBeam] = useState<string>("all");
  const [viewMode, setViewMode] = useState<ViewMode>("delta_pos");
  const [absSubMode, setAbsSubMode] = useState<AbsSubMode>("stacked");
  const [includeVerification, setIncludeVerification] = useState<boolean>(false);

  // Series visibility toggles (for delta_pos mode)
  const [showDeltaLat, setShowDeltaLat] = useState<boolean>(true);
  const [showDeltaLong, setShowDeltaLong] = useState<boolean>(true);
  const [showDeltaVert, setShowDeltaVert] = useState<boolean>(true);
  const [showDelta3D, setShowDelta3D] = useState<boolean>(true);

  // Series visibility toggles (for angles mode)
  const [showPitch, setShowPitch] = useState<boolean>(true);
  const [showRoll, setShowRoll] = useState<boolean>(true);
  const [showSupport, setShowSupport] = useState<boolean>(true);

  // Hover state
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);

  useEffect(() => {
    let live = true;
    setLoading(true);
    setError(null);
    getCouchTrends(planId)
      .then((res) => {
        if (!live) return;
        setData(res);
        setLoading(false);
      })
      .catch((err) => {
        if (!live) return;
        setError(err?.response?.data?.detail || "Failed to load couch tracking trends.");
        setLoading(false);
      });
    return () => {
      live = false;
    };
  }, [planId]);

  // Filter series points based on verification toggle
  const currentSeries: CouchTrendPoint[] = useMemo(() => {
    if (!data || !data.series_by_beam) return [];
    const pts = data.series_by_beam[selectedBeam] || data.series_by_beam["all"] || [];
    if (!includeVerification) {
      return pts.filter((p) => !p.is_verification && p.fraction_number > 0);
    }
    return pts;
  }, [data, selectedBeam, includeVerification]);

  if (loading) {
    return (
      <div className="rounded-lg border border-clinical-border bg-clinical-surface p-8 text-center">
        <Activity size={24} className="animate-spin text-clinical-accent mx-auto mb-2" />
        <p className="text-xs text-clinical-muted">Loading 6-DoF couch position &amp; rotation tracking data...</p>
      </div>
    );
  }

  if (error || !data || currentSeries.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-clinical-border p-6 text-center bg-clinical-surface">
        <Move size={24} className="mx-auto text-clinical-muted mb-2 opacity-50" />
        <h3 className="text-xs font-semibold text-clinical-text">No Couch Positioning Records</h3>
        <p className="text-[11px] text-clinical-muted max-w-md mx-auto mt-1">
          {error || "Couch 6-DoF coordinates will populate as RT Ion delivery records are analyzed for each fraction."}
        </p>
      </div>
    );
  }

  // Selected beam label
  const selectedBeamObj = data.beams.find((b) => String(b.beam_number) === selectedBeam);
  const selectedBeamLabel =
    selectedBeam === "all"
      ? "Composite / Mean Across All Beams"
      : `Beam ${selectedBeamObj?.beam_number ?? selectedBeam}: ${selectedBeamObj?.beam_name ?? ""}`;

  return (
    <div className="rounded-lg border border-clinical-border bg-clinical-surface p-4 shadow-sm space-y-4">
      {/* HEADER */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-clinical-border/50 pb-3">
        <div className="flex items-center gap-3">
          <div className="p-2 rounded border bg-clinical-bg border-clinical-border/60 text-clinical-accent">
            <Move size={18} />
          </div>
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-xs font-bold text-clinical-text uppercase tracking-wider">
                Fractional Couch 6-DoF Position &amp; Rotation Tracking
              </h2>
              <span className="text-[10px] font-bold px-2 py-0.5 rounded bg-sky-100 text-sky-800 dark:bg-sky-950/60 dark:text-sky-300 border border-sky-300 dark:border-sky-800">
                Baseline Reference: Fraction {data.baseline_fraction ?? 1}
              </span>
            </div>
            <p className="text-[11px] text-clinical-muted mt-0.5">
              Monitors inter-fraction patient setup displacement (Lat X, Long Y, Vert Z) and 6-DoF table rotations (Pitch, Roll, Support) across the treatment course.
            </p>
          </div>
        </div>

        {/* Verification run filter toggle */}
        <div className="flex items-center gap-3">
          <label className="flex items-center gap-2 text-xs text-clinical-muted cursor-pointer hover:text-clinical-text transition-colors">
            <input
              type="checkbox"
              checked={includeVerification}
              onChange={(e) => setIncludeVerification(e.target.checked)}
              className="rounded border-clinical-border text-clinical-accent focus:ring-0 cursor-pointer"
            />
            <span className="text-[11px]">Include Dry Run (Fx 0)</span>
          </label>
        </div>
      </div>

      {/* CONTROLS BAR: BEAM SELECTOR & VIEW MODE SELECTOR */}
      <div className="flex flex-wrap items-center justify-between gap-3 bg-clinical-bg/40 p-2.5 rounded-lg border border-clinical-border/40">
        {/* Beam Selector Buttons */}
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[11px] font-semibold text-clinical-muted uppercase mr-1">Beam:</span>
          <button
            onClick={() => setSelectedBeam("all")}
            className={`px-2.5 py-1 text-xs rounded font-medium transition-all ${
              selectedBeam === "all"
                ? "bg-clinical-accent text-white shadow-sm"
                : "bg-clinical-surface hover:bg-clinical-border/40 text-clinical-text border border-clinical-border"
            }`}
          >
            All Beams (Mean)
          </button>
          {data.beams.map((b) => {
            const isSel = String(b.beam_number) === selectedBeam;
            return (
              <button
                key={b.beam_number}
                onClick={() => setSelectedBeam(String(b.beam_number))}
                className={`px-2.5 py-1 text-xs rounded font-medium transition-all ${
                  isSel
                    ? "bg-clinical-accent text-white shadow-sm"
                    : "bg-clinical-surface hover:bg-clinical-border/40 text-clinical-text border border-clinical-border"
                }`}
                title={`Planned Gantry: ${b.planned_gantry_angle.toFixed(1)}°`}
              >
                Beam {b.beam_number}: {b.beam_name}
              </button>
            );
          })}
        </div>

        {/* Mode Selector Tabs */}
        <div className="flex items-center gap-1 bg-clinical-surface p-1 rounded-lg border border-clinical-border">
          <button
            onClick={() => setViewMode("delta_pos")}
            className={`px-2.5 py-1 text-xs rounded font-medium transition-colors ${
              viewMode === "delta_pos"
                ? "bg-clinical-accent text-white shadow-xs"
                : "text-clinical-muted hover:text-clinical-text"
            }`}
            title="Recommended: Plot inter-fraction displacement (ΔX, ΔY, ΔZ) from baseline on a shared millimeter scale"
          >
            3D Shift (ΔX, ΔY, ΔZ)
          </button>
          <button
            onClick={() => setViewMode("angles")}
            className={`px-2.5 py-1 text-xs rounded font-medium transition-colors ${
              viewMode === "angles"
                ? "bg-clinical-accent text-white shadow-xs"
                : "text-clinical-muted hover:text-clinical-text"
            }`}
            title="Plot Pitch, Roll, and Support angles on a shared degree scale"
          >
            Angles (Pitch, Roll, Support)
          </button>
          <button
            onClick={() => setViewMode("abs_pos")}
            className={`px-2.5 py-1 text-xs rounded font-medium transition-colors ${
              viewMode === "abs_pos"
                ? "bg-clinical-accent text-white shadow-xs"
                : "text-clinical-muted hover:text-clinical-text"
            }`}
            title="View absolute table coordinates in millimeters"
          >
            Absolute Coordinates (mm)
          </button>
        </div>
      </div>

      {/* SUB-CONTROLS & LEGEND */}
      <div className="flex flex-wrap items-center justify-between gap-3 text-xs px-1">
        <div className="flex items-center gap-2">
          <span className="font-semibold text-clinical-text">{selectedBeamLabel}</span>
          <span className="text-clinical-muted">&bull;</span>
          <span className="text-clinical-muted">{currentSeries.length} Delivered Fractions</span>
        </div>

        {/* Mode-specific toggles / sub-selectors */}
        {viewMode === "delta_pos" && (
          <div className="flex flex-wrap items-center gap-3">
            <button
              onClick={() => setShowDeltaLat(!showDeltaLat)}
              className={`flex items-center gap-1.5 px-2 py-0.5 rounded border text-[11px] transition-all ${
                showDeltaLat
                  ? "bg-cyan-500/10 border-cyan-500/50 text-cyan-700 dark:text-cyan-300 font-semibold"
                  : "border-clinical-border text-clinical-muted opacity-60"
              }`}
            >
              <span className="w-2.5 h-0.5 bg-cyan-500 inline-block rounded" />
              ΔX (Lateral)
            </button>
            <button
              onClick={() => setShowDeltaLong(!showDeltaLong)}
              className={`flex items-center gap-1.5 px-2 py-0.5 rounded border text-[11px] transition-all ${
                showDeltaLong
                  ? "bg-emerald-500/10 border-emerald-500/50 text-emerald-700 dark:text-emerald-300 font-semibold"
                  : "border-clinical-border text-clinical-muted opacity-60"
              }`}
            >
              <span className="w-2.5 h-0.5 bg-emerald-500 inline-block rounded" />
              ΔY (Longitudinal)
            </button>
            <button
              onClick={() => setShowDeltaVert(!showDeltaVert)}
              className={`flex items-center gap-1.5 px-2 py-0.5 rounded border text-[11px] transition-all ${
                showDeltaVert
                  ? "bg-amber-500/10 border-amber-500/50 text-amber-700 dark:text-amber-300 font-semibold"
                  : "border-clinical-border text-clinical-muted opacity-60"
              }`}
            >
              <span className="w-2.5 h-0.5 bg-amber-500 inline-block rounded" />
              ΔZ (Vertical)
            </button>
            <button
              onClick={() => setShowDelta3D(!showDelta3D)}
              className={`flex items-center gap-1.5 px-2 py-0.5 rounded border text-[11px] transition-all ${
                showDelta3D
                  ? "bg-purple-500/10 border-purple-500/50 text-purple-700 dark:text-purple-300 font-semibold"
                  : "border-clinical-border text-clinical-muted opacity-60"
              }`}
            >
              <span className="w-2.5 h-0.5 bg-purple-500 inline-block rounded" />
              3D Vector Shift
            </button>
          </div>
        )}

        {viewMode === "angles" && (
          <div className="flex flex-wrap items-center gap-3">
            <button
              onClick={() => setShowPitch(!showPitch)}
              className={`flex items-center gap-1.5 px-2 py-0.5 rounded border text-[11px] transition-all ${
                showPitch
                  ? "bg-indigo-500/10 border-indigo-500/50 text-indigo-700 dark:text-indigo-300 font-semibold"
                  : "border-clinical-border text-clinical-muted opacity-60"
              }`}
            >
              <span className="w-2.5 h-0.5 bg-indigo-500 inline-block rounded" />
              Pitch (θx)
            </button>
            <button
              onClick={() => setShowRoll(!showRoll)}
              className={`flex items-center gap-1.5 px-2 py-0.5 rounded border text-[11px] transition-all ${
                showRoll
                  ? "bg-rose-500/10 border-rose-500/50 text-rose-700 dark:text-rose-300 font-semibold"
                  : "border-clinical-border text-clinical-muted opacity-60"
              }`}
            >
              <span className="w-2.5 h-0.5 bg-rose-500 inline-block rounded" />
              Roll (θy)
            </button>
            <button
              onClick={() => setShowSupport(!showSupport)}
              className={`flex items-center gap-1.5 px-2 py-0.5 rounded border text-[11px] transition-all ${
                showSupport
                  ? "bg-teal-500/10 border-teal-500/50 text-teal-700 dark:text-teal-300 font-semibold"
                  : "border-clinical-border text-clinical-muted opacity-60"
              }`}
            >
              <span className="w-2.5 h-0.5 bg-teal-500 inline-block rounded" />
              Support Angle (θz)
            </button>
          </div>
        )}

        {viewMode === "abs_pos" && (
          <div className="flex items-center gap-1 bg-clinical-bg/60 p-0.5 rounded border border-clinical-border">
            <button
              onClick={() => setAbsSubMode("stacked")}
              className={`px-2 py-0.5 rounded text-[11px] font-medium transition-colors ${
                absSubMode === "stacked"
                  ? "bg-clinical-surface text-clinical-text font-semibold shadow-xs"
                  : "text-clinical-muted hover:text-clinical-text"
              }`}
            >
              Stacked (All 3 Axes)
            </button>
            <button
              onClick={() => setAbsSubMode("lat")}
              className={`px-2 py-0.5 rounded text-[11px] font-medium transition-colors ${
                absSubMode === "lat"
                  ? "bg-clinical-surface text-cyan-600 font-semibold shadow-xs"
                  : "text-clinical-muted hover:text-clinical-text"
              }`}
            >
              Lateral (X)
            </button>
            <button
              onClick={() => setAbsSubMode("long")}
              className={`px-2 py-0.5 rounded text-[11px] font-medium transition-colors ${
                absSubMode === "long"
                  ? "bg-clinical-surface text-emerald-600 font-semibold shadow-xs"
                  : "text-clinical-muted hover:text-clinical-text"
              }`}
            >
              Longitudinal (Y)
            </button>
            <button
              onClick={() => setAbsSubMode("vert")}
              className={`px-2 py-0.5 rounded text-[11px] font-medium transition-colors ${
                absSubMode === "vert"
                  ? "bg-clinical-surface text-amber-600 font-semibold shadow-xs"
                  : "text-clinical-muted hover:text-clinical-text"
              }`}
            >
              Vertical (Z)
            </button>
          </div>
        )}
      </div>

      {/* CHART CONTAINER */}
      <div className="relative rounded-lg border border-clinical-border bg-clinical-bg/30 p-3 overflow-hidden">
        {viewMode === "delta_pos" && (
          <DeltaPositionChart
            points={currentSeries}
            showLat={showDeltaLat}
            showLong={showDeltaLong}
            showVert={showDeltaVert}
            show3D={showDelta3D}
            hoverIndex={hoverIndex}
            setHoverIndex={setHoverIndex}
            tolerances={data.tolerances}
          />
        )}

        {viewMode === "angles" && (
          <AnglesChart
            points={currentSeries}
            showPitch={showPitch}
            showRoll={showRoll}
            showSupport={showSupport}
            hoverIndex={hoverIndex}
            setHoverIndex={setHoverIndex}
            tolerances={data.tolerances}
          />
        )}

        {viewMode === "abs_pos" && (
          <AbsolutePositionChart
            points={currentSeries}
            subMode={absSubMode}
            hoverIndex={hoverIndex}
            setHoverIndex={setHoverIndex}
          />
        )}

        {/* Hover Tooltip Card */}
        {hoverIndex !== null && currentSeries[hoverIndex] && (
          <HoverTooltipCard
            point={currentSeries[hoverIndex]}
            tolerances={data.tolerances}
          />
        )}
      </div>

      {/* SUMMARY KPI CARDS */}
      <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-6 gap-2.5 pt-1">
        <div className="rounded border border-clinical-border/60 bg-clinical-bg/40 p-2.5">
          <span className="text-[10px] text-clinical-muted uppercase tracking-wider block mb-0.5">
            Max |ΔX| (Lateral)
          </span>
          <span
            className={`text-xs font-bold ${
              data.summary.max_delta_lateral_mm > 3.0 ? "text-amber-500" : "text-clinical-text"
            }`}
          >
            {data.summary.max_delta_lateral_mm.toFixed(2)} mm
          </span>
          <span className="text-[9px] text-clinical-muted block mt-0.5">Tolerance &plusmn;3.0 mm</span>
        </div>

        <div className="rounded border border-clinical-border/60 bg-clinical-bg/40 p-2.5">
          <span className="text-[10px] text-clinical-muted uppercase tracking-wider block mb-0.5">
            Max |ΔY| (Longitudinal)
          </span>
          <span
            className={`text-xs font-bold ${
              data.summary.max_delta_longitudinal_mm > 3.0 ? "text-amber-500" : "text-clinical-text"
            }`}
          >
            {data.summary.max_delta_longitudinal_mm.toFixed(2)} mm
          </span>
          <span className="text-[9px] text-clinical-muted block mt-0.5">Tolerance &plusmn;3.0 mm</span>
        </div>

        <div className="rounded border border-clinical-border/60 bg-clinical-bg/40 p-2.5">
          <span className="text-[10px] text-clinical-muted uppercase tracking-wider block mb-0.5">
            Max |ΔZ| (Vertical)
          </span>
          <span
            className={`text-xs font-bold ${
              data.summary.max_delta_vertical_mm > 3.0 ? "text-amber-500" : "text-clinical-text"
            }`}
          >
            {data.summary.max_delta_vertical_mm.toFixed(2)} mm
          </span>
          <span className="text-[9px] text-clinical-muted block mt-0.5">Tolerance &plusmn;3.0 mm</span>
        </div>

        <div className="rounded border border-clinical-border/60 bg-clinical-bg/40 p-2.5">
          <span className="text-[10px] text-clinical-muted uppercase tracking-wider block mb-0.5">
            Max 3D Vector Shift
          </span>
          <span
            className={`text-xs font-bold ${
              data.summary.max_delta_3d_mm > 5.0 ? "text-purple-400" : "text-clinical-text"
            }`}
          >
            {data.summary.max_delta_3d_mm.toFixed(2)} mm
          </span>
          <span className="text-[9px] text-clinical-muted block mt-0.5">3D Hypotenuse</span>
        </div>

        <div className="rounded border border-clinical-border/60 bg-clinical-bg/40 p-2.5">
          <span className="text-[10px] text-clinical-muted uppercase tracking-wider block mb-0.5">
            Max Pitch / Roll
          </span>
          <span className="text-xs font-bold text-clinical-text">
            {data.summary.max_delta_pitch_deg.toFixed(2)}&deg; / {data.summary.max_delta_roll_deg.toFixed(2)}&deg;
          </span>
          <span className="text-[9px] text-clinical-muted block mt-0.5">Tolerance &plusmn;1.0&deg;</span>
        </div>

        <div className="rounded border border-clinical-border/60 bg-clinical-bg/40 p-2.5">
          <span className="text-[10px] text-clinical-muted uppercase tracking-wider block mb-0.5">
            Max Support Rotation
          </span>
          <span className="text-xs font-bold text-clinical-text">
            {data.summary.max_delta_support_deg.toFixed(2)}&deg;
          </span>
          <span className="text-[9px] text-clinical-muted block mt-0.5">Couch Yaw (θz)</span>
        </div>
      </div>
    </div>
  );
};

/* ========================================================================= */
/* MODE 1: INTER-FRACTION POSITION DRIFT (ΔX, ΔY, ΔZ from Fx 1 Baseline)     */
/* ========================================================================= */
interface DeltaChartProps {
  points: CouchTrendPoint[];
  showLat: boolean;
  showLong: boolean;
  showVert: boolean;
  show3D: boolean;
  hoverIndex: number | null;
  setHoverIndex: (idx: number | null) => void;
  tolerances: {
    translation_action_mm: number;
    translation_tolerance_mm: number;
  };
}

const DeltaPositionChart: React.FC<DeltaChartProps> = ({
  points,
  showLat,
  showLong,
  showVert,
  show3D,
  hoverIndex,
  setHoverIndex,
  tolerances,
}) => {
  const w = 840;
  const h = 260;
  const padL = 50;
  const padR = 25;
  const padT = 20;
  const padB = 35;
  const plotW = w - padL - padR;
  const plotH = h - padT - padB;

  // Compute symmetric or adaptive Y range around 0
  const allVals: number[] = [];
  points.forEach((p) => {
    if (showLat) allVals.push(p.delta_lateral_mm);
    if (showLong) allVals.push(p.delta_longitudinal_mm);
    if (showVert) allVals.push(p.delta_vertical_mm);
    if (show3D) allVals.push(p.delta_3d_mm);
  });
  allVals.push(tolerances.translation_action_mm, -tolerances.translation_action_mm);

  const rawMin = Math.min(...allVals);
  const rawMax = Math.max(...allVals);
  const yMin = Math.floor(Math.min(rawMin - 1.5, -4.0));
  const yMax = Math.ceil(Math.max(rawMax + 1.5, 4.0));

  const n = Math.max(1, points.length - 1);
  const px = (i: number) => padL + (plotW * i) / n;
  const py = (v: number) => padT + plotH * (1 - (v - yMin) / (yMax - yMin));

  const yActionPos = py(tolerances.translation_action_mm);
  const yActionNeg = py(-tolerances.translation_action_mm);

  // Y-axis ticks
  const tickStep = Math.max(2, Math.round((yMax - yMin) / 6));
  const ticks: number[] = [];
  for (let t = yMin; t <= yMax; t += tickStep) {
    ticks.push(t);
  }
  if (!ticks.includes(0)) ticks.push(0);
  ticks.sort((a, b) => a - b);

  return (
    <svg
      width="100%"
      viewBox={`0 0 ${w} ${h}`}
      className="overflow-visible select-none cursor-crosshair"
      onMouseLeave={() => setHoverIndex(null)}
    >
      {/* Background Tolerance Band (Action limit ±3mm) */}
      <rect
        x={padL}
        y={yActionPos}
        width={plotW}
        height={Math.max(0, yActionNeg - yActionPos)}
        fill="rgba(16, 185, 129, 0.04)"
      />

      {/* Gridlines */}
      {ticks.map((val) => (
        <g key={val}>
          <line
            x1={padL}
            y1={py(val)}
            x2={w - padR}
            y2={py(val)}
            stroke={val === 0 ? "rgba(156, 163, 175, 0.5)" : "rgba(128, 128, 128, 0.15)"}
            strokeWidth={val === 0 ? 1.5 : 1}
            strokeDasharray={val === 0 ? undefined : "2 3"}
          />
          <text
            x={padL - 8}
            y={py(val) + 3}
            fontSize={9}
            textAnchor="end"
            fill={val === 0 ? "#9ca3af" : "#6b7280"}
            fontWeight={val === 0 ? "bold" : "normal"}
          >
            {val > 0 ? `+${val}` : val} mm
          </text>
        </g>
      ))}

      {/* Action Limit Threshold Lines (+3mm and -3mm) */}
      <line
        x1={padL}
        y1={yActionPos}
        x2={w - padR}
        y2={yActionPos}
        stroke="#f59e0b"
        strokeWidth={1.2}
        strokeDasharray="4 3"
      />
      <text x={w - padR} y={yActionPos - 4} fontSize={8} textAnchor="end" fill="#f59e0b" fontWeight="bold">
        +3 mm Action
      </text>

      <line
        x1={padL}
        y1={yActionNeg}
        x2={w - padR}
        y2={yActionNeg}
        stroke="#f59e0b"
        strokeWidth={1.2}
        strokeDasharray="4 3"
      />
      <text x={w - padR} y={yActionNeg + 10} fontSize={8} textAnchor="end" fill="#f59e0b" fontWeight="bold">
        -3 mm Action
      </text>

      {/* Polylines for each active series */}
      {show3D && (
        <polyline
          points={points.map((p, i) => `${px(i)},${py(p.delta_3d_mm)}`).join(" ")}
          fill="none"
          stroke="#a855f7"
          strokeWidth={1.5}
          strokeDasharray="3 2"
          opacity={0.7}
        />
      )}

      {showLat && (
        <polyline
          points={points.map((p, i) => `${px(i)},${py(p.delta_lateral_mm)}`).join(" ")}
          fill="none"
          stroke="#06b6d4"
          strokeWidth={2}
        />
      )}

      {showLong && (
        <polyline
          points={points.map((p, i) => `${px(i)},${py(p.delta_longitudinal_mm)}`).join(" ")}
          fill="none"
          stroke="#10b981"
          strokeWidth={2}
        />
      )}

      {showVert && (
        <polyline
          points={points.map((p, i) => `${px(i)},${py(p.delta_vertical_mm)}`).join(" ")}
          fill="none"
          stroke="#f59e0b"
          strokeWidth={2}
        />
      )}

      {/* Points and vertical guide lines */}
      {points.map((p, i) => {
        const x = px(i);
        const isHovered = hoverIndex === i;
        const isVerif = p.is_verification;

        return (
          <g
            key={p.fraction_number}
            onMouseEnter={() => setHoverIndex(i)}
            className="cursor-pointer"
          >
            {/* Transparent hover capture column */}
            <rect
              x={x - (plotW / n) / 2}
              y={padT}
              width={plotW / n}
              height={plotH}
              fill={isHovered ? "rgba(255, 255, 255, 0.05)" : "transparent"}
            />

            {/* Hover guideline */}
            {isHovered && (
              <line
                x1={x}
                y1={padT}
                x2={x}
                y2={padT + plotH}
                stroke="#60a5fa"
                strokeWidth={1.5}
                strokeDasharray="2 2"
              />
            )}

            {/* Dots */}
            {show3D && (
              <circle
                cx={x}
                cy={py(p.delta_3d_mm)}
                r={isHovered ? 4 : 2.5}
                fill="#a855f7"
                stroke="#ffffff"
                strokeWidth={1}
              />
            )}
            {showLat && (
              <circle
                cx={x}
                cy={py(p.delta_lateral_mm)}
                r={isHovered ? 5 : 3.5}
                fill={isVerif ? "#c084fc" : "#06b6d4"}
                stroke="#ffffff"
                strokeWidth={1.5}
              />
            )}
            {showLong && (
              <circle
                cx={x}
                cy={py(p.delta_longitudinal_mm)}
                r={isHovered ? 5 : 3.5}
                fill={isVerif ? "#c084fc" : "#10b981"}
                stroke="#ffffff"
                strokeWidth={1.5}
              />
            )}
            {showVert && (
              <circle
                cx={x}
                cy={py(p.delta_vertical_mm)}
                r={isHovered ? 5 : 3.5}
                fill={isVerif ? "#c084fc" : "#f59e0b"}
                stroke="#ffffff"
                strokeWidth={1.5}
              />
            )}

            {/* X-axis tick labels */}
            <text
              x={x}
              y={h - 12}
              fontSize={9}
              textAnchor="middle"
              fill={isHovered ? "#38bdf8" : "#888"}
              fontWeight={isHovered ? "bold" : "normal"}
            >
              {isVerif ? "Fx 0 (V)" : `Fx ${p.fraction_number}`}
            </text>
          </g>
        );
      })}
    </svg>
  );
};

/* ========================================================================= */
/* MODE 2: COUCH ROTATION ANGLES (Pitch, Roll, Support)                      */
/* ========================================================================= */
interface AnglesChartProps {
  points: CouchTrendPoint[];
  showPitch: boolean;
  showRoll: boolean;
  showSupport: boolean;
  hoverIndex: number | null;
  setHoverIndex: (idx: number | null) => void;
  tolerances: {
    rotation_action_deg: number;
    rotation_tolerance_deg: number;
  };
}

const AnglesChart: React.FC<AnglesChartProps> = ({
  points,
  showPitch,
  showRoll,
  showSupport,
  hoverIndex,
  setHoverIndex,
  tolerances,
}) => {
  const w = 840;
  const h = 260;
  const padL = 50;
  const padR = 25;
  const padT = 20;
  const padB = 35;
  const plotW = w - padL - padR;
  const plotH = h - padT - padB;

  const allVals: number[] = [];
  points.forEach((p) => {
    if (showPitch) allVals.push(p.pitch_deg);
    if (showRoll) allVals.push(p.roll_deg);
    if (showSupport) allVals.push(p.support_angle_deg);
  });
  allVals.push(tolerances.rotation_action_deg, -tolerances.rotation_action_deg);

  const rawMin = Math.min(...allVals);
  const rawMax = Math.max(...allVals);
  const yMin = Math.floor(Math.min(rawMin - 0.5, -1.5));
  const yMax = Math.ceil(Math.max(rawMax + 0.5, 1.5));

  const n = Math.max(1, points.length - 1);
  const px = (i: number) => padL + (plotW * i) / n;
  const py = (v: number) => padT + plotH * (1 - (v - yMin) / (yMax - yMin));

  const yActionPos = py(tolerances.rotation_action_deg);
  const yActionNeg = py(-tolerances.rotation_action_deg);

  const tickStep = 0.5;
  const ticks: number[] = [];
  for (let t = yMin; t <= yMax + 0.001; t += tickStep) {
    ticks.push(Number(t.toFixed(1)));
  }

  return (
    <svg
      width="100%"
      viewBox={`0 0 ${w} ${h}`}
      className="overflow-visible select-none cursor-crosshair"
      onMouseLeave={() => setHoverIndex(null)}
    >
      {/* Action threshold band */}
      <rect
        x={padL}
        y={yActionPos}
        width={plotW}
        height={Math.max(0, yActionNeg - yActionPos)}
        fill="rgba(99, 102, 241, 0.04)"
      />

      {/* Gridlines */}
      {ticks.map((val) => (
        <g key={val}>
          <line
            x1={padL}
            y1={py(val)}
            x2={w - padR}
            y2={py(val)}
            stroke={val === 0 ? "rgba(156, 163, 175, 0.5)" : "rgba(128, 128, 128, 0.15)"}
            strokeWidth={val === 0 ? 1.5 : 1}
            strokeDasharray={val === 0 ? undefined : "2 3"}
          />
          <text
            x={padL - 8}
            y={py(val) + 3}
            fontSize={9}
            textAnchor="end"
            fill={val === 0 ? "#9ca3af" : "#6b7280"}
            fontWeight={val === 0 ? "bold" : "normal"}
          >
            {val > 0 ? `+${val.toFixed(1)}` : val.toFixed(1)}&deg;
          </text>
        </g>
      ))}

      {/* Action Threshold Lines (±1.0 deg) */}
      <line
        x1={padL}
        y1={yActionPos}
        x2={w - padR}
        y2={yActionPos}
        stroke="#f59e0b"
        strokeWidth={1.2}
        strokeDasharray="4 3"
      />
      <text x={w - padR} y={yActionPos - 4} fontSize={8} textAnchor="end" fill="#f59e0b" fontWeight="bold">
        +1.0&deg; Action
      </text>

      <line
        x1={padL}
        y1={yActionNeg}
        x2={w - padR}
        y2={yActionNeg}
        stroke="#f59e0b"
        strokeWidth={1.2}
        strokeDasharray="4 3"
      />
      <text x={w - padR} y={yActionNeg + 10} fontSize={8} textAnchor="end" fill="#f59e0b" fontWeight="bold">
        -1.0&deg; Action
      </text>

      {/* Polylines */}
      {showPitch && (
        <polyline
          points={points.map((p, i) => `${px(i)},${py(p.pitch_deg)}`).join(" ")}
          fill="none"
          stroke="#6366f1"
          strokeWidth={2}
        />
      )}

      {showRoll && (
        <polyline
          points={points.map((p, i) => `${px(i)},${py(p.roll_deg)}`).join(" ")}
          fill="none"
          stroke="#f43f5e"
          strokeWidth={2}
        />
      )}

      {showSupport && (
        <polyline
          points={points.map((p, i) => `${px(i)},${py(p.support_angle_deg)}`).join(" ")}
          fill="none"
          stroke="#14b8a6"
          strokeWidth={2}
        />
      )}

      {/* Points */}
      {points.map((p, i) => {
        const x = px(i);
        const isHovered = hoverIndex === i;

        return (
          <g
            key={p.fraction_number}
            onMouseEnter={() => setHoverIndex(i)}
            className="cursor-pointer"
          >
            <rect
              x={x - (plotW / n) / 2}
              y={padT}
              width={plotW / n}
              height={plotH}
              fill={isHovered ? "rgba(255, 255, 255, 0.05)" : "transparent"}
            />

            {isHovered && (
              <line
                x1={x}
                y1={padT}
                x2={x}
                y2={padT + plotH}
                stroke="#60a5fa"
                strokeWidth={1.5}
                strokeDasharray="2 2"
              />
            )}

            {showPitch && (
              <circle
                cx={x}
                cy={py(p.pitch_deg)}
                r={isHovered ? 5 : 3.5}
                fill="#6366f1"
                stroke="#ffffff"
                strokeWidth={1.5}
              />
            )}

            {showRoll && (
              <circle
                cx={x}
                cy={py(p.roll_deg)}
                r={isHovered ? 5 : 3.5}
                fill="#f43f5e"
                stroke="#ffffff"
                strokeWidth={1.5}
              />
            )}

            {showSupport && (
              <circle
                cx={x}
                cy={py(p.support_angle_deg)}
                r={isHovered ? 5 : 3.5}
                fill="#14b8a6"
                stroke="#ffffff"
                strokeWidth={1.5}
              />
            )}

            <text
              x={x}
              y={h - 12}
              fontSize={9}
              textAnchor="middle"
              fill={isHovered ? "#38bdf8" : "#888"}
              fontWeight={isHovered ? "bold" : "normal"}
            >
              Fx {p.fraction_number}
            </text>
          </g>
        );
      })}
    </svg>
  );
};

/* ========================================================================= */
/* MODE 3: ABSOLUTE COORDINATES (Stacked or Individual Subplots)             */
/* ========================================================================= */
interface AbsoluteChartProps {
  points: CouchTrendPoint[];
  subMode: AbsSubMode;
  hoverIndex: number | null;
  setHoverIndex: (idx: number | null) => void;
}

const AbsolutePositionChart: React.FC<AbsoluteChartProps> = ({
  points,
  subMode,
  hoverIndex,
  setHoverIndex,
}) => {
  if (subMode === "stacked") {
    return (
      <div className="space-y-3 select-none">
        <SingleAxisSubplot
          title="Lateral Position (X)"
          unit="mm"
          color="#06b6d4"
          points={points}
          getValue={(p) => p.lateral_mm}
          hoverIndex={hoverIndex}
          setHoverIndex={setHoverIndex}
          height={110}
        />
        <SingleAxisSubplot
          title="Longitudinal Position (Y)"
          unit="mm"
          color="#10b981"
          points={points}
          getValue={(p) => p.longitudinal_mm}
          hoverIndex={hoverIndex}
          setHoverIndex={setHoverIndex}
          height={110}
        />
        <SingleAxisSubplot
          title="Vertical Position (Z)"
          unit="mm"
          color="#f59e0b"
          points={points}
          getValue={(p) => p.vertical_mm}
          hoverIndex={hoverIndex}
          setHoverIndex={setHoverIndex}
          height={110}
          showXLabels
        />
      </div>
    );
  }

  const axisConfig = {
    lat: { title: "Lateral Position (X)", color: "#06b6d4", getValue: (p: CouchTrendPoint) => p.lateral_mm },
    long: { title: "Longitudinal Position (Y)", color: "#10b981", getValue: (p: CouchTrendPoint) => p.longitudinal_mm },
    vert: { title: "Vertical Position (Z)", color: "#f59e0b", getValue: (p: CouchTrendPoint) => p.vertical_mm },
  }[subMode];

  return (
    <SingleAxisSubplot
      title={axisConfig.title}
      unit="mm"
      color={axisConfig.color}
      points={points}
      getValue={axisConfig.getValue}
      hoverIndex={hoverIndex}
      setHoverIndex={setHoverIndex}
      height={260}
      showXLabels
    />
  );
};

interface SingleAxisSubplotProps {
  title: string;
  unit: string;
  color: string;
  points: CouchTrendPoint[];
  getValue: (p: CouchTrendPoint) => number;
  hoverIndex: number | null;
  setHoverIndex: (idx: number | null) => void;
  height: number;
  showXLabels?: boolean;
}

const SingleAxisSubplot: React.FC<SingleAxisSubplotProps> = ({
  title,
  unit,
  color,
  points,
  getValue,
  hoverIndex,
  setHoverIndex,
  height,
  showXLabels = false,
}) => {
  const w = 840;
  const h = height;
  const padL = 65;
  const padR = 25;
  const padT = 16;
  const padB = showXLabels ? 30 : 12;
  const plotW = w - padL - padR;
  const plotH = h - padT - padB;

  const vals = points.map(getValue);
  const minV = Math.min(...vals);
  const maxV = Math.max(...vals);
  const span = Math.max(0.5, maxV - minV);
  const yMin = Math.floor(minV - span * 0.2);
  const yMax = Math.ceil(maxV + span * 0.2);

  const n = Math.max(1, points.length - 1);
  const px = (i: number) => padL + (plotW * i) / n;
  const py = (v: number) => padT + plotH * (1 - (v - yMin) / (yMax - yMin));

  const ticks = [yMin, (yMin + yMax) / 2, yMax].map((v) => Number(v.toFixed(1)));

  return (
    <div className="relative">
      <div className="flex items-center justify-between text-[11px] font-semibold text-clinical-text px-1 mb-1">
        <span style={{ color }}>{title}</span>
        <span className="text-[10px] text-clinical-muted">Range: {minV.toFixed(1)} to {maxV.toFixed(1)} {unit}</span>
      </div>
      <svg
        width="100%"
        viewBox={`0 0 ${w} ${h}`}
        className="overflow-visible select-none cursor-crosshair"
        onMouseLeave={() => setHoverIndex(null)}
      >
        {ticks.map((val) => (
          <g key={val}>
            <line
              x1={padL}
              y1={py(val)}
              x2={w - padR}
              y2={py(val)}
              stroke="rgba(128, 128, 128, 0.15)"
              strokeWidth={1}
              strokeDasharray="2 3"
            />
            <text x={padL - 8} y={py(val) + 3} fontSize={9} textAnchor="end" fill="#888">
              {val.toFixed(1)} {unit}
            </text>
          </g>
        ))}

        <polyline
          points={points.map((p, i) => `${px(i)},${py(getValue(p))}`).join(" ")}
          fill="none"
          stroke={color}
          strokeWidth={2}
        />

        {points.map((p, i) => {
          const x = px(i);
          const y = py(getValue(p));
          const isHovered = hoverIndex === i;

          return (
            <g
              key={p.fraction_number}
              onMouseEnter={() => setHoverIndex(i)}
              className="cursor-pointer"
            >
              <rect
                x={x - (plotW / n) / 2}
                y={padT}
                width={plotW / n}
                height={plotH}
                fill={isHovered ? "rgba(255, 255, 255, 0.05)" : "transparent"}
              />

              {isHovered && (
                <line
                  x1={x}
                  y1={padT}
                  x2={x}
                  y2={padT + plotH}
                  stroke="#60a5fa"
                  strokeWidth={1.5}
                  strokeDasharray="2 2"
                />
              )}

              <circle
                cx={x}
                cy={y}
                r={isHovered ? 5 : 3.5}
                fill={color}
                stroke="#ffffff"
                strokeWidth={1.5}
              />

              {showXLabels && (
                <text
                  x={x}
                  y={h - 10}
                  fontSize={9}
                  textAnchor="middle"
                  fill={isHovered ? "#38bdf8" : "#888"}
                  fontWeight={isHovered ? "bold" : "normal"}
                >
                  Fx {p.fraction_number}
                </text>
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
};

/* ========================================================================= */
/* HOVER TOOLTIP CARD                                                        */
/* ========================================================================= */
const HoverTooltipCard: React.FC<{
  point: CouchTrendPoint;
  tolerances: { translation_action_mm: number; rotation_action_deg: number };
}> = ({ point, tolerances }) => {
  const isVerif = point.is_verification;
  const isActionExceeded =
    Math.abs(point.delta_lateral_mm) > tolerances.translation_action_mm ||
    Math.abs(point.delta_longitudinal_mm) > tolerances.translation_action_mm ||
    Math.abs(point.delta_vertical_mm) > tolerances.translation_action_mm ||
    Math.abs(point.delta_pitch_deg) > tolerances.rotation_action_deg ||
    Math.abs(point.delta_roll_deg) > tolerances.rotation_action_deg;

  return (
    <div className="absolute top-2 right-2 bg-clinical-surface/95 backdrop-blur-sm border border-clinical-border shadow-lg rounded-lg p-3 text-xs pointer-events-none min-w-[240px] z-10 space-y-2">
      <div className="flex items-center justify-between border-b border-clinical-border/50 pb-1.5">
        <div className="flex items-center gap-1.5">
          <span className="font-bold text-clinical-text">
            {isVerif ? "Pre-Treatment Verification (Dry Run)" : `Fraction ${point.fraction_number}`}
          </span>
        </div>
        <span
          className={`text-[9px] font-bold px-1.5 py-0.2 rounded ${
            isVerif
              ? "bg-purple-100 text-purple-800 dark:bg-purple-950/60 dark:text-purple-300 border border-purple-300"
              : "bg-sky-100 text-sky-800 dark:bg-sky-950/60 dark:text-sky-300 border border-sky-300"
          }`}
        >
          {isVerif ? "VERIFICATION" : "CURATIVE"}
        </span>
      </div>

      <div className="text-[10px] text-clinical-muted flex items-center justify-between">
        <span>Date: {point.treatment_date}</span>
        <span>Time: {point.treatment_time}</span>
      </div>

      {/* Translations */}
      <div className="space-y-1 pt-0.5">
        <div className="text-[10px] uppercase tracking-wider font-semibold text-clinical-muted flex items-center justify-between">
          <span>Table Position</span>
          <span>Actual (Δ from Fx 1)</span>
        </div>
        <div className="flex justify-between items-center text-[11px]">
          <span className="text-cyan-600 dark:text-cyan-400 font-medium">Lateral (X):</span>
          <span className="font-mono">
            {point.lateral_mm.toFixed(2)} mm{" "}
            <span
              className={`font-semibold ${
                Math.abs(point.delta_lateral_mm) > tolerances.translation_action_mm
                  ? "text-amber-500 font-bold"
                  : "text-clinical-muted"
              }`}
            >
              ({point.delta_lateral_mm >= 0 ? "+" : ""}
              {point.delta_lateral_mm.toFixed(2)})
            </span>
          </span>
        </div>
        <div className="flex justify-between items-center text-[11px]">
          <span className="text-emerald-600 dark:text-emerald-400 font-medium">Longitudinal (Y):</span>
          <span className="font-mono">
            {point.longitudinal_mm.toFixed(2)} mm{" "}
            <span
              className={`font-semibold ${
                Math.abs(point.delta_longitudinal_mm) > tolerances.translation_action_mm
                  ? "text-amber-500 font-bold"
                  : "text-clinical-muted"
              }`}
            >
              ({point.delta_longitudinal_mm >= 0 ? "+" : ""}
              {point.delta_longitudinal_mm.toFixed(2)})
            </span>
          </span>
        </div>
        <div className="flex justify-between items-center text-[11px]">
          <span className="text-amber-600 dark:text-amber-400 font-medium">Vertical (Z):</span>
          <span className="font-mono">
            {point.vertical_mm.toFixed(2)} mm{" "}
            <span
              className={`font-semibold ${
                Math.abs(point.delta_vertical_mm) > tolerances.translation_action_mm
                  ? "text-amber-500 font-bold"
                  : "text-clinical-muted"
              }`}
            >
              ({point.delta_vertical_mm >= 0 ? "+" : ""}
              {point.delta_vertical_mm.toFixed(2)})
            </span>
          </span>
        </div>
        <div className="flex justify-between items-center text-[11px] border-t border-clinical-border/30 pt-1">
          <span className="text-purple-600 dark:text-purple-400 font-medium">3D Vector Shift:</span>
          <span className="font-mono font-semibold">{point.delta_3d_mm.toFixed(2)} mm</span>
        </div>
      </div>

      {/* Rotations */}
      <div className="space-y-1 pt-1 border-t border-clinical-border/40">
        <div className="text-[10px] uppercase tracking-wider font-semibold text-clinical-muted flex items-center justify-between">
          <span>6-DoF Angles</span>
          <span>Actual (Δ from Fx 1)</span>
        </div>
        <div className="flex justify-between items-center text-[11px]">
          <span className="text-indigo-600 dark:text-indigo-400 font-medium">Pitch (θx):</span>
          <span className="font-mono">
            {point.pitch_deg.toFixed(2)}&deg;{" "}
            <span className="text-clinical-muted">
              ({point.delta_pitch_deg >= 0 ? "+" : ""}
              {point.delta_pitch_deg.toFixed(2)}&deg;)
            </span>
          </span>
        </div>
        <div className="flex justify-between items-center text-[11px]">
          <span className="text-rose-600 dark:text-rose-400 font-medium">Roll (θy):</span>
          <span className="font-mono">
            {point.roll_deg.toFixed(2)}&deg;{" "}
            <span className="text-clinical-muted">
              ({point.delta_roll_deg >= 0 ? "+" : ""}
              {point.delta_roll_deg.toFixed(2)}&deg;)
            </span>
          </span>
        </div>
        <div className="flex justify-between items-center text-[11px]">
          <span className="text-teal-600 dark:text-teal-400 font-medium">Support (θz):</span>
          <span className="font-mono">
            {point.support_angle_deg.toFixed(2)}&deg;{" "}
            <span className="text-clinical-muted">
              ({point.delta_support_deg >= 0 ? "+" : ""}
              {point.delta_support_deg.toFixed(2)}&deg;)
            </span>
          </span>
        </div>
      </div>

      {isActionExceeded && (
        <div className="flex items-center gap-1.5 text-[10px] font-semibold text-amber-500 bg-amber-500/10 p-1 rounded">
          <AlertTriangle size={12} className="shrink-0" />
          <span>Action threshold (&plusmn;3mm or &plusmn;1&deg;) exceeded</span>
        </div>
      )}
    </div>
  );
};
