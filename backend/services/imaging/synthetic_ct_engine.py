"""Synthetic CT generation engine (SimpleITK-based).

Implements deformable image registration (DIR) of planning CT onto
CBCT anatomy, transferring planning-CT HU. Voxels outside the CBCT
FOV keep original planning-CT HU, so the exported volume is always
dose-calculable over the full planning grid.
"""

from __future__ import annotations

import datetime
import os
from typing import Any, Optional

import numpy as np
import SimpleITK as sitk
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

from .volume_geometry import VolumeGeometry


def _axes_from_geometry(geometry: VolumeGeometry) -> tuple[tuple[float, float, float], np.ndarray]:
    p000 = np.asarray(geometry.voxel_to_physical((0, 0, 0)), dtype=float)
    pi = np.asarray(geometry.voxel_to_physical((1, 0, 0)), dtype=float)
    pj = np.asarray(geometry.voxel_to_physical((0, 1, 0)), dtype=float)
    pk = np.asarray(geometry.voxel_to_physical((0, 0, 1)), dtype=float)
    di, dj, dk = pi - p000, pj - p000, pk - p000
    axes = np.stack(
        [
            di / max(np.linalg.norm(di), 1e-9),
            dj / max(np.linalg.norm(dj), 1e-9),
            dk / max(np.linalg.norm(dk), 1e-9),
        ],
        axis=1,
    )
    return (float(p000[0]), float(p000[1]), float(p000[2])), axes


def _to_sitk(arr_kji: np.ndarray, geometry: VolumeGeometry) -> sitk.Image:
    img = sitk.GetImageFromArray(np.ascontiguousarray(arr_kji))
    sx, sy, sz = geometry.spacing_mm
    img.SetSpacing((float(sx), float(sy), float(sz)))
    origin, axes = _axes_from_geometry(geometry)
    img.SetOrigin(origin)
    img.SetDirection(tuple(axes.flatten(order="F")))
    return img


def _body_mask(img: sitk.Image, threshold_hu: float = -400.0) -> sitk.Image:
    """Patient body mask: largest connected component above threshold."""
    bin_ = sitk.BinaryThreshold(
        img, lowerThreshold=float(threshold_hu), upperThreshold=1e9,
        insideValue=1, outsideValue=0,
    )
    bin_ = sitk.BinaryErode(bin_, [2, 2, 2])
    cc = sitk.ConnectedComponent(bin_)
    cc = sitk.RelabelComponent(cc, sortByObjectSize=True)
    body = sitk.BinaryThreshold(cc, 1, 1, 1, 0)
    body = sitk.BinaryDilate(body, [4, 4, 4])
    body = sitk.BinaryMorphologicalClosing(body, [3, 3, 3])
    body = sitk.BinaryFillhole(body)
    return body


def cbct_external_mask(
    cbct_kji: np.ndarray,
    spacing_mm: tuple[float, float, float],
    *,
    threshold_hu: float = -300.0,
    open_mm: float = 6.0,
    close_mm: float = 4.0,
) -> np.ndarray:
    """External contour derived from CBCT anatomy, hardened against streak halo."""
    img = sitk.GetImageFromArray(cbct_kji.astype(np.float32))
    img.SetSpacing(tuple(float(v) for v in spacing_mm))
    b = sitk.BinaryThreshold(
        img, lowerThreshold=float(threshold_hu), upperThreshold=1e9, insideValue=1, outsideValue=0
    )

    def rad(mm: float) -> list[int]:
        return [max(1, int(round(mm / float(s)))) for s in spacing_mm]

    b = sitk.BinaryMorphologicalOpening(b, rad(open_mm))
    cc = sitk.RelabelComponent(sitk.ConnectedComponent(b), sortByObjectSize=True)
    b = sitk.BinaryThreshold(cc, 1, 1, 1, 0)
    b = sitk.BinaryMorphologicalClosing(b, rad(close_mm))
    b = sitk.BinaryFillhole(b)
    arr = sitk.GetArrayFromImage(b) > 0

    for k in range(arr.shape[0]):
        if arr[k].any():
            sl = sitk.GetImageFromArray(arr[k].astype(np.uint8))
            arr[k] = sitk.GetArrayFromImage(sitk.BinaryFillhole(sl)) > 0
    return arr


