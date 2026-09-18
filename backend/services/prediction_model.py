"""
services/prediction_model.py -- pre-treatment gamma prediction, part 1.

Two things, both read-only:

  1. ROOM SYSTEMATIC MODEL -- what deviation does each room currently apply to
     a planned spot list? This is the perturbation that turns a plan into a
     predicted delivery, and it is measured from delivered fractions rather
     than assumed.

  2. MC-vs-LOG CALIBRATION -- does the pre-treatment MCsquare gamma already
     track the log-vs-Rx gamma that follows? If it does, much of the predictive
     work is already done and the perturbation model only needs to tighten it.

WHY A PHYSICAL MODEL RATHER THAN A TRAINED ONE
----------------------------------------------
Every input here is a measured machine property, so a wrong prediction can be
traced to a wrong term. There is no training set, so there is no
leave-one-plan-out collapse, and it works from the first plan rather than
needing twenty. This is the room-model approach of Toscano et al. (Phys Med
Biol 2019;64:095021), not a learned predictor.

WHAT IT CAN AND CANNOT PREDICT
------------------------------
It predicts the REPRODUCIBLE component of delivery deviation. It cannot
predict transient excursions, and the residuals will show that plainly: on
plan 17 beam LA the inputs are identical across all 21 fractions, so the
prediction is identical, while fractions 7-9 failed and the rest passed. That
is the point of measuring residuals rather than asserting the limitation.

Usage (from the backend directory):
    python -m services.prediction_model systematics
    python -m services.prediction_model calibration
"""
from __future__ import annotations

import argparse
import math
import os
import sqlite3
import statistics
import sys
from dataclasses import dataclass
from typing import Any, Optional

# Criteria live in gate.py so this module and the gate cannot drift apart.
from services import gate

DEFAULT_DB = "./data/psqa.db"

#: Reconstruction artifact guard, matching routers/monitoring.py.
MATCH_ARTIFACT_MM = 10.0

#: Minimum deliveries before a room's systematic model is usable.
MIN_DELIVERIES = 10


#: Mean radial distance of a zero-mean 2D Gaussian with per-axis sigma s is
#: s*sqrt(pi/2). Inverting recovers the per-SPOT scatter from the stored
#: per-beam mean radial error.
_RAYLEIGH_MEAN = math.sqrt(math.pi / 2.0)

#: Mean absolute value of a zero-mean normal with sigma s is s*sqrt(2/pi).
_HALFNORMAL_MEAN = math.sqrt(2.0 / math.pi)


@dataclass
class Systematic:
    """The reproducible deviation a room applies to a planned spot list.

    Two components, and the distinction is what makes the model work:

      SYSTEMATIC (dx_mm, dy_mm, mu_pct) -- the mean offset applied to every
      spot. This is a near-uniform translation of the whole field, and gamma
      at 2 mm DTA is almost blind to it: a +0.03 mm shift of everything moves
      the dose distribution by 1.5% of the DTA tolerance. On its own it would
      predict ~100% for every plan.

      PER-SPOT SCATTER (spot_sigma_mm, spot_mu_sigma_pct) -- the spread of
      individual spots about that mean. This is what actually degrades gamma,
      because neighbouring spots move in different directions and the summed
      profile ripples. Plan 17 failed at a mean radial error of 0.31 mm, far
      inside 2 mm DTA, for exactly this reason.

    Scatter is recovered from the stored per-beam summaries: beam_deliveries
    holds mean radial error and mean absolute MU error, not per-spot standard
    deviations, so the underlying sigma is inverted from those moments.
    """
    machine: str
    n: int
    dx_mm: float
    dy_mm: float
    dx_sd: float
    dy_sd: float
    mu_pct: float
    mu_sd: float
    #: Spread of per-beam means about the room mean. Large values mean the
    #: deviation is beam/angle dependent and a single room-level offset is
    #: too coarse a model.
    dx_between_beam_sd: float
    dy_between_beam_sd: float
    mu_between_beam_sd: float
    #: Per-spot positional scatter (mm, per axis), from mean radial error.
    #: Defaulted fields must come last -- see dataclass field ordering.
    spot_sigma_mm: float = 0.0
    #: Per-spot MU scatter (% of prescribed), from mean absolute MU error.
    spot_mu_sigma_pct: float = 0.0

    @property
    def angle_dependent(self) -> bool:
        """True when between-beam spread rivals within-beam scatter."""
        return (self.dx_between_beam_sd > self.dx_sd
                or self.dy_between_beam_sd > self.dy_sd)

    def to_dict(self) -> dict[str, Any]:
        return {
            "machine": self.machine, "n": self.n,
            "dx_mm": round(self.dx_mm, 4), "dy_mm": round(self.dy_mm, 4),
            "dx_sd": round(self.dx_sd, 4), "dy_sd": round(self.dy_sd, 4),
            "mu_pct": round(self.mu_pct, 4), "mu_sd": round(self.mu_sd, 4),
            "spot_sigma_mm": round(self.spot_sigma_mm, 4),
            "spot_mu_sigma_pct": round(self.spot_mu_sigma_pct, 4),
            "angle_dependent": self.angle_dependent,
        }


