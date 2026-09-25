# posterior_range_check.py
#
# Range diagnostic for posterior/oblique GANTRY beams (couch-in-path check).
# For each beam of a plan, using existing MCsquare per-beam doses + TPS
# per-beam RTDoses (no MCsquare run needed):
#   - reads gantry angle + isocenter from the RTPLAN
#   - samples MC and TPS dose along the beam CENTRAL AXIS through isocenter
#   - prints distal 80% / 50% edge positions along depth and the MC-TPS
#     shift in mm (positive = MC edge deeper than TPS)
#   - prints the dose-weighted COM projected onto the beam axis, MC vs TPS
#     (COM is the reliable alignment metric; argmax is noisy)
#   - prints the CT HU profile along the UPSTREAM ray (entrance side), which
#     for posterior beams passes through the couch: shows exactly what the
#     ct_density_override (COUCH_WALL_DILATION_VOX) left in the beam path
#
# Run from backend\:
#   ..\python\python.exe posterior_range_check.py <plan_id>
#   ..\python\python.exe posterior_range_check.py <plan_id> --beam 1
#
# POSTRANGE_V1 marker for Select-String.

import argparse
import math
import os
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

T_MIN, T_MAX, T_STEP = -350.0, 350.0, 1.0   # mm along axis, 0 = isocenter
HU_T_MIN, HU_STEP = -350.0, 2.0             # upstream HU scan


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


IGNORE_EXTS = {
    ".raw", ".mhd", ".txt", ".json", ".npz", ".py", ".png", ".jpg",
    ".jpeg", ".csv", ".log", ".sh", ".bat", ".exe", ".dll", ".so", ".pyc"
}


def scan_store_dicoms(store_path: str):
    p = Path(store_path)
    files_to_check: List[Path] = []

    if p.is_file():
        files_to_check.append(p)
        if p.parent.is_dir():
            for sibling in p.parent.iterdir():
                if sibling.is_file() and sibling != p and sibling.suffix.lower() not in IGNORE_EXTS:
                    files_to_check.append(sibling)
    elif p.is_dir():
        for f in p.rglob("*"):
            if f.is_file() and not f.name.startswith("."):
                if f.suffix.lower() not in IGNORE_EXTS:
                    files_to_check.append(f)
    else:
        return {"plans": [], "doses": [], "structs": [], "cts": [], "summary": {}}

    seen = set()
    unique_files = []
    for f in files_to_check:
        try:
            rf = f.resolve()
            if rf not in seen:
                seen.add(rf)
                unique_files.append(f)
        except Exception:
            unique_files.append(f)

    categorized = {
        "plans": [],
        "doses": [],
        "structs": [],
        "cts": [],
        "summary": {},
    }

    for f in unique_files:
        try:
            d = pydicom.dcmread(str(f), stop_before_pixels=True, force=True)
            sop = str(getattr(d, "SOPClassUID", ""))
            mod = str(getattr(d, "Modality", "")).upper()

            cat_name = mod if mod else (sop if sop else "Unknown")
            categorized["summary"][cat_name] = categorized["summary"].get(cat_name, 0) + 1

            if (
                sop in (RTIONPLAN_UID, "1.2.840.10008.5.1.4.1.1.481.5", "1.2.840.10008.5.1.4.1.1.481.9")
                or "PLAN" in mod
                or hasattr(d, "IonBeamSequence")
                or hasattr(d, "BeamSequence")
            ):
                categorized["plans"].append((f, d))
            elif sop == RTDOSE_UID or mod == "RTDOSE":
                categorized["doses"].append((f, d))
            elif sop == "1.2.840.10008.5.1.4.1.1.481.3" or mod == "RTSTRUCT":
                categorized["structs"].append((f, d))
            elif sop == "1.2.840.10008.5.1.4.1.1.2" or mod == "CT":
                categorized["cts"].append((f, d))
        except Exception:
            continue

    if not categorized["plans"] and p.is_dir() and p.parent.is_dir() and p.parent != p:
        parent_plans = []
        for f in p.parent.iterdir():
            if f.is_file() and f.suffix.lower() not in IGNORE_EXTS:
                try:
                    d = pydicom.dcmread(str(f), stop_before_pixels=True, force=True)
                    sop = str(getattr(d, "SOPClassUID", ""))
                    mod = str(getattr(d, "Modality", "")).upper()
                    if (
                        sop in (RTIONPLAN_UID, "1.2.840.10008.5.1.4.1.1.481.5")
                        or "PLAN" in mod
                        or hasattr(d, "IonBeamSequence")
                        or hasattr(d, "BeamSequence")
                    ):
                        parent_plans.append((f, d))
                except Exception:
                    continue
        if parent_plans:
            print(f"NOTE: Found RTPLAN in parent directory '{p.parent}'. Including it in search.")
            categorized["plans"].extend(parent_plans)

    return categorized


