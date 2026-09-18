import type { GammaResult } from "../types";
import { C } from "../theme";

const LABELS: Record<string, string> = {
  mcSquare_vs_TPS: "MCsquare vs TPS",
  log_vs_TPS: "Log recon vs Rx",
  mcSquare_vs_log: "MCsquare vs Log",
};

export function GammaBar({ result }: { result: GammaResult }) {
  const pct = Math.max(0, Math.min(100, result.passing_rate));
  const passed = result.passed;
  const fill = passed ? C.barPass : C.barFail;
  const text = passed ? C.passText : C.measureText;

  return (
    <div>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          fontSize: 12,
          marginBottom: 4,
        }}
      >
        <span style={{ color: C.text, fontWeight: 500 }}>
          {LABELS[result.comparison_type] ?? result.comparison_type}
        </span>
        <span style={{ color: text, fontWeight: 500 }}>{pct.toFixed(1)}%</span>
      </div>
      <div
        style={{
          position: "relative",
          height: 6,
          background: C.track,
          borderRadius: 3,
          overflow: "hidden",
        }}
      >
        <div
          style={{ height: "100%", borderRadius: 3, width: `${pct}%`, background: fill }}
        />
        <div
          title={`Threshold ${result.threshold}%`}
          style={{
            position: "absolute",
            top: 0,
            height: "100%",
            width: 1,
            background: "rgba(0,0,0,0.45)",
            left: `${result.threshold}%`,
          }}
        />
      </div>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          fontSize: 10,
          color: C.muted,
          marginTop: 3,
        }}
      >
        <span>
          {result.dd_percent}% / {result.dta_mm} mm
        </span>
        <span>threshold {result.threshold}%</span>
      </div>
    </div>
  );
}