def _rows(db_path: str, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Database not found: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _sd(vals: list[float]) -> float:
    return statistics.stdev(vals) if len(vals) > 1 else 0.0


# ---------------------------------------------------------------------------
# 1. Room systematic model
# ---------------------------------------------------------------------------

def room_systematics(db_path: str = DEFAULT_DB) -> dict[str, Systematic]:
    """Measure each room's reproducible spot-position and MU deviation.

    Aggregates the signed per-beam-delivery means already stored by the log
    reconstructor. Signed dx/dy matter here, not radial distance: radial error
    is unsigned and cannot say which way to move a spot, so it is the wrong
    quantity to build a perturbation from.
    """
    rows = _rows(db_path, """
        -- Retained rows (plan_id < 0) are deliberately INCLUDED: they are
        -- machine history, which is the whole point of retaining them.
        SELECT machine, beam_name, plan_id,
               pos_mean_dx_mm, pos_mean_dy_mm, mu_deviation_pct,
               pos_mean_radial_mm, mu_err_mean_abs_pct
        FROM beam_deliveries
        WHERE COALESCE(record_incomplete, 0) = 0
          AND not_scored IS NULL
          AND machine IS NOT NULL
          AND (pos_max_radial_mm IS NULL OR pos_max_radial_mm <= ?)
    """, (MATCH_ARTIFACT_MM,))

    by_machine: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_machine.setdefault(r["machine"], []).append(r)

    out: dict[str, Systematic] = {}
    for machine, rs in by_machine.items():
        dx = [r["pos_mean_dx_mm"] for r in rs if r["pos_mean_dx_mm"] is not None]
        dy = [r["pos_mean_dy_mm"] for r in rs if r["pos_mean_dy_mm"] is not None]
        mu = [r["mu_deviation_pct"] for r in rs if r["mu_deviation_pct"] is not None]
        if len(mu) < MIN_DELIVERIES:
            continue

        # Between-beam spread: how much the per-beam means differ from each
        # other. If this is large the room-level mean is an inadequate model.
        by_beam: dict[tuple, list[sqlite3.Row]] = {}
        for r in rs:
            by_beam.setdefault((r["plan_id"], r["beam_name"]), []).append(r)

        def beam_means(field: str) -> list[float]:
            means = []
            for grp in by_beam.values():
                vals = [g[field] for g in grp if g[field] is not None]
                if vals:
                    means.append(statistics.fmean(vals))
            return means

        # Per-spot scatter, inverted from the stored per-beam moments.
        radial = [r["pos_mean_radial_mm"] for r in rs
                  if r["pos_mean_radial_mm"] is not None]
        mu_abs = [r["mu_err_mean_abs_pct"] for r in rs
                  if r["mu_err_mean_abs_pct"] is not None]
        spot_sigma = (statistics.fmean(radial) / _RAYLEIGH_MEAN) if radial else 0.0
        spot_mu_sigma = (statistics.fmean(mu_abs) / _HALFNORMAL_MEAN) if mu_abs else 0.0

        out[machine] = Systematic(
            machine=machine, n=len(mu),
            spot_sigma_mm=spot_sigma, spot_mu_sigma_pct=spot_mu_sigma,
            dx_mm=statistics.fmean(dx) if dx else 0.0,
            dy_mm=statistics.fmean(dy) if dy else 0.0,
            dx_sd=_sd(dx), dy_sd=_sd(dy),
            mu_pct=statistics.fmean(mu), mu_sd=_sd(mu),
            dx_between_beam_sd=_sd(beam_means("pos_mean_dx_mm")),
            dy_between_beam_sd=_sd(beam_means("pos_mean_dy_mm")),
            mu_between_beam_sd=_sd(beam_means("mu_deviation_pct")),
        )
    return out


# ---------------------------------------------------------------------------
# 2. MC-vs-log calibration
# ---------------------------------------------------------------------------

@dataclass
class Pairing:
    plan_id: int
    beam: str
    mc_gamma: Optional[float]
    log_n: int
    log_mean: Optional[float]
    log_min: Optional[float]
    machine: Optional[str]

    @property
    def residual(self) -> Optional[float]:
        if self.mc_gamma is None or self.log_mean is None:
            return None
        return self.log_mean - self.mc_gamma


def mc_vs_log(db_path: str = DEFAULT_DB,
              criterion: Optional[tuple[float, float]] = None) -> list[Pairing]:
    """Pair each beam's pre-treatment MC gamma with the log gamma that followed.

    Only MC results computed at `criterion` are paired: a 3%/3mm result is not
    comparable to a 2%/2mm log gamma, and pairing them would manufacture an
    apparent relationship out of a criterion difference.
    """
    # Defaults to the secondary-dose criterion in gate.py, so this analysis
    # follows the configured criterion rather than a hardcoded copy of it.
    dd, dta = criterion or (gate.GAMMA_DD_PERCENT, gate.GAMMA_DTA_MM)
    mc_rows = _rows(db_path, """
        SELECT plan_id, field_name, passing_rate FROM gamma_results g
        WHERE comparison_type = 'mcSquare_vs_TPS'
          AND ABS(dd_percent - ?) < 0.01 AND ABS(dta_mm - ?) < 0.01
          AND id = (SELECT MAX(id) FROM gamma_results
                    WHERE plan_id = g.plan_id
                      AND comparison_type = g.comparison_type
                      AND field_name = g.field_name)
    """, (dd, dta))
    mc = {(r["plan_id"], r["field_name"]): r["passing_rate"] for r in mc_rows}

    log_rows = _rows(db_path, """
        SELECT plan_id, beam_name, machine, gamma_passing_rate
        FROM beam_deliveries
        WHERE COALESCE(record_incomplete, 0) = 0 AND not_scored IS NULL
          AND gamma_passing_rate IS NOT NULL
    """)
    logs: dict[tuple, list[float]] = {}
    machines: dict[tuple, str] = {}
    for r in log_rows:
        key = (r["plan_id"], r["beam_name"])
        logs.setdefault(key, []).append(float(r["gamma_passing_rate"]))
        if r["machine"]:
            machines[key] = r["machine"]

    keys = sorted(set(mc) | set(logs))
    out: list[Pairing] = []
    for k in keys:
        vals = logs.get(k, [])
        out.append(Pairing(
            plan_id=k[0], beam=k[1],
            mc_gamma=mc.get(k),
            log_n=len(vals),
            log_mean=statistics.fmean(vals) if vals else None,
            log_min=min(vals) if vals else None,
            machine=machines.get(k),
        ))
    return out


def correlation(pairs: list[Pairing]) -> Optional[float]:
    xs = [p.mc_gamma for p in pairs if p.residual is not None]
    ys = [p.log_mean for p in pairs if p.residual is not None]
    if len(xs) < 3:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = math.sqrt(sum((x - mx) ** 2 for x in xs)
                    * sum((y - my) ** 2 for y in ys))
    return num / den if den else None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _report_systematics(db_path: str) -> None:
    sysd = room_systematics(db_path)
    if not sysd:
        print("\nNot enough delivered fractions to model any room "
              f"(need >= {MIN_DELIVERIES} per room).\n")
        return
    print("\nROOM SYSTEMATIC MODEL -- measured perturbation per room\n")
    for m, s in sorted(sysd.items()):
        print(f"=== {m} ===  ({s.n} beam deliveries)")
        print(f"  spot dx : {s.dx_mm:+.4f} mm   (within-beam SD {s.dx_sd:.4f}, "
              f"between-beam SD {s.dx_between_beam_sd:.4f})")
        print(f"  spot dy : {s.dy_mm:+.4f} mm   (within-beam SD {s.dy_sd:.4f}, "
              f"between-beam SD {s.dy_between_beam_sd:.4f})")
        print(f"  MU      : {s.mu_pct:+.4f} %   (within-beam SD {s.mu_sd:.4f}, "
              f"between-beam SD {s.mu_between_beam_sd:.4f})")
        print()
        print(f"  per-spot scatter (what actually degrades gamma):")
        print(f"    position : sigma {s.spot_sigma_mm:.4f} mm per axis")
        print(f"    MU       : sigma {s.spot_mu_sigma_pct:.4f} % of prescribed")
        if s.angle_dependent:
            print("  NOTE: between-beam spread exceeds within-beam scatter, so")
            print("        the deviation is beam/angle dependent. A single")
            print("        room-level offset will be too coarse; the model")
            print("        should be conditioned on gantry angle.")
        print()


def _report_calibration(db_path: str) -> None:
    pairs = mc_vs_log(db_path)
    paired = [p for p in pairs if p.residual is not None]
    print(f"\nMC-vs-LOG CALIBRATION  "
          f"(MC at {gate.GAMMA_CRITERION} only, log at "
          f"{gate.LOG_GAMMA_CRITERION})\n")
    print(f"{'plan':>5} {'beam':<6} {'machine':<20} {'MC':>7} {'n':>4} "
          f"{'log mean':>9} {'log min':>8} {'resid':>7}")
    print("-" * 74)
    for p in pairs:
        mcs = f"{p.mc_gamma:.1f}" if p.mc_gamma is not None else "--"
        lm = f"{p.log_mean:.1f}" if p.log_mean is not None else "--"
        lmin = f"{p.log_min:.1f}" if p.log_min is not None else "--"
        res = f"{p.residual:+.1f}" if p.residual is not None else "--"
        print(f"{p.plan_id:>5} {p.beam:<6} {(p.machine or '--'):<20} "
              f"{mcs:>7} {p.log_n:>4} {lm:>9} {lmin:>8} {res:>7}")

    print()
    if len(paired) < 3:
        print(f"Only {len(paired)} beam(s) have BOTH a "
              f"{gate.GAMMA_CRITERION} MC result and delivered fractions.")
        print(f"Recalculate off-criterion plans at {gate.GAMMA_CRITERION} to")
        print("make the rest of the evidence base comparable.")
        print()
        return

    resids = [p.residual for p in paired]
    r = correlation(paired)
    print(f"paired beams      : {len(paired)}")
    print(f"residual mean     : {statistics.fmean(resids):+.2f} % "
          f"(log mean minus MC)")
    print(f"residual SD       : {_sd(resids):.2f} %")
    print(f"residual range    : {min(resids):+.2f} to {max(resids):+.2f} %")
    if r is not None:
        print(f"Pearson r         : {r:+.3f}")
        if abs(r) < 0.3:
            print()
            print("Weak correlation: pre-treatment MC gamma does not track the")
            print("log gamma that follows. That is informative rather than")
            print("disappointing -- it says the two measure different things,")
            print("and that a perturbation model is needed if a pre-treatment")
            print("number in log-gamma units is wanted.")
    # Beams that later failed, and what MC said about them beforehand.
    failed = [p for p in paired if p.log_min is not None and p.log_min < 90.0]
    if failed:
        print()
        print("Beams with a sub-90% delivery, and their pre-treatment MC:")
        for p in failed:
            print(f"  plan {p.plan_id} {p.beam}: MC {p.mc_gamma:.1f}%  ->  "
                  f"log min {p.log_min:.1f}% over {p.log_n} deliveries")
        clean = [p.mc_gamma for p in paired if p not in failed]
        if clean:
            print(f"  MC gamma of beams that never failed: "
                  f"{min(clean):.1f} to {max(clean):.1f}% "
                  f"(mean {statistics.fmean(clean):.1f}%)")
            print()
            print("  If the failed beams' MC gamma sits inside that range, the")
            print("  failures were not visible pre-treatment -- which is the")
            print("  transient-failure argument, in numbers.")
    print()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Pre-treatment gamma prediction.")
    ap.add_argument("mode",
                    choices=["systematics", "calibration", "predict", "table",
                             "store", "both"],
                    nargs="?", default="both")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--plan", type=int, default=None,
                    help="plan id (required for 'predict')")
    ap.add_argument("--machine", default=None,
                    help="room model to use, e.g. TR1_Franklin_GR1")
    ap.add_argument("--realizations", type=int, default=DEFAULT_REALIZATIONS)
    args = ap.parse_args(argv)

    if args.mode == "store":
        _report_store(args.db, args.plan, args.realizations)
        return 0
    if args.mode == "table":
        _report_table(args.db, args.realizations)
        return 0
    if args.mode == "predict":
        if args.plan is None:
            print("predict mode needs --plan <id>")
            return 1
        _report_prediction(args.db, args.plan, args.machine, args.realizations)
        return 0
    if args.mode in ("systematics", "both"):
        _report_systematics(args.db)
    if args.mode in ("calibration", "both"):
        _report_calibration(args.db)
    return 0


