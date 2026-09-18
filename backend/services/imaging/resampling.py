"""Resampling between volumes / onto reference grids (SimpleITK-backed).

All resampling is physical-space correct: numpy arrays are converted to
SimpleITK images carrying their full LPS geometry before resampling.
Out-of-FOV voxels are filled with air (-1000 HU).
"""

from __future__ import annotations

import numpy as np
import SimpleITK as sitk

from .volume_geometry import VolumeGeometry

OUTSIDE_FILL = -1000.0  # HU air


def to_sitk(array: np.ndarray, geom: VolumeGeometry) -> sitk.Image:
    img = sitk.GetImageFromArray(np.ascontiguousarray(array))  # expects [k, j, i]
    img.SetOrigin(tuple(float(v) for v in geom.origin_lps))
    img.SetSpacing(tuple(float(v) for v in geom.spacing_mm))
    img.SetDirection(tuple(float(v) for v in geom.direction))
    return img


def from_sitk(img: sitk.Image) -> tuple[np.ndarray, VolumeGeometry]:
    geom = VolumeGeometry(
        origin_lps=tuple(img.GetOrigin()),
        spacing_mm=tuple(img.GetSpacing()),
        size_voxels=tuple(img.GetSize()),
        direction=tuple(img.GetDirection()),
    )
    return sitk.GetArrayFromImage(img), geom


def resample_to_reference(
    moving_array: np.ndarray,
    moving_geom: VolumeGeometry,
    reference_geom: VolumeGeometry,
    transform: sitk.Transform | None = None,
    interpolator=sitk.sitkLinear,
    default_value: float = OUTSIDE_FILL,
) -> np.ndarray:
    """Resample moving array onto reference_geom grid.
    transform maps FIXED (reference) LPS points to MOVING LPS points."""
    moving = to_sitk(moving_array, moving_geom)
    ref = sitk.Image(reference_geom.size_voxels, moving.GetPixelID())
    ref.SetOrigin(tuple(float(v) for v in reference_geom.origin_lps))
    ref.SetSpacing(tuple(float(v) for v in reference_geom.spacing_mm))
    ref.SetDirection(tuple(float(v) for v in reference_geom.direction))
    out = sitk.Resample(
        moving,
        ref,
        transform or sitk.Transform(),
        interpolator,
        default_value,
        moving.GetPixelID(),
    )
    return sitk.GetArrayFromImage(out)
