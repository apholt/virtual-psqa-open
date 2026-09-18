"""Rigid 6-DOF transform utilities.

Canonical semantics:
    p_fixed = R @ (p_moving - c) + c + t

where R is a 3x3 rotation matrix, c the rotation center (LPS mm)
and t the translation (mm). Euler angles use the convention:
    R = Rz(rz) @ Ry(ry) @ Rx(rx)
with angles in DEGREES about the LPS axes.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
import numpy as np
import SimpleITK as sitk


@dataclass
class RigidTransform:
    """Rigid 6-DOF transform mapping moving LPS points to fixed LPS points."""

    rotation: tuple[float, ...] = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    translation_mm: tuple[float, float, float] = (0.0, 0.0, 0.0)
    center_lps: tuple[float, float, float] = (0.0, 0.0, 0.0)
    fixed_volume_id: str = ""
    moving_volume_id: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["rotation"] = list(self.rotation)
        d["translation_mm"] = list(self.translation_mm)
        d["center_lps"] = list(self.center_lps)
        return d


def matrix_from_euler_deg(rx: float, ry: float, rz: float) -> np.ndarray:
    """R = Rz @ Ry @ Rx, angles in degrees."""
    ax, ay, az = np.deg2rad([rx, ry, rz])
    cx, sx = np.cos(ax), np.sin(ax)
    cy, sy = np.cos(ay), np.sin(ay)
    cz, sz = np.cos(az), np.sin(az)
    rx_m = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    ry_m = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    rz_m = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return rz_m @ ry_m @ rx_m


def euler_deg_from_matrix(rot: np.ndarray) -> tuple[float, float, float]:
    """Inverse of matrix_from_euler_deg."""
    r = np.asarray(rot, dtype=float)
    sy = -r[2, 0]
    sy = np.clip(sy, -1.0, 1.0)
    ry = np.arcsin(sy)
    if abs(sy) < 1.0 - 1e-9:
        rx = np.arctan2(r[2, 1], r[2, 2])
        rz = np.arctan2(r[1, 0], r[0, 0])
    else:
        rx = np.arctan2(-r[1, 2], r[1, 1])
        rz = 0.0
    return tuple(np.rad2deg([rx, ry, rz]))


def make_transform(
    translation_mm=(0.0, 0.0, 0.0),
    rotation_deg=(0.0, 0.0, 0.0),
    center_lps=(0.0, 0.0, 0.0),
    fixed_volume_id: str = "",
    moving_volume_id: str = "",
) -> RigidTransform:
    rot = matrix_from_euler_deg(*rotation_deg)
    return RigidTransform(
        rotation=tuple(rot.flatten()),
        translation_mm=tuple(float(v) for v in translation_mm),
        center_lps=tuple(float(v) for v in center_lps),
        fixed_volume_id=fixed_volume_id,
        moving_volume_id=moving_volume_id,
    )


def to_matrix44(t: RigidTransform) -> np.ndarray:
    """Homogeneous 4x4 with the center folded into the translation:
    p_fixed = R @ p + (c + t - R @ c)."""
    r = np.asarray(t.rotation, dtype=float).reshape(3, 3)
    c = np.asarray(t.center_lps, dtype=float)
    tr = np.asarray(t.translation_mm, dtype=float)
    m = np.eye(4)
    m[:3, :3] = r
    m[:3, 3] = c + tr - r @ c
    return m


def from_matrix44(
    m: np.ndarray, fixed_volume_id: str = "", moving_volume_id: str = ""
) -> RigidTransform:
    m = np.asarray(m, dtype=float)
    return RigidTransform(
        rotation=tuple(m[:3, :3].flatten()),
        translation_mm=tuple(m[:3, 3]),
        center_lps=(0.0, 0.0, 0.0),
        fixed_volume_id=fixed_volume_id,
        moving_volume_id=moving_volume_id,
    )


def invert(t: RigidTransform) -> RigidTransform:
    m = to_matrix44(t)
    inv = np.eye(4)
    r_inv = m[:3, :3].T
    inv[:3, :3] = r_inv
    inv[:3, 3] = -r_inv @ m[:3, 3]
    return from_matrix44(inv, t.moving_volume_id, t.fixed_volume_id)


def to_sitk_fixed_to_moving(t: RigidTransform) -> sitk.Transform:
    """SimpleITK transform for resampling moving image onto fixed grid."""
    inv = invert(t)
    aff = sitk.AffineTransform(3)
    aff.SetMatrix(tuple(np.asarray(inv.rotation, dtype=float)))
    aff.SetTranslation(tuple(inv.translation_mm))
    aff.SetCenter(tuple(inv.center_lps))
    return aff