if __name__ == "__main__":
    sys.exit(main())


# ---------------------------------------------------------------------------
# 3. Predicted per-field gamma
# ---------------------------------------------------------------------------
#
# Runs through the SAME code path as a real delivery: the prescribed profile
# and the predicted profile are both built by
# services.log_reconstruction._reconstruct_profile, and scored by
# services.log_gamma.gamma_log_vs_rx at the same 2%/2mm/0.5mm recipe the
# clinical log QA uses. Reimplementing either would make the predicted and
# actual gammas incomparable, which would defeat the purpose.

#: Realizations per beam. The prediction is a distribution, not a point: the
#: per-spot scatter is random, so a single draw is one sample of the outcome.
DEFAULT_REALIZATIONS = 12


@dataclass
class BeamPrediction:
    beam_name: str
    machine: str
    n_spots: int
    realizations: list[float]

    @property
    def mean(self) -> float:
        return statistics.fmean(self.realizations)

    @property
    def sd(self) -> float:
        return _sd(self.realizations)

    @property
    def worst(self) -> float:
        return min(self.realizations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "beam_name": self.beam_name, "machine": self.machine,
            "n_spots": self.n_spots, "n_realizations": len(self.realizations),
            "predicted_gamma_mean": round(self.mean, 2),
            "predicted_gamma_sd": round(self.sd, 2),
            "predicted_gamma_worst": round(self.worst, 2),
        }


