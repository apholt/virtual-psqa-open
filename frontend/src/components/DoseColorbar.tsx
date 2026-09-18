import { useEffect, useRef } from "react";

interface Props {
  /** Global dose max used for colour scaling across panels (Gy). */
  scaleMax?: number;
  /** Display window as fractions [0..1] of scaleMax -- must match the panel. */
  windowMin?: number;
  windowMax?: number;
  /** Optional caption shown above the bar, e.g. "MC" or "TPS". */
  label?: string;
  /** Number of gradient sample columns (fidelity of the drawn map). */
  steps?: number;
  className?: string;
}

const clamp01 = (v: number) => (v < 0 ? 0 : v > 1 ? 1 : v);

/** Standard jet colormap -- IDENTICAL to DoseMapCanvas so the legend matches
 *  the colorwash exactly. t in [0,1] -> [r,g,b] 0..255. */
function jet(t: number): [number, number, number] {
  const r = clamp01(1.5 - Math.abs(4 * t - 3));
  const g = clamp01(1.5 - Math.abs(4 * t - 2));
  const b = clamp01(1.5 - Math.abs(4 * t - 1));
  return [r * 255, g * 255, b * 255];
}

const fmtGy = (v: number) => (v >= 100 ? v.toFixed(0) : v.toFixed(1));

/**
 * Horizontal dose colorbar / legend for the MC and TPS panels.
 * Renders the jet gradient across the *visible* dose window and labels the
 * endpoints + midpoint in Gy. Drop it directly under a DoseMapCanvas panel and
 * pass the same scaleMax / windowMin / windowMax that panel receives.
 */
export function DoseColorbar({
  scaleMax = 1,
  windowMin = 0,
  windowMax = 1,
  label,
  steps = 256,
  className,
}: Props) {
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
      const [r, g, b] = jet(i / (steps - 1));
      const o = i * 4;
      img.data[o] = r;
      img.data[o + 1] = g;
      img.data[o + 2] = b;
      img.data[o + 3] = 255;
    }
    ctx.putImageData(img, 0, 0);
  }, [steps]);

  const lo = windowMin * scaleMax;
  const hi = Math.max(windowMax * scaleMax, lo);
  const mid = (lo + hi) / 2;

  return (
    <div
      className={className}
      style={{ width: "100%", userSelect: "none" }}
    >
      {label && (
        <div
          style={{
            fontSize: 11,
            letterSpacing: "0.04em",
            color: "#7d8590",
            marginBottom: 3,
          }}
        >
          {label}
        </div>
      )}
      <canvas
        ref={canvasRef}
        style={{
          width: "100%",
          height: 8,
          display: "block",
          borderRadius: 3,
          imageRendering: "auto",
        }}
      />
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          fontSize: 10,
          color: "#7d8590",
          marginTop: 2,
          fontVariantNumeric: "tabular-nums",
        }}
      >
        <span>{fmtGy(lo)}</span>
        <span>{fmtGy(mid)}</span>
        <span>{fmtGy(hi)} Gy</span>
      </div>
    </div>
  );
}
