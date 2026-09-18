"""
services/room_spc.py -- room / machine-level statistical process control.

This is evidence layer 3 of the delivery gate (services/gate.py): is the
treatment room currently in control?

WHY THIS IS NOT THE SAME AS THE PER-PLAN CHARTS
-----------------------------------------------
The per-plan-beam control charts in routers/monitoring.py baseline each beam on
its own first fractions. That is what made the plan-17 catch possible -- a
tight, geometry-specific baseline sensitive enough to flag beam LP while its
gamma was still passing at 96.6-97.4%.

But it has a structural blind spot: a slow machine-wide drift is invisible to
it, because every NEW plan establishes its baseline at whatever the machine is
doing that month. The chart re-centres on the drift and reports "in control"
forever. Room-level pooling is what closes that gap, and it is the layer that
speaks to transient machine excursions -- the failure mode plan-level evidence
structurally cannot anticipate.

POOLING POSITION ERROR CORRECTLY
--------------------------------
Absolute pos_mean_radial_mm is plan-geometry dependent: different plans have
genuinely different baselines, so pooling raw values across plans manufactures
variance from geometry rather than measuring machine drift, and the resulting
control limits would be too wide to detect anything.

So the position chart pools RESIDUALS: each beam delivery is expressed as its
deviation from that plan-beam's own median, and the residuals are pooled by
room. Geometry cancels; room-wide drift survives. A plan-beam needs at least
MIN_DELIVERIES_FOR_BASELINE deliveries before it can contribute a residual.

MU deviation is already a percentage of prescribed MU, so it is plan-
independent by construction and pools directly.

CONTROL LIMITS
--------------
Individuals / moving-range (I-MR) charts, sigma estimated as MRbar / 1.128
(d2 for n=2). This is the standard approach for individual measurements and is
robust to slow trends, which matter here -- a mean/SD estimate computed over a
window that already contains the drift would widen the limits to accommodate
it. Signal rules are a subset of the Nelson rules (see _signals).

ABSOLUTE LIMITS
---------------
Relative SPC answers "has the machine changed?" but not "is the machine within
the commissioned envelope?" A machine that drifted before the window opened is
in control relative to itself. Optional absolute limits close that; they are
null by default and reported as not configured rather than silently passing.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import statistics
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

#: d2 for subgroup size 2 -- converts MEAN moving range to a sigma estimate.
_D2_N2 = 1.128

#: d4 for subgroup size 2 -- converts MEDIAN moving range to a sigma estimate.
#: The median form is used here for robustness; see _imr_limits.
_D4_MEDIAN_N2 = 0.954

#: Deliveries in the trailing window used to judge current room state.
DEFAULT_WINDOW = 30

#: Minimum deliveries in the window before the chart is considered established.
MIN_FOR_ESTABLISHED = 15

#: A plan-beam needs this many deliveries before its median is a usable
#: baseline for residual computation.
MIN_DELIVERIES_FOR_BASELINE = 3

#: Optional absolute limits. Fill from commissioning; null means not checked.
ABSOLUTE_LIMITS: dict[str, Optional[float]] = {
    "pos_mean_radial_mm_max": None,
    "mu_deviation_pct_abs_max": None,
}


@dataclass
class Delivery:
    plan_id: int
    fraction_number: int
    beam_name: str
    machine: Optional[str]
    treatment_date: Optional[str]
    pos_mean_radial_mm: Optional[float]
    mu_deviation_pct: Optional[float]
    gamma_passing_rate: Optional[float]

    @property
    def key(self) -> tuple[int, str]:
        return (self.plan_id, self.beam_name)


@dataclass
class Chart:
    metric: str
    unit: str
    centre: Optional[float] = None
    sigma: Optional[float] = None
    ucl: Optional[float] = None
    lcl: Optional[float] = None
    n: int = 0
    established: bool = False
    values: list[float] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    dates: list[Optional[str]] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    minor_signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "unit": self.unit,
            "centre": None if self.centre is None else round(self.centre, 4),
            "sigma": None if self.sigma is None else round(self.sigma, 4),
            "ucl": None if self.ucl is None else round(self.ucl, 4),
            "lcl": None if self.lcl is None else round(self.lcl, 4),
            "n": self.n,
            "established": self.established,
            "signals": list(self.signals),
            "minor_signals": list(self.minor_signals),
            "points": [
                {"label": lab, "value": round(val, 4)}
                for lab, val in zip(self.labels, self.values)
            ],
        }


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

_SQL = """
SELECT plan_id, fraction_number, beam_name, machine, treatment_date,
       pos_mean_radial_mm, mu_deviation_pct, gamma_passing_rate
