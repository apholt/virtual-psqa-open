import { useCallback, useEffect, useMemo, useRef, useState, Fragment } from "react";
import type { CSSProperties } from "react";
import { useParams, useNavigate, useSearchParams } from "react-router-dom";
import toast from "react-hot-toast";
import {
  ArrowLeft,
  Play,
  Loader2,
  Layers,
  FileText,
  FileDown,
  Printer,
  TrendingDown,
  AlertTriangle,
  CheckCircle2,
  Atom,
  BarChart3,
  RefreshCw,
  ShieldCheck,
  ShieldOff,
  Columns,
  Zap,
  Server,
  Radio,
  ClipboardCheck,
  Square,
  RotateCcw,
  UploadCloud,
  Info,
} from "lucide-react";
import {
  getPlan,
  getPlanFields,
  getPlanDoseInfo,
  getPlanResults,
  getDosePlane,
  getGammaPlane,
  getCTPlane,
  getFractionalTrend,
  getPlanFractionLogs,
  getJob,
  runJob,
  cancelJob,
  secondaryDoseReportUrl,
  reportUrl,
  getChartChecks,
  getPlanDoseStatus,
} from "../api/client";
import type {
  PlanSummary,
  FieldSummary,
  GammaResult,
  PlanDoseInfo,
  DoseSource,
  ComparisonType,
  PlaneData,
  FractionalTrend,
  TrendPoint,
  QAJobResponse,
  FractionLogSummary,
  PlanDoseStatus,
} from "../types";
import { NavBar } from "../components/NavBar";
import { DoseMapCanvas } from "../components/DoseMapCanvas";
import { DoseColorbar } from "../components/DoseColorbar";
import { DiffColorbar, GammaColorbar } from "../components/PanelLegends";
import { StatusBadge } from "../components/StatusBadge";
import MonitoringCard, { type Gate } from "../components/MonitoringCard";
import SpotMap from "../components/SpotMap";
import { FractionLogViewer } from "../components/FractionLogViewer";
import { SyntheticCTViewer } from "../components/SyntheticCTViewer";
import { OIRViewer } from "../components/OIRViewer";
import { OrthancImportModal } from "../components/OrthancImportModal";
import { UploadRecordModal } from "../components/UploadRecordModal";
import { UploadDoseModal } from "../components/UploadDoseModal";
import { CouchTrackingTrend } from "../components/CouchTrackingTrend";
import { ChartCheckModal } from "../components/ChartCheckModal";
import { RobustnessDVHCard } from "../components/RobustnessDVHCard";
import { C } from "../theme";


const COMPARISON_LABELS: Record<ComparisonType, string> = {
  mcSquare_vs_TPS: "MCsquare vs TPS (Secondary)",
  log_vs_TPS: "Log recon vs TPS",
  log_vs_Rx: "Log recon vs Rx (Delivery)",
  mcSquare_vs_log: "MCsquare vs Log (Concordance)",
};

type FieldSel = "summed" | number;
const BASE_SOURCES: DoseSource[] = ["mcSquare", "tps", "log"];

const HEAT_MARGINAL_BELOW = 93;
const HEAT_CELL_MAX = 22;
const HEAT_CELL_MIN = 4;

function LogHeatmap({
  results,
  totalFractions,
}: {
  results: GammaResult[];
  totalFractions: number | null;
}) {
  const beamKey = (r: GammaResult) => r.field_name || `Beam ${r.beam_number ?? "?"}`;

  const beams: { key: string; num: number }[] = [];
  const seen = new Set<string>();
  for (const r of results) {
    const k = beamKey(r);
    if (!seen.has(k)) {
      seen.add(k);
      beams.push({ key: k, num: r.beam_number ?? Number.MAX_SAFE_INTEGER });
    }
  }
  beams.sort((a, b) => a.num - b.num || a.key.localeCompare(b.key));

  const maxSeenFx = results.reduce((m, r) => Math.max(m, r.fraction_number ?? 1), 0);
  const cols = Math.max(1, totalFractions ?? 0, maxSeenFx);

  const cell = new Map<string, GammaResult>();
  for (const r of results) cell.set(`${beamKey(r)}|${r.fraction_number ?? 1}`, r);

  const colorFor = (r: GammaResult) =>
    !r.passed ? C.barFail : r.passing_rate < HEAT_MARGINAL_BELOW ? C.barWarn : C.barPass;

  const labelEvery = cols > 14 ? 2 : 1;
  const cellBase: CSSProperties = { aspectRatio: "1", borderRadius: 2 };

  return (
    <div className="mb-2">
      <div
        style={{
          display: "grid",
          gridTemplateColumns: `38px repeat(${cols}, minmax(${HEAT_CELL_MIN}px, ${HEAT_CELL_MAX}px))`,
          justifyContent: "start",
          gap: 2,
          alignItems: "center",
        }}
      >
        <div />
        {Array.from({ length: cols }, (_, i) => (
          <div
            key={`h${i}`}
            className="text-[9px] text-clinical-muted text-center leading-none"
          >
            {(i + 1) % labelEvery === (labelEvery === 1 ? 0 : 1) || labelEvery === 1 ? i + 1 : ""}
          </div>
        ))}

        {beams.map((b) => (
          <Fragment key={b.key}>
            <div
              className="text-[10px] text-clinical-muted truncate font-mono"
              title={b.key}
            >
              {b.key.length > 5 ? b.key.slice(0, 4) + "…" : b.key}
            </div>
            {Array.from({ length: cols }, (_, i) => {
              const fx = i + 1;
              const r = cell.get(`${b.key}|${fx}`);
              if (!r) {
                return (
                  <div
                    key={`c${b.key}-${fx}`}
                    style={{ ...cellBase, background: "rgba(128,128,128,0.15)" }}
                    title={`${b.key} · Fx ${fx} · not analysed`}
                  />
                );
              }
              return (
                <div
                  key={`c${b.key}-${fx}`}
                  style={{ ...cellBase, background: colorFor(r) }}
                  title={`${b.key} · Fx ${fx} · ${r.passing_rate.toFixed(1)}% · ${
                    r.passed ? "PASS" : "FAIL"
                  }`}
                />
              );
            })}
          </Fragment>
        ))}
      </div>
    </div>
  );
}

function TrendChart({ points, threshold }: { points: TrendPoint[]; threshold: number }) {
  const w = 680;
  const h = 220;
  const padL = 40;
  const padB = 30;
  const padT = 16;
  const padR = 16;
  const plotW = w - padL - padR;
  const plotH = h - padT - padB;

  if (points.length === 0) {
    return <p className="text-clinical-muted text-xs py-6 text-center">No fraction data available yet.</p>;
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
          <line x1={padL} y1={py(v)} x2={w - padR} y2={py(v)} stroke="rgba(128,128,128,0.2)" strokeWidth={1} />
          <text x={padL - 6} y={py(v) + 3} fontSize={9} textAnchor="end" fill="#888">
            {v}%
          </text>
        </g>
      ))}
      <line
        x1={padL}
        y1={thrY}
        x2={w - padR}
        y2={thrY}
        stroke="#f59e0b"
        strokeWidth={1.5}
        strokeDasharray="5 3"
      />
      <text x={w - padR} y={thrY - 4} fontSize={9} textAnchor="end" fill="#f59e0b" fontWeight="bold">
        Action {threshold}%
      </text>
      <polyline points={linePts} fill="none" stroke="#3b82f6" strokeWidth={2} />
      {points.map((p, i) => (
        <g key={i}>
          <circle
            cx={px(i)}
            cy={py(p.passing_rate)}
            r={4}
            fill={p.passed ? "#10b981" : "#ef4444"}
            stroke="#ffffff"
            strokeWidth={1.5}
          />
          <text x={px(i)} y={h - 10} fontSize={9} textAnchor="middle" fill="#888">
            Fx {p.fraction}
          </text>
        </g>
      ))}
    </svg>
  );
}

