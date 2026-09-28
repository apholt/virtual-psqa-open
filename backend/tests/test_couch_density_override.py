"""
test_couch_density_override.py — Tests for continuous couch density calibration,
foam core modeling, skin guard, and check_table_dilation diagnostics.
"""

from pathlib import Path
import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence

from ct_density_override import (
    couch_density_to_hu,
    foam_density_to_hu,
    apply_density_overrides,
    _structure_physical_properties,
    _structure_material_overrides,
    AIR_HU,
)
import ct_density_override


def test_couch_density_to_hu_interpolation():
    cal_file = Path("MCsquare/Scanners/default/HU_Density_Conversion.txt")
    if not cal_file.exists():
        return
    cal = np.loadtxt(str(cal_file), float)
    hu_cal, dens_cal = cal[:, 0], cal[:, 1]

    mat_file = Path("MCsquare/Scanners/default/HU_Material_Conversion.txt")
    mat_cal = np.loadtxt(str(mat_file), float)
    hu_mat, mat_id = mat_cal[:, 0], mat_cal[:, 1].astype(int)

    test_densities = [1.20, 1.50, 1.80, 2.03, 2.10, 2.15, 2.20, 2.35]
    for d in test_densities:
        hu = couch_density_to_hu(d)
        interp_d = float(np.interp(hu, hu_cal, dens_cal))
        assert abs(interp_d - d) < 1e-4, f"Density mismatch for {d}: got {interp_d}"

        # Verify material label is 67 (CFRP)
        assigned_mat = mat_id[0]
        for h, m in zip(hu_mat, mat_id):
            if hu >= h:
                assigned_mat = m
        assert assigned_mat == 67, f"Material for HU {hu} is {assigned_mat}, expected 67 (CFRP)"


def test_foam_density_to_hu_interpolation():
    cal_file = Path("MCsquare/Scanners/default/HU_Density_Conversion.txt")
    if not cal_file.exists():
        return
    cal = np.loadtxt(str(cal_file), float)
    hu_cal, dens_cal = cal[:, 0], cal[:, 1]

    test_foams = [0.0012, 0.03, 0.05, 0.08, 0.10, 0.15, 0.20]
    for d in test_foams:
        hu = foam_density_to_hu(d)
        interp_d = float(np.interp(hu, hu_cal, dens_cal))
        assert abs(interp_d - d) < 1e-4, f"Foam density mismatch for {d}: got {interp_d}"


