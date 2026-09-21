"""
backend/services/oir_service.py -- Offline Image Review (OIR) service.

Provides:
1. Retrieval of Planning CT (TPCT) volume metadata and slice geometry.
2. Per-fraction CBCT series discovery (handling 1-2 CBCT series per treatment).
3. Registration matrix extraction from DICOM REG files ("Treated Match") and
   nominal hardware geometry ("Initial Setup / Unregistered").
4. Fast 2D slice resampling of CBCT onto the Planning CT grid using 6-DoF rigid transforms.
5. RTSTRUCT ROI extraction and 2D contour polygon projection into image pixel space.
6. Physicist weekly / 5-fraction chart check sign-off persistence and tracking.
"""
from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import pydicom
from scipy.ndimage import map_coordinates
from sqlalchemy.orm import Session

from config import settings
from models.fraction import Fraction
from models.plan import Plan
from services.imaging.dicom_volume_loader import (
    SeriesRecord,
    VolumeGeometry,
    load_series_volume,
    scan_dicom_slices,
)
from services.imaging.rigid_transforms import (
    RigidTransform,
    euler_deg_from_matrix,
    from_matrix44,
    to_matrix44,
)

logger = logging.getLogger(__name__)

# LRU In-memory Volume Cache
# Keys: "tpct:{plan_id}" or "cbct:{plan_id}:{fraction_num}:{series_uid}"
_VOLUME_CACHE: Dict[str, Tuple[np.ndarray, VolumeGeometry, dict, float]] = {}
_MAX_CACHED_VOLUMES = 6

# Parsed RTSTRUCT cache: plan_id -> (mtime, parsed_contours_list)
_RTSTRUCT_CACHE: Dict[int, Tuple[float, List[Tuple[float, int, str, List[int], np.ndarray]]]] = {}

# Parsed REG cache: reg_file_path -> (mtime, 4x4 matrix, shifts_dict)
_REG_CACHE: Dict[str, Tuple[float, np.ndarray, dict]] = {}


def _manage_cache(key: str, volume: np.ndarray, geom: VolumeGeometry, meta: dict) -> None:
    """Store volume in cache with timestamp and evict oldest if exceeding limit."""
    import time
    now = time.time()
    if len(_VOLUME_CACHE) >= _MAX_CACHED_VOLUMES and key not in _VOLUME_CACHE:
        oldest_key = min(_VOLUME_CACHE.keys(), key=lambda k: _VOLUME_CACHE[k][3])
        logger.info(f"OIR Cache evicting volume: {oldest_key}")
        del _VOLUME_CACHE[oldest_key]
    _VOLUME_CACHE[key] = (volume, geom, meta, now)


def _get_from_cache(key: str) -> Optional[Tuple[np.ndarray, VolumeGeometry, dict]]:
    """Retrieve volume from cache if present, updating access timestamp."""
    import time
    if key in _VOLUME_CACHE:
        vol, geom, meta, _ = _VOLUME_CACHE[key]
        _VOLUME_CACHE[key] = (vol, geom, meta, time.time())
        return vol, geom, meta
    return None


def resolve_plan_store_path(plan: Plan) -> Optional[Path]:
    """Resolve DICOM store directory path across both Linux and Windows installations."""
    if not plan or not plan.dicom_store_path:
        return None
    p = Path(plan.dicom_store_path)
    if p.is_absolute() and p.exists():
        return p
    if p.exists():
        return p.resolve()
    # Check relative to backend/
    backend_dir = Path(__file__).resolve().parent.parent
    bp = (backend_dir / plan.dicom_store_path).resolve()
    if bp.exists():
        return bp
    # Check in settings.DICOM_STORE_PATH
    store_base = Path(settings.DICOM_STORE_PATH)
    if not store_base.is_absolute():
        store_base = (backend_dir / store_base).resolve()
    candidate = (store_base / p.name).resolve()
    if candidate.exists():
        return candidate
    return None


def resolve_results_dir(plan_id: int) -> Path:
    """Resolve results path for a given plan."""
    results_base = Path(settings.RESULTS_PATH)
    if not results_base.is_absolute():
        backend_dir = Path(__file__).resolve().parent.parent
        results_base = (backend_dir / results_base).resolve()
    return results_base / f"plan_{plan_id}"


