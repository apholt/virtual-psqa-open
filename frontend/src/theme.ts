/**
 * Central design-system tokens + helpers for the light/warm "paper" theme.
 *
 * The build plan specifies exact hex values that have no Tailwind equivalent,
 * so the reference screens (Dashboard, PlanDetail) use these via `style` props.
 */
import type { CSSProperties } from "react";

export const C = {
  pageBg: "#F1EFE8",
  cardBg: "#ffffff",
  border: "rgba(0,0,0,0.12)",
  borderStrong: "rgba(0,0,0,0.25)",
  text: "#1a1a18",
  muted: "#5F5E5A",
  faint: "#888780",
  track: "#E8E6DF",
  accent: "#7F77DD",

  // verdict / status
  measureBg: "#FCEBEB",
  measureText: "#791F1F",
  measureBorder: "#F09595",
  flagBg: "#FAEEDA",
  flagText: "#633806",
  passBg: "#EAF3DE",
  passText: "#27500A",
  pendBg: "#E6F1FB",
  pendText: "#0C447C",
  runBg: "#EEEDFE",
  runText: "#3C3489",

  // bars
  barPass: "#639922",
  barFail: "#E24B4A",
  barWarn: "#EF9F27",

  // evidence dots
  dotComplexity: "#639922",
  dotMc: "#378ADD",
  dotLog: "#D85A30",
  dotPhysical: "#888780",
  iconComplexity: "#533B07",
} as const;

// GATE_STATUS_V1 -----------------------------------------------------------
// One status vocabulary for the whole app. plan.qa_status now stores a
// GateStatus verbatim (services/gate.py), written only by
// services/pipeline.py::persist_gate.
//
// The six gate values are clinical conclusions. "pending" (gate has not run
// yet), "running" (job in flight) and "failed" (job errored) describe machine
// state, not a verdict, and are kept distinct so a plan awaiting evaluation
// never reads as a decision.
//
// barColor()/probTextColor() were removed with this change: both mapped a
// 0-1 ML pass probability, which no longer exists.

export type GateStatusKey =
  | "cleared" | "verified"
  | "investigate" | "incomplete"
  | "measure" | "escalate"
  | "pending" | "running" | "failed";

export interface StatusStyle {
  label: string;
  bg: string;
  fg: string;
  pulse?: boolean;
}

export const GATE_BADGE: Record<GateStatusKey, StatusStyle> = {
  cleared:     { label: "Cleared",     bg: C.passBg,    fg: C.passText },
  verified:    { label: "Verified",    bg: C.passBg,    fg: C.passText },
  investigate: { label: "Investigate", bg: C.flagBg,    fg: C.flagText },
  incomplete:  { label: "Incomplete",  bg: C.track,     fg: C.muted },
  measure:     { label: "Measure",     bg: C.measureBg, fg: C.measureText },
  escalate:    { label: "Escalate",    bg: C.measureBg, fg: C.measureText },
  pending:     { label: "Pending",     bg: C.pendBg,    fg: C.pendText },
  running:     { label: "Running",     bg: C.runBg,     fg: C.runText, pulse: true },
  failed:      { label: "Failed",      bg: C.measureBg, fg: C.measureText },
};

/** Never let an unrecognised status break a table or badge. */
export function gateStyle(v: string | null | undefined): StatusStyle {
  if (!v) return { label: "\u2014", bg: "transparent", fg: C.muted };
  return GATE_BADGE[v as GateStatusKey] ?? { label: v, bg: C.track, fg: C.muted };
}

/** True when the gate has cleared the plan to treat without a dry run. */
export function isCleared(v: string | null | undefined): boolean {
  return v === "cleared" || v === "verified";
}


// ---- reusable style fragments ----

export const cardStyle: CSSProperties = {
  background: C.cardBg,
  border: `0.5px solid ${C.border}`,
  borderRadius: 10,
  padding: "12px 14px",
};

export const secLabelStyle: CSSProperties = {
  fontSize: 11,
  fontWeight: 500,
  color: C.muted,
  textTransform: "uppercase",
  letterSpacing: ".04em",
  marginBottom: 10,
};

export const badgeStyle = (bg: string, text: string): CSSProperties => ({
  display: "inline-flex",
  alignItems: "center",
  gap: 4,
  padding: "3px 9px",
  borderRadius: 20,
  fontSize: 11,
  fontWeight: 500,
  background: bg,
  color: text,
});

export const btnStyle: CSSProperties = {
  background: "transparent",
  border: `0.5px solid ${C.borderStrong}`,
  borderRadius: 6,
  padding: "7px 12px",
  fontSize: 12,
  cursor: "pointer",
  color: C.text,
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
};

/** Format a number with thousands separators, falling back to a dash. */
export function fmt(n: number | null | undefined, digits = 0): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return n.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}