def predict_plan_gamma(plan_path: str, systematic: Systematic,
                       n_realizations: int = DEFAULT_REALIZATIONS,
                       seed: Optional[int] = 0) -> list[BeamPrediction]:
    """Predict per-beam log-vs-Rx gamma for a plan that has not been delivered.

    For each beam: build the prescribed profile from the plan's own spots, then
    build predicted delivered profiles by applying the room's systematic offset
    plus a per-spot random draw from the measured scatter, and gamma each one
    against the prescription.

    Note what this does NOT model. The draw is independent per spot, so it
    reproduces the reproducible statistics of the room and nothing else. A
    transient excursion changes the scatter itself, and no pre-delivery input
    carries that information -- on plan 17 the inputs are identical for
    fraction 6 and fraction 7. The spread across realizations is therefore the
    predictable variation only, and comparing it to observed failures is the
    point of the exercise rather than a limitation of it.
    """
    import numpy as np
    import pydicom
    from services.log_reconstruction import (
        _extract_prescribed_spots, _reconstruct_profile,
    )
    from services.log_gamma import gamma_log_vs_rx
    from services.log_reconstructor import (
        LOG_GAMMA_DD_PERCENT, LOG_GAMMA_DTA_MM, LOG_GAMMA_RESOLUTION_MM,
    )

    rng = np.random.default_rng(seed)
    plan_ds = pydicom.dcmread(plan_path, force=True)
    out: list[BeamPrediction] = []

    for idx, beam in enumerate(plan_ds.IonBeamSequence):
        name = str(getattr(beam, "BeamName", f"beam{idx}")).split(":")[0].strip()
        rx = _extract_prescribed_spots(plan_ds, idx)
        if rx.shape[0] == 0:
            continue

        sig_x = rx[:, 2] / 2.355
        sig_y = rx[:, 3] / 2.355
        prescribed = _reconstruct_profile(rx[:, 4], rx[:, 5], sig_x, sig_y,
                                          rx[:, 1])

        rates: list[float] = []
        for _ in range(n_realizations):
            n = rx.shape[0]
            x = (rx[:, 4] + systematic.dx_mm
                 + rng.normal(0.0, systematic.spot_sigma_mm, n))
            y = (rx[:, 5] + systematic.dy_mm
                 + rng.normal(0.0, systematic.spot_sigma_mm, n))
            mu_scale = 1.0 + (systematic.mu_pct
                              + rng.normal(0.0, systematic.spot_mu_sigma_pct, n)
                              ) / 100.0
            mu = rx[:, 1] * mu_scale

            predicted = _reconstruct_profile(x, y, sig_x, sig_y, mu)
            _gmap, rate, _fails, roi = gamma_log_vs_rx(
                predicted, prescribed,
                dd_percent=LOG_GAMMA_DD_PERCENT,
                dta_mm=LOG_GAMMA_DTA_MM,
                resolution_mm=LOG_GAMMA_RESOLUTION_MM,
            )
            if roi > 0:
                rates.append(float(rate))

        if rates:
            out.append(BeamPrediction(beam_name=name,
                                      machine=systematic.machine,
                                      n_spots=int(rx.shape[0]),
                                      realizations=rates))
    return out


