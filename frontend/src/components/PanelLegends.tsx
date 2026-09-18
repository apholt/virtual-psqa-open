import { useEffect, useRef } from "react";

/* Colormaps below are copied VERBATIM from DoseMapCanvas so the legends match
 * the colorwash exactly. If you change a map there, mirror it here (or move all
 * three maps into a shared colormaps.ts and import). */

const MUTED = "#7d8590";

/* ------------------------------- Diff ---------------------------------- */

/** Diverging colormap for dose difference. t in [-1,1]: blue (eval cold)
 *  through agreement to red (eval hot). */
function divergingColor(t: number): [number, number, number] {
  const u = Math.min(1, Math.abs(t));
  if (t > 0) return [255, Math.round(190 * (1 - u)), Math.round(40 * (1 - u))];
  return [Math.round(40 * (1 - u)), Math.round(190 * (1 - u)), 255];
}

interface DiffProps {
  /** Global dose max (Gy). */
  scaleMax?: number;
  /** Saturation as fraction of scaleMax -- must match DoseMapCanvas DIFF_SAT. */
  diffSat?: number;
  label?: string;
  steps?: number;
  className?: string;
}

const fmtSigned = (v: number) => {
  const s = Math.abs(v) >= 10 ? Math.abs(v).toFixed(0) : Math.abs(v).toFixed(1);
  return v > 0 ? `+${s}` : v < 0 ? `-${s}` : "0";
};

/** Dose-difference legend (eval - ref). Center is agreement. */
export function DiffColorbar({
  scaleMax = 1,
  diffSat = 0.1,
  label,
  steps = 256,
  className,
}: DiffProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    canvas.width = steps;
    canvas.height = 1;
    const img = ctx.createImageData(steps, 1);
    for (let i = 0; i < steps; i++) {
      const t = (i / (steps - 1)) * 2 - 1; // -1 .. +1
      const [r, g, b] = divergingColor(t);
      const o = i * 4;
      img.data[o] = r;
      img.data[o + 1] = g;
      img.data[o + 2] = b;
      img.data[o + 3] = 255;
    }
    ctx.putImageData(img, 0, 0);
  }, [steps]);

  const hi = diffSat * scaleMax;

  return (
    <div className={className} style={{ width: "100%", userSelect: "none" }}>
      {label && (
        <div style={{ fontSize: 11, letterSpacing: "0.04em", color: MUTED, marginBottom: 3 }}>
          {label}
        </div>
      )}
      <div style={{ position: "relative", width: "100%" }}>
        <canvas
          ref={canvasRef}
          style={{ width: "100%", height: 8, display: "block", borderRadius: 3 }}
        />
        {/* 0 / agreement tick */}
        <div
          style={{
            position: "absolute",
            top: -1,
            bottom: -1,
            left: "50%",
            width: 1,
            background: "rgba(13,17,23,0.7)",
            transform: "translateX(-0.5px)",
          }}
        />
      </div>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          fontSize: 10,
          color: MUTED,
          marginTop: 2,
          fontVariantNumeric: "tabular-nums",
        }}
      >
        <span>{fmtSigned(-hi)}</span>
        <span>0</span>
        <span>{fmtSigned(hi)} Gy</span>
      </div>
    </div>
  );
}

/* ------------------------------- Gamma --------------------------------- */

/** Gamma colormap: green (pass, g<=1) -> red (fail, g>1). */
function gammaColorSolid(g: number): [number, number, number] {
  if (g <= 1) {
    const t = Math.max(0, Math.min(1, g));
    return [30 + 30 * t, 150 + 70 * (1 - t), 60 * (1 - t)];
  }
  const t = Math.max(0, Math.min(1, g - 1));
  return [200 + 40 * t, 90 * (1 - t), 40 * (1 - t)];
}

interface GammaProps {
  /** Upper end of the gamma display range. */
  gammaMax?: number;
  /** Pass/fail line (usually 1.0). */
  threshold?: number;
  label?: string;
  steps?: number;
  className?: string;
}

/** Gamma-index legend with the pass/fail line marked. */
export function GammaColorbar({
  gammaMax = 2,
  threshold = 1,
  label,
  steps = 256,
  className,
}: GammaProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    canvas.width = steps;
    canvas.height = 1;
    const img = ctx.createImageData(steps, 1);
    for (let i = 0; i < steps; i++) {
      const g = (i / (steps - 1)) * gammaMax;
      const [r, gr, b] = gammaColorSolid(g);
      const o = i * 4;
      img.data[o] = r;
      img.data[o + 1] = gr;
      img.data[o + 2] = b;
      img.data[o + 3] = 255;
    }
    ctx.putImageData(img, 0, 0);
  }, [steps, gammaMax]);

  const thrPct = Math.max(0, Math.min(1, threshold / gammaMax)) * 100;

  return (
    <div className={className} style={{ width: "100%", userSelect: "none" }}>
      {label && (
        <div style={{ fontSize: 11, letterSpacing: "0.04em", color: MUTED, marginBottom: 3 }}>
          {label}
        </div>
      )}
      <div style={{ position: "relative", width: "100%" }}>
        <canvas
          ref={canvasRef}
          style={{ width: "100%", height: 8, display: "block", borderRadius: 3 }}
        />
        {/* pass/fail line */}
        <div
          style={{
            position: "absolute",
            top: -1,
            bottom: -1,
            left: `${thrPct}%`,
            width: 1,
            background: "rgba(13,17,23,0.85)",
            transform: "translateX(-0.5px)",
          }}
        />
      </div>
      <div
        style={{
          position: "relative",
          height: 12,
          fontSize: 10,
          color: MUTED,
          marginTop: 2,
          fontVariantNumeric: "tabular-nums",
        }}
      >
        <span style={{ position: "absolute", left: 0 }}>0</span>
        <span style={{ position: "absolute", left: `${thrPct}%`, transform: "translateX(-50%)" }}>
          {threshold.toFixed(1)}
        </span>
        <span style={{ position: "absolute", right: 0 }}>γ {gammaMax.toFixed(1)}</span>
      </div>
    </div>
  );
}