def get_plan_tpct_volume(plan_id: int, db: Session) -> Tuple[np.ndarray, VolumeGeometry, dict]:
    """Retrieve or load Planning CT (TPCT) volume for a plan."""
    cache_key = f"tpct:{plan_id}"
    cached = _get_from_cache(cache_key)
    if cached:
        return cached

    plan = db.query(Plan).filter_by(id=plan_id).first()
    if not plan:
        raise ValueError(f"Plan {plan_id} not found in database.")

    plan_dir = resolve_plan_store_path(plan)
    if not plan_dir or not plan_dir.exists():
        raise FileNotFoundError(f"DICOM store directory not found for plan {plan_id}: {plan.dicom_store_path}")

    series_list = scan_dicom_slices([plan_dir])
    if not series_list:
        raise FileNotFoundError(f"No CT image series found in {plan_dir}.")

    # Pick the CT series with the most slices (planning CT)
    best_series = max(series_list, key=lambda s: len(s.slices))
    volume, geometry, meta = load_series_volume(best_series)
    _manage_cache(cache_key, volume, geometry, meta)
    return volume, geometry, meta


def find_plan_rtstruct(plan: Plan) -> Optional[Path]:
    """Find RTSTRUCT file associated with the plan."""
    plan_dir = resolve_plan_store_path(plan)
    if not plan_dir or not plan_dir.exists():
        return None

    # 1. Search plan_dir directly
    for f in plan_dir.glob("*.dcm"):
        try:
            ds = pydicom.dcmread(str(f), stop_before_pixels=True)
            if str(getattr(ds, "Modality", "")).upper() in ("RTSTRUCT", "STRUCT"):
                return f
        except Exception:
            pass

    # 2. Search subdirectories
    for f in plan_dir.rglob("*.dcm"):
        try:
            ds = pydicom.dcmread(str(f), stop_before_pixels=True)
            if str(getattr(ds, "Modality", "")).upper() in ("RTSTRUCT", "STRUCT"):
                return f
        except Exception:
            pass
    return None


