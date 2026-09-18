"""
Gamma analysis engine — Phase 3.

Pure NumPy implementation using the vectorised local-search approach of
Wendling et al., Med Phys 34 (2007). Instead of comparing every reference
voxel against the entire evaluation grid (O(N^2)), each reference voxel is
compared only against evaluation voxels within a search radius of a few DTA,
which is both fast and standard practice.

Provides gamma_2d (single plane) and gamma_3d (full volume). Per-beam proton
QA must use gamma_3d: proton dose is deposited along the (angled) beam axis, so
any single axial plane is unrepresentative and a per-beam z-misalignment between
MC and TPS collapses a 2D score even when the 3D dose agrees.
"""
from __future__ import annotations

import numpy as np


def gamma_2d(
    reference: np.ndarray,
    evaluation: np.ndarray,
    dd_percent: float,
    dta_mm: float,
    voxel_size_mm: float,
    dose_threshold_percent: float = 10.0,
    global_normalization: bool = True,
    search_radius_dta: float = 3.0,
) -> tuple[np.ndarray, float]:
    """
    Global gamma analysis on 2D dose planes.

    Args:
        reference:  2D reference dose plane (Gy).
        evaluation: 2D evaluation dose plane (Gy), same shape as reference.
        dd_percent: Dose-difference criterion (%).
        dta_mm:     Distance-to-agreement criterion (mm).
        voxel_size_mm: In-plane voxel size (mm).
        dose_threshold_percent: Voxels below this % of max ref dose are excluded.
        global_normalization: Normalise DD to the global max dose (vs local).
        search_radius_dta: Search window radius expressed in multiples of DTA.

    Returns:
        (gamma_map, passing_rate_percent). gamma_map has NaN where excluded.
    """
    assert reference.shape == evaluation.shape, "Dose grids must have identical shape."
    reference = reference.astype(np.float32)
    evaluation = evaluation.astype(np.float32)

    ref_max = float(reference.max()) if reference.size else 0.0
    if ref_max <= 0:
        return np.full(reference.shape, np.nan, dtype=np.float32), 0.0

    dd_abs = (dd_percent / 100.0) * ref_max  # global normalisation
    threshold_mask = reference >= (dose_threshold_percent / 100.0) * ref_max

    # Search window in voxels (round up so we always cover search_radius * DTA).
    radius_vox = int(np.ceil(search_radius_dta * dta_mm / voxel_size_mm))
    radius_vox = max(radius_vox, 1)

    gamma_sq_min = np.full(reference.shape, np.inf, dtype=np.float32)

    # For each candidate shift within the window, compute gamma^2 contribution
    # vectorised across the whole plane, then keep the running minimum.
    for dr in range(-radius_vox, radius_vox + 1):
        for dc in range(-radius_vox, radius_vox + 1):
            dist_mm_sq = (dr * dr + dc * dc) * (voxel_size_mm ** 2)
            if dist_mm_sq > (search_radius_dta * dta_mm) ** 2:
                continue  # outside circular search radius
            spatial_term = dist_mm_sq / (dta_mm ** 2)

            shifted = _shift_plane(evaluation, dr, dc)
            dose_term = ((shifted - reference) / dd_abs) ** 2
            gamma_sq = dose_term + spatial_term
            np.minimum(gamma_sq_min, gamma_sq, out=gamma_sq_min)

    gamma_map = np.sqrt(gamma_sq_min)
    gamma_map[~threshold_mask] = np.nan

    valid = gamma_map[threshold_mask]
    valid = valid[~np.isnan(valid)]
    passing_rate = (
        float(np.sum(valid <= 1.0) / len(valid) * 100.0) if len(valid) > 0 else 0.0
    )
    return gamma_map.astype(np.float32), passing_rate


