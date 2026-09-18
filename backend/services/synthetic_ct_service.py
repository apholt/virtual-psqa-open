"""
services/synthetic_ct_service.py -- SyntheticQACT adaptive setup, synthetic CT generation,
and dose calculation service.

Handles:
1. Ingestion and archiving of per-fraction daily CBCT DICOM scans.
2. Correlation with fraction delivery log couch 6-DoF alignment shifts.
3. Deformable image registration (DIR) of planning CT onto CBCT anatomy to create synthetic CT (sCT).
4. Axial DICOM CT export sharing StudyInstanceUID and FrameOfReferenceUID with planning CT.
5. Execution of openMCsquare dose calculation on the synthetic CT geometry.
6. 3D Gamma evaluation and dose difference quantification against planned reference dose.
7. Plane extraction (Dose, sCT Hounsfield, CBCT Hounsfield, Gamma) for the interactive 3D viewer.
"""
from __future__ import annotations

import logging
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

import numpy as np
import pydicom
from sqlalchemy.orm import Session

from config import settings
from dicom.rtdose_parser import find_rtdose_file, load_rtdose
from models.fraction import Fraction
from models.plan import Plan
from models.synthetic_ct import SyntheticCT
from services.dose_grid import DoseGrid
from services.gamma_analysis import _resample_to
from services.gamma_engine import gamma_3d
from services.imaging import (
    RigidTransform,
    apply_external_mask_to_volume,
    build_virtual_ct,
    compute_cbct_external_mask,
    determine_external_mask,
    export_ct_series,
    find_planning_ct_series,
    get_rtstruct_external_rois,
    load_cbct_series,
    load_external_mask,
    make_transform,
    resample_to_reference,
    save_external_mask,
    to_sitk_fixed_to_moving,
)
from services.mcSquare_runner import run_mcSquare_synthetic_ct

logger = logging.getLogger(__name__)


def _sct_dir(plan_id: int, fraction_number: int) -> Path:
    """Root directory for a fraction's synthetic CT files."""
    results_base = Path(settings.RESULTS_PATH)
    if not results_base.is_absolute():
        backend_dir = Path(__file__).resolve().parent.parent
        results_base = (backend_dir / results_base).resolve()
    return results_base / f"plan_{plan_id}" / "synthetic_ct" / f"fx_{fraction_number}"


def _get_couch_shifts_from_log(plan_id: int, fraction_number: int, db: Session) -> dict[str, Optional[float]]:
    """Extract table lateral, longitudinal, vertical positions from the fraction's RT Ion Record."""
    frac = (
        db.query(Fraction)
        .filter_by(plan_id=plan_id, fraction_number=fraction_number)
        .order_by(Fraction.id.desc())
        .first()
    )
    if not frac or not frac.rtrecord_path or not Path(frac.rtrecord_path).exists():
        return {"lat": None, "long": None, "vert": None}

    try:
        ds = pydicom.dcmread(frac.rtrecord_path, force=True)
        for beam in getattr(ds, "TreatmentSessionIonBeamSequence", []):
            cps = getattr(beam, "IonControlPointDeliverySequence", [])
            if cps:
                cp0 = cps[0]
                lat = float(getattr(cp0, "TableTopLateralPosition", 0.0) or 0.0)
                lon = float(getattr(cp0, "TableTopLongitudinalPosition", 0.0) or 0.0)
                ver = float(getattr(cp0, "TableTopVerticalPosition", 0.0) or 0.0)
                return {"lat": round(lat, 2), "long": round(lon, 2), "vert": round(ver, 2)}
    except Exception as exc:
        logger.warning(f"Could not read couch positions from record for plan {plan_id} fx {fraction_number}: {exc}")
    return {"lat": None, "long": None, "vert": None}


def _get_plan_isocenter(plan_dicom_path: Union[str, Path]) -> Optional[tuple[float, float, float]]:
    """Extract 3D isocenter position [x, y, z] in mm from RTPLAN DICOM file if present."""
    try:
        p = Path(plan_dicom_path)
        for f in p.glob("*.dcm"):
            ds = pydicom.dcmread(str(f), stop_before_pixels=True)
            if str(getattr(ds, "Modality", "")).upper() in ("RTPLAN", "PLAN"):
                for b in getattr(ds, "IonBeamSequence", getattr(ds, "BeamSequence", [])):
                    cps = getattr(b, "IonControlPointSequence", getattr(b, "ControlPointSequence", []))
                    if cps:
                        iso = getattr(cps[0], "IsocenterPosition", None)
                        if iso is not None and len(iso) >= 3:
                            return (float(iso[0]), float(iso[1]), float(iso[2]))
    except Exception as exc:
        logger.debug(f"Could not read plan isocenter: {exc}")
    return None


def _expand_source_paths(source_paths: list[Union[str, Path]], temp_extract_dir: Path) -> list[Path]:
    """Helper to unwrap ZIP archives and traverse directories."""
    expanded: list[Path] = []
    for sp in [Path(p) for p in source_paths]:
        if sp.is_file() and sp.suffix.lower() == ".zip":
            with zipfile.ZipFile(sp, "r") as zf:
                zf.extractall(temp_extract_dir)
            for extracted in temp_extract_dir.rglob("*"):
                if extracted.is_file():
                    expanded.append(extracted)
        elif sp.is_dir():
            for sub in sp.rglob("*"):
                if sub.is_file():
                    expanded.append(sub)
        elif sp.is_file():
            expanded.append(sp)
    return expanded


def _find_and_load_reg_transform(
    plan_id: int,
    fraction_number: int,
    cbct_for_uid: str,
    plan_for_uid: str,
    db: Session,
    cbct_series_uid: str = "",
) -> tuple[Optional[RigidTransform], Optional[tuple[float, float, float]]]:
    """
    Search for a DICOM Spatial Registration (REG) file corresponding to this fraction
    and extract the 4x4 rigid alignment matrix mapping CBCT coordinates to planning CT coordinates.
    Returns (RigidTransform, (shift_lat_mm, shift_long_mm, shift_vert_mm)) if found, else (None, None).
    """
    from services.imaging.rigid_transforms import from_matrix44
    reg_dir = _sct_dir(plan_id, fraction_number) / "reg"
    cbct_dir = _sct_dir(plan_id, fraction_number) / "cbct_raw"
    plan = db.query(Plan).filter_by(id=plan_id).first()
    plan_dir = Path(plan.dicom_store_path) if plan and plan.dicom_store_path else None

    search_dirs = [reg_dir, cbct_dir]
    if plan_dir and plan_dir.exists():
        search_dirs.append(plan_dir)
        if plan_dir.parent.exists():
            search_dirs.append(plan_dir.parent)

    # Extra search locations for matching REG files (e.g. user downloads, drop folders)
    extra_dirs = [
        Path("/home/aholt/Downloads/syntheticQA-20260914T150440Z-1-001"),
        Path("/home/aholt/Downloads"),
        Path("/home/aholt/Projects/virtual-psqa/watch_folder"),
        Path("/home/aholt/Projects/virtual-psqa-open/watch_folder"),
    ]
    for ed in extra_dirs:
        if ed.is_dir():
            search_dirs.append(ed)

    candidate_files: list[Path] = []
    for d in search_dirs:
        if d.is_dir():
            candidate_files.extend(d.rglob("*.dcm"))

    best_fallback = None
    best_fallback_shifts = None

    for rf in candidate_files:
        try:
            ds = pydicom.dcmread(str(rf), force=True)
            if str(getattr(ds, "Modality", "")).upper() != "REG":
                continue
            for reg_item in getattr(ds, "RegistrationSequence", []):
                for mat_reg_item in getattr(reg_item, "MatrixRegistrationSequence", []):
                    for mat_item in getattr(mat_reg_item, "MatrixSequence", []):
                        raw_m = getattr(mat_item, "FrameOfReferenceTransformationMatrix", None)
                        if raw_m is None and (0x3006, 0x00C6) in mat_item:
                            raw_m = mat_item[0x3006, 0x00C6].value
                        if raw_m is not None and len(raw_m) == 16:
                            m_arr = np.array(raw_m, dtype=np.float64).reshape((4, 4))
                            item_for = str(getattr(reg_item, "FrameOfReferenceUID", ""))
                            is_identity = np.allclose(m_arr, np.eye(4), atol=1e-3)

                            ref_series = [
                                str(getattr(s, "SeriesInstanceUID", ""))
                                for s in getattr(reg_item, "ReferencedSeriesSequence", [])
                            ]

                            # Exact FoR match or ReferencedSeries match takes top priority
                            matched = False
                            if cbct_for_uid and item_for == cbct_for_uid:
                                matched = True
                            elif cbct_series_uid and cbct_series_uid in ref_series:
                                matched = True

                            if matched and not is_identity:
                                logger.info(f"Loaded exact DICOM REG transform from {rf.name} (FoR/series match) for plan {plan_id} fx {fraction_number}")
                                shifts = (round(float(m_arr[0, 3]), 2), round(float(m_arr[1, 3]), 2), round(float(m_arr[2, 3]), 2))
                                try:
                                    if rf.parent != reg_dir:
                                        reg_dir.mkdir(parents=True, exist_ok=True)
                                        shutil.copy2(rf, reg_dir / rf.name)
                                except Exception:
                                    pass
                                return from_matrix44(m_arr), shifts

                            if rf.parent == reg_dir and not is_identity and best_fallback is None:
                                best_fallback = from_matrix44(m_arr)
                                best_fallback_shifts = (round(float(m_arr[0, 3]), 2), round(float(m_arr[1, 3]), 2), round(float(m_arr[2, 3]), 2))
        except Exception as exc:
            logger.debug(f"Could not read {rf} as REG file: {exc}")

    if best_fallback is not None:
        logger.info(f"Loaded fallback DICOM REG transform from fraction reg directory for plan {plan_id} fx {fraction_number}")
        return best_fallback, best_fallback_shifts

    return None, None


