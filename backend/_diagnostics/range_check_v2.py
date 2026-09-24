# range_check_v2.py  --  POSTRANGE_V2
#
# Corrected posterior-beam range diagnostic. Fixes the flaw in v1: v1
# normalized each depth-dose curve to its OWN plateau, so a long low-level
# MCsquare entrance-channel tail (physical dose in low-density voxels that the
# TPS hard-clips at the External contour) shifted the normalization and could
# fake a distal-edge difference.
#
# v2 changes:
#   - works in ABSOLUTE Gy, no per-curve normalization
#   - reports where each beam's dose STARTS and STOPS in absolute terms
#     (first/last t crossing an absolute-Gy floor), MC vs TPS, separately
#   - distal edge measured as the falloff of the TARGET dose only: threshold
#     is a fraction of a robust high-dose level (99th percentile along the
#     ray), and edges are found by walking OUT from the peak, so an entrance
#     tail on the far (proximal) side cannot move the distal number
#   - reports entrance-channel dose explicitly: MC and TPS dose at fixed
#     upstream depths (-150, -120, -90 mm), so the tail is quantified not
#     hidden
#   - couch/HU context dropped (already resolved); geometry identical to v1
#
# Run from backend\:
#   ..\python\python.exe range_check_v2.py 17
#   ..\python\python.exe range_check_v2.py 17 --beam 1

import argparse
import math
import sqlite3
import sys
from pathlib import Path

import numpy as np

try:
    import pydicom
    from scipy.ndimage import map_coordinates
except ImportError as e:
    print("FATAL: missing dependency: %r" % e)
    sys.exit(2)

RTDOSE_UID = "1.2.840.10008.5.1.4.1.1.481.2"
RTIONPLAN_UID = "1.2.840.10008.5.1.4.1.1.481.8"
_MHD_DTYPES = {
    "MET_FLOAT": np.float32, "MET_DOUBLE": np.float64, "MET_SHORT": np.int16,
    "MET_USHORT": np.uint16, "MET_UCHAR": np.uint8, "MET_INT": np.int32,
    "MET_UINT": np.uint32,
}

T_MIN, T_MAX, T_STEP = -350.0, 350.0, 1.0
ENTRANCE_DEPTHS = [-150.0, -120.0, -90.0, -60.0]


def fail(msg):
    print("FATAL: " + msg)
    sys.exit(1)


def read_mhd(mhd_path):
    meta = {}
    for line in Path(mhd_path).read_text().splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            meta[k.strip()] = v.strip()
    nx, ny, nz = (int(v) for v in meta["DimSize"].split())
    sx, sy, sz = (float(v) for v in meta.get("ElementSpacing", "1 1 1").split())
    off = meta.get("Offset", "0 0 0").split()
    origin = (float(off[0]), float(off[1]), float(off[2]))
    dtype = _MHD_DTYPES.get(meta.get("ElementType", "MET_FLOAT"), np.float32)
    raw = Path(mhd_path).parent / meta.get("ElementDataFile",
                                           Path(mhd_path).stem + ".raw")
    arr = np.fromfile(str(raw), dtype=dtype).reshape((nz, ny, nx)).astype(
        np.float32)
    return arr, origin, (sx, sy, sz)


def read_dose_volume(dose_path):
    p = Path(dose_path)
    if p.suffix.lower() == ".npz":
        data = np.load(str(p))
        arr = data["array"].astype(np.float32)
        sp = data["spacing"]
        orig = data["origin"]
        spacing = (float(sp[2]), float(sp[1]), float(sp[0]))
        origin = (float(orig[2]), float(orig[1]), float(orig[0]))
        return arr, origin, spacing
    return read_mhd(dose_path)


def find_store(plan_id):
    candidates = [
        Path("./backend/data/psqa.db"),
        Path("./data/psqa.db"),
        Path("../data/psqa.db"),
        Path("../../backend/data/psqa.db"),
        Path("/home/aholt/Projects/virtual-psqa-open/backend/data/psqa.db"),
    ]
    for db in candidates:
        if db.exists():
            try:
                con = sqlite3.connect(str(db))
                cur = con.cursor()
                for t in ("plans", "plan"):
                    try:
                        cur.execute(
                            "SELECT dicom_store_path FROM %s WHERE id=?" % t,
                            (plan_id,))
                        row = cur.fetchone()
                        if row and row[0]:
                            raw_path = row[0]
                            store_candidates = [
                                Path(raw_path),
                                Path("backend") / raw_path,
                                Path("..") / raw_path,
                                db.parent / raw_path,
                                db.parent.parent / raw_path,
                            ]
                            for sc in store_candidates:
                                if sc.is_dir() and any(sc.glob("*.dcm")):
                                    return str(sc)
                    except sqlite3.OperationalError:
                        continue
            except Exception:
                pass
    return None


