import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Layers,
  Loader2,
  RefreshCw,
  Sliders,
} from "lucide-react";
import toast from "react-hot-toast";
import { calculatePlanDVH, getPlanDVH } from "../api/client";
import type { PlanDVHResponse } from "../types";

interface RobustnessDVHCardProps {
  planId: number;
  hasMCDose?: boolean;
  onLaunchMC?: () => void;
  className?: string;
}

export function RobustnessDVHCard({
  planId,
  hasMCDose = true,
  onLaunchMC,
  className = "",
}: RobustnessDVHCardProps) {
  const [data, setData] = useState<PlanDVHResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [recalculating, setRecalculating] = useState(false);
  const [selectedRois, setSelectedRois] = useState<Set<number>>(new Set());
  const [showConfig, setShowConfig] = useState(false);

  // Uncertainty parameters
  const [setupMm, setSetupMm] = useState(3.0);
  const [rangePct, setRangePct] = useState(3.0);
  const [numScenarios, setNumScenarios] = useState<9 | 21>(9);

  // Graph hover crosshair
  const [hoverX, setHoverX] = useState<number | null>(null);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const clipId = useId();

  const loadDVH = useCallback(async () => {
    try {
      setLoading(true);
      const res = await getPlanDVH(planId);
      setData(res);
      setSetupMm(res.setup_uncertainty_mm);
      setRangePct(res.range_uncertainty_pct);
      setNumScenarios(res.num_scenarios === 21 ? 21 : 9);

      // Select targets and top 3 OARs by default
      const defaultSelected = new Set<number>();
      let oarCount = 0;
      for (const r of res.rois) {
        if (r.is_target) {
          defaultSelected.add(r.roi_number);
        } else if (r.type === "OAR" && oarCount < 3) {
          defaultSelected.add(r.roi_number);
          oarCount++;
        }
      }
      // If none matched, select all
      if (defaultSelected.size === 0 && res.rois.length > 0) {
        res.rois.slice(0, 4).forEach((r) => defaultSelected.add(r.roi_number));
      }
      setSelectedRois(defaultSelected);
    } catch (err: any) {
      // 404 or missing dose is expected if MC hasn't run yet
      setData(null);
    } finally {
      setLoading(false);
    }
  }, [planId]);

  useEffect(() => {
    loadDVH();
  }, [loadDVH]);

  const handleRecalculate = async () => {
    try {
      setRecalculating(true);
      const res = await calculatePlanDVH(planId, {
        setup_uncertainty_mm: setupMm,
        range_uncertainty_pct: rangePct,
        num_scenarios: numScenarios,
      });
      setData(res);
      toast.success(
        `Robustness DVH re-evaluated (${res.num_scenarios} scenarios: ±${setupMm}mm, ±${rangePct}%)`
      );
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || "Failed to recalculate robustness DVH");
    } finally {
      setRecalculating(false);
    }
  };

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
    setSelectedRois(new Set(data.rois.map((r) => r.roi_number)));
  };

  const selectTargetsOnly = () => {
    if (!data) return;
    setSelectedRois(new Set(data.rois.filter((r) => r.is_target).map((r) => r.roi_number)));
  };

  const selectOarsOnly = () => {
    if (!data) return;
    setSelectedRois(new Set(data.rois.filter((r) => !r.is_target && r.type === "OAR").map((r) => r.roi_number)));
  };

  // Compute SVG dimensions and scales
  const chartGeometry = useMemo(() => {
    if (!data || data.rois.length === 0) return null;
    const firstRoi = data.rois[0];
    const doseBins = firstRoi.dvh.dose_bins_gy;
    const maxDose = doseBins[doseBins.length - 1] || 60;

    const width = 800;
    const height = 360;
    const margin = { top: 20, right: 30, bottom: 40, left: 50 };
    const innerW = width - margin.left - margin.right;
    const innerH = height - margin.top - margin.bottom;

    const scaleX = (doseGy: number) => margin.left + (doseGy / maxDose) * innerW;
    const scaleY = (volPct: number) => margin.top + innerH - (volPct / 100) * innerH;

    const invertX = (pixelX: number) => {
      const clamped = Math.max(margin.left, Math.min(width - margin.right, pixelX));
      return ((clamped - margin.left) / innerW) * maxDose;
    };

    return { width, height, margin, innerW, innerH, maxDose, scaleX, scaleY, invertX, doseBins };
  }, [data]);

  const handleMouseMove = (e: React.MouseEvent<SVGSVGElement>) => {
    if (!chartGeometry || !svgRef.current) return;
    const rect = svgRef.current.getBoundingClientRect();
    const pixelX = ((e.clientX - rect.left) / rect.width) * chartGeometry.width;
    if (
      pixelX >= chartGeometry.margin.left &&
      pixelX <= chartGeometry.width - chartGeometry.margin.right
    ) {
      setHoverX(chartGeometry.invertX(pixelX));
    } else {
      setHoverX(null);
    }
  };

  const handleMouseLeave = () => setHoverX(null);

  if (loading) {
    return (
      <div className={`bg-clinical-surface border border-clinical-border rounded-lg p-6 ${className}`}>
        <div className="flex items-center justify-center gap-2 text-xs text-clinical-muted py-12">
          <Loader2 size={16} className="animate-spin text-clinical-accent" />
          <span>Loading openMCsquare robustness &amp; DVH analysis…</span>
        </div>
      </div>
    );
  }

  if (!hasMCDose || !data || data.rois.length === 0) {
    return (
      <div className={`bg-clinical-surface border border-clinical-border rounded-lg p-6 ${className}`}>
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <Activity className="text-clinical-accent" size={18} />
            <h3 className="text-sm font-semibold text-clinical-text">
              openMCsquare Robustness Analysis &amp; DVH Prediction
            </h3>
          </div>
          <span className="text-[10px] px-2 py-0.5 rounded bg-amber-100 dark:bg-amber-950/60 text-amber-700 dark:text-amber-300 font-medium border border-amber-300 dark:border-amber-700">
            Pending Simulation
          </span>
        </div>

        <div className="rounded-lg border border-dashed border-clinical-border p-8 text-center bg-clinical-bg/30">
          <Layers size={28} className="mx-auto text-clinical-muted mb-2" />
          <h4 className="text-xs font-semibold text-clinical-text">
            Robustness &amp; DVH Predictions Not Yet Computed
          </h4>
          <p className="text-xs text-clinical-muted max-w-md mx-auto mt-1 mb-4">
            Monte Carlo secondary recalculation provides clinical DVH curves and scenario-based
            uncertainty envelopes (±3 mm setup, ±3% range) across all segmented structures.
          </p>
          {onLaunchMC && (
            <button
              onClick={onLaunchMC}
              className="px-4 py-2 bg-clinical-accent text-white text-xs font-medium rounded hover:bg-clinical-accent/90 transition-colors shadow-xs"
            >
              Run openMCsquare Secondary Calculation
            </button>
          )}
        </div>
      </div>
    );
  }

  // Active ROIs for plotting
  const activeRois = data.rois.filter((r) => selectedRois.has(r.roi_number));

  return (
    <div className={`bg-clinical-surface border border-clinical-border rounded-lg p-5 shadow-xs ${className}`}>
      {/* Header Bar */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-3 pb-4 border-b border-clinical-border/60">
        <div>
          <div className="flex items-center gap-2 flex-wrap">
            <Activity className="text-clinical-accent" size={18} />
            <h3 className="text-sm font-bold text-clinical-text">
              openMCsquare Robustness &amp; DVH Predictions
            </h3>
            <span className="text-[10px] font-mono px-2 py-0.5 rounded bg-blue-50 dark:bg-blue-950/50 text-blue-700 dark:text-blue-300 border border-blue-200 dark:border-blue-800">
              {data.num_scenarios} Scenarios (±{data.setup_uncertainty_mm}mm Setup, ±{data.range_uncertainty_pct}% Range)
            </span>
            <span className="text-[10px] px-2 py-0.5 rounded bg-green-50 dark:bg-green-950/50 text-green-700 dark:text-green-300 border border-green-200 dark:border-green-800">
              openMCsquare MC Secondary
            </span>
          </div>
          <p className="text-[11px] text-clinical-muted mt-1">
            Dose-volume histogram under nominal delivery and worst-case geometric/range uncertainty envelopes.
            {data.prescription_dose_gy ? ` Reference Rx: ${data.prescription_dose_gy.toFixed(1)} Gy.` : ""}
          </p>
        </div>

        {/* Action Controls */}
        <div className="flex items-center gap-2">
          <button
            onClick={() => setShowConfig(!showConfig)}
            className="flex items-center gap-1.5 px-2.5 py-1.5 rounded border border-clinical-border bg-clinical-bg text-xs font-medium text-clinical-text hover:bg-clinical-border/40 transition-colors"
            title="Configure setup & range uncertainty parameters"
          >
            <Sliders size={13} />
            <span>Robustness Settings</span>
            {showConfig ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
          </button>

          <button
            onClick={handleRecalculate}
            disabled={recalculating}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-clinical-accent text-white rounded text-xs font-medium hover:bg-clinical-accent/90 disabled:opacity-50 transition-colors shadow-xs"
            title="Recalculate robustness scenarios"
          >
            <RefreshCw size={13} className={recalculating ? "animate-spin" : ""} />
            <span>{recalculating ? "Evaluating Scenarios…" : "Re-evaluate"}</span>
          </button>
        </div>
      </div>

      {/* Uncertainty Configuration Drawer */}
      {showConfig && (
        <div className="my-3 p-3.5 rounded-lg border border-clinical-border bg-clinical-bg/60 grid grid-cols-1 sm:grid-cols-3 gap-4 text-xs">
          <div>
            <label className="block text-[11px] font-semibold text-clinical-muted mb-1.5">
              Isocenter Setup Uncertainty (± mm)
            </label>
            <div className="flex items-center gap-1.5">
              {[2.0, 3.0, 5.0].map((val) => (
                <button
                  key={val}
                  type="button"
                  onClick={() => setSetupMm(val)}
                  className={`px-2 py-1 rounded text-xs font-medium border transition-colors ${
                    setupMm === val
                      ? "bg-clinical-accent text-white border-clinical-accent"
                      : "bg-clinical-surface border-clinical-border text-clinical-text hover:bg-clinical-border/30"
                  }`}
                >
                  {val} mm
                </button>
              ))}
              <input
                type="number"
                step="0.5"
                min="0.5"
                max="10.0"
                value={setupMm}
                onChange={(e) => setSetupMm(parseFloat(e.target.value) || 3.0)}
                className="w-16 px-2 py-1 rounded border border-clinical-border bg-clinical-surface text-clinical-text text-xs"
              />
            </div>
            <span className="text-[10px] text-clinical-muted mt-1 block">
              Cardinal shifts (±X, ±Y, ±Z)
            </span>
          </div>

          <div>
            <label className="block text-[11px] font-semibold text-clinical-muted mb-1.5">
              Proton Range Uncertainty (± %)
            </label>
            <div className="flex items-center gap-1.5">
              {[2.0, 3.0, 3.5, 5.0].map((val) => (
                <button
                  key={val}
                  type="button"
                  onClick={() => setRangePct(val)}
                  className={`px-2 py-1 rounded text-xs font-medium border transition-colors ${
                    rangePct === val
                      ? "bg-clinical-accent text-white border-clinical-accent"
                      : "bg-clinical-surface border-clinical-border text-clinical-text hover:bg-clinical-border/30"
                  }`}
                >
                  {val}%
                </button>
              ))}
              <input
                type="number"
                step="0.5"
                min="0.5"
                max="10.0"
                value={rangePct}
                onChange={(e) => setRangePct(parseFloat(e.target.value) || 3.0)}
                className="w-16 px-2 py-1 rounded border border-clinical-border bg-clinical-surface text-clinical-text text-xs"
              />
            </div>
            <span className="text-[10px] text-clinical-muted mt-1 block">
              CT density / stopping power scaling
            </span>
          </div>

          <div>
            <label className="block text-[11px] font-semibold text-clinical-muted mb-1.5">
              Evaluation Scenarios
            </label>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => setNumScenarios(9)}
                className={`flex-1 px-2.5 py-1 rounded text-xs font-medium border transition-colors ${
                  numScenarios === 9
                    ? "bg-clinical-accent text-white border-clinical-accent"
                    : "bg-clinical-surface border-clinical-border text-clinical-text hover:bg-clinical-border/30"
                }`}
              >
                9 Scenarios (Fast)
              </button>
              <button
                type="button"
                onClick={() => setNumScenarios(21)}
                className={`flex-1 px-2.5 py-1 rounded text-xs font-medium border transition-colors ${
                  numScenarios === 21
                    ? "bg-clinical-accent text-white border-clinical-accent"
                    : "bg-clinical-surface border-clinical-border text-clinical-text hover:bg-clinical-border/30"
                }`}
              >
                21 Scenarios (Compound)
              </button>
            </div>
            <span className="text-[10px] text-clinical-muted mt-1 block">
              {numScenarios === 9 ? "6 Setup shifts + 2 Range shifts + Nominal" : "All compound diagonal combinations"}
            </span>
          </div>
        </div>
      )}

      {/* ROI Multi-Select Filter Bar */}
      <div className="my-3 flex items-center justify-between flex-wrap gap-2 text-xs">
        <div className="flex items-center gap-2">
          <span className="text-[11px] font-semibold text-clinical-muted uppercase tracking-wider">
            Segmented ROIs ({data.rois.length}):
          </span>
          <button
            onClick={selectAll}
            className="text-[11px] text-clinical-accent hover:underline font-medium"
          >
            Select All
          </button>
          <span className="text-clinical-border">|</span>
          <button
            onClick={selectTargetsOnly}
            className="text-[11px] text-clinical-accent hover:underline font-medium"
          >
            Targets
          </button>
          <span className="text-clinical-border">|</span>
          <button
            onClick={selectOarsOnly}
            className="text-[11px] text-clinical-accent hover:underline font-medium"
          >
            OARs Only
          </button>
        </div>

        {/* Legend Indicator */}
        <div className="flex items-center gap-4 text-[11px] text-clinical-muted">
          <div className="flex items-center gap-1.5">
            <span className="inline-block w-4 h-0.5 bg-clinical-text" />
            <span>MC Nominal</span>
          </div>
          <div className="flex items-center gap-1.5">
            <span
              className="inline-block w-4 h-0.5"
              style={{
                borderBottom: "1.5px dashed var(--color-clinical-text, #888)",
              }}
            />
            <span>TPS Plan</span>
          </div>
          <div className="flex items-center gap-1.5">
            <span className="inline-block w-3.5 h-3 rounded-xs bg-blue-500/30 border border-blue-500/50" />
            <span>Robustness Band (Min/Max)</span>
          </div>
        </div>
      </div>

      {/* ROI Toggle Chips */}
      <div className="flex flex-wrap gap-1.5 mb-4">
        {data.rois.map((roi) => {
          const isSelected = selectedRois.has(roi.roi_number);
          return (
            <button
              key={roi.roi_number}
              onClick={() => toggleRoi(roi.roi_number)}
              className={`flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs transition-all border ${
                isSelected
                  ? "bg-clinical-surface border-clinical-border text-clinical-text font-medium shadow-2xs"
                  : "bg-clinical-bg/40 border-clinical-border/40 text-clinical-muted opacity-60 hover:opacity-90"
              }`}
            >
              <span
                className="w-2.5 h-2.5 rounded-full shrink-0"
                style={{
                  backgroundColor: roi.color,
                  boxShadow: isSelected ? `0 0 4px ${roi.color}` : "none",
                }}
              />
              <span className="truncate max-w-[140px]">{roi.name}</span>
              <span
                className={`text-[9px] px-1 py-0.2 rounded font-bold ${
                  roi.is_target
                    ? "bg-red-100 dark:bg-red-950/80 text-red-700 dark:text-red-300"
                    : "bg-blue-100 dark:bg-blue-950/80 text-blue-700 dark:text-blue-300"
                }`}
              >
                {roi.is_target ? "Target" : roi.type}
              </span>
            </button>
          );
        })}
      </div>

      {/* Interactive DVH Chart (SVG) */}
      {chartGeometry && (
        <div className="relative rounded-lg border border-clinical-border bg-clinical-bg/40 p-2 overflow-hidden">
          <svg
            ref={svgRef}
            viewBox={`0 0 ${chartGeometry.width} ${chartGeometry.height}`}
            className="w-full h-auto cursor-crosshair select-none"
            onMouseMove={handleMouseMove}
            onMouseLeave={handleMouseLeave}
          >
            <defs>
              <clipPath id={clipId}>
                <rect
                  x={chartGeometry.margin.left}
                  y={chartGeometry.margin.top}
                  width={chartGeometry.innerW}
                  height={chartGeometry.innerH}
                />
              </clipPath>
            </defs>

            {/* Grid Lines - Volume (%) */}
            {[0, 20, 40, 60, 80, 100].map((vol) => {
              const y = chartGeometry.scaleY(vol);
              return (
                <g key={vol}>
                  <line
                    x1={chartGeometry.margin.left}
                    y1={y}
                    x2={chartGeometry.width - chartGeometry.margin.right}
                    y2={y}
                    stroke="currentColor"
                    strokeOpacity={vol === 0 || vol === 100 ? 0.25 : 0.1}
                    strokeDasharray={vol === 0 || vol === 100 ? undefined : "3 3"}
                  />
                  <text
                    x={chartGeometry.margin.left - 8}
                    y={y + 3.5}
                    textAnchor="end"
                    fontSize={10}
                    fill="currentColor"
                    opacity={0.6}
                  >
                    {vol}%
                  </text>
                </g>
              );
            })}

            {/* Grid Lines - Dose (Gy) */}
            {Array.from(
              { length: Math.floor(chartGeometry.maxDose / 10) + 1 },
              (_, i) => i * 10
            ).map((dose) => {
              const x = chartGeometry.scaleX(dose);
              return (
                <g key={dose}>
                  <line
                    x1={x}
                    y1={chartGeometry.margin.top}
                    x2={x}
                    y2={chartGeometry.height - chartGeometry.margin.bottom}
                    stroke="currentColor"
                    strokeOpacity={dose === 0 ? 0.25 : 0.1}
                    strokeDasharray={dose === 0 ? undefined : "3 3"}
                  />
                  <text
                    x={x}
                    y={chartGeometry.height - chartGeometry.margin.bottom + 15}
                    textAnchor="middle"
                    fontSize={10}
                    fill="currentColor"
                    opacity={0.6}
                  >
                    {dose}
                  </text>
                </g>
              );
            })}

            {/* Prescription Dose Marker Line if available */}
            {data.prescription_dose_gy && data.prescription_dose_gy <= chartGeometry.maxDose && (
              <g>
                <line
                  x1={chartGeometry.scaleX(data.prescription_dose_gy)}
                  y1={chartGeometry.margin.top}
                  x2={chartGeometry.scaleX(data.prescription_dose_gy)}
                  y2={chartGeometry.height - chartGeometry.margin.bottom}
                  stroke="#ef4444"
                  strokeWidth={1.5}
                  strokeDasharray="4 4"
                  strokeOpacity={0.7}
                />
                <text
                  x={chartGeometry.scaleX(data.prescription_dose_gy) + 4}
                  y={chartGeometry.margin.top + 12}
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
              x={chartGeometry.margin.left + chartGeometry.innerW / 2}
              y={chartGeometry.height - 6}
              textAnchor="middle"
              fontSize={11}
              fontWeight="600"
              fill="currentColor"
              opacity={0.8}
            >
              Absorbed Dose (Gy)
            </text>
            <text
              transform={`rotate(-90)`}
              x={-(chartGeometry.margin.top + chartGeometry.innerH / 2)}
              y={14}
              textAnchor="middle"
              fontSize={11}
              fontWeight="600"
              fill="currentColor"
              opacity={0.8}
            >
              Volume (%)
            </text>

            {/* Curves and Shaded Robustness Envelopes */}
            <g clipPath={`url(#${clipId})`}>
              {activeRois.map((roi) => {
                const bins = roi.dvh.dose_bins_gy;
                const nom = roi.dvh.mc_nominal_volume_pct;
                const minC = roi.dvh.mc_min_volume_pct;
                const maxC = roi.dvh.mc_max_volume_pct;
                const tps = roi.dvh.tps_volume_pct;

                // Build Robustness Band Polygon Path
                // Forward along max curve, backward along min curve
                const bandPoints: string[] = [];
                for (let i = 0; i < bins.length; i++) {
                  bandPoints.push(`${chartGeometry.scaleX(bins[i])},${chartGeometry.scaleY(maxC[i])}`);
                }
                for (let i = bins.length - 1; i >= 0; i--) {
                  bandPoints.push(`${chartGeometry.scaleX(bins[i])},${chartGeometry.scaleY(minC[i])}`);
                }
                const bandPath = `M ${bandPoints.join(" L ")} Z`;

                // Build Nominal Line Path
                const nomPoints = bins
                  .map((d, i) => `${chartGeometry.scaleX(d)},${chartGeometry.scaleY(nom[i])}`)
                  .join(" L ");
                const nomPath = `M ${nomPoints}`;

                // Build TPS Line Path
                const tpsPoints = tps
                  ? bins
                      .map((d, i) => `${chartGeometry.scaleX(d)},${chartGeometry.scaleY(tps[i])}`)
                      .join(" L ")
                  : null;
                const tpsPath = tpsPoints ? `M ${tpsPoints}` : null;

                return (
                  <g key={roi.roi_number}>
                    {/* Shaded Uncertainty Envelope */}
                    <path
                      d={bandPath}
                      fill={roi.color}
                      fillOpacity={0.2}
                      stroke="none"
                    />

                    {/* TPS Planned Reference Curve (Dashed) */}
                    {tpsPath && (
                      <path
                        d={tpsPath}
                        fill="none"
                        stroke={roi.color}
                        strokeWidth={1.5}
                        strokeDasharray="4 3"
                        strokeOpacity={0.65}
                      />
                    )}

                    {/* MCsquare Nominal Curve (Solid) */}
                    <path
                      d={nomPath}
                      fill="none"
                      stroke={roi.color}
                      strokeWidth={2.2}
                      strokeLinecap="round"
                    />
                  </g>
                );
              })}

              {/* Hover Crosshair Vertical Line */}
              {hoverX !== null && (
                <line
                  x1={chartGeometry.scaleX(hoverX)}
                  y1={chartGeometry.margin.top}
                  x2={chartGeometry.scaleX(hoverX)}
                  y2={chartGeometry.height - chartGeometry.margin.bottom}
                  stroke="currentColor"
                  strokeWidth={1}
                  strokeDasharray="2 2"
                  strokeOpacity={0.8}
                />
              )}
            </g>
          </svg>

          {/* Floating Hover Tooltip */}
          {hoverX !== null && (
            <div
              className="absolute top-3 right-3 bg-clinical-surface/95 border border-clinical-border rounded-md shadow-md p-2.5 text-xs pointer-events-none z-10 max-w-xs backdrop-blur-xs"
            >
              <div className="font-semibold text-clinical-text border-b border-clinical-border/60 pb-1 mb-1.5 flex justify-between">
                <span>Dose: {hoverX.toFixed(1)} Gy</span>
                {data.prescription_dose_gy && (
                  <span className="text-[10px] text-clinical-muted">
                    {((hoverX / data.prescription_dose_gy) * 100).toFixed(0)}% Rx
                  </span>
                )}
              </div>
              <div className="space-y-1">
                {activeRois.map((roi) => {
                  const bins = roi.dvh.dose_bins_gy;
                  // Binary search closest index
                  let idx = 0;
                  let minDiff = 999;
                  for (let i = 0; i < bins.length; i++) {
                    const diff = Math.abs(bins[i] - hoverX);
                    if (diff < minDiff) {
                      minDiff = diff;
                      idx = i;
                    }
                  }
                  const nomVol = roi.dvh.mc_nominal_volume_pct[idx] ?? 0;
                  const minVol = roi.dvh.mc_min_volume_pct[idx] ?? 0;
                  const maxVol = roi.dvh.mc_max_volume_pct[idx] ?? 0;
                  const tpsVol = roi.dvh.tps_volume_pct?.[idx];

                  return (
                    <div key={roi.roi_number} className="flex items-center justify-between gap-3 text-[11px]">
                      <div className="flex items-center gap-1.5 truncate">
                        <span className="w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: roi.color }} />
                        <span className="truncate text-clinical-text">{roi.name}:</span>
                      </div>
                      <div className="font-mono text-right shrink-0">
                        <span className="font-semibold text-clinical-text">{nomVol.toFixed(1)}%</span>
                        <span className="text-clinical-muted text-[10px] ml-1">
                          [{minVol.toFixed(0)}-{maxVol.toFixed(0)}%]
                        </span>
                        {tpsVol !== undefined && (
                          <span className="text-clinical-muted text-[10px] ml-1">
                            (TPS: {tpsVol.toFixed(1)}%)
                          </span>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Clinical Metrics & Robustness Evaluation Table */}
      <div className="mt-4 pt-3 border-t border-clinical-border/60">
        <div className="flex items-center justify-between mb-2.5">
          <h4 className="text-xs font-bold text-clinical-text uppercase tracking-wider flex items-center gap-1.5">
            <CheckCircle2 size={14} className="text-green-600 dark:text-green-400" />
            <span>Robustness Scenarios &amp; Dosimetric Criteria Summary</span>
          </h4>
          <span className="text-[11px] text-clinical-muted">
            Uncertainty bounds show [Worst-case Min — Max] across all {data.num_scenarios} scenarios
          </span>
        </div>

        <div className="overflow-x-auto rounded border border-clinical-border bg-clinical-surface">
          <table className="w-full text-xs text-left border-collapse">
            <thead>
              <tr className="border-b border-clinical-border bg-clinical-bg/60 text-clinical-muted text-[10px] uppercase font-semibold">
                <th className="py-2 px-3">Structure</th>
                <th className="py-2 px-3">Type</th>
                <th className="py-2 px-3">Vol (cc)</th>
                <th className="py-2 px-3">TPS D95%</th>
                <th className="py-2 px-3">openMCsquare D95% [Min — Max]</th>
                <th className="py-2 px-3">MC D50% (Median)</th>
                <th className="py-2 px-3">MC D2% (Hot Spot)</th>
                <th className="py-2 px-3">MC Dmean</th>
                <th className="py-2 px-3">Robustness Status</th>
              </tr>
            </thead>
            <tbody>
              {data.rois.map((roi) => {
                const isSelected = selectedRois.has(roi.roi_number);
                const m = roi.metrics;

                return (
                  <tr
                    key={roi.roi_number}
                    className={`border-b border-clinical-border/40 hover:bg-clinical-bg/40 transition-colors cursor-pointer ${
                      isSelected ? "" : "opacity-50"
                    }`}
                    onClick={() => toggleRoi(roi.roi_number)}
                  >
                    <td className="py-2 px-3 font-medium text-clinical-text flex items-center gap-2">
                      <span className="w-2.5 h-2.5 rounded-full shrink-0" style={{ backgroundColor: roi.color }} />
                      <span>{roi.name}</span>
                    </td>
                    <td className="py-2 px-3">
                      <span
                        className={`text-[9px] px-1.5 py-0.5 rounded font-bold uppercase ${
                          roi.is_target
                            ? "bg-red-100 dark:bg-red-950 text-red-700 dark:text-red-300"
                            : "bg-blue-100 dark:bg-blue-950 text-blue-700 dark:text-blue-300"
                        }`}
                      >
                        {roi.type}
                      </span>
                    </td>
                    <td className="py-2 px-3 font-mono text-clinical-muted">
                      {roi.volume_cc.toFixed(1)}
                    </td>
                    <td className="py-2 px-3 font-mono font-medium text-clinical-text">
                      {m.d95.tps !== null && m.d95.tps !== undefined ? `${m.d95.tps.toFixed(1)} Gy` : "—"}
                    </td>
                    <td className="py-2 px-3 font-mono font-semibold">
                      <span className="text-clinical-text">{m.d95.mc_nominal.toFixed(1)} Gy</span>
                      <span className="text-clinical-muted text-[10px] font-normal ml-1.5">
                        [{m.d95.mc_min.toFixed(1)} – {m.d95.mc_max.toFixed(1)}]
                      </span>
                    </td>
                    <td className="py-2 px-3 font-mono text-clinical-text">
                      {m.d50.mc_nominal.toFixed(1)} Gy
                    </td>
                    <td className="py-2 px-3 font-mono text-clinical-text">
                      {m.d2.mc_nominal.toFixed(1)} Gy
                    </td>
                    <td className="py-2 px-3 font-mono text-clinical-muted">
                      {m.d_mean.mc_nominal.toFixed(1)} Gy
                    </td>
                    <td className="py-2 px-3">
                      {roi.robustness_pass ? (
                        <span className="inline-flex items-center gap-1 text-[10px] font-bold px-2 py-0.5 rounded bg-green-100 dark:bg-green-950/80 text-green-700 dark:text-green-300 border border-green-300 dark:border-green-800">
                          <CheckCircle2 size={10} /> ROBUST PASS
                        </span>
                      ) : (
                        <span className="inline-flex items-center gap-1 text-[10px] font-bold px-2 py-0.5 rounded bg-amber-100 dark:bg-amber-950/80 text-amber-700 dark:text-amber-300 border border-amber-300 dark:border-amber-800" title={roi.robustness_note || ""}>
                          <AlertTriangle size={10} /> ACTION
                        </span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
