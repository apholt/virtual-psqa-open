import React, { useEffect, useRef, useState } from "react";
import toast from "react-hot-toast";
import {
  Activity,
  AlertTriangle,
  CheckCircle,
  Clock,
  Download,
  Eye,
  FileText,
  Layers,
  Play,
  RefreshCw,
  ShieldCheck,
  Sun,
  Upload,
  X,
  XOctagon,
} from "lucide-react";
import {
  approveExternalAndCalculate,
  calculateSyntheticCT,
  cancelSyntheticCT,
  generateSyntheticCT,
  getFractionExternalInfo,
  getFractionSyntheticCT,
  getPlanSyntheticCTs,
  importCBCTAndGenerate,
  recomputeExternalContour,
  syntheticCTReportUrl,
} from "../api/client";
import {
  ExternalContourInfo,
  PlanSummary,
  SyntheticCTDetail,
  SyntheticCTSummary,
} from "../types";
import { DeformedDVHCard } from "./DeformedDVHCard";

interface SyntheticCTViewerProps {
  planId: number;
  plan?: PlanSummary | null;
}

const WL_PRESETS = [
  { label: "Soft Tissue", width: 400, center: 40 },
  { label: "Dental / Metal", width: 4000, center: 500 },
  { label: "Bone", width: 1800, center: 400 },
  { label: "Brain", width: 80, center: 40 },
  { label: "Lung / Air", width: 1500, center: -600 },
];

