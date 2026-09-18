/*
 * MonitoringCard.tsx  --  the delivery gate, on the plan detail page.
 *
 * Answers one question: may this plan be treated without a pre-treatment dry
 * run, and on what evidence?
 *
 * Layout follows the gate's actual logic rather than flattening it. The four
 * evidence layers are not a uniform list: three are PRE-TREATMENT and decide
 * clearance, one is IN-VIVO and can override them mid-course. Grouping them
 * that way is the difference between showing the verdict and showing how the
 * verdict was reached.
 *
 * Charts collapse by default -- three beams of control chart is detail, not
 * headline -- but auto-expand whenever a beam carries a signal, since that is
 * the series that caught the plan-17 excursion on a beam whose gamma passed.
 *
 * Dependency-free: inline SVG, no chart library. Fetches from
 *   GET /api/plans/{planId}/monitoring   (see routers/monitoring.py)
 */
import { useEffect, useState } from "react";

type Point = {
  fraction: number;
  date: string | null;
  machine: string | null;
  value: number;
  mu_deviation_pct: number | null;
  gamma: number | null;
  flags: string[];
};
type Chart = {
  beam: string;
  metric: string;
  unit: string;
  established: boolean;
  centre: number | null;
  sigma: number | null;
  ucl: number | null;
  lcl: number | null;
  n: number;
  points: Point[];
};
export type GateStatus =
  | "measure"
  | "cleared"
  | "verified"
  | "investigate"
  | "escalate"
  | "incomplete"
  // legacy status from the pre-gate.py monitoring endpoint; kept so the card
  // renders correctly against a backend that has not been repointed yet.
  | "dry_run_or_first_fraction";

export type GateLayer = {
  name: string;
  status: "pass" | "marginal" | "fail" | "unavailable";
  detail: string;
  /** GATE_SUMMARY_V1 -- one short clause; `detail` stays the full text. */
  summary?: string;
  metrics?: Record<string, unknown>;
};

export type Gate = {
  status: GateStatus;
  reason: string;
  short_reason?: string;
  pretreatment_cleared?: boolean;
  dry_run_waived?: boolean;
  missing_layers?: string[];
  layers?: GateLayer[];
  tier1?: { passed: boolean; min_passing_rate: number } | null;
  tier2?: {
    fractions_analysed: number;
    below_action: number;
    control_signal: boolean;
  } | null;
};

type Prediction = {
  beam_name: string;
  n_spots: number;
  n_realizations: number;
  predicted_mean: number;
  predicted_sd: number;
  predicted_worst: number;
  model_machine: string | null;
  model_spot_sigma_mm: number | null;
  criterion: string;
  created_at: string;
  // The endpoint also returns delivered_n / delivered_mean / delivered_min /
  // min_residual. They are deliberately not shown: the prediction is
  // pre-treatment supporting evidence, and per-fraction outcomes are already
  // covered by the log verification layer and the control charts below.
};

type Payload = {
  plan_id: number;
  plan_label: string;
  machine?: string | null;
  gate: Gate;
  log_summary: {
    fractions_analysed: number;
    plan_fractions: number | null;
    mean_passing_rate: number | null;
    min_passing_rate: number | null;
    criterion: string;
    action_level: number;
  };
  predictions?: Prediction[];
  charts: Chart[];
  note: string;
};

const P = {
  text: "#1c1c1c",
  muted: "#6b6b6b",
  faint: "#9a9a9a",
  rule: "#e8e8e8",
  pass: "#1b5e20",
  warn: "#8a6d00",
  fail: "#8a1f1f",
  idle: "#8a8a8a",
  line: "#2e5a88",
  limit: "#c0392b",
};

const STATUS: Record<GateStatus, { badge: string; fg: string; bg: string }> = {
  cleared:                   { badge: "Cleared",     fg: P.pass, bg: "#e7f3e8" },
  verified:                  { badge: "Verified",    fg: P.pass, bg: "#e7f3e8" },
  investigate:               { badge: "Investigate", fg: P.warn, bg: "#fbf3d6" },
  measure:                   { badge: "Measure",     fg: P.fail, bg: "#f7e4e4" },
  escalate:                  { badge: "Escalate",    fg: P.fail, bg: "#f7e4e4" },
  incomplete:                { badge: "Incomplete",  fg: P.muted, bg: "#efefef" },
  dry_run_or_first_fraction: { badge: "Awaiting log", fg: P.muted, bg: "#efefef" },
};