FROM beam_deliveries
WHERE COALESCE(record_incomplete, 0) = 0
  AND not_scored IS NULL
  AND machine IS NOT NULL
ORDER BY treatment_date, fraction_number, id
"""


def load_deliveries(db_path: str = "./data/psqa.db") -> list[Delivery]:
    """Read scored beam deliveries. Incomplete/unscored records are excluded:
    an aborted delivery with zero meterset is not a machine-state observation.
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Database not found: {db_path}")
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(_SQL).fetchall()
    finally:
        conn.close()
    return [Delivery(*r) for r in rows]


# ---------------------------------------------------------------------------
# Chart construction
# ---------------------------------------------------------------------------

def _imr_limits(values: list[float]) -> tuple[Optional[float], Optional[float]]:
    """Return (centre, sigma) from a ROBUST individuals/moving-range estimate.

    Median centre and median moving range, rather than mean and mean-MR.

    This matters more than it looks. Limits here are computed from a window
    that may already contain the excursion being looked for, and mean-based
    estimators let the excursion contaminate its own limits twice over: it
    drags the centre toward itself, and it inflates sigma. The visible symptom
    is spurious run-rule signals -- with a shifted centre, every in-control
    point falls on one side, tripping "8 consecutive on the same side" even
    though nothing drifted.

    The median centre and median moving range have ~50% breakdown, so a
    minority excursion moves them very little and the event shows up as
    out-of-limit points instead of corrupting the baseline.
    """
    if len(values) < 2:
        return (values[0] if values else None), None
    moving_ranges = [abs(values[i] - values[i - 1])
                     for i in range(1, len(values))]
    mr_med = statistics.median(moving_ranges)
    sigma = mr_med / _D4_MEDIAN_N2 if mr_med > 0 else 0.0
    return statistics.median(values), sigma


#: A signal must span at least this many distinct treatment dates before it
#: blocks. See _signals for why.
MIN_DATES_FOR_MAJOR = 2

#: PRACTICAL SIGNIFICANCE FLOORS -- clinical thresholds, not statistical ones.
#:
#: Residual charts strip out plan geometry, leaving only within-plan-beam
#: fraction-to-fraction scatter. On real SC360 data that scatter has sigma of
#: order 0.01 mm, putting the 3-sigma limit near 0.03 mm -- far below anything
#: dosimetrically relevant. Without a floor the chart blocks treatment on
#: deviations that are statistically real and clinically meaningless, and every
#: room reads out of control permanently.
#:
#: A deviation must therefore exceed BOTH the statistical limit AND the floor
#: below to count toward a signal. This mirrors the tolerance/action-limit
#: distinction in AAPM TG-218 and the practical-significance principle in
#: machine-QA SPC.
#:
#: DEFAULTS BELOW REQUIRE PHYSICIST SIGN-OFF. The position floor is set at
#: roughly half the plan-17 excursion (which moved mean radial error ~0.2 mm
#: and did produce gamma failures), so that event is caught with margin while
#: sub-0.1 mm noise is not. The MU floor is a placeholder pending the
#: commissioned output tolerance.
PRACTICAL_FLOOR: dict[str, float] = {
    "position_residual_mm": 0.10,
    "mu_deviation_pct": 1.00,
}