def load_rtplan_beams(categorized: dict) -> Tuple[Dict[int, dict], Optional[Path]]:
    best_beams = {}
    best_plan_file = None
    best_treatment_count = -1

    for f, d in categorized["plans"]:
        seq = getattr(d, "IonBeamSequence", None) or getattr(d, "BeamSequence", None)
        if not seq:
            continue

        plan_beams = {}
        treatment_beams = 0
        for b in seq:
            try:
                bn = int(getattr(b, "BeamNumber", len(plan_beams) + 1))
            except (TypeError, ValueError):
                bn = len(plan_beams) + 1

            name = str(b.get("BeamName", "") or b.get("IonBeamName", "") or f"Beam_{bn}")
            cps = getattr(b, "IonControlPointSequence", None) or getattr(b, "ControlPointSequence", [])

            gantry = None
            iso = None
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

            if gantry is None and hasattr(b, "GantryAngle"):
                try:
                    gantry = float(b.GantryAngle)
                except (TypeError, ValueError):
                    pass
            if iso is None and hasattr(b, "IsocenterPosition"):
                try:
                    iso = [float(v) for v in b.IsocenterPosition]
                except (TypeError, ValueError):
                    pass

            deliv_type = str(getattr(b, "TreatmentDeliveryType", "TREATMENT")).upper()
            if deliv_type != "SETUP":
                treatment_beams += 1

            plan_beams[bn] = dict(name=name, gantry=gantry, iso=iso)

        if treatment_beams > best_treatment_count or (not best_beams and plan_beams):
            best_beams = plan_beams
            best_plan_file = f
            best_treatment_count = treatment_beams

    return best_beams, best_plan_file


def load_tps_beams(categorized: dict) -> List[dict]:
    import re
    out = []
    for f, _ in categorized["doses"]:
        try:
            full_d = pydicom.dcmread(str(f), force=True)
            if str(getattr(full_d, "DoseSummationType", "")).upper() != "BEAM":
                continue
            beam_no = None
            try:
                beam_no = int(
                    full_d.ReferencedRTPlanSequence[0]
                    .ReferencedFractionGroupSequence[0]
                    .ReferencedBeamSequence[0]
                    .ReferencedBeamNumber
                )
            except Exception:
                pass
            if beam_no is None and hasattr(full_d, "ReferencedBeamNumber"):
                try:
                    beam_no = int(full_d.ReferencedBeamNumber)
                except Exception:
                    pass
            if beam_no is None:
                match = re.search(r'beam[_\s\-]*(\d+)', f.name, re.IGNORECASE) or re.search(
                    r'beam[_\s\-]*(\d+)', getattr(full_d, "SeriesDescription", ""), re.IGNORECASE
                )
                if match:
                    beam_no = int(match.group(1))

            scaling = float(getattr(full_d, "DoseGridScaling", 1.0))
            arr = full_d.pixel_array.astype(np.float32) * scaling
            ipp = [float(v) for v in full_d.ImagePositionPatient]
            prow = float(full_d.PixelSpacing[0])
            pcol = float(full_d.PixelSpacing[1])
            if hasattr(full_d, "GridFrameOffsetVector"):
                gfov = np.array([float(v) for v in full_d.GridFrameOffsetVector])
            else:
                gfov = np.arange(arr.shape[0]) * float(getattr(full_d, "SliceThickness", 2.0))

            out.append(dict(
                beam=beam_no,
                file=f.name,
                array=arr,
                xs=ipp[0] + np.arange(arr.shape[2]) * pcol,
                ys=ipp[1] + np.arange(arr.shape[1]) * prow,
                zs=ipp[2] + gfov,
            ))
        except Exception:
            continue
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


