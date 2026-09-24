"""
Gamma analysis orchestration -- Phase 3.

Loads the TPS, MCsquare, and log-reconstructed dose grids for a plan, runs the
configured gamma comparisons, persists GammaResult rows + saved gamma maps,
and stores them as evidence. It does NOT derive the plan verdict:
services/gate.py owns that (see GATE_VERDICT_V1 below).

Per-beam analysis: when MCsquare per-beam doses are available (mc_dose_beam1.npz,
mc_dose_beam2.npz etc.) and matching TPS beam RTDoses are present, gamma is run
per field and results stored with field_name set to the beam name from the plan
and beam_number set to the DICOM BeamNumber (authoritative pairing key for the
frontend -- never rely on row order).

Grid alignment: the MCsquare worker emits dose on the planning CT grid, which is
a different shape/spacing/extent than the (cropped) TPS RTDose grid. The gamma
engine requires identical shapes, so every MCsquare source is resampled onto its
matching TPS grid (trilinear) before comparison -- see _resample_to().

Gamma evaluation grid: the raw dose grids are 2 mm, but a 2 mm DTA on a 2 mm grid
gives the spatial search only ~1 voxel of freedom, which severely under-reports
the passing rate (an artifact of DTA ~= voxel size). To match the validated SDC
(which computes/evaluates on a ~1 mm grid) and standard gamma practice, both dose
planes are upsampled to GAMMA_EVAL_VOXEL_MM (default 1 mm) before the gamma
comparison. Passing rate is the reported quantity and is resolution-consistent.

Comparison thresholds (configurable in config.py):
    mcSquare_vs_TPS : 2.0%  / 2 mm / 95%   (site commissioning; matches SDC)
    log_vs_TPS      : 3.0%  / 2 mm / 90%   (TG-218)
    mcSquare_vs_log : 2.0%  / 2 mm / 90%   (concordance)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pydicom
from sqlalchemy.orm import Session

from config import settings
from dicom.rtdose_parser import find_rtdose_file, find_beam_rtdose_files, load_rtdose
from models.gamma_result import GammaResult
from models.plan import Plan
from models.qa_job import QAJob
from services.dose_grid import DoseGrid
from services.dose_cache import cached_load
from services.gamma_engine import gamma_2d, gamma_3d
from services.job_control import JobCancelled, is_cancelled

logger = logging.getLogger(__name__)


@dataclass
class ComparisonSpec:
    name: str
    reference: str
    evaluation: str
    dd: float
    dta: float
    threshold: float


def _comparison_specs() -> list[ComparisonSpec]:
    return [
        ComparisonSpec(
            "mcSquare_vs_TPS", "tps", "mcSquare",
            settings.GAMMA_MCSQUARE_VS_TPS_DD,
            settings.GAMMA_MCSQUARE_VS_TPS_DTA,
            settings.GAMMA_MCSQUARE_VS_TPS_THRESHOLD,
        ),
        ComparisonSpec(
            "log_vs_TPS", "tps", "log",
            settings.GAMMA_LOG_VS_TPS_DD,
            settings.GAMMA_LOG_VS_TPS_DTA,
            settings.GAMMA_LOG_VS_TPS_THRESHOLD,
        ),
        ComparisonSpec(
            "mcSquare_vs_log", "log", "mcSquare",
            settings.GAMMA_CONCORDANCE_DD,
            settings.GAMMA_CONCORDANCE_DTA,
            settings.GAMMA_CONCORDANCE_THRESHOLD,
        ),
    ]


def _latest_job_result(plan_id: int, job_type: str, db: Session) -> Optional[str]:
    job = (
        db.query(QAJob)
        .filter_by(plan_id=plan_id, job_type=job_type, status="complete")
        .order_by(QAJob.id.desc())
        .first()
    )
    if job and job.result_path and Path(job.result_path).exists():
        return job.result_path
    # Fallback for stopped/cancelled jobs with partial result
    job_partial = (
        db.query(QAJob)
        .filter_by(plan_id=plan_id, job_type=job_type, status="cancelled")
        .order_by(QAJob.id.desc())
        .first()
    )
    if job_partial and job_partial.result_path and Path(job_partial.result_path).exists():
        return job_partial.result_path
    # Fallback for mcSquare: check if mc_dose.npz exists in standard output dir
    if job_type == "mcSquare":
        default_mc = _mc_output_dir(plan_id) / "mc_dose.npz"
        if default_mc.is_file():
            return str(default_mc)
    return None


def _mc_output_dir(plan_id: int) -> Path:
    return Path(settings.RESULTS_PATH) / f"plan_{plan_id}" / "mcSquare_output"


# find_rtdose_file / find_beam_rtdose_files scan every DICOM header in the
# store (~150 CT slices) -- ~1.5 s per call, and load_plan_doses runs on EVERY
# viewer request. Cache the scan per (store, directory mtime); adding a file
# to the store (e.g. an RT record) bumps the dir mtime and invalidates.
_STORE_SCAN_CACHE: dict = {}
_STORE_SCAN_CACHE_MAX = 32


def _scan_store(
    store: str,
    plan_uid: Optional[str] = None,
    valid_beams: Optional[set[int]] = None,
) -> tuple[Optional[str], dict]:
    try:
        valid_tuple = tuple(sorted(valid_beams)) if valid_beams else None
        key = (store, Path(store).stat().st_mtime_ns, plan_uid, valid_tuple)
    except OSError:
        key = (store, 0, plan_uid, None)
    hit = _STORE_SCAN_CACHE.get(key)
    if hit is not None:
        return hit
    rtdose_path = find_rtdose_file(store, plan_uid=plan_uid)
    try:
        beam_rtdoses = find_beam_rtdose_files(store, plan_uid=plan_uid, valid_beam_numbers=valid_beams) or {}
    except Exception:
        beam_rtdoses = {}
    if len(_STORE_SCAN_CACHE) >= _STORE_SCAN_CACHE_MAX:
        _STORE_SCAN_CACHE.pop(next(iter(_STORE_SCAN_CACHE)))
    _STORE_SCAN_CACHE[key] = (rtdose_path, beam_rtdoses)
    return rtdose_path, beam_rtdoses


def _beam_names_from_plan(dicom_store_path: str, plan_uid: Optional[str] = None) -> dict[int, str]:
    """Returns {beam_number: beam_name} from the RT Ion Plan, filtered by plan_uid if given."""
    beam_names: dict[int, str] = {}
    for path in Path(dicom_store_path).glob("*.dcm"):
        try:
            dcm = pydicom.dcmread(str(path), stop_before_pixels=True)
            if str(dcm.get("Modality", "")).upper() not in ("RTPLAN", "RTIBTR"):
                continue
            if plan_uid is not None and str(getattr(dcm, "SOPInstanceUID", "")) != plan_uid:
                continue
            for beam in getattr(dcm, "IonBeamSequence", []):
                num = int(getattr(beam, "BeamNumber", 0))
                name = str(getattr(beam, "BeamName", f"Beam{num}"))
                beam_names[num] = name
            if not beam_names:
                for beam in getattr(dcm, "BeamSequence", []):
                    num = int(getattr(beam, "BeamNumber", 0))
                    name = str(getattr(beam, "BeamName", f"Beam{num}"))
                    beam_names[num] = name
            if beam_names:
                break
        except Exception:
            continue
    return beam_names


_EXTERNAL_MASK_CACHE: dict = {}
_EXTERNAL_MASK_CACHE_MAX = 32


def _external_mask(target: DoseGrid, dicom_store_path: str) -> Optional[np.ndarray]:
    """
    Rasterize the patient External contour onto `target`'s grid. Gamma is
    evaluated INSIDE the patient (TG-218 practice): the TPS reports dose in the
    couch and in open air where MCsquare's Dose_Segmentation zeroes it, so
    scoring those regions structurally fails the comparison without any
    clinical meaning. Returns a bool array (True inside External) or None if no
    External ROI is found. Cached per (store, store mtime, grid geometry).
    """
    from PIL import Image as _PILImage, ImageDraw as _PILDraw

    try:
        store_mtime = Path(dicom_store_path).stat().st_mtime_ns
    except OSError:
        store_mtime = 0
    key = (
        str(dicom_store_path), store_mtime,
        target.shape, tuple(target.spacing), tuple(target.origin),
    )
    if key in _EXTERNAL_MASK_CACHE:
        return _EXTERNAL_MASK_CACHE[key]

    rtstruct = None
    for path in Path(dicom_store_path).rglob("*.dcm"):
        try:
            d = pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
        except Exception:
            continue
        if str(getattr(d, "SOPClassUID", "")) == "1.2.840.10008.5.1.4.1.1.481.3":
            rtstruct = str(path)
            break
    if rtstruct is None:
        if len(_EXTERNAL_MASK_CACHE) >= _EXTERNAL_MASK_CACHE_MAX:
            _EXTERNAL_MASK_CACHE.pop(next(iter(_EXTERNAL_MASK_CACHE)))
        _EXTERNAL_MASK_CACHE[key] = None
        return None

    dcm = pydicom.dcmread(rtstruct, force=True)
    names = {s.ROINumber: str(s.ROIName) for s in getattr(dcm, "StructureSetROISequence", [])}

    candidate_numbers: list[int] = []
    # 1. Prefer interpreted type EXTERNAL from RTROIObservationsSequence
    for obs in getattr(dcm, "RTROIObservationsSequence", []):
        if str(getattr(obs, "RTROIInterpretedType", "")).upper() == "EXTERNAL":
            ref_num = getattr(obs, "ReferencedROINumber", None)
            if ref_num is not None and ref_num not in candidate_numbers:
                candidate_numbers.append(ref_num)

    # 2. Exact match against common body contour names
    for pref in ("external", "body", "patient", "skin_surface", "skin"):
        for num, nm in names.items():
            if nm.strip().lower() == pref and num not in candidate_numbers:
                candidate_numbers.append(num)

    # 3. Fallback to substring matching
    for num, nm in names.items():
        clean = nm.strip().lower()
        if any(k in clean for k in ("external", "body", "patient")) and num not in candidate_numbers:
            candidate_numbers.append(num)

    rc = None
    roi_contours = {
        getattr(r, "ReferencedROINumber", None): r
        for r in getattr(dcm, "ROIContourSequence", [])
    }
    for cand in candidate_numbers:
        r = roi_contours.get(cand)
        if r is not None and getattr(r, "ContourSequence", None):
            rc = r
            break

    if rc is None:
        if len(_EXTERNAL_MASK_CACHE) >= _EXTERNAL_MASK_CACHE_MAX:
            _EXTERNAL_MASK_CACHE.pop(next(iter(_EXTERNAL_MASK_CACHE)))
        _EXTERNAL_MASK_CACHE[key] = None
        return None

    nzv, nyv, nxv = target.shape
    sz, sy, sx = (float(v) for v in target.spacing)
    oz, oy, ox = (float(v) for v in target.origin)
    mask = np.zeros((nzv, nyv, nxv), dtype=bool)
    for dslice in getattr(rc, "ContourSequence", []):
        cd = getattr(dslice, "ContourData", None)
        if cd is None or len(cd) < 3:
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
        from scipy.ndimage import binary_fill_holes
        for z in range(nzv):
            if mask[z].any():
                mask[z] = binary_fill_holes(mask[z])

    if len(_EXTERNAL_MASK_CACHE) >= _EXTERNAL_MASK_CACHE_MAX:
        _EXTERNAL_MASK_CACHE.pop(next(iter(_EXTERNAL_MASK_CACHE)))
    _EXTERNAL_MASK_CACHE[key] = mask if mask.any() else None
    return _EXTERNAL_MASK_CACHE[key]


def _resample_to(src: DoseGrid, target: DoseGrid) -> DoseGrid:
    """
    Resample `src` onto `target`'s grid (shape / spacing / origin), trilinear.

    Conforms CT-grid MCsquare dose onto the (cropped, coarser) TPS RTDose grid so
    the gamma engine -- which requires identical shapes -- can compare them plane
    by plane. All geometry is patient-space mm; axis order is (z, y, x)
    throughout, matching DoseGrid (spacing = (sz, sy, sx), origin = (z0, y0, x0)).
    """
    from scipy.ndimage import map_coordinates

    if (src.shape == target.shape
            and np.allclose(src.spacing, target.spacing)
            and np.allclose(src.origin, target.origin)):
        return src  # already aligned -- nothing to do

    sz, sy, sx = (float(v) for v in src.spacing)
    oz, oy, ox = (float(v) for v in src.origin)
    tz, ty, tx = (float(v) for v in target.spacing)
    poz, poy, pox = (float(v) for v in target.origin)
    nz, ny, nx = target.array.shape

    # Patient-space coordinate (mm) of each TARGET voxel along each axis, then
    # convert to fractional SOURCE index space for interpolation.
    zi = (poz + np.arange(nz) * tz - oz) / sz
    yi = (poy + np.arange(ny) * ty - oy) / sy
    xi = (pox + np.arange(nx) * tx - ox) / sx

    ZI, YI, XI = np.meshgrid(zi, yi, xi, indexing="ij")
    resampled = map_coordinates(
        src.array, [ZI, YI, XI], order=1, mode="constant", cval=0.0
    ).astype(np.float32)
    return DoseGrid(array=resampled, spacing=target.spacing, origin=target.origin)


# Resampling a full CT-grid MC dose (e.g. 142x512x512) onto the TPS grid takes
# seconds of numpy; load_plan_doses runs on EVERY plane request from the dose
# viewer, so without a cache the UI resamples (summed + every beam) per slider
# tick. Cache resampled grids keyed by (source file, mtime, target geometry).
# Resampled grids are TPS-shaped (small), so memory stays modest; simple FIFO
# eviction caps it.
_RESAMPLE_CACHE: dict = {}
_RESAMPLE_CACHE_MAX = 24


def _resample_cached(
    src_path: str,
    src: DoseGrid,
    target: DoseGrid,
    ext_mask: Optional[np.ndarray] = None,
) -> DoseGrid:
    try:
        mtime = Path(src_path).stat().st_mtime_ns
    except OSError:
        mtime = 0
    key = (
        str(src_path), mtime,
        target.shape, tuple(target.spacing), tuple(target.origin),
        ext_mask is not None,
    )
    hit = _RESAMPLE_CACHE.get(key)
    if hit is not None:
        return hit
    out = _resample_to(src, target)
    if ext_mask is not None:
        out = DoseGrid(
            array=np.where(ext_mask, out.array, 0.0).astype(np.float32),
            spacing=out.spacing,
            origin=out.origin,
        )
    if len(_RESAMPLE_CACHE) >= _RESAMPLE_CACHE_MAX:
        _RESAMPLE_CACHE.pop(next(iter(_RESAMPLE_CACHE)))
    _RESAMPLE_CACHE[key] = out
    return out


def _synthesize_composite_dose(beam_doses: dict[int, DoseGrid]) -> Optional[DoseGrid]:
    """
    Synthesizes a composite plan-level DoseGrid by summing individual beam DoseGrids.
    Conforms any differently-shaped beam grids onto the primary reference beam geometry.
    """
    if not beam_doses:
        return None
    sorted_nums = sorted(beam_doses.keys())
    ref = beam_doses[sorted_nums[0]]
    total_array = np.copy(ref.array).astype(np.float32)
    for num in sorted_nums[1:]:
        bg = beam_doses[num]
        if (bg.shape == ref.shape and
                np.allclose(bg.spacing, ref.spacing) and
                np.allclose(bg.origin, ref.origin)):
            total_array += bg.array
        else:
            resampled = _resample_to(bg, ref)
            total_array += resampled.array
    return DoseGrid(array=total_array, spacing=ref.spacing, origin=ref.origin)


def clear_dose_caches() -> None:
    """Flushes scan, mask, and resample caches (e.g. after uploading new dose files)."""
    _STORE_SCAN_CACHE.clear()
    _EXTERNAL_MASK_CACHE.clear()
    _RESAMPLE_CACHE.clear()


def load_plan_doses(plan_id: int, db: Session) -> dict[str, DoseGrid]:
    """
    Returns available dose sources keyed by 'tps', 'mcSquare', 'log', plus
    per-beam sources 'tps_beam{N}' and 'mcSquare_beam{N}' when available.
    Only sources that exist are included.

    MCsquare sources are emitted on the CT grid and are resampled here onto the
    matching TPS grid so downstream comparisons (and the live plane viewer) get
    identical shapes.
    """
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if plan is None:
        raise ValueError(f"Plan {plan_id} not found")

    doses: dict[str, DoseGrid] = {}

    valid_beam_names = _beam_names_from_plan(plan.dicom_store_path, plan_uid=plan.rtplan_uid)
    valid_beams = set(valid_beam_names.keys()) if valid_beam_names else None
    rtdose_path, beam_rtdoses = _scan_store(
        plan.dicom_store_path, plan_uid=plan.rtplan_uid, valid_beams=valid_beams
    )
    if rtdose_path:
        doses["tps"] = cached_load(rtdose_path)

    mc_path = _latest_job_result(plan_id, "mcSquare", db)
    if mc_path:
        doses["mcSquare"] = cached_load(mc_path)

    # The clinical log_reconstruction job returns its OUTPUT DIRECTORY
    # (log_output/) with per-beam 150x150 water-plane reconstructions, NOT a
    # single TPS-grid dose file. Its gamma (log_vs_Rx) is computed and stored
    # by the log job itself, so it must NOT be loaded here for the TPS-grid
    # comparison. Only load if the result is an actual dose FILE, never a dir.
    log_path = _latest_job_result(plan_id, "log_reconstruction", db)
    if log_path and Path(log_path).is_file():
        try:
            doses["log"] = cached_load(log_path)
        except Exception as exc:
            logger.debug(f"Skipping log dose load for plan {plan_id}: {exc}")

    # --- Per-beam TPS sources (beam-level RTDose, from the cached scan) ---
    for beam_num, path in (beam_rtdoses or {}).items():
        try:
            doses[f"tps_beam{beam_num}"] = cached_load(path)
        except Exception:
            continue

    # If plan-level RTDOSE was not found, but all plan beams have individual RTDOSE files:
    # Synthesize composite TPS dose by summing all beam dose grids.
    n_fields = getattr(plan, "number_of_fields", None)
    has_valid_n_fields = isinstance(n_fields, int) and n_fields > 0
    expected_beams = valid_beams or (set((beam_rtdoses or {}).keys()) if (has_valid_n_fields and len(beam_rtdoses or {}) >= n_fields) else None)
    if "tps" not in doses and expected_beams and expected_beams.issubset(set((beam_rtdoses or {}).keys())):
        beam_dose_grids = {b: doses[f"tps_beam{b}"] for b in expected_beams if f"tps_beam{b}" in doses}
        if len(beam_dose_grids) == len(expected_beams):
            synth_dose = _synthesize_composite_dose(beam_dose_grids)
            if synth_dose is not None:
                doses["tps"] = synth_dose
                logger.info(
                    f"Plan {plan_id}: Plan-level RTDose file was missing; synthesized composite TPS dose from {len(expected_beams)} beam dose grids."
                )

    # --- Per-beam MCsquare sources (mc_dose_beam{N}.npz) ---
    mc_dir = _mc_output_dir(plan_id)
    mc_beam_paths: dict[str, str] = {}
    if mc_dir.exists():
        for npz in sorted(mc_dir.glob("mc_dose_beam*.npz")):
            try:
                beam_num = int(npz.stem.rsplit("beam", 1)[1])
            except (IndexError, ValueError):
                continue
            try:
                key = f"mcSquare_beam{beam_num}"
                doses[key] = cached_load(str(npz))
                mc_beam_paths[key] = str(npz)
            except Exception:
                continue

    # --- Conform CT-grid MCsquare doses onto the matching TPS grid ---
    # The gamma engine requires identical shapes; the worker emits MC dose on the
    # CT grid while the TPS RTDose is a different, cropped grid. Resample MC->TPS.
    # Mask both MCsquare and TPS doses to the external patient contour (TG-218)
    # so dose displays and gamma comparisons evaluate strictly within the patient,
    # zeroing couch and air scatter outside the body.
    # Uses the resample cache: this function runs per viewer request, and
    # uncached resampling of CT-grid volumes is what made the UI lag.
    ext_tps = _external_mask(doses["tps"], plan.dicom_store_path) if "tps" in doses else None
    if "tps" in doses and ext_tps is not None:
        doses["tps"] = DoseGrid(
            array=np.where(ext_tps, doses["tps"].array, 0.0).astype(np.float32),
            spacing=doses["tps"].spacing,
            origin=doses["tps"].origin,
        )
    if "tps" in doses and "mcSquare" in doses:
        doses["mcSquare"] = _resample_cached(
            mc_path, doses["mcSquare"], doses["tps"], ext_mask=ext_tps
        )
    for key in [k for k in doses if k.startswith("mcSquare_beam")]:
        beam_num = key.rsplit("beam", 1)[1]
        tps_key = f"tps_beam{beam_num}"
        target = doses.get(tps_key) or doses.get("tps")
        if target is not None:
            ext_target = (
                ext_tps
                if target is doses.get("tps")
                else _external_mask(target, plan.dicom_store_path)
            )
            doses[key] = _resample_cached(
                mc_beam_paths.get(key, key), doses[key], target, ext_mask=ext_target
            )
    for key in [k for k in doses if k.startswith("tps_beam")]:
        ext_beam = (
            ext_tps
            if (
                "tps" in doses
                and doses[key].shape == doses["tps"].shape
                and np.allclose(doses[key].origin, doses["tps"].origin)
                and np.allclose(doses[key].spacing, doses["tps"].spacing)
            )
            else _external_mask(doses[key], plan.dicom_store_path)
        )
        if ext_beam is not None:
            doses[key] = DoseGrid(
                array=np.where(ext_beam, doses[key].array, 0.0).astype(np.float32),
                spacing=doses[key].spacing,
                origin=doses[key].origin,
            )

    return doses


def _in_plane_voxel_mm(grid: DoseGrid) -> float:
    return float(grid.spacing[1])


def _upsample_plane(plane: np.ndarray, factor: int) -> np.ndarray:
    """
    Bilinearly upsample a 2D dose plane by an integer factor. Uses scipy if
    available, otherwise falls back to a pure-numpy bilinear interpolation.
    """
    if factor <= 1:
        return plane
    try:
        from scipy.ndimage import zoom
        return zoom(plane, factor, order=1)
    except Exception:
        # Pure-numpy bilinear fallback
        r, c = plane.shape
        new_r, new_c = r * factor, c * factor
        ri = np.linspace(0, r - 1, new_r)
        ci = np.linspace(0, c - 1, new_c)
        r0 = np.floor(ri).astype(int); r1 = np.minimum(r0 + 1, r - 1)
        c0 = np.floor(ci).astype(int); c1 = np.minimum(c0 + 1, c - 1)
        wr = (ri - r0)[:, None]; wc = (ci - c0)[None, :]
        top = plane[r0][:, c0] * (1 - wc) + plane[r0][:, c1] * wc
        bot = plane[r1][:, c0] * (1 - wc) + plane[r1][:, c1] * wc
        return top * (1 - wr) + bot * wr


def _gamma_eval(
    ref_plane: np.ndarray, ev_plane: np.ndarray,
    dd: float, dta: float, voxel_mm: float,
) -> tuple[np.ndarray, float]:
    """
    Runs gamma_2d on an upsampled grid so the DTA search is not crippled by
    coarse voxels (DTA ~= voxel size). Both planes are upsampled to
    ~GAMMA_EVAL_VOXEL_MM before comparison. The returned gamma map is at the
    evaluation (fine) resolution; the passing rate is resolution-consistent.
    """
    target_mm = float(getattr(settings, "GAMMA_EVAL_VOXEL_MM", 1.0))
    factor = max(1, int(round(voxel_mm / target_mm)))
    if factor <= 1:
        return gamma_2d(
            ref_plane, ev_plane,
            dd_percent=dd, dta_mm=dta,
            voxel_size_mm=voxel_mm,
            dose_threshold_percent=settings.DOSE_THRESHOLD_PERCENT,
        )
    ref_up = _upsample_plane(ref_plane, factor)
    ev_up = _upsample_plane(ev_plane, factor)
    eval_voxel_mm = voxel_mm / factor
    return gamma_2d(
        ref_up, ev_up,
        dd_percent=dd, dta_mm=dta,
        voxel_size_mm=eval_voxel_mm,
        dose_threshold_percent=settings.DOSE_THRESHOLD_PERCENT,
    )


def _gamma_eval_3d(
    ref_grid: DoseGrid, ev_grid: DoseGrid, dd: float, dta: float,
) -> tuple[np.ndarray, float, int, float]:
    """
    Volumetric (3D) gamma for a MC-vs-TPS dose pair on a shared grid.

    A single axial plane is unrepresentative for angled proton fields (the beam
    deposits dose along its own axis, so per-beam MC and TPS peak on different
    axial slices -- see the LA 30 mm max-plane offset). 3D gamma removes that
    artifact. For speed we crop to the high-dose bounding box (+ DTA-search
    margin) and upsample toward GAMMA_EVAL_VOXEL_MM before the search; the
    passing rate is unchanged by cropping because it is scored only over
    above-threshold reference voxels, which all lie inside the crop.

    Returns (representative_2d_gamma_map, passing_rate, plane_index, voxel_mm)
    where the map is the axial slice through the hottest cropped plane, kept for
    the viewer/DB contract; the passing_rate is the true 3D value.
    """
    from scipy.ndimage import zoom

    ref = ref_grid.array.astype(np.float32)
    ev = ev_grid.array.astype(np.float32)
    spacing = tuple(float(s) for s in ref_grid.spacing)  # (sz, sy, sx)
    thr_pct = float(getattr(settings, "DOSE_THRESHOLD_PERCENT", 10.0))

    ref_max = float(ref.max()) if ref.size else 0.0
    if ref_max <= 0:
        return np.full(ref.shape[1:], np.nan, np.float32), 0.0, 0, spacing[1]

    mask = ref >= (thr_pct / 100.0) * ref_max
    if not mask.any():
        return np.full(ref.shape[1:], np.nan, np.float32), 0.0, 0, spacing[1]

    # --- crop to high-dose bbox + margin (>= search radius so DTA can reach) ---
    max_dist = 3.0 * dta
    marg = [int(np.ceil(max_dist / s)) + 2 for s in spacing]
    idx = np.where(mask)
    z0 = max(0, int(idx[0].min()) - marg[0]); z1 = min(ref.shape[0], int(idx[0].max()) + marg[0] + 1)
    y0 = max(0, int(idx[1].min()) - marg[1]); y1 = min(ref.shape[1], int(idx[1].max()) + marg[1] + 1)
    x0 = max(0, int(idx[2].min()) - marg[2]); x1 = min(ref.shape[2], int(idx[2].max()) + marg[2] + 1)
    ref_c = ref[z0:z1, y0:y1, x0:x1]
    ev_c = ev[z0:z1, y0:y1, x0:x1]

    # --- upsample toward target voxel size (bounded so runtime stays sane) ---
    target_mm = float(getattr(settings, "GAMMA_EVAL_VOXEL_MM", 1.0))
    zoom_f = [s / target_mm for s in spacing]
    est_voxels = ref_c.size * float(np.prod(zoom_f))
    VOXEL_CAP = 8_000_000  # keep the 3D search tractable in pure numpy
    if est_voxels > VOXEL_CAP:
        scale = (VOXEL_CAP / est_voxels) ** (1.0 / 3.0)
        zoom_f = [max(1.0, f * scale) for f in zoom_f]
    if any(abs(f - 1.0) > 1e-3 for f in zoom_f):
        ref_c = zoom(ref_c, zoom_f, order=1)
        ev_c = zoom(ev_c, zoom_f, order=1)
        vox = tuple(spacing[i] / zoom_f[i] for i in range(3))
    else:
        vox = spacing

    gvol, rate = gamma_3d(
        ref_c, ev_c, dd_percent=dd, dta_mm=dta,
        voxel_size_mm=vox, dose_threshold_percent=thr_pct,
    )

    # Representative slice for the viewer: hottest axial plane in the crop.
    plane_max = ref_c.reshape(ref_c.shape[0], -1).max(axis=1)
    zc = int(np.argmax(plane_max))
    return gvol[zc].astype(np.float32), rate, zc, float(vox[1])


def _gamma_map_path(
    plan_id: int, comparison: str,
    fraction_number: Optional[int] = None,
    beam_number: Optional[int] = None,
) -> Path:
    parts = [comparison]
    if beam_number is not None:
        parts.append(f"beam{beam_number}")
    if fraction_number is not None:
        parts.append(f"fx{fraction_number}")
    suffix = "_".join(parts)
    return Path(settings.RESULTS_PATH) / f"plan_{plan_id}" / "gamma" / f"{suffix}.npz"


def save_gamma_map(
    path: Path, gamma_map: np.ndarray, plane_index: int, voxel_mm: float,
    passing_rate: float, dd: float, dta: float,
) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        path,
        gamma_map=gamma_map.astype(np.float32),
        plane_index=np.int32(plane_index),
        voxel_mm=np.float32(voxel_mm),
        passing_rate=np.float32(passing_rate),
        dd=np.float32(dd),
        dta=np.float32(dta),
    )
    return str(path)


def compute_gamma_plane(
    plan_id: int, comparison: str, z_index: int, db: Session
) -> tuple[np.ndarray, float]:
    """
    Live gamma computation for a single plane (used by the dose viewer overlay).
    """
    spec = next((s for s in _comparison_specs() if s.name == comparison), None)
    if spec is None:
        raise ValueError(f"Unknown comparison: {comparison}")

    doses = load_plan_doses(plan_id, db)
    if spec.reference not in doses or spec.evaluation not in doses:
        raise FileNotFoundError(
            f"Doses for {comparison} not available "
            f"(need {spec.reference} and {spec.evaluation})."
        )

    ref = doses[spec.reference]
    ev = doses[spec.evaluation]
    z = max(0, min(z_index, ref.shape[0] - 1))
    return _gamma_eval(
        ref.plane(z), ev.plane(z),
        dd=spec.dd, dta=spec.dta,
        voxel_mm=_in_plane_voxel_mm(ref),
    )


# GATE_VERDICT_V1 -- compute_verdict() removed.
#
# It wrote plan.qa_status in its own vocabulary (pass / flagged /
# measure_needed) while services/gate.py independently computed a GateStatus
# (cleared / verified / investigate / escalate / measure / incomplete) and
# persisted nothing. The two disagreed: plan 3 read "pass" here while the gate
# read "incomplete" because its secondary dose was scored off-criterion, and
# plan 8 read "pending" while the gate read "verified".
#
# Gamma results are EVIDENCE. The gate is the only thing that turns evidence
# into a verdict, and it is now the only writer of plan.qa_status
# (services/pipeline.py::_persist_gate).


def run_gamma_analysis(
    plan_id: int, db: Session, job_id: Optional[int] = None,
    fraction_number: Optional[int] = None,
) -> dict:
    """
    Runs gamma comparisons for all available dose pairs.

    For MCsquare vs TPS: runs per-beam when per-beam MCsquare doses are available
    (mc_dose_beam{N}.npz) and matching TPS beam RTDoses exist. Falls back to
    summed dose comparison if per-beam data is unavailable OR if the per-beam
    sets share no common beam numbers (previously an empty intersection wrote
    zero rows and silently counted as a pass).

    All beams must pass for the mcSquare_vs_TPS comparison to count as passed.
    """
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if plan is None:
        raise ValueError(f"Plan {plan_id} not found")

    doses = load_plan_doses(plan_id, db)
    if "tps" not in doses:
        raise FileNotFoundError("TPS RTDose unavailable -- cannot run gamma analysis.")

    specs = _comparison_specs()
    runnable = [s for s in specs if s.reference in doses and s.evaluation in doses]
    if fraction_number is not None:
        runnable = [s for s in runnable if "log" in (s.reference, s.evaluation)]
    if not runnable:
        raise FileNotFoundError(
            "No dose pairs available. Run MCsquare and/or log reconstruction first."
        )

    # Clear prior gamma results for this scope
    if fraction_number is None:
        db.query(GammaResult).filter(
            GammaResult.plan_id == plan_id,
            GammaResult.fraction_number.is_(None),
        ).delete(synchronize_session=False)
    else:
        db.query(GammaResult).filter(
            GammaResult.plan_id == plan_id,
            GammaResult.fraction_number == fraction_number,
        ).delete(synchronize_session=False)
    db.commit()

    passed_map: dict[str, bool] = {}
    results_summary = []

    # Check for per-beam MCsquare doses
    mc_output_dir = _mc_output_dir(plan_id)
    beam_mc_doses: dict[int, DoseGrid] = {}
    for npz in sorted(mc_output_dir.glob("mc_dose_beam*.npz")):
        try:
            beam_num = int(npz.stem.replace("mc_dose_beam", ""))
            beam_mc_doses[beam_num] = DoseGrid.load(str(npz))
        except Exception as e:
            logger.warning(f"Could not load {npz}: {e}")

    beam_names = _beam_names_from_plan(plan.dicom_store_path, plan_uid=plan.rtplan_uid)
    valid_beams = set(beam_names.keys()) if beam_names else None
    beam_tps_doses = find_beam_rtdose_files(
        plan.dicom_store_path, plan_uid=plan.rtplan_uid, valid_beam_numbers=valid_beams
    )

    # Per-beam mode requires a NON-EMPTY intersection of beam numbers. An empty
    # intersection previously produced zero rows and all([]) == True (silent
    # pass) -- now it logs and falls back to the summed comparison instead.
    common_beams = set(beam_mc_doses.keys()) & set((beam_tps_doses or {}).keys())
    if valid_beams:
        common_beams = common_beams & valid_beams
    has_per_beam = bool(common_beams)
    if (beam_mc_doses or beam_tps_doses) and not has_per_beam:
        logger.warning(
            f"Plan {plan_id}: per-beam doses present but no common beam numbers "
            f"(MC: {sorted(beam_mc_doses)}, TPS: {sorted((beam_tps_doses or {}).keys())}) "
            f"-- falling back to summed-dose comparison."
        )

    total_steps = len(runnable)
    step = 0

    for spec in runnable:
        if job_id is not None and is_cancelled(job_id):
            raise JobCancelled()

        if spec.name == "mcSquare_vs_TPS" and has_per_beam and fraction_number is None:
            # Per-beam gamma analysis
            beam_results: list[tuple[int, float, bool]] = []

            for beam_num in sorted(common_beams):
                tps_beam = load_rtdose(beam_tps_doses[beam_num])
                # Score gamma inside the patient only (TG-218): zero the
                # reference outside External so couch/air voxels -- where the
                # TPS reports dose but MC's Dose_Segmentation zeroes it -- fall
                # below the dose threshold and are excluded.
                ext = _external_mask(tps_beam, plan.dicom_store_path)
                if ext is not None:
                    tps_beam = DoseGrid(
                        array=np.where(ext, tps_beam.array, 0.0).astype(np.float32),
                        spacing=tps_beam.spacing, origin=tps_beam.origin,
                    )
                # MC beam dose is on the CT grid -- conform it to this beam's TPS
                # grid so shapes match and the z index refers to the same plane.
                mc_beam = _resample_to(beam_mc_doses[beam_num], tps_beam)
                if ext is not None:
                    mc_beam = DoseGrid(
                        array=np.where(ext, mc_beam.array, 0.0).astype(np.float32),
                        spacing=mc_beam.spacing, origin=mc_beam.origin,
                    )
                field_name = beam_names.get(beam_num, f"Beam {beam_num}")

                # Volumetric gamma -- a single axial plane is unrepresentative
                # for angled proton fields (MC/TPS peak on different slices).
                gamma_map, passing_rate, z, voxel_mm = _gamma_eval_3d(
                    tps_beam, mc_beam, dd=spec.dd, dta=spec.dta,
                )
                passed = passing_rate >= spec.threshold
                beam_results.append((beam_num, passing_rate, passed))

                map_path = save_gamma_map(
                    _gamma_map_path(plan_id, spec.name, fraction_number, beam_num),
                    gamma_map, z, voxel_mm, passing_rate, spec.dd, spec.dta,
                )

                row = GammaResult(
                    plan_id=plan_id,
                    fraction_number=fraction_number,
                    field_name=field_name,
                    beam_number=beam_num,
                    comparison_type=spec.name,
                    dd_percent=spec.dd,
                    dta_mm=spec.dta,
                    passing_rate=round(passing_rate, 2),
                    threshold=spec.threshold,
                    passed=passed,
                    gamma_map_path=map_path,
                )
                db.add(row)

                results_summary.append({
                    "comparison_type": spec.name,
                    "field_name": field_name,
                    "beam_number": beam_num,
                    "passing_rate": round(passing_rate, 2),
                    "threshold": spec.threshold,
                    "passed": passed,
                    "plane_index": z,
                })
                logger.info(
                    f"Beam {beam_num} ({field_name}): "
                    f"{passing_rate:.1f}% @ {spec.dd}%/{spec.dta}mm "
                    f"({'PASS' if passed else 'FAIL'})"
                )

            # Evaluate overall composite 3D gamma (summed MC vs summed/plan TPS)
            comp_passed = True
            comp_pr = 0.0
            if spec.reference in doses and spec.evaluation in doses:
                ref = doses[spec.reference]
                ev = doses[spec.evaluation]
                if ev.shape != ref.shape:
                    ev = _resample_to(ev, ref)

                ext = _external_mask(ref, plan.dicom_store_path)
                if ext is not None:
                    ref = DoseGrid(
                        array=np.where(ext, ref.array, 0.0).astype(np.float32),
                        spacing=ref.spacing, origin=ref.origin,
                    )
                    ev = DoseGrid(
                        array=np.where(ext, ev.array, 0.0).astype(np.float32),
                        spacing=ev.spacing, origin=ev.origin,
                    )

                comp_gamma_map, comp_pr, comp_z, comp_voxel_mm = _gamma_eval_3d(
                    ref, ev, dd=spec.dd, dta=spec.dta,
                )
                comp_passed = comp_pr >= spec.threshold
                comp_map_path = save_gamma_map(
                    _gamma_map_path(plan_id, spec.name, fraction_number),
                    comp_gamma_map, comp_z, comp_voxel_mm, comp_pr, spec.dd, spec.dta,
                )

                comp_row = GammaResult(
                    plan_id=plan_id,
                    fraction_number=fraction_number,
                    field_name="Composite",
                    beam_number=None,
                    comparison_type=spec.name,
                    dd_percent=spec.dd,
                    dta_mm=spec.dta,
                    passing_rate=round(comp_pr, 2),
                    threshold=spec.threshold,
                    passed=comp_passed,
                    gamma_map_path=comp_map_path,
                )
                db.add(comp_row)
                results_summary.append({
                    "comparison_type": spec.name,
                    "field_name": "Composite",
                    "passing_rate": round(comp_pr, 2),
                    "threshold": spec.threshold,
                    "passed": comp_passed,
                    "plane_index": comp_z,
                })
                logger.info(
                    f"mcSquare_vs_TPS 3D composite: {comp_pr:.1f}% @ {spec.dd}%/{spec.dta}mm "
                    f"({'PASS' if comp_passed else 'FAIL'})"
                )

            # All beams and composite must pass for the overall comparison to pass
            all_passed = all(p for _, _, p in beam_results) and comp_passed
            mean_pr = np.mean([pr for _, pr, _ in beam_results]) if beam_results else 0.0
            passed_map[spec.name] = all_passed
            logger.info(
                f"mcSquare_vs_TPS overall: composite={comp_pr:.1f}%, beam_mean={mean_pr:.1f}% "
                f"({'ALL PASS' if all_passed else 'SOME FAIL'})"
            )

        else:
            # Summed dose comparison (fallback or non-mcSquare specs).
            ref = doses[spec.reference]
            ev = doses[spec.evaluation]
            # Defensive: conform evaluation onto the reference grid if they differ
            # (e.g. mcSquare_vs_log where log is on yet another grid).
            if ev.shape != ref.shape:
                ev = _resample_to(ev, ref)

            if spec.name == "mcSquare_vs_TPS":
                # Score inside the patient only (see per-beam branch).
                ext = _external_mask(ref, plan.dicom_store_path)
                if ext is not None:
                    ref = DoseGrid(
                        array=np.where(ext, ref.array, 0.0).astype(np.float32),
                        spacing=ref.spacing, origin=ref.origin,
                    )
                    ev = DoseGrid(
                        array=np.where(ext, ev.array, 0.0).astype(np.float32),
                        spacing=ev.spacing, origin=ev.origin,
                    )
                # Volumetric gamma for the summed MC-vs-TPS fallback too.
                gamma_map, passing_rate, z, voxel_mm = _gamma_eval_3d(
                    ref, ev, dd=spec.dd, dta=spec.dta,
                )
            else:
                z = ref.max_dose_plane_index()
                voxel_mm = _in_plane_voxel_mm(ref)
                gamma_map, passing_rate = _gamma_eval(
                    ref.plane(z), ev.plane(z),
                    dd=spec.dd, dta=spec.dta, voxel_mm=voxel_mm,
                )
            passed = passing_rate >= spec.threshold
            passed_map[spec.name] = passed

            map_path = save_gamma_map(
                _gamma_map_path(plan_id, spec.name, fraction_number),
                gamma_map, z, voxel_mm, passing_rate, spec.dd, spec.dta,
            )

            row = GammaResult(
                plan_id=plan_id,
                fraction_number=fraction_number,
                field_name="Composite",
                beam_number=None,
                comparison_type=spec.name,
                dd_percent=spec.dd,
                dta_mm=spec.dta,
                passing_rate=round(passing_rate, 2),
                threshold=spec.threshold,
                passed=passed,
                gamma_map_path=map_path,
            )
            db.add(row)
            results_summary.append({
                "comparison_type": spec.name,
                "field_name": "Composite",
                "passing_rate": round(passing_rate, 2),
                "threshold": spec.threshold,
                "passed": passed,
                "plane_index": z,
            })

        step += 1
        if job_id is not None:
            from services.mcSquare_runner import _update_progress
            _update_progress(db, job_id, step / total_steps * 0.95)

    db.commit()

    # NOTE: compute_verdict removed. The gate (services/gate.py) consumes
    # these rows and decides; see services/pipeline.py::_persist_gate.

    if job_id is not None:
        from services.mcSquare_runner import _update_progress
        _update_progress(db, job_id, 1.0)

    n_pass = sum(1 for v in passed_map.values() if v)
    logger.info(f"Gamma analysis for plan {plan_id} -> "
                f"{n_pass}/{len(passed_map)} comparison(s) passed")
    return {"passed_by_comparison": dict(passed_map),
            "results": results_summary}


def ensure_composite_gamma(plan_id: int, db: Session) -> Optional[GammaResult]:
    """
    Ensures that a 3D composite GammaResult (field_name='Composite', beam_number=None)
    exists for mcSquare_vs_TPS pre-treatment QA. If per-beam results exist but the
    composite evaluation was never written, computes and persists it on the fly.
    """
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if plan is None:
        return None

    # Check if composite already exists
    existing = (
        db.query(GammaResult)
        .filter(
            GammaResult.plan_id == plan_id,
            GammaResult.comparison_type == "mcSquare_vs_TPS",
            GammaResult.fraction_number.is_(None),
            (GammaResult.field_name == "Composite") | (GammaResult.beam_number.is_(None)),
        )
        .first()
    )
    if existing:
        return existing

    # Check if we have mcSquare_vs_TPS per-beam results
    beam_results = (
        db.query(GammaResult)
        .filter(
            GammaResult.plan_id == plan_id,
            GammaResult.comparison_type == "mcSquare_vs_TPS",
            GammaResult.fraction_number.is_(None),
            GammaResult.beam_number.isnot(None),
        )
        .all()
    )
    if not beam_results:
        return None

    try:
        doses = load_plan_doses(plan_id, db)
        if "tps" not in doses or "mcSquare" not in doses:
            return None

        spec = next((s for s in _comparison_specs() if s.name == "mcSquare_vs_TPS"), None)
        if not spec:
            return None

        ref = doses["tps"]
        ev = doses["mcSquare"]
        if ev.shape != ref.shape:
            ev = _resample_to(ev, ref)

        ext = _external_mask(ref, plan.dicom_store_path)
        if ext is not None:
            ref = DoseGrid(
                array=np.where(ext, ref.array, 0.0).astype(np.float32),
                spacing=ref.spacing, origin=ref.origin,
            )
            ev = DoseGrid(
                array=np.where(ext, ev.array, 0.0).astype(np.float32),
                spacing=ev.spacing, origin=ev.origin,
            )

        comp_gamma_map, comp_pr, comp_z, comp_voxel_mm = _gamma_eval_3d(
            ref, ev, dd=spec.dd, dta=spec.dta,
        )
        comp_passed = comp_pr >= spec.threshold
        comp_map_path = save_gamma_map(
            _gamma_map_path(plan_id, spec.name, None),
            comp_gamma_map, comp_z, comp_voxel_mm, comp_pr, spec.dd, spec.dta,
        )

        comp_row = GammaResult(
            plan_id=plan_id,
            fraction_number=None,
            field_name="Composite",
            beam_number=None,
            comparison_type=spec.name,
            dd_percent=spec.dd,
            dta_mm=spec.dta,
            passing_rate=round(comp_pr, 2),
            threshold=spec.threshold,
            passed=comp_passed,
            gamma_map_path=comp_map_path,
        )
        db.add(comp_row)
        db.commit()
        db.refresh(comp_row)
        logger.info(
            f"Plan {plan_id}: Auto-computed and persisted missing 3D composite gamma: {comp_pr:.1f}%"
        )
        return comp_row
    except Exception as exc:
        logger.warning(f"Plan {plan_id}: ensure_composite_gamma failed: {exc}")
        return None



def check_plan_dose_status(plan_id: int, db: Session) -> dict:
    """
    Checks the completeness of TPS RTDOSE files for a plan:
    - Verifies whether a PLAN-level RTDOSE exists
    - Verifies whether each expected beam has an individual RTDOSE
    - Detects whether composite dose is synthesized from beam doses
    - Identifies missing plan dose and missing beam numbers/names
    - Provides actionable warning messages for the user
    """
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if plan is None:
        raise ValueError(f"Plan {plan_id} not found")

    beam_names = _beam_names_from_plan(plan.dicom_store_path, plan_uid=plan.rtplan_uid)
    valid_beams = set(beam_names.keys()) if beam_names else None

    rtdose_path, beam_rtdoses = _scan_store(
        plan.dicom_store_path, plan_uid=plan.rtplan_uid, valid_beams=valid_beams
    )

    has_plan_dose = bool(rtdose_path)
    plan_dose_file = Path(rtdose_path).name if rtdose_path else None

    n_fields = getattr(plan, "number_of_fields", None)
    if not beam_names and isinstance(n_fields, int) and n_fields > 0:
        beam_names = {i: f"Beam {i}" for i in range(1, n_fields + 1)}

    expected_beams = []
    missing_beam_numbers = []
    missing_beam_names = []

    for b_num in sorted(beam_names.keys()):
        b_name = beam_names[b_num]
        b_path = (beam_rtdoses or {}).get(b_num)
        has_b_dose = bool(b_path)
        dose_filename = Path(b_path).name if b_path else None
        expected_beams.append({
            "beam_number": b_num,
            "beam_name": b_name,
            "has_dose": has_b_dose,
            "has_rtdose": has_b_dose,
            "dose_file": dose_filename,
            "file_name": dose_filename,
        })
        if not has_b_dose:
            missing_beam_numbers.append(b_num)
            missing_beam_names.append(b_name)

    total_expected = len(expected_beams)
    present_beams_count = total_expected - len(missing_beam_numbers)
    all_beams_present = (total_expected > 0 and len(missing_beam_numbers) == 0)

    is_plan_dose_synthesized = (not has_plan_dose) and all_beams_present
    missing_plan_dose = (not has_plan_dose) and (not all_beams_present)

    warnings: list[str] = []
    if is_plan_dose_synthesized:
        warnings.append(
            f"Plan-level RTDOSE was not found in the DICOM export; a composite reference dose was automatically synthesized by summing all {total_expected} beam doses."
        )
    elif missing_plan_dose:
        warnings.append(
            "Missing total plan RTDOSE file (DoseSummationType=PLAN). Upload the composite plan dose file to enable full plan gamma analysis."
        )

    if missing_beam_numbers:
        missing_str = ", ".join(f"Beam {n} ({beam_names.get(n, n)})" for n in missing_beam_numbers)
        warnings.append(
            f"Missing individual RTDOSE file(s) for: {missing_str}. Field-by-field gamma evaluation requires per-beam RTDOSE files."
        )

    status = "complete"
    if missing_plan_dose and missing_beam_numbers:
        status = "missing_files"
    elif missing_plan_dose:
        status = "missing_plan_dose"
    elif missing_beam_numbers:
        status = "partial_beams"
    elif is_plan_dose_synthesized:
        status = "synthesized"

    return {
        "plan_id": plan_id,
        "plan_label": plan.plan_label,
        "number_of_fields": plan.number_of_fields,
        "has_plan_dose": has_plan_dose,
        "plan_dose_file": plan_dose_file,
        "is_plan_dose_synthesized": is_plan_dose_synthesized,
        "expected_beams": expected_beams,
        "all_beams_present": all_beams_present,
        "present_beams_count": present_beams_count,
        "total_beams_count": total_expected,
        "missing_plan_dose": missing_plan_dose,
        "missing_beam_numbers": missing_beam_numbers,
        "missing_beam_names": missing_beam_names,
        "status": status,
        "warnings": warnings,
    }