def load_rtplan_beams(store):
    for f in sorted(Path(store).glob("*.dcm")):
        try:
            d = pydicom.dcmread(str(f), stop_before_pixels=True)
        except Exception:
            continue
        if getattr(d, "SOPClassUID", "") != RTIONPLAN_UID and \
           str(getattr(d, "Modality", "")).upper() != "RTPLAN":
            continue
        seq = getattr(d, "IonBeamSequence", None) or getattr(
            d, "BeamSequence", None)
        if not seq:
            continue
        out = {}
        for b in seq:
            bn = int(b.BeamNumber)
            name = str(b.get("BeamName", "") or "")
            cps = getattr(b, "IonControlPointSequence", None) or getattr(
                b, "ControlPointSequence", [])
            gantry = iso = None
            for cp in cps:
                if gantry is None and hasattr(cp, "GantryAngle"):
                    try:
                        gantry = float(cp.GantryAngle)
                    except (TypeError, ValueError):
                        pass
                if iso is None and hasattr(cp, "IsocenterPosition"):
                    try:
                        iso = [float(v) for v in cp.IsocenterPosition]
                    except (TypeError, ValueError):
                        pass
                if gantry is not None and iso is not None:
                    break
            out[bn] = dict(name=name, gantry=gantry, iso=iso)
        if out:
            return out
    return {}


def load_tps_beams(store):
    out = []
    for f in sorted(Path(store).glob("*.dcm")):
        try:
            d = pydicom.dcmread(str(f))
        except Exception:
            continue
        if getattr(d, "SOPClassUID", "") != RTDOSE_UID:
            continue
        if str(getattr(d, "DoseSummationType", "")).upper() != "BEAM":
            continue
        try:
            beam_no = int(d.ReferencedRTPlanSequence[0]
                          .ReferencedFractionGroupSequence[0]
                          .ReferencedBeamSequence[0].ReferencedBeamNumber)
        except Exception:
            beam_no = None
        arr = d.pixel_array.astype(np.float32) * float(d.DoseGridScaling)
        ipp = [float(v) for v in d.ImagePositionPatient]
        prow = float(d.PixelSpacing[0])
        pcol = float(d.PixelSpacing[1])
        gfov = np.array([float(v) for v in d.GridFrameOffsetVector])
        out.append(dict(
            beam=beam_no, file=f.name, array=arr,
            xs=ipp[0] + np.arange(arr.shape[2]) * pcol,
            ys=ipp[1] + np.arange(arr.shape[1]) * prow,
            zs=ipp[2] + gfov))
    return out


def sample_on_tps(mc_arr, mc_origin, mc_spacing, tps):
    ox, oy, oz = mc_origin
    sx, sy, sz = mc_spacing
    zi = (tps["zs"] - oz) / sz
    yi = (tps["ys"] - oy) / sy
    xi = (tps["xs"] - ox) / sx
    Z, Y, X = np.meshgrid(zi, yi, xi, indexing="ij")
    return map_coordinates(mc_arr, [Z, Y, X], order=1, mode="constant",
                           cval=0.0)


def sample_ray(vol, tps, points):
    xs, ys, zs = tps["xs"], tps["ys"], tps["zs"]
    dx = xs[1] - xs[0] if len(xs) > 1 else 1.0
    dy = ys[1] - ys[0] if len(ys) > 1 else 1.0
    dz = zs[1] - zs[0] if len(zs) > 1 else 1.0
    xi = (points[:, 0] - xs[0]) / dx
    yi = (points[:, 1] - ys[0]) / dy
    zi = (points[:, 2] - zs[0]) / dz
    return map_coordinates(vol, [zi, yi, xi], order=1, mode="constant",
                           cval=0.0)


