"""Imaging package for Virtual PSQA.

Provides:
- VolumeGeometry: LPS coordinate system volume geometry
- RigidTransform: 6-DOF rigid transforms
- resample_to_reference: physical space resampling via SimpleITK
- load_cbct_series, find_planning_ct_series: DICOM series loaders
- build_virtual_ct, export_ct_series: Synthetic CT generation and DICOM export
"""

from .volume_geometry import VolumeGeometry
from .rigid_transforms import RigidTransform, make_transform, to_sitk_fixed_to_moving
from .resampling import resample_to_reference, to_sitk, from_sitk
from .dicom_volume_loader import load_cbct_series, find_planning_ct_series, scan_dicom_slices
from .synthetic_ct_engine import (
    build_virtual_ct,
    export_ct_series,
    cbct_external_mask,
    deform_mask,
    propagate_rois_through_dir,
)
from .external_contour import (
    get_rtstruct_external_mask,
    get_rtstruct_external_rois,
    compute_robust_external_mask,
    compute_cbct_external_mask,
    apply_external_mask_to_volume,
    determine_external_mask,
    save_external_mask,
    load_external_mask,
)

__all__ = [
    "VolumeGeometry",
    "RigidTransform",
    "make_transform",
    "to_sitk_fixed_to_moving",
    "resample_to_reference",
    "to_sitk",
    "from_sitk",
    "load_cbct_series",
    "find_planning_ct_series",
    "scan_dicom_slices",
    "build_virtual_ct",
    "export_ct_series",
    "cbct_external_mask",
    "deform_mask",
    "propagate_rois_through_dir",
    "get_rtstruct_external_mask",
    "get_rtstruct_external_rois",
    "compute_robust_external_mask",
    "compute_cbct_external_mask",
    "apply_external_mask_to_volume",
    "determine_external_mask",
    "save_external_mask",
    "load_external_mask",
]