def gamma_3d(
    reference: np.ndarray,
    evaluation: np.ndarray,
    dd_percent: float,
    dta_mm: float,
    voxel_size_mm: tuple[float, float, float] | float,
    dose_threshold_percent: float = 10.0,
    search_radius_dta: float = 3.0,
) -> tuple[np.ndarray, float]:
    """
    Global 3D gamma analysis over a dose volume (z, y, x).

    Same vectorised local-search as gamma_2d, extended to a 3D neighbourhood.
    voxel_size_mm may be a scalar or a (sz, sy, sx) tuple (anisotropic grids —
    e.g. 2 mm slices with finer in-plane spacing — are handled correctly).

    Returns:
        (gamma_volume, passing_rate_percent). gamma_volume has NaN where excluded.
    """
    assert reference.shape == evaluation.shape, "Dose grids must have identical shape."
    reference = reference.astype(np.float32)
    evaluation = evaluation.astype(np.float32)

    if np.isscalar(voxel_size_mm):
        vz = vy = vx = float(voxel_size_mm)
    else:
        vz, vy, vx = (float(v) for v in voxel_size_mm)

    ref_max = float(reference.max()) if reference.size else 0.0
    if ref_max <= 0:
        return np.full(reference.shape, np.nan, dtype=np.float32), 0.0

    dd_abs = (dd_percent / 100.0) * ref_max
    threshold_mask = reference >= (dose_threshold_percent / 100.0) * ref_max

    max_dist = search_radius_dta * dta_mm
    rz = max(int(np.ceil(max_dist / vz)), 1)
    ry = max(int(np.ceil(max_dist / vy)), 1)
    rx = max(int(np.ceil(max_dist / vx)), 1)
    max_dist_sq = max_dist ** 2

    gamma_sq_min = np.full(reference.shape, np.inf, dtype=np.float32)

    for dz in range(-rz, rz + 1):
        for dy in range(-ry, ry + 1):
            for dx in range(-rx, rx + 1):
                dist_mm_sq = (dz * vz) ** 2 + (dy * vy) ** 2 + (dx * vx) ** 2
                if dist_mm_sq > max_dist_sq:
                    continue  # outside spherical search radius
                spatial_term = dist_mm_sq / (dta_mm ** 2)

                shifted = _shift_volume(evaluation, dz, dy, dx)
                dose_term = ((shifted - reference) / dd_abs) ** 2
                gamma_sq = dose_term + spatial_term
                np.minimum(gamma_sq_min, gamma_sq, out=gamma_sq_min)

    gamma_vol = np.sqrt(gamma_sq_min)
    gamma_vol[~threshold_mask] = np.nan

    valid = gamma_vol[threshold_mask]
    valid = valid[~np.isnan(valid)]
    passing_rate = (
        float(np.sum(valid <= 1.0) / len(valid) * 100.0) if len(valid) > 0 else 0.0
    )
    return gamma_vol.astype(np.float32), passing_rate


def _shift_plane(arr: np.ndarray, dr: int, dc: int) -> np.ndarray:
    """
    Shifts a 2D array by (dr, dc), filling exposed edges with +inf so that
    out-of-bounds evaluation points never produce a spuriously low gamma.
    """
    out = np.full_like(arr, np.inf)
    r_src_start = max(0, -dr)
    r_src_end = arr.shape[0] - max(0, dr)
    c_src_start = max(0, -dc)
    c_src_end = arr.shape[1] - max(0, dc)

    r_dst_start = max(0, dr)
    r_dst_end = arr.shape[0] - max(0, -dr)
    c_dst_start = max(0, dc)
    c_dst_end = arr.shape[1] - max(0, -dc)

    out[r_dst_start:r_dst_end, c_dst_start:c_dst_end] = arr[
        r_src_start:r_src_end, c_src_start:c_src_end
    ]
    return out


def _shift_volume(arr: np.ndarray, dz: int, dy: int, dx: int) -> np.ndarray:
    """3D analogue of _shift_plane; exposed edges filled with +inf."""
    out = np.full_like(arr, np.inf)

    z_src_start = max(0, -dz); z_src_end = arr.shape[0] - max(0, dz)
    y_src_start = max(0, -dy); y_src_end = arr.shape[1] - max(0, dy)
    x_src_start = max(0, -dx); x_src_end = arr.shape[2] - max(0, dx)

    z_dst_start = max(0, dz); z_dst_end = arr.shape[0] - max(0, -dz)
    y_dst_start = max(0, dy); y_dst_end = arr.shape[1] - max(0, -dy)
    x_dst_start = max(0, dx); x_dst_end = arr.shape[2] - max(0, -dx)

    out[z_dst_start:z_dst_end, y_dst_start:y_dst_end, x_dst_start:x_dst_end] = arr[
        z_src_start:z_src_end, y_src_start:y_src_end, x_src_start:x_src_end
    ]
    return out