def get_oir_plan_info(plan_id: int, db: Session) -> dict:
    """
    Scan planning CT, RTSTRUCT, fractions with CBCTs, and REG registrations.
    Returns complete metadata for the OIR UI.
    """
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if not plan:
        raise ValueError(f"Plan {plan_id} not found")

    # 1. Planning CT Volume Info
    tpct_vol, tpct_geom, tpct_meta = get_plan_tpct_volume(plan_id, db)
    num_slices = int(tpct_vol.shape[0])
    slice_thickness = float(tpct_geom.spacing_mm[2])
    z_coords = [
        round(float(tpct_geom.origin_lps[2] + i * slice_thickness), 2)
        for i in range(num_slices)
    ]

    # 2. RTSTRUCT ROIs
    rois = []
    rtstruct_path = find_plan_rtstruct(plan)
    if rtstruct_path and rtstruct_path.exists():
        try:
            ds = pydicom.dcmread(str(rtstruct_path))
            roi_names = {}
            for s in getattr(ds, "StructureSetROISequence", []):
                roi_names[int(s.ROINumber)] = str(getattr(s, "ROIName", f"ROI_{s.ROINumber}"))

            for o in getattr(ds, "ROIContourSequence", []):
                num = int(getattr(o, "ReferencedROINumber", 0))
                raw_color = getattr(o, "ROIDisplayColor", None)
                if raw_color is not None and len(raw_color) >= 3:
                    try:
                        color = [int(c) for c in raw_color[:3]]
                    except Exception:
                        color = [255, 0, 0]
                else:
                    color = [255, 0, 0]
                contours = getattr(o, "ContourSequence", [])
                zs = []
                for c in contours:
                    data = getattr(c, "ContourData", [])
                    if len(data) >= 3:
                        zs.append(float(data[2]))
                rois.append({
                    "roi_number": num,
                    "roi_name": roi_names.get(num, f"ROI_{num}"),
                    "color": color,
                    "num_contours": len(contours),
                    "z_min": min(zs) if zs else None,
                    "z_max": max(zs) if zs else None,
                })
        except Exception as exc:
            logger.warning(f"Error reading RTSTRUCT for plan {plan_id}: {exc}")

    # Sort ROIs: External first, then PTV/CTV, then alphabetical
    def roi_sort_key(r: dict) -> tuple:
        name = r["roi_name"].lower()
        if "external" in name or "body" in name:
            return (0, name)
        if "ptv" in name:
            return (1, name)
        if "ctv" in name:
            return (2, name)
        if "gtv" in name:
            return (3, name)
        return (4, name)

    rois.sort(key=roi_sort_key)

    # 3. Available Fractions with CBCT & REG data
    results_dir = resolve_results_dir(plan_id)
    sct_dir = results_dir / "synthetic_ct"
    fractions_info = []

    if sct_dir.exists():
        fx_dirs = sorted(sct_dir.glob("fx_*"), key=lambda d: int(d.name.split("_")[-1]) if d.name.split("_")[-1].isdigit() else 999)
        for fxd in fx_dirs:
            try:
                fx_num = int(fxd.name.split("_")[-1])
            except ValueError:
                continue

            cbct_raw_dir = fxd / "cbct_raw"
            reg_dir = fxd / "reg"

            cbct_series_list = []
            if cbct_raw_dir.exists():
                all_series = scan_dicom_slices([cbct_raw_dir])
                for s in all_series:
                    desc = "CBCT"
                    acq_time = ""
                    scan_date = ""
                    for_uid = ""
                    if s.first_dataset_path:
                        try:
                            ds_hdr = pydicom.dcmread(str(s.first_dataset_path), stop_before_pixels=True)
                            desc = str(getattr(ds_hdr, "SeriesDescription", desc) or desc)
                            acq_time = str(getattr(ds_hdr, "AcquisitionTime", "") or "")
                            scan_date = str(getattr(ds_hdr, "AcquisitionDate", getattr(ds_hdr, "SeriesDate", "")) or "")
                            for_uid = str(getattr(ds_hdr, "FrameOfReferenceUID", "") or "")
                        except Exception:
                            pass
                    cbct_series_list.append({
                        "series_instance_uid": s.series_instance_uid,
                        "series_description": desc,
                        "acquisition_time": acq_time,
                        "scan_date": scan_date,
                        "num_slices": len(s.slices),
                        "frame_of_reference_uid": for_uid,
                    })

            if not cbct_series_list:
                continue

            # Registrations for this fraction
            regs_list = []
            reg_files = list(reg_dir.glob("*.dcm")) if reg_dir.exists() else []

            # Parse each REG file found
            for rf in sorted(reg_files):
                try:
                    rds = pydicom.dcmread(str(rf))
                    rf_label = str(getattr(rds, "SeriesDescription", "") or getattr(rds, "ContentDescription", "") or rf.name)
                    rf_date = str(getattr(rds, "InstanceCreationDate", "") or "")
                    rf_time = str(getattr(rds, "InstanceCreationTime", "") or "")
                    reg_id = rf.name

                    # Locate 4x4 matrix
                    found_mat = None
                    target_for = ""
                    for reg_item in getattr(rds, "RegistrationSequence", []):
                        for mat_reg in getattr(reg_item, "MatrixRegistrationSequence", []):
                            for mat_item in getattr(mat_reg, "MatrixSequence", []):
                                raw_m = getattr(mat_item, "FrameOfReferenceTransformationMatrix", None)
                                if raw_m is None and (0x3006, 0x00C6) in mat_item:
                                    raw_m = mat_item[0x3006, 0x00C6].value
                                if raw_m is not None and len(raw_m) == 16:
                                    m_arr = np.array(raw_m, dtype=float).reshape((4, 4))
                                    if not np.allclose(m_arr, np.eye(4)):
                                        found_mat = m_arr
                                        target_for = str(getattr(reg_item, "FrameOfReferenceUID", ""))
                                        break
                    if found_mat is not None:
                        rx, ry, rz = euler_deg_from_matrix(found_mat[:3, :3])
                        regs_list.append({
                            "registration_id": reg_id,
                            "type": "treated",
                            "label": f"Treated Match ({rf.stem[:18]}…)" if len(rf.stem) > 20 else f"Treated Match ({rf.stem})",
                            "description": rf_label,
                            "creation_datetime": f"{rf_date} {rf_time}".strip(),
                            "shifts": {
                                "lat_x_mm": round(float(found_mat[0, 3]), 2),
                                "long_y_mm": round(float(found_mat[1, 3]), 2),
                                "vert_z_mm": round(float(found_mat[2, 3]), 2),
                                "pitch_deg": round(float(rx), 2),
                                "yaw_deg": round(float(ry), 2),
                                "roll_deg": round(float(rz), 2),
                            },
                            "target_for_uid": target_for,
                        })
                except Exception as exc:
                    logger.debug(f"Could not parse REG file {rf.name}: {exc}")

            # Initial Setup registration option:
            # If we have a treated match with matrix [R | T], the initial setup corresponds
            # to zero couch shift from the room geometry (or zero translation relative to setup).
            # We provide an explicit "Initial Setup (Unregistered)" entry:
            nominal_trans = [0.0, -70.0, -825.0]  # default room isocenter baseline
            if regs_list:
                # Base initial registration on the first treated registration without couch shifts
                first_reg = regs_list[0]
                regs_list.append({
                    "registration_id": "initial_setup",
                    "type": "initial",
                    "label": "Initial Setup (Unregistered)",
                    "description": "Patient position on table before couch shifts were applied",
                    "creation_datetime": "",
                    "shifts": {
                        "lat_x_mm": 0.0,
                        "long_y_mm": 0.0,
                        "vert_z_mm": 0.0,
                        "pitch_deg": 0.0,
                        "yaw_deg": 0.0,
                        "roll_deg": 0.0,
                    },
                    "target_for_uid": first_reg.get("target_for_uid", ""),
                })
            else:
                # Default identity / zero shift
                regs_list.append({
                    "registration_id": "initial_setup",
                    "type": "initial",
                    "label": "Initial Setup (Unregistered)",
                    "description": "Zero shift registration",
                    "creation_datetime": "",
                    "shifts": {
                        "lat_x_mm": 0.0,
                        "long_y_mm": 0.0,
                        "vert_z_mm": 0.0,
                        "pitch_deg": 0.0,
                        "yaw_deg": 0.0,
                        "roll_deg": 0.0,
                    },
                    "target_for_uid": "",
                })

            fractions_info.append({
                "fraction_number": fx_num,
                "cbct_series": cbct_series_list,
                "registrations": regs_list,
            })

    # 4. Load saved chart checks
    chart_checks = get_chart_checks(plan_id)

    return {
        "plan_id": plan_id,
        "plan_label": plan.plan_label,
        "patient_id": plan.patient_id,
        "num_slices": num_slices,
        "slice_thickness_mm": slice_thickness,
        "pixel_spacing_mm": [float(tpct_geom.spacing_mm[0]), float(tpct_geom.spacing_mm[1])],
        "origin_lps": [float(v) for v in tpct_geom.origin_lps],
        "z_coordinates": z_coords,
        "default_slice": num_slices // 2,
        "rois": rois,
        "fractions": fractions_info,
        "chart_checks": chart_checks,
    }