def _multiscale_demons(
    fixed: sitk.Image,
    moving: sitk.Image,
    *,
    iterations: tuple[int, ...] = (60, 40, 20),
    smoothing_sigma_mm: float = 2.0,
    initial_field: Optional[sitk.Image] = None,
) -> sitk.Image:
    """Diffeomorphic demons over a shrink pyramid."""
    demons = sitk.DiffeomorphicDemonsRegistrationFilter()
    demons.SetSmoothDisplacementField(True)
    demons.SetStandardDeviations(smoothing_sigma_mm)

    shrinks = [2 ** (len(iterations) - 1 - lvl) for lvl in range(len(iterations))]
    field: Optional[sitk.Image] = (
        sitk.Cast(initial_field, sitk.sitkVectorFloat64)
        if initial_field is not None
        else None
    )
    for lvl, (shrink, n_it) in enumerate(zip(shrinks, iterations)):
        if shrink > 1:
            f = sitk.Shrink(fixed, [shrink] * 3)
            m = sitk.Shrink(moving, [shrink] * 3)
        else:
            f, m = fixed, moving
        demons.SetNumberOfIterations(int(n_it))
        if field is None:
            field = demons.Execute(f, m)
        else:
            up = sitk.Resample(
                field, f, sitk.Transform(), sitk.sitkLinear, 0.0,
                sitk.sitkVectorFloat64,
            )
            field = demons.Execute(f, m, up)
    assert field is not None
    if field.GetSize() != fixed.GetSize():
        field = sitk.Resample(
            field, fixed, sitk.Transform(), sitk.sitkLinear, 0.0,
            sitk.sitkVectorFloat64,
        )
    return field


def _bspline_lcc_field(
    fixed: sitk.Image,
    moving: sitk.Image,
    *,
    grid_spacing_mm: float = 30.0,
    iterations: tuple[int, ...] = (60, 40, 20),
    lcc_radius_vox: int = 3,
    initial_field: Optional[sitk.Image] = None,
) -> sitk.Image:
    """Multi-resolution B-spline DIR driven by ANTS local correlation."""
    reg = sitk.ImageRegistrationMethod()
    reg.SetMetricAsANTSNeighborhoodCorrelation(int(lcc_radius_vox))
    reg.SetMetricSamplingStrategy(reg.RANDOM)
    reg.SetMetricSamplingPercentage(0.20, seed=1234)
    reg.SetInterpolator(sitk.sitkLinear)

    size = fixed.GetSize()
    sp = fixed.GetSpacing()
    mesh = [max(1, int(round(size[i] * sp[i] / grid_spacing_mm))) for i in range(3)]
    tx = sitk.BSplineTransformInitializer(fixed, mesh, order=3)
    reg.SetInitialTransform(tx, inPlace=True)
    if initial_field is not None:
        reg.SetMovingInitialTransform(
            sitk.DisplacementFieldTransform(sitk.Image(initial_field))
        )

    n_lvl = len(iterations)
    reg.SetShrinkFactorsPerLevel([2 ** (n_lvl - 1 - i) for i in range(n_lvl)])
    reg.SetSmoothingSigmasPerLevel([float(n_lvl - 1 - i) for i in range(n_lvl)])
    reg.SmoothingSigmasAreSpecifiedInPhysicalUnitsOff()
    reg.SetOptimizerAsGradientDescent(
        learningRate=1.0,
        numberOfIterations=int(max(iterations)),
        convergenceMinimumValue=1e-6,
        convergenceWindowSize=10,
        estimateLearningRate=reg.EachIteration,
    )
    reg.SetOptimizerScalesFromPhysicalShift()
    final = reg.Execute(fixed, moving)

    composite = sitk.CompositeTransform(3)
    if initial_field is not None:
        composite.AddTransform(sitk.DisplacementFieldTransform(sitk.Image(initial_field)))
    composite.AddTransform(final)
    return sitk.TransformToDisplacementField(
        composite, sitk.sitkVectorFloat64, fixed.GetSize(),
        fixed.GetOrigin(), fixed.GetSpacing(), fixed.GetDirection(),
    )


