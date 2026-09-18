import { useEffect, useRef } from "react";
import type { PlaneData } from "../types";

type Mode = "dose" | "gamma" | "diff";

interface Props {
  plane: PlaneData | null;
  mode: Mode;
  /** Global dose max used for consistent colour scaling across panels (Gy). */
  scaleMax?: number;
  /** Display window as fractions [0..1] of scaleMax (dose mode only). */
  windowMin?: number;
  windowMax?: number;
  showIsodose?: boolean;
  /**
   * Optional CT plane (HU) rendered as a grayscale backdrop under the
   * dose/gamma colorwash. May be at a DIFFERENT (finer) resolution than
   * `plane` -- both layers cover the same physical extent and are composited
   * by scaled drawImage, so resolutions need not match.
   */
  backdrop?: PlaneData | null;
  /** Internal render width in px (display stretches to the panel width). */
  size?: number;
  /**
   * When set, the canvas fits within this fraction of the viewport height
   * (e.g. 34 => max-height 34vh), shrinking to preserve aspect, so a 2x2
   * grid of panels fits on one screen without scrolling.
   */
  fitVh?: number;
}

const clamp01 = (v: number) => (v < 0 ? 0 : v > 1 ? 1 : v);

/** Standard jet colormap. t in [0,1] -> [r,g,b] 0..255. */
function jet(t: number): [number, number, number] {
  const r = clamp01(1.5 - Math.abs(4 * t - 3));
  const g = clamp01(1.5 - Math.abs(4 * t - 2));
  const b = clamp01(1.5 - Math.abs(4 * t - 1));
  return [r * 255, g * 255, b * 255];
}

/** Gamma colormap: green (pass, g<=1) -> red (fail, g>1). null = excluded. */
function gammaColor(g: number): [number, number, number] | null {
  if (Number.isNaN(g) || !Number.isFinite(g)) return null;
  if (g <= 1) {
    const t = clamp01(g);
    return [30 + 30 * t, 150 + 70 * (1 - t), 60 * (1 - t)];
  }
  const t = clamp01(g - 1);
  return [200 + 40 * t, 90 * (1 - t), 40 * (1 - t)];
}

const ISODOSE_LEVELS = [0.95, 0.8, 0.5, 0.2];
// Isodose line appearance -- thin + semi-transparent so the colorwash shows
// through. Width is in internal render-px units (see `size`); tune freely.
const ISODOSE_LINE_WIDTH = 1.2;
const ISODOSE_COLOR = "rgba(255, 255, 255, 0.55)";

// CT display window: soft tissue (W 400 / L 40).
const CT_LO = -160;
const CT_HI = 240;
// Colorwash opacity over CT.
const DOSE_ALPHA = 0.55;
const GAMMA_ALPHA = 0.65;
const DIFF_ALPHA = 0.7;
// Dose below this fraction of max is fully transparent over CT.
const DOSE_WASH_THRESHOLD = 0.08;
// Difference display: fully saturated at +/- DIFF_SAT of scaleMax; agreement
// better than DIFF_FLOOR of scaleMax is hidden (transparent).
const DIFF_SAT = 0.1;
const DIFF_FLOOR = 0.03;

/** Diverging colormap for dose difference. t in [-1,1]: blue (eval cold)
 *  through transparent-agreement to red (eval hot). */
function divergingColor(t: number): [number, number, number] {
  const u = Math.min(1, Math.abs(t));
  if (t > 0) return [255, Math.round(190 * (1 - u)), Math.round(40 * (1 - u))];
  return [Math.round(40 * (1 - u)), Math.round(190 * (1 - u)), 255];
}

/** Render a PlaneData to an offscreen canvas via a per-pixel color function
 *  returning [r,g,b,a]. */
function renderLayer(
  p: PlaneData,
  color: (v: number, i: number) => [number, number, number, number]
): HTMLCanvasElement {
  const off = document.createElement("canvas");
  off.width = p.cols;
  off.height = p.rows;
  const ctx = off.getContext("2d")!;
  const img = ctx.createImageData(p.cols, p.rows);
  for (let i = 0; i < p.rows * p.cols; i++) {
    const [r, g, b, a] = color(p.data[i], i);
    const o = i * 4;
    img.data[o] = r;
    img.data[o + 1] = g;
    img.data[o + 2] = b;
    img.data[o + 3] = a;
  }
  ctx.putImageData(img, 0, 0);
  return off;
}

