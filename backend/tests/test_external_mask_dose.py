import tempfile
from pathlib import Path
from unittest.mock import MagicMock
import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence

from services.dose_grid import DoseGrid
from services.gamma_analysis import (
    _external_mask,
    _resample_cached,
    load_plan_doses,
    _EXTERNAL_MASK_CACHE,
    _RESAMPLE_CACHE,
)
from models.plan import Plan


def create_rtstruct_with_roi(
    dir_path: Path,
    roi_name: str = "External",
    interpreted_type: str = "EXTERNAL",
) -> Path:
    file_path = dir_path / "rtstruct.dcm"
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.481.3"
    file_meta.MediaStorageSOPInstanceUID = f"1.2.826.0.1.3680043.9.7243.rtstruct.{roi_name}"
    file_meta.TransferSyntaxUID = pydicom.uid.ExplicitVRLittleEndian

    ds = pydicom.dataset.FileDataset(str(file_path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.Modality = "RTSTRUCT"
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.481.3"
    ds.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID

    roi = Dataset()
    roi.ROINumber = 1
    roi.ROIName = roi_name
    ds.StructureSetROISequence = Sequence([roi])

    if interpreted_type:
        obs = Dataset()
        obs.ObservationNumber = 1
        obs.ReferencedROINumber = 1
        obs.RTROIInterpretedType = interpreted_type
        ds.RTROIObservationsSequence = Sequence([obs])
    else:
        ds.RTROIObservationsSequence = Sequence([])

    rc = Dataset()
    rc.ReferencedROINumber = 1
    rc.ROIDisplayColor = [0, 255, 0]

    # A square contour on slice z=10.0 mm from x=10..30, y=10..30
    s = Dataset()
    s.ContourGeometricType = "CLOSED_PLANAR"
    s.NumberOfContourPoints = 4
    s.ContourData = [
        10.0, 10.0, 10.0,
        30.0, 10.0, 10.0,
        30.0, 30.0, 10.0,
        10.0, 30.0, 10.0,
    ]
    rc.ContourSequence = Sequence([s])
    ds.ROIContourSequence = Sequence([rc])

    ds.save_as(str(file_path))
    return file_path


def test_external_mask_interpreted_type():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        create_rtstruct_with_roi(tmp_path, roi_name="Skin", interpreted_type="EXTERNAL")

        target = DoseGrid(
            array=np.zeros((5, 50, 50), dtype=np.float32),
            spacing=(2.0, 1.0, 1.0),
            origin=(6.0, 0.0, 0.0),
        )
        mask = _external_mask(target, str(tmp_path))
        assert mask is not None
        assert mask.shape == (5, 50, 50)
        # z=10.0 mm corresponds to index (10.0 - 6.0)/2.0 = 2
        assert mask[2, 20, 20] == True
        assert mask[2, 0, 0] == False
        assert mask[0, 20, 20] == False
        assert mask[4, 20, 20] == False


def test_external_mask_body_name_fallback():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        create_rtstruct_with_roi(tmp_path, roi_name="BODY", interpreted_type="")

        target = DoseGrid(
            array=np.zeros((5, 50, 50), dtype=np.float32),
            spacing=(2.0, 1.0, 1.0),
            origin=(6.0, 0.0, 0.0),
        )
        mask = _external_mask(target, str(tmp_path))
        assert mask is not None
        assert mask[2, 20, 20] == True
        assert mask[2, 5, 5] == False


def test_resample_cached_with_mask():
    _RESAMPLE_CACHE.clear()
    src = DoseGrid(
        array=np.full((5, 50, 50), 10.0, dtype=np.float32),
        spacing=(2.0, 1.0, 1.0),
        origin=(6.0, 0.0, 0.0),
    )
    target = DoseGrid(
        array=np.zeros((5, 50, 50), dtype=np.float32),
        spacing=(2.0, 1.0, 1.0),
        origin=(6.0, 0.0, 0.0),
    )
    ext_mask = np.zeros((5, 50, 50), dtype=bool)
    ext_mask[2, 15:25, 15:25] = True

    with tempfile.NamedTemporaryFile(suffix=".npz") as f:
        src.save(f.name)
        # 1. Unmasked call
        res_unmasked = _resample_cached(f.name, src, target, ext_mask=None)
        assert np.isclose(res_unmasked.array[2, 20, 20], 10.0)
        assert np.isclose(res_unmasked.array[2, 0, 0], 10.0)

        # 2. Masked call
        res_masked = _resample_cached(f.name, src, target, ext_mask=ext_mask)
        assert np.isclose(res_masked.array[2, 20, 20], 10.0)
        assert np.isclose(res_masked.array[2, 0, 0], 0.0)
        assert np.isclose(res_masked.array[0, 20, 20], 0.0)


def test_load_plan_doses_masks_mcsquare():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        create_rtstruct_with_roi(tmp_path, roi_name="External", interpreted_type="EXTERNAL")

        tps_grid = DoseGrid(
            array=np.zeros((5, 50, 50), dtype=np.float32),
            spacing=(2.0, 1.0, 1.0),
            origin=(6.0, 0.0, 0.0),
        )
        tps_grid.array[2, 15:25, 15:25] = 2.0  # TPS dose inside body

        mc_grid = DoseGrid(
            array=np.full((5, 50, 50), 2.0, dtype=np.float32),  # MC dose everywhere (including couch/air)
            spacing=(2.0, 1.0, 1.0),
            origin=(6.0, 0.0, 0.0),
        )
        mc_file = tmp_path / "mc_dose.npz"
        mc_grid.save(mc_file)

        plan = MagicMock(spec=Plan)
        plan.id = 999
        plan.dicom_store_path = str(tmp_path)
        plan.rtplan_uid = "1.2.3"

        db = MagicMock()
        db.query().filter_by().first.return_value = plan

        from services import gamma_analysis
        orig_scan_store = gamma_analysis._scan_store
        orig_latest = gamma_analysis._latest_job_result
        orig_cached_load = gamma_analysis.cached_load

        try:
            gamma_analysis._scan_store = MagicMock(return_value=(str(tmp_path / "tps.dcm"), {}))
            gamma_analysis._latest_job_result = MagicMock(return_value=str(mc_file))
            def fake_load(path):
                if "tps" in path:
                    return tps_grid
                return mc_grid
            gamma_analysis.cached_load = fake_load

            doses = load_plan_doses(999, db)
            assert "mcSquare" in doses
            mc_loaded = doses["mcSquare"]

            # Dose inside external contour should be non-zero
            assert mc_loaded.array[2, 20, 20] > 0.0
            # Dose outside external contour should be 0.0!
            assert mc_loaded.array[2, 0, 0] == 0.0
            assert mc_loaded.array[0, 20, 20] == 0.0
        finally:
            gamma_analysis._scan_store = orig_scan_store
            gamma_analysis._latest_job_result = orig_latest
            gamma_analysis.cached_load = orig_cached_load
