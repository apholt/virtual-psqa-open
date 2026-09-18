import type { CSSProperties, ReactNode } from "react";
import { C, cardStyle, secLabelStyle } from "../theme";

/** White card with the standard border / radius / padding. */
export function Card({
  children,
  style,
  className,
}: {
  children: ReactNode;
  style?: CSSProperties;
  className?: string;
}) {
  return (
    <div className={className} style={{ ...cardStyle, ...style }}>
      {children}
    </div>
  );
}

/** Uppercase section label sitting above content. */
export function SectionLabel({
  children,
  style,
}: {
  children: ReactNode;
  style?: CSSProperties;
}) {
  return <div style={{ ...secLabelStyle, ...style }}>{children}</div>;
}

/** Universal stat row: label left, monospace value right. */
export function StatRow({
  label,
  value,
  valueColor,
  last,
}: {
  label: ReactNode;
  value: ReactNode;
  valueColor?: string;
  last?: boolean;
}) {
  return (
    <div
      style={{
        display: "flex",
        justifyContent: "space-between",
        alignItems: "baseline",
        padding: "5px 0",
        borderBottom: last ? "none" : `0.5px solid ${C.border}`,
        fontSize: 12,
      }}
    >
      <span style={{ color: C.muted }}>{label}</span>
      <span
        style={{
          fontWeight: 500,
          fontFamily: "monospace",
          fontSize: 11,
          color: valueColor ?? C.text,
          textAlign: "right",
        }}
      >
        {value}
      </span>
    </div>
  );
}

/** Thin inline progress bar (passing-rate style). */
export function ProgressBar({
  value,
  color,
  width = 60,
  height = 4,
}: {
  value: number; // 0..100
  color: string;
  width?: number | string;
  height?: number;
}) {
  return (
    <span
      style={{
        height,
        width,
        background: C.track,
        borderRadius: 2,
        display: "inline-block",
        verticalAlign: "middle",
      }}
    >
      <span
        style={{
          display: "block",
          height: "100%",
          borderRadius: 2,
          width: `${Math.max(0, Math.min(100, value))}%`,
          background: color,
        }}
      />
    </span>
  );
}
