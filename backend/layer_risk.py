"""
layer_risk.py  --  Pre-delivery delivery-accuracy risk from the RT Ion Plan.

No ML, no labels, no training. The machine quantizes meterset at ~0.002 MU, so
a control point with a small total meterset carries a proportionally larger
delivery error. That relationship was measured directly from 1295 control
points (spearman(mean MU/spot, |dev|) = -0.55), and it is a property of the
machine rather than of any particular plan.

Computes, from the RTPlan alone and before any beam is delivered:
  - per-layer meterset, spot count, mean and minimum spot MU
  - expected |deviation| and a 95th-percentile bound, from the empirical curve
  - flags for layers likely to deliver outside 2%
  - a plan-level summary suitable for a planning review card

v2: RTPLAN resolution is by SOPInstanceUID matched against plans.rtplan_uid.
A store directory may contain a foreign patient's plan (this has occurred), so
taking the first glob hit is unsafe. If the UID cannot be matched the plan is
reported UNRESOLVED and skipped -- never scored against a guessed file.

The calibration table is empirical and should be regenerated as records
accumulate:
    ..\\python\\python.exe layer_risk.py --calibrate layers.csv

Usage (from virtual-psqa\\backend):
    ..\\python\\python.exe layer_risk.py --audit
    ..\\python\\python.exe layer_risk.py --all
    ..\\python\\python.exe layer_risk.py --plan 16
    ..\\python\\python.exe layer_risk.py --file path\\to\\RTPLAN.dcm
    ..\\python\\python.exe layer_risk.py --plan 16 --json
"""

import argparse
import csv
import glob
import json
import os
import sqlite3
import sys

try:
    import pydicom
except ImportError:
    sys.exit("FATAL: pydicom not importable by this interpreter")

DB = r"data\psqa.db"

# Machine quantum: smallest nonzero delivered spot meterset observed.
MIN_SPOT_MU = 0.002

# Empirical deviation curve: (layer_mu_lower_bound, mean_abs_pct, p95_abs_pct)
# Derived from 1295 control points, plans 5/15/16/17, ProNova SC360.
# NOTE: leave-one-plan-out showed these bins are not yet stable. Provisional;
# regenerate with --calibrate once a dozen plans have records, and consider
# collapsing to three bins.
CALIBRATION = [
    (29.268, 0.69, 1.45),
    (17.054, 0.64, 1.33),
    (8.493,  0.87, 1.63),
    (3.088,  1.05, 2.48),
    (0.0,    3.10, 6.31),
]

# A layer whose p95 bound exceeds this is worth a planner's attention.
FLAG_P95_PCT = 2.0


def expected_dev(layer_mu, table=None):
    """(mean, p95) expected |deviation| % for a layer of this meterset."""
    tbl = table or CALIBRATION
    for lo, mean, p95 in tbl:
        if layer_mu >= lo:
            return mean, p95
    return tbl[-1][1], tbl[-1][2]


