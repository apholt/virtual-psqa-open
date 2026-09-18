"""
spc_position.py  --  statistical process control on delivery accuracy.

Plan 17 LA was delivered 21 times with identical geometry and failed log-QA
gamma on exactly three consecutive days (23-25 June 2026). Mean radial spot
position error tripled on those days -- 0.31, 0.32, 0.38 mm against a 0.10-0.14
mm baseline -- and returned to baseline afterwards. MU delivery was unremarkable
throughout. The failure was a positioning excursion on GR1, and it was visible
in the treatment records alone.

That is a detector, and it needs no labels, no failures and no model: establish
the in-control distribution of a delivery metric per machine, then flag
deliveries that fall outside it.

Limits are ROBUST (median +/- k * 1.4826 * MAD) rather than mean/SD, because an
excursion present in the data would inflate an SD-based limit and hide itself.

Western Electric rules applied:
  Rule 1  a single point beyond 3 sigma
  Rule 2  2 of 3 consecutive points beyond 2 sigma on one side
  Rule 3  8 consecutive points on one side of the centre line

Usage (from virtual-psqa\\backend):
    ..\\python\\python.exe spc_position.py
    ..\\python\\python.exe spc_position.py --metric mu_deviation_pct
    ..\\python\\python.exe spc_position.py --machine TR1_Franklin_GR1
    ..\\python\\python.exe spc_position.py --by-beam
    ..\\python\\python.exe spc_position.py --csv spc.csv
"""

import argparse
import csv
import math
import os
import sqlite3
import sys
from collections import defaultdict

DB = r"data\psqa.db"

METRICS = [
    "pos_mean_radial_mm",
    "pos_p95_radial_mm",
    "pos_max_radial_mm",
    "mu_deviation_pct",
    "mu_err_mean_abs_pct",
]

# A single spot matched to the wrong plan spot produces an enormous
# pos_max_radial_mm while the mean and p95 stay normal. That is a
# reconstruction matching artifact, not a delivery excursion.
MATCH_ARTIFACT_MM = 10.0

MAD_TO_SIGMA = 1.4826


def median(v):
    s = sorted(v)
    n = len(s)
    if n == 0:
        return None
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def robust_limits(v, k=3.0):
    """(centre, sigma) from median and MAD."""
    c = median(v)
    if c is None:
        return None, None
    mad = median([abs(x - c) for x in v])
    sigma = MAD_TO_SIGMA * mad if mad else 0.0
    if sigma == 0.0:
        # Degenerate spread: fall back to SD so limits are not zero-width.
        m = sum(v) / len(v)
        sigma = math.sqrt(sum((x - m) ** 2 for x in v) / max(1, len(v) - 1))
    return c, sigma


def western_electric(vals, centre, sigma):
    """Return {index: [rule names]} for violations."""
    flags = defaultdict(list)
    if sigma <= 0:
        return flags
    z = [(x - centre) / sigma for x in vals]

    for i, zi in enumerate(z):
        if abs(zi) > 3:
            flags[i].append("R1 beyond 3s")

    for i in range(len(z) - 2):
        w = z[i:i + 3]
        for sign in (1, -1):
            if sum(1 for x in w if sign * x > 2) >= 2:
                for j, x in enumerate(w):
                    if sign * x > 2:
                        flags[i + j].append("R2 2of3 beyond 2s")

    run, sign = 0, 0
    for i, zi in enumerate(z):
        s = 1 if zi > 0 else (-1 if zi < 0 else 0)
        if s and s == sign:
            run += 1
        else:
            sign, run = s, 1
        if run >= 8:
            for j in range(i - 7, i + 1):
                if "R3 run of 8" not in flags[j]:
                    flags[j].append("R3 run of 8")
    return flags


def bar(value, lo, hi, width=34):
    if hi <= lo:
        return ""
    f = (value - lo) / (hi - lo)
    f = max(0.0, min(1.0, f))
    n = int(round(f * (width - 1)))
    return "." * n + "|" + "." * (width - 1 - n)