export function DoseMapCanvas({
  plane,
  mode,
  scaleMax,
  windowMin = 0,
  windowMax = 1,
  showIsodose = false,
  backdrop = null,
  size = 800,
  fitVh,
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    // Internal render resolution; CSS stretches to fill the panel width while
    // the aspect ratio follows the data.
    const aspect = plane && plane.rows > 0 ? plane.cols / plane.rows : 1;
    const renderW = size;
    const renderH = Math.round(size / aspect);
    canvas.width = renderW;
    canvas.height = renderH;
    if (fitVh) {
      // Fit within a viewport-height budget, preserving aspect: the browser
      // resolves width auto + max constraints + aspect-ratio together.
      canvas.style.width = "auto";
      canvas.style.height = "auto";
      canvas.style.maxWidth = "100%";
      canvas.style.maxHeight = `${fitVh}vh`;
      canvas.style.display = "block";
      canvas.style.marginLeft = "auto";
      canvas.style.marginRight = "auto";
    } else {
      canvas.style.width = "100%";
      canvas.style.height = "auto";
    }
    canvas.style.aspectRatio = `${renderW} / ${renderH}`;

    if (!plane || plane.rows === 0 || plane.cols === 0) {
      ctx.fillStyle = "#0d1117";
      ctx.fillRect(0, 0, renderW, renderH);
      ctx.fillStyle = "#7d8590";
      ctx.font = "20px Inter, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("No data", renderW / 2, renderH / 2);
      return;
    }

    const { data, rows, cols } = plane;
    const dMax = mode !== "gamma" ? (scaleMax ?? plane.maxDose ?? 1) : 0;
    const lo = mode === "dose" ? windowMin * dMax : 0;
    const hi = mode === "dose" ? Math.max(windowMax * dMax, lo + 1e-6) : 0;
    const washFloor = DOSE_WASH_THRESHOLD * dMax;
    const diffSat = DIFF_SAT * dMax;
    const diffFloor = DIFF_FLOOR * dMax;
    const hasBg = !!(backdrop && backdrop.rows > 0 && backdrop.cols > 0);

    // --- Layer 1: CT grayscale (fine grid) or dark background ---
    ctx.fillStyle = "#0d1117";
    ctx.fillRect(0, 0, renderW, renderH);
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = "high";
    if (hasBg) {
      const ctLayer = renderLayer(backdrop!, (hu) => {
        const g = clamp01((hu - CT_LO) / (CT_HI - CT_LO)) * 255;
        return [g, g, g, 255];
      });
      ctx.drawImage(ctLayer, 0, 0, renderW, renderH);
    }

    // --- Layer 2: dose / gamma colorwash with alpha ---
    const washAlpha = hasBg
      ? Math.round(
          (mode === "dose"
            ? DOSE_ALPHA
            : mode === "diff"
              ? DIFF_ALPHA
              : GAMMA_ALPHA) * 255
        )
      : 255;

    const doseLayer = renderLayer(plane, (v) => {
      if (mode === "dose") {
        if (hasBg && v <= washFloor) return [0, 0, 0, 0];
        const t = clamp01((v - lo) / (hi - lo));
        const [r, g, b] = jet(t);
        return [r, g, b, washAlpha];
      }
      if (mode === "diff") {
        if (Math.abs(v) < diffFloor || !Number.isFinite(v)) {
          return hasBg ? [0, 0, 0, 0] : [13, 17, 23, 255];
        }
        const t = Math.max(-1, Math.min(1, v / Math.max(diffSat, 1e-6)));
        const [r, g, b] = divergingColor(t);
        return [r, g, b, washAlpha];
      }
      const rgb = gammaColor(v);
      if (rgb === null) return hasBg ? [0, 0, 0, 0] : [13, 17, 23, 255];
      return [rgb[0], rgb[1], rgb[2], washAlpha];
    });

    ctx.drawImage(doseLayer, 0, 0, renderW, renderH);

    // --- Layer 3: isodose contours ---
    // Drawn as real strokes on the main canvas AT DISPLAY RESOLUTION, after the
    // (smoothed) colorwash. Previously these were baked as opaque pixel marks
    // into the low-res doseLayer, which the scaled drawImage then blurred into
    // thick bands that washed out the colorwash. Stroking here keeps them crisp
    // and thin regardless of grid resolution, and the alpha lets dose show
    // through.
    if (mode === "dose" && showIsodose && dMax > 0) {
      const sx = renderW / cols;
      const sy = renderH / rows;
      ctx.save();
      ctx.lineWidth = ISODOSE_LINE_WIDTH;
      ctx.strokeStyle = ISODOSE_COLOR;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      const path = new Path2D();
      for (const level of ISODOSE_LEVELS) {
        const thr = level * dMax;
        for (let r = 0; r < rows - 1; r++) {
          for (let c = 0; c < cols - 1; c++) {
            const inside = data[r * cols + c] >= thr;
            // vertical edge between (r,c) and (r,c+1)
            if (inside !== (data[r * cols + c + 1] >= thr)) {
              const x = (c + 1) * sx;
              path.moveTo(x, r * sy);
              path.lineTo(x, (r + 1) * sy);
            }
            // horizontal edge between (r,c) and (r+1,c)
            if (inside !== (data[(r + 1) * cols + c] >= thr)) {
              const y = (r + 1) * sy;
              path.moveTo(c * sx, y);
              path.lineTo((c + 1) * sx, y);
            }
          }
        }
      }
      ctx.stroke(path);
      ctx.restore();
    }
  }, [plane, mode, scaleMax, windowMin, windowMax, showIsodose, backdrop, size, fitVh]);

  return (
    <canvas
      ref={canvasRef}
      className="rounded-md border border-clinical-border bg-clinical-bg w-full"
    />
  );
}