def beam_dir(gantry_deg):
    g = math.radians(gantry_deg)
    return np.array([-math.sin(g), math.cos(g), 0.0])


def distal_edge_from_peak(ts, prof, level, frac):
    """Walk OUT from the peak toward +t (distal). Return the t where prof
    falls below frac*level on the distal side (linear interp)."""
    if level <= 0:
        return None
    pk = int(np.argmax(prof))
    thr = frac * level
    i = pk
    while i < len(ts) - 1 and prof[i] >= thr:
        i += 1
    if i == pk:
        return ts[pk]
    t0, t1 = ts[i - 1], ts[i]
    y0, y1 = prof[i - 1], prof[i]
    if y0 == y1:
        return t1
    return t0 + (thr - y0) * (t1 - t0) / (y1 - y0)


def dose_span(ts, prof, floor_gy):
    """First and last t where absolute dose >= floor_gy."""
    above = prof >= floor_gy
    if not above.any():
        return None, None
    idx = np.where(above)[0]
    return ts[idx[0]], ts[idx[-1]]


def at_depth(ts, prof, t):
    return float(np.interp(t, ts, prof))


def main():
    ap = argparse.ArgumentParser(description="Range check diagnostic comparing MCsquare and TPS doses along beam axis.")
    ap.add_argument("plan_id", type=int, nargs="?", default=None,
                    help="Plan ID from database (optional if --store and --plan-dir are passed)")
    ap.add_argument("--beam", type=int, default=None,
                    help="Restrict to a single beam number")
    ap.add_argument("--store", default=None,
                    help="Path directly to DICOM store containing RTPLAN and RTDOSE files")
    ap.add_argument("--plan-dir", default=None,
                    help="Path directly to plan result folder (e.g. data/results/plan_1)")
    ap.add_argument("--results", default=None,
                    help="Base results directory (e.g. data/results or backend/data/results)")
    ap.add_argument("--floor-frac", type=float, default=0.05,
                    help="Absolute dose floor as fraction of TPS beam max (default: 0.05)")
    args = ap.parse_args()

    # Resolve store
    store = args.store
    if store is None and args.plan_id is not None:
        store = find_store(args.plan_id)
    if store is None and args.plan_dir:
        # Check if DICOM store or files are inside or adjacent to plan_dir
        pd = Path(args.plan_dir)
        if any(pd.glob("*.dcm")):
            store = str(pd)
    if store is None:
        fail("Could not resolve DICOM store. Please pass --store /path/to/dicom_folder")

    # Resolve plan_dir
    plan_dir = None
    if args.plan_dir:
        plan_dir = Path(args.plan_dir)
    elif args.plan_id is not None:
        res_dirs = [args.results] if args.results else [
            "./data/results",
            "./backend/data/results",
            "../data/results",
            "../backend/data/results",
            "./data/RESULTS",
            "./backend/data/RESULTS",
            "/home/aholt/Projects/virtual-psqa-open/backend/data/results",
        ]
        for rd in res_dirs:
            if rd and Path(rd).is_dir():
                cand = Path(rd) / f"plan_{args.plan_id}"
                if cand.is_dir():
                    if any(cand.rglob("Dose_Beam*.mhd")) or any(cand.rglob("mc_dose_beam*.npz")):
                        plan_dir = cand
                        break
                    elif plan_dir is None:
                        plan_dir = cand
    if plan_dir is None or not plan_dir.is_dir():
        fail("Plan results folder not found. Please pass --plan-dir /path/to/results/plan_X")

    plan_beams = load_rtplan_beams(store)
    mc_files = sorted(plan_dir.rglob("Dose_Beam*.mhd"))
    if not mc_files:
        mc_files = sorted(plan_dir.rglob("mc_dose_beam*.npz"))
    if not mc_files:
        fail(f"No Dose_Beam*.mhd or mc_dose_beam*.npz found under {plan_dir}")
    tps_beams = load_tps_beams(store)
    if not tps_beams:
        fail(f"No BEAM RTDose found in {store}")

    ts = np.arange(T_MIN, T_MAX + T_STEP, T_STEP)

    for mcf in mc_files:
        bn = int("".join(ch for ch in mcf.stem if ch.isdigit()))
        if args.beam is not None and bn != args.beam:
            continue
        tps = next((t for t in tps_beams if t["beam"] == bn), None)
        pb = plan_beams.get(bn)
        if tps is None or pb is None or pb["gantry"] is None:
            print("beam %d: missing TPS dose or geometry - skipped" % bn)
            continue

        iso = np.array(pb["iso"])
        d = beam_dir(pb["gantry"])
        mc_raw, mc_o, mc_sp = read_dose_volume(mcf)
        mc = np.ascontiguousarray(np.flip(np.flip(mc_raw, 2), 1))
        mc_s = sample_on_tps(mc, mc_o, mc_sp, tps)
        tarr = tps["array"]

        pts = iso[None, :] + ts[:, None] * d[None, :]
        tp = sample_ray(tarr, tps, pts)     # absolute Gy
        mp = sample_ray(mc_s, tps, pts)     # MC raw*flip (unscaled)

        tps_max = float(tarr.max())
        mc_max = float(mc_s.max())
        # robust high-dose level along THIS ray (99th pct of the >20%-of-peak
        # samples), used only to define the distal falloff fraction
        def hi_level(p):
            pk = p.max()
            if pk <= 0:
                return 0.0
            core = p[p >= 0.2 * pk]
            return float(np.percentile(core, 99)) if core.size else pk
        tp_hi = hi_level(tp)
        mp_hi = hi_level(mp)

        print("\n================ BEAM %d (%s) ================"
              % (bn, pb["name"] or "?"))
        print("gantry %.1f  iso (%.1f, %.1f, %.1f)  dir (%.3f, %.3f, %.3f)"
              % (pb["gantry"], iso[0], iso[1], iso[2], d[0], d[1], d[2]))
        print("TPS beam max %.4f Gy   MC beam max (raw*flip) %.4g" %
              (tps_max, mc_max))
        print("ray high-dose level:  TPS %.4f Gy   MC %.4g (raw)"
              % (tp_hi, mp_hi))

        print("\n-- DISTAL falloff (walked outward from each curve's own "
              "peak; entrance tail cannot affect this) --")
        for frac in (0.8, 0.5, 0.2):
            te = distal_edge_from_peak(ts, tp, tp_hi, frac)
            me = distal_edge_from_peak(ts, mp, mp_hi, frac)
            if te is None or me is None:
                print("  %d%%: unresolved" % int(frac * 100))
                continue
            print("  %2d%% distal:  TPS %+8.1f mm   MC %+8.1f mm   "
                  "(MC-TPS = %+6.2f mm)"
                  % (int(frac * 100), te, me, me - te))

        print("\n-- ABSOLUTE dose span (first..last t above floor) --")
        tp_floor = args.floor_frac * tps_max
        mc_floor = args.floor_frac * mc_max
        ts0, ts1 = dose_span(ts, tp, tp_floor)
        ms0, ms1 = dose_span(ts, mp, mc_floor)
        print("  floor: TPS %.4f Gy (%.0f%% of max) | MC %.4g raw"
              % (tp_floor, args.floor_frac * 100, mc_floor))
        if ts0 is not None:
            print("  TPS dose from t=%+.1f to t=%+.1f mm  (span %.1f mm)"
                  % (ts0, ts1, ts1 - ts0))
        if ms0 is not None:
            print("  MC  dose from t=%+.1f to t=%+.1f mm  (span %.1f mm)"
                  % (ms0, ms1, ms1 - ms0))
            if ts0 is not None:
                print("  entrance-side start diff (MC-TPS) = %+.1f mm  "
                      "(negative = MC starts further upstream)"
                      % (ms0 - ts0))

        print("\n-- ENTRANCE-CHANNEL dose (fraction of each beam's own "
              "high-dose level) --")
        for t in ENTRANCE_DEPTHS:
            tv = at_depth(ts, tp, t)
            mv = at_depth(ts, mp, t)
            tr = tv / tp_hi if tp_hi > 0 else 0
            mr = mv / mp_hi if mp_hi > 0 else 0
            print("  t=%+6.0f mm:  TPS %5.1f%% of level   MC %5.1f%% of level"
                  % (t, tr * 100, mr * 100))
        print("  (large MC%% with near-zero TPS%% here = MC entrance-channel "
              "dose the TPS clips at External -- expected, and this is what "
              "distorted v1's normalization)")


if __name__ == "__main__":
    main()
