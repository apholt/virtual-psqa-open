import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import toast from "react-hot-toast";
import {
  AlertTriangle,
  ChevronLeft,
  ChevronRight,
  ClipboardCheck,
  Columns,
  Eye,
  EyeOff,
  Grid,
  Layers,
  RefreshCw,
  Sliders,
  UserCheck,
} from "lucide-react";
import {
  getOirInfo,
  getOirSlice,
  saveOirChartCheck,
} from "../api/client";
import type {
  OirChartCheck,
  OirFractionInfo,
  OirPlanInfo,
  OirRegistration,
  OirSliceData,
  PlanSummary,
} from "../types";

interface OIRViewerProps {
  planId: number;
  plan?: PlanSummary | null;
}

const WL_PRESETS = [
  { label: "Soft Tissue", width: 400, center: 40 },
  { label: "Bone", width: 1800, center: 400 },
  { label: "Brain", width: 80, center: 40 },
  { label: "Lung", width: 1500, center: -600 },
  { label: "Full Range", width: 3000, center: 500 },
];

export const OIRViewer: React.FC<OIRViewerProps> = ({ planId }) => {
  // Plan OIR metadata
  const [oirInfo, setOirInfo] = useState<OirPlanInfo | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [sliceLoading, setSliceLoading] = useState<boolean>(false);

  // Selected fraction, CBCT series, and registration
  const [selectedFraction, setSelectedFraction] = useState<number>(1);
  const [selectedSeriesUid, setSelectedSeriesUid] = useState<string>("");
  const [selectedRegId, setSelectedRegId] = useState<string>("");

  // Slice navigation
  const [sliceIdx, setSliceIdx] = useState<number>(0);
  const [sliceData, setSliceData] = useState<OirSliceData | null>(null);

  // Window/Level controls (independent or linked)
  const [windowWidth, setWindowWidth] = useState<number>(400);
  const [windowCenter, setWindowCenter] = useState<number>(40);

  // Fusion modes: "alpha" | "split" | "checkerboard"
  const [fusionMode, setFusionMode] = useState<"alpha" | "split" | "checkerboard">("alpha");
  const [alpha, setAlpha] = useState<number>(0.5); // 0 = 100% TPCT, 1 = 100% CBCT
  const [splitPos, setSplitPos] = useState<number>(0.5); // 0..1 split horizontal
  const [checkerSize, setCheckerSize] = useState<number>(32); // 16, 32, 64 px
  const [colorWash, setColorWash] = useState<boolean>(false);

  // RTSTRUCT Contour overlays
  const [showContours, setShowContours] = useState<boolean>(true);
  const [showContourFill, setShowContourFill] = useState<boolean>(false);
  const [selectedRoiNumbers, setSelectedRoiNumbers] = useState<Set<number>>(new Set());
  const [showRoiDrawer, setShowRoiDrawer] = useState<boolean>(false);

  // Hover metadata
  const [hoverPixel, setHoverPixel] = useState<{ x: number; y: number; huTpct: number; huCbct: number } | null>(null);

  // OIR Sign-off panel (Physicist or Physician)
  const [chartChecks, setChartChecks] = useState<OirChartCheck[]>([]);
  const [reviewerRole, setReviewerRole] = useState<"physicist" | "physician">("physicist");
  const [reviewerName, setReviewerName] = useState<string>(
    () => localStorage.getItem("psqa_oir_physicist") || localStorage.getItem("psqa_oir_reviewer") || ""
  );
  const [checkStatus, setCheckStatus] = useState<"pass" | "acceptable" | "flagged">("pass");
  const [checkNotes, setCheckNotes] = useState<string>("");
  const [shiftsVerified, setShiftsVerified] = useState<boolean>(true);
  const [contoursVerified, setContoursVerified] = useState<boolean>(true);
  const [savingCheck, setSavingCheck] = useState<boolean>(false);

  // Canvas ref
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const reqSeq = useRef<number>(0);

  // Decoded pixel buffers
  const decodedData = useRef<{
    tpctArray: Int16Array | null;
    cbctArray: Int16Array | null;
    dims: [number, number];
  }>({
    tpctArray: null,
    cbctArray: null,
    dims: [512, 512],
  });

  // 1. Fetch OIR Plan Info
  const loadInfo = useCallback(async () => {
    try {
      setLoading(true);
      const data = await getOirInfo(planId);
      setOirInfo(data);
      setChartChecks(data.chart_checks || []);

      // Default slice
      setSliceIdx(data.default_slice || Math.floor(data.num_slices / 2));

      // Default to all ROIs selected
      if (data.rois && data.rois.length > 0) {
        setSelectedRoiNumbers(new Set(data.rois.map((r) => r.roi_number)));
      }

      // Default fraction
      if (data.fractions && data.fractions.length > 0) {
        const firstFx = data.fractions[0];
        setSelectedFraction(firstFx.fraction_number);
        if (firstFx.cbct_series && firstFx.cbct_series.length > 0) {
          setSelectedSeriesUid(firstFx.cbct_series[0].series_instance_uid);
        }
        if (firstFx.registrations && firstFx.registrations.length > 0) {
          setSelectedRegId(firstFx.registrations[0].registration_id);
        }
      }
    } catch (err: any) {
      toast.error(`Failed to load OIR information: ${err?.message || "Plan not found"}`);
    } finally {
      setLoading(false);
    }
  }, [planId]);

  useEffect(() => {
    loadInfo();
  }, [loadInfo]);

  // Current fraction info
  const currentFractionInfo: OirFractionInfo | undefined = useMemo(() => {
    return oirInfo?.fractions.find((f) => f.fraction_number === selectedFraction);
  }, [oirInfo, selectedFraction]);

  // Current registration info
  const currentRegInfo: OirRegistration | undefined = useMemo(() => {
    return currentFractionInfo?.registrations.find((r) => r.registration_id === selectedRegId);
  }, [currentFractionInfo, selectedRegId]);

  // Handle changing fraction
  const handleFractionChange = (fxNum: number) => {
    setSelectedFraction(fxNum);
    const fxInfo = oirInfo?.fractions.find((f) => f.fraction_number === fxNum);
    if (fxInfo) {
      if (fxInfo.cbct_series && fxInfo.cbct_series.length > 0) {
        setSelectedSeriesUid(fxInfo.cbct_series[0].series_instance_uid);
      }
      if (fxInfo.registrations && fxInfo.registrations.length > 0) {
        setSelectedRegId(fxInfo.registrations[0].registration_id);
      }
    }
  };

  // 2. Fetch Slice Data
  const loadSlice = useCallback(async () => {
    if (!oirInfo || oirInfo.fractions.length === 0) return;
    const currentSeq = ++reqSeq.current;
    try {
      setSliceLoading(true);
      const data = await getOirSlice(
        planId,
        sliceIdx,
        selectedFraction,
        selectedRegId,
        selectedSeriesUid || undefined
      );

      if (currentSeq !== reqSeq.current) return;

      // Decode base64 Int16
      const decodeB64 = (b64: string): Int16Array => {
        const bin = atob(b64);
        const bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
        return new Int16Array(bytes.buffer);
      };

      decodedData.current = {
        tpctArray: decodeB64(data.tpct_b64),
        cbctArray: decodeB64(data.cbct_b64),
        dims: data.dimensions,
      };

      setSliceData(data);
    } catch (err: any) {
      if (currentSeq === reqSeq.current) {
        toast.error(`Slice error: ${err?.message || "Failed to load slice"}`);
      }
    } finally {
      if (currentSeq === reqSeq.current) {
        setSliceLoading(false);
      }
    }
  }, [planId, sliceIdx, selectedFraction, selectedRegId, selectedSeriesUid, oirInfo]);

  useEffect(() => {
    loadSlice();
  }, [loadSlice]);

  // 3. Render Canvas with dynamic W/L and Fusion
  const renderCanvas = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const { tpctArray, cbctArray, dims } = decodedData.current;
    if (!tpctArray || !cbctArray) return;

    const [rows, cols] = dims;
    canvas.width = cols;
    canvas.height = rows;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const imgData = ctx.createImageData(cols, rows);
    const data = imgData.data;

    const minHU = windowCenter - windowWidth / 2;
    const scale = 255.0 / Math.max(1, windowWidth);

    const splitCol = Math.round(cols * splitPos);

    for (let r = 0; r < rows; r++) {
      const rowOffset = r * cols;
      for (let c = 0; c < cols; c++) {
        const i = rowOffset + c;
        const pIdx = i * 4;

        const huTpct = tpctArray[i];
        const huCbct = cbctArray[i];

        // Normalized HU [0..255]
        const vTpct = Math.min(255, Math.max(0, (huTpct - minHU) * scale));
        const vCbct = Math.min(255, Math.max(0, (huCbct - minHU) * scale));

        let red = 0;
        let green = 0;
        let blue = 0;

        if (fusionMode === "alpha") {
          if (colorWash) {
            // Color Wash: TPCT in Cyan/Green, CBCT in Red/Orange
            // Matching areas blend to warm yellow/neutral; misalignments show distinct red or cyan fringes!
            const wTpct = 1.0 - alpha;
            const wCbct = alpha;
            red = Math.min(255, wCbct * vCbct * 1.2);
            green = Math.min(255, wTpct * vTpct + wCbct * vCbct * 0.4);
            blue = Math.min(255, wTpct * vTpct * 0.9);
          } else {
            // Standard Greyscale Alpha Blend
            const val = (1.0 - alpha) * vTpct + alpha * vCbct;
            red = val;
            green = val;
            blue = val;
          }
        } else if (fusionMode === "split") {
          // Curtain / Split View
          if (c < splitCol) {
            red = vTpct;
            green = vTpct;
            blue = vTpct;
          } else if (c === splitCol || c === splitCol + 1) {
            // Bright divider line
            red = 59;
            green = 130;
            blue = 246; // Tailwind blue-500
          } else {
            red = vCbct;
            green = vCbct;
            blue = vCbct;
          }
        } else if (fusionMode === "checkerboard") {
          // Checkerboard tile pattern
          const tile = (Math.floor(r / checkerSize) + Math.floor(c / checkerSize)) % 2;
          const val = tile === 0 ? vTpct : vCbct;
          red = val;
          green = val;
          blue = val;
        }

        data[pIdx] = red;
        data[pIdx + 1] = green;
        data[pIdx + 2] = blue;
        data[pIdx + 3] = 255;
      }
    }

    ctx.putImageData(imgData, 0, 0);
  }, [windowCenter, windowWidth, fusionMode, alpha, splitPos, checkerSize, colorWash]);

  useEffect(() => {
    renderCanvas();
  }, [renderCanvas, sliceData]);

  // Mousewheel slice scrolling over canvas
  const handleWheel = (e: React.WheelEvent) => {
    e.preventDefault();
    if (!oirInfo) return;
    const delta = e.deltaY > 0 ? 1 : -1;
    setSliceIdx((prev) => Math.max(0, Math.min(oirInfo.num_slices - 1, prev + delta)));
  };

  // Canvas mousemove for HU probe
  const handleMouseMove = (e: React.MouseEvent<HTMLDivElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const x = Math.floor(((e.clientX - rect.left) / rect.width) * 512);
    const y = Math.floor(((e.clientY - rect.top) / rect.height) * 512);

    if (x >= 0 && x < 512 && y >= 0 && y < 512) {
      const idx = y * 512 + x;
      const { tpctArray, cbctArray } = decodedData.current;
      if (tpctArray && cbctArray) {
        setHoverPixel({
          x,
          y,
          huTpct: tpctArray[idx],
          huCbct: cbctArray[idx],
        });
      }
    }
  };

  // Switch reviewer role between Medical Physicist and Radiation Oncologist (Physician)
  const handleRoleChange = (role: "physicist" | "physician") => {
    setReviewerRole(role);
    if (role === "physician") {
      setReviewerName(localStorage.getItem("psqa_oir_physician") || "");
    } else {
      setReviewerName(localStorage.getItem("psqa_oir_physicist") || localStorage.getItem("psqa_oir_reviewer") || "");
    }
  };

  // Save OIR sign-off (Physicist or Physician)
  const handleSaveChartCheck = async () => {
    if (!reviewerName.trim()) {
      toast.error(`Please enter ${reviewerRole === "physician" ? "physician" : "physicist"} reviewer name or initials.`);
      return;
    }
    try {
      setSavingCheck(true);
      if (reviewerRole === "physician") {
        localStorage.setItem("psqa_oir_physician", reviewerName.trim());
      } else {
        localStorage.setItem("psqa_oir_physicist", reviewerName.trim());
        localStorage.setItem("psqa_oir_reviewer", reviewerName.trim());
      }
      const res = await saveOirChartCheck(planId, {
        fraction_number: selectedFraction,
        reviewer_name: reviewerName,
        status: checkStatus,
        notes: checkNotes,
        shifts_verified: shiftsVerified,
        contours_verified: contoursVerified,
        reviewer_role: reviewerRole,
      });
      setChartChecks((prev) => {
        const filtered = prev.filter((r) => r.fraction_number !== selectedFraction);
        return [res, ...filtered];
      });
      toast.success(
        `Fraction ${selectedFraction} ${reviewerRole === "physician" ? "Physician" : "Physicist"} sign-off recorded!`
      );
    } catch (err: any) {
      toast.error(`Failed to save sign-off: ${err?.message || "Server error"}`);
    } finally {
      setSavingCheck(false);
    }
  };

  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center p-16 space-y-4 text-clinical-muted">
        <RefreshCw className="animate-spin text-clinical-accent" size={32} />
        <span className="text-sm font-medium">Loading Offline Image Review (OIR) Dataset…</span>
      </div>
    );
  }

  if (!oirInfo || oirInfo.fractions.length === 0) {
    return (
      <div className="rounded-xl border border-clinical-border bg-clinical-surface p-8 text-center space-y-3">
        <AlertTriangle size={36} className="mx-auto text-amber-500" />
        <h3 className="text-sm font-bold text-clinical-text">No Daily CBCT Datasets Available</h3>
        <p className="text-xs text-clinical-muted max-w-md mx-auto">
          Offline Image Review requires at least one daily CBCT series for this plan. Ingest CBCT
          DICOM slices or run SyntheticQACT to enable offline comparison.
        </p>
      </div>
    );
  }

  // Filter contours on current slice by enabled ROIs
  const visibleContours = (sliceData?.contours || []).filter((c) =>
    selectedRoiNumbers.has(c.roi_number)
  );

  return (
    <div className="space-y-4">
      {/* Top Header & Fraction Selection Bar */}
      <div className="rounded-xl border border-clinical-border bg-clinical-surface p-4 flex flex-wrap items-center justify-between gap-4 shadow-sm">
        <div className="flex items-center gap-3">
          <div className="p-2 rounded-lg bg-clinical-accent/10 text-clinical-accent">
            <Columns size={20} />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h2 className="text-sm font-bold text-clinical-text">
                Offline Image Review (OIR)
              </h2>
              <span className="text-[10px] uppercase font-bold px-2 py-0.5 rounded bg-blue-100 dark:bg-blue-950 text-blue-700 dark:text-blue-300">
                Planning CT vs Daily CBCT
              </span>
            </div>
            <p className="text-xs text-clinical-muted mt-0.5">
              Offline Image Review &amp; Setup Alignment Verification &bull; {oirInfo.fractions.length} Fraction{oirInfo.fractions.length > 1 ? "s" : ""} Available
            </p>
          </div>
        </div>

        {/* Fraction & Series Selectors */}
        <div className="flex items-center gap-2 flex-wrap text-xs">
          {/* Fraction Selector */}
          <div className="flex items-center gap-1.5 bg-clinical-bg p-1.5 rounded-lg border border-clinical-border">
            <span className="text-[11px] font-semibold text-clinical-muted px-1">Fraction:</span>
            <select
              value={selectedFraction}
              onChange={(e) => handleFractionChange(Number(e.target.value))}
              className="bg-clinical-surface border border-clinical-border rounded px-2 py-1 font-semibold text-clinical-text text-xs focus:outline-none focus:ring-1 focus:ring-clinical-accent"
            >
              {oirInfo.fractions.map((f) => (
                <option key={f.fraction_number} value={f.fraction_number}>
                  Fraction {f.fraction_number}
                </option>
              ))}
            </select>
          </div>

          {/* Multiple CBCT Series Selector (if fraction has >1 acquisitions) */}
          {currentFractionInfo && currentFractionInfo.cbct_series.length > 1 && (
            <div className="flex items-center gap-1.5 bg-clinical-bg p-1.5 rounded-lg border border-clinical-border">
              <span className="text-[11px] font-semibold text-clinical-muted px-1">Acquisition:</span>
              <select
                value={selectedSeriesUid}
                onChange={(e) => setSelectedSeriesUid(e.target.value)}
                className="bg-clinical-surface border border-clinical-border rounded px-2 py-1 text-xs text-clinical-text focus:outline-none focus:ring-1 focus:ring-clinical-accent"
              >
                {currentFractionInfo.cbct_series.map((s, idx) => (
                  <option key={s.series_instance_uid} value={s.series_instance_uid}>
                    Series {idx + 1} ({s.series_description || "CBCT"}, {s.num_slices} sl)
                  </option>
                ))}
              </select>
            </div>
          )}

          {/* Registration Mode Selector */}
          <div className="flex items-center gap-1 bg-clinical-bg p-1 rounded-lg border border-clinical-border">
            {currentFractionInfo?.registrations.map((reg) => (
              <button
                key={reg.registration_id}
                onClick={() => setSelectedRegId(reg.registration_id)}
                className={`px-2.5 py-1 rounded text-xs font-semibold transition-all ${
                  selectedRegId === reg.registration_id
                    ? "bg-clinical-accent text-white shadow-xs"
                    : "text-clinical-muted hover:text-clinical-text"
                }`}
                title={reg.description}
              >
                {reg.type === "treated" ? "Treated Match (REG)" : "Initial Setup (Unregistered)"}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Registration 6-DoF Shift Badges Banner */}
      {currentRegInfo && (
        <div className="rounded-xl border border-clinical-border/60 bg-clinical-surface px-4 py-2.5 flex flex-wrap items-center justify-between gap-3 text-xs">
          <div className="flex items-center gap-2">
            <span className="font-semibold text-clinical-text">
              Active Registration:
            </span>
            <span className="font-mono text-clinical-accent font-bold">
              {currentRegInfo.label}
            </span>
            {currentRegInfo.creation_datetime && (
              <span className="text-clinical-muted text-[11px]">
                ({currentRegInfo.creation_datetime})
              </span>
            )}
          </div>

          {/* 6-DoF Shifts Grid */}
          <div className="flex items-center gap-2 flex-wrap">
            <div className="px-2 py-1 rounded bg-clinical-bg border border-clinical-border flex items-center gap-1.5 font-mono text-[11px]">
              <span className="text-clinical-muted font-sans font-medium">&Delta;Lat (X):</span>
              <span className={Math.abs(currentRegInfo.shifts.lat_x_mm) > 3.0 ? "text-amber-500 font-bold" : "text-green-600 dark:text-green-400 font-bold"}>
                {currentRegInfo.shifts.lat_x_mm > 0 ? `+${currentRegInfo.shifts.lat_x_mm}` : currentRegInfo.shifts.lat_x_mm} mm
              </span>
            </div>

            <div className="px-2 py-1 rounded bg-clinical-bg border border-clinical-border flex items-center gap-1.5 font-mono text-[11px]">
              <span className="text-clinical-muted font-sans font-medium">&Delta;Long (Y):</span>
              <span className={Math.abs(currentRegInfo.shifts.long_y_mm) > 3.0 ? "text-amber-500 font-bold" : "text-green-600 dark:text-green-400 font-bold"}>
                {currentRegInfo.shifts.long_y_mm > 0 ? `+${currentRegInfo.shifts.long_y_mm}` : currentRegInfo.shifts.long_y_mm} mm
              </span>
            </div>

            <div className="px-2 py-1 rounded bg-clinical-bg border border-clinical-border flex items-center gap-1.5 font-mono text-[11px]">
              <span className="text-clinical-muted font-sans font-medium">&Delta;Vert (Z):</span>
              <span className="font-bold text-clinical-text">
                {currentRegInfo.shifts.vert_z_mm > 0 ? `+${currentRegInfo.shifts.vert_z_mm}` : currentRegInfo.shifts.vert_z_mm} mm
              </span>
            </div>

            <div className="px-2 py-1 rounded bg-clinical-bg border border-clinical-border flex items-center gap-1.5 font-mono text-[11px]">
              <span className="text-clinical-muted font-sans font-medium">Rotations:</span>
              <span className="text-clinical-muted">
                P: {currentRegInfo.shifts.pitch_deg}&deg; &bull; Y: {currentRegInfo.shifts.yaw_deg}&deg; &bull; R: {currentRegInfo.shifts.roll_deg}&deg;
              </span>
            </div>
          </div>
        </div>
      )}

      {/* Main Two-Column Layout */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-5">
        {/* Left Column (8/12): Interactive Axial Fusion Canvas */}
        <div className="lg:col-span-8 space-y-3">
          {/* Viewer Controls Toolbar */}
          <div className="rounded-xl border border-clinical-border bg-clinical-surface p-3 flex flex-wrap items-center justify-between gap-3 text-xs">
            {/* Fusion Mode Buttons */}
            <div className="flex items-center gap-1 bg-clinical-bg p-1 rounded-lg border border-clinical-border">
              <button
                onClick={() => setFusionMode("alpha")}
                className={`px-2 py-1 rounded text-xs font-semibold flex items-center gap-1 ${
                  fusionMode === "alpha"
                    ? "bg-clinical-accent text-white shadow-xs"
                    : "text-clinical-muted hover:text-clinical-text"
                }`}
              >
                <Layers size={13} /> Blend
              </button>
              <button
                onClick={() => setFusionMode("split")}
                className={`px-2 py-1 rounded text-xs font-semibold flex items-center gap-1 ${
                  fusionMode === "split"
                    ? "bg-clinical-accent text-white shadow-xs"
                    : "text-clinical-muted hover:text-clinical-text"
                }`}
              >
                <Columns size={13} /> Split
              </button>
              <button
                onClick={() => setFusionMode("checkerboard")}
                className={`px-2 py-1 rounded text-xs font-semibold flex items-center gap-1 ${
                  fusionMode === "checkerboard"
                    ? "bg-clinical-accent text-white shadow-xs"
                    : "text-clinical-muted hover:text-clinical-text"
                }`}
              >
                <Grid size={13} /> Checker
              </button>
            </div>

            {/* Fusion Specific Controls */}
            {fusionMode === "alpha" && (
              <div className="flex items-center gap-2 flex-1 max-w-xs">
                <span className="text-[11px] font-semibold text-clinical-muted shrink-0">
                  TPCT {Math.round((1 - alpha) * 100)}%
                </span>
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.02}
                  value={alpha}
                  onChange={(e) => setAlpha(parseFloat(e.target.value))}
                  className="w-full h-1.5 bg-clinical-border rounded-lg appearance-none cursor-pointer accent-clinical-accent"
                />
                <span className="text-[11px] font-semibold text-clinical-muted shrink-0">
                  CBCT {Math.round(alpha * 100)}%
                </span>
                <button
                  onClick={() => setColorWash(!colorWash)}
                  className={`px-2 py-1 rounded text-[11px] font-semibold border transition-all ${
                    colorWash
                      ? "bg-purple-100 dark:bg-purple-950 border-purple-400 text-purple-700 dark:text-purple-300 font-bold"
                      : "border-clinical-border text-clinical-muted hover:text-clinical-text"
                  }`}
                  title="Toggle Color Wash (Cyan/Red) to spot registration edge mismatches"
                >
                  Color Wash
                </button>
              </div>
            )}

            {fusionMode === "split" && (
              <div className="flex items-center gap-2 flex-1 max-w-xs">
                <span className="text-[11px] font-semibold text-clinical-muted shrink-0">Left: TPCT</span>
                <input
                  type="range"
                  min={0.05}
                  max={0.95}
                  step={0.01}
                  value={splitPos}
                  onChange={(e) => setSplitPos(parseFloat(e.target.value))}
                  className="w-full h-1.5 bg-clinical-border rounded-lg appearance-none cursor-pointer accent-clinical-accent"
                />
                <span className="text-[11px] font-semibold text-clinical-muted shrink-0">Right: CBCT</span>
              </div>
            )}

            {fusionMode === "checkerboard" && (
              <div className="flex items-center gap-2">
                <span className="text-[11px] font-semibold text-clinical-muted">Tile:</span>
                {[16, 32, 64].map((sz) => (
                  <button
                    key={sz}
                    onClick={() => setCheckerSize(sz)}
                    className={`px-2 py-0.5 rounded text-[11px] font-mono font-semibold border ${
                      checkerSize === sz
                        ? "bg-clinical-accent text-white border-clinical-accent"
                        : "border-clinical-border text-clinical-muted"
                    }`}
                  >
                    {sz}px
                  </button>
                ))}
              </div>
            )}

            {/* RTSTRUCT Toggle & Drawer Button */}
            <div className="flex items-center gap-1.5">
              <button
                onClick={() => setShowContours(!showContours)}
                className={`px-2.5 py-1 rounded text-xs font-semibold flex items-center gap-1.5 border transition-all ${
                  showContours
                    ? "bg-green-100 dark:bg-green-950 border-green-500 text-green-800 dark:text-green-300 font-bold"
                    : "border-clinical-border text-clinical-muted hover:text-clinical-text"
                }`}
              >
                {showContours ? <Eye size={13} /> : <EyeOff size={13} />}
                Contours ({visibleContours.length})
              </button>
              <button
                onClick={() => setShowRoiDrawer(!showRoiDrawer)}
                className={`p-1.5 rounded border border-clinical-border hover:bg-clinical-bg text-clinical-muted hover:text-clinical-text ${
                  showRoiDrawer ? "bg-clinical-bg text-clinical-accent" : ""
                }`}
                title="Open ROI Contour Manager"
              >
                <Sliders size={13} />
              </button>
            </div>
          </div>

          {/* Canvas Viewer Viewport */}
          <div
            className="relative rounded-xl border border-clinical-border bg-black overflow-hidden flex items-center justify-center select-none"
            style={{ minHeight: "520px" }}
            onWheel={handleWheel}
            onMouseMove={handleMouseMove}
            onMouseLeave={() => setHoverPixel(null)}
          >
            {/* HTML5 Canvas for Fused HU Pixel Map */}
            <canvas
              ref={canvasRef}
              className="max-w-full max-h-full object-contain"
              style={{ width: "512px", height: "512px", imageRendering: "pixelated" }}
            />

            {/* SVG Vector Overlay for Contours & Annotations */}
            {showContours && (
              <svg
                viewBox="0 0 512 512"
                className="absolute inset-0 max-w-full max-h-full pointer-events-none mx-auto"
                style={{ width: "512px", height: "512px" }}
              >
                {visibleContours.map((c, idx) => {
                  const ptsStr = c.points.map((p, i) => `${i === 0 ? "M" : "L"} ${p[0]} ${p[1]}`).join(" ") + " Z";
                  const colorStr = `rgb(${c.color[0]}, ${c.color[1]}, ${c.color[2]})`;
                  return (
                    <path
                      key={idx}
                      d={ptsStr}
                      stroke={colorStr}
                      strokeWidth={1.75}
                      fill={
                        showContourFill
                          ? `rgba(${c.color[0]}, ${c.color[1]}, ${c.color[2]}, 0.2)`
                          : "none"
                      }
                      strokeLinejoin="round"
                      strokeLinecap="round"
                    />
                  );
                })}

                {/* Subtle Isocenter Crosshair */}
                <line x1={256} y1={246} x2={256} y2={266} stroke="rgba(255,255,255,0.4)" strokeWidth={1} />
                <line x1={246} y1={256} x2={266} y2={256} stroke="rgba(255,255,255,0.4)" strokeWidth={1} />
              </svg>
            )}

            {/* Orientation Labels */}
            <div className="absolute top-2 left-1/2 -translate-x-1/2 text-[11px] font-bold text-white/70 bg-black/40 px-1.5 py-0.5 rounded pointer-events-none">
              A
            </div>
            <div className="absolute bottom-2 left-1/2 -translate-x-1/2 text-[11px] font-bold text-white/70 bg-black/40 px-1.5 py-0.5 rounded pointer-events-none">
              P
            </div>
            <div className="absolute left-2 top-1/2 -translate-y-1/2 text-[11px] font-bold text-white/70 bg-black/40 px-1.5 py-0.5 rounded pointer-events-none">
              R
            </div>
            <div className="absolute right-2 top-1/2 -translate-y-1/2 text-[11px] font-bold text-white/70 bg-black/40 px-1.5 py-0.5 rounded pointer-events-none">
              L
            </div>

            {/* Loading Indicator */}
            {sliceLoading && (
              <div className="absolute top-3 right-3 bg-black/60 backdrop-blur-xs text-white px-2 py-1 rounded text-xs flex items-center gap-1.5">
                <RefreshCw className="animate-spin" size={12} />
                <span>Resampling…</span>
              </div>
            )}

            {/* Scale Bar (5 cm) */}
            <div className="absolute bottom-3 right-3 text-white/80 bg-black/60 px-2 py-1 rounded text-[10px] font-mono pointer-events-none flex flex-col items-center">
              <div className="w-12 h-1 border-b border-l border-r border-white/80 mb-0.5" />
              <span>5 cm</span>
            </div>

            {/* Slice & Position Floating Badge */}
            <div className="absolute bottom-3 left-3 bg-black/70 backdrop-blur-xs text-white px-2.5 py-1 rounded text-xs font-mono pointer-events-none space-y-0.5">
              <div>
                Slice {sliceIdx + 1} / {oirInfo.num_slices} &bull; Z:{" "}
                <span className="text-clinical-accent font-bold">
                  {oirInfo.z_coordinates[sliceIdx] ?? 0} mm
                </span>
              </div>
              {hoverPixel && (
                <div className="text-[10px] text-gray-300">
                  X:{hoverPixel.x} Y:{hoverPixel.y} &bull; TPCT: {hoverPixel.huTpct} HU &bull; CBCT: {hoverPixel.huCbct} HU
                </div>
              )}
            </div>
          </div>

          {/* Slice Slider & Navigation Controls */}
          <div className="rounded-xl border border-clinical-border bg-clinical-surface p-3 space-y-2 text-xs">
            <div className="flex items-center justify-between">
              <span className="font-semibold text-clinical-text">
                Axial Slice Navigation:
              </span>
              <div className="flex items-center gap-2">
                <span className="font-mono text-clinical-muted">
                  Slice {sliceIdx + 1} of {oirInfo.num_slices} ({oirInfo.z_coordinates[sliceIdx]} mm)
                </span>
                <div className="flex items-center gap-1">
                  <button
                    onClick={() => setSliceIdx((p) => Math.max(0, p - 5))}
                    className="px-1.5 py-0.5 rounded bg-clinical-bg border border-clinical-border hover:bg-clinical-border text-[11px] font-mono"
                    title="-5 slices"
                  >
                    -5
                  </button>
                  <button
                    onClick={() => setSliceIdx((p) => Math.max(0, p - 1))}
                    className="p-1 rounded bg-clinical-bg border border-clinical-border hover:bg-clinical-border"
                    title="-1 slice"
                  >
                    <ChevronLeft size={14} />
                  </button>
                  <button
                    onClick={() => setSliceIdx((p) => Math.min(oirInfo.num_slices - 1, p + 1))}
                    className="p-1 rounded bg-clinical-bg border border-clinical-border hover:bg-clinical-border"
                    title="+1 slice"
                  >
                    <ChevronRight size={14} />
                  </button>
                  <button
                    onClick={() => setSliceIdx((p) => Math.min(oirInfo.num_slices - 1, p + 5))}
                    className="px-1.5 py-0.5 rounded bg-clinical-bg border border-clinical-border hover:bg-clinical-border text-[11px] font-mono"
                    title="+5 slices"
                  >
                    +5
                  </button>
                </div>
              </div>
            </div>

            <input
              type="range"
              min={0}
              max={oirInfo.num_slices - 1}
              value={sliceIdx}
              onChange={(e) => setSliceIdx(parseInt(e.target.value, 10))}
              className="w-full h-2 bg-clinical-border rounded-lg appearance-none cursor-pointer accent-clinical-accent"
            />
          </div>

          {/* Window / Level Controls Bar */}
          <div className="rounded-xl border border-clinical-border bg-clinical-surface p-3 flex flex-wrap items-center justify-between gap-3 text-xs">
            <div className="flex items-center gap-2">
              <span className="font-semibold text-clinical-text shrink-0">W/L Presets:</span>
              <div className="flex items-center gap-1 flex-wrap">
                {WL_PRESETS.map((p) => (
                  <button
                    key={p.label}
                    onClick={() => {
                      setWindowWidth(p.width);
                      setWindowCenter(p.center);
                    }}
                    className={`px-2 py-1 rounded text-xs border transition-all ${
                      windowWidth === p.width && windowCenter === p.center
                        ? "bg-clinical-accent text-white border-clinical-accent font-semibold"
                        : "border-clinical-border text-clinical-muted hover:text-clinical-text"
                    }`}
                  >
                    {p.label}
                  </button>
                ))}
              </div>
            </div>

            {/* Custom W/L Sliders */}
            <div className="flex items-center gap-3">
              <div className="flex items-center gap-1.5">
                <span className="text-[11px] text-clinical-muted">W:</span>
                <input
                  type="number"
                  value={windowWidth}
                  onChange={(e) => setWindowWidth(Math.max(1, parseInt(e.target.value) || 1))}
                  className="w-16 px-1.5 py-0.5 rounded border border-clinical-border text-right font-mono text-xs bg-clinical-bg"
                />
              </div>
              <div className="flex items-center gap-1.5">
                <span className="text-[11px] text-clinical-muted">L:</span>
                <input
                  type="number"
                  value={windowCenter}
                  onChange={(e) => setWindowCenter(parseInt(e.target.value) || 0)}
                  className="w-16 px-1.5 py-0.5 rounded border border-clinical-border text-right font-mono text-xs bg-clinical-bg"
                />
              </div>
            </div>
          </div>
        </div>

        {/* Right Column (4/12): RTSTRUCT ROI Contours & OIR Sign-Off */}
        <div className="lg:col-span-4 space-y-4">
          {/* RTSTRUCT Structure Selector Box */}
          <div className="rounded-xl border border-clinical-border bg-clinical-surface p-4 space-y-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Layers size={16} className="text-clinical-accent" />
                <h3 className="text-xs font-bold text-clinical-text uppercase tracking-wider">
                  RTSTRUCT Contours ({oirInfo.rois.length})
                </h3>
              </div>
              <div className="flex items-center gap-1 text-[11px]">
                <button
                  onClick={() => setSelectedRoiNumbers(new Set(oirInfo.rois.map((r) => r.roi_number)))}
                  className="text-clinical-accent hover:underline font-semibold"
                >
                  All
                </button>
                <span className="text-clinical-muted">&bull;</span>
                <button
                  onClick={() => setSelectedRoiNumbers(new Set())}
                  className="text-clinical-muted hover:text-clinical-text"
                >
                  None
                </button>
                <span className="text-clinical-muted">&bull;</span>
                <button
                  onClick={() => setShowContourFill(!showContourFill)}
                  className={`px-1.5 py-0.2 rounded font-semibold ${
                    showContourFill ? "bg-clinical-accent/20 text-clinical-accent" : "text-clinical-muted"
                  }`}
                  title="Toggle semi-transparent color fill inside contours"
                >
                  Fill
                </button>
              </div>
            </div>

            {/* Scrollable list of ROIs */}
            <div className="max-h-60 overflow-y-auto space-y-1.5 pr-1 text-xs">
              {oirInfo.rois.map((roi) => {
                const isSelected = selectedRoiNumbers.has(roi.roi_number);
                const currentZ = oirInfo.z_coordinates[sliceIdx] ?? 0;
                const isOnSlice = roi.z_min !== null && roi.z_max !== null && currentZ >= roi.z_min && currentZ <= roi.z_max;

                return (
                  <label
                    key={roi.roi_number}
                    className={`flex items-center justify-between p-1.5 rounded cursor-pointer transition-colors ${
                      isOnSlice ? "bg-clinical-bg/80 font-semibold" : "hover:bg-clinical-bg/40 opacity-80"
                    }`}
                  >
                    <div className="flex items-center gap-2 truncate">
                      <input
                        type="checkbox"
                        checked={isSelected}
                        onChange={(e) => {
                          const next = new Set(selectedRoiNumbers);
                          if (e.target.checked) next.add(roi.roi_number);
                          else next.delete(roi.roi_number);
                          setSelectedRoiNumbers(next);
                        }}
                        className="rounded border-clinical-border text-clinical-accent focus:ring-clinical-accent"
                      />
                      <span
                        className="w-3 h-3 rounded-full shrink-0 border border-black/20"
                        style={{ backgroundColor: `rgb(${roi.color[0]}, ${roi.color[1]}, ${roi.color[2]})` }}
                      />
                      <span className="truncate text-clinical-text" title={roi.roi_name}>
                        {roi.roi_name}
                      </span>
                    </div>
                    {isOnSlice && (
                      <span className="text-[10px] font-bold text-green-600 dark:text-green-400 bg-green-100 dark:bg-green-950/80 px-1.5 py-0.2 rounded">
                        On Slice
                      </span>
                    )}
                  </label>
                );
              })}
            </div>
          </div>

          {/* Offline Image Review (OIR) Sign-Off Panel */}
          <div className="rounded-xl border border-clinical-border bg-clinical-surface p-4 space-y-3.5 shadow-sm">
            <div className="flex items-center justify-between border-b border-clinical-border pb-2.5">
              <div className="flex items-center gap-2">
                <ClipboardCheck size={16} className="text-clinical-accent" />
                <h3 className="text-xs font-bold text-clinical-text uppercase tracking-wider">
                  Offline Image Review Sign-Off
                </h3>
              </div>
              <span className="text-[10px] font-bold px-2 py-0.5 rounded bg-blue-100 dark:bg-blue-950 text-blue-700 dark:text-blue-300">
                Fx {selectedFraction}
              </span>
            </div>

            {/* Role Toggle Selector */}
            <div className="space-y-1 text-xs">
              <label className="font-semibold text-clinical-muted text-[11px] block">
                Sign-Off Clinical Role:
              </label>
              <div className="grid grid-cols-2 gap-1.5">
                <button
                  type="button"
                  onClick={() => handleRoleChange("physicist")}
                  className={`py-1.5 px-2 rounded-lg font-semibold text-xs border transition-all ${
                    reviewerRole === "physicist"
                      ? "bg-clinical-accent text-white border-clinical-accent shadow-xs"
                      : "border-clinical-border text-clinical-muted hover:text-clinical-text bg-clinical-bg"
                  }`}
                >
                  Medical Physicist
                </button>
                <button
                  type="button"
                  onClick={() => handleRoleChange("physician")}
                  className={`py-1.5 px-2 rounded-lg font-semibold text-xs border transition-all ${
                    reviewerRole === "physician"
                      ? "bg-clinical-accent text-white border-clinical-accent shadow-xs"
                      : "border-clinical-border text-clinical-muted hover:text-clinical-text bg-clinical-bg"
                  }`}
                >
                  Radiation Oncologist
                </button>
              </div>
            </div>

            {/* Checklist Verification Items based on Role */}
            <div className="space-y-2 text-xs">
              {reviewerRole === "physicist" ? (
                <>
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={shiftsVerified}
                      onChange={(e) => setShiftsVerified(e.target.checked)}
                      className="rounded border-clinical-border text-clinical-accent focus:ring-clinical-accent"
                    />
                    <span className="text-clinical-text">
                      6-DoF table shifts verified within institutional action limits (&le; 3 mm / 2&deg;)
                    </span>
                  </label>

                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={contoursVerified}
                      onChange={(e) => setContoursVerified(e.target.checked)}
                      className="rounded border-clinical-border text-clinical-accent focus:ring-clinical-accent"
                    />
                    <span className="text-clinical-text">
                      Patient anatomy &amp; critical OAR margins match Planning CT geometry
                    </span>
                  </label>
                </>
              ) : (
                <>
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={shiftsVerified}
                      onChange={(e) => setShiftsVerified(e.target.checked)}
                      className="rounded border-clinical-border text-clinical-accent focus:ring-clinical-accent"
                    />
                    <span className="text-clinical-text">
                      Daily CBCT / kV alignment &amp; target coverage clinically verified by Physician
                    </span>
                  </label>

                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={contoursVerified}
                      onChange={(e) => setContoursVerified(e.target.checked)}
                      className="rounded border-clinical-border text-clinical-accent focus:ring-clinical-accent"
                    />
                    <span className="text-clinical-text">
                      Critical OAR clearance and anatomical margins approved for treatment
                    </span>
                  </label>
                </>
              )}
            </div>

            {/* Verdict Selection */}
            <div className="space-y-1 text-xs">
              <label className="font-semibold text-clinical-muted text-[11px] block">
                Review Verdict:
              </label>
              <div className="grid grid-cols-3 gap-1.5">
                <button
                  type="button"
                  onClick={() => setCheckStatus("pass")}
                  className={`py-1.5 px-2 rounded font-semibold text-center border transition-all ${
                    checkStatus === "pass"
                      ? "bg-green-600 text-white border-green-600 shadow-xs"
                      : "border-clinical-border text-clinical-muted hover:text-clinical-text"
                  }`}
                >
                  Pass / Approved
                </button>
                <button
                  type="button"
                  onClick={() => setCheckStatus("acceptable")}
                  className={`py-1.5 px-2 rounded font-semibold text-center border transition-all ${
                    checkStatus === "acceptable"
                      ? "bg-amber-500 text-white border-amber-500 shadow-xs"
                      : "border-clinical-border text-clinical-muted hover:text-clinical-text"
                  }`}
                >
                  Acceptable
                </button>
                <button
                  type="button"
                  onClick={() => setCheckStatus("flagged")}
                  className={`py-1.5 px-2 rounded font-semibold text-center border transition-all ${
                    checkStatus === "flagged"
                      ? "bg-red-600 text-white border-red-600 shadow-xs"
                      : "border-clinical-border text-clinical-muted hover:text-clinical-text"
                  }`}
                >
                  Flagged
                </button>
              </div>
            </div>

            {/* Reviewer Name & Notes Input */}
            <div className="space-y-2 text-xs">
              <div>
                <label className="font-semibold text-clinical-muted text-[11px] block mb-1">
                  {reviewerRole === "physician" ? "Physician Name / Initials:" : "Physicist Name / Initials:"}
                </label>
                <input
                  type="text"
                  value={reviewerName}
                  onChange={(e) => setReviewerName(e.target.value)}
                  placeholder={reviewerRole === "physician" ? "e.g. Dr. Jane Smith, MD" : "e.g. A. Holt, MS, DABR"}
                  className="w-full px-2.5 py-1.5 rounded-lg border border-clinical-border bg-clinical-bg text-clinical-text text-xs focus:outline-none focus:ring-1 focus:ring-clinical-accent"
                />
              </div>

              <div>
                <label className="font-semibold text-clinical-muted text-[11px] block mb-1">
                  Clinical Review Comments:
                </label>
                <textarea
                  rows={2}
                  value={checkNotes}
                  onChange={(e) => setCheckNotes(e.target.value)}
                  placeholder={
                    reviewerRole === "physician"
                      ? "e.g., Daily CBCT verified. Target position acceptable for treatment."
                      : "e.g., Target coverage intact. Cord clearance confirmed. No anatomical change noted."
                  }
                  className="w-full px-2.5 py-1.5 rounded-lg border border-clinical-border bg-clinical-bg text-clinical-text text-xs focus:outline-none focus:ring-1 focus:ring-clinical-accent resize-none"
                />
              </div>
            </div>

            {/* Submit Sign-Off Button */}
            <button
              onClick={handleSaveChartCheck}
              disabled={savingCheck}
              className="w-full py-2 px-3 rounded-lg bg-clinical-accent hover:bg-clinical-accent/90 text-white font-semibold text-xs flex items-center justify-center gap-1.5 shadow-sm transition-all disabled:opacity-50"
            >
              <UserCheck size={14} />
              {savingCheck
                ? "Saving Sign-Off…"
                : `Sign Off Fraction ${selectedFraction} (${reviewerRole === "physician" ? "Physician" : "Physicist"})`}
            </button>

            {/* Previous Sign-Off Reviews History */}
            {chartChecks.length > 0 && (
              <div className="pt-2 border-t border-clinical-border space-y-2">
                <span className="text-[11px] font-bold text-clinical-muted uppercase tracking-wider block">
                  Sign-Off Log ({chartChecks.length}):
                </span>
                <div className="max-h-52 overflow-y-auto space-y-2 text-xs">
                  {chartChecks.map((cc) => (
                    <div
                      key={cc.id}
                      className="p-2.5 rounded-lg bg-clinical-bg border border-clinical-border/60 space-y-1.5"
                    >
                      <div className="flex items-center justify-between">
                        <span className="font-bold text-clinical-text">
                          Fraction {cc.fraction_number}
                        </span>
                        <span
                          className={`text-[10px] font-bold px-1.5 py-0.5 rounded uppercase ${
                            cc.status === "pass"
                              ? "bg-green-100 dark:bg-green-950 text-green-700 dark:text-green-300"
                              : cc.status === "acceptable"
                              ? "bg-amber-100 dark:bg-amber-950 text-amber-700 dark:text-amber-300"
                              : "bg-red-100 dark:bg-red-950 text-red-700 dark:text-red-300"
                          }`}
                        >
                          {cc.status}
                        </span>
                      </div>

                      {/* Status indicators for Physics and Physician */}
                      <div className="grid grid-cols-2 gap-1.5 text-[11px]">
                        <div className="p-1.5 rounded bg-clinical-surface/80 border border-clinical-border/50">
                          <span className="text-[10px] font-semibold text-clinical-muted block">
                            Physics Sign-Off:
                          </span>
                          {cc.physics_reviewed ? (
                            <div>
                              <span className="font-semibold text-clinical-text block truncate" title={cc.physics_reviewer || "Medical Physicist"}>
                                {cc.physics_reviewer || "Medical Physicist"}
                              </span>
                              <span className="text-[9px] text-green-600 dark:text-green-400 font-bold uppercase">
                                Signed ({cc.physics_status || "Pass"})
                              </span>
                            </div>
                          ) : (
                            <span className="text-[10px] text-amber-600 dark:text-amber-400 font-semibold italic">
                              Pending Review
                            </span>
                          )}
                        </div>

                        <div className="p-1.5 rounded bg-clinical-surface/80 border border-clinical-border/50">
                          <span className="text-[10px] font-semibold text-clinical-muted block">
                            Physician Sign-Off:
                          </span>
                          {cc.physician_reviewed ? (
                            <div>
                              <span className="font-semibold text-clinical-text block truncate" title={cc.physician_reviewer || "Radiation Oncologist"}>
                                {cc.physician_reviewer || "Radiation Oncologist"}
                              </span>
                              <span className="text-[9px] text-green-600 dark:text-green-400 font-bold uppercase">
                                Signed ({cc.physician_status || "Pass"})
                              </span>
                            </div>
                          ) : (
                            <span className="text-[10px] text-clinical-muted font-semibold italic">
                              Pending Review
                            </span>
                          )}
                        </div>
                      </div>

                      {cc.notes && (
                        <p className="text-[11px] text-clinical-muted italic">
                          "{cc.notes}"
                        </p>
                      )}
                      <span className="text-[9px] text-clinical-muted block">
                        Updated: {new Date(cc.timestamp).toLocaleString()}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};

export default OIRViewer;
