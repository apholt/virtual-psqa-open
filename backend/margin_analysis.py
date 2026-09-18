"""
margin_analysis.py  --  can the pre-treatment dry run be skipped?

The question is NOT "will this plan fail log QA". It never has: 2%/2mm gamma
runs 98-100% against a 90% action level, so the binary label is constant and
unlearnable. The question is whether the MARGIN to that action level is ever
eroded by anything knowable before delivery.

If margin is uniformly large and uncorrelated with plan content, then the
pre-treatment delivery is not informative about the plan -- it is re-measuring
a property of the delivery system. That is the argument for skipping it, and it
is made from the absence of a relationship, not from a trained model.

If instead some plan characteristic does track margin, that characteristic
defines which plans still warrant a dry run.

Produces:
  1. MARGIN DISTRIBUTION   log_vs_Rx passing rate vs the action level, overall
                           and by plan / beam / machine. The minimum ever
                           observed is the number a committee will ask for.

  2. CORRELATIONS          rank correlation of margin against every
                           pre-delivery beam characteristic (layer count, beam
                           MU, smallest layer, MU per spot, spot count, energy
                           span, low-MU layer fraction) plus machine.

  3. VERIFIED ENVELOPE     the range of each characteristic across beams that
                           have been verified. A future plan outside this
                           envelope is unlike anything validated and still
                           warrants a dry run -- a process-capability screen
                           that requires no failures to construct.

Usage (from virtual-psqa\\backend):
    ..\\python\\python.exe margin_analysis.py
    ..\\python\\python.exe margin_analysis.py --csv margins.csv
    ..\\python\\python.exe margin_analysis.py --screen 21
"""

import argparse
import csv
import glob
import math
import os
import sqlite3
import sys
from collections import defaultdict

try:
    import pydicom
except ImportError:
    sys.exit("FATAL: pydicom not importable by this interpreter")

DB = r"data\psqa.db"
ACTION_LEVEL = 90.0          # % passing at 2%/2mm, TG-218
LOW_MU_LAYER = 3.0           # layers below this drive most delivery deviation

FEATURES = [
    "n_layers", "beam_mu", "min_layer_mu", "median_layer_mu",
    "mean_mu_per_spot", "n_spots", "max_spots_per_layer",
    "energy_min", "energy_max", "energy_span", "frac_low_mu_layers",
]


# ----------------------------------------------------------------------
# plan geometry
# ----------------------------------------------------------------------

def resolve_rtplan(store, want_uid):
    if not store or not os.path.isdir(store):
        return None
    cands = []
    for p in glob.glob(os.path.join(store, "**", "*.dcm"), recursive=True):
        try:
            ds = pydicom.dcmread(p, stop_before_pixels=True, force=True,
                                 specific_tags=["Modality", "SOPInstanceUID"])
        except Exception:
            continue
        if str(getattr(ds, "Modality", "")).upper() == "RTPLAN":
            cands.append((p, str(getattr(ds, "SOPInstanceUID", ""))))
    if not cands:
        return None
    if want_uid:
        exact = [p for p, u in cands if u == str(want_uid)]
        if len(exact) == 1:
            return exact[0]
        return None
    return cands[0][0] if len(cands) == 1 else None