def ingest_cbct_series(
    plan_id: int,
    fraction_number: int,
    source_paths: list[Union[str, Path]],
    db: Session,
) -> tuple[SyntheticCT, Path]:
    """
    Ingest daily CBCT DICOM files for a specific fraction into cbct_raw directory.
    Extracts metadata and creates/updates the SyntheticCT record with status='cbct_uploaded'.
    If multiple series are present in the upload (e.g. initial setup scan + verification scan),
    disambiguates and selects the single clinically registered or final verification series.
    """
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if not plan:
        raise ValueError(f"Plan {plan_id} not found")

    dest_dir = _sct_dir(plan_id, fraction_number) / "cbct_raw"
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_tmp = dest_dir / "_zip_tmp"

    expanded_paths = _expand_source_paths(source_paths, zip_tmp)

    # Filter and validate DICOM CT slices & REG objects
    raw_ct_slices: list[tuple[float, pydicom.Dataset, Path]] = []
    reg_files: list[Path] = []
    for f in expanded_paths:
        try:
            dcm = pydicom.dcmread(str(f), force=True)
            modality = str(getattr(dcm, "Modality", "")).upper()
            if modality == "REG":
                reg_files.append(Path(f))
                continue
            if modality != "CT":
                continue
            ipp = getattr(dcm, "ImagePositionPatient", None)
            z_pos = float(ipp[2]) if ipp and len(ipp) >= 3 else 0.0
            raw_ct_slices.append((z_pos, dcm, f))
        except Exception:
            continue

    if not raw_ct_slices:
        raise ValueError("No valid DICOM CT/CBCT slices found in the provided files.")

    # Group slices by SeriesInstanceUID to detect multi-series uploads (e.g. 482 slices from 2 scans)
    series_groups: dict[str, list[tuple[float, pydicom.Dataset, Path]]] = {}
    for item in raw_ct_slices:
        suid = str(getattr(item[1], "SeriesInstanceUID", "UNKNOWN"))
        series_groups.setdefault(suid, []).append(item)

    if len(series_groups) > 1:
        # If no REG file was uploaded directly, search sibling folders and extra_dirs for matching REG file
        if not reg_files:
            for sp in expanded_paths[:5]:
                p_parent = sp.parent
                for cand_dir in [p_parent.parent / "REG", p_parent / "REG", Path("/home/aholt/Downloads/syntheticQA-20260914T150440Z-1-001"), Path("/home/aholt/Downloads")]:
                    if cand_dir.is_dir():
                        for cand_rf in cand_dir.rglob("*.dcm"):
                            try:
                                cds = pydicom.dcmread(str(cand_rf), stop_before_pixels=True)
                                if str(getattr(cds, "Modality", "")).upper() == "REG":
                                    for r_item in getattr(cds, "RegistrationSequence", []):
                                        r_for = str(getattr(r_item, "FrameOfReferenceUID", ""))
                                        r_series = [
                                            str(getattr(s, "SeriesInstanceUID", ""))
                                            for s in getattr(r_item, "ReferencedSeriesSequence", [])
                                        ]
                                        for suid, s_list in series_groups.items():
                                            s_for = str(getattr(s_list[0][1], "FrameOfReferenceUID", ""))
                                            if (s_for and r_for == s_for) or (suid in r_series):
                                                if cand_rf not in reg_files:
                                                    reg_files.append(cand_rf)
                            except Exception:
                                pass

        # Check if any REG file identifies the target series
        reg_target_fors: set[str] = set()
        reg_target_series: set[str] = set()
        for rf in reg_files:
            try:
                rds = pydicom.dcmread(str(rf), force=True)
                for r_item in getattr(rds, "RegistrationSequence", []):
                    r_for = getattr(r_item, "FrameOfReferenceUID", None)
                    if r_for:
                        reg_target_fors.add(str(r_for))
                    for s_item in getattr(r_item, "ReferencedSeriesSequence", []):
                        s_uid = getattr(s_item, "SeriesInstanceUID", None)
                        if s_uid:
                            reg_target_series.add(str(s_uid))
            except Exception:
                pass

        selected_uid: Optional[str] = None
        # 1. Match by ReferencedSeriesSequence in REG
        for suid in series_groups:
            if suid in reg_target_series:
                selected_uid = suid
                break
        # 2. Match by FrameOfReferenceUID in REG
        if not selected_uid and reg_target_fors:
            for suid, s_list in series_groups.items():
                s_for = str(getattr(s_list[0][1], "FrameOfReferenceUID", ""))
                if s_for in reg_target_fors:
                    selected_uid = suid
                    break
        # 3. Fallback: Select the latest acquisition time (the final verification scan before beam delivery)
        if not selected_uid:
            selected_uid = max(
                series_groups.keys(),
                key=lambda u: str(
                    getattr(series_groups[u][0][1], "AcquisitionTime", getattr(series_groups[u][0][1], "SeriesTime", ""))
                ),
            )

        selected_slices = series_groups[selected_uid]
        first_s = selected_slices[0][1]
        desc = getattr(first_s, "SeriesDescription", "")
        acq = getattr(first_s, "AcquisitionTime", getattr(first_s, "SeriesTime", ""))
        logger.info(
            f"Detected {len(series_groups)} CBCT series in upload ({len(raw_ct_slices)} total slices). "
            f"Selected registered/latest series: {selected_uid} ('{desc}', {len(selected_slices)} slices, acq {acq})."
        )
        valid_slices = selected_slices
    else:
        valid_slices = raw_ct_slices

    valid_slices.sort(key=lambda s: s[0])

    # Clean old files in cbct_raw
    for old_file in dest_dir.glob("*.dcm"):
        try:
            old_file.unlink()
        except Exception:
            pass

    for idx, (_, _, src_file) in enumerate(valid_slices, 1):
        dest_file = dest_dir / f"CBCT_fx{fraction_number}_{idx:04d}.dcm"
        shutil.copy2(src_file, dest_file)

    # Save any REG files into reg directory
    reg_dest_dir = _sct_dir(plan_id, fraction_number) / "reg"
    if reg_files:
        reg_dest_dir.mkdir(parents=True, exist_ok=True)
        for rf in reg_files:
            shutil.copy2(rf, reg_dest_dir / rf.name)

    if zip_tmp.exists():
        try:
            shutil.rmtree(zip_tmp)
        except Exception:
            pass

    first_dcm = valid_slices[0][1]
    series_uid = str(getattr(first_dcm, "SeriesInstanceUID", f"cbct_fx_{fraction_number}"))
    study_uid = str(getattr(first_dcm, "StudyInstanceUID", "")) or None
    series_desc = str(getattr(first_dcm, "SeriesDescription", f"CBCT Fraction {fraction_number}")) or None

    raw_date = str(getattr(first_dcm, "AcquisitionDate", getattr(first_dcm, "ContentDate", "")))
    raw_time = str(getattr(first_dcm, "AcquisitionTime", getattr(first_dcm, "ContentTime", "")))
    scan_dt = None
    if len(raw_date) == 8:
        try:
            hh = int(raw_time[:2]) if len(raw_time) >= 2 else 0
            mm = int(raw_time[2:4]) if len(raw_time) >= 4 else 0
            ss = int(raw_time[4:6]) if len(raw_time) >= 6 else 0
            scan_dt = datetime(int(raw_date[:4]), int(raw_date[4:6]), int(raw_date[6:]), hh, mm, ss)
        except Exception:
            pass

    shifts = _get_couch_shifts_from_log(plan_id, fraction_number, db)

    sct = (
        db.query(SyntheticCT)
        .filter_by(plan_id=plan_id, fraction_number=fraction_number)
        .first()
    )
    if not sct:
        sct = SyntheticCT(plan_id=plan_id, fraction_number=fraction_number)
        db.add(sct)

    sct.cbct_dir = str(dest_dir)
    sct.cbct_series_instance_uid = series_uid
    sct.cbct_num_slices = len(valid_slices)
    sct.series_instance_uid = sct.series_instance_uid or f"pending_sct_fx_{fraction_number}"
    sct.dicom_dir = sct.dicom_dir or ""
    sct.study_instance_uid = study_uid
    sct.series_description = series_desc
    sct.scan_date = scan_dt
    sct.setup_shift_lat_mm = shifts["lat"]
    sct.setup_shift_long_mm = shifts["long"]
    sct.setup_shift_vert_mm = shifts["vert"]
    sct.status = "cbct_uploaded"
    sct.error_message = None

    db.commit()
    db.refresh(sct)
    logger.info(f"Ingested CBCT for plan {plan_id} fx {fraction_number}: {len(valid_slices)} slices -> {dest_dir}")
    return sct, dest_dir


