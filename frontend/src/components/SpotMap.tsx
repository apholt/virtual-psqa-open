/*
 * SpotMap.tsx -- per-spot delivered-vs-prescribed detail for one beam-fraction.
 *
 * Every spot is drawn at its PLANNED position and coloured by a chosen
 * deviation metric. The point of plotting position rather than a histogram is
 * spatial structure: a uniform translation, a radial scaling, an edge-weighted
 * magnet nonlinearity and pure random scatter all produce the same mean radial
 * error and mean entirely different things about the machine. Only the map
 * distinguishes them.
 *
 * Fetches GET /api/plans/{id}/fractions/{fx}/spots?beam=NAME, which re-runs
 * reconstruction on demand -- expect a few seconds.
 */
import { useEffect, useMemo, useState } from "react";

type Spots = {
  plan_id: number;
  plan_label: string;
  fraction: number;
  beam: string;
  machine: string | null;
  treatment_date: string | null;
  n_spots: number;
  x: number[];
  y: number[];
  energy: number[];
  layer: number[];
  dx: number[];
  dy: number[];
  rx_mu: number[];
  dv_mu: number[];
  mu_err: number[];
  mu_err_pct: number[];
  size_rx_x: number[];
  size_rx_y: number[];
  size_dv_x: number[];
  size_dv_y: number[];
};

type FractionList = {
  fractions: { fraction_number: number; delivery_date: string | null;
               machine: string | null; qa_status: string }[];
  beams: string[];
};

/** Grid-warp exaggeration for the transform panel. At 60x a 5 mrad rotation
 *  tilts the grid ~17 degrees -- unmistakable without being cartoonish. */
const GRID_EXAG = 60;

/** Ceiling on plot size. The plot is square (560x560 viewBox) and scales to
 *  its container, so once this card went full width an uncapped 100% produced
 *  a plot taller than the screen. Capping width keeps it square. */
const PLOT_MAX_PX = 380;

const P = {
  text: "#1c1c1c", muted: "#6b6b6b", faint: "#9a9a9a", rule: "#e8e8e8",
  cool: "#2e5a88", warm: "#c0392b", mid: "#e8e8e8",
};

type MetricKey = "radial" | "dx" | "dy" | "mu" | "muabs" | "rxmu" | "size";

const METRICS: { key: MetricKey; label: string; unit: string;
                 diverging: boolean }[] = [
  { key: "radial", label: "Radial deviation", unit: "mm", diverging: false },
  { key: "dx", label: "dx", unit: "mm", diverging: true },
  { key: "dy", label: "dy", unit: "mm", diverging: true },
  { key: "mu", label: "MU error (%)", unit: "%", diverging: true },
  { key: "muabs", label: "MU error (absolute)", unit: "MU", diverging: true },
  { key: "rxmu", label: "Planned MU", unit: "MU", diverging: false },
  { key: "size", label: "Spot size change", unit: "mm", diverging: true },
];

/** MU line for tooltips: planned and delivered in absolute MU, then the
 *  relative error. Percentage alone is misleading near the machine minimum,
 *  where a 17% error can be 0.004 MU. */
function muLine(d: Spots, i: number): string {
  const rx = d.rx_mu[i];
  const dv = d.dv_mu?.[i];
  const abs = d.mu_err?.[i];
  if (dv == null || abs == null) {
    return `MU ${rx} planned (${d.mu_err_pct[i]}%)`;
  }
  return (
    `MU planned ${rx}, delivered ${dv}\n` +
    `MU error ${abs >= 0 ? "+" : ""}${abs} (${d.mu_err_pct[i]}%)`
  );
}

/** Percentile without sorting the caller's array. */
function pct(vals: number[], q: number): number {
  if (!vals.length) return 0;
  const s = [...vals].sort((a, b) => a - b);
  return s[Math.min(s.length - 1, Math.max(0, Math.floor(q * (s.length - 1))))];
}

function colorFor(v: number, lo: number, hi: number, diverging: boolean): string {
  if (diverging) {
    const m = Math.max(Math.abs(lo), Math.abs(hi)) || 1;
    const t = Math.max(-1, Math.min(1, v / m));
    // Blue for negative, red for positive, pale in the middle.
    const a = Math.abs(t);
    return t >= 0
      ? `rgba(192,57,43,${0.15 + 0.75 * a})`
      : `rgba(46,90,136,${0.15 + 0.75 * a})`;
  }
  const t = hi > lo ? Math.max(0, Math.min(1, (v - lo) / (hi - lo))) : 0;
  return `rgba(192,57,43,${0.12 + 0.78 * t})`;
}