def plan_machine(db_path: str, plan_id: int) -> Optional[str]:
    """The room this plan was actually delivered on, if any.

    Defaulting to the busiest room instead of the plan's own would apply the
    wrong scatter: GR2's per-spot sigma is roughly twice GR1's, so the two
    rooms give materially different predictions for the same plan.
    """
    rows = _rows(db_path,
                 "SELECT machine, COUNT(*) n FROM beam_deliveries "
                 "WHERE plan_id = ? AND machine IS NOT NULL "
                 "GROUP BY machine ORDER BY n DESC LIMIT 1", (plan_id,))
    return rows[0]["machine"] if rows else None


def _resolve_systematic(db_path: str, plan_id: int, machine: Optional[str],
                        sysd: dict[str, Systematic]) -> Optional[Systematic]:
    name = machine or plan_machine(db_path, plan_id)
    if name and name in sysd:
        return sysd[name]
    if not sysd:
        return None
    # Undelivered plan: no room of its own yet. Use the room with the LARGEST
    # scatter, since that is the conservative prediction -- an optimistic room
    # model would understate the expected degradation.
    return max(sysd.values(), key=lambda s: s.spot_sigma_mm)


def _report_prediction(db_path: str, plan_id: int, machine: Optional[str],
                       n_real: int) -> None:
    import sqlite3 as _sq
    rows = _rows(db_path,
                 "SELECT dicom_store_path, rtplan_uid, plan_label "
                 "FROM plans WHERE id = ?", (plan_id,))
    if not rows:
        print(f"No plan with id {plan_id}")
        return
    plan = rows[0]

    from services.evidence import _find_rtplan
    plan_path = _find_rtplan(plan["dicom_store_path"], plan["rtplan_uid"])
    if not plan_path:
        print(f"Could not locate the RT Ion Plan for plan {plan_id}.")
        return

    sysd = room_systematics(db_path)
    sysm = _resolve_systematic(db_path, plan_id, machine, sysd)
    if sysm is None:
        print("No room has enough delivered fractions to build a model yet.")
        return
    own = plan_machine(db_path, plan_id)
    if own and own != sysm.machine:
        print(f"NOTE: plan was delivered on {own}, predicting with "
              f"{sysm.machine}.")
    elif not own:
        print(f"Plan not yet delivered; using {sysm.machine} "
              f"(largest per-spot scatter, i.e. the conservative room).")

    print(f"\nPREDICTED per-field gamma -- plan {plan_id} "
          f"({plan['plan_label']})")
    print(f"room model: {sysm.machine}  "
          f"(dx {sysm.dx_mm:+.3f}, dy {sysm.dy_mm:+.3f} mm, "
          f"MU {sysm.mu_pct:+.3f}%, spot sigma {sysm.spot_sigma_mm:.3f} mm)")
    print(f"{n_real} realizations per beam, {gate.LOG_GAMMA_CRITERION} "
          f"vs prescription\n")

    preds = predict_plan_gamma(plan_path, sysm, n_real)
    if not preds:
        print("No beams predicted.")
        return
    print(f"{'beam':<8} {'spots':>7} {'mean':>8} {'sd':>7} {'worst':>8}")
    print("-" * 42)
    for p in preds:
        print(f"{p.beam_name:<8} {p.n_spots:>7} {p.mean:>7.1f}% "
              f"{p.sd:>6.2f}% {p.worst:>7.1f}%")

    # Compare against what this plan actually delivered, if anything has.
    actual = _rows(db_path,
                   "SELECT beam_name, COUNT(*) n, AVG(gamma_passing_rate) avg, "
                   "       MIN(gamma_passing_rate) min "
                   "FROM beam_deliveries WHERE plan_id = ? "
                   "  AND COALESCE(record_incomplete,0)=0 AND not_scored IS NULL "
                   "  AND gamma_passing_rate IS NOT NULL GROUP BY beam_name",
                   (plan_id,))
    if actual:
        by_beam = {r["beam_name"]: r for r in actual}
        print()
        print("versus delivered:")
        print(f"{'beam':<8} {'pred':>8} {'act mean':>9} {'act min':>8} "
              f"{'mean res':>9} {'MIN RES':>8} {'n':>4}")
        print("-" * 60)
        for p in preds:
            a = by_beam.get(p.beam_name)
            if not a:
                continue
            print(f"{p.beam_name:<8} {p.mean:>7.1f}% {a['avg']:>8.1f}% "
                  f"{a['min']:>7.1f}% {a['avg'] - p.mean:>+8.1f}% "
                  f"{p.mean - a['min']:>7.1f}% {a['n']:>4}")
        print()
        print("MIN RES (prediction minus worst delivery) is the number that")
        print("carries the finding. A transient failure barely moves the mean")
        print("-- plan 17 LA delivered normally 18 times out of 21 -- so the")
        print("mean residual hides it and the minimum exposes it.")
    print()