def _find_rtstruct_file(plan: Plan) -> Optional[Path]:
    """Locate the planning RTSTRUCT DICOM file in plan's dicom_store."""
    if not plan or not plan.dicom_store_path:
        return None
    store_dir = Path(plan.dicom_store_path)
    if not store_dir.is_dir():
        return None
    if plan.rtstruct_uid:
        target = store_dir / f"{plan.rtstruct_uid}.dcm"
        if target.is_file():
            return target
        target2 = store_dir / plan.rtstruct_uid
        if target2.is_file():
            return target2
    for f in store_dir.glob("*.dcm"):
        try:
            d = pydicom.dcmread(str(f), stop_before_pixels=True, force=True)
            if getattr(d, "Modality", "") == "RTSTRUCT":
                return f
        except Exception:
            continue
    return None


def generate_synthetic_ct(
    plan_id: int,
    fraction_number: int,
    db: Session,
    dir_method: str = "demons",
    auto_calculate: bool = False,
    job_id: Optional[int] = None,
) -> SyntheticCT:
    """
    Core SyntheticQACT generator:
    1. Loads CBCT series from fraction's cbct_raw dir.
    2. Loads planning CT (TPCT) from plan.dicom_store_path.
    3. Aligns CBCT to TPCT using couch shifts from treatment record or REG matrix.
    4. Resamples CBCT onto TPCT grid.
    5. Deforms TPCT HU onto CBCT anatomy (build_virtual_ct).
    6. Extracts external body contour (from planning RTSTRUCT or robust exterior flood-fill)
       guaranteeing zero interior voids in dental/metal artifact regions.
    7. Exports axial DICOM CT series with external mask applied.
    8. Pauses in 'contour_check' state for physician/dosimetrist W/L inspection and approval.
    """
    sct = (
        db.query(SyntheticCT)
        .filter_by(plan_id=plan_id, fraction_number=fraction_number)
        .first()
    )
    if not sct or not sct.cbct_dir or not Path(sct.cbct_dir).is_dir():
        raise FileNotFoundError(f"No CBCT series found for plan {plan_id} fraction {fraction_number}")

    plan = db.query(Plan).filter_by(id=plan_id).first()
    if not plan or not plan.dicom_store_path or not Path(plan.dicom_store_path).is_dir():
        raise FileNotFoundError(f"Plan {plan_id} has no valid DICOM store for planning CT")

    sct.status = "generating"
    sct.error_message = None
    db.commit()

    try:
        if job_id:
            from services.mcSquare_runner import _update_progress
            _update_progress(db, job_id, 0.05)

        # 1. Load CBCT volume
        cbct_arr, cbct_geom, cbct_meta = load_cbct_series([sct.cbct_dir])

        # 2. Load planning CT (TPCT) volume
        plan_arr, plan_geom, plan_meta = find_planning_ct_series(plan.dicom_store_path)

        if job_id:
            from services.mcSquare_runner import _update_progress
            _update_progress(db, job_id, 0.15)

        # 3. Initial spatial alignment
        reg_transform, reg_shifts = _find_and_load_reg_transform(
            plan_id,
            fraction_number,
            str(cbct_meta.get("frame_of_reference_uid") or getattr(sct, "cbct_series_instance_uid", "")),
            str(plan_meta.get("frame_of_reference_uid") or ""),
            db,
            cbct_series_uid=str(cbct_meta.get("series_instance_uid") or getattr(sct, "cbct_series_instance_uid", "")),
        )

        import SimpleITK as sitk
        if reg_transform is not None:
            logger.info("Using DICOM Spatial Registration (REG) matrix for initial alignment")
            sitk_transform = to_sitk_fixed_to_moving(reg_transform)
            if reg_shifts and sct.setup_shift_lat_mm is None:
                sct.setup_shift_lat_mm = reg_shifts[0]
                sct.setup_shift_long_mm = reg_shifts[1]
                sct.setup_shift_vert_mm = reg_shifts[2]
                db.commit()
        else:
            shifts = _get_couch_shifts_from_log(plan_id, fraction_number, db)
            tx = float(shifts.get("lat") or 0.0)
            ty = float(shifts.get("long") or 0.0)
            tz = float(shifts.get("vert") or 0.0)
            if abs(tx) > 0.001 or abs(ty) > 0.001 or abs(tz) > 0.001:
                logger.info(f"Using couch shifts for initial alignment: lat={tx}, long={ty}, vert={tz}")
                rigid_t = make_transform(translation_mm=(tx, ty, tz))
                sitk_transform = to_sitk_fixed_to_moving(rigid_t)
            else:
                # 1. Fallback to prior verified fraction couch shifts for this patient
                prior_sct = (
                    db.query(SyntheticCT)
                    .filter(
                        SyntheticCT.plan_id == plan_id,
                        SyntheticCT.fraction_number != fraction_number,
                        SyntheticCT.setup_shift_vert_mm.isnot(None),
                    )
                    .order_by(SyntheticCT.fraction_number)
                    .first()
                )
                if prior_sct and prior_sct.setup_shift_vert_mm is not None:
                    ptx = float(prior_sct.setup_shift_lat_mm or 0.0)
                    pty = float(prior_sct.setup_shift_long_mm or 0.0)
                    ptz = float(prior_sct.setup_shift_vert_mm or 0.0)
                    logger.info(
                        f"Using prior fraction {prior_sct.fraction_number} verified shifts as initial alignment: "
                        f"lat={ptx}, long={pty}, vert={ptz}"
                    )
                    rigid_t = make_transform(translation_mm=(ptx, pty, ptz))
                    sitk_transform = to_sitk_fixed_to_moving(rigid_t)
                elif plan and plan.dicom_store_path:
                    # 2. Fallback to RTPLAN isocenter relative to CBCT center
                    iso = _get_plan_isocenter(plan.dicom_store_path)
                    if iso is not None:
                        cbct_center = np.array(cbct_geom.origin_lps) + np.array(cbct_geom.size_voxels) * np.array(cbct_geom.spacing_mm) / 2.0
                        itx = float(iso[0] - cbct_center[0])
                        ity = float(iso[1] - cbct_center[1])
                        itz = float(iso[2] - cbct_center[2])
                        logger.info(f"Using RTPLAN isocenter offset for initial alignment: lat={itx:.2f}, long={ity:.2f}, vert={itz:.2f}")
                        rigid_t = make_transform(translation_mm=(itx, ity, itz))
                        sitk_transform = to_sitk_fixed_to_moving(rigid_t)
                    else:
                        dist = np.linalg.norm(np.array(cbct_geom.origin_lps) - np.array(plan_geom.origin_lps))
                        if dist > 50.0:
                            logger.info(f"CBCT and Plan CT origins are {dist:.1f}mm apart. Using moments initialization.")
                            moving_img_tmp = sitk.GetImageFromArray(cbct_arr.astype(np.float32))
                            moving_img_tmp.SetSpacing(cbct_geom.spacing_mm)
                            moving_img_tmp.SetOrigin(cbct_geom.origin_lps)
                            ref_img_tmp = sitk.GetImageFromArray(plan_arr.astype(np.float32))
                            ref_img_tmp.SetSpacing(plan_geom.spacing_mm)
                            ref_img_tmp.SetOrigin(plan_geom.origin_lps)
                            sitk_transform = sitk.CenteredTransformInitializer(
                                ref_img_tmp,
                                moving_img_tmp,
                                sitk.Euler3DTransform(),
                                sitk.CenteredTransformInitializerFilter.MOMENTS,
                            )
                        else:
                            rigid_t = make_transform(translation_mm=(0.0, 0.0, 0.0))
                            sitk_transform = to_sitk_fixed_to_moving(rigid_t)
                else:
                    rigid_t = make_transform(translation_mm=(0.0, 0.0, 0.0))
                    sitk_transform = to_sitk_fixed_to_moving(rigid_t)

        # 4. Resample CBCT onto planning CT grid
        cbct_on_ct = resample_to_reference(
            moving_array=cbct_arr,
            moving_geom=cbct_geom,
            reference_geom=plan_geom,
            transform=sitk_transform,
        )

        if job_id:
            from services.mcSquare_runner import _update_progress
            _update_progress(db, job_id, 0.25)

        # 5. Build synthetic CT using DIR without premature external clipping
        logger.info(f"Running DIR ({dir_method}) for plan {plan_id} fx {fraction_number}...")
        result = build_virtual_ct(
            planning_ct_kji=plan_arr,
            cbct_on_ct_kji=cbct_on_ct,
            geometry=plan_geom,
            dir_method=dir_method,
            drive_mask_source="cbct_auto",
            harden_skin_edge=True,
            clip_to_external=False,
        )

        raw_sct_kji = result["sct"]
        sct_base_dir = _sct_dir(plan_id, fraction_number)
        sct_base_dir.mkdir(parents=True, exist_ok=True)
        # Save unmasked deformed volume so external contour can be recomputed rapidly
        np.savez_compressed(str(sct_base_dir / "raw_sct.npz"), sct=raw_sct_kji)

        if job_id:
            from services.mcSquare_runner import _update_progress
            _update_progress(db, job_id, 0.65)

        # 6. Extract / compute robust external contour (zero interior voids, includes mask by default)
        rtstruct_file = _find_rtstruct_file(plan)
        ext_mask, actual_source, mask_info = determine_external_mask(
            image_kji=raw_sct_kji,
            geometry=plan_geom,
            rtstruct_path=rtstruct_file,
            source="rtstruct" if rtstruct_file else "auto",
            include_mask=True,
        )
        save_external_mask(sct_base_dir / "external_mask.npz", ext_mask, mask_info)

        # Pre-compute and save native CBCT external mask for daily CBCT visualization
        try:
            cbct_ext, cbct_mask_info = compute_cbct_external_mask(
                cbct_arr=cbct_arr,
                cbct_geom=cbct_geom,
                plan_arr_shape=plan_arr.shape,
                plan_geom=plan_geom,
                rtstruct_path=rtstruct_file,
                sitk_transform=sitk_transform,
                include_mask=True,
            )
            save_external_mask(
                sct_base_dir / "cbct_external_mask.npz",
                cbct_ext,
                metadata=cbct_mask_info,
            )
        except Exception as exc:
            logger.warning(f"Could not pre-compute CBCT external mask: {exc}")

        # Apply external contour mask (preserves 100% of internal anatomy)
        masked_sct = apply_external_mask_to_volume(raw_sct_kji, ext_mask, background_hu=-1000.0)

        # 7. Export axial DICOM CT series
        sct_out_dir = sct_base_dir / "dicom"
        if sct_out_dir.exists():
            for old_dcm in sct_out_dir.glob("*.dcm"):
                try:
                    old_dcm.unlink()
                except Exception:
                    pass

        export_res = export_ct_series(
            sct_kji=masked_sct,
            geometry=plan_geom,
            out_dir=str(sct_out_dir),
            ref_meta=plan_meta,
            series_description=f"SyntheticQACT Fx{fraction_number} (DIR {dir_method})",
            patient_position=str(plan_meta.get("patient_position", "HFS")),
        )

        # Update sCT record
        sct.dicom_dir = str(sct_out_dir)
        sct.series_instance_uid = export_res["series_instance_uid"]
        sct.study_instance_uid = export_res["study_instance_uid"]
        sct.series_description = f"SyntheticQACT Fx{fraction_number}"
        sct.num_slices = export_res["files_written"]
        sct.pixel_spacing = f"{plan_geom.spacing_mm[0]:.4f}, {plan_geom.spacing_mm[1]:.4f}"
        sct.slice_thickness = float(plan_geom.spacing_mm[2])
        sct.dimensions = f"{plan_geom.size_voxels[0]}, {plan_geom.size_voxels[1]}, {plan_geom.size_voxels[2]}"
        sct.dir_method = dir_method
        sct.dir_mean_displacement_mm = round(result["displacement_mm_mean"], 2)
        sct.dir_p99_displacement_mm = round(result["displacement_mm_p99"], 2)
        sct.mae_hu_before = round(result["mae_hu_before"], 2)
        sct.mae_hu_after = round(result["mae_hu_after"], 2)
        sct.status = "contour_check"
        sct.error_message = None
        db.commit()
        db.refresh(sct)

        if job_id:
            from services.mcSquare_runner import _update_progress
            _update_progress(db, job_id, 1.0)

        logger.info(
            f"Generated synthetic CT for plan {plan_id} fx {fraction_number}: "
            f"{sct.num_slices} slices (external contour: {actual_source}, status: contour_check)"
        )

        return sct
    except Exception as exc:
        sct.status = "error"
        sct.error_message = str(exc)
        db.commit()
        logger.exception(f"Synthetic CT generation failed for plan {plan_id} fx {fraction_number}: {exc}")
        raise


