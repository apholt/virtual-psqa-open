"""
backend/services/log_gamma.py -- 2D delivery log vs. prescription gamma analysis.

Evaluates 2D planar dose distributions reconstructed from machine delivery logs
(RT Ion Records) against the planned prescription using standard AAPM TG-218
gamma analysis (dose difference % and distance-to-agreement mm criteria).
"""
from __future__ import annotations

import logging
import numpy as np
from scipy.interpolate import RegularGridInterpolator

from services.gamma_engine import gamma_2d

logger = logging.getLogger(__name__)


def _resample_plane(
    m: np.ndarray,
    coords_cm: np.ndarray,
    resolution_cm: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Resamples a 2D array from its native coordinate grid to a target isotropic
    resolution via bilinear interpolation using RegularGridInterpolator.

    Args:
        m: 2D array (native resolution).
        coords_cm: 1D coordinate vector along each axis (cm).
        resolution_cm: Target grid spacing (cm).

    Returns:
        (x_new, y_new, m_resampled)
    """
    n_new_x = int(round((coords_cm[-1] - coords_cm[0]) / resolution_cm))
    n_new_y = int(round((coords_cm[-1] - coords_cm[0]) / resolution_cm))
    x = np.linspace(coords_cm[0], coords_cm[-1], n_new_x)
    y = np.linspace(coords_cm[0], coords_cm[-1], n_new_y)
    interp = RegularGridInterpolator(
        (coords_cm, coords_cm),
        m,
        bounds_error=False,
        fill_value=0.0,
    )
    xv, yv = np.meshgrid(x, y)
    m_new = interp((yv, xv))
    return x, y, m_new


def gamma_log_vs_rx(
    delivered_dose: np.ndarray,
    prescribed_dose: np.ndarray,
    dd_percent: float = 2.0,
    dta_mm: float = 2.0,
    resolution_mm: float = 0.5,
    roi_threshold_percent: float = 2.0,
) -> tuple[np.ndarray, float, int, int]:
    """
    Global 2D gamma analysis comparing delivered dose (from delivery log) against
    the planned prescription dose.

    Args:
        delivered_dose: 2D array of delivered dose (isocenter plane).
        prescribed_dose: 2D array of prescribed dose (isocenter plane).
        dd_percent: Dose-difference criterion (% of maximum prescription dose).
        dta_mm: Distance-to-agreement criterion (mm).
        resolution_mm: In-plane evaluation grid resolution (mm).
        roi_threshold_percent: Dose threshold cutoff (% of maximum dose).

    Returns:
        (gamma_map, passing_rate_percent, fails, roi_pixels)
    """
    ref = np.asarray(prescribed_dose, dtype=np.float32)
    eval_dose = np.asarray(delivered_dose, dtype=np.float32)

    rx_max = float(np.max(ref)) if ref.size > 0 else 0.0
    if rx_max <= 0:
        return np.zeros_like(ref), 0.0, 0, 0

    # Resample to evaluation resolution if requested
    if resolution_mm > 0 and ref.shape[0] > 1:
        coords_cm = np.linspace(-15.0, 14.95, ref.shape[0])
        res_cm = resolution_mm / 10.0
        _, _, eval_resampled = _resample_plane(eval_dose, coords_cm, res_cm)
        _, _, ref_resampled = _resample_plane(ref, coords_cm, res_cm)
        voxel_size_mm = resolution_mm
    else:
        eval_resampled = eval_dose
        ref_resampled = ref
        voxel_size_mm = 300.0 / ref.shape[0]

    gamma_map, passing_rate = gamma_2d(
        reference=ref_resampled,
        evaluation=eval_resampled,
        dd_percent=dd_percent,
        dta_mm=dta_mm,
        voxel_size_mm=voxel_size_mm,
        dose_threshold_percent=roi_threshold_percent,
        global_normalization=True,
        search_radius_dta=3.0,
    )

    valid_mask = ~np.isnan(gamma_map)
    roi_pixels = int(np.sum(valid_mask))
    if roi_pixels == 0:
        return np.zeros_like(ref_resampled), 0.0, 0, 0

    valid_vals = gamma_map[valid_mask]
    fails = int(np.sum(valid_vals > 1.0))
    passing_rate = round(float(np.sum(valid_vals <= 1.0) / roi_pixels * 100.0), 1)

    clean_map = np.where(np.isnan(gamma_map), 0.0, gamma_map).astype(np.float32)
    return clean_map, passing_rate, fails, roi_pixels
