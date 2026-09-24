# fb_depth_check.py
#
# Reads existing MCsquare per-beam doses + TPS per-beam RTDoses for a plan and
# prints, per beam:
#   - the X (lateral/depth for FB beams) profile through the TPS dose COM,
#     MC vs TPS, each normalized to its own plateau
#   - the distal 80% and 50% falloff positions on both sides, in mm, and the
#     MC-TPS difference at each edge
#   - the raw MC max voxel location and the CT HU at that voxel (gold check)
#   - MC and TPS max doses
#
# No MCsquare run needed. Run from backend\:
#   ..\python\python.exe fb_depth_check.py <plan_id>
#
# DEPTHCHECK_V1 marker for Select-String.

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np

try:
    import pydicom
    from scipy.ndimage import map_coordinates, center_of_mass
except ImportError as e:
    print("FATAL: missing dependency: %r" % e)
    sys.exit(2)

RTDOSE_UID = "1.2.840.10008.5.1.4.1.1.481.2"
_MHD_DTYPES = {
    "MET_FLOAT": np.float32, "MET_DOUBLE": np.float64, "MET_SHORT": np.int16,
    "MET_USHORT": np.uint16, "MET_UCHAR": np.uint8, "MET_INT": np.int32,
    "MET_UINT": np.uint32,
}


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


def find_store(plan_id):
    db = Path("./data/psqa.db")
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
                        return row[0]
                except sqlite3.OperationalError:
                    continue
        except Exception:
            pass
    return None


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
            zs=ipp[2] + gfov,
            spacing=(abs(gfov[1] - gfov[0]) if len(gfov) > 1 else 1.0,
                     prow, pcol),
        ))
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