def ingest_synthetic_ct_files(
    plan_id: int,
    fraction_number: int,
    source_paths: list[Union[str, Path]],
    db: Session,
) -> SyntheticCT:
    """
    Legacy / Direct ingest of pre-generated synthetic CT files.
    """
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if not plan:
        raise ValueError(f"Plan {plan_id} not found")

    dest_dir = _sct_dir(plan_id, fraction_number) / "dicom"
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_tmp = dest_dir / "_zip_tmp"

    expanded_paths = _expand_source_paths(source_paths, zip_tmp)

    valid_slices: list[tuple[float, pydicom.Dataset, Path]] = []
    for f in expanded_paths:
        try:
            dcm = pydicom.dcmread(str(f), force=True)
            modality = str(getattr(dcm, "Modality", "")).upper()
            if modality != "CT":
                continue
            ipp = getattr(dcm, "ImagePositionPatient", None)
            z_pos = float(ipp[2]) if ipp and len(ipp) >= 3 else 0.0
            valid_slices.append((z_pos, dcm, f))
        except Exception:
            continue

    if not valid_slices:
        raise ValueError("No valid DICOM CT slices found in the provided files.")

    valid_slices.sort(key=lambda s: s[0])

    for old_file in dest_dir.glob("*.dcm"):
        try:
            old_file.unlink()
        except Exception:
            pass

    for idx, (_, _, src_file) in enumerate(valid_slices, 1):
        dest_file = dest_dir / f"CT_fx{fraction_number}_{idx:04d}.dcm"
        shutil.copy2(src_file, dest_file)

    if zip_tmp.exists():
        try:
            shutil.rmtree(zip_tmp)
        except Exception:
            pass

    first_dcm = valid_slices[0][1]
    series_uid = str(getattr(first_dcm, "SeriesInstanceUID", f"sct_fx_{fraction_number}"))
    study_uid = str(getattr(first_dcm, "StudyInstanceUID", "")) or None
    series_desc = str(getattr(first_dcm, "SeriesDescription", f"SyntheticQACT Fraction {fraction_number}")) or None

    raw_date = str(getattr(first_dcm, "AcquisitionDate", getattr(first_dcm, "ContentDate", "")))
    raw_time = str(getattr(first_dcm, "AcquisitionTime", getattr(first_dcm, "ContentTime", "")))
    scan_dt = None
    if len(raw_date) == 8:
        try:
            hh = int(raw_time[:2]) if len(raw_time) >= 2 else 0
            mm = int(raw_time[2:4]) if len(raw_time) >= 4 else 0
            ss = int(raw_time[4:6]) if len(raw_time) >= 6 else 0
            scan_dt = datetime(int(raw_date[:4]), int(raw_date[4:6]), int(raw_date[6:]), hh, mm, ss)
        except Exception:
            pass

    spacing_tag = getattr(first_dcm, "PixelSpacing", [1.0, 1.0])
    slice_thick = float(getattr(first_dcm, "SliceThickness", 2.0) or 2.0)
    spacing_str = f"{float(spacing_tag[0]):.4f}, {float(spacing_tag[1]):.4f}"
    dims_str = f"{int(getattr(first_dcm, 'Columns', 512))}, {int(getattr(first_dcm, 'Rows', 512))}, {len(valid_slices)}"
    shifts = _get_couch_shifts_from_log(plan_id, fraction_number, db)

    sct = (
        db.query(SyntheticCT)
        .filter_by(plan_id=plan_id, fraction_number=fraction_number)
        .first()
    )
    if not sct:
        sct = SyntheticCT(plan_id=plan_id, fraction_number=fraction_number)
        db.add(sct)

    sct.series_instance_uid = series_uid
    sct.study_instance_uid = study_uid
    sct.series_description = series_desc
    sct.scan_date = scan_dt
    sct.dicom_dir = str(dest_dir)
    sct.num_slices = len(valid_slices)
    sct.pixel_spacing = spacing_str
    sct.slice_thickness = slice_thick
    sct.dimensions = dims_str
    sct.setup_shift_lat_mm = shifts["lat"]
    sct.setup_shift_long_mm = shifts["long"]
    sct.setup_shift_vert_mm = shifts["vert"]
    sct.status = "pending"
    sct.error_message = None

    db.commit()
    db.refresh(sct)
    return sct