def _create_mock_rtstruct(tmp_path: Path, has_foam_core: bool = False) -> Path:
    ds = Dataset()
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.481.3"
    file_meta.MediaStorageSOPInstanceUID = "1.2.3.4.5"
    file_meta.TransferSyntaxUID = pydicom.uid.ExplicitVRLittleEndian
    ds.file_meta = file_meta

    ds.SOPClassUID = file_meta.MediaStorageSOPClassUID
    ds.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
    ds.Modality = "RTSTRUCT"

    # StructureSetROISequence
    s1 = Dataset()
    s1.ROINumber = 1
    s1.ROIName = "External"
    s2 = Dataset()
    s2.ROINumber = 2
    s2.ROIName = "MedPhoton Couch (Shell)"
    s3 = Dataset()
    s3.ROINumber = 3
    s3.ROIName = "MedPhoton Couch (Core)"
    ds.StructureSetROISequence = Sequence([s1, s2, s3])

    # RTROIObservationsSequence
    obs1 = Dataset()
    obs1.ReferencedROINumber = 1
    obs1.RTROIInterpretedType = "EXTERNAL"

    obs2 = Dataset()
    obs2.ReferencedROINumber = 2
    obs2.RTROIInterpretedType = "SUPPORT"
    obs2.add_new((0x300A, 0x00E1), "SH", "Shell 2.03")

    obs3 = Dataset()
    obs3.ReferencedROINumber = 3
    obs3.RTROIInterpretedType = "SUPPORT"
    obs3.add_new((0x300A, 0x00E1), "SH", "Air")

    if has_foam_core:
        p_item = Dataset()
        p_item.ROIPhysicalProperty = "REL_MASS_DENSITY"
        p_item.ROIPhysicalPropertyValue = "0.05"
        obs3.ROIPhysicalPropertiesSequence = Sequence([p_item])

    ds.RTROIObservationsSequence = Sequence([obs1, obs2, obs3])

    # ROIContourSequence with small square contours
    rc1 = Dataset()
    rc1.ReferencedROINumber = 1
    cs1 = Dataset()
    cs1.ContourGeometricType = "CLOSED_PLANAR"
    cs1.NumberOfContourPoints = 4
    # External contour from (10, 10) to (30, 30) at z=0
    cs1.ContourData = [10, 10, 0, 30, 10, 0, 30, 30, 0, 10, 30, 0]
    rc1.ContourSequence = Sequence([cs1])

    rc2 = Dataset()
    rc2.ReferencedROINumber = 2
    cs2 = Dataset()
    cs2.ContourGeometricType = "CLOSED_PLANAR"
    cs2.NumberOfContourPoints = 4
    # Couch shell from (5, 5) to (35, 35) at z=0 (overlapping External edge)
    cs2.ContourData = [5, 5, 0, 35, 5, 0, 35, 35, 0, 5, 35, 0]
    rc2.ContourSequence = Sequence([cs2])

    rc3 = Dataset()
    rc3.ReferencedROINumber = 3
    cs3 = Dataset()
    cs3.ContourGeometricType = "CLOSED_PLANAR"
    cs3.NumberOfContourPoints = 4
    # Couch core from (12, 12) to (18, 18) at z=0
    cs3.ContourData = [12, 12, 0, 18, 12, 0, 18, 18, 0, 12, 18, 0]
    rc3.ContourSequence = Sequence([cs3])

    ds.ROIContourSequence = Sequence([rc1, rc2, rc3])

    path = tmp_path / "test_rs.dcm"
    ds.save_as(str(path), write_like_original=False)
    return path


class MockCT:
    def __init__(self, nx=40, ny=40, nz=1):
        self.GridSize = [nx, ny, nz]
        self.PixelSpacing = [1.0, 1.0, 1.0]
        self.ImagePositionPatient = [0.0, 0.0, 0.0]
        self.Image = np.zeros((nx, ny, nz), dtype=np.float32)


def test_skin_guard_and_density_override(tmp_path: Path):
    rs_path = _create_mock_rtstruct(tmp_path, has_foam_core=False)
    ct = MockCT()

    # Test with continuous density override and dilation with skin guard
    ct_density_override.COUCH_SHELL_DENSITY_OVERRIDE = 2.15
    ct_density_override.COUCH_WALL_DILATION_VOX = 2

    try:
        logs = []
        applied = apply_density_overrides(ct, str(rs_path), log=logs.append)
        assert applied >= 2

        # Verify target HU for 2.15 g/cm3 was painted
        expected_hu = couch_density_to_hu(2.15)
        # Check that couch shell voxels outside patient received expected_hu
        # Point (6, 6, 0) is inside couch shell but outside External
        assert ct.Image[6, 6, 0] == np.float32(expected_hu)

        # Check core voxel was painted AIR
        assert ct.Image[15, 15, 0] == np.float32(AIR_HU)

    finally:
        ct_density_override.COUCH_SHELL_DENSITY_OVERRIDE = None
        ct_density_override.COUCH_WALL_DILATION_VOX = 0


def test_foam_core_density_override(tmp_path: Path):
    rs_path = _create_mock_rtstruct(tmp_path, has_foam_core=True)
    ct = MockCT()

    ct_density_override.COUCH_SHELL_DENSITY_OVERRIDE = None
    ct_density_override.COUCH_WALL_DILATION_VOX = 0

    logs = []
    applied = apply_density_overrides(ct, str(rs_path), log=logs.append)
    assert applied >= 2

    # Core voxel should have foam HU, NOT -1000
    expected_foam_hu = foam_density_to_hu(0.05)
    assert ct.Image[15, 15, 0] == np.float32(expected_foam_hu)
    assert any("foam model" in l for l in logs)
