"""
services/imaging/external_contour.py -- Robust external contour extraction and editing
for Synthetic CT.

Guarantees ZERO internal voids:
- Prioritizes clinical External ROI from Planning RTSTRUCT if available.
- Fallback: 2D slice-by-slice exterior flood fill, morphological closing & hole-filling,
  or optional 2D convex envelope that bridges across severe dental artifact streaks.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pydicom
from PIL import Image as _PILImage, ImageDraw as _PILDraw
from scipy.ndimage import binary_closing, binary_fill_holes
from scipy.spatial import ConvexHull

from services.imaging.volume_geometry import VolumeGeometry

logger = logging.getLogger(__name__)


def get_rtstruct_external_rois(rtstruct_path: Optional[str | Path]) -> list[Dict[str, Any]]:
    """
    Inspect planning RTSTRUCT and return all candidate external/body/skin ROIs with metadata:
    [{"roi_number": 1, "roi_name": "External", "interpreted_type": "EXTERNAL", "includes_mask": True, "is_recommended": True}, ...]
    """
    if not rtstruct_path:
        return []
    path = Path(rtstruct_path)
    if not path.is_file():
        return []

    try:
        dcm = pydicom.dcmread(str(path), force=True)
        if str(getattr(dcm, "Modality", "")).upper() != "RTSTRUCT":
            return []

        names = {s.ROINumber: str(s.ROIName) for s in getattr(dcm, "StructureSetROISequence", [])}
        obs_types = {}
        for obs in getattr(dcm, "RTROIObservationsSequence", []):
            obs_types[obs.ReferencedROINumber] = str(getattr(obs, "RTROIInterpretedType", "")).upper()

        candidates: list[Dict[str, Any]] = []
        for num, name in names.items():
            clean = name.strip().lower()
            t = obs_types.get(num, "")
            is_ext_type = (t == "EXTERNAL")
            has_keywords = any(k in clean for k in ("external", "body", "patient", "skin"))
            if is_ext_type or has_keywords:
                is_skin_only = ("skin" in clean and "external" not in clean and "body" not in clean)
                candidates.append({
                    "roi_number": int(num),
                    "roi_name": name,
                    "interpreted_type": t,
                    "includes_mask": not is_skin_only,
                    "is_recommended": is_ext_type or clean in ("external", "body"),
                })
        candidates.sort(key=lambda c: (not c["is_recommended"], not c["includes_mask"], c["roi_name"]))
        return candidates
    except Exception as exc:
        logger.warning(f"Could not read RTSTRUCT ROIs from {rtstruct_path}: {exc}")
        return []


def get_rtstruct_external_mask(
    rtstruct_path: str | Path,
    shape: Tuple[int, int, int],
    geometry: VolumeGeometry,
    preferred_roi_names: Optional[Tuple[str, ...] | list[str]] = None,
    include_mask: bool = True,
) -> Optional[np.ndarray]:
    """
    Extract the clinical External / Body ROI from the planning RTSTRUCT and rasterize
    onto the 3D volume grid (nz, ny, nx).
    Returns boolean numpy array or None if not found.
    """
    path = Path(rtstruct_path)
    if not path.is_file():
        return None

    try:
        dcm = pydicom.dcmread(str(path), force=True)
        if str(getattr(dcm, "Modality", "")).upper() != "RTSTRUCT":
            return None

        # Find External ROI Number
        names = {s.ROINumber: str(s.ROIName) for s in getattr(dcm, "StructureSetROISequence", [])}
        ext_number = None

        # Determine preference order based on include_mask flag if not specified
        prefs = preferred_roi_names
        if not prefs:
            if include_mask:
                prefs = ("external", "body", "patient", "skin_surface", "skin")
            else:
                prefs = ("skin_surface", "skin", "external", "body", "patient")

        # 0. Check preferred ROI names
        for pref in prefs:
            pref_clean = pref.strip().lower()
            for num, nm in names.items():
                if nm.strip().lower() == pref_clean:
                    ext_number = num
                    break
            if ext_number is not None:
                break

        # 1. Prefer RTROIObservationsSequence interpreted type
        if ext_number is None:
            for obs in getattr(dcm, "RTROIObservationsSequence", []):
                if str(getattr(obs, "RTROIInterpretedType", "")).upper() == "EXTERNAL":
                    ext_number = getattr(obs, "ReferencedROINumber", None)
                    break

        # 2. Fall back to name match
        if ext_number is None:
            for num, nm in names.items():
                clean_nm = nm.strip().lower()
                if clean_nm in ("external", "body", "patient", "skin"):
                    ext_number = num
                    break

        if ext_number is None:
            return None

        rc = next(
            (
                r
                for r in getattr(dcm, "ROIContourSequence", [])
                if getattr(r, "ReferencedROINumber", None) == ext_number
            ),
            None,
        )
        if rc is None or not hasattr(rc, "ContourSequence"):
            return None

        nzv, nyv, nxv = shape
        sx, sy, sz = geometry.spacing_mm
        ox, oy, oz = geometry.origin_lps

        mask = np.zeros((nzv, nyv, nxv), dtype=bool)
        for dslice in rc.ContourSequence:
            cd = getattr(dslice, "ContourData", None)
            if not cd or len(cd) < 9:
                continue
            xs = (np.asarray(cd[0::3], dtype=float) - ox) / sx
            ys = (np.asarray(cd[1::3], dtype=float) - oy) / sy
            zi = int(round((float(cd[2]) - oz) / sz))
            if zi < 0 or zi >= nzv:
                continue
            xy = list(zip(xs, ys))
            if len(xy) < 3:
                continue
            img = _PILImage.new("L", (nxv, nyv), 0)
            _PILDraw.Draw(img).polygon(xy, outline=1, fill=1)
            mask[zi] |= np.array(img, dtype=bool)

        if mask.any():
            # Apply binary fillhole slice by slice to guarantee zero internal voids
            for z in range(nzv):
                if mask[z].any():
                    mask[z] = binary_fill_holes(mask[z])
            logger.info(
                f"Extracted clinical External contour from RTSTRUCT ({names.get(ext_number)}): {mask.sum()} voxels"
            )
            return mask
    except Exception as exc:
        logger.warning(f"Failed to extract RTSTRUCT External contour from {path}: {exc}")

    return None


def compute_robust_external_mask(
    image_kji: np.ndarray,
    threshold_hu: float = -350.0,
    closing_radius: int = 5,
    use_convex_hull: bool = False,
    couch_cutoff_fraction: float = 0.92,
    include_mask: bool = True,
) -> np.ndarray:
    """
    Derive a solid external body mask slice-by-slice.
    Guaranteed zero internal voids via morphological closing + 2D hole filling.
    If include_mask=True:
      - Uses tissue/plastic threshold (-500 HU) to capture perforated thermoplastic masks.
      - Uses larger closing radius (>=14 px) to bridge the 5-15mm air gap between the mask and skin.
      - Fills all interior voids so the mask and internal air gap are solid interior.
    If use_convex_hull=True, applies 2D convex envelope per slice (ideal for bridging
    severe metal artifact dark streaks in the oral cavity).
    """
    nz, ny, nx = image_kji.shape
    out_mask = np.zeros((nz, ny, nx), dtype=bool)

    eff_thresh = min(threshold_hu, -500.0) if include_mask else threshold_hu
    eff_radius = max(closing_radius, 14) if include_mask else closing_radius
    selem = np.ones((eff_radius, eff_radius), dtype=bool)
    couch_row = int(round(ny * couch_cutoff_fraction))

    for z in range(nz):
        sl = image_kji[z]
        tissue = sl > float(eff_thresh)
        if not np.any(tissue):
            continue

        # Exclude lower edge couch table if prominent
        tissue_clean = tissue.copy()
        if couch_row < ny:
            tissue_clean[couch_row:, :] = False

        if not np.any(tissue_clean):
            continue

        if use_convex_hull:
            ys, xs = np.where(tissue_clean)
            if len(ys) >= 4:
                try:
                    points = np.column_stack((xs, ys))
                    hull = ConvexHull(points)
                    hull_pts = [(float(points[v, 0]), float(points[v, 1])) for v in hull.vertices]
                    img = _PILImage.new("L", (nx, ny), 0)
                    _PILDraw.Draw(img).polygon(hull_pts, outline=1, fill=1)
                    out_mask[z] = np.array(img, dtype=bool)
                    continue
                except Exception:
                    pass

        # Close gap streaks and air gaps between mask and skin
        closed = binary_closing(tissue_clean, structure=selem)
        # Fill all interior holes (airways, sinuses, air gaps inside mask)
        out_mask[z] = binary_fill_holes(closed)

    return out_mask


def determine_external_mask(
    image_kji: np.ndarray,
    geometry: VolumeGeometry,
    rtstruct_path: Optional[str | Path] = None,
    source: str = "rtstruct",
    threshold_hu: float = -350.0,
    closing_radius: int = 5,
    use_convex_hull: bool = False,
    include_mask: bool = True,
    selected_roi_name: Optional[str] = None,
) -> Tuple[np.ndarray, str, Dict[str, Any]]:
    """
    Determines the external body contour mask according to requested source:
    - 'rtstruct': Try planning RTSTRUCT External ROI; fallback to 'auto' if unavailable.
    - 'auto': Robust morphological closing + 2D hole-filling (bridges mask if include_mask=True).
    - 'convex_hull': Robust 2D convex envelope per slice.

    Returns (mask_bool_array, actual_source, metadata_dict).
    """
    mask = None
    actual_source = source

    prefs = [selected_roi_name] if selected_roi_name else (
        ("external", "body", "patient", "skin_surface", "skin") if include_mask
        else ("skin_surface", "skin", "external", "body", "patient")
    )

    if source == "rtstruct" and rtstruct_path:
        mask = get_rtstruct_external_mask(
            rtstruct_path, image_kji.shape, geometry, preferred_roi_names=prefs, include_mask=include_mask
        )
        if mask is not None and mask.any():
            actual_source = "rtstruct"
        else:
            logger.warning(
                f"RTSTRUCT External contour not found at {rtstruct_path}, falling back to auto-contour."
            )
            mask = None

    if mask is None:
        actual_source = "convex_hull" if (source == "convex_hull" or use_convex_hull) else "auto"
        mask = compute_robust_external_mask(
            image_kji,
            threshold_hu=threshold_hu,
            closing_radius=closing_radius,
            use_convex_hull=(actual_source == "convex_hull"),
            include_mask=include_mask,
        )

    info = {
        "source": actual_source,
        "requested_source": source,
        "threshold_hu": float(threshold_hu),
        "closing_radius": int(closing_radius),
        "use_convex_hull": bool(actual_source == "convex_hull"),
        "include_mask": bool(include_mask),
        "selected_roi": selected_roi_name,
        "total_voxels": int(mask.sum()),
        "has_rtstruct": bool(rtstruct_path and Path(rtstruct_path).is_file()),
    }
    return mask, actual_source, info


def save_external_mask(
    mask_path: str | Path,
    mask: np.ndarray,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Saves the boolean external mask and metadata to compressed npz."""
    p = Path(mask_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    meta_json = json.dumps(metadata or {})
    np.savez_compressed(str(p), mask=mask.astype(bool), metadata=np.array(meta_json))
    logger.info(f"Saved external mask to {p} ({mask.sum()} voxels)")


def load_external_mask(
    mask_path: str | Path,
) -> Tuple[Optional[np.ndarray], Dict[str, Any]]:
    """Loads boolean external mask and metadata from npz file."""
    p = Path(mask_path)
    if not p.is_file():
        return None, {}
    try:
        data = np.load(str(p))
        mask = data["mask"].astype(bool)
        meta_str = str(data["metadata"]) if "metadata" in data else "{}"
        metadata = json.loads(meta_str) if meta_str else {}
        return mask, metadata
    except Exception as exc:
        logger.warning(f"Failed to load external mask from {p}: {exc}")
        return None, {}


def apply_external_mask_to_volume(
    volume_kji: np.ndarray,
    external_mask: np.ndarray,
    background_hu: float = -1000.0,
) -> np.ndarray:
    """
    Applies the external contour mask. Voxels outside the external contour
    are forced to background_hu (-1000 HU, air). All voxels inside the contour
    are 100% preserved.
    """
    out = volume_kji.copy()
    outside = ~external_mask
    out[outside] = float(background_hu)
    return out


def compute_cbct_external_mask(
    cbct_arr: np.ndarray,
    cbct_geom: VolumeGeometry,
    plan_arr_shape: Optional[Tuple[int, int, int]] = None,
    plan_geom: Optional[VolumeGeometry] = None,
    rtstruct_path: Optional[str | Path] = None,
    sitk_transform: Optional[Any] = None,
    preferred_roi_names: Optional[Tuple[str, ...] | list[str]] = None,
    threshold_hu: float = -350.0,
    closing_radius: int = 7,
    include_mask: bool = True,
    selected_roi_name: Optional[str] = None,
) -> Tuple[np.ndarray, Dict[str, Any]]:
    """
    Computes external body mask on native CBCT geometry.
    Prioritizes propagating planning RTSTRUCT (External / Skin_Surface) via registration transform.
    Falls back to robust morphological auto-contouring if RTSTRUCT/transform is unavailable.
    """
    prefs = [selected_roi_name] if selected_roi_name else (
        preferred_roi_names if preferred_roi_names else (
            ("external", "body", "patient", "skin_surface", "skin") if include_mask
            else ("skin_surface", "skin", "external", "body", "patient")
        )
    )

    if rtstruct_path and plan_arr_shape and plan_geom and sitk_transform is not None:
        try:
            import SimpleITK as sitk
            from services.imaging.resampling import resample_to_reference

            plan_mask = get_rtstruct_external_mask(
                rtstruct_path,
                plan_arr_shape,
                plan_geom,
                preferred_roi_names=prefs,
                include_mask=include_mask,
            )
            if plan_mask is not None and plan_mask.any():
                inv_transform = sitk_transform.GetInverse()
                cbct_mask = resample_to_reference(
                    moving_array=plan_mask.astype(np.float32),
                    moving_geom=plan_geom,
                    reference_geom=cbct_geom,
                    transform=inv_transform,
                    interpolator=sitk.sitkNearestNeighbor,
                ) > 0
                if cbct_mask.any():
                    logger.info(
                        f"Successfully propagated planning RTSTRUCT onto CBCT grid: {int(cbct_mask.sum())} voxels"
                    )
                    return cbct_mask, {
                        "source": "rtstruct_propagated",
                        "include_mask": bool(include_mask),
                        "selected_roi": selected_roi_name,
                        "num_slices": cbct_arr.shape[0],
                        "total_voxels": int(cbct_mask.sum()),
                    }
        except Exception as exc:
            logger.warning(f"Failed to propagate RTSTRUCT onto CBCT: {exc}")

    # Fallback to robust CBCT auto-contouring
    cbct_mask = compute_robust_external_mask(
        cbct_arr,
        threshold_hu=threshold_hu,
        closing_radius=closing_radius,
        use_convex_hull=False,
        couch_cutoff_fraction=0.92,
        include_mask=include_mask,
    )
    return cbct_mask, {
        "source": "cbct_auto",
        "threshold_hu": threshold_hu,
        "closing_radius": closing_radius,
        "include_mask": bool(include_mask),
        "num_slices": cbct_arr.shape[0],
        "total_voxels": int(cbct_mask.sum()),
    }