def build_virtual_ct(
    planning_ct_kji: np.ndarray,
    cbct_on_ct_kji: np.ndarray,
    geometry: VolumeGeometry,
    *,
    iterations: tuple[int, ...] = (60, 40, 20),
    smoothing_sigma_mm: float = 2.0,
    cbct_valid_threshold_hu: float = -900.0,
    fov_blend_sigma_mm: float = 8.0,
    background_hu: float = -1000.0,
    dir_shrink: int = 2,
    mask_hardware: bool = True,
    drive_mask_source: str = "cbct_auto",
    drive_margin_mm: float = 10.0,
    dir_method: str = "demons",
    bspline_grid_mm: float = 30.0,
    clip_to_external: bool = True,
    harden_skin_edge: bool = True,
    skin_edge_hu: float = 0.0,
) -> dict:
    """Deform planning CT HU onto CBCT anatomy on the shared grid."""
    ct = _to_sitk(planning_ct_kji.astype(np.float32), geometry)
    cb = _to_sitk(cbct_on_ct_kji.astype(np.float32), geometry)

    ct_drive, cb_drive = ct, cb
    drive_np: Optional[np.ndarray] = None

    if mask_hardware or drive_mask_source == "cbct_auto":
        try:
            if drive_mask_source == "cbct_auto":
                drive_np = cbct_external_mask(
                    cbct_on_ct_kji,
                    tuple(float(v) for v in geometry.spacing_mm),
                )
                drive = sitk.GetImageFromArray(drive_np.astype(np.uint8))
                drive.CopyInformation(ct)
            else:
                ct_body = _body_mask(ct)
                cb_body = _body_mask(cb)
                drive = sitk.Or(ct_body, cb_body)
                drive_np = sitk.GetArrayFromImage(drive) > 0

            drive_f = sitk.Cast(drive, sitk.sitkFloat32)
            air = float(background_hu)
            ct_drive = ct * drive_f + air * (1.0 - drive_f)
            cb_drive = cb * drive_f + air * (1.0 - drive_f)
        except Exception:
            ct_drive, cb_drive = ct, cb

    if harden_skin_edge and drive_np is not None:
        try:
            air = float(background_hu)
            drv = sitk.GetImageFromArray(drive_np.astype(np.uint8))
            drv.CopyInformation(cb)
            drv_f = sitk.Cast(drv, sitk.sitkFloat32)
            edge_cb = sitk.Cast(drv - sitk.BinaryErode(drv, [2, 2, 1]), sitk.sitkFloat32)
            cb_drive = cb_drive * drv_f + air * (1.0 - drv_f)
            cb_drive = cb_drive * (1.0 - edge_cb) + float(skin_edge_hu) * edge_cb
        except Exception:
            pass

    valid = sitk.Cast(cb_drive > float(cbct_valid_threshold_hu), sitk.sitkFloat32)
    valid = sitk.SmoothingRecursiveGaussian(valid, float(fov_blend_sigma_mm))

    cb_hm = sitk.HistogramMatching(
        cb_drive, ct_drive, numberOfHistogramLevels=256,
        numberOfMatchPoints=16, thresholdAtMeanIntensity=True,
    )

    s = max(1, int(dir_shrink))
    if s > 1:
        ct_reg = sitk.Shrink(ct_drive, [s] * 3)
        cb_reg = sitk.Shrink(cb_hm, [s] * 3)
    else:
        ct_reg, cb_reg = ct_drive, cb_hm

    if dir_method == "bspline_lcc":
        cb_reg_lcc = sitk.Shrink(cb_drive, [s] * 3) if s > 1 else cb_drive
        field = _bspline_lcc_field(
            cb_reg_lcc, ct_reg, grid_spacing_mm=bspline_grid_mm,
            iterations=iterations,
        )
    else:
        field = _multiscale_demons(
            cb_reg, ct_reg, iterations=iterations,
            smoothing_sigma_mm=smoothing_sigma_mm,
        )

    if s > 1:
        field = sitk.Resample(
            field, cb, sitk.Transform(), sitk.sitkLinear, 0.0,
            sitk.sitkVectorFloat64,
        )

    # Blend field to identity outside CBCT valid region
    valid = sitk.Cast(valid, sitk.sitkFloat64)
    fx = sitk.VectorIndexSelectionCast(field, 0) * valid
    fy = sitk.VectorIndexSelectionCast(field, 1) * valid
    fz = sitk.VectorIndexSelectionCast(field, 2) * valid
    field = sitk.Compose(fx, fy, fz)

    tx = sitk.DisplacementFieldTransform(sitk.Image(field))
    sct_img = sitk.Resample(
        ct, cb, tx, sitk.sitkLinear, float(background_hu), sitk.sitkFloat32
    )

    mask_np = sitk.GetArrayFromImage(valid) > 0.5
    cb_np = sitk.GetArrayFromImage(cb_hm)
    before = float(np.mean(np.abs(sitk.GetArrayFromImage(ct_drive)[mask_np] - cb_np[mask_np]))) if mask_np.any() else 0.0
    sct_np = sitk.GetArrayFromImage(sct_img)
    after = float(np.mean(np.abs(sct_np[mask_np] - cb_np[mask_np]))) if mask_np.any() else 0.0

    mag = sitk.GetArrayFromImage(sitk.VectorMagnitude(field))
    sct_out = np.rint(sct_np)

    if clip_to_external and drive_np is not None:
        try:
            ct_body_np = sitk.GetArrayFromImage(_body_mask(ct)) > 0
        except Exception:
            ct_body_np = sct_out > float(background_hu) + 300

        slice_in = drive_np.reshape(drive_np.shape[0], -1).any(axis=1)
        nk_ = drive_np.shape[0]
        core_k = slice_in.copy()
        ramp_k = np.zeros(nk_, dtype=bool)
        idx = np.where(slice_in)[0]
        ramp_n = 3
        if idx.size:
            k0, k1 = int(idx.min()), int(idx.max())
            for kk_ in range(max(0, k0 - ramp_n), min(nk_, k0 + ramp_n)):
                ramp_k[kk_] = True
                core_k[kk_] = False
            for kk_ in range(max(0, k1 - ramp_n + 1), min(nk_, k1 + ramp_n + 1)):
                ramp_k[kk_] = True
                core_k[kk_] = False

        core3 = np.broadcast_to(core_k[:, None, None], drive_np.shape)
        ramp3 = np.broadcast_to(ramp_k[:, None, None], drive_np.shape)
        out3 = np.broadcast_to((~(core_k | ramp_k))[:, None, None], drive_np.shape)

        full_external = (
            (core3 & drive_np)
            | (ramp3 & (drive_np | ct_body_np))
            | (out3 & ct_body_np)
        )
        outside = (~full_external) & (core3 | ramp3) & (sct_out > float(background_hu) + 50)
        sct_out[outside] = float(background_hu)

    return {
        "sct": np.clip(sct_out, -1024, 3071).astype(np.int16),
        "mae_hu_before": before,
        "mae_hu_after": after,
        "displacement_mm_mean": float(mag[mask_np].mean()) if mask_np.any() else 0.0,
        "displacement_mm_p99": float(np.percentile(mag[mask_np], 99)) if mask_np.any() else 0.0,
        "fov_fraction": float(mask_np.mean()),
        "field": field,
    }