# ---------------------------------------------------------------------------
# 4. Calibration table across all delivered plans
# ---------------------------------------------------------------------------

def _report_table(db_path: str, n_real: int) -> None:
    """One row per delivered plan-beam: prediction vs what was delivered.

    This is the calibration study in a single table. The quantity of interest
    is MIN RES -- prediction minus the worst delivered fraction -- because a
    transient failure barely disturbs the mean.
    """
    from services.evidence import _find_rtplan

    sysd = room_systematics(db_path)
    if not sysd:
        print("No room has enough delivered fractions to build a model yet.")
        return

    plans = _rows(db_path, """
        SELECT DISTINCT p.id, p.plan_label, p.dicom_store_path, p.rtplan_uid
        FROM plans p JOIN beam_deliveries b ON b.plan_id = p.id
        WHERE COALESCE(b.record_incomplete,0)=0 AND b.not_scored IS NULL
          AND b.gamma_passing_rate IS NOT NULL
        ORDER BY p.id
    """)

    print(f"\nCALIBRATION TABLE -- predicted vs delivered, "
          f"{n_real} realizations per beam, {gate.LOG_GAMMA_CRITERION}\n")
    print(f"{'plan':>5} {'label':<12} {'beam':<6} {'room':<20} {'pred':>7} "
          f"{'act mean':>9} {'act min':>8} {'MIN RES':>8} {'n':>4} {'fail':>5}")
    print("-" * 96)

    rows_out: list[tuple] = []
    for pl in plans:
        path = _find_rtplan(pl["dicom_store_path"], pl["rtplan_uid"])
        if not path:
            print(f"{pl['id']:>5} {pl['plan_label'][:12]:<12} "
                  f"(RT Ion Plan not found)")
            continue
        sysm = _resolve_systematic(db_path, pl["id"], None, sysd)
        if sysm is None:
            continue
        try:
            preds = predict_plan_gamma(path, sysm, n_real)
        except Exception as exc:  # noqa: BLE001
            print(f"{pl['id']:>5} {pl['plan_label'][:12]:<12} ERROR {exc}")
            continue

        actual = _rows(db_path,
                       "SELECT beam_name, COUNT(*) n, AVG(gamma_passing_rate) avg, "
                       "       MIN(gamma_passing_rate) min "
                       "FROM beam_deliveries WHERE plan_id = ? "
                       "  AND COALESCE(record_incomplete,0)=0 "
                       "  AND not_scored IS NULL "
                       "  AND gamma_passing_rate IS NOT NULL GROUP BY beam_name",
                       (pl["id"],))
        by_beam = {r["beam_name"]: r for r in actual}

        for p in preds:
            a = by_beam.get(p.beam_name)
            if not a:
                continue
            min_res = p.mean - a["min"]
            failed = a["min"] < 90.0
            rows_out.append((min_res, failed, pl["id"], p.beam_name))
            print(f"{pl['id']:>5} {pl['plan_label'][:12]:<12} "
                  f"{p.beam_name:<6} {sysm.machine[:20]:<20} "
                  f"{p.mean:>6.1f}% {a['avg']:>8.1f}% {a['min']:>7.1f}% "
                  f"{min_res:>7.1f}% {a['n']:>4} {'YES' if failed else '':>5}")

    if not rows_out:
        print("\nNo plan-beams with both a prediction and delivered fractions.")
        return

    failed = sorted(r[0] for r in rows_out if r[1])
    clean = sorted(r[0] for r in rows_out if not r[1])
    print()
    print(f"beams analysed        : {len(rows_out)}  "
          f"({len(failed)} with a sub-90% delivery)")
    if clean:
        print(f"MIN RES, never failed : {min(clean):.1f} to {max(clean):.1f} %")
    if failed:
        print(f"MIN RES, failed       : {min(failed):.1f} to {max(failed):.1f} %")
    if clean and failed:
        gap = min(failed) - max(clean)
        print(f"separation            : {gap:+.1f} % between the worst clean "
              f"beam and the best failing one")
        if gap > 0:
            print()
            print("The two populations do not overlap on this metric. That is")
            print("the calibration result: delivery failures sit outside the")
            print("predictable component by a margin, so no pre-treatment")
            print("model built from reproducible machine behaviour could have")
            print("anticipated them.")
    print()


