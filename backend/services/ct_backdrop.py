"""
CT backdrop service -- serves planning-CT planes aligned to the TPS dose grid
so the frontend can composite the dose colorwash over patient anatomy.

v2: the CT is resampled onto a FINE grid (~1 mm in-plane) covering the same
physical extent as the TPS dose grid, instead of being decimated to the dose
grid's 2-3 mm voxels. The frontend composites the coarse dose layer over the
fine CT layer; because both cover the same extent, they align when stretched
to the same canvas. Plane count (z) matches the dose grid one-to-one.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np
import pydicom
from sqlalchemy.orm import Session

from models.plan import Plan
from services.dose_grid import DoseGrid

logger = logging.getLogger(__name__)

# Fine HU volume cached per (store path, dose-grid geometry).
_CT_CACHE: dict = {}
_CT_CACHE_MAX = 4

# Target in-plane resolution for the backdrop (mm); zoom factor capped so a
# huge dose grid cannot explode memory.
_TARGET_MM = 1.0
_MAX_ZOOM = 3


def _load_ct_volume(store: str) -> Optional[DoseGrid]:
    """Assemble the planning CT series from the DICOM store as a HU volume.

    Assumes standard axial HFS acquisition (identity in-plane orientation),
    same as the vendored SDC pipeline this store feeds.
    """
    files = []
    for p in Path(store).rglob("*.dcm"):
        try:
            d = pydicom.dcmread(str(p), stop_before_pixels=True, force=True)
        except Exception:
            continue
        if str(d.get("Modality", "")).upper() == "CT":
            files.append(str(p))
    if not files:
        return None

    slices = []
    for f in files:
        try:
            slices.append(pydicom.dcmread(f, force=True))
        except Exception:
            continue
    slices = [s for s in slices if hasattr(s, "ImagePositionPatient")]
    if not slices:
        return None
    slices.sort(key=lambda s: float(s.ImagePositionPatient[2]))

    py, px = (float(v) for v in slices[0].PixelSpacing)
    z0 = float(slices[0].ImagePositionPatient[2])
    if len(slices) > 1:
        sz = abs(float(slices[1].ImagePositionPatient[2]) - z0)
    else:
        sz = float(getattr(slices[0], "SliceThickness", 1.0) or 1.0)

    try:
        vol = np.stack([s.pixel_array for s in slices]).astype(np.float32)
    except Exception as exc:
        logger.warning(f"CT pixel assembly failed for {store}: {exc}")
        return None

    slope = float(getattr(slices[0], "RescaleSlope", 1.0) or 1.0)
    inter = float(getattr(slices[0], "RescaleIntercept", 0.0) or 0.0)
    vol = vol * slope + inter  # -> Hounsfield units

    origin = (
        z0,
        float(slices[0].ImagePositionPatient[1]),
        float(slices[0].ImagePositionPatient[0]),
    )
    return DoseGrid(array=vol, spacing=(sz, py, px), origin=origin)


def _fine_target(dose: DoseGrid) -> DoseGrid:
    """
    Empty grid covering the SAME physical extent as `dose`, with in-plane
    voxels refined toward _TARGET_MM (z spacing unchanged -- one CT plane per
    dose plane). The origin is shifted by half the voxel-size difference so the
    fine and coarse grids share cell EDGES, keeping the two layers aligned when
    stretched to the same canvas.
    """
    nz, ny, nx = dose.shape
    sz, sy, sx = (float(v) for v in dose.spacing)
    oz, oy, ox = (float(v) for v in dose.origin)

    fy = min(_MAX_ZOOM, max(1, int(round(sy / _TARGET_MM))))
    fx = min(_MAX_ZOOM, max(1, int(round(sx / _TARGET_MM))))
    fsy, fsx = sy / fy, sx / fx

    return DoseGrid(
        array=np.zeros((nz, ny * fy, nx * fx), dtype=np.float32),
        spacing=(sz, fsy, fsx),
        origin=(oz, oy - (sy - fsy) / 2.0, ox - (sx - fsx) / 2.0),
    )


def _resample_hu(src: DoseGrid, target: DoseGrid) -> DoseGrid:
    """Trilinear resample of the HU volume onto `target`'s grid; outside
    voxels fill with air (-1000 HU)."""
    from scipy.ndimage import map_coordinates

    sz, sy, sx = (float(v) for v in src.spacing)
    oz, oy, ox = (float(v) for v in src.origin)
    tz, ty, tx = (float(v) for v in target.spacing)
    poz, poy, pox = (float(v) for v in target.origin)
    nz, ny, nx = target.array.shape

    zi = (poz + np.arange(nz) * tz - oz) / sz
    yi = (poy + np.arange(ny) * ty - oy) / sy
    xi = (pox + np.arange(nx) * tx - ox) / sx

    ZI, YI, XI = np.meshgrid(zi, yi, xi, indexing="ij")
    resampled = map_coordinates(
        src.array, [ZI, YI, XI], order=1, mode="constant", cval=-1000.0
    ).astype(np.float32)
    return DoseGrid(array=resampled, spacing=target.spacing, origin=target.origin)


def get_ct_on_grid(plan_id: int, dose_grid: DoseGrid, db: Session) -> Optional[DoseGrid]:
    """Fine-resolution HU volume aligned to `dose_grid`'s extent, cached."""
    plan = db.query(Plan).filter_by(id=plan_id).first()
    if plan is None:
        return None
    store = plan.dicom_store_path
    key = (store, dose_grid.shape, tuple(dose_grid.spacing), tuple(dose_grid.origin))
    hit = _CT_CACHE.get(key)
    if hit is not None:
        return hit

    ct = _load_ct_volume(store)
    if ct is None:
        _CT_CACHE[key] = None
        return None
    out = _resample_hu(ct, _fine_target(dose_grid))
    if len(_CT_CACHE) >= _CT_CACHE_MAX:
        _CT_CACHE.pop(next(iter(_CT_CACHE)))
    _CT_CACHE[key] = out
    return out