export default function SpotMap({ planId }: { planId: number }) {
  const [list, setList] = useState<FractionList | null>(null);
  const [fx, setFx] = useState<number | null>(null);
  const [beam, setBeam] = useState<string | null>(null);
  const [data, setData] = useState<Spots | null>(null);
  const [metric, setMetric] = useState<MetricKey>("radial");
  const [layer, setLayer] = useState<number | "all">("all");
  const [mode, setMode] = useState<"grid" | "colour" | "vector">("grid");
  const [mag, setMag] = useState(50);
  // AFFINE_V1 -- show the deviation field minus its fitted transform.
  const [residual, setResidual] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    fetch(`/api/plans/${planId}/fractions`)
      .then((r) => r.json())
      .then((j: FractionList) => {
        if (!live) return;
        setList(j);
        if (j.fractions.length) setFx(j.fractions[0].fraction_number);
        if (j.beams.length) setBeam(j.beams[0]);
      })
      .catch(() => live && setErr("Could not list fractions."));
    return () => { live = false; };
  }, [planId]);

  useEffect(() => {
    if (fx == null || !beam) return;
    let live = true;
    setBusy(true);
    setErr(null);
    fetch(`/api/plans/${planId}/fractions/${fx}/spots?beam=${encodeURIComponent(beam)}`)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((j) => { if (live) { setData(j); setLayer("all"); setBusy(false); } })
      .catch((e) => { if (live) { setErr(String(e)); setBusy(false); } });
    return () => { live = false; };
  }, [planId, fx, beam]);

  /**
   * AFFINE_V1 -- least-squares affine fit of the deviation field:
   *   dx = tx + a*x + b*y,   dy = ty + c*x + d*y
   * decomposed as translation (tx, ty), rotation (c - b)/2, and scale (a, d).
   *
   * Measured on this machine the deviation field is ~96% a fixed geometric
   * transform (about 5.4 mrad rotation, present in the raw strip-chamber data
   * and confirmed by the vendor report) and ~4% scatter. Whether a delivery
   * is NORMAL is a question about the scatter, not the transform, so the
   * residual view subtracts the fit. The 3x3 normal-equation solve is exact
   * and cheap; no library needed.
   */
  const fit = useMemo(() => {
    if (!data || data.x.length < 8) return null;
    const X = data.x, Y = data.y, n = X.length;
    let sx = 0, sy = 0, sxx = 0, syy = 0, sxy = 0;
    for (let i = 0; i < n; i++) {
      sx += X[i]; sy += Y[i];
      sxx += X[i] * X[i]; syy += Y[i] * Y[i]; sxy += X[i] * Y[i];
    }
    // Solve A^T A c = A^T b for A = [1 x y] via explicit 3x3 inverse.
    const M = [[n, sx, sy], [sx, sxx, sxy], [sy, sxy, syy]];
    const det =
      M[0][0] * (M[1][1] * M[2][2] - M[1][2] * M[2][1]) -
      M[0][1] * (M[1][0] * M[2][2] - M[1][2] * M[2][0]) +
      M[0][2] * (M[1][0] * M[2][1] - M[1][1] * M[2][0]);
    if (Math.abs(det) < 1e-9) return null;
    const inv = [
      [(M[1][1] * M[2][2] - M[1][2] * M[2][1]) / det,
       (M[0][2] * M[2][1] - M[0][1] * M[2][2]) / det,
       (M[0][1] * M[1][2] - M[0][2] * M[1][1]) / det],
      [(M[1][2] * M[2][0] - M[1][0] * M[2][2]) / det,
       (M[0][0] * M[2][2] - M[0][2] * M[2][0]) / det,
       (M[0][2] * M[1][0] - M[0][0] * M[1][2]) / det],
      [(M[1][0] * M[2][1] - M[1][1] * M[2][0]) / det,
       (M[0][1] * M[2][0] - M[0][0] * M[2][1]) / det,
       (M[0][0] * M[1][1] - M[0][1] * M[1][0]) / det],
    ];
    const solve = (d: number[]) => {
      let b0 = 0, b1 = 0, b2 = 0;
      for (let i = 0; i < n; i++) { b0 += d[i]; b1 += X[i] * d[i]; b2 += Y[i] * d[i]; }
      return [
        inv[0][0] * b0 + inv[0][1] * b1 + inv[0][2] * b2,
        inv[1][0] * b0 + inv[1][1] * b1 + inv[1][2] * b2,
        inv[2][0] * b0 + inv[2][1] * b1 + inv[2][2] * b2,
      ];
    };
    const cx = solve(data.dx), cy = solve(data.dy);
    const px = (x: number, y: number) => cx[0] + cx[1] * x + cx[2] * y;
    const py = (x: number, y: number) => cy[0] + cy[1] * x + cy[2] * y;
    let rs = 0, rmax = 0;
    for (let i = 0; i < n; i++) {
      const r = Math.hypot(data.dx[i] - px(X[i], Y[i]), data.dy[i] - py(X[i], Y[i]));
      rs += r; if (r > rmax) rmax = r;
    }
    return {
      tx: cx[0], ty: cy[0],
      rotMrad: 1000 * (cy[1] - cx[2]) / 2,
      scaleX: 100 * cx[1], scaleY: 100 * cy[2],
      residMean: rs / n, residMax: rmax,
      px, py,
    };
  }, [data]);

  /** Effective deviations: raw, or residual after removing the fit. */
  const eff = useMemo(() => {
    if (!data) return null;
    if (!residual || !fit) return { dx: data.dx, dy: data.dy };
    return {
      dx: data.dx.map((d, i) => d - fit.px(data.x[i], data.y[i])),
      dy: data.dy.map((d, i) => d - fit.py(data.x[i], data.y[i])),
    };
  }, [data, residual, fit]);

  const values = useMemo<number[]>(() => {
    if (!data) return [];
    const DX = eff?.dx ?? data.dx, DY = eff?.dy ?? data.dy;
    switch (metric) {
      case "radial":
        return DX.map((d, i) => Math.hypot(d, DY[i]));
      case "dx": return DX;
      case "dy": return DY;
      case "mu": return data.mu_err_pct;
      case "muabs": return data.mu_err ?? [];
      case "rxmu": return data.rx_mu;
      case "size":
        if (!data.size_dv_x.length) return [];
        return data.size_dv_x.map((v, i) => v - data.size_rx_x[i]);
    }
  }, [data, metric, eff]);


  /** Distinct energy layers, with a representative energy and spot count.
   *  A PBS field paints one layer per energy, so overlaying all of them puts
   *  ~30 planes on one plot and hides any layer-specific structure. */
  const layers = useMemo(() => {
    if (!data) return [] as { layer: number; energy: number; n: number }[];
    const m = new Map<number, { layer: number; energy: number; n: number }>();
    data.layer.forEach((L, i) => {
      const e = m.get(L);
      if (e) e.n += 1;
      else m.set(L, { layer: L, energy: data.energy[i], n: 1 });
    });
    return [...m.values()].sort((a, b) => a.layer - b.layer);
  }, [data]);

  /** Indices of the spots currently shown. */
  const idxs = useMemo(() => {
    if (!data) return [] as number[];
    if (layer === "all") return data.layer.map((_v, i) => i);
    return data.layer.reduce<number[]>((acc, L, i) => {
      if (L === layer) acc.push(i);
      return acc;
    }, []);
  }, [data, layer]);

  const layerPos = layers.findIndex((l) => l.layer === layer);
  const stepLayer = (d: number) => {
    if (!layers.length) return;
    if (layer === "all") { setLayer(layers[d > 0 ? 0 : layers.length - 1].layer); return; }
    const next = layerPos + d;
    if (next < 0 || next >= layers.length) return;
    setLayer(layers[next].layer);
  };

  /** Vector mode draws two SVG nodes per spot, so a 22k-spot all-layer view
   *  becomes unresponsive. Thin the set above a threshold and say so, rather
   *  than silently rendering a fraction or silently hanging the page. */
  const VEC_MAX = 4000;
  const vecStride = Math.max(1, Math.ceil(idxs.length / VEC_MAX));
  const vecIdxs = useMemo(
    () => (vecStride === 1 ? idxs : idxs.filter((_v, k) => k % vecStride === 0)),
    [idxs, vecStride],
  );

  /** Radial magnitudes for colouring vectors, independent of the metric
   *  selector (which is hidden in vector mode). */
  const radial = useMemo(() => {
    if (!data) return [] as number[];
    const DX = eff?.dx ?? data.dx, DY = eff?.dy ?? data.dy;
    return idxs.map((i) => Math.hypot(DX[i], DY[i]));
  }, [data, idxs, eff]);
  const radLo = pct(radial, 0.02);
  const radHi = pct(radial, 0.98);


  const def = METRICS.find((m) => m.key === metric)!;
  // Clip the scale at the 2nd/98th percentile so a handful of extreme spots
  // do not flatten the colour range for the other three thousand.
  // Scale from the VISIBLE spots, so stepping into a layer rescales to that
  // layer's range instead of being flattened by the whole field.
  const shown = useMemo(() => idxs.map((i) => values[i]).filter(
    (v) => v != null && Number.isFinite(v)), [idxs, values]);
  const lo = def.diverging ? -Math.abs(pct(shown, 0.02)) : pct(shown, 0.02);
  const hi = def.diverging
    ? Math.max(Math.abs(pct(shown, 0.02)), Math.abs(pct(shown, 0.98)))
    : pct(shown, 0.98);

  const W = 560, H = 560, pad = 34;
  // Extent stays fixed to the whole field even when one layer is shown, so
  // layers can be compared against each other rather than each rescaling.
  const ext = useMemo(() => {
    if (!data || !data.x.length) return 130;
    return Math.max(
      10,
      Math.max(...data.x.map(Math.abs), ...data.y.map(Math.abs)) * 1.08,
    );
  }, [data]);
  /**
   * GRID_V1 -- per-spot residual magnitudes for the scatter panel, and the
   * count beyond the 2.0 mm action tolerance. The transform panel needs no
   * per-spot data at all: it draws the FIT, which is why it is clean by
   * construction. This machine's measured split: TR2 Music City carries a
   * stable ~5.0 mrad rotation and +0.5% Y scale (present in the raw strip-
   * chamber data, in the vendor's own report, and on phantom deliveries);
   * TR1 Franklin carries essentially none. The excursion window on plan 8
   * (fx15 onward) lives entirely in the residual, not the transform.
   */
  const residMag = useMemo(() => {
    if (!data || !fit) return [];
    return data.dx.map((d, i) => Math.hypot(
      d - fit.px(data.x[i], data.y[i]),
      data.dy[i] - fit.py(data.x[i], data.y[i])));
  }, [data, fit]);
  // Map the shown spots' residuals directly. The previous form filtered
  // residMag with `idxs.includes(i)`, but Array.filter passes the index into
  // the array being filtered, not the original -- so the wrong elements were
  // selected and the colour scale was set from near-zero values, painting
  // almost every spot at full saturation.
  const residHi = useMemo(() => {
    const shownMags = idxs.map((i) => residMag[i]).filter((v) => v != null);
    return Math.max(pct(shownMags, 0.98), 0.05);
  }, [idxs, residMag]);
  const POS_TOL_MM = 2.0;
  const nBeyondTol = useMemo(
    () => idxs.reduce((a, i) => a + (residMag[i] > POS_TOL_MM ? 1 : 0), 0),
    [idxs, residMag]);

  const sx = (v: number) => pad + ((v + ext) / (2 * ext)) * (W - 2 * pad);
  const sy = (v: number) => H - pad - ((v + ext) / (2 * ext)) * (H - 2 * pad);

  const stats = useMemo(() => {
    if (!shown.length) return null;
    const mean = shown.reduce((a, b) => a + b, 0) / shown.length;
    const sorted = [...shown].sort((a, b) => a - b);
    return {
      mean,
      p50: sorted[Math.floor(sorted.length / 2)],
      p95: sorted[Math.floor(sorted.length * 0.95)],
      max: sorted[sorted.length - 1],
      min: sorted[0],
    };
  }, [shown]);

  const sel: React.CSSProperties = {
    font: "inherit", fontSize: 12, padding: "3px 6px",
    border: `1px solid ${P.rule}`, borderRadius: 6, background: "#fff",
    color: P.text,
  };

  return (
    <div style={card}>
      <div style={{ display: "flex", gap: 10, alignItems: "center",
                    flexWrap: "wrap", marginBottom: 10 }}>
        <span style={{ fontSize: 10.5, letterSpacing: 0.7, color: P.faint,
                       textTransform: "uppercase", marginRight: "auto" }}>
          Spot deviation map
        </span>
        <select style={sel} value={fx ?? ""} disabled={!list}
                onChange={(e) => setFx(Number(e.target.value))}>
          {list?.fractions.map((f) => (
            <option key={f.fraction_number} value={f.fraction_number}>
              fx {f.fraction_number}
              {f.delivery_date ? ` \u00b7 ${f.delivery_date}` : ""}
            </option>
          ))}
        </select>
        <select style={sel} value={beam ?? ""} disabled={!list}
                onChange={(e) => setBeam(e.target.value)}>
          {list?.beams.map((b) => <option key={b} value={b}>{b}</option>)}
        </select>
        <select style={sel} value={mode}
                onChange={(e) => setMode(
                  e.target.value as "grid" | "colour" | "vector")}>
          <option value="grid">Transform + scatter</option>
          <option value="colour">Colour</option>
          <option value="vector">Vectors</option>
        </select>
        {fit && mode !== "grid" && (
          <button
            onClick={() => setResidual((v) => !v)}
            title={residual
              ? "Showing scatter after removing the fitted transform"
              : "Showing the raw deviation field"}
            style={{ ...sel, cursor: "pointer",
                     background: residual ? "#f0ede4" : "#fff" }}>
            {residual ? "Residual" : "Raw"}
          </button>
        )}
        {mode === "colour" ? (
          <select style={sel} value={metric}
                  onChange={(e) => setMetric(e.target.value as MetricKey)}>
            {METRICS.map((m) => (
              <option key={m.key} value={m.key}>{m.label}</option>
            ))}
          </select>
        ) : (
          <select style={sel} value={mag}
                  onChange={(e) => setMag(Number(e.target.value))}>
            {[10, 25, 50, 100, 200, 400].map((m) => (
              <option key={m} value={m}>{m}{"\u00d7"}</option>
            ))}
          </select>
        )}

      </div>

      {layers.length > 1 && (
        <div style={{ display: "flex", gap: 8, alignItems: "center",
                      marginBottom: 8, fontSize: 12, color: P.muted }}>
          <button onClick={() => stepLayer(-1)} disabled={layerPos <= 0}
                  aria-label="previous layer" style={stepBtn}>
            {"\u25c0"}
          </button>
          <select style={{ ...sel, minWidth: 190 }} value={String(layer)}
                  onChange={(e) => setLayer(
                    e.target.value === "all" ? "all" : Number(e.target.value))}>
            <option value="all">
              All {layers.length} layers ({data?.n_spots ?? 0} spots)
            </option>
            {layers.map((l) => (
              <option key={l.layer} value={l.layer}>
                Layer {l.layer + 1} {"\u00b7"} {l.energy.toFixed(1)} MeV
                {" "}({l.n} spots)
              </option>
            ))}
          </select>
          <button onClick={() => stepLayer(1)}
                  disabled={layer !== "all" && layerPos >= layers.length - 1}
                  aria-label="next layer" style={stepBtn}>
            {"\u25b6"}
          </button>
          {layer !== "all" && (
            <button onClick={() => setLayer("all")} style={{ ...stepBtn,
                    width: "auto", padding: "2px 8px" }}>
              all
            </button>
          )}
          <span style={{ color: P.faint }}>
            {layer === "all"
              ? "all layers overlaid"
              : `showing ${idxs.length} of ${data?.n_spots ?? 0} spots`}
          </span>
        </div>
      )}

      {err && <div style={{ fontSize: 12, color: P.warm }}>{err}</div>}
      {busy && (
        <div style={{ fontSize: 12, color: P.muted, padding: "40px 0",
                      textAlign: "center" }}>
          Reconstructing {beam} fx {fx}{"\u2026"}
        </div>
      )}

      {!busy && data && fit && mode !== "grid" && (
        <div style={{ display: "flex", gap: 14, flexWrap: "wrap",
                      fontSize: 11.5, color: P.muted, margin: "2px 0 8px",
                      padding: "7px 10px", background: "#faf9f5",
                      borderRadius: 8 }}>
          <span>rotation{" "}
            <b style={{ color: Math.abs(fit.rotMrad) > 2 ? "#8a6d1a" : undefined }}>
              {fit.rotMrad >= 0 ? "+" : ""}{fit.rotMrad.toFixed(2)}
            </b> mrad</span>
          <span>scale{" "}
            <b>{fit.scaleX >= 0 ? "+" : ""}{fit.scaleX.toFixed(3)},{" "}
               {fit.scaleY >= 0 ? "+" : ""}{fit.scaleY.toFixed(3)}</b> %</span>
          <span>residual{" "}
            <b>{fit.residMean.toFixed(3)}</b> mean mm</span>
          {residual && <span style={{ color: "#8a6d1a" }}>
            showing residual field</span>}
        </div>
      )}

      {!busy && data && fit && mode === "grid" && (
        <div style={{ display: "grid",
                      gridTemplateColumns: "minmax(0,1fr) minmax(0,1fr)",
                      gap: 12, alignItems: "start" }}>
          {/* -------- left: the transform, drawn as a warped grid -------- */}
          <div>
            <div style={{ fontSize: 12, fontWeight: 500 }}>The transform</div>
            <div style={{ fontSize: 11, color: P.faint, margin: "1px 0 6px" }}>
              Fitted geometry, exaggerated {GRID_EXAG}{"\u00d7"}
              {data.machine ? ` \u00b7 ${data.machine}` : ""}
            </div>
            <svg viewBox={`0 0 ${W} ${H}`} role="img"
                 aria-label="Prescribed grid and fitted delivered grid"
                 style={{ width: "100%", height: "auto", display: "block",
                          maxWidth: PLOT_MAX_PX, margin: "0 auto" }}>
              <rect x={pad} y={pad} width={W - 2 * pad} height={H - 2 * pad}
                    fill="none" stroke={P.rule} />
              {(() => {
                const NL = 6;
                const step = ext / NL;
                const els: React.ReactNode[] = [];
                // prescribed grid
                for (let i = -NL; i <= NL; i++) {
                  els.push(<line key={`px${i}`} x1={sx(i * step)} y1={sy(-ext)}
                                 x2={sx(i * step)} y2={sy(ext)}
                                 stroke={P.rule} strokeWidth={1} />);
                  els.push(<line key={`py${i}`} x1={sx(-ext)} y1={sy(i * step)}
                                 x2={sx(ext)} y2={sy(i * step)}
                                 stroke={P.rule} strokeWidth={1} />);
                }
                // fitted grid, exaggerated
                const warp = (x: number, y: number): [number, number] => [
                  x + fit.px(x, y) * GRID_EXAG,
                  y + fit.py(x, y) * GRID_EXAG,
                ];
                for (let i = -NL; i <= NL; i++) {
                  const v: string[] = [], h: string[] = [];
                  for (let j = -NL; j <= NL; j++) {
                    const [ax, ay] = warp(i * step, j * step);
                    v.push(`${sx(ax)},${sy(ay)}`);
                    const [bx, by] = warp(j * step, i * step);
                    h.push(`${sx(bx)},${sy(by)}`);
                  }
                  els.push(<polyline key={`wx${i}`} points={v.join(" ")}
                                     fill="none" stroke="#2a78d6"
                                     strokeWidth={1.5} />);
                  els.push(<polyline key={`wy${i}`} points={h.join(" ")}
                                     fill="none" stroke="#2a78d6"
                                     strokeWidth={1.5} />);
                }
                return els;
              })()}
            </svg>
            <div style={{ display: "flex", gap: 14, fontSize: 11,
                          color: P.faint, marginTop: 4 }}>
              <span style={{ display: "inline-flex", alignItems: "center",
                             gap: 5 }}>
                <span style={{ width: 14, height: 2, background: P.rule,
                               display: "inline-block" }} />prescribed</span>
              <span style={{ display: "inline-flex", alignItems: "center",
                             gap: 5 }}>
                <span style={{ width: 14, height: 2, background: "#2a78d6",
                               display: "inline-block" }} />as delivered (fit)</span>
            </div>
            <div style={{ fontSize: 11.5, color: P.muted, marginTop: 6,
                          lineHeight: 1.6 }}>
              rotation <b style={{ color: Math.abs(fit.rotMrad) > 2
                                   ? "#8a6d1a" : undefined }}>
                {fit.rotMrad >= 0 ? "+" : ""}{fit.rotMrad.toFixed(2)} mrad</b>
              {" \u00b7 "}scale <b>{fit.scaleX >= 0 ? "+" : ""}
                {fit.scaleX.toFixed(3)}, {fit.scaleY >= 0 ? "+" : ""}
                {fit.scaleY.toFixed(3)}%</b>
              {" \u00b7 "}translation <b>{fit.tx >= 0 ? "+" : ""}
                {fit.tx.toFixed(3)}, {fit.ty >= 0 ? "+" : ""}
                {fit.ty.toFixed(3)} mm</b>
            </div>
          </div>

          {/* -------- right: the scatter, per-spot residual -------- */}
          <div>
            <div style={{ fontSize: 12, fontWeight: 500 }}>The scatter</div>
            <div style={{ fontSize: 11, color: P.faint, margin: "1px 0 6px" }}>
              Per-spot residual after removing the transform
            </div>
            <svg viewBox={`0 0 ${W} ${H}`} role="img"
                 aria-label="Per-spot residual deviation"
                 style={{ width: "100%", height: "auto", display: "block",
                          maxWidth: PLOT_MAX_PX, margin: "0 auto" }}>
              <rect x={pad} y={pad} width={W - 2 * pad} height={H - 2 * pad}
                    fill="none" stroke={P.rule} />
              <line x1={sx(0)} x2={sx(0)} y1={pad} y2={H - pad}
                    stroke={P.rule} strokeDasharray="3 3" />
              <line x1={pad} x2={W - pad} y1={sy(0)} y2={sy(0)}
                    stroke={P.rule} strokeDasharray="3 3" />
              {idxs.map((i) => {
                const m = residMag[i];
                const out = m > POS_TOL_MM;
                return (
                  <g key={i}>
                    <circle cx={sx(data.x[i])} cy={sy(data.y[i])}
                            r={layer === "all" ? 2.1 : 3}
                            fill={colorFor(m, 0, residHi, false)} />
                    {out && (
                      <circle cx={sx(data.x[i])} cy={sy(data.y[i])} r={6}
                              fill="none" stroke="#c0392b" strokeWidth={1.5} />
                    )}
                    <title>
                      {`x ${data.x[i]} y ${data.y[i]} mm\n` +
                       `residual ${m.toFixed(3)} mm` +
                       (out ? ` \u2014 beyond ${POS_TOL_MM} mm tolerance` : "") +
                       `\nE ${data.energy[i]} MeV (layer ${data.layer[i] + 1})\n` +
                       muLine(data, i)}
                    </title>
                  </g>
                );
              })}
            </svg>
            <div style={{ display: "flex", alignItems: "center", gap: 8,
                          marginTop: 4, fontSize: 11, color: P.faint }}>
              <span>0</span>
              <span style={{ flex: 1, height: 8, borderRadius: 4,
                             background: `linear-gradient(90deg, rgba(192,57,43,0.10), rgba(192,57,43,0.9))` }} />
              <span>{residHi.toFixed(2)} mm</span>
              <span style={{ display: "inline-flex", alignItems: "center",
                             gap: 5, marginLeft: 10 }}>
                <span style={{ width: 9, height: 9,
                               border: "1.5px solid #c0392b",
                               borderRadius: "50%",
                               display: "inline-block" }} />
                {"> "}{POS_TOL_MM} mm
              </span>
            </div>
            <div style={{ fontSize: 11.5, color: P.muted, marginTop: 6,
                          lineHeight: 1.6 }}>
              residual <b>{fit.residMean.toFixed(3)}</b> mean
              {" \u00b7 "}<b>{fit.residMax.toFixed(3)}</b> max mm
              {" \u00b7 "}
              <b style={{ color: nBeyondTol > 0 ? "#c0392b" : undefined }}>
                {nBeyondTol}
              </b> spot{nBeyondTol === 1 ? "" : "s"} beyond {POS_TOL_MM} mm
            </div>
          </div>
        </div>
      )}

      {!busy && data && shown.length > 0 && mode !== "grid" && (
        <>
          <svg viewBox={`0 0 ${W} ${H}`} role="img"
               aria-label={`Spot deviation map, beam ${data.beam}`}
               style={{ width: "100%", height: "auto", display: "block",
                          maxWidth: PLOT_MAX_PX, margin: "0 auto" }}>
            <rect x={pad} y={pad} width={W - 2 * pad} height={H - 2 * pad}
                  fill="none" stroke={P.rule} />
            <line x1={sx(0)} x2={sx(0)} y1={pad} y2={H - pad}
                  stroke={P.rule} strokeDasharray="3 3" />
            <line x1={pad} x2={W - pad} y1={sy(0)} y2={sy(0)}
                  stroke={P.rule} strokeDasharray="3 3" />
            {mode === "vector" && vecIdxs.map((i) => {
              const x0 = sx(data.x[i]);
              const y0 = sy(data.y[i]);
              const DX = eff?.dx ?? data.dx, DY = eff?.dy ?? data.dy;
              const x1 = sx(data.x[i] + DX[i] * mag);
              const y1 = sy(data.y[i] + DY[i] * mag);
              const r = Math.hypot(DX[i], DY[i]);
              const col = colorFor(r, radLo, radHi, false);
              // Arrowhead. Without one the line is direction-ambiguous, and
              // direction is the whole reason for this mode -- a tail dot
              // alone is far too small to read at this density.
              const len = Math.hypot(x1 - x0, y1 - y0);
              const ux = len > 0.01 ? (x1 - x0) / len : 0;
              const uy = len > 0.01 ? (y1 - y0) / len : 0;
              const h = Math.min(4, Math.max(1.8, len * 0.32));
              const w = h * 0.55;
              const head = len > 1.2
                ? `${x1},${y1} ${x1 - h * ux + w * uy},${y1 - h * uy - w * ux} `
                  + `${x1 - h * ux - w * uy},${y1 - h * uy + w * ux}`
                : null;
              return (
                <g key={i}>
                  <line x1={x0} y1={y0} x2={x1} y2={y1}
                        stroke={col} strokeWidth={0.9} strokeLinecap="round" />
                  {head && <polygon points={head} fill={col} />}
                  <circle cx={x0} cy={y0} r={1.3} fill="#555" />
                  <title>
                    {`x ${data.x[i]} y ${data.y[i]} mm\n` +
                     `dx ${data.dx[i]} dy ${data.dy[i]} mm (${r.toFixed(3)} mm)\n` +
                     `E ${data.energy[i]} MeV (layer ${data.layer[i] + 1})\n` +
                     muLine(data, i)}
                  </title>
                </g>
              );
            })}
            {mode === "colour" && idxs.map((i) => (
              <circle key={i} cx={sx(data.x[i])} cy={sy(data.y[i])}
                      r={layer === "all" ? 2.1 : 3}
                      fill={colorFor(values[i], lo, hi, def.diverging)}>
                <title>
                  {`x ${data.x[i]} y ${data.y[i]} mm\n` +
                   `E ${data.energy[i]} MeV (layer ${data.layer[i] + 1})\n` +
                   `dx ${data.dx[i]} dy ${data.dy[i]} mm\n` +
                   muLine(data, i)}
                </title>
              </circle>
            ))}
            <text x={sx(0)} y={H - pad + 14} textAnchor="middle"
                  fontSize={9} fill={P.faint}>x (mm)</text>
            <text x={pad - 8} y={sy(0)} textAnchor="end" fontSize={9}
                  fill={P.faint} dominantBaseline="middle">y</text>
            <text x={pad} y={H - pad + 14} fontSize={9} fill={P.faint}>
              {(-ext).toFixed(0)}
            </text>
            <text x={W - pad} y={H - pad + 14} textAnchor="end" fontSize={9}
                  fill={P.faint}>{ext.toFixed(0)}</text>
          </svg>

          {mode === "vector" && (
            <div style={{ display: "flex", alignItems: "center", gap: 10,
                          marginTop: 6, fontSize: 11, color: P.faint }}>
              <svg width={76} height={12} aria-hidden>
                {(() => {
                  const L = Math.min(64, (1 * mag * (W - 2 * pad)) / (2 * ext));
                  return (
                    <>
                      <line x1={3} y1={6} x2={3 + L} y2={6}
                            stroke={P.warm} strokeWidth={1.4} />
                      <polygon points={`${3 + L},6 ${3 + L - 4},3.8 ${3 + L - 4},8.2`}
                               fill={P.warm} />
                      <circle cx={3} cy={6} r={1.8} fill="#555" />
                    </>
                  );
                })()}
              </svg>
              <span>
                1 mm at {mag}{"\u00d7"}. Dot = planned position, arrowhead =
                where the spot landed.
              </span>
              {vecStride > 1 && (
                <span style={{ color: P.warm }}>
                  showing every {vecStride}
                  {vecStride === 2 ? "nd" : vecStride === 3 ? "rd" : "th"} spot
                  {" \u2014 pick a single layer for all of them"}
                </span>
              )}
            </div>
          )}

          {mode === "colour" && (
          <div style={{ display: "flex", alignItems: "center", gap: 8,
                        marginTop: 6, fontSize: 11, color: P.faint }}>
            <span>{def.diverging ? lo.toFixed(3) : lo.toFixed(3)}</span>
            <span style={{ flex: 1, height: 8, borderRadius: 4,
                           background: def.diverging
                             ? `linear-gradient(90deg, rgba(46,90,136,0.9), ${P.mid}, rgba(192,57,43,0.9))`
                             : `linear-gradient(90deg, rgba(192,57,43,0.12), rgba(192,57,43,0.9))` }} />
            <span>{hi.toFixed(3)} {def.unit}</span>
          </div>
          )}

          {stats && (
            <div style={{ display: "flex", gap: 16, flexWrap: "wrap",
                          fontSize: 11.5, color: P.muted, marginTop: 8 }}>
              <span><b>{idxs.length}</b> spots
                {layer !== "all" && layerPos >= 0
                  ? ` \u00b7 layer ${layer + 1}/${layers.length} at ${layers[layerPos].energy.toFixed(1)} MeV`
                  : ""}</span>
              <span>median <b>{stats.p50.toFixed(3)}</b></span>
              <span>p95 <b>{stats.p95.toFixed(3)}</b></span>
              <span>range <b>{stats.min.toFixed(3)}</b> to{" "}
                <b>{stats.max.toFixed(3)}</b> {def.unit}</span>
              {data.machine && <span>{data.machine}</span>}
            </div>
          )}

          <p style={{ fontSize: 11, color: P.faint, lineHeight: 1.5,
                      margin: "8px 0 0" }}>
            {mode === "vector"
              ? `Displacements are magnified ${mag} times; at true scale a
                 sub-millimetre deviation on a 260 mm field would be invisible.
                 Arrows all pointing the same way is a whole-field translation;
                 arrows pointing inward or outward from the centre is a scale
                 error; arrows in no consistent direction is random scatter.`
              : `Colour scale is clipped at the 2nd/98th percentile so a few
                 extreme spots do not flatten the range. Look for structure
                 rather than magnitude: a uniform tint is a whole-field
                 translation, a centre-to-edge gradient is scaling or magnet
                 nonlinearity, and salt-and-pepper is random scatter.`}
          </p>
        </>
      )}

      {!busy && data && shown.length === 0 && (
        <div style={{ fontSize: 12, color: P.muted, padding: "30px 0",
                      textAlign: "center" }}>
          No data for this metric on this beam.
        </div>
      )}
    </div>
  );
}

const stepBtn: React.CSSProperties = {
  font: "inherit", fontSize: 11, width: 24, height: 22, lineHeight: 1,
  border: `1px solid ${P.rule}`, borderRadius: 6, background: "#fff",
  color: P.muted, cursor: "pointer", padding: 0,
};

const card: React.CSSProperties = {
  border: "1px solid #e2e2e2",
  borderRadius: 10,
  padding: "16px 18px",
  background: "#fff",
  fontFamily: "system-ui, sans-serif",
  color: P.text,
  maxWidth: "100%",
};