def calculate_synthetic_ct_dose(
    plan_id: int,
    fraction_number: int,
    db: Session,
    job_id: Optional[int] = None,
) -> str:
    """
    Executes openMCsquare on the fraction's synthetic CT, evaluates 3D Gamma against
    the planned reference dose, and persists adaptive QA metrics.
    """
    sct = (
        db.query(SyntheticCT)
        .filter_by(plan_id=plan_id, fraction_number=fraction_number)
        .first()
    )
    if not sct or not sct.dicom_dir or not Path(sct.dicom_dir).is_dir():
        raise FileNotFoundError(
            f"No synthetic CT DICOM series found for plan {plan_id} fraction {fraction_number}"
        )

    sct.status = "running"
    db.commit()

    output_dir = _sct_dir(plan_id, fraction_number) / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        mc_path = run_mcSquare_synthetic_ct(
            plan_id=plan_id,
            fraction_number=fraction_number,
            ct_dir=sct.dicom_dir,
            output_dir=str(output_dir),
            job_id=job_id or 0,
            db=db,
        )
        mc_grid = DoseGrid.load(mc_path)

        plan = db.query(Plan).filter_by(id=plan_id).first()
        rtdose_path = find_rtdose_file(plan.dicom_store_path, plan_uid=plan.rtplan_uid) if plan else None
        if not rtdose_path:
            raise FileNotFoundError(f"No reference TPS RTDose found for plan {plan_id}")

        tps_grid = load_rtdose(rtdose_path)
        mc_resampled = _resample_to(mc_grid, tps_grid)

        # 3D Gamma 3%/3mm
        gamma_map_33, pass_rate_33 = gamma_3d(
            reference=tps_grid.array,
            evaluation=mc_resampled.array,
            dd_percent=3.0,
            dta_mm=3.0,
            voxel_size_mm=tuple(tps_grid.spacing),
            dose_threshold_percent=10.0,
        )

        # 3D Gamma 2%/2mm
        gamma_map_22, pass_rate_22 = gamma_3d(
            reference=tps_grid.array,
            evaluation=mc_resampled.array,
            dd_percent=2.0,
            dta_mm=2.0,
            voxel_size_mm=tuple(tps_grid.spacing),
            dose_threshold_percent=10.0,
        )

        d_max = float(tps_grid.array.max())
        mask_eval = tps_grid.array >= (0.10 * d_max)
        if np.any(mask_eval):
            diff_rel = (mc_resampled.array[mask_eval] - tps_grid.array[mask_eval]) / d_max * 100.0
            mean_diff = float(np.mean(diff_rel))
            max_diff = float(np.max(np.abs(diff_rel)))
        else:
            mean_diff = 0.0
            max_diff = 0.0

        gamma_save_path = output_dir / "gamma_sct_vs_tps.npz"
        np.savez_compressed(
            str(gamma_save_path),
            gamma_map=gamma_map_33.astype(np.float32),
            gamma_2mm=gamma_map_22.astype(np.float32),
            passing_rate=pass_rate_33,
            passing_rate_2mm=pass_rate_22,
            spacing=tps_grid.spacing,
            origin=tps_grid.origin,
        )

        sct.status = "complete"
        sct.dose_path = str(mc_path)
        sct.gamma_passing_rate = round(pass_rate_33, 2)
        sct.gamma_2mm_passing_rate = round(pass_rate_22, 2)
        sct.gamma_passed = bool(pass_rate_33 >= 90.0)
        sct.mean_dose_diff_pct = round(mean_diff, 2)
        sct.max_dose_diff_pct = round(max_diff, 2)
        sct.calculated_at = datetime.now(timezone.utc)
        sct.error_message = None

        db.commit()
        db.refresh(sct)
        logger.info(
            f"Completed synthetic CT calculation for plan {plan_id} fx {fraction_number}: "
            f"3%/3mm={pass_rate_33:.1f}%, 2%/2mm={pass_rate_22:.1f}%"
        )
        return str(mc_path)
    except Exception as exc:
        sct.status = "error"
        sct.error_message = str(exc)
        db.commit()
        logger.exception(f"Synthetic CT calculation failed for plan {plan_id} fx {fraction_number}: {exc}")
        raise


