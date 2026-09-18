import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, Play, Loader2, Layers } from "lucide-react";
import toast from "react-hot-toast";
import {
  getCTPlane,
  getDosePlane,
  getGammaPlane,
  getPlanDoseInfo,
  getPlanResults,
  getJob,
  runJob,
} from "../api/client";
import type {
  ComparisonType,
  DoseSource,
  GammaResult,
  PlaneData,
  PlanDoseInfo,
} from "../types";
import { DoseMapCanvas } from "../components/DoseMapCanvas";
import { DoseColorbar } from "../components/DoseColorbar";
import { DiffColorbar, GammaColorbar } from "../components/PanelLegends";
import { GammaBar } from "../components/GammaBar";
import { StatusBadge } from "../components/StatusBadge";
import { RobustnessDVHCard } from "../components/RobustnessDVHCard";

const SOURCE_LABELS: Record<DoseSource, string> = {
  tps: "TPS (RayStation)",
  mcSquare: "MCsquare MC",
  log: "Log reconstruction",
};
const COMPARISON_ORDER: ComparisonType[] = [
  "mcSquare_vs_TPS",
  "log_vs_TPS",
  "log_vs_Rx",
  "mcSquare_vs_log",
];
const COMPARISON_LABELS: Record<ComparisonType, string> = {
  mcSquare_vs_TPS: "MCsquare vs TPS",
  log_vs_TPS: "Log recon vs TPS",
  log_vs_Rx: "Log recon vs Rx",
  mcSquare_vs_log: "MCsquare vs Log",
};

// "summed" shows the plan-level composite; a number shows that beam's field.
type FieldSel = "summed" | number;

// Base sources shown in each field mode (in panel order).
const BASE_SOURCES: DoseSource[] = ["mcSquare", "tps", "log"];

