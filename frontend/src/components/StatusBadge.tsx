import type { QAStatus } from "../types";
import { badgeStyle, gateStyle } from "../theme";

interface Props {
  status: QAStatus | string | null;
}

/**
 * GATE_STATUS_V1 -- single source of truth for status styling.
 *
 * The label/colour map moved to theme.ts::GATE_BADGE so this badge, the
 * dashboard table and the plan header cannot drift apart. Unknown values
 * render as themselves rather than breaking the row.
 */
export function StatusBadge({ status }: Props) {
  const cfg = gateStyle(status);
  return (
    <span style={badgeStyle(cfg.bg, cfg.fg)}>
      {cfg.pulse && (
        <span
          className="live-dot"
          style={{ background: cfg.fg, width: 6, height: 6 }}
        />
      )}
      {cfg.label}
    </span>
  );
}