def calibrate(csv_path):
    """Regenerate the deviation curve from a layers.csv produced by
    layer_deviation.py. Prints a CALIBRATION block to paste back in."""
    rows = []
    with open(csv_path, newline="") as f:
        for r in csv.DictReader(f):
            try:
                rows.append((float(r["spec_mu"]), float(r["abs_pct_dev"])))
            except (KeyError, ValueError):
                continue
    if len(rows) < 50:
        sys.exit(f"only {len(rows)} usable rows - need substantially more")

    rows.sort(key=lambda t: t[0])
    n = len(rows)
    cuts = [0, n // 10, n // 4, n // 2, 3 * n // 4, n]
    out = []
    for a, b in zip(cuts[:-1], cuts[1:]):
        seg = rows[a:b]
        if not seg:
            continue
        devs = sorted(d for _, d in seg)
        lo_mu = seg[0][0]
        mean = sum(devs) / len(devs)
        p95 = devs[min(len(devs) - 1, int(0.95 * len(devs)))]
        out.append((round(lo_mu, 3), round(mean, 2), round(p95, 2), len(seg)))

    print(f"# regenerated from {csv_path}, n={n} control points")
    print("CALIBRATION = [")
    for lo, mean, p95, cnt in sorted(out, reverse=True):
        print(f"    ({lo:<8}, {mean:<5}, {p95:<5}),   # n={cnt}")
    print("]")


# ----------------------------------------------------------------------
# RTPlan resolution -- by UID, never by glob order
# ----------------------------------------------------------------------

def rtplans_under(store):
    """[(path, sop_instance_uid, patient_id)] for every RTPLAN under store."""
    found = []
    if not store or not os.path.isdir(store):
        return found
    for p in glob.glob(os.path.join(store, "**", "*.dcm"), recursive=True):
        try:
            ds = pydicom.dcmread(
                p, stop_before_pixels=True,
                specific_tags=["Modality", "SOPInstanceUID", "PatientID"])
        except Exception:
            continue
        if str(getattr(ds, "Modality", "")) == "RTPLAN":
            found.append((p,
                          str(getattr(ds, "SOPInstanceUID", "")),
                          str(getattr(ds, "PatientID", ""))))
    return found


def resolve_rtplan(store, want_uid):
    """Return (path, note). path is None when resolution is not certain."""
    cands = rtplans_under(store)
    if not cands:
        return None, "no RTPLAN under store path"

    if want_uid:
        exact = [c for c in cands if c[1] == str(want_uid)]
        if len(exact) == 1:
            note = "uid match"
            foreign = [c for c in cands if c[1] != str(want_uid)]
            if foreign:
                note += f"; WARNING {len(foreign)} foreign RTPLAN(s) in store: "
                note += ", ".join(f"{os.path.basename(p)} (patient {pt})"
                                  for p, _, pt in foreign)
            return exact[0][0], note
        if len(exact) > 1:
            return None, f"{len(exact)} files share uid {want_uid}"

    if len(cands) == 1:
        p, uid, pt = cands[0]
        if want_uid:
            return None, (f"sole RTPLAN uid {uid} does not match db uid "
                          f"{want_uid} (file patient {pt})")
        return p, "sole candidate, no db uid to check"

    return None, (f"{len(cands)} RTPLANs under store, none matching db uid "
                  f"{want_uid}")


# ----------------------------------------------------------------------
# Layer extraction
# ----------------------------------------------------------------------

def beam_metersets(ds):
    """ReferencedBeamNumber -> BeamMeterset (MU per fraction)."""
    out = {}
    for fg in getattr(ds, "FractionGroupSequence", []) or []:
        for rb in getattr(fg, "ReferencedBeamSequence", []) or []:
            bn = getattr(rb, "ReferencedBeamNumber", None)
            mu = getattr(rb, "BeamMeterset", None)
            if bn is not None and mu is not None:
                out[int(bn)] = float(mu)
    return out


def layers_from_plan(ds):
    """Yield one dict per energy layer (control point carrying spot weights)."""
    bm = beam_metersets(ds)
    for beam in getattr(ds, "IonBeamSequence", []) or []:
        bnum = int(getattr(beam, "BeamNumber", 0))
        bname = str(getattr(beam, "BeamName", "") or "")
        final_cum = float(getattr(beam, "FinalCumulativeMetersetWeight", 0) or 0)
        beam_mu = bm.get(bnum)
        if not final_cum or not beam_mu:
            continue
        scale = beam_mu / final_cum

        for ci, cp in enumerate(getattr(beam, "IonControlPointSequence", []) or []):
            w = getattr(cp, "ScanSpotMetersetWeights", None)
            if w is None:
                continue
            try:
                weights = [float(v) for v in w]
            except TypeError:
                weights = [float(w)]
            nz = [v for v in weights if v > 0]
            if not nz:
                continue  # paired zero-weight control point

            spot_mu = [v * scale for v in nz]
            layer_mu = sum(spot_mu)
            mean_dev, p95_dev = expected_dev(layer_mu)

            yield {
                "beam_number": bnum,
                "beam_name": bname,
                "cp_index": ci,
                "energy_mev": float(getattr(cp, "NominalBeamEnergy", 0) or 0),
                "n_spots": len(spot_mu),
                "layer_mu": round(layer_mu, 4),
                "mean_mu_per_spot": round(layer_mu / len(spot_mu), 5),
                "min_spot_mu": round(min(spot_mu), 5),
                "expected_dev_pct": mean_dev,
                "p95_dev_pct": p95_dev,
                "flagged": p95_dev > FLAG_P95_PCT,
                "below_quantum": sum(1 for v in spot_mu if v < MIN_SPOT_MU),
            }


def summarise(layers):
    if not layers:
        return {}
    flagged = [l for l in layers if l["flagged"]]
    total_mu = sum(l["layer_mu"] for l in layers)
    wdev = (sum(l["expected_dev_pct"] * l["layer_mu"] for l in layers) / total_mu
            if total_mu else 0.0)
    return {
        "n_layers": len(layers),
        "n_flagged": len(flagged),
        "pct_layers_flagged": round(100.0 * len(flagged) / len(layers), 1),
        "total_mu": round(total_mu, 2),
        "min_layer_mu": round(min(l["layer_mu"] for l in layers), 3),
        "worst_p95_dev_pct": max(l["p95_dev_pct"] for l in layers),
        "mu_weighted_expected_dev_pct": round(wdev, 3),
        "spots_below_quantum": sum(l["below_quantum"] for l in layers),
    }


# ----------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------

def report(label, layers, as_json, note=None):
    s = summarise(layers)
    if as_json:
        print(json.dumps({"plan": label, "note": note, "summary": s,
                          "layers": layers}, indent=2))
        return

    print("=" * 74)
    print(label)
    if note:
        print(f"  [{note}]")
    print("=" * 74)
    if not layers:
        print("  no layers extracted")
        return

    print(f"  {'beam':<14}{'cp':>5}{'MeV':>8}{'spots':>7}{'MU':>10}"
          f"{'MU/spot':>10}{'exp%':>7}{'p95%':>7}  flag")
    for l in layers:
        flag = "  <<<" if l["flagged"] else ""
        print(f"  {l['beam_name'][:13]:<14}{l['cp_index']:>5}"
              f"{l['energy_mev']:>8.1f}{l['n_spots']:>7}{l['layer_mu']:>10.3f}"
              f"{l['mean_mu_per_spot']:>10.4f}{l['expected_dev_pct']:>7.2f}"
              f"{l['p95_dev_pct']:>7.2f}{flag}")

    print("-" * 74)
    print(f"  layers                    : {s['n_layers']}")
    print(f"  flagged (p95 > {FLAG_P95_PCT}%)      : {s['n_flagged']} "
          f"({s['pct_layers_flagged']}%)")
    print(f"  smallest layer            : {s['min_layer_mu']} MU")
    print(f"  worst-case layer dev (p95): {s['worst_p95_dev_pct']:.2f} %")
    print(f"  MU-weighted expected dev  : {s['mu_weighted_expected_dev_pct']:.3f} %")
    if s["spots_below_quantum"]:
        print(f"  SPOTS BELOW {MIN_SPOT_MU} MU     : {s['spots_below_quantum']}"
              "  (may not be deliverable)")

    if s["n_flagged"]:
        print(f"\n  Review: {s['n_flagged']} layer(s) below ~8 MU are expected to")
        print("  deliver outside 2%. Individually within tolerance; deviations")
        print("  are random in sign and largely cancel on summation. This is a")
        print("  planning-optimisation signal, not a QA verdict.")
    else:
        print("\n  No layers in the high-deviation regime.")


def audit(plans):
    """Resolution status for every plan, with no scoring."""
    print(f"{'plan':>5}  {'status':<12}  note")
    print("-" * 74)
    bad = 0
    for pid, patient, label, name, uid, store in plans:
        path, note = resolve_rtplan(store, uid)
        status = "ok" if path else "UNRESOLVED"
        if (not path) or ("WARNING" in note):
            bad += 1
        print(f"{pid:>5}  {status:<12}  {note}")
    print("-" * 74)
    print(f"{bad} plan(s) need attention")


def db_plans(where=""):
    if not os.path.exists(DB):
        sys.exit(f"FATAL: {DB} not found - run from virtual-psqa\\backend")
    cx = sqlite3.connect(DB)
    rows = cx.execute(
        "SELECT id, patient_id, plan_label, plan_name, rtplan_uid, "
        f"dicom_store_path FROM plans {where} ORDER BY id"
    ).fetchall()
    cx.close()
    return rows


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--plan", type=int, help="plan id from the database")
    g.add_argument("--file", help="path to an RT Ion Plan DICOM file")
    g.add_argument("--all", action="store_true", help="every plan in the database")
    g.add_argument("--audit", action="store_true",
                   help="resolution status only, no scoring")
    g.add_argument("--calibrate", help="regenerate CALIBRATION from layers.csv")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.calibrate:
        calibrate(args.calibrate)
        return

    if args.file:
        ds = pydicom.dcmread(args.file)
        report(args.file, list(layers_from_plan(ds)), args.json)
        return

    if args.audit:
        audit(db_plans())
        return

    plans = db_plans("" if args.all else f"WHERE id = {int(args.plan)}")
    if not plans:
        sys.exit("no matching plans")

    unresolved = []
    for pid, patient, label, name, uid, store in plans:
        title = f"plan {pid}: {label or name or '<unnamed>'}"
        path, note = resolve_rtplan(store, uid)
        if not path:
            unresolved.append((pid, note))
            print("=" * 74)
            print(title)
            print(f"  UNRESOLVED - not scored: {note}")
            print()
            continue
        try:
            ds = pydicom.dcmread(path)
        except Exception as e:
            print(f"{title}: unreadable RTPLAN: {e}")
            continue
        report(title, list(layers_from_plan(ds)), args.json, note=note)
        print()

    if unresolved:
        print("=" * 74)
        print(f"{len(unresolved)} plan(s) could not be resolved to an RTPLAN:")
        for pid, note in unresolved:
            print(f"  plan {pid}: {note}")


if __name__ == "__main__":
    main()
