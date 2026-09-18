"""Volume geometry in the DICOM patient coordinate system (LPS).

Conventions:
* Physical coordinates are DICOM patient coordinates: LPS
  (+x = patient Left, +y = patient Posterior, +z = patient Superior), in mm.
* Voxel arrays are numpy arrays indexed array[k, j, i] where
  i = column (x-like axis), j = row (y-like axis), k = slice (z-like axis).
  This matches SimpleITK.GetArrayFromImage ordering.
* direction is a 3x3 matrix whose COLUMNS are the unit direction cosines of
  the i, j, k voxel axes expressed in LPS.
* Conversion: physical = origin + direction @ (spacing * (i, j, k))
"""

from __future__ import annotations

from dataclasses import dataclass, field
import numpy as np

DIRECTION_TOLERANCE = 1e-4


@dataclass
class VolumeGeometry:
    """Geometry of a regularly sampled 3D volume in LPS coordinates."""

    origin_lps: tuple[float, float, float]
    spacing_mm: tuple[float, float, float]  # (i, j, k) spacing in mm
    size_voxels: tuple[int, int, int]       # (i, j, k) = (cols, rows, slices)
    direction: tuple[float, ...] = field(
        default=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    )  # 3x3 row-major, columns = i/j/k axis cosines in LPS

    @property
    def direction_matrix(self) -> np.ndarray:
        return np.asarray(self.direction, dtype=float).reshape(3, 3)

    def voxel_to_physical(self, ijk: np.ndarray | tuple) -> np.ndarray:
        """Continuous voxel index (i, j, k) -> LPS physical point (mm)."""
        ijk = np.atleast_2d(np.asarray(ijk, dtype=float))
        scaled = ijk * np.asarray(self.spacing_mm, dtype=float)
        pts = scaled @ self.direction_matrix.T + np.asarray(self.origin_lps)
        return pts[0] if pts.shape[0] == 1 else pts

    def physical_to_voxel(self, lps: np.ndarray | tuple) -> np.ndarray:
        """LPS physical point (mm) -> continuous voxel index (i, j, k)."""
        lps = np.atleast_2d(np.asarray(lps, dtype=float))
        rel = lps - np.asarray(self.origin_lps)
        scaled = rel @ np.linalg.inv(self.direction_matrix).T
        ijk = scaled / np.asarray(self.spacing_mm, dtype=float)
        return ijk[0] if ijk.shape[0] == 1 else ijk

    def physical_extent_lps(self) -> tuple[np.ndarray, np.ndarray]:
        """Axis-aligned LPS bounding box (min_corner, max_corner) in mm."""
        ni, nj, nk = self.size_voxels
        corners_ijk = [
            (i, j, k)
            for i in (0, ni - 1)
            for j in (0, nj - 1)
            for k in (0, nk - 1)
        ]
        pts = np.array([self.voxel_to_physical(c) for c in corners_ijk])
        return pts.min(axis=0), pts.max(axis=0)

    def is_axis_aligned(self, tol: float = DIRECTION_TOLERANCE) -> bool:
        """True if voxel axes coincide with the LPS axes (identity direction)."""
        return bool(np.allclose(self.direction_matrix, np.eye(3), atol=tol))

    def to_dict(self) -> dict:
        return {
            "origin_lps": list(self.origin_lps),
            "spacing_mm": list(self.spacing_mm),
            "size_voxels": list(self.size_voxels),
            "direction": list(self.direction),
        }

    @staticmethod
    def from_dict(d: dict) -> "VolumeGeometry":
        return VolumeGeometry(
            origin_lps=tuple(d["origin_lps"]),
            spacing_mm=tuple(d["spacing_mm"]),
            size_voxels=tuple(d["size_voxels"]),
            direction=tuple(d["direction"]),
        )


def directions_compatible(a: VolumeGeometry, b: VolumeGeometry, tol: float = 1e-3) -> bool:
    """Whether two volumes have numerically compatible voxel-axis directions."""
    return bool(np.allclose(a.direction_matrix, b.direction_matrix, atol=tol))