/** Headline reads as the decision, in the physicist's terms -- not the status
 *  token. "No dry run required" is the thing being asked; "verified" is only
 *  how the system got there. */
function headline(gate: Gate, ls: Payload["log_summary"]) {
  const fx = ls.fractions_analysed;
  const of = ls.plan_fractions ? ` of ${ls.plan_fractions}` : "";
  switch (gate.status) {
    case "cleared":
      return { title: "No dry run required",
               sub: "Verification begins with the first delivered fraction." };
    case "verified":
      return { title: "No dry run required",
               sub: `${fx}${of} fraction(s) verified in vivo.` };
    case "investigate":
      return { title: "Review before the next fraction",
               sub: gate.short_reason || gate.reason };
    case "measure":
      return { title: "Physical measurement required", sub: gate.reason };
    case "escalate":
      return { title: "Escalate to physics", sub: gate.reason };
    case "incomplete":
      return { title: "Not yet cleared", sub: gate.reason };
    default:
      return { title: "Awaiting first delivery log", sub: gate.reason };
  }
}

const LAYER_LABEL: Record<string, string> = {
  secondary_dose: "Secondary dose calculation",
  deliverability: "Deliverability vs machine limits",
  machine_state: "Room control state",
  log_verification: "Per-fraction log verification",
};

const PRETREATMENT = ["secondary_dose", "deliverability", "machine_state"];

const DOT: Record<GateLayer["status"], string> = {
  pass: P.pass, marginal: P.warn, fail: P.fail, unavailable: P.faint,
};

/**
 * MONITORING_V3 -- one line per layer.
 *
 * Each row renders the layer's `summary`, with the full `detail` on hover.
 * The detail strings run 200+ characters and wrap to three lines, which is
 * how the same control-chart sentence came to occupy four separate places on
 * the plan page. The phase column replaces the two group headings.
 */
