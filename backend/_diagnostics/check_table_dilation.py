#!/usr/bin/env python3
"""
check_table_dilation.py — Evaluate couch wall dilation values for posterior beams.

Quantifies the central-axis Water-Equivalent Thickness (WET) through the treatment
couch under different in-plane dilation values (0, 1, 2, 3, 4 voxels) and compares
against the observed distal range shift between MCsquare and TPS doses.

Usage:
  python check_table_dilation.py <plan_id>
  python check_table_dilation.py --store /path/to/dicom_store
  python check_table_dilation.py --store /path/to/dicom_store --plan-dir /path/to/results/plan_X
  python check_table_dilation.py --store /path/to/RTPLAN.dcm
"""

import argparse
import math
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

try:
    import pydicom
    from PIL import Image, ImageDraw
    from scipy.ndimage import binary_dilation, map_coordinates
except ImportError as e:
    print(f"FATAL: Missing dependency: {e}")
    sys.exit(2)

IGNORE_EXTS = {
    ".raw", ".mhd", ".txt", ".json", ".npz", ".py", ".png", ".jpg",
    ".jpeg", ".csv", ".log", ".sh", ".bat", ".exe", ".dll", ".so", ".pyc"
}

RTIONPLAN_UID = "1.2.840.10008.5.1.4.1.1.481.8"
RTPLAN_UID = "1.2.840.10008.5.1.4.1.1.481.5"
RTDOSE_UID = "1.2.840.10008.5.1.4.1.1.481.2"
RTSTRUCT_UID = "1.2.840.10008.5.1.4.1.1.481.3"
CT_IMAGE_UID = "1.2.840.10008.5.1.4.1.1.2"

AIR_HU = -1000.0


def fail(msg: str):
    print(f"FATAL: {msg}")
    sys.exit(1)


def find_plan_info(plan_id: int) -> dict:
    candidates = [
        Path("./backend/data/psqa.db"),
        Path("./data/psqa.db"),
        Path("../data/psqa.db"),
        Path("../../backend/data/psqa.db"),
    ]
    for db in candidates:
        if db.exists():
            try:
                con = sqlite3.connect(str(db))
                cur = con.cursor()
                for t in ("plans", "plan"):
                    try:
                        cur.execute(
                            "SELECT dicom_store_path, rtplan_uid, plan_name FROM %s WHERE id=?" % t,
                            (plan_id,),
                        )
                        row = cur.fetchone()
                        if row:
                            raw_path, rtplan_uid, plan_name = row[0], row[1], row[2]
                            resolved_store = None
                            if raw_path:
                                store_candidates = [
                                    Path(raw_path),
                                    Path("backend") / raw_path,
                                    Path("..") / raw_path,
                                    db.parent / raw_path,
                                    db.parent.parent / raw_path,
                                ]
                                for sc in store_candidates:
                                    if sc.exists():
                                        resolved_store = str(sc)
                                        break
                            return {
                                "store_path": resolved_store or raw_path,
                                "rtplan_uid": rtplan_uid,
                                "plan_name": plan_name,
                            }
                    except sqlite3.OperationalError:
                        continue
            except Exception:
                pass
    return {}


def find_store(plan_id: int) -> Optional[str]:
    return find_plan_info(plan_id).get("store_path")


def find_plan_results(plan_id: int, base_dir: Optional[str] = None) -> Optional[Path]:
    res_dirs = [base_dir] if base_dir else [
        "./backend/data/results",
        "./data/results",
        "../data/results",
        "../backend/data/results",
        "./backend/data/RESULTS",
        "./data/RESULTS",
    ]
    for rd in res_dirs:
        if rd and Path(rd).is_dir():
            cand = Path(rd) / f"plan_{plan_id}"
            if cand.is_dir():
                return cand
    return None


