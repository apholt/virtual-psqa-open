"""DICOM CT and CBCT series scanner, sorter, and volume loader."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

import numpy as np
import pydicom
from pydicom.errors import InvalidDicomError

from .volume_geometry import VolumeGeometry

CT_IMAGE_STORAGE = "1.2.840.10008.5.1.4.1.1.2"


@dataclass
class SliceRecord:
    path: str
    sop_instance_uid: str
    ipp: tuple[float, float, float]
    iop: tuple[float, ...]
    rows: int
    columns: int
    pixel_spacing: tuple[float, float]
    slice_thickness: float | None
    instance_number: int | None


@dataclass
class SeriesRecord:
    series_instance_uid: str
    directory: str
    slices: list[SliceRecord] = field(default_factory=list)
    first_dataset_path: str = ""
    unsupported_reason: str | None = None


def _read_header(path: str) -> Optional[pydicom.Dataset]:
    try:
        return pydicom.dcmread(path, stop_before_pixels=True, force=True)
    except (InvalidDicomError, Exception):
        return None


def slice_normal(iop: tuple[float, ...]) -> np.ndarray:
    """Slice normal = row_cosines x col_cosines (LPS)."""
    row = np.asarray(iop[0:3], dtype=float)
    col = np.asarray(iop[3:6], dtype=float)
    return np.cross(row, col)


def sort_slices_by_position(slices: list[SliceRecord]) -> list[SliceRecord]:
    """Sort slices along normal (ascending) based on ImagePositionPatient."""
    if not slices:
        return []
    normal = slice_normal(slices[0].iop)
    return sorted(slices, key=lambda s: float(np.dot(np.asarray(s.ipp), normal)))


def scan_dicom_slices(source_paths: list[Union[str, Path]]) -> list[SeriesRecord]:
    """Scan paths / files and group valid CT/CBCT slices by SeriesInstanceUID."""
    series_map: dict[str, SeriesRecord] = {}
    expanded: list[Path] = []
    for sp in source_paths:
        p = Path(sp)
        if p.is_dir():
            for root, _, files in os.walk(str(p)):
                for f in files:
                    if f.lower().endswith(".dcm") or not "." in f:
                        expanded.append(Path(root) / f)
        elif p.is_file():
            expanded.append(p)

    for fp in expanded:
        ds = _read_header(str(fp))
        if ds is None:
            continue
        modality = str(getattr(ds, "Modality", "")).upper()
        if modality != "CT":
            continue

        uid = str(getattr(ds, "SeriesInstanceUID", "")) or "UNKNOWN"
        rec = series_map.setdefault(
            uid, SeriesRecord(series_instance_uid=uid, directory=str(fp.parent))
        )
        try:
            rec.slices.append(
                SliceRecord(
                    path=str(fp),
                    sop_instance_uid=str(ds.SOPInstanceUID),
                    ipp=tuple(float(v) for v in ds.ImagePositionPatient),
                    iop=tuple(float(v) for v in ds.ImageOrientationPatient),
                    rows=int(ds.Rows),
                    columns=int(ds.Columns),
                    pixel_spacing=(float(ds.PixelSpacing[0]), float(ds.PixelSpacing[1])),
                    slice_thickness=float(ds.SliceThickness) if getattr(ds, "SliceThickness", None) is not None else None,
                    instance_number=int(ds.InstanceNumber) if getattr(ds, "InstanceNumber", None) is not None else None,
                )
            )
            if not rec.first_dataset_path:
                rec.first_dataset_path = str(fp)
        except Exception as exc:
            rec.unsupported_reason = f"Missing geometry tags in {fp.name}: {exc}"

    return list(series_map.values())


def load_series_volume(rec: SeriesRecord) -> tuple[np.ndarray, VolumeGeometry, dict]:
    """
    Load a SeriesRecord into an int16 numpy array [k, j, i] with LPS geometry
    and metadata dictionary.
    """
    if rec.unsupported_reason:
        raise ValueError(rec.unsupported_reason)
    if len(rec.slices) < 1:
        raise ValueError(f"Series {rec.series_instance_uid} has no slices.")

    ordered = sort_slices_by_position(rec.slices)
    first = ordered[0]
    normal = slice_normal(first.iop)

    positions = np.array([float(np.dot(np.asarray(s.ipp), normal)) for s in ordered])
    gaps = np.diff(positions)
    slice_spacing = float(np.median(gaps)) if len(gaps) else 0.0

    ni, nj, nk = first.columns, first.rows, len(ordered)
    volume = np.empty((nk, nj, ni), dtype=np.int16)
    for k, s in enumerate(ordered):
        ds = pydicom.dcmread(s.path, force=True)
        pixels = ds.pixel_array.astype(np.float32)
        slope = float(getattr(ds, "RescaleSlope", 1.0) or 1.0)
        intercept = float(getattr(ds, "RescaleIntercept", 0.0) or 0.0)
        volume[k] = np.clip(pixels * slope + intercept, -32768, 32767).astype(np.int16)

    row_cos = np.asarray(first.iop[0:3])
    col_cos = np.asarray(first.iop[3:6])
    direction = np.column_stack([row_cos, col_cos, normal]).flatten()

    geometry = VolumeGeometry(
        origin_lps=tuple(first.ipp),
        spacing_mm=(
            first.pixel_spacing[1],
            first.pixel_spacing[0],
            abs(slice_spacing) if slice_spacing else (first.slice_thickness or 1.0),
        ),
        size_voxels=(ni, nj, nk),
        direction=tuple(float(v) for v in direction),
    )

    ds0 = pydicom.dcmread(ordered[0].path, stop_before_pixels=True, force=True)
    meta = {
        "series_instance_uid": rec.series_instance_uid,
        "study_instance_uid": str(getattr(ds0, "StudyInstanceUID", "")),
        "frame_of_reference_uid": str(getattr(ds0, "FrameOfReferenceUID", "")),
        "patient_name": str(getattr(ds0, "PatientName", "")),
        "patient_id": str(getattr(ds0, "PatientID", "")),
        "patient_birth_date": str(getattr(ds0, "PatientBirthDate", "")),
        "patient_sex": str(getattr(ds0, "PatientSex", "")),
        "study_date": str(getattr(ds0, "StudyDate", "")),
        "study_time": str(getattr(ds0, "StudyTime", "")),
        "study_id": str(getattr(ds0, "StudyID", "")),
        "accession_number": str(getattr(ds0, "AccessionNumber", "")),
        "patient_position": str(getattr(ds0, "PatientPosition", "HFS")),
        "num_slices": nk,
        "slice_thickness": first.slice_thickness,
        "pixel_spacing": first.pixel_spacing,
    }
    return volume, geometry, meta


def find_planning_ct_series(dicom_store_path: Union[str, Path]) -> tuple[np.ndarray, VolumeGeometry, dict]:
    """Locate and load the planning CT series in the given plan DICOM store."""
    store = Path(dicom_store_path)
    if not store.is_dir():
        raise FileNotFoundError(f"DICOM store directory not found: {store}")

    series_list = scan_dicom_slices([store])
    if not series_list:
        raise FileNotFoundError(
            f"No planning CT image series found in {store}. Ensure the planning CT "
            f"DICOM slices are present in the plan store."
        )

    # Pick the series with the most slices (in case there are scouts/surveys)
    best_series = max(series_list, key=lambda s: len(s.slices))
    return load_series_volume(best_series)


def load_cbct_series(source_paths: list[Union[str, Path]]) -> tuple[np.ndarray, VolumeGeometry, dict]:
    """Scan and load a single CBCT series from the given paths."""
    series_list = scan_dicom_slices(source_paths)
    if not series_list:
        raise FileNotFoundError("No valid DICOM CT/CBCT slices found in input files.")

    def series_sort_key(s: SeriesRecord):
        acq_time = ""
        if s.first_dataset_path:
            try:
                ds = _read_header(s.first_dataset_path)
                acq_time = str(getattr(ds, "AcquisitionTime", getattr(ds, "SeriesTime", "")))
            except Exception:
                pass
        return (len(s.slices), acq_time)

    best_series = max(series_list, key=series_sort_key)
    return load_series_volume(best_series)