def get_fraction_cbct_volume(
    plan_id: int, fraction_number: int, series_instance_uid: Optional[str] = None
) -> Tuple[np.ndarray, VolumeGeometry, dict]:
    """Load or retrieve the selected CBCT volume for a fraction."""
    results_dir = resolve_results_dir(plan_id)
    cbct_dir = results_dir / "synthetic_ct" / f"fx_{fraction_number}" / "cbct_raw"
    if not cbct_dir.exists():
        raise FileNotFoundError(f"CBCT directory not found for plan {plan_id} fx {fraction_number}")

    all_series = scan_dicom_slices([cbct_dir])
    if not all_series:
        raise FileNotFoundError(f"No CBCT slices found in {cbct_dir}")

    target_series = None
    if series_instance_uid:
        for s in all_series:
            if s.series_instance_uid == series_instance_uid:
                target_series = s
                break

    if target_series is None:
        # Default to series with highest number of slices or latest acquisition
        target_series = max(all_series, key=lambda s: len(s.slices))

    cache_key = f"cbct:{plan_id}:{fraction_number}:{target_series.series_instance_uid}"
    cached = _get_from_cache(cache_key)
    if cached:
        return cached

    volume, geometry, meta = load_series_volume(target_series)
    _manage_cache(cache_key, volume, geometry, meta)
    return volume, geometry, meta


