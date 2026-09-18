"""
DoseGrid — lightweight container for a 3D dose distribution plus geometry.

Used as the common interchange format between the MCsquare runner, the log
reconstructor, the RTDose loader, and (Phase 3) the gamma engine. Saved to disk
as a compressed .npz bundle so the dose array and its geometry travel together.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass
class DoseGrid:
    """
    A 3D dose distribution.

    array:   float32, shape (n_z, n_y, n_x), dose in Gy
    spacing: (sz, sy, sx) voxel spacing in mm
    origin:  (z0, y0, x0) position of voxel [0,0,0] in mm (patient coords)
    """

    array: np.ndarray
    spacing: tuple[float, float, float]
    origin: tuple[float, float, float]

    @property
    def shape(self) -> tuple[int, int, int]:
        return tuple(self.array.shape)  # type: ignore[return-value]

    @property
    def max_dose(self) -> float:
        return float(self.array.max()) if self.array.size else 0.0

    def max_dose_plane_index(self) -> int:
        """Returns the axial (z) index of the plane containing the global max dose."""
        if self.array.size == 0:
            return 0
        per_plane_max = self.array.reshape(self.array.shape[0], -1).max(axis=1)
        return int(np.argmax(per_plane_max))

    def plane(self, z_index: int) -> np.ndarray:
        """Returns the 2D (n_y, n_x) dose plane at the given axial index."""
        z_index = max(0, min(z_index, self.array.shape[0] - 1))
        return self.array[z_index]

    def save(self, path: str | Path) -> str:
        """Saves to a compressed .npz bundle. Returns the written path."""
        path = Path(path)
        if path.suffix != ".npz":
            path = path.with_suffix(".npz")
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            path,
            array=self.array.astype(np.float32),
            spacing=np.asarray(self.spacing, dtype=np.float64),
            origin=np.asarray(self.origin, dtype=np.float64),
        )
        return str(path)

    @classmethod
    def load(cls, path: str | Path) -> "DoseGrid":
        data = np.load(str(path))
        return cls(
            array=data["array"].astype(np.float32),
            spacing=tuple(float(v) for v in data["spacing"]),
            origin=tuple(float(v) for v in data["origin"]),
        )