def edge_positions(xs, prof, frac):
    """Positions where profile crosses frac*max on the left and right of the
    peak region (linear interp). Returns (left_mm, right_mm)."""
    m = prof.max()
    if m <= 0:
        return None, None
    thr = frac * m
    above = prof >= thr
    if not above.any():
        return None, None
    idx = np.where(above)[0]
    li, ri = idx[0], idx[-1]
    # interp left edge
    if li > 0:
        x0, x1 = xs[li - 1], xs[li]
        y0, y1 = prof[li - 1], prof[li]
        left = x0 + (thr - y0) * (x1 - x0) / max(y1 - y0, 1e-12)
    else:
        left = xs[0]
    if ri < len(xs) - 1:
        x0, x1 = xs[ri], xs[ri + 1]
        y0, y1 = prof[ri], prof[ri + 1]
        right = x0 + (thr - y0) * (x1 - x0) / min(y1 - y0, -1e-12)
    else:
        right = xs[-1]
    return left, right


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("plan_id", type=int)
    ap.add_argument("--store", default=None)
    ap.add_argument("--results", default="./data/RESULTS")
    args = ap.parse_args()

    store = args.store or find_store(args.plan_id)
    if store is None:
        fail("could not resolve dicom store; pass --store")
    plan_dir = Path(args.results) / ("plan_%d" % args.plan_id)
    if not plan_dir.is_dir():
        # try alternate casing used by runner logs
        alt = Path("./data/results") / ("plan_%d" % args.plan_id)
        if alt.is_dir():
            plan_dir = alt
        else:
            fail("plan folder not found under %s" % args.results)

    mc_files = sorted(plan_dir.rglob("Dose_Beam*.mhd"))
    if not mc_files:
        fail("no Dose_Beam*.mhd under %s" % plan_dir)
    ct_files = list(plan_dir.rglob("CT.mhd"))
    ct = None
    if ct_files:
        ct_arr, ct_origin, ct_spacing = read_mhd(ct_files[0])
        ct = (ct_arr, ct_origin, ct_spacing)
        print("CT: %s  dims(z,y,x)=%s" % (ct_files[0], ct_arr.shape))
    else:
        print("NOTE: CT.mhd not found - skipping HU-at-max check")

    tps_beams = load_tps_beams(store)
    if not tps_beams:
        fail("no BEAM RTDose files in %s" % store)
    print("TPS beam grid spacing (z,y,x) mm: %s"
          % (tps_beams[0]["spacing"],))

    for mcf in mc_files:
        # beam number from filename Dose_BeamN.mhd
        bn = int("".join(ch for ch in mcf.stem if ch.isdigit()))
        tps = next((t for t in tps_beams if t["beam"] == bn), None)
        if tps is None:
            print("beam %d: no matching TPS beam dose - skipped" % bn)
            continue

        mc_raw, mc_origin, mc_spacing = read_mhd(mcf)

        # HU at raw MC max voxel (before any flips - same array MCsquare wrote)
        zi, yi, xi = np.unravel_index(np.argmax(mc_raw), mc_raw.shape)
        print("\n================ BEAM %d ================" % bn)
        print("MC raw max voxel (z,y,x)=(%d,%d,%d)  raw value=%.4g"
              % (zi, yi, xi, mc_raw.max()))
        if ct is not None:
            ct_arr = ct[0]
            if ct_arr.shape == mc_raw.shape:
                hu = float(ct_arr[zi, yi, xi])
                # small neighborhood max HU
                z0, z1 = max(zi - 2, 0), min(zi + 3, ct_arr.shape[0])
                y0, y1 = max(yi - 2, 0), min(yi + 3, ct_arr.shape[1])
                x0, x1 = max(xi - 2, 0), min(xi + 3, ct_arr.shape[2])
                hu_nb = float(ct_arr[z0:z1, y0:y1, x0:x1].max())
                print("CT HU at max voxel = %.0f   (5x5x5 neighborhood max "
                      "HU = %.0f)" % (hu, hu_nb))
                if hu_nb > 3000:
                    print(">>> MC max sits in/near a HIGH-Z override region "
                          "(gold?) - max value is a noise artifact")
            else:
                print("CT shape differs from dose shape - HU check skipped")

        # pipeline frame: flip X and Y, then sample onto TPS grid
        mc = np.ascontiguousarray(np.flip(np.flip(mc_raw, 2), 1))
        mc_s = sample_on_tps(mc, mc_origin, mc_spacing, tps)

        tarr = tps["array"]
        print("TPS beam max = %.3f Gy    (MC array is unscaled raw*flip; "
              "profiles below are normalized)" % tarr.max())

        # profile line through TPS COM in (z,y), along X
        czi, cyi, cxi = (int(round(v)) for v in center_of_mass(tarr))
        czi = min(max(czi, 0), tarr.shape[0] - 1)
        cyi = min(max(cyi, 0), tarr.shape[1] - 1)
        xs = tps["xs"]
        tp_prof = tarr[czi, cyi, :].astype(float)
        mc_prof = mc_s[czi, cyi, :].astype(float)

        # normalize each to its own central-plateau mean (middle 50% of the
        # above-50% region) so shape/range compare independent of scaling
        def norm(p):
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

        tpn = norm(tp_prof)
        mcn = norm(mc_prof)

        for frac in (0.8, 0.5):
            tl, tr = edge_positions(xs, tpn, frac)
            ml, mr = edge_positions(xs, mcn, frac)
            if None in (tl, tr, ml, mr):
                print("  %d%% edges: not resolvable" % int(frac * 100))
                continue
            print("  %2d%% edge  LEFT : TPS %8.1f mm  MC %8.1f mm  "
                  "(MC-TPS = %+6.2f mm)"
                  % (int(frac * 100), tl, ml, ml - tl))
            print("  %2d%% edge  RIGHT: TPS %8.1f mm  MC %8.1f mm  "
                  "(MC-TPS = %+6.2f mm)"
                  % (int(frac * 100), tr, mr, mr - tr))

        print("\n  profile (world X mm | TPS_norm | MC_norm)  every 2nd "
              "voxel, >5%% region:")
        m = max(tpn.max(), mcn.max())
        for i in range(0, len(xs), 2):
            if tpn[i] > 0.05 * m or mcn[i] > 0.05 * m:
                bar_len = int(30 * mcn[i] / m) if m > 0 else 0
                print("  %8.1f | %6.3f | %6.3f  %s"
                      % (xs[i], tpn[i], mcn[i], "#" * bar_len))


if __name__ == "__main__":
    main()
