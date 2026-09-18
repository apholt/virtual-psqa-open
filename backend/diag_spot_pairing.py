"""
diag_spot_pairing.py -- audit log spot pairing against the vendor method.

Answers three questions on real records, without changing anything:

  1. ALIGNMENT. services/log_reconstruction.py::_spot_metrics pairs delivered
     to prescribed spots by ordinal position after truncating to the shorter
     array. If a spot is missing from the middle of a delivery, every spot
     after the gap is subtracted from the WRONG prescribed spot. The record's
     ScanSpotPrescribedIndices tells us the true mapping, so we compare.

  2. DENOMINATOR. Pass rates divide by matched spots. The vendor divides by
     all prescribed spots. Undelivered spots currently vanish instead of
     counting as failures.

  3. MU SCALING. Prescribed "MU" is ScanSpotMetersetWeights, unscaled. If
     FinalCumulativeMetersetWeight != BeamMeterset these are weights, not MU,
     and mu_deviation_pct is meaningless.

Run from the backend directory:
    ..\\python\\python.exe diag_spot_pairing.py            (all fractions)
    ..\\python\\python.exe diag_spot_pairing.py 8          (one plan)
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

import numpy as np
import pydicom


def audit_beam(plan_ds, record_ds, p_idx, r_idx, beam_name):
    from services.log_reconstruction import (
        _extract_prescribed_spots, _extract_delivered_spots)

    rx = _extract_prescribed_spots(plan_ds, p_idx)
    dv = _extract_delivered_spots(record_ds, r_idx)

    dmask = dv[:, 0] != 0
    d = dv[dmask]
    idx = d[:, 0].astype(int)

    n_rx = len(rx)
    n_dv = len(d)
    n_matched = min(n_rx, n_dv)

    print(f"  beam {beam_name}")
    print(f"    prescribed spots : {n_rx}")
    print(f"    delivered spots  : {n_dv}   (test pulses dropped: "
          f"{int((~dmask).sum())})")
    print(f"    ordinal-matched  : {n_matched}")

    # ---- 1. alignment -----------------------------------------------------
    lo, hi = int(idx.min()), int(idx.max())
    contiguous = (hi - lo + 1) == len(np.unique(idx)) == n_dv
    print(f"    PrescribedIndices: {lo}..{hi}, unique={len(np.unique(idx))}, "
          f"contiguous={contiguous}")

    # Ordinal pairing assumes delivered[k] <-> prescribed[k].
    # Index pairing says   delivered[k] <-> prescribed[idx[k] - base].
    for base in (0, 1):
        tgt = idx - base
        if tgt.min() < 0 or tgt.max() >= n_rx:
            continue
        ordinal = np.arange(n_dv)
        disagree = int(np.count_nonzero(tgt[:n_matched] != ordinal[:n_matched]))
        if disagree == 0:
            print(f"    alignment        : OK (indices are {base}-based and "
                  f"match ordinal order)")
            break
        # Quantify the damage: offsets under each pairing.
        xo_ord = d[:n_matched, 5] - rx[:n_matched, 4]
        yo_ord = d[:n_matched, 6] - rx[:n_matched, 5]
        xo_idx = d[:, 5] - rx[tgt, 4]
        yo_idx = d[:, 6] - rx[tgt, 5]
        print(f"    alignment        : *** {disagree} spot(s) MISPAIRED "
              f"({base}-based indices) ***")
        print(f"      ordinal pairing  max|dx|={np.abs(xo_ord).max():.3f} "
              f"max|dy|={np.abs(yo_ord).max():.3f} mm")
        print(f"      index   pairing  max|dx|={np.abs(xo_idx).max():.3f} "
              f"max|dy|={np.abs(yo_idx).max():.3f} mm")
        break
    else:
        print("    alignment        : indices out of range for both 0- and "
              "1-based interpretations -- inspect by hand")

    # ---- 2. denominator ---------------------------------------------------
    if n_dv < n_rx:
        xo = d[:n_matched, 5] - rx[:n_matched, 4]
        yo = d[:n_matched, 6] - rx[:n_matched, 5]
        rad = np.hypot(xo, yo)
        pass_matched = 100.0 * np.count_nonzero(rad <= 0.5) / rad.size
        pass_prescribed = 100.0 * np.count_nonzero(rad <= 0.5) / n_rx
        print(f"    denominator      : {n_rx - n_dv} spot(s) undelivered. "
              f"mag pass@0.5mm = {pass_matched:.2f}% over matched, "
              f"{pass_prescribed:.2f}% over prescribed")
    else:
        print("    denominator      : all prescribed spots delivered "
              "(no difference)")

    # ---- 3. MU scaling ----------------------------------------------------
    beam = plan_ds.IonBeamSequence[p_idx]
    try:
        fcmw = float(beam.FinalCumulativeMetersetWeight)
    except Exception:
        fcmw = None
    bmu = None
    try:
        for rbs in plan_ds.FractionGroupSequence[0].ReferencedBeamSequence:
            if int(rbs.ReferencedBeamNumber) == int(beam.BeamNumber):
                bmu = float(rbs.BeamMeterset)
                break
    except Exception:
        pass
    w_sum = float(np.sum(rx[:, 1]))
    d_sum = float(np.sum(d[:, 2]))
    print(f"    weights sum      : {w_sum:.2f}   delivered MU: {d_sum:.2f}")
    if fcmw is not None and bmu is not None:
        scale = bmu / fcmw if fcmw else float("nan")
        print(f"    FinalCumWeight={fcmw:.4f}  BeamMeterset={bmu:.2f}  "
              f"scale={scale:.4f}")
        if abs(scale - 1.0) > 1e-6:
            print(f"      *** weights are NOT MU. Scaled Rx would be "
                  f"{w_sum * scale:.2f} vs delivered {d_sum:.2f} ***")
        else:
            print("      weights are already MU (scale = 1)")
    else:
        print("    FinalCumulativeMetersetWeight / BeamMeterset unavailable")
    print()


def main() -> None:
    from database import SessionLocal
    from models.fraction import Fraction
    from models.plan import Plan
    from services.log_reconstruction import _normalize_beam_name

    want = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else None

    db = SessionLocal()
    try:
        q = db.query(Fraction).filter(Fraction.rtrecord_path.isnot(None))
        if want:
            q = q.filter(Fraction.plan_id == want)
        fracs = q.order_by(Fraction.plan_id, Fraction.fraction_number).all()
        rows = [(f.plan_id, f.fraction_number, f.rtrecord_path) for f in fracs]
        plans = {p.id: p for p in db.query(Plan).all()}
    finally:
        db.close()

    if not rows:
        print("No fractions with an RT Ion Record found.")
        return

    from services.log_reconstructor import _resolve_plan_file

    seen = set()
    for plan_id, fx, rec_path in rows:
        key = (plan_id, fx)
        if key in seen:
            continue
        seen.add(key)
        plan = plans.get(plan_id)
        if plan is None:
            continue
        try:
            plan_path = _resolve_plan_file(plan)
            plan_ds = pydicom.dcmread(plan_path, force=True)
            rec_ds = pydicom.dcmread(rec_path, force=True)
        except Exception as exc:
            print(f"plan {plan_id} fx {fx}: could not read ({exc})")
            continue

        print(f"=== plan {plan_id} ({plan.plan_label}) fx {fx} ===")
        pmap = {_normalize_beam_name(b.BeamName): i
                for i, b in enumerate(plan_ds.IonBeamSequence)}
        for r_idx, rb in enumerate(rec_ds.TreatmentSessionIonBeamSequence):
            norm = _normalize_beam_name(str(rb.BeamName))
            if norm not in pmap:
                print(f"  beam {norm}: no plan match, skipped")
                continue
            try:
                audit_beam(plan_ds, rec_ds, pmap[norm], r_idx, norm)
            except Exception as exc:
                print(f"  beam {norm}: audit failed ({exc})")


if __name__ == "__main__":
    main()