# ---------------------------------------------------------------------------
# 5. Storage -- predictions computed once at plan level, read many times
# ---------------------------------------------------------------------------
#
# Prediction costs seconds per beam, so it cannot run on a page load. It is
# computed once per plan and stored, together with the room-model parameters
# that produced it: a prediction made against a stale room model must be
# identifiable as such, not silently trusted.

_SCHEMA = """
CREATE TABLE IF NOT EXISTS gamma_predictions (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id                 INTEGER NOT NULL,
    beam_name               TEXT NOT NULL,
    n_spots                 INTEGER,
    n_realizations          INTEGER,
    predicted_mean          REAL,
    predicted_sd            REAL,
    predicted_worst         REAL,
    model_machine           TEXT,
    model_n_deliveries      INTEGER,
    model_dx_mm             REAL,
    model_dy_mm             REAL,
    model_mu_pct            REAL,
    model_spot_sigma_mm     REAL,
    model_spot_mu_sigma_pct REAL,
    criterion               TEXT,
    created_at              TEXT NOT NULL,
    UNIQUE (plan_id, beam_name)
)
"""


def ensure_schema(db_path: str = DEFAULT_DB) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def store_plan_prediction(plan_id: int, db_path: str = DEFAULT_DB,
                          n_real: int = DEFAULT_REALIZATIONS,
                          machine: Optional[str] = None) -> int:
    """Predict every beam of a plan and store the result. Returns beams stored.

    Safe to re-run: rows are replaced, so recomputing after the room model has
    moved simply refreshes them.
    """
    from services.evidence import _find_rtplan

    ensure_schema(db_path)
    rows = _rows(db_path, "SELECT dicom_store_path, rtplan_uid FROM plans "
                          "WHERE id = ?", (plan_id,))
    if not rows:
        return 0
    path = _find_rtplan(rows[0]["dicom_store_path"], rows[0]["rtplan_uid"])
    if not path:
        logger_msg = f"plan {plan_id}: RT Ion Plan not found; no prediction"
        print(logger_msg)
        return 0

    sysd = room_systematics(db_path)
    sysm = _resolve_systematic(db_path, plan_id, machine, sysd)
    if sysm is None:
        return 0

    preds = predict_plan_gamma(path, sysm, n_real)
    if not preds:
        return 0

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        # Clear any rows for this plan id first: a reused id may carry the
        # previous plan's beams, which UPSERT alone would leave behind.
        conn.execute("DELETE FROM gamma_predictions WHERE plan_id = ?",
                     (plan_id,))
        for p in preds:
            conn.execute("""
                INSERT INTO gamma_predictions
                    (plan_id, beam_name, n_spots, n_realizations,
                     predicted_mean, predicted_sd, predicted_worst,
                     model_machine, model_n_deliveries, model_dx_mm,
                     model_dy_mm, model_mu_pct, model_spot_sigma_mm,
                     model_spot_mu_sigma_pct, criterion, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(plan_id, beam_name) DO UPDATE SET
                    n_spots=excluded.n_spots,
                    n_realizations=excluded.n_realizations,
                    predicted_mean=excluded.predicted_mean,
                    predicted_sd=excluded.predicted_sd,
                    predicted_worst=excluded.predicted_worst,
                    model_machine=excluded.model_machine,
                    model_n_deliveries=excluded.model_n_deliveries,
                    model_dx_mm=excluded.model_dx_mm,
                    model_dy_mm=excluded.model_dy_mm,
                    model_mu_pct=excluded.model_mu_pct,
                    model_spot_sigma_mm=excluded.model_spot_sigma_mm,
                    model_spot_mu_sigma_pct=excluded.model_spot_mu_sigma_pct,
                    criterion=excluded.criterion,
                    created_at=excluded.created_at
            """, (plan_id, p.beam_name, p.n_spots, len(p.realizations),
                  round(p.mean, 3), round(p.sd, 3), round(p.worst, 3),
                  sysm.machine, sysm.n, round(sysm.dx_mm, 5),
                  round(sysm.dy_mm, 5), round(sysm.mu_pct, 5),
                  round(sysm.spot_sigma_mm, 5),
                  round(sysm.spot_mu_sigma_pct, 5),
                  gate.LOG_GAMMA_CRITERION, now))
        conn.commit()
    finally:
        conn.close()
    return len(preds)