function LayerRow({ layer }: { layer: GateLayer }) {
  const blocking = layer.status === "fail" || layer.status === "unavailable";
  const phase = PRETREATMENT.includes(layer.name) ? "before tx" : "during tx";
  return (
    <div style={{ display: "flex", gap: 10, alignItems: "baseline",
                  padding: "6px 0 6px 8px",
                  borderBottom: `0.5px solid ${P.rule}`,
                  borderLeft: `2px solid ${blocking ? DOT[layer.status] : "transparent"}`,
                  fontSize: 12.5 }}
         title={layer.detail}>
      <span aria-hidden style={{ width: 7, height: 7, borderRadius: "50%",
                                 background: DOT[layer.status], flexShrink: 0,
                                 transform: "translateY(-1px)" }} />
      <span style={{ minWidth: 190, color: P.text }}>
        {LAYER_LABEL[layer.name] ?? layer.name}
      </span>
      <span style={{ color: P.muted, flex: 1, overflow: "hidden",
                     textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {layer.summary || layer.detail}
      </span>
      <span style={{ color: P.faint, fontSize: 11, flexShrink: 0,
                     minWidth: 54, textAlign: "right" }}>
        {phase}
      </span>
    </div>
  );
}


/** Pre-treatment predicted gamma per field.
 *
 *  Shown once, before treatment, as supporting pre-treatment evidence. It
 *  models the REPRODUCIBLE component of delivery only: the room's measured
 *  systematic offset plus its per-spot scatter, sampled over several
 *  realizations against the prescription.
 *
 *  It is deliberately NOT framed as predicting the outcome, because the
 *  calibration study on this data showed it does not discriminate: across 14
 *  plan-beams it predicted 100% for every beam, including the two that later
 *  failed. What a near-100% prediction does say is that the room's
 *  reproducible behaviour, applied to this spot list, produces no measurable
 *  degradation -- a statement about deliverability under normal machine
 *  behaviour, which is what it is used for here.
 */
function PredictionRows({ preds }: { preds: Prediction[] }) {
  if (!preds.length) return null;
  const worst = Math.min(...preds.map((p) => p.predicted_mean));
  const model = preds[0];
  // MONITORING_V3 -- one line, caveat on hover. This is supporting context,
  // not a gate layer: it models only the room's reproducible component, and
  // the calibration study showed it predicted ~100% for every beam including
  // the two that later failed. It should not occupy five lines inside the
  // list of things that decide clearance.
  const caveat =
    `Reproducible component only \u2014 ${model.model_machine} offset and ` +
    `per-spot scatter, ${model.n_realizations} realizations at ` +
    `${model.criterion}. Does not anticipate transient machine excursions; ` +
    `per-fraction log verification covers those.`;
  return (
    <div style={{ display: "flex", gap: 10, alignItems: "baseline",
                  padding: "6px 0 6px 8px", fontSize: 12.5,
                  borderLeft: "2px solid transparent" }}
         title={caveat}>
      <span aria-hidden style={{ width: 7, height: 7, borderRadius: "50%",
                                 background: P.faint, flexShrink: 0,
                                 transform: "translateY(-1px)" }} />
      <span style={{ minWidth: 190, color: P.muted }}>
        Predicted delivery
      </span>
      <span style={{ color: P.muted, flex: 1, overflow: "hidden",
                     textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {preds.length > 1
          ? `worst ${worst.toFixed(1)}% across ${preds.length} fields`
          : `${preds[0].predicted_mean.toFixed(1)}%`}
        {" \u00b7 reproducible component only"}
      </span>
      <span style={{ color: P.faint, fontSize: 11, flexShrink: 0,
                     minWidth: 54, textAlign: "right" }}>
        context
      </span>
    </div>
  );
}

export default function MonitoringCard(
  { planId, onGate }: { planId: number; onGate?: (gate: Gate) => void },
) {
  const [data, setData] = useState<Payload | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setData(null);
    setErr(null);
    fetch(`/api/plans/${planId}/monitoring`)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((j) => {
        if (!live) return;
        setData(j);
        onGate?.(j.gate);
      })
      .catch((e) => live && setErr(String(e)));
    return () => { live = false; };
  }, [planId]);

  if (err) return <div style={card}>Monitoring unavailable: {err}</div>;
  if (!data) return <div style={card}>Loading delivery verification{"\u2026"}</div>;

  const s = STATUS[data.gate.status] ??
    { badge: data.gate.status, fg: P.muted, bg: "#efefef" };
  const ls = data.log_summary;
  const h = headline(data.gate, ls);
  const layers = data.gate.layers ?? [];
  const pre = layers.filter((l) => PRETREATMENT.includes(l.name));
  const vivo = layers.filter((l) => !PRETREATMENT.includes(l.name));
  // MONITORING_V4 -- per-beam position control charts removed from this page.
  //
  // They are room-level SPC rendered on a plan page: a physicist chasing a
  // room signal needs every plan on that machine, not one plan's three beams.
  // The gate's machine_state row already states the finding in one line.
  //
  // They were also misleading here. On P_SV the limits were computed over a
  // window that included the excursion, so the two healthy baseline fractions
  // (fx9, fx12 at 0.21 mm) fell below the lower limit and were flagged, while
  // the degraded fractions sat inside the band -- the chart marked the good
  // deliveries as out of control. Baseline exclusion has to be fixed in
  // services/room_spc.py before charts like these are worth showing anywhere.

  return (
    <div style={card}>
      <div style={{ display: "flex", justifyContent: "space-between",
                    alignItems: "center" }}>
        <span style={{ fontSize: 10.5, letterSpacing: 0.7, color: P.faint,
                       textTransform: "uppercase" }}>
          Delivery verification
        </span>
        <span style={{ background: s.bg, color: s.fg, fontWeight: 600,
                       fontSize: 11.5, padding: "3px 10px", borderRadius: 11 }}>
          {s.badge}
        </span>
      </div>

      <div style={{ margin: "12px 0 2px", fontSize: 19, fontWeight: 500,
                    letterSpacing: -0.2, color: P.text }}>
        {h.title}
      </div>
      <div style={{ fontSize: 12.5, color: P.muted, lineHeight: 1.5 }}>
        {h.sub}
      </div>

      {layers.length > 0 && (
        <div style={{ marginTop: 6, borderTop: `1px solid ${P.rule}` }}>
          {/* MONITORING_V3 -- one flat list, pre-treatment layers first.
              The per-row phase column carries what the two group headings
              used to say, without spending two lines to say it. */}
          {[...pre, ...vivo].map((l) => <LayerRow key={l.name} layer={l} />)}
          {data.predictions?.length ? (
            <PredictionRows preds={data.predictions} />
          ) : null}
        </div>
      )}

    </div>
  );
}

const card: React.CSSProperties = {
  border: "1px solid #e2e2e2",
  borderRadius: 10,
  padding: "16px 18px",
  background: "#fff",
  fontFamily: "system-ui, sans-serif",
  color: P.text,
  maxWidth: "100%",
};