def get_oir_slice(
    plan_id: int,
    slice_idx: int,
    fraction_number: int,
    registration_id: str,
    cbct_series_uid: Optional[str],
    db: Session,
) -> dict:
    """
    Extract a single Planning CT slice and resample the corresponding CBCT slice
    using the specified registration. Also returns RTSTRUCT contour polygons on this slice.
    """
    # 1. TPCT Slice
    tpct_vol, tpct_geom, tpct_meta = get_plan_tpct_volume(plan_id, db)
    num_slices = tpct_vol.shape[0]
    slice_idx = max(0, min(num_slices - 1, slice_idx))
    tpct_slice = tpct_vol[slice_idx].astype(np.int16)

    slice_z_mm = float(tpct_geom.origin_lps[2] + slice_idx * tpct_geom.spacing_mm[2])

    # 2. CBCT Volume
    cbct_vol, cbct_geom, cbct_meta = get_fraction_cbct_volume(plan_id, fraction_number, cbct_series_uid)

    # 3. Determine Registration Matrix (maps CBCT coordinates -> TPCT coordinates)
    # P_tpct = M @ P_cbct  =>  P_cbct = M_inv @ P_tpct
    results_dir = resolve_results_dir(plan_id)
    reg_dir = results_dir / "synthetic_ct" / f"fx_{fraction_number}" / "reg"

    M = None
    shifts_out = {
        "lat_x_mm": 0.0,
        "long_y_mm": 0.0,
        "vert_z_mm": 0.0,
        "pitch_deg": 0.0,
        "yaw_deg": 0.0,
        "roll_deg": 0.0,
    }

def _get_parsed_rtstruct_contours(plan_id: int, plan: Plan) -> List[Tuple[float, int, str, List[int], np.ndarray]]:
    """Get parsed contour list [(z, roi_num, roi_name, color, pts_xy)] with file mtime checking."""
    rtstruct_path = find_plan_rtstruct(plan)
    if not rtstruct_path or not rtstruct_path.exists():
        return []
    mtime = rtstruct_path.stat().st_mtime
    if plan_id in _RTSTRUCT_CACHE and _RTSTRUCT_CACHE[plan_id][0] == mtime:
        return _RTSTRUCT_CACHE[plan_id][1]

    try:
        ds = pydicom.dcmread(str(rtstruct_path))
        roi_names = {int(s.ROINumber): str(getattr(s, "ROIName", f"ROI_{s.ROINumber}"))
                     for s in getattr(ds, "StructureSetROISequence", [])}
        parsed = []
        for o in getattr(ds, "ROIContourSequence", []):
            num = int(getattr(o, "ReferencedROINumber", 0))
            raw_color = getattr(o, "ROIDisplayColor", None)
            if raw_color is not None and len(raw_color) >= 3:
                try:
                    color = [int(c) for c in raw_color[:3]]
                except Exception:
                    color = [255, 0, 0]
            else:
                color = [255, 0, 0]
            name = roi_names.get(num, f"ROI_{num}")
            for c in getattr(o, "ContourSequence", []):
                data = getattr(c, "ContourData", [])
                if len(data) >= 3:
                    pts = np.array(data, dtype=float).reshape((-1, 3))
                    z = float(pts[0, 2])
                    parsed.append((z, num, name, color, pts[:, :2]))
        _RTSTRUCT_CACHE[plan_id] = (mtime, parsed)
        return parsed
    except Exception as exc:
        logger.warning(f"Failed to parse RTSTRUCT for plan {plan_id}: {exc}")
        return []


def _get_reg_matrix_cached(reg_file: Path) -> Tuple[Optional[np.ndarray], dict]:
    """Parse and cache 4x4 matrix and shifts from a REG DICOM file."""
    if not reg_file.exists():
        return None, {}
    fkey = str(reg_file.resolve())
    mtime = reg_file.stat().st_mtime
    if fkey in _REG_CACHE and _REG_CACHE[fkey][0] == mtime:
        return _REG_CACHE[fkey][1], _REG_CACHE[fkey][2]

    try:
        rds = pydicom.dcmread(str(reg_file))
        M = None
        shifts = {"lat_x_mm": 0.0, "long_y_mm": 0.0, "vert_z_mm": 0.0, "pitch_deg": 0.0, "yaw_deg": 0.0, "roll_deg": 0.0}
        for reg_item in getattr(rds, "RegistrationSequence", []):
            for mat_reg in getattr(reg_item, "MatrixRegistrationSequence", []):
                for mat_item in getattr(mat_reg, "MatrixSequence", []):
                    raw_m = getattr(mat_item, "FrameOfReferenceTransformationMatrix", None)
                    if raw_m is None and (0x3006, 0x00C6) in mat_item:
                        raw_m = mat_item[0x3006, 0x00C6].value
                    if raw_m is not None and len(raw_m) == 16:
                        m_arr = np.array(raw_m, dtype=float).reshape((4, 4))
                        if not np.allclose(m_arr, np.eye(4)):
                            M = m_arr
                            rx, ry, rz = euler_deg_from_matrix(M[:3, :3])
                            shifts = {
                                "lat_x_mm": round(float(M[0, 3]), 2),
                                "long_y_mm": round(float(M[1, 3]), 2),
                                "vert_z_mm": round(float(M[2, 3]), 2),
                                "pitch_deg": round(float(rx), 2),
                                "yaw_deg": round(float(ry), 2),
                                "roll_deg": round(float(rz), 2),
                            }
                            break
        _REG_CACHE[fkey] = (mtime, M, shifts)
        return M, shifts
    except Exception as exc:
        logger.warning(f"Error reading REG file {reg_file}: {exc}")
        return None, {}


