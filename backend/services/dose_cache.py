"""
Cached dose loading.

The dose viewer fetches one plane at a time while the user scrubs the depth
slider, so the same DoseGrid is requested repeatedly. These LRU caches key on
(path, mtime) so an updated file is automatically reloaded.
"""
from __future__ import annotations

import os
from functools import lru_cache

from dicom.rtdose_parser import load_rtdose
from services.dose_grid import DoseGrid


@lru_cache(maxsize=64)
def _load_npz(path: str, mtime: float) -> DoseGrid:
    return DoseGrid.load(path)


@lru_cache(maxsize=16)
def _load_rtdose(path: str, mtime: float) -> DoseGrid:
    return load_rtdose(path)


def cached_load(path: str) -> DoseGrid:
    """Loads a DoseGrid from a .npz bundle or an RTDose .dcm, with caching."""
    mtime = os.path.getmtime(path)
    if path.lower().endswith(".npz"):
        return _load_npz(path, mtime)
    return _load_rtdose(path, mtime)
