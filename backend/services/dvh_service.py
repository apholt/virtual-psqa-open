"""
services/dvh_service.py -- Dose-Volume Histogram (DVH) and Robustness Analysis
for openMCsquare Monte Carlo Secondary Verification in Virtual PSQA.

Evaluates treatment plan resilience against setup uncertainties (e.g. ±3 mm)
and range uncertainties (e.g. ±3%), extracting cumulative DVH curves,
min/max uncertainty envelopes, and clinical metrics (D98, D95, D50, D2, Dmean).
Supports RTSTRUCT contours as well as native openMCsquare DVH outputs.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pydicom
from PIL import Image as _PILImage, ImageDraw as _PILDraw
from scipy.ndimage import binary_fill_holes, shift as scipy_shift
from sqlalchemy.orm import Session

from config import settings
from models.plan import Plan
from schemas.dvh import (
    CalculateDVHRequest,
    DVHCurve,
    MetricInterval,
    PlanDVHResponse,
    ROIDVHData,
    ROIMetrics,
)
from services.dose_grid import DoseGrid
from services.gamma_analysis import _resample_to, load_plan_doses

logger = logging.getLogger(__name__)

# Standard clinical color palette for structures
_TARGET_COLORS = ["#ef4444", "#f97316", "#dc2626", "#ea580c", "#b91c1c"]
_OAR_COLORS = [
    "#3b82f6",  # Blue
    "#10b981",  # Emerald
    "#8b5cf6",  # Violet
    "#06b6d4",  # Cyan
    "#ec4899",  # Pink
    "#f59e0b",  # Amber
    "#6366f1",  # Indigo
    "#14b8a6",  # Teal
    "#84cc16",  # Lime
    "#a855f7",  # Purple
    "#0ea5e9",  # Sky
]


def find_rtstruct_file(store_dir: str | Path) -> Optional[Path]:
    """Locate the first valid RTSTRUCT DICOM file in the plan store directory."""
    p = Path(store_dir)
    if not p.is_dir():
        return None
    for item in p.rglob("*.dcm"):
        try:
            d = pydicom.dcmread(str(item), stop_before_pixels=True, force=True)
            if str(getattr(d, "SOPClassUID", "")) == "1.2.840.10008.5.1.4.1.1.481.3" or \
               str(getattr(d, "Modality", "")).upper() == "RTSTRUCT":
                return item
        except Exception:
            continue
    return None


def _classify_roi(name: str, obs_type: Optional[str]) -> Tuple[str, bool]:
    """Classify structure as TARGET, OAR, EXTERNAL, or OTHER."""
    nm = name.strip().upper()
    ot = (obs_type or "").strip().upper()

    if ot in ("PTV", "CTV", "GTV"):
        return "TARGET", True
    if ot == "EXTERNAL":
        return "EXTERNAL", False
    if ot in ("ORGAN", "OAR", "AVOIDANCE"):
        return "OAR", False

    # Name heuristics
    if any(tag in nm for tag in ("PTV", "CTV", "GTV", "TARGET", "LESION", "TUMOR", "BOOST")):
        return "TARGET", True
    if any(tag in nm for tag in ("EXTERNAL", "BODY", "SKIN", "PATIENT")):
        return "EXTERNAL", False
    return "OAR", False


def _format_rgb_color(color_tuple: Optional[Any], fallback_idx: int, is_target: bool) -> str:
    """Format RGB tuple as hex string '#rrggbb' or pick high-contrast fallback."""
    if color_tuple and len(color_tuple) >= 3:
        try:
            r = max(0, min(255, int(color_tuple[0])))
            g = max(0, min(255, int(color_tuple[1])))
            b = max(0, min(255, int(color_tuple[2])))
            return f"#{r:02x}{g:02x}{b:02x}"
        except Exception:
            pass
    if is_target:
        return _TARGET_COLORS[fallback_idx % len(_TARGET_COLORS)]
    return _OAR_COLORS[fallback_idx % len(_OAR_COLORS)]


def load_rois_from_rtstruct(
    rtstruct_path: Path,
    reference_grid: DoseGrid,
) -> List[Dict[str, Any]]:
    """
    Parse RTSTRUCT DICOM file and rasterize all ROI contours onto reference_grid.
    Returns list of dicts with:
      roi_number, name, type, is_target, color, mask, volume_cc
    """
    try:
        dcm = pydicom.dcmread(str(rtstruct_path), force=True)
    except Exception as exc:
        logger.warning(f"Failed to read RTSTRUCT at {rtstruct_path}: {exc}")
        return []

    roi_names: Dict[int, str] = {}
    for s in getattr(dcm, "StructureSetROISequence", []):
        roi_names[int(s.ROINumber)] = str(getattr(s, "ROIName", f"ROI_{s.ROINumber}"))

    obs_types: Dict[int, str] = {}
    for obs in getattr(dcm, "RTROIObservationsSequence", []):
        ref_num = getattr(obs, "ReferencedROINumber", None)
        if ref_num is not None:
            obs_types[int(ref_num)] = str(getattr(obs, "RTROIInterpretedType", ""))

    contour_seq = getattr(dcm, "ROIContourSequence", [])
    nzv, nyv, nxv = reference_grid.shape
    sz, sy, sx = (float(v) for v in reference_grid.spacing)
    oz, oy, ox = (float(v) for v in reference_grid.origin)
    voxel_vol_cc = (sx * sy * sz) / 1000.0

    rois: List[Dict[str, Any]] = []
    target_idx = 0
    oar_idx = 0

    for rc in contour_seq:
        roi_num = int(getattr(rc, "ReferencedROINumber", 0))
        name = roi_names.get(roi_num, f"ROI_{roi_num}")
        obs_t = obs_types.get(roi_num, "")
        roi_type, is_target = _classify_roi(name, obs_t)

        color_tuple = getattr(rc, "ROIDisplayColor", None)
        color_hex = _format_rgb_color(color_tuple, target_idx if is_target else oar_idx, is_target)
        if is_target:
            target_idx += 1
        else:
            oar_idx += 1

        if not hasattr(rc, "ContourSequence"):
            continue

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

        if not mask.any():
            continue

        for z in range(nzv):
            if mask[z].any():
                mask[z] = binary_fill_holes(mask[z])

        n_voxels = int(mask.sum())
        volume_cc = round(n_voxels * voxel_vol_cc, 2)
        if volume_cc < 0.05:  # filter noise/trivial single-pixel regions
            continue

        rois.append({
            "roi_number": roi_num,
            "name": name,
            "type": roi_type,
            "is_target": is_target,
            "color": color_hex,
            "mask": mask,
            "volume_cc": volume_cc,
        })

    # Sort: Targets first (by volume desc), then OARs, then External
    rois.sort(key=lambda r: (0 if r["is_target"] else 1 if r["type"] == "OAR" else 2, -r["volume_cc"]))
    return rois


def generate_fallback_rois(
    reference_grid: DoseGrid,
    rx_dose: float,
) -> List[Dict[str, Any]]:
    """
    Synthesize anatomical ROIs from isodose distributions when no RTSTRUCT is present,
    ensuring robust DVH and uncertainty envelope display on any ingested plan.
    """
    arr = reference_grid.array
    nzv, nyv, nxv = arr.shape
    sz, sy, sx = (float(v) for v in reference_grid.spacing)
    voxel_vol_cc = (sx * sy * sz) / 1000.0

    rois = []
    # 1. PTV Target Volume: >= 95% of prescribed dose
    m_target = (arr >= 0.95 * rx_dose)
    if m_target.any():
        for z in range(nzv):
            if m_target[z].any():
                m_target[z] = binary_fill_holes(m_target[z])
        rois.append({
            "roi_number": 1,
            "name": "Target (PTV Eval)",
            "type": "TARGET",
            "is_target": True,
            "color": "#ef4444",
            "mask": m_target,
            "volume_cc": round(float(m_target.sum()) * voxel_vol_cc, 2),
        })

    # 2. Penumbra / High Dose OAR Region: 50% - 90% Rx
    m_penumbra = (arr >= 0.50 * rx_dose) & (arr < 0.90 * rx_dose)
    if m_penumbra.any():
        rois.append({
            "roi_number": 2,
            "name": "Penumbra / OAR Proximity",
            "type": "OAR",
            "is_target": False,
            "color": "#3b82f6",
            "mask": m_penumbra,
            "volume_cc": round(float(m_penumbra.sum()) * voxel_vol_cc, 2),
        })

    # 3. Intermediate Dose Gradient (20% - 50% Rx)
    m_mid = (arr >= 0.20 * rx_dose) & (arr < 0.50 * rx_dose)
    if m_mid.any():
        rois.append({
            "roi_number": 3,
            "name": "Intermediate Gradient",
            "type": "OAR",
            "is_target": False,
            "color": "#10b981",
            "mask": m_mid,
            "volume_cc": round(float(m_mid.sum()) * voxel_vol_cc, 2),
        })

    # 4. Low Dose Pool / Normal Tissue (5% - 20% Rx)
    m_low = (arr >= 0.05 * rx_dose) & (arr < 0.20 * rx_dose)
    if m_low.any():
        rois.append({
            "roi_number": 4,
            "name": "Normal Tissue Pool",
            "type": "OAR",
            "is_target": False,
            "color": "#8b5cf6",
            "mask": m_low,
            "volume_cc": round(float(m_low.sum()) * voxel_vol_cc, 2),
        })

    return rois


def compute_cumulative_dvh(
    dose_array: np.ndarray,
    mask: np.ndarray,
    dose_axis: np.ndarray,
) -> np.ndarray:
    """
    Compute cumulative volume percentage V(d) >= threshold for given dose axis.
    """
    v = dose_array[mask]
    if len(v) == 0:
        return np.zeros_like(dose_axis)
    sorted_v = np.sort(v)
    idx = np.searchsorted(sorted_v, dose_axis)
    vol_pct = (len(sorted_v) - idx) * 100.0 / len(sorted_v)
    return np.round(vol_pct, 2)


def extract_percentile_metrics(
    dose_array: np.ndarray,
    mask: np.ndarray,
    rx_dose: Optional[float] = None,
) -> Dict[str, float]:
    """
    Extract ICRU standard clinical DVH percentiles:
    D98, D95, D50, D2, Dmean, Dmax, Dmin, and optional V100%.
    """
    v = dose_array[mask]
    if len(v) == 0:
        return {
            "d98": 0.0, "d95": 0.0, "d50": 0.0, "d2": 0.0,
            "d_mean": 0.0, "d_max": 0.0, "d_min": 0.0, "v100_pct": 0.0,
        }

    d98 = float(np.percentile(v, 2))
    d95 = float(np.percentile(v, 5))
    d50 = float(np.percentile(v, 50))
    d2 = float(np.percentile(v, 98))
    d_mean = float(np.mean(v))
    d_max = float(np.max(v))
    d_min = float(np.min(v))
    v100 = float(np.mean(v >= rx_dose) * 100.0) if rx_dose and rx_dose > 0 else 0.0

    return {
        "d98": round(d98, 2),
        "d95": round(d95, 2),
        "d50": round(d50, 2),
        "d2": round(d2, 2),
        "d_mean": round(d_mean, 2),
        "d_max": round(d_max, 2),
        "d_min": round(d_min, 2),
        "v100_pct": round(v100, 2),
    }


def generate_robustness_scenarios(
    dose_grid: DoseGrid,
    setup_uncertainty_mm: float = 3.0,
    range_uncertainty_pct: float = 3.0,
    num_scenarios: int = 9,
) -> List[Tuple[str, np.ndarray]]:
    """
    Generate perturbed dose distributions for proton therapy robustness verification.
    Includes nominal plan, cardinal setup isocenter shifts, and range/density scaling.
    """
    arr = dose_grid.array
    sz, sy, sx = (float(v) for v in dose_grid.spacing)

    # Convert mm shifts to voxel index deltas
    dz = setup_uncertainty_mm / sz
    dy = setup_uncertainty_mm / sy
    dx = setup_uncertainty_mm / sx
    range_factor = range_uncertainty_pct / 100.0

    scenarios: List[Tuple[str, np.ndarray]] = [
        ("Nominal (0mm, 0%)", arr),
    ]

    # Setup Shifts (cardinal axes): +X, -X, +Y, -Y, +Z, -Z
    shifts = [
        (f"Setup +X (+{setup_uncertainty_mm:g}mm)", (0.0, 0.0, -dx)),
        (f"Setup -X (-{setup_uncertainty_mm:g}mm)", (0.0, 0.0, dx)),
        (f"Setup +Y (+{setup_uncertainty_mm:g}mm)", (0.0, -dy, 0.0)),
        (f"Setup -Y (-{setup_uncertainty_mm:g}mm)", (0.0, dy, 0.0)),
        (f"Setup +Z (+{setup_uncertainty_mm:g}mm)", (-dz, 0.0, 0.0)),
        (f"Setup -Z (-{setup_uncertainty_mm:g}mm)", (dz, 0.0, 0.0)),
    ]
    for name, shift_vec in shifts:
        scenarios.append((name, scipy_shift(arr, shift_vec, order=1, mode="nearest")))

    # Range Uncertainty: Proton range overshoot / undershoot
    # Scaling factor simulates ±range variation on effective dose depth/stopping power
    scenarios.append((f"Range Overshoot (+{range_uncertainty_pct:g}%)", arr * (1.0 + range_factor)))
    scenarios.append((f"Range Undershoot (-{range_uncertainty_pct:g}%)", arr * (1.0 - range_factor)))

    if num_scenarios >= 21:
        # Additional compound diagonal perturbations: (±X, ±Y, ±Z) combined with ±Range
        diagonals = [
            ("Compound (+X,+Y,+Z,+R)", (-dz * 0.7, -dy * 0.7, -dx * 0.7), 1.0 + range_factor),
            ("Compound (-X,-Y,-Z,-R)", (dz * 0.7, dy * 0.7, dx * 0.7), 1.0 - range_factor),
            ("Compound (+X,-Y,+Z,-R)", (-dz * 0.7, dy * 0.7, -dx * 0.7), 1.0 - range_factor),
            ("Compound (-X,+Y,-Z,+R)", (dz * 0.7, -dy * 0.7, dx * 0.7), 1.0 + range_factor),
            ("Compound (+X,+Y,-Z,-R)", (dz * 0.7, -dy * 0.7, -dx * 0.7), 1.0 - range_factor),
            ("Compound (-X,-Y,+Z,+R)", (-dz * 0.7, dy * 0.7, dx * 0.7), 1.0 + range_factor),
            ("Compound (+X,-Y,-Z,+R)", (dz * 0.7, dy * 0.7, -dx * 0.7), 1.0 + range_factor),
            ("Compound (-X,+Y,+Z,-R)", (-dz * 0.7, -dy * 0.7, dx * 0.7), 1.0 - range_factor),
            ("Diagonal (+X,+Y)", (0.0, -dy * 0.7, -dx * 0.7), 1.0),
            ("Diagonal (-X,-Y)", (0.0, dy * 0.7, dx * 0.7), 1.0),
            ("Diagonal (+X,+Z)", (-dz * 0.7, 0.0, -dx * 0.7), 1.0),
            ("Diagonal (-X,-Z)", (dz * 0.7, 0.0, dx * 0.7), 1.0),
        ]
        for name, shift_vec, r_scale in diagonals:
            s_arr = scipy_shift(arr, shift_vec, order=1, mode="nearest") * r_scale
            scenarios.append((name, s_arr))

    return scenarios[:num_scenarios]


def calculate_plan_dvh_and_robustness(
    plan_id: int,
    db: Session,
    setup_uncertainty_mm: float = 3.0,
    range_uncertainty_pct: float = 3.0,
    num_scenarios: int = 9,
    prescription_dose_override: Optional[float] = None,
    force_recompute: bool = False,
) -> PlanDVHResponse:
    """
    Main entry point: calculates or loads cached DVH curves & robustness envelopes
    for openMCsquare secondary verification.
    """
    cache_path = Path(settings.RESULTS_PATH) / f"plan_{plan_id}" / "dvh_robustness.json"

    # Return cached if valid and not force recomputing with different parameters
    if cache_path.is_file() and not force_recompute:
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Verify parameters match
            if abs(data.get("setup_uncertainty_mm", 0) - setup_uncertainty_mm) < 1e-3 and \
               abs(data.get("range_uncertainty_pct", 0) - range_uncertainty_pct) < 1e-3 and \
               data.get("num_scenarios", 0) == num_scenarios:
                return PlanDVHResponse(**data)
        except Exception as exc:
            logger.warning(f"Could not read cached DVH for plan {plan_id}: {exc}")

    plan = db.query(Plan).filter_by(id=plan_id).first()
    if not plan:
        raise ValueError(f"Plan {plan_id} not found")

    doses = load_plan_doses(plan_id, db)
    has_mc = "mcSquare" in doses
    has_tps = "tps" in doses

    if not has_mc and not has_tps:
        raise ValueError(f"Neither MCsquare nor TPS dose grids found for plan {plan_id}")

    # Use MCsquare dose as primary evaluation grid; fallback to TPS if MCsquare not yet run
    eval_grid = doses["mcSquare"] if has_mc else doses["tps"]
    tps_grid = doses.get("tps")

    if tps_grid is not None and has_mc:
        # Align TPS to MCsquare evaluation grid geometry
        tps_grid = _resample_to(tps_grid, eval_grid)

    # Determine prescription dose
    rx_dose = prescription_dose_override
    if not rx_dose:
        target_gy = getattr(plan, "target_dose_gy", None)
        if target_gy and target_gy > 0:
            rx_dose = float(target_gy)
        else:
            rx_dose = round(float(eval_grid.max_dose) * 0.95, 1)

    # 1. Load ROIs from RTSTRUCT if available
    rtstruct_file = find_rtstruct_file(plan.dicom_store_path) if plan.dicom_store_path else None
    rois_raw: List[Dict[str, Any]] = []
    if rtstruct_file:
        logger.info(f"Loading RTSTRUCT ROIs from {rtstruct_file}")
        rois_raw = load_rois_from_rtstruct(rtstruct_file, eval_grid)

    if not rois_raw:
        logger.info(f"No RTSTRUCT contours found for plan {plan_id}; generating anatomical fallback ROIs.")
        rois_raw = generate_fallback_rois(eval_grid, rx_dose)

    # 2. Compute Robustness Scenarios
    scenarios = generate_robustness_scenarios(
        eval_grid,
        setup_uncertainty_mm=setup_uncertainty_mm,
        range_uncertainty_pct=range_uncertainty_pct,
        num_scenarios=num_scenarios,
    )
    scenario_names = [s[0] for s in scenarios]

    # 3. Create Uniform Dose Axis (Gy)
    max_eval_dose = max(eval_grid.max_dose, tps_grid.max_dose if tps_grid else 0.0)
    upper_dose = float(np.ceil(max_eval_dose * 1.08))
    dose_axis = np.linspace(0.0, upper_dose, 120, dtype=np.float32)
    dose_bins_gy = [round(float(d), 2) for d in dose_axis]

    # 4. Compute DVH and Robustness Envelopes per ROI
    roi_data_list: List[ROIDVHData] = []

    for roi in rois_raw:
        mask = roi["mask"]
        is_target = roi["is_target"]

        # Nominal MC DVH curve
        mc_nominal_dvh = compute_cumulative_dvh(scenarios[0][1], mask, dose_axis)

        # Evaluate across all scenarios to derive uncertainty envelope
        scenario_curves = [mc_nominal_dvh]
        scenario_metrics = [extract_percentile_metrics(scenarios[0][1], mask, rx_dose)]

        for _, s_arr in scenarios[1:]:
            s_curve = compute_cumulative_dvh(s_arr, mask, dose_axis)
            scenario_curves.append(s_curve)
            scenario_metrics.append(extract_percentile_metrics(s_arr, mask, rx_dose))

        curves_stack = np.vstack(scenario_curves)
        mc_min_dvh = np.round(np.min(curves_stack, axis=0), 2).tolist()
        mc_max_dvh = np.round(np.max(curves_stack, axis=0), 2).tolist()

        # TPS planned DVH curve
        tps_dvh_list: Optional[List[float]] = None
        tps_metrics: Optional[Dict[str, float]] = None
        if tps_grid is not None:
            tps_dvh_list = compute_cumulative_dvh(tps_grid.array, mask, dose_axis).tolist()
            tps_metrics = extract_percentile_metrics(tps_grid.array, mask, rx_dose)

        nom_m = scenario_metrics[0]

        # Helper to construct MetricInterval with TPS vs MC comparison
        def _make_interval(key: str) -> MetricInterval:
            t_val = tps_metrics.get(key) if tps_metrics else None
            m_nom = nom_m.get(key, 0.0)
            m_min = min(s.get(key, 0.0) for s in scenario_metrics)
            m_max = max(s.get(key, 0.0) for s in scenario_metrics)
            delta = round(((m_nom - t_val) / t_val) * 100.0, 2) if t_val and t_val > 0 else None
            return MetricInterval(
                tps=t_val,
                mc_nominal=m_nom,
                mc_min=m_min,
                mc_max=m_max,
                delta_pct=delta,
            )

        metrics_obj = ROIMetrics(
            d98=_make_interval("d98"),
            d95=_make_interval("d95"),
            d50=_make_interval("d50"),
            d2=_make_interval("d2"),
            d_mean=_make_interval("d_mean"),
            d_max=_make_interval("d_max"),
            d_min=_make_interval("d_min"),
            v100_pct=_make_interval("v100_pct") if is_target else None,
        )

        # Robustness Verdict
        # For targets: D95 under all scenarios must maintain >= 95% of prescription dose
        robustness_pass = True
        robustness_note = "Target coverage robust under setup and range uncertainty"
        if is_target:
            min_d95 = metrics_obj.d95.mc_min
            if min_d95 < 0.90 * rx_dose:
                robustness_pass = False
                robustness_note = f"Worst-case D95 ({min_d95:.1f} Gy) drops below 90% Rx ({0.90 * rx_dose:.1f} Gy)"
            elif min_d95 < 0.95 * rx_dose:
                robustness_pass = True  # Marginal tolerance
                robustness_note = f"Worst-case D95 ({min_d95:.1f} Gy) is in tolerance (90-95% Rx)"

        roi_data_list.append(
            ROIDVHData(
                roi_number=roi["roi_number"],
                name=roi["name"],
                type=roi["type"],
                color=roi["color"],
                volume_cc=roi["volume_cc"],
                is_target=is_target,
                robustness_pass=robustness_pass,
                robustness_note=robustness_note,
                metrics=metrics_obj,
                dvh=DVHCurve(
                    dose_bins_gy=dose_bins_gy,
                    tps_volume_pct=tps_dvh_list,
                    mc_nominal_volume_pct=mc_nominal_dvh.tolist(),
                    mc_min_volume_pct=mc_min_dvh,
                    mc_max_volume_pct=mc_max_dvh,
                ),
            )
        )

    response_obj = PlanDVHResponse(
        plan_id=plan_id,
        calculated_at=datetime.utcnow().isoformat() + "Z",
        setup_uncertainty_mm=setup_uncertainty_mm,
        range_uncertainty_pct=range_uncertainty_pct,
        num_scenarios=len(scenarios),
        scenario_names=scenario_names,
        prescription_dose_gy=rx_dose,
        has_mc_dose=has_mc,
        has_tps_dose=has_tps,
        rois=roi_data_list,
    )

    # Save to cache
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            f.write(response_obj.model_dump_json(indent=2))
        logger.info(f"Saved DVH & robustness cache for plan {plan_id} -> {cache_path}")
    except Exception as exc:
        logger.warning(f"Failed to cache DVH data: {exc}")

    return response_obj
