import { useEffect, useState } from "react";

/**
 * Delivered spot statistics per analysed fraction, from the log QA pipeline.
 * Self-contained: fetches /api/results/plan/{planId}/spot-stats itself.
 * Renders nothing until at least one fraction has statistics.
 *
 * Usage in PlanDetail.tsx (inside the Log file reconstruction card):
 *   import { SpotStats } from "../components/SpotStats";
 *   ...
 *   <SpotStats planId={id} />
 */

interface BeamStats {
  beam_name: string;
  n_spots_prescribed: number;
  n_spots_delivered: number;
  n_spots_matched?: number;
  mu_prescribed: number;
  mu_delivered: number;
  mu_deviation_pct?: number;
  pos_mean_dx_mm?: number;
  pos_mean_dy_mm?: number;
  pos_std_dx_mm?: number;
  pos_std_dy_mm?: number;
  pos_mean_radial_mm?: number;
  pos_p95_radial_mm?: number;
  pos_max_radial_mm?: number;
  mu_err_mean_abs_pct?: number;
  mu_err_max_abs_pct?: number;
  size_x_min_mm?: number;
  size_x_max_mm?: number;
  size_y_min_mm?: number;
  size_y_max_mm?: number;
  size_max_abs_diff_mm?: number;
  size_is_plan_echo?: boolean;
  gamma_passing_rate?: number;
}

interface FractionStats {
  plan_id: number;
  fraction: number;
  treatment_date: string;
  machine: string;
  beams: BeamStats[];
}

const th: React.CSSProperties = {
  textAlign: "right",
  fontSize: 10,
  fontWeight: 500,
  color: "#8a8578",
  textTransform: "uppercase",
  letterSpacing: ".03em",
  padding: "4px 8px 4px 0",
  borderBottom: "0.5px solid #e5e1d8",
  whiteSpace: "nowrap",
};
const td: React.CSSProperties = {
  textAlign: "right",
  fontSize: 11,
  fontFamily: "monospace",
  color: "#3d3a33",
  padding: "5px 8px 5px 0",
  borderBottom: "0.5px solid #efece5",
  whiteSpace: "nowrap",
};

const fmt = (v: number | undefined, digits = 2, suffix = "") =>
  v === undefined || v === null ? "\u2014" : `${v.toFixed(digits)}${suffix}`;

export function SpotStats({ planId }: { planId: number }) {
  const [fractions, setFractions] = useState<FractionStats[]>([]);

  useEffect(() => {
    let live = true;
    fetch(`/api/results/plan/${planId}/spot-stats`)
      .then((r) => (r.ok ? r.json() : []))
      .then((data) => {
        if (live && Array.isArray(data)) setFractions(data);
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [planId]);

  if (fractions.length === 0) return null;

  const anyEcho = fractions.some((f) =>
    f.beams.some((b) => b.size_is_plan_echo)
  );

  return (
    <div style={{ marginTop: 12 }}>
      <p
        style={{
          fontSize: 11,
          fontWeight: 500,
          color: "#8a8578",
          textTransform: "uppercase",
          letterSpacing: ".04em",
          marginBottom: 6,
        }}
      >
        Delivered spot statistics
      </p>
      {fractions.map((f) => (
        <div key={f.fraction} style={{ marginBottom: 8 }}>
          <p style={{ fontSize: 11, color: "#8a8578", margin: "2px 0 4px" }}>
            Fx{f.fraction}
            {f.treatment_date ? ` \u00b7 ${f.treatment_date}` : ""}
            {f.machine ? ` \u00b7 ${f.machine}` : ""}
          </p>
          <div style={{ overflowX: "auto" }}>
            <table style={{ borderCollapse: "collapse", width: "100%" }}>
              <thead>
                <tr>
                  <th style={{ ...th, textAlign: "left" }}>Beam</th>
                  <th style={th}>Spots</th>
                  <th style={th}>ΔMU</th>
                  <th style={th}>Pos μ radial</th>
                  <th style={th}>Pos p95</th>
                  <th style={th}>Pos max</th>
                  <th style={th}>σx / σy</th>
                  <th style={th}>MU/spot max</th>
                  <th style={th}>Spot size x</th>
                  <th style={th}>Spot size y</th>
                </tr>
              </thead>
              <tbody>
                {f.beams.map((b) => (
                  <tr key={b.beam_name}>
                    <td style={{ ...td, textAlign: "left", fontFamily: "inherit" }}>
                      {b.beam_name}
                    </td>
                    <td style={td}>
                      {b.n_spots_delivered}/{b.n_spots_prescribed}
                    </td>
                    <td style={td}>{fmt(b.mu_deviation_pct, 2, "%")}</td>
                    <td style={td}>{fmt(b.pos_mean_radial_mm, 2, " mm")}</td>
                    <td style={td}>{fmt(b.pos_p95_radial_mm, 2, " mm")}</td>
                    <td style={td}>{fmt(b.pos_max_radial_mm, 2, " mm")}</td>
                    <td style={td}>
                      {fmt(b.pos_std_dx_mm, 2)} / {fmt(b.pos_std_dy_mm, 2)} mm
                    </td>
                    <td style={td}>{fmt(b.mu_err_max_abs_pct, 1, "%")}</td>
                    <td style={td}>
                      {fmt(b.size_x_min_mm, 1)}{"\u2013"}{fmt(b.size_x_max_mm, 1)} mm
                    </td>
                    <td style={td}>
                      {fmt(b.size_y_min_mm, 1)}{"\u2013"}{fmt(b.size_y_max_mm, 1)} mm
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}
      {anyEcho && (
        <p style={{ fontSize: 10, color: "#a8a496", marginTop: 2 }}>
          Spot sizes in the record match plan-nominal values exactly (echo) {"\u2014"}
          size is displayed for reference but cannot indicate drift on this machine.
        </p>
      )}
    </div>
  );
}