def _signals(values: list[float], labels: list[str],
             dates: list[Optional[str]],
             centre: float, sigma: float,
             floor: float = 0.0) -> tuple[list[str], list[str]]:
    """Nelson-rule subset, returning (major, minor) signal summaries.

    Signals are SUMMARISED, not emitted per point: one excursion spanning three
    treatment days trips rule 1 on every affected beam delivery and rule 2 on
    every overlapping triple, and listing each instance buries the finding.

    MAJOR vs MINOR is the clinically important distinction, and it is not a
    statistical one. At 3 sigma with several rules over a 30-40 point window,
    isolated false alarms are expected at roughly one in ten windows per room.
    A gate that blocks treatment on that noise gets overridden, and an
    overridden gate is worse than no gate.

    The real excursion this layer exists to catch spanned three consecutive
    treatment days across two beams. Isolated statistical noise does not. So a
    signal is MAJOR only if it spans at least MIN_DATES_FOR_MAJOR distinct
    treatment dates; otherwise it is MINOR -- reported for review, but not
    blocking. Sustained drift (rule 3) is inherently multi-day and always
    major.
    """
    major: list[str] = []
    minor: list[str] = []
    if sigma <= 0:
        return major, minor

    def distinct_dates(idxs: list[int]) -> int:
        return len({dates[i] for i in idxs if dates[i]}) or len(idxs)

    # Rule 1 -- beyond 3 sigma AND beyond the practical floor.
    beyond3 = [i for i, v in enumerate(values)
               if abs(v - centre) > 3 * sigma and abs(v - centre) > floor]
    if beyond3:
        worst = max(beyond3, key=lambda i: abs(values[i] - centre))
        nd = distinct_dates(beyond3)
        msg = (f"{len(beyond3)} point(s) beyond 3 sigma across {nd} "
               f"treatment date(s) (worst {values[worst]:+.3f} at "
               f"{labels[worst]})")
        (major if nd >= MIN_DATES_FOR_MAJOR else minor).append(msg)

    # Rule 2 -- 2 of 3 consecutive points beyond 2 sigma on the same side.
    rule2_idx: list[int] = []
    for i in range(len(values) - 2):
        window = values[i:i + 3]
        for sign in (1, -1):
            hits = [v for v in window
                    if sign * (v - centre) > 2 * sigma
                    and abs(v - centre) > floor]
            if len(hits) >= 2:
                rule2_idx.append(i)
                break
    if rule2_idx:
        nd = distinct_dates(rule2_idx)
        msg = (f"{len(rule2_idx)} run(s) of 2-of-3 beyond 2 sigma across "
               f"{nd} treatment date(s) (first at {labels[rule2_idx[0]]})")
        (major if nd >= MIN_DATES_FOR_MAJOR else minor).append(msg)

    # Rule 3 -- 8 consecutive points on the same side of centre (drift).
    # Inherently sustained, so always major.
    run_sign = 0
    run_len = 0
    for i, val in enumerate(values):
        sign = 1 if val > centre else (-1 if val < centre else 0)
        if sign != 0 and sign == run_sign:
            run_len += 1
        else:
            run_sign, run_len = sign, 1
        if run_len >= 8:
            # A sustained run only matters if the run itself sits a
            # practically meaningful distance from centre.
            run_vals = values[max(0, i - run_len + 1):i + 1]
            mean_offset = abs(statistics.fmean(run_vals) - centre)
            if mean_offset > floor:
                side = "above" if run_sign > 0 else "below"
                major.append(f"8 consecutive points {side} centre through "
                             f"{labels[i]} (sustained shift, mean offset "
                             f"{mean_offset:+.3f})")
                break  # one drift report is enough
            run_sign, run_len = 0, 0

    return major, minor


def _build_chart(metric: str, unit: str, values: list[float],
                 labels: list[str],
                 dates: Optional[list[Optional[str]]] = None,
                 floor: float = 0.0) -> Chart:
    dates = dates if dates is not None else [None] * len(values)
    chart = Chart(metric=metric, unit=unit, values=values, labels=labels,
                  dates=dates, n=len(values))
    if not values:
        return chart
    centre, sigma = _imr_limits(values)
    chart.centre = centre
    chart.sigma = sigma
    chart.established = len(values) >= MIN_FOR_ESTABLISHED
    if centre is not None and sigma is not None:
        chart.ucl = centre + 3 * sigma
        chart.lcl = centre - 3 * sigma
        if chart.established:
            chart.signals, chart.minor_signals = _signals(
                values, labels, dates, centre, sigma, floor)
    return chart


# ---------------------------------------------------------------------------
# Room state
# ---------------------------------------------------------------------------

