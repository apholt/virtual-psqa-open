import React, { useEffect, useState } from "react";
import {
  Activity,
  Layers,
  Clock,
  MapPin,
  User,
  Sliders,
  Calendar,
  Zap,
  Loader2,
  ShieldCheck,
  AlertTriangle,
  Upload,
} from "lucide-react";
import toast from "react-hot-toast";
import {
  getPlanFractionLog,
  getPlanFractionLogs,
  runJob,
  getJob,
  reuploadFractionRecord,
} from "../api/client";
import type { FractionLogReport, FractionLogSummary, FractionBeamLog } from "../types";

interface Props {
  planId: number;
}

export const FractionLogViewer: React.FC<Props> = ({ planId }) => {
  const [summaries, setSummaries] = useState<FractionLogSummary[]>([]);
  const [selectedFx, setSelectedFx] = useState<number>(1);
  const [logReport, setLogReport] = useState<FractionLogReport | null>(null);
  const [selectedBeamIndex, setSelectedBeamIndex] = useState<number>(0);
  const [loading, setLoading] = useState<boolean>(true);
  const [runningLogRecon, setRunningLogRecon] = useState<boolean>(false);
  const [reuploading, setReuploading] = useState<boolean>(false);

  const handleFileReupload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setReuploading(true);
    const toastId = toast.loading(`Uploading and re-analyzing replacement record for Fraction ${selectedFx}…`);
    try {
      const res = await reuploadFractionRecord(planId, selectedFx, file);
      if (res.is_interrupted) {
        toast.error(`Replacement uploaded, but still contains interruption flags: ${res.interruption_reason}`, {
          id: toastId,
          duration: 6000,
        });
      } else {
        toast.success(`Complete merged record successfully processed for Fraction ${selectedFx}!`, { id: toastId });
      }
      const sums = await getPlanFractionLogs(planId);
      setSummaries(sums);
      const rep = await getPlanFractionLog(planId, selectedFx);
      setLogReport(rep);
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || "Failed to re-upload record", { id: toastId });
    } finally {
      setReuploading(false);
      e.target.value = "";
    }
  };

  useEffect(() => {
    let live = true;
    setLoading(true);
    getPlanFractionLogs(planId)
      .then((sums) => {
        if (!live) return;
        setSummaries(sums);
        if (sums.length > 0) {
          const defaultFx = sums[sums.length - 1].fraction_number;
          setSelectedFx(defaultFx);
        } else {
          setLoading(false);
        }
      })
      .catch(() => {
        if (!live) return;
        setLoading(false);
      });
    return () => {
      live = false;
    };
  }, [planId]);

  useEffect(() => {
    if (!selectedFx) return;
    let live = true;
    setLoading(true);
    getPlanFractionLog(planId, selectedFx)
      .then((rep) => {
        if (!live) return;
        setLogReport(rep);
        setSelectedBeamIndex(0);
        setLoading(false);
      })
      .catch(() => {
        if (!live) return;
        setLoading(false);
      });
    return () => {
      live = false;
    };
  }, [planId, selectedFx]);

  const handleRunLogRecon = async () => {
    if (!selectedFx) return;
    try {
      setRunningLogRecon(true);
      const job = await runJob(planId, "log_reconstruction", selectedFx);
      const interval = setInterval(async () => {
        try {
          const status = await getJob(job.id);
          if (status.status === "complete" || status.status === "error" || status.status === "cancelled") {
            clearInterval(interval);
            setRunningLogRecon(false);
            const sums = await getPlanFractionLogs(planId);
            setSummaries(sums);
            const rep = await getPlanFractionLog(planId, selectedFx);
            setLogReport(rep);
          }
        } catch {
          clearInterval(interval);
          setRunningLogRecon(false);
        }
      }, 1000);
    } catch {
      setRunningLogRecon(false);
    }
  };

  if (loading && !logReport) {
    return (
      <div className="rounded-lg border border-clinical-border bg-clinical-surface p-8 text-center">
        <Activity size={24} className="animate-spin text-clinical-accent mx-auto mb-2" />
        <p className="text-xs text-clinical-muted">Loading fraction delivery log analysis...</p>
      </div>
    );
  }

  if (!logReport || logReport.beams.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-clinical-border p-6 text-center bg-clinical-surface">
        <Activity size={24} className="mx-auto text-clinical-muted mb-2 opacity-60" />
        <h3 className="text-xs font-semibold text-clinical-text">No Delivery Logs Recorded</h3>
        <p className="text-[11px] text-clinical-muted max-w-sm mx-auto mt-1">
          Fraction delivery logs (RT Ion Records) will populate this clinical layer-by-layer QA analysis as treatments are delivered.
        </p>
      </div>
    );
  }

  const selectedBeam: FractionBeamLog = logReport.beams[selectedBeamIndex] || logReport.beams[0];

  const isVerif = logReport.delivery_type === "verification" || logReport.fraction_number === 0 || Boolean(logReport.is_verification);

  return (
    <div className="space-y-4">
      {/* HEADER & WORKFLOW SUMMARY */}
      <div className="rounded-lg border border-clinical-border bg-clinical-surface p-4 shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-clinical-border/50 pb-3 mb-3">
          <div className="flex items-center gap-3">
            <div className={`p-2 rounded border ${isVerif ? "bg-purple-950/40 border-purple-800/60" : "bg-clinical-bg border-clinical-border/60"}`}>
              {isVerif ? <ShieldCheck size={18} className="text-purple-400" /> : <Activity size={18} className="text-clinical-accent" />}
            </div>
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <h2 className="text-xs font-bold text-clinical-text uppercase tracking-wider">
                  {isVerif ? "Pre-Treatment Verification Delivery Log QA" : `Fraction ${logReport.fraction_number} Clinical Delivery Log QA`}
                </h2>
                <span
                  className={`text-[10px] font-bold px-2 py-0.5 rounded ${
                    isVerif
                      ? "bg-purple-100 text-purple-800 dark:bg-purple-950/60 dark:text-purple-300 border border-purple-300 dark:border-purple-800"
                      : "bg-sky-100 text-sky-800 dark:bg-sky-950/60 dark:text-sky-300 border border-sky-300 dark:border-sky-800"
                  }`}
                >
                  {isVerif ? "VERIFICATION (DRY RUN - NO PATIENT)" : `CURATIVE (FRACTION ${logReport.fraction_number})`}
                </span>
                {logReport.fraction_gamma_passing_rate != null && (
                  <span
                    className={`text-[10px] font-bold px-2 py-0.5 rounded ${
                      logReport.fraction_gamma_passed
                        ? "bg-green-100 text-green-800 dark:bg-green-950/60 dark:text-green-300 border border-green-300 dark:border-green-800"
                        : "bg-red-100 text-red-800 dark:bg-red-950/60 dark:text-red-300 border border-red-300 dark:border-red-800"
                    }`}
                  >
                    Mean Gamma: {logReport.fraction_gamma_passing_rate.toFixed(1)}% ({logReport.fraction_gamma_passed ? "PASS" : "ACTION REQUIRED"})
                  </span>
                )}
                {logReport.is_interrupted && (
                  <span className="text-[10px] font-bold px-2 py-0.5 rounded bg-amber-100 text-amber-900 dark:bg-amber-950/70 dark:text-amber-300 border border-amber-400 dark:border-amber-700 flex items-center gap-1">
                    <AlertTriangle size={11} className="text-amber-500" />
                    PARTIAL / INTERRUPTED DELIVERY
                  </span>
                )}
              </div>
              <p className="text-[11px] text-clinical-muted mt-0.5">
                {isVerif
                  ? "Pre-treatment machine delivery verification run without patient on the table to check spot delivery logs and identify plan problems."
                  : "Clinical fraction delivery log verification (patient on table). Spot position accuracy, energy layer meterset, and couch delivery verification."}
              </p>
            </div>
          </div>

          {/* Fraction Selector & Re-run */}
          <div className="flex items-center gap-2">
            {summaries.length > 1 && (
              <div className="flex items-center gap-2">
                <span className="text-[11px] font-medium text-clinical-muted">Select Log:</span>
                <select
                  value={selectedFx}
                  onChange={(e) => setSelectedFx(Number(e.target.value))}
                  className="text-xs bg-clinical-bg border border-clinical-border rounded px-2.5 py-1 text-clinical-text focus:outline-none focus:border-clinical-accent"
                >
                  {summaries.map((s) => {
                    const isV = s.delivery_type === "verification" || s.fraction_number === 0 || Boolean(s.is_verification);
                    const flag = s.is_interrupted ? "⚠️ [PARTIAL] " : "";
                    const label = isV
                      ? `${flag}[VERIFICATION] Dry Run (No Patient) - ${s.treatment_date || "—"}`
                      : `${flag}Fraction ${s.fraction_number}: Curative (${s.treatment_date || "—"})`;
                    return (
                      <option key={s.fraction_number} value={s.fraction_number}>
                        {label}{s.fraction_gamma_passing_rate != null ? ` - ${s.fraction_gamma_passing_rate.toFixed(1)}%` : ""}
                      </option>
                    );
                  })}
                </select>
              </div>
            )}
            <label
              className="flex items-center gap-1.5 px-2.5 py-1 text-xs bg-clinical-bg hover:bg-clinical-border/40 border border-clinical-border rounded text-clinical-text font-medium transition-colors cursor-pointer"
              title={`Re-upload merged or corrected DICOM RT Record for Fraction ${selectedFx}`}
            >
              {reuploading ? (
                <Loader2 size={12} className="animate-spin text-clinical-accent" />
              ) : (
                <Upload size={12} className="text-clinical-accent" />
              )}
              <span>Re-upload Record</span>
              <input
                type="file"
                accept=".dcm,application/dicom"
                className="hidden"
                disabled={reuploading}
                onChange={handleFileReupload}
              />
            </label>
            <button
              onClick={handleRunLogRecon}
              disabled={runningLogRecon}
              className="flex items-center gap-1.5 px-2.5 py-1 text-xs bg-clinical-bg hover:bg-clinical-border/40 border border-clinical-border rounded text-clinical-text font-medium transition-colors disabled:opacity-50"
              title={`Re-run log dose reconstruction and gamma calculation for ${isVerif ? "Verification Run" : `Fraction ${selectedFx}`}`}
            >
              {runningLogRecon ? (
                <>
                  <Loader2 size={12} className="animate-spin text-amber-500" />
                  Running Log QA…
                </>
              ) : (
                <>
                  <Zap size={12} className="text-amber-500" />
                  Re-calculate Gamma
                </>
              )}
            </button>
          </div>
        </div>

        {/* Verification vs Curative Context Callout */}
        <div
          className={`mb-3 p-2.5 rounded border text-xs flex items-start gap-2.5 ${
            isVerif
              ? "bg-purple-950/20 border-purple-800/40 text-purple-300 dark:text-purple-200"
              : "bg-sky-950/20 border-sky-800/40 text-sky-300 dark:text-sky-200"
          }`}
        >
          {isVerif ? (
            <ShieldCheck size={16} className="text-purple-400 shrink-0 mt-0.5" />
          ) : (
            <Activity size={16} className="text-sky-400 shrink-0 mt-0.5" />
          )}
          <div>
            <span className="font-semibold text-clinical-text">
              {isVerif ? "Verification Delivery Record (Pre-Treatment QA):" : `Curative Treatment Record (Fraction ${logReport.fraction_number}):`}
            </span>{" "}
            {isVerif
              ? "This delivery was executed WITHOUT the patient on the table to verify beam steering, spot positions, and plan delivery integrity prior to patient treatment."
              : "This delivery was executed WITH the patient on the table during clinical treatment to monitor delivered spot positions and energy layer fluence."}
          </div>
        </div>

        {/* Interrupted Delivery Alert & Re-upload Action */}
        {logReport.is_interrupted && (
          <div className="mb-3 p-3 rounded-lg border border-amber-500/50 bg-amber-500/10 text-amber-900 dark:text-amber-200 text-xs flex flex-col sm:flex-row sm:items-center justify-between gap-3 shadow-sm">
            <div className="flex items-start gap-2.5">
              <AlertTriangle size={18} className="text-amber-500 shrink-0 mt-0.5" />
              <div>
                <div className="font-bold flex items-center gap-1.5 text-amber-800 dark:text-amber-300">
                  Partial / Interrupted Delivery Recorded
                  {logReport.interruption_type && (
                    <span className="text-[10px] px-1.5 py-0.2 rounded bg-amber-200/60 dark:bg-amber-900/60 font-mono">
                      {logReport.interruption_type}
                    </span>
                  )}
                </div>
                <p className="mt-0.5 text-amber-800/90 dark:text-amber-200/90">
                  {logReport.interruption_reason || "This fraction delivery was terminated early or missing planned beams. Calculated gamma passing rate reflects partial delivery against prescribed dose."}
                </p>
                <p className="mt-1 text-[11px] text-amber-700/80 dark:text-amber-300/70">
                  If a continuation beam was delivered or records were merged elsewhere, re-upload the complete merged record (.dcm) to overwrite this partial delivery and re-score gamma.
                </p>
              </div>
            </div>

            <div className="shrink-0 flex items-center">
              <label
                className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-semibold cursor-pointer transition-colors shadow-sm ${
                  reuploading
                    ? "bg-amber-400 text-amber-950 opacity-50 cursor-not-allowed"
                    : "bg-amber-600 hover:bg-amber-500 text-white dark:bg-amber-500 dark:hover:bg-amber-400 dark:text-black"
                }`}
              >
                {reuploading ? (
                  <>
                    <Loader2 size={13} className="animate-spin" />
                    Processing…
                  </>
                ) : (
                  <>
                    <Upload size={13} />
                    Re-upload Merged Record
                  </>
                )}
                <input
                  type="file"
                  accept=".dcm,application/dicom"
                  className="hidden"
                  disabled={reuploading}
                  onChange={handleFileReupload}
                />
              </label>
            </div>
          </div>
        )}

        {/* Demographics / Delivery Meta Grid */}
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-6 gap-3 text-xs bg-clinical-bg/40 p-3 rounded border border-clinical-border/40">
          <div>
            <span className="text-[10px] text-clinical-muted flex items-center gap-1 mb-0.5">
              <MapPin size={11} /> Treatment Room
            </span>
            <span className="font-semibold text-clinical-text">{logReport.treatment_room}</span>
          </div>
          <div>
            <span className="text-[10px] text-clinical-muted flex items-center gap-1 mb-0.5">
              <Calendar size={11} /> Treatment Date
            </span>
            <span className="font-semibold text-clinical-text">{logReport.treatment_date || "—"}</span>
          </div>
          <div>
            <span className="text-[10px] text-clinical-muted flex items-center gap-1 mb-0.5">
              <Clock size={11} /> Delivery Time
            </span>
            <span className="font-semibold text-clinical-text">{logReport.treatment_time || "—"}</span>
          </div>
          <div>
            <span className="text-[10px] text-clinical-muted flex items-center gap-1 mb-0.5">
              <User size={11} /> Operator / System
            </span>
            <span className="font-semibold text-clinical-text truncate block">{logReport.operator}</span>
          </div>
          <div>
            <span className="text-[10px] text-clinical-muted flex items-center gap-1 mb-0.5">
              <Layers size={11} /> Fields Delivered
            </span>
            <span className="font-semibold text-clinical-text">{logReport.n_fields} Beams</span>
          </div>
          <div>
            <span className="text-[10px] text-clinical-muted flex items-center gap-1 mb-0.5">
              <Sliders size={11} /> Total Meterset
            </span>
            <span className="font-semibold text-clinical-text">
              {logReport.total_delivered_mu.toFixed(1)} MU{" "}
              <span className="text-[10px] text-clinical-muted font-normal">
                ({logReport.total_mu_deviation_pct >= 0 ? "+" : ""}
                {logReport.total_mu_deviation_pct.toFixed(2)}%)
              </span>
            </span>
          </div>
        </div>
      </div>

      {/* BEAM SELECTOR TABS */}
      <div className="flex flex-wrap gap-2">
        {logReport.beams.map((b, idx) => {
          const isSel = idx === selectedBeamIndex;
          const beamInterrupted = b.is_interrupted || (b.beam_termination_status && b.beam_termination_status !== "NORMAL");
          return (
            <button
              key={b.beam_name}
              onClick={() => setSelectedBeamIndex(idx)}
              className={`flex-1 min-w-[180px] p-3 text-left rounded-lg border transition-all ${
                isSel
                  ? "bg-clinical-surface border-clinical-accent shadow-sm ring-1 ring-clinical-accent/30"
                  : "bg-clinical-surface/70 border-clinical-border hover:bg-clinical-surface hover:border-clinical-border/80"
              }`}
            >
              <div className="flex items-center justify-between mb-1">
                <div className="flex items-center gap-1.5">
                  <span className="text-xs font-bold text-clinical-text">
                    Beam {b.beam_number}: {b.beam_name}
                  </span>
                  {beamInterrupted && (
                    <span
                      className="text-[9px] px-1 py-0.2 rounded bg-amber-500/20 text-amber-700 dark:text-amber-300 border border-amber-500/40 font-semibold"
                      title={`Beam status: ${b.beam_termination_status || "INTERRUPTED"}`}
                    >
                      PARTIAL
                    </span>
                  )}
                </div>
                {b.gamma_passing_rate != null && (
                  <span
                    className={`text-[10px] font-bold px-1.5 py-0.2 rounded ${
                      b.gamma_passed
                        ? "text-green-700 dark:text-green-300 bg-green-500/10"
                        : "text-red-700 dark:text-red-300 bg-red-500/10"
                    }`}
                  >
                    {b.gamma_passing_rate.toFixed(1)}% &gamma;
                  </span>
                )}
              </div>
              <div className="flex items-center justify-between text-[11px] text-clinical-muted">
                <span>Gantry {b.actual_gantry_angle.toFixed(1)}&deg;</span>
                <span>{b.delivered_primary_mu.toFixed(1)} MU</span>
                <span>{b.n_layers} Layers</span>
              </div>
            </button>
          );
        })}
      </div>

      {/* FIELD DETAIL CARD: GANTRY, 6-DOF COUCH, MU & POSITION PASS RATES */}
      <div className="rounded-lg border border-clinical-border bg-clinical-surface p-4 shadow-sm space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-clinical-border/40 pb-2.5">
          <div className="flex items-center gap-2">
            <h3 className="text-xs font-bold text-clinical-text uppercase tracking-wide">
              Field Execution Summary: {selectedBeam.beam_name} ({selectedBeam.record_beam_name})
            </h3>
          </div>
          <div className="text-[11px] text-clinical-muted flex items-center gap-3">
            <span>
              Planned Gantry: <b>{selectedBeam.planned_gantry_angle.toFixed(1)}&deg;</b> &rarr; Actual: <b>{selectedBeam.actual_gantry_angle.toFixed(2)}&deg;</b>
            </span>
            <span>&bull;</span>
            <span>
              Spots: <b>{selectedBeam.n_spots.toLocaleString()}</b>
            </span>
          </div>
        </div>

        {/* 3-Column Delivery Metrics: Meterset, Couch, Position Pass Rates */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {/* 1. Meterset Delivery */}
          <div className="rounded border border-clinical-border/60 bg-clinical-bg/30 p-3 text-xs space-y-2">
            <h4 className="font-semibold text-clinical-text text-[11px] uppercase tracking-wider text-clinical-muted border-b border-clinical-border/30 pb-1">
              Meterset Delivery Accuracy
            </h4>
            <div className="flex justify-between items-center">
              <span className="text-clinical-muted">Beam Status:</span>
              <span
                className={`font-semibold px-1.5 py-0.2 text-[10px] rounded ${
                  !selectedBeam.beam_termination_status || selectedBeam.beam_termination_status === "NORMAL"
                    ? "text-green-700 dark:text-green-300 bg-green-500/10"
                    : "text-amber-700 dark:text-amber-300 bg-amber-500/20 border border-amber-500/40"
                }`}
              >
                {selectedBeam.beam_termination_status || "NORMAL"}
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-clinical-muted">Prescribed Dose:</span>
              <span className="font-semibold text-clinical-text">{selectedBeam.prescribed_mu.toFixed(2)} MU</span>
            </div>
            <div className="flex justify-between">
              <span className="text-clinical-muted">Delivered Primary:</span>
              <span className="font-semibold text-clinical-text">
                {selectedBeam.delivered_primary_mu.toFixed(2)} MU{" "}
                <span className="text-[10px] text-green-600 dark:text-green-400">
                  ({selectedBeam.deviation_primary_pct >= 0 ? "+" : ""}
                  {selectedBeam.deviation_primary_pct.toFixed(2)}%)
                </span>
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-clinical-muted">Delivered Secondary:</span>
              <span className="font-semibold text-clinical-text">
                {selectedBeam.delivered_secondary_mu.toFixed(2)} MU{" "}
                <span className="text-[10px] text-clinical-muted">
                  ({selectedBeam.deviation_secondary_pct >= 0 ? "+" : ""}
                  {selectedBeam.deviation_secondary_pct.toFixed(2)}%)
                </span>
              </span>
            </div>
          </div>

          {/* 2. Couch 6-DoF Positioning */}
          <div className="rounded border border-clinical-border/60 bg-clinical-bg/30 p-3 text-xs space-y-2">
            <h4 className="font-semibold text-clinical-text text-[11px] uppercase tracking-wider text-clinical-muted border-b border-clinical-border/30 pb-1">
              Couch 6-DoF Position &amp; Angles
            </h4>
            <div className="flex justify-between">
              <span className="text-clinical-muted">Lat / Long / Vert:</span>
              <span className="font-semibold text-clinical-text">
                {selectedBeam.table_position.lateral_mm.toFixed(1)} / {selectedBeam.table_position.longitudinal_mm.toFixed(1)} / {selectedBeam.table_position.vertical_mm.toFixed(1)} mm
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-clinical-muted">Pitch / Roll:</span>
              <span className="font-semibold text-clinical-text">
                {selectedBeam.table_position.pitch_deg.toFixed(2)}&deg; / {selectedBeam.table_position.roll_deg.toFixed(2)}&deg;
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-clinical-muted">Support Angle:</span>
              <span className="font-semibold text-clinical-text">{selectedBeam.table_position.support_angle_deg.toFixed(1)}&deg;</span>
            </div>
          </div>

          {/* 3. Spot Position Pass Rates */}
          <div className="rounded border border-clinical-border/60 bg-clinical-bg/30 p-3 text-xs space-y-2">
            <h4 className="font-semibold text-clinical-text text-[11px] uppercase tracking-wider text-clinical-muted border-b border-clinical-border/30 pb-1">
              Spot Position Pass Rates (Isocenter)
            </h4>
            <div className="flex justify-between">
              <span className="text-clinical-muted">X-Axis (&le;0.5 / &le;2.0 mm):</span>
              <span className="font-semibold text-clinical-text">
                {selectedBeam.position_pass_rates.x_05mm.toFixed(1)}% / {selectedBeam.position_pass_rates.x_20mm.toFixed(1)}%
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-clinical-muted">Y-Axis (&le;0.5 / &le;2.0 mm):</span>
              <span className="font-semibold text-clinical-text">
                {selectedBeam.position_pass_rates.y_05mm.toFixed(1)}% / {selectedBeam.position_pass_rates.y_20mm.toFixed(1)}%
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-clinical-muted">Magnitude (&le;0.5 / &le;2.0 mm):</span>
              <span className="font-semibold text-clinical-text">
                {selectedBeam.position_pass_rates.mag_05mm.toFixed(1)}% / {selectedBeam.position_pass_rates.mag_20mm.toFixed(1)}%
              </span>
            </div>
          </div>
        </div>

        {/* LAYER-BY-LAYER SPOT TABLE */}
        <div className="space-y-2 pt-2">
          <div className="flex items-center justify-between">
            <h4 className="text-xs font-bold text-clinical-text uppercase tracking-wider flex items-center gap-1.5">
              <Layers size={13} className="text-clinical-accent" />
              Layer-by-Layer Energy &amp; Spot Breakdown ({selectedBeam.layers.length} Energy Layers)
            </h4>
            <span className="text-[10px] text-clinical-muted">
              Projected to isocenter plane &middot; ProNova SC360 PBS
            </span>
          </div>

          <div className="overflow-x-auto rounded border border-clinical-border/70">
            <table className="w-full text-xs text-left border-collapse bg-clinical-bg/20">
              <thead>
                <tr className="border-b border-clinical-border bg-clinical-bg/60 text-clinical-muted uppercase text-[10px]">
                  <th className="py-2 px-2.5 text-center">Layer</th>
                  <th className="py-2 px-2.5 text-right">Energy (MeV)</th>
                  <th className="py-2 px-2.5 text-right">Spots</th>
                  <th className="py-2 px-2.5 text-right">Rx MU (Cum.)</th>
                  <th className="py-2 px-2.5 text-right">Del MU (Cum.)</th>
                  <th className="py-2 px-2.5 text-center">X Pass (0.5 / 2.0 mm)</th>
                  <th className="py-2 px-2.5 text-center">Y Pass (0.5 / 2.0 mm)</th>
                  <th className="py-2 px-2.5 text-center">Mag Pass (0.5 / 2.0 mm)</th>
                  <th className="py-2 px-2.5 text-right">Max Dev (X, Y, Mag)</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-clinical-border/40 font-mono text-[11px]">
                {selectedBeam.layers.map((l) => {
                  const isMagWarn = l.mag_pass_05mm < 80.0;
                  return (
                    <tr
                      key={l.layer_index}
                      className={`hover:bg-clinical-border/20 transition-colors ${
                        isMagWarn ? "bg-amber-50/20 dark:bg-amber-950/20" : ""
                      }`}
                    >
                      <td className="py-1.5 px-2.5 text-center font-sans font-semibold text-clinical-text">
                        {l.layer_index}
                      </td>
                      <td className="py-1.5 px-2.5 text-right font-semibold text-clinical-text">
                        {l.energy_mev.toFixed(1)}
                      </td>
                      <td className="py-1.5 px-2.5 text-right text-clinical-muted">{l.spot_count}</td>
                      <td className="py-1.5 px-2.5 text-right text-clinical-muted">{l.cumulative_rx_mu.toFixed(2)}</td>
                      <td className="py-1.5 px-2.5 text-right font-medium text-clinical-text">
                        {l.cumulative_del_mu.toFixed(2)}
                      </td>
                      <td className="py-1.5 px-2.5 text-center">
                        <span className={l.x_pass_05mm < 95.0 ? "text-amber-600 dark:text-amber-400 font-semibold" : "text-clinical-text"}>
                          {l.x_pass_05mm.toFixed(1)}%
                        </span>{" "}
                        <span className="text-clinical-muted text-[10px]">/ {l.x_pass_20mm.toFixed(1)}%</span>
                      </td>
                      <td className="py-1.5 px-2.5 text-center">
                        <span className={l.y_pass_05mm < 95.0 ? "text-amber-600 dark:text-amber-400 font-semibold" : "text-clinical-text"}>
                          {l.y_pass_05mm.toFixed(1)}%
                        </span>{" "}
                        <span className="text-clinical-muted text-[10px]">/ {l.y_pass_20mm.toFixed(1)}%</span>
                      </td>
                      <td className="py-1.5 px-2.5 text-center">
                        <span className={l.mag_pass_05mm < 80.0 ? "text-amber-600 dark:text-amber-400 font-bold" : l.mag_pass_05mm < 95.0 ? "text-amber-600 dark:text-amber-400 font-medium" : "text-clinical-text font-medium"}>
                          {l.mag_pass_05mm.toFixed(1)}%
                        </span>{" "}
                        <span className="text-clinical-muted text-[10px]">/ {l.mag_pass_20mm.toFixed(1)}%</span>
                      </td>
                      <td className="py-1.5 px-2.5 text-right text-clinical-muted text-[10px]">
                        {l.max_abs_dx_mm.toFixed(1)}, {l.max_abs_dy_mm.toFixed(1)}, {l.max_mag_mm.toFixed(1)} mm
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
};