def read_mhd(mhd_path: Path):
    meta = {}
    for line in mhd_path.read_text().splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            meta[k.strip()] = v.strip()
    nx, ny, nz = (int(v) for v in meta["DimSize"].split())
    sx, sy, sz = (float(v) for v in meta.get("ElementSpacing", "1 1 1").split())
    off = meta.get("Offset", "0 0 0").split()
    origin = (float(off[0]), float(off[1]), float(off[2]))
    dtype_str = meta.get("ElementType", "MET_FLOAT")
    dtype = np.float64 if dtype_str == "MET_DOUBLE" else np.float32
    raw = mhd_path.parent / meta.get("ElementDataFile", mhd_path.stem + ".raw")
    arr = np.fromfile(str(raw), dtype=dtype).reshape((nz, ny, nx)).astype(np.float32)
    return arr, origin, (sx, sy, sz)


def read_dose_volume(dose_path: Path):
    if dose_path.suffix.lower() == ".npz":
        data = np.load(str(dose_path))
        arr = data["array"].astype(np.float32)
        sp = data["spacing"]
        orig = data["origin"]
        spacing = (float(sp[2]), float(sp[1]), float(sp[0]))
        origin = (float(orig[2]), float(orig[1]), float(orig[0]))
        return arr, origin, spacing
    return read_mhd(dose_path)


