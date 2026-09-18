"""
spot_deviation.py  --  Model-free delivery accuracy from RT Ion Records.

Compares SpecifiedMeterset against DeliveredMeterset at every control point,
using only what is inside the record. No dose reconstruction, no gamma, no TPS.
This isolates the question "did the machine deliver what it was told" from
every modelling artifact downstream of it.

Purpose: determine whether the log-QA target has any variance at all. If
deviations are uniformly negligible, there is nothing to predict and that is
the finding. If deviations are real but the gamma still reads ~99.7%, the
gamma is the insensitive part.

Also flags duplicate records (same SOPInstanceUID stored under two fractions),
which inflate counts and correlate labels.

Usage (from virtual-psqa\\backend):
    ..\\python\\python.exe spot_deviation.py
    ..\\python\\python.exe spot_deviation.py --csv deviations.csv
    ..\\python\\python.exe spot_deviation.py --min-mu 0.02
"""

import argparse
import collections
import csv
import math
import os
import sqlite3
import sys

try:
    import pydicom
except ImportError:
    sys.exit("FATAL: pydicom not importable by this interpreter")

DB = r"data\psqa.db"


def pct(a, b):
    """Percent deviation of a from b; None if b is ~0."""
    if b is None or abs(b) < 1e-9:
        return None
    return 100.0 * (a - b) / b


def quantiles(vals, qs=(0.5, 0.9, 0.95, 0.99, 1.0)):
    if not vals:
        return {}
    s = sorted(vals)
    out = {}
    for q in qs:
        i = min(len(s) - 1, max(0, int(math.ceil(q * len(s)) - 1)))
        out[q] = s[i]
    return out


