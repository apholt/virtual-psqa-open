import tempfile
from pathlib import Path
from unittest.mock import MagicMock
import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import generate_uid

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
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
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


def test_find_rtdose_file_skips_beam_doses():
    from dicom.rtdose_parser import find_rtdose_file

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        # Create an RTDOSE that is a BEAM dose
        file_path = tmp_path / "beam1_dose.dcm"
        file_meta = FileMetaDataset()
        file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.481.2"
        file_meta.MediaStorageSOPInstanceUID = generate_uid()
        file_meta.TransferSyntaxUID = pydicom.uid.ExplicitVRLittleEndian

        ds = pydicom.dataset.FileDataset(str(file_path), {}, file_meta=file_meta, preamble=b"\0" * 128)
        ds.Modality = "RTDOSE"
        ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.481.2"
        ds.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
        ds.DoseSummationType = "BEAM"

        ref_plan_uid = generate_uid()
        ref_plan = Dataset()
        ref_plan.ReferencedSOPInstanceUID = ref_plan_uid
        ref_fg = Dataset()
        ref_fg.ReferencedFractionGroupNumber = 1
        ref_beam = Dataset()
        ref_beam.ReferencedBeamNumber = 1
        ref_fg.ReferencedBeamSequence = Sequence([ref_beam])
        ref_plan.ReferencedFractionGroupSequence = Sequence([ref_fg])
        ds.ReferencedRTPlanSequence = Sequence([ref_plan])

        ds.save_as(str(file_path))

        # find_rtdose_file MUST NOT return this beam dose as the plan-level dose!
        plan_dose = find_rtdose_file(str(tmp_path), plan_uid=ref_plan_uid)
        assert plan_dose is None


def test_load_plan_doses_synthesizes_when_plan_dose_missing():
    from services.gamma_analysis import load_plan_doses, check_plan_dose_status

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        b1_grid = DoseGrid(
            array=np.full((5, 50, 50), 2.0, dtype=np.float32),
            spacing=(2.0, 1.0, 1.0),
            origin=(6.0, 0.0, 0.0),
        )
        b2_grid = DoseGrid(
            array=np.full((5, 50, 50), 3.0, dtype=np.float32),
            spacing=(2.0, 1.0, 1.0),
            origin=(6.0, 0.0, 0.0),
        )

        plan = MagicMock(spec=Plan)
        plan.id = 888
        plan.plan_label = "Test_2Field"
        plan.number_of_fields = 2
        plan.dicom_store_path = str(tmp_path)
        plan.rtplan_uid = "1.2.3.4"

        db = MagicMock()
        db.query().filter_by().first.return_value = plan

        from services import gamma_analysis
        orig_scan_store = gamma_analysis._scan_store
        orig_beam_names = gamma_analysis._beam_names_from_plan
        orig_cached_load = gamma_analysis.cached_load

        try:
            # rtdose_path is None (missing plan dose), but both beam doses exist!
            gamma_analysis._beam_names_from_plan = MagicMock(return_value={1: "Beam 1", 2: "Beam 2"})
            gamma_analysis._scan_store = MagicMock(return_value=(None, {1: str(tmp_path / "b1.dcm"), 2: str(tmp_path / "b2.dcm")}))
            def fake_load(path):
                if "b1" in path:
                    return b1_grid
                return b2_grid
            gamma_analysis.cached_load = fake_load

            doses = load_plan_doses(888, db)
            assert "tps" in doses
            assert np.isclose(doses["tps"].array[2, 20, 20], 5.0)  # 2.0 + 3.0 = 5.0

            # Check status
            status = check_plan_dose_status(888, db)
            assert status["has_plan_dose"] is False
            assert status["is_plan_dose_synthesized"] is True
            assert status["all_beams_present"] is True
            assert len(status["missing_beam_numbers"]) == 0
        finally:
            gamma_analysis._scan_store = orig_scan_store
            gamma_analysis._beam_names_from_plan = orig_beam_names
            gamma_analysis.cached_load = orig_cached_load


def test_dose_status_and_upload_endpoints():
    import uuid
    from fastapi.testclient import TestClient
    from main import app
    from config import settings
    from database import SessionLocal
    from models.patient import Patient
    from models.plan import Plan
    from services.auth_service import create_session_token

    token = create_session_token("admin")
    client = TestClient(app, cookies={settings.AUTH_SESSION_COOKIE: token})

    db = SessionLocal()
    uid_str = uuid.uuid4().hex[:8]
    p_id = f"P_DOSE_{uid_str}"
    plan_uid = generate_uid()

    with tempfile.TemporaryDirectory() as tmpdir:
        patient = Patient(patient_id=p_id, patient_name="Dose Test")
        db.add(patient)
        db.commit()
        db.refresh(patient)

        plan = Plan(
            patient_id=patient.id,
            plan_label=f"Plan_{uid_str}",
            plan_name="Plan Dose Test",
            number_of_fields=3,
            dicom_store_path=str(tmpdir),
            rtplan_uid=plan_uid,
            qa_status="pending",
        )
        db.add(plan)
        db.commit()
        db.refresh(plan)

        # 1. Check dose status before uploading any dose file
        resp = client.get(f"/api/plans/{plan.id}/dose-status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_plan_dose"] is False
        assert data["missing_plan_dose"] is True
        assert len(data["missing_beam_numbers"]) == 3

        # 2. Upload an RTDOSE file using the new upload-doses endpoint
        with tempfile.TemporaryDirectory() as upload_src_dir:
            dose_file_path = Path(upload_src_dir) / "source_dose.dcm"
            file_meta = FileMetaDataset()
            file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.481.2"
            file_meta.MediaStorageSOPInstanceUID = generate_uid()
            file_meta.TransferSyntaxUID = pydicom.uid.ExplicitVRLittleEndian

            ds = pydicom.dataset.FileDataset(str(dose_file_path), {}, file_meta=file_meta, preamble=b"\0" * 128)
            ds.Modality = "RTDOSE"
            ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.481.2"
            ds.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
            ds.DoseSummationType = "PLAN"
            ref_plan = Dataset()
            ref_plan.ReferencedSOPInstanceUID = plan_uid
            ds.ReferencedRTPlanSequence = Sequence([ref_plan])
            ds.save_as(str(dose_file_path))

            with open(dose_file_path, "rb") as f:
                upload_resp = client.post(
                    f"/api/plans/{plan.id}/upload-doses",
                    files={"files": ("plan_dose.dcm", f, "application/dicom")},
                    data={"recalculate": "false"},
                )
            assert upload_resp.status_code == 200
            up_data = upload_resp.json()
            assert up_data["success"] is True
            assert "plan_dose.dcm" in up_data["saved_files"]
            assert up_data["dose_status"]["has_plan_dose"] is True

        # Clean up db
        db.delete(plan)
        db.delete(patient)
        db.commit()
    db.close()