def get_sct_dose_plane(
    plan_id: int,
    fraction_number: int,
    z: int,
    db: Session,
) -> tuple[np.ndarray, float]:
    """Returns an axial dose plane (2D float32 array) and the global maximum dose."""
    sct = (
        db.query(SyntheticCT)
        .filter_by(plan_id=plan_id, fraction_number=fraction_number)
        .first()
    )
    if not sct or not sct.dose_path or not Path(sct.dose_path).exists():
        raise FileNotFoundError(f"Synthetic CT dose not calculated for plan {plan_id} fx {fraction_number}")

    grid = DoseGrid.load(sct.dose_path)
    nz, ny, nx = grid.shape
    if z < 0 or z >= nz:
        raise ValueError(f"Slice index z={z} out of bounds for depth nz={nz}")

    plane = grid.array[z, :, :].astype(np.float32)
    max_dose = float(grid.array.max())
    return plane, max_dose


def get_sct_gamma_plane(
    plan_id: int,
    fraction_number: int,
    z: int,
    db: Session,
) -> tuple[np.ndarray, float]:
    """Returns an axial slice of the 3D gamma map aligned to CT slice z and the slice passing rate."""
    gamma_path = _sct_dir(plan_id, fraction_number) / "output" / "gamma_sct_vs_tps.npz"
    if not gamma_path.exists():
        raise FileNotFoundError(f"Gamma map not found for plan {plan_id} fx {fraction_number}")

    data = np.load(str(gamma_path))
    gmap = data["gamma_map"]

    sct = (
        db.query(SyntheticCT)
        .filter_by(plan_id=plan_id, fraction_number=fraction_number)
        .first()
    )
    if sct and sct.dose_path and Path(sct.dose_path).exists():
        dose_data = np.load(sct.dose_path)
        c_shape = dose_data["array"].shape
        c_spacing = dose_data["spacing"]
        c_origin = dose_data["origin"]

        if z < 0 or z >= c_shape[0]:
            raise ValueError(f"Slice index z={z} out of bounds for depth nz={c_shape[0]}")

        if gmap.shape == c_shape:
            plane = gmap[z, :, :].astype(np.float32)
        else:
            from scipy.ndimage import map_coordinates

            g_spacing = data["spacing"]
            g_origin = data["origin"]
            z_phys = c_origin[0] + z * c_spacing[0]
            iz = (z_phys - g_origin[0]) / g_spacing[0]

            if iz < 0 or iz > gmap.shape[0] - 1:
                plane = np.full((c_shape[1], c_shape[2]), np.nan, dtype=np.float32)
            else:
                iz0 = int(np.floor(iz))
                iz1 = min(iz0 + 1, gmap.shape[0] - 1)
                w1 = float(iz - iz0)
                w0 = 1.0 - w1
                gslice = w0 * gmap[iz0] + w1 * gmap[iz1]

                iy = (c_origin[1] + np.arange(c_shape[1]) * c_spacing[1] - g_origin[1]) / g_spacing[1]
                ix = (c_origin[2] + np.arange(c_shape[2]) * c_spacing[2] - g_origin[2]) / g_spacing[2]
                coords_y, coords_x = np.meshgrid(iy, ix, indexing="ij")
                plane = map_coordinates(gslice, [coords_y, coords_x], order=1, cval=np.nan).astype(np.float32)
    else:
        nz, ny, nx = gmap.shape
        if z < 0 or z >= nz:
            raise ValueError(f"Slice index z={z} out of bounds for depth nz={nz}")
        plane = gmap[z, :, :].astype(np.float32)

    valid = np.isfinite(plane) & (plane >= 0)
    pass_rate = float(np.sum(plane[valid] <= 1.0) / np.sum(valid) * 100.0) if np.any(valid) else 100.0
    return plane, pass_rate


def get_sct_image_plane(
    plan_id: int,
    fraction_number: int,
    z: int,
    db: Session,
) -> np.ndarray:
    """Returns an axial slice of the synthetic CT image in Hounsfield Units as a float32 array."""
    sct = (
        db.query(SyntheticCT)
        .filter_by(plan_id=plan_id, fraction_number=fraction_number)
        .first()
    )
    if not sct or not sct.dicom_dir or not Path(sct.dicom_dir).is_dir():
        raise FileNotFoundError(f"Synthetic CT images not found for plan {plan_id} fx {fraction_number}")

    slices = sorted(list(Path(sct.dicom_dir).glob("*.dcm")))
    if not slices:
        raise FileNotFoundError(f"No DICOM slices in {sct.dicom_dir}")

    if z < 0 or z >= len(slices):
        raise ValueError(f"Slice index z={z} out of bounds for total slices={len(slices)}")

    dcm = pydicom.dcmread(str(slices[z]), force=True)
    pixels = dcm.pixel_array.astype(np.float32)
    slope = float(getattr(dcm, "RescaleSlope", 1.0) or 1.0)
    intercept = float(getattr(dcm, "RescaleIntercept", 0.0) or 0.0)
    hu = pixels * slope + intercept
    return hu.astype(np.float32)


def get_cbct_image_plane(
    plan_id: int,
    fraction_number: int,
    z: int,
    db: Session,
) -> np.ndarray:
    """Returns an axial slice of the raw CBCT image in Hounsfield Units as a float32 array."""
    sct = (
        db.query(SyntheticCT)
        .filter_by(plan_id=plan_id, fraction_number=fraction_number)
        .first()
    )
    if not sct or not sct.cbct_dir or not Path(sct.cbct_dir).is_dir():
        raise FileNotFoundError(f"Raw CBCT images not found for plan {plan_id} fx {fraction_number}")

    slices = sorted(list(Path(sct.cbct_dir).glob("*.dcm")))
    if not slices:
        raise FileNotFoundError(f"No CBCT DICOM slices in {sct.cbct_dir}")

    if z < 0 or z >= len(slices):
        raise ValueError(f"Slice index z={z} out of bounds for total slices={len(slices)}")

    dcm = pydicom.dcmread(str(slices[z]), force=True)
    pixels = dcm.pixel_array.astype(np.float32)
    slope = float(getattr(dcm, "RescaleSlope", 1.0) or 1.0)
    intercept = float(getattr(dcm, "RescaleIntercept", 0.0) or 0.0)
    hu = pixels * slope + intercept
    return hu.astype(np.float32)