def position_residuals(deliveries: Iterable[Delivery]) -> dict[int, float]:
    """Map beam-delivery index -> residual from its plan-beam median.

    Baselines use each plan-beam's FULL history, not just the window, so a
    residual is measured against that beam's own established behaviour rather
    than against the window it sits in.
    """
    by_key: dict[tuple[int, str], list[float]] = {}
    ordered = list(deliveries)
    for d in ordered:
        if d.pos_mean_radial_mm is None:
            continue
        by_key.setdefault(d.key, []).append(d.pos_mean_radial_mm)

    baselines = {
        key: statistics.median(vals)
        for key, vals in by_key.items()
        if len(vals) >= MIN_DELIVERIES_FOR_BASELINE
    }

    residuals: dict[int, float] = {}
    for idx, d in enumerate(ordered):
        if d.pos_mean_radial_mm is None:
            continue
        base = baselines.get(d.key)
        if base is None:
            continue
        residuals[idx] = d.pos_mean_radial_mm - base
    return residuals


def room_state(deliveries: list[Delivery], machine: str,
               window: int = DEFAULT_WINDOW,
               absolute_limits: Optional[dict[str, Optional[float]]] = None,
               practical_floor: Optional[dict[str, float]] = None
               ) -> dict[str, Any]:
    """Evaluate one room. Returns the payload services/gate.py expects.

    gate.evaluate_machine_state consumes:
        {"machine", "in_control", "signals", "n_fractions"}
    Extra keys are passed through for display and are ignored by the gate.
    """
    limits = dict(ABSOLUTE_LIMITS)
    if absolute_limits:
        limits.update(absolute_limits)

    room = [d for d in deliveries if d.machine == machine]
    residuals = position_residuals(room)

    recent = room[-window:] if window else room
    offset = len(room) - len(recent)

    pos_vals: list[float] = []
    pos_labels: list[str] = []
    pos_dates: list[Optional[str]] = []
    mu_vals: list[float] = []
    mu_labels: list[str] = []
    mu_dates: list[Optional[str]] = []

    for i, d in enumerate(recent):
        label = f"p{d.plan_id}/{d.beam_name}/fx{d.fraction_number}"
        resid = residuals.get(offset + i)
        if resid is not None:
            pos_vals.append(resid)
            pos_labels.append(label)
            pos_dates.append(d.treatment_date)
        if d.mu_deviation_pct is not None:
            mu_vals.append(d.mu_deviation_pct)
            mu_labels.append(label)
            mu_dates.append(d.treatment_date)

    floors = dict(PRACTICAL_FLOOR)
    if practical_floor:
        floors.update(practical_floor)

    pos_chart = _build_chart("position residual (vs plan-beam baseline)",
                             "mm", pos_vals, pos_labels, pos_dates,
                             floors.get("position_residual_mm", 0.0))
    mu_chart = _build_chart("MU deviation", "%", mu_vals, mu_labels, mu_dates,
                            floors.get("mu_deviation_pct", 0.0))

    signals: list[str] = []
    signals.extend(f"position: {s}" for s in pos_chart.signals)
    signals.extend(f"MU: {s}" for s in mu_chart.signals)
    advisories: list[str] = []
    advisories.extend(f"position: {s}" for s in pos_chart.minor_signals)
    advisories.extend(f"MU: {s}" for s in mu_chart.minor_signals)

    # Absolute limits -- relative SPC cannot see a drift that predates the
    # window, so these are the commissioned-envelope backstop.
    unconfigured: list[str] = []
    pos_abs = limits.get("pos_mean_radial_mm_max")
    if pos_abs is None:
        unconfigured.append("pos_mean_radial_mm_max")
    else:
        over = [d for d in recent
                if d.pos_mean_radial_mm is not None
                and d.pos_mean_radial_mm > float(pos_abs)]
        if over:
            worst = max(over, key=lambda d: d.pos_mean_radial_mm)
            signals.append(
                f"position: {len(over)} delivery(ies) above absolute limit "
                f"{pos_abs} mm (worst {worst.pos_mean_radial_mm:.3f} mm)")
    mu_abs = limits.get("mu_deviation_pct_abs_max")
    if mu_abs is None:
        unconfigured.append("mu_deviation_pct_abs_max")
    else:
        over = [d for d in recent
                if d.mu_deviation_pct is not None
                and abs(d.mu_deviation_pct) > float(mu_abs)]
        if over:
            worst = max(over, key=lambda d: abs(d.mu_deviation_pct))
            signals.append(
                f"MU: {len(over)} delivery(ies) beyond absolute limit "
                f"+/-{mu_abs}% (worst {worst.mu_deviation_pct:+.2f}%)")

    established = pos_chart.established or mu_chart.established
    in_control: Optional[bool] = None if not established else (len(signals) == 0)

    return {
        "machine": machine,
        "in_control": in_control,
        "signals": signals,
        "advisories": advisories,
        "n_fractions": len({(d.plan_id, d.fraction_number) for d in recent}),
        "n_deliveries": len(recent),
        "window": window,
        "established": established,
        "absolute_limits_unconfigured": unconfigured,
        "practical_floor": floors,
        "charts": [pos_chart.to_dict(), mu_chart.to_dict()],
    }