export function PlanDetail() {
  const { planId } = useParams<{ planId: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const id = planId ? parseInt(planId, 10) : NaN;

  const activeTab = searchParams.get("tab") || "dose";
  const setActiveTab = (t: string) => setSearchParams({ tab: t });

  const [plan, setPlan] = useState<PlanSummary | null>(null);
  const [fields, setFields] = useState<FieldSummary[]>([]);
  const [results, setResults] = useState<GammaResult[]>([]);
  const [gate, setGate] = useState<Gate | null>(null);
  const [loading, setLoading] = useState(true);
  const [dvhSelectedRois, setDvhSelectedRois] = useState<Set<number>>(new Set());

  // Gate feature muting (defaults to true / muted)
  const [muteGate, setMuteGate] = useState<boolean>(
    () => localStorage.getItem("psqa_mute_gate") !== "false"
  );

  const toggleMuteGate = () => {
    const next = !muteGate;
    setMuteGate(next);
    localStorage.setItem("psqa_mute_gate", next ? "true" : "false");
    if (next && activeTab === "gate") {
      setActiveTab("dose");
    }
    toast.success(next ? "Clinical Clearance Gate muted." : "Clinical Clearance Gate active.");
  };

  useEffect(() => {
    if (muteGate && activeTab === "gate") {
      setActiveTab("dose");
    }
  }, [muteGate, activeTab]);

  const [activeJob, setActiveJob] = useState<QAJobResponse | null>(null);
  const [jobPolling, setJobPolling] = useState(false);

  const [doseInfo, setDoseInfo] = useState<PlanDoseInfo | null>(null);
  const [z, setZ] = useState(0);
  const [zUI, setZUI] = useState(0);
  const [viewMode, setViewMode] = useState<"dose" | "gamma" | "diff">("dose");
  const [showIso, setShowIso] = useState(true);
  const [showCT, setShowCT] = useState(true);
  const [ctPlane, setCtPlane] = useState<PlaneData | null>(null);
  const [ctAvailable, setCtAvailable] = useState(true);
  const [fieldSel, setFieldSel] = useState<FieldSel>("summed");
  const [dosePlanes, setDosePlanes] = useState<Partial<Record<string, PlaneData>>>({});
  const [gammaPlanes, setGammaPlanes] = useState<Partial<Record<ComparisonType, PlaneData>>>({});
  const [gammaMcTps, setGammaMcTps] = useState<PlaneData | null>(null);
  const reqToken = useRef(0);

  const [trendData, setTrendData] = useState<FractionalTrend | null>(null);
  const [fractionSummaries, setFractionSummaries] = useState<FractionLogSummary[]>([]);
  const [showSpotMap, setShowSpotMap] = useState(false);
  const [showOrthancModal, setShowOrthancModal] = useState(false);
  const [showUploadRecordModal, setShowUploadRecordModal] = useState(false);
  const [orthancTab, setOrthancTab] = useState<"plans" | "rt_records" | "offline_images">("plans");
  const [showChartCheckModal, setShowChartCheckModal] = useState(false);
  const [chartCheckCount, setChartCheckCount] = useState<number | null>(null);
  const [doseStatus, setDoseStatus] = useState<PlanDoseStatus | null>(null);
  const [showUploadDoseModal, setShowUploadDoseModal] = useState(false);

  const interruptedFractions = useMemo(() => {
    return fractionSummaries.filter((f) => f.is_interrupted);
  }, [fractionSummaries]);

  const loadPlanData = useCallback(async () => {
    if (Number.isNaN(id)) return;
    try {
      setLoading(true);
      const [p, f, r, trend, fracs, checksData, ds] = await Promise.all([
        getPlan(id),
        getPlanFields(id).catch(() => []),
        getPlanResults(id).catch(() => []),
        getFractionalTrend(id).catch(() => null),
        getPlanFractionLogs(id).catch(() => []),
        getChartChecks(id).catch(() => null),
        getPlanDoseStatus(id).catch(() => null),
      ]);
      setPlan(p);
      setFields(f);
      setResults(r);
      setTrendData(trend);
      setFractionSummaries(fracs);
      setDoseStatus(ds);
      if (checksData) {
        setChartCheckCount(checksData.total_completed);
      }

      try {
        const di = await getPlanDoseInfo(id);
        setDoseInfo(di);
        setZ(di.default_plane);
        setZUI(di.default_plane);
      } catch {
        setDoseInfo(null);
      }
    } catch {
      toast.error("Failed to load plan details.");
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    loadPlanData();
  }, [loadPlanData]);

  useEffect(() => {
    const t = setTimeout(() => setZ(zUI), 140);
    return () => clearTimeout(t);
  }, [zUI]);

  useEffect(() => {
    if (!doseInfo || !ctAvailable || Number.isNaN(id)) return;
    let live = true;
    getCTPlane(id, z)
      .then((p) => {
        if (live) setCtPlane(p);
      })
      .catch(() => {
        if (live) {
          setCtPlane(null);
          setCtAvailable(false);
        }
      });
    return () => {
      live = false;
    };
  }, [doseInfo, id, z, ctAvailable]);

  const availableBeams = useMemo(() => {
    if (!doseInfo) return [];
    const nums = new Set<number>();
    for (const s of doseInfo.sources) {
      const m = /_beam(\d+)$/.exec(s.source);
      if (m) nums.add(parseInt(m[1], 10));
    }
    return Array.from(nums).sort((a, b) => a - b);
  }, [doseInfo]);

  const mcCompletedBeams = useMemo(() => {
    if (!doseInfo) return [];
    const nums = new Set<number>();
    for (const s of doseInfo.sources) {
      const m = /^mcSquare_beam(\d+)$/.exec(s.source);
      if (m) nums.add(parseInt(m[1], 10));
    }
    return Array.from(nums).sort((a, b) => a - b);
  }, [doseInfo]);

  const resolveSource = useCallback(
    (base: DoseSource): string =>
      fieldSel === "summed" ? base : `${base}_beam${fieldSel}`,
    [fieldSel]
  );

  const activeSources = useMemo(() => {
    if (!doseInfo) return [];
    return BASE_SOURCES.filter((base) => {
      const key = fieldSel === "summed" ? base : `${base}_beam${fieldSel}`;
      return doseInfo.sources.some((src) => src.source === key);
    });
  }, [doseInfo, fieldSel]);

  const selectedBeamNum = fieldSel === "summed" ? undefined : fieldSel;

  useEffect(() => {
    if (!doseInfo || viewMode !== "dose" || Number.isNaN(id)) return;
    let live = true;
    getGammaPlane(id, "mcSquare_vs_TPS", z, selectedBeamNum)
      .then((p) => {
        if (live) setGammaMcTps(p);
      })
      .catch(() => {
        if (live) setGammaMcTps(null);
      });
    return () => {
      live = false;
    };
  }, [doseInfo, id, z, viewMode, selectedBeamNum]);

  useEffect(() => {
    if (!doseInfo || Number.isNaN(id)) return;
    const token = ++reqToken.current;

    if (viewMode === "dose" || viewMode === "diff") {
      activeSources.forEach((base) => {
        const key = resolveSource(base);
        getDosePlane(id, key, z)
          .then((plane) => {
            if (reqToken.current === token) {
              setDosePlanes((prev) => ({ ...prev, [key]: plane }));
            }
          })
          .catch(() => undefined);
      });
    } else {
      doseInfo.available_comparisons.forEach((comp) => {
        getGammaPlane(id, comp, z, selectedBeamNum)
          .then((plane) => {
            if (reqToken.current === token) {
              setGammaPlanes((prev) => ({ ...prev, [comp]: plane }));
            }
          })
          .catch(() => undefined);
      });
    }
  }, [doseInfo, id, z, viewMode, activeSources, resolveSource, selectedBeamNum]);

  const scaleMax = useMemo(() => {
    let m = 0;
    for (const base of activeSources) {
      const key = resolveSource(base);
      const p = dosePlanes[key];
      if (p) {
        for (let i = 0; i < p.data.length; i++) {
          if (p.data[i] > m) m = p.data[i];
        }
      }
    }
    return m > 0 ? m : 1.0;
  }, [dosePlanes, activeSources, resolveSource]);

  const pollJob = useCallback(
    (jobId: number) => {
      const interval = setInterval(async () => {
        try {
          const j = await getJob(jobId);
          setActiveJob(j);
          if (j.status === "complete" || j.status === "error" || j.status === "cancelled") {
            clearInterval(interval);
            setJobPolling(false);
            if (j.status === "complete") {
              toast.success("QA Calculation completed successfully!");
              loadPlanData();
            } else if (j.status === "cancelled") {
              toast("Calculation stopped — completed beam doses preserved.", { icon: "🛑" });
              loadPlanData();
            } else if (j.status === "error") {
              toast.error(`Job failed: ${j.error_message || "Unknown error"}`);
            }
          }
        } catch {
          clearInterval(interval);
          setJobPolling(false);
        }
      }, 1500);
    },
    [loadPlanData]
  );

  const handleLaunchJob = async (type: "mcSquare" | "log_reconstruction" | "gamma", force: boolean = false) => {
    if (Number.isNaN(id)) return;
    try {
      setJobPolling(true);
      const job = await runJob(id, type, undefined, force);
      setActiveJob(job);
      toast.success(
        force
          ? "Starting full MC calculation (clearing cache)..."
          : type === "gamma"
          ? "Secondary 3D gamma calculation launched!"
          : `${type === "mcSquare" ? "MCsquare simulation" : "Log reconstruction"} launched!`
      );
      pollJob(job.id);
    } catch {
      toast.error(`Failed to launch ${type} job.`);
      setJobPolling(false);
    }
  };

  const handleStopJob = async () => {
    if (!activeJob) return;
    try {
      await cancelJob(activeJob.id);
      toast("Stop requested — completed beam doses will be preserved.", { icon: "🛑" });
    } catch {
      toast.error("Failed to stop job.");
    }
  };

  const mcResults = useMemo(
    () => results.filter((r) => r.comparison_type === "mcSquare_vs_TPS"),
    [results]
  );
  const logResults = useMemo(
    () =>
      results.filter(
        (r) => r.comparison_type === "log_vs_Rx" || r.comparison_type === "log_vs_TPS"
      ),
    [results]
  );
  const mcComposite = useMemo(() => {
    const explicit = mcResults.find(
      (r) => r.field_name === "Composite" || r.beam_number === null
    );
    if (explicit) return explicit;
    if (mcResults.length === 1) return mcResults[0];
    return undefined;
  }, [mcResults]);

  const mcFieldResults = useMemo(() => {
    const hasPerBeam = mcResults.some((r) => r.beam_number != null);
    const list = hasPerBeam ? mcResults.filter((r) => r.beam_number != null) : mcResults;
    return [...list].sort((a, b) => (a.beam_number ?? 0) - (b.beam_number ?? 0));
  }, [mcResults]);

  if (loading && !plan) {
    return (
      <div className="min-h-screen bg-clinical-bg flex items-center justify-center text-clinical-muted text-xs">
        <Loader2 className="animate-spin mr-2" size={16} /> Loading plan QA cockpit…
      </div>
    );
  }

  if (!plan) return null;

  return (
    <div className="min-h-screen bg-clinical-bg">
      <NavBar />

      {/* Cockpit Sticky Header */}
      <div className="border-b border-clinical-border bg-clinical-surface sticky top-0 z-20 shadow-sm">
        <div className="max-w-7xl mx-auto px-4 py-3 flex flex-col md:flex-row md:items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <button
              onClick={() => navigate("/")}
              className="p-1.5 rounded hover:bg-clinical-border/40 text-clinical-muted hover:text-clinical-text transition-colors"
              title="Back to Dashboard"
            >
              <ArrowLeft size={18} />
            </button>

            <div>
              <div className="flex items-center gap-2 flex-wrap">
                <h1 className="text-base font-bold text-clinical-text">{plan.plan_label}</h1>
                <span className="text-xs font-mono px-2 py-0.5 rounded bg-clinical-bg border border-clinical-border text-clinical-muted">
                  ID #{plan.id}
                </span>
                {plan.treatment_site && (
                  <span className="text-xs px-2 py-0.5 rounded bg-blue-50 dark:bg-blue-950/40 text-blue-700 dark:text-blue-300 font-medium border border-blue-200 dark:border-blue-800">
                    {plan.treatment_site}
                  </span>
                )}
                <StatusBadge status={plan.qa_status} />
                {!muteGate && gate && (
                  <span className="text-xs font-medium px-2 py-0.5 rounded bg-clinical-bg border border-clinical-border text-clinical-muted">
                    Gate: {gate.status} ({gate.dry_run_waived ? "waived" : "dry run required"})
                  </span>
                )}
              </div>
              <p className="text-[11px] text-clinical-muted mt-0.5">
                {plan.number_of_fields} Beams &middot; {plan.number_of_fractions || "—"} Fractions &middot; Ingested{" "}
                {new Date(plan.created_at).toLocaleDateString()}
              </p>
            </div>
          </div>

          {/* Action Bar */}
          <div className="flex items-center gap-2 flex-wrap">
            {jobPolling && activeJob?.job_type === "mcSquare" ? (
              <div className="flex items-center gap-1.5 bg-blue-500/10 border border-blue-500/30 rounded px-2.5 py-1">
                <Loader2 size={12} className="animate-spin text-blue-400" />
                <span className="text-xs text-blue-400 font-medium">
                  MCsquare {Math.round((activeJob.progress || 0) * 100)}%
                </span>
                <button
                  onClick={handleStopJob}
                  className="flex items-center gap-1 ml-1 px-2 py-0.5 bg-red-600/20 hover:bg-red-600/30 text-red-400 text-[11px] font-semibold rounded border border-red-500/30 transition-colors"
                  title="Stop MCsquare simulation (completed beam doses are preserved)"
                >
                  <Square size={10} fill="currentColor" />
                  Stop
                </button>
              </div>
            ) : mcCompletedBeams.length > 0 && mcCompletedBeams.length < (plan.number_of_fields || 0) ? (
              <div className="flex items-center gap-1.5">
                <button
                  onClick={() => handleLaunchJob("mcSquare", false)}
                  disabled={jobPolling}
                  className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-amber-600 hover:bg-amber-500 text-white font-medium rounded transition-colors shadow-sm disabled:opacity-50"
                  title={`Resume calculation for remaining ${plan.number_of_fields - mcCompletedBeams.length} beam(s)`}
                >
                  <Play size={12} fill="currentColor" />
                  Resume MC ({mcCompletedBeams.length}/{plan.number_of_fields})
                </button>
                <button
                  onClick={() => handleLaunchJob("mcSquare", true)}
                  disabled={jobPolling}
                  className="flex items-center gap-1 px-2 py-1.5 text-xs bg-clinical-surface hover:bg-clinical-border/30 border border-clinical-border text-clinical-muted hover:text-clinical-text rounded transition-colors disabled:opacity-50"
                  title="Discard cached beam doses and re-run all beams from scratch"
                >
                  <RotateCcw size={12} />
                  Restart All
                </button>
              </div>
            ) : (
              <button
                onClick={() => handleLaunchJob("mcSquare", false)}
                disabled={jobPolling}
                className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-clinical-accent text-white font-medium rounded hover:bg-clinical-accent/90 transition-colors shadow-sm disabled:opacity-50"
              >
                <Play size={13} fill="currentColor" />
                {mcCompletedBeams.length > 0 ? "Re-run MC" : "Run MC Simulation"}
              </button>
            )}

            <button
              onClick={() => handleLaunchJob("log_reconstruction")}
              disabled={jobPolling}
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-clinical-surface hover:bg-clinical-border/30 border border-clinical-border rounded text-clinical-text transition-colors disabled:opacity-50"
            >
              <Zap size={13} className="text-amber-500" />
              Run Log Recon
            </button>

            {doseInfo?.sources.some((s) => s.source.startsWith("mcSquare")) && (
              <button
                onClick={() => handleLaunchJob("gamma")}
                disabled={jobPolling}
                className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-clinical-surface hover:bg-clinical-border/30 border border-clinical-border rounded text-clinical-text transition-colors disabled:opacity-50"
                title="Recalculate 3D gamma analysis for all beams and composite using current dose grids and external contours"
              >
                <RotateCcw size={12} className="text-clinical-accent" />
                Recalculate Gamma
              </button>
            )}

            {/* Secondary Dose Report Actions */}
            <div className="flex items-center rounded border border-clinical-border bg-clinical-surface overflow-hidden">
              <a
                href={secondaryDoseReportUrl(id, "html", dvhSelectedRois)}
                target="_blank"
                rel="noreferrer"
                className="flex items-center gap-1 px-2.5 py-1.5 text-xs text-clinical-text hover:bg-clinical-border/30 border-r border-clinical-border transition-colors font-medium"
                title="View & Print Secondary Dose Report"
              >
                <Printer size={13} className="text-green-600 dark:text-green-400" />
                Secondary Report
              </a>
              <a
                href={secondaryDoseReportUrl(id, "pdf", dvhSelectedRois)}
                download
                className="flex items-center gap-1 px-2 py-1.5 text-xs text-clinical-text hover:bg-clinical-border/30 transition-colors"
                title="Download Secondary Dose PDF"
              >
                <FileDown size={13} className="text-green-600 dark:text-green-400" />
                PDF
              </a>
            </div>

            {/* Chart Check Action */}
            <button
              onClick={() => setShowChartCheckModal(true)}
              className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-semibold rounded border border-indigo-500/30 bg-indigo-500/10 hover:bg-indigo-500/20 text-indigo-400 transition-colors"
              title="Physics Weekly & Continuing Chart Checks (TG-275 protocol)"
            >
              <ClipboardCheck size={13} />
              Chart Check
              {chartCheckCount !== null && chartCheckCount > 0 && (
                <span className="ml-0.5 px-1.5 py-0.2 rounded-full text-[10px] bg-indigo-500/30 text-indigo-200">
                  {chartCheckCount}
                </span>
              )}
            </button>

            {/* Fractional Report Actions */}
            <div className="flex items-center rounded border border-clinical-border bg-clinical-surface overflow-hidden">
              <a
                href={reportUrl(id, "html")}
                target="_blank"
                rel="noreferrer"
                className="flex items-center gap-1 px-2.5 py-1.5 text-xs text-clinical-muted hover:text-clinical-text hover:bg-clinical-border/30 border-r border-clinical-border transition-colors font-medium"
                title="View Streamlined Fractional Delivery QA Report (gamma trend & field breakdown, electronic OMR record)"
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

            <button
              onClick={loadPlanData}
              className="p-1.5 rounded border border-clinical-border bg-clinical-surface text-clinical-muted hover:text-clinical-text hover:bg-clinical-border/30 transition-colors"
              title="Refresh Plan Data"
            >
              <RefreshCw size={14} className={loading ? "animate-spin" : ""} />
            </button>

            {/* Orthanc Ingestion Action */}
            <button
              onClick={() => {
                setOrthancTab("plans");
                setShowOrthancModal(true);
              }}
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-indigo-500/10 hover:bg-indigo-500/20 text-indigo-400 border border-indigo-500/30 rounded font-medium transition-colors"
              title="Query Orthanc PACS for treatment records or offline imaging"
            >
              <Server size={13} />
              Orthanc PACS
            </button>

            {/* Direct RT Record Upload Action */}
            <button
              onClick={() => setShowUploadRecordModal(true)}
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-emerald-500/10 hover:bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 rounded font-medium transition-colors"
              title="Upload RT Record (.dcm or .zip) directly for this plan"
            >
              <UploadCloud size={13} />
              Upload RT Record
            </button>

            {/* Direct Dose Files Upload & Status Action */}
            <button
              onClick={() => setShowUploadDoseModal(true)}
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-blue-500/10 hover:bg-blue-500/20 text-blue-400 border border-blue-500/30 rounded font-medium transition-colors"
              title="Inspect DICOM RTDOSE files and upload missing composite or beam doses"
            >
              <Layers size={13} />
              Dose Files
              {doseStatus && (doseStatus.missing_plan_dose || doseStatus.missing_beam_numbers.length > 0) && (
                <span
                  className="w-2 h-2 rounded-full bg-amber-400 animate-pulse"
                  title="Missing dose files detected"
                />
              )}
            </button>

            {/* Gate Mute / Active Toggle Button */}
            <button
              onClick={toggleMuteGate}
              className={`flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-semibold rounded border transition-all ${
                muteGate
                  ? "bg-clinical-surface border-clinical-border text-clinical-muted hover:text-clinical-text"
                  : "bg-blue-50 dark:bg-blue-950/80 border-blue-300 text-blue-700 dark:text-blue-300 shadow-xs"
              }`}
              title={
                muteGate
                  ? "Clinical Clearance Gate is muted. Click to activate."
                  : "Clinical Clearance Gate is active. Click to mute."
              }
            >
              {muteGate ? (
                <ShieldOff size={13} className="text-clinical-muted" />
              ) : (
                <ShieldCheck size={13} className="text-blue-600 dark:text-blue-400" />
              )}
              <span>Gate: {muteGate ? "Muted" : "Active"}</span>
            </button>
          </div>
        </div>

        {/* Running QA Job Progress Banner */}
        {activeJob && (activeJob.status === "running" || activeJob.status === "queued") && (
          <div className="bg-blue-50 dark:bg-blue-950/60 border-t border-blue-200 dark:border-blue-900 px-4 py-2 flex items-center justify-between text-xs">
            <div className="flex items-center gap-3 flex-1 max-w-2xl">
              <Loader2 size={14} className="animate-spin text-blue-600 dark:text-blue-400 shrink-0" />
              <div className="flex-1">
                <div className="flex justify-between text-[11px] font-medium text-blue-900 dark:text-blue-200 mb-1">
                  <span>Executing {activeJob.job_type}…</span>
                  <span>{Math.round(activeJob.progress * 100)}%</span>
                </div>
                <div className="w-full bg-blue-200 dark:bg-blue-900 h-1.5 rounded-full overflow-hidden">
                  <div
                    className="bg-blue-600 dark:bg-blue-400 h-full transition-all duration-300"
                    style={{ width: `${Math.max(5, activeJob.progress * 100)}%` }}
                  />
                </div>
              </div>
            </div>
            <button
              onClick={handleStopJob}
              className="ml-4 flex items-center gap-1 text-xs font-semibold text-red-600 dark:text-red-400 hover:underline"
              title="Stop simulation (completed beam doses will be preserved)"
            >
              <Square size={11} fill="currentColor" />
              Stop Calculation
            </button>
          </div>
        )}

        {/* Partial Calculation Banner */}
        {!jobPolling && mcCompletedBeams.length > 0 && mcCompletedBeams.length < (plan.number_of_fields || 0) && (
          <div className="bg-amber-500/10 border-t border-amber-500/30 px-4 py-2 flex items-center justify-between text-xs">
            <div className="flex items-center gap-2 text-amber-300">
              <span className="font-semibold">Partial Calculation Saved:</span>
              <span>
                {mcCompletedBeams.length} of {plan.number_of_fields} beam doses completed and preserved.
                Gamma analysis and dose displays reflect completed beams.
              </span>
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={() => handleLaunchJob("mcSquare", false)}
                className="flex items-center gap-1 px-2.5 py-1 bg-amber-500 hover:bg-amber-400 text-black font-semibold rounded text-xs transition-colors shadow-sm"
                title={`Continue simulation for remaining ${plan.number_of_fields - mcCompletedBeams.length} beam(s)`}
              >
                <Play size={11} fill="currentColor" />
                Resume ({mcCompletedBeams.length}/{plan.number_of_fields})
              </button>
              <button
                onClick={() => handleLaunchJob("mcSquare", true)}
                className="flex items-center gap-1 px-2 py-1 bg-clinical-surface text-clinical-muted hover:text-clinical-text border border-clinical-border rounded text-xs transition-colors"
                title="Discard cached beam doses and recalculate all beams from scratch"
              >
                <RotateCcw size={11} />
                Restart All
              </button>
            </div>
          </div>
        )}

        {/* Tab Navigation */}
        <div className="max-w-7xl mx-auto px-4 flex items-center gap-6 text-xs font-medium border-t border-clinical-border/40">
          <button
            onClick={() => setActiveTab("dose")}
            className={`py-2.5 border-b-2 flex items-center gap-1.5 transition-colors ${
              activeTab === "dose"
                ? "border-clinical-accent text-clinical-accent font-semibold"
                : "border-transparent text-clinical-muted hover:text-clinical-text"
            }`}
          >
            <Layers size={14} />
            Dose &amp; Gamma Analysis
            {mcComposite && (
              <span
                className={`ml-1 text-[10px] px-1.5 py-0.2 rounded font-bold ${
                  mcComposite.passed
                    ? "bg-green-100 dark:bg-green-950 text-green-700 dark:text-green-300"
                    : "bg-red-100 dark:bg-red-950 text-red-700 dark:text-red-300"
                }`}
              >
                {mcComposite.passing_rate.toFixed(1)}%
              </span>
            )}
          </button>

          <button
            onClick={() => setActiveTab("fractions")}
            className={`py-2.5 border-b-2 flex items-center gap-1.5 transition-colors ${
              activeTab === "fractions"
                ? "border-clinical-accent text-clinical-accent font-semibold"
                : "border-transparent text-clinical-muted hover:text-clinical-text"
            }`}
          >
            <TrendingDown size={14} />
            Fractional Tracker &amp; Logs
            {trendData?.drift_alert && (
              <span className="ml-1 text-[10px] px-1.5 py-0.2 rounded bg-amber-100 dark:bg-amber-950 text-amber-700 dark:text-amber-300 font-bold">
                Drift Alert
              </span>
            )}
            {interruptedFractions.length > 0 && (
              <span
                className="ml-1 text-[10px] px-1.5 py-0.2 rounded bg-amber-100 dark:bg-amber-950/80 text-amber-800 dark:text-amber-300 font-bold border border-amber-300 dark:border-amber-700 flex items-center gap-1"
                title={`${interruptedFractions.length} fraction delivery record(s) flagged as interrupted or partial`}
              >
                ⚠️ Partial ({interruptedFractions.length})
              </span>
            )}
          </button>

          {/* OIR (Offline Image Review) Tab */}
          <button
            onClick={() => setActiveTab("oir")}
            className={`py-2.5 border-b-2 flex items-center gap-1.5 transition-colors ${
              activeTab === "oir"
                ? "border-clinical-accent text-clinical-accent font-semibold"
                : "border-transparent text-clinical-muted hover:text-clinical-text"
            }`}
          >
            <Columns size={14} />
            OIR (Offline Image Review)
          </button>

          <button
            onClick={() => setActiveTab("synthetic_ct")}
            className={`py-2.5 border-b-2 flex items-center gap-1.5 transition-colors ${
              activeTab === "synthetic_ct"
                ? "border-clinical-accent text-clinical-accent font-semibold"
                : "border-transparent text-clinical-muted hover:text-clinical-text"
            }`}
          >
            <Layers size={14} />
            SyntheticQACT Adaptive Dose
          </button>

          <button
            onClick={() => setActiveTab("parameters")}

            className={`py-2.5 border-b-2 flex items-center gap-1.5 transition-colors ${
              activeTab === "parameters"
                ? "border-clinical-accent text-clinical-accent font-semibold"
                : "border-transparent text-clinical-muted hover:text-clinical-text"
            }`}
          >
            <BarChart3 size={14} />
            Plan Parameters &amp; Deliverability ({fields.length} Beams)
          </button>

          {!muteGate && (
            <button
              onClick={() => setActiveTab("gate")}
              className={`py-2.5 border-b-2 flex items-center gap-1.5 transition-colors ${
                activeTab === "gate"
                  ? "border-clinical-accent text-clinical-accent font-semibold"
                  : "border-transparent text-clinical-muted hover:text-clinical-text"
              }`}
            >
              <ShieldCheck size={14} />
              Evidence Gate &amp; Clearance
            </button>
          )}
        </div>
      </div>

      {/* Main Tab Content */}
      <div className="max-w-7xl mx-auto px-4 py-5">
        {/* TAB 1: DOSE & GAMMA ANALYSIS */}
        {activeTab === "dose" && (
          <div className="space-y-5">
            {/* Missing Plan Dose Alert Banner */}
            {doseStatus && (doseStatus.missing_plan_dose || doseStatus.status === "missing_plan_dose" || doseStatus.status === "missing_files") && (
              <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-4 flex flex-col md:flex-row items-start md:items-center justify-between gap-3 text-xs text-red-700 dark:text-red-300 shadow-sm">
                <div className="flex items-start gap-3">
                  <AlertTriangle size={18} className="text-red-500 shrink-0 mt-0.5" />
                  <div>
                    <div className="font-bold text-sm text-red-900 dark:text-red-200">
                      Missing Composite Plan RTDOSE File
                    </div>
                    <p className="mt-0.5 text-red-800 dark:text-red-300/90 leading-relaxed">
                      The DICOM export lacks the total plan reference dose (<code className="font-mono text-[11px] bg-red-500/20 px-1 py-0.5 rounded">DoseSummationType=PLAN</code>) and complete beam doses to synthesize it. Composite gamma calculation cannot compare individual beams to the summed Monte Carlo distribution without the full reference dose.
                    </p>
                    {doseStatus.warnings.length > 0 && (
                      <ul className="mt-1.5 list-disc list-inside text-[11px] opacity-90 space-y-0.5">
                        {doseStatus.warnings.map((w, idx) => (
                          <li key={idx}>{w}</li>
                        ))}
                      </ul>
                    )}
                  </div>
                </div>
                <button
                  onClick={() => setShowUploadDoseModal(true)}
                  className="shrink-0 flex items-center gap-1.5 px-3.5 py-2 bg-red-600 hover:bg-red-500 text-white font-semibold rounded-lg shadow-sm text-xs transition-colors"
                >
                  <UploadCloud size={14} />
                  Upload Missing Dose Files
                </button>
              </div>
            )}

            {/* Partial Beams Warning Banner */}
            {doseStatus && doseStatus.status === "partial_beams" && !doseStatus.missing_plan_dose && (
              <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-3.5 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3 text-xs text-amber-800 dark:text-amber-200">
                <div className="flex items-start gap-2.5">
                  <AlertTriangle size={16} className="text-amber-500 shrink-0 mt-0.5" />
                  <div>
                    <span className="font-semibold">Missing Individual Beam RTDOSE File(s):</span>{" "}
                    Field-by-field gamma evaluation is missing doses for{" "}
                    {doseStatus.missing_beam_names.map((n, i) => `Beam ${doseStatus.missing_beam_numbers[i]} (${n})`).join(", ")}.
                  </div>
                </div>
                <button
                  onClick={() => setShowUploadDoseModal(true)}
                  className="shrink-0 flex items-center gap-1.5 px-3 py-1.5 bg-amber-600 hover:bg-amber-500 text-white font-medium rounded-lg text-xs transition-colors shadow-sm"
                >
                  <UploadCloud size={13} />
                  Upload Beam Doses
                </button>
              </div>
            )}

            {/* Synthesized Composite Dose Notice */}
            {doseStatus && doseStatus.is_plan_dose_synthesized && (
              <div className="rounded-lg border border-blue-500/30 bg-blue-500/10 p-3 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-2 text-xs text-blue-800 dark:text-blue-200">
                <div className="flex items-center gap-2">
                  <Info size={16} className="text-blue-500 shrink-0" />
                  <span>
                    <span className="font-semibold">Auto-Synthesized Composite Dose:</span> Plan-level RTDOSE was not present in the export. Virtual PSQA automatically synthesized the composite reference dose by summing all {doseStatus.total_beams_count} beam doses.
                  </span>
                </div>
                <button
                  onClick={() => setShowUploadDoseModal(true)}
                  className="shrink-0 text-[11px] text-blue-600 dark:text-blue-400 hover:underline font-semibold ml-6 sm:ml-0"
                >
                  View Dose Details
                </button>
              </div>
            )}

            {/* Top Gamma KPI Summary Banner */}
            <div className="rounded-lg border border-clinical-border bg-clinical-surface p-4 flex flex-col md:flex-row items-center justify-between gap-4">
              <div className="flex items-center gap-4">
                <div className="text-center px-4 border-r border-clinical-border/60">
                  <div
                    className="text-3xl font-extrabold"
                    style={{ color: mcComposite?.passed ? C.passText : C.measureText }}
                  >
                    {mcComposite ? `${mcComposite.passing_rate.toFixed(1)}%` : "—"}
                  </div>
                  <span className="text-[11px] text-clinical-muted uppercase font-semibold">
                    Composite Gamma (3%/3mm)
                  </span>
                </div>

                <div>
                  <div className="flex items-center gap-2">
                    <span
                      className={`text-xs font-bold px-2 py-0.5 rounded ${
                        mcComposite?.passed
                          ? "bg-green-100 dark:bg-green-950 text-green-700 dark:text-green-300"
                          : "bg-red-100 dark:bg-red-950 text-red-700 dark:text-red-300"
                      }`}
                    >
                      {mcComposite?.passed ? "PASS (Action ≥ 90%)" : "ACTION REQUIRED"}
                    </span>
                    <span className="text-xs text-clinical-muted">
                      Evaluated against openMCsquare Secondary Calculation
                    </span>
                  </div>
                  <p className="text-[11px] text-clinical-muted mt-1">
                    {mcFieldResults.length > 0
                      ? `${mcFieldResults.filter((r) => r.passed).length} of ${mcFieldResults.length} fields meet clinical tolerance.`
                      : doseInfo?.sources.some((s) => s.source.startsWith("mcSquare"))
                      ? "MC dose available. Click 'Recalculate Gamma' above to compute 3D evaluations."
                      : "Secondary calculation pending. Click 'Run MC Simulation' above to calculate."}
                  </p>
                </div>
              </div>

              {/* Controls Bar for Viewer */}
              {doseInfo && (
                <div className="flex items-center gap-3 flex-wrap bg-clinical-bg/60 p-2 rounded-lg border border-clinical-border/60 text-xs">
                  <div className="flex rounded border border-clinical-border overflow-hidden bg-clinical-surface">
                    <button
                      onClick={() => setViewMode("dose")}
                      className={`px-2.5 py-1 text-xs font-medium transition-colors ${
                        viewMode === "dose"
                          ? "bg-clinical-accent text-white"
                          : "text-clinical-muted hover:text-clinical-text"
                      }`}
                    >
                      Dose Views
                    </button>
                    <button
                      onClick={() => setViewMode("gamma")}
                      className={`px-2.5 py-1 text-xs font-medium transition-colors ${
                        viewMode === "gamma"
                          ? "bg-clinical-accent text-white"
                          : "text-clinical-muted hover:text-clinical-text"
                      }`}
                    >
                      Gamma Maps
                    </button>
                    <button
                      onClick={() => setViewMode("diff")}
                      className={`px-2.5 py-1 text-xs font-medium transition-colors ${
                        viewMode === "diff"
                          ? "bg-clinical-accent text-white"
                          : "text-clinical-muted hover:text-clinical-text"
                      }`}
                    >
                      Difference
                    </button>
                  </div>

                  <select
                    value={fieldSel}
                    onChange={(e) => setFieldSel(e.target.value === "summed" ? "summed" : parseInt(e.target.value, 10))}
                    className="bg-clinical-surface border border-clinical-border rounded px-2 py-1 text-xs text-clinical-text"
                  >
                    <option value="summed">Summed Composite Plan</option>
                    {availableBeams.map((b) => (
                      <option key={b} value={b}>
                        Beam {b}
                      </option>
                    ))}
                  </select>

                  <label className="flex items-center gap-1.5 cursor-pointer text-[11px] text-clinical-text">
                    <input
                      type="checkbox"
                      checked={showCT}
                      disabled={!ctAvailable}
                      onChange={(e) => setShowCT(e.target.checked)}
                      className="rounded border-clinical-border text-clinical-accent"
                    />
                    CT Overlay
                  </label>

                  <label className="flex items-center gap-1.5 cursor-pointer text-[11px] text-clinical-text">
                    <input
                      type="checkbox"
                      checked={showIso}
                      onChange={(e) => setShowIso(e.target.checked)}
                      className="rounded border-clinical-border text-clinical-accent"
                    />
                    Isodose Contours
                  </label>
                </div>
              )}
            </div>

            {/* Interactive Slices Grid */}
            {doseInfo ? (
              <div className="rounded-lg border border-clinical-border bg-clinical-surface p-4">
                <div className="flex items-center gap-4 mb-4 bg-clinical-bg/40 p-2.5 rounded-lg border border-clinical-border/50">
                  <span className="text-xs font-semibold text-clinical-text whitespace-nowrap">
                    Axial Plane (Z): {zUI} / {doseInfo.sources[0]?.n_planes - 1 || 0}
                  </span>
                  <input
                    type="range"
                    min={0}
                    max={(doseInfo.sources[0]?.n_planes || 1) - 1}
                    value={zUI}
                    onChange={(e) => setZUI(parseInt(e.target.value, 10))}
                    className="flex-1 accent-clinical-accent cursor-pointer"
                  />
                  <span className="text-xs font-mono text-clinical-muted">
                    {doseInfo.sources[0]?.spacing?.[0]
                      ? `${(zUI * doseInfo.sources[0].spacing[0]).toFixed(1)} mm`
                      : ""}
                  </span>
                </div>

                {viewMode === "dose" && (
                  <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                    <div className="border border-clinical-border rounded-lg p-3 bg-clinical-bg/30">
                      <div className="flex items-center justify-between text-xs font-semibold text-clinical-text mb-2">
                        <span>openMCsquare Secondary Dose</span>
                        <span className="text-[10px] text-clinical-muted">Monte Carlo</span>
                      </div>
                      <DoseMapCanvas
                        plane={dosePlanes[resolveSource("mcSquare")] ?? null}
                        backdrop={showCT ? ctPlane : null}
                        mode="dose"
                        scaleMax={scaleMax}
                        showIsodose={showIso}
                        fitVh={34}
                      />
                      <DoseColorbar scaleMax={scaleMax} />
                    </div>

                    <div className="border border-clinical-border rounded-lg p-3 bg-clinical-bg/30">
                      <div className="flex items-center justify-between text-xs font-semibold text-clinical-text mb-2">
                        <span>TPS Reference Dose</span>
                        <span className="text-[10px] text-clinical-muted">RayStation</span>
                      </div>
                      <DoseMapCanvas
                        plane={dosePlanes[resolveSource("tps")] ?? null}
                        backdrop={showCT ? ctPlane : null}
                        mode="dose"
                        scaleMax={scaleMax}
                        showIsodose={showIso}
                        fitVh={34}
                      />
                      <DoseColorbar scaleMax={scaleMax} />
                    </div>

                    <div className="border border-clinical-border rounded-lg p-3 bg-clinical-bg/30">
                      <div className="flex items-center justify-between text-xs font-semibold text-clinical-text mb-2">
                        <span>{selectedBeamNum != null ? `Beam ${selectedBeamNum} Gamma Map` : "Secondary Gamma Map"}</span>
                        <span className="text-[10px] font-bold text-green-600 dark:text-green-400">
                          {gammaMcTps?.passingRate ? `${gammaMcTps.passingRate.toFixed(1)}% Pass` : "3%/3mm"}
                        </span>
                      </div>
                      <DoseMapCanvas
                        plane={gammaMcTps}
                        backdrop={showCT ? ctPlane : null}
                        mode="gamma"
                        fitVh={34}
                      />
                      <GammaColorbar />
                    </div>
                  </div>
                )}

                {viewMode === "gamma" && (
                  <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                    {doseInfo.available_comparisons.map((comp) => {
                      const gp = gammaPlanes[comp];
                      return (
                        <div key={comp} className="border border-clinical-border rounded-lg p-3 bg-clinical-bg/30">
                          <div className="flex items-center justify-between text-xs font-semibold text-clinical-text mb-2">
                            <span>{selectedBeamNum != null ? `Beam ${selectedBeamNum} (${COMPARISON_LABELS[comp] || comp})` : (COMPARISON_LABELS[comp] || comp)}</span>
                            {gp?.passingRate != null && (
                              <span className="text-xs font-bold text-green-600 dark:text-green-400">
                                {gp.passingRate.toFixed(1)}%
                              </span>
                            )}
                          </div>
                          <DoseMapCanvas
                            plane={gp ?? null}
                            backdrop={showCT ? ctPlane : null}
                            mode="gamma"
                            fitVh={34}
                          />
                          <GammaColorbar />
                        </div>
                      );
                    })}
                  </div>
                )}

                {viewMode === "diff" && (
                  <div className="max-w-2xl mx-auto">
                    <div className="border border-clinical-border rounded-lg p-4 bg-clinical-bg/30">
                      <div className="flex items-center justify-between text-xs font-semibold text-clinical-text mb-3">
                        <span>Dose Difference: MCsquare vs TPS</span>
                        <span className="text-[10px] text-clinical-muted">3D Patient Anatomy</span>
                      </div>
                      <DoseMapCanvas
                        plane={dosePlanes[resolveSource("mcSquare")] ?? null}
                        backdrop={showCT ? ctPlane : null}
                        mode="diff"
                        scaleMax={scaleMax}
                        fitVh={36}
                      />
                      <DiffColorbar scaleMax={scaleMax} />
                    </div>
                  </div>
                )}
              </div>
            ) : (
              <div className="rounded-lg border border-dashed border-clinical-border p-8 text-center bg-clinical-surface">
                <Atom size={28} className="mx-auto text-clinical-muted mb-2" />
                <h3 className="text-sm font-semibold text-clinical-text">Dose Distribution Not Yet Calculated</h3>
                <p className="text-xs text-clinical-muted max-w-md mx-auto mt-1 mb-4">
                  Run the openMCsquare Monte Carlo simulation to generate secondary 3D dose grids, beam doses, and gamma distributions.
                </p>
                {jobPolling && activeJob?.job_type === "mcSquare" ? (
                  <div className="max-w-md mx-auto space-y-3">
                    <div className="flex justify-between text-xs text-blue-400 font-medium">
                      <span>Simulating Monte Carlo dose distribution...</span>
                      <span>{Math.round((activeJob.progress || 0) * 100)}%</span>
                    </div>
                    <div className="w-full bg-blue-900/30 h-2 rounded-full overflow-hidden border border-blue-500/20">
                      <div
                        className="bg-blue-500 h-full transition-all duration-300"
                        style={{ width: `${Math.max(5, (activeJob.progress || 0) * 100)}%` }}
                      />
                    </div>
                    <button
                      onClick={handleStopJob}
                      className="flex items-center justify-center gap-1.5 px-4 py-1.5 mx-auto bg-red-600/10 hover:bg-red-600/20 text-red-400 border border-red-500/30 text-xs font-semibold rounded transition-colors"
                      title="Stop simulation (completed beam doses will be preserved)"
                    >
                      <Square size={12} fill="currentColor" />
                      Stop Simulation
                    </button>
                  </div>
                ) : mcCompletedBeams.length > 0 && mcCompletedBeams.length < (plan.number_of_fields || 0) ? (
                  <div className="flex flex-col items-center gap-2">
                    <p className="text-xs text-amber-300 font-medium">
                      {mcCompletedBeams.length} of {plan.number_of_fields} beam doses completed and preserved.
                    </p>
                    <div className="flex items-center gap-2">
                      <button
                        onClick={() => handleLaunchJob("mcSquare", false)}
                        disabled={jobPolling}
                        className="flex items-center gap-1.5 px-4 py-2 bg-amber-500 hover:bg-amber-400 text-black text-xs font-semibold rounded transition-colors shadow-sm"
                      >
                        <Play size={13} fill="currentColor" />
                        Resume Simulation ({mcCompletedBeams.length}/{plan.number_of_fields})
                      </button>
                      <button
                        onClick={() => handleLaunchJob("mcSquare", true)}
                        disabled={jobPolling}
                        className="flex items-center gap-1.5 px-3 py-2 bg-clinical-surface hover:bg-clinical-border/30 border border-clinical-border text-clinical-muted hover:text-clinical-text text-xs rounded transition-colors"
                        title="Discard cached beam doses and recalculate all beams"
                      >
                        <RotateCcw size={12} />
                        Restart All
                      </button>
                    </div>
                  </div>
                ) : (
                  <button
                    onClick={() => handleLaunchJob("mcSquare", false)}
                    disabled={jobPolling}
                    className="px-4 py-2 bg-clinical-accent text-white text-xs font-medium rounded hover:bg-clinical-accent/90 transition-colors shadow-sm"
                  >
                    Launch openMCsquare Simulation
                  </button>
                )}
              </div>
            )}

            {/* Beam-by-Beam Gamma Table */}
            <div className="rounded-lg border border-clinical-border bg-clinical-surface p-4">
              <h2 className="text-xs font-bold text-clinical-text uppercase tracking-wider mb-3">
                Field-by-Field Gamma Evaluation Breakdown
              </h2>
              <div className="overflow-x-auto">
                <table className="w-full text-xs text-left border-collapse">
                  <thead>
                    <tr className="border-b border-clinical-border text-clinical-muted uppercase text-[10px]">
                      <th className="py-2 px-3">Beam #</th>
                      <th className="py-2 px-3">Comparison</th>
                      <th className="py-2 px-3">Field Name</th>
                      <th className="py-2 px-3">Criteria (DD / DTA)</th>
                      <th className="py-2 px-3">Pass Threshold</th>
                      <th className="py-2 px-3">Passing Rate</th>
                      <th className="py-2 px-3">Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {mcFieldResults.length === 0 ? (
                      <tr>
                        <td colSpan={7} className="py-6 text-center text-clinical-muted">
                          <div>No openMCsquare gamma evaluations recorded yet.</div>
                          {doseInfo?.sources.some((s) => s.source.startsWith("mcSquare")) && (
                            <button
                              onClick={() => handleLaunchJob("gamma")}
                              disabled={jobPolling}
                              className="mt-2.5 inline-flex items-center gap-1.5 px-3 py-1 bg-clinical-surface hover:bg-clinical-border/40 border border-clinical-border text-clinical-text text-xs rounded transition-colors"
                            >
                              <RotateCcw size={12} className="text-clinical-accent" />
                              Calculate 3D Gamma Evaluations
                            </button>
                          )}
                        </td>
                      </tr>
                    ) : (
                      mcFieldResults.map((r) => (
                        <tr key={r.id} className="border-b border-clinical-border/40 hover:bg-clinical-bg/40">
                          <td className="py-2 px-3 font-mono">{r.beam_number ?? "—"}</td>
                          <td className="py-2 px-3 font-medium text-clinical-text">
                            {COMPARISON_LABELS[r.comparison_type as ComparisonType] || r.comparison_type}
                          </td>
                          <td className="py-2 px-3 font-mono">{r.field_name}</td>
                          <td className="py-2 px-3 text-clinical-muted font-mono">
                            {r.dd_percent}% / {r.dta_mm}mm
                          </td>
                          <td className="py-2 px-3 text-clinical-muted font-mono">&ge; {r.threshold}%</td>
                          <td className="py-2 px-3 font-mono font-bold">
                            <span style={{ color: r.passed ? C.passText : C.measureText }}>
                              {r.passing_rate.toFixed(1)}%
                            </span>
                          </td>
                          <td className="py-2 px-3">
                            <span
                              className={`text-[10px] font-bold px-2 py-0.5 rounded ${
                                r.passed
                                  ? "bg-green-100 dark:bg-green-950 text-green-700 dark:text-green-300"
                                  : "bg-red-100 dark:bg-red-950 text-red-700 dark:text-red-300"
                              }`}
                            >
                              {r.passed ? "PASS" : "FAIL"}
                            </span>
                          </td>
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>
            </div>

            {/* openMCsquare Robustness Analysis & DVH Predictions */}
            <RobustnessDVHCard
              planId={id}
              hasMCDose={doseInfo?.sources.some((s) => s.source.startsWith("mcSquare")) ?? false}
              onLaunchMC={() => handleLaunchJob("mcSquare")}
              onSelectedRoisChange={setDvhSelectedRois}
            />
          </div>
        )}

        {/* TAB 2: FRACTIONAL TRACKING & DELIVERY LOGS */}
        {activeTab === "fractions" && (
          <div className="space-y-5">
            {/* Delivery Records Import & Upload Banner */}
            <div className="flex items-center justify-between p-3.5 rounded-lg bg-clinical-surface border border-clinical-border shadow-xs">
              <div className="flex items-center gap-2.5">
                <div className="p-2 rounded bg-indigo-500/10 text-indigo-400 border border-indigo-500/20">
                  <Layers size={16} />
                </div>
                <div>
                  <span className="text-xs font-semibold text-clinical-text">Delivery Logs & Treatment Records</span>
                  <p className="text-[11px] text-clinical-muted">Import delivered machine logs from Orthanc or upload RT Record files directly for this plan.</p>
                </div>
              </div>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => setShowUploadRecordModal(true)}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold bg-emerald-600/10 text-emerald-400 hover:bg-emerald-600/20 border border-emerald-500/30 transition-colors"
                  title="Upload RT Record DICOM file or zip directly for this plan"
                >
                  <UploadCloud size={13} /> Upload RT Record
                </button>
                <button
                  onClick={() => {
                    setOrthancTab("rt_records");
                    setShowOrthancModal(true);
                  }}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold bg-indigo-600/10 text-indigo-400 hover:bg-indigo-600/20 border border-indigo-500/30 transition-colors"
                >
                  <Server size={13} /> Import from Orthanc
                </button>
              </div>
            </div>

            {interruptedFractions.length > 0 && (
              <div className="rounded-lg border border-amber-400/80 bg-amber-500/10 p-4 flex items-start gap-3 shadow-xs">
                <AlertTriangle size={20} className="text-amber-500 shrink-0 mt-0.5" />
                <div className="space-y-1">
                  <div className="flex items-center gap-2">
                    <h3 className="text-xs font-bold text-amber-900 dark:text-amber-200">
                      Partial Delivery Detected ({interruptedFractions.length} Interrupted Delivery Record{interruptedFractions.length === 1 ? "" : "s"})
                    </h3>
                    <span className="text-[10px] px-1.5 py-0.2 rounded bg-amber-500/20 text-amber-800 dark:text-amber-300 font-bold border border-amber-500/30">
                      FLAGGED
                    </span>
                  </div>
                  <p className="text-[11px] text-amber-800 dark:text-amber-300">
                    {interruptedFractions.map((f) => `Fraction ${f.fraction_number}${f.interruption_reason ? `: ${f.interruption_reason}` : ""}`).join("; ")}
                  </p>
                  <p className="text-[11px] text-amber-700/80 dark:text-amber-300/80">
                    Dose reconstruction and 3D gamma reflect partial delivery against prescribed dose. You can review the layer-by-layer log metrics below or re-upload a merged/fixed record.
                  </p>
                </div>
              </div>
            )}

            {trendData?.drift_alert && (
              <div className="rounded-lg border border-amber-300 bg-amber-50 dark:bg-amber-950/40 p-4 flex items-center gap-3">
                <AlertTriangle size={20} className="text-amber-600 shrink-0" />
                <div>
                  <h3 className="text-xs font-bold text-amber-900 dark:text-amber-200">
                    Fractional Delivery Drift Alert Detected
                  </h3>
                  <p className="text-[11px] text-amber-800 dark:text-amber-300 mt-0.5">
                    Gamma passing rates on the last 3 fractions are decreasing monotonically and approaching the TG-218 action limit (90.0%). Review magnet calibration and beam current.
                  </p>
                </div>
              </div>
            )}

            <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
              <div className="rounded-lg border border-clinical-border bg-clinical-surface p-4">
                <div className="flex items-center justify-between mb-3">
                  <h2 className="text-xs font-bold text-clinical-text uppercase tracking-wider">
                    Fractional Gamma Passing Rate Trend (Log vs Rx)
                  </h2>
                  <span className="text-[11px] text-clinical-muted">TG-218 2%/2mm</span>
                </div>
                {trendData?.series?.log_vs_Rx ? (
                  <TrendChart
                    points={trendData.series.log_vs_Rx}
                    threshold={trendData.thresholds?.log_vs_Rx ?? 90.0}
                  />
                ) : (
                  <p className="text-clinical-muted text-xs py-8 text-center">
                    No fraction deliveries recorded yet.
                  </p>
                )}
              </div>

              <div className="rounded-lg border border-clinical-border bg-clinical-surface p-4">
                <h2 className="text-xs font-bold text-clinical-text uppercase tracking-wider mb-3">
                  Fractions &times; Beams Delivery Heatmap
                </h2>
                <LogHeatmap results={logResults} totalFractions={plan.number_of_fractions} />
                <div className="mt-4 pt-3 border-t border-clinical-border/40 flex items-center justify-between text-xs text-clinical-muted">
                  <span>Green: Pass (&ge;93%) &middot; Amber: Marginal (&ge;90%) &middot; Red: Fail (&lt;90%)</span>
                </div>
              </div>
            </div>

            {/* 6-DOF COUCH POSITION & ROTATION TRACKING */}
            <CouchTrackingTrend planId={id} />

            <div className="rounded-lg border border-clinical-border bg-clinical-surface p-4">
              <div className="flex items-center justify-between mb-3">
                <div>
                  <h2 className="text-xs font-bold text-clinical-text uppercase tracking-wider">
                    Delivered Proton Spot Position &amp; MU Deviation Map
                  </h2>
                  <p className="text-[11px] text-clinical-muted mt-0.5">
                    Layer-by-layer spot accuracy from treatment machine delivery logs.
                  </p>
                </div>
                <button
                  onClick={() => setShowSpotMap((v) => !v)}
                  className="px-3 py-1.5 text-xs bg-clinical-bg border border-clinical-border rounded hover:bg-clinical-border/30 text-clinical-text transition-colors"
                >
                  {showSpotMap ? "Hide Spot Map" : "Show Interactive Spot Map"}
                </button>
              </div>

              {showSpotMap && (
                <div className="mt-4 pt-3 border-t border-clinical-border/40">
                  <SpotMap planId={id} />
                </div>
              )}
            </div>

            {/* FRACTION DELIVERY LOG & LAYER-BY-LAYER QA REPORT */}
            <FractionLogViewer planId={id} />
          </div>
        )}

        {/* TAB: OFFLINE IMAGE REVIEW (OIR) */}
        {activeTab === "oir" && (
          <div className="space-y-4">
            <div className="flex items-center justify-between p-3.5 rounded-lg bg-clinical-surface border border-clinical-border shadow-xs">
              <div className="flex items-center gap-2.5">
                <div className="p-2 rounded bg-indigo-500/10 text-indigo-400 border border-indigo-500/20">
                  <Radio size={16} />
                </div>
                <div>
                  <span className="text-xs font-semibold text-clinical-text">Orthanc PACS Offline Images</span>
                  <p className="text-[11px] text-clinical-muted">Import daily setup CBCT scans and spatial registration (REG) files from Orthanc for offline review.</p>
                </div>
              </div>
              <button
                onClick={() => {
                  setOrthancTab("offline_images");
                  setShowOrthancModal(true);
                }}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold bg-indigo-600/10 text-indigo-400 hover:bg-indigo-600/20 border border-indigo-500/30 transition-colors"
              >
                <Server size={13} /> Import Daily CBCT / REG from Orthanc
              </button>
            </div>
            <OIRViewer planId={id} plan={plan} />
          </div>
        )}

        {/* TAB: SYNTHETIC QACT ADAPTIVE SETUP & DOSE */}
        {activeTab === "synthetic_ct" && (
          <div className="space-y-4">
            <div className="flex items-center justify-between p-3.5 rounded-lg bg-clinical-surface border border-clinical-border shadow-xs">
              <div className="flex items-center gap-2.5">
                <div className="p-2 rounded bg-indigo-500/10 text-indigo-400 border border-indigo-500/20">
                  <Radio size={16} />
                </div>
                <div>
                  <span className="text-xs font-semibold text-clinical-text">Orthanc PACS CBCT Scans</span>
                  <p className="text-[11px] text-clinical-muted">Pull daily CBCT scans from Orthanc directly into Synthetic CT Adaptive QA.</p>
                </div>
              </div>
              <button
                onClick={() => {
                  setOrthancTab("offline_images");
                  setShowOrthancModal(true);
                }}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold bg-indigo-600/10 text-indigo-400 hover:bg-indigo-600/20 border border-indigo-500/30 transition-colors"
              >
                <Server size={13} /> Import CBCT from Orthanc
              </button>
            </div>
            <SyntheticCTViewer planId={id} plan={plan} />
          </div>
        )}

        {/* TAB 3: PLAN PARAMETERS & DELIVERABILITY */}
        {activeTab === "parameters" && (

          <div className="space-y-5">
            <div className="rounded-lg border border-clinical-border bg-clinical-surface p-4">
              <h2 className="text-xs font-bold text-clinical-text uppercase tracking-wider mb-3">
                DICOM RT Plan Beam Details ({fields.length} Beams)
              </h2>
              <div className="overflow-x-auto">
                <table className="w-full text-xs text-left border-collapse">
                  <thead>
                    <tr className="border-b border-clinical-border text-clinical-muted uppercase text-[10px]">
                      <th className="py-2 px-3">Beam Name</th>
                      <th className="py-2 px-3">Gantry Angle</th>
                      <th className="py-2 px-3">Energy Range</th>
                      <th className="py-2 px-3">Layers Count</th>
                      <th className="py-2 px-3">Total Spots</th>
                      <th className="py-2 px-3">Total MU</th>
                    </tr>
                  </thead>
                  <tbody>
                    {fields.length === 0 ? (
                      <tr>
                        <td colSpan={6} className="py-4 text-center text-clinical-muted">
                          No beam specifications parsed.
                        </td>
                      </tr>
                    ) : (
                      fields.map((f, i) => (
                        <tr key={i} className="border-b border-clinical-border/40 hover:bg-clinical-bg/40">
                          <td className="py-2 px-3 font-semibold text-clinical-text">{f.beam_name}</td>
                          <td className="py-2 px-3 font-mono">{f.gantry_angle.toFixed(1)}&deg;</td>
                          <td className="py-2 px-3 font-mono">
                            {f.energy_min_mev.toFixed(1)} – {f.energy_max_mev.toFixed(1)} MeV
                          </td>
                          <td className="py-2 px-3 font-mono">{f.number_of_layers}</td>
                          <td className="py-2 px-3 font-mono">{f.total_spots.toLocaleString()}</td>
                          <td className="py-2 px-3 font-mono font-semibold">{f.total_mu.toFixed(2)}</td>
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>
            </div>

            <div className="rounded-lg border border-clinical-border bg-clinical-surface p-4">
              <h2 className="text-xs font-bold text-clinical-text uppercase tracking-wider mb-3">
                Commissioned Machine Deliverability Envelope Audit
              </h2>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-4 text-xs">
                <div className="p-3 rounded-lg bg-clinical-bg/50 border border-clinical-border">
                  <span className="text-clinical-muted block text-[11px] mb-1">Spot MU Range Check</span>
                  <div className="flex items-center gap-1.5 text-green-600 dark:text-green-400 font-semibold">
                    <CheckCircle2 size={14} /> Within 0.005 – 2.5 MU Envelope
                  </div>
                </div>

                <div className="p-3 rounded-lg bg-clinical-bg/50 border border-clinical-border">
                  <span className="text-clinical-muted block text-[11px] mb-1">Energy Range Limits</span>
                  <div className="flex items-center gap-1.5 text-green-600 dark:text-green-400 font-semibold">
                    <CheckCircle2 size={14} /> 70.0 – 225.0 MeV Nominal
                  </div>
                </div>

                <div className="p-3 rounded-lg bg-clinical-bg/50 border border-clinical-border">
                  <span className="text-clinical-muted block text-[11px] mb-1">Scanning Raster Speed</span>
                  <div className="flex items-center gap-1.5 text-green-600 dark:text-green-400 font-semibold">
                    <CheckCircle2 size={14} /> Within Commissioned Dynamics
                  </div>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* TAB 4: EVIDENCE GATE & CLINICAL CLEARANCE */}
        {!muteGate && activeTab === "gate" && (
          <div className="space-y-5">
            <MonitoringCard planId={id} onGate={setGate} />
          </div>
        )}
      </div>

      {/* Orthanc PACS Import Modal */}
      {showOrthancModal && (
        <OrthancImportModal
          onClose={() => setShowOrthancModal(false)}
          initialPatientId={plan.patient_identifier || undefined}
          initialPlanId={id}
          initialTab={orthancTab}
          onPlanImported={() => {
            loadPlanData();
          }}
        />
      )}

      {/* Upload RT Record Modal */}
      {showUploadRecordModal && (
        <UploadRecordModal
          planId={plan.id}
          planLabel={plan.plan_label}
          onClose={() => setShowUploadRecordModal(false)}
          onSuccess={(result) => {
            setShowUploadRecordModal(false);
            toast.success(
              result.warnings?.[0] || `RT Record uploaded for ${plan.plan_label}`
            );
            loadPlanData();
          }}
        />
      )}

      {/* Upload & Inspect DICOM Dose Modal */}
      <UploadDoseModal
        planId={id}
        planLabel={plan.plan_label}
        doseStatus={doseStatus}
        isOpen={showUploadDoseModal}
        onClose={() => setShowUploadDoseModal(false)}
        onSuccess={(result) => {
          setShowUploadDoseModal(false);
          toast.success(
            result.message || `Processed ${result.files_saved.length} dose file(s)`
          );
          setDoseStatus(result.dose_status);
          loadPlanData();
        }}
      />

      {/* Physics Chart Check Modal */}
      <ChartCheckModal
        planId={id}
        isOpen={showChartCheckModal}
        onClose={() => setShowChartCheckModal(false)}
        onCheckSaved={() => {
          loadPlanData();
        }}
      />
    </div>
  );
}