def scan_store_dicoms(store_path: str):
    """
    Recursively inspects store_path and categorizes all DICOM files by modality and SOP Class UID.
    Handles single files, directories, nested series folders, and files without extensions.
    """
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

            if sop == "1.2.840.10008.5.1.4.1.1.481.9" or mod == "RTRECORD":
                continue

            if (
                sop in (RTIONPLAN_UID, RTPLAN_UID)
                or mod in ("RTPLAN", "PLAN")
                or hasattr(d, "IonBeamSequence")
                or hasattr(d, "BeamSequence")
            ):
                categorized["plans"].append((f, d))
            elif sop == RTDOSE_UID or mod == "RTDOSE":
                categorized["doses"].append((f, d))
            elif sop == RTSTRUCT_UID or mod == "RTSTRUCT":
                categorized["structs"].append((f, d))
            elif sop == CT_IMAGE_UID or mod == "CT":
                categorized["cts"].append((f, d))
        except Exception:
            continue

    # Fallback: if no plans found in current folder, check parent directory if it was a subfolder
    if not categorized["plans"] and p.is_dir() and p.parent.is_dir() and p.parent != p:
        parent_plans = []
        for f in p.parent.iterdir():
            if f.is_file() and f.suffix.lower() not in IGNORE_EXTS:
                try:
                    d = pydicom.dcmread(str(f), stop_before_pixels=True, force=True)
                    sop = str(getattr(d, "SOPClassUID", ""))
                    mod = str(getattr(d, "Modality", "")).upper()
                    if sop == "1.2.840.10008.5.1.4.1.1.481.9" or mod == "RTRECORD":
                        continue
                    if (
                        sop in (RTIONPLAN_UID, RTPLAN_UID)
                        or mod in ("RTPLAN", "PLAN")
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


def load_rtplan_beams(
    categorized: dict,
    target_uid: Optional[str] = None,
    target_name: Optional[str] = None,
) -> Tuple[Dict[int, dict], Optional[Path], Optional[object]]:
    valid_plans = []

    for f, d in categorized["plans"]:
        seq = getattr(d, "IonBeamSequence", None) or getattr(d, "BeamSequence", None)
        if not seq:
            continue

        uid = str(getattr(d, "SOPInstanceUID", ""))
        label = str(getattr(d, "RTPlanLabel", "") or getattr(d, "RTPlanName", "") or f.stem)

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

            plan_beams[bn] = dict(name=name, gantry=gantry, iso=iso, dcm_beam=b)

        valid_plans.append({
            "file": f,
            "dcm": d,
            "uid": uid,
            "label": label,
            "beams": plan_beams,
            "treatment_beams": treatment_beams,
        })

    if not valid_plans:
        return {}, None, None

    chosen = None

    # 1. Match by target UID (e.g. from database plan_id)
    if target_uid:
        chosen = next((p for p in valid_plans if p["uid"] == target_uid), None)

    # 2. Match by target name / label
    if not chosen and target_name:
        chosen = next(
            (p for p in valid_plans if target_name.lower() in p["label"].lower() or target_name.lower() in p["file"].name.lower()),
            None
        )

    # 3. Match against RTDOSE references in store
    if not chosen:
        ref_uids = set()
        for _, dd in categorized.get("doses", []):
            rseq = getattr(dd, "ReferencedRTPlanSequence", None)
            if rseq and len(rseq) > 0 and hasattr(rseq[0], "ReferencedSOPInstanceUID"):
                ref_uids.add(str(rseq[0].ReferencedSOPInstanceUID))
        if ref_uids:
            dose_matches = [p for p in valid_plans if p["uid"] in ref_uids]
            if dose_matches:
                dose_matches.sort(key=lambda p: p["treatment_beams"], reverse=True)
                chosen = dose_matches[0]

    # 4. Default: highest treatment beam count
    if not chosen:
        valid_plans.sort(key=lambda p: p["treatment_beams"], reverse=True)
        chosen = valid_plans[0]

    if len(valid_plans) > 1:
        print(f"\nNOTE: Found {len(valid_plans)} RTPLAN(s) under search path:")
        for idx, vp in enumerate(valid_plans, 1):
            is_active = (vp["file"] == chosen["file"])
            marker = " -> [ACTIVE]" if is_active else "    "
            print(f"{marker} [{idx}] {vp['file'].name} | Label: '{vp['label']}' | Treatment Fields: {vp['treatment_beams']} | UID: {vp['uid'][:32]}...")
        if not target_uid and not target_name:
            print("  Hint: Use Option 3 ('python check_table_dilation.py <plan_id>') or pass --plan-name to select a different plan.\n")

    return chosen["beams"], chosen["file"], chosen["dcm"]


def load_tps_beams(categorized: dict, active_plan_uid: Optional[str] = None) -> List[dict]:
    out = []
    for f, _ in categorized["doses"]:
        try:
            full_d = pydicom.dcmread(str(f), force=True)
            if str(getattr(full_d, "DoseSummationType", "")).upper() != "BEAM":
                continue

            # If active_plan_uid is specified, verify this dose belongs to the chosen RTPLAN
            if active_plan_uid:
                ref_seq = getattr(full_d, "ReferencedRTPlanSequence", None)
                if ref_seq and len(ref_seq) > 0 and hasattr(ref_seq[0], "ReferencedSOPInstanceUID"):
                    dose_plan_uid = str(ref_seq[0].ReferencedSOPInstanceUID)
                    if dose_plan_uid != active_plan_uid:
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


def beam_dir(gantry_deg: float) -> np.ndarray:
    """Direction vector of beam travel (HFS, couch 0):
    gantry 0 = anterior (+Y travel), 90 = left (-X travel), 180 = posterior (-Y travel)."""
    g = math.radians(gantry_deg)
    return np.array([-math.sin(g), math.cos(g), 0.0])


def load_raw_ct_geometry(categorized: dict, plan_dir: Optional[Path] = None):
    # 1. CT slices from DICOM store
    if categorized["cts"]:
        slices = [pydicom.dcmread(str(f), stop_before_pixels=True, force=True) for f, _ in categorized["cts"]]
        slices.sort(key=lambda d: float(d.ImagePositionPatient[2]))
        d0 = slices[0]
        ipp = [float(d0.ImagePositionPatient[0]), float(d0.ImagePositionPatient[1]), float(d0.ImagePositionPatient[2])]
        prow, pcol = float(d0.PixelSpacing[0]), float(d0.PixelSpacing[1])
        dz = float(slices[1].ImagePositionPatient[2]) - float(slices[0].ImagePositionPatient[2]) if len(slices) > 1 else 2.0
        grid_size = (int(d0.Rows), int(d0.Columns), len(slices))
        return grid_size, ipp, (pcol, prow, dz)

    # 2. Fallback: CT.mhd from plan results directory
    if plan_dir:
        ct_mhds = list(plan_dir.rglob("CT.mhd"))
        if ct_mhds:
            ct_mhd = ct_mhds[0]
            arr, origin, spacing = read_mhd(ct_mhd)
            nz, ny, nx = arr.shape
            ipp = [origin[0], origin[1], origin[2]]
            ps = [spacing[0], spacing[1], spacing[2]]
            return (ny, nx, nz), ipp, ps

    return None, None, None


def load_rtstruct(categorized: dict, active_plan_dcm: Optional[object] = None) -> Optional[Path]:
    if not categorized["structs"]:
        return None
    if active_plan_dcm is not None:
        ref_seq = getattr(active_plan_dcm, "ReferencedStructureSetSequence", None)
        if ref_seq and len(ref_seq) > 0 and hasattr(ref_seq[0], "ReferencedSOPInstanceUID"):
            target_uid = str(ref_seq[0].ReferencedSOPInstanceUID)
            for f, d in categorized["structs"]:
                if str(getattr(d, "SOPInstanceUID", "")) == target_uid:
                    return f
    for f, d in categorized["structs"]:
        names = [str(s.ROIName).lower() for s in getattr(d, "StructureSetROISequence", [])]
        if any("couch" in n or "shell" in n or "table" in n or "qfix" in n for n in names):
            return f
    return categorized["structs"][0][0]


def rasterize_roi(rc, grid_size, ipp, ps) -> np.ndarray:
    ny, nx, nz = grid_size
    mask = np.zeros((ny, nx, nz), dtype=bool)
    for dslice in getattr(rc, "ContourSequence", []):
        cd = getattr(dslice, "ContourData", None)
        if cd is None or len(cd) < 3:
            continue
        xs = (np.asarray(cd[0::3], dtype=float) - ipp[0]) / ps[0]
        ys = (np.asarray(cd[1::3], dtype=float) - ipp[1]) / ps[1]
        sid = int(round((float(cd[2]) - ipp[2]) / ps[2]))
        if sid < 0 or sid >= nz:
            continue
        xy = list(zip(xs, ys))
        if len(xy) < 3:
            continue
        img = Image.new("L", (nx, ny), 0)
        ImageDraw.Draw(img).polygon(xy, outline=1, fill=1)
        mask[:, :, sid] |= np.array(img, dtype=bool)
    return mask


def get_couch_contours(rtstruct_path: Path):
    dcm = pydicom.dcmread(str(rtstruct_path), force=True)
    names = {s.ROINumber: str(s.ROIName) for s in getattr(dcm, "StructureSetROISequence", [])}
    overrides = {}
    for obs in getattr(dcm, "RTROIObservationsSequence", []):
        num = getattr(obs, "ReferencedROINumber", None)
        if num in names and (0x300A, 0x00E1) in obs:
            overrides[names[num]] = str(obs[0x300A, 0x00E1].value)

    contours = {}
    for rc in getattr(dcm, "ROIContourSequence", []):
        num = getattr(rc, "ReferencedROINumber", None)
        if num in names:
            contours[names[num]] = rc

    shells = []
    cores = []
    external = None

    for name, rc in contours.items():
        low = name.lower()
        mat = overrides.get(name, "").lower()
        if "external" in low:
            external = rc
        elif "air" in mat or "core" in low:
            cores.append((name, rc))
        elif "shell" in low or "couch" in low or "table" in low or "qfix" in low or "2.03" in mat or "1.2" in mat:
            density = 2.03 if ("2.03" in mat or "shell" in low) else 1.20
            shells.append((name, rc, density))

    return shells, cores, external


def compute_distal_falloff(ts, prof, frac=0.8):
    m = prof.max()
    if m <= 0:
        return None
    pk = int(np.argmax(prof))
    thr = frac * m
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


def measure_dose_range_shift(mcf: Path, tps: dict, pb: dict) -> Optional[float]:
    """Measure distal 80% range difference (MC - TPS) in mm along beam axis."""
    try:
        iso = np.array(pb["iso"])
        d = beam_dir(pb["gantry"])
        mc_raw, mc_o, mc_sp = read_dose_volume(mcf)
        mc = np.ascontiguousarray(np.flip(np.flip(mc_raw, 2), 1))

        ox, oy, oz = mc_o
        sx, sy, sz = mc_sp
        zi = (tps["zs"] - oz) / sz
        yi = (tps["ys"] - oy) / sy
        xi = (tps["xs"] - ox) / sx
        Z, Y, X = np.meshgrid(zi, yi, xi, indexing="ij")
        mc_s = map_coordinates(mc, [Z, Y, X], order=1, mode="constant", cval=0.0)

        ts = np.arange(-350.0, 350.0 + 1.0, 1.0)
        pts = iso[None, :] + ts[:, None] * d[None, :]

        xs, ys, zs = tps["xs"], tps["ys"], tps["zs"]
        dx = xs[1] - xs[0] if len(xs) > 1 else 1.0
        dy = ys[1] - ys[0] if len(ys) > 1 else 1.0
        dz = zs[1] - zs[0] if len(zs) > 1 else 1.0
        xi_p = (pts[:, 0] - xs[0]) / dx
        yi_p = (pts[:, 1] - ys[0]) / dy
        zi_p = (pts[:, 2] - zs[0]) / dz

        tp_prof = map_coordinates(tps["array"], [zi_p, yi_p, xi_p], order=1, mode="constant", cval=0.0)
        mc_prof = map_coordinates(mc_s, [zi_p, yi_p, xi_p], order=1, mode="constant", cval=0.0)

        t_edge = compute_distal_falloff(ts, tp_prof, 0.8)
        m_edge = compute_distal_falloff(ts, mc_prof, 0.8)
        if t_edge is not None and m_edge is not None:
            return float(m_edge - t_edge)
    except Exception:
        pass
    return None


def main():
    parser = argparse.ArgumentParser(
        description="Check and simulate couch wall dilation values for posterior beams."
    )
    parser.add_argument("plan_id", type=int, nargs="?", default=None, help="Plan ID in database")
    parser.add_argument("--store", default=None, help="Path to DICOM store directory or RTPLAN file")
    parser.add_argument("--plan-dir", default=None, help="Path to plan results directory")
    parser.add_argument("--plan-name", default=None, help="Specific plan name or label if store contains multiple plans")
    parser.add_argument("--beam", type=int, default=None, help="Specific beam number to check")
    args = parser.parse_args()

    plan_id = args.plan_id
    if plan_id is None and args.plan_dir:
        m = re.search(r"plan[_\-](\d+)", str(args.plan_dir))
        if m:
            plan_id = int(m.group(1))

    plan_info = find_plan_info(plan_id) if plan_id is not None else {}

    store = args.store
    if store is None and plan_info.get("store_path"):
        store = plan_info["store_path"]
    if store is None and args.plan_dir:
        pd = Path(args.plan_dir)
        if pd.is_dir():
            store = str(pd)
    if store is None:
        fail("Could not find DICOM store. Specify with --store /path/to/dicom_folder or pass <plan_id>")

    store_path = os.path.abspath(os.path.expanduser(store))
    print(f"Scanning DICOM Store: {store_path}")

    categorized = scan_store_dicoms(store_path)

    n_plans = len(categorized["plans"])
    n_doses = len(categorized["doses"])
    n_structs = len(categorized["structs"])
    n_cts = len(categorized["cts"])

    print(f"Discovered DICOM objects: {n_plans} RTPlan(s), {n_doses} RTDose(s), {n_structs} RTStruct(s), {n_cts} CT slice(s)")

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

    target_uid = plan_info.get("rtplan_uid")
    plan_beams, plan_file, plan_dcm = load_rtplan_beams(
        categorized,
        target_uid=target_uid,
        target_name=args.plan_name,
    )
    if not plan_beams or not plan_file:
        fail("No valid beam sequences could be parsed from the RTPLAN file(s)")

    print(f"Active RTPLAN: {plan_file.name} (Fields: {len(plan_beams)})")

    # Locate results folder
    plan_dir = None
    if args.plan_dir:
        plan_dir = Path(args.plan_dir)
    elif plan_id is not None:
        plan_dir = find_plan_results(plan_id)

    rtstruct_path = load_rtstruct(categorized, active_plan_dcm=plan_dcm)
    if not rtstruct_path:
        fail("No RTSTRUCT found in store (needed for couch contours)")
    print(f"RTSTRUCT:     {rtstruct_path.name}")

    grid_size, ipp, ps = load_raw_ct_geometry(categorized, plan_dir=plan_dir)
    if not grid_size:
        fail("Could not load CT geometry (no CT slices in store and no CT.mhd in plan-dir)")
    ny, nx, nz = grid_size
    print(f"CT Geometry:  Grid=(Y={ny}, X={nx}, Z={nz})  PixelSpacing=({ps[0]:.2f}, {ps[1]:.2f}) mm  SliceThickness={ps[2]:.2f} mm")

    shells, cores, ext = get_couch_contours(rtstruct_path)
    if not shells:
        print("\nWARNING: No couch or support shell structures identified in RTSTRUCT.")
        print("Material override / dilation only applies when couch structures exist.")
        return

    print(f"\nIdentified Couch Structures:")
    for name, rc, dens in shells:
        print(f"  • Shell: '{name}' (Target density: {dens:.2f} g/cm3)")
    for name, rc in cores:
        print(f"  • Core:  '{name}' (Air, -1000 HU)")

    print("\nRasterizing couch contours...")
    raw_shells = [(name, rasterize_roi(rc, grid_size, ipp, ps), dens) for name, rc, dens in shells]
    core_mask = np.zeros(grid_size, dtype=bool)
    for name, rc in cores:
        core_mask |= rasterize_roi(rc, grid_size, ipp, ps)

    all_shell_mask = np.zeros(grid_size, dtype=bool)
    for _, smask, _ in raw_shells:
        all_shell_mask |= smask
    couch_z_indices = np.where(all_shell_mask.any(axis=(0, 1)))[0]
    if len(couch_z_indices) > 0:
        couch_z_min_mm = ipp[2] + couch_z_indices[0] * ps[2]
        couch_z_max_mm = ipp[2] + couch_z_indices[-1] * ps[2]
        couch_z_mid_idx = couch_z_indices[len(couch_z_indices) // 2]
        couch_z_mid_mm = ipp[2] + couch_z_mid_idx * ps[2]
        print(f"Couch Z-extent: {couch_z_min_mm:.1f} to {couch_z_max_mm:.1f} mm (slices {couch_z_indices[0]} to {couch_z_indices[-1]})")
    else:
        couch_z_mid_mm = None

    active_uid = str(getattr(plan_dcm, "SOPInstanceUID", "")) if plan_dcm else None
    tps_beams = load_tps_beams(categorized, active_plan_uid=active_uid) if plan_dir else []
    mc_files = sorted(plan_dir.rglob("Dose_Beam*.mhd")) if plan_dir else []
    if not mc_files and plan_dir:
        mc_files = sorted(plan_dir.rglob("mc_dose_beam*.npz"))

    candidate_dilations = [0, 1, 2, 3, 4]
    struct_2d = np.zeros((3, 3, 1), dtype=bool)
    struct_2d[:, :, 0] = True  # in-plane only

    for bn, pb in plan_beams.items():
        if args.beam is not None and bn != args.beam:
            continue
        gantry = pb["gantry"]
        iso = pb["iso"]
        if gantry is None or iso is None:
            continue

        d = beam_dir(gantry)
        is_posterior = 90.0 <= (gantry % 360.0) <= 270.0

        print(f"\n================================================================================")
        print(f"BEAM {bn}: {pb['name']} (Gantry {gantry:.1f}°) — {'POSTERIOR (Enters through Couch)' if is_posterior else 'Anterior/Oblique'}")
        print(f"Isocenter: ({iso[0]:.1f}, {iso[1]:.1f}, {iso[2]:.1f}) mm  Travel Dir: ({d[0]:.3f}, {d[1]:.3f}, {d[2]:.3f})")

        eval_z = iso[2]
        if len(couch_z_indices) > 0 and (eval_z < couch_z_min_mm or eval_z > couch_z_max_mm):
            print(f"NOTE: Isocenter Z ({eval_z:.1f} mm) is outside the couch Z range ({couch_z_min_mm:.1f}..{couch_z_max_mm:.1f} mm).")
            print(f"      Evaluating couch ray at representative couch slice Z={couch_z_mid_mm:.1f} mm.")
            eval_z = couch_z_mid_mm

        range_error_mm = None
        if tps_beams and mc_files:
            mcf = next((m for m in mc_files if str(bn) in m.stem), None)
            tps = next((t for t in tps_beams if t["beam"] == bn), None)
            if mcf and tps:
                range_error_mm = measure_dose_range_shift(mcf, tps, pb)
                if range_error_mm is not None:
                    status = "OVERRANGING / OVERSHOOT" if range_error_mm > 0 else "UNDERRANGING / SHORT"
                    print(f"Observed 80% Distal Range Shift (MC - TPS): {range_error_mm:+.2f} mm  [{status}]")

        step_mm = 0.5
        ts = np.arange(-350.0, 350.0, step_mm)
        pts = np.array([iso[0], iso[1], eval_z])[None, :] + ts[:, None] * d[None, :]

        xi = (pts[:, 0] - ipp[0]) / ps[0]
        yi = (pts[:, 1] - ipp[1]) / ps[1]
        zi = (pts[:, 2] - ipp[2]) / ps[2]

        valid = (xi >= 0) & (xi < nx - 1) & (yi >= 0) & (yi < ny - 1) & (zi >= 0) & (zi < nz - 1)
        zi_v, yi_v, xi_v = zi[valid], yi[valid], xi[valid]

        print("-" * 80)
        print(f"{'Dilation':<15} | {'Shell Voxels':<14} | {'Couch WET (mm)':<16} | {'Δ WET vs 0':<12} | {'Predicted Residual Shift'}")
        print("-" * 80)

        wet_at_0 = None
        results = []

        for dil in candidate_dilations:
            density_vol = np.zeros(grid_size, dtype=np.float32)

            total_voxels = 0
            for sname, smask, dens in raw_shells:
                m = smask
                if dil > 0:
                    m = binary_dilation(smask, structure=struct_2d, iterations=dil)
                density_vol[m] = float(dens)
                total_voxels += int(m.sum())

            density_vol[core_mask] = 0.0

            # Transpose (Y, X, Z) -> (Z, Y, X) for map_coordinates
            dens_zyx = np.transpose(density_vol, (2, 0, 1))

            dens_ray = map_coordinates(
                dens_zyx,
                [zi_v, yi_v, xi_v],
                order=1,
                mode="constant",
                cval=0.0,
            )
            wet_mm = float(np.sum(dens_ray) * step_mm)

            if dil == 0:
                wet_at_0 = wet_mm
            delta_wet = wet_mm - (wet_at_0 or 0.0)

            res_shift_str = "—"
            if range_error_mm is not None:
                pred_shift = range_error_mm - delta_wet
                match_tag = " (EXACT MATCH!)" if abs(pred_shift) < 0.3 else ""
                res_shift_str = f"{pred_shift:+.2f} mm{match_tag}"

            print(
                f"{f'{dil} vox' + (' (current)' if dil == 0 else ''):<15} | "
                f"{total_voxels:<14,d} | "
                f"{wet_mm:<16.2f} | "
                f"{f'{delta_wet:+.2f} mm':<12} | "
                f"{res_shift_str}"
            )
            results.append((dil, delta_wet, wet_mm))

        print("-" * 80)
        if range_error_mm is not None and range_error_mm > 0.3:
            best_dil = min(results, key=lambda r: abs(range_error_mm - r[1]))[0]
            print(f"\n>>> CLINICAL RECOMMENDATION FOR THIS BEAM:")
            print(f"    Observed overranging is +{range_error_mm:.2f} mm.")
            print(f"    Setting COUCH_WALL_DILATION_VOX = {best_dil} in backend/ct_density_override.py")
            print(f"    adds approximately +{results[best_dil][1]:.2f} mm of WET to cancel out this overranging.")
        elif range_error_mm is not None:
            print(f"\n>>> Dilation evaluation: Distal edges match within {abs(range_error_mm):.2f} mm.")

    print("\n" + "=" * 80)
    print("HOW TO APPLY A DILATION VALUE CLINICALLY:")
    print("  1. Open backend/ct_density_override.py")
    print("  2. Set line ~128: COUCH_WALL_DILATION_VOX = <target_voxels>  (e.g. 1 or 2)")
    print("  3. Rerun the QA pipeline for the plan.")
    print("=" * 80)


if __name__ == "__main__":
    main()