def get_sct_external_plane(
    plan_id: int,
    fraction_number: int,
    z: int,
    db: Session,
    target: str = "sct",
) -> np.ndarray:
    """Returns a 2D uint8 slice (0 or 1) of the external body mask for slice z."""
    sct_dir = _sct_dir(plan_id, fraction_number)

    if target == "cbct":
        mask_file = sct_dir / "cbct_external_mask.npz"
        if not mask_file.is_file():
            # Compute on the fly using propagated RTSTRUCT if available, or fallback to auto
            sct = (
                db.query(SyntheticCT)
                .filter_by(plan_id=plan_id, fraction_number=fraction_number)
                .first()
            )
            if not sct or not sct.cbct_dir or not Path(sct.cbct_dir).is_dir():
                raise FileNotFoundError(f"Raw CBCT images not found for plan {plan_id} fx {fraction_number}")
            cbct_arr, cbct_geom, cbct_meta = load_cbct_series([sct.cbct_dir])
            plan = db.query(Plan).filter_by(id=plan_id).first()
            plan_arr, plan_geom, plan_meta = find_planning_ct_series(plan.dicom_store_path) if (plan and plan.dicom_store_path) else (None, None, None)
            rtstruct_file = _find_rtstruct_file(plan) if plan else None

            # Spatial alignment transform
            sitk_transform = None
            if plan_geom is not None and cbct_geom is not None:
                reg_transform, _ = _find_and_load_reg_transform(
                    plan_id,
                    fraction_number,
                    str(cbct_meta.get("frame_of_reference_uid") or getattr(sct, "cbct_series_instance_uid", "")),
                    str(plan_meta.get("frame_of_reference_uid") or ""),
                    db,
                )
                if reg_transform is not None:
                    sitk_transform = to_sitk_fixed_to_moving(reg_transform)
                else:
                    shifts = _get_couch_shifts_from_log(plan_id, fraction_number, db)
                    tx = float(shifts.get("lat") or 0.0)
                    ty = float(shifts.get("long") or 0.0)
                    tz = float(shifts.get("vert") or 0.0)
                    if abs(tx) > 0.001 or abs(ty) > 0.001 or abs(tz) > 0.001:
                        rigid_t = make_transform(translation_mm=(tx, ty, tz))
                        sitk_transform = to_sitk_fixed_to_moving(rigid_t)

            cbct_mask, cbct_meta_dict = compute_cbct_external_mask(
                cbct_arr=cbct_arr,
                cbct_geom=cbct_geom,
                plan_arr_shape=plan_arr.shape if plan_arr is not None else None,
                plan_geom=plan_geom,
                rtstruct_path=rtstruct_file,
                sitk_transform=sitk_transform,
            )
            save_external_mask(mask_file, cbct_mask, metadata=cbct_meta_dict)
        mask, _ = load_external_mask(mask_file)
    else:
        mask_file = sct_dir / "external_mask.npz"
        if not mask_file.is_file():
            raise FileNotFoundError(f"External mask not found for plan {plan_id} fx {fraction_number}")
        mask, _ = load_external_mask(mask_file)

    if mask is None:
        raise FileNotFoundError(f"Failed to load external mask for plan {plan_id} fx {fraction_number} (target={target})")
    nz, ny, nx = mask.shape
    if z < 0 or z >= nz:
        raise ValueError(f"Slice index z={z} out of bounds for total slices={nz} (target={target})")
    return mask[z, :, :].astype(np.uint8)


def get_sct_external_info(
    plan_id: int,
    fraction_number: int,
    db: Session,
    target: str = "sct",
) -> dict[str, Any]:
    """Returns metadata about the current external contour mask and available sources."""
    plan = db.query(Plan).filter_by(id=plan_id).first()
    rtstruct_file = _find_rtstruct_file(plan) if plan else None

    sct_dir = _sct_dir(plan_id, fraction_number)
    mask_file = sct_dir / ("cbct_external_mask.npz" if target == "cbct" else "external_mask.npz")
    mask, meta = load_external_mask(mask_file)

    default_source = "rtstruct" if rtstruct_file else "auto"
    available_rois = get_rtstruct_external_rois(rtstruct_file) if rtstruct_file else []

    return {
        "has_mask": mask is not None,
        "total_voxels": int(mask.sum()) if mask is not None else 0,
        "source": meta.get("source", default_source),
        "threshold_hu": meta.get("threshold_hu", -350.0),
        "closing_radius": meta.get("closing_radius", 5),
        "use_convex_hull": meta.get("use_convex_hull", False),
        "include_mask": meta.get("include_mask", True),
        "selected_roi": meta.get("selected_roi"),
        "has_rtstruct": bool(rtstruct_file and rtstruct_file.is_file()),
        "rtstruct_file": rtstruct_file.name if rtstruct_file else None,
        "available_rois": available_rois,
        "target": target,
    }


def recompute_fraction_external(
    plan_id: int,
    fraction_number: int,
    source: str,
    threshold_hu: float,
    closing_radius: int,
    use_convex_hull: bool,
    db: Session,
    include_mask: bool = True,
    selected_roi_name: Optional[str] = None,
) -> dict[str, Any]:
    """
    Recomputes the external contour mask with user-adjusted parameters or source,
    updates external_mask.npz, re-applies the mask to the synthetic CT volume,
    and re-exports the DICOM CT series.
    """
    sct = (
        db.query(SyntheticCT)
        .filter_by(plan_id=plan_id, fraction_number=fraction_number)
        .first()
    )
    if not sct or not sct.dicom_dir:
        raise FileNotFoundError(f"Synthetic CT not found for plan {plan_id} fx {fraction_number}")

    plan = db.query(Plan).filter_by(id=plan_id).first()
    if not plan or not plan.dicom_store_path:
        raise FileNotFoundError(f"Plan {plan_id} has no valid DICOM store")

    sct_dir = _sct_dir(plan_id, fraction_number)
    raw_npz = sct_dir / "raw_sct.npz"
    plan_arr, plan_geom, plan_meta = find_planning_ct_series(plan.dicom_store_path)

    if raw_npz.is_file():
        raw_data = np.load(str(raw_npz))
        raw_sct = raw_data["sct"]
    else:
        # Load directly from existing DICOM slices
        dicom_files = sorted(list(Path(sct.dicom_dir).glob("*.dcm")))
        if not dicom_files:
            raise FileNotFoundError(f"No DICOM slices found in {sct.dicom_dir}")
        slices = [pydicom.dcmread(str(f), force=True) for f in dicom_files]
        raw_sct = np.stack(
            [
                s.pixel_array.astype(np.float32) * float(getattr(s, "RescaleSlope", 1.0))
                + float(getattr(s, "RescaleIntercept", 0.0))
                for s in slices
            ],
            axis=0,
        )

    rtstruct_file = _find_rtstruct_file(plan)
    mask, actual_source, mask_info = determine_external_mask(
        image_kji=raw_sct,
        geometry=plan_geom,
        rtstruct_path=rtstruct_file,
        source=source,
        threshold_hu=threshold_hu,
        closing_radius=closing_radius,
        use_convex_hull=use_convex_hull,
        include_mask=include_mask,
        selected_roi_name=selected_roi_name,
    )

    save_external_mask(sct_dir / "external_mask.npz", mask, mask_info)

    # Also keep CBCT external mask synchronized
    if sct.cbct_dir and Path(sct.cbct_dir).is_dir():
        try:
            cbct_arr, cbct_geom, cbct_meta = load_cbct_series([sct.cbct_dir])
            sitk_transform = None
            reg_transform, _ = _find_and_load_reg_transform(
                plan_id,
                fraction_number,
                str(cbct_meta.get("frame_of_reference_uid") or getattr(sct, "cbct_series_instance_uid", "")),
                str(plan_meta.get("frame_of_reference_uid") or ""),
                db,
                cbct_series_uid=str(cbct_meta.get("series_instance_uid") or getattr(sct, "cbct_series_instance_uid", "")),
            )
            if reg_transform is not None:
                sitk_transform = to_sitk_fixed_to_moving(reg_transform)
            else:
                shifts = _get_couch_shifts_from_log(plan_id, fraction_number, db)
                tx = float(shifts.get("lat") or 0.0)
                ty = float(shifts.get("long") or 0.0)
                tz = float(shifts.get("vert") or 0.0)
                if abs(tx) > 0.001 or abs(ty) > 0.001 or abs(tz) > 0.001:
                    rigid_t = make_transform(translation_mm=(tx, ty, tz))
                    sitk_transform = to_sitk_fixed_to_moving(rigid_t)

            cbct_mask, cbct_meta_dict = compute_cbct_external_mask(
                cbct_arr=cbct_arr,
                cbct_geom=cbct_geom,
                plan_arr_shape=plan_arr.shape,
                plan_geom=plan_geom,
                rtstruct_path=rtstruct_file if source == "rtstruct" else None,
                sitk_transform=sitk_transform,
                threshold_hu=threshold_hu,
                closing_radius=closing_radius,
                include_mask=include_mask,
                selected_roi_name=selected_roi_name,
            )
            save_external_mask(sct_dir / "cbct_external_mask.npz", cbct_mask, metadata=cbct_meta_dict)
        except Exception as exc:
            logger.warning(f"Could not recompute CBCT external mask: {exc}")

    # Re-apply external mask to volume
    masked_sct = apply_external_mask_to_volume(raw_sct, mask, background_hu=-1000.0)

    # Re-export DICOM series
    export_res = export_ct_series(
        sct_kji=masked_sct,
        geometry=plan_geom,
        out_dir=str(sct.dicom_dir),
        ref_meta=plan_meta,
        series_description=sct.series_description or f"SyntheticQACT Fx{fraction_number}",
        patient_position=str(plan_meta.get("patient_position", "HFS")),
    )

    sct.status = "contour_check"
    sct.num_slices = export_res["files_written"]
    db.commit()
    db.refresh(sct)

    logger.info(
        f"Recomputed external contour for plan {plan_id} fx {fraction_number} "
        f"using {actual_source} ({mask.sum()} voxels)"
    )
    return {
        "status": "success",
        "mask_info": mask_info,
        "num_slices": sct.num_slices,
    }


