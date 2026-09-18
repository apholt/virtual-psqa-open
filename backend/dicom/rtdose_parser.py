"""
RTDose parser — loads the TPS dose grid into a DoseGrid container.
Applies DoseGridScaling (3004,000E) to convert raw pixel values to Gy and
reconstructs voxel spacing from PixelSpacing + GridFrameOffsetVector.
"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Optional
import numpy as np
import pydicom
from services.dose_grid import DoseGrid
logger = logging.getLogger(__name__)

def find_rtdose_file(dicom_store_path: str, plan_uid: Optional[str] = None) -> Optional[str]:
    """
    Scans a plan's dicom_store folder for the RTDOSE file to use as the TPS
    reference dose. Prefers the PLAN-level summed dose (DoseSummationType=PLAN)
    over field/beam doses.

    If `plan_uid` is provided, filters for RTDose files whose
    ReferencedRTPlanSequence matches `plan_uid`, preventing cross-contamination
    when multiple plans or adaptive versions share a patient store.
    Falls back to unreferenced RTDose only if no matching plan-referenced dose exists.
    """
    matching_candidates: list[str] = []
    matching_plan_dose: Optional[str] = None
    fallback_candidates: list[str] = []
    fallback_plan_dose: Optional[str] = None

    for path in sorted(Path(dicom_store_path).glob("*.dcm")):
        try:
            dcm = pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
            if str(dcm.get("Modality", "")).upper() != "RTDOSE":
                continue

            summary_type = str(dcm.get("DoseSummationType", "")).upper()
            ref_plan_uid = None
            try:
                ref_seq = getattr(dcm, "ReferencedRTPlanSequence", None)
                if ref_seq and len(ref_seq) > 0:
                    ref_plan_uid = str(ref_seq[0].ReferencedSOPInstanceUID)
            except Exception:
                pass

            if plan_uid is not None:
                if ref_plan_uid is not None:
                    if ref_plan_uid == plan_uid:
                        matching_candidates.append(str(path))
                        if summary_type == "PLAN":
                            matching_plan_dose = str(path)
                    else:
                        # Explicitly references a DIFFERENT plan — skip!
                        logger.debug(
                            f"Skipping RTDose {path.name}: references plan {ref_plan_uid} != target {plan_uid}"
                        )
                        continue
                else:
                    fallback_candidates.append(str(path))
                    if summary_type == "PLAN":
                        fallback_plan_dose = str(path)
            else:
                fallback_candidates.append(str(path))
                if summary_type == "PLAN":
                    fallback_plan_dose = str(path)
        except Exception:
            continue

    if matching_plan_dose:
        logger.info(f"Using plan-matched PLAN-level RTDose: {matching_plan_dose}")
        return matching_plan_dose
    if matching_candidates:
        logger.info(f"Using plan-matched RTDose: {matching_candidates[0]}")
        return matching_candidates[0]
    if fallback_plan_dose:
        logger.info(f"Using fallback PLAN-level RTDose: {fallback_plan_dose}")
        return fallback_plan_dose
    if fallback_candidates:
        logger.warning(
            f"No plan-matched RTDose found in {dicom_store_path}; "
            f"falling back to first available RTDose: {fallback_candidates[0]}"
        )
        return fallback_candidates[0]
    return None


def find_beam_rtdose_files(
    dicom_store_path: str,
    plan_uid: Optional[str] = None,
    valid_beam_numbers: Optional[set[int] | list[int]] = None,
) -> dict[int, str]:
    """
    Finds per-beam RTDose files (DoseSummationType=BEAM or BEAM_SESSION).
    Returns {beam_number: file_path} by reading ReferencedBeamNumber from each file.

    If `plan_uid` is provided, skips any RTDOSE referencing a different RTPlan.
    If `valid_beam_numbers` is provided, skips beam numbers not belonging to the plan.
    """
    valid_set = set(valid_beam_numbers) if valid_beam_numbers is not None else None
    beam_doses: dict[int, str] = {}
    for path in sorted(Path(dicom_store_path).glob("*.dcm")):
        try:
            dcm = pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
            if str(dcm.get("Modality", "")).upper() != "RTDOSE":
                continue
            summary_type = str(dcm.get("DoseSummationType", "")).upper()
            if summary_type not in ("BEAM", "BEAM_SESSION", "FRACTION"):
                continue

            # Verify referenced plan if provided
            ref_plan_uid = None
            try:
                ref_seq = getattr(dcm, "ReferencedRTPlanSequence", None)
                if ref_seq and len(ref_seq) > 0:
                    ref_plan_uid = str(ref_seq[0].ReferencedSOPInstanceUID)
            except Exception:
                pass

            if plan_uid is not None and ref_plan_uid is not None and ref_plan_uid != plan_uid:
                logger.debug(
                    f"Skipping beam RTDose {path.name}: references plan {ref_plan_uid} != target {plan_uid}"
                )
                continue

            # Extract the referenced beam number
            beam_num = None
            try:
                rfp = dcm.ReferencedRTPlanSequence[0]
                rfg = rfp.ReferencedFractionGroupSequence[0]
                rfb = rfg.ReferencedBeamSequence[0]
                beam_num = int(rfb.ReferencedBeamNumber)
            except Exception:
                pass

            if beam_num is not None:
                if valid_set is not None and beam_num not in valid_set:
                    logger.debug(
                        f"Skipping beam RTDose {path.name}: beam {beam_num} not in valid plan beams {valid_set}"
                    )
                    continue
                beam_doses[beam_num] = str(path)
                logger.info(f"Found BEAM-level RTDose for beam {beam_num}: {path.name}")
        except Exception:
            continue
    return beam_doses


def _voxel_spacing(dcm: pydicom.Dataset) -> tuple[float, float, float]:
    """Returns (sz, sy, sx) voxel spacing in mm."""
    pixel_spacing = list(getattr(dcm, "PixelSpacing", [2.0, 2.0]))
    sy = float(pixel_spacing[0])
    sx = float(pixel_spacing[1]) if len(pixel_spacing) > 1 else sy
    # z spacing from GridFrameOffsetVector if available, else SliceThickness
    offsets = getattr(dcm, "GridFrameOffsetVector", None)
    if offsets is not None and len(offsets) >= 2:
        sz = abs(float(offsets[1]) - float(offsets[0]))
    else:
        sz = float(dcm.get("SliceThickness", sy) or sy)
    return (sz, sy, sx)


def extract_dose_plane(dcm: pydicom.Dataset, depth_index: int) -> np.ndarray:
    """
    Returns a single axial dose plane in Gy.
    RTDose pixel_array shape is (n_slices, rows, cols).
    """
    pixel_array = dcm.pixel_array.astype(np.float32)
    scaling = float(dcm.get("DoseGridScaling", 1.0) or 1.0)
    if pixel_array.ndim == 2:
        return pixel_array * scaling
    depth_index = max(0, min(depth_index, pixel_array.shape[0] - 1))
    return pixel_array[depth_index] * scaling


def load_rtdose(path: str) -> DoseGrid:
    """
    Loads a full RTDose file into a DoseGrid (dose in Gy).
    """
    dcm = pydicom.dcmread(path)
    scaling = float(dcm.get("DoseGridScaling", 1.0) or 1.0)
    pixel_array = dcm.pixel_array.astype(np.float32)
    if pixel_array.ndim == 2:
        pixel_array = pixel_array[np.newaxis, ...]
    dose = pixel_array * scaling
    spacing = _voxel_spacing(dcm)
    ipp = list(getattr(dcm, "ImagePositionPatient", [0.0, 0.0, 0.0]))
    # ipp is (x, y, z); store origin as (z, y, x) to match array axis order
    origin = (
        float(ipp[2]) if len(ipp) > 2 else 0.0,
        float(ipp[1]) if len(ipp) > 1 else 0.0,
        float(ipp[0]) if len(ipp) > 0 else 0.0,
    )
    return DoseGrid(array=dose, spacing=spacing, origin=origin)


def load_plan_rtdose(dicom_store_path: str, plan_uid: Optional[str] = None) -> DoseGrid:
    """Convenience: finds and loads the RTDose for a plan's dicom_store folder."""
    path = find_rtdose_file(dicom_store_path, plan_uid=plan_uid)
    if path is None:
        raise FileNotFoundError(f"No RTDOSE file found in {dicom_store_path}")
    return load_rtdose(path)