def load(metric, machine_filter):
    cx = sqlite3.connect(DB)
    q = (f"SELECT machine, beam_name, treatment_date, plan_id, "
         f"fraction_number, {metric}, pos_max_radial_mm, gamma_passing_rate "
         f"FROM beam_deliveries WHERE record_incomplete = 0 "
         f"AND {metric} IS NOT NULL AND treatment_date IS NOT NULL")
    if machine_filter:
        q += " AND machine = ?"
        rows = cx.execute(q + " ORDER BY treatment_date, plan_id",
                          (machine_filter,)).fetchall()
    else:
        rows = cx.execute(q + " ORDER BY treatment_date, plan_id").fetchall()
    cx.close()
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--metric", default="pos_mean_radial_mm", choices=METRICS)
    ap.add_argument("--machine", default=None)
    ap.add_argument("--by-beam", action="store_true",
                    help="separate chart per machine+beam rather than machine")
    ap.add_argument("--k", type=float, default=3.0)
    ap.add_argument("--csv", default=None)
    args = ap.parse_args()

    if not os.path.exists(DB):
        sys.exit(f"FATAL: {DB} not found - run from virtual-psqa\\backend")

    rows = load(args.metric, args.machine)
    if not rows:
        sys.exit("no rows -- has migrate_add_machine_beam.py been run?")

    artifacts = [r for r in rows
                 if r[6] is not None and r[6] > MATCH_ARTIFACT_MM]
    clean = [r for r in rows
             if not (r[6] is not None and r[6] > MATCH_ARTIFACT_MM)]

    print(f"metric = {args.metric}")
    print(f"{len(clean)} deliveries charted")
    if artifacts:
        print(f"\n{len(artifacts)} row(s) excluded as reconstruction matching "
              f"artifacts (pos_max_radial_mm > {MATCH_ARTIFACT_MM} mm):")
        for m, b, d, p, fx, v, mx, g in artifacts:
            print(f"  plan {p} fx {fx} {b} {d}: max radial {mx:.1f} mm "
                  f"(mean-based metrics normal -- one spot mismatched)")

    groups = defaultdict(list)
    for m, b, d, p, fx, v, mx, g in clean:
        key = (f"plan {p}", m, b) if args.by_beam else (m,)
        groups[key].append((d, p, fx, b, float(v), g))

    out_rows = []
    for key in sorted(groups):
        series = sorted(groups[key], key=lambda t: (t[0], t[1], t[2]))
        vals = [t[4] for t in series]
        if len(vals) < 8:
            print(f"\n{' / '.join(key)}: only {len(vals)} deliveries, skipped")
            continue

        centre, sigma = robust_limits(vals, args.k)
        ucl = centre + args.k * sigma
        lcl = max(0.0, centre - args.k * sigma) if "pos_" in args.metric \
            else centre - args.k * sigma
        flags = western_electric(vals, centre, sigma)

        print("\n" + "=" * 78)
        print(" / ".join(key))
        print("=" * 78)
        print(f"  centre (median) {centre:.4f}   sigma (MAD) {sigma:.4f}   "
              f"UCL {ucl:.4f}   LCL {lcl:.4f}")
        lo = min(min(vals), lcl)
        hi = max(max(vals), ucl)
        print(f"  {'date':<10}{'plan':>5}{'fx':>4}{'beam':>6}{'value':>10}"
              f"  {'chart':<34} flags")

        for i, (d, p, fx, b, v, g) in enumerate(series):
            fl = ", ".join(flags.get(i, []))
            mark = "  <<< " + fl if fl else ""
            print(f"  {d:<10}{p:>5}{fx:>4}{b:>6}{v:>10.4f}  "
                  f"{bar(v, lo, hi):<34}{mark}")
            out_rows.append({
                "machine": key[0],
                "beam": b,
                "date": d,
                "plan_id": p,
                "fraction_number": fx,
                "metric": args.metric,
                "value": round(v, 5),
                "centre": round(centre, 5),
                "sigma": round(sigma, 5),
                "ucl": round(ucl, 5),
                "violations": fl,
                "gamma_passing_rate": g,
            })

        viol = sorted(flags)
        if viol:
            print(f"\n  {len(viol)} out-of-control point(s):")
            for i in viol:
                d, p, fx, b, v, g = series[i]
                gs = f", gamma {g:.1f}%" if g is not None else ""
                print(f"    {d} plan {p} fx {fx} {b}: {v:.4f} "
                      f"({(v-centre)/sigma:+.1f} sigma{gs}) "
                      f"[{', '.join(flags[i])}]")

            # Do the out-of-control points coincide with gamma failures?
            oc_g = [series[i][5] for i in viol if series[i][5] is not None]
            ic_g = [t[5] for j, t in enumerate(series)
                    if j not in flags and t[5] is not None]
            if oc_g and ic_g:
                print(f"\n  mean gamma, out-of-control : "
                      f"{sum(oc_g)/len(oc_g):.2f} %  (n={len(oc_g)})")
                print(f"  mean gamma, in-control     : "
                      f"{sum(ic_g)/len(ic_g):.2f} %  (n={len(ic_g)})")
                if sum(oc_g)/len(oc_g) < sum(ic_g)/len(ic_g) - 1.0:
                    print("  >>> Out-of-control deliveries show materially lower")
                    print("  >>> gamma. The chart detects what the gamma flags.")
        else:
            print("\n  no out-of-control points")

    if args.csv and out_rows:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
            w.writeheader()
            w.writerows(out_rows)
        print(f"\nwrote {args.csv} ({len(out_rows)} rows)")


if __name__ == "__main__":
    main()