def deform_mask(mask_kji: np.ndarray, geometry: VolumeGeometry, field: sitk.Image) -> np.ndarray:
    """Deform a 3D binary ROI mask using the DIR displacement field."""
    mask_sitk = _to_sitk(mask_kji.astype(np.uint8), geometry)
    tx = sitk.DisplacementFieldTransform(sitk.Image(field))
    ref = sitk.Image(field)
    deformed = sitk.Resample(mask_sitk, ref, tx, sitk.sitkNearestNeighbor, 0, sitk.sitkUInt8)
    return sitk.GetArrayFromImage(deformed) > 0


def propagate_rois_through_dir(
    rois: list[dict[str, Any]],
    geometry: VolumeGeometry,
    field: sitk.Image,
) -> list[dict[str, Any]]:
    """
    Deforms a list of planning ROIs (from RTSTRUCT) onto the synthetic CT anatomy
    using the DIR displacement field.
    Returns the list of ROIs with both 'mask' (deformed) and 'planned_mask' (original),
    along with 'planned_volume_cc' and 'deformed_volume_cc'.
    """
    tx = sitk.DisplacementFieldTransform(sitk.Image(field))
    ref = sitk.Image(field)
    sx, sy, sz = geometry.spacing_mm
    voxel_vol_cc = (float(sx) * float(sy) * float(sz)) / 1000.0

    propagated = []
    for r in rois:
        orig_mask = r["mask"]
        orig_vol_cc = r.get("volume_cc", round(float(orig_mask.sum()) * voxel_vol_cc, 2))
        mask_sitk = _to_sitk(orig_mask.astype(np.uint8), geometry)
        deformed_sitk = sitk.Resample(mask_sitk, ref, tx, sitk.sitkNearestNeighbor, 0, sitk.sitkUInt8)
        deformed_mask = sitk.GetArrayFromImage(deformed_sitk) > 0
        deformed_vol_cc = round(float(deformed_mask.sum()) * voxel_vol_cc, 2)

        propagated.append({
            "roi_number": r["roi_number"],
            "name": r["name"],
            "type": r["type"],
            "is_target": r["is_target"],
            "color": r["color"],
            "mask": deformed_mask,
            "planned_mask": orig_mask,
            "volume_cc": deformed_vol_cc,
            "deformed_volume_cc": deformed_vol_cc,
            "planned_volume_cc": orig_vol_cc,
        })
    return propagated