def get_oir_slice(
    plan_id: int,
    slice_idx: int,
    fraction_number: int,
    registration_id: str,
    cbct_series_uid: Optional[str],
    db: Session,
) -> dict:
    """
    Extract a single Planning CT slice and resample the corresponding CBCT slice
    using the specified registration. Also returns RTSTRUCT contour polygons on this slice.
    """
    # 1. TPCT Slice
    tpct_vol, tpct_geom, tpct_meta = get_plan_tpct_volume(plan_id, db)
    num_slices = tpct_vol.shape[0]
    slice_idx = max(0, min(num_slices - 1, slice_idx))
    tpct_slice = tpct_vol[slice_idx].astype(np.int16)

    slice_z_mm = float(tpct_geom.origin_lps[2] + slice_idx * tpct_geom.spacing_mm[2])

    # 2. CBCT Volume
    cbct_vol, cbct_geom, cbct_meta = get_fraction_cbct_volume(plan_id, fraction_number, cbct_series_uid)

    # 3. Determine Registration Matrix (maps CBCT coordinates -> TPCT coordinates)
    results_dir = resolve_results_dir(plan_id)
    reg_dir = results_dir / "synthetic_ct" / f"fx_{fraction_number}" / "reg"

    M = None
    shifts_out = {
        "lat_x_mm": 0.0,
        "long_y_mm": 0.0,
        "vert_z_mm": 0.0,
        "pitch_deg": 0.0,
        "yaw_deg": 0.0,
        "roll_deg": 0.0,
    }

    if registration_id and registration_id != "initial_setup" and reg_dir.exists():
        reg_file = reg_dir / registration_id
        if not reg_file.exists():
            candidates = list(reg_dir.glob(f"*{registration_id}*"))
            if candidates:
                reg_file = candidates[0]
        if reg_file.exists():
            M, shifts_out = _get_reg_matrix_cached(reg_file)

    if M is None and registration_id != "initial_setup" and reg_dir.exists():
        # Fallback: pick first valid REG in directory
        for rf in sorted(reg_dir.glob("*.dcm")):
            M, shifts_out = _get_reg_matrix_cached(rf)
            if M is not None:
                break

    # If "initial_setup" or no matrix was found, use nominal room alignment
    if M is None or registration_id == "initial_setup":
        cbct_center_lps = [
            cbct_geom.origin_lps[i] + cbct_geom.size_voxels[i] * cbct_geom.spacing_mm[i] / 2.0
            for i in range(3)
        ]
        tpct_center_lps = [
            tpct_geom.origin_lps[i] + tpct_geom.size_voxels[i] * tpct_geom.spacing_mm[i] / 2.0
            for i in range(3)
        ]
        t_nom = [
            tpct_center_lps[0] - cbct_center_lps[0],
            tpct_center_lps[1] - cbct_center_lps[1],
            tpct_center_lps[2] - cbct_center_lps[2],
        ]
        M = np.eye(4)
        M[:3, 3] = t_nom
        shifts_out = {
            "lat_x_mm": 0.0,
            "long_y_mm": 0.0,
            "vert_z_mm": 0.0,
            "pitch_deg": 0.0,
            "yaw_deg": 0.0,
            "roll_deg": 0.0,
        }

    # 4. Resample CBCT onto TPCT slice plane
    M_inv = np.linalg.inv(M)

    h, w = tpct_slice.shape
    x_coords = tpct_geom.origin_lps[0] + np.arange(w) * tpct_geom.spacing_mm[0]
    y_coords = tpct_geom.origin_lps[1] + np.arange(h) * tpct_geom.spacing_mm[1]
    X, Y = np.meshgrid(x_coords, y_coords)
    Z = np.full_like(X, slice_z_mm)

    pts_tpct = np.column_stack([X.ravel(), Y.ravel(), Z.ravel(), np.ones(X.size)])
    pts_cbct = (M_inv @ pts_tpct.T).T

    vox_j = (pts_cbct[:, 0] - cbct_geom.origin_lps[0]) / cbct_geom.spacing_mm[0]
    vox_i = (pts_cbct[:, 1] - cbct_geom.origin_lps[1]) / cbct_geom.spacing_mm[1]
    vox_k = (pts_cbct[:, 2] - cbct_geom.origin_lps[2]) / cbct_geom.spacing_mm[2]

    coords = np.vstack([vox_k, vox_i, vox_j])
    cbct_resampled = map_coordinates(
        cbct_vol, coords, order=1, mode="constant", cval=-1000.0
    ).reshape(h, w)
    cbct_slice = np.clip(cbct_resampled, -1024, 3071).astype(np.int16)

    # 5. Extract RTSTRUCT Contours for this slice (instantaneous from cache)
    contours_list = []
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if plan:
        parsed_contours = _get_parsed_rtstruct_contours(plan_id, plan)
        half_slice = float(tpct_geom.spacing_mm[2]) / 2.0
        ox = float(tpct_geom.origin_lps[0])
        oy = float(tpct_geom.origin_lps[1])
        sx = float(tpct_geom.spacing_mm[0])
        sy = float(tpct_geom.spacing_mm[1])

        for c_z, num, name, color, pts_xy in parsed_contours:
            if abs(c_z - slice_z_mm) <= half_slice:
                px = (pts_xy[:, 0] - ox) / sx
                py = (pts_xy[:, 1] - oy) / sy
                step = max(1, len(pts_xy) // 150)
                sampled_pts = [[round(float(px[i]), 1), round(float(py[i]), 1)] for i in range(0, len(pts_xy), step)]
                contours_list.append({
                    "roi_number": num,
                    "roi_name": name,
                    "color": color,
                    "points": sampled_pts,
                })

    # 6. Base64 Encode Int16 HU Arrays
    tpct_b64 = base64.b64encode(tpct_slice.tobytes()).decode("ascii")
    cbct_b64 = base64.b64encode(cbct_slice.tobytes()).decode("ascii")

    return {
        "slice_index": slice_idx,
        "total_slices": num_slices,
        "slice_z_mm": round(slice_z_mm, 2),
        "tpct_b64": tpct_b64,
        "cbct_b64": cbct_b64,
        "dimensions": [h, w],
        "tpct_range": [int(tpct_slice.min()), int(tpct_slice.max())],
        "cbct_range": [int(cbct_slice.min()), int(cbct_slice.max())],
        "registration": {
            "id": registration_id,
            "shifts": shifts_out,
        },
        "contours": contours_list,
    }


def _chart_check_file(plan_id: int) -> Path:
    """Path to the persisted chart check reviews JSON file for a plan."""
    results_dir = resolve_results_dir(plan_id)
    results_dir.mkdir(parents=True, exist_ok=True)
    return results_dir / "oir_chart_checks.json"


def get_chart_checks(plan_id: int) -> list[dict]:
    """Load all chart check records for a plan."""
    fpath = _chart_check_file(plan_id)
    if fpath.exists():
        try:
            return json.loads(fpath.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"Could not read chart check records from {fpath}: {exc}")
    return []


def save_chart_check(
    plan_id: int,
    fraction_number: int,
    reviewer_name: str,
    status: str,
    notes: str,
    shifts_verified: bool = True,
    contours_verified: bool = True,
) -> dict:
    """Record a physicist chart check review for a fraction."""
    fpath = _chart_check_file(plan_id)
    records = get_chart_checks(plan_id)

    new_record = {
        "id": f"cc_{int(datetime.now(timezone.utc).timestamp())}",
        "fraction_number": fraction_number,
        "reviewer_name": reviewer_name.strip() or "Medical Physicist",
        "status": status,  # "pass", "acceptable", "flagged"
        "notes": notes.strip(),
        "shifts_verified": shifts_verified,
        "contours_verified": contours_verified,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # Upsert or prepend
    existing_idx = next(
        (i for i, r in enumerate(records) if r.get("fraction_number") == fraction_number),
        None,
    )
    if existing_idx is not None:
        records[existing_idx] = new_record
    else:
        records.insert(0, new_record)

    fpath.write_text(json.dumps(records, indent=2), encoding="utf-8")
    return new_record