def analyse(ds, min_mu):
    """Return per-record stats dict."""
    cp_dev = []          # per-control-point % deviation
    spot_mu = []         # every delivered spot meterset
    n_cp = 0
    n_spots = 0
    n_zero_spots = 0
    n_small_spots = 0
    spec_total = 0.0
    deliv_total = 0.0

    beams = getattr(ds, "TreatmentSessionIonBeamSequence", []) or []
    for beam in beams:
        cps = getattr(beam, "IonControlPointDeliverySequence", []) or []
        prev_spec = 0.0
        prev_deliv = 0.0
        for cp in cps:
            n_cp += 1
            spec_cum = getattr(cp, "SpecifiedMeterset", None)
            deliv_cum = getattr(cp, "DeliveredMeterset", None)

            if spec_cum is not None and deliv_cum is not None:
                spec_cum = float(spec_cum)
                deliv_cum = float(deliv_cum)
                # Cumulative in the standard; take the increment.
                d_spec = spec_cum - prev_spec
                d_deliv = deliv_cum - prev_deliv
                if d_spec < 0 or d_deliv < 0:
                    # Not cumulative after all - treat as per-CP values.
                    d_spec, d_deliv = spec_cum, deliv_cum
                prev_spec, prev_deliv = spec_cum, deliv_cum

                p = pct(d_deliv, d_spec)
                if p is not None:
                    cp_dev.append(p)
                spec_total += max(d_spec, 0.0)
                deliv_total += max(d_deliv, 0.0)

            spots = getattr(cp, "ScanSpotMetersetsDelivered", None)
            if spots is not None:
                try:
                    vals = [float(v) for v in spots]
                except TypeError:
                    vals = [float(spots)]
                n_spots += len(vals)
                spot_mu.extend(vals)
                n_zero_spots += sum(1 for v in vals if v == 0.0)
                n_small_spots += sum(1 for v in vals if 0.0 < v < min_mu)

    total_dev = pct(deliv_total, spec_total)
    absdev = [abs(d) for d in cp_dev]
    return {
        "n_beams": len(beams),
        "n_cp": n_cp,
        "n_spots": n_spots,
        "n_zero_spots": n_zero_spots,
        "n_small_spots": n_small_spots,
        "spec_total_mu": round(spec_total, 4),
        "deliv_total_mu": round(deliv_total, 4),
        "total_pct_dev": None if total_dev is None else round(total_dev, 5),
        "cp_max_abs_pct": round(max(absdev), 5) if absdev else None,
        "cp_mean_abs_pct": round(sum(absdev) / len(absdev), 5) if absdev else None,
        "_cp_dev": cp_dev,
        "_spot_mu": spot_mu,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-mu", type=float, default=0.01,
                    help="spots below this delivered MU counted as 'small'")
    ap.add_argument("--csv", default=None, help="write per-fraction rows here")
    args = ap.parse_args()

    if not os.path.exists(DB):
        sys.exit(f"FATAL: {DB} not found - run from virtual-psqa\\backend")

    cx = sqlite3.connect(DB)
    frs = cx.execute(
        "SELECT id, plan_id, fraction_number, rtrecord_path FROM fractions "
        "WHERE rtrecord_path IS NOT NULL ORDER BY plan_id, fraction_number"
    ).fetchall()
    cx.close()

    rows = []
    all_cp_dev = []
    all_spot_mu = []
    uids = collections.defaultdict(list)

    print(f"{'fid':>4} {'plan':>5} {'fx':>4} {'spots':>7} {'totdev%':>9} "
          f"{'cpmax%':>9} {'zero':>6} {'small':>6}")
    print("-" * 60)

    for fid, plan_id, fx, path in frs:
        if not os.path.exists(path):
            print(f"{fid:>4} {plan_id:>5} {fx:>4}   MISSING")
            continue
        try:
            ds = pydicom.dcmread(path)
        except Exception as e:
            print(f"{fid:>4} {plan_id:>5} {fx:>4}   UNREADABLE {e}")
            continue

        uid = getattr(ds, "SOPInstanceUID", None)
        if uid:
            uids[uid].append(fid)

        s = analyse(ds, args.min_mu)
        all_cp_dev.extend(s.pop("_cp_dev"))
        all_spot_mu.extend(s.pop("_spot_mu"))

        td = "n/a" if s["total_pct_dev"] is None else f"{s['total_pct_dev']:.4f}"
        cm = "n/a" if s["cp_max_abs_pct"] is None else f"{s['cp_max_abs_pct']:.4f}"
        print(f"{fid:>4} {plan_id:>5} {fx:>4} {s['n_spots']:>7} {td:>9} "
              f"{cm:>9} {s['n_zero_spots']:>6} {s['n_small_spots']:>6}")

        rows.append(dict(fraction_id=fid, plan_id=plan_id, fraction_number=fx, **s))

    # ---------- duplicates ----------
    dupes = {u: f for u, f in uids.items() if len(f) > 1}
    print("\n" + "=" * 60)
    if dupes:
        print(f"DUPLICATE RECORDS: {len(dupes)} SOPInstanceUID(s) stored twice")
        for u, f in dupes.items():
            print(f"  fractions {f}  uid={u}")
        print("  These are the same delivery counted more than once.")
    else:
        print("No duplicate SOPInstanceUIDs.")

    # ---------- distributions ----------
    print("\nCONTROL-POINT DEVIATION (delivered vs specified, %)")
    if all_cp_dev:
        a = [abs(d) for d in all_cp_dev]
        q = quantiles(a)
        print(f"  n control points : {len(a)}")
        print(f"  mean |dev|       : {sum(a)/len(a):.5f} %")
        for k, v in q.items():
            print(f"  p{int(k*100):<3}             : {v:.5f} %")
        spread = max(a) - min(a)
        print(f"  range            : {min(a):.5f} to {max(a):.5f} %")
        if max(a) < 0.5:
            print("\n  >>> No meaningful variance. Delivery tracks the plan to well")
            print("  >>> under 0.5%. There is no log-QA failure mode to predict.")
        else:
            print("\n  >>> Real deviation present. If log_vs_Rx gamma still reads")
            print("  >>> ~99.7%, the gamma is too insensitive to expose it.")
    else:
        print("  no usable Specified/Delivered pairs found")

    print("\nDELIVERED SPOT METERSET DISTRIBUTION")
    if all_spot_mu:
        nz = [v for v in all_spot_mu if v > 0]
        print(f"  total spots      : {len(all_spot_mu)}")
        print(f"  zero-MU spots    : {sum(1 for v in all_spot_mu if v == 0.0)}")
        print(f"  min nonzero MU   : {min(nz):.6f}" if nz else "  all zero")
        print(f"  median MU        : {sorted(nz)[len(nz)//2]:.6f}" if nz else "")
        print(f"  max MU           : {max(all_spot_mu):.6f}")
        print(f"  below {args.min_mu} MU   : "
              f"{sum(1 for v in all_spot_mu if 0 < v < args.min_mu)}")
    else:
        print("  no spot metersets found")

    if args.csv and rows:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"\nwrote {args.csv} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