def export_ct_series(
    sct_kji: np.ndarray,
    geometry: VolumeGeometry,
    out_dir: str,
    *,
    ref_meta: Optional[dict] = None,
    series_description: str = "SyntheticQACT Virtual CT",
    patient_position: str = "HFS",
) -> dict:
    """Write an axial DICOM CT series sharing Study and FrameOfReference UIDs."""
    meta = ref_meta or {}
    os.makedirs(out_dir, exist_ok=True)

    arr = np.asarray(sct_kji, dtype=np.int16)
    nk, nj, ni = arr.shape
    sx, sy, sz = (float(s) for s in geometry.spacing_mm)
    origin, axes = _axes_from_geometry(geometry)
    row_dir = axes[:, 0]
    col_dir = axes[:, 1]
    k_dir = axes[:, 2]

    now = datetime.datetime.now()
    date, time = now.strftime("%Y%m%d"), now.strftime("%H%M%S")
    series_uid = generate_uid()
    study_uid = meta.get("study_instance_uid") or generate_uid()
    for_uid = meta.get("frame_of_reference_uid") or generate_uid()

    intercept, slope = -1024, 1
    stored = (arr.astype(np.int32) - intercept).astype(np.uint16)

    written: list[str] = []
    sop_uids: list[str] = []

    for k in range(nk):
        ds = Dataset()
        fm = FileMetaDataset()
        fm.MediaStorageSOPClassUID = CTImageStorage
        fm.MediaStorageSOPInstanceUID = generate_uid()
        fm.TransferSyntaxUID = ExplicitVRLittleEndian
        ds.file_meta = fm

        ds.SpecificCharacterSet = "ISO_IR 100"
        ds.SOPClassUID = CTImageStorage
        ds.SOPInstanceUID = fm.MediaStorageSOPInstanceUID
        ds.Modality = "CT"
        ds.ImageType = ["DERIVED", "SECONDARY", "AXIAL"]
        ds.SeriesDescription = series_description
        ds.StudyDate = meta.get("study_date", date)
        ds.SeriesDate = date
        ds.AcquisitionDate = date
        ds.ContentDate = date
        ds.StudyTime = meta.get("study_time", time)
        ds.SeriesTime = time
        ds.ContentTime = time
        ds.AccessionNumber = meta.get("accession_number", "")
        ds.Manufacturer = "Virtual PSQA SyntheticQACT"
        ds.StationName = "sCT-generator"

        ds.PatientName = meta.get("patient_name", "")
        ds.PatientID = meta.get("patient_id", "")
        ds.PatientBirthDate = meta.get("patient_birth_date", "")
        ds.PatientSex = meta.get("patient_sex", "")

        ds.StudyInstanceUID = study_uid
        ds.SeriesInstanceUID = series_uid
        ds.StudyID = meta.get("study_id", "")
        ds.SeriesNumber = 990
        ds.InstanceNumber = k + 1
        ds.FrameOfReferenceUID = for_uid
        ds.PositionReferenceIndicator = ""
        ds.PatientPosition = patient_position

        ipp = np.asarray(origin) + k * sz * k_dir
        ds.ImagePositionPatient = [f"{v:.6f}" for v in ipp]
        ds.ImageOrientationPatient = [f"{v:.6f}" for v in (*row_dir, *col_dir)]
        ds.SliceThickness = f"{sz:.6f}"
        ds.SliceLocation = f"{float(ipp[2]):.6f}"
        ds.PixelSpacing = [f"{sy:.6f}", f"{sx:.6f}"]
        ds.KVP = "120"

        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.Rows = nj
        ds.Columns = ni
        ds.BitsAllocated = 16
        ds.BitsStored = 16
        ds.HighBit = 15
        ds.PixelRepresentation = 0
        ds.RescaleIntercept = str(intercept)
        ds.RescaleSlope = str(slope)
        ds.RescaleType = "HU"
        ds.WindowCenter = "40"
        ds.WindowWidth = "400"
        ds.PixelData = np.ascontiguousarray(stored[k]).tobytes()

        path = os.path.join(out_dir, f"sct_{k + 1:04d}.dcm")
        ds.save_as(path, enforce_file_format=True)
        written.append(path)
        sop_uids.append(str(ds.SOPInstanceUID))

    return {
        "files_written": len(written),
        "sop_instance_uids": sop_uids,
        "output_dir": out_dir,
        "series_instance_uid": series_uid,
        "study_instance_uid": study_uid,
        "frame_of_reference_uid": for_uid,
    }