def sample_ray_tpsgrid(vol, tps, points):
    """Sample vol (on TPS grid) at world points (N,3)."""
    xs, ys, zs = tps["xs"], tps["ys"], tps["zs"]
    dx = xs[1] - xs[0] if len(xs) > 1 else 1.0
    dy = ys[1] - ys[0] if len(ys) > 1 else 1.0
    dz = zs[1] - zs[0] if len(zs) > 1 else 1.0
    xi = (points[:, 0] - xs[0]) / dx
    yi = (points[:, 1] - ys[0]) / dy
    zi = (points[:, 2] - zs[0]) / dz
    return map_coordinates(vol, [zi, yi, xi], order=1, mode="constant",
                           cval=0.0)


def sample_ray_mhd(arr, origin, spacing, points):
    ox, oy, oz = origin
    sx, sy, sz = spacing
    xi = (points[:, 0] - ox) / sx
    yi = (points[:, 1] - oy) / sy
    zi = (points[:, 2] - oz) / sz
    return map_coordinates(arr, [zi, yi, xi], order=1, mode="constant",
                           cval=-1000.0)


def beam_dir(gantry_deg):
    """Direction of beam travel in DICOM patient coords (HFS, couch 0):
    gantry 0 = anterior source, travel +Y; gantry 90 = left source,
    travel -X; gantry 180 = posterior source, travel -Y."""
    g = math.radians(gantry_deg)
    return np.array([-math.sin(g), math.cos(g), 0.0])


def edge_positions(ts, prof, frac):
    m = prof.max()
    if m <= 0:
        return None, None
    thr = frac * m
    above = prof >= thr
    if not above.any():
        return None, None
    idx = np.where(above)[0]
    li, ri = idx[0], idx[-1]
    if li > 0:
        t0, t1 = ts[li - 1], ts[li]
        y0, y1 = prof[li - 1], prof[li]
        left = t0 + (thr - y0) * (t1 - t0) / max(y1 - y0, 1e-12)
    else:
        left = ts[0]
    if ri < len(ts) - 1:
        t0, t1 = ts[ri], ts[ri + 1]
        y0, y1 = prof[ri], prof[ri + 1]
        right = t0 + (thr - y0) * (t1 - t0) / min(y1 - y0, -1e-12)
    else:
        right = ts[-1]
    return left, right


def norm_plateau(p):
    m = p.max()
    if m <= 0:
        return p
    idx = np.where(p >= 0.5 * m)[0]
    if len(idx) < 4:
        return p / m
    lo = idx[0] + len(idx) // 4
    hi = idx[-1] - len(idx) // 4
    plateau = p[lo:hi + 1].mean() if hi > lo else m
    return p / plateau