def plan_predictions(plan_id: int, db_path: str = DEFAULT_DB) -> list[dict]:
    """Stored predictions for a plan, joined to what has been delivered."""
    try:
        rows = _rows(db_path, """
            SELECT p.beam_name, p.n_spots, p.n_realizations,
                   p.predicted_mean, p.predicted_sd, p.predicted_worst,
                   p.model_machine, p.model_spot_sigma_mm, p.criterion,
                   p.created_at,
                   (SELECT COUNT(*) FROM beam_deliveries b
                     WHERE b.plan_id = p.plan_id AND b.beam_name = p.beam_name
                       AND COALESCE(b.record_incomplete,0)=0
                       AND b.not_scored IS NULL
                       AND b.gamma_passing_rate IS NOT NULL) AS delivered_n,
                   (SELECT AVG(gamma_passing_rate) FROM beam_deliveries b
                     WHERE b.plan_id = p.plan_id AND b.beam_name = p.beam_name
                       AND COALESCE(b.record_incomplete,0)=0
                       AND b.not_scored IS NULL) AS delivered_mean,
                   (SELECT MIN(gamma_passing_rate) FROM beam_deliveries b
                     WHERE b.plan_id = p.plan_id AND b.beam_name = p.beam_name
                       AND COALESCE(b.record_incomplete,0)=0
                       AND b.not_scored IS NULL) AS delivered_min
            FROM gamma_predictions p
            WHERE p.plan_id = ?
            ORDER BY p.beam_name
        """, (plan_id,))
    except sqlite3.OperationalError:
        return []  # table not created yet

    # Drop rows whose beam is not in the CURRENT plan. plans.id is INTEGER
    # PRIMARY KEY without AUTOINCREMENT, so SQLite reissues the id of a
    # deleted plan to the next one ingested; without this check a new plan
    # silently inherits the previous occupant's predictions, which is exactly
    # what happened when an FB prostate plan's LL/RL rows surfaced on an
    # unrelated head-and-neck plan.
    live = _live_beam_names(plan_id, db_path)
    out = []
    for r in rows:
        d = dict(r)
        if live is not None and d["beam_name"] not in live:
            continue
        if d["delivered_min"] is not None and d["predicted_mean"] is not None:
            d["min_residual"] = round(d["predicted_mean"] - d["delivered_min"], 2)
        else:
            d["min_residual"] = None
        out.append(d)
    return out


def _live_beam_names(plan_id: int, db_path: str) -> Optional[set[str]]:
    """Beam names actually present in this plan's RT Ion Plan.

    Returns None if the plan file cannot be read, in which case the caller
    should not filter -- refusing to display is better than displaying wrong
    data, but so is not hiding good data over a transient read failure.
    """
    try:
        import pydicom
        from services.evidence import _find_rtplan
        rows = _rows(db_path, "SELECT dicom_store_path, rtplan_uid FROM plans "
                              "WHERE id = ?", (plan_id,))
        if not rows:
            return set()
        path = _find_rtplan(rows[0]["dicom_store_path"], rows[0]["rtplan_uid"])
        if not path:
            return None
        ds = pydicom.dcmread(path, force=True)
        return {str(b.BeamName).split(":")[0].strip()
                for b in ds.IonBeamSequence}
    except Exception:  # noqa: BLE001
        return None


def _report_store(db_path: str, plan_id: Optional[int], n_real: int) -> None:
    if plan_id is not None:
        ids = [plan_id]
    else:
        ids = [r["id"] for r in _rows(db_path, "SELECT id FROM plans ORDER BY id")]
    total = 0
    for pid in ids:
        try:
            n = store_plan_prediction(pid, db_path, n_real)
        except Exception as exc:  # noqa: BLE001
            print(f"plan {pid}: ERROR {exc}")
            continue
        if n:
            print(f"plan {pid}: stored {n} beam prediction(s)")
            total += n
    print(f"\n{total} beam prediction(s) stored.\n")