export const SyntheticCTViewer: React.FC<SyntheticCTViewerProps> = ({ planId }) => {
  const [summaries, setSummaries] = useState<SyntheticCTSummary[]>([]);
  const [selectedFraction, setSelectedFraction] = useState<number>(1);
  const [detail, setDetail] = useState<SyntheticCTDetail | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [calculating, setCalculating] = useState<boolean>(false);
  const [generating, setGenerating] = useState<boolean>(false);
  const [showUploadModal, setShowUploadModal] = useState<boolean>(false);

  // Upload state
  const [uploadFraction, setUploadFraction] = useState<number>(1);
  const [uploadFiles, setUploadFiles] = useState<File[]>([]);
  const [autoGenerate, setAutoGenerate] = useState<boolean>(true);
  const [autoCalc, setAutoCalc] = useState<boolean>(true);
  const [dirMethod, setDirMethod] = useState<string>("demons");
  const [uploading, setUploading] = useState<boolean>(false);
  const [uploadError, setUploadError] = useState<string | null>(null);

  // Window / Level state
  const [windowWidth, setWindowWidth] = useState<number>(400);
  const [windowCenter, setWindowCenter] = useState<number>(40);

  // External Contour state
  const [showContour, setShowContour] = useState<boolean>(true);
  const [externalInfo, setExternalInfo] = useState<ExternalContourInfo | null>(null);
  const [recomputingContour, setRecomputingContour] = useState<boolean>(false);
  const [approvingContour, setApprovingContour] = useState<boolean>(false);
  const [cancellingDose, setCancellingDose] = useState<boolean>(false);

  // Contour Determination Parameters
  const [contourSource, setContourSource] = useState<string>("rtstruct");
  const [contourThreshold, setContourThreshold] = useState<number>(-350);
  const [contourRadius, setContourRadius] = useState<number>(5);
  const [useConvexHull, setUseConvexHull] = useState<boolean>(false);
  const [includeMask, setIncludeMask] = useState<boolean>(true);
  const [selectedRoi, setSelectedRoi] = useState<string>("");

  // Slice viewer state
  const [sliceZ, setSliceZ] = useState<number>(0);
  const [viewMode, setViewMode] = useState<"dose" | "ct" | "cbct" | "gamma">("ct");
  const [doseOpacity, setDoseOpacity] = useState<number>(0.65);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [planeLoading, setPlaneLoading] = useState<boolean>(false);

  const fetchSummaries = async () => {
    try {
      const data = await getPlanSyntheticCTs(planId);
      setSummaries(data);
      if (data.length > 0) {
        const found = data.find((s) => s.fraction_number === selectedFraction);
        if (!found) {
          setSelectedFraction(data[0].fraction_number);
        }
      }
    } catch (err) {
      console.error("Error fetching synthetic CT summaries:", err);
    } finally {
      setLoading(false);
    }
  };

  const fetchExternalInfo = async (fx: number, targetMode: string = viewMode) => {
    try {
      const targetParam = targetMode === "cbct" ? "cbct" : "sct";
      const info = await getFractionExternalInfo(planId, fx, targetParam);
      setExternalInfo(info);
      if (info.source) {
        setContourSource(info.source);
      }
      if (info.threshold_hu !== undefined) {
        setContourThreshold(info.threshold_hu);
      }
      if (info.closing_radius !== undefined) {
        setContourRadius(info.closing_radius);
      }
      if (info.use_convex_hull !== undefined) {
        setUseConvexHull(info.use_convex_hull);
      }
      if (info.include_mask !== undefined) {
        setIncludeMask(info.include_mask);
      }
      if (info.selected_roi) {
        setSelectedRoi(info.selected_roi);
      } else if (info.available_rois && info.available_rois.length > 0) {
        const rec = info.available_rois.find((r) => r.is_recommended) || info.available_rois[0];
        setSelectedRoi(rec.roi_name);
      }
    } catch (err) {
      setExternalInfo(null);
    }
  };

  const fetchDetail = async (fx: number) => {
    try {
      const d = await getFractionSyntheticCT(planId, fx);
      setDetail(d);
      const totalSlices = d.num_slices > 0 ? d.num_slices : (d.cbct_num_slices || 0);
      if (totalSlices > 0 && sliceZ >= totalSlices) {
        setSliceZ(Math.floor(totalSlices / 2));
      }
      // If the scan has dose, default to dose overlay, else synthetic CT
      if (d.has_dose && viewMode === "ct") {
        setViewMode("dose");
      }
      fetchExternalInfo(fx, viewMode);
    } catch (err) {
      setDetail(null);
      setExternalInfo(null);
    }
  };

  useEffect(() => {
    fetchSummaries();
  }, [planId]);

  useEffect(() => {
    if (selectedFraction) {
      fetchDetail(selectedFraction);
    }
  }, [planId, selectedFraction]);

  useEffect(() => {
    if (selectedFraction) {
      fetchExternalInfo(selectedFraction, viewMode);
    }
  }, [viewMode]);

  // Auto-poll while synthetic CT generation or calculation is active
  useEffect(() => {
    const isWorking =
      (detail && ["generating", "running", "pending", "queued"].includes(detail.status)) ||
      summaries.some((s) => ["generating", "running", "pending", "queued"].includes(s.status));
    if (!isWorking) return;

    const interval = setInterval(async () => {
      try {
        await fetchSummaries();
        if (selectedFraction) {
          await fetchDetail(selectedFraction);
        }
      } catch (err) {
        console.error("Polling synthetic CT error:", err);
      }
    }, 2500);

    return () => clearInterval(interval);
  }, [detail?.status, summaries, selectedFraction, planId]);

  // Load and render slice image
  useEffect(() => {
    if (!detail || !canvasRef.current) return;
    const totalSlices = viewMode === "cbct"
      ? (detail.cbct_num_slices || 241)
      : (detail.num_slices > 0 ? detail.num_slices : (detail.cbct_num_slices || 0));
    if (totalSlices === 0) return;

    let isSubscribed = true;
    setPlaneLoading(true);

    const renderPlane = async () => {
      try {
        let baseRes: Response;
        if (viewMode === "cbct") {
          baseRes = await fetch(
            `/api/plans/${planId}/synthetic-ct/${selectedFraction}/cbct/plane/${sliceZ}`
          );
        } else {
          baseRes = await fetch(
            `/api/plans/${planId}/synthetic-ct/${selectedFraction}/ct/plane/${sliceZ}`
          );
        }

        if (!baseRes.ok) return;

        const rows = parseInt(baseRes.headers.get("X-Rows") || "512", 10);
        const cols = parseInt(baseRes.headers.get("X-Cols") || "512", 10);
        const ctBuf = await baseRes.arrayBuffer();
        const ctArray = new Float32Array(ctBuf);

        let doseArray: Float32Array | null = null;
        let maxDose = 1.0;
        if (viewMode === "dose" && detail.has_dose) {
          try {
            const dRes = await fetch(
              `/api/plans/${planId}/synthetic-ct/${selectedFraction}/dose/plane/${sliceZ}`
            );
            if (dRes.ok) {
              maxDose = parseFloat(dRes.headers.get("X-Max-Dose") || "1.0") || 1.0;
              const dBuf = await dRes.arrayBuffer();
              doseArray = new Float32Array(dBuf);
            }
          } catch (e) {
            // dose optional
          }
        }

        let gammaArray: Float32Array | null = null;
        if (viewMode === "gamma" && detail.status === "complete") {
          try {
            const gRes = await fetch(
              `/api/plans/${planId}/synthetic-ct/${selectedFraction}/gamma/plane/${sliceZ}`
            );
            if (gRes.ok) {
              const gBuf = await gRes.arrayBuffer();
              gammaArray = new Float32Array(gBuf);
            }
          } catch (e) {
            // gamma optional
          }
        }

        // Fetch External Contour mask if requested
        let isContourEdge: Uint8Array | null = null;
        if (showContour && (detail.num_slices > 0 || detail.cbct_num_slices)) {
          try {
            const extTarget = viewMode === "cbct" ? "cbct" : "sct";
            const extRes = await fetch(
              `/api/plans/${planId}/synthetic-ct/${selectedFraction}/external/plane/${sliceZ}?target=${extTarget}`
            );
            if (extRes.ok) {
              const extRows = parseInt(extRes.headers.get("X-Rows") || `${rows}`, 10);
              const extCols = parseInt(extRes.headers.get("X-Cols") || `${cols}`, 10);
              const extBuf = await extRes.arrayBuffer();
              const extArray = new Uint8Array(extBuf);

              // 1. Detect edge on mask using its native stride (extCols) to avoid zig-zag distortion
              const nativeEdge = new Uint8Array(extRows * extCols);
              for (let r = 1; r < extRows - 1; r++) {
                const rOffset = r * extCols;
                for (let c = 1; c < extCols - 1; c++) {
                  const idx = rOffset + c;
                  if (extArray[idx] === 1) {
                    if (
                      extArray[idx - 1] === 0 ||
                      extArray[idx + 1] === 0 ||
                      extArray[idx - extCols] === 0 ||
                      extArray[idx + extCols] === 0
                    ) {
                      nativeEdge[idx] = 1;
                      nativeEdge[idx - 1] = 1;
                      nativeEdge[idx + 1] = 1;
                      nativeEdge[idx - extCols] = 1;
                      nativeEdge[idx + extCols] = 1;
                    }
                  }
                }
              }

              // 2. Map to canvas dimensions
              if (extRows === rows && extCols === cols) {
                isContourEdge = nativeEdge;
              } else {
                isContourEdge = new Uint8Array(rows * cols);
                const scaleR = extRows / rows;
                const scaleC = extCols / cols;
                for (let r = 0; r < rows; r++) {
                  const srcR = Math.min(extRows - 1, Math.round(r * scaleR));
                  const dstOffset = r * cols;
                  const srcOffset = srcR * extCols;
                  for (let c = 0; c < cols; c++) {
                    const srcC = Math.min(extCols - 1, Math.round(c * scaleC));
                    if (nativeEdge[srcOffset + srcC] === 1) {
                      isContourEdge[dstOffset + c] = 1;
                    }
                  }
                }
              }
            }
          } catch (e) {
            // contour optional
          }
        }

        if (!isSubscribed || !canvasRef.current) return;
        const canvas = canvasRef.current;
        canvas.width = cols;
        canvas.height = rows;
        const ctx = canvas.getContext("2d");
        if (!ctx) return;

        const imgData = ctx.createImageData(cols, rows);
        const data = imgData.data;

        // Window/level for CT HU
        const minHU = windowCenter - windowWidth / 2;
        const maxHU = windowCenter + windowWidth / 2;

        for (let i = 0; i < rows * cols; i++) {
          const hu = ctArray[i];
          let val = Math.round(((hu - minHU) / (maxHU - minHU)) * 255);
          val = Math.max(0, Math.min(255, val));

          let r = val;
          let g = val;
          let b = val;

          // Overlay Dose
          if (viewMode === "dose" && doseArray) {
            const dVal = doseArray[i];
            const normDose = Math.max(0, Math.min(1, dVal / (maxDose || 1)));
            if (normDose > 0.05) {
              let dr = 0, dg = 0, db = 0;
              if (normDose < 0.25) {
                dr = 0;
                dg = Math.round((normDose / 0.25) * 255);
                db = 255;
              } else if (normDose < 0.5) {
                dr = 0;
                dg = 255;
                db = Math.round((1 - (normDose - 0.25) / 0.25) * 255);
              } else if (normDose < 0.75) {
                dr = Math.round(((normDose - 0.5) / 0.25) * 255);
                dg = 255;
                db = 0;
              } else {
                dr = 255;
                dg = Math.round((1 - (normDose - 0.75) / 0.25) * 255);
                db = 0;
              }

              const alpha = doseOpacity;
              r = Math.round(r * (1 - alpha) + dr * alpha);
              g = Math.round(g * (1 - alpha) + dg * alpha);
              b = Math.round(b * (1 - alpha) + db * alpha);
            }
          }

          // Overlay Gamma
          if (viewMode === "gamma" && gammaArray) {
            const gVal = gammaArray[i];
            if (isFinite(gVal) && gVal >= 0) {
              const alpha = 0.5;
              if (gVal <= 1.0) {
                // Pass -> Green
                r = Math.round(r * (1 - alpha) + 34 * alpha);
                g = Math.round(g * (1 - alpha) + 197 * alpha);
                b = Math.round(b * (1 - alpha) + 94 * alpha);
              } else {
                // Fail -> Red
                r = Math.round(r * (1 - alpha) + 239 * alpha);
                g = Math.round(g * (1 - alpha) + 68 * alpha);
                b = Math.round(b * (1 - alpha) + 68 * alpha);
              }
            }
          }

          // Overlay External Contour (crisp neon emerald outline)
          if (isContourEdge && isContourEdge[i] === 1) {
            r = 16;
            g = 240;
            b = 135;
          }

          const idx = i * 4;
          data[idx] = r;
          data[idx + 1] = g;
          data[idx + 2] = b;
          data[idx + 3] = 255;
        }

        ctx.putImageData(imgData, 0, 0);
      } catch (err) {
        console.error("Failed rendering plane:", err);
      } finally {
        if (isSubscribed) setPlaneLoading(false);
      }
    };

    renderPlane();
    return () => {
      isSubscribed = false;
    };
  }, [planId, selectedFraction, sliceZ, viewMode, doseOpacity, windowWidth, windowCenter, showContour, detail]);

  const handleUploadSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (uploadFiles.length === 0) {
      setUploadError("Please select at least one CBCT DICOM slice or a .zip archive.");
      return;
    }
    setUploading(true);
    setUploadError(null);
    try {
      await importCBCTAndGenerate(
        planId,
        uploadFraction,
        uploadFiles,
        autoGenerate,
        autoCalc,
        dirMethod
      );
      setShowUploadModal(false);
      setUploadFiles([]);
      await fetchSummaries();
      setSelectedFraction(uploadFraction);
      await fetchDetail(uploadFraction);
    } catch (err: any) {
      setUploadError(err?.response?.data?.detail || err.message || "Upload failed");
    } finally {
      setUploading(false);
    }
  };

  const handleGenerate = async (autoCalculate: boolean = true) => {
    if (!selectedFraction) return;
    setGenerating(true);
    try {
      await generateSyntheticCT(planId, selectedFraction, dirMethod, autoCalculate);
      await fetchSummaries();
      await fetchDetail(selectedFraction);
    } catch (err) {
      console.error("Generate sCT error:", err);
    } finally {
      setGenerating(false);
    }
  };

  const handleRecalculate = async () => {
    if (!selectedFraction) return;
    setCalculating(true);
    try {
      await calculateSyntheticCT(planId, selectedFraction);
      await fetchSummaries();
      await fetchDetail(selectedFraction);
    } catch (err) {
      console.error("Recalculate error:", err);
    } finally {
      setCalculating(false);
    }
  };

  const handleCancelDose = async () => {
    if (!selectedFraction) return;
    setCancellingDose(true);
    try {
      await cancelSyntheticCT(planId, selectedFraction);
      toast.success("Calculation cancelled.");
      await fetchSummaries();
      await fetchDetail(selectedFraction);
    } catch (err) {
      console.error("Cancel calculation error:", err);
      toast.error("Failed to cancel calculation.");
    } finally {
      setCancellingDose(false);
      setCalculating(false);
      setGenerating(false);
      setApprovingContour(false);
    }
  };

  const handleRecomputeContour = async () => {
    if (!selectedFraction) return;
    setRecomputingContour(true);
    try {
      await recomputeExternalContour(planId, selectedFraction, {
        source: contourSource,
        threshold_hu: Number(contourThreshold),
        closing_radius: Number(contourRadius),
        use_convex_hull: Boolean(useConvexHull),
        include_mask: Boolean(includeMask),
        roi_name: contourSource === "rtstruct" && selectedRoi ? selectedRoi : undefined,
      });
      await fetchExternalInfo(selectedFraction, viewMode);
      await fetchDetail(selectedFraction);
    } catch (err: any) {
      console.error("Recompute external contour error:", err);
    } finally {
      setRecomputingContour(false);
    }
  };

  const handleApproveContour = async () => {
    if (!selectedFraction) return;
    setApprovingContour(true);
    try {
      await approveExternalAndCalculate(planId, selectedFraction);
      await fetchSummaries();
      await fetchDetail(selectedFraction);
    } catch (err: any) {
      console.error("Approve contour error:", err);
    } finally {
      setApprovingContour(false);
    }
  };

  const currentTotalSlices = detail
    ? (viewMode === "cbct"
        ? (detail.cbct_num_slices || 241)
        : (detail.num_slices > 0 ? detail.num_slices : (detail.cbct_num_slices || 0)))
    : 0;

  // Clamp sliceZ when viewMode or total slices changes
  useEffect(() => {
    if (currentTotalSlices > 0 && sliceZ >= currentTotalSlices) {
      setSliceZ(Math.max(0, currentTotalSlices - 1));
    }
  }, [viewMode, currentTotalSlices, sliceZ]);

  return (
    <div className="space-y-6">
      {/* Header Banner */}
      <div className="bg-clinical-surface border border-clinical-border rounded-xl p-5 shadow-sm">
        <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
          <div>
            <div className="flex items-center gap-2">
              <span className="p-1.5 bg-blue-500/10 text-blue-400 rounded-md">
                <Layers size={18} />
              </span>
              <h2 className="text-lg font-bold text-clinical-text">
                SyntheticQACT Adaptive Setup &amp; Dose Verification
              </h2>
            </div>
            <p className="text-xs text-clinical-muted mt-1">
              Imports daily CBCT, deforms the planning CT (TPCT) to anatomy-of-the-day, and computes openMCsquare proton dose to verify target coverage and anatomy changes.
            </p>
          </div>

          <div className="flex items-center gap-2 flex-wrap">
            <button
              onClick={() => {
                setUploadFraction(selectedFraction || (summaries.length > 0 ? summaries.length + 1 : 1));
                setShowUploadModal(true);
              }}
              className="px-3.5 py-2 bg-blue-600 hover:bg-blue-500 text-white text-xs font-semibold rounded-lg flex items-center gap-1.5 shadow-sm transition-colors"
            >
              <Upload size={14} />
              Import Daily CBCT
            </button>

            <a
              href={syntheticCTReportUrl(planId, selectedFraction, "html")}
              target="_blank"
              rel="noopener noreferrer"
              className="px-3 py-2 bg-clinical-card border border-clinical-border hover:bg-clinical-surface text-clinical-text text-xs font-medium rounded-lg flex items-center gap-1.5 transition-colors"
            >
              <FileText size={14} className="text-blue-400" />
              Adaptive QA Report
            </a>

            <a
              href={syntheticCTReportUrl(planId, selectedFraction, "pdf")}
              target="_blank"
              rel="noopener noreferrer"
              className="p-2 bg-clinical-card border border-clinical-border hover:bg-clinical-surface text-clinical-muted hover:text-clinical-text rounded-lg transition-colors"
              title="Download PDF Report"
            >
              <Download size={14} />
            </a>
          </div>
        </div>
      </div>

      {/* Fraction Course Selector */}
      <div className="space-y-2">
        <div className="flex items-center justify-between text-xs text-clinical-muted px-1">
          <span className="font-semibold uppercase tracking-wider">Treatment Course Fractions</span>
          <span>{summaries.length} Fractions Configured</span>
        </div>

        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-2.5">
          {summaries.map((s) => {
            const isSelected = s.fraction_number === selectedFraction;
            const isPass = s.gamma_passed;
            return (
              <button
                key={s.fraction_number}
                onClick={() => setSelectedFraction(s.fraction_number)}
                className={`p-3 rounded-xl border text-left transition-all ${
                  isSelected
                    ? "bg-blue-600/10 border-blue-500 shadow-sm ring-1 ring-blue-500/20"
                    : "bg-clinical-surface border-clinical-border hover:border-clinical-muted/40"
                }`}
              >
                <div className="flex items-center justify-between">
                  <span className="text-xs font-bold text-clinical-text">
                    Fraction {s.fraction_number}
                  </span>
                  {s.status === "complete" ? (
                    <span
                      className={`w-2 h-2 rounded-full ${
                        isPass ? "bg-emerald-500 shadow-emerald-500/40" : "bg-red-500"
                      }`}
                    />
                  ) : s.status === "contour_check" ? (
                    <span
                      className="w-2 h-2 rounded-full bg-amber-400 animate-pulse shadow-amber-400/40"
                      title="External contour check required"
                    />
                  ) : s.status === "running" || s.status === "generating" ? (
                    <RefreshCw size={12} className="text-blue-400 animate-spin" />
                  ) : (
                    <Clock size={12} className="text-clinical-muted" />
                  )}
                </div>

                <div className="mt-2 text-base font-bold">
                  {s.gamma_passing_rate !== null ? (
                    <span className={isPass ? "text-emerald-400" : "text-red-400"}>
                      {s.gamma_passing_rate.toFixed(1)}%
                    </span>
                  ) : s.status === "contour_check" ? (
                    <span className="text-amber-400 text-xs uppercase font-bold tracking-wide">
                      Contour Check
                    </span>
                  ) : (
                    <span className="text-clinical-muted text-xs uppercase">{s.status.replace("_", " ")}</span>
                  )}
                </div>

                <div className="text-[10px] text-clinical-muted truncate mt-0.5">
                  {s.setup_shift_lat_mm !== null
                    ? `Couch: ${s.setup_shift_lat_mm > 0 ? "+" : ""}${s.setup_shift_lat_mm}mm`
                    : s.num_slices > 0 ? `${s.num_slices} sCT Slices` : `${s.cbct_num_slices || 0} CBCT Slices`}
                </div>
              </button>
            );
          })}

          {summaries.length === 0 && !loading && (
            <div className="col-span-full py-8 text-center bg-clinical-surface border border-dashed border-clinical-border rounded-xl">
              <Layers size={28} className="mx-auto text-clinical-muted mb-2 opacity-50" />
              <p className="text-sm text-clinical-text font-medium">No SyntheticQACT scans imported yet</p>
              <p className="text-xs text-clinical-muted mt-0.5">
                Import daily CBCT scans to generate synthetic CTs and evaluate effective dose delivery.
              </p>
              <button
                onClick={() => setShowUploadModal(true)}
                className="mt-3 px-3 py-1.5 bg-blue-600 hover:bg-blue-500 text-white text-xs font-medium rounded-lg inline-flex items-center gap-1.5"
              >
                <Upload size={13} />
                Import Fraction 1 CBCT
              </button>
            </div>
          )}
        </div>
      </div>

      {/* Selected Fraction Details & 3D Viewer */}
      {detail && (
        <div className="space-y-6">
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Left Column: Adaptive QA Metrics & Actions */}
          <div className="space-y-4">
            {/* KPI Card */}
            <div className="bg-clinical-surface border border-clinical-border rounded-xl p-5 space-y-4">
              <div className="flex items-center justify-between">
                <span className="text-xs font-bold uppercase tracking-wider text-clinical-muted">
                  Adaptive QA · Fraction {detail.fraction_number}
                </span>
                <span
                  className={`px-2 py-0.5 rounded text-[11px] font-bold uppercase tracking-wide border ${
                    detail.gamma_passed
                      ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/30"
                      : detail.status === "contour_check"
                      ? "bg-amber-500/10 text-amber-400 border-amber-500/30"
                      : detail.status === "complete"
                      ? "bg-red-500/10 text-red-400 border-red-500/30"
                      : "bg-blue-500/10 text-blue-400 border-blue-500/30"
                  }`}
                >
                  {detail.gamma_passed
                    ? "PASS (3%/3mm)"
                    : detail.status === "contour_check"
                    ? "CONTOUR CHECK REQUIRED"
                    : detail.status === "complete"
                    ? "ACTION REQUIRED"
                    : detail.status.replace("_", " ")}
                </span>
              </div>

              {/* Gamma score readout */}
              <div className="flex items-baseline gap-3">
                <div
                  className={`text-4xl font-extrabold ${
                    detail.gamma_passed
                      ? "text-emerald-400"
                      : detail.status === "complete"
                      ? "text-red-400"
                      : "text-clinical-text"
                  }`}
                >
                  {detail.gamma_passing_rate !== null ? `${detail.gamma_passing_rate.toFixed(1)}%` : "—"}
                </div>
                <div className="text-xs text-clinical-muted">
                  passing rate
                  <div className="text-[10px] text-clinical-muted/80">3% / 3mm DTA · &ge;90% pass</div>
                </div>
              </div>

              {/* Secondary Metrics */}
              <div className="grid grid-cols-2 gap-2 pt-2 border-t border-clinical-border/60">
                <div className="bg-clinical-card/60 p-2.5 rounded-lg border border-clinical-border/40">
                  <span className="text-[10px] text-clinical-muted block">2% / 2mm Gamma</span>
                  <span className="text-sm font-semibold text-clinical-text">
                    {detail.gamma_2mm_passing_rate !== null
                      ? `${detail.gamma_2mm_passing_rate.toFixed(1)}%`
                      : "—"}
                  </span>
                </div>

                <div className="bg-clinical-card/60 p-2.5 rounded-lg border border-clinical-border/40">
                  <span className="text-[10px] text-clinical-muted block">Mean Dose &Delta;</span>
                  <span className="text-sm font-semibold text-clinical-text">
                    {detail.mean_dose_diff_pct !== null
                      ? `${detail.mean_dose_diff_pct > 0 ? "+" : ""}${detail.mean_dose_diff_pct.toFixed(1)}%`
                      : "—"}
                  </span>
                </div>
              </div>

              {/* DIR QA Statistics */}
              {detail.mae_hu_after !== null && (
                <div className="pt-3 border-t border-clinical-border/60 space-y-2 text-xs">
                  <span className="font-semibold text-clinical-text flex items-center gap-1">
                    <Activity size={12} className="text-blue-400" />
                    DIR Deformation QA
                  </span>
                  <div className="grid grid-cols-2 gap-2 text-[11px] text-clinical-muted bg-clinical-card/40 p-2 rounded-lg">
                    <div>
                      <span className="text-clinical-muted/80 block">HU MAE Before:</span>
                      <span className="font-mono text-clinical-text">{detail.mae_hu_before ?? "—"} HU</span>
                    </div>
                    <div>
                      <span className="text-clinical-muted/80 block">HU MAE After:</span>
                      <span className="font-mono text-emerald-400 font-semibold">{detail.mae_hu_after ?? "—"} HU</span>
                    </div>
                    <div>
                      <span className="text-clinical-muted/80 block">Mean Disp:</span>
                      <span className="font-mono text-clinical-text">{detail.dir_mean_displacement_mm ?? "—"} mm</span>
                    </div>
                    <div>
                      <span className="text-clinical-muted/80 block">P99 Disp:</span>
                      <span className="font-mono text-clinical-text">{detail.dir_p99_displacement_mm ?? "—"} mm</span>
                    </div>
                  </div>
                </div>
              )}

              {/* Couch Alignment Shifts */}
              {(detail.setup_shift_lat_mm !== null || detail.setup_shift_long_mm !== null || detail.setup_shift_vert_mm !== null) && (
                <div className="pt-3 border-t border-clinical-border/60 space-y-1.5 text-xs">
                  <span className="font-semibold text-clinical-text">Couch Setup Shifts (Delivery Log)</span>
                  <div className="grid grid-cols-3 gap-1.5 text-center">
                    <div className="bg-clinical-card/40 py-1.5 rounded border border-clinical-border/40">
                      <div className="text-[10px] text-clinical-muted">Lateral</div>
                      <div className="font-mono text-xs font-semibold text-clinical-text">
                        {detail.setup_shift_lat_mm !== null ? `${detail.setup_shift_lat_mm > 0 ? "+" : ""}${detail.setup_shift_lat_mm}` : "0"} mm
                      </div>
                    </div>
                    <div className="bg-clinical-card/40 py-1.5 rounded border border-clinical-border/40">
                      <div className="text-[10px] text-clinical-muted">Long</div>
                      <div className="font-mono text-xs font-semibold text-clinical-text">
                        {detail.setup_shift_long_mm !== null ? `${detail.setup_shift_long_mm > 0 ? "+" : ""}${detail.setup_shift_long_mm}` : "0"} mm
                      </div>
                    </div>
                    <div className="bg-clinical-card/40 py-1.5 rounded border border-clinical-border/40">
                      <div className="text-[10px] text-clinical-muted">Vertical</div>
                      <div className="font-mono text-xs font-semibold text-clinical-text">
                        {detail.setup_shift_vert_mm !== null ? `${detail.setup_shift_vert_mm > 0 ? "+" : ""}${detail.setup_shift_vert_mm}` : "0"} mm
                      </div>
                    </div>
                  </div>
                </div>
              )}

              {/* Scan Metadata */}
              <div className="pt-3 border-t border-clinical-border/60 text-[11px] text-clinical-muted space-y-1">
                <div className="flex justify-between">
                  <span>sCT Slices:</span>
                  <span className="font-mono text-clinical-text">{detail.num_slices || "Not generated"}</span>
                </div>
                {detail.cbct_num_slices && (
                  <div className="flex justify-between">
                    <span>CBCT Slices:</span>
                    <span className="font-mono text-clinical-text">{detail.cbct_num_slices}</span>
                  </div>
                )}
                <div className="flex justify-between">
                  <span>Resolution:</span>
                  <span className="font-mono text-clinical-text">{detail.dimensions || "—"}</span>
                </div>
                <div className="flex justify-between">
                  <span>DIR Method:</span>
                  <span className="font-mono text-clinical-text capitalize">{detail.dir_method || "demons"}</span>
                </div>
              </div>

              {/* Action Buttons & Status Indicators */}
              <div className="pt-3 border-t border-clinical-border/60 space-y-2">
                {(detail.status === "generating" || detail.status === "running") && (
                  <div className="p-3 bg-blue-500/10 border border-blue-500/30 rounded-lg text-xs text-blue-300 flex items-center gap-2.5">
                    <RefreshCw size={16} className="animate-spin text-blue-400 shrink-0" />
                    <div>
                      <div className="font-semibold text-blue-200">
                        {detail.status === "generating"
                          ? "Generating Synthetic CT (DIR)..."
                          : "Simulating openMCsquare Dose..."}
                      </div>
                      <div className="text-[11px] text-blue-300/80 mt-0.5">
                        Deforming planning CT to daily CBCT anatomy and computing QA metrics. Updates automatically.
                      </div>
                    </div>
                  </div>
                )}

                {(detail.status === "error" || detail.error_message) && (
                  <div className="p-3 bg-red-500/10 border border-red-500/30 rounded-lg text-xs text-red-300 space-y-2">
                    <div className="flex items-start gap-2">
                      <AlertTriangle size={15} className="text-red-400 shrink-0 mt-0.5" />
                      <div className="space-y-1">
                        <div className="font-semibold text-red-200">Processing Error</div>
                        <div className="text-[11px] text-red-300/90 break-words font-mono">
                          {detail.error_message || "An error occurred during synthetic CT generation."}
                        </div>
                      </div>
                    </div>
                    <button
                      onClick={() => handleGenerate(true)}
                      disabled={generating}
                      className="w-full py-1.5 bg-red-600 hover:bg-red-500 disabled:opacity-50 text-white text-xs font-semibold rounded-md flex items-center justify-center gap-1.5 transition-colors"
                    >
                      {generating ? (
                        <>
                          <RefreshCw size={12} className="animate-spin" />
                          Retrying Generation...
                        </>
                      ) : (
                        <>
                          <Play size={12} />
                          Retry Synthetic CT &amp; Dose
                        </>
                      )}
                    </button>
                  </div>
                )}

                {(detail.status === "running" || calculating) ? (
                  <div className="flex items-center gap-2">
                    <div className="flex-1 py-2 bg-blue-500/10 border border-blue-500/30 text-blue-400 text-xs font-medium rounded-lg flex items-center justify-center gap-1.5">
                      <RefreshCw size={13} className="animate-spin text-blue-400" />
                      Simulating MC Dose...
                    </div>
                    <button
                      onClick={handleCancelDose}
                      disabled={cancellingDose}
                      className="px-3.5 py-2 bg-red-600/10 hover:bg-red-600/20 text-red-400 border border-red-500/30 text-xs font-semibold rounded-lg flex items-center justify-center gap-1.5 transition-colors disabled:opacity-50"
                      title="Cancel openMCsquare calculation for this fraction"
                    >
                      <XOctagon size={13} />
                      {cancellingDose ? "Cancelling..." : "Cancel"}
                    </button>
                  </div>
                ) : (detail.status === "generating" || generating) ? (
                  <div className="flex items-center gap-2">
                    <div className="flex-1 py-2 bg-blue-500/10 border border-blue-500/30 text-blue-400 text-xs font-medium rounded-lg flex items-center justify-center gap-1.5">
                      <RefreshCw size={13} className="animate-spin text-blue-400" />
                      Generating Synthetic CT...
                    </div>
                    <button
                      onClick={handleCancelDose}
                      disabled={cancellingDose}
                      className="px-3.5 py-2 bg-red-600/10 hover:bg-red-600/20 text-red-400 border border-red-500/30 text-xs font-semibold rounded-lg flex items-center justify-center gap-1.5 transition-colors disabled:opacity-50"
                      title="Cancel sCT generation for this fraction"
                    >
                      <XOctagon size={13} />
                      {cancellingDose ? "Cancelling..." : "Cancel"}
                    </button>
                  </div>
                ) : (
                  <>
                    {(detail.status === "cbct_uploaded" || (detail.status === "error" && !detail.num_slices)) && (
                      <button
                        onClick={() => handleGenerate(true)}
                        disabled={generating}
                        className="w-full py-2 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white text-xs font-semibold rounded-lg flex items-center justify-center gap-1.5 transition-colors"
                      >
                        <Play size={13} />
                        Generate Synthetic CT &amp; Dose
                      </button>
                    )}

                    {detail.num_slices > 0 && detail.status !== "contour_check" && (
                      <button
                        onClick={handleRecalculate}
                        disabled={calculating}
                        className="w-full py-2 bg-clinical-card border border-clinical-border hover:bg-clinical-surface text-clinical-text text-xs font-medium rounded-lg flex items-center justify-center gap-1.5 transition-colors disabled:opacity-50"
                      >
                        <RefreshCw size={13} />
                        Recalculate openMCsquare Dose
                      </button>
                    )}
                  </>
                )}
              </div>
            </div>

            {/* External Contour Verification & Adjustment Card */}
            {detail.num_slices > 0 && (
              <div
                className={`border rounded-xl p-5 space-y-4 shadow-sm transition-all ${
                  detail.status === "contour_check"
                    ? "bg-amber-500/5 border-amber-500/40 ring-1 ring-amber-500/20"
                    : "bg-clinical-surface border-clinical-border"
                }`}
              >
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <ShieldCheck
                      size={16}
                      className={detail.status === "contour_check" ? "text-amber-400" : "text-emerald-400"}
                    />
                    <span className="text-xs font-bold uppercase tracking-wider text-clinical-text">
                      External Contour Check
                    </span>
                  </div>
                  {detail.status === "contour_check" ? (
                    <span className="px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wide bg-amber-500/10 text-amber-400 border border-amber-500/30">
                      Approval Needed
                    </span>
                  ) : (
                    <span className="px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wide bg-emerald-500/10 text-emerald-400 border border-emerald-500/30 flex items-center gap-1">
                      <CheckCircle size={10} /> Verified
                    </span>
                  )}
                </div>

                <p className="text-xs text-clinical-muted leading-relaxed">
                  High-density dental implants and metal artifacts can cause dark streak shadowing. Inspect the patient external boundary to ensure zero internal voids before calculating dose.
                </p>

                {/* Current Source Badge & Info */}
                <div className="p-3 bg-clinical-card/50 rounded-lg border border-clinical-border/40 text-xs space-y-1.5">
                  <div className="flex justify-between items-center text-[11px]">
                    <span className="text-clinical-muted">Active Mask Source:</span>
                    <span className="font-semibold text-clinical-text uppercase">
                      {externalInfo?.source === "rtstruct"
                        ? "Planning RTSTRUCT External"
                        : externalInfo?.source === "rtstruct_propagated"
                        ? "Propagated RTSTRUCT Skin Prior"
                        : externalInfo?.source === "convex_hull"
                        ? "Convex Envelope (Bridged)"
                        : "Robust Auto-Contour"}
                    </span>
                  </div>
                  <div className="flex justify-between items-center text-[11px]">
                    <span className="text-clinical-muted">Immobilization Mask:</span>
                    <span
                      className={`font-semibold ${
                        externalInfo?.include_mask !== false
                          ? "text-blue-400"
                          : "text-amber-400"
                      }`}
                    >
                      {externalInfo?.include_mask !== false
                        ? "Included (Mask + Patient)"
                        : "Excluded (Skin Dermis Only)"}
                    </span>
                  </div>
                  {externalInfo?.selected_roi && (
                    <div className="flex justify-between items-center text-[11px]">
                      <span className="text-clinical-muted">Selected ROI:</span>
                      <span className="font-mono text-clinical-text font-medium">
                        {externalInfo.selected_roi}
                      </span>
                    </div>
                  )}
                  {externalInfo?.total_voxels ? (
                    <div className="flex justify-between items-center text-[11px]">
                      <span className="text-clinical-muted">Contoured Voxels:</span>
                      <span className="font-mono text-emerald-400 font-semibold">
                        {externalInfo.total_voxels.toLocaleString()} voxels (0 voids)
                      </span>
                    </div>
                  ) : null}
                  {externalInfo?.has_rtstruct && (
                    <div className="text-[10px] text-emerald-400/90 flex items-center gap-1 pt-0.5">
                      <CheckCircle size={10} /> Clinical RTSTRUCT External contour is available.
                    </div>
                  )}
                </div>

                {/* Source Selection & Adjustment Controls */}
                <div className="space-y-3 pt-2 border-t border-clinical-border/50 text-xs">
                  <div>
                    <label className="text-clinical-muted text-[11px] font-semibold block mb-1.5">
                      Contour Determination Source
                    </label>
                    <select
                      value={contourSource}
                      onChange={(e) => setContourSource(e.target.value)}
                      className="w-full bg-clinical-card border border-clinical-border rounded-lg px-2.5 py-1.5 text-xs text-clinical-text focus:outline-none focus:border-blue-500"
                    >
                      {externalInfo?.has_rtstruct && (
                        <option value="rtstruct">
                          Planning RTSTRUCT External (Gold Standard)
                        </option>
                      )}
                      <option value="auto">
                        Robust Auto-Contour (Exterior Flood-Fill &amp; Closing)
                      </option>
                      <option value="convex_hull">
                        Convex Envelope (Streak-Bridging Hull)
                      </option>
                    </select>
                  </div>

                  {/* RTSTRUCT ROI Picker */}
                  {contourSource === "rtstruct" &&
                    externalInfo?.available_rois &&
                    externalInfo.available_rois.length > 0 && (
                      <div>
                        <label className="text-clinical-muted text-[11px] font-semibold block mb-1.5">
                          RTSTRUCT Target ROI
                        </label>
                        <select
                          value={selectedRoi}
                          onChange={(e) => {
                            const val = e.target.value;
                            setSelectedRoi(val);
                            const found = externalInfo.available_rois?.find(
                              (r) => r.roi_name === val
                            );
                            if (found) {
                              setIncludeMask(found.includes_mask);
                            }
                          }}
                          className="w-full bg-clinical-card border border-clinical-border rounded-lg px-2.5 py-1.5 text-xs text-clinical-text focus:outline-none focus:border-blue-500 font-mono"
                        >
                          {externalInfo.available_rois.map((roi) => (
                            <option key={roi.roi_number} value={roi.roi_name}>
                              {roi.roi_name} {roi.includes_mask ? "(Mask + Headrest)" : "(Skin Only)"} {roi.is_recommended ? "★ Recommended" : ""}
                            </option>
                          ))}
                        </select>
                      </div>
                    )}

                  {/* Mask Inclusion Toggle */}
                  <div className="bg-clinical-surface/60 border border-clinical-border/60 rounded-lg p-2.5 space-y-1">
                    <label className="flex items-center gap-2 cursor-pointer select-none">
                      <input
                        type="checkbox"
                        checked={includeMask}
                        onChange={(e) => {
                          const checked = e.target.checked;
                          setIncludeMask(checked);
                          if (checked) {
                            if (contourThreshold > -450) setContourThreshold(-500);
                            if (contourRadius < 10) setContourRadius(15);
                            const maskRoi = externalInfo?.available_rois?.find(
                              (r) => r.includes_mask
                            );
                            if (maskRoi) setSelectedRoi(maskRoi.roi_name);
                          } else {
                            if (contourThreshold <= -450) setContourThreshold(-350);
                            if (contourRadius >= 10) setContourRadius(5);
                            const skinRoi = externalInfo?.available_rois?.find(
                              (r) => !r.includes_mask
                            );
                            if (skinRoi) setSelectedRoi(skinRoi.roi_name);
                          }
                        }}
                        className="rounded border-clinical-border text-blue-500 focus:ring-blue-500 h-4 w-4 cursor-pointer accent-blue-500"
                      />
                      <span className="text-xs font-semibold text-clinical-text">
                        Include Immobilization Mask &amp; Headrest
                      </span>
                    </label>
                    <p className="text-[10px] text-clinical-muted leading-relaxed pl-6">
                      Encapsulates the thermoplastic mask mesh and headrest within the external boundary to avoid missing material during dose computation.
                    </p>
                  </div>

                  {contourSource !== "rtstruct" && (
                    <div className="grid grid-cols-2 gap-2 pt-1">
                      <div>
                        <div className="flex justify-between text-[10px] text-clinical-muted mb-1">
                          <span>Tissue Threshold:</span>
                          <span className="font-mono text-clinical-text">{contourThreshold} HU</span>
                        </div>
                        <input
                          type="range"
                          min="-600"
                          max="-100"
                          step="25"
                          value={contourThreshold}
                          onChange={(e) => setContourThreshold(parseFloat(e.target.value))}
                          className="w-full accent-blue-500 cursor-pointer"
                        />
                      </div>
                      <div>
                        <div className="flex justify-between text-[10px] text-clinical-muted mb-1">
                          <span>Closing Radius:</span>
                          <span className="font-mono text-clinical-text">{contourRadius} px</span>
                        </div>
                        <input
                          type="range"
                          min="1"
                          max="25"
                          step="2"
                          value={contourRadius}
                          onChange={(e) => setContourRadius(parseInt(e.target.value, 10))}
                          className="w-full accent-blue-500 cursor-pointer"
                        />
                      </div>
                    </div>
                  )}

                  <button
                    onClick={handleRecomputeContour}
                    disabled={recomputingContour}
                    className="w-full py-1.5 bg-clinical-card border border-clinical-border hover:bg-clinical-surface text-clinical-text text-xs font-medium rounded-lg flex items-center justify-center gap-1.5 transition-colors disabled:opacity-50"
                  >
                    {recomputingContour ? (
                      <>
                        <RefreshCw size={12} className="animate-spin text-blue-400" />
                        Recomputing External Mask...
                      </>
                    ) : (
                      <>
                        <RefreshCw size={12} />
                        Recompute External Contour
                      </>
                    )}
                  </button>
                </div>

                {/* Primary Action Button: Approve & Calculate Dose */}
                {detail.status === "contour_check" && (
                  <div className="pt-2">
                    <button
                      onClick={handleApproveContour}
                      disabled={approvingContour}
                      className="w-full py-2.5 bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white text-xs font-bold rounded-lg flex items-center justify-center gap-2 shadow-sm transition-colors"
                    >
                      {approvingContour ? (
                        <>
                          <RefreshCw size={14} className="animate-spin" />
                          Starting MC Dose Simulation...
                        </>
                      ) : (
                        <>
                          <ShieldCheck size={14} />
                          Approve Contour &amp; Calculate Dose
                        </>
                      )}
                    </button>
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Right Column: 3D Axial Canvas Viewer */}
          <div className="lg:col-span-2 bg-clinical-surface border border-clinical-border rounded-xl p-5 space-y-4">
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 pb-3 border-b border-clinical-border">
              <div className="flex items-center gap-2">
                <Eye size={16} className="text-blue-400" />
                <span className="text-xs font-bold text-clinical-text uppercase tracking-wider">
                  Interactive Axial Plane
                </span>
              </div>

              {/* View Mode Toggle */}
              <div className="flex items-center gap-1 bg-clinical-card p-1 rounded-lg border border-clinical-border text-xs">
                <button
                  onClick={() => setViewMode("dose")}
                  className={`px-2.5 py-1 rounded font-medium transition-colors ${
                    viewMode === "dose"
                      ? "bg-blue-600 text-white shadow-xs"
                      : "text-clinical-muted hover:text-clinical-text"
                  }`}
                >
                  Dose Overlay
                </button>
                <button
                  onClick={() => setViewMode("ct")}
                  className={`px-2.5 py-1 rounded font-medium transition-colors ${
                    viewMode === "ct"
                      ? "bg-blue-600 text-white shadow-xs"
                      : "text-clinical-muted hover:text-clinical-text"
                  }`}
                >
                  Synthetic CT
                </button>
                {detail.has_cbct && (
                  <button
                    onClick={() => setViewMode("cbct")}
                    className={`px-2.5 py-1 rounded font-medium transition-colors ${
                      viewMode === "cbct"
                        ? "bg-blue-600 text-white shadow-xs"
                        : "text-clinical-muted hover:text-clinical-text"
                    }`}
                  >
                    Raw CBCT
                  </button>
                )}
                <button
                  onClick={() => setViewMode("gamma")}
                  className={`px-2.5 py-1 rounded font-medium transition-colors ${
                    viewMode === "gamma"
                      ? "bg-blue-600 text-white shadow-xs"
                      : "text-clinical-muted hover:text-clinical-text"
                  }`}
                >
                  3D Gamma
                </button>
              </div>
            </div>

            {/* Window / Level & Contour Controls Bar */}
            <div className="bg-clinical-card/70 border border-clinical-border rounded-lg p-3 space-y-3">
              {/* Presets Row */}
              <div className="flex items-center justify-between flex-wrap gap-2">
                <div className="flex items-center gap-1.5 flex-wrap">
                  <span className="text-[11px] font-semibold text-clinical-muted mr-1 flex items-center gap-1">
                    <Sun size={12} /> W/L Presets:
                  </span>
                  {WL_PRESETS.map((p) => {
                    const isActive = windowWidth === p.width && windowCenter === p.center;
                    return (
                      <button
                        key={p.label}
                        onClick={() => {
                          setWindowWidth(p.width);
                          setWindowCenter(p.center);
                        }}
                        className={`px-2 py-1 text-[11px] rounded-md font-medium transition-colors ${
                          isActive
                            ? "bg-blue-600 text-white shadow-xs"
                            : "bg-clinical-surface border border-clinical-border/60 text-clinical-muted hover:text-clinical-text hover:bg-clinical-surface/80"
                        }`}
                      >
                        {p.label} ({p.width}/{p.center})
                      </button>
                    );
                  })}
                </div>

                {/* Contour Overlay Toggle */}
                <button
                  onClick={() => setShowContour(!showContour)}
                  className={`px-2.5 py-1 rounded-md text-[11px] font-medium border flex items-center gap-1.5 transition-colors ${
                    showContour
                      ? "bg-emerald-500/15 border-emerald-500/40 text-emerald-300 shadow-xs"
                      : "bg-clinical-surface border-clinical-border text-clinical-muted hover:text-clinical-text"
                  }`}
                >
                  <span
                    className={`w-2 h-2 rounded-full ${
                      showContour ? "bg-emerald-400 shadow-emerald-400/50 shadow-sm" : "bg-zinc-500"
                    }`}
                  />
                  External Contour
                </button>
              </div>

              {/* Sliders Row: Window Width & Window Center & Dose Opacity */}
              <div className="grid grid-cols-1 md:grid-cols-3 gap-3 pt-1 border-t border-clinical-border/40 text-xs">
                <div>
                  <div className="flex justify-between text-[11px] text-clinical-muted mb-1">
                    <span>Window Width (W):</span>
                    <span className="font-mono text-clinical-text font-semibold">{windowWidth} HU</span>
                  </div>
                  <input
                    type="range"
                    min="50"
                    max="5000"
                    step="50"
                    value={windowWidth}
                    onChange={(e) => setWindowWidth(parseInt(e.target.value, 10))}
                    className="w-full accent-blue-500 cursor-pointer"
                  />
                </div>

                <div>
                  <div className="flex justify-between text-[11px] text-clinical-muted mb-1">
                    <span>Window Level / Center (L):</span>
                    <span className="font-mono text-clinical-text font-semibold">{windowCenter} HU</span>
                  </div>
                  <input
                    type="range"
                    min="-1000"
                    max="1000"
                    step="20"
                    value={windowCenter}
                    onChange={(e) => setWindowCenter(parseInt(e.target.value, 10))}
                    className="w-full accent-blue-500 cursor-pointer"
                  />
                </div>

                {viewMode === "dose" ? (
                  <div>
                    <div className="flex justify-between text-[11px] text-clinical-muted mb-1">
                      <span>Dose Opacity:</span>
                      <span className="font-mono text-clinical-text font-semibold">{Math.round(doseOpacity * 100)}%</span>
                    </div>
                    <input
                      type="range"
                      min="0.1"
                      max="1.0"
                      step="0.05"
                      value={doseOpacity}
                      onChange={(e) => setDoseOpacity(parseFloat(e.target.value))}
                      className="w-full accent-blue-500 cursor-pointer"
                    />
                  </div>
                ) : (
                  <div className="flex items-end justify-end pb-1">
                    <button
                      onClick={() => {
                        setWindowWidth(400);
                        setWindowCenter(40);
                      }}
                      className="px-2.5 py-1 text-[11px] bg-clinical-surface border border-clinical-border/60 hover:border-clinical-muted text-clinical-muted hover:text-clinical-text rounded-md transition-colors"
                    >
                      Reset W/L (400/40)
                    </button>
                  </div>
                )}
              </div>
            </div>

            {/* Canvas Container */}
            <div className="relative aspect-square max-h-[500px] w-full mx-auto bg-black rounded-lg overflow-hidden border border-clinical-border flex items-center justify-center">
              <canvas ref={canvasRef} className="max-w-full max-h-full object-contain" />

              {planeLoading && (
                <div className="absolute inset-0 bg-black/40 backdrop-blur-xs flex items-center justify-center">
                  <RefreshCw size={24} className="text-blue-400 animate-spin" />
                </div>
              )}

              {/* View HUD */}
              <div className="absolute top-3 left-3 bg-black/70 backdrop-blur-md px-2.5 py-1 rounded text-[10px] font-mono text-zinc-300 border border-white/10 flex items-center gap-3">
                <div>
                  Slice: {sliceZ + 1} / {currentTotalSlices || 1}
                </div>
                <div>Mode: {viewMode.toUpperCase()}</div>
                <div>
                  W: {windowWidth} L: {windowCenter}
                </div>
              </div>

              {/* Bottom HUD info */}
              <div className="absolute bottom-3 left-3 bg-black/70 backdrop-blur-md px-2.5 py-1 rounded text-[10px] font-mono text-zinc-300 border border-white/10 flex items-center gap-2">
                {showContour && (
                  <span className="flex items-center gap-1.5 text-emerald-400">
                    <span className="w-2 h-2 rounded-full bg-emerald-400 shadow-sm" />
                    External: {viewMode === "cbct" ? (externalInfo?.source === "rtstruct_propagated" ? "Propagated Skin Prior" : "CBCT Auto") : (externalInfo?.source === "rtstruct" ? "RTSTRUCT" : "Auto")}
                  </span>
                )}
              </div>

              {viewMode === "gamma" && (
                <div className="absolute bottom-3 right-3 bg-black/70 backdrop-blur-md px-2.5 py-1 rounded text-[10px] font-mono text-zinc-300 border border-white/10 flex items-center gap-2">
                  <span className="flex items-center gap-1">
                    <span className="w-2 h-2 rounded-full bg-emerald-400" /> &gamma; &le; 1
                  </span>
                  <span className="flex items-center gap-1">
                    <span className="w-2 h-2 rounded-full bg-red-400" /> &gamma; &gt; 1
                  </span>
                </div>
              )}
            </div>

            {/* Z-Slice Slider */}
            <div className="space-y-1.5 pt-2">
              <div className="flex items-center justify-between text-xs text-clinical-muted">
                <span>Axial Slice Navigation</span>
                <span className="font-mono font-medium text-clinical-text">
                  Slice {sliceZ + 1} of {currentTotalSlices || 1}
                </span>
              </div>
              <input
                type="range"
                min="0"
                max={Math.max(0, currentTotalSlices - 1)}
                value={sliceZ}
                onChange={(e) => setSliceZ(parseInt(e.target.value, 10))}
                className="w-full accent-blue-500 cursor-pointer"
              />
            </div>
          </div>
        </div>

        {/* Deformed Target Coverage & Adaptive DVH Card */}
        <DeformedDVHCard
          planId={planId}
          fractionNumber={selectedFraction}
          hasDose={detail.has_dose}
          hasDvh={detail.has_dvh}
        />
      </div>
    )}

      {/* Upload CBCT Modal */}
      {showUploadModal && (
        <div className="fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4">
          <div className="bg-clinical-surface border border-clinical-border rounded-xl max-w-lg w-full p-6 shadow-xl space-y-5">
            <div className="flex items-center justify-between pb-3 border-b border-clinical-border">
              <div className="flex items-center gap-2">
                <Upload size={18} className="text-blue-400" />
                <h3 className="text-base font-bold text-clinical-text">
                  Import Daily CBCT &amp; Generate Synthetic CT
                </h3>
              </div>
              <button
                onClick={() => setShowUploadModal(false)}
                className="text-clinical-muted hover:text-clinical-text"
              >
                <X size={18} />
              </button>
            </div>

            <form onSubmit={handleUploadSubmit} className="space-y-4">
              <div>
                <label className="text-xs font-semibold text-clinical-muted block mb-1.5">
                  Treatment Fraction Number
                </label>
                <input
                  type="number"
                  min="1"
                  max="40"
                  value={uploadFraction}
                  onChange={(e) => setUploadFraction(parseInt(e.target.value, 10) || 1)}
                  className="w-full bg-clinical-card border border-clinical-border rounded-lg px-3 py-2 text-sm text-clinical-text focus:outline-none focus:border-blue-500"
                  required
                />
                <span className="text-[11px] text-clinical-muted mt-1 block">
                  Assign this daily CBCT scan to a treatment fraction in the course.
                </span>
              </div>

              <div>
                <label className="text-xs font-semibold text-clinical-muted block mb-1.5">
                  Select Daily CBCT DICOM Slices or .ZIP Archive
                </label>
                <input
                  type="file"
                  multiple
                  accept=".dcm,.zip"
                  onChange={(e) => {
                    if (e.target.files) {
                      setUploadFiles(Array.from(e.target.files));
                    }
                  }}
                  className="w-full text-xs text-clinical-muted file:mr-3 file:py-2 file:px-3 file:rounded-lg file:border-0 file:text-xs file:font-semibold file:bg-blue-600 file:text-white hover:file:bg-blue-500 cursor-pointer"
                  required
                />
                {uploadFiles.length > 0 && (
                  <p className="text-xs text-emerald-400 font-medium mt-1.5">
                    {uploadFiles.length} file(s) selected
                  </p>
                )}
              </div>

              <div>
                <label className="text-xs font-semibold text-clinical-muted block mb-1.5">
                  Deformable Registration (DIR) Method
                </label>
                <select
                  value={dirMethod}
                  onChange={(e) => setDirMethod(e.target.value)}
                  className="w-full bg-clinical-card border border-clinical-border rounded-lg px-3 py-2 text-xs text-clinical-text focus:outline-none focus:border-blue-500"
                >
                  <option value="demons">Multiscale Diffeomorphic Demons (Fast &amp; Robust)</option>
                  <option value="bspline_lcc">B-spline Local Correlation / ANTS LCC (Scatter-Robust)</option>
                </select>
                <span className="text-[11px] text-clinical-muted mt-1 block">
                  Demons deforms planning CT HU to CBCT anatomy. B-spline LCC provides extra invariance if high streak/cupping artifacts exist.
                </span>
              </div>

              <div className="space-y-2 pt-1">
                <div className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    id="autoGenerate"
                    checked={autoGenerate}
                    onChange={(e) => setAutoGenerate(e.target.checked)}
                    className="rounded border-clinical-border text-blue-600 accent-blue-500"
                  />
                  <label htmlFor="autoGenerate" className="text-xs text-clinical-text font-medium cursor-pointer">
                    Automatically generate Synthetic CT after CBCT upload
                  </label>
                </div>

                <div className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    id="autoCalc"
                    checked={autoCalc}
                    onChange={(e) => setAutoCalc(e.target.checked)}
                    disabled={!autoGenerate}
                    className="rounded border-clinical-border text-blue-600 accent-blue-500 disabled:opacity-50"
                  />
                  <label htmlFor="autoCalc" className="text-xs text-clinical-text font-medium cursor-pointer">
                    Automatically calculate openMCsquare dose &amp; 3D Gamma
                  </label>
                </div>
              </div>

              {uploadError && (
                <div className="p-3 bg-red-500/10 border border-red-500/30 rounded-lg text-xs text-red-400 flex items-start gap-2">
                  <AlertTriangle size={14} className="mt-0.5 shrink-0" />
                  <span>{uploadError}</span>
                </div>
              )}

              <div className="flex items-center justify-end gap-2 pt-3 border-t border-clinical-border">
                <button
                  type="button"
                  onClick={() => setShowUploadModal(false)}
                  className="px-4 py-2 bg-clinical-card border border-clinical-border text-clinical-text text-xs font-semibold rounded-lg hover:bg-clinical-surface"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={uploading}
                  className="px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white text-xs font-semibold rounded-lg flex items-center gap-1.5 disabled:opacity-50"
                >
                  {uploading ? (
                    <>
                      <RefreshCw size={13} className="animate-spin" />
                      Uploading &amp; Processing...
                    </>
                  ) : (
                    <>
                      <Upload size={13} />
                      Import CBCT &amp; Run
                    </>
                  )}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
};