def combine_states(states: list[dict[str, Any]], label: str) -> dict[str, Any]:
    """Combine several rooms into one machine-state answer.

    Used before the first delivery, when the plan names only a machine class
    ("ProNova SC360 GR") and not which gantry will treat. The safe reading of
    "is the room in control?" is then "is EVERY room that could deliver this in
    control?" -- clearing a plan because one gantry is healthy would be wrong
    when it may be treated on the other.

    A room whose chart is not yet established contributes no opinion; if none
    of the candidates is established, the combined state is undetermined
    (in_control None) and the gate treats the layer as unavailable.
    """
    established = [s for s in states if s.get("established")]
    if not established:
        return {"machine": label, "in_control": None, "signals": [],
                "n_fractions": 0, "established": False, "rooms": [
                    s.get("machine") for s in states]}
    signals: list[str] = []
    for st in established:
        signals.extend(f"{st['machine']}: {sig}" for sig in st.get("signals", []))
    return {
        "machine": label,
        "in_control": len(signals) == 0,
        "signals": signals,
        "n_fractions": sum(s.get("n_fractions", 0) for s in established),
        "established": True,
        "rooms": [s.get("machine") for s in established],
    }


def all_rooms(deliveries: list[Delivery],
              window: int = DEFAULT_WINDOW) -> dict[str, dict[str, Any]]:
    machines = sorted({d.machine for d in deliveries if d.machine})
    return {m: room_state(deliveries, m, window) for m in machines}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _report(state: dict[str, Any]) -> None:
    ic = state["in_control"]
    verdict = ("IN CONTROL" if ic else
               "OUT OF CONTROL" if ic is False else "NOT ESTABLISHED")
    print(f"=== {state['machine']} === {verdict}")
    print(f"  window          : last {state['n_deliveries']} deliveries "
          f"({state['n_fractions']} fractions)")
    for c in state["charts"]:
        if c["n"] == 0:
            print(f"  {c['metric']:44s}: no data")
            continue
        print(f"  {c['metric']} [{c['unit']}]")
        print(f"    n={c['n']}  centre={c['centre']}  sigma={c['sigma']}  "
              f"UCL={c['ucl']}  LCL={c['lcl']}"
              f"{'' if c['established'] else '  (baseline forming)'}")
    if state["signals"]:
        print("  SIGNALS (blocking):")
        for s in state["signals"]:
            print(f"    - {s}")
    else:
        print("  no blocking control-chart signals")
    if state.get("advisories"):
        print("  advisory (single treatment date, not blocking):")
        for s in state["advisories"]:
            print(f"    - {s}")
    if state["absolute_limits_unconfigured"]:
        print(f"  absolute limits not configured: "
              f"{', '.join(state['absolute_limits_unconfigured'])}")
    pf = state.get("practical_floor") or {}
    if pf:
        print(f"  practical floors: position {pf.get('position_residual_mm')} mm, "
              f"MU {pf.get('mu_deviation_pct')}%  (clinical thresholds -- "
              f"require physicist sign-off)")
    print()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--db", default="./data/psqa.db")
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    ap.add_argument("--machine", default=None)
    args = ap.parse_args(argv)

    deliveries = load_deliveries(args.db)
    print(f"\nLoaded {len(deliveries)} scored beam deliveries from {args.db}\n")
    if not deliveries:
        print("Nothing to chart.")
        return 0

    if args.machine:
        _report(room_state(deliveries, args.machine, args.window))
    else:
        for state in all_rooms(deliveries, args.window).values():
            _report(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