def beam_features(rtplan_path):
    """beam_name -> pre-delivery characteristics."""
    ds = pydicom.dcmread(rtplan_path)
    bm = {}
    for fg in getattr(ds, "FractionGroupSequence", []) or []:
        for rb in getattr(fg, "ReferencedBeamSequence", []) or []:
            bn = getattr(rb, "ReferencedBeamNumber", None)
            mu = getattr(rb, "BeamMeterset", None)
            if bn is not None and mu is not None:
                bm[int(bn)] = float(mu)

    out = {}
    for beam in getattr(ds, "IonBeamSequence", []) or []:
        bnum = int(getattr(beam, "BeamNumber", 0))
        bname = str(getattr(beam, "BeamName", "") or "")
        final_cum = float(getattr(beam, "FinalCumulativeMetersetWeight", 0) or 0)
        beam_mu = bm.get(bnum)
        if not final_cum or not beam_mu:
            continue
        scale = beam_mu / final_cum

        layer_mus, spot_counts, energies, all_spot_mu = [], [], [], []
        for cp in getattr(beam, "IonControlPointSequence", []) or []:
            w = getattr(cp, "ScanSpotMetersetWeights", None)
            if w is None:
                continue
            try:
                weights = [float(v) for v in w]
            except TypeError:
                weights = [float(w)]
            nz = [v for v in weights if v > 0]
            if not nz:
                continue
            spot_mu = [v * scale for v in nz]
            layer_mus.append(sum(spot_mu))
            spot_counts.append(len(spot_mu))
            all_spot_mu.extend(spot_mu)
            e = float(getattr(cp, "NominalBeamEnergy", 0) or 0)
            if e:
                energies.append(e)

        if not layer_mus:
            continue
        n_low = sum(1 for m in layer_mus if m < LOW_MU_LAYER)
        out[bname] = {
            "n_layers": len(layer_mus),
            "beam_mu": round(beam_mu, 2),
            "min_layer_mu": round(min(layer_mus), 3),
            "median_layer_mu": round(sorted(layer_mus)[len(layer_mus) // 2], 3),
            "mean_mu_per_spot": round(sum(all_spot_mu) / len(all_spot_mu), 5),
            "n_spots": len(all_spot_mu),
            "max_spots_per_layer": max(spot_counts),
            "energy_min": round(min(energies), 1) if energies else 0.0,
            "energy_max": round(max(energies), 1) if energies else 0.0,
            "energy_span": round(max(energies) - min(energies), 1) if energies else 0.0,
            "frac_low_mu_layers": round(n_low / len(layer_mus), 4),
        }
    return out


# ----------------------------------------------------------------------
# stats
# ----------------------------------------------------------------------

def spearman(xs, ys):
    n = len(xs)
    if n < 3:
        return None

    def ranks(v):
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def q(sorted_vals, f):
    n = len(sorted_vals)
    return sorted_vals[min(n - 1, max(0, int(f * n)))]


# ----------------------------------------------------------------------

def gather():
    cx = sqlite3.connect(DB)

    plans = {r[0]: (r[1], r[2], r[3]) for r in cx.execute(
        "SELECT id, plan_label, rtplan_uid, dicom_store_path FROM plans")}

    machine = {}
    for r in cx.execute(
            "SELECT plan_id, fraction_number, machine FROM fractions "
            "WHERE machine IS NOT NULL"):
        machine[(r[0], r[1])] = r[2]

    gammas = cx.execute(
        "SELECT plan_id, fraction_number, field_name, passing_rate, threshold "
        "FROM gamma_results WHERE comparison_type = 'log_vs_Rx' "
        "ORDER BY plan_id, fraction_number"
    ).fetchall()
    cx.close()

    feat_cache = {}
    rows, skipped_zero, unresolved = [], 0, set()

    for pid, fx, beam, pr, thr in gammas:
        if pr is None or pr <= 0.0:
            skipped_zero += 1
            continue
        if pid not in feat_cache:
            label, uid, store = plans.get(pid, (None, None, None))
            path = resolve_rtplan(store, uid)
            feat_cache[pid] = beam_features(path) if path else {}
            if not path:
                unresolved.add(pid)
        f = feat_cache[pid].get(beam)
        if not f:
            continue
        rows.append({
            "plan_id": pid,
            "fraction_number": fx,
            "beam_name": beam,
            "machine": machine.get((pid, fx), ""),
            "passing_rate": float(pr),
            "margin": float(pr) - (float(thr) if thr else ACTION_LEVEL),
            **f,
        })
    return rows, skipped_zero, unresolved


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=None)
    ap.add_argument("--screen", type=int, default=None,
                    help="plan id to screen against the verified envelope")
    args = ap.parse_args()

    if not os.path.exists(DB):
        sys.exit(f"FATAL: {DB} not found - run from virtual-psqa\\backend")

    rows, skipped, unresolved = gather()
    if not rows:
        sys.exit("no log_vs_Rx gamma rows with matched plan geometry")

    print(f"{len(rows)} verified beam deliveries "
          f"({len({r['plan_id'] for r in rows})} plans)")
    if skipped:
        print(f"excluded {skipped} row(s) with 0% (aborted records)")
    if unresolved:
        print(f"RTPLAN unresolved for plan(s): {sorted(unresolved)}")

    prs = sorted(r["passing_rate"] for r in rows)
    print("\n" + "=" * 68)
    print(f"1. MARGIN TO ACTION LEVEL ({ACTION_LEVEL}% at 2%/2mm)")
    print("=" * 68)
    print(f"  minimum observed : {prs[0]:.2f} %   "
          f"(margin {prs[0]-ACTION_LEVEL:+.2f} points)")
    print(f"  p1               : {q(prs,0.01):.2f} %")
    print(f"  p5               : {q(prs,0.05):.2f} %")
    print(f"  median           : {q(prs,0.50):.2f} %")
    print(f"  maximum          : {prs[-1]:.2f} %")
    print(f"  below action     : {sum(1 for p in prs if p < ACTION_LEVEL)} "
          f"of {len(prs)}")

    print("\n  worst beam per plan")
    print(f"    {'plan':>6}{'beam':>8}{'n':>5}{'min%':>9}{'mean%':>9}")
    byplan = defaultdict(list)
    for r in rows:
        byplan[(r["plan_id"], r["beam_name"])].append(r["passing_rate"])
    worst = {}
    for (pid, beam), v in byplan.items():
        if pid not in worst or min(v) < worst[pid][2]:
            worst[pid] = (beam, len(v), min(v), sum(v) / len(v))
    for pid in sorted(worst):
        b, n, mn, mean = worst[pid]
        print(f"    {pid:>6}{b:>8}{n:>5}{mn:>9.2f}{mean:>9.2f}")

    machines = sorted({r["machine"] for r in rows if r["machine"]})
    if len(machines) > 1:
        print("\n  by machine")
        print(f"    {'machine':<26}{'n':>5}{'min%':>9}{'mean%':>9}")
        for m in machines:
            v = [r["passing_rate"] for r in rows if r["machine"] == m]
            print(f"    {m:<26}{len(v):>5}{min(v):>9.2f}"
                  f"{sum(v)/len(v):>9.2f}")

    print("\n" + "=" * 68)
    print("2. DOES ANY PRE-DELIVERY CHARACTERISTIC TRACK MARGIN?")
    print("=" * 68)
    print(f"  {'feature':<24}{'rho':>9}{'|rho|':>9}")
    ys = [r["passing_rate"] for r in rows]
    results = []
    for f in FEATURES:
        xs = [r[f] for r in rows]
        rho = spearman(xs, ys)
        if rho is not None:
            results.append((f, rho))
    for f, rho in sorted(results, key=lambda t: -abs(t[1])):
        print(f"  {f:<24}{rho:>9.3f}{abs(rho):>9.3f}")

    strongest = max((abs(r) for _, r in results), default=0.0)
    print()
    if strongest < 0.3:
        print("  >>> No pre-delivery characteristic tracks margin (all |rho| <")
        print("  >>> 0.3). Margin is a property of the delivery system, not of")
        print("  >>> the plan. A per-plan dry run measures nothing plan-specific.")
    else:
        print(f"  >>> Strongest |rho| = {strongest:.3f}. Inspect that feature --")
        print("  >>> it may identify which plans still warrant a dry run.")

    print("\n" + "=" * 68)
    print("3. VERIFIED ENVELOPE (range covered by delivered-and-checked beams)")
    print("=" * 68)
    print(f"  {'feature':<24}{'min':>12}{'p05':>12}{'p95':>12}{'max':>12}")
    env = {}
    for f in FEATURES:
        v = sorted(r[f] for r in rows)
        env[f] = (v[0], q(v, 0.05), q(v, 0.95), v[-1])
        print(f"  {f:<24}{v[0]:>12.3f}{q(v,0.05):>12.3f}"
              f"{q(v,0.95):>12.3f}{v[-1]:>12.3f}")
    print(f"\n  machines verified: {', '.join(machines) if machines else 'unknown'}")
    print("  A future beam outside these ranges is unlike anything verified")
    print("  and warrants a dry run regardless of predicted margin.")

    # ---------- screen a plan ----------
    if args.screen is not None:
        cx = sqlite3.connect(DB)
        r = cx.execute("SELECT plan_label, rtplan_uid, dicom_store_path "
                       "FROM plans WHERE id=?", (args.screen,)).fetchone()
        cx.close()
        if not r:
            sys.exit(f"no plan {args.screen}")
        path = resolve_rtplan(r[2], r[1])
        if not path:
            sys.exit(f"could not resolve RTPLAN for plan {args.screen}")
        print("\n" + "=" * 68)
        print(f"SCREEN: plan {args.screen} ({r[0]}) vs verified envelope")
        print("=" * 68)
        feats = beam_features(path)
        any_out = False
        for bname, f in feats.items():
            outs = []
            for k in FEATURES:
                lo, _, _, hi = env[k]
                if f[k] < lo or f[k] > hi:
                    outs.append(f"{k}={f[k]:g} (verified {lo:g}-{hi:g})")
            if outs:
                any_out = True
                print(f"  beam {bname}: OUTSIDE ENVELOPE")
                for o in outs:
                    print(f"      {o}")
            else:
                print(f"  beam {bname}: within envelope")
        print("\n  " + ("At least one beam is unlike anything verified -- "
                        "dry run advised."
                        if any_out else
                        "All beams within the verified envelope."))

    if args.csv:
        with open(args.csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"\nwrote {args.csv} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