def main():
    ap = argparse.ArgumentParser(description="Posterior beam range diagnostic and entrance HU ray scan.")
    ap.add_argument("plan_id", type=int, nargs="?", default=None,
                    help="Plan ID from database (optional if --store and --plan-dir are passed)")
    ap.add_argument("--beam", type=int, default=None,
                    help="restrict to one beam number")
    ap.add_argument("--store", default=None,
                    help="Path directly to DICOM store containing RTPLAN and RTDOSE files")
    ap.add_argument("--plan-dir", default=None,
                    help="Path directly to plan result folder (e.g. data/results/plan_1)")
    ap.add_argument("--results", default=None,
                    help="Base results directory (e.g. data/results or backend/data/results)")
    args = ap.parse_args()

    # Resolve store
    store = args.store
    if store is None and args.plan_id is not None:
        store = find_store(args.plan_id)
    if store is None and args.plan_dir:
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
            "./backend/data/results",
            "./data/results",
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

    store_path = os.path.abspath(os.path.expanduser(store))
    print(f"Scanning DICOM Store: {store_path}")
    categorized = scan_store_dicoms(store_path)

    n_plans = len(categorized["plans"])
    n_doses = len(categorized["doses"])
    print(f"Discovered DICOM objects: {n_plans} RTPlan(s), {n_doses} RTDose(s)")

    if n_plans == 0:
        print("\n" + "=" * 80)
        print(f"ERROR: No RTPLAN file found in '{store_path}'.")
        print("=" * 80)
        if categorized["summary"]:
            print("Files found in this directory breakdown:")
            for mod_name, count in sorted(categorized["summary"].items()):
                print(f"  • {mod_name}: {count} file(s)")
        else:
            print("No readable DICOM files were discovered under this path.")
        print("\nTroubleshooting:")
        print("  1. Verify your DICOM export includes the RTPLAN (RP) file.")
        print("  2. If the plan is in a separate folder, specify: --store /path/to/plan_folder")
        print("  3. You can also pass the RTPLAN file directly: --store /path/to/RP.dcm")
        print("=" * 80)
        sys.exit(1)

    plan_beams, plan_file = load_rtplan_beams(categorized)
    if not plan_beams or not plan_file:
        fail("No valid beam sequences could be parsed from the RTPLAN file(s)")

    print(f"Active RTPLAN: {plan_file.name} (Fields: {len(plan_beams)})")

    mc_files = sorted(plan_dir.rglob("Dose_Beam*.mhd"))
    if not mc_files:
        mc_files = sorted(plan_dir.rglob("mc_dose_beam*.npz"))
    if not mc_files:
        fail("no Dose_Beam*.mhd or mc_dose_beam*.npz under %s" % plan_dir)

    ct_files = list(plan_dir.rglob("CT.mhd"))
    ct = None
    if ct_files:
        ct_arr, ct_o, ct_s = read_mhd(ct_files[0])
        # same pipeline frame as the dose handling: flip X and Y
        ct_flipped = np.ascontiguousarray(np.flip(np.flip(ct_arr, 2), 1))
        ct = (ct_flipped, ct_o, ct_s)
        print("CT: %s  dims(z,y,x)=%s" % (ct_files[0], ct_arr.shape))
    else:
        print("NOTE: CT.mhd not found - upstream HU scan skipped")

    tps_beams = load_tps_beams(categorized)
    if not tps_beams:
        fail("no BEAM RTDose files in %s" % store)

    ts = np.arange(T_MIN, T_MAX + T_STEP, T_STEP)

    for mcf in mc_files:
        bn = int("".join(ch for ch in mcf.stem if ch.isdigit()))
        if args.beam is not None and bn != args.beam:
            continue
        tps = next((t for t in tps_beams if t["beam"] == bn), None)
        pb = plan_beams.get(bn)
        if tps is None or pb is None or pb["gantry"] is None or \
           pb["iso"] is None:
            print("beam %d: missing TPS dose or plan geometry - skipped" % bn)
            continue

        iso = np.array(pb["iso"])
        d = beam_dir(pb["gantry"])
        print("\n================ BEAM %d (%s) ================"
              % (bn, pb["name"] or "?"))
        print("gantry %.1f deg   iso (%.1f, %.1f, %.1f) mm   "
              "travel dir (%.3f, %.3f, %.3f)"
              % (pb["gantry"], iso[0], iso[1], iso[2], d[0], d[1], d[2]))

        mc_raw, mc_origin, mc_spacing = read_dose_volume(mcf)
        mc = np.ascontiguousarray(np.flip(np.flip(mc_raw, 2), 1))
        mc_s = sample_on_tps(mc, mc_origin, mc_spacing, tps)
        tarr = tps["array"]

        # points along axis: t<0 upstream (entrance side), t>0 downstream
        pts = iso[None, :] + ts[:, None] * d[None, :]
        tp_prof = sample_ray_tpsgrid(tarr, tps, pts)
        mc_prof = sample_ray_tpsgrid(mc_s, tps, pts)

        tpn = norm_plateau(tp_prof)
        mcn = norm_plateau(mc_prof)

        # dose-weighted COM projection on the axis (whole 3D volume)
        def com_t(vol):
            w = vol.astype(np.float64)
            s = w.sum()
            if s <= 0:
                return None
            zi, yi, xi = np.meshgrid(
                np.arange(vol.shape[0]), np.arange(vol.shape[1]),
                np.arange(vol.shape[2]), indexing="ij")
            wx = (w * tps["xs"][xi]).sum() / s
            wy = (w * tps["ys"][yi]).sum() / s
            wz = (w * tps["zs"][zi]).sum() / s
            return float(np.dot(np.array([wx, wy, wz]) - iso, d))
        ct_tps = com_t(tarr)
        ct_mc = com_t(mc_s)
        if ct_tps is not None and ct_mc is not None:
            print("COM along axis:  TPS %+8.2f mm   MC %+8.2f mm   "
                  "(MC-TPS = %+6.2f mm along beam)"
                  % (ct_tps, ct_mc, ct_mc - ct_tps))

        for frac in (0.8, 0.5):
            tl, tr = edge_positions(ts, tpn, frac)
            ml, mr = edge_positions(ts, mcn, frac)
            if None in (tl, tr, ml, mr):
                print("  %d%% edges: not resolvable" % int(frac * 100))
                continue
            print("  %2d%% PROXIMAL edge: TPS %+8.1f mm  MC %+8.1f mm  "
                  "(MC-TPS = %+6.2f mm)"
                  % (int(frac * 100), tl, ml, ml - tl))
            print("  %2d%% DISTAL   edge: TPS %+8.1f mm  MC %+8.1f mm  "
                  "(MC-TPS = %+6.2f mm)"
                  % (int(frac * 100), tr, mr, mr - tr))

        print("  (negative MC-TPS at DISTAL edge = MC range SHORT = MC sees "
              "MORE WET upstream)")

        # upstream HU scan (entrance side) - couch visibility
        if ct is not None:
            hts = np.arange(HU_T_MIN, 0.0 + HU_STEP, HU_STEP)
            hpts = iso[None, :] + hts[:, None] * d[None, :]
            hu = sample_ray_mhd(ct[0], ct[1], ct[2], hpts)
            print("\n  upstream ray HU (t mm from iso | HU)  values > -800 "
                  "flagged as material:")
            in_mat = False
            for t, h in zip(hts, hu):
                mat = h > -800
                if mat != in_mat:
                    print("    t=%+7.1f  HU=%7.1f   %s"
                          % (t, h, "<-- enters material" if mat
                             else "<-- exits material"))
                    in_mat = mat
            # compact table every 10 mm
            print("    t(mm):  " + " ".join("%+6.0f" % t
                                            for t in hts[::5]))
            print("    HU:     " + " ".join("%6.0f" % h
                                            for h in hu[::5]))

        print("\n  depth-dose along axis (t mm | TPS_norm | MC_norm), "
              ">5%% region, every 4 mm:")
        m = max(tpn.max(), mcn.max())
        for i in range(0, len(ts), 4):
            if tpn[i] > 0.05 * m or mcn[i] > 0.05 * m:
                bar = int(30 * mcn[i] / m) if m > 0 else 0
                print("  %+8.1f | %6.3f | %6.3f  %s"
                      % (ts[i], tpn[i], mcn[i], "#" * bar))


if __name__ == "__main__":
    main()