def approve_external_and_calculate_dose(
    plan_id: int,
    fraction_number: int,
    db: Session,
    background_tasks: Optional[Any] = None,
) -> dict[str, Any]:
    """
    Approves the external contour and queues openMCsquare dose calculation on the synthetic CT.
    """
    from models.qa_job import QAJob
    from services.job_runner import run_qa_job

    sct = (
        db.query(SyntheticCT)
        .filter_by(plan_id=plan_id, fraction_number=fraction_number)
        .first()
    )
    if not sct or not sct.dicom_dir or not Path(sct.dicom_dir).is_dir():
        raise FileNotFoundError(
            f"No synthetic CT DICOM series found for plan {plan_id} fraction {fraction_number}"
        )

    job = QAJob(
        plan_id=plan_id,
        job_type="synthetic_ct_mcSquare",
        status="queued",
        progress=0.0,
        fraction_number=fraction_number,
    )
    db.add(job)
    sct.status = "running"
    sct.error_message = None
    db.commit()
    db.refresh(job)

    if background_tasks:
        background_tasks.add_task(run_qa_job, job.id)
    else:
        import threading
        t = threading.Thread(target=run_qa_job, args=(job.id,), daemon=True)
        t.start()

    return {
        "job_id": job.id,
        "status": "queued",
        "fraction_number": fraction_number,
    }


def list_synthetic_cts(plan_id: int, db: Session) -> list[dict[str, Any]]:
    """List summary metadata for all synthetic CT scans of a plan."""
    records = (
        db.query(SyntheticCT)
        .filter_by(plan_id=plan_id)
        .order_by(SyntheticCT.fraction_number.asc())
        .all()
    )
    summaries = []
    for r in records:
        summaries.append({
            "id": r.id,
            "fraction_number": r.fraction_number,
            "series_instance_uid": r.series_instance_uid,
            "series_description": r.series_description,
            "scan_date": r.scan_date.isoformat() if r.scan_date else None,
            "num_slices": r.num_slices,
            "cbct_num_slices": r.cbct_num_slices,
            "has_cbct": bool(r.cbct_dir and Path(r.cbct_dir).is_dir()),
            "dimensions": r.dimensions,
            "pixel_spacing": r.pixel_spacing,
            "slice_thickness": r.slice_thickness,
            "dir_method": r.dir_method,
            "dir_mean_displacement_mm": r.dir_mean_displacement_mm,
            "dir_p99_displacement_mm": r.dir_p99_displacement_mm,
            "mae_hu_before": r.mae_hu_before,
            "mae_hu_after": r.mae_hu_after,
            "status": r.status,
            "gamma_passing_rate": r.gamma_passing_rate,
            "gamma_2mm_passing_rate": r.gamma_2mm_passing_rate,
            "gamma_passed": r.gamma_passed,
            "mean_dose_diff_pct": r.mean_dose_diff_pct,
            "max_dose_diff_pct": r.max_dose_diff_pct,
            "setup_shift_lat_mm": r.setup_shift_lat_mm,
            "setup_shift_long_mm": r.setup_shift_long_mm,
            "setup_shift_vert_mm": r.setup_shift_vert_mm,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "calculated_at": r.calculated_at.isoformat() if r.calculated_at else None,
        })
    return summaries


def get_synthetic_ct_detail(plan_id: int, fraction_number: int, db: Session) -> Optional[dict[str, Any]]:
    """Detailed metadata and metrics for a specific fraction's synthetic CT."""
    r = (
        db.query(SyntheticCT)
        .filter_by(plan_id=plan_id, fraction_number=fraction_number)
        .first()
    )
    if not r:
        return None

    return {
        "id": r.id,
        "plan_id": r.plan_id,
        "fraction_number": r.fraction_number,
        "series_instance_uid": r.series_instance_uid,
        "study_instance_uid": r.study_instance_uid,
        "series_description": r.series_description,
        "scan_date": r.scan_date.isoformat() if r.scan_date else None,
        "num_slices": r.num_slices,
        "cbct_num_slices": r.cbct_num_slices,
        "has_cbct": bool(r.cbct_dir and Path(r.cbct_dir).is_dir()),
        "dimensions": r.dimensions,
        "pixel_spacing": r.pixel_spacing,
        "slice_thickness": r.slice_thickness,
        "dir_method": r.dir_method,
        "dir_mean_displacement_mm": r.dir_mean_displacement_mm,
        "dir_p99_displacement_mm": r.dir_p99_displacement_mm,
        "mae_hu_before": r.mae_hu_before,
        "mae_hu_after": r.mae_hu_after,
        "status": r.status,
        "error_message": r.error_message,
        "has_external_mask": bool((_sct_dir(plan_id, fraction_number) / "external_mask.npz").is_file()),
        "has_dose": bool(r.dose_path and Path(r.dose_path).exists()),
        "gamma_passing_rate": r.gamma_passing_rate,
        "gamma_2mm_passing_rate": r.gamma_2mm_passing_rate,
        "gamma_passed": r.gamma_passed,
        "mean_dose_diff_pct": r.mean_dose_diff_pct,
        "max_dose_diff_pct": r.max_dose_diff_pct,
        "setup_shift_lat_mm": r.setup_shift_lat_mm,
        "setup_shift_long_mm": r.setup_shift_long_mm,
        "setup_shift_vert_mm": r.setup_shift_vert_mm,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "calculated_at": r.calculated_at.isoformat() if r.calculated_at else None,
    }