export function DoseComparison() {
  const { planId } = useParams<{ planId: string }>();
  const navigate = useNavigate();
  const id = planId ? parseInt(planId, 10) : NaN;

  const [info, setInfo] = useState<PlanDoseInfo | null>(null);
  const [results, setResults] = useState<GammaResult[]>([]);
  // zUI tracks the slider instantly; z (the fetch trigger) follows after a
  // short debounce so dragging does not fire a request per pixel of movement.
  const [z, setZ] = useState(0);
  const [zUI, setZUI] = useState(0);
  const [view, setView] = useState<"dose" | "gamma">("dose");
  const [showIso, setShowIso] = useState(true);
  const [showCT, setShowCT] = useState(true);
  const [ctPlane, setCtPlane] = useState<PlaneData | null>(null);
  const [ctAvailable, setCtAvailable] = useState(true);
  const [field, setField] = useState<FieldSel>("summed");
  const [dosePlanes, setDosePlanes] = useState<Partial<Record<string, PlaneData>>>({});
  const [gammaPlanes, setGammaPlanes] = useState<Partial<Record<ComparisonType, PlaneData>>>({});
  // Live gamma slice (MCsquare vs TPS) shown in the dose-view 2x2 grid. It is
  // computed server-side on demand, so it fills in a beat after the dose
  // panels rather than blocking them.
  const [gammaMcTps, setGammaMcTps] = useState<PlaneData | null>(null);
  const [running, setRunning] = useState(false);
  const reqToken = useRef(0);

  const loadInfo = useCallback(async () => {
    const di = await getPlanDoseInfo(id);
    setInfo(di);
    setZ(di.default_plane);
    setZUI(di.default_plane);
    setResults(await getPlanResults(id));
    return di;
  }, [id]);

  // Debounce: commit the slider position to the fetch trigger after 150 ms of
  // no movement.
  useEffect(() => {
    const t = setTimeout(() => setZ(zUI), 150);
    return () => clearTimeout(t);
  }, [zUI]);

  useEffect(() => {
    if (Number.isNaN(id)) {
      navigate("/");
      return;
    }
    loadInfo().catch(() => navigate("/"));
  }, [id, navigate, loadInfo]);

  // Detect which beams have per-beam sources available (e.g. "mcSquare_beam1").
  const availableBeams = (() => {
    if (!info) return [];
    const nums = new Set<number>();
    for (const s of info.sources) {
      const m = /_beam(\d+)$/.exec(s.source);
      if (m) nums.add(parseInt(m[1], 10));
    }
    return Array.from(nums).sort((a, b) => a - b);
  })();

  // Resolve the actual source key for a base source under the current field.
  const resolveSource = useCallback(
    (base: DoseSource): string =>
      field === "summed" ? base : `${base}_beam${field}`,
    [field]
  );

  // Which base sources actually exist for the current field selection.
  const activeSources = (() => {
    if (!info) return [];
    return BASE_SOURCES.filter((base) => {
      const key = field === "summed" ? base : `${base}_beam${field}`;
      return info.sources.some((src) => src.source === key);
    });
  })();

  // Fetch the CT backdrop plane for the current depth (shared by all panels).
  // 404 = no CT series in the store -> disable the toggle for this plan.
  useEffect(() => {
    if (!info || !ctAvailable) return;
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [info, id, z]);

  // Non-blocking fetch of the gamma slice for the dose-view 2x2 grid.
  useEffect(() => {
    if (!info || view !== "dose") return;
    if (!info.available_comparisons.includes("mcSquare_vs_TPS")) {
      setGammaMcTps(null);
      return;
    }
    let live = true;
    setGammaMcTps(null);
    getGammaPlane(id, "mcSquare_vs_TPS", z)
      .then((p) => {
        if (live) setGammaMcTps(p);
      })
      .catch(() => {
        if (live) setGammaMcTps(null);
      });
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [info, id, z, view]);

  // Fetch planes whenever depth / view / field / availability changes.
  useEffect(() => {
    if (!info) return;
    const token = ++reqToken.current;

    if (view === "dose") {
      Promise.all(
        activeSources.map((base) => {
          const key = resolveSource(base);
          return getDosePlane(id, key, z).then((p) => [base, p] as const);
        })
      )
        .then((pairs) => {
          if (token !== reqToken.current) return;
          const next: Partial<Record<string, PlaneData>> = {};
          for (const [base, p] of pairs) next[base] = p;
          setDosePlanes(next);
        })
        .catch(() => {});
    } else {
      const avail = info.available_comparisons;
      Promise.all(
        avail.map((c) => getGammaPlane(id, c, z).then((p) => [c, p] as const))
      )
        .then((pairs) => {
          if (token !== reqToken.current) return;
          const next: Partial<Record<ComparisonType, PlaneData>> = {};
          for (const [c, p] of pairs) next[c] = p;
          setGammaPlanes(next);
        })
        .catch(() => {});
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [info, id, z, view, field]);

  const handleRunGamma = async () => {
    setRunning(true);
    try {
      const job = await runJob(id, "gamma");
      // Poll until the job reaches a terminal state. 3D per-beam gamma takes
      // minutes; the old 60x800ms loop (48 s cap) gave up long before the job
      // finished, so the page never refreshed. Cap at 15 min as a safety net.
      const deadline = Date.now() + 15 * 60 * 1000;
      while (Date.now() < deadline) {
        await new Promise((res) => setTimeout(res, 2000));
        const j = await getJob(job.id);
        if (j.status === "complete") {
          toast.success("Gamma analysis complete");
          await loadInfo();
          return;
        }
        if (j.status === "error" || j.status === "cancelled") {
          toast.error(`Gamma analysis ${j.status}`);
          return;
        }
      }
      toast.error("Timed out waiting for gamma analysis — refresh to check.");
    } catch {
      toast.error("Failed to run gamma analysis");
    } finally {
      setRunning(false);
    }
  };

  // Dose difference (MCsquare - TPS) computed client-side: both planes are on
  // the same grid, so this needs no extra request. NOTE: must live with the
  // other hooks, ABOVE the `if (!info)` early return (rules of hooks).
  const diffPlane: (PlaneData & { maxAbs: number }) | null = useMemo(() => {
    const a = dosePlanes["mcSquare"];
    const b = dosePlanes["tps"];
    if (!a || !b || a.rows !== b.rows || a.cols !== b.cols) return null;
    const d = new Float32Array(a.data.length);
    let maxAbs = 0;
    for (let i = 0; i < d.length; i++) {
      const v = a.data[i] - b.data[i];
      d[i] = v;
      const av = Math.abs(v);
      if (av > maxAbs) maxAbs = av;
    }
    return { data: d, rows: a.rows, cols: a.cols, maxAbs };
  }, [dosePlanes]);

  if (!info) {
    return (
      <div className="min-h-screen bg-clinical-bg flex items-center justify-center text-clinical-muted text-sm">
        <Loader2 className="animate-spin mr-2" size={16} /> Loading dose data…
      </div>
    );
  }

  // Metadata for the current field's sources (falls back to any matching source).
  const metaFor = (base: DoseSource) => {
    const key = resolveSource(base);
    return info.sources.find((x) => x.source === key);
  };

  const nPlanes = info.sources[0]?.n_planes ?? 1;
  const sliceSpacing = info.sources[0]?.spacing?.[0] ?? 1;
  const scaleMax = Math.max(...info.sources.map((s) => s.max_dose), 1e-6);
  const hasComparisons = info.available_comparisons.length > 0;
  const hasResults = results.length > 0;

  // Per-field mcSquare_vs_TPS results, ordered by beam. Match on the persisted
  // DICOM beam_number (authoritative). Fallback for legacy rows without
  // beam_number: sort by id ASC = write order = ascending beam number. NEVER
  // index into API order (id DESC) -- that is what inverted the field labels.
  const mcPerField = results
    .filter((x) => x.comparison_type === "mcSquare_vs_TPS" && x.field_name)
    .sort((a, b) =>
      a.beam_number != null && b.beam_number != null
        ? a.beam_number - b.beam_number
        : a.id - b.id
    );

  // Beam-number -> field_name from stored gamma results, for selector labels.
  const beamLabel = (n: number): string => {
    const r =
      mcPerField.find((x) => x.beam_number === n) ?? mcPerField[n - 1];
    return r?.field_name ? `Field ${n}: ${r.field_name}` : `Field ${n}`;
  };

  // Per-field gamma result to spotlight in the sidebar.
  const fieldResults =
    field === "summed"
      ? results
      : results.filter((r) => {
          if (r.beam_number != null) return r.beam_number === field;
          const same = results
            .filter((y) => y.comparison_type === r.comparison_type)
            .sort((a, b) => a.id - b.id);
          return same.indexOf(r) === (field as number) - 1;
        });

  return (
    <div className="min-h-screen bg-clinical-bg">
      {/* Header */}
      <div className="border-b border-clinical-border bg-clinical-surface">
        <div className="w-full px-6 py-4 flex items-center gap-4">
          <button
            onClick={() => navigate(`/plans/${id}/simulation`)}
            className="text-clinical-muted hover:text-clinical-text transition-colors"
          >
            <ArrowLeft size={18} />
          </button>
          <div className="flex-1">
            <h1 className="text-base font-semibold text-clinical-text">Dose comparison</h1>
            <p className="text-xs text-clinical-muted">Plan #{id}</p>
          </div>
          <StatusBadge status={info.verdict} />
        </div>
      </div>

      <div className="w-full px-6 py-3 grid grid-cols-1 lg:grid-cols-[1fr_300px] gap-6">
        {/* Main viewer */}
        <div>
          {/* Controls */}
          <div className="flex flex-wrap items-center gap-3 mb-4">
            <div className="flex rounded-md border border-clinical-border overflow-hidden">
              {(["dose", "gamma"] as const).map((v) => (
                <button
                  key={v}
                  onClick={() => setView(v)}
                  disabled={v === "gamma" && !hasComparisons}
                  className={`px-3 py-1.5 text-xs font-medium transition-colors disabled:opacity-40 disabled:cursor-not-allowed ${
                    view === v
                      ? "bg-clinical-accent text-white"
                      : "bg-clinical-surface text-clinical-muted hover:text-clinical-text"
                  }`}
                >
                  {v === "dose" ? "Dose" : "Gamma"}
                </button>
              ))}
            </div>

            {/* Field selector — only when per-beam sources exist */}
            {availableBeams.length > 0 && (
              <div className="flex rounded-md border border-clinical-border overflow-hidden">
                <button
                  onClick={() => setField("summed")}
                  className={`px-3 py-1.5 text-xs font-medium transition-colors ${
                    field === "summed"
                      ? "bg-clinical-accent text-white"
                      : "bg-clinical-surface text-clinical-muted hover:text-clinical-text"
                  }`}
                >
                  Summed
                </button>
                {availableBeams.map((n) => (
                  <button
                    key={n}
                    onClick={() => setField(n)}
                    className={`px-3 py-1.5 text-xs font-medium transition-colors border-l border-clinical-border ${
                      field === n
                        ? "bg-clinical-accent text-white"
                        : "bg-clinical-surface text-clinical-muted hover:text-clinical-text"
                    }`}
                  >
                    {beamLabel(n)}
                  </button>
                ))}
              </div>
            )}

            {view === "dose" && (
              <label className="flex items-center gap-2 text-xs text-clinical-muted cursor-pointer">
                <input
                  type="checkbox"
                  checked={showIso}
                  onChange={(e) => setShowIso(e.target.checked)}
                  className="accent-clinical-accent"
                />
                Isodose lines
              </label>
            )}

            {ctAvailable && (
              <label className="flex items-center gap-2 text-xs text-clinical-muted cursor-pointer">
                <input
                  type="checkbox"
                  checked={showCT}
                  onChange={(e) => setShowCT(e.target.checked)}
                  className="accent-clinical-accent"
                />
                CT backdrop
              </label>
            )}

            <div className="flex items-center gap-1.5 text-xs text-clinical-muted ml-auto">
              <Layers size={13} />
              Slice {zUI + 1}/{nPlanes} · {(zUI * sliceSpacing).toFixed(0)} mm
            </div>
          </div>

          {/* Panels */}
          {view === "dose" ? (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              {activeSources.map((base) => (
                <div key={base} className="bg-clinical-surface border border-clinical-border rounded-lg p-3">
                  <p className="text-xs font-medium text-clinical-text mb-2">
                    {SOURCE_LABELS[base]}
                    {field !== "summed" && (
                      <span className="text-clinical-muted font-normal"> · {beamLabel(field as number)}</span>
                    )}
                  </p>
                  <DoseMapCanvas
                    plane={dosePlanes[base] ?? null}
                    mode="dose"
                    scaleMax={scaleMax}
                    showIsodose={showIso}
                    backdrop={showCT ? ctPlane : null}
                    size={800}
                    fitVh={33}
                  />
                  <DoseColorbar className="mt-2" scaleMax={scaleMax} />
                  <p className="text-[10px] text-clinical-muted mt-2">
                    max {(metaFor(base)?.max_dose ?? 0).toFixed(2)} Gy
                  </p>
                </div>
              ))}
              {diffPlane && (
                <div className="bg-clinical-surface border border-clinical-border rounded-lg p-3">
                  <p className="text-xs font-medium text-clinical-text mb-2">
                    Difference · MC − TPS
                    {field !== "summed" && (
                      <span className="text-clinical-muted font-normal"> · {beamLabel(field as number)}</span>
                    )}
                  </p>
                  <DoseMapCanvas
                    plane={diffPlane}
                    mode="diff"
                    scaleMax={scaleMax}
                    backdrop={showCT ? ctPlane : null}
                    size={800}
                    fitVh={33}
                  />
                  <DiffColorbar className="mt-2" scaleMax={scaleMax} />
                  <p className="text-[10px] text-clinical-muted mt-2">
                    red = MC hot · blue = MC cold · saturates ±10% of max ·
                    &lt;2% hidden · max |Δ| {diffPlane.maxAbs.toFixed(2)} Gy
                  </p>
                </div>
              )}
              {info.available_comparisons.includes("mcSquare_vs_TPS") && (
                <div className="bg-clinical-surface border border-clinical-border rounded-lg p-3">
                  <p className="text-xs font-medium text-clinical-text mb-2">
                    Gamma · MCsquare vs TPS
                    <span className="text-clinical-muted font-normal"> · summed, this slice</span>
                  </p>
                  <DoseMapCanvas
                    plane={gammaMcTps}
                    mode="gamma"
                    backdrop={showCT ? ctPlane : null}
                    size={800}
                    fitVh={33}
                  />
                  <GammaColorbar className="mt-2" />
                  <p className="text-[10px] text-clinical-muted mt-2">
                    γ plane passing:{" "}
                    {gammaMcTps?.passingRate !== undefined
                      ? `${gammaMcTps.passingRate!.toFixed(1)}%`
                      : "computing…"}
                    {" "}· green γ ≤ 1 · red γ &gt; 1
                  </p>
                </div>
              )}
            </div>
          ) : (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              {info.available_comparisons.map((c) => (
                <div key={c} className="bg-clinical-surface border border-clinical-border rounded-lg p-3">
                  <p className="text-xs font-medium text-clinical-text mb-2">
                    {COMPARISON_LABELS[c]}
                  </p>
                  <DoseMapCanvas
                    plane={gammaPlanes[c] ?? null}
                    mode="gamma"
                    backdrop={showCT ? ctPlane : null}
                    size={800}
                    fitVh={33}
                  />
                  <p className="text-[10px] text-clinical-muted mt-2">
                    γ plane passing:{" "}
                    {gammaPlanes[c]?.passingRate !== undefined
                      ? `${gammaPlanes[c]!.passingRate!.toFixed(1)}%`
                      : "—"}
                  </p>
                </div>
              ))}
            </div>
          )}

          {/* Depth slider */}
          <div className="mt-3">
            <input
              type="range"
              min={0}
              max={Math.max(0, nPlanes - 1)}
              value={zUI}
              onChange={(e) => setZUI(parseInt(e.target.value, 10))}
              className="w-full accent-clinical-accent"
            />
          </div>

          {/* Gamma legend (gamma view) */}
          {view === "gamma" && (
            <div className="flex items-center gap-4 mt-3 text-[10px] text-clinical-muted">
              <span className="flex items-center gap-1">
                <span className="inline-block w-3 h-3 rounded-sm" style={{ background: "rgb(40,200,60)" }} />
                γ ≤ 1 (pass)
              </span>
              <span className="flex items-center gap-1">
                <span className="inline-block w-3 h-3 rounded-sm" style={{ background: "rgb(220,40,30)" }} />
                γ &gt; 1 (fail)
              </span>
              <span className="flex items-center gap-1">
                <span className="inline-block w-3 h-3 rounded-sm bg-clinical-bg border border-clinical-border" />
                below threshold
              </span>
            </div>
          )}
        </div>

        {/* Sidebar */}
        <div className="space-y-4">
          <div className="bg-clinical-surface border border-clinical-border rounded-lg p-4">
            <p className="text-xs font-medium text-clinical-muted uppercase tracking-wider mb-3">
              Verdict
            </p>
            <StatusBadge status={info.verdict} />
          </div>

          <div className="bg-clinical-surface border border-clinical-border rounded-lg p-4">
            <p className="text-xs font-medium text-clinical-muted uppercase tracking-wider mb-3">
              Gamma passing rates
              {field !== "summed" && (
                <span className="normal-case text-clinical-text"> · {beamLabel(field as number)}</span>
              )}
            </p>
            {hasResults ? (
              <div className="space-y-4">
                {(field === "summed" ? results : fieldResults).length > 0 ? (
                  COMPARISON_ORDER.filter((c) =>
                    (field === "summed" ? results : fieldResults).some(
                      (r) => r.comparison_type === c
                    )
                  ).map((c) => {
                    const r = (field === "summed" ? results : fieldResults).find(
                      (x) => x.comparison_type === c
                    )!;
                    return <GammaBar key={c} result={r} />;
                  })
                ) : (
                  <p className="text-xs text-clinical-muted">
                    No per-field result stored for this beam.
                  </p>
                )}
              </div>
            ) : (
              <p className="text-xs text-clinical-muted">
                No gamma results yet. Run the analysis to compute passing rates.
              </p>
            )}
          </div>

          <button
            onClick={handleRunGamma}
            disabled={running || !hasComparisons}
            className="w-full flex items-center justify-center gap-2 bg-clinical-accent hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed text-white px-4 py-2 rounded-md text-sm font-medium transition-colors"
          >
            {running ? (
              <>
                <Loader2 size={14} className="animate-spin" /> Analysing…
              </>
            ) : (
              <>
                <Play size={14} /> {hasResults ? "Re-run gamma analysis" : "Run gamma analysis"}
              </>
            )}
          </button>
          {!hasComparisons && (
            <p className="text-[11px] text-clinical-muted">
              Run MCsquare and/or log reconstruction first to enable comparisons.
            </p>
          )}
        </div>
      </div>

      {/* openMCsquare Robustness Analysis & DVH Predictions */}
      <div className="w-full px-6 pb-8">
        <RobustnessDVHCard
          planId={id}
          hasMCDose={info.sources.some((s) => s.source.startsWith("mcSquare"))}
        />
      </div>
    </div>
  